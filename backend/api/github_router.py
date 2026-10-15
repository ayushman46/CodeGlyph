"""Secure GitHub OAuth, file review, and branch/PR endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from agent.code_edit import EditingAuthorizationError, git_blob_sha, verify_edit_ticket
from api.auth import get_current_user
from config import get_editing_ticket_secret, settings
from database import DatabaseConfigurationError, get_turso_store
from github.github_client import (
    GitHubAPIError,
    GitHubAuthRequiredError,
    GitHubConflictError,
    GitHubForkNotReadyError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    create_branch_and_commit,
    create_branch_and_commit_files,
    exchange_code_for_token,
    fetch_raw_file,
    fork_repo,
    get_authenticated_user,
    get_authorization_url,
    get_file_at_ref,
    get_repo_details,
    open_pull_request,
    validate_branch_name,
    validate_file_path,
)
from github.token_crypto import TokenCryptoError, decrypt_token, encrypt_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["github"])


class PushFileRequest(BaseModel):
    file_path: str = Field(min_length=1, max_length=500)
    # Size is enforced on the whole change in _payload_files (413), and the
    # request body itself is capped before parsing by main.py's middleware.
    new_content: str = Field(min_length=1)
    file_sha: str = Field(default="", max_length=64)
    action: Literal["modify", "create"] = "modify"


class PushPRRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=200)
    # Legacy one-file fields remain accepted while ``files`` is the preferred
    # shape for issue fixes that require an atomic multi-file commit.
    file_path: str = Field(default="", max_length=500)
    new_content: str = Field(default="")
    files: list[PushFileRequest] = Field(default_factory=list, max_length=8)
    branch_name: str = Field(default="", max_length=100)
    commit_message: str = Field(default="", max_length=500)
    pr_title: str = Field(default="", max_length=300)
    pr_body: str = Field(default="", max_length=5000)
    base_sha: str = Field(default="", max_length=64)
    file_sha: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=100)
    edit_ticket: str = Field(default="", min_length=1, max_length=2000)


class CompleteGithubRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=512)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _github_http_error(error: GitHubAPIError) -> HTTPException:
    """Map a GitHub client error to the HTTP status the review UI acts on.

    Only an expired/revoked token is a 401 (the UI then asks to reconnect).
    Rate limits are 429, organization/permission refusals 403 with an
    ``X-Error-Code`` header so they are not mistaken for an editing-ticket
    refusal, and a fork that GitHub is still creating is a retryable 503.
    """
    if isinstance(error, GitHubAuthRequiredError):
        return HTTPException(status_code=401, detail=str(error))
    if isinstance(error, GitHubRateLimitError):
        retry_after = str(max(1, int(error.retry_after or 60)))
        return HTTPException(status_code=429, detail=str(error), headers={"Retry-After": retry_after, "X-Error-Code": "github_rate_limit"})
    if isinstance(error, GitHubPermissionError):
        return HTTPException(status_code=403, detail=str(error), headers={"X-Error-Code": "github_permission"})
    if isinstance(error, GitHubForkNotReadyError):
        return HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "30", "X-Error-Code": "github_fork_not_ready"})
    if isinstance(error, GitHubConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, GitHubNotFoundError):
        return HTTPException(status_code=404, detail=str(error), headers={"X-Error-Code": "github_not_found"})
    return HTTPException(status_code=400, detail=str(error))


def _payload_files(payload: PushPRRequest) -> list[dict[str, str]]:
    """Normalize legacy and multi-file payloads before authorization/work."""
    raw_files = payload.files or []
    if raw_files:
        files = [
            {
                "file_path": validate_file_path(item.file_path),
                "new_content": item.new_content,
                "expected_file_sha": item.file_sha.strip(),
                "action": item.action,
            }
            for item in raw_files
        ]
    else:
        if not payload.file_path.strip() or not payload.new_content:
            raise HTTPException(status_code=422, detail="A file path and complete replacement content are required.")
        files = [{
            "file_path": validate_file_path(payload.file_path),
            "new_content": payload.new_content,
            "expected_file_sha": payload.file_sha.strip(),
            "action": "modify",
        }]
    if len({item["file_path"] for item in files}) != len(files):
        raise HTTPException(status_code=422, detail="The same file cannot be submitted twice.")
    # A modified file must carry the reviewed blob SHA so a concurrent upstream
    # change is detected; a new file has no prior revision.
    if any(item["action"] == "modify" and not item["expected_file_sha"] for item in files):
        raise HTTPException(status_code=409, detail="Refresh every current file before pushing so concurrent edits cannot be overwritten.")
    total_bytes = sum(len(item["new_content"].encode("utf-8")) for item in files)
    if total_bytes > max(1, settings.max_github_change_bytes):
        raise HTTPException(status_code=413, detail="The proposed changes are too large to review in the browser.")
    return files


def _safe_redirect(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    # OAuth callback only needs to return to a known SPA route. Never persist
    # an arbitrary absolute URL or protocol-relative redirect.
    if parsed.scheme or parsed.netloc or "\\" in candidate or "\x00" in candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/app"
    path = candidate.split("?", 1)[0]
    if path not in {"/", "/app", "/account", "/pricing"}:
        return "/app"
    return candidate


def _github_callback_uri(request: Request = None) -> str:
    """Return one stable callback URI for authorization and token exchange.

    A configured URI always wins. Render's ``RENDER_EXTERNAL_URL`` is the
    trusted deployment URL and avoids accidentally sending ``localhost`` from
    a production service when the optional variable was omitted. The request
    URL is used only for local development; production redirects are never
    built from an arbitrary Host header.
    """
    configured = settings.github_redirect_uri.strip().rstrip("/")
    if configured:
        configured_parts = urlparse(configured)
        request_host = (request.url.hostname or "").lower() if request is not None else ""
        configured_host = (configured_parts.hostname or "").lower()
        # A localhost value copied from a developer .env must never be sent
        # by the public Render service. Prefer Render's canonical URL in that
        # one misconfiguration case; all other explicit values remain exact.
        configured_is_local = configured_host in {"localhost", "127.0.0.1", "::1"}
        request_is_public = bool(request_host and request_host not in {"localhost", "127.0.0.1", "::1"})
        if not (configured_is_local and request_is_public):
            return configured
    external = settings.render_external_url.strip().rstrip("/")
    if external:
        parsed = urlparse(external)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{external}/api/github/callback"
    if request is not None:
        host = (request.url.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return f"{request.url.scheme}://{request.url.netloc}/api/github/callback"
    return "http://localhost:8000/api/github/callback"


def _parse_github_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The indexed repository is not a valid GitHub URL.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("The indexed repository is not a valid GitHub URL.")
    return parts[0], parts[1][:-4] if parts[1].endswith(".git") else parts[1]


async def _ensure_github_table(store) -> None:
    """Compatibility bootstrap for local tests/old databases; production uses migrations."""
    await store.execute("""
        CREATE TABLE IF NOT EXISTS user_github_tokens (
          user_id TEXT PRIMARY KEY, github_user_id TEXT, github_username TEXT NOT NULL,
          access_token TEXT NOT NULL, scope TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    await store.execute("""
        CREATE TABLE IF NOT EXISTS github_oauth_states (
          state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, redirect_to TEXT NOT NULL,
          expires_at TEXT NOT NULL, used_at TEXT
        )
    """)
    await store.execute("""
        CREATE TABLE IF NOT EXISTS github_change_operations (
          operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, repo_name TEXT NOT NULL,
          file_path TEXT NOT NULL, content_hash TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
          result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)


def _token_key() -> str:
    key = settings.github_token_encryption_key.strip()
    if not key:
        raise TokenCryptoError("GitHub token encryption is not configured on the server.")
    return key


async def _load_token(store, user_id: str) -> tuple[str, str]:
    row = await store.fetch_one("SELECT access_token, github_username FROM user_github_tokens WHERE user_id = ?", [user_id])
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="GitHub account not connected. Please connect GitHub before pushing changes.")
    value = str(row.get("access_token") or "")
    try:
        token = decrypt_token(value, _token_key())
    except TokenCryptoError as error:
        # A one-time compatibility path upgrades rows created by the previous
        # release. The plaintext value is never returned or logged.
        if value.startswith("v1:"):
            raise HTTPException(status_code=401, detail="GitHub connection needs to be refreshed. Please reconnect GitHub.") from error
        try:
            token = value
            encrypted = encrypt_token(token, _token_key())
            await store.execute("UPDATE user_github_tokens SET access_token = ?, updated_at = ? WHERE user_id = ?", [encrypted, _timestamp(), user_id])
        except TokenCryptoError:
            raise HTTPException(status_code=503, detail="GitHub token storage is not configured on the server.") from error
    return token, str(row.get("github_username") or "github_user")


async def _consume_state(store, state: str) -> dict[str, Any] | None:
    """Consume an unexpired state regardless of user (legacy helper)."""
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    now = _timestamp()
    row = await store.fetch_one("SELECT user_id, redirect_to FROM github_oauth_states WHERE state_hash = ? AND used_at IS NULL AND expires_at > ?", [state_hash, now])
    if not row:
        return None
    claimed = await store.execute("UPDATE github_oauth_states SET used_at = ? WHERE state_hash = ? AND used_at IS NULL AND expires_at > ?", [now, state_hash, now])
    if not claimed.rows_affected:
        return None
    return row


@router.get("/github/status")
async def github_status(current_user=Depends(get_current_user)):
    try:
        store = get_turso_store()
        await _ensure_github_table(store)
        row = await store.fetch_one("SELECT github_username, updated_at FROM user_github_tokens WHERE user_id = ?", [current_user.id])
        if not row:
            return {"connected": False, "github_username": None}
        return {"connected": True, "github_username": row.get("github_username"), "updated_at": row.get("updated_at")}
    except HTTPException:
        raise
    except DatabaseConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Error checking GitHub status")
        raise HTTPException(status_code=500, detail="Could not check GitHub status.") from error


@router.get("/github/login")
async def github_login(request: Request = None, redirect_to: str = Query(default="/app"), current_user=Depends(get_current_user)):
    if not settings.github_client_id.strip() or not settings.github_client_secret.strip():
        raise HTTPException(status_code=503, detail="GitHub integration is not configured on the server.")
    redirect_uri = _github_callback_uri(request)
    clean_redirect = _safe_redirect(redirect_to)
    state = secrets.token_urlsafe(32)
    store = get_turso_store()
    await _ensure_github_table(store)
    expires = (datetime.now(UTC) + timedelta(seconds=max(60, settings.github_oauth_state_ttl_seconds))).isoformat()
    await store.execute("DELETE FROM github_oauth_states WHERE expires_at <= ?", [_timestamp()])
    await store.execute("INSERT INTO github_oauth_states (state_hash, user_id, redirect_to, expires_at) VALUES (?, ?, ?, ?)", [hashlib.sha256(state.encode()).hexdigest(), current_user.id, clean_redirect, expires])
    return {"authorization_url": get_authorization_url(settings.github_client_id, redirect_uri, state)}


def _frontend_origin() -> str:
    """The SPA origin that receives OAuth results (never a request header).

    Render must set GITHUB_FRONTEND_ORIGIN for a split Vercel/Render
    deployment. The trusted Render service URL is a safe fallback when the
    frontend and backend are served together.
    """
    target_origin = settings.github_frontend_origin.strip()
    if not target_origin:
        external = settings.render_external_url.strip().rstrip("/")
        parsed_external = urlparse(external)
        if external and parsed_external.scheme in {"http", "https"} and parsed_external.netloc:
            target_origin = external
        else:
            target_origin = settings.cors_origins.split(",")[0].strip() or "http://localhost:5173"
    return target_origin.rstrip("/")


@router.get("/github/callback", include_in_schema=False)
async def github_callback(request: Request = None, code: str = "", state: str = "", error: str = ""):
    """Hand GitHub's authorization response to the SPA; never link a token here.

    This endpoint is unauthenticated (GitHub redirects the browser to it),
    so it cannot know which application user is present. Linking the token
    here let anyone who started a flow attach a victim's GitHub token to the
    attacker's account (OAuth login CSRF). The code and state are passed in
    the URL fragment (never sent to a server or in a Referer) to the SPA's
    /github/callback page, which completes the connection with its own
    bearer token through POST /api/github/complete; that endpoint requires the
    signed-in user to be the user who started the flow. Works across split
    origins without third-party cookies.
    """
    target = f"{_frontend_origin()}/github/callback"
    if error:
        fragment = urlencode({"error": "GitHub authorization was cancelled."})
    elif not code or not state or len(code) > 2048 or len(state) > 512:
        fragment = urlencode({"error": "The authorization response was incomplete."})
    else:
        fragment = urlencode({"code": code, "state": state})
    response = RedirectResponse(f"{target}#{fragment}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _claim_state_for_user(store, state: str, user_id: str) -> dict[str, Any] | None:
    """Consume an unexpired state only when it belongs to ``user_id``."""
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    now = _timestamp()
    row = await store.fetch_one(
        "SELECT user_id, redirect_to FROM github_oauth_states WHERE state_hash = ? AND used_at IS NULL AND expires_at > ?",
        [state_hash, now],
    )
    if not row:
        return None
    if str(row.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403,
            detail="This GitHub authorization was started from a different account. Start the GitHub connection again from your own review window.",
        )
    claimed = await store.execute(
        "UPDATE github_oauth_states SET used_at = ? WHERE state_hash = ? AND user_id = ? AND used_at IS NULL AND expires_at > ?",
        [now, state_hash, str(user_id), now],
    )
    if not claimed.rows_affected:
        return None
    return row


@router.post("/github/complete")
async def github_complete(payload: CompleteGithubRequest, request: Request = None, current_user=Depends(get_current_user)):
    """Finish the OAuth flow for the signed-in user who started it."""
    if not settings.github_client_id.strip() or not settings.github_client_secret.strip():
        raise HTTPException(status_code=503, detail="GitHub integration is not configured on the server.")
    store = get_turso_store()
    await _ensure_github_table(store)
    state_row = await _claim_state_for_user(store, payload.state, current_user.id)
    if not state_row:
        raise HTTPException(status_code=400, detail="The GitHub authorization expired or was already used. Start the GitHub connection again.")
    try:
        token_data = await exchange_code_for_token(settings.github_client_id, settings.github_client_secret, payload.code, _github_callback_uri(request))
        access_token = str(token_data["access_token"])
        profile = await get_authenticated_user(access_token)
        username = str(profile.get("login") or "github_user")[:100]
        encrypted = encrypt_token(access_token, _token_key())
        await store.execute(
            "INSERT INTO user_github_tokens (user_id, github_user_id, github_username, access_token, scope, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET github_user_id = excluded.github_user_id, github_username = excluded.github_username, "
            "access_token = excluded.access_token, scope = excluded.scope, updated_at = excluded.updated_at",
            [current_user.id, str(profile.get("id") or ""), username, encrypted, str(token_data.get("scope") or "public_repo"), _timestamp()],
        )
        return {"connected": True, "github_username": username, "redirect_to": state_row.get("redirect_to") or "/app"}
    except TokenCryptoError as error:
        logger.error("GitHub OAuth token encryption is not configured correctly")
        raise HTTPException(
            status_code=503,
            detail="GitHub token encryption is not configured correctly on the server. Set GITHUB_TOKEN_ENCRYPTION_KEY in Render and redeploy.",
        ) from error
    except GitHubAPIError as error:
        logger.warning("GitHub connection failed: %s", type(error).__name__)
        raise HTTPException(
            status_code=400,
            detail="GitHub rejected the authorization. Check GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and GITHUB_REDIRECT_URI, then try again.",
        ) from error


@router.post("/github/disconnect")
async def github_disconnect(current_user=Depends(get_current_user)):
    try:
        store = get_turso_store()
        await _ensure_github_table(store)
        await store.execute("DELETE FROM user_github_tokens WHERE user_id = ?", [current_user.id])
        return {"disconnected": True}
    except Exception as error:
        logger.exception("Error disconnecting GitHub")
        raise HTTPException(status_code=500, detail="Could not disconnect GitHub.") from error


@router.get("/github/file")
async def github_file(
    repo_name: str = Query(min_length=1, max_length=200),
    file_path: str = Query(min_length=1, max_length=500),
    edit_ticket: str = Header(default="", alias="X-Editing-Ticket", max_length=2000),
    current_user=Depends(get_current_user),
):
    try:
        path = validate_file_path(file_path)
        verify_edit_ticket(
            get_editing_ticket_secret(),
            edit_ticket,
            user_id=current_user.id,
            repo_name=repo_name,
            file_path=path,
        )
        store = get_turso_store()
        await _ensure_github_table(store)
        token: str | None = None
        try:
            token, _ = await _load_token(store, current_user.id)
        except HTTPException as error:
            # Reviewing a public file does not require a GitHub connection;
            # only creating the branch and pull request does.
            if error.status_code != 401:
                raise
        repo = await store.fetch_one("SELECT github_url FROM repos WHERE user_id = ? AND repo_name = ?", [current_user.id, repo_name])
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found.")
        owner, name = _parse_github_url(str(repo["github_url"]))
        if not token:
            data = await fetch_raw_file(owner, name, path, ref="HEAD", max_bytes=settings.github_editor_max_bytes)
            if data is None:
                raise HTTPException(status_code=404, detail="That file does not exist on the default branch.")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise HTTPException(status_code=400, detail="That file is not UTF-8 text and cannot be edited here.") from error
            return {"path": path, "content": content, "sha": git_blob_sha(data), "size": len(data), "base_sha": "", "source": "raw"}
        # Reading an already-public repository may still be blocked by an
        # organization's OAuth-app policy. The GitHub client may retry these
        # read-only calls anonymously; commits and PRs remain token-gated.
        metadata = await get_repo_details(token, owner, name, allow_public_fallback=True)
        result = await get_file_at_ref(
            token,
            owner,
            name,
            path,
            str(metadata.get("default_branch") or "main"),
            max_content_bytes=settings.github_editor_max_bytes,
            allow_public_fallback=True,
        )
        return {"path": result["path"], "content": result["content"], "sha": result["sha"], "size": result["size"], "base_sha": ""}
    except HTTPException:
        raise
    except GitHubAPIError as error:
        raise _github_http_error(error) from error
    except EditingAuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except Exception as error:
        logger.exception("Could not read GitHub file")
        raise HTTPException(status_code=502, detail="Could not load the current GitHub file.") from error


@router.post("/github/push-pr")
async def push_pr(payload: PushPRRequest, current_user=Depends(get_current_user)):
    store = None
    operation_id = None
    try:
        store = get_turso_store()
        await _ensure_github_table(store)
        token, username = await _load_token(store, current_user.id)
        # Authenticate the account before validating the complete replacement
        # payload, preserving the endpoint's auth boundary for malformed or
        # incomplete requests.
        files = _payload_files(payload)
        file_paths = [item["file_path"] for item in files]
        file_key = ",".join(file_paths)
        # Validate before any GitHub call: an invalid name used to be rejected
        # only after the user's account had already been given a new fork.
        target_branch = validate_branch_name(
            payload.branch_name.strip() or f"codebase-intel/patch-{uuid4().hex[:10]}", require_prefix=True,
        )
        repo_row = await store.fetch_one("SELECT id, github_url FROM repos WHERE repo_name = ? AND user_id = ?", [payload.repo_name, current_user.id])
        if not repo_row:
            raise HTTPException(status_code=404, detail="Repository not found.")
        try:
            for file_path in file_paths:
                verify_edit_ticket(
                    get_editing_ticket_secret(),
                    payload.edit_ticket,
                    user_id=current_user.id,
                    repo_name=payload.repo_name,
                    file_path=file_path,
                )
        except EditingAuthorizationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        request_key = payload.idempotency_key.strip() or uuid4().hex
        if len(request_key) > 100 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in request_key):
            raise HTTPException(status_code=422, detail="The change request key is invalid.")
        canonical_contents = json.dumps(
            [{"file_path": item["file_path"], "content": item["new_content"]} for item in files],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        content_hash = hashlib.sha256(canonical_contents.encode("utf-8")).hexdigest()
        operation_id = hashlib.sha256(f"{current_user.id}:{request_key}".encode("utf-8")).hexdigest()
        operation = await store.fetch_one("SELECT repo_name, file_path, content_hash, status, result_json FROM github_change_operations WHERE operation_id = ?", [operation_id])
        progress: dict[str, Any] = {}
        if operation:
            if operation.get("repo_name") != payload.repo_name or operation.get("file_path") != file_key or operation.get("content_hash") != content_hash:
                raise HTTPException(status_code=409, detail="That change request key was already used for a different change.")
            if operation.get("status") == "completed" and operation.get("result_json"):
                try:
                    return json.loads(operation["result_json"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=409, detail="That change request needs to be submitted again.")
            if operation.get("status") == "processing":
                raise HTTPException(status_code=409, detail="This change is already being pushed. Wait for the existing pull request.")
            # A failed attempt keeps the branch/commit it already created in
            # result_json, so this retry can resume instead of failing on
            # "branch already exists" and leaving an orphan branch.
            try:
                previous = json.loads(operation.get("result_json") or "null")
                if isinstance(previous, dict) and isinstance(previous.get("progress"), dict):
                    progress = previous["progress"]
            except (TypeError, ValueError):
                progress = {}
            claimed = await store.execute(
                "UPDATE github_change_operations SET status = 'processing', updated_at = ? "
                "WHERE operation_id = ? AND status = 'failed'",
                [_timestamp(), operation_id],
            )
            if not claimed.rows_affected:
                raise HTTPException(status_code=409, detail="This change is already being pushed. Wait for the existing pull request.")
        else:
            inserted = await store.execute("INSERT OR IGNORE INTO github_change_operations (operation_id, user_id, repo_name, file_path, content_hash, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)", [operation_id, current_user.id, payload.repo_name, file_key, content_hash, _timestamp(), _timestamp()])
            if not inserted.rows_affected:
                raise HTTPException(status_code=409, detail="This change is already being pushed. Wait for the existing pull request.")
        base_owner, base_repo = _parse_github_url(str(repo_row["github_url"]))
        details = await get_repo_details(token, base_owner, base_repo)
        upstream_branch = str(details.get("default_branch") or "main")
        has_push = bool((details.get("permissions") or {}).get("push") or (details.get("permissions") or {}).get("admin"))
        target_owner, target_repo, pr_head = base_owner, base_repo, ""
        if not has_push:
            fork = await fork_repo(token, base_owner, base_repo, username=username)
            target_owner = str(fork.get("owner", {}).get("login") or username)
            target_repo = str(fork.get("name") or base_repo)
            pr_head = f"{target_owner}:{target_branch}"
        else:
            pr_head = target_branch
        # The branch is always built on the upstream default-branch head and
        # checked against upstream (where the change was reviewed). For a fork
        # the objects and branch are created in the fork, which shares
        # GitHub's object network; the fork's own, possibly stale or
        # diverged, default branch is never used.
        resume_commit = ""
        if progress.get("branch_name") == target_branch and progress.get("target_repo") == f"{target_owner}/{target_repo}":
            resume_commit = str(progress.get("commit_sha") or "")
        commit_options = {
            "expected_base_sha": payload.base_sha,
            "source_owner": base_owner,
            "source_repo": base_repo,
            "resume_commit_sha": resume_commit,
        }
        if len(files) == 1 and files[0]["action"] == "modify":
            item = files[0]
            committed = await create_branch_and_commit(
                token, target_owner, target_repo, upstream_branch, target_branch,
                item["file_path"], item["new_content"],
                payload.commit_message or f"Update {item['file_path']}",
                expected_file_sha=item["expected_file_sha"],
                **commit_options,
            )
        else:
            # A new file must use the path that carries each file's action:
            # the legacy one-file wrapper treats everything as a modification
            # and would skip the "still absent upstream" check, silently
            # overwriting a file that appeared after the review.
            committed = await create_branch_and_commit_files(
                token, target_owner, target_repo, upstream_branch, target_branch,
                files,
                payload.commit_message or (f"Add {files[0]['file_path']}" if len(files) == 1 else f"Update {len(files)} files"),
                **commit_options,
            )
        progress = {"branch_name": committed["branch_name"], "commit_sha": committed.get("commit_sha"), "target_repo": f"{target_owner}/{target_repo}"}
        try:
            await store.execute(
                "UPDATE github_change_operations SET result_json = ?, updated_at = ? WHERE operation_id = ? AND status = 'processing'",
                [json.dumps({"progress": progress}, separators=(",", ":")), _timestamp(), operation_id],
            )
        except Exception:
            logger.warning("Could not record GitHub push progress", exc_info=True)
        default_title = payload.pr_title or (f"Update {files[0]['file_path']}" if len(files) == 1 else f"Update {len(files)} files")
        default_body = payload.pr_body or (
            (f"Reviewed changes to `{files[0]['file_path']}`" if len(files) == 1 else f"Reviewed changes to {len(files)} files")
            + " created by Codebase Intelligence."
        )
        pr = await open_pull_request(token, base_owner, base_repo, pr_head, upstream_branch, default_title, default_body)
        result = {"success": True, **pr, "branch_name": committed["branch_name"], "target_repo": f"{target_owner}/{target_repo}", "is_fork": not has_push}
        try:
            await store.execute("UPDATE github_change_operations SET status = 'completed', result_json = ?, updated_at = ? WHERE operation_id = ?", [json.dumps(result, separators=(",", ":")), _timestamp(), operation_id])
        except Exception:
            # GitHub is the source of truth for the already-created PR. Do not
            # turn a successful external write into a misleading 502 when a
            # transient Turso write fails; the URL remains actionable and the
            # warning makes the persistence gap observable.
            logger.exception("GitHub PR created but operation result could not be persisted")
            result["operation_persisted"] = False
        return result
    except HTTPException:
        raise
    except GitHubAPIError as error:
        if store is not None and operation_id:
            await store.execute("UPDATE github_change_operations SET status = 'failed', updated_at = ? WHERE operation_id = ? AND status = 'processing'", [_timestamp(), operation_id])
        raise _github_http_error(error) from error
    except TokenCryptoError as error:
        if store is not None and operation_id:
            await store.execute("UPDATE github_change_operations SET status = 'failed', updated_at = ? WHERE operation_id = ? AND status = 'processing'", [_timestamp(), operation_id])
        raise HTTPException(status_code=503, detail="GitHub token storage is not configured on the server.") from error
    except Exception as error:
        if store is not None and operation_id:
            try:
                await store.execute("UPDATE github_change_operations SET status = 'failed', updated_at = ? WHERE operation_id = ? AND status = 'processing'", [_timestamp(), operation_id])
            except Exception:
                logger.warning("Could not mark GitHub change operation failed", exc_info=True)
        logger.exception("GitHub branch or PR creation failed")
        raise HTTPException(status_code=502, detail="GitHub could not create the branch and pull request. Please try again.") from error
