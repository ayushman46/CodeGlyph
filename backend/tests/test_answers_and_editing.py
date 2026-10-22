"""Integration tests for the answer pipeline, the SSE endpoint, the code editor
and the pull-request plumbing.

Every network or model boundary (NVIDIA chat, GitHub raw/API) is patched.
Stores are embedded libsql files created per test from the real
``turso/*.sql`` schema, so SQL, FTS5 triggers, JSON-column parsing and tenant
scoping run for real. Nothing here sets process-wide environment variables.
"""

import asyncio
import hashlib
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.query_router as query_router
from agent.agent import run_agent_loop, stream_agent_answer
from agent.code_edit import EditingAuthorizationError, create_edit_ticket, git_blob_sha, verify_edit_ticket
from agent.editor import EditRequest, run_code_edit
from agent.nemotron import LLMProviderError
from api.auth import get_current_user
from api.github_router import PushFileRequest, PushPRRequest, _payload_files, push_pr
from api.query_router import (
    QueryRequest,
    answer_events,
    build_context,
    build_retrieval_query,
    persist_proposal,
    save_message,
)
from api.repository_state import fetch_owned_repository
from config import ModelConfigurationError, UserRequestLimiter, get_answer_model_options, settings
from database import REQUIRED_TABLES, TursoStore, apply_migrations
from github.github_client import (
    GitHubAPIError,
    GitHubAuthRequiredError,
    GitHubConflictError,
    create_branch_and_commit_files,
    fetch_issue,
    file_exists_at_ref,
    sync_fork_branch,
)
from github.token_crypto import encrypt_token
from retrieval.retriever import build_evidence_plan

run = asyncio.run

SECRET = "test-editing-secret-answers-0123"
TOKEN_KEY = "test-github-token-encryption-secret-0123456789"
USER = "user-alpha"
OTHER = "user-beta"
REPO_ID = "repo-alpha"
REPO_NAME = "widgets"
GITHUB_URL = "https://github.com/acme/widgets"
COMMIT = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
STAGING = "run-7f3a:"

AUTH = "def login(user):\n    return check(user)\n\n\ndef logout(user):\n    return True\n"
SESSION = "def check(user):\n    return user is not None\n"
API = "from src.auth import login\nfrom src.session import check\n\n\ndef handler(user):\n    return check(user) and login(user)\n"
WINDOWS = b"def greet():\r\n    return 'hi'\r\n"
QUESTION = "How does session validation work?"


def expected_blob_sha(data: bytes) -> str:
    """Git's blob address, computed independently of agent.code_edit."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def chunk(path, start, content, *, score=0.5, chunk_id=None, symbols=()):
    return {
        "id": chunk_id or f"{REPO_ID}:{path}:{start}",
        "repo_id": REPO_ID,
        "file_path": path,
        "start_line": start,
        "end_line": start + content.count("\n"),
        "language": "python",
        "symbols": list(symbols),
        "content": content,
        "_relevance_score": score,
        "_retrieval_methods": ["keyword"],
        "_retrieval_reasons": ["Keyword match"],
    }


AUTH_CHUNK = chunk("src/auth.py", 1, AUTH.rstrip("\n"), symbols=("login", "logout"))
SESSION_CHUNK = chunk("src/session.py", 1, SESSION.rstrip("\n"), symbols=("check",))
API_CHUNK = chunk("src/api.py", 1, API.rstrip("\n"), symbols=("handler",))


async def seed_repo(store, *, repo_id=REPO_ID, user_id=USER, repo_name=REPO_NAME, github_url=GITHUB_URL,
                    chunk_count=3, status="ready", commit_sha=COMMIT, staging_prefix=None, job_status=None):
    now = "2026-10-01T00:00:00+00:00"
    await store.execute(
        "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [repo_id, user_id, repo_name, github_url, status, chunk_count, now, now],
    )
    if commit_sha:
        await store.execute(
            "INSERT INTO repo_snapshots (repo_id, commit_sha, source, selection_json, fetched_at) VALUES (?, ?, 'archive', '{}', ?)",
            [repo_id, commit_sha, now],
        )
    if staging_prefix:
        await store.execute(
            "INSERT INTO repo_index_runs (repo_id, job_id, chunk_prefix, started_at) VALUES (?, ?, ?, ?)",
            [repo_id, f"job-{repo_id}", staging_prefix, now],
        )
    if job_status:
        await store.execute(
            "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [f"job-{repo_id}", repo_id, user_id, github_url, job_status, now, now],
        )


async def seed_files(store, files: dict[str, str], extra_chunks=()):
    """Index whole files as one chunk each plus a manifest row."""
    rows = [chunk(path, 1, text.rstrip("\n")) for path, text in files.items()] + list(extra_chunks)
    await store.insert_chunks(rows)
    for path in {*files, *(row["file_path"] for row in extra_chunks)}:
        await store.execute(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
            [REPO_ID, path, hashlib.sha256(path.encode()).hexdigest(), 100, "2026-10-01T00:00:00+00:00"],
        )


def scripted_stream(deltas, *, error=None, delay=0.0, calls=None):
    """Stand-in for agent.agent.stream_agent_answer."""
    async def fake(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        for delta in deltas:
            if delay:
                await asyncio.sleep(delay)
            yield delta
        if error is not None:
            raise error
    return fake


async def collect(store, repo, request, *, user_id=USER, stream=True):
    return [event async for event in answer_events(store, repo, request, user_id, stream=stream)]


def parse_sse(body: str):
    """Parse a text/event-stream body; comment frames dispatch no event."""
    events, comments = [], []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name, data = "message", []
        for line in frame.split("\n"):
            if line.startswith(":"):
                comments.append(line[1:].strip())
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events, comments


def sample_proposal():
    test_content = "def test_logout():\n    assert True\n"
    auth_after = AUTH.replace("    return True", "    return False")
    session_after = SESSION.replace("is not None", "is not None and user.active")
    return {
        "id": "3f1c2b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b",
        "title": "Clear the session on logout",
        "summary": "Logout invalidates the session and a regression test covers it.",
        "pr_body": "## Summary\nClear the session on logout.",
        "validation": ["pytest tests/test_logout.py"],
        "issue_reference": None,
        "base_ref": "HEAD",
        "indexed_commit": COMMIT,
        "files": [
            {"file_path": "src/auth.py", "action": "modify", "summary": "Return False",
             "changes": [{"old": "    return True", "new": "    return False", "reason": ""}],
             "before": AUTH, "after": auth_after, "base_blob_sha": expected_blob_sha(AUTH.encode()),
             "additions": 1, "deletions": 1, "language": "python", "source": "github"},
            {"file_path": "src/session.py", "action": "modify", "summary": "Require an active user",
             "changes": [{"old": "is not None", "new": "is not None and user.active", "reason": ""}],
             "before": SESSION, "after": session_after, "base_blob_sha": expected_blob_sha(SESSION.encode()),
             "additions": 1, "deletions": 1, "language": "python", "source": "github"},
            {"file_path": "tests/test_logout.py", "action": "create", "summary": "Regression test", "changes": [],
             "before": "", "after": test_content, "base_blob_sha": None, "additions": 2, "deletions": 0,
             "language": "python", "source": "generated"},
        ],
        "stats": {"files": 3, "additions": 4, "deletions": 2},
        "file_path": "src/auth.py",
        "changes": [{"old": "    return True", "new": "    return False", "reason": ""}],
    }


class DatabaseTestCase(unittest.TestCase):
    """A fresh embedded database with the complete migrated schema."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'x.db'}", "local-test-token")
        self.applied = run(apply_migrations(self.store))

    def tearDown(self):
        run(self.store.close())
        self._tmp.cleanup()

    def chat_rows(self, repo_id=REPO_ID, user_id=USER):
        return run(self.store.fetch_all(
            "SELECT role, content, citations, tool_calls, mode FROM chat_messages WHERE repo_id = ? AND user_id = ? "
            "ORDER BY created_at, role DESC",
            [repo_id, user_id],
        ))


class SchemaBootstrapTests(DatabaseTestCase):
    def test_migrations_create_every_required_table_and_are_idempotent(self):
        self.assertEqual(self.applied, ["00_init.sql", "05_performance.sql", "06_scale_and_editing.sql"])
        tables = {row["name"] for row in run(self.store.fetch_all("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"))}
        self.assertEqual([table for table in REQUIRED_TABLES if table not in tables], [])
        self.assertIn("chunks_fts", tables)
        self.assertEqual(run(apply_migrations(self.store)), [])


# ---------------------------------------------------------------------------
# answer_events: the single answer pipeline
# ---------------------------------------------------------------------------


class AnswerEventsTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(seed_repo(self.store, staging_prefix=STAGING))
        self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        self.repo_map = AsyncMock(return_value={"text": "2 indexed files.\nsrc/ (2 files)"})

    def ask(self, question, *, workflow="general", stream=True, retrieved=(), stream_fn=None, loop=None, editor=None):
        retrieve = AsyncMock(return_value=list(retrieved))
        patches = [
            patch("api.query_router.retrieve_context", retrieve),
            patch("api.query_router.build_repo_map", self.repo_map),
            patch("api.query_router.stream_agent_answer", stream_fn or scripted_stream(["unused"])),
            patch("api.query_router.run_agent_loop", loop or AsyncMock(return_value=("unused", []))),
            patch("api.query_router.run_code_edit", editor or AsyncMock(return_value=("No change", [], None))),
            patch.object(settings, "editing_ticket_secret", SECRET),
        ]
        for item in patches:
            item.start()
        try:
            request = QueryRequest(repo_name=REPO_NAME, question=question, workflow=workflow)
            events = run(collect(self.store, self.repo, request, stream=stream))
        finally:
            for item in reversed(patches):
                item.stop()
        return events, retrieve

    def test_repository_presentation_exposes_the_internal_staging_prefix_and_snapshot(self):
        self.assertTrue(self.repo["queryable"])
        self.assertEqual(self.repo["staging_prefix"], STAGING)
        self.assertEqual(self.repo["commit_sha"], COMMIT)

    def test_greeting_short_circuits_retrieval_and_the_model(self):
        calls = []
        events, retrieve = self.ask("Hello there!", stream_fn=scripted_stream(["x"], calls=calls))
        self.assertEqual([name for name, _ in events], ["done"])
        done = events[0][1]
        self.assertEqual(done["mode"], "greeting")
        self.assertEqual(done["answer"], f"Hello. What would you like to understand about **{REPO_NAME}**?")
        self.assertEqual(done["citations"], [])
        self.assertEqual(done["snapshot"], {"commit_sha": COMMIT, "github_url": GITHUB_URL})
        retrieve.assert_not_awaited()
        self.assertEqual(calls, [])
        rows = self.chat_rows()
        self.assertEqual([(row["role"], row["content"]) for row in rows], [("user", "Hello there!"), ("assistant", done["answer"])])
        self.assertEqual(rows[1]["mode"], "greeting")

    def test_editing_request_without_action_path_or_issue_returns_guidance(self):
        for question in ("Is this approach reasonable?", "what do you think about the current layout?"):
            with self.subTest(question=question):
                editor = AsyncMock(return_value=("No change", [], None))
                events, retrieve = self.ask(question, workflow="editing", editor=editor)
                self.assertEqual([name for name, _ in events], ["done"])
                done = events[0][1]
                self.assertEqual(done["mode"], "editing_guidance")
                self.assertEqual(done["model_profile"], "code")
                self.assertIn("Describe the change you want", done["answer"])
                self.assertIsNone(done["edit_ticket"])
                retrieve.assert_not_awaited()
                editor.assert_not_awaited()
        # An action verb, an explicit path, or an issue link is actionable.
        for question in ("Rename the login helper", "Look at src/auth.py", "See https://github.com/acme/widgets/issues/12"):
            with self.subTest(question=question):
                editor = AsyncMock(return_value=("No change", [], None))
                events, retrieve = self.ask(question, workflow="editing", retrieved=[AUTH_CHUNK], editor=editor)
                retrieve.assert_awaited_once()
                editor.assert_awaited_once()
                self.assertEqual(events[-1][1]["mode"], "editing")

    def test_missing_or_overview_only_evidence_returns_repository_guidance(self):
        readme = chunk("README.md", 1, "# Widgets\nSession validation overview")
        for retrieved in ([], [readme]):
            with self.subTest(retrieved=[item["file_path"] for item in retrieved]):
                calls = []
                events, _ = self.ask(QUESTION, retrieved=retrieved, stream_fn=scripted_stream(["x"], calls=calls))
                self.assertEqual([name for name, _ in events], ["stage", "done"])
                self.assertEqual(events[0][1]["stage"], "retrieving")
                done = events[-1][1]
                self.assertEqual(done["mode"], "repository_guidance")
                self.assertEqual(done["citations"], [])
                self.assertIn("won't guess", done["answer"])
                self.assertEqual(calls, [])

    def test_streaming_answer_emits_stage_citations_tokens_and_done(self):
        deltas = ["Sessions are validated in ", "`src/session.py:L1-L2`", " before login."]
        calls = []
        events, retrieve = self.ask(
            QUESTION, retrieved=[AUTH_CHUNK, SESSION_CHUNK], stream_fn=scripted_stream(deltas, calls=calls),
        )
        self.assertEqual(
            [name for name, _ in events],
            ["stage", "citations", "stage", "token", "token", "token", "done"],
        )
        self.assertEqual(events[0][1], {"stage": "retrieving", "detail": "Searching the indexed code"})
        self.assertEqual(events[2][1]["stage"], "generating")
        self.assertEqual([data["text"] for name, data in events if name == "token"], deltas)

        citation_event = events[1][1]
        self.assertEqual([item["file_path"] for item in citation_event["citations"]], ["src/auth.py", "src/session.py"])
        self.assertEqual(
            citation_event["citations"][0]["github_url"],
            f"https://github.com/acme/widgets/blob/{COMMIT}/src/auth.py#L1-L6",
        )
        self.assertEqual(
            citation_event["citations"][1]["github_url"],
            f"https://github.com/acme/widgets/blob/{COMMIT}/src/session.py#L1-L2",
        )
        self.assertEqual(citation_event["snapshot"], {"commit_sha": COMMIT, "github_url": GITHUB_URL})

        done = events[-1][1]
        self.assertEqual(done["answer"], "".join(deltas))
        self.assertEqual(done["mode"], "rag")
        self.assertEqual(done["model_profile"], "fast")
        # The source the answer actually cites is ranked first and marked.
        self.assertEqual([item["file_path"] for item in done["citations"]], ["src/session.py", "src/auth.py"])
        self.assertEqual([item["cited_in_answer"] for item in done["citations"]], [True, False])

        self.assertEqual(retrieve.await_args.args, (self.store, REPO_ID, QUESTION))
        kwargs = retrieve.await_args.kwargs
        self.assertEqual(kwargs["staging_prefix"], STAGING)
        self.assertEqual(kwargs["top_k"], settings.retrieval_top_k)
        self.assertEqual(kwargs["workflow"], "general")
        self.assertIsNone(kwargs["retrieval_query"])
        self.assertEqual(kwargs["boost_paths"], [])

        self.assertEqual(len(calls), 1)
        question, context, history, profile, workflow, plan, repo_map_text = calls[0][0]
        self.assertEqual(question, QUESTION)
        self.assertIn("File: src/auth.py (L1-L6)", context)
        self.assertIn("    1| def login(user):", context)
        self.assertIn("    6|     return True", context)
        self.assertIn("    2|     return user is not None", context)
        self.assertEqual(history, [])
        self.assertEqual((profile, workflow), ("fast", "general"))
        self.assertEqual(plan["workflow"], "general")
        self.assertEqual(repo_map_text, "2 indexed files.\nsrc/ (2 files)")

        rows = self.chat_rows()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["content"], QUESTION)
        self.assertEqual(rows[1]["content"], done["answer"])
        self.assertEqual(rows[1]["mode"], "rag")
        self.assertEqual([item["file_path"] for item in rows[1]["citations"]], ["src/session.py", "src/auth.py"])

    def test_non_streaming_path_uses_run_agent_loop(self):
        loop = AsyncMock(return_value=("Login is in `src/auth.py:L1-L6`.", [{"tool": "lookup"}]))
        calls = []
        events, _ = self.ask(
            QUESTION, stream=False, retrieved=[AUTH_CHUNK, SESSION_CHUNK], loop=loop,
            stream_fn=scripted_stream(["never"], calls=calls),
        )
        self.assertEqual([name for name, _ in events], ["stage", "citations", "stage", "done"])
        done = events[-1][1]
        self.assertEqual(done["answer"], "Login is in `src/auth.py:L1-L6`.")
        self.assertEqual(done["tool_calls"], [{"tool": "lookup"}])
        self.assertEqual(done["mode"], "rag")
        self.assertEqual([item["cited_in_answer"] for item in done["citations"]], [True, False])
        self.assertEqual(calls, [])
        loop.assert_awaited_once()
        args, kwargs = loop.await_args
        self.assertEqual(args[:3], (self.store, REPO_ID, QUESTION))
        self.assertIn("    1| def login(user):", args[3])
        self.assertEqual(args[5], "fast")
        self.assertEqual(kwargs["workflow"], "general")
        self.assertEqual(kwargs["repo_map_text"], "2 indexed files.\nsrc/ (2 files)")
        self.assertEqual(kwargs["evidence_plan"]["workflow"], "general")

    def test_model_failure_before_any_token_falls_back_to_retrieved_evidence(self):
        for error in (LLMProviderError("NVIDIA is unavailable"), ModelConfigurationError("NVIDIA_API_KEY is missing")):
            with self.subTest(error=type(error).__name__):
                events, _ = self.ask(QUESTION, retrieved=[AUTH_CHUNK], stream_fn=scripted_stream([], error=error))
                self.assertEqual([name for name, _ in events], ["stage", "citations", "stage", "done"])
                done = events[-1][1]
                self.assertEqual(done["mode"], "retrieval_fallback")
                self.assertTrue(done["answer"].startswith("## Retrieved code context"))
                self.assertIn("`src/auth.py:L1-L6`", done["answer"])
                self.assertNotIn("No files were changed", done["answer"])
        loop = AsyncMock(side_effect=LLMProviderError("busy"))
        events, _ = self.ask(QUESTION, stream=False, retrieved=[AUTH_CHUNK], loop=loop)
        self.assertEqual(events[-1][1]["mode"], "retrieval_fallback")
        self.assertEqual(events[-1][1]["tool_calls"], [])

    def test_model_failure_after_tokens_keeps_the_partial_answer(self):
        events, _ = self.ask(
            QUESTION, retrieved=[AUTH_CHUNK],
            stream_fn=scripted_stream(["Sessions are validated "], error=LLMProviderError("stream dropped")),
        )
        self.assertEqual([name for name, _ in events], ["stage", "citations", "stage", "token", "done"])
        done = events[-1][1]
        self.assertEqual(done["mode"], "partial")
        self.assertTrue(done["answer"].startswith("Sessions are validated "))
        self.assertIn("The answer was interrupted", done["answer"])
        rows = self.chat_rows()
        self.assertEqual(rows[-1]["content"], done["answer"])
        self.assertEqual(rows[-1]["mode"], "partial")

    def test_short_follow_up_carries_previous_question_and_cited_paths(self):
        self.assertEqual(build_retrieval_query("Where is it called?", []), (None, []))
        first_answer = "Validation happens in `src/auth.py:L1-L6`."
        self.ask(QUESTION, retrieved=[SESSION_CHUNK, AUTH_CHUNK], stream_fn=scripted_stream([first_answer]))

        calls = []
        events, retrieve = self.ask(
            "Where is it called?", retrieved=[API_CHUNK], stream_fn=scripted_stream(["In the API."], calls=calls),
        )
        kwargs = retrieve.await_args.kwargs
        self.assertEqual(kwargs["retrieval_query"], f"Where is it called?\n{QUESTION}")
        # Only the file the previous answer actually cited is carried over.
        self.assertEqual(kwargs["boost_paths"], ["src/auth.py"])
        history = calls[0][0][2]
        self.assertEqual(history, [
            {"role": "user", "content": QUESTION, "cited_paths": []},
            {"role": "assistant", "content": first_answer, "cited_paths": ["src/auth.py"]},
        ])
        self.assertEqual(events[-1][1]["answer"], "In the API.")

        long_question = (
            "Can you walk me through every single step of how it eventually persists the refreshed "
            "session token into the database layer?"
        )
        for question in ("Explain the database migration runner", long_question):
            with self.subTest(question=question):
                _, retrieve = self.ask(question, retrieved=[API_CHUNK], stream_fn=scripted_stream(["ok"]))
                self.assertIsNone(retrieve.await_args.kwargs["retrieval_query"])
                self.assertEqual(retrieve.await_args.kwargs["boost_paths"], [])

    def test_editing_answer_relays_stages_tickets_every_file_and_persists_the_proposal(self):
        proposal = sample_proposal()
        captured = {}

        async def fake_run_code_edit(request):
            captured["request"] = request
            await request.emit("planning", "Planning the change across 2 candidate files")
            await request.emit("editing", "Writing the patch")
            return "## Proposed change", [{"tool": "plan_change", "input": {}, "result_summary": "plan"}], proposal

        retrieved = [
            AUTH_CHUNK,
            chunk("src/auth.py", 2, "    return check(user)", chunk_id="auth-2"),
            chunk("src/auth.py", 5, "def logout(user):", chunk_id="auth-5"),
            SESSION_CHUNK,
        ]
        question = "Make logout clear the session and add a regression test"
        events, retrieve = self.ask(question, workflow="editing", retrieved=retrieved, editor=fake_run_code_edit)
        self.assertEqual([name for name, _ in events], ["stage", "citations", "stage", "stage", "done"])
        self.assertEqual([data["stage"] for name, data in events if name == "stage"], ["retrieving", "planning", "editing"])
        # Editing cites a file-diverse subset: at most two chunks per file.
        self.assertEqual([item["file_path"] for item in events[1][1]["citations"]], ["src/auth.py", "src/auth.py", "src/session.py"])
        self.assertEqual(retrieve.await_args.kwargs["top_k"], settings.editing_retrieval_top_k)

        done = events[-1][1]
        self.assertEqual(done["mode"], "editing")
        self.assertEqual(done["model_profile"], "code")
        self.assertEqual(done["edit_suggestion"], proposal)
        ticket = done["edit_ticket"]
        for path in ("src/auth.py", "src/session.py", "tests/test_logout.py"):
            payload = verify_edit_ticket(SECRET, ticket, user_id=USER, repo_name=REPO_NAME, file_path=path)
            self.assertEqual(payload["r"], REPO_NAME)
        with self.assertRaises(EditingAuthorizationError):
            verify_edit_ticket(SECRET, ticket, user_id=USER, repo_name=REPO_NAME, file_path="src/api.py")
        with self.assertRaises(EditingAuthorizationError):
            verify_edit_ticket(SECRET, ticket, user_id=OTHER, repo_name=REPO_NAME, file_path="src/auth.py")
        self.assertEqual(
            done["tool_calls"][-1],
            {"tool": "code_edit", "input": {"proposal_id": proposal["id"]}, "result_summary": proposal["title"]},
        )

        row = run(self.store.fetch_one("SELECT * FROM edit_proposals WHERE id = ?", [proposal["id"]]))
        self.assertEqual((row["user_id"], row["repo_id"], row["repo_name"]), (USER, REPO_ID, REPO_NAME))
        self.assertEqual(json.loads(row["payload_json"]), proposal)

        request = captured["request"]
        self.assertIsInstance(request, EditRequest)
        self.assertEqual(request.user_id, USER)
        self.assertEqual(request.question, question)
        self.assertEqual(request.evidence_chunks, retrieved)
        self.assertEqual(request.repo["id"], REPO_ID)
        self.assertEqual(request.repo_map_text, "2 indexed files.\nsrc/ (2 files)")

        rows = self.chat_rows()
        self.assertEqual(rows[-1]["mode"], "editing")
        self.assertEqual(rows[-1]["content"], "## Proposed change")
        self.assertEqual(rows[-1]["tool_calls"][-1]["tool"], "code_edit")

    def test_editing_model_outage_returns_evidence_without_a_ticket(self):
        editor = AsyncMock(side_effect=LLMProviderError("All code models are busy"))
        events, _ = self.ask("Fix src/auth.py logout", workflow="editing", retrieved=[AUTH_CHUNK], editor=editor)
        done = events[-1][1]
        self.assertEqual(done["mode"], "retrieval_fallback")
        self.assertTrue(done["answer"].startswith("No files were changed"))
        self.assertIsNone(done["edit_suggestion"])
        self.assertIsNone(done["edit_ticket"])
        self.assertEqual(run(self.store.fetch_all("SELECT id FROM edit_proposals")), [])

    def test_build_context_prefixes_real_line_numbers_within_budget(self):
        evidence = [chunk("src/deep.py", 41, "first = 1\nsecond = 2\nthird = 3")]
        context = build_context(evidence)
        self.assertIn("File: src/deep.py (L41-L43)", context)
        self.assertIn("   41| first = 1\n   42| second = 2\n   43| third = 3", context)
        header_size = len(build_context(evidence).split("   41|")[0])
        truncated = build_context(evidence, max_characters=header_size + 2 + len("   41| first = 1") + 1)
        self.assertIn("   41| first = 1", truncated)
        self.assertNotIn("   42|", truncated)


