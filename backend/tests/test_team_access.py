"""Sharing a repository with teammates (FEAT-06): access is enforced on the server.

* Non-members get 404 (never data) on every repository route, including
  routes that take the repository name in the query string or body.
* Viewers can ask, search and browse, but cannot create edits, push, re-index,
  stop indexing, rename, delete or manage sharing (403).
* Editors can create edit proposals and push, but cannot manage (403).
* The owner can do everything; removal, role changes and revocation take
  effect on the very next request; expired and revoked links are refused.
* A member who already owns a repository with the same name gets an alias.
* Deleting a repository removes its memberships and invite links.
* Conversations stay private per user; members use the owner's index.

Every test is offline: a migrated temporary libSQL database, the real FastAPI
routes, and patched model/GitHub providers.
"""

import test_000_environment  # noqa: F401  (isolate from real services first)

import asyncio
import itertools
import logging
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from agent.code_edit import create_edit_ticket
from api.auth import AuthenticatedUser, get_current_user
from api.repository_state import unique_alias
from api.sharing_router import hash_token, redact_link_tokens
from config import UserRequestLimiter, settings
from database import TursoStore, apply_migrations

run = asyncio.run

SECRET = "test-editing-secret-team-access-0123"
OWNER = "owner-ann"
VIEWER = "viewer-bo"
EDITOR = "editor-cy"
STRANGER = "stranger-di"
REPO_ID = "repo-ky"
REPO = "ky-http"
GITHUB_URL = "https://github.com/sindresorhus/ky.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-10-01T00:00:00+00:00"
SOURCE = "export const retry = (request) => {\n  return request.retry ?? 2;\n};\n"
CHUNK = {
    "id": f"{REPO_ID}:source/retry.ts:1", "repo_id": REPO_ID, "file_path": "source/retry.ts", "start_line": 1,
    "end_line": 3, "language": "typescript", "symbols": ["retry"], "content": SOURCE.rstrip("\n"),
    "_relevance_score": 0.9, "_retrieval_methods": ["keyword"], "_retrieval_reasons": ["Keyword match"],
}
ANSWER = "Retries are configured in `source/retry.ts:L1-L3`."


def proposal(proposal_id="7a1c2b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b"):
    return {
        "id": proposal_id, "title": "Retry three times", "summary": "Raise the default.", "pr_body": "## Summary\nRetry 3x.",
        "validation": [], "issue_reference": None, "base_ref": "HEAD", "indexed_commit": COMMIT,
        "files": [{
            "file_path": "source/retry.ts", "action": "modify", "summary": "Default to three",
            "changes": [{"old": "?? 2", "new": "?? 3", "reason": ""}],
            "before": SOURCE, "after": SOURCE.replace("?? 2", "?? 3"), "base_blob_sha": "abc",
            "additions": 1, "deletions": 1, "language": "typescript", "source": "github",
        }],
        "stats": {"files": 1, "additions": 1, "deletions": 1},
        "file_path": "source/retry.ts",
        "changes": [{"old": "?? 2", "new": "?? 3", "reason": ""}],
    }


async def seed_repo(store, *, repo_id=REPO_ID, user_id=OWNER, repo_name=REPO, chunks=True):
    await store.execute(
        "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'ready', ?, ?, ?)",
        [repo_id, user_id, repo_name, GITHUB_URL, 1 if chunks else 0, NOW, NOW],
    )
    await store.execute(
        "INSERT INTO repo_snapshots (repo_id, commit_sha, source, selection_json, fetched_at) VALUES (?, ?, 'archive', ?, ?)",
        [repo_id, COMMIT, '{"selected_bytes": 4096}', NOW],
    )
    if chunks:
        row = {key: value for key, value in CHUNK.items() if not key.startswith("_")}
        await store.insert_chunks([{**row, "id": f"{repo_id}:source/retry.ts:1", "repo_id": repo_id, "embedding": None}])
        await store.execute(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, 'h', 4096, ?)",
            [repo_id, "source/retry.ts", NOW],
        )


async def streamed_answer(*_args, **_kwargs):
    yield ANSWER


def local_user(request: Request) -> AuthenticatedUser:
    """The test identity is the bearer value, like scripts/dev_server.py's `local:<id>`."""
    user_id = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or "anonymous"
    return AuthenticatedUser(id=user_id, access_token="test", raw_user={"email": f"{user_id}@example.test"})


class TeamAccessCase(unittest.TestCase):
    def setUp(self):
        from main import app

        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'team.db'}", "local-test-token")
        run(apply_migrations(self.store))
        run(seed_repo(self.store))
        app.dependency_overrides[get_current_user] = local_user
        self.addCleanup(app.dependency_overrides.pop, get_current_user, None)
        patches = [
            patch(f"api.{module}.get_turso_store", return_value=self.store)
            for module in ("query_router", "repos_router", "explore_router", "github_router", "sharing_router", "billing_router")
        ] + [
            patch(f"api.{module}.assert_turso_schema", new=AsyncMock())
            for module in ("query_router", "repos_router", "explore_router", "sharing_router")
        ] + [
            patch("api.query_router.query_request_limiter", UserRequestLimiter(100, 1)),
            patch("api.query_router.build_repo_map", new=AsyncMock(return_value={"text": ""})),
            patch("api.query_router.retrieve_context", new=AsyncMock(return_value=[dict(CHUNK)])),
            patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=(ANSWER, []))),
            patch("api.query_router.stream_agent_answer", new=streamed_answer),
            # The first proposal has a known id; later ones get fresh ids.
            patch("api.query_router.run_code_edit", new=AsyncMock(side_effect=(
                ("A change is ready.", [], proposal(proposal_id)) for proposal_id in itertools.chain(
                    [proposal()["id"]], (str(uuid.uuid4()) for _ in itertools.count()),
                )
            ))),
            patch("api.explore_router.fetch_raw_file", new=AsyncMock(return_value=None)),
            patch("api.github_router._load_token", new=AsyncMock(return_value=("gh-token", "hubuser"))),
            patch("api.github_router.fetch_raw_file", new=AsyncMock(return_value=SOURCE.encode())),
            patch.object(settings, "editing_ticket_secret", SECRET),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(app)

    def tearDown(self):
        run(self.store.close())
        self._tmp.cleanup()

    # -------------------------------------------------------------- helpers

    def call(self, user, method, path, **kwargs):
        return self.client.request(method, f"/api{path}", headers={"Authorization": f"Bearer {user}"}, **kwargs)

    def invite(self, role="viewer", user=OWNER, repo=REPO):
        response = self.call(user, "POST", f"/repos/{repo}/invites", json={"role": role, "expires_in_days": 7})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def join(self, user, role="viewer"):
        token = self.invite(role)["token"]
        response = self.call(user, "POST", f"/invites/{token}/accept")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ticket(self, user, repo_name):
        return create_edit_ticket(SECRET, user_id=user, repo_name=repo_name, file_path="source/retry.ts", ttl_seconds=600)

    def rows(self, sql, args=()):
        return run(self.store.fetch_all(sql, list(args)))


