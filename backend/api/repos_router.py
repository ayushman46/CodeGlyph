"""Owned repository management backed by Turso."""

import json
import logging
import posixpath
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import get_current_user
from api.repository_state import (
    fetch_owned_repository,
    friendly_conflict,
    list_repositories,
    public_view,
    raise_storage_error,
)
from database import assert_turso_schema, get_turso_store
from ingest.pipeline import (
    IngestionConflictError,
    cancel_repository_indexing,
    delete_repository,
    enforce_ingestion_capacity,
    is_safe_repo_label,
    queue_existing_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class RenameRepositoryRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=200)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _normalise_impact_path(file_path: str) -> str:
    """Validate one relative source path before it reaches a SQL LIKE clause."""
    candidate = file_path.strip().replace("\\", "/")
    if not candidate or len(candidate) > 500 or "\x00" in candidate:
        raise HTTPException(status_code=422, detail="Enter a file path from this repository, such as src/app.ts.")
    if candidate.startswith("/"):
        raise HTTPException(
            status_code=422,
            detail="Enter the path relative to the repository root, without a leading slash (for example src/app.ts).",
        )
    normalised = posixpath.normpath(candidate)
    parts = normalised.split("/")
    if normalised in {"", "."} or normalised.startswith("../") or ".." in parts:
        raise HTTPException(
            status_code=422,
            detail="Enter a path inside this repository; \"..\" segments aren't allowed (for example src/app.ts).",
        )
    return normalised


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def owned_repo(store, user_id: str, repo_name: str):
    """Compatibility wrapper used by other routers and tests."""
    return await fetch_owned_repository(store, user_id, repo_name)


def _database_errors(action: str):
    """Map storage failures to safe responses with one consistent policy.

    ``action`` completes "Couldn't ..." in the message the user sees.
    """
    def handle(error: Exception):
        if isinstance(error, IngestionConflictError):
            raise HTTPException(status_code=409, detail=friendly_conflict(error)) from error
        raise_storage_error(error, action, logger)
    return handle