# ---------------------------------------------------------------------------
# HTTP endpoints (FastAPI TestClient, real database, patched providers)
# ---------------------------------------------------------------------------


class EndpointTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        from main import app

        self.app = app
        run(seed_repo(self.store))
        self.current_user = SimpleNamespace(id=USER)
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.addCleanup(app.dependency_overrides.pop, get_current_user, None)
        self.limiter = UserRequestLimiter(100, 1)
        for item in (
            patch("api.query_router.get_turso_store", return_value=self.store),
            patch("api.query_router.assert_turso_schema", new=AsyncMock()),
            patch("api.query_router.query_request_limiter", self.limiter),
            patch("api.query_router.build_repo_map", new=AsyncMock(return_value={"text": ""})),
            patch("api.github_router.get_turso_store", return_value=self.store),
            patch.object(settings, "editing_ticket_secret", SECRET),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(app)

    def stream(self, question=QUESTION, repo_name=REPO_NAME, *, retrieved=(AUTH_CHUNK, SESSION_CHUNK), deltas=("Hi",),
               retrieve=None, stream_fn=None):
        with patch("api.query_router.retrieve_context", retrieve or AsyncMock(return_value=list(retrieved))), \
             patch("api.query_router.stream_agent_answer", stream_fn or scripted_stream(list(deltas))):
            return self.client.post("/api/query/stream", json={"repo_name": repo_name, "question": question})


class StreamingEndpointTests(EndpointTestCase):
    def test_stream_endpoint_sends_ordered_events_and_releases_the_limiter(self):
        deltas = ["Validated in ", "`src/auth.py:L1-L6`", "."]
        response = self.stream(deltas=deltas)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        events, comments = parse_sse(response.text)
        self.assertEqual(comments, ["connected"])
        self.assertEqual(
            [name for name, _ in events],
            ["stage", "citations", "stage", "token", "token", "token", "done"],
        )
        self.assertEqual(events[1][1]["citations"][0]["github_url"], f"https://github.com/acme/widgets/blob/{COMMIT}/src/auth.py#L1-L6")
        done = events[-1][1]
        self.assertEqual(done["answer"], "".join(deltas))
        self.assertEqual(done["citations"][0]["file_path"], "src/auth.py")
        self.assertTrue(done["citations"][0]["cited_in_answer"])
        self.assertNotIn(USER, self.limiter._active)
        # max_concurrent=1: a second question only succeeds if the slot was freed.
        again = self.stream(deltas=["Again"])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(parse_sse(again.text)[0][-1][1]["answer"], "Again")
        rows = self.chat_rows()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant", "user", "assistant"])
        self.assertEqual(rows[1]["content"], "".join(deltas))

    def test_keep_alive_comments_are_ignorable_and_do_not_change_events(self):
        real_wait_for = asyncio.wait_for

        async def quick_wait_for(awaitable, timeout=None):
            # Only the 15-second keep-alive timer is shortened.
            return await real_wait_for(awaitable, timeout=0.02 if timeout == 15 else timeout)

        proxy = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
        proxy.wait_for = quick_wait_for
        deltas = ["slow ", "answer"]
        with patch("api.query_router.asyncio", proxy):
            response = self.stream(stream_fn=scripted_stream(deltas, delay=0.12))
        self.assertEqual(response.status_code, 200)
        self.assertIn(": keep-alive\n\n", response.text)
        events, comments = parse_sse(response.text)
        self.assertEqual(comments[0], "connected")
        self.assertGreaterEqual(comments.count("keep-alive"), 2)
        self.assertEqual([name for name, _ in events], ["stage", "citations", "stage", "token", "token", "done"])
        self.assertEqual(events[-1][1]["answer"], "slow answer")
        self.assertNotIn(USER, self.limiter._active)

    def test_error_event_is_terminal_and_the_limiter_is_released(self):
        failing = AsyncMock(side_effect=RuntimeError("disk I/O error at /var/data/secret.db"))
        with self.assertLogs("api.query_router", level="ERROR"):
            response = self.stream(retrieve=failing)
        self.assertEqual(response.status_code, 200)
        events, _ = parse_sse(response.text)
        self.assertEqual([name for name, _ in events], ["stage", "error"])
        error = events[-1][1]
        self.assertEqual(error["status"], 500)
        self.assertTrue(error["detail"])
        self.assertNotIn("secret.db", error["detail"])
        self.assertNotIn(USER, self.limiter._active)
        self.assertEqual(self.stream(deltas=["Recovered"]).status_code, 200)

    def test_unqueryable_or_unknown_repository_fails_as_plain_http_before_streaming(self):
        run(seed_repo(self.store, repo_id="repo-pending", repo_name="pending", chunk_count=0, status="queued",
                      commit_sha=None, job_status="queued"))
        retrieve = AsyncMock()
        response = self.stream(repo_name="pending", retrieve=retrieve)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertIn("still being indexed", response.json()["detail"])
        self.assertNotIn("event:", response.text)
        retrieve.assert_not_awaited()
        self.assertNotIn(USER, self.limiter._active)

        missing = self.stream(repo_name="nope", retrieve=retrieve)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "Repository not found.")
        self.assertNotIn(USER, self.limiter._active)

    def test_concurrent_question_is_rejected_without_releasing_the_other_slot(self):
        run(self.limiter.acquire(USER))
        response = self.stream()
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many questions", response.json()["detail"])
        self.assertEqual(self.limiter._active.get(USER), 1)

    def test_json_query_endpoint_returns_the_done_payload(self):
        loop = AsyncMock(return_value=("See `src/session.py:L1-L2`.", []))
        with patch("api.query_router.retrieve_context", AsyncMock(return_value=[AUTH_CHUNK, SESSION_CHUNK])), \
             patch("api.query_router.run_agent_loop", loop):
            response = self.client.post("/api/query", json={"repo_name": REPO_NAME, "question": QUESTION})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answer"], "See `src/session.py:L1-L2`.")
        self.assertEqual(body["mode"], "rag")
        self.assertEqual(body["citations"][0]["file_path"], "src/session.py")
        self.assertEqual(body["snapshot"]["commit_sha"], COMMIT)
        self.assertNotIn(USER, self.limiter._active)

    def test_disconnect_cancels_generation_and_releases_exactly_once(self):
        class CountingLimiter(UserRequestLimiter):
            acquired = released = 0

            async def acquire(self, user_id):
                await super().acquire(user_id)
                CountingLimiter.acquired += 1

            async def release(self, user_id):
                CountingLimiter.released += 1
                await super().release(user_id)

        limiter = CountingLimiter(100, 1)
        state = {"cancelled": False}

        async def blocking_stream(*_args, **_kwargs):
            yield "Sessions are "
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def scenario():
            response = await query_router.query_repo_stream(
                QueryRequest(repo_name=REPO_NAME, question=QUESTION), SimpleNamespace(id=USER),
            )
            frames = []
            iterator = response.body_iterator
            while not any(frame.startswith("event: token") for frame in frames):
                frames.append(await asyncio.wait_for(iterator.__anext__(), 5))
            await iterator.aclose()  # the client went away mid-answer
            await response.background()
            return frames

        with patch("api.query_router.query_request_limiter", limiter), \
             patch("api.query_router.retrieve_context", AsyncMock(return_value=[AUTH_CHUNK])), \
             patch("api.query_router.stream_agent_answer", blocking_stream):
            frames = run(scenario())
        self.assertEqual(frames[0], ": connected\n\n")
        self.assertTrue(state["cancelled"])
        self.assertEqual((CountingLimiter.acquired, CountingLimiter.released), (1, 1))
        self.assertNotIn(USER, limiter._active)

    def test_disconnect_during_a_blocked_send_stops_generation_before_freeing_the_slot(self):
        """Drive the real Starlette response the way uvicorn does (ASGI 2.3).

        When the client goes away while the response is waiting in ``send``
        (a slow reader, or the middleware's zero-capacity stream), Starlette
        cancels the send and leaves the event generator suspended. Generation
        must still stop; otherwise the background task frees the per-user slot
        while the model keeps generating, and the concurrency limit means
        nothing.
        """
        state = {"cancelled": False, "finished": False}

        async def slow_stream(*_args, **_kwargs):
            yield "Sessions are "
            try:
                await asyncio.sleep(0.3)
                yield "validated."
                state["finished"] = True
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def scenario():
            response = await query_router.query_repo_stream(
                QueryRequest(repo_name=REPO_NAME, question=QUESTION), SimpleNamespace(id=USER),
            )
            token_sent = asyncio.Event()

            async def send(message):
                if b"event: token" in message.get("body", b""):
                    token_sent.set()
                    await asyncio.Event().wait()  # the client stopped reading

            async def receive():
                await token_sent.wait()
                return {"type": "http.disconnect"}

            await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 5)
            # ``response`` is still referenced, so garbage collection cannot be
            # what closes the generator.
            snapshot = dict(state), dict(self.limiter._active)
            await asyncio.sleep(0.5)
            return response, snapshot

        with patch("api.query_router.retrieve_context", AsyncMock(return_value=[AUTH_CHUNK])), \
             patch("api.query_router.stream_agent_answer", slow_stream):
            _response, (at_return, active_at_return) = run(scenario())
        self.assertEqual(at_return, {"cancelled": True, "finished": False})
        self.assertEqual(active_at_return, {})
        # The abandoned answer was not generated to completion and saved.
        self.assertEqual(self.chat_rows(), [])

    def test_invalid_question_fails_as_plain_http_before_streaming(self):
        retrieve = AsyncMock()
        cases = (
            ({"question": "   \n\t "}, "non-whitespace"),
            ({"question": QUESTION, "model_profile": "code"}, "Code editing and PR mode"),
        )
        for overrides, detail in cases:
            with self.subTest(overrides=overrides), \
                 patch("api.query_router.retrieve_context", retrieve):
                response = self.client.post("/api/query/stream", json={"repo_name": REPO_NAME, **overrides})
                self.assertEqual(response.status_code, 422)
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                self.assertIn(detail, response.json()["detail"])
                self.assertNotIn(USER, self.limiter._active)
                json_response = self.client.post("/api/query", json={"repo_name": REPO_NAME, **overrides})
                self.assertEqual(json_response.status_code, 422)
                self.assertEqual(json_response.json()["detail"], response.json()["detail"])
        retrieve.assert_not_awaited()
        self.assertEqual(self.chat_rows(), [])
        # Code mode is still accepted together with the editing workflow.
        with patch("api.query_router.retrieve_context", AsyncMock(return_value=[AUTH_CHUNK])), \
             patch("api.query_router.run_code_edit", AsyncMock(return_value=("No change", [], None))):
            response = self.client.post("/api/query/stream", json={
                "repo_name": REPO_NAME, "question": "Fix src/auth.py logout", "model_profile": "code", "workflow": "editing",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_sse(response.text)[0][-1][0], "done")

    def test_cancellation_before_streaming_starts_releases_the_slot(self):
        entered = asyncio.Event()

        async def hanging_lookup(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def scenario():
            task = asyncio.create_task(query_router.query_repo_stream(
                QueryRequest(repo_name=REPO_NAME, question=QUESTION), SimpleNamespace(id=USER),
            ))
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(self.limiter._active.get(USER), 1)
            task.cancel()  # server shutdown, a request timeout, a cancelled task group
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch("api.query_router.get_owned_repo", hanging_lookup):
            run(scenario())
        self.assertNotIn(USER, self.limiter._active)


class EditProposalEndpointTests(EndpointTestCase):
    def setUp(self):
        super().setUp()
        self.proposal = sample_proposal()
        repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        run(persist_proposal(self.store, self.proposal, repo=repo, repo_name=REPO_NAME, user_id=USER))

    def test_owner_reopens_a_proposal_with_a_fresh_ticket_for_the_current_name(self):
        original = query_router.issue_edit_ticket(USER, REPO_NAME, self.proposal)
        time.sleep(0.002)
        response = self.client.get(f"/api/edits/{self.proposal['id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["proposal"], self.proposal)
        self.assertEqual(body["repo_name"], REPO_NAME)
        self.assertNotEqual(body["edit_ticket"], original)
        for path in ("src/auth.py", "src/session.py", "tests/test_logout.py"):
            payload = verify_edit_ticket(SECRET, body["edit_ticket"], user_id=USER, repo_name=REPO_NAME, file_path=path)
            self.assertGreaterEqual(payload["e"], int(time.time()) + settings.editing_ticket_ttl_seconds - 5)

        run(self.store.execute("UPDATE repos SET repo_name = ? WHERE id = ?", ["widgets-renamed", REPO_ID]))
        renamed = self.client.get(f"/api/edits/{self.proposal['id']}").json()
        self.assertEqual(renamed["repo_name"], "widgets-renamed")
        verify_edit_ticket(SECRET, renamed["edit_ticket"], user_id=USER, repo_name="widgets-renamed", file_path="src/auth.py")
        with self.assertRaises(EditingAuthorizationError):
            verify_edit_ticket(SECRET, renamed["edit_ticket"], user_id=USER, repo_name=REPO_NAME, file_path="src/auth.py")

    def test_proposals_are_owner_only_and_ids_are_validated(self):
        self.current_user = SimpleNamespace(id=OTHER)
        response = self.client.get(f"/api/edits/{self.proposal['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Proposal not found.")
        self.current_user = SimpleNamespace(id=USER)
        self.assertEqual(self.client.get("/api/edits/not-a-uuid").status_code, 404)
        self.assertEqual(self.client.get("/api/edits/00000000-0000-0000-0000-000000000000").status_code, 404)


class ConversationEndpointTests(EndpointTestCase):
    def test_delete_conversation_clears_only_the_owners_repository_history(self):
        run(seed_repo(self.store, repo_id="repo-gadgets", repo_name="gadgets"))
        run(seed_repo(self.store, repo_id="repo-other", user_id=OTHER, repo_name=REPO_NAME))
        for repo_id, user_id in ((REPO_ID, USER), (REPO_ID, USER), ("repo-gadgets", USER), ("repo-other", OTHER)):
            run(save_message(self.store, repo_id=repo_id, user_id=user_id, role="user", content=f"q for {repo_id}"))

        response = self.client.delete(f"/api/conversations/{REPO_NAME}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cleared": True})
        self.assertEqual(self.chat_rows(), [])
        self.assertEqual(len(self.chat_rows("repo-gadgets")), 1)
        self.assertEqual(len(self.chat_rows("repo-other", OTHER)), 1)
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}").json(), {"messages": [], "has_more": False, "next_before": None})

        self.assertEqual(self.client.delete("/api/conversations/unknown").status_code, 404)

    def test_history_pages_back_without_gaps_and_omits_cited_code(self):
        import json as _json
        rows = []
        for index in range(7):
            # Two pairs share a timestamp, so ordering must fall back to the id.
            created = f"2026-10-01T09:00:{index // 2:02d}+00:00"
            message_id = f"m{index:02d}"
            citations = [{"file_path": "src/a.ts", "start_line": 1, "end_line": 3, "content": "x" * 5000, "cited_in_answer": True}]
            run(self.store.execute(
                "INSERT INTO chat_messages (id, repo_id, user_id, role, content, citations, tool_calls, mode, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [message_id, REPO_ID, USER, "assistant" if index % 2 else "user", f"message {index}",
                 _json.dumps(citations if index % 2 else []), "[]", None, None, created],
            ))
            rows.append((created, message_id))
        expected = [message_id for _created, message_id in sorted(rows)]

        seen, before, pages = [], None, 0
        while True:
            params = {"limit": 3, **({"before": before} if before else {})}
            body = self.client.get(f"/api/conversations/{REPO_NAME}", params=params).json()
            pages += 1
            ids = [message["id"] for message in body["messages"]]
            self.assertEqual(ids, sorted(ids, key=lambda value: dict((m, c) for c, m in rows)[value] + value))
            seen = ids + seen
            for message in body["messages"]:
                for citation in message["citations"]:
                    self.assertNotIn("content", citation)
                    self.assertEqual(citation["file_path"], "src/a.ts")
            if not body["has_more"]:
                self.assertIsNone(body["next_before"])
                break
            before = body["next_before"]
        self.assertEqual(pages, 3)
        self.assertEqual(seen, expected)

        # Without a cursor the newest page comes back, as before.
        newest = self.client.get(f"/api/conversations/{REPO_NAME}", params={"limit": 2}).json()
        self.assertEqual([message["id"] for message in newest["messages"]], expected[-2:])
        self.assertTrue(newest["has_more"])