# Every route that takes a repository name, and how to call it. The guard
# test below fails when a new repository route is added without being listed
# here, so each new route gets an access decision.
# A well-formed conversation id; the repository check comes before any conversation lookup.
THREAD = "c" * 32


def repo_routes(name, ticket=""):
    return [
        ("GET", f"/status/{name}", {}),
        ("GET", f"/repos/{name}/coverage", {}),
        ("GET", f"/repos/{name}/impact", {"params": {"file_path": "source/retry.ts"}}),
        ("GET", f"/repos/{name}/overview", {}),
        ("GET", f"/repos/{name}/search", {"params": {"q": "retry"}}),
        ("GET", f"/repos/{name}/file", {"params": {"path": "source/retry.ts"}}),
        ("GET", f"/repos/{name}/files", {"params": {"q": "retry"}}),
        ("GET", f"/repos/{name}/symbols", {"params": {"q": "retry"}}),
        ("GET", f"/repos/{name}/impact/suggestions", {}),
        ("GET", f"/conversations/{name}", {}),
        ("DELETE", f"/conversations/{name}", {}),
        ("GET", f"/conversations/{name}/threads", {}),
        ("PATCH", f"/conversations/{name}/threads/{THREAD}", {"json": {"title": "x"}}),
        ("DELETE", f"/conversations/{name}/threads/{THREAD}", {}),
        ("POST", f"/conversations/{name}/threads/{THREAD}/restore", {}),
        ("POST", "/query", {"json": {"repo_name": name, "question": "Where are retries configured?"}}),
        ("POST", "/query/stream", {"json": {"repo_name": name, "question": "Where are retries configured?"}}),
        ("PATCH", f"/repos/{name}", {"json": {"repo_name": "renamed"}}),
        ("POST", f"/repos/{name}/reindex", {}),
        ("POST", f"/repos/{name}/cancel-indexing", {}),
        ("DELETE", f"/repos/{name}", {}),
        ("GET", f"/repos/{name}/sharing", {}),
        ("POST", f"/repos/{name}/invites", {"json": {"role": "viewer"}}),
        ("DELETE", f"/repos/{name}/invites/some-invite", {}),
        ("PATCH", f"/repos/{name}/members/{VIEWER}", {"json": {"role": "editor"}}),
        ("DELETE", f"/repos/{name}/members/{VIEWER}", {}),
        ("DELETE", f"/repos/{name}/membership", {}),
        ("GET", "/github/file", {"params": {"repo_name": name, "file_path": "source/retry.ts"},
                                 "headers_extra": {"X-Editing-Ticket": ticket}}),
        ("POST", "/github/push-pr", {"json": {"repo_name": name, "file_path": "source/retry.ts", "new_content": "x",
                                              "file_sha": "abc", "edit_ticket": ticket or "missing"}}),
        ("GET", "/github/pr-status", {"params": {"repo_name": name, "pr_number": 1}}),
        ("POST", "/answers/share", {"json": {"repo_name": name, "answer": ANSWER}}),
    ]


def routes_naming_a_repository(spec):
    """(METHOD, path without /api) of every route that takes a repository name:
    in the path, the query string, a header or anywhere in the request body."""
    schemas = spec.get("components", {}).get("schemas", {})

    def has_repo_name(schema, seen=frozenset()):
        if not isinstance(schema, dict):
            return False
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return name not in seen and has_repo_name(schemas.get(name), seen | {name})
        properties = schema.get("properties") or {}
        if "repo_name" in properties:
            return True
        nested = [*properties.values(), schema.get("items"), schema.get("additionalProperties")]
        nested += [item for key in ("allOf", "anyOf", "oneOf") for item in schema.get(key, [])]
        return any(has_repo_name(item, seen) for item in nested)

    found = set()
    for path, operations in spec["paths"].items():
        short = path.removeprefix("/api")
        for method, operation in operations.items():
            body = (operation.get("requestBody") or {}).get("content", {})
            if (
                "{repo_name}" in short
                or any(parameter.get("name") == "repo_name" for parameter in operation.get("parameters", []))
                or any(has_repo_name(content.get("schema")) for content in body.values())
            ):
                found.add((method.upper(), short))
    return found


