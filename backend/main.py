import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from config import get_cors_origins, should_run_local_ingestion_worker, settings
from api.ingest_router import router as ingest_router
from api.query_router import router as query_router
from api.repos_router import router as repos_router
from api.billing_router import router as billing_router
from api.github_router import router as github_router
from api.explore_router import router as explore_router
from agent.nemotron import close_async_client
from database import DatabaseConfigurationError, assert_turso_schema, get_turso_store
from frontend_static import HashedAssets, revalidated_file
from ingest.local_worker import process_queue_forever

logger = logging.getLogger("codebase_intel")
_dependency_cache: dict[bool, tuple[float, dict]] = {}
_dependency_cache_lock = asyncio.Lock()


async def _bootstrap_schema() -> None:
    """Apply additive migrations at startup so the first request is fast."""
    try:
        await assert_turso_schema()
    except DatabaseConfigurationError as error:
        logger.warning("Turso schema bootstrap skipped: %s", error)
    except Exception:
        logger.warning("Turso schema bootstrap failed; requests will retry it", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bootstrap the schema and start the automatic ingestion worker."""
    bootstrap_task = asyncio.create_task(_bootstrap_schema(), name="turso-schema-bootstrap")
    worker_task = None
    if should_run_local_ingestion_worker():
        worker_task = asyncio.create_task(process_queue_forever(), name="local-ingestion-worker")
    try:
        yield
    finally:
        for task in (worker_task, bootstrap_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        with suppress(Exception):
            await close_async_client()
        with suppress(Exception):
            await get_turso_store().close()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

app = FastAPI(
    title="Codebase Intelligence System",
    lifespan=lifespan,
    # Reserve the public /docs route for the React marketing page when the
    # application is deployed as one Render web service.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

# Request bodies are capped before routing. FastAPI reads and JSON-decodes a
# body before it resolves Depends(get_current_user), so without this a single
# anonymous request could buffer hundreds of MB and OOM the 512 MB instance.
DEFAULT_MAX_BODY_BYTES = 256 * 1024
_LARGE_BODY_PATHS = frozenset({"/api/github/push-pr"})


def max_body_bytes_for(path: str) -> int:
    """Per-route body cap. Push bodies carry complete reviewed files: allow
    the configured change size with headroom for JSON string escaping."""
    if path.rstrip("/") in _LARGE_BODY_PATHS:
        return 2 * max(1, int(settings.max_github_change_bytes)) + 1024 * 1024
    return DEFAULT_MAX_BODY_BYTES


class BodySizeLimitMiddleware:
    """Pure ASGI middleware that rejects oversized bodies with 413.

    A declared Content-Length above the cap is refused without reading the
    body. A body without a length (chunked transfer) is read here only up to
    the cap and then replayed to the application, so memory stays bounded.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reject(scope, receive, send, limit: int) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"The request body is too large (limit {limit} bytes)."},
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = max_body_bytes_for(scope.get("path") or "")
        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                break
        if declared is not None and (declared < 0 or declared > limit):
            await self._reject(scope, receive, send, limit)
            return
        if declared is not None:
            # The server enforces the declared length; still count defensively.
            received = 0

            async def counted_receive():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body") or b"")
                    if received > limit:
                        raise _BodyTooLarge()
                return message

            try:
                await self.app(scope, counted_receive, send)
            except _BodyTooLarge:
                logger.warning("Request body exceeded its declared length path=%s", scope.get("path"))
            return
        # No declared length: buffer at most ``limit`` bytes, then replay.
        messages, total = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            total += len(message.get("body") or b"")
            if total > limit:
                await self._reject(scope, receive, send, limit)
                return
            messages.append(message)
            if not message.get("more_body"):
                break
        replay = iter(messages)

        async def replay_receive():
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await self.app(scope, replay_receive, send)


class _BodyTooLarge(Exception):
    pass


# JSON payloads (repository lists, citations) compress well. Starlette skips
# text/event-stream, so streamed answers are never buffered.
# Added first so it sits inside CORS: a browser can still read the 413.
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # The editing review endpoint carries a short-lived, file-scoped ticket
    # in this header. Without it, the browser rejects the cross-origin
    # preflight before FastAPI can validate the ticket or call GitHub.
    allow_headers=["Authorization", "Content-Type", "X-Editing-Ticket", "X-Request-ID"],
    # X-Error-Code / Retry-After let the review UI tell a GitHub permission
    # refusal or rate limit apart from an editing-ticket 403.
    expose_headers=["X-Request-ID", "X-Error-Code", "Retry-After"],
)



def _safe_validation_errors(errors) -> list:
    """Validation details without the rejected input.

    The default handler echoes ``input``: that can be megabytes of file
    content, and a lone UTF-16 surrogate in it made the JSON encoder raise,
    turning a 422 into a plain-text 500.
    """
    cleaned = []
    for error in errors:
        if isinstance(error, dict):
            error = {key: value for key, value in error.items() if key not in {"input", "url"}}
        cleaned.append(error)
    return jsonable_encoder(cleaned)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_request: Request, exc: RequestValidationError):
    body = json.dumps({"detail": _safe_validation_errors(exc.errors())}, ensure_ascii=True, default=str)
    return Response(content=body, status_code=422, media_type="application/json")