class GitHubFileEndpointTests(EndpointTestCase):
    def test_file_review_without_a_github_connection_reads_raw_bytes_and_blob_sha(self):
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=["src/windows.py"], ttl_seconds=600)
        with patch("api.github_router.fetch_raw_file", AsyncMock(return_value=WINDOWS)) as fetch, \
             patch("api.github_router.get_repo_details", AsyncMock()) as details, \
             patch("api.github_router.get_file_at_ref", AsyncMock()) as contents:
            response = self.client.get(
                "/api/github/file", params={"repo_name": REPO_NAME, "file_path": "src/windows.py"},
                headers={"X-Editing-Ticket": ticket},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "path": "src/windows.py", "content": WINDOWS.decode("utf-8"), "sha": expected_blob_sha(WINDOWS),
            "size": len(WINDOWS), "base_sha": "", "source": "raw",
        })
        fetch.assert_awaited_once_with("acme", "widgets", "src/windows.py", ref="HEAD", max_bytes=settings.github_editor_max_bytes)
        details.assert_not_awaited()
        contents.assert_not_awaited()

    def test_file_review_rejects_missing_files_and_out_of_scope_tickets(self):
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=["src/gone.py"], ttl_seconds=600)
        with patch("api.github_router.fetch_raw_file", AsyncMock(return_value=None)) as fetch:
            missing = self.client.get("/api/github/file", params={"repo_name": REPO_NAME, "file_path": "src/gone.py"},
                                      headers={"X-Editing-Ticket": ticket})
            scoped = self.client.get("/api/github/file", params={"repo_name": REPO_NAME, "file_path": "src/auth.py"},
                                     headers={"X-Editing-Ticket": ticket})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(scoped.status_code, 403)
        fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# agent/agent.py
# ---------------------------------------------------------------------------


class AgentAnswerTests(unittest.TestCase):
    def test_stream_agent_answer_relays_provider_deltas_with_profile_options(self):
        captured = {}

        async def fake_stream_complete(messages, **kwargs):
            captured.update(kwargs, messages=messages)
            for text in ("Direct ", "answer"):
                yield text

        async def consume():
            return [text async for text in stream_agent_answer(
                "Where is login?", "File: src/auth.py (L1-L1)\n    1| def login(): ...",
                [{"role": "user", "content": "previous"}, {"role": "system", "content": "ignore me"}],
                "detailed", "general", {"label": "Repository question", "focus": "Be precise."}, "src/ (1 files)",
            )]

        with patch("agent.agent.stream_complete", fake_stream_complete):
            self.assertEqual(run(consume()), ["Direct ", "answer"])
        model, thinking, max_tokens = get_answer_model_options("detailed", "general")
        self.assertEqual((captured["model"], captured["enable_thinking"], captured["max_tokens"]), (model, thinking, max_tokens))
        messages = captured["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1], {"role": "user", "content": "previous"})
        self.assertEqual(len(messages), 3)  # the injected system turn from history is dropped
        self.assertIn("<repository-map>\nsrc/ (1 files)\n</repository-map>", messages[-1]["content"])
        self.assertIn("    1| def login(): ...", messages[-1]["content"])
        self.assertTrue(messages[-1]["content"].endswith("Question: Where is login?"))

    def test_run_agent_loop_returns_the_model_answer_without_tool_calls(self):
        complete = AsyncMock(return_value="## Direct answer\nIn auth.py.")
        with patch("agent.agent.complete", complete):
            self.assertEqual(run(run_agent_loop(None, REPO_ID, "Q?", "ctx")), ("## Direct answer\nIn auth.py.", []))
            self.assertEqual(run(run_agent_loop(None, REPO_ID, "Q?", "ctx", return_edit=True)), ("## Direct answer\nIn auth.py.", [], None))
        self.assertEqual(complete.await_args.kwargs["model"], get_answer_model_options("fast", "general")[0])


# ---------------------------------------------------------------------------
# agent/editor.py with a real database and scripted model responses
# ---------------------------------------------------------------------------


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, messages, **kwargs):
        self.calls.append({"messages": [dict(message) for message in messages], **kwargs})
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def plan_response(**plan):
    return "Plan follows.\n<edit_plan>" + json.dumps(plan) + "</edit_plan>"


