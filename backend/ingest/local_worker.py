"""Local and self-hosted runner for the durable Turso ingestion queue."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import httpx

from config import settings
from database import assert_turso_schema, get_turso_store
from ingest.pipeline import process_one_queued_ingestion, recover_orphaned_runs, recover_stuck_repos

logger = logging.getLogger(__name__)

# Run the recovery sweep every N poll cycles to catch repositories left in an
# intermediate state by a prior crash. The condition is rare and cheap.
_RECOVERY_SWEEP_INTERVAL = 10
# The orphan sweep also runs on its own timer, so a stop whose worker died is
# finished promptly even while this worker is busy with a long job.
_RECOVERY_LOOP_SECONDS = 30.0
# Prune the shared embedding cache roughly hourly at the default poll rate.
_CACHE_PRUNE_INTERVAL = 1200
_EMBEDDING_CACHE_RETENTION_DAYS = 45


def keepalive_url() -> str | None:
    base = (settings.public_app_url or settings.render_external_url).strip().rstrip("/")
    if not settings.keepalive_during_ingestion or not base.startswith(("http://", "https://")):
        return None
    return f"{base}/api/health"


async def _keepalive_loop(url: str) -> None:
    """Ping the public URL so a free Render instance does not sleep mid-job."""
    interval = max(60, int(settings.keepalive_interval_seconds))
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            await asyncio.sleep(interval)
            try:
                await client.get(url, headers={"User-Agent": "codebase-intel-keepalive"})
            except httpx.HTTPError:
                logger.debug("Keep-alive ping failed", exc_info=True)


@contextlib.asynccontextmanager
async def keep_instance_awake():
    url = keepalive_url()
    task = asyncio.create_task(_keepalive_loop(url)) if url else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def prune_embedding_cache(store) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=_EMBEDDING_CACHE_RETENTION_DAYS)).isoformat()
    await store.execute("DELETE FROM embedding_cache WHERE updated_at < ?", [cutoff])


async def run_recovery_sweep(store) -> None:
    recovered = await recover_stuck_repos(store)
    if recovered:
        logger.info("Recovery sweep restored %d stuck repository(ies)", recovered)
    orphaned = await recover_orphaned_runs(store)
    if orphaned:
        logger.info("Recovery sweep finished %d orphaned indexing run(s)", orphaned)


async def _recovery_loop() -> None:
    while True:
        await asyncio.sleep(_RECOVERY_LOOP_SECONDS)
        try:
            await assert_turso_schema()
            await run_recovery_sweep(get_turso_store())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Background recovery sweep failed; retrying later", exc_info=True)


async def process_queue_forever():
    """Claim one queued job at a time without blocking FastAPI request handling."""
    logger.info("Local ingestion worker started; polling every %s seconds", settings.local_ingestion_poll_seconds)
    recovery = asyncio.create_task(_recovery_loop())
    try:
        await _process_queue_forever()
    finally:
        recovery.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recovery


async def _process_queue_forever():
    poll_count = 0
    failure_streak = 0

    while True:
        try:
            await assert_turso_schema()
            store = get_turso_store()
            poll_count += 1
            if poll_count == 1 or poll_count % _RECOVERY_SWEEP_INTERVAL == 0:
                await run_recovery_sweep(store)
            if poll_count % _CACHE_PRUNE_INTERVAL == 0:
                await prune_embedding_cache(store)
            async with keep_instance_awake():
                result = await process_one_queued_ingestion(store)
            failure_streak = 0
            # Continue immediately while work is available; otherwise avoid
            # repeatedly querying Turso when the queue is empty.
            delay = 0 if result.get("processed") else settings.local_ingestion_poll_seconds
        except asyncio.CancelledError:
            logger.info("Local ingestion worker stopped")
            raise
        except Exception:
            logger.exception("Local ingestion worker could not process the queue; retrying")
            failure_streak += 1
            delay = max(
                settings.local_ingestion_poll_seconds,
                min(30.0, 3.0 * (2 ** min(failure_streak - 1, 3))),
            )

        await asyncio.sleep(delay)
