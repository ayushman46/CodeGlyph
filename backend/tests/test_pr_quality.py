"""Pull-request quality gates: push-time syntax check, upstream freshness,
repository conventions, the PR file cap and CI status (SPEC G1/G6c/G7/G8/G9).

GitHub is an in-memory double served through ``httpx.MockTransport`` that
models blobs, trees, commits, refs, pull requests and CI results, so the real
client code (URLs, payloads, error mapping) runs end to end. Nothing here
reaches the network. ``agent.quality`` is replaced by a small ``ast``-based
checker with the same contract, so these tests do not depend on parser
binaries; ``RealCheckerTests`` repeats the key cases with the real module.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.code_edit import apply_hunks, create_edit_ticket, git_blob_sha
from api.auth import get_current_user
from api.github_router import (
    PushFileRequest,
    PushPRRequest,
    _github_http_error,
    _pr_conventions,
    push_pr,
    router as github_router,
)
from config import settings
from database import TursoStore, apply_migrations
from github.github_client import (
    GitHubAPIError,
    GitHubConflictError,
    GitHubFileTooLargeError,
    GitHubStaleConflictError,
    create_branch_and_commit_files,
    get_blob_text,
    summarize_pull_request_status,
)
from github.token_crypto import encrypt_token

run = asyncio.run
_REAL_ASYNC_CLIENT = httpx.AsyncClient

SECRET = "test-editing-secret-pr-quality-0123"
TOKEN_KEY = "test-github-token-encryption-secret-pr-quality"
USER = "user-quality"
OTHER = "user-other"
REPO_ID = "repo-quality"
REPO_NAME = "widgets"
GITHUB_URL = "https://github.com/acme/widgets"
FULL = "acme/widgets"
HEAD_SHA = "a" * 40


def _sha(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()


# --------------------------------------------------------------- fakes


class FakeGitHub:
    """A small GitHub: one repository (acme/widgets by default) with real object names."""

    def __init__(self, files: dict[str, str] | None = None, *, push: bool = True, full_name: str = FULL):
        self.full_name = full_name
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, tuple[str, str]]] = {}  # flat {path: (mode, blob)}
        self.subtrees: dict[str, tuple[str, str]] = {}  # sha -> (root tree, "dir/")
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: dict[int, dict] = {}
        self.statuses: dict[str, list] = {}
        self.check_runs: dict[str, list] = {}
        self.workflow_runs: dict[str, list] = {}
        self.requests: list[tuple[str, str]] = []
        self.overrides: list[tuple[str, re.Pattern, httpx.Response]] = []
        self.permissions = {"push": push, "admin": False}
        root = self._tree({path: ("100644", self.blob(text)) for path, text in (files or {}).items()})
        self.refs["main"] = self._commit(root, [], "initial")
        self.transport = httpx.MockTransport(self.handle)

    # -- object model
    def blob(self, text: str | bytes) -> str:
        data = text.encode("utf-8") if isinstance(text, str) else text
        sha = git_blob_sha(data)
        self.blobs[sha] = data
        return sha

    def _tree(self, flat: dict[str, tuple[str, str]]) -> str:
        sha = _sha("tree", sorted(flat.items()))
        self.trees[sha] = dict(flat)
        return sha

    def _commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = _sha("commit", tree, parents, message, len(self.commits))
        self.commits[sha] = {"tree": tree, "parents": list(parents), "message": message}
        return sha

    def flat(self, ref: str = "main") -> dict[str, tuple[str, str]]:
        commit = self.refs.get(ref, ref)
        return self.trees[self.commits[commit]["tree"]]

    def text(self, path: str, ref: str = "main") -> str:
        return self.blobs[self.flat(ref)[path][1]].decode("utf-8")

    def sha_of(self, path: str, ref: str = "main") -> str:
        return self.flat(ref)[path][1]

    def commit_upstream(self, changes: dict[str, str | None], message: str = "upstream") -> str:
        flat = dict(self.flat("main"))
        for path, text in changes.items():
            if text is None:
                flat.pop(path, None)
            else:
                flat[path] = (flat.get(path, ("100644", ""))[0], self.blob(text))
        self.refs["main"] = self._commit(self._tree(flat), [self.refs["main"]], message)
        return self.refs["main"]

    def fail(self, method: str, pattern: str, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        self.overrides.append((method, re.compile(pattern), httpx.Response(status, json=payload or {"message": "error"}, headers=headers or {})))

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path in self.requests if method != "GET"]

    def patched(self):
        transport = self.transport

        def client(*args, **kwargs):
            kwargs.pop("transport", None)
            return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)

        return patch("httpx.AsyncClient", client)

    # -- HTTP
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.requests.append((request.method, path))
        for method, pattern, response in self.overrides:
            if method == request.method and pattern.search(path):
                return response
        prefix = f"/repos/{self.full_name}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(prefix):]
        body = json.loads(request.content or b"null") if request.method == "POST" else None
        return getattr(self, f"_{request.method.lower()}")(rest, request, body)

    def _get(self, rest: str, request: httpx.Request, _body) -> httpx.Response:
        not_found = httpx.Response(404, json={"message": "Not Found"})
        if rest == "":
            owner, name = self.full_name.split("/")
            return httpx.Response(200, json={"full_name": self.full_name, "name": name, "owner": {"login": owner},
                                             "default_branch": "main", "permissions": self.permissions})
        if rest.startswith("/git/ref/heads/"):
            sha = self.refs.get(rest.removeprefix("/git/ref/heads/"))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else not_found
        if rest.startswith("/git/commits/"):
            commit = self.commits.get(rest.rsplit("/", 1)[-1])
            if not commit:
                return not_found
            return httpx.Response(200, json={"tree": {"sha": commit["tree"]}, "parents": [{"sha": p} for p in commit["parents"]]})
        if rest.startswith("/git/trees/"):
            sha = rest.rsplit("/", 1)[-1]
            root, prefix = (sha, "") if sha in self.trees else self.subtrees.get(sha, (None, None))
            if root is None:
                return not_found
            listing, seen = [], set()
            for full_path, (mode, blob) in sorted(self.trees[root].items()):
                if not full_path.startswith(prefix):
                    continue
                relative = full_path[len(prefix):]
                if "/" in relative:
                    name = relative.split("/", 1)[0]
                    if name not in seen:
                        seen.add(name)
                        child = _sha("subtree", root, prefix + name + "/")
                        self.subtrees[child] = (root, prefix + name + "/")
                        listing.append({"path": name, "type": "tree", "mode": "040000", "sha": child})
                else:
                    listing.append({"path": relative, "type": "blob", "mode": mode, "sha": blob})
            return httpx.Response(200, json={"sha": sha, "tree": listing, "truncated": False})
        if rest.startswith("/contents/"):
            file_path = rest.removeprefix("/contents/")
            entry = self.flat(request.url.params.get("ref") or "main").get(file_path)
            if not entry:
                return not_found
            data = self.blobs[entry[1]]
            return httpx.Response(200, json={"type": "file", "path": file_path, "sha": entry[1], "size": len(data),
                                             "content": base64.b64encode(data).decode()})
        if rest.startswith("/git/blobs/"):
            data = self.blobs.get(rest.rsplit("/", 1)[-1])
            if data is None:
                return not_found
            return httpx.Response(200, json={"sha": rest.rsplit("/", 1)[-1], "size": len(data), "encoding": "base64",
                                             "content": base64.b64encode(data).decode()})
        match = re.fullmatch(r"/pulls/(\d+)", rest)
        if match:
            pull = self.pulls.get(int(match.group(1)))
            return httpx.Response(200, json=pull) if pull else not_found
        match = re.fullmatch(r"/commits/([0-9a-f]+)/status", rest)
        if match:
            statuses = self.statuses.get(match.group(1), [])
            return httpx.Response(200, json={"state": "pending", "statuses": statuses, "total_count": len(statuses)})
        match = re.fullmatch(r"/commits/([0-9a-f]+)/check-runs", rest)
        if match:
            runs = self.check_runs.get(match.group(1), [])
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if rest == "/actions/runs":
            runs = self.workflow_runs.get(request.url.params.get("head_sha"), [])
            return httpx.Response(200, json={"total_count": len(runs), "workflow_runs": runs})
        return not_found

    def _post(self, rest: str, _request: httpx.Request, body: dict) -> httpx.Response:
        if rest == "/git/blobs":
            return httpx.Response(201, json={"sha": self.blob(body["content"])})
        if rest == "/git/trees":
            flat = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                flat[entry["path"]] = (entry["mode"], entry["sha"])
            return httpx.Response(201, json={"sha": self._tree(flat)})
        if rest == "/git/commits":
            return httpx.Response(201, json={"sha": self._commit(body["tree"], body["parents"], body["message"])})
        if rest == "/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[branch] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if rest == "/pulls":
            number = len(self.pulls) + 1
            head = body["head"].split(":")[-1]
            self.pulls[number] = {
                "number": number, "html_url": f"https://github.com/{self.full_name}/pull/{number}", "title": body["title"],
                "body": body["body"], "state": "open", "draft": False, "merged": False, "mergeable": True,
                "mergeable_state": "clean", "head": {"sha": self.refs[head], "ref": head, "repo": {"full_name": self.full_name}},
                "base": {"ref": body["base"]},
            }
            return httpx.Response(201, json=self.pulls[number])
        return httpx.Response(404, json={"message": "Not Found"})


def fake_quality():
    """``agent.quality`` with the contract's shape: Python via ``ast``, baseline rule."""
    module = types.ModuleType("agent.quality")
    module.calls = []

    def errors(text):
        try:
            ast.parse(text)
            return []
        except SyntaxError as error:
            return [{"line": error.lineno, "column": error.offset, "message": error.msg}]

    def check_syntax(path, after, *, before=None):
        if not path.endswith(".py"):
            return {"status": "skipped", "checker": "none", "errors": [], "detail": "No syntax checker for this file type."}
        found = errors(after)
        if not found:
            return {"status": "pass", "checker": "ast", "errors": [], "detail": "parsed"}
        if before is not None and errors(before) and len(found) <= len(errors(before)):
            return {"status": "warn", "checker": "ast", "errors": found,
                    "detail": "this file already failed to parse with ast before the change"}
        return {"status": "fail", "checker": "ast", "errors": found, "detail": "syntax error"}

    async def check_syntax_many(items):
        module.calls.append([(item["file_path"], "before" in item) for item in items])
        return {item["file_path"]: check_syntax(item["file_path"], item["after"], before=item.get("before")) for item in items}

    module.check_syntax = check_syntax
    module.check_syntax_many = check_syntax_many
    return module