class RouteCoverageTests(unittest.TestCase):
    def test_every_repository_route_has_an_access_decision(self):
        from main import app

        listed = {(method, path) for method, path, _ in repo_routes("{repo_name}")}
        listed = {(method, path.replace("some-invite", "{invite_id}").replace(VIEWER, "{member_id}").replace(THREAD, "{conversation_id}")) for method, path in listed}
        found = routes_naming_a_repository(app.openapi())
        # Routes naming the repository in the body or query string are found too.
        self.assertLessEqual(
            {("POST", "/query"), ("POST", "/query/stream"), ("POST", "/github/push-pr"), ("POST", "/answers/share"),
             ("GET", "/github/file"), ("GET", "/github/pr-status")},
            found,
        )
        for method, path in sorted(found):
            self.assertIn((method, path), listed, f"{method} {path} takes a repository name but has no access test")

    def test_the_guard_finds_a_repository_name_in_the_path_query_or_body(self):
        from fastapi import Body, FastAPI
        from pydantic import BaseModel

        class Thread(BaseModel):
            repo_name: str
            title: str = ""

        class Wrapped(BaseModel):
            threads: list[Thread]

        class Other(BaseModel):
            github_url: str

        probe = FastAPI()

        @probe.post("/api/threads")
        async def create_thread(payload: Thread):
            return payload

        @probe.post("/api/threads/many")
        async def create_threads(payload: Wrapped):
            return payload

        @probe.post("/api/threads/fields")
        async def create_from_fields(repo_name: str = Body(...), title: str = Body("")):
            return repo_name

        @probe.get("/api/threads")
        async def list_threads(repo_name: str):
            return repo_name

        @probe.get("/api/r/{repo_name}/threads")
        async def repo_threads(repo_name: str):
            return repo_name

        @probe.post("/api/other")
        async def other(payload: Other):
            return payload

        self.assertEqual(routes_naming_a_repository(probe.openapi()), {
            ("POST", "/threads"), ("POST", "/threads/many"), ("POST", "/threads/fields"),
            ("GET", "/threads"), ("GET", "/r/{repo_name}/threads"),
        })