@router.get("/status/{repo_name}")
async def get_status(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        return public_view(await owned_repo(get_turso_store(), current_user.id, repo_name))
    except Exception as error:
        _database_errors("load the repository status")(error)


@router.get("/repos")
async def list_repos(current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        return await list_repositories(get_turso_store(), current_user.id)
    except Exception as error:
        _database_errors("load your repositories")(error)


@router.get("/repos/statuses")
async def list_repo_statuses(current_user=Depends(get_current_user)):
    """Return every repository's status in one tenant-scoped query.

    The payload deliberately omits the omitted-path list; that can be
    thousands of entries for a large repository and this endpoint is polled.
    """
    try:
        await assert_turso_schema()
        return await list_repositories(get_turso_store(), current_user.id)
    except Exception as error:
        _database_errors("refresh indexing progress")(error)


@router.get("/repos/{repo_name}/coverage")
async def repository_coverage(repo_name: str, current_user=Depends(get_current_user)):
    """Detailed coverage: why files were omitted and the indexing budget."""
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        row = await store.fetch_one(
            "SELECT excluded_paths, excluded_reasons FROM repo_coverage WHERE repo_id = ?", [repo["id"]]
        ) or {}
        snapshot = await store.fetch_one(
            "SELECT selection_json FROM repo_snapshots WHERE repo_id = ?", [repo["id"]]
        ) or {}
        selection = snapshot.get("selection_json")
        if isinstance(selection, str):
            try:
                selection = json.loads(selection)
            except ValueError:
                selection = {}
        if not isinstance(selection, dict):
            selection = {}
        excluded_paths = row.get("excluded_paths") if isinstance(row.get("excluded_paths"), list) else []
        budget_sample = selection.get("budget_omitted_sample")
        return {
            "repository": repo_name,
            "total_seen_files": repo["total_seen_files"],
            "eligible_files": repo["eligible_files"],
            "indexed_files": repo["indexed_files"],
            "excluded_files": repo["excluded_files"],
            "excluded_bytes": repo["excluded_bytes"],
            "excluded_reasons": row.get("excluded_reasons") if isinstance(row.get("excluded_reasons"), dict) else {},
            "excluded_paths": excluded_paths,
            # The path list is a bounded sample; these say how much of it is shown.
            "excluded_paths_shown": len(excluded_paths),
            "excluded_paths_total": repo["excluded_files"],
            # What the importance budget skipped, by kind, and the most
            # important skipped files first (path, category, priority).
            "budget_omitted_by_category": selection.get("budget_omitted_by_category")
            if isinstance(selection.get("budget_omitted_by_category"), dict) else {},
            "budget_omitted_sample": budget_sample if isinstance(budget_sample, list) else [],
            "selection": repo["selection"],
            "commit_sha": repo["commit_sha"],
        }
    except Exception as error:
        _database_errors("load the coverage report")(error)


@router.get("/repos/{repo_name}/impact")
async def repository_impact(
    repo_name: str,
    file_path: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    current_user=Depends(get_current_user),
):
    """Show the local files that would be affected by changing one source file.

    The graph is deliberately conservative: only imports resolved to another
    indexed file are returned. External packages and unresolved aliases are
    omitted instead of being presented as speculative breakage.
    """
    target = _normalise_impact_path(file_path)
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        escaped = _escape_like(target.lower())
        # During a refresh the replacement rows are hidden from every read
        # endpoint until the swap, exactly like search and chat.
        staging = repo.get("staging_prefix")
        stage_sql, stage_args = ("", []) if not staging else (" AND id NOT LIKE ?", [f"{staging}%"])
        target_rows = await store.fetch_all(
            "SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND (lower(file_path) = ? "
            "OR (lower(file_path) LIKE ? ESCAPE '\\' AND instr(lower(file_path), '/') > 0))"
            f"{stage_sql} ORDER BY file_path LIMIT ?",
            [repo["id"], target.lower(), f"%/{escaped}", *stage_args, limit],
        )
        target_paths = [str(row["file_path"]) for row in target_rows]
        if not target_paths:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No indexed file matches {target}. Check the spelling and use the path from the repository "
                    "root, or open the file tree to pick one. Skipped files are listed in the coverage report."
                ),
            )

        dependency_conditions = []
        dependency_args = [repo["id"]]
        for path in target_paths:
            escaped_path = _escape_like(path)
            dependency_conditions.append("(d.target_file = ? OR d.target_file LIKE ? ESCAPE '\\')")
            dependency_args.extend([path, f"%/{escaped_path}"])
        edges = await store.fetch_all(
            "SELECT d.source_file, d.target_file, d.import_name, d.line_number "
            "FROM repo_dependencies d "
            f"WHERE d.repo_id = ? AND ({' OR '.join(dependency_conditions)}) "
            "ORDER BY d.source_file, d.line_number, d.target_file LIMIT ?",
            [*dependency_args, limit * 4],
        )
        # Distinct dependent *files* (not chunk rows, which let a few large
        # files use up the limit) that are part of the published index.
        indexed_source = (
            "EXISTS (SELECT 1 FROM chunks c WHERE c.repo_id = d.repo_id AND c.file_path = d.source_file"
            + (" AND c.id NOT LIKE ?" if staging else "") + ")"
        )
        dependents_where = f"d.repo_id = ? AND ({' OR '.join(dependency_conditions)}) AND {indexed_source}"
        dependents_args = [*dependency_args, *stage_args]
        dependents = await store.fetch_all(
            f"SELECT DISTINCT d.source_file AS file_path FROM repo_dependencies d WHERE {dependents_where} "
            "ORDER BY d.source_file LIMIT ?",
            [*dependents_args, limit],
        )
        dependent_paths = sorted({str(row["file_path"]) for row in dependents})
        counted = await store.fetch_one(
            f"SELECT COUNT(DISTINCT d.source_file) AS count FROM repo_dependencies d WHERE {dependents_where}",
            dependents_args,
        )
        dependent_total = max(len(dependent_paths), int((counted or {}).get("count") or 0))
        # Imports of the target (what it depends on) give the other half of
        # the change surface.
        target_conditions = " OR ".join("d.source_file = ?" for _ in target_paths)
        imports = await store.fetch_all(
            f"SELECT DISTINCT d.target_file FROM repo_dependencies d WHERE d.repo_id = ? AND ({target_conditions}) "
            "ORDER BY d.target_file LIMIT ?",
            [repo["id"], *target_paths, limit],
        )
        truncated = dependent_total > len(dependent_paths)
        if dependent_paths:
            summary = (
                f"{dependent_total:,} indexed file(s) import the selected file"
                + (f" (showing {len(dependent_paths):,})." if truncated else ".")
            )
        else:
            summary = "No other indexed file imports the selected file."
        summary += (
            " Counts include resolved local imports (relative paths, workspace packages and tsconfig path "
            "aliases); external packages and computed import paths are not included."
        )
        return {
            "repository": repo_name,
            "target_files": target_paths,
            "dependent_files": dependent_paths,
            "dependent_count": dependent_total,
            "truncated": truncated,
            "imported_files": [str(row["target_file"]) for row in imports],
            "edges": edges,
            "summary": summary,
        }
    except Exception as error:
        _database_errors("analyze the change impact")(error)


