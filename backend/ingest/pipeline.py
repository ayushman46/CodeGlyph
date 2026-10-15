"""Durable Turso-backed repository ingestion pipeline."""

import asyncio
import gc
import hashlib
import json
import os
import logging
import threading
import time
from array import array
from datetime import UTC, datetime, timedelta
from queue import Empty, Full, Queue
from uuid import uuid4

from config import settings
from database import Statement, assert_turso_schema, explain_database_error, get_turso_store
from ingest.chunker import STREAMING_FILE_THRESHOLD, chunk_file, iter_chunk_file
from ingest.cloner import (
    BUDGET_CATEGORY_LABELS,
    acquire_repository_source,
    file_category,
    cleanup_repo,
    estimate_chunks,
    get_file_selection_report,
    normalize_github_url,
    parse_github_ref,
    parse_github_repository,
    repository_name,
    repository_source_url,
    RepositoryCloneError,
    RepositoryValidationError,
    SourceFetchCancelled,
)
from ingest.dependencies import build_manifest_and_dependency_manifest
from ingest.embedder import EmbeddingUnavailableError, embed_chunks
from ingest.memory import pressure_level, rss_mb, wait_for_memory_headroom
from ingest.summarizer import build_kt_cache
from quota import RepositoryQuotaExceededError, quota_exceeded_message, repository_byte_budget

logger = logging.getLogger(__name__)

# Legacy ``repos.status`` values that mean a job may still be running. The
# public status is derived from ``repo_index_state.phase`` and the durable job
# row; see ``present_repository``.
ACTIVE_REPOSITORY_STATUSES = {"queued", "cloning", "chunking", "embedding", "summarizing"}
ACTIVE_JOB_STATUSES = ("queued", "processing")
INDEX_PHASES_IN_PROGRESS = {"queued", "cloning", "scanning", "manifesting", "chunking", "keyword", "embedding"}
# A processing job whose heartbeat is older than this is treated as orphaned
# (its worker restarted or crashed): it is requeued, and cancel/delete clean
# it up immediately. A side task heartbeats every JOB_WATCH_INTERVAL_SECONDS
# for the whole run (including the archive download), so a live worker is
# never mistaken for a dead one.
WORKER_LIVENESS_SECONDS = 90
JOB_WATCH_INTERVAL_SECONDS = 15.0
# How quickly a Stop reaches a run that is inside a blocking phase (the
# archive download): one cheap indexed read per poll.
JOB_STOP_POLL_SECONDS = 2.5
# repo_index_runs rows for a repository's *first* index carry this marker in
# front of the chunk prefix. Those rows stay visible (keyword search works
# while indexing), but they are not a published index: a stop, failure or
# crash discards them instead of presenting a partial index as Ready.
INITIAL_RUN_MARKER = "initial:"

# Background tasks must be strongly referenced or asyncio may garbage-collect
# them before they finish.
_background_tasks: set[asyncio.Task] = set()


class IngestionConflictError(RuntimeError):
    pass


class IngestionCancelledError(RuntimeError):
    pass


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def spawn_background(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _lease_is_stale(lease_time: str | None, threshold: datetime) -> bool:
    if not lease_time:
        return True
    try:
        return datetime.fromisoformat(str(lease_time).replace("Z", "+00:00")) < threshold
    except ValueError:
        return True


def worker_is_alive(job: dict | None) -> bool:
    """Return whether a processing job still has a live worker heartbeat."""
    if not job or job.get("status") != "processing":
        return False
    threshold = datetime.now(UTC) - timedelta(seconds=WORKER_LIVENESS_SECONDS)
    return not _lease_is_stale(job.get("heartbeat_at") or job.get("claimed_at"), threshold)


# ---------------------------------------------------------------------------
# Durable index state
# ---------------------------------------------------------------------------


async def update_index_state(store, repo_id: str, *, phase: str, keyword_files: int | None = None,
                             keyword_chunks: int | None = None, semantic_progress: int | None = None,
                             embedding_status: str | None = None, searchable_at: str | None = None,
                             semantic_ready_at: str | None = None) -> None:
    """Publish durable, user-visible ingestion phase without changing repos.status."""
    assignments = ["phase = ?", "updated_at = ?"]
    update_args: list[object] = [phase, timestamp()]
    for field, value in (("keyword_files", keyword_files), ("keyword_chunks", keyword_chunks),
                         ("semantic_progress", semantic_progress), ("embedding_status", embedding_status),
                         ("searchable_at", searchable_at), ("semantic_ready_at", semantic_ready_at)):
        if value is not None:
            assignments.append(f"{field} = ?")
            update_args.append(value)
    update_args.append(repo_id)
    result = await store.execute(
        "UPDATE repo_index_state SET " + ", ".join(assignments) + " WHERE repo_id = ?", update_args
    )
    if result.rows_affected:
        return
    await store.execute(
        "INSERT OR IGNORE INTO repo_index_state (repo_id, phase, keyword_files, keyword_chunks, semantic_progress, "
        "embedding_status, searchable_at, semantic_ready_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [repo_id, phase, keyword_files or 0, keyword_chunks or 0, semantic_progress or 0,
         embedding_status or "pending", searchable_at, semantic_ready_at, timestamp()],
    )


async def publish_index_progress(store, repo_id: str, *, keyword_files: int | None = None,
                                 embedding_status: str | None = None, allow_ready: bool = False,
                                 expected_total: int | None = None, staging_prefix: str | None = None,
                                 phase_override: str | None = None, skipped: int = 0) -> tuple[int, int]:
    """Compute real semantic progress from persisted rows, never a fake timer.

    ``expected_total`` keeps the percentage honest while chunking is still
    producing rows; otherwise the ratio would repeatedly approach 100% as
    each buffer finished embedding.
    """
    predicate, args = "repo_id = ?", [repo_id]
    if staging_prefix:
        predicate += " AND id NOT LIKE ?"
        args.append(f"{staging_prefix}%")
    totals = await store.fetch_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded "
        f"FROM chunks WHERE {predicate}", args
    ) or {}
    total = int(totals.get("total") or 0)
    embedded = int(totals.get("embedded") or 0)
    denominator = max(total, int(expected_total or 0))
    # Passages the provider permanently rejected stay keyword-searchable and
    # count as done, so one odd file cannot keep a repository below Ready.
    done = min(total, embedded + max(0, int(skipped)))
    progress = 100 if denominator == 0 else min(100, int((done / denominator) * 100))
    phase = phase_override or ("ready" if allow_ready and progress >= 100 and embedding_status != "degraded" else "searchable")
    await update_index_state(
        store, repo_id, phase=phase, keyword_files=keyword_files, keyword_chunks=total,
        semantic_progress=progress, embedding_status=embedding_status or ("complete" if progress >= 100 else "running"),
        semantic_ready_at=timestamp() if phase == "ready" else None,
    )
    return total, progress


async def persist_ingestion_metrics(store, job_id: str | None, repo_id: str, metrics: dict) -> None:
    if not settings.ingestion_metrics_enabled or not job_id:
        return
    await store.execute(
        "INSERT INTO ingestion_metrics (job_id, repo_id, metrics_json, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET metrics_json = excluded.metrics_json, updated_at = excluded.updated_at",
        [job_id, repo_id, json.dumps(metrics, separators=(",", ":")), timestamp()],
    )


async def persist_snapshot(store, repo_id: str, *, commit_sha: str | None, source: str, selection: dict) -> None:
    await store.execute(
        "INSERT INTO repo_snapshots (repo_id, commit_sha, source, selection_json, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(repo_id) DO UPDATE SET commit_sha = excluded.commit_sha, source = excluded.source, "
        "selection_json = excluded.selection_json, fetched_at = excluded.fetched_at",
        [repo_id, commit_sha, source, json.dumps(selection, separators=(",", ":")), timestamp()],
    )


async def _background_build_kt_cache(store, repo_id: str) -> None:
    """Build deterministic onboarding metadata after the source is searchable."""
    try:
        indexed_chunks = await store.fetch_all(
            "SELECT file_path, start_line, end_line, language, symbols FROM chunks "
            "WHERE repo_id = ? ORDER BY file_path, start_line LIMIT ?",
            [repo_id, settings.max_repository_chunks],
        )
        await build_kt_cache(store, repo_id, indexed_chunks)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Metadata is a convenience and must never turn a searchable index
        # into a failed job. The next re-index can rebuild it safely.
        logger.warning("Background repository metadata failed for %s", repo_id, exc_info=True)