app.include_router(ingest_router, prefix="/api")
app.include_router(query_router, prefix="/api")
app.include_router(repos_router, prefix="/api")
app.include_router(billing_router, prefix="/api")
app.include_router(github_router, prefix="/api")
app.include_router(explore_router, prefix="/api")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "").strip()
    if len(request_id) > 96 or not request_id.replace("-", "").replace("_", "").isalnum():
        request_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request failed method=%s path=%s request_id=%s", request.method, request.url.path, request_id)
        raise
    # The SPA uses only same-origin scripts and API calls. Supabase is needed
    # for OAuth/session traffic and remote HTTPS avatars are permitted.
    # The transient GitHub OAuth popup supplies its own nonce-based CSP. The
    # SPA itself uses only bundled self-hosted scripts.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "connect-src 'self' https://*.supabase.co https://checkout.razorpay.com https://api.razorpay.com https://api.github.com https://github.com; "
        "img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' https://checkout.razorpay.com; "
        "frame-src https://checkout.razorpay.com https://api.razorpay.com https://github.com; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request completed method=%s path=%s status=%s latency_ms=%s request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        int((time.perf_counter() - started) * 1000),
        request_id,
    )
    return response

@app.get("/api/health", include_in_schema=False)
async def health_check():
    """Fast operational snapshot: liveness plus dependency configuration."""
    checks = await _dependency_checks(probe_nvidia=False)
    healthy = checks["turso"]["status"] == "ok" and checks["nvidia"]["status"] in {"ok", "configured"}
    return {"status": "ok" if healthy else "degraded", "dependencies": checks}


def _probe_nvidia_sync() -> str:
    """Perform a bounded provider probe without creating a chat/embedding job."""
    base_url = settings.nvidia_base_url.strip().rstrip("/")
    request = UrlRequest(f"{base_url}/models", headers={"Authorization": f"Bearer {settings.nvidia_api_key.strip()}"})
    try:
        with urlopen(request, timeout=3.0) as response:
            return "ok" if 200 <= response.status < 300 else "unavailable"
    except HTTPError as error:
        # A healthy API can reject a model-list permission while still being
        # reachable; only transport/server failures make readiness fail.
        return "ok" if 400 <= error.code < 500 else "unavailable"
    except (URLError, TimeoutError, OSError):
        return "unavailable"


async def _dependency_checks(*, probe_nvidia: bool) -> dict:
    cache_ttl = 3.0 if probe_nvidia else 10.0
    cached = _dependency_cache.get(probe_nvidia)
    if cached and time.monotonic() - cached[0] < cache_ttl:
        return cached[1]

    async def check_turso() -> dict:
        try:
            await assert_turso_schema()
            row = await get_turso_store().fetch_one("SELECT 1 AS ok")
            return {"status": "ok" if row and int(row.get("ok", 0)) == 1 else "unavailable"}
        except DatabaseConfigurationError:
            return {"status": "unconfigured"}
        except Exception:
            logger.warning("Turso health probe failed", exc_info=True)
            return {"status": "unavailable"}

    async def check_nvidia() -> dict:
        if not settings.nvidia_api_key.strip():
            return {"status": "unconfigured"}
        if not settings.nvidia_base_url.strip().lower().startswith("https://"):
            return {"status": "invalid_configuration"}
        if not probe_nvidia:
            return {"status": "configured"}
        return {"status": await asyncio.to_thread(_probe_nvidia_sync)}

    # Serialize refreshes so a burst of deployment probes does not open many
    # Turso streams or provider sockets at once. Callers still receive the
    # cached snapshot during the normal probe interval.
    async with _dependency_cache_lock:
        cached = _dependency_cache.get(probe_nvidia)
        if cached and time.monotonic() - cached[0] < cache_ttl:
            return cached[1]
        turso, nvidia = await asyncio.gather(check_turso(), check_nvidia())
        result = {"turso": turso, "nvidia": nvidia}
        _dependency_cache[probe_nvidia] = (time.monotonic(), result)
        return result


@app.get("/api/ready", include_in_schema=False)
async def readiness_check():
    """Dependency readiness probe for deployment checks and incident diagnosis."""
    checks = await _dependency_checks(probe_nvidia=True)
    ready = checks["turso"]["status"] == "ok" and checks["nvidia"]["status"] == "ok"
    payload = {"status": "ready" if ready else "not_ready", "dependencies": checks}
    return JSONResponse(status_code=200 if ready else 503, content=payload)


if FRONTEND_DIST.is_dir():
    # Render builds the Vite site before starting FastAPI. Serve assets directly
    # and send all remaining non-API paths to the SPA for client-side routing.
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", HashedAssets(directory=assets_dir), name="frontend-assets")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def serve_frontend(frontend_path: str):
        # Unknown API paths are API errors, not SPA routes.
        if frontend_path == "api" or frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        requested_file = (FRONTEND_DIST / frontend_path).resolve()
        if frontend_path and requested_file.is_relative_to(FRONTEND_DIST.resolve()) and requested_file.is_file():
            return revalidated_file(requested_file)
        return revalidated_file(FRONTEND_DIST / "index.html")
else:
    # Local API-only startup does not package frontend/dist.
    @app.get("/")
    def read_root():
        return {"status": "ok", "message": "Codebase Intelligence System API v2"}
