"""Regression tests for the code-editing, PR-push and API-security QA fixes.

Covers API-SEC-01/PR-2 (body cap), API-SEC-02 (OAuth CSRF), API-SEC-05,
API-SEC-06/PR-7, API-SEC-08, EDIT-BIGFILE-VIEW, EDIT-HEX-ISSUE-REF/EP-03,
EP-01, EP-02, EP-05/PR-3, EP-09, EP-13, EP-14, EXP-6, PR-1, PR-4, PR-5, PR-6.
Every GitHub and model boundary is patched; stores are embedded libsql files.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from fastapi.testclient import TestClient

import test_answers_and_editing as base
from agent.code_edit import EditingAuthorizationError, create_edit_ticket, verify_edit_ticket
from agent.editor import CodeEditor, EditRequest, reconstruct_indexed_file, run_code_edit
from agent.nemotron import final_code_edit_block
from api.auth import get_current_user
from api.github_router import PushFileRequest, PushPRRequest, github_callback, github_complete, CompleteGithubRequest, push_pr
from api.repository_state import fetch_owned_repository
from config import settings
from github import github_client as gc
from github.github_client import (
    GitHubAPIError,
    GitHubAuthRequiredError,
    GitHubConflictError,
    GitHubFileTooLargeError,
    GitHubForkNotReadyError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    _raise_for_api,
    create_branch_and_commit_files,
)
from github.token_crypto import encrypt_token
from retrieval.retriever import build_evidence_plan

run = asyncio.run
http_response = base.http_response
USER, OTHER, REPO_NAME = base.USER, base.OTHER, base.REPO_NAME
SECRET, TOKEN_KEY = base.SECRET, base.TOKEN_KEY


def api_response(status, payload=None, headers=None):
    response = base.http_response(status, payload)
    response.headers = {"content-type": "application/json", **(headers or {})}
    return response


# --------------------------------------------------------------- EP-09


class StructuredOutputTests(unittest.TestCase):
    def test_draft_inside_reasoning_is_ignored_and_the_last_block_wins(self):
        content = (
            '<think>First draft: <code_edit>{"new": "DRAFT"}</code_edit> wait, that is wrong.</think>'
            '<code_edit>{"new": "FINAL"}</code_edit>'
        )
        self.assertEqual(final_code_edit_block(content), '<code_edit>{"new": "FINAL"}</code_edit>')
        self.assertEqual(
            final_code_edit_block('<code_edit>{"a": 1}</code_edit> retry <code_edit>{"a": 2}</code_edit>'),
            '<code_edit>{"a": 2}</code_edit>',
        )
        self.assertIsNone(final_code_edit_block("<think><code_edit>{}</code_edit></think>no block"))


# ------------------------------------------------------- API-SEC-06 / PR-7


class TicketMessageTests(unittest.TestCase):
    def test_specific_rejection_reasons_reach_the_user(self):
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path="src/a.py", ttl_seconds=600)
        with self.assertRaisesRegex(EditingAuthorizationError, "not valid for this repository"):
            verify_edit_ticket(SECRET, ticket, user_id=USER, repo_name="other", file_path="src/a.py")
        with self.assertRaisesRegex(EditingAuthorizationError, "not valid for this file"):
            verify_edit_ticket(SECRET, ticket, user_id=USER, repo_name=REPO_NAME, file_path="src/b.py")
        body = base64.urlsafe_b64encode(json.dumps(
            {"v": 1, "u": USER, "r": REPO_NAME, "p": "src/a.py", "e": int(time.time()) - 1, "n": "x"}
        ).encode()).decode().rstrip("=")
        expired = f"v1.{body}.{hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()}"
        with self.assertRaisesRegex(EditingAuthorizationError, "expired"):
            verify_edit_ticket(SECRET, expired, user_id=USER, repo_name=REPO_NAME, file_path="src/a.py")


# ------------------------------------------- API-SEC-01 / 05 / 08, PR-2


class ApiHardeningTests(base.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        import main

        self.main = main
        self.client = TestClient(main.app)

    def test_oversized_bodies_are_rejected_before_parsing_or_auth(self):
        big = b'{"repo_name":"w","question":"' + b"x" * (self.main.DEFAULT_MAX_BODY_BYTES + 10) + b'"}'
        response = self.client.post("/api/query", content=big, headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertIn("too large", response.json()["detail"])
        # The push endpoint accepts complete files up to the change limit...
        self.assertGreater(self.main.max_body_bytes_for("/api/github/push-pr"), settings.max_github_change_bytes)
        with patch.object(settings, "max_github_change_bytes", 1000):
            limit = self.main.max_body_bytes_for("/api/github/push-pr")
            response = self.client.post("/api/github/push-pr", content=b"x" * (limit + 1), headers={"content-type": "application/json"})
        # ...and still refuses more, without authenticating first.
        self.assertEqual(response.status_code, 413)

    def test_chunked_bodies_are_counted_while_streaming(self):
        middleware = self.main.BodySizeLimitMiddleware(AsyncMock())
        sent = []
        messages = [{"type": "http.request", "body": b"x" * 200_000, "more_body": True} for _ in range(3)]

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/api/query", "headers": []}
        run(middleware(scope, receive, send))
        self.assertEqual(sent[0]["status"], 413)
        middleware.app.assert_not_awaited()
        self.assertEqual(len(messages), 1, "the body must not be read past the limit")

        # A small chunked body is replayed to the application intact.
        seen = []

        async def app(scope, receive, send):
            while True:
                message = await receive()
                seen.append(message.get("body"))
                if not message.get("more_body"):
                    break

        small = [{"type": "http.request", "body": b"ab", "more_body": True}, {"type": "http.request", "body": b"c", "more_body": False}]

        async def receive_small():
            return small.pop(0)

        run(self.main.BodySizeLimitMiddleware(app)(scope, receive_small, send))
        self.assertEqual(seen, [b"ab", b"c"])

    def test_lone_surrogate_is_a_json_422_not_a_500(self):
        self.main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
        self.addCleanup(self.main.app.dependency_overrides.pop, get_current_user, None)
        response = self.client.post(
            "/api/query", content=b'{"repo_name":"beta","question":"where is \\ud800 booking"}',
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertTrue(detail and all("input" not in item for item in detail))

    def test_unknown_api_paths_are_json_404(self):
        if not hasattr(self.main, "serve_frontend"):
            self.skipTest("frontend/dist is not built")
        response = self.client.get("/api/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response.headers["content-type"])


# ------------------------------------------------------------ PR-4, PR-6


class GitHubErrorMappingTests(unittest.TestCase):
    def test_only_401_means_reconnect(self):
        cases = (
            (api_response(401, {"message": "Bad credentials"}), GitHubAuthRequiredError),
            (api_response(403, {"message": "You have exceeded a secondary rate limit."}), GitHubRateLimitError),
            (api_response(403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 120)}), GitHubRateLimitError),
            (api_response(429, {}), GitHubRateLimitError),
            (api_response(403, {"message": "Although you appear to have the correct authorization credentials, the `acme` organization has enabled OAuth App access restrictions"}), GitHubPermissionError),
            (api_response(403, {"message": "Resource not accessible by integration"}), GitHubPermissionError),
            (api_response(404, {"message": "Not Found"}), GitHubNotFoundError),
            (api_response(500, {}), GitHubAPIError),
        )
        for response, expected in cases:
            with self.subTest(status=response.status_code, expected=expected.__name__):
                with self.assertRaises(expected) as context:
                    _raise_for_api(response, "read the repository")
                self.assertIs(type(context.exception), expected)
        with self.assertRaises(GitHubNotFoundError) as context:
            _raise_for_api(api_response(404), "read the repository")
        self.assertEqual(str(context.exception), "GitHub could not read the repository: it was not found or is not visible to your account.")
        with self.assertRaises(GitHubPermissionError) as context:
            _raise_for_api(api_response(403, {"message": "x has enabled OAuth App access restrictions"}), "create the pull request")
        self.assertIn("organization", str(context.exception))

    def test_router_status_codes(self):
        from api.github_router import _github_http_error

        self.assertEqual(_github_http_error(GitHubAuthRequiredError("x")).status_code, 401)
        limited = _github_http_error(GitHubRateLimitError("x", 90))
        self.assertEqual((limited.status_code, limited.headers["Retry-After"]), (429, "90"))
        permission = _github_http_error(GitHubPermissionError("x"))
        self.assertEqual((permission.status_code, permission.headers["X-Error-Code"]), (403, "github_permission"))
        self.assertEqual(_github_http_error(GitHubForkNotReadyError("x")).status_code, 503)
        self.assertEqual(_github_http_error(GitHubConflictError("x")).status_code, 409)
        self.assertEqual(_github_http_error(GitHubNotFoundError("x")).status_code, 404)
        self.assertEqual(_github_http_error(GitHubAPIError("x")).status_code, 400)

    def test_fork_that_is_still_being_created_is_retryable(self):
        async def post(url, **kwargs):
            return api_response(202, {"owner": {"login": "bob"}, "name": "widgets"})

        with patch("httpx.AsyncClient.post", AsyncMock(side_effect=post)), \
             patch("httpx.AsyncClient.get", AsyncMock(return_value=api_response(404))), \
             patch("asyncio.sleep", AsyncMock()):
            with self.assertRaises(GitHubForkNotReadyError) as context:
                run(gc.fork_repo("token", "acme", "widgets", username="bob"))
        self.assertIn("still creating your fork", str(context.exception))


# ------------------------------------------------- GitHub double (git data)


class FakeGitData:
    """Routes git-data REST calls; upstream acme/widgets, fork bob/widgets."""

    def __init__(self, *, trees, files, branch_exists=None):
        self.trees = trees
        self.files = files
        self.branch_exists = branch_exists  # {"sha", "tree", "parents"} or None
        self.gets, self.posts = [], []

    async def get(self, url, **kwargs):
        path = url.removeprefix("https://api.github.com")
        self.gets.append(path)
        if path.endswith("/repos/acme/widgets/git/ref/heads/main"):
            return api_response(200, {"object": {"sha": "upstream-head"}})
        if path.endswith("/git/ref/heads/codebase-intel/fix") and self.branch_exists:
            return api_response(200, {"object": {"sha": self.branch_exists["sha"]}})
        if "/repos/acme/widgets/git/commits/upstream-head" in path:
            return api_response(200, {"tree": {"sha": "root"}})
        if self.branch_exists and path.endswith(f"/git/commits/{self.branch_exists['sha']}"):
            return api_response(200, {"tree": {"sha": self.branch_exists["tree"]}, "parents": [{"sha": p} for p in self.branch_exists["parents"]]})
        if "/repos/acme/widgets/git/trees/" in path:
            sha = path.rsplit("/", 1)[-1]
            return api_response(200, {"tree": self.trees[sha]})
        return api_response(404, {"message": "Not Found"})

    async def post(self, url, **kwargs):
        path = url.removeprefix("https://api.github.com")
        self.posts.append((path, kwargs.get("json")))
        if path.endswith("/git/blobs"):
            return api_response(201, {"sha": f"blob-{len(self.posts)}"})
        if path.endswith("/git/trees"):
            return api_response(201, {"sha": "new-tree"})
        if path.endswith("/git/commits"):
            return api_response(201, {"sha": "new-commit"})
        if path.endswith("/git/refs"):
            return api_response(422 if self.branch_exists else 201, {"message": "Reference already exists"})
        raise AssertionError(path)

    async def contents(self, token, owner, repo, path, ref, **kwargs):
        if (owner, repo) != ("acme", "widgets"):
            raise AssertionError(f"file checks must use upstream, got {owner}/{repo}")
        if path not in self.files:
            raise GitHubNotFoundError("GitHub could not read the requested file: it was not found.")
        return {"sha": self.files[path]}

    def patches(self):
        return (
            patch("httpx.AsyncClient.get", AsyncMock(side_effect=self.get)),
            patch("httpx.AsyncClient.post", AsyncMock(side_effect=self.post)),
            patch("github.github_client.get_file_at_ref", self.contents),
        )


def deep_tree(paths, modes):
    """Nested tree listings for ``paths`` keyed by synthetic tree SHAs."""
    trees: dict[str, list] = {"root": []}
    for path in paths:
        parts = path.split("/")
        current = "root"
        for depth, name in enumerate(parts[:-1]):
            child = "t-" + "-".join(parts[:depth + 1])
            if not any(entry["path"] == name for entry in trees[current]):
                trees[current].append({"path": name, "type": "tree", "mode": "040000", "sha": child})
                trees.setdefault(child, [])
            current = child
        trees[current].append({"path": parts[-1], "type": "blob", "mode": modes.get(path, "100644"), "sha": "s:" + path})
    return trees


def commit(fake, files, **kwargs):
    patches = fake.patches()
    with patches[0], patches[1], patches[2]:
        return run(create_branch_and_commit_files("token", kwargs.pop("owner", "acme"), kwargs.pop("repo", "widgets"), "main",
                                                  "codebase-intel/fix", files, "msg", **kwargs))


class CommitFlowTests(unittest.TestCase):
    def test_modes_are_preserved_for_many_deep_directories(self):
        paths = [f"packages/p{index}/src/lib/deep/nested/file{index}.ts" for index in range(7)] + ["packages/testing/scripts/run.sh"]
        trees = deep_tree(paths, {"packages/testing/scripts/run.sh": "100755"})
        fake = FakeGitData(trees=trees, files={path: "s:" + path for path in paths})
        files = [{"file_path": path, "new_content": "x\n", "expected_file_sha": "s:" + path} for path in paths]
        result = commit(fake, files)
        self.assertEqual(result["commit_sha"], "new-commit")
        tree = next(body for path, body in fake.posts if path.endswith("/git/trees"))
        modes = {entry["path"]: entry["mode"] for entry in tree["tree"]}
        self.assertEqual(modes["packages/testing/scripts/run.sh"], "100755")
        self.assertGreater(sum(1 for path in fake.gets if "/git/trees/" in path), 24)

    def test_unknown_mode_refuses_instead_of_defaulting(self):
        trees = {"root": [{"path": "src", "type": "tree", "mode": "040000", "sha": "t-src"}], "t-src": []}
        fake = FakeGitData(trees=trees, files={"src/run.sh": "s"})
        with self.assertRaisesRegex(GitHubAPIError, "file mode of src/run.sh"):
            commit(fake, [{"file_path": "src/run.sh", "new_content": "x\n", "expected_file_sha": "s"}])
        self.assertFalse([path for path, _ in fake.posts])

    def test_fork_commit_is_based_on_upstream_and_written_to_the_fork(self):
        trees = deep_tree(["src/a.ts"], {})
        fake = FakeGitData(trees=trees, files={"src/a.ts": "s:src/a.ts"})
        commit(fake, [{"file_path": "src/a.ts", "new_content": "x\n", "expected_file_sha": "s:src/a.ts"}],
               owner="bob", source_owner="acme", source_repo="widgets")
        self.assertTrue(all(path.startswith("/repos/bob/widgets/") for path, _ in fake.posts))
        self.assertFalse(any("/repos/bob/widgets/git/ref/heads/main" in path for path in fake.gets))
        new_commit = next(body for path, body in fake.posts if path.endswith("/git/commits"))
        self.assertEqual(new_commit["parents"], ["upstream-head"])

    def test_modified_file_deleted_upstream_is_a_named_conflict(self):
        fake = FakeGitData(trees=deep_tree(["src/a.ts"], {}), files={})
        with self.assertRaises(GitHubConflictError) as context:
            commit(fake, [{"file_path": "lib/util.py", "new_content": "x\n", "expected_file_sha": "s"}])
        self.assertIn("lib/util.py was deleted or moved upstream", str(context.exception))
        self.assertEqual(fake.posts, [])

    def test_existing_branch_with_the_same_change_is_resumed(self):
        trees = deep_tree(["src/a.ts"], {})
        same = FakeGitData(trees=trees, files={"src/a.ts": "s:src/a.ts"},
                           branch_exists={"sha": "earlier-commit", "tree": "new-tree", "parents": ["upstream-head"]})
        result = commit(same, [{"file_path": "src/a.ts", "new_content": "x\n", "expected_file_sha": "s:src/a.ts"}])
        self.assertEqual((result["commit_sha"], result.get("resumed")), ("earlier-commit", True))
        different = FakeGitData(trees=trees, files={"src/a.ts": "s:src/a.ts"},
                                branch_exists={"sha": "other", "tree": "other-tree", "parents": ["upstream-head"]})
        with self.assertRaisesRegex(GitHubConflictError, "already exists with different content"):
            commit(different, [{"file_path": "src/a.ts", "new_content": "x\n", "expected_file_sha": "s:src/a.ts"}])

    def test_recorded_commit_short_circuits_the_retry(self):
        fake = FakeGitData(trees={}, files={})
        with patch("github.github_client.get_branch_head", AsyncMock(return_value="recorded")):
            result = commit(fake, [{"file_path": "src/a.ts", "new_content": "x\n", "expected_file_sha": "s"}], resume_commit_sha="recorded")
        self.assertEqual((result["commit_sha"], result["resumed"]), ("recorded", True))
        self.assertEqual(fake.posts, [])


# ----------------------------------------------------- EP-05 / PR-3 router


class PushResumeTests(base.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(base.seed_repo(self.store))
        for item in (
            patch.object(settings, "editing_ticket_secret", SECRET),
            patch.object(settings, "github_token_encryption_key", TOKEN_KEY),
            patch("api.github_router.get_turso_store", return_value=self.store),
            patch("api.github_router.get_repo_details", AsyncMock(return_value={"permissions": {"push": True}, "default_branch": "main"})),
        ):
            item.start()
            self.addCleanup(item.stop)
        run(self.store.execute(
            "INSERT INTO user_github_tokens (user_id, github_user_id, github_username, access_token, scope, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [USER, "1", "octocat", encrypt_token("gho_test", TOKEN_KEY), "public_repo", "2026-10-01T00:00:00+00:00"],
        ))

    def request(self):
        files = [PushFileRequest(file_path="src/auth.py", new_content="x = 2\n", file_sha="reviewed")]
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=["src/auth.py"], ttl_seconds=600)
        return PushPRRequest(repo_name=REPO_NAME, files=files, branch_name="codebase-intel/fix", edit_ticket=ticket, idempotency_key="same-key")

    def test_retry_after_failed_pr_creation_resumes_the_recorded_branch(self):
        committed = AsyncMock(return_value={"commit_sha": "c1", "branch_name": "codebase-intel/fix", "owner": "acme", "repo": "widgets"})
        open_pr = AsyncMock(side_effect=[
            GitHubRateLimitError("GitHub rate limit reached while trying to create the pull request.", 120),
            {"pr_url": "https://github.com/acme/widgets/pull/3", "pr_number": 3, "title": "t", "already_existed": True},
        ])
        with patch("api.github_router.create_branch_and_commit", committed), patch("api.github_router.open_pull_request", open_pr):
            with self.assertRaises(HTTPException) as context:
                run(push_pr(self.request(), SimpleNamespace(id=USER)))
            self.assertEqual(context.exception.status_code, 429)
            row = run(self.store.fetch_one("SELECT status, result_json FROM github_change_operations"))
            self.assertEqual(row["status"], "failed")
            self.assertEqual(json.loads(row["result_json"])["progress"]["commit_sha"], "c1")
            result = run(push_pr(self.request(), SimpleNamespace(id=USER)))
        self.assertTrue(result["success"])
        self.assertEqual(committed.await_args_list[1].kwargs["resume_commit_sha"], "c1")
        self.assertEqual(committed.await_args_list[1].kwargs["source_owner"], "acme")

    def test_forbidden_github_write_is_403_not_a_disconnect(self):
        committed = AsyncMock(side_effect=GitHubPermissionError("Your GitHub account does not have permission to create the commit."))
        with patch("api.github_router.create_branch_and_commit", committed):
            with self.assertRaises(HTTPException) as context:
                run(push_pr(self.request(), SimpleNamespace(id=USER)))
        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.headers["X-Error-Code"], "github_permission")


# ------------------------------------------------------------ API-SEC-02


class OAuthBindingTests(base.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        for item in (
            patch.object(settings, "github_client_id", "client"),
            patch.object(settings, "github_client_secret", "secret"),
            patch.object(settings, "github_token_encryption_key", TOKEN_KEY),
            patch.object(settings, "github_frontend_origin", "https://app.example.test"),
            patch("api.github_router.get_turso_store", return_value=self.store),
        ):
            item.start()
            self.addCleanup(item.stop)

    def start_flow(self, user):
        from api.github_router import github_login

        url = run(github_login(redirect_to="/app", current_user=SimpleNamespace(id=user)))["authorization_url"]
        return parse_qs(urlparse(url).query)["state"][0]

    def test_callback_never_links_a_token_and_hands_the_code_to_the_spa(self):
        state = self.start_flow(OTHER)
        exchange = AsyncMock()
        with patch("api.github_router.exchange_code_for_token", exchange):
            response = run(github_callback(code="VICTIM", state=state))
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith("https://app.example.test/github/callback#"))
        fragment = parse_qs(location.split("#", 1)[1])
        self.assertEqual((fragment["code"], fragment["state"]), (["VICTIM"], [state]))
        exchange.assert_not_awaited()
        self.assertIsNone(run(self.store.fetch_one("SELECT user_id FROM user_github_tokens")))

    def test_completion_requires_the_user_who_started_the_flow(self):
        attacker_state = self.start_flow(OTHER)
        exchange = AsyncMock(return_value={"access_token": "gho_victim", "scope": "public_repo"})
        profile = AsyncMock(return_value={"login": "victim", "id": 7})
        with patch("api.github_router.exchange_code_for_token", exchange), patch("api.github_router.get_authenticated_user", profile):
            # The victim's browser completes the attacker's flow: refused.
            with self.assertRaises(HTTPException) as context:
                run(github_complete(CompleteGithubRequest(code="VICTIM", state=attacker_state), current_user=SimpleNamespace(id=USER)))
            self.assertEqual(context.exception.status_code, 403)
            exchange.assert_not_awaited()
            self.assertIsNone(run(self.store.fetch_one("SELECT user_id FROM user_github_tokens")))
            # The rightful user completes their own flow once.
            own_state = self.start_flow(USER)
            result = run(github_complete(CompleteGithubRequest(code="MINE", state=own_state), current_user=SimpleNamespace(id=USER)))
            self.assertEqual(result["github_username"], "victim")
            row = run(self.store.fetch_one("SELECT user_id, github_username FROM user_github_tokens"))
            self.assertEqual((row["user_id"], row["github_username"]), (USER, "victim"))
            with self.assertRaises(HTTPException) as context:
                run(github_complete(CompleteGithubRequest(code="MINE", state=own_state), current_user=SimpleNamespace(id=USER)))
            self.assertEqual(context.exception.status_code, 400)


# ------------------------------------------------------- EP-14 / EXP-6


class ReconstructionTests(unittest.TestCase):
    def test_fragments_of_one_line_are_joined(self):
        line = "    font-weight: bolder; " + "z" * 60
        chunks = [
            {"file_path": "a.css", "start_line": 1, "content": "a {\n" + line[:40]},
            {"file_path": "a.css", "start_line": 2, "content": line[15:] + "\n}"},
        ]
        content, _ = reconstruct_indexed_file(chunks, "a.css")
        self.assertEqual(content, f"a {{\n{line}\n}}\n")
        # Order independent, and a remainder without overlap continues the line.
        content, _ = reconstruct_indexed_file(list(reversed(chunks)), "a.css")
        self.assertEqual(content, f"a {{\n{line}\n}}\n")
        content, _ = reconstruct_indexed_file([
            {"file_path": "b.css", "start_line": 1, "content": "    font-weigh"},
            {"file_path": "b.css", "start_line": 1, "content": "t: bolder;"},
        ], "b.css")
        self.assertEqual(content, "    font-weight: bolder;\n")

    def test_manifest_size_restores_final_newline_and_crlf(self):
        chunks = [{"file_path": "w.py", "start_line": 1, "content": "a\nb"}]
        self.assertEqual(reconstruct_indexed_file(chunks, "w.py", 3), ("a\nb", True))
        self.assertEqual(reconstruct_indexed_file(chunks, "w.py", 4), ("a\nb\n", True))
        self.assertEqual(reconstruct_indexed_file(chunks, "w.py", 6), ("a\r\nb\r\n", True))
        self.assertEqual(reconstruct_indexed_file(chunks, "w.py", 99), ("a\nb\n", False))


# ------------------------------------------------- editor (EP-01/02/03/13)


class EditorFixTests(base.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(base.seed_repo(self.store))
        self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        self.raw: dict = {}

    def seed(self, files):
        run(base.seed_files(self.store, files))
        self.raw.update({path: text.encode() for path, text in files.items()})

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        value = self.raw.get(path)
        if isinstance(value, BaseException):
            raise value
        return value

    def edit(self, question, evidence, responses, *, issue=None):
        request = EditRequest(
            store=self.store, repo=self.repo, user_id=USER, question=question, evidence_chunks=list(evidence),
            evidence_plan=build_evidence_plan(question, "editing"), repo_map_text="",
        )
        model = base.ScriptedModel(responses)
        fetch_issue = AsyncMock(return_value=issue)
        with patch("agent.editor.complete", model), patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", fetch_issue), \
             patch("agent.editor.dependency_file_paths", AsyncMock(return_value=[])), \
             patch("agent.editor.find_symbol_usages", AsyncMock(return_value=[])):
            markdown, tool_calls, proposal = run(run_code_edit(request))
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model, fetch_issue=fetch_issue)

    def test_hex_colours_are_not_issue_references(self):
        self.seed({"src/theme.css": "h1 {\n  color: #292929;\n}\n"})
        fix = base.edit_response([{"file_path": "src/theme.css", "action": "modify", "summary": "Darker",
                                   "changes": [{"old": "color: #292929;", "new": "color: #333;"}]}], title=None,
                                 pr_body="## Summary\nDarker heading.")
        result = self.edit("In src/theme.css change the heading color from #292929 to #333", [base.chunk("src/theme.css", 1, "h1 {")], [fix])
        result.fetch_issue.assert_not_awaited()
        self.assertIsNone(result.proposal["issue_reference"])
        self.assertNotIn("Fixes", result.proposal["pr_body"])
        self.assertEqual(result.proposal["title"], "Update src/theme.css")

    def test_unconfirmed_issue_without_closing_intent_is_only_referenced(self):
        self.seed({"src/auth.py": base.AUTH})
        responses = [
            base.plan_response(files_to_modify=["src/auth.py"], files_to_create=[], symbols_to_trace=[], approach=""),
            base.edit_response([base.LOGOUT_FIX], title=None, pr_body="## Summary\nDone."),
        ]
        result = self.edit("Make logout return False as discussed in issue 12", [base.AUTH_CHUNK], responses, issue=None)
        self.assertEqual(result.proposal["pr_body"], "Refs #12\n\n## Summary\nDone.")
        self.assertFalse(result.proposal["issue_confirmed"])
        self.assertEqual(result.proposal["title"], "Update src/auth.py")

    def test_named_file_that_fits_the_context_is_shown_whole(self):
        lines = [f"export const value{index} = {index};" for index in range(2500)]
        big = "\n".join(lines) + "\n"
        self.assertGreater(len(big), 60_000)
        self.assertLess(len(big), settings.editing_context_characters)
        self.seed({"src/big.ts": big})
        fix = base.edit_response([{"file_path": "src/big.ts", "action": "modify", "summary": "x",
                                   "changes": [{"old": "export const value2490 = 2490;", "new": "export const value2490 = 0;"}]}])
        result = self.edit("In src/big.ts set value2490 to 0", [base.chunk("src/big.ts", 1, lines[0])], [fix])
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("export const value2499 = 2499;", prompt)
        self.assertNotIn("omitted", prompt)
        self.assertEqual(result.proposal["files"][0]["after"], big.replace("value2490 = 2490", "value2490 = 0"))

    def test_large_file_view_shows_the_region_the_request_names(self):
        head = "\n".join(f"const filler{index} = '{'f' * 60}';" for index in range(3000))
        tail = "function RecurringBookings() {\n  const shown = 4;\n  return shown;\n}"
        big = head + "\n" + tail + "\n" + "\n".join(f"const after{index} = 1;" for index in range(50)) + "\n"
        self.assertGreater(len(big), settings.editing_context_characters)
        self.seed({"src/view.tsx": big})
        fix = base.edit_response([{"file_path": "src/view.tsx", "action": "modify", "summary": "5",
                                   "changes": [{"old": "  const shown = 4;", "new": "  const shown = 5;"}]}])
        result = self.edit("In src/view.tsx the RecurringBookings component shows 4 dates; show 5", [base.chunk("src/view.tsx", 1, "const filler0")], [fix])
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("function RecurringBookings() {", prompt)
        self.assertIn("partial view", prompt)
        self.assertEqual(result.proposal["files"][0]["after"], big.replace("const shown = 4;", "const shown = 5;"))

    def test_every_named_file_is_sent_and_unchanged_ones_are_reported(self):
        big = lambda name: "\n".join(f"export const {name}{index} = {index};" for index in range(3500)) + "\n"  # noqa: E731
        files = {"src/a.ts": big("a"), "src/b.ts": big("b"), "src/c.ts": "export const c = 1;\n"}
        self.seed(files)
        only_a = base.edit_response([{"file_path": "src/a.ts", "action": "modify", "summary": "x",
                                      "changes": [{"old": "export const a1 = 1;", "new": "export const a1 = 2;"}]}])
        question = "Update src/a.ts, src/b.ts and src/c.ts to bump the first constant."
        evidence = [base.chunk(path, 1, text.split("\n")[0]) for path, text in files.items()]
        result = self.edit(question, evidence, [only_a, only_a])
        prompt = result.model.calls[0]["messages"][1]["content"]
        for path in files:
            self.assertIn(f"===== FILE: {path}", prompt)
        # The named-but-unchanged files triggered one repair request...
        self.assertIn("does not modify", result.model.calls[1]["messages"][-1]["content"])
        # ...and are reported rather than silently dropped.
        self.assertTrue(result.proposal["incomplete"])
        self.assertEqual(sorted(item["file_path"] for item in result.proposal["unchanged_files"]), ["src/b.ts", "src/c.ts"])
        self.assertIn("## Not changed", result.markdown)
        self.assertIn("not changed: ", result.tool_calls[-1]["result_summary"])

    def test_model_may_explain_why_a_named_file_needs_no_change(self):
        self.seed({"src/auth.py": base.AUTH, "src/session.py": base.SESSION})
        response = base.edit_response([base.LOGOUT_FIX], unchanged_files=[{"file_path": "src/session.py", "reason": "check() is unaffected"}])
        result = self.edit("Change src/auth.py and src/session.py so logout returns False", [base.AUTH_CHUNK, base.SESSION_CHUNK], [response])
        self.assertEqual(len(result.model.calls), 1)
        self.assertFalse(result.proposal["incomplete"])
        self.assertEqual(result.proposal["unchanged_files"], [{"file_path": "src/session.py", "reason": "check() is unaffected"}])

    def test_file_over_the_editor_limit_is_refused_not_rebuilt_from_the_index(self):
        self.seed({"src/huge.py": "x = 1\n"})
        self.raw["src/huge.py"] = GitHubFileTooLargeError("src/huge.py is too large to edit safely in the browser.")
        result = self.edit("Change src/huge.py so x is 2", [base.chunk("src/huge.py", 1, "x = 1")], [])
        self.assertIsNone(result.proposal)
        self.assertIn("larger than the editor limit", result.markdown)
        self.assertEqual(result.model.calls, [])

    def test_view_budgets_share_the_context_without_dropping_files(self):
        from agent.editor import LoadedFile

        files = [LoadedFile("a", "x" * 200_000, None, "github"), LoadedFile("b", "y" * 1_000, None, "github"),
                 LoadedFile("c", "z" * 90_000, None, "github")]
        budgets = CodeEditor._view_budgets(files)
        self.assertEqual(set(budgets), {"a", "b", "c"})
        self.assertEqual(budgets["b"], 1_000)
        self.assertLessEqual(sum(min(budgets[item.path], len(item.content)) for item in files),
                             settings.editing_context_characters + 4_000)


if __name__ == "__main__":
    unittest.main()