async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def iter_chunked_files(file_paths: list[str], repo_path: str):
    """Yield bounded concurrent chunking results without retaining a repo.

    Chunking is CPU and file-I/O work. This helper overlaps a small number of
    ordinary files, but automatically falls back to one streaming worker when
    any input is large enough to make concurrent full-file buffers unsafe on
    the 512 MB Render instance. Within each file, line ranges stay exact.
    """
    if not file_paths:
        return
    configured_workers = max(1, int(settings.ingestion_chunk_workers))
    # Keep the scheduler's serial/streaming decision aligned with the chunker
    # threshold so a large file is never materialised as a full chunk list.
    large_threshold = max(1, min(int(settings.ingestion_large_file_serial_bytes), STREAMING_FILE_THRESHOLD))

    def is_large(path: str) -> bool:
        try:
            return os.path.getsize(path) >= large_threshold
        except OSError:
            return False

    has_large_file = any(is_large(path) for path in file_paths)
    worker_count = 1 if has_large_file else min(configured_workers, len(file_paths))

    if has_large_file:
        # A large source file is streamed through a tiny queue. The producer
        # thread owns the generator and blocks when the queue is full, keeping
        # the worker's live source allocation bounded.
        async def stream_one(path: str):
            queue: Queue[tuple[str, object]] = Queue(maxsize=2)
            sentinel = object()
            stopped = threading.Event()

            def put_item(item: tuple[str, object]) -> None:
                while not stopped.is_set():
                    try:
                        queue.put(item, timeout=0.1)
                        return
                    except Full:
                        continue

            def produce() -> None:
                error: BaseException | None = None
                try:
                    for chunk in iter_chunk_file(path, repo_path):
                        if stopped.is_set():
                            break
                        put_item(("chunk", chunk))
                except BaseException as caught:
                    error = caught
                finally:
                    put_item(("done", error or sentinel))

            producer = asyncio.create_task(run_blocking(produce))

            def get_item() -> tuple[str, object] | None:
                try:
                    return queue.get(timeout=0.25)
                except Empty:
                    return None

            try:
                while True:
                    item = await run_blocking(get_item)
                    if item is None:
                        continue
                    kind, value = item
                    if kind == "done":
                        if value is not sentinel:
                            raise value  # type: ignore[misc]
                        break
                    yield value
                await producer
            except BaseException:
                stopped.set()
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                raise

        for path in file_paths:
            async for chunk in stream_one(path):
                yield path, [chunk]
        return

    async def chunk_one(path: str):
        return path, await run_blocking(chunk_file, path, repo_path)

    # Preserve the caller's priority order: files are selected most important
    # first, so a chunk budget that runs out drops only the least important.
    for offset in range(0, len(file_paths), worker_count):
        tasks = [asyncio.create_task(chunk_one(path)) for path in file_paths[offset:offset + worker_count]]
        try:
            for task in tasks:
                yield await task
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


# ---------------------------------------------------------------------------
# Repository identity and queueing
# ---------------------------------------------------------------------------


def is_safe_repo_label(name: str) -> bool:
    """Labels are URL path segments: browsers collapse "." and ".." segments."""
    return bool(name) and name.strip(".") != "" and not any(
        character in name for character in ("/", "\\", "\x00", "?", "#", "%")
    )


async def find_repo_by_url(store, user_id: str, canonical_url: str) -> dict | None:
    """GitHub owner/repo names are case-insensitive, so match the URL that way."""
    return await store.fetch_one(
        "SELECT id, status, repo_name FROM repos WHERE user_id = ? AND lower(github_url) = lower(?)",
        [user_id, canonical_url],
    )


async def _available_repo_name(store, user_id: str, canonical_url: str) -> str:
    """Pick a unique workspace label; two owners can both have a ``utils`` repo."""
    base = repository_name(canonical_url)
    owner, _ = parse_github_repository(canonical_url)
    if not is_safe_repo_label(base):
        # "..." is a valid GitHub name but not a usable workspace label.
        base = f"repo-{owner}"
    candidates = [base, f"{base}-{owner}"] + [f"{base}-{owner}-{index}" for index in range(2, 50)]
    for candidate in candidates:
        taken = await store.fetch_one(
            "SELECT id FROM repos WHERE user_id = ? AND repo_name = ?", [user_id, candidate]
        )
        if not taken:
            return candidate
    return f"{base}-{uuid4().hex[:6]}"


async def ensure_repo_record(store, github_url: str, user_id: str):
    """Create or re-queue a repository only for its authenticated owner.

    Existing chunks are retained until the worker has a fresh snapshot. The
    worker compares file hashes and replaces only changed paths, keeping the
    published index queryable while a refresh waits in the queue.
    """
    canonical_url = normalize_github_url(github_url)
    existing = await find_repo_by_url(store, user_id, canonical_url)
    now = timestamp()
    if existing:
        if existing.get("status") in ACTIVE_REPOSITORY_STATUSES:
            job = await store.fetch_one("SELECT status FROM ingestion_jobs WHERE repo_id = ?", [existing["id"]])
            if job is None or job.get("status") in ACTIVE_JOB_STATUSES:
                raise IngestionConflictError("Repository ingestion is already in progress.")
        repo_id = existing["id"]
        repo_name = existing.get("repo_name") or repository_name(canonical_url)
        await store.execute(
            "UPDATE repos SET github_url = ?, status = 'queued', error_message = NULL, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            [canonical_url, now, repo_id, user_id],
        )
    else:
        repo_id = str(uuid4())
        repo_name = await _available_repo_name(store, user_id, canonical_url)
        await store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)",
            [repo_id, user_id, repo_name, canonical_url, now, now],
        )
    return repo_id, repo_name


async def queue_existing_repo(store, repo: dict, user_id: str) -> tuple[str, str]:
    """Queue a known repository without rebuilding its user-facing name."""
    repo_id = str(repo["id"])
    repo_name = str(repo["repo_name"])
    canonical_url = normalize_github_url(str(repo["github_url"]))
    # A repository added from a /tree/<ref> or /commit/<sha> link is
    # re-indexed at the same ref.
    selection = repo.get("selection") if isinstance(repo.get("selection"), dict) else {}
    stored_ref = selection.get("ref") if isinstance(selection.get("ref"), str) else None
    if stored_ref:
        try:
            kind = "commit" if selection.get("ref_kind") == "commit" else "tree"
            canonical_url = repository_source_url(canonical_url, parse_github_ref(
                f"{canonical_url.removesuffix('.git')}/{kind}/{stored_ref}"
            ))
        except RepositoryValidationError:
            pass
    now = timestamp()
    await store.execute(
        "UPDATE repos SET status = 'queued', error_message = NULL, updated_at = ? "
        "WHERE id = ? AND user_id = ?",
        [now, repo_id, user_id],
    )
    await enqueue_ingestion_job(store, canonical_url, user_id, repo_id)
    return repo_id, repo_name


async def enforce_ingestion_capacity(store, user_id: str, github_url: str) -> None:
    canonical_url = normalize_github_url(github_url)
    existing = await find_repo_by_url(store, user_id, canonical_url)
    if not existing:
        repository_count = await store.fetch_one("SELECT COUNT(*) AS count FROM repos WHERE user_id = ?", [user_id])
        if int((repository_count or {}).get("count") or 0) >= settings.max_repositories_per_user:
            raise IngestionConflictError(
                "Repository limit reached for this workspace. Delete an unused repository before adding another."
            )

    active_count = await store.fetch_one(
        "SELECT COUNT(*) AS count FROM ingestion_jobs WHERE user_id = ? AND status IN ('queued', 'processing')",
        [user_id],
    )
    if int((active_count or {}).get("count") or 0) >= settings.max_active_ingestion_jobs_per_user:
        raise IngestionConflictError(
            "An indexing job is already running for this workspace. Wait for it to finish or stop it first."
        )


async def enqueue_ingestion_job(store, github_url: str, user_id: str, repo_id: str):
    """Persist durable queue work before returning an API response."""
    now = timestamp()
    proposed_job_id = str(uuid4())
    await store.execute(
        "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'queued', 0, ?, ?) "
        "ON CONFLICT(repo_id) DO UPDATE SET user_id = excluded.user_id, github_url = excluded.github_url, "
        "status = 'queued', attempts = 0, claimed_at = NULL, heartbeat_at = NULL, claim_token = NULL, "
        "finished_at = NULL, last_error = NULL, updated_at = excluded.updated_at",
        [proposed_job_id, repo_id, user_id, github_url, now, now],
    )
    # An upsert keeps the original row id. Re-read it after the write so two
    # concurrent submissions cannot create metadata pointing at a losing id.
    actual_job = await store.fetch_one("SELECT id FROM ingestion_jobs WHERE repo_id = ?", [repo_id])
    job_id = str((actual_job or {}).get("id") or proposed_job_id)
    await store.execute(
        "INSERT INTO ingestion_job_meta (job_id, priority, phase, progress, updated_at) VALUES (?, ?, 'queued', 0, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET priority = excluded.priority, phase = 'queued', progress = 0, updated_at = excluded.updated_at",
        [job_id, 10, now],
    )
    has_index = await get_repo_chunk_count(store, repo_id) > 0
    # A refresh keeps the published index queryable, so leave its semantic
    # progress intact; only a brand-new repository starts from zero.
    if has_index:
        await update_index_state(store, repo_id, phase="queued")
    else:
        await update_index_state(store, repo_id, phase="queued", semantic_progress=0, embedding_status="pending")


async def job_is_active(store, repo_id: str, claim_token: str | None = None) -> bool:
    job = await store.fetch_one("SELECT status, claim_token FROM ingestion_jobs WHERE repo_id = ?", [repo_id])
    return bool(job and job["status"] == "processing" and (claim_token is None or job["claim_token"] == claim_token))


async def raise_if_ingestion_cancelled(store, repo_id: str, claim_token: str | None = None):
    if claim_token is None:
        job = await store.fetch_one("SELECT status FROM ingestion_jobs WHERE repo_id = ?", [repo_id])
        if job and job["status"] == "cancelled":
            raise IngestionCancelledError("Indexing was stopped by the user.")
        return
    if not await job_is_active(store, repo_id, claim_token):
        raise IngestionCancelledError("Indexing was stopped by the user.")


