"""Bounded GitHub REST client for reviewed branch and pull request changes."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

# agent.code_edit imports neither github.* nor retrieval.* (no import cycle).
from agent.code_edit import exact_hunks, reapply_change
from config import settings

logger = logging.getLogger(__name__)
GITHUB_API_BASE = "https://api.github.com"
GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_ACCESS_TOKEN = "https://github.com/login/oauth/access_token"
_REF_INVALID = re.compile(r"[\x00-\x20~^:?*\[\\]|\.\.|@\{")
_PATH_INVALID = re.compile(r"(^/|/$|(^|/)\.\.(/|$)|\x00)")
# Git object names: SHA-1 (40 hex) or SHA-256 (64 hex) repositories.
_OBJECT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DEFAULT_MAX_PR_FILES = 24


def _setting(name: str, default: Any) -> Any:
    """A setting that a newer ``config.py`` defines, or its documented default."""
    value = getattr(settings, name, default)
    return default if value is None else value


def max_pr_files() -> int:
    """Files one pull request may contain (``settings.github_max_pr_files``)."""
    try:
        return max(1, int(_setting("github_max_pr_files", DEFAULT_MAX_PR_FILES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PR_FILES


def is_object_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_OBJECT_SHA.fullmatch(value.strip().lower()))


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns an unexpected response."""


class GitHubAuthRequiredError(GitHubAPIError):
    """Raised when the connected GitHub token is invalid or expired."""


class GitHubConflictError(GitHubAPIError):
    """Raised when a reviewed change is stale or its branch already exists."""


class GitHubStaleConflictError(GitHubConflictError):
    """Raised when upstream moved on and the reviewed change cannot follow it.

    The API answers it with ``X-Error-Code: stale_conflict`` so the review UI
    can offer "Refresh from GitHub" instead of a new branch name.
    """


class GitHubNotFoundError(GitHubAPIError):
    """Raised when GitHub answers 404 for a repository, ref, or file."""


class GitHubRateLimitError(GitHubAPIError):
    """Raised for GitHub primary/secondary rate limits (retryable later)."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GitHubPermissionError(GitHubAPIError):
    """Raised for a 403 that a reconnect cannot fix (org policy, no access)."""


class GitHubFileTooLargeError(GitHubAPIError):
    """Raised when a file exceeds the byte limit of a bounded read."""


class GitHubForkNotReadyError(GitHubAPIError):
    """Raised when GitHub has not finished creating a fork yet (retryable)."""


def get_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{GITHUB_OAUTH_AUTHORIZE}?{urlencode({'client_id': client_id.strip(), 'redirect_uri': redirect_uri.strip(), 'scope': 'public_repo', 'state': state})}"


def _github_headers(access_token: str, *, raw: bool = False) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token.strip()}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Codebase-Intelligence-App",
    }


def _github_public_headers() -> dict[str, str]:
    """Headers for a read-only request to content already public on GitHub."""
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Codebase-Intelligence-App",
    }


def _api_path(*parts: str) -> str:
    return "/".join(quote(str(part).strip("/"), safe="") for part in parts)


def validate_file_path(file_path: str) -> str:
    value = file_path.strip().replace("\\", "/")
    if not value or len(value) > 500 or _PATH_INVALID.search(value) or any(part in {"", "."} for part in value.split("/")):
        raise GitHubAPIError("File path must be a relative path inside the repository.")
    return value


def validate_branch_name(branch_name: str, *, require_prefix: bool = False) -> str:
    value = branch_name.strip()
    if not value or len(value) > 100 or (require_prefix and not value.startswith("codebase-intel/")):
        raise GitHubAPIError("Branch names must start with codebase-intel/.")
    if _REF_INVALID.search(value) or value.endswith((".", "/")) or value.startswith((".", "/")):
        raise GitHubAPIError("Branch name contains invalid Git characters.")
    return value


async def exchange_code_for_token(client_id: str, client_secret: str, code: str, redirect_uri: str = "") -> dict[str, Any]:
    payload = {"client_id": client_id.strip(), "client_secret": client_secret.strip(), "code": code.strip()}
    if redirect_uri.strip():
        payload["redirect_uri"] = redirect_uri.strip()
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(GITHUB_OAUTH_ACCESS_TOKEN, data=payload, headers={"Accept": "application/json", "User-Agent": "Codebase-Intelligence-App"})
    if response.status_code != 200:
        raise GitHubAPIError(f"GitHub OAuth token exchange failed (HTTP {response.status_code})")
    data = response.json()
    if data.get("error") or not str(data.get("access_token") or "").strip():
        raise GitHubAPIError("GitHub OAuth authorization was not completed.")
    return data


def _response_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    return str(data.get("message") or "") if isinstance(data, dict) else ""


def _retry_after_seconds(response: httpx.Response) -> int | None:
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
        if value and str(value).strip().isdigit():
            return int(str(value).strip())
        reset = headers.get("x-ratelimit-reset")
        if reset and str(reset).strip().isdigit():
            return max(1, int(reset) - int(time.time()))
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _is_rate_limited(response: httpx.Response, message: str) -> bool:
    headers = getattr(response, "headers", None) or {}
    try:
        remaining = str(headers.get("x-ratelimit-remaining") or "").strip()
    except AttributeError:
        remaining = ""
    return response.status_code == 429 or remaining == "0" or "rate limit" in message.lower()


def _raise_for_api(response: httpx.Response, action: str) -> None:
    """Map a GitHub error response to a specific, actionable exception.

    Only a 401 means the connection must be renewed. A 403 is a rate limit,
    an organization OAuth-app restriction, or a missing permission: none of
    them is fixed by reconnecting, so they must not disconnect GitHub.
    ``action`` is a verb phrase such as "read the repository".
    """
    status_code = response.status_code
    if status_code < 400:
        return
    if status_code == 401:
        raise GitHubAuthRequiredError("Your GitHub connection expired or was revoked. Reconnect GitHub.")
    if status_code in {403, 429}:
        message = _response_message(response)
        if _is_rate_limited(response, message):
            retry_after = _retry_after_seconds(response)
            when = f" Try again in about {max(1, (retry_after + 59) // 60)} minute(s)." if retry_after else " Try again in a few minutes."
            raise GitHubRateLimitError(f"GitHub rate limit reached while trying to {action}.{when}", retry_after)
        lowered = message.lower()
        if "oauth app access restrictions" in lowered or "organization has enabled" in lowered or "saml" in lowered:
            raise GitHubPermissionError(
                f"The repository's organization blocks this app (GitHub could not {action}). "
                "An organization owner must approve the Codebase Intel OAuth app, or SAML SSO must be authorized for your token."
            )
        raise GitHubPermissionError(
            f"Your GitHub account does not have permission to {action}."
            + (f" GitHub said: {message[:200]}" if message else "")
        )
    if status_code == 404:
        raise GitHubNotFoundError(f"GitHub could not {action}: it was not found or is not visible to your account.")
    raise GitHubAPIError(f"GitHub could not {action} (HTTP {status_code}).")


async def get_authenticated_user(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{GITHUB_API_BASE}/user", headers=_github_headers(access_token))
    _raise_for_api(response, "read the connected account")
    return response.json()


async def get_repo_details(
    access_token: str,
    owner: str,
    repo: str,
    *,
    allow_public_fallback: bool = False,
) -> dict[str, Any]:
    """Read repository metadata, optionally retrying without OAuth.

    Organization OAuth policies can deny an application while the repository
    remains public. The review screen only needs public metadata, so it may
    retry anonymously. Write operations leave this disabled and still require
    the connected GitHub token.
    """
    endpoint = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(endpoint, headers=_github_headers(access_token))
        if response.status_code in {401, 403} and allow_public_fallback:
            public_response = await client.get(endpoint, headers=_github_public_headers())
            if public_response.status_code < 400:
                return public_response.json()
    _raise_for_api(response, "read the repository")
    return response.json()


async def fork_repo(access_token: str, owner: str, repo: str, *, username: str = "") -> dict[str, Any]:
    """Create a fork and wait briefly for GitHub's asynchronous fork to appear."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/forks", headers=_github_headers(access_token))
        if response.status_code == 422:
            # GitHub returns 422 when this account already owns the fork. It
            # is safe to reuse it instead of making a retry look like a hard
            # failure or creating a second branch in the upstream repository.
            existing_owner = username.strip()
            if existing_owner:
                existing = await client.get(
                    f"{GITHUB_API_BASE}/repos/{_api_path(existing_owner, repo)}",
                    headers=_github_headers(access_token),
                )
                if existing.status_code == 200:
                    return existing.json()
        if response.status_code not in {200, 202}:
            _raise_for_api(response, "create a repository fork")
        data = response.json()
        fork_owner = str(data.get("owner", {}).get("login") or username).strip()
        fork_name = str(data.get("name") or repo).strip()
        if response.status_code == 202 and fork_owner:
            for delay in (0.5, 1, 2, 3, 5):
                await asyncio.sleep(delay)
                ready = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(fork_owner, fork_name)}", headers=_github_headers(access_token))
                if ready.status_code == 200:
                    return ready.json()
            raise GitHubForkNotReadyError(
                f"GitHub is still creating your fork {fork_owner}/{fork_name}. Nothing was pushed; try again in a minute."
            )
        return data