RETRY_PY = "import os\n\n\ndef retry_limit():\n    return 2\n\n\ndef other():\n    return 'x'\n"
RETRY_PY_EDITED = RETRY_PY.replace("return 2", "return 3")


class GitHubTestCase(unittest.TestCase):
    """Fake GitHub + fake ``agent.quality`` for one test."""

    files = {"src/retry.py": RETRY_PY, "README.md": "# Widgets\n"}

    def setUp(self):
        self.github = FakeGitHub(self.files)
        self.quality = fake_quality()
        for item in (self.github.patched(), patch.dict(sys.modules, {"agent.quality": self.quality})):
            item.start()
            self.addCleanup(item.stop)

    def commit(self, files, **kwargs):
        return run(create_branch_and_commit_files("token", "acme", "widgets", "main", kwargs.pop("branch", "codebase-intel/fix"),
                                                  files, kwargs.pop("message", "Fix"), **kwargs))


class RouterTestCase(GitHubTestCase):
    """Adds a migrated database, a connected GitHub account and the router."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'x.db'}", "local-test-token")
        self.addCleanup(lambda: run(self.store.close()))
        run(apply_migrations(self.store))
        now = "2026-10-01T00:00:00+00:00"
        run(self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) VALUES (?, ?, ?, ?, 'ready', 1, ?, ?)",
            [REPO_ID, USER, REPO_NAME, GITHUB_URL, now, now],
        ))
        run(self.store.execute(
            "INSERT INTO user_github_tokens (user_id, github_user_id, github_username, access_token, scope, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [USER, "1", "octocat", encrypt_token("gho_quality", TOKEN_KEY), "public_repo", now],
        ))
        for item in (
            patch.object(settings, "editing_ticket_secret", SECRET),
            patch.object(settings, "github_token_encryption_key", TOKEN_KEY),
            patch("api.github_router.get_turso_store", return_value=self.store),
        ):
            item.start()
            self.addCleanup(item.stop)

    def request(self, files, key="push-1", **fields):
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=[item.file_path for item in files], ttl_seconds=600)
        fields.setdefault("branch_name", "codebase-intel/fix")
        return PushPRRequest(repo_name=REPO_NAME, files=files, edit_ticket=ticket, idempotency_key=key, **fields)

    def push(self, request, user=USER):
        return run(push_pr(request, SimpleNamespace(id=user)))

    def http(self, user=USER):
        app = FastAPI()
        app.include_router(github_router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user)
        return TestClient(app)

    def store_proposal(self, proposal_id, *, conventions=None, pr_body="", user=USER):
        payload = {"id": proposal_id, "title": "t", "pr_body": pr_body, "files": []}
        if conventions is not None:
            payload["conventions"] = conventions
        run(self.store.execute(
            "INSERT INTO edit_proposals (id, user_id, repo_id, repo_name, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [proposal_id, user, REPO_ID, REPO_NAME, json.dumps(payload), "2026-10-01T00:00:00+00:00"],
        ))


# ------------------------------------------------- G8 upstream freshness


class FreshnessTests(GitHubTestCase):
    def test_upstream_line_above_the_hunk_is_reapplied_on_latest(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/retry.py": "# Copyright 2026 Acme\n" + RETRY_PY})
        latest = self.github.sha_of("src/retry.py")
        result = self.commit([{"file_path": "src/retry.py", "new_content": RETRY_PY_EDITED, "expected_file_sha": reviewed}])
        pushed = self.github.text("src/retry.py", result["commit_sha"])
        self.assertEqual(pushed, "# Copyright 2026 Acme\n" + RETRY_PY_EDITED)
        self.assertEqual(result["refreshed_files"], [{
            "file_path": "src/retry.py", "reviewed_sha": reviewed, "latest_sha": latest,
            "note": "Re-applied the reviewed change on top of the newer upstream version; every hunk matched exactly once.",
        }])
        # Built on the latest base commit, and the merge was syntax-checked against latest.
        self.assertEqual(self.github.commits[result["commit_sha"]]["parents"], [self.github.refs["main"]])
        self.assertIn([("src/retry.py", True)], self.quality.calls)

    def test_upstream_change_to_the_hunk_lines_is_a_stale_conflict_with_no_writes(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/retry.py": RETRY_PY.replace("return 2", "return 5")})
        with self.assertRaises(GitHubStaleConflictError) as context:
            self.commit([{"file_path": "src/retry.py", "new_content": RETRY_PY_EDITED, "expected_file_sha": reviewed}])
        self.assertIn("src/retry.py changed while you were reviewing it", str(context.exception))
        self.assertIn("hunk 1 no longer matches", str(context.exception))
        self.assertEqual(self.github.writes, [])
        error = _github_http_error(context.exception)
        self.assertEqual((error.status_code, error.headers["X-Error-Code"]), (409, "stale_conflict"))

    def test_a_merge_that_no_longer_parses_is_a_stale_conflict(self):
        base = "if ready:\n    a()\n    b()\n    c()\n    d()\nx = 1\n"
        edited = base.replace("    d()\n", "    d()\nelse:\n    e()\n")
        latest = base.replace("if ready:", "with ready:")
        github = FakeGitHub({"src/flow.py": base})
        with github.patched():
            reviewed = github.sha_of("src/flow.py")
            github.commit_upstream({"src/flow.py": latest})
            with self.assertRaises(GitHubStaleConflictError) as context:
                run(create_branch_and_commit_files("token", "acme", "widgets", "main", "codebase-intel/fix",
                                                   [{"file_path": "src/flow.py", "new_content": edited, "expected_file_sha": reviewed}], "Fix"))
        self.assertIn("the merged file no longer parses", str(context.exception))
        self.assertIn("src/flow.py:6", str(context.exception))
        self.assertEqual(github.writes, [])

    def test_crlf_upstream_file_keeps_crlf(self):
        base = RETRY_PY.replace("\n", "\r\n")
        github = FakeGitHub({"src/retry.py": base})
        with github.patched():
            reviewed = github.sha_of("src/retry.py")
            github.commit_upstream({"src/retry.py": "# header\r\n" + base})
            edited = base.replace("return 2", "return 3")
            result = run(create_branch_and_commit_files("token", "acme", "widgets", "main", "codebase-intel/fix",
                                                        [{"file_path": "src/retry.py", "new_content": edited, "expected_file_sha": reviewed}], "Fix"))
            pushed = github.text("src/retry.py", result["commit_sha"])
        self.assertEqual(pushed, "# header\r\n" + edited)
        self.assertNotIn("\n", pushed.replace("\r\n", ""))
        self.assertEqual(len(result["refreshed_files"]), 1)

    def test_unchanged_sha_takes_todays_path_without_blob_reads(self):
        reviewed = self.github.sha_of("src/retry.py")
        result = self.commit([{"file_path": "src/retry.py", "new_content": RETRY_PY_EDITED, "expected_file_sha": reviewed}])
        self.assertEqual(result["refreshed_files"], [])
        self.assertFalse([path for method, path in self.github.requests if method == "GET" and "/git/blobs/" in path])
        self.assertEqual(self.github.text("src/retry.py", result["commit_sha"]), RETRY_PY_EDITED)
        self.assertEqual(self.quality.calls, [])

    def test_unreadable_reviewed_revision_is_a_stale_conflict(self):
        self.github.commit_upstream({"src/retry.py": "# header\n" + RETRY_PY})
        for reviewed in ("reviewed-old", "b" * 40):  # not an object name / unknown blob
            with self.subTest(reviewed=reviewed), self.assertRaises(GitHubStaleConflictError) as context:
                self.commit([{"file_path": "src/retry.py", "new_content": RETRY_PY_EDITED, "expected_file_sha": reviewed}])
            self.assertIn("changed while you were reviewing it", str(context.exception))
        self.assertEqual(self.github.writes, [])

    def test_created_and_deleted_files_keep_their_refusals(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/new.py": "x = 1\n", "src/retry.py": None})
        with self.assertRaisesRegex(GitHubStaleConflictError, "src/new.py now exists upstream"):
            self.commit([{"file_path": "src/new.py", "new_content": "y = 2\n", "action": "create"}])
        with self.assertRaisesRegex(GitHubStaleConflictError, "src/retry.py was deleted or moved upstream"):
            self.commit([{"file_path": "src/retry.py", "new_content": RETRY_PY_EDITED, "expected_file_sha": reviewed}])
        self.assertEqual(self.github.writes, [])

    def test_get_blob_text_bounds_and_validates(self):
        sha = self.github.sha_of("src/retry.py")
        self.assertEqual(run(get_blob_text("t", "acme", "widgets", sha, 10_000)), RETRY_PY)
        with self.assertRaises(GitHubFileTooLargeError):
            run(get_blob_text("t", "acme", "widgets", sha, 10))
        binary = self.github.blob(b"\xff\xfe\x00binary")
        with self.assertRaisesRegex(GitHubAPIError, "not UTF-8"):
            run(get_blob_text("t", "acme", "widgets", binary, 10_000))
        before = len(self.github.requests)
        with self.assertRaises(GitHubAPIError):
            run(get_blob_text("t", "acme", "widgets", "../../etc", 10_000))
        self.assertEqual(len(self.github.requests), before)


# --------------------------------------------- G7 PR file cap (24 files)


class FileCapTests(GitHubTestCase):
    files = {f"src/m{index}.py": f"value = {index}\n" for index in range(25)}

    def test_create_branch_and_commit_files_accepts_24_and_refuses_25(self):
        files = [{"file_path": f"src/m{index}.py", "new_content": f"value = {index + 100}\n",
                  "expected_file_sha": self.github.sha_of(f"src/m{index}.py")} for index in range(25)]
        result = self.commit(files[:24])
        self.assertEqual(len([m for m, p in self.github.requests if m == "POST" and p.endswith("/git/blobs")]), 24)
        self.assertEqual(self.github.text("src/m23.py", result["commit_sha"]), "value = 123\n")
        with self.assertRaisesRegex(GitHubAPIError, "between one and 24 files"):
            self.commit(files, branch="codebase-intel/too-many")

    def test_push_request_accepts_24_files_and_rejects_25_with_422(self):
        files = [PushFileRequest(file_path=f"src/m{index}.py", new_content="x = 1\n", file_sha="a" * 40) for index in range(25)]
        self.assertEqual(len(PushPRRequest(repo_name=REPO_NAME, files=files[:24], edit_ticket="t").files), 24)
        with self.assertRaises(ValidationError):
            PushPRRequest(repo_name=REPO_NAME, files=files, edit_ticket="t")
        app = FastAPI()
        app.include_router(github_router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
        response = TestClient(app).post("/api/github/push-pr", json={
            "repo_name": REPO_NAME, "edit_ticket": "t", "files": [item.model_dump() for item in files],
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.github.requests, [])


# ------------------------------------------ G1 push-time syntax check


BROKEN_PY = "def retry_limit()\n    return 3\n"


class PushQualityTests(RouterTestCase):
    def broken_request(self, **fields):
        return self.request([PushFileRequest(file_path="src/retry.py", new_content=BROKEN_PY, file_sha=self.github.sha_of("src/retry.py"))], **fields)

    def test_syntax_failure_is_a_422_and_nothing_is_written(self):
        response = self.http().post("/api/github/push-pr", json=self.broken_request().model_dump())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["x-error-code"], "quality_failed")
        detail = response.json()["detail"]
        self.assertIn("1 file has syntax errors", detail["message"])
        self.assertEqual(detail["files"], [{"file_path": "src/retry.py", "line": 1, "column": 18, "message": "expected ':'"}])
        # Only the reviewed blob was read (baseline rule); no fork, blob, tree, ref or PR was created.
        self.assertEqual(self.github.writes, [])
        self.assertEqual([path for _, path in self.github.requests], [f"/repos/{FULL}/git/blobs/{self.github.sha_of('src/retry.py')}"])
        self.assertIsNone(run(self.store.fetch_one("SELECT status FROM github_change_operations")))

    def test_acknowledged_failure_is_pushed_and_reported(self):
        result = self.push(self.broken_request(acknowledge_quality=True))
        self.assertTrue(result["success"])
        self.assertEqual(result["quality"]["status"], "fail")
        self.assertTrue(result["quality"]["acknowledged"])
        self.assertEqual(result["quality"]["files"][0]["errors"][0]["line"], 1)
        pull = self.github.pulls[result["pr_number"]]
        self.assertEqual(self.github.text("src/retry.py", pull["head"]["sha"]), BROKEN_PY)

    def test_file_that_already_failed_before_the_change_only_warns(self):
        github = FakeGitHub({"src/legacy.py": "def legacy(\n    pass\n"})
        with github.patched():
            request = self.request([PushFileRequest(file_path="src/legacy.py", new_content="def legacy(\n    return 1\n",
                                                    file_sha=github.sha_of("src/legacy.py"))])
            result = self.push(request)
        self.assertEqual(result["quality"]["status"], "warn")
        self.assertFalse(result["quality"]["acknowledged"])
        self.assertIn("already failed to parse", result["quality"]["files"][0]["detail"])
        self.assertEqual(self.quality.calls, [[("src/legacy.py", False)], [("src/legacy.py", True)]])

    def test_clean_push_reports_pass_and_skipped_files_without_extra_reads(self):
        request = self.request([
            PushFileRequest(file_path="src/retry.py", new_content=RETRY_PY_EDITED, file_sha=self.github.sha_of("src/retry.py")),
            PushFileRequest(file_path="README.md", new_content="# Widgets\n\nRetries 3 times.\n", file_sha=self.github.sha_of("README.md")),
        ])
        result = self.push(request)
        self.assertEqual(result["quality"], {"status": "pass", "acknowledged": False, "files": [
            {"file_path": "src/retry.py", "status": "pass", "checker": "ast", "errors": [], "detail": "parsed"},
            {"file_path": "README.md", "status": "skipped", "checker": "none", "errors": [], "detail": "No syntax checker for this file type."},
        ]})
        self.assertFalse([path for method, path in self.github.requests if "/git/blobs/" in path and method == "GET"])
        self.assertEqual(result["refreshed_files"], [])

    def test_missing_or_crashing_checker_never_blocks_a_push(self):
        broken = types.ModuleType("agent.quality")

        async def explode(items):
            raise RuntimeError("parser crashed")

        broken.check_syntax_many = explode
        with patch.dict(sys.modules, {"agent.quality": broken}):
            result = self.push(self.broken_request(key="crash"))
        self.assertEqual(result["quality"]["files"][0]["status"], "skipped")
        self.assertEqual(result["quality"]["status"], "pass")
        with patch.dict(sys.modules, {"agent.quality": None}):  # import fails
            result = self.push(self.broken_request(key="missing", branch_name="codebase-intel/other"))
        self.assertEqual(result["quality"]["files"][0]["detail"], "The syntax checker is not available on the server.")

    def test_stale_conflict_through_the_api_has_its_error_code(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/retry.py": RETRY_PY.replace("return 2", "return 5")})
        request = self.request([PushFileRequest(file_path="src/retry.py", new_content=RETRY_PY_EDITED, file_sha=reviewed)])
        response = self.http().post("/api/github/push-pr", json=request.model_dump())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["x-error-code"], "stale_conflict")
        self.assertIn("src/retry.py", response.json()["detail"])
        self.assertIn("hunk 1 no longer matches", response.json()["detail"])
        self.assertNotIn("already exists", response.json()["detail"])  # the UI must not offer a new branch name
        self.assertFalse([path for method, path in self.github.requests if method == "POST"])
        self.assertEqual(run(self.store.fetch_one("SELECT status FROM github_change_operations"))["status"], "failed")

    def test_refreshed_files_and_replay_return_the_stored_result(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/retry.py": "# Copyright\n" + RETRY_PY})
        request = self.request([PushFileRequest(file_path="src/retry.py", new_content=RETRY_PY_EDITED, file_sha=reviewed)], key="replay")
        first = self.push(request)
        self.assertEqual(first["refreshed_files"][0]["file_path"], "src/retry.py")
        self.assertEqual(self.github.text("src/retry.py", self.github.pulls[first["pr_number"]]["head"]["sha"]), "# Copyright\n" + RETRY_PY_EDITED)
        requests = len(self.github.requests)
        replay = self.push(request)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.github.requests), requests)
        for key in ("refreshed_files", "conventions_applied", "convention_warnings", "quality"):
            self.assertIn(key, replay)

    def test_resumed_push_keeps_the_refreshed_files_of_the_first_attempt(self):
        reviewed = self.github.sha_of("src/retry.py")
        self.github.commit_upstream({"src/retry.py": "# Copyright\n" + RETRY_PY})
        request = self.request([PushFileRequest(file_path="src/retry.py", new_content=RETRY_PY_EDITED, file_sha=reviewed)], key="resume")
        self.github.fail("POST", r"/pulls$", 502, {"message": "Server Error"})
        with self.assertRaises(HTTPException):
            self.push(request)
        self.github.overrides.clear()
        result = self.push(request)
        self.assertTrue(result["success"])
        self.assertEqual([item["file_path"] for item in result["refreshed_files"]], ["src/retry.py"])
        self.assertEqual(len([path for method, path in self.github.requests if method == "POST" and path.endswith("/git/commits")]), 1)


# ------------------------------------------- G6c repository conventions


CONVENTIONAL = {"pr_template_path": None, "commit_style": "conventional", "commit_style_source": "recent commits (24 of 30)",
                "contributing_path": None, "agent_instructions_path": None, "dco_required": False, "notes": []}
PROPOSAL_ID = "8b0c3a52-6c55-4a31-9a8b-0d1f2e3a4b5c"


class ConventionTests(RouterTestCase):
    def push_with(self, *, title, commit_message="", body="## Summary\nRetry three times.", conventions=CONVENTIONAL,
                  pr_body="", key="conv", proposal_id=PROPOSAL_ID, owner=USER):
        if conventions is not None or pr_body:
            self.store_proposal(PROPOSAL_ID, conventions=conventions, pr_body=pr_body, user=owner)
        request = self.request(
            [PushFileRequest(file_path="src/retry.py", new_content=RETRY_PY_EDITED, file_sha=self.github.sha_of("src/retry.py"))],
            key=key, pr_title=title, pr_body=body, commit_message=commit_message, proposal_id=proposal_id,
            branch_name=f"codebase-intel/{key}",
        )
        result = self.push(request)
        pull = self.github.pulls[result["pr_number"]]
        commit = self.github.commits[pull["head"]["sha"]]
        return result, pull, commit

    def test_non_conventional_title_warns_once_and_is_not_rewritten(self):
        result, pull, commit = self.push_with(title="Raise the default retry limit", commit_message="Raise the default retry limit")
        self.assertEqual(result["convention_warnings"], [
            "Title does not follow Conventional Commits, which 24 of the last 30 commits use; a title check may fail.",
        ])
        self.assertEqual(pull["title"], "Raise the default retry limit")
        self.assertEqual(commit["message"], "Raise the default retry limit")
        self.assertEqual(result["conventions_applied"], [])

    def test_conventional_title_becomes_the_commit_subject(self):
        result, pull, commit = self.push_with(title="fix(retry): raise the default limit to 3",
                                              commit_message="Raise the limit\n\nThe old default gave up too early.")
        self.assertEqual(pull["title"], "fix(retry): raise the default limit to 3")
        self.assertEqual(commit["message"], "fix(retry): raise the default limit to 3\n\nThe old default gave up too early.")
        self.assertEqual([item["id"] for item in result["conventions_applied"]], ["conventional_commits", "commit_subject_from_title"])
        self.assertEqual(result["convention_warnings"], [])

    def test_conventional_commit_message_is_kept(self):
        result, _, commit = self.push_with(title="fix: raise the retry limit", commit_message="fix(retry): use 3 attempts")
        self.assertEqual(commit["message"], "fix(retry): use 3 attempts")
        self.assertEqual([item["id"] for item in result["conventions_applied"]], ["conventional_commits"])

    def test_template_closing_keyword_and_dco(self):
        template_body = "## Description\nRaise the limit.\n\n## How Has This Been Tested?\n- [ ] npm test\n\n## Checklist\n- [ ] Docs"
        conventions = {**CONVENTIONAL, "commit_style": "plain", "pr_template_path": ".github/pull_request_template.md", "dco_required": True}
        result, pull, _ = self.push_with(title="Raise the retry limit", conventions=conventions, pr_body=template_body,
                                         body=template_body + "\n\nFixes #42")
        self.assertEqual([item["id"] for item in result["conventions_applied"]], ["pull_request_template", "closing_keyword"])
        self.assertIn("#42", result["conventions_applied"][1]["detail"])
        self.assertEqual(result["convention_warnings"], ["DCO sign-off required; GitHub's DCO check may fail because the commit has no Signed-off-by line."])
        self.assertEqual(pull["body"], template_body + "\n\nFixes #42")
        self.assertIn("- [ ] npm test", pull["body"])  # a checklist box is never ticked

    def test_template_sections_removed_by_the_user_warn(self):
        template_body = "## Description\nx\n\n## How Has This Been Tested?\n- [ ] npm test\n\n## Checklist\n- [ ] Docs"
        conventions = {**CONVENTIONAL, "commit_style": "unknown", "pr_template_path": ".github/pull_request_template.md"}
        result, pull, _ = self.push_with(title="Raise the retry limit", conventions=conventions, pr_body=template_body,
                                         body="## Description\nJust this.")
        self.assertEqual(result["conventions_applied"], [])
        self.assertEqual(len(result["convention_warnings"]), 1)
        self.assertIn('"How Has This Been Tested?", "Checklist" sections', result["convention_warnings"][0])
        self.assertEqual(pull["body"], "## Description\nJust this.")

    def test_proposal_of_another_user_or_without_conventions_is_ignored(self):
        result, _, commit = self.push_with(title="Raise the retry limit", owner=OTHER)
        self.assertEqual((result["conventions_applied"], result["convention_warnings"]), ([], []))
        self.assertEqual(commit["message"], "Update src/retry.py")
        result, _, _ = self.push_with(title="Raise it", conventions=None, pr_body="", proposal_id="", key="none",
                                      body="Closes: #7")
        self.assertEqual(result["conventions_applied"][0]["id"], "closing_keyword")

    def test_request_fields(self):
        self.assertEqual(len(PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", pr_body="x" * 12_000).pr_body), 12_000)
        self.assertEqual(len(PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", pr_body="x" * 20_000).pr_body), 20_000)
        with self.assertRaises(ValidationError):
            PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", pr_body="x" * 20_001)
        with self.assertRaises(ValidationError):
            PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", proposal_id="../../etc/passwd")
        request = PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", proposal_id=PROPOSAL_ID)
        self.assertEqual(PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", proposal_id=None).proposal_id, "")
        self.assertEqual((request.proposal_id, request.acknowledge_quality), (PROPOSAL_ID, False))

    def test_conventions_helper_is_pure(self):
        message, applied, warnings = _pr_conventions(
            {"commit_style": "conventional", "commit_style_source": "commitlint config"},
            title="Add jitter", body="", commit_message="Add jitter", generated_body="",
        )
        self.assertEqual(message, "Add jitter")
        self.assertEqual(applied, [])
        self.assertEqual(warnings, ["Title does not follow Conventional Commits, which this repository uses (commitlint config); a title check may fail."])
        self.assertEqual(_pr_conventions({}, title="x", body="fixes acme/widgets#9", commit_message="x", generated_body="")[1][0]["id"], "closing_keyword")
        self.assertEqual(_pr_conventions({}, title="x", body="prefixes #9", commit_message="x", generated_body="")[1], [])


# ----------------------------------------------------- G9 PR / CI status


def check_run(name, status="completed", conclusion="success", suite=1):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": f"https://github.com/{FULL}/runs/{name}",
            "started_at": "2026-10-21T08:00:00Z", "completed_at": "2026-10-21T08:05:00Z" if status == "completed" else None,
            "check_suite": {"id": suite}}


class PullRequestStatusTests(RouterTestCase):
    def setUp(self):
        super().setUp()
        self.github.pulls[12] = {
            "number": 12, "html_url": f"https://github.com/{FULL}/pull/12", "state": "open", "draft": False, "merged": False,
            "mergeable": True, "mergeable_state": "clean", "head": {"sha": HEAD_SHA, "repo": {"full_name": "octocat/widgets"}},
            "base": {"ref": "main"},
        }

    def status(self, number=12, *, client=None, **params):
        return (client or self.http()).get("/api/github/pr-status", params={"repo_name": REPO_NAME, "pr_number": number, **params})

    def test_success(self):
        self.github.check_runs[HEAD_SHA] = [check_run("lint"), check_run("Node.js 22", conclusion="skipped")]
        self.github.statuses[HEAD_SHA] = [{"context": "ci/circleci", "state": "success", "target_url": "https://circleci.com/x"}]
        response = self.status()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["state"], "success")
        self.assertEqual(data["counts"], {"total": 3, "success": 2, "failure": 0, "pending": 0, "neutral": 0, "skipped": 1, "action_required": 0})
        self.assertEqual(data["summary"], "2 of 3 checks passed, 1 skipped")
        self.assertEqual(data["pr"], {"number": 12, "url": f"https://github.com/{FULL}/pull/12", "state": "open", "merged": False,
                                      "draft": False, "mergeable": True, "mergeable_state": "clean", "head_sha": HEAD_SHA,
                                      "base_ref": "main", "head_repo": "octocat/widgets"})
        self.assertEqual(data["checks_url"], f"https://github.com/{FULL}/pull/12/checks")
        self.assertTrue(data["fetched_at"])
        status_check = next(check for check in data["checks"] if check["source"] == "status")
        self.assertEqual((status_check["status"], status_check["conclusion"], status_check["url"]), ("completed", "success", "https://circleci.com/x"))
        reads = [path for _, path in self.github.requests]
        self.assertEqual(sorted(reads), sorted([f"/repos/{FULL}/pulls/12", f"/repos/{FULL}/commits/{HEAD_SHA}/status",
                                                f"/repos/{FULL}/commits/{HEAD_SHA}/check-runs", f"/repos/{FULL}/actions/runs"]))

    def test_failure_is_listed_first(self):
        self.github.check_runs[HEAD_SHA] = [check_run("a-lint"), check_run("b-test", status="in_progress", conclusion=None),
                                            check_run("c-build", conclusion="timed_out")]
        self.github.statuses[HEAD_SHA] = [{"context": "deploy", "state": "error"}]
        data = self.status().json()
        self.assertEqual(data["state"], "failure")
        self.assertEqual([check["name"] for check in data["checks"]], ["c-build", "deploy", "b-test", "a-lint"])
        self.assertEqual(data["summary"], "1 of 4 checks passed, 2 failed, 1 pending")

    def test_pending(self):
        self.github.check_runs[HEAD_SHA] = [check_run("lint"), check_run("test", status="queued", conclusion=None)]
        self.github.statuses[HEAD_SHA] = [{"context": "ci/legacy", "state": "pending"}]
        data = self.status().json()
        self.assertEqual(data["state"], "pending")
        self.assertEqual(data["counts"]["pending"], 2)
        self.assertEqual({check["status"] for check in data["checks"] if check["name"] != "lint"}, {"queued", "in_progress"})

    def test_action_required_for_a_fork_waiting_for_approval(self):
        self.github.check_runs[HEAD_SHA] = [check_run("lint", suite=1)]
        self.github.workflow_runs[HEAD_SHA] = [
            {"name": "CI", "status": "completed", "conclusion": "action_required", "check_suite_id": 2,
             "html_url": f"https://github.com/{FULL}/actions/runs/2"},
            {"name": "Lint", "status": "completed", "conclusion": "success", "check_suite_id": 1},  # duplicate of its check run
        ]
        data = self.status().json()
        self.assertEqual(data["state"], "action_required")
        self.assertEqual(data["counts"]["total"], 2)
        self.assertEqual(data["checks"][0]["source"], "workflow_run")
        self.assertEqual(data["summary"], "1 run is waiting for a maintainer's approval; 1 of 1 other check passed")
        self.github.check_runs[HEAD_SHA] = []
        self.github.workflow_runs[HEAD_SHA] = self.github.workflow_runs[HEAD_SHA][:1]
        self.assertEqual(self.status().json()["summary"], "1 run is waiting for a maintainer's approval before CI starts.")

    def test_none_and_dirty(self):
        self.github.pulls[12]["mergeable_state"] = "dirty"
        self.github.pulls[12]["mergeable"] = False
        data = self.status().json()
        self.assertEqual((data["state"], data["counts"]["total"], data["checks"]), ("none", 0, []))
        self.assertEqual(data["summary"], "No checks have reported on this pull request yet.")
        self.assertEqual((data["pr"]["mergeable_state"], data["pr"]["mergeable"]), ("dirty", False))

    def test_at_most_50_checks(self):
        self.github.check_runs[HEAD_SHA] = [check_run(f"job-{index:02d}") for index in range(60)] + [check_run("zz", conclusion="failure")]
        data = self.status().json()
        self.assertEqual(len(data["checks"]), 50)
        self.assertEqual(data["checks"][0]["name"], "zz")
        self.assertEqual(data["counts"]["total"], 61)

    def test_errors(self):
        unknown = self.status(99)
        self.assertEqual((unknown.status_code, unknown.headers["x-error-code"]), (404, "github_pr_not_found"))
        self.github.fail("GET", r"/pulls/12$", 401, {"message": "Bad credentials"})
        self.assertEqual(self.status().status_code, 401)
        self.github.overrides.clear()
        self.github.fail("GET", r"/check-runs$", 403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "retry-after": "120"})
        limited = self.status()
        self.assertEqual((limited.status_code, limited.headers["retry-after"], limited.headers["x-error-code"]), (429, "120", "github_rate_limit"))
        self.github.overrides.clear()
        self.github.fail("GET", r"/pulls/12$", 403, {"message": "Resource not accessible by integration"})
        forbidden = self.status()
        self.assertEqual((forbidden.status_code, forbidden.headers["x-error-code"]), (403, "github_permission"))
        self.github.overrides.clear()
        self.github.fail("GET", r"/commits/.*/status$", 500, {"message": "boom"})
        self.assertEqual(self.status().status_code, 502)
        self.github.overrides.clear()
        self.github.fail("GET", r"/actions/runs$", 404, {"message": "Actions disabled"})  # best effort
        self.assertEqual(self.status().status_code, 200)

    def test_repo_must_be_the_users_and_pr_number_valid(self):
        before = len(self.github.requests)
        response = self.http().get("/api/github/pr-status", params={"repo_name": "someone-elses", "pr_number": 12})
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Repository not found."))
        for number in (0, -1, 1_000_000_001, "abc"):
            with self.subTest(number=number):
                self.assertEqual(self.status(number).status_code, 422)
        self.assertEqual(len(self.github.requests), before)

    def test_not_connected_is_401(self):
        run(self.store.execute("DELETE FROM user_github_tokens"))
        self.assertEqual(self.status().status_code, 401)
        self.assertEqual(self.github.requests, [])

    def test_summary_helper_handles_sparse_payloads(self):
        data = summarize_pull_request_status({"number": 3, "head": {"sha": HEAD_SHA}, "head_repo": None}, {}, {"check_runs": [None, {}]},
                                             {"workflow_runs": "x"}, number=3, fetched_at="2026-10-21T08:00:00+00:00")
        self.assertEqual(data["pr"]["head_repo"], "")
        self.assertEqual(data["pr"]["mergeable"], None)
        self.assertEqual(data["counts"]["total"], 1)
        self.assertEqual(data["state"], "pending")


# --------------------------------------- with the real agent.quality module


@unittest.skipUnless(importlib.util.find_spec("agent.quality") is not None, "agent.quality has not landed yet")
class RealCheckerTests(RouterTestCase):
    """The same gates with the real checkers (esbuild / tree-sitter / ast)."""

    files = {"source/utils/delay.ts": "export const delay = async (ms: number): Promise<void> => {\n\tawait new Promise(resolve => {\n\t\tsetTimeout(resolve, ms);\n\t});\n};\n"}

    def setUp(self):
        super().setUp()
        sys.modules.pop("agent.quality", None)  # undo the fake for this class
        self.addCleanup(sys.modules.pop, "agent.quality", None)

    def test_broken_typescript_is_refused_with_its_location(self):
        before = self.github.text("source/utils/delay.ts")
        broken = before.removesuffix("};\n")  # drop the closing brace
        request = self.request([PushFileRequest(file_path="source/utils/delay.ts", new_content=broken,
                                                file_sha=self.github.sha_of("source/utils/delay.ts"))])
        response = self.http().post("/api/github/push-pr", json=request.model_dump())
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.headers["x-error-code"], "quality_failed")
        located = response.json()["detail"]["files"][0]
        self.assertEqual(located["file_path"], "source/utils/delay.ts")
        self.assertIsInstance(located["line"], int)
        self.assertEqual(self.github.writes, [])

    def test_valid_typescript_passes_and_upstream_merge_is_rechecked(self):
        reviewed = self.github.sha_of("source/utils/delay.ts")
        before = self.github.text("source/utils/delay.ts")
        self.github.commit_upstream({"source/utils/delay.ts": "// Licensed MIT\n" + before})
        edited = before.replace("(ms: number)", "(ms: number = 0)")
        result = self.push(self.request([PushFileRequest(file_path="source/utils/delay.ts", new_content=edited, file_sha=reviewed)]))
        self.assertEqual(result["quality"]["status"], "pass")
        self.assertIn(result["quality"]["files"][0]["checker"], {"esbuild", "tree-sitter"})
        self.assertEqual(result["refreshed_files"][0]["file_path"], "source/utils/delay.ts")
        self.assertEqual(self.github.text("source/utils/delay.ts", self.github.pulls[result["pr_number"]]["head"]["sha"]),
                         "// Licensed MIT\n" + edited)


if __name__ == "__main__":
    unittest.main()