@router.delete("/repos/{repo_name}")
async def delete_repo(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        await delete_repository(store, repo["id"], current_user.id)
    except Exception as error:
        _database_errors("delete the repository")(error)
    return {"message": f"Deleted {repo_name} and its index."}


@router.patch("/repos/{repo_name}")
async def rename_repo(repo_name: str, payload: RenameRepositoryRequest, current_user=Depends(get_current_user)):
    new_name = " ".join(payload.repo_name.split())
    if not new_name:
        raise HTTPException(status_code=422, detail="Enter a name with at least one letter or number.")
    if any(character in new_name for character in ("/", "\\", "\x00", "?", "#", "%")):
        raise HTTPException(status_code=422, detail="Names can't contain /, \\, ?, # or %. Try a hyphen instead.")
    if not is_safe_repo_label(new_name) or any(ord(character) < 0x20 or ord(character) == 0x7F for character in new_name):
        # Browsers resolve "." and ".." path segments (even as %2e) before
        # sending a request, so such a repository could never be reached.
        raise HTTPException(
            status_code=422, detail="Names can't be only dots or contain control characters. Choose another name.",
        )
    if new_name == repo_name:
        return {"repo_name": new_name}
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        duplicate = await store.fetch_one(
            "SELECT id FROM repos WHERE user_id = ? AND repo_name = ?", [current_user.id, new_name]
        )
        if duplicate:
            raise HTTPException(
                status_code=409, detail=f"You already have a repository named {new_name}. Choose a different name.",
            )
        await store.execute(
            "UPDATE repos SET repo_name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            [new_name, now(), repo["id"], current_user.id],
        )
    except Exception as error:
        _database_errors("rename the repository")(error)
    return {"repo_name": new_name}


@router.post("/repos/{repo_name}/reindex")
async def reindex_repository(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        existing = await owned_repo(store, current_user.id, repo_name)
        if existing["is_indexing"]:
            raise IngestionConflictError(
                "This repository is already being indexed. Wait for it to finish, or stop it first."
            )
        await enforce_ingestion_capacity(store, current_user.id, existing["github_url"])
        _repo_id, normalized_name = await queue_existing_repo(store, existing, current_user.id)
    except Exception as error:
        _database_errors("queue a re-index")(error)
    return {
        "message": (
            "Re-index queued. Questions keep using the current index until the new one is ready."
            if existing["queryable"] else "Re-index queued. Indexing starts as soon as a worker is free."
        ),
        "repo_name": normalized_name,
        "status": "queued",
    }


@router.post("/repos/{repo_name}/cancel-indexing")
async def cancel_indexing(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        outcome = await cancel_repository_indexing(store, repo["id"], current_user.id)
    except Exception as error:
        _database_errors("stop indexing")(error)
    return {
        "message": (
            "Stopping indexing. This takes a few seconds." if outcome == "stopping" else
            "Indexing stopped. Re-index whenever you're ready."
        ),
        "repo_name": repo_name,
        "state": outcome,
    }