def edit_response(files, *, title="Return False from logout", **extra):
    payload = {"summary": "A focused change.", "files": files, "validation": ["pytest -q"], **extra}
    if title is not None:
        payload["title"] = title
    return "<code_edit>\n" + json.dumps(payload) + "\n</code_edit>"


LOGOUT_FIX = {
    "file_path": "src/auth.py", "action": "modify", "summary": "Return False on logout",
    "changes": [{"old": "def logout(user):\n    return True", "new": "def logout(user):\n    return False", "reason": "Requested"}],
}


class CodeEditorTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(seed_repo(self.store))
        self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        run(seed_files(self.store, {
            "src/auth.py": AUTH, "src/session.py": SESSION, "src/api.py": API,
            "src/windows.py": WINDOWS.decode().replace("\r\n", "\n"),
        }))
        self.raw = {
            "src/auth.py": AUTH.encode(), "src/session.py": SESSION.encode(), "src/api.py": API.encode(),
            "src/windows.py": WINDOWS,
        }
        self.fetch_calls = []
        self.stages = []

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        self.fetch_calls.append((owner, repo, path, ref, max_bytes))
        value = self.raw.get(path)
        if isinstance(value, BaseException):
            raise value
        return value

    def edit(self, question, evidence, responses, *, issue=None):
        async def emit(stage, detail):
            self.stages.append(stage)

        request = EditRequest(
            store=self.store, repo=self.repo, user_id=USER, question=question, evidence_chunks=list(evidence),
            evidence_plan=build_evidence_plan(question, "editing"), repo_map_text="src/ (4 files)", emit=emit,
        )
        model = ScriptedModel(responses)
        fetch_issue = AsyncMock(return_value=issue)
        stall = {"max": 0.0, "last": 0.0}

        async def edit_while_measuring_the_event_loop():
            # Every other request shares this loop: a long synchronous step in
            # the editor freezes all of them.
            async def ticker():
                while True:
                    await asyncio.sleep(0.01)
                    now = time.perf_counter()
                    stall["max"] = max(stall["max"], now - stall["last"])
                    stall["last"] = now

            stall["last"] = time.perf_counter()
            watcher = asyncio.create_task(ticker())
            try:
                return await run_code_edit(request)
            finally:
                # A stall at the very end is never seen by the ticker itself.
                stall["max"] = max(stall["max"], time.perf_counter() - stall["last"])
                watcher.cancel()

        with patch("agent.editor.complete", model), \
             patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", fetch_issue):
            markdown, tool_calls, proposal = run(edit_while_measuring_the_event_loop())
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model,
                               fetch_issue=fetch_issue, max_loop_stall=stall["max"])

    def test_explicit_path_request_skips_planning_and_pins_the_blob_sha(self):
        result = self.edit(
            "Fix src/auth.py so logout returns False", [AUTH_CHUNK, SESSION_CHUNK, API_CHUNK],
            [edit_response([LOGOUT_FIX])],
        )
        self.assertEqual(len(result.model.calls), 1)
        self.assertTrue(result.model.calls[0]["structured_output"])
        self.assertEqual(self.stages, ["reading", "editing", "validating"])
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn(f"===== FILE: src/auth.py =====\n{AUTH}\n===== END FILE: src/auth.py =====", prompt)
        self.assertNotIn("===== FILE: src/session.py", prompt)

        proposal = result.proposal
        self.assertEqual(len(proposal["files"]), 1)
        item = proposal["files"][0]
        self.assertEqual((item["file_path"], item["action"], item["source"], item["language"]), ("src/auth.py", "modify", "github", "python"))
        self.assertEqual(item["before"], AUTH)
        self.assertEqual(item["after"], AUTH.replace("    return True", "    return False"))
        self.assertEqual(item["base_blob_sha"], expected_blob_sha(AUTH.encode()))
        self.assertEqual(item["base_blob_sha"], git_blob_sha(AUTH.encode()))
        self.assertEqual((item["additions"], item["deletions"]), (1, 1))
        self.assertEqual(proposal["stats"], {"files": 1, "additions": 1, "deletions": 1})
        self.assertEqual(proposal["title"], "Return False from logout")
        self.assertEqual(proposal["indexed_commit"], COMMIT)
        self.assertEqual(proposal["validation"], ["pytest -q"])
        self.assertEqual(len(proposal["id"]), 36)
        self.assertEqual(self.fetch_calls[0][:4], ("acme", "widgets", "src/auth.py", "HEAD"))
        self.assertEqual([call["tool"] for call in result.tool_calls], ["plan_change", "validate_patch"])
        self.assertIn("**Update `src/auth.py`** (+1 −1)", result.markdown)

    def test_planned_change_modifies_two_files_traces_callers_and_creates_one(self):
        test_content = "from src.auth import logout\n\n\ndef test_logout_clears_session():\n    assert logout(object()) is True"
        session_new = "def check(user):\n    return user is not None\n\n\ndef clear(user):\n    user.session = None"
        responses = [
            plan_response(
                files_to_modify=["src/auth.py", "src/session.py"], files_to_create=["tests/test_logout.py"],
                symbols_to_trace=["check"], approach="Clear the session on logout and cover it with a test.",
            ),
            edit_response([
                {"file_path": "src/auth.py", "action": "modify", "summary": "Clear the session",
                 "changes": [{"old": "def logout(user):\n    return True", "new": "def logout(user):\n    clear(user)\n    return True"}]},
                {"file_path": "src/session.py", "action": "modify", "summary": "Add clear()",
                 "changes": [{"old": "def check(user):\n    return user is not None", "new": session_new}]},
                {"file_path": "tests/test_logout.py", "action": "create", "summary": "Regression test", "content": test_content},
            ], title="Clear the session on logout"),
        ]
        result = self.edit(
            "Make logout clear the session and add a regression test",
            [AUTH_CHUNK, SESSION_CHUNK, API_CHUNK], responses,
        )
        self.assertEqual(len(result.model.calls), 2)
        self.assertFalse(result.model.calls[0].get("structured_output", False))
        self.assertIn("<edit_plan>", result.model.calls[0]["messages"][0]["content"])
        self.assertTrue(result.model.calls[1]["structured_output"])
        self.assertEqual(self.stages, ["planning", "tracing", "reading", "editing", "validating"])

        prompt = result.model.calls[1]["messages"][1]["content"]
        self.assertIn("===== FILE: src/auth.py =====", prompt)
        self.assertIn("===== FILE: src/session.py =====", prompt)
        self.assertIn("New files you may create: tests/test_logout.py", prompt)
        self.assertIn("Planned approach:\nClear the session on logout and cover it with a test.", prompt)
        # Symbol tracing found the real caller through FTS, excluding edited files.
        self.assertIn("--- src/api.py (L1-L6, read-only usage) ---", prompt)

        proposal = result.proposal
        self.assertEqual(
            [(item["file_path"], item["action"]) for item in proposal["files"]],
            [("src/auth.py", "modify"), ("src/session.py", "modify"), ("tests/test_logout.py", "create")],
        )
        auth, session, created = proposal["files"]
        self.assertEqual(auth["after"], AUTH.replace("    return True", "    clear(user)\n    return True"))
        self.assertEqual(auth["base_blob_sha"], expected_blob_sha(AUTH.encode()))
        self.assertEqual((auth["additions"], auth["deletions"]), (1, 0))
        self.assertEqual(session["before"], SESSION)
        self.assertEqual(session["after"], session_new + "\n")
        self.assertEqual(session["base_blob_sha"], expected_blob_sha(SESSION.encode()))
        self.assertEqual((session["additions"], session["deletions"]), (4, 0))
        self.assertEqual(created["before"], "")
        self.assertEqual(created["after"], test_content + "\n")
        self.assertIsNone(created["base_blob_sha"])
        self.assertEqual((created["additions"], created["deletions"], created["source"]), (5, 0, "generated"))
        self.assertEqual(proposal["stats"], {"files": 3, "additions": 10, "deletions": 0})
        self.assertEqual([call["tool"] for call in result.tool_calls], ["plan_change", "trace_symbols", "validate_patch"])
        self.assertEqual(result.tool_calls[0]["input"], {"files": ["src/auth.py", "src/session.py"], "new_files": ["tests/test_logout.py"]})
        # The new path was confirmed absent on GitHub before it was accepted.
        self.assertIn(("acme", "widgets", "tests/test_logout.py", "HEAD", 4_000_000), self.fetch_calls)

    def test_crlf_file_keeps_its_line_endings_and_raw_blob_sha(self):
        result = self.edit(
            "Fix src/windows.py so greet says hello", [chunk("src/windows.py", 1, "def greet():\n    return 'hi'")],
            [edit_response([{"file_path": "src/windows.py", "action": "modify", "summary": "Say hello",
                             "changes": [{"old": "    return 'hi'", "new": "    return 'hello'"}]}])],
        )
        item = result.proposal["files"][0]
        self.assertEqual(item["before"], "def greet():\r\n    return 'hi'\r\n")
        self.assertEqual(item["after"], "def greet():\r\n    return 'hello'\r\n")
        self.assertEqual(item["base_blob_sha"], expected_blob_sha(WINDOWS))
        self.assertNotEqual(item["base_blob_sha"], expected_blob_sha(WINDOWS.replace(b"\r\n", b"\n")))
        self.assertEqual((item["additions"], item["deletions"]), (1, 1))

    def test_repair_round_receives_the_validator_error_and_fixes_the_hunk(self):
        bad = edit_response([{**LOGOUT_FIX, "changes": [{"old": "def logout(user):\n    return Truee", "new": "x"}]}])
        good = edit_response([LOGOUT_FIX])
        result = self.edit("Fix src/auth.py so logout returns False", [AUTH_CHUNK], [bad, good])
        self.assertEqual(len(result.model.calls), 2)
        repair_messages = result.model.calls[1]["messages"]
        self.assertEqual(len(repair_messages), 4)
        self.assertEqual(repair_messages[2], {"role": "assistant", "content": bad})
        self.assertIn("rejected by the exact-match validator", repair_messages[3]["content"])
        self.assertIn("src/auth.py: Hunk 1 was not found in the current file.", repair_messages[3]["content"])
        self.assertEqual(self.stages, ["reading", "editing", "validating", "repairing", "validating"])
        self.assertEqual(result.proposal["files"][0]["after"], AUTH.replace("    return True", "    return False"))

    def test_second_invalid_patch_becomes_a_helpful_message(self):
        bad = edit_response([{**LOGOUT_FIX, "changes": [{"old": "nonexistent text", "new": "x"}]}])
        result = self.edit("Fix src/auth.py so logout returns False", [AUTH_CHUNK], [bad, bad])
        self.assertIsNone(result.proposal)
        self.assertTrue(result.markdown.startswith("## I couldn't produce a safe patch"))
        self.assertIn("Hunk 1 was not found in the current file.", result.markdown)
        self.assertIn("No files were changed.", result.markdown)
        self.assertEqual(len(result.model.calls), 2)

    def test_creating_an_existing_path_is_rejected(self):
        # Indexed path: rejected, then repaired into a plain modification.
        first = edit_response([LOGOUT_FIX, {"file_path": "src/session.py", "action": "create", "content": "x = 1\n"}])
        result = self.edit("Fix src/auth.py so logout returns False", [AUTH_CHUNK], [first, edit_response([LOGOUT_FIX])])
        self.assertIn(
            "src/session.py already exists; modify it with exact hunks instead of creating it.",
            result.model.calls[1]["messages"][-1]["content"],
        )
        self.assertEqual([item["file_path"] for item in result.proposal["files"]], ["src/auth.py"])

        # Present on GitHub but not in the index: still rejected.
        self.raw["src/extra.py"] = b"x = 1\n"
        self.fetch_calls.clear()
        create_extra = edit_response([LOGOUT_FIX, {"file_path": "src/extra.py", "action": "create", "content": "y = 2\n"}])
        result = self.edit("Fix src/auth.py so logout returns False", [AUTH_CHUNK], [create_extra, create_extra])
        self.assertIsNone(result.proposal)
        self.assertIn("src/extra.py already exists", result.markdown)
        self.assertIn(("acme", "widgets", "src/extra.py", "HEAD", 4_000_000), self.fetch_calls)

    def test_more_than_three_new_files_are_rejected(self):
        planned = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
        creates = [{"file_path": path, "action": "create", "content": "assert True\n"} for path in ["tests/test_unplanned.py", *planned]]
        responses = [
            plan_response(files_to_modify=["src/auth.py"], files_to_create=planned, symbols_to_trace=[], approach="Add tests."),
            edit_response([LOGOUT_FIX, *creates]),
            edit_response([LOGOUT_FIX, *creates]),
        ]
        result = self.edit("Make logout return False and add tests", [AUTH_CHUNK], responses)
        self.assertIsNone(result.proposal)
        self.assertIn("At most 3 new files may be created.", result.markdown)

    def test_unreadable_github_file_falls_back_to_chunk_reconstruction(self):
        legacy_chunks = [
            chunk("src/legacy.py", 1, "def a():\n    return 1"),
            chunk("src/legacy.py", 5, "def b():\n    return 2"),
        ]
        run(seed_files(self.store, {}, extra_chunks=legacy_chunks))
        reconstructed = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        fix = edit_response([{"file_path": "src/legacy.py", "action": "modify", "summary": "Return 3",
                              "changes": [{"old": "def b():\n    return 2", "new": "def b():\n    return 3"}]}])
        for failure in (GitHubAPIError("GitHub could not return src/legacy.py (HTTP 500)."), RuntimeError("connection reset"), None):
            with self.subTest(failure=repr(failure)):
                self.raw["src/legacy.py"] = failure
                self.stages.clear()
                result = self.edit("Fix src/legacy.py so b returns 3", legacy_chunks, [fix])
                item = result.proposal["files"][0]
                self.assertEqual(item["before"], reconstructed)
                self.assertEqual(item["after"], reconstructed.replace("return 2", "return 3"))
                self.assertIsNone(item["base_blob_sha"])
                self.assertEqual(item["source"], "index")

    def test_issue_url_adds_acceptance_criteria_and_fixes_reference(self):
        issue = {
            "number": 42, "title": "Logout keeps the session alive", "body": "Logging out must return False.",
            "state": "open", "labels": ["bug"], "html_url": "https://github.com/acme/widgets/issues/42",
        }
        responses = [
            plan_response(files_to_modify=["src/auth.py"], files_to_create=[], symbols_to_trace=[], approach="Return False."),
            edit_response([LOGOUT_FIX], pr_body="## Summary\nLogout now returns False."),
        ]
        result = self.edit("Please resolve https://github.com/acme/widgets/issues/42", [AUTH_CHUNK], responses, issue=issue)
        result.fetch_issue.assert_awaited_once_with("acme", "widgets", 42, access_token=None)
        criteria = "Acceptance criteria from the referenced issue:\nGitHub issue #42: Logout keeps the session alive"
        self.assertIn(criteria, result.model.calls[0]["messages"][1]["content"])
        self.assertIn("Labels: bug", result.model.calls[0]["messages"][1]["content"])
        self.assertIn(criteria, result.model.calls[1]["messages"][1]["content"])
        self.assertEqual(result.proposal["pr_body"], "Fixes #42\n\n## Summary\nLogout now returns False.")
        self.assertEqual(result.proposal["issue_reference"], "42")
        self.assertEqual(result.tool_calls[0]["tool"], "read_github_issue")

    def test_issue_link_is_exact_and_qualified_for_another_repository(self):
        cases = (
            # "#42" in the body must not count as a reference to issue #4.
            ("Fix #4 so logout returns False", "## Summary\nSee #42 for context.", ("acme", "widgets", 4),
             "Fixes #4\n\n## Summary\nSee #42 for context."),
            # GitHub resolves a bare "#5" against the PR's repository, which
            # would close an unrelated issue there on merge.
            ("Resolve https://github.com/other-org/upstream/issues/5 here", "## Summary\nPorted fix.",
             ("other-org", "upstream", 5), "Fixes other-org/upstream#5\n\n## Summary\nPorted fix."),
            # A body that already links the same-repository issue is kept.
            ("Fix #4 so logout returns False", "Closes #4.\n\n## Summary\nDone.", ("acme", "widgets", 4),
             "Closes #4.\n\n## Summary\nDone."),
        )
        for question, body, issue_call, expected in cases:
            with self.subTest(question=question, body=body):
                responses = [
                    plan_response(files_to_modify=["src/auth.py"], files_to_create=[], symbols_to_trace=[], approach=""),
                    edit_response([LOGOUT_FIX], pr_body=body),
                ]
                result = self.edit(question, [AUTH_CHUNK], responses, issue=None)
                result.fetch_issue.assert_awaited_once_with(*issue_call, access_token=None)
                self.assertEqual(result.proposal["pr_body"], expected)
                self.assertEqual(result.proposal["issue_reference"], str(issue_call[2]))

    def test_issue_number_uses_the_indexed_repository_even_when_unreadable(self):
        responses = [
            plan_response(files_to_modify=["src/auth.py"], files_to_create=[], symbols_to_trace=[], approach=""),
            edit_response([LOGOUT_FIX], title=None, pr_body="## Summary\nDone."),
        ]
        result = self.edit("Fix #7 so logout returns False", [AUTH_CHUNK], responses, issue=None)
        result.fetch_issue.assert_awaited_once_with("acme", "widgets", 7, access_token=None)
        self.assertNotIn("Acceptance criteria", result.model.calls[0]["messages"][1]["content"])
        self.assertEqual(result.proposal["pr_body"], "Fixes #7\n\n## Summary\nDone.")
        self.assertEqual(result.proposal["title"], "Fix #7")

    def test_no_candidate_files_returns_guidance_without_a_proposal(self):
        result = self.edit("Make the docs friendlier", [chunk("README.md", 1, "# Widgets")], [])
        self.assertIsNone(result.proposal)
        self.assertIn("I could not find the code this change applies to", result.markdown)
        self.assertEqual(result.model.calls, [])
        self.assertEqual(self.fetch_calls, [])

    def test_large_file_view_marks_omissions_but_validates_against_the_full_file(self):
        big = "".join(f"value_{index:04d} = compute({index})  # keep this line distinct\n" for index in range(1, 401))
        self.raw["src/big.py"] = big.encode()
        evidence = [chunk("src/big.py", 200, "\n".join(big.split("\n")[199:210]))]
        run(seed_files(self.store, {}, extra_chunks=evidence))
        fix = edit_response([{"file_path": "src/big.py", "action": "modify", "summary": "Use compute_v2",
                              "changes": [{"old": "value_0205 = compute(205)", "new": "value_0205 = compute_v2(205)"},
                                          {"old": "value_0005 = compute(5)", "new": "value_0005 = compute_v2(5)"}]}])
        # The whole editing context is smaller than the file, so it is shown
        # as the block most relevant to the request (not the file head).
        with patch.object(settings, "editing_context_characters", 4_000):
            result = self.edit("Fix src/big.py so value_0205 uses compute_v2", evidence, [fix])
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("[... lines 1-200 omitted ...]", prompt)
        self.assertIn("[... lines 241-400 omitted ...]", prompt)
        self.assertIn("value_0201 = compute(201)", prompt)
        self.assertIn("value_0240 = compute(240)", prompt)
        self.assertNotIn("value_0200 = compute(200)", prompt)
        self.assertNotIn("value_0241 = compute(241)", prompt)
        self.assertNotIn("value_0005 = compute(5)", prompt)
        item = result.proposal["files"][0]
        self.assertEqual(item["before"], big)
        self.assertEqual(
            item["after"],
            big.replace("value_0205 = compute(205)", "value_0205 = compute_v2(205)").replace(
                "value_0005 = compute(5)", "value_0005 = compute_v2(5)"),
        )
        self.assertEqual(item["base_blob_sha"], expected_blob_sha(big.encode()))
        self.assertEqual((item["additions"], item["deletions"]), (2, 2))

    def test_long_file_statistics_do_not_stall_the_event_loop(self):
        # Blank lines and closing braces repeat thousands of times in a long
        # file; aligning everything between two distant hunks line by line
        # is quadratic and used to run synchronously on the event loop.
        lines = ["    }" if index % 3 == 0 else "" if index % 3 == 1 else f"    value_{index} = compute({index})"
                 for index in range(15_000)]
        big = "\n".join(lines) + "\n"
        self.raw["src/huge.py"] = big.encode()
        evidence = [chunk("src/huge.py", 3, "    value_2 = compute(2)")]
        run(seed_files(self.store, {}, extra_chunks=evidence))
        fix = edit_response([{"file_path": "src/huge.py", "action": "modify", "summary": "Use compute_v2", "changes": [
            {"old": "    value_2 = compute(2)", "new": "    value_2 = compute_v2(2)"},
            {"old": "    value_14999 = compute(14999)", "new": "    value_14999 = compute_v2(14999)\n    checked = True"},
        ]}])
        result = self.edit("Fix src/huge.py so values use compute_v2", evidence, [fix])
        item = result.proposal["files"][0]
        self.assertEqual(item["after"], big.replace("compute(2)\n", "compute_v2(2)\n", 1).replace(
            "    value_14999 = compute(14999)", "    value_14999 = compute_v2(14999)\n    checked = True"))
        self.assertEqual((item["additions"], item["deletions"]), (3, 2))
        self.assertEqual(result.proposal["stats"], {"files": 1, "additions": 3, "deletions": 2})
        self.assertLess(result.max_loop_stall, 1.0)

    def test_index_fallback_ignores_rows_staged_by_an_in_progress_refresh(self):
        # A refresh writes the new version of a changed file beside the old
        # one under a job-scoped id prefix; only the swap publishes it.
        published = [
            chunk("src/legacy.py", 1, "def a():\n    return 1"),
            chunk("src/legacy.py", 5, "def b():\n    return 2"),
        ]
        staged = [chunk("src/legacy.py", 1, "def a():\n    return 10\n\n\ndef b():\n    return 20\n\n\ndef c():\n    return 30",
                        chunk_id=f"{STAGING}src/legacy.py:1")]
        run(seed_files(self.store, {}, extra_chunks=[*published, *staged]))
        fix = edit_response([{"file_path": "src/legacy.py", "action": "modify", "summary": "Return 3",
                              "changes": [{"old": "def b():\n    return 2\n", "new": "def b():\n    return 3\n"}]}])
        self.raw["src/legacy.py"] = GitHubAPIError("GitHub could not return src/legacy.py (HTTP 503).")
        published_text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        for registered_when in ("before the request", "during the request"):
            with self.subTest(registered_when=registered_when):
                run(self.store.execute("DELETE FROM repo_index_runs WHERE repo_id = ?", [REPO_ID]))
                register = ("INSERT INTO repo_index_runs (repo_id, job_id, chunk_prefix, started_at) "
                            "VALUES (?, 'job-refresh', ?, '2026-10-01T00:00:00+00:00')")
                if registered_when == "before the request":
                    run(self.store.execute(register, [REPO_ID, STAGING]))
                    self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
                    self.assertEqual(self.repo["staging_prefix"], STAGING)
                else:
                    # The request started before the refresh registered its run.
                    self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
                    self.assertIsNone(self.repo["staging_prefix"])
                    run(self.store.execute(register, [REPO_ID, STAGING]))
                result = self.edit("Fix src/legacy.py so b returns 3", published, [fix])
                self.assertIsNotNone(result.proposal, result.markdown)
                item = result.proposal["files"][0]
                self.assertEqual(item["before"], published_text)
                self.assertEqual(item["after"], published_text.replace("return 2", "return 3"))
                self.assertEqual(item["source"], "index")
                self.assertNotIn("def c()", result.model.calls[0]["messages"][1]["content"])

    def test_malformed_model_fields_are_normalised_instead_of_crashing(self):
        # A plan whose lists arrive as a number, a bare string or a boolean.
        responses = [
            plan_response(files_to_modify=2, files_to_create="tests/test_logout.py", symbols_to_trace="check", approach="Return False."),
            edit_response([LOGOUT_FIX, {"file_path": "tests/test_logout.py", "action": "create", "content": "assert True\n"}],
                          validation="pytest tests/test_logout.py"),
        ]
        result = self.edit("Make logout return False and add a regression test", [AUTH_CHUNK, SESSION_CHUNK, API_CHUNK], responses)
        self.assertIsNotNone(result.proposal, result.markdown)
        self.assertEqual(result.tool_calls[0]["input"]["new_files"], ["tests/test_logout.py"])
        self.assertIn("tracing", self.stages)
        self.assertEqual(result.tool_calls[1]["input"], {"symbols": ["check"]})
        # One validation command, not one entry per character.
        self.assertEqual(result.proposal["validation"], ["pytest tests/test_logout.py"])

        # A code edit whose "files" is not a list is a validation failure that
        # earns the repair round, never an unhandled TypeError (HTTP 500).
        self.stages.clear()
        result = self.edit("Fix src/auth.py so logout returns False", [AUTH_CHUNK], [edit_response(True), edit_response([LOGOUT_FIX])])
        self.assertIsNotNone(result.proposal, result.markdown)
        self.assertIn("could not produce a safe change", result.model.calls[1]["messages"][-1]["content"])
        self.assertEqual(self.stages, ["reading", "editing", "validating", "repairing", "validating"])