async def heartbeat_job(store, job_id: str | None, claim_token: str | None) -> None:
    if job_id and claim_token:
        now = timestamp()
        await store.execute(
            "UPDATE ingestion_jobs SET heartbeat_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing' AND claim_token = ?",
            [now, now, job_id, claim_token],
        )
        await store.execute(
            "UPDATE ingestion_job_meta SET phase = COALESCE((SELECT phase FROM repo_index_state s "
            "JOIN ingestion_jobs j ON j.repo_id = s.repo_id WHERE j.id = ?), phase), "
            "progress = COALESCE((SELECT semantic_progress FROM repo_index_state s "
            "JOIN ingestion_jobs j ON j.repo_id = s.repo_id WHERE j.id = ?), progress), updated_at = ? "
            "WHERE job_id = ?",
            [job_id, job_id, now, job_id],
        )


_REPO_COLUMNS = {"status", "chunk_count", "error_message", "repo_name", "github_url"}


async def update_repo(store, repo_id: str, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - _REPO_COLUMNS
    if unknown:  # column names are interpolated, so keep them allow-listed
        raise ValueError(f"Unsupported repository fields: {sorted(unknown)}")
    fields["updated_at"] = timestamp()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await store.execute(f"UPDATE repos SET {assignments} WHERE id = ?", [*fields.values(), repo_id])


# ---------------------------------------------------------------------------
# Re-index staging (hidden replacement rows)
# ---------------------------------------------------------------------------


def run_chunk_prefix(value: str | None) -> str | None:
    """The chunk id prefix of a repo_index_runs row (initial marker removed)."""
    if not value:
        return None
    value = str(value)
    if value.startswith(INITIAL_RUN_MARKER):
        value = value[len(INITIAL_RUN_MARKER):]
    return value or None


def is_initial_run(value: str | None) -> bool:
    return bool(value) and str(value).startswith(INITIAL_RUN_MARKER)


async def get_staging_prefix(store, repo_id: str) -> str | None:
    """Prefix of rows hidden from retrieval (a refresh's replacement rows)."""
    row = await store.fetch_one("SELECT chunk_prefix FROM repo_index_runs WHERE repo_id = ?", [repo_id])
    prefix = (row or {}).get("chunk_prefix")
    if not prefix or is_initial_run(prefix):
        return None
    return str(prefix)


async def get_run_prefix(store, repo_id: str) -> str | None:
    """Prefix of the unpublished rows of the current run, staged or initial."""
    row = await store.fetch_one("SELECT chunk_prefix FROM repo_index_runs WHERE repo_id = ?", [repo_id])
    return run_chunk_prefix((row or {}).get("chunk_prefix"))


async def begin_index_run(store, repo_id: str, job_id: str | None, chunk_prefix: str, *, initial: bool = False) -> None:
    """Register this run's rows as unpublished.

    A refresh's replacement rows are hidden from retrieval until the swap. A
    first index (``initial``) stays visible for early keyword search, but is
    still discarded if the run never completes.
    """
    await discard_staged_rows(store, repo_id)
    await store.execute(
        "INSERT INTO repo_index_runs (repo_id, job_id, chunk_prefix, started_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo_id) DO UPDATE SET job_id = excluded.job_id, chunk_prefix = excluded.chunk_prefix, "
        "started_at = excluded.started_at",
        [repo_id, job_id, (INITIAL_RUN_MARKER if initial else "") + chunk_prefix, timestamp()],
    )


async def end_index_run(store, repo_id: str) -> None:
    await store.execute("DELETE FROM repo_index_runs WHERE repo_id = ?", [repo_id])


async def discard_staged_rows(store, repo_id: str) -> None:
    """Remove unpublished rows left by a cancelled, failed, or crashed run."""
    prefix = await get_run_prefix(store, repo_id)
    if prefix:
        await store.execute("DELETE FROM chunks WHERE repo_id = ? AND id LIKE ?", [repo_id, f"{prefix}%"])
    await end_index_run(store, repo_id)


async def get_repo_chunk_count(store, repo_id: str) -> int:
    result = await store.fetch_one("SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ?", [repo_id])
    return int(result["count"]) if result and result.get("count") is not None else 0


async def published_chunk_count(store, repo_id: str, staging_prefix: str | None) -> int:
    if not staging_prefix:
        return await get_repo_chunk_count(store, repo_id)
    result = await store.fetch_one(
        "SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ? AND id NOT LIKE ?",
        [repo_id, f"{staging_prefix}%"],
    )
    return int((result or {}).get("count") or 0)


async def get_repo_error_message(store, repo_id: str) -> str | None:
    result = await store.fetch_one("SELECT error_message FROM repos WHERE id = ?", [repo_id])
    return result.get("error_message") if result else None


async def clear_derived_rows(store, repo_id: str) -> None:
    """Remove every index artefact of a repository (not the repository row)."""
    await store.batch([
        Statement(f"DELETE FROM {table} WHERE repo_id = ?", [repo_id])
        for table in (
            "kt_cache", "repo_files", "repo_dependencies", "repo_coverage", "file_vectors",
            "repo_snapshots", "repo_index_runs", "chunks",
        )
    ])


async def restore_published_index(store, repo_id: str, message: str) -> bool:
    """After a stopped/failed refresh, republish the previous index if any.

    Returns True when a previous index exists (the repository stays ready).
    """
    await discard_staged_rows(store, repo_id)
    count = await get_repo_chunk_count(store, repo_id)
    if count <= 0:
        return False
    await update_repo(store, repo_id, status="ready", chunk_count=count, error_message=message)
    try:
        await publish_index_progress(store, repo_id, allow_ready=True)
    except Exception:
        logger.debug("Could not republish previous index state", exc_info=True)
    return True


async def cancel_repository_indexing(store, repo_id: str, user_id: str) -> str:
    """Stop a queued or running job without destroying a published index.

    Returns ``"stopping"`` when a live worker will clean up its own staged
    rows at the next checkpoint, or ``"stopped"`` when the cleanup happened
    here (queued job, or a worker that died mid-run).
    """
    job = await store.fetch_one(
        "SELECT id, status, heartbeat_at, claimed_at FROM ingestion_jobs WHERE repo_id = ? AND user_id = ?",
        [repo_id, user_id],
    )
    if not job or job.get("status") not in ACTIVE_JOB_STATUSES:
        raise IngestionConflictError("This repository is not currently being indexed.")
    alive = worker_is_alive(job)
    now = timestamp()
    cancelled = await store.execute(
        "UPDATE ingestion_jobs SET status = 'cancelled', claimed_at = NULL, heartbeat_at = NULL, claim_token = NULL, "
        "finished_at = ?, last_error = 'Indexing stopped by the user.', updated_at = ? "
        "WHERE id = ? AND status IN ('queued', 'processing') RETURNING id",
        [now, now, job["id"]],
    )
    if not cancelled.rows:
        raise IngestionConflictError("This repository finished indexing before it could be stopped.")
    if alive:
        await update_repo(store, repo_id, error_message="Stopping indexing…")
        return "stopping"
    await finish_cancelled_repository(store, repo_id)
    return "stopped"


async def finish_cancelled_repository(store, repo_id: str) -> None:
    if await restore_published_index(store, repo_id, "Index refresh stopped. The previous index remains available."):
        return
    await clear_derived_rows(store, repo_id)
    await update_repo(store, repo_id, status="cancelled", chunk_count=0, error_message="Indexing stopped by you.")
    try:
        await update_index_state(store, repo_id, phase="cancelled", keyword_files=0, keyword_chunks=0,
                                 semantic_progress=0, embedding_status="degraded")
    except Exception:
        logger.debug("Could not publish cancelled ingestion state", exc_info=True)


async def delete_repository(store, repo_id: str, user_id: str) -> None:
    """Delete a repository and every artefact, refusing while a worker writes."""
    job = await store.fetch_one(
        "SELECT id, status, heartbeat_at, claimed_at FROM ingestion_jobs WHERE repo_id = ?", [repo_id]
    )
    if worker_is_alive(job):
        raise IngestionConflictError("Stop indexing before deleting this repository.")
    if job and job.get("status") in ACTIVE_JOB_STATUSES:
        # A queued job (or an orphaned one) must never be claimed after the
        # repository row disappears.
        await store.execute(
            "UPDATE ingestion_jobs SET status = 'cancelled', claim_token = NULL, updated_at = ? WHERE repo_id = ?",
            [timestamp(), repo_id],
        )
    await store.batch([
        Statement("DELETE FROM ingestion_job_meta WHERE job_id IN (SELECT id FROM ingestion_jobs WHERE repo_id = ?)", [repo_id]),
        Statement("DELETE FROM ingestion_metrics WHERE repo_id = ?", [repo_id]),
        *[
            Statement(f"DELETE FROM {table} WHERE repo_id = ?", [repo_id])
            for table in (
                "chat_messages", "edit_proposals", "ingestion_jobs", "kt_cache", "repo_dependencies", "repo_files",
                "repo_coverage", "repo_index_state", "repo_snapshots", "file_vectors", "repo_index_runs", "chunks",
            )
        ],
        Statement("DELETE FROM repos WHERE id = ? AND user_id = ?", [repo_id, user_id]),
    ])


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


async def embed_repository_chunks(
    store,
    repo_id: str,
    chunks: list[dict],
    job_id: str | None = None,
    claim_token: str | None = None,
    *,
    progress_offset: int = 0,
    progress_total: int | None = None,
    metrics: dict | None = None,
    cache_enabled: bool = True,
):
    """Embed in cancellable batches and publish meaningful UI progress."""
    batch_size = max(1, settings.embedding_batch_size)
    minimum_batch_size = min(batch_size, max(1, settings.embedding_min_batch_size))
    progress_interval = max(1, settings.embedding_progress_interval_batches)
    heartbeat_interval = max(1, settings.embedding_heartbeat_interval_batches)
    total_chunks = len(chunks)

    async def report_progress(completed: int):
        overall_completed = progress_offset + completed
        total = max(int(progress_total or 0), overall_completed)
        percent = int((overall_completed / total) * 100) if total else 0
        await update_repo(
            store, repo_id,
            error_message=(
                f"Building the semantic index: {overall_completed:,} of {total:,} code sections ({min(100, percent)}%). "
                "Keyword search already works."
            ),
        )

    # Hash only the passage content, not its path, so identical code in two
    # files can reuse one vector. The lookup is bounded to the current buffer.
    for chunk in chunks:
        chunk.setdefault("content_hash", hashlib.sha256(str(chunk.get("content") or "").encode("utf-8")).hexdigest())
    cache_reader = getattr(store, "get_embedding_cache", None) if cache_enabled else None
    cached = await cache_reader([chunk["content_hash"] for chunk in chunks]) if cache_reader else {}
    cache_hits = 0
    pending_by_hash: dict[str, dict] = {}
    for chunk in chunks:
        vector = cached.get(chunk["content_hash"])
        if vector is not None and len(vector) == settings.embedding_dimension:
            chunk["embedding"] = array("f", vector)
            chunk["_embedding_cache_hit"] = True
            cache_hits += 1
        else:
            pending_by_hash.setdefault(chunk["content_hash"], chunk)

    pending = list(pending_by_hash.values())
    if cache_hits or progress_offset == 0:
        await report_progress(cache_hits)

    def record_embedding_request() -> None:
        if metrics is not None:
            metrics["embedding_requests"] = int(metrics.get("embedding_requests", 0)) + 1

    offset = 0
    batch_number = 0
    while offset < len(pending):
        batch_number += 1
        await raise_if_ingestion_cancelled(store, repo_id, claim_token)
        await wait_for_memory_headroom()
        current_size = min(batch_size, len(pending) - offset)
        batch = pending[offset:offset + current_size]
        observed_batch_size = [current_size]
        await run_blocking(
            embed_chunks,
            batch,
            initial_batch_size=current_size,
            on_batch_size_change=lambda value: observed_batch_size.__setitem__(0, value),
            on_request=record_embedding_request,
        )
        offset += current_size
        completed = sum(1 for item in chunks if item.get("embedding") is not None)
        # Persist a provider payload reduction for the rest of the repository.
        batch_size = max(minimum_batch_size, min(current_size, observed_batch_size[0]))
        if batch_number % progress_interval == 0 or completed == total_chunks:
            await report_progress(completed)
        if job_id and claim_token and (batch_number % heartbeat_interval == 0 or completed == total_chunks):
            await heartbeat_job(store, job_id, claim_token)
        if pressure_level() in {"warning", "critical"}:
            gc.collect()
    # Fan vectors back out to duplicate passages in this bounded buffer.
    vectors_by_hash = {chunk["content_hash"]: chunk.get("embedding") for chunk in pending if chunk.get("embedding") is not None}
    for chunk in chunks:
        if chunk.get("embedding") is None and chunk["content_hash"] in vectors_by_hash:
            chunk["embedding"] = vectors_by_hash[chunk["content_hash"]]
    cache_writer = getattr(store, "save_embedding_cache", None) if cache_enabled else None
    if cache_writer:
        cache_records = {}
        for chunk in chunks:
            content_hash = chunk.get("content_hash")
            if content_hash and content_hash not in cached and chunk.get("embedding") is not None:
                cache_records[content_hash] = {"content_hash": content_hash, "embedding": chunk["embedding"], "updated_at": timestamp()}
        await cache_writer(list(cache_records.values()))
    return list(chunks)


class FileVectorAccumulator:
    """Running per-file vector sums for centroid (file-level) semantic search.

    Only files whose chunks are still being produced stay in memory; finished
    files are flushed after each buffer, keeping the footprint to a handful of
    2,048-float arrays even for repositories with thousands of files.
    """

    def __init__(self):
        self._sums: dict[str, array] = {}
        self._counts: dict[str, int] = {}

    def add(self, path: str, vector) -> None:
        current = self._sums.get(path)
        if current is None:
            self._sums[path] = array("f", vector)
            self._counts[path] = 1
            return
        self._sums[path] = array("f", (left + right for left, right in zip(current, vector)))
        self._counts[path] += 1

    def pop_completed(self, open_paths: set[str] | None = None) -> list[tuple[str, array, int]]:
        ready = []
        for path in [path for path in self._sums if not open_paths or path not in open_paths]:
            total, count = self._sums.pop(path), self._counts.pop(path)
            ready.append((path, array("f", (value / count for value in total)), count))
        return ready

    def __len__(self) -> int:
        return len(self._sums)


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------


async def requeue_stale_jobs(store):
    # Heartbeats arrive every few seconds for a live run (see _watch_job), so
    # a lease silent for the liveness window belongs to a dead worker. The
    # configured job timeout still applies when it is shorter.
    stale_after = min(int(settings.ingestion_job_timeout_seconds), WORKER_LIVENESS_SECONDS)
    threshold = datetime.now(UTC) - timedelta(seconds=max(1, stale_after))
    jobs = await store.fetch_all(
        "SELECT id, repo_id, attempts, claim_token, claimed_at, heartbeat_at FROM ingestion_jobs WHERE status = 'processing'"
    )
    for job in jobs:
        if not _lease_is_stale(job.get("heartbeat_at") or job.get("claimed_at"), threshold):
            continue
        attempts = int(job.get("attempts") or 0)
        failed = attempts >= settings.max_ingestion_attempts
        error = (
            "Indexing timed out repeatedly. Try again, or index a smaller repository."
            if failed else "Previous worker lease expired; retrying."
        )
        now = timestamp()
        result = await store.execute(
            "UPDATE ingestion_jobs SET status = ?, claimed_at = NULL, heartbeat_at = NULL, claim_token = NULL, "
            "finished_at = ?, last_error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing' AND COALESCE(claim_token, '') = COALESCE(?, '') "
            "AND COALESCE(heartbeat_at, claimed_at) = ? RETURNING id",
            ["failed" if failed else "queued", now if failed else None, error, now, job["id"], job.get("claim_token"), job.get("heartbeat_at") or job.get("claimed_at")],
        )
        if result.rows_affected and failed:
            if not await restore_published_index(store, job["repo_id"], "The refresh timed out. The previous index remains available."):
                await update_repo(store, job["repo_id"], status="failed", error_message=error)
                await update_index_state(store, job["repo_id"], phase="failed", embedding_status="degraded")


async def claim_next_ingestion_job(store):
    """Claim one queue row atomically; overlapping workers cannot claim it twice."""
    await requeue_stale_jobs(store)
    claim_token = str(uuid4())
    now = timestamp()
    result = await store.execute(
        "UPDATE ingestion_jobs SET status = 'processing', claimed_at = ?, heartbeat_at = ?, claim_token = ?, "
        "attempts = attempts + 1, last_error = NULL, updated_at = ? "
        "WHERE id = (SELECT queued.id FROM ingestion_jobs queued "
        "LEFT JOIN ingestion_job_meta meta ON meta.job_id = queued.id "
        "WHERE queued.status = 'queued' ORDER BY COALESCE(meta.priority, 20) ASC, queued.created_at ASC LIMIT 1) "
        "AND status = 'queued' "
        "RETURNING id, repo_id, user_id, github_url, attempts",
        [now, now, claim_token, now],
    )
    if not result.rows:
        return None
    job = dict(result.rows[0])
    job["claim_token"] = claim_token
    return job


async def persist_coverage(store, repo_id: str, report: dict, indexed_files: int = 0) -> None:
    """Persist bounded, explainable ingestion coverage for the repository UI."""
    excluded_paths = list(report.get("excluded_paths") or [])[: max(0, settings.excluded_paths_limit)]
    await store.execute(
        "INSERT INTO repo_coverage (repo_id, total_seen_files, eligible_files, indexed_files, "
        "excluded_files, excluded_bytes, excluded_reasons, excluded_paths, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(repo_id) DO UPDATE SET total_seen_files = excluded.total_seen_files, "
        "eligible_files = excluded.eligible_files, indexed_files = excluded.indexed_files, "
        "excluded_files = excluded.excluded_files, excluded_bytes = excluded.excluded_bytes, "
        "excluded_reasons = excluded.excluded_reasons, excluded_paths = excluded.excluded_paths, updated_at = excluded.updated_at",
        [
            repo_id,
            int(report.get("total_seen_files", 0)),
            int(report.get("eligible_files", 0)),
            int(indexed_files),
            int(report.get("excluded_files", 0)),
            int(report.get("excluded_bytes", 0)),
            json.dumps(report.get("excluded_reasons") or {}),
            json.dumps(excluded_paths),
            timestamp(),
        ],
    )


def build_file_manifest(files: list[str], repo_path: str) -> dict[str, dict[str, int | str]]:
    """Hash selected source files for deterministic incremental re-indexing."""
    manifest: dict[str, dict[str, int | str]] = {}
    for file_path in files:
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        relative = os.path.relpath(file_path, repo_path).replace(os.sep, "/")
        manifest[relative] = {"content_hash": digest.hexdigest(), "byte_size": os.path.getsize(file_path)}
    return manifest


def _in_list_windows(values: list[str], size: int = 400):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


async def replace_changed_file_chunks(
    store,
    repo_id: str,
    changed_paths: set[str],
    removed_paths: set[str],
    preserve_id_prefix: str | None = None,
) -> None:
    """Remove stale chunks while preserving the current ingestion version.

    Refreshes keep the old rows until the new rows have been completely
    chunked and embedded. New rows carry a job-scoped id prefix, so the final
    cleanup removes only the old version. A failed refresh therefore never
    destroys a previously searchable repository.
    """
    statements = _replacement_statements(repo_id, changed_paths, removed_paths, preserve_id_prefix)
    if statements:
        await store.batch(statements)


def _replacement_statements(
    repo_id: str, changed_paths: set[str], removed_paths: set[str], preserve_id_prefix: str | None = None,
) -> list[Statement]:
    statements: list[Statement] = []
    for window in _in_list_windows(sorted(changed_paths)):
        placeholders = ", ".join("?" for _ in window)
        predicate = f"repo_id = ? AND file_path IN ({placeholders})"
        args: list[object] = [repo_id, *window]
        if preserve_id_prefix:
            predicate += " AND id NOT LIKE ?"
            args.append(f"{preserve_id_prefix}%")
        statements.append(Statement(f"DELETE FROM chunks WHERE {predicate}", args))
    for window in _in_list_windows(sorted(removed_paths)):
        placeholders = ", ".join("?" for _ in window)
        statements.append(Statement(f"DELETE FROM chunks WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id, *window]))
        statements.append(Statement(f"DELETE FROM repo_files WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id, *window]))
        statements.append(Statement(f"DELETE FROM file_vectors WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id, *window]))
    return statements


async def swap_in_replacement(
    store, repo_id: str, changed_paths: set[str], removed_paths: set[str], chunk_id_prefix: str,
    stale_vector_paths: set[str] | None = None,
) -> None:
    """Publish a refresh atomically.

    Deleting the previous version of every changed/removed path and removing
    the staging marker (which un-hides the new rows) run as one transaction,
    so a concurrent query sees either the whole old index or the whole new
    one, never a moment where changed files are missing from both.
    """
    statements = _replacement_statements(repo_id, changed_paths, removed_paths, chunk_id_prefix)
    for window in _in_list_windows(sorted(stale_vector_paths or ())):
        placeholders = ", ".join("?" for _ in window)
        statements.append(Statement(f"DELETE FROM file_vectors WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id, *window]))
    statements.append(Statement("DELETE FROM repo_index_runs WHERE repo_id = ?", [repo_id]))
    await store.batch(statements)


async def persist_file_manifest(store, repo_id: str, manifest: dict[str, dict[str, int | str]]) -> None:
    statements: list[Statement] = []
    for file_path, metadata in manifest.items():
        statements.append(Statement(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_id, file_path) DO UPDATE SET content_hash = excluded.content_hash, "
            "byte_size = excluded.byte_size, updated_at = excluded.updated_at",
            [repo_id, file_path, metadata["content_hash"], metadata["byte_size"], timestamp()],
        ))
        if len(statements) >= max(1, settings.chunk_insert_batch_size):
            await store.batch(statements)
            statements = []
    if statements:
        await store.batch(statements)


async def persist_dependency_manifest(store, repo_id: str, dependencies: list[dict]) -> None:
    """Replace the small resolved dependency graph after a successful index."""
    await store.execute("DELETE FROM repo_dependencies WHERE repo_id = ?", [repo_id])
    statements: list[Statement] = []
    for edge in dependencies:
        statements.append(Statement(
            "INSERT OR IGNORE INTO repo_dependencies (repo_id, source_file, target_file, import_name, line_number) "
            "VALUES (?, ?, ?, ?, ?)",
            [repo_id, edge["source_file"], edge["target_file"], edge["import_name"], edge["line_number"]],
        ))
        if len(statements) >= max(1, settings.chunk_insert_batch_size):
            await store.batch(statements)
            statements = []
    if statements:
        await store.batch(statements)


async def finalize_successful_job(store, job: dict) -> bool:
    """Publish ready only if the same worker still owns the queue lease.

    Repo metadata is fetched before the atomic job UPDATE so the repo status
    can be written immediately afterward.
    """
    now = timestamp()
    repo_id = job["repo_id"]
    chunk_count = await get_repo_chunk_count(store, repo_id)
    error_message = await get_repo_error_message(store, repo_id)
    completed = await store.execute(
        "UPDATE ingestion_jobs SET status = 'completed', finished_at = ?, claimed_at = NULL, heartbeat_at = NULL, "
        "claim_token = NULL, updated_at = ? WHERE id = ? AND status = 'processing' AND claim_token = ? RETURNING id",
        [now, now, job["id"], job["claim_token"]],
    )
    if not completed.rows:
        return False
    await update_repo(store, repo_id, status="ready", chunk_count=chunk_count, error_message=error_message)
    return True


async def mark_job_failed(store, job: dict) -> None:
    now = timestamp()
    await store.execute(
        "UPDATE ingestion_jobs SET status = 'failed', finished_at = ?, claimed_at = NULL, heartbeat_at = NULL, "
        "claim_token = NULL, updated_at = ? WHERE id = ? AND status = 'processing' AND claim_token = ?",
        [now, now, job["id"], job["claim_token"]],
    )


async def recover_stuck_repos(store) -> int:
    """Fix repositories stuck in active statuses whose ingestion jobs already completed."""
    stuck = await store.fetch_all(
        "SELECT r.id, r.status, j.status AS job_status "
        "FROM repos r JOIN ingestion_jobs j ON j.repo_id = r.id "
        "WHERE r.status IN (?, ?, ?, ?, ?) AND j.status = 'completed'",
        sorted(ACTIVE_REPOSITORY_STATUSES),
    )
    recovered = 0
    for repo in stuck:
        repo_id = repo["id"]
        chunk_count = await get_repo_chunk_count(store, repo_id)
        error_message = await get_repo_error_message(store, repo_id)
        await update_repo(store, repo_id, status="ready", chunk_count=chunk_count, error_message=error_message)
        recovered += 1
        logger.info("Recovered stuck repository %s from status '%s' to 'ready'", repo_id, repo["status"])
    return recovered


def _older_than(value: str | None, seconds: float) -> bool:
    return _lease_is_stale(value, datetime.now(UTC) - timedelta(seconds=seconds))


async def recover_orphaned_runs(store) -> int:
    """Finish runs whose worker is gone.

    * A stopped job whose worker never finished the cleanup (it restarted or
      crashed after "Stopping indexing…") is cleaned up here, once the stop
      is older than the worker liveness window.
    * Unpublished rows of a run whose job is no longer active are discarded;
      a first index that never completed is not left looking Ready.
    """
    recovered = 0
    stopped = await store.fetch_all(
        "SELECT j.repo_id, j.updated_at FROM ingestion_jobs j JOIN repos r ON r.id = j.repo_id "
        "LEFT JOIN repo_index_runs runs ON runs.repo_id = j.repo_id "
        "WHERE j.status = 'cancelled' AND (runs.repo_id IS NOT NULL OR r.status IN (?, ?, ?, ?, ?))",
        sorted(ACTIVE_REPOSITORY_STATUSES),
    )
    handled: set[str] = set()
    for row in stopped:
        repo_id = str(row["repo_id"])
        handled.add(repo_id)
        if not _older_than(row.get("updated_at"), WORKER_LIVENESS_SECONDS):
            continue  # a live worker may still be finishing its own cleanup
        await finish_cancelled_repository(store, repo_id)
        recovered += 1
    rows = await store.fetch_all(
        "SELECT runs.repo_id, runs.chunk_prefix, j.status AS job_status FROM repo_index_runs runs "
        "LEFT JOIN ingestion_jobs j ON j.repo_id = runs.repo_id "
        "WHERE j.status IS NULL OR j.status NOT IN ('queued', 'processing', 'cancelled')"
    )
    for row in rows:
        repo_id = str(row["repo_id"])
        if repo_id in handled:
            continue
        initial = is_initial_run(row.get("chunk_prefix"))
        await discard_staged_rows(store, repo_id)
        if initial and not await restore_published_index(
            store, repo_id, "Indexing was interrupted. The previous index remains available.",
        ):
            message = "Indexing was interrupted before it finished. Re-index to try again."
            await update_repo(store, repo_id, status="failed", chunk_count=0, error_message=message)
            await update_index_state(store, repo_id, phase="failed", keyword_chunks=0, embedding_status="degraded")
        recovered += 1
    return recovered


async def release_claimed_job(store, job: dict) -> None:
    """Hand a claimed job back to the queue (graceful worker shutdown).

    A deploy's SIGTERM cancels the worker task; without this the job stayed
    'processing' with a fresh heartbeat and blocked the workspace until the
    lease expired. The attempt is not counted against the retry limit.
    """
    now = timestamp()
    await store.execute(
        "UPDATE ingestion_jobs SET status = 'queued', claimed_at = NULL, heartbeat_at = NULL, claim_token = NULL, "
        "attempts = MAX(0, attempts - 1), last_error = 'The indexing worker restarted; retrying.', updated_at = ? "
        "WHERE id = ? AND status = 'processing' AND claim_token = ?",
        [now, job["id"], job["claim_token"]],
    )


async def process_one_queued_ingestion(store):
    job = await claim_next_ingestion_job(store)
    if not job:
        return {"processed": False}
    try:
        succeeded = await run_ingestion_for_repo(
            store, job["github_url"], job["user_id"], job["repo_id"], job_id=job["id"], claim_token=job["claim_token"]
        )
    except asyncio.CancelledError:
        try:
            await asyncio.shield(release_claimed_job(store, job))
        except BaseException:
            logger.warning("Could not release ingestion job %s on shutdown", job.get("id"), exc_info=True)
        raise
    if succeeded:
        succeeded = await finalize_successful_job(store, job)
    else:
        await mark_job_failed(store, job)
    return {"processed": True, "repo_id": job["repo_id"], "succeeded": succeeded}


async def run_ingestion(github_url: str, user_id: str):
    await assert_turso_schema()
    await run_ingestion_for_repo(get_turso_store(), github_url, user_id)


def _selection_summary(report: dict, *, byte_budget: int, quota_limited: bool, chunk_budget_paths: list[str]) -> dict:
    return {
        "candidate_files": int(report.get("candidate_files", report.get("eligible_files", 0))),
        "selected_files": int(report.get("eligible_files", 0)),
        "selected_bytes": int(report.get("eligible_bytes", 0)),
        "estimated_chunks": int(report.get("estimated_chunks", 0)),
        "budget_omitted_files": int(report.get("budget_omitted_files", 0)) + len(chunk_budget_paths),
        "budget_omitted_by_category": dict(report.get("budget_omitted_by_category") or {}),
        "byte_budget": int(byte_budget),
        "quota_limited": bool(quota_limited),
        "selection_budget": report.get("selection_budget") or {},
    }


def _completion_message(summary: dict, semantic_warning: str | None) -> str | None:
    """Explain a budgeted index plainly; None means nothing needs attention."""
    notes = []
    omitted = int(summary.get("budget_omitted_files") or 0)
    if omitted:
        selected = int(summary.get("selected_files") or 0)
        reason = (
            "to stay within your plan's indexed-source limit"
            if summary.get("quota_limited") else
            "to stay within the repository indexing budget"
        )
        categories = {
            str(name): int(count) for name, count in (summary.get("budget_omitted_by_category") or {}).items()
            if int(count or 0) > 0
        }
        breakdown = ", ".join(
            f"{count:,} {BUDGET_CATEGORY_LABELS.get(name, name)}"
            for name, count in sorted(categories.items(), key=lambda item: (-item[1], item[0]))
        )
        notes.append(
            f"Indexed the {selected:,} most important files. {omitted:,} lower-priority files were skipped {reason}"
            + (f" ({breakdown})." if breakdown else ".")
            + " The coverage report lists the most important skipped files."
        )
    if semantic_warning:
        notes.append(semantic_warning)
    return " ".join(notes) or None


async def _watch_job(store, repo_id: str, job_id: str, claim_token: str, stop: threading.Event) -> None:
    """Heartbeat the lease and notice a stop for the whole run.

    Long blocking phases (the archive download, manifest hashing) never reach
    a cancellation checkpoint; this side task keeps the lease fresh so a live
    worker is never mistaken for a dead one, and sets ``stop`` as soon as the
    job is cancelled or its lease is lost so the download aborts promptly.
    """
    last_heartbeat = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(JOB_STOP_POLL_SECONDS)
        try:
            if not await job_is_active(store, repo_id, claim_token):
                stop.set()
                return
            if time.monotonic() - last_heartbeat >= JOB_WATCH_INTERVAL_SECONDS:
                await heartbeat_job(store, job_id, claim_token)
                last_heartbeat = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Job watcher could not reach the database", exc_info=True)


async def run_ingestion_for_repo(
    store, github_url: str, user_id: str, repo_id: str | None = None,
    job_id: str | None = None, claim_token: str | None = None,
):
    stop_requested = threading.Event()
    watcher: asyncio.Task | None = None
    try:
        if job_id and claim_token and repo_id:
            watcher = asyncio.create_task(_watch_job(store, repo_id, job_id, claim_token, stop_requested))
        return await _run_ingestion_for_repo(
            store, github_url, user_id, repo_id, job_id, claim_token, stop_requested,
        )
    finally:
        stop_requested.set()
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def _run_ingestion_for_repo(
    store, github_url: str, user_id: str, repo_id: str | None,
    job_id: str | None, claim_token: str | None, stop_requested: threading.Event,
):
    snapshot = None
    initial_run = False
    # Track the version that existed before this run. Cancelling or failing a
    # refresh must not erase a healthy searchable index; only this run's
    # job-scoped rows are disposable until the swap completes.
    preexisting_chunk_count = 0
    chunk_id_prefix: str | None = None
    staged_run = False
    replacement_swapped = False
    started_at = time.perf_counter()
    metrics = {
        "clone_ms": 0, "scan_ms": 0, "manifest_ms": 0, "chunk_ms": 0,
        "keyword_index_ms": 0, "embedding_ms": 0, "database_ms": 0,
        "dependency_ms": 0, "summarization_ms": 0, "total_ms": 0,
        "files_selected": 0, "files_excluded": 0, "bytes_selected": 0,
        "chunks_created": 0, "chunks_embedded": 0, "embedding_requests": 0,
        "embedding_cache_hits": 0, "embedding_failures": 0, "db_batches": 0,
        "peak_rss_mb": 0, "source": "", "files_budget_omitted": 0,
    }
    try:
        canonical_url = normalize_github_url(github_url)
        source_url = repository_source_url(github_url)
        if repo_id is None:
            repo_id, _ = await ensure_repo_record(store, canonical_url, user_id)
        # Rows of an earlier attempt that never completed (a crash, a
        # restart) are not an index; drop them before judging refresh-ness.
        await discard_staged_rows(store, repo_id)
        preexisting_chunk_count = await get_repo_chunk_count(store, repo_id)
        refreshing = preexisting_chunk_count > 0
        await raise_if_ingestion_cancelled(store, repo_id, claim_token)
        await heartbeat_job(store, job_id, claim_token)
        await update_repo(
            store, repo_id, status="cloning",
            error_message=(
                "Refreshing the index. The previous index stays available until the new one is ready."
                if refreshing else "Downloading the repository snapshot."
            ),
        )
        await update_index_state(store, repo_id, phase="cloning", **({} if refreshing else {"embedding_status": "pending"}))
        clone_started = time.perf_counter()
        should_stop = stop_requested.is_set if claim_token else None
        try:
            snapshot = await run_blocking(acquire_repository_source, source_url, should_stop)
        except SourceFetchCancelled as error:
            raise IngestionCancelledError(str(error)) from error
        repo_path = snapshot.path
        metrics["clone_ms"] = int((time.perf_counter() - clone_started) * 1000)
        metrics["source"] = snapshot.source

        await raise_if_ingestion_cancelled(store, repo_id, claim_token)
        await heartbeat_job(store, job_id, claim_token)
        await update_repo(
            store, repo_id, status="chunking",
            **({} if refreshing else {"error_message": "Selecting the most important files to index."}),
        )
        await update_index_state(store, repo_id, phase="scanning")

        # The quota becomes a byte budget for the importance-ranked selector:
        # a nearly full plan indexes the most important code rather than
        # failing the whole repository.
        byte_budget, usage = await repository_byte_budget(store, user_id, repo_id)
        if byte_budget < 1_000:
            raise RepositoryQuotaExceededError(quota_exceeded_message(usage))
        scan_started = time.perf_counter()
        selection_report = await run_blocking(
            get_file_selection_report, repo_path, pre_excluded=snapshot, byte_budget=byte_budget,
        )
        metrics["scan_ms"] = int((time.perf_counter() - scan_started) * 1000)
        metrics["files_selected"] = int(selection_report.get("eligible_files", 0))
        metrics["files_excluded"] = int(selection_report.get("excluded_files", 0))
        metrics["bytes_selected"] = int(selection_report.get("eligible_bytes", 0))
        metrics["files_budget_omitted"] = int(selection_report.get("budget_omitted_files", 0))
        quota_limited = byte_budget < settings.max_repository_bytes and int(selection_report.get("budget_omitted_files", 0)) > 0
        await persist_coverage(store, repo_id, selection_report)
        files = selection_report["files"]
        if not files:
            if int(selection_report.get("budget_omitted_files", 0)) and quota_limited:
                raise RepositoryQuotaExceededError(quota_exceeded_message(usage))
            raise ValueError("No supported text source files were found in this repository.")

        await update_index_state(store, repo_id, phase="manifesting")
        if not refreshing:
            await update_repo(store, repo_id, error_message="Hashing files and resolving imports.")
        manifest_started = time.perf_counter()
        previous_rows_task = store.fetch_all(
            "SELECT file_path, content_hash FROM repo_files WHERE repo_id = ?", [repo_id]
        )
        manifest_task = run_blocking(build_manifest_and_dependency_manifest, files, repo_path)
        (manifest, dependencies), previous_rows = await asyncio.gather(manifest_task, previous_rows_task)
        metrics["manifest_ms"] = int((time.perf_counter() - manifest_started) * 1000)
        metrics["dependency_ms"] = metrics["manifest_ms"]
        await raise_if_ingestion_cancelled(store, repo_id, claim_token)
        previous_manifest = {row["file_path"]: row["content_hash"] for row in previous_rows}
        changed_paths = {
            path for path, metadata in manifest.items()
            if previous_manifest.get(path) != metadata["content_hash"]
        }
        removed_paths = set(previous_manifest) - set(manifest)
        existing_count = await get_repo_chunk_count(store, repo_id)
        defer_replacement = existing_count > 0
        # ``existing_count`` still contains rows for changed or removed paths;
        # subtract them so a refresh near the budget is judged on the index
        # that will exist after the swap.
        stale_paths = sorted(changed_paths | removed_paths)
        stale_chunk_count = 0
        for window in _in_list_windows(stale_paths):
            placeholders = ", ".join("?" for _ in window)
            stale_rows = await store.fetch_one(
                f"SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ? AND file_path IN ({placeholders})",
                [repo_id, *window],
            )
            stale_chunk_count += int((stale_rows or {}).get("count", 0))
        retained_chunk_count = max(0, existing_count - stale_chunk_count)
        if not defer_replacement:
            await replace_changed_file_chunks(store, repo_id, changed_paths, removed_paths)

        # Selection order is importance order; chunk in that order so a chunk
        # budget that runs out only ever drops the least important files.
        relative_of = {file_path: os.path.relpath(file_path, repo_path).replace(os.sep, "/") for file_path in files}
        changed_files = [file_path for file_path in files if relative_of[file_path] in changed_paths]
        expected_new_chunks = int(-(-sum(
            estimate_chunks(int(manifest[relative_of[path]]["byte_size"])) for path in changed_files
        ) // 1))
        expected_total = retained_chunk_count + expected_new_chunks

        chunked_paths: set[str] = set()
        chunk_budget_paths: list[str] = []
        new_chunk_count = 0
        semantic_index_warning = None
        chunk_started = time.perf_counter()
        keyword_published = False
        # A fresh run id per attempt so cleanup never matches rows written by
        # a previous attempt of the same durable job.
        chunk_id_prefix = f"{job_id or 'ingest'}-{uuid4()}-"
        if defer_replacement:
            await begin_index_run(store, repo_id, job_id, chunk_id_prefix)
            staged_run = True
        else:
            # Visible for early keyword search, but registered so a stop or
            # crash discards it instead of presenting it as a finished index.
            await begin_index_run(store, repo_id, job_id, chunk_id_prefix, initial=True)
            initial_run = True
        await update_index_state(store, repo_id, phase="chunking")
        cache_enabled = int(selection_report.get("eligible_bytes", 0)) <= settings.embedding_cache_max_repository_bytes
        file_vectors = FileVectorAccumulator()
        vector_paths: set[str] = set()
        current_path: str | None = None

        async def persist_keyword_chunks(records: list[dict]) -> None:
            step = max(1, settings.chunk_insert_batch_size)
            for offset in range(0, len(records), step):
                await raise_if_ingestion_cancelled(store, repo_id, claim_token)
                await heartbeat_job(store, job_id, claim_token)
                db_started = time.perf_counter()
                await store.insert_chunks(records[offset:offset + step])
                metrics["database_ms"] += int((time.perf_counter() - db_started) * 1000)
                metrics["db_batches"] += 1

        async def persist_embedding_updates(records: list[dict]) -> None:
            updater = getattr(store, "update_chunk_embeddings", None)
            if not updater:
                return
            step = max(1, settings.chunk_insert_batch_size)
            for offset in range(0, len(records), step):
                await raise_if_ingestion_cancelled(store, repo_id, claim_token)
                db_started = time.perf_counter()
                await updater(records[offset:offset + step])
                metrics["database_ms"] += int((time.perf_counter() - db_started) * 1000)
                metrics["db_batches"] += 1

        async def flush_file_vectors(final: bool = False) -> None:
            writer = getattr(store, "upsert_file_vectors", None)
            completed = file_vectors.pop_completed(None if final else ({current_path} if current_path else None))
            if writer and completed:
                await writer(repo_id, completed, timestamp())
                vector_paths.update(path for path, _vector, _count in completed)

        chunk_buffer: list[dict] = []
        embedded_count = 0
        embeddings_disabled = False

        async def publish_refresh_progress() -> None:
            """Real progress of a refresh from its staged (hidden) rows."""
            staged = await store.fetch_one(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded "
                "FROM chunks WHERE repo_id = ? AND id LIKE ?",
                [repo_id, f"{chunk_id_prefix}%"],
            ) or {}
            staged_total = int(staged.get("total") or 0)
            done = staged_total if embeddings_disabled else int(staged.get("embedded") or 0)
            denominator = max(staged_total, expected_new_chunks, 1)
            await update_index_state(
                store, repo_id, phase="embedding", semantic_progress=min(99, int(done * 100 / denominator)),
            )

        async def flush_chunk_buffer() -> None:
            nonlocal chunk_buffer, embedded_count, semantic_index_warning, embeddings_disabled, keyword_published
            if not chunk_buffer:
                return
            batch = chunk_buffer
            chunk_buffer = []
            records = [
                {
                    "id": f"{chunk_id_prefix}{uuid4()}", "repo_id": repo_id, "file_path": chunk["file_path"],
                    "start_line": chunk["start_line"], "end_line": chunk["end_line"],
                    "language": chunk["language"], "symbols": chunk.get("symbols", []),
                    "content": chunk["content"], "embedding": None,
                    "content_hash": hashlib.sha256(str(chunk.get("content") or "").encode("utf-8")).hexdigest(),
                }
                for chunk in batch
            ]
            keyword_started = time.perf_counter()
            await persist_keyword_chunks(records)
            await raise_if_ingestion_cancelled(store, repo_id, claim_token)
            metrics["keyword_index_ms"] += int((time.perf_counter() - keyword_started) * 1000)
            if not keyword_published:
                keyword_published = True
                if defer_replacement:
                    # Replacement rows stay hidden; the previous index keeps
                    # answering questions until the swap.
                    await update_repo(
                        store, repo_id, status="chunking", chunk_count=existing_count,
                        error_message="Refreshing the index. The previous index stays available until the new one is ready.",
                    )
                else:
                    count = await get_repo_chunk_count(store, repo_id)
                    await update_repo(
                        store, repo_id, status="ready", chunk_count=count,
                        error_message="Ready to explore with keyword search. The semantic index is still being built.",
                    )
                    await update_index_state(
                        store, repo_id, phase="searchable", keyword_files=len(chunked_paths), keyword_chunks=count,
                        semantic_progress=0, embedding_status="running", searchable_at=timestamp(),
                    )
            if embeddings_disabled:
                embedded_chunks = records
            else:
                try:
                    embedding_started = time.perf_counter()
                    embedded_chunks = await embed_repository_chunks(
                        store, repo_id, records, job_id, claim_token,
                        progress_offset=embedded_count,
                        progress_total=max(expected_total, retained_chunk_count + new_chunk_count, 1),
                        metrics=metrics, cache_enabled=cache_enabled,
                    )
                    metrics["embedding_ms"] += int((time.perf_counter() - embedding_started) * 1000)
                    metrics["chunks_embedded"] += sum(1 for chunk in embedded_chunks if chunk.get("embedding") is not None)
                    metrics["embedding_cache_hits"] += sum(1 for chunk in embedded_chunks if chunk.get("_embedding_cache_hit"))
                    metrics["embedding_skipped"] = int(metrics.get("embedding_skipped", 0)) + sum(1 for chunk in embedded_chunks if chunk.get("_embedding_skipped"))
                    await persist_embedding_updates(embedded_chunks)
                    for chunk in embedded_chunks:
                        if chunk.get("embedding") is not None:
                            file_vectors.add(chunk["file_path"], chunk["embedding"])
                    await flush_file_vectors()
                except EmbeddingUnavailableError as error:
                    # A provider outage is job-wide, not batch-local; do not
                    # spend another round of retries on every later buffer.
                    embeddings_disabled = True
                    logger.warning("NVIDIA embeddings unavailable for %s; using keyword retrieval: %s", repo_id, error)
                    semantic_index_warning = (
                        "Semantic search is temporarily unavailable because the embedding provider did not respond. "
                        "Keyword search works; re-index later to add semantic search."
                    )
                    metrics["embedding_failures"] += 1
                    embedded_chunks = records
                    await update_repo(
                        store, repo_id,
                        error_message=(
                            "Building the keyword index. Semantic search is unavailable right now because the "
                            "embedding provider did not respond; keyword search works."
                        ),
                    )
            if not defer_replacement:
                published_total, _progress = await publish_index_progress(
                    store, repo_id, keyword_files=len(chunked_paths),
                    embedding_status="degraded" if embeddings_disabled else None,
                    expected_total=expected_total,
                )
                await update_repo(store, repo_id, chunk_count=published_total)
            else:
                await publish_refresh_progress()
            embedded_count += len(embedded_chunks)
            metrics["chunks_created"] += len(records)
            metrics["peak_rss_mb"] = max(metrics["peak_rss_mb"], round(rss_mb(), 1))
            del embedded_chunks, records, batch

        file_index = 0
        processed_paths: set[str] = set()
        pressure = "normal"
        chunk_cap = max(1, int(settings.max_repository_chunks))
        budget_stop_path: str | None = None
        partial_path: str | None = None
        async for file_path, file_chunks in iter_chunked_files(changed_files, repo_path):
            await raise_if_ingestion_cancelled(store, repo_id, claim_token)
            relative_path = relative_of.get(file_path) or os.path.relpath(file_path, repo_path).replace(os.sep, "/")
            if relative_path not in processed_paths:
                processed_paths.add(relative_path)
                file_index += 1
            current_path = relative_path
            if not file_chunks:
                continue
            if retained_chunk_count + new_chunk_count + len(file_chunks) > chunk_cap:
                # The chunk budget is spent. Files arrive in importance order,
                # so everything after this point is less important. A streamed
                # file that crosses the budget mid-way keeps the chunks already
                # indexed and is retried in full on the next refresh.
                budget_stop_path = relative_path
                if relative_path in chunked_paths:
                    partial_path = relative_path
                break
            chunked_paths.add(relative_path)
            new_chunk_count += len(file_chunks)
            chunk_buffer.extend(file_chunks)
            configured_buffer = max(1, settings.embedding_chunk_buffer_size)
            # Sampling RSS every 64 chunks reacts well before the next provider
            # batch without paying a probe per streamed chunk.
            if new_chunk_count == len(file_chunks) or new_chunk_count % 64 == 0:
                pressure = pressure_level()
            if pressure in {"elevated", "warning", "critical"}:
                configured_buffer = max(16, configured_buffer // 2)
            if len(chunk_buffer) >= configured_buffer:
                await flush_chunk_buffer()
            if file_index == len(changed_files) or file_index % max(1, settings.embedding_progress_interval_batches * 10) == 0:
                if defer_replacement or not keyword_published:
                    await update_repo(
                        store, repo_id,
                        error_message=f"Reading source files ({file_index:,} of {len(changed_files):,} changed files).",
                    )
            del file_chunks

        current_path = None
        # Every section is known now: report progress against the real total,
        # not the size-based estimate.
        expected_total = retained_chunk_count + new_chunk_count
        expected_new_chunks = new_chunk_count
        await flush_chunk_buffer()
        await flush_file_vectors(final=True)
        metrics["chunk_ms"] = int((time.perf_counter() - chunk_started) * 1000)

        # Changed files never indexed because the chunk budget ran out (the
        # file that hit the budget plus every later one). Drop them from the
        # manifest so the next refresh treats them as new instead of
        # "unchanged"; their previous rows are removed at the swap.
        if budget_stop_path is not None:
            for file_path in changed_files:
                relative_path = relative_of[file_path]
                if relative_path in chunked_paths:
                    continue
                if relative_path not in processed_paths or relative_path == budget_stop_path:
                    chunk_budget_paths.append(relative_path)
        for relative_path in chunk_budget_paths:
            manifest.pop(relative_path, None)
        if partial_path:
            # Keep the partial rows searchable, but force a full retry later.
            manifest.pop(partial_path, None)
        removed_paths |= set(chunk_budget_paths) & set(previous_manifest)

        chunking_failed_paths = sorted((changed_paths - chunked_paths) - set(chunk_budget_paths))
        reasons = dict(selection_report.get("excluded_reasons") or {})
        if chunking_failed_paths:
            reasons["empty_or_unreadable"] = int(reasons.get("empty_or_unreadable", 0)) + len(chunking_failed_paths)
        if chunk_budget_paths:
            reasons["chunk_budget"] = int(reasons.get("chunk_budget", 0)) + len(chunk_budget_paths)
        if partial_path:
            reasons["partially_indexed"] = int(reasons.get("partially_indexed", 0)) + 1
        if chunk_budget_paths:
            categories = dict(selection_report.get("budget_omitted_by_category") or {})
            for relative_path in chunk_budget_paths:
                category = file_category(relative_path)
                categories[category] = int(categories.get(category, 0)) + 1
            selection_report["budget_omitted_by_category"] = categories
        if chunking_failed_paths or chunk_budget_paths:
            selection_report["excluded_reasons"] = dict(sorted(reasons.items()))
            selection_report["excluded_files"] = int(selection_report.get("excluded_files", 0)) + len(chunking_failed_paths) + len(chunk_budget_paths)
            selection_report["eligible_files"] = max(0, int(selection_report.get("eligible_files", 0)) - len(chunk_budget_paths))
            selection_report["excluded_paths"] = sorted({
                *selection_report.get("excluded_paths", []), *chunking_failed_paths, *chunk_budget_paths,
            })[: max(0, settings.excluded_paths_limit)]
        if not chunked_paths and existing_count == 0:
            raise ValueError("No readable source code chunks were created from this repository.")

        await raise_if_ingestion_cancelled(store, repo_id, claim_token)
        if defer_replacement:
            # Swap: delete only the previous version of changed/removed paths.
            # This run's rows (job prefix) become visible once the staging
            # marker is removed.
            await swap_in_replacement(
                store, repo_id, changed_paths | set(chunk_budget_paths), removed_paths, chunk_id_prefix,
                stale_vector_paths=(changed_paths - vector_paths) | set(chunk_budget_paths),
            )
            staged_run = False
            replacement_swapped = True
        await persist_file_manifest(store, repo_id, manifest)
        await persist_dependency_manifest(store, repo_id, dependencies)
        indexed_count = await store.fetch_one(
            "SELECT COUNT(DISTINCT file_path) AS count FROM chunks WHERE repo_id = ?", [repo_id]
        )
        indexed_file_count = int((indexed_count or {}).get("count", 0))
        await persist_coverage(store, repo_id, selection_report, indexed_file_count)
        summary = _selection_summary(
            selection_report, byte_budget=byte_budget, quota_limited=quota_limited, chunk_budget_paths=chunk_budget_paths,
        )
        snapshot_ref = getattr(snapshot, "ref", None)
        if snapshot_ref:
            summary["ref"] = snapshot_ref
            requested = parse_github_ref(source_url)
            summary["ref_kind"] = requested.kind if requested else "tree"
        # The most important budget omissions are only read by the coverage
        # endpoint; the summary itself stays small because it is polled.
        snapshot_selection = dict(summary)
        sample = list(selection_report.get("budget_omitted_sample") or [])
        known = {item.get("path") for item in sample}
        sample.extend(
            {"path": path, "priority": None, "category": file_category(path), "reason": "chunk_budget"}
            for path in chunk_budget_paths[:50] if path not in known
        )
        if sample:
            snapshot_selection["budget_omitted_sample"] = sample
        await persist_snapshot(store, repo_id, commit_sha=snapshot.commit_sha, source=snapshot.source, selection=snapshot_selection)

        await heartbeat_job(store, job_id, claim_token)
        # Metadata is deliberately out of the critical path. Source search is
        # already durable; a restart can safely regenerate this cache later.
        spawn_background(_background_build_kt_cache(store, repo_id))
        _, semantic_progress = await publish_index_progress(
            store, repo_id, keyword_files=indexed_file_count,
            embedding_status="degraded" if embeddings_disabled else None,
            allow_ready=True, skipped=int(metrics.get("embedding_skipped", 0)),
        )
        completion_message = _completion_message(summary, semantic_index_warning)
        if initial_run:
            # The first index is complete: it is now the published index.
            await end_index_run(store, repo_id)
            initial_run = False
        final_count = await get_repo_chunk_count(store, repo_id)
        await update_repo(store, repo_id, status="ready", chunk_count=final_count, error_message=completion_message)
        metrics["total_ms"] = int((time.perf_counter() - started_at) * 1000)
        metrics["peak_rss_mb"] = max(metrics["peak_rss_mb"], round(rss_mb(), 1))
        await persist_ingestion_metrics(store, job_id, repo_id, metrics)
        return True
    except IngestionCancelledError:
        logger.info("Repository ingestion cancelled for %s", repo_id)
        if repo_id:
            if replacement_swapped:
                # The cancellation raced the final swap; the new rows are now
                # the published index, so finish the durable state instead.
                chunk_count = await get_repo_chunk_count(store, repo_id)
                await update_repo(
                    store, repo_id, status="ready", chunk_count=chunk_count,
                    error_message="The refresh completed before cancellation was received.",
                )
                try:
                    await publish_index_progress(store, repo_id, allow_ready=True)
                except Exception:
                    logger.debug("Could not publish completed cancellation state", exc_info=True)
                return True
            try:
                if chunk_id_prefix and not staged_run:
                    await store.execute(
                        "DELETE FROM chunks WHERE repo_id = ? AND id LIKE ?", [repo_id, f"{chunk_id_prefix}%"],
                    )
                await finish_cancelled_repository(store, repo_id)
            except Exception:
                logger.warning("Could not finish cancellation cleanup for %s", repo_id, exc_info=True)
        return False
    except Exception as error:
        safe_types = (ValueError, RepositoryValidationError, RepositoryCloneError, RepositoryQuotaExceededError)
        error_message = str(error).strip() if isinstance(error, safe_types) else explain_database_error(error)
        error_message = error_message or "Repository ingestion could not be completed."
        if isinstance(error, safe_types):
            logger.warning("Repository ingestion stopped: %s", error_message)
        else:
            logger.exception("Repository ingestion failed")
        if repo_id:
            try:
                # A failed run must not leave half of a new index behind. The
                # job-scoped prefix lets an existing index survive because its
                # rows never share this prefix.
                if chunk_id_prefix and not replacement_swapped:
                    await store.execute(
                        "DELETE FROM chunks WHERE repo_id = ? AND id LIKE ?", [repo_id, f"{chunk_id_prefix}%"],
                    )
                if replacement_swapped:
                    count = await get_repo_chunk_count(store, repo_id)
                    await update_repo(store, repo_id, status="ready", chunk_count=count,
                                      error_message=f"The refresh finished with a warning: {error_message[:300]}")
                elif not await restore_published_index(
                    store, repo_id, f"The refresh failed: {error_message[:300]} The previous index remains available.",
                ):
                    await update_repo(store, repo_id, status="failed", error_message=error_message[:500])
                    await update_index_state(store, repo_id, phase="failed", embedding_status="degraded")
                metrics["total_ms"] = int((time.perf_counter() - started_at) * 1000)
                metrics["peak_rss_mb"] = max(metrics["peak_rss_mb"], round(rss_mb(), 1))
                await persist_ingestion_metrics(store, job_id, repo_id, metrics)
            except Exception:
                logger.warning("Could not persist failed ingestion state for %s", repo_id, exc_info=True)
        return False
    finally:
        if snapshot is not None:
            try:
                await run_blocking(cleanup_repo, snapshot.path)
            except Exception:
                logger.warning("Could not clean up temporary repository directory")
