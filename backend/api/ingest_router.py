import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import get_current_user
from api.repository_state import friendly_conflict, raise_storage_error
from config import ingest_request_limiter
from database import DatabaseConfigurationError, TransientDatabaseError, assert_turso_schema, get_turso_store
from ingest.cloner import RepositoryValidationError, normalize_github_url, parse_github_ref, repository_source_url
from ingest.pipeline import IngestionConflictError, enqueue_ingestion_job, enforce_ingestion_capacity, ensure_repo_record

logger = logging.getLogger(__name__)
router = APIRouter()

_START_FAILED = "Couldn't start indexing this repository because of a problem on our side. Try again in a moment."


class IngestRequest(BaseModel):
    github_url: str = Field(min_length=1, max_length=2_048)


@router.post("/ingest")
async def ingest_repo(req: IngestRequest, current_user=Depends(get_current_user)):
    # Cheap URL validation runs before the rate limiter so typing mistakes
    # never use up the user's repository-request allowance.
    try:
        github_url = normalize_github_url(req.github_url)
        ref = parse_github_ref(req.github_url)
        # The durable job keeps the linked branch/tag/commit so the worker
        # indexes exactly what the user pointed at.
        source_url = repository_source_url(req.github_url, ref)
    except RepositoryValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    limiter_acquired = False
    try:
        await ingest_request_limiter.acquire(current_user.id)
        limiter_acquired = True
        await assert_turso_schema()
        # Turso is server-only. The verified Supabase user id is persisted on
        # each record and is required again by all user-facing queries.
        store = get_turso_store()
        await enforce_ingestion_capacity(store, current_user.id, github_url)
        repo_id, repo_name = await ensure_repo_record(store, github_url, current_user.id)
        await enqueue_ingestion_job(store, source_url, current_user.id, repo_id)
    except RepositoryValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except IngestionConflictError as error:
        raise HTTPException(status_code=409, detail=friendly_conflict(error)) from error
    except (DatabaseConfigurationError, TransientDatabaseError) as error:
        raise_storage_error(error, "start indexing this repository", logger)
    except RuntimeError as error:
        if str(error) in {"rate_limit", "concurrency_limit"}:
            raise HTTPException(
                status_code=429,
                detail="You've added several repositories in a short time. Wait a minute, then try again.",
            ) from error
        logger.exception("Could not start repository ingestion")
        raise HTTPException(status_code=500, detail=_START_FAILED) from error
    except Exception as error:
        logger.exception("Could not start repository ingestion")
        raise HTTPException(status_code=500, detail=_START_FAILED) from error
    finally:
        if limiter_acquired:
            await ingest_request_limiter.release(current_user.id)

    response = {
        "message": "Indexing queued. You can start asking questions as soon as keyword search is ready.",
        "repo_name": repo_name,
        "github_url": github_url,
        "status": "queued",
    }
    if ref is not None:
        response["ref"] = ref.candidates()[0]
        response["ref_kind"] = ref.kind
    return response