class NonMemberTests(TeamAccessCase):
    def test_non_member_gets_404_and_no_data_on_every_repository_route(self):
        self.join(VIEWER)  # someone else being a member changes nothing for a stranger
        ticket = self.ticket(STRANGER, REPO)
        for method, path, options in repo_routes(REPO, ticket):
            options = dict(options)
            headers = {"Authorization": f"Bearer {STRANGER}", **options.pop("headers_extra", {})}
            with self.subTest(route=f"{method} {path}"):
                response = self.client.request(method, f"/api{path}", headers=headers, **options)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["detail"], "Repository not found.")
                self.assertNotIn(GITHUB_URL.removesuffix(".git"), response.text)
                self.assertNotIn("retry ??", response.text)
        self.assertEqual(self.rows("SELECT id FROM repos WHERE id = ?", [REPO_ID]), [{"id": REPO_ID}])
        self.assertEqual(self.rows("SELECT * FROM repo_invites WHERE created_by = ?", [STRANGER]), [])

    def test_repository_list_never_includes_other_peoples_repositories(self):
        self.assertEqual(self.call(STRANGER, "GET", "/repos").json(), [])
        self.assertEqual(self.call(STRANGER, "GET", "/repos/statuses").json(), [])

    def test_an_unknown_name_is_404_even_without_the_sharing_tables(self):
        run(self.store.execute("DROP TABLE repo_members"))
        response = self.call(STRANGER, "GET", f"/status/{REPO}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.call(OWNER, "GET", f"/status/{REPO}").status_code, 200)
        self.assertEqual([repo["repo_name"] for repo in self.call(OWNER, "GET", "/repos").json()], [REPO])


class ViewerTests(TeamAccessCase):
    def setUp(self):
        super().setUp()
        self.joined = self.join(VIEWER, "viewer")

    def test_viewer_sees_the_shared_repository_without_re_indexing(self):
        self.assertEqual(self.joined, {"repo_name": REPO, "role": "viewer", "joined": True})
        listed = self.call(VIEWER, "GET", "/repos").json()
        self.assertEqual(len(listed), 1)
        shared = listed[0]
        self.assertEqual(shared["id"], REPO_ID)
        self.assertEqual(shared["repo_name"], REPO)
        self.assertEqual(shared["access_role"], "viewer")
        self.assertTrue(shared["shared"])
        self.assertEqual(shared["owner_label"], f"{OWNER}@example.test")
        self.assertTrue(shared["queryable"])
        self.assertFalse(shared["capabilities"]["propose_changes"])
        self.assertTrue(shared["capabilities"]["ask"])
        # Nothing was copied or queued for the member.
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM repos"), [{"n": 1}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM ingestion_jobs"), [{"n": 0}])

    def test_viewer_can_ask_search_and_browse(self):
        answer = self.call(VIEWER, "POST", "/query", json={"repo_name": REPO, "question": "Where are retries configured?"})
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertEqual(body["answer"], ANSWER)
        self.assertEqual(body["citations"][0]["file_path"], "source/retry.ts")
        self.assertIn(COMMIT, body["citations"][0]["github_url"])
        streamed = self.call(VIEWER, "POST", "/query/stream", json={"repo_name": REPO, "question": "Where are retries?"})
        self.assertEqual(streamed.status_code, 200)
        self.assertIn("event: done", streamed.text)
        for path, options in (
            (f"/repos/{REPO}/overview", {}),
            (f"/repos/{REPO}/search", {"params": {"q": "retry"}}),
            (f"/repos/{REPO}/file", {"params": {"path": "source/retry.ts"}}),
            (f"/repos/{REPO}/coverage", {}),
            (f"/repos/{REPO}/impact", {"params": {"file_path": "source/retry.ts"}}),
            (f"/status/{REPO}", {}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.call(VIEWER, "GET", path, **options).status_code, 200)
        status = self.call(VIEWER, "GET", f"/status/{REPO}").json()
        self.assertEqual(status["access_role"], "viewer")
        self.assertNotIn("staging_prefix", status)

    def test_viewer_cannot_edit_push_or_manage(self):
        refused = [
            ("POST", "/query", {"json": {"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"}}),
            ("POST", "/query/stream", {"json": {"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"}}),
            ("PATCH", f"/repos/{REPO}", {"json": {"repo_name": "mine-now"}}),
            ("POST", f"/repos/{REPO}/reindex", {}),
            ("POST", f"/repos/{REPO}/cancel-indexing", {}),
            ("DELETE", f"/repos/{REPO}", {}),
            ("GET", f"/repos/{REPO}/sharing", {}),
            ("POST", f"/repos/{REPO}/invites", {"json": {"role": "editor"}}),
            ("PATCH", f"/repos/{REPO}/members/{VIEWER}", {"json": {"role": "editor"}}),
            ("DELETE", f"/repos/{REPO}/members/{VIEWER}", {}),
            ("POST", "/github/push-pr", {"json": {"repo_name": REPO, "file_path": "source/retry.ts", "new_content": "x",
                                                  "file_sha": "abc", "edit_ticket": self.ticket(VIEWER, REPO)}}),
        ]
        for method, path, options in refused:
            with self.subTest(route=f"{method} {path}"):
                response = self.call(VIEWER, method, path, **options)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.headers.get("x-error-code"), "insufficient_role")
        file_response = self.client.get(
            "/api/github/file", params={"repo_name": REPO, "file_path": "source/retry.ts"},
            headers={"Authorization": f"Bearer {VIEWER}", "X-Editing-Ticket": self.ticket(VIEWER, REPO)},
        )
        self.assertEqual(file_response.status_code, 403)
        edit = self.call(VIEWER, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"})
        self.assertIn("Edit & PR", edit.json()["detail"])
        self.assertIn(f"{OWNER}@example.test", edit.json()["detail"])
        # Nothing changed and no proposal was produced.
        self.assertEqual(self.rows("SELECT repo_name FROM repos WHERE id = ?", [REPO_ID]), [{"repo_name": REPO}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM edit_proposals"), [{"n": 0}])
        self.assertEqual(self.rows("SELECT role FROM repo_members WHERE user_id = ?", [VIEWER]), [{"role": "viewer"}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM ingestion_jobs"), [{"n": 0}])

    def test_conversations_stay_private_per_user(self):
        self.call(VIEWER, "POST", "/query", json={"repo_name": REPO, "question": "Viewer question?"})
        self.call(OWNER, "POST", "/query", json={"repo_name": REPO, "question": "Owner question?"})
        viewer_history = [m["content"] for m in self.call(VIEWER, "GET", f"/conversations/{REPO}").json()["messages"]]
        owner_history = [m["content"] for m in self.call(OWNER, "GET", f"/conversations/{REPO}").json()["messages"]]
        self.assertEqual(viewer_history, ["Viewer question?", ANSWER])
        self.assertEqual(owner_history, ["Owner question?", ANSWER])
        # Clearing your conversation leaves the other person's alone.
        self.assertEqual(self.call(VIEWER, "DELETE", f"/conversations/{REPO}").status_code, 200)
        self.assertEqual(len(self.call(OWNER, "GET", f"/conversations/{REPO}").json()["messages"]), 2)

    def test_shared_repository_does_not_use_the_members_quota(self):
        usage = self.call(VIEWER, "GET", "/account/usage")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(usage.json()["used_bytes"], 0)
        self.assertGreater(self.call(OWNER, "GET", "/account/usage").json()["used_bytes"], 0)

    def test_owner_sees_member_count_and_members(self):
        owned = self.call(OWNER, "GET", "/repos").json()[0]
        self.assertEqual(owned["access_role"], "owner")
        self.assertFalse(owned["shared"])
        self.assertEqual(owned["member_count"], 1)
        sharing = self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()
        self.assertEqual(sharing["full_name"], "sindresorhus/ky")
        self.assertEqual([(m["user_id"], m["label"], m["role"]) for m in sharing["members"]],
                         [(VIEWER, f"{VIEWER}@example.test", "viewer")])
        self.assertEqual(sharing["invites"][0]["accepted_count"], 1)


class EditorTests(TeamAccessCase):
    def setUp(self):
        super().setUp()
        self.join(EDITOR, "editor")

    def test_editor_creates_a_private_edit_proposal_and_can_push(self):
        response = self.call(EDITOR, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["edit_suggestion"]["id"], proposal()["id"])
        self.assertTrue(body["edit_ticket"])
        stored = self.rows("SELECT user_id, repo_id, repo_name FROM edit_proposals")
        self.assertEqual(stored, [{"user_id": EDITOR, "repo_id": REPO_ID, "repo_name": REPO}])
        reopened = self.call(EDITOR, "GET", f"/edits/{proposal()['id']}").json()
        self.assertEqual(reopened["repo_name"], REPO)
        self.assertTrue(reopened["edit_ticket"])
        # Saved proposals are private: the owner cannot open the editor's.
        self.assertEqual(self.call(OWNER, "GET", f"/edits/{proposal()['id']}").status_code, 404)
        # Reviewing reads the public file; GitHub is connected only to push.
        with patch("api.github_router._load_token", new=AsyncMock(side_effect=HTTPException(status_code=401))):
            review = self.client.get(
                "/api/github/file", params={"repo_name": REPO, "file_path": "source/retry.ts"},
                headers={"Authorization": f"Bearer {EDITOR}", "X-Editing-Ticket": body["edit_ticket"]},
            )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["content"], SOURCE)
        with patch("api.github_router.get_repo_details", new=AsyncMock(return_value={"permissions": {"push": True}, "default_branch": "main"})), \
             patch("api.github_router._push_quality", new=AsyncMock(return_value={"status": "pass", "files": []})), \
             patch("api.github_router.create_branch_and_commit", new=AsyncMock(return_value={"branch_name": "codebase-intel/retry", "commit_sha": "c1"})), \
             patch("api.github_router.open_pull_request", new=AsyncMock(return_value={"pr_url": "https://github.com/sindresorhus/ky/pull/9", "pr_number": 9})):
            pushed = self.call(EDITOR, "POST", "/github/push-pr", json={
                "repo_name": REPO, "file_path": "source/retry.ts", "new_content": SOURCE.replace("?? 2", "?? 3"),
                "file_sha": "abc", "edit_ticket": body["edit_ticket"], "branch_name": "codebase-intel/retry",
            })
        self.assertEqual(pushed.status_code, 200, pushed.text)
        self.assertEqual(pushed.json()["pr_number"], 9)
        self.assertEqual(self.rows("SELECT user_id FROM github_change_operations"), [{"user_id": EDITOR}])

    def test_editor_cannot_manage(self):
        for method, path, options in (
            ("PATCH", f"/repos/{REPO}", {"json": {"repo_name": "mine-now"}}),
            ("POST", f"/repos/{REPO}/reindex", {}),
            ("POST", f"/repos/{REPO}/cancel-indexing", {}),
            ("DELETE", f"/repos/{REPO}", {}),
            ("GET", f"/repos/{REPO}/sharing", {}),
            ("POST", f"/repos/{REPO}/invites", {"json": {"role": "editor"}}),
            ("DELETE", f"/repos/{REPO}/members/{EDITOR}", {}),
        ):
            with self.subTest(route=f"{method} {path}"):
                response = self.call(EDITOR, method, path, **options)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIn("Only the owner", response.json()["detail"])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM repos"), [{"n": 1}])

    def test_downgrade_to_viewer_takes_effect_on_the_next_request(self):
        made = self.call(EDITOR, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"}).json()
        changed = self.call(OWNER, "PATCH", f"/repos/{REPO}/members/{EDITOR}", json={"role": "viewer"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["member"]["role"], "viewer")
        again = self.call(EDITOR, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"})
        self.assertEqual(again.status_code, 403)
        # An earlier ticket no longer opens a pull request, and no new one is issued.
        refused = self.call(EDITOR, "POST", "/github/push-pr", json={
            "repo_name": REPO, "file_path": "source/retry.ts", "new_content": "x", "file_sha": "abc", "edit_ticket": made["edit_ticket"],
        })
        self.assertEqual(refused.status_code, 403)
        self.assertIsNone(self.call(EDITOR, "GET", f"/edits/{proposal()['id']}").json()["edit_ticket"])
        upgraded = self.call(OWNER, "PATCH", f"/repos/{REPO}/members/{EDITOR}", json={"role": "editor"})
        self.assertEqual(upgraded.status_code, 200)
        self.assertEqual(
            self.call(EDITOR, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"}).status_code,
            200,
        )


class OwnerTests(TeamAccessCase):
    def test_owner_can_do_everything(self):
        self.join(VIEWER)
        created = self.invite("editor")
        self.assertEqual(created["path"], f"/invite/{created['token']}")
        self.assertGreaterEqual(len(created["token"]), 40)
        self.assertEqual(created["invite"]["token_hint"], created["token"][-4:])
        sharing = self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()
        self.assertEqual(len(sharing["invites"]), 2)
        self.assertNotIn(created["token"], str(sharing))
        self.assertEqual(self.call(OWNER, "DELETE", f"/repos/{REPO}/invites/{created['invite']['id']}").status_code, 200)
        self.assertEqual(self.call(OWNER, "PATCH", f"/repos/{REPO}/members/{VIEWER}", json={"role": "editor"}).status_code, 200)
        self.assertEqual(self.call(OWNER, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"}).status_code, 200)
        self.assertEqual(self.call(OWNER, "POST", f"/repos/{REPO}/reindex").status_code, 200)
        self.assertEqual(self.call(OWNER, "POST", f"/repos/{REPO}/cancel-indexing").status_code, 200)
        renamed = self.call(OWNER, "PATCH", f"/repos/{REPO}", json={"repo_name": "ky-main"})
        self.assertEqual(renamed.status_code, 200)
        # Renaming the owner's repository does not move the member's alias.
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").json()["owner_repo_name"], "ky-main")
        self.assertEqual(self.call(OWNER, "DELETE", f"/repos/ky-main/members/{VIEWER}").status_code, 200)

    def test_only_the_token_hash_is_stored(self):
        created = self.invite()
        stored = self.rows("SELECT token_hash, token_hint FROM repo_invites")[0]
        self.assertNotEqual(stored["token_hash"], created["token"])
        self.assertEqual(len(stored["token_hash"]), 64)
        self.assertNotIn(created["token"], str(self.rows("SELECT * FROM repo_invites")))

    def test_owner_cannot_join_or_leave_their_own_repository(self):
        token = self.invite()["token"]
        preview = self.call(OWNER, "GET", f"/invites/{token}").json()
        self.assertEqual(preview["status"], "owner")
        self.assertEqual(self.call(OWNER, "POST", f"/invites/{token}/accept").status_code, 409)
        self.assertEqual(self.call(OWNER, "DELETE", f"/repos/{REPO}/membership").status_code, 409)

    def test_deleting_the_repository_removes_memberships_and_invites(self):
        self.join(VIEWER)
        token = self.invite()["token"]
        deleted = self.call(OWNER, "DELETE", f"/repos/{REPO}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.rows("SELECT * FROM repo_members"), [])
        self.assertEqual(self.rows("SELECT * FROM repo_invites"), [])
        self.assertEqual(self.call(VIEWER, "GET", "/repos").json(), [])
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").json()["detail"], "Repository not found.")
        self.assertEqual(self.call(STRANGER, "GET", f"/invites/{token}").status_code, 404)


class RemovalAndRevocationTests(TeamAccessCase):
    def test_removed_member_gets_404_immediately_and_loses_the_listing(self):
        self.join(VIEWER)
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").status_code, 200)
        self.assertEqual(self.call(OWNER, "DELETE", f"/repos/{REPO}/members/{VIEWER}").status_code, 200)
        for method, path, options in repo_routes(REPO, self.ticket(VIEWER, REPO)):
            options = dict(options)
            headers = {"Authorization": f"Bearer {VIEWER}", **options.pop("headers_extra", {})}
            with self.subTest(route=f"{method} {path}"):
                response = self.client.request(method, f"/api{path}", headers=headers, **options)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["detail"], "This repository is no longer shared with you.")
                self.assertEqual(response.headers.get("x-error-code"), "share_removed")
        self.assertEqual(self.call(VIEWER, "GET", "/repos").json(), [])
        self.assertEqual(self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()["members"], [])

    def test_a_link_created_before_the_removal_cannot_bring_the_member_back(self):
        token = self.invite()["token"]
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{token}/accept").status_code, 200)
        self.call(OWNER, "DELETE", f"/repos/{REPO}/members/{VIEWER}")
        self.assertEqual(self.call(VIEWER, "GET", f"/invites/{token}").json()["status"], "removed")
        refused = self.call(VIEWER, "POST", f"/invites/{token}/accept")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("removed your access", refused.json()["detail"])
        # A new link from the owner works.
        rejoined = self.call(VIEWER, "POST", f"/invites/{self.invite('editor')['token']}/accept")
        self.assertEqual(rejoined.json(), {"repo_name": REPO, "role": "editor", "joined": True})
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").json()["access_role"], "editor")

    def test_member_can_leave_and_rejoin(self):
        token = self.invite()["token"]
        self.call(VIEWER, "POST", f"/invites/{token}/accept")
        self.assertEqual(self.call(VIEWER, "DELETE", f"/repos/{REPO}/membership").json(), {"left": True, "repo_name": REPO})
        gone = self.call(VIEWER, "GET", f"/status/{REPO}")
        self.assertEqual((gone.status_code, gone.json()["detail"]), (404, "Repository not found."))
        self.assertEqual(self.call(VIEWER, "GET", "/repos").json(), [])
        self.assertEqual(self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()["members"], [])
        self.assertEqual(self.call(VIEWER, "GET", f"/invites/{token}").json()["status"], "valid")
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{token}/accept").json(), {"repo_name": REPO, "role": "viewer", "joined": True})
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").json()["access_role"], "viewer")

    def test_leaving_and_rejoining_with_an_older_link_keeps_the_role_the_owner_set(self):
        editor_link = self.invite("editor")["token"]
        self.call(EDITOR, "POST", f"/invites/{editor_link}/accept")
        self.assertEqual(self.call(OWNER, "PATCH", f"/repos/{REPO}/members/{EDITOR}", json={"role": "viewer"}).status_code, 200)
        self.assertEqual(self.call(EDITOR, "DELETE", f"/repos/{REPO}/membership").status_code, 200)
        # The invite page says which role joining gives, and why it is not the link's.
        preview = self.call(EDITOR, "GET", f"/invites/{editor_link}").json()
        self.assertEqual((preview["status"], preview["role"], preview["role_kept"]), ("valid", "viewer", True))
        rejoined = self.call(EDITOR, "POST", f"/invites/{editor_link}/accept")
        self.assertEqual(rejoined.json(), {"repo_name": REPO, "role": "viewer", "joined": True})
        self.assertEqual(self.call(EDITOR, "GET", f"/status/{REPO}").json()["access_role"], "viewer")
        edit = self.call(EDITOR, "POST", "/query", json={"repo_name": REPO, "question": "Change the retry default to three", "workflow": "editing"})
        self.assertEqual(edit.status_code, 403)
        self.assertEqual(self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()["members"][0]["role"], "viewer")
        # A link the owner makes after the change grants its own role.
        self.call(EDITOR, "DELETE", f"/repos/{REPO}/membership")
        newer = self.invite("editor")["token"]
        self.assertFalse(self.call(EDITOR, "GET", f"/invites/{newer}").json()["role_kept"])
        self.assertEqual(self.call(EDITOR, "POST", f"/invites/{newer}/accept").json()["role"], "editor")

    def test_an_upgrade_also_outlasts_leaving(self):
        viewer_link = self.invite("viewer")["token"]
        self.call(VIEWER, "POST", f"/invites/{viewer_link}/accept")
        self.call(OWNER, "PATCH", f"/repos/{REPO}/members/{VIEWER}", json={"role": "editor"})
        self.call(VIEWER, "DELETE", f"/repos/{REPO}/membership")
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{viewer_link}/accept").json()["role"], "editor")

    def test_leaving_never_reopens_a_link_made_before_a_removal(self):
        before = self.invite()["token"]
        self.call(VIEWER, "POST", f"/invites/{before}/accept")
        self.call(OWNER, "DELETE", f"/repos/{REPO}/members/{VIEWER}")
        after = self.invite()["token"]
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{after}/accept").status_code, 200)
        self.assertEqual(self.call(VIEWER, "DELETE", f"/repos/{REPO}/membership").status_code, 200)
        # Having come back and left does not make the pre-removal link work.
        self.assertEqual(self.call(VIEWER, "GET", f"/invites/{before}").json()["status"], "removed")
        refused = self.call(VIEWER, "POST", f"/invites/{before}/accept")
        self.assertEqual((refused.status_code, refused.headers.get("x-error-code")), (403, "share_removed"))
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").json()["detail"], "Repository not found.")
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{after}/accept").json()["joined"], True)

    def test_revoked_link_is_refused_but_members_stay(self):
        created = self.invite()
        self.call(VIEWER, "POST", f"/invites/{created['token']}/accept")
        self.call(OWNER, "DELETE", f"/repos/{REPO}/invites/{created['invite']['id']}")
        for method, path in (("GET", f"/invites/{created['token']}"), ("POST", f"/invites/{created['token']}/accept")):
            response = self.call(EDITOR, method, path)
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.headers.get("x-error-code"), "invite_revoked")
            self.assertIn("revoked", response.json()["detail"])
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}").status_code, 200)
        self.assertEqual(self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()["invites"], [])

    def test_expired_link_is_refused(self):
        token = self.invite()["token"]
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        run(self.store.execute("UPDATE repo_invites SET expires_at = ?", [past]))
        for method, path in (("GET", f"/invites/{token}"), ("POST", f"/invites/{token}/accept")):
            response = self.call(VIEWER, method, path)
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.headers.get("x-error-code"), "invite_expired")
            self.assertIn("expired", response.json()["detail"])
        self.assertEqual(self.rows("SELECT * FROM repo_members"), [])
        self.assertEqual(self.call(OWNER, "GET", f"/repos/{REPO}/sharing").json()["invites"], [])

    def test_unknown_or_malformed_links_are_404(self):
        for token in ("x" * 43, "short", "not a token!"):
            with self.subTest(token=token):
                response = self.call(VIEWER, "GET", f"/invites/{token}")
                self.assertEqual(response.status_code, 404)
                self.assertIn("isn't valid", response.json()["detail"])

    def test_expiry_is_bounded(self):
        self.assertEqual(self.call(OWNER, "POST", f"/repos/{REPO}/invites", json={"role": "viewer", "expires_in_days": 0}).status_code, 422)
        self.assertEqual(self.call(OWNER, "POST", f"/repos/{REPO}/invites", json={"role": "owner"}).status_code, 422)

    def test_active_invite_links_are_capped(self):
        from api.sharing_router import MAX_ACTIVE_INVITES

        for _ in range(MAX_ACTIVE_INVITES):
            self.invite()
        refused = self.call(OWNER, "POST", f"/repos/{REPO}/invites", json={"role": "viewer"})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("Revoke one", refused.json()["detail"])

    def test_invite_preview_shows_repository_owner_and_role(self):
        token = self.invite("editor")["token"]
        preview = self.call(VIEWER, "GET", f"/invites/{token}").json()
        self.assertEqual(preview["status"], "valid")
        self.assertEqual(preview["repository"], REPO)
        self.assertEqual(preview["full_name"], "sindresorhus/ky")
        self.assertEqual(preview["owner_label"], f"{OWNER}@example.test")
        self.assertEqual(preview["role"], "editor")
        self.call(VIEWER, "POST", f"/invites/{token}/accept")
        again = self.call(VIEWER, "GET", f"/invites/{token}").json()
        self.assertEqual((again["status"], again["repo_name"], again["access_role"]), ("member", REPO, "editor"))

    def test_joining_again_never_changes_the_role_set_by_the_owner(self):
        self.join(VIEWER, "viewer")
        result = self.call(VIEWER, "POST", f"/invites/{self.invite('editor')['token']}/accept").json()
        self.assertEqual(result, {"repo_name": REPO, "role": "viewer", "joined": False})


class LinkTokensInLogsTests(TeamAccessCase):
    """Invite and answer links are bearer tokens in the URL path: logs must not hold them."""

    def test_paths_with_a_link_token_are_redacted_to_a_fingerprint(self):
        token = "Jx4m8k2Q9vZpL0aT7wR3nY6uC1eB5dHfGgKiMoNsPqX"
        mark = f"[token:{hash_token(token)[:8]}]"
        for path, expected in (
            (f"/api/invites/{token}", f"/api/invites/{mark}"),
            (f"/api/invites/{token}/accept", f"/api/invites/{mark}/accept"),
            (f"/api/shared-answers/{token}?x=1", f"/api/shared-answers/{mark}?x=1"),
            (f"/invite/{token}", f"/invite/{mark}"),
            (f"/shared/answer/{token}", f"/shared/answer/{mark}"),
            # Invite ids, member ids and other paths are not secrets.
            (f"/api/repos/{REPO}/invites/0f9c7d2e-1111-4222-8333-444455556666", f"/api/repos/{REPO}/invites/0f9c7d2e-1111-4222-8333-444455556666"),
            (f"/api/status/{REPO}", f"/api/status/{REPO}"),
        ):
            with self.subTest(path=path):
                self.assertEqual(redact_link_tokens(path), expected)

    def test_the_apps_request_log_shows_a_fingerprint_not_the_token(self):
        token = self.invite()["token"]
        with self.assertLogs("codebase_intel", "INFO") as captured:
            self.call(VIEWER, "GET", f"/invites/{token}")
            self.call(VIEWER, "POST", f"/invites/{token}/accept")
            self.call(VIEWER, "GET", f"/shared-answers/{token}")
        text = "\n".join(captured.output)
        self.assertNotIn(token, text)
        self.assertIn(f"path=/api/invites/[token:{hash_token(token)[:8]}]/accept status=200", text)

    def test_uvicorns_access_log_shows_a_fingerprint_not_the_token(self):
        import uvicorn

        from main import app

        # The server applies its logging config at startup, after main is
        # imported (scripts/dev_server.py) or before (uvicorn main:app); the
        # filter must survive either way.
        uvicorn.Config(app=app)
        token = self.invite()["token"]
        access = logging.getLogger("uvicorn.access")
        with self.assertLogs("uvicorn.access", "INFO") as captured:
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "POST", f"/api/invites/{token}/accept", "1.1", 200)
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", f"/invite/{token}", "1.1", 200)
        self.assertNotIn(token, "\n".join(captured.output))
        self.assertEqual(len(captured.records[0].args), 5)  # uvicorn's AccessFormatter unpacks five values
        self.assertIn(f"/invite/[token:{hash_token(token)[:8]}]", captured.output[1])


class AliasTests(TeamAccessCase):
    def test_member_with_a_same_named_repository_gets_an_alias(self):
        run(seed_repo(self.store, repo_id="repo-own", user_id=VIEWER, repo_name=REPO))
        joined = self.join(VIEWER)
        self.assertEqual(joined["repo_name"], f"{REPO}-shared")
        own = self.call(VIEWER, "GET", f"/status/{REPO}").json()
        shared = self.call(VIEWER, "GET", f"/status/{REPO}-shared").json()
        self.assertEqual((own["id"], own["access_role"]), ("repo-own", "owner"))
        self.assertEqual((shared["id"], shared["access_role"], shared["owner_repo_name"]), (REPO_ID, "viewer", REPO))
        names = [(repo["repo_name"], repo["shared"]) for repo in self.call(VIEWER, "GET", "/repos").json()]
        self.assertEqual(names, [(REPO, False), (f"{REPO}-shared", True)])
        answer = self.call(VIEWER, "POST", "/query", json={"repo_name": f"{REPO}-shared", "question": "Where are retries?"})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.rows("SELECT repo_id FROM chat_messages WHERE user_id = ? LIMIT 1", [VIEWER]), [{"repo_id": REPO_ID}])

    def test_a_third_repository_with_the_same_name_gets_a_numbered_alias(self):
        run(seed_repo(self.store, repo_id="repo-own", user_id=VIEWER, repo_name=REPO))
        run(seed_repo(self.store, repo_id="repo-other", user_id=EDITOR, repo_name=REPO))
        self.join(VIEWER)
        token = self.invite(user=EDITOR)["token"]
        self.assertEqual(self.call(VIEWER, "POST", f"/invites/{token}/accept").json()["repo_name"], f"{REPO}-shared-2")
        self.assertEqual(unique_alias("a", {"a", "a-shared", "a-shared-2"}), "a-shared-3")

    def test_the_members_own_repository_keeps_a_name_it_takes_later(self):
        self.join(VIEWER)
        run(seed_repo(self.store, repo_id="repo-own", user_id=VIEWER, repo_name="scratch"))
        self.assertEqual(self.call(VIEWER, "PATCH", "/repos/scratch", json={"repo_name": REPO}).status_code, 200)
        names = [(repo["repo_name"], repo["id"]) for repo in self.call(VIEWER, "GET", "/repos").json()]
        self.assertEqual(names, [(REPO, "repo-own"), (f"{REPO}-shared", REPO_ID)])
        self.assertEqual(self.call(VIEWER, "GET", f"/status/{REPO}-shared").json()["id"], REPO_ID)


class AnswerShareTests(TeamAccessCase):
    """FEAT-15: a read-only link to one answer, revocable by its author."""

    def ask(self, user, question="Where are retries configured?"):
        response = self.call(user, "POST", "/query", json={"repo_name": REPO, "question": question})
        self.assertEqual(response.status_code, 200, response.text)
        return self.call(user, "GET", f"/conversations/{REPO}").json()["messages"][-1]

    def share(self, user, **body):
        response = self.call(user, "POST", "/answers/share", json={"repo_name": REPO, **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_member_shares_an_answer_any_signed_in_user_reads_it_and_revoking_hides_it(self):
        self.join(VIEWER)
        stored = self.ask(VIEWER)
        created = self.share(VIEWER, message_id=stored["id"])
        self.assertEqual(created["path"], f"/shared/answer/{created['token']}")
        shown = self.call(STRANGER, "GET", f"/shared-answers/{created['token']}")
        self.assertEqual(shown.status_code, 200, shown.text)
        body = shown.json()
        self.assertEqual(body["question"], "Where are retries configured?")
        self.assertEqual(body["answer"], ANSWER)
        self.assertEqual(body["repository"], "sindresorhus/ky")
        self.assertEqual(body["commit_sha"], COMMIT)
        self.assertEqual(body["shared_by"], f"{VIEWER}@example.test")
        self.assertFalse(body["yours"])
        citation = body["citations"][0]
        self.assertEqual(citation["file_path"], "source/retry.ts")
        self.assertIn(f"/blob/{COMMIT}/source/retry.ts#L1-L3", citation["github_url"])
        self.assertNotIn("content", citation)
        self.assertNotIn(created["token"], str(self.rows("SELECT * FROM answer_shares")))

        listed = self.call(VIEWER, "GET", "/answers/shares").json()["shares"]
        self.assertEqual([(item["id"], item["question"]) for item in listed], [(created["id"], "Where are retries configured?")])
        self.assertEqual(self.call(STRANGER, "DELETE", f"/answers/shares/{created['id']}").status_code, 404)
        self.assertEqual(self.call(VIEWER, "DELETE", f"/answers/shares/{created['id']}").status_code, 200)
        gone = self.call(STRANGER, "GET", f"/shared-answers/{created['token']}")
        self.assertEqual((gone.status_code, gone.headers.get("x-error-code")), (404, "answer_unavailable"))
        self.assertEqual(self.call(VIEWER, "GET", "/answers/shares").json()["shares"], [])

    def test_a_just_streamed_answer_is_found_by_its_text(self):
        self.ask(OWNER)
        created = self.share(OWNER, message_id="client-side-id", answer=ANSWER)
        self.assertTrue(self.call(OWNER, "GET", f"/shared-answers/{created['token']}").json()["yours"])

    def test_only_your_own_saved_answer_in_a_repository_you_can_open_is_shared(self):
        owner_answer = self.ask(OWNER)
        self.join(VIEWER)
        refused = self.call(VIEWER, "POST", "/answers/share", json={"repo_name": REPO, "message_id": owner_answer["id"]})
        self.assertEqual(refused.status_code, 404)
        self.assertIn("isn't saved in your conversation", refused.json()["detail"])
        made_up = self.call(VIEWER, "POST", "/answers/share", json={"repo_name": REPO, "answer": "Anything I like"})
        self.assertEqual(made_up.status_code, 404)
        stranger = self.call(STRANGER, "POST", "/answers/share", json={"repo_name": REPO, "message_id": owner_answer["id"]})
        self.assertEqual((stranger.status_code, stranger.json()["detail"]), (404, "Repository not found."))
        self.assertEqual(self.rows("SELECT * FROM answer_shares"), [])

    def test_the_snapshot_survives_a_cleared_conversation(self):
        created = self.share(OWNER, message_id=self.ask(OWNER)["id"])
        self.call(OWNER, "DELETE", f"/conversations/{REPO}")
        self.assertEqual(self.call(STRANGER, "GET", f"/shared-answers/{created['token']}").json()["answer"], ANSWER)

    def test_a_link_stops_working_when_its_author_loses_access_or_the_repository_is_deleted(self):
        self.join(VIEWER)
        member_link = self.share(VIEWER, message_id=self.ask(VIEWER)["id"])
        owner_link = self.share(OWNER, message_id=self.ask(OWNER)["id"])
        self.call(OWNER, "DELETE", f"/repos/{REPO}/members/{VIEWER}")
        self.assertEqual(self.call(STRANGER, "GET", f"/shared-answers/{member_link['token']}").status_code, 404)
        self.assertEqual(self.call(STRANGER, "GET", f"/shared-answers/{owner_link['token']}").status_code, 200)
        self.assertEqual(self.call(OWNER, "DELETE", f"/repos/{REPO}").status_code, 200)
        self.assertEqual(self.call(STRANGER, "GET", f"/shared-answers/{owner_link['token']}").status_code, 404)
        self.assertEqual(self.rows("SELECT * FROM answer_shares"), [])

    def test_unknown_links_are_404(self):
        for token in ("x" * 43, "short"):
            self.assertEqual(self.call(STRANGER, "GET", f"/shared-answers/{token}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