# ---------------------------------------------------------------------------
# GitHub pull-request plumbing
# ---------------------------------------------------------------------------


def http_response(status, payload=None):
    response = MagicMock(status_code=status, headers={"content-type": "application/json; charset=utf-8"})
    response.json.return_value = payload if payload is not None else {}
    return response


class PushPayloadTests(unittest.TestCase):
    def test_new_file_needs_no_blob_sha_but_a_modified_file_does(self):
        payload = PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", files=[
            PushFileRequest(file_path="src/auth.py", new_content="x = 1\n", file_sha="abc123"),
            PushFileRequest(file_path="tests/test_logout.py", new_content="assert True\n", action="create"),
        ])
        self.assertEqual(_payload_files(payload), [
            {"file_path": "src/auth.py", "new_content": "x = 1\n", "expected_file_sha": "abc123", "action": "modify"},
            {"file_path": "tests/test_logout.py", "new_content": "assert True\n", "expected_file_sha": "", "action": "create"},
        ])
        for invalid, status in (
            (PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", files=[PushFileRequest(file_path="src/auth.py", new_content="x\n")]), 409),
            (PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", file_path="src/auth.py", new_content="x\n"), 409),
            (PushPRRequest(repo_name=REPO_NAME, edit_ticket="t", files=[
                PushFileRequest(file_path="a.py", new_content="x\n", action="create"),
                PushFileRequest(file_path="a.py", new_content="y\n", action="create"),
            ]), 422),
        ):
            with self.subTest(status=status), self.assertRaises(HTTPException) as context:
                _payload_files(invalid)
            self.assertEqual(context.exception.status_code, status)
        with self.assertRaises(ValidationError):
            PushFileRequest(file_path="a.py", new_content="x\n", action="delete")


class CommitPreconditionTests(unittest.TestCase):
    REF = http_response(200, {"object": {"sha": "base-commit"}})

    def test_create_is_refused_when_the_path_now_exists_upstream(self):
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=self.REF)), \
             patch("httpx.AsyncClient.post", AsyncMock()) as post, \
             patch("github.github_client.file_exists_at_ref", AsyncMock(return_value=True)) as exists:
            with self.assertRaises(GitHubConflictError) as context:
                run(create_branch_and_commit_files(
                    "token", "acme", "widgets", "main", "codebase-intel/add-test",
                    [{"file_path": "tests/test_logout.py", "new_content": "assert True\n", "action": "create"}], "Add test",
                ))
        self.assertIn("tests/test_logout.py now exists upstream", str(context.exception))
        exists.assert_awaited_once_with("token", "acme", "widgets", "tests/test_logout.py", "main")
        post.assert_not_awaited()

    def test_create_and_current_modify_commit_one_tree(self):
        posts = [http_response(201, {"sha": sha}) for sha in ("blob-1", "blob-2", "tree-1", "commit-1")] + [http_response(201)]
        listings = [
            http_response(200, {"tree": [{"path": "src", "type": "tree", "mode": "040000", "sha": "src-tree"}]}),
            http_response(200, {"tree": [{"path": "auth.py", "type": "blob", "mode": "100644", "sha": "reviewed"}]}),
        ]
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=[self.REF, http_response(200, {"tree": {"sha": "base-tree"}}), *listings])), \
             patch("httpx.AsyncClient.post", AsyncMock(side_effect=posts)) as post, \
             patch("github.github_client.file_exists_at_ref", AsyncMock(return_value=False)), \
             patch("github.github_client.get_file_at_ref", AsyncMock(return_value={"sha": "reviewed"})) as contents:
            result = run(create_branch_and_commit_files(
                "token", "acme", "widgets", "main", "codebase-intel/fix",
                [
                    {"file_path": "src/auth.py", "new_content": "x = 2\n", "expected_file_sha": "reviewed", "action": "modify"},
                    {"file_path": "tests/test_logout.py", "new_content": "assert True\n", "action": "create"},
                ],
                "Fix logout",
            ))
        self.assertEqual(result["commit_sha"], "commit-1")
        self.assertEqual(contents.await_args.kwargs, {"metadata_only": True})
        tree = post.await_args_list[2].kwargs["json"]
        self.assertEqual(tree["base_tree"], "base-tree")
        self.assertEqual([(entry["path"], entry["sha"]) for entry in tree["tree"]], [("src/auth.py", "blob-1"), ("tests/test_logout.py", "blob-2")])

    def test_modified_files_keep_their_git_mode(self):
        """An edited script must stay executable (and a symlink a symlink)."""
        trees = {
            "base-tree": [{"path": "scripts", "type": "tree", "mode": "040000", "sha": "scripts-tree"},
                          {"path": "README.md", "type": "blob", "mode": "100644", "sha": "r"}],
            "scripts-tree": [{"path": "deploy.sh", "type": "blob", "mode": "100755", "sha": "old-deploy"},
                             {"path": "current", "type": "blob", "mode": "120000", "sha": "old-link"}],
        }
        tree_reads = []

        async def get(url, **kwargs):
            if url.endswith("/git/ref/heads/main"):
                return self.REF
            if "/git/commits/" in url:
                return http_response(200, {"tree": {"sha": "base-tree"}})
            if "/git/trees/" in url:
                sha = url.rsplit("/", 1)[-1]
                tree_reads.append(sha)
                return http_response(200, {"sha": sha, "tree": trees[sha], "truncated": False})
            raise AssertionError(url)

        posts = [http_response(201, {"sha": sha}) for sha in ("blob-1", "blob-2", "blob-3", "blob-4", "tree-1", "commit-1")] + [http_response(201)]
        files = [
            {"file_path": "scripts/deploy.sh", "new_content": "#!/bin/sh\necho deploy\n", "expected_file_sha": "old-deploy", "action": "modify"},
            {"file_path": "scripts/current", "new_content": "releases/v2", "expected_file_sha": "old-link", "action": "modify"},
            {"file_path": "README.md", "new_content": "# Widgets\n", "expected_file_sha": "r", "action": "modify"},
            {"file_path": "scripts/new.sh", "new_content": "#!/bin/sh\n", "action": "create"},
        ]
        shas = {"scripts/deploy.sh": "old-deploy", "scripts/current": "old-link", "README.md": "r"}
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=get)), \
             patch("httpx.AsyncClient.post", AsyncMock(side_effect=posts)) as post, \
             patch("github.github_client.file_exists_at_ref", AsyncMock(return_value=False)), \
             patch("github.github_client.get_file_at_ref", AsyncMock(side_effect=lambda *a, **k: {"sha": shas[a[3]]})):
            run(create_branch_and_commit_files("token", "acme", "widgets", "main", "codebase-intel/deploy", files, "Deploy"))
        tree = post.await_args_list[4].kwargs["json"]["tree"]
        self.assertEqual(
            [(entry["path"], entry["mode"]) for entry in tree],
            [("scripts/deploy.sh", "100755"), ("scripts/current", "120000"), ("README.md", "100644"), ("scripts/new.sh", "100644")],
        )
        # Each directory listing is read once, however many files it holds.
        self.assertEqual(tree_reads, ["base-tree", "scripts-tree"])

    def test_modify_with_a_stale_blob_sha_is_refused(self):
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=self.REF)), \
             patch("httpx.AsyncClient.post", AsyncMock()) as post, \
             patch("github.github_client.get_file_at_ref", AsyncMock(return_value={"sha": "upstream-new"})) as contents:
            with self.assertRaises(GitHubConflictError) as context:
                run(create_branch_and_commit_files(
                    "token", "acme", "widgets", "main", "codebase-intel/fix",
                    [{"file_path": "src/auth.py", "new_content": "x\n", "expected_file_sha": "reviewed-old", "action": "modify"}],
                    "Fix",
                ))
        self.assertIn("changed while you were reviewing", str(context.exception))
        self.assertEqual(contents.await_args.args, ("token", "acme", "widgets", "src/auth.py", "main"))
        post.assert_not_awaited()

    def test_fork_sync_is_best_effort(self):
        cases = (
            (AsyncMock(return_value=http_response(200, {"merge_type": "fast-forward"})), True),
            (AsyncMock(return_value=http_response(409, {"message": "There are merge conflicts"})), False),
            (AsyncMock(return_value=http_response(422, {"message": "Not a fork"})), False),
            (AsyncMock(side_effect=httpx.ConnectError("connection refused")), False),
        )
        for post, expected in cases:
            with self.subTest(expected=expected), patch("httpx.AsyncClient.post", post):
                self.assertIs(run(sync_fork_branch("token", "octocat", "widgets", "main")), expected)
                self.assertEqual(post.await_args.args[0], "https://api.github.com/repos/octocat/widgets/merge-upstream")
                self.assertEqual(post.await_args.kwargs["json"], {"branch": "main"})

    def test_fetch_issue_degrades_to_none_on_unreadable_responses(self):
        # The issue is optional acceptance criteria: an unreadable answer from
        # GitHub must not turn the whole edit into an HTTP 500.
        not_json = http_response(200)
        not_json.json.side_effect = ValueError("Expecting value: line 1 column 1")
        cases = (
            not_json,
            http_response(200, ["not", "an", "issue"]),
            http_response(200, {"number": "three", "title": "Bad number"}),
            http_response(200, {"number": 3, "title": "A pull request", "pull_request": {"url": "x"}}),
            http_response(404, {"message": "Not Found"}),
        )
        for response in cases:
            with self.subTest(status=response.status_code), \
                 patch("httpx.AsyncClient.get", AsyncMock(return_value=response)):
                self.assertIsNone(run(fetch_issue("acme", "widgets", 3)))
        issue = http_response(200, {"number": 3, "title": "Logout bug", "body": None, "state": "open",
                                    "html_url": "https://github.com/acme/widgets/issues/3", "labels": [{"name": "bug"}, "stray"]})
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=issue)):
            self.assertEqual(run(fetch_issue("acme", "widgets", 3)), {
                "number": 3, "title": "Logout bug", "body": "", "state": "open",
                "html_url": "https://github.com/acme/widgets/issues/3", "labels": ["bug"],
            })

    def test_file_exists_at_ref_maps_contents_api_responses(self):
        cases = (
            (http_response(404, {"message": "Not Found"}), False),
            (http_response(200, {"type": "file", "sha": "abc", "size": 3}), True),
        )
        for response, expected in cases:
            with self.subTest(status=response.status_code):
                with patch("httpx.AsyncClient.get", AsyncMock(return_value=response)):
                    self.assertIs(run(file_exists_at_ref("token", "acme", "widgets", "src/a.py", "main")), expected)
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=http_response(401))):
            with self.assertRaises(GitHubAuthRequiredError):
                run(file_exists_at_ref("token", "acme", "widgets", "src/a.py", "main"))
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=http_response(500))):
            with self.assertRaises(GitHubAPIError):
                run(file_exists_at_ref("token", "acme", "widgets", "src/a.py", "main"))


class PushPullRequestRouterTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(seed_repo(self.store))
        for item in (
            patch.object(settings, "editing_ticket_secret", SECRET),
            patch.object(settings, "github_token_encryption_key", TOKEN_KEY),
            patch("api.github_router.get_turso_store", return_value=self.store),
            patch("api.github_router.get_repo_details", AsyncMock(return_value={"permissions": {"push": True}, "default_branch": "main"})),
        ):
            item.start()
            self.addCleanup(item.stop)
        run(self.store.execute(
            "INSERT INTO user_github_tokens (user_id, github_user_id, github_username, access_token, scope, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [USER, "1", "octocat", encrypt_token("gho_test_token", TOKEN_KEY), "public_repo", "2026-10-01T00:00:00+00:00"],
        ))

    def request(self, files, key):
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=[item.file_path for item in files], ttl_seconds=600)
        return PushPRRequest(repo_name=REPO_NAME, files=files, branch_name="codebase-intel/add-test", edit_ticket=ticket, idempotency_key=key)

    def test_single_new_file_push_checks_that_the_path_is_still_absent(self):
        request = self.request([PushFileRequest(file_path="tests/test_logout.py", new_content="assert True\n", action="create")], "single-create")
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=http_response(200, {"object": {"sha": "base"}, "tree": {"sha": "t"}}))), \
             patch("httpx.AsyncClient.post", AsyncMock(side_effect=AssertionError("no blob may be created"))) as post, \
             patch("github.github_client.file_exists_at_ref", AsyncMock(return_value=True)) as exists, \
             patch("api.github_router.open_pull_request", AsyncMock()) as open_pr:
            with self.assertRaises(HTTPException) as context:
                run(push_pr(request, SimpleNamespace(id=USER)))
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("now exists upstream", context.exception.detail)
        exists.assert_awaited_once_with("gho_test_token", "acme", "widgets", "tests/test_logout.py", "main")
        post.assert_not_awaited()
        open_pr.assert_not_awaited()
        operation = run(self.store.fetch_one("SELECT status FROM github_change_operations"))
        self.assertEqual(operation["status"], "failed")

    def test_mixed_push_forwards_actions_and_shas_to_the_atomic_commit(self):
        files = [
            PushFileRequest(file_path="src/auth.py", new_content="x = 2\n", file_sha="reviewed"),
            PushFileRequest(file_path="tests/test_logout.py", new_content="assert True\n", action="create"),
        ]
        commit = AsyncMock(return_value={"commit_sha": "c", "branch_name": "codebase-intel/add-test", "owner": "acme", "repo": "widgets"})
        open_pr = AsyncMock(return_value={"pr_url": "https://github.com/acme/widgets/pull/9", "pr_number": 9, "title": "t"})
        with patch("api.github_router.create_branch_and_commit_files", commit), \
             patch("api.github_router.open_pull_request", open_pr):
            result = run(push_pr(self.request(files, "mixed"), SimpleNamespace(id=USER)))
        self.assertTrue(result["success"])
        self.assertEqual(result["pr_number"], 9)
        sent = commit.await_args.args[5]
        self.assertEqual(
            [(item["file_path"], item["action"], item["expected_file_sha"]) for item in sent],
            [("src/auth.py", "modify", "reviewed"), ("tests/test_logout.py", "create", "")],
        )

    def fork_github(self, *, upstream_sha="reviewed-upstream"):
        """A small GitHub double for a contributor without push access.

        The reviewed blob SHA was read from upstream (acme/widgets). The
        reused fork (octocat/widgets) has a stale, diverged default branch
        that must never be read: the PR branch is built on the upstream head
        and its objects are created in the fork.
        """
        state = {"posts": []}

        async def post(url, **kwargs):
            state["posts"].append((url.removeprefix("https://api.github.com"), kwargs.get("json")))
            for suffix, sha in (("/git/blobs", "blob-1"), ("/git/trees", "tree-1"), ("/git/commits", "commit-1"), ("/git/refs", "")):
                if url.startswith("https://api.github.com/repos/octocat/widgets") and url.endswith(suffix):
                    return http_response(201, {"sha": sha})
            raise AssertionError(f"unexpected POST {url}")

        async def get(url, **kwargs):
            if url.endswith("/repos/acme/widgets/git/ref/heads/main"):
                return http_response(200, {"object": {"sha": "upstream-head"}})
            if "/repos/acme/widgets/git/commits/" in url:
                return http_response(200, {"tree": {"sha": "base-tree"}})
            if url.endswith("/repos/acme/widgets/git/trees/base-tree"):
                return http_response(200, {"tree": [{"path": "src", "type": "tree", "mode": "040000", "sha": "src-tree"}]})
            if url.endswith("/repos/acme/widgets/git/trees/src-tree"):
                return http_response(200, {"tree": [{"path": "auth.py", "type": "blob", "mode": "100644", "sha": upstream_sha}]})
            raise AssertionError(f"unexpected GET {url}")

        async def contents(token, owner, repo, path, ref, **kwargs):
            self.assertEqual((owner, repo, ref), ("acme", "widgets", "main"))
            return {"sha": upstream_sha}

        return state, post, get, contents

    def push_from_fork(self, request, state, post, get, contents):
        open_pr = AsyncMock(return_value={"pr_url": "https://github.com/acme/widgets/pull/12", "pr_number": 12, "title": "t"})
        fork = AsyncMock(return_value={"owner": {"login": "octocat"}, "name": "widgets", "default_branch": "main"})
        with patch("api.github_router.get_repo_details", AsyncMock(return_value={"permissions": {"push": False}, "default_branch": "main"})), \
             patch("api.github_router.fork_repo", fork), \
             patch("api.github_router.open_pull_request", open_pr), \
             patch("httpx.AsyncClient.post", AsyncMock(side_effect=post)), \
             patch("httpx.AsyncClient.get", AsyncMock(side_effect=get)), \
             patch("github.github_client.get_file_at_ref", contents):
            return run(push_pr(request, SimpleNamespace(id=USER))), open_pr, fork

    def test_fork_pr_is_built_on_the_upstream_head_not_the_fork_default_branch(self):
        state, post, get, contents = self.fork_github()
        files = [PushFileRequest(file_path="src/auth.py", new_content="x = 2\n", file_sha="reviewed-upstream")]
        result, open_pr, fork = self.push_from_fork(self.request(files, "fork-upstream"), state, post, get, contents)
        self.assertTrue(result["success"])
        self.assertTrue(result["is_fork"])
        self.assertEqual(result["target_repo"], "octocat/widgets")
        fork.assert_awaited_once()
        self.assertNotIn("/repos/octocat/widgets/merge-upstream", [url for url, _ in state["posts"]])
        commit = next(body for url, body in state["posts"] if url.endswith("/git/commits"))
        self.assertEqual(commit["parents"], ["upstream-head"])
        self.assertEqual(open_pr.await_args.args[1:5], ("acme", "widgets", "octocat:codebase-intel/add-test", "main"))

    def test_fork_push_still_refuses_a_revision_that_changed_upstream(self):
        state, post, get, contents = self.fork_github(upstream_sha="changed-upstream")
        files = [PushFileRequest(file_path="src/auth.py", new_content="x = 2\n", file_sha="reviewed-upstream")]
        with self.assertRaises(HTTPException) as context:
            self.push_from_fork(self.request(files, "fork-stale"), state, post, get, contents)
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("changed while you were reviewing", context.exception.detail)
        self.assertEqual(state["posts"], [])

    def test_invalid_branch_name_is_rejected_before_any_github_side_effect(self):
        files = [PushFileRequest(file_path="src/auth.py", new_content="x = 2\n", file_sha="reviewed")]
        ticket = create_edit_ticket(SECRET, user_id=USER, repo_name=REPO_NAME, file_path=["src/auth.py"], ttl_seconds=600)
        request = PushPRRequest(repo_name=REPO_NAME, files=files, branch_name="feature/no-prefix", edit_ticket=ticket,
                                idempotency_key="bad-branch")
        details = AsyncMock(return_value={"permissions": {"push": False}, "default_branch": "main"})
        fork = AsyncMock(return_value={"owner": {"login": "octocat"}, "name": "widgets", "default_branch": "main"})
        commit = AsyncMock()
        with patch("api.github_router.get_repo_details", details), \
             patch("api.github_router.fork_repo", fork), \
             patch("api.github_router.create_branch_and_commit_files", commit), \
             patch("api.github_router.create_branch_and_commit", commit):
            with self.assertRaises(HTTPException) as context:
                run(push_pr(request, SimpleNamespace(id=USER)))
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("codebase-intel/", context.exception.detail)
        fork.assert_not_awaited()
        details.assert_not_awaited()
        commit.assert_not_awaited()
        self.assertIsNone(run(self.store.fetch_one("SELECT status FROM github_change_operations")))


if __name__ == "__main__":
    unittest.main()