async def sync_fork_branch(access_token: str, owner: str, repo: str, branch: str) -> bool:
    """Fast-forward a fork's branch to its upstream branch (best effort).

    Reviewed blob SHAs are read from the upstream repository, but a fork that
    was created earlier can be far behind it. Checked against the stale fork,
    every modified file looks "changed while you were reviewing it", and a
    commit built on it would carry old versions of other upstream changes.
    Returns False when GitHub cannot sync it (for example a diverged fork);
    the precondition checks that follow still refuse a stale revision.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/merge-upstream",
                headers=_github_headers(access_token),
                json={"branch": branch},
            )
    except httpx.HTTPError as error:
        logger.info("Could not sync fork %s/%s: %s", owner, repo, type(error).__name__)
        return False
    if response.status_code == 200:
        return True
    logger.info("Fork %s/%s was not synced with upstream (HTTP %s)", owner, repo, response.status_code)
    return False


_PRESERVED_MODES = {"100644", "100755", "120000"}
# Hard ceiling on directory listings for one change; the per-request budget
# is sized from the paths (every distinct directory prefix plus the root).
_MAX_TREE_READS = 400


def _tree_read_budget(paths: list[str]) -> int:
    prefixes = {""}
    for path in paths:
        parts = path.split("/")[:-1]
        prefixes.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    return min(_MAX_TREE_READS, len(prefixes))


async def _current_file_modes(client: httpx.AsyncClient, owner: str, repo: str, headers: dict[str, str],
                              base_tree_sha: str, paths: list[str]) -> dict[str, str]:
    """Return the Git mode of each existing file in the base tree (best effort).

    Every tree entry must name a mode, and always writing 100644 silently
    dropped the executable bit of an edited script (or replaced a symlink
    with a regular file). Directory listings are read one level at a time,
    so the cost follows the depth of the edited paths, not the repository
    size; the read budget is sized to cover every directory the paths need.
    A mode that cannot be determined raises instead of silently turning an
    executable script or symlink into a regular file.
    """
    listings: dict[str, list] = {}
    budget = _tree_read_budget(paths)

    async def entries(tree_sha: str) -> list:
        if tree_sha not in listings:
            if len(listings) >= budget:
                raise LookupError("tree read budget exhausted")
            response = await client.get(
                f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/trees/{quote(tree_sha, safe='')}", headers=headers,
            )
            if response.status_code != 200:
                _raise_for_api(response, "read the repository tree")
            data = response.json() if response.status_code == 200 else None
            if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
                raise LookupError("tree unavailable")
            listings[tree_sha] = data["tree"]
        return listings[tree_sha]

    def find(listing: list, name: str, kind: str) -> dict | None:
        return next((entry for entry in listing if isinstance(entry, dict) and entry.get("path") == name and entry.get("type") == kind), None)

    modes: dict[str, str] = {}
    for path in paths:
        try:
            *directories, name = path.split("/")
            tree_sha = base_tree_sha
            for directory in directories:
                entry = find(await entries(tree_sha), directory, "tree")
                if entry is None:
                    raise LookupError(path)
                tree_sha = str(entry.get("sha") or "")
            entry = find(await entries(tree_sha), name, "blob")
            if entry is None:
                raise LookupError(path)
            if str(entry.get("mode")) in _PRESERVED_MODES:
                modes[path] = str(entry["mode"])
        except (GitHubRateLimitError, GitHubAuthRequiredError, GitHubPermissionError):
            raise
        except Exception as error:
            logger.warning("Could not determine the file mode of %s: %s", path, error)
            raise GitHubAPIError(
                f"Could not read the current file mode of {path} from GitHub, so nothing was pushed. Try again."
            ) from error
    return modes


async def get_file_at_ref(
    access_token: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
    *,
    metadata_only: bool = False,
    max_content_bytes: int | None = None,
    allow_public_fallback: bool = False,
) -> dict[str, Any]:
    """Return file text and revision, or only metadata for large files."""
    path = validate_file_path(file_path)
    endpoint = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(endpoint, headers=_github_headers(access_token))
        if response.status_code in {401, 403} and allow_public_fallback:
            public_response = await client.get(endpoint, headers=_github_public_headers())
            if public_response.status_code < 400:
                response = public_response
        _raise_for_api(response, "read the requested file")
        data = response.json() if "json" in response.headers.get("content-type", "").lower() else None
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubAPIError("The selected path is not a file.")
        sha = str(data.get("sha") or "").strip()
        size = int(data.get("size") or 0)
        if metadata_only or (max_content_bytes is not None and size > max_content_bytes):
            content = ""
        elif size <= 1_000_000 and data.get("content"):
            try:
                content = base64.b64decode(str(data["content"]).encode(), validate=False).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise GitHubAPIError("GitHub returned a non-text file.") from error
        else:
            if not sha:
                raise GitHubAPIError("GitHub did not return a file revision.")
            blob_endpoint = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/blobs/{quote(sha, safe='')}"
            blob = await client.get(blob_endpoint, headers=_github_headers(access_token))
            if blob.status_code in {401, 403} and allow_public_fallback:
                public_blob = await client.get(blob_endpoint, headers=_github_public_headers())
                if public_blob.status_code < 400:
                    blob = public_blob
            _raise_for_api(blob, "read the requested file blob")
            if "json" in blob.headers.get("content-type", "").lower():
                blob_data = blob.json()
                try:
                    content = base64.b64decode(str(blob_data.get("content") or "").encode(), validate=False).decode("utf-8")
                except (ValueError, UnicodeDecodeError) as error:
                    raise GitHubAPIError("GitHub returned a non-text file.") from error
            else:
                content = blob.text
        return {"path": path, "sha": sha, "size": size, "content": content, "ref": ref}


async def fetch_raw_file(
    owner: str,
    repo: str,
    file_path: str,
    *,
    ref: str = "HEAD",
    max_bytes: int,
) -> bytes | None:
    """Read a public file's exact bytes from raw.githubusercontent.com.

    The raw CDN is not subject to the REST API's 60 requests/hour anonymous
    limit, which shared Render egress IPs exhaust quickly. Returns None when
    the file does not exist at ``ref``. The read is bounded so a huge file is
    never buffered.
    """
    path = validate_file_path(file_path)
    url = (
        f"{settings.github_raw_base_url.rstrip('/')}/{quote(owner, safe='')}/{quote(repo, safe='')}/"
        f"{quote(ref, safe='')}/{quote(path, safe='/')}"
    )
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Codebase-Intelligence-App"}) as response:
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise GitHubAPIError(f"GitHub could not return {path} (HTTP {response.status_code}).")
            chunks: list[bytes] = []
            total = 0
            async for block in response.aiter_bytes():
                total += len(block)
                if total > max_bytes:
                    raise GitHubFileTooLargeError(f"{path} is too large to edit safely in the browser.")
                chunks.append(block)
    return b"".join(chunks)


async def fetch_issue(owner: str, repo: str, number: int, *, access_token: str | None = None) -> dict[str, Any] | None:
    """Return an issue's title and body, or None when it cannot be read."""
    headers = _github_headers(access_token) if access_token else _github_public_headers()
    endpoint = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/issues/{int(number)}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(endpoint, headers=headers)
            if response.status_code in {401, 403} and access_token:
                response = await client.get(endpoint, headers=_github_public_headers())
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    # The issue only adds acceptance criteria: an unreadable body must make
    # the edit continue without it, not escape as an HTTP 500.
    try:
        data = response.json()
        if not isinstance(data, dict) or data.get("pull_request"):
            return None
        labels = data.get("labels") if isinstance(data.get("labels"), list) else []
        return {
            "number": int(data.get("number") or number),
            "title": str(data.get("title") or "")[:300],
            "body": str(data.get("body") or "")[:12_000],
            "state": str(data.get("state") or ""),
            "html_url": str(data.get("html_url") or ""),
            "labels": [str(label.get("name")) for label in labels if isinstance(label, dict)][:10],
        }
    except (TypeError, ValueError):
        return None


