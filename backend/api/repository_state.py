"""One consistent, tenant-scoped view of repository and indexing state.

The UI needs three independent facts that the legacy ``status`` string mixed
together: whether a job is running (``is_indexing``), whether questions can be
answered (``queryable``), and the human-readable phase. They are derived here
from the durable job row, the published chunk count, and the index state so
every endpoint reports the same thing.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, NoReturn

from fastapi import HTTPException

from database import DatabaseConfigurationError, explain_database_error
from ingest.cloner import RepositoryValidationError, parse_github_repository
from ingest.pipeline import ACTIVE_REPOSITORY_STATUSES, INDEX_PHASES_IN_PROGRESS, is_initial_run

_ACTIVE_JOB = {"queued", "processing"}

# The indexing steps and their labels mirror ``frontend/src/lib/phase.js`` so
# a label computed here reads exactly like the one the UI already shows.
INDEX_STEPS = (
    ("queued", "Queued"),
    ("cloning", "Downloading snapshot"),
    ("scanning", "Selecting files"),
    ("manifesting", "Comparing changes"),
    ("chunking", "Building keyword search"),
    ("searchable", "Semantic index"),
    ("ready", "Ready"),
)
_STEP_INDEX = {
    "queued": 0, "cloning": 1, "scanning": 2, "manifesting": 3, "chunking": 4, "keyword": 4,
    "searchable": 5, "embedding": 5, "ready": 6,
}
_STEP_DETAILS = (
    "Waiting for an indexing worker to pick this repository up.",
    "Downloading the repository snapshot from GitHub.",
    "Choosing which files to index, most important first.",
    "Checking which files changed so unchanged ones can be reused.",
    "Reading the code into searchable sections with exact line numbers.",
    "Keyword search is ready, so you can ask questions now. Meaning-based (semantic) search is still being added.",
    "Fully indexed.",
)
# A processing job heartbeats its lease throughout a run (see
# ``pipeline._watch_job``); this long without one means the worker stopped.
_STALLED_AFTER_SECONDS = 180
# Messages the pipeline publishes while a run is in progress. Once the run is
# over they are stale and must not be shown as the repository's status.
_IN_PROGRESS_MESSAGE_PREFIXES = (
    "Downloading the repository snapshot",
    "Refreshing the index.",
    "Hashing files and resolving imports",
    "Reading source files (",
    "Building the semantic index:",
    "Building the keyword index.",
    "Ready to explore with keyword search.",
    "Stopping indexing",
)
# Operator-facing database explanations (``database.explain_database_error``)
# can be stored as a failed run's message. Users see plain language instead.
_STORAGE_MESSAGE_REWRITES = (
    ("The Turso database connection is temporarily unavailable. Please retry in a moment.",
     "The database was briefly unreachable."),
    ("Turso schema is not initialized. Run the SQL files in turso/ or enable AUTO_MIGRATE, then restart the backend.",
     "A server problem interrupted indexing."),
    ("The configured embedding dimension does not match the Turso schema. "
     "Set EMBEDDING_DIMENSION=2048 and run the current turso/00_init.sql.",
     "A server problem interrupted indexing."),
    ("Turso rejected the database credential. Replace TURSO_AUTH_TOKEN and restart the backend.",
     "A server problem interrupted indexing."),
    ("The database could not complete that request. Please try again shortly.",
     "A temporary server problem interrupted indexing."),
)
_OPERATOR_JARGON = ("Turso", "TURSO_", "AUTO_MIGRATE", "EMBEDDING_DIMENSION", "Traceback", "turso/")

REPOSITORY_SELECT = (
    "SELECT r.id, r.repo_name, r.github_url, r.status AS repo_status, r.chunk_count, r.error_message, "
    "r.created_at, r.updated_at, "
    "COALESCE(s.phase, r.status) AS index_phase, COALESCE(s.semantic_progress, 0) AS semantic_progress, "
    "COALESCE(s.embedding_status, 'complete') AS embedding_status, COALESCE(s.keyword_files, 0) AS keyword_files, "
    "COALESCE(s.keyword_chunks, 0) AS keyword_chunks, s.searchable_at, s.semantic_ready_at, "
    "j.status AS job_status, j.attempts AS job_attempts, "
    "COALESCE(c.total_seen_files, 0) AS total_seen_files, COALESCE(c.eligible_files, 0) AS eligible_files, "
    "COALESCE(c.indexed_files, 0) AS indexed_files, COALESCE(c.excluded_files, 0) AS excluded_files, "
    "COALESCE(c.excluded_bytes, 0) AS excluded_bytes, COALESCE(c.excluded_reasons, '{}') AS excluded_reasons, "
    "snap.commit_sha, COALESCE(snap.selection_json, '{}') AS selection_json, snap.fetched_at AS indexed_at, "
    "runs.chunk_prefix AS run_prefix, runs.started_at AS run_started_at, s.updated_at AS index_updated_at, "
    "j.created_at AS job_created_at, j.claimed_at AS job_claimed_at, j.heartbeat_at AS job_heartbeat_at "
    "FROM repos r "
    "LEFT JOIN repo_coverage c ON c.repo_id = r.id "
    "LEFT JOIN repo_index_state s ON s.repo_id = r.id "
    "LEFT JOIN ingestion_jobs j ON j.repo_id = r.id "
    "LEFT JOIN repo_snapshots snap ON snap.repo_id = r.id "
    "LEFT JOIN repo_index_runs runs ON runs.repo_id = r.id "
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def friendly_stored_message(message: Any) -> str | None:
    """A stored status/error message in plain language (no settings or SQL)."""
    text = " ".join(str(message or "").split())
    if not text:
        return None
    for operator, friendly in _STORAGE_MESSAGE_REWRITES:
        text = text.replace(operator, friendly)
    if any(marker in text for marker in _OPERATOR_JARGON):
        return "A server problem interrupted indexing."
    return text


def eta_label(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return "Less than a minute left"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"About {minutes} minute{'s' if minutes != 1 else ''} left"
    hours = round(seconds / 3600)
    if hours > 6:
        return "Several hours left"
    return f"About {max(1, hours)} hour{'s' if max(1, hours) != 1 else ''} left"


def _estimate_remaining_seconds(view: dict[str, Any], row: dict[str, Any], now: datetime) -> float | None:
    """Remaining time of the section/embedding loop from its real progress.

    Keyword sections and embeddings are written batch by batch from the run's
    start, so ``semantic_progress`` over the run's elapsed time is an honest
    rate. Earlier phases have no measurable progress and get no estimate.
    """
    if not view["is_indexing"] or view["status"] not in {"searchable", "embedding"}:
        return None
    progress = view["semantic_progress"]
    started = _parse_time(row.get("run_started_at"))
    if started is None or not 3 <= progress < 100:
        return None
    elapsed = (now - started).total_seconds()
    if elapsed < 20:
        return None
    return min(elapsed * (100 - progress) / progress, 24 * 3600.0)


def _failure_next_step(message: str | None) -> tuple[str, str]:
    text = (message or "").lower()
    if "workspace includes up to" in text:
        return "free_space", "Delete a repository you no longer need or upgrade your plan, then re-index."
    if "not found" in text or "make sure the repository is public" in text:
        return "check_url", "Check the URL (and the branch, tag or commit, if you linked one), then add the repository again."
    if "download limit" in text:
        return "check_url", "This repository is larger than the download limit. Try a smaller repository."
    return "reindex", "Re-index to try again."


def describe_index_state(view: dict[str, Any], row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """User-facing description of where indexing stands and what works now.

    Everything here is derived from the same row as ``status``; it adds the
    words, step position, rough time estimate and next action a progress UI
    needs so every client describes a repository the same way.
    """
    now = now or datetime.now(UTC)
    status = view["status"]
    phase = view["index_phase"]
    indexing = view["is_indexing"]
    queryable = view["queryable"]
    refreshing = view["refreshing"]
    progress = view["semantic_progress"]
    step = _STEP_INDEX.get(status, _STEP_INDEX.get(phase, 0))
    repo_status = str(row.get("repo_status") or "")
    stopping = status == "cancelled" and repo_status not in {"cancelled", "failed"}

    message = friendly_stored_message(row.get("error_message"))
    if message and not indexing and not stopping and message.startswith(_IN_PROGRESS_MESSAGE_PREFIXES):
        message = None  # left over from a run that already ended

    keyword_only = (progress < 100 and view["embedding_status"] == "degraded") or "searchable" in {status, phase}
    semantic_ready = queryable and view["embedding_status"] != "degraded" and (
        refreshing or (not indexing and not keyword_only)
    )

    stalled = False
    if indexing and view["job_status"] == "processing":
        beat = _parse_time(row.get("job_heartbeat_at")) or _parse_time(row.get("job_claimed_at"))
        stalled = beat is not None and (now - beat).total_seconds() > _STALLED_AFTER_SECONDS

    if status == "failed":
        label = "Needs attention"
        detail = message or "Indexing didn't finish."
        action, next_step = _failure_next_step(message)
        percent = 0
    elif status == "cancelled":
        label = "Stopping" if stopping else "Stopped"
        detail = "Stopping indexing. This takes a few seconds." if stopping else "Indexing was stopped before it finished."
        action, next_step = ("wait", "Wait a moment while indexing stops.") if stopping else (
            "reindex", "Re-index whenever you're ready to build the index.")
        percent = 0
    elif not indexing:
        label = "Ready · keyword search" if keyword_only else "Ready"
        if keyword_only:
            detail = "Questions, code search and file browsing work. Meaning-based (semantic) search isn't available for this index."
            action, next_step = "ask", "Ask a question now. Re-index later to add semantic search."
        else:
            detail = "Fully indexed: questions, code search and file browsing all work."
            action, next_step = "ask", "Ask a question, search the code, or open a file."
        percent = 100
    else:
        step_label = INDEX_STEPS[step][1]
        if refreshing:
            label = f"Refreshing · {step_label}"
        elif "searchable" in {status, phase}:
            label = f"Ready · semantic {progress}%"
        else:
            label = step_label
        if stalled:
            detail = (
                "Indexing hasn't reported progress for a few minutes. It resumes automatically if the worker "
                "restarted; if it stays stuck, stop it and re-index."
            )
        elif refreshing:
            base = _STEP_DETAILS[min(step, 4)] if step < 5 else "Building the refreshed index."
            detail = f"{base} Questions use the current index until the refresh finishes."
        else:
            detail = _STEP_DETAILS[step]
        if queryable and refreshing:
            action, next_step = "ask", "Ask questions now; answers use the current index until the refresh finishes."
        elif queryable:
            action, next_step = "ask", "Ask questions now. Answers get better once semantic search finishes."
        else:
            action, next_step = "wait", "You can ask questions as soon as keyword search is ready."
        percent = round(55 + progress * 0.44) if step >= 5 else round(step / 5 * 55)

    remaining = _estimate_remaining_seconds(view, row, now) if not stalled else None
    claimed = row.get("job_claimed_at") if indexing and view["job_status"] == "processing" else None
    return {
        "phase_label": label,
        "phase_detail": detail,
        # 1-based position among the seven steps (Queued ... Ready).
        "phase_step": step + 1,
        "phase_steps": len(INDEX_STEPS),
        "progress_percent": max(0, min(100, int(percent))),
        "status_message": detail if stalled else (message or detail),
        "next_action": action,
        "next_step": next_step,
        "eta_seconds": int(round(remaining)) if remaining is not None else None,
        "eta_label": eta_label(remaining),
        "stalled": stalled,
        "indexing_started_at": claimed,
        "searchable_at": row.get("searchable_at"),
        "semantic_ready_at": row.get("semantic_ready_at"),
        "progress_updated_at": row.get("index_updated_at"),
        # What already works, so a UI can enable features before indexing ends.
        "capabilities": {
            "ask": queryable,
            "search": queryable,
            "browse_files": queryable,
            "semantic_search": bool(semantic_ready),
            "propose_changes": queryable,
        },
        "message": message,
    }


def present_repository(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Translate a joined repository row into the public API shape."""
    phase = str(row.get("index_phase") or row.get("repo_status") or "queued")
    repo_status = str(row.get("repo_status") or phase)
    job_status = row.get("job_status")
    chunk_count = _int(row.get("chunk_count"))
    run_prefix = row.get("run_prefix")
    initial_run = is_initial_run(run_prefix)
    staged_refresh = bool(run_prefix) and not initial_run
    is_indexing = job_status in _ACTIVE_JOB or (job_status is None and repo_status == "queued")
    if is_indexing:
        if phase in INDEX_PHASES_IN_PROGRESS or phase == "searchable":
            status = phase
        elif phase == "ready" and chunk_count > 0:
            # The worker publishes "ready" a moment before it closes the job
            # row; never fall back to "queued" in that window.
            status = "searchable"
        else:
            status = "queued"
    elif job_status == "cancelled" and repo_status in ACTIVE_REPOSITORY_STATUSES | {"ready"} and (
        chunk_count == 0 or initial_run
    ):
        # Stop was requested and the worker is still winding down (or has
        # gone): this is a stop in progress, never a failure.
        status = "cancelled"
    elif repo_status in {"failed", "cancelled"} and chunk_count == 0:
        status = repo_status
    elif chunk_count > 0:
        status = "ready"
    else:
        status = repo_status if repo_status in {"failed", "cancelled", "ready"} else "failed"
    try:
        owner, name = parse_github_repository(str(row.get("github_url") or ""))
        full_name = f"{owner}/{name}"
    except (RepositoryValidationError, ValueError):
        owner, name, full_name = "", "", ""
    selection = row.get("selection_json") if isinstance(row.get("selection_json"), dict) else {}
    # The omitted-file sample is served by the coverage endpoint only; this
    # shape is polled for every repository.
    selection = {key: value for key, value in selection.items() if key != "budget_omitted_sample"}
    # The pipeline records the indexed branch/tag/commit in the snapshot
    # summary; it is null until a snapshot with a pinned ref is published.
    ref = selection.get("ref") if isinstance(selection.get("ref"), str) and selection.get("ref") else None
    ref_kind = selection.get("ref_kind") if ref and isinstance(selection.get("ref_kind"), str) else None
    view = {
        "id": row.get("id"),
        "repo_name": row.get("repo_name"),
        "github_url": row.get("github_url"),
        "owner": owner,
        "full_name": full_name,
        "status": status,
        "index_phase": phase,
        "job_status": job_status,
        "is_indexing": bool(is_indexing),
        "queryable": chunk_count > 0 and not (job_status == "cancelled" and initial_run),
        "chunk_count": chunk_count,
        "error_message": row.get("error_message"),
        "message": row.get("error_message"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "semantic_progress": max(0, min(100, _int(row.get("semantic_progress")))),
        "embedding_status": row.get("embedding_status") or "complete",
        "keyword_files": _int(row.get("keyword_files")),
        "keyword_chunks": _int(row.get("keyword_chunks")),
        "total_seen_files": _int(row.get("total_seen_files")),
        "eligible_files": _int(row.get("eligible_files")),
        "indexed_files": _int(row.get("indexed_files")),
        "excluded_files": _int(row.get("excluded_files")),
        "excluded_bytes": _int(row.get("excluded_bytes")),
        "excluded_reasons": row.get("excluded_reasons") if isinstance(row.get("excluded_reasons"), dict) else {},
        "commit_sha": row.get("commit_sha"),
        "ref": ref,
        "ref_kind": ref_kind,
        "indexed_at": row.get("indexed_at"),
        "selection": selection,
        # Only a run that replaces an earlier completed index is a refresh; a
        # first index that is already keyword-searchable is not.
        "refreshing": bool(
            is_indexing and chunk_count > 0 and not initial_run
            and (staged_refresh or row.get("commit_sha") or row.get("indexed_at"))
        ),
    }
    # ``message`` is the stored text in plain language (stale progress from a
    # finished run removed); ``error_message`` keeps the stored value as is.
    view.update(describe_index_state(view, row, now))
    return view


async def list_repositories(store, user_id: str) -> list[dict[str, Any]]:
    rows = await store.fetch_all(REPOSITORY_SELECT + "WHERE r.user_id = ? ORDER BY r.created_at DESC", [user_id])
    return [present_repository(row) for row in rows]


async def fetch_owned_repository(store, user_id: str, repo_name: str) -> dict[str, Any]:
    """Return the presented repository plus internal fields, or raise 404.

    ``staging_prefix`` is internal: retrieval uses it to hide the rows of an
    in-progress refresh. It is never returned to clients.
    """
    row = await store.fetch_one(REPOSITORY_SELECT + "WHERE r.user_id = ? AND r.repo_name = ?", [user_id, repo_name])
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found.")
    presented = present_repository(row)
    run_prefix = row.get("run_prefix")
    # A first index's rows are visible while it builds; only a refresh's
    # replacement rows are hidden from retrieval.
    presented["staging_prefix"] = None if not run_prefix or is_initial_run(run_prefix) else run_prefix
    return presented


def public_view(repo: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in repo.items() if key != "staging_prefix"}


# Conflict messages raised below the API layer, in the words the UI shows.
_CONFLICT_REWRITES = {
    "Repository ingestion is already in progress.":
        "This repository is already being indexed. Wait for it to finish, or stop it first.",
}


def friendly_conflict(message: Any) -> str:
    text = str(message or "").strip()
    return _CONFLICT_REWRITES.get(text, text or "That can't be done right now. Refresh the page and try again.")


def raise_storage_error(error: Exception, action: str, log: logging.Logger) -> NoReturn:
    """Raise a safe, actionable HTTP error for a failed storage operation.

    ``action`` completes "Couldn't ..." (e.g. "load the repository overview").
    Operator detail (setting names, schema hints) goes to the log only; the
    user gets what happened and what to do next. Status codes are unchanged:
    503 for a missing/misconfigured database, 502 for a failed request.
    """
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, DatabaseConfigurationError) and not str(error).startswith("The Turso database connection"):
        log.error("Could not %s: %s", action, error)
        raise HTTPException(
            status_code=503,
            detail=f"Couldn't {action} because the service is temporarily unavailable. Try again in a few minutes.",
        ) from error
    log.exception("Could not %s", action)
    explanation = explain_database_error(error)
    if "temporarily unavailable" in explanation:
        detail = f"Couldn't {action} because the database is briefly unavailable. Wait a few seconds and try again."
    elif explanation.startswith("The database could not complete"):
        detail = f"Couldn't {action} right now. Try again in a moment."
    else:
        log.error("Storage needs operator attention: %s", explanation)
        detail = (
            f"Couldn't {action} because of a server problem. Try again later; "
            "if it keeps happening, contact support."
        )
    raise HTTPException(status_code=502, detail=detail) from error
