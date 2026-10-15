"""Unit tests for GitHub OAuth and Git Data API integration."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database import TursoStore
from github.github_client import (
    GitHubAPIError,
    GitHubAuthRequiredError,
    create_branch_and_commit,
    get_authorization_url,
    open_pull_request,
)


class GitHubIntegrationTests(unittest.TestCase):
    def test_tokens_are_authenticated_encrypted_at_rest(self):
        from github.token_crypto import decrypt_token, encrypt_token

        # Fernet keys are generated in the test so this assertion also checks
        # that an accidental plaintext value is never accepted as current data.
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        encrypted = encrypt_token("ghp_example", key)
        self.assertTrue(encrypted.startswith("v1:"))
        self.assertEqual(decrypt_token(encrypted, key), "ghp_example")
        with self.assertRaises(Exception):
            decrypt_token("ghp_example", key)

    def test_tokens_accept_a_stable_render_secret(self):
        from github.token_crypto import decrypt_token, encrypt_token

        secret = "render-production-github-token-secret-2026"
        encrypted = encrypt_token("ghp_render_example", secret)
        self.assertTrue(encrypted.startswith("v1:"))
        self.assertEqual(decrypt_token(encrypted, secret), "ghp_render_example")
        with self.assertRaises(Exception):
            decrypt_token(encrypted, secret + "-changed")

    def test_branch_and_file_validation_blocks_traversal_and_overwrite_names(self):
        from github.github_client import GitHubAPIError, validate_branch_name, validate_file_path

        self.assertEqual(validate_branch_name("codebase-intel/fix-auth", require_prefix=True), "codebase-intel/fix-auth")
        with self.assertRaises(GitHubAPIError):
            validate_branch_name("main", require_prefix=True)
        with self.assertRaises(GitHubAPIError):
            validate_file_path("../../secrets.env")

    def test_oauth_state_is_single_use(self):
        from api.github_router import _consume_state, _ensure_github_table
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            async def run():
                await _ensure_github_table(store)
                state = "opaque-state"
                await store.execute(
                    "INSERT INTO github_oauth_states VALUES (?, ?, ?, ?, NULL)",
                    [hashlib.sha256(state.encode()).hexdigest(), "user-1", "/dashboard", "2999-01-01T00:00:00+00:00"],
                )
                first = await _consume_state(store, state)
                second = await _consume_state(store, state)
                return first, second
            first, second = asyncio.run(run())
        self.assertEqual(first["user_id"], "user-1")
        self.assertIsNone(second)
    def test_get_authorization_url_encodes_parameters(self):
        url = get_authorization_url(
            client_id="test-client-id",
            redirect_uri="http://localhost:8000/api/github/callback",
            state="test-state-token",
        )
        self.assertIn("client_id=test-client-id", url)
        self.assertIn("scope=public_repo", url)
        self.assertIn("state=test-state-token", url)
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))

    def test_github_token_table_can_be_created_and_queried(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")

            async def run():
                from api.github_router import _ensure_github_table
                await _ensure_github_table(store)
                await store.execute(
                    "INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ["user-1", "gh-123", "octocat", "ghp_secret_token", "repo", "2026-09-03T00:00:00Z"],
                )
                row = await store.fetch_one(
                    "SELECT github_username, access_token FROM user_github_tokens WHERE user_id = ?",
                    ["user-1"],
                )
                return row

            row = asyncio.run(run())
            self.assertIsNotNone(row)
            self.assertEqual(row["github_username"], "octocat")
            self.assertEqual(row["access_token"], "ghp_secret_token")

    @patch("github.github_client.get_file_at_ref", new_callable=AsyncMock)
    @patch("httpx.AsyncClient.get")
    @patch("httpx.AsyncClient.post")
    def test_create_branch_and_commit_calls_git_data_api(self, mock_post, mock_get, mock_file):
        # 1. Base branch ref
        ref_mock = MagicMock()
        ref_mock.status_code = 200
        ref_mock.json.return_value = {"object": {"sha": "base_commit_123"}}

        # 2. Base commit
        commit_mock = MagicMock()
        commit_mock.status_code = 200
        commit_mock.json.return_value = {"tree": {"sha": "base_tree_456"}}

        # 2b. Directory listings that resolve the existing file's Git mode.
        root_tree = MagicMock(status_code=200)
        root_tree.json.return_value = {"tree": [{"path": "src", "type": "tree", "mode": "040000", "sha": "src_tree"}]}
        src_tree = MagicMock(status_code=200)
        src_tree.json.return_value = {"tree": [{"path": "app.py", "type": "blob", "mode": "100644", "sha": "file_sha"}]}

        mock_get.side_effect = [ref_mock, commit_mock, root_tree, src_tree]
        mock_file.return_value = {"sha": "file_sha", "content": "old", "size": 3}

        # 3. Create blob
        blob_mock = MagicMock()
        blob_mock.status_code = 201
        blob_mock.json.return_value = {"sha": "blob_789"}

        # 4. Create tree
        tree_mock = MagicMock()
        tree_mock.status_code = 201
        tree_mock.json.return_value = {"sha": "tree_abc"}

        # 5. Create commit
        new_commit_mock = MagicMock()
        new_commit_mock.status_code = 201
        new_commit_mock.json.return_value = {"sha": "new_commit_def"}

        # 6. Create ref
        new_ref_mock = MagicMock()
        new_ref_mock.status_code = 201
        new_ref_mock.json.return_value = {"ref": "refs/heads/patch-1"}

        mock_post.side_effect = [blob_mock, tree_mock, new_commit_mock, new_ref_mock]

        result = asyncio.run(
            create_branch_and_commit(
                access_token="fake-token",
                owner="octocat",
                repo="hello-world",
                base_branch="main",
                branch_name="codebase-intel/patch-1",
                file_path="src/app.py",
                new_content="print('hello')",
                commit_message="Fix app.py",
            )
        )
        self.assertEqual(result["commit_sha"], "new_commit_def")
        self.assertEqual(result["branch_name"], "codebase-intel/patch-1")

    @patch("httpx.AsyncClient.post")
    @patch("httpx.AsyncClient.get")
    def test_create_branch_and_commit_files_creates_one_atomic_tree(self, mock_get, mock_post):
        from github.github_client import create_branch_and_commit_files

        ref_mock = MagicMock(status_code=200)
        ref_mock.json.return_value = {"object": {"sha": "base_commit"}}
        commit_mock = MagicMock(status_code=200)
        commit_mock.json.return_value = {"tree": {"sha": "base_tree"}}
        root_tree = MagicMock(status_code=200)
        root_tree.json.return_value = {"tree": [
            {"path": "src", "type": "tree", "mode": "040000", "sha": "src_tree"},
            {"path": "tests", "type": "tree", "mode": "040000", "sha": "tests_tree"},
        ]}
        src_tree = MagicMock(status_code=200)
        src_tree.json.return_value = {"tree": [{"path": "auth.py", "type": "blob", "mode": "100644", "sha": "a"}]}
        tests_tree = MagicMock(status_code=200)
        tests_tree.json.return_value = {"tree": [{"path": "test_auth.py", "type": "blob", "mode": "100644", "sha": "b"}]}
        mock_get.side_effect = [ref_mock, commit_mock, root_tree, src_tree, tests_tree]

        blob_one = MagicMock(status_code=201)
        blob_one.json.return_value = {"sha": "blob_one"}
        blob_two = MagicMock(status_code=201)
        blob_two.json.return_value = {"sha": "blob_two"}
        tree_mock = MagicMock(status_code=201)
        tree_mock.json.return_value = {"sha": "new_tree"}
        commit_result = MagicMock(status_code=201)
        commit_result.json.return_value = {"sha": "new_commit"}
        ref_result = MagicMock(status_code=201)
        ref_result.json.return_value = {"ref": "refs/heads/codebase-intel/patch"}
        mock_post.side_effect = [blob_one, blob_two, tree_mock, commit_result, ref_result]

        result = asyncio.run(create_branch_and_commit_files(
            "fake-token", "octocat", "hello-world", "main", "codebase-intel/patch",
            [
                {"file_path": "src/auth.py", "new_content": "return True"},
                {"file_path": "tests/test_auth.py", "new_content": "assert True"},
            ],
            "Fix authentication",
        ))
        self.assertEqual(result["commit_sha"], "new_commit")
        tree_payload = mock_post.call_args_list[2].kwargs["json"]
        self.assertEqual([item["path"] for item in tree_payload["tree"]], ["src/auth.py", "tests/test_auth.py"])

    @patch("httpx.AsyncClient.post")
    def test_open_pull_request_returns_pr_metadata(self, mock_post):
        pr_mock = MagicMock()
        pr_mock.status_code = 201
        pr_mock.json.return_value = {
            "html_url": "https://github.com/octocat/hello-world/pull/42",
            "number": 42,
            "title": "Codebase Intelligence Fix",
        }
        mock_post.return_value = pr_mock

        res = asyncio.run(
            open_pull_request(
                access_token="fake-token",
                base_owner="octocat",
                base_repo="hello-world",
                head="patch-1",
                base_branch="main",
                title="Codebase Intelligence Fix",
                body="Automated PR",
            )
        )
        self.assertEqual(res["pr_url"], "https://github.com/octocat/hello-world/pull/42")
        self.assertEqual(res["pr_number"], 42)
        self.assertFalse(res["already_existed"])

    def test_router_status_unconnected(self):
        from types import SimpleNamespace
        from api.github_router import github_status

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-empty")
            with patch("api.github_router.get_turso_store", return_value=store):
                res = asyncio.run(github_status(user))
            self.assertEqual(res, {"connected": False, "github_username": None})

    def test_router_status_connected(self):
        from types import SimpleNamespace
        from api.github_router import github_status, _ensure_github_table

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-connected")

            async def setup_and_call():
                await _ensure_github_table(store)
                await store.execute(
                    "INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ["user-connected", "123", "hubuser", "token_abc", "repo", "2026-09-03T12:00:00Z"],
                )
                return await github_status(user)

            with patch("api.github_router.get_turso_store", return_value=store):
                res = asyncio.run(setup_and_call())
            self.assertTrue(res["connected"])
            self.assertEqual(res["github_username"], "hubuser")

    def test_router_login_generates_auth_url(self):
        from types import SimpleNamespace
        from api.github_router import github_login
        from config import settings

        user = SimpleNamespace(id="user-test")
        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            with patch.object(settings, "github_client_id", "client-123"), \
                 patch.object(settings, "github_client_secret", "secret"), \
                 patch.object(settings, "github_redirect_uri", "http://test/callback"), \
                 patch("api.github_router.get_turso_store", return_value=store):
                res = asyncio.run(github_login(redirect_to="/dashboard", current_user=user))
        self.assertIn("client_id=client-123", res["authorization_url"])
        self.assertIn("scope=public_repo", res["authorization_url"])

    def test_router_disconnect_deletes_token(self):
        from types import SimpleNamespace
        from api.github_router import github_disconnect, _ensure_github_table

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-del")

            async def run():
                await _ensure_github_table(store)
                await store.execute(
                    "INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ["user-del", "123", "hubuser", "token_abc", "repo", "2026-09-03T12:00:00Z"],
                )
                await github_disconnect(user)
                return await store.fetch_one("SELECT * FROM user_github_tokens WHERE user_id = ?", ["user-del"])

            with patch("api.github_router.get_turso_store", return_value=store):
                remaining = asyncio.run(run())
            self.assertIsNone(remaining)

    def test_router_push_pr_unconnected_raises_401(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from api.github_router import PushPRRequest, push_pr

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-no-token")
            req = PushPRRequest(repo_name="demo-repo", file_path="main.py", new_content="x = 1")
            with patch("api.github_router.get_turso_store", return_value=store):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(push_pr(req, user))
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("GitHub account not connected", ctx.exception.detail)

    def test_router_push_pr_unknown_repo_raises_404(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from api.github_router import PushPRRequest, push_pr, _ensure_github_table

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-with-token")

            async def run():
                await _ensure_github_table(store)
                await store.execute(
                    "CREATE TABLE IF NOT EXISTS repos (id TEXT PRIMARY KEY, user_id TEXT, repo_name TEXT, github_url TEXT)"
                )
                await store.execute(
                    "INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ["user-with-token", "123", "hubuser", "token_abc", "repo", "2026-09-03T12:00:00Z"],
                )
                req = PushPRRequest(repo_name="nonexistent-repo", file_path="main.py", new_content="x = 1", file_sha="file-sha")
                return await push_pr(req, user)

            with patch("api.github_router.get_turso_store", return_value=store):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(run())
            self.assertEqual(ctx.exception.status_code, 404)

    def test_router_push_pr_requires_editing_ticket_even_when_github_is_connected(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from api.github_router import PushPRRequest, push_pr, _ensure_github_table
        from config import settings

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-ticket")

            async def run():
                await _ensure_github_table(store)
                await store.execute("CREATE TABLE IF NOT EXISTS repos (id TEXT PRIMARY KEY, user_id TEXT, repo_name TEXT, github_url TEXT)")
                await store.execute("INSERT INTO repos VALUES (?, ?, ?, ?)", ["repo-1", "user-ticket", "cool-repo", "https://github.com/owner-org/cool-repo"])
                await store.execute("INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)", ["user-ticket", "123", "owner-org", "token_abc", "repo", "2026-09-03T12:00:00Z"])
                return await push_pr(PushPRRequest(repo_name="cool-repo", file_path="src/main.py", new_content="x = 1", file_sha="file-sha"), user)

            with patch("api.github_router.get_turso_store", return_value=store), \
                 patch.object(settings, "editing_ticket_secret", "test-editing-secret"):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(run())
            self.assertEqual(ctx.exception.status_code, 403)

    @patch("api.github_router.get_repo_details")
    @patch("api.github_router.create_branch_and_commit")
    @patch("api.github_router.open_pull_request")
    def test_router_push_pr_direct_push_success(self, mock_pr, mock_commit, mock_repo):
        from types import SimpleNamespace
        from agent.code_edit import create_edit_ticket
        from api.github_router import PushPRRequest, push_pr, _ensure_github_table
        from config import settings

        mock_repo.return_value = {
            "permissions": {"push": True, "admin": True},
            "default_branch": "main",
        }
        mock_commit.return_value = {
            "commit_sha": "abc12345",
            "branch_name": "feature-fix",
            "owner": "owner-org",
            "repo": "cool-repo",
        }
        mock_pr.return_value = {
            "pr_url": "https://github.com/owner-org/cool-repo/pull/1",
            "pr_number": 1,
            "title": "Fix bug",
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-push")

            async def run():
                await _ensure_github_table(store)
                await store.execute(
                    "CREATE TABLE IF NOT EXISTS repos (id TEXT PRIMARY KEY, user_id TEXT, repo_name TEXT, github_url TEXT)"
                )
                await store.execute(
                    "INSERT INTO repos VALUES (?, ?, ?, ?)",
                    ["repo-1", "user-push", "cool-repo", "https://github.com/owner-org/cool-repo"],
                )
                await store.execute(
                    "INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ["user-push", "123", "owner-org", "token_abc", "repo", "2026-09-03T12:00:00Z"],
                )
                req = PushPRRequest(
                    repo_name="cool-repo",
                    file_path="src/main.py",
                    new_content="print('updated')",
                    branch_name="codebase-intel/feature-fix",
                    pr_title="Fix bug",
                    file_sha="file-sha",
                    edit_ticket=create_edit_ticket(
                        "test-editing-secret",
                        user_id="user-push",
                        repo_name="cool-repo",
                        file_path="src/main.py",
                        ttl_seconds=600,
                    ),
                )
                return await push_pr(req, user)

            with patch("api.github_router.get_turso_store", return_value=store), \
                 patch.object(settings, "editing_ticket_secret", "test-editing-secret"):
                result = asyncio.run(run())

            self.assertTrue(result["success"])
            self.assertEqual(result["pr_url"], "https://github.com/owner-org/cool-repo/pull/1")
            self.assertEqual(result["pr_number"], 1)
            self.assertFalse(result["is_fork"])

    @patch("api.github_router.get_repo_details")
    @patch("api.github_router.create_branch_and_commit_files")
    @patch("api.github_router.open_pull_request")
    def test_router_push_pr_multi_file_uses_atomic_commit(self, mock_pr, mock_commit, mock_repo):
        from types import SimpleNamespace
        from agent.code_edit import create_edit_ticket
        from api.github_router import PushFileRequest, PushPRRequest, _ensure_github_table, push_pr
        from config import settings

        mock_repo.return_value = {"permissions": {"push": True}, "default_branch": "main"}
        mock_commit.return_value = {"commit_sha": "abc", "branch_name": "codebase-intel/issue-1428", "owner": "owner-org", "repo": "cool-repo"}
        mock_pr.return_value = {"pr_url": "https://github.com/owner-org/cool-repo/pull/2", "pr_number": 2, "title": "Fix issue 1428"}

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-token")
            user = SimpleNamespace(id="user-multi")

            async def run():
                await _ensure_github_table(store)
                await store.execute("CREATE TABLE IF NOT EXISTS repos (id TEXT PRIMARY KEY, user_id TEXT, repo_name TEXT, github_url TEXT)")
                await store.execute("INSERT INTO repos VALUES (?, ?, ?, ?)", ["repo-1", "user-multi", "cool-repo", "https://github.com/owner-org/cool-repo"])
                await store.execute("INSERT INTO user_github_tokens VALUES (?, ?, ?, ?, ?, ?)", ["user-multi", "123", "hubuser", "token_abc", "repo", "2026-09-03T12:00:00Z"])
                ticket = create_edit_ticket("test-editing-secret", user_id="user-multi", repo_name="cool-repo", file_path=["src/auth.py", "tests/test_auth.py"], ttl_seconds=600)
                request = PushPRRequest(
                    repo_name="cool-repo",
                    files=[
                        PushFileRequest(file_path="src/auth.py", new_content="return True", file_sha="sha-auth"),
                        PushFileRequest(file_path="tests/test_auth.py", new_content="assert True", file_sha="sha-test"),
                    ],
                    branch_name="codebase-intel/issue-1428",
                    pr_title="Fix issue 1428",
                    edit_ticket=ticket,
                    idempotency_key="multi-change",
                )
                return await push_pr(request, user)

            with patch("api.github_router.get_turso_store", return_value=store), patch.object(settings, "editing_ticket_secret", "test-editing-secret"):
                result = asyncio.run(run())
            self.assertTrue(result["success"])
            mock_commit.assert_awaited_once()
            self.assertEqual(len(mock_commit.await_args.args[5]), 2)


if __name__ == "__main__":
    unittest.main()
