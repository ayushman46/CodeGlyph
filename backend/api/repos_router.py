"""Owned repository management backed by Turso."""

import json
import logging
import posixpath
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import impact
from api.auth import get_current_user
from api.repository_state import (
    fetch_accessible_repository,
    friendly_conflict,
    list_accessible_repositories,
    public_view,
    raise_storage_error,
    remove_repository_access,
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


async def owned_repo(store, user_id: str, repo_name: str, permission: str = "view"):
    """Compatibility wrapper used by other routers and tests (owner or member, see ``fetch_accessible_repository``)."""
    return await fetch_accessible_repository(store, user_id, repo_name, permission=permission)


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
        return await list_accessible_repositories(get_turso_store(), current_user.id)
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
        return await list_accessible_repositories(get_turso_store(), current_user.id)
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
    depth: Annotated[int, Query(ge=1, le=impact.MAX_DEPTH)] = 1,
    symbol: Annotated[str | None, Query(max_length=120)] = None,
):
    """Show the files and tests that would be affected by changing one file.

    The graph is deliberately conservative: only imports resolved to another
    indexed file are returned. External packages and unresolved aliases are
    omitted instead of being presented as speculative breakage. ``depth``
    (1-3) follows imports of imports; ``symbol`` lists the test functions in
    the affected test files that use (or are named after) that name. A path
    that matches several files returns ``ambiguous`` with the candidates and
    analyses nothing until one is chosen.
    """
    target_query = _normalise_impact_path(file_path)
    try:
        symbol_name = impact.normalise_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        matches, complete = await impact.resolve_target(store, repo, target_query)
        if not matches and not complete:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"More than {impact.PATH_SCAN_LIMIT:,} indexed paths look like {target_query}. "
                    "Type more of the path, such as the folder it is in."
                ),
            )
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No indexed file matches {target_query}. Check the spelling and use the path from the repository "
                    "root, or open the file tree to pick one. Skipped files are listed in the coverage report."
                ),
            )
        graph = await impact.ensure_current_graph(store, repo)
        base = {
            "repository": repo_name,
            "query": target_query,
            "commit_sha": repo.get("commit_sha"),
            "github_url": repo.get("github_url"),
            "graph": graph,
        }
        if len(matches) > 1 or not complete:
            # An incomplete list (the scan limit was reached) is never
            # analysed, even with one match: there may be others.
            shown = matches[:impact.CANDIDATE_LIMIT]
            counts = await impact.dependent_counts(store, str(repo["id"]), shown)
            return {
                **base,
                "ambiguous": True,
                "candidates": [{"file_path": path, "dependent_count": counts.get(path, 0)} for path in shown],
                "candidate_count": len(matches),
                "candidates_complete": complete,
                "target_file": None,
                "target_files": [],
                "dependents": [],
                "dependent_files": [],
                "dependent_count": 0,
                "summary": (
                    f"{'' if complete else 'At least '}{len(matches):,} indexed "
                    f"{'file matches' if len(matches) == 1 else 'files match'} {target_query}. "
                    "Choose one to see its impact; nothing is analysed until you do."
                ),
            }
        report = await impact.analyse_impact(store, repo, matches[0], depth=depth, symbol=symbol_name, limit=limit)
        return {**base, "ambiguous": False, "candidates": [], **report}
    except Exception as error:
        _database_errors("analyze the change impact")(error)


@router.get("/repos/{repo_name}/impact/suggestions")
async def repository_impact_suggestions(repo_name: str, current_user=Depends(get_current_user)):
    """The most imported indexed files: examples to start an impact check from.

    Opening the dialog calls this first, so an index whose import graph
    predates the current resolver is rebuilt before the first analysis.
    """
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name)
        graph = await impact.ensure_current_graph(store, repo)
        files = await impact.most_imported_files(store, repo)
        return {"repository": repo_name, "files": files, "graph": graph}
    except Exception as error:
        _database_errors("load impact suggestions")(error)


@router.delete("/repos/{repo_name}")
async def delete_repo(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await owned_repo(store, current_user.id, repo_name, "manage")
        await delete_repository(store, repo["id"], current_user.id)
        await remove_repository_access(store, repo["id"])
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
        repo = await owned_repo(store, current_user.id, repo_name, "manage")
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
        existing = await owned_repo(store, current_user.id, repo_name, "manage")
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
        repo = await owned_repo(store, current_user.id, repo_name, "manage")
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