async def file_exists_at_ref(access_token: str, owner: str, repo: str, file_path: str, ref: str) -> bool:
    try:
        await get_file_at_ref(access_token, owner, repo, file_path, ref, metadata_only=True)
        return True
    except GitHubNotFoundError:
        return False


async def get_blob_text(access_token: str, owner: str, repo: str, sha: str, max_bytes: int) -> str:
    """Return the UTF-8 text of one Git blob (``GET /git/blobs/{sha}``).

    Used to read the exact revision a change was reviewed against, which the
    contents API can no longer return once the branch has moved on. A blob
    larger than ``max_bytes`` or one that is not UTF-8 text is refused.
    """
    clean_sha = str(sha or "").strip().lower()
    if not is_object_sha(clean_sha):
        raise GitHubNotFoundError("GitHub could not read the file revision: it is not a valid Git object name.")
    endpoint = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/blobs/{clean_sha}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(endpoint, headers=_github_headers(access_token))
    _raise_for_api(response, "read the file revision")
    try:
        data = response.json()
    except ValueError as error:
        raise GitHubAPIError("GitHub returned an unreadable file revision.") from error
    if not isinstance(data, dict):
        raise GitHubAPIError("GitHub returned an unreadable file revision.")
    limit = max(0, int(max_bytes))
    try:
        declared = int(data.get("size") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise GitHubFileTooLargeError(f"The file revision {clean_sha[:7]} is larger than {limit:,} bytes.")
    content = data.get("content")
    if not isinstance(content, str):
        raise GitHubAPIError("GitHub did not return the file revision's content.")
    try:
        raw = base64.b64decode(content.encode("ascii"), validate=False) if data.get("encoding", "base64") == "base64" else content.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as error:  # binascii.Error is a ValueError
        raise GitHubAPIError("GitHub returned an unreadable file revision.") from error
    if len(raw) > limit:
        raise GitHubFileTooLargeError(f"The file revision {clean_sha[:7]} is larger than {limit:,} bytes.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitHubAPIError("The file revision is not UTF-8 text.") from error


# --------------------------------------------------------- syntax checking


_SYNTAX_STATUSES = frozenset({"pass", "warn", "fail", "skipped"})


def _skipped_syntax(detail: str) -> dict[str, Any]:
    return {"status": "skipped", "checker": "none", "errors": [], "detail": detail}


def _normalise_syntax(result: Any) -> dict[str, Any]:
    """Coerce a checker result into ``{status, checker, errors, detail}``."""
    if not isinstance(result, dict):
        return _skipped_syntax("No syntax result was produced for this file.")
    status = str(result.get("status") or "skipped")
    errors = []
    for error in result.get("errors") or []:
        if not isinstance(error, dict):
            continue
        line, column = error.get("line"), error.get("column")
        errors.append({
            "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
            "column": column if isinstance(column, int) and not isinstance(column, bool) else None,
            "message": str(error.get("message") or "Syntax error")[:300],
        })
        if len(errors) >= 20:
            break
    return {
        "status": status if status in _SYNTAX_STATUSES else "skipped",
        "checker": str(result.get("checker") or "none"),
        "errors": errors,
        "detail": str(result.get("detail") or "")[:400],
    }


async def check_syntax_items(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Syntax-check ``[{file_path, after, before?}]`` with ``agent.quality``.

    Checks run one at a time through ``check_syntax_many`` (it serialises
    parsers process-wide for Render's memory limit). The checker is never a
    reason to lose a push: when it is unavailable or crashes, every file is
    reported as ``skipped`` with the reason instead of ``pass``.
    """
    if not items:
        return {}
    try:
        from agent.quality import check_syntax_many  # local: agent.quality must stay import-cycle free
    except ImportError:
        logger.warning("agent.quality is unavailable; syntax checks are skipped")
        return {item["file_path"]: _skipped_syntax("The syntax checker is not available on the server.") for item in items}
    try:
        results = await check_syntax_many(items)
    except Exception:
        logger.exception("Syntax check failed unexpectedly")
        return {item["file_path"]: _skipped_syntax("The syntax check could not run.") for item in items}
    results = results if isinstance(results, dict) else {}
    return {item["file_path"]: _normalise_syntax(results.get(item["file_path"])) for item in items}


def describe_syntax_error(path: str, result: dict[str, Any]) -> str:
    """``path:line:column message (checker)`` for the first error of a result."""
    errors = result.get("errors") or []
    if not errors:
        return f"{path}: {result.get('detail') or 'syntax error'}"
    first = errors[0]
    location = path
    if first.get("line") is not None:
        location += f":{first['line']}"
        if first.get("column") is not None:
            location += f":{first['column']}"
    return f"{location} {first.get('message') or 'Syntax error'} ({result.get('checker') or 'parser'})"


# ------------------------------------------------- re-applying on upstream


def _reapply_failure_reason(base: str, edited: str, latest: str) -> str:
    """Name the first reviewed hunk that no longer fits the latest version."""
    hunks = exact_hunks(base, edited)
    if not hunks:
        return "the reviewed change could not be expressed as exact edits"
    lf_latest = latest.replace("\r\n", "\n")
    for number, hunk in enumerate(hunks, start=1):
        old = str(hunk.get("old") or "").replace("\r\n", "\n")
        count = lf_latest.count(old) if old else 0
        if count == 0:
            return f"hunk {number} no longer matches the latest version"
        if count > 1:
            return f"hunk {number} now matches {count} places in the latest version"
    return "its hunks overlap in the latest version"


async def _refresh_on_latest(access_token: str, owner: str, repo: str, item: dict[str, str], latest_meta: dict[str, Any]) -> dict[str, str]:
    """Re-apply a reviewed change to a file that changed upstream since review.

    Reads the reviewed blob and the latest blob, replays the reviewed hunks on
    the latest text (every hunk must match exactly once) and requires the
    result to parse. Returns the ``refreshed_files`` entry and replaces
    ``item["new_content"]`` with the merged text; raises
    ``GitHubStaleConflictError`` naming the reason otherwise.
    """
    path = item["file_path"]
    reviewed_sha = item["expected_file_sha"].strip().lower()
    latest_sha = str(latest_meta.get("sha") or "").strip().lower()

    def refuse(reason: str) -> GitHubStaleConflictError:
        return GitHubStaleConflictError(
            f"{path} changed while you were reviewing it, and {reason}. Refresh the files and review again."
        )

    if not (is_object_sha(reviewed_sha) and is_object_sha(latest_sha)):
        raise refuse("the reviewed revision cannot be compared with the latest one")
    limit = max(1, int(settings.github_editor_max_bytes))
    try:
        latest_size = int(latest_meta.get("size") or 0)
    except (TypeError, ValueError):
        latest_size = 0
    if latest_size > limit:
        raise refuse("the latest version is too large to re-apply the change safely")
    try:
        base = await get_blob_text(access_token, owner, repo, reviewed_sha, limit)
        latest = await get_blob_text(access_token, owner, repo, latest_sha, limit)
    except (GitHubAuthRequiredError, GitHubRateLimitError, GitHubPermissionError):
        raise
    except GitHubAPIError as error:
        raise refuse("the reviewed and latest versions could not be read to re-apply it") from error
    edited = item["new_content"]
    merged = reapply_change(base, edited, latest)
    if merged is None:
        raise refuse(_reapply_failure_reason(base, edited, latest))
    checked = (await check_syntax_items([{"file_path": path, "after": merged, "before": latest}])).get(path) or {}
    if checked.get("status") == "fail":
        raise refuse(f"the merged file no longer parses: {describe_syntax_error(path, checked)}")
    item["new_content"] = merged
    return {
        "file_path": path,
        "reviewed_sha": reviewed_sha,
        "latest_sha": latest_sha,
        "note": "Re-applied the reviewed change on top of the newer upstream version; every hunk matched exactly once.",
    }


async def create_branch_and_commit_files(
    access_token: str,
    owner: str,
    repo: str,
    base_branch: str,
    branch_name: str,
    files: list[dict[str, Any]],
    commit_message: str,
    *,
    expected_base_sha: str = "",
    source_owner: str = "",
    source_repo: str = "",
    resume_commit_sha: str = "",
) -> dict[str, Any]:
    """Create one atomic commit for a bounded set of reviewed files.

    Every blob is created from the complete current file content supplied by
    the review UI. The base ref and each expected file SHA are checked before
    any branch is created, so a stale multi-file review cannot overwrite a
    concurrent GitHub change.

    ``source_owner``/``source_repo`` name the repository the change was
    reviewed against (the upstream of a fork). The base commit, the file
    preconditions and the file modes are read there, while blobs, the tree,
    the commit and the branch are created in ``owner``/``repo``. A fork shares
    GitHub's object network with its upstream, so the commit can use the
    upstream head as its parent: the pull request then contains only the
    reviewed change, whatever the state of the fork's own default branch.

    ``resume_commit_sha`` is the commit an earlier attempt of the same
    operation already put on ``branch_name``. When the branch still points at
    it, no new objects are created and the call returns that commit, so a
    retry after a failed pull request creation can continue.

    A modified file whose upstream revision moved on since review is not
    refused outright: the reviewed change is re-applied to the latest version
    when every hunk still matches exactly once and the result still parses
    (listed in ``refreshed_files``); otherwise ``GitHubStaleConflictError``
    names the file and the reason. Nothing is written before that decision.
    """
    clean_branch = validate_branch_name(branch_name)
    limit = max_pr_files()
    if not isinstance(files, list) or not files or len(files) > limit:
        raise GitHubAPIError(f"A pull request must contain between one and {limit} files.")
    clean_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise GitHubAPIError("Each pull request file must be an object.")
        clean_path = validate_file_path(str(item.get("file_path") or ""))
        if clean_path in seen_paths:
            raise GitHubAPIError("A pull request cannot contain the same file twice.")
        new_content = item.get("new_content")
        if not isinstance(new_content, str) or not new_content:
            raise GitHubAPIError("File content cannot be empty.")
        action = str(item.get("action") or "modify")
        if action not in {"modify", "create"}:
            raise GitHubAPIError("Each pull request file must be modified or created.")
        seen_paths.add(clean_path)
        clean_files.append({
            "file_path": clean_path,
            "new_content": new_content,
            "expected_file_sha": str(item.get("expected_file_sha") or "").strip(),
            "action": action,
        })
    headers = _github_headers(access_token)
    read_owner, read_repo = (source_owner or owner).strip(), (source_repo or repo).strip()
    if resume_commit_sha.strip():
        head = await get_branch_head(access_token, owner, repo, clean_branch)
        if head and head == resume_commit_sha.strip():
            return {"commit_sha": head, "branch_name": clean_branch, "owner": owner, "repo": repo, "resumed": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        ref_res = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(read_owner, read_repo)}/git/ref/heads/{quote(base_branch, safe='')}", headers=headers)
        _raise_for_api(ref_res, "read the base branch")
        base_commit_sha = str(ref_res.json().get("object", {}).get("sha") or "")
        if expected_base_sha and base_commit_sha != expected_base_sha:
            raise GitHubStaleConflictError("The base branch changed while you were reviewing this change. Refresh the file and review again.")
        # Check all file preconditions before creating any blob or ref: a
        # modified file must still be at the reviewed revision (or the change
        # must re-apply cleanly to the newer one), and a new file must not
        # have appeared upstream in the meantime.
        refreshed: list[dict[str, str]] = []
        for item in clean_files:
            if item["action"] == "create":
                if await file_exists_at_ref(access_token, read_owner, read_repo, item["file_path"], base_branch):
                    raise GitHubStaleConflictError(f"{item['file_path']} now exists upstream. Regenerate the change.")
            elif item["expected_file_sha"]:
                try:
                    file_res = await get_file_at_ref(access_token, read_owner, read_repo, item["file_path"], base_branch, metadata_only=True)
                except GitHubNotFoundError as error:
                    raise GitHubStaleConflictError(
                        f"{item['file_path']} was deleted or moved upstream after it was reviewed. Regenerate the change."
                    ) from error
                if file_res["sha"] != item["expected_file_sha"]:
                    refreshed.append(await _refresh_on_latest(access_token, read_owner, read_repo, item, file_res))
        commit_res = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(read_owner, read_repo)}/git/commits/{quote(base_commit_sha, safe='')}", headers=headers)
        _raise_for_api(commit_res, "read the base commit")
        base_tree_sha = str(commit_res.json().get("tree", {}).get("sha") or "")
        modes = await _current_file_modes(
            client, read_owner, read_repo, headers, base_tree_sha,
            [item["file_path"] for item in clean_files if item["action"] == "modify"],
        )
        tree_entries = []
        for item in clean_files:
            blob_res = await client.post(
                f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/blobs",
                headers=headers,
                json={"content": item["new_content"], "encoding": "utf-8"},
            )
            if blob_res.status_code != 201:
                _raise_for_api(blob_res, "create the file blob")
            blob_sha = str(blob_res.json().get("sha") or "")
            if not blob_sha:
                raise GitHubAPIError("GitHub did not return a blob revision.")
            tree_entries.append({"path": item["file_path"], "mode": modes.get(item["file_path"], "100644"), "type": "blob", "sha": blob_sha})
        tree_res = await client.post(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/trees", headers=headers, json={"base_tree": base_tree_sha, "tree": tree_entries})
        if tree_res.status_code != 201:
            _raise_for_api(tree_res, "create the file tree")
        new_tree_sha = str(tree_res.json().get("sha") or "")
        default_message = "Update " + ", ".join(item["file_path"] for item in clean_files[:3])
        if len(clean_files) > 3:
            default_message += f" and {len(clean_files) - 3} more files"
        new_commit_res = await client.post(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/commits", headers=headers, json={"message": commit_message.strip() or default_message, "tree": new_tree_sha, "parents": [base_commit_sha]})
        if new_commit_res.status_code != 201:
            _raise_for_api(new_commit_res, "create the commit")
        new_commit_sha = str(new_commit_res.json().get("sha") or "")
        new_ref_res = await client.post(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/refs", headers=headers, json={"ref": f"refs/heads/{clean_branch}", "sha": new_commit_sha})
        if new_ref_res.status_code == 422:
            # An earlier attempt (whose reply was lost, or whose PR step
            # failed) may already have created this branch with the same
            # change. Continue when it holds exactly this tree on this base.
            existing = await _branch_commit(client, owner, repo, headers, clean_branch)
            if existing and existing.get("tree") == new_tree_sha and base_commit_sha in existing.get("parents", []):
                return {"commit_sha": existing["sha"], "branch_name": clean_branch, "owner": owner, "repo": repo, "resumed": True,
                        "refreshed_files": refreshed}
            raise GitHubConflictError(
                f"The branch {clean_branch} already exists with different content. Choose a new branch name to avoid overwriting it."
            )
        if new_ref_res.status_code != 201:
            _raise_for_api(new_ref_res, "create the branch")
    return {"commit_sha": new_commit_sha, "branch_name": clean_branch, "owner": owner, "repo": repo, "refreshed_files": refreshed}


async def _branch_commit(client: httpx.AsyncClient, owner: str, repo: str, headers: dict[str, str], branch: str) -> dict | None:
    """Return {sha, tree, parents} of a branch head, or None when absent."""
    try:
        ref_res = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/ref/heads/{quote(branch, safe='/')}", headers=headers)
        if ref_res.status_code != 200:
            return None
        sha = str((ref_res.json().get("object") or {}).get("sha") or "")
        if not sha:
            return None
        commit_res = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/commits/{quote(sha, safe='')}", headers=headers)
        if commit_res.status_code != 200:
            return None
        data = commit_res.json()
        return {
            "sha": sha,
            "tree": str((data.get("tree") or {}).get("sha") or ""),
            "parents": [str(parent.get("sha") or "") for parent in data.get("parents") or [] if isinstance(parent, dict)],
        }
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None


async def get_branch_head(access_token: str, owner: str, repo: str, branch: str) -> str | None:
    """Return the commit SHA a branch points at, or None when it does not exist."""
    headers = _github_headers(access_token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}/git/ref/heads/{quote(branch, safe='/')}", headers=headers)
    if response.status_code == 404:
        return None
    _raise_for_api(response, "read the pull request branch")
    try:
        return str((response.json().get("object") or {}).get("sha") or "") or None
    except (ValueError, AttributeError):
        return None


async def create_branch_and_commit(
    access_token: str,
    owner: str,
    repo: str,
    base_branch: str,
    branch_name: str,
    file_path: str,
    new_content: str,
    commit_message: str,
    *,
    expected_base_sha: str = "",
    expected_file_sha: str = "",
    **options: Any,
) -> dict[str, Any]:
    """Backward-compatible one-file wrapper around the atomic commit path."""
    return await create_branch_and_commit_files(
        access_token,
        owner,
        repo,
        base_branch,
        branch_name,
        [{"file_path": file_path, "new_content": new_content, "expected_file_sha": expected_file_sha}],
        commit_message,
        expected_base_sha=expected_base_sha,
        **options,
    )


async def open_pull_request(access_token: str, base_owner: str, base_repo: str, head: str, base_branch: str, title: str, body: str) -> dict[str, Any]:
    headers = _github_headers(access_token)
    payload = {"title": title.strip() or "Codebase Intelligence: Proposed Changes", "head": head.strip(), "base": base_branch.strip(), "body": body.strip() or "Automated pull request created via Codebase Intelligence."}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{GITHUB_API_BASE}/repos/{_api_path(base_owner, base_repo)}/pulls", headers=headers, json=payload)
        if response.status_code == 422:
            data = response.json()
            if any("already exists" in str(error.get("message", "")).lower() for error in data.get("errors", [])):
                # GitHub filters by head only in "owner:branch" form.
                head_filter = head.strip() if ":" in head else f"{base_owner}:{head.strip()}"
                list_res = await client.get(f"{GITHUB_API_BASE}/repos/{_api_path(base_owner, base_repo)}/pulls", params={"head": head_filter, "state": "open"}, headers=headers)
                if list_res.status_code == 200 and list_res.json():
                    existing = list_res.json()[0]
                    return {"pr_url": existing["html_url"], "pr_number": existing["number"], "title": existing["title"], "already_existed": True}
            raise GitHubAPIError("GitHub rejected the pull request. Check the branch and repository permissions.")
        if response.status_code != 201:
            _raise_for_api(response, "create the pull request")
        data = response.json()
        return {"pr_url": data["html_url"], "pr_number": data["number"], "title": data["title"], "already_existed": False}


# ------------------------------------------------------- PR and CI status


_CI_FAILED = frozenset({"failure", "timed_out", "cancelled", "startup_failure"})
_CI_PENDING = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
# Failures (and checks blocked on a maintainer) first, then pending, then the rest.
_CI_ORDER = {"failure": 0, "action_required": 1, "pending": 2, "neutral": 3, "skipped": 4, "success": 5}
_CI_BUCKETS = ("success", "failure", "pending", "neutral", "skipped", "action_required")
MAX_STATUS_CHECKS = 50


def _ci_bucket(status: str, conclusion: str | None) -> str:
    """Classify one check as success/failure/pending/neutral/skipped/action_required."""
    if status == "action_required" or conclusion == "action_required":
        return "action_required"
    if status in _CI_PENDING or (status != "completed" and not conclusion):
        return "pending"
    if conclusion == "success":
        return "success"
    if conclusion in _CI_FAILED:
        return "failure"
    if conclusion == "skipped":
        return "skipped"
    return "neutral"


def _ci_status(value: str) -> str:
    return value if value in {"completed", "in_progress"} else "queued"


def _text_or_none(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _pull_summary(pull: dict[str, Any], number: int) -> dict[str, Any]:
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    mergeable = pull.get("mergeable")
    try:
        pr_number = int(pull.get("number") or number)
    except (TypeError, ValueError):
        pr_number = number
    return {
        "number": pr_number,
        "url": str(pull.get("html_url") or ""),
        "state": "closed" if pull.get("state") == "closed" else "open",
        "merged": bool(pull.get("merged") or pull.get("merged_at")),
        "draft": bool(pull.get("draft")),
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "mergeable_state": str(pull.get("mergeable_state") or "unknown"),
        "head_sha": str(head.get("sha") or ""),
        "base_ref": str(base.get("ref") or ""),
        "head_repo": str(head_repo.get("full_name") or ""),
    }


def _ci_summary(counts: dict[str, int]) -> str:
    waiting = counts["action_required"]
    total = counts["total"] - waiting
    parts = []
    if total:
        parts.append(f"{counts['success']} of {total}{' other' if waiting else ''} check{'s' if total != 1 else ''} passed")
        for key, word in (("failure", "failed"), ("pending", "pending"), ("skipped", "skipped"), ("neutral", "neutral")):
            if counts[key]:
                parts.append(f"{counts[key]} {word}")
    checks = ", ".join(parts)
    if waiting:
        lead = f"{waiting} run{'s are' if waiting != 1 else ' is'} waiting for a maintainer's approval"
        return f"{lead}; {checks}" if checks else f"{lead} before CI starts."
    return checks or "No checks have reported on this pull request yet."


def summarize_pull_request_status(
    pull: dict[str, Any],
    combined_status: dict[str, Any],
    check_runs: dict[str, Any],
    workflow_runs: dict[str, Any],
    *,
    number: int,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Merge commit statuses, check runs and workflow runs into one CI verdict.

    ``state`` is ``action_required`` when any run or check waits for a
    maintainer (fork PRs from first-time contributors), else ``failure`` when
    anything failed, else ``pending`` while anything runs, else ``success``
    when at least one check reported, else ``none``.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    runs = [run for run in (check_runs.get("check_runs") or []) if isinstance(run, dict)]
    suites_with_runs = set()
    for run in runs:
        suite = run.get("check_suite") if isinstance(run.get("check_suite"), dict) else {}
        if suite.get("id") is not None:
            suites_with_runs.add(suite.get("id"))
        status = str(run.get("status") or "")
        conclusion = _text_or_none(run.get("conclusion"))
        entries.append((_ci_bucket(status, conclusion), {
            "name": str(run.get("name") or "Check"),
            "source": "check_run",
            "status": _ci_status(status),
            "conclusion": conclusion,
            "url": _text_or_none(run.get("html_url")) or _text_or_none(run.get("details_url")),
            "started_at": _text_or_none(run.get("started_at")),
            "completed_at": _text_or_none(run.get("completed_at")),
        }))
    for item in combined_status.get("statuses") or []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "pending")
        conclusion = {"success": "success", "failure": "failure", "error": "failure"}.get(state)
        entries.append((_ci_bucket("completed" if conclusion else "pending", conclusion), {
            "name": str(item.get("context") or "status"),
            "source": "status",
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
            "url": _text_or_none(item.get("target_url")),
            "started_at": _text_or_none(item.get("created_at")),
            "completed_at": _text_or_none(item.get("updated_at")) if conclusion else None,
        }))
    # A workflow run normally shows up as its jobs' check runs. It is listed
    # itself only when it has none (for example a fork PR's run waiting for a
    # maintainer's approval, which has not started any job) or needs action.
    for run in workflow_runs.get("workflow_runs") or []:
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "")
        conclusion = _text_or_none(run.get("conclusion"))
        if status == "action_required" and not conclusion:
            conclusion = "action_required"
        bucket = _ci_bucket(status, conclusion)
        if run.get("check_suite_id") in suites_with_runs and bucket != "action_required":
            continue
        entries.append((bucket, {
            "name": str(run.get("name") or run.get("display_title") or "Workflow run"),
            "source": "workflow_run",
            "status": _ci_status(status),
            "conclusion": conclusion,
            "url": _text_or_none(run.get("html_url")),
            "started_at": _text_or_none(run.get("run_started_at")) or _text_or_none(run.get("created_at")),
            "completed_at": _text_or_none(run.get("updated_at")) if status == "completed" else None,
        }))
    counts = {"total": len(entries), **{bucket: 0 for bucket in _CI_BUCKETS}}
    for bucket, _ in entries:
        counts[bucket] += 1
    if counts["action_required"]:
        state = "action_required"
    elif counts["failure"]:
        state = "failure"
    elif counts["pending"]:
        state = "pending"
    elif entries:
        state = "success"
    else:
        state = "none"
    ordered = sorted(entries, key=lambda pair: (_CI_ORDER[pair[0]], pair[1]["name"].lower()))
    pr = _pull_summary(pull, number)
    return {
        "pr": pr,
        "state": state,
        "summary": _ci_summary(counts),
        "counts": counts,
        "checks": [entry for _, entry in ordered[:MAX_STATUS_CHECKS]],
        "checks_url": f"{pr['url']}/checks" if pr["url"] else "",
        "fetched_at": fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _ci_payload(response: httpx.Response, action: str, *, best_effort: bool = False) -> dict[str, Any]:
    """JSON of a CI read; a missing resource is empty.

    Token and rate-limit errors always propagate. ``best_effort`` reads (the
    Actions API, which may be disabled for a repository) are empty on any
    other error instead of failing the whole status.
    """
    if response.status_code == 404:
        return {}
    if response.status_code >= 400:
        try:
            _raise_for_api(response, action)
        except (GitHubAuthRequiredError, GitHubRateLimitError):
            raise
        except GitHubAPIError:
            if best_effort:
                return {}
            raise
    try:
        data = response.json()
    except ValueError as error:
        if best_effort:
            return {}
        raise GitHubAPIError(f"GitHub returned an unreadable answer while trying to {action}.") from error
    return data if isinstance(data, dict) else {}


async def get_pull_request_status(access_token: str, owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return the pull request's merge readiness and CI checks.

    Four reads: the pull request, the combined commit status, the check runs
    and the Actions workflow runs of the head commit (the last only to detect
    runs waiting for a maintainer's approval). Raises ``GitHubNotFoundError``
    when the pull request does not exist or is not visible.
    """
    number = int(number)
    base_url = f"{GITHUB_API_BASE}/repos/{_api_path(owner, repo)}"
    headers = _github_headers(access_token)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{base_url}/pulls/{number}", headers=headers)
        _raise_for_api(response, f"read pull request #{number}")
        try:
            pull = response.json()
        except ValueError as error:
            raise GitHubAPIError("GitHub returned an unreadable pull request.") from error
        if not isinstance(pull, dict):
            raise GitHubAPIError("GitHub returned an unreadable pull request.")
        head_sha = str((pull.get("head") or {}).get("sha") or "").strip().lower() if isinstance(pull.get("head"), dict) else ""
        if not is_object_sha(head_sha):
            raise GitHubAPIError("GitHub returned a pull request without a head commit.")
        status_res, runs_res, workflow_res = await asyncio.gather(
            client.get(f"{base_url}/commits/{head_sha}/status", headers=headers),
            client.get(f"{base_url}/commits/{head_sha}/check-runs", headers=headers, params={"per_page": 100}),
            client.get(f"{base_url}/actions/runs", headers=headers, params={"head_sha": head_sha, "per_page": 20}),
        )
    return summarize_pull_request_status(
        pull,
        _ci_payload(status_res, "read the commit statuses"),
        _ci_payload(runs_res, "read the check runs"),
        _ci_payload(workflow_res, "read the workflow runs", best_effort=True),
        number=number,
    )
