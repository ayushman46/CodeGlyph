"""User-experience contract of the repository API.

* Starter questions name real, indexed files and models of the repository,
  are varied (overview / where / how / change) and never mention a file that
  is missing from the published index.
* The status payload explains each indexing state in plain language: label,
  step, progress, a time estimate when one is honest, what already works and
  what to do next. Existing fields keep their shape.
* Error messages say what happened and what to do, without settings names,
  SQL or stack traces.

Every test is offline and uses a temporary embedded libSQL database.
"""

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import api.explore_router as explore
import api.repos_router as repos
from api import ingest_router
from api.explore_router import (
    classify_repository_paths,
    rank_models,
    suggest_question_details,
    suggest_questions,
)
from api.repository_state import (
    describe_index_state,
    friendly_stored_message,
    present_repository,
    raise_storage_error,
)
from config import settings
from database import (
    DatabaseConfigurationError,
    DatabaseStatementError,
    TransientDatabaseError,
    TursoStore,
    apply_migrations,
)
from ingest.pipeline import IngestionConflictError
from retrieval.retriever import FILE_PATH_PATTERN

USER = "ux-user"
URL = "https://github.com/acme/scheduler.git"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
JARGON = ("Turso", "TURSO_", "AUTO_MIGRATE", "EMBEDDING_DIMENSION", "SQL", "Traceback", "backend")


def ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


PRISMA_SCHEMA = """generator client {
  provider = "prisma-client-js"
}

model User {
  id         Int         @id
  bookings   Booking[]
  eventTypes EventType[]
  teams      Team[]
  hosts      Host[]
}

model Team {
  id         Int         @id
  members    User[]
  eventTypes EventType[]
}

model EventType {
  id       Int       @id
  owner    User      @relation(fields: [ownerId], references: [id])
  ownerId  Int
  team     Team?
  bookings Booking[]
  hosts    Host[]
}

model Booking {
  id          Int        @id
  user        User
  eventType   EventType
  attendees   Attendee[]
}

model Attendee {
  id      Int     @id
  booking Booking
}

model Host {
  id        Int       @id
  eventType EventType
  user      User
}
"""

# Indexed (manifest + chunks) files of a small scheduling monorepo.
INDEXED_FILES = {
    "README.md": "# Scheduler\nOpen scheduling infrastructure.\n",
    "package.json": '{"name": "scheduler"}\n',
    "playwright.config.ts": "export default {};\n",
    "packages/prisma/schema.prisma": PRISMA_SCHEMA,
    "packages/platform/examples/base/prisma/schema.prisma": "model Example {\n  id Int @id\n}\n",
    "packages/trpc/server/routers/_app.ts": "export const appRouter = router({});\n",
    "packages/trpc/server/routers/viewer/bookings/_router.tsx": "export const bookingsRouter = router({});\n",
    "packages/trpc/server/routers/viewer/eventTypes/_router.ts": "export const eventTypesRouter = router({});\n",
    "packages/features/auth/lib/next-auth-options.ts": "export const AUTH_OPTIONS = {};\n",
    "packages/features/bookings/lib/create-booking.ts": "export function createBooking() {}\n",
    "packages/ui/button.tsx": "export const Button = () => null;\n",
    "packages/ui/card.tsx": "export const Card = () => null;\n",
    "apps/web/pages/index.tsx": "export default function Home() {}\n",
    "apps/web/pages/bookings.tsx": "export default function Bookings() {}\n",
    "apps/api/src/main.ts": "bootstrap();\n",
}
# In the manifest but never published (e.g. a failed batch): never suggested.
MANIFEST_ONLY_FILES = {"packages/auth/next-auth-options.ts": "export const AUTH = {};\n"}


def chunk_rows(repo_id: str, path: str, text: str, lines_per_chunk: int = 200) -> list[dict]:
    lines = text.rstrip("\n").split("\n")
    rows = []
    for offset in range(0, len(lines), lines_per_chunk):
        window = lines[offset: offset + lines_per_chunk]
        rows.append({
            "id": f"{uuid.uuid4()}", "repo_id": repo_id, "file_path": path, "start_line": offset + 1,
            "end_line": offset + len(window), "language": path.rsplit(".", 1)[-1], "symbols": [],
            "content": "\n".join(window), "embedding": None,
        })
    return rows


class StoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'ux.db'}", "local-test-token")
        await apply_migrations(self.store)
        self.user = SimpleNamespace(id=USER)

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    async def add_repo(self, name="scheduler", status="ready", files=None, manifest_only=None, lines_per_chunk=200):
        repo_id = f"repo-{uuid.uuid4()}"
        files = INDEXED_FILES if files is None else files
        manifest_only = MANIFEST_ONLY_FILES if manifest_only is None else manifest_only
        rows = [row for path, text in files.items() for row in chunk_rows(repo_id, path, text, lines_per_chunk)]
        now = datetime.now(UTC).isoformat()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [repo_id, USER, name, URL, status, len(rows), now, now],
        )
        if rows:
            await self.store.insert_chunks(rows)
        for path, text in {**files, **manifest_only}.items():
            await self.store.execute(
                "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
                [repo_id, path, "h", len(text.encode()), now],
            )
        return repo_id

    def patched(self):
        return (
            patch.object(explore, "get_turso_store", return_value=self.store),
            patch.object(explore, "assert_turso_schema", new=AsyncMock()),
            patch.object(explore, "fetch_raw_file", new=AsyncMock(return_value=None)),
            patch.object(repos, "get_turso_store", return_value=self.store),
            patch.object(repos, "assert_turso_schema", new=AsyncMock()),
        )

    async def overview(self, name="scheduler"):
        p = self.patched()
        with p[0], p[1]:
            return await explore.repository_overview(name, self.user)

    async def indexed_paths(self, repo_id):
        rows = await self.store.fetch_all("SELECT DISTINCT file_path FROM chunks WHERE repo_id = ?", [repo_id])
        return {row["file_path"] for row in rows}


# ---------------------------------------------------------------------------
# Starter questions
# ---------------------------------------------------------------------------


class SuggestedQuestionTests(StoreCase):
    async def test_questions_name_real_files_and_models_and_vary_in_kind(self):
        repo_id = await self.add_repo()
        overview = await self.overview()
        questions = overview["suggested_questions"]
        details = overview["suggested_question_details"]
        self.assertTrue(4 <= len(questions) <= 6, questions)
        self.assertEqual(questions, [item["question"] for item in details])
        self.assertTrue(all(isinstance(question, str) for question in questions))
        self.assertTrue({"overview", "where", "how", "change"} <= {item["kind"] for item in details}, details)

        text = "\n".join(questions)
        self.assertIn("according to README.md", questions[0])
        self.assertIn("across packages/ and apps/", questions[0])
        # The most central domain model (not the generic User/Team) with its file.
        self.assertIn(
            "Where is the EventType model defined in packages/prisma/schema.prisma, and which models reference it?",
            questions,
        )
        # The API question is about a router for another domain model, and the
        # change question follows that model without pinning one file.
        self.assertIn("packages/trpc/server/routers/viewer/bookings/_router.tsx", text)
        self.assertIn("Which files would I need to change to add a new field to the Booking model?", questions)
        self.assertIn("packages/features/auth/lib/next-auth-options.ts", text)
        self.assertIn("playwright.config.ts", text)
        self.assertNotIn("examples", text)

    async def test_every_named_file_is_in_the_published_index(self):
        repo_id = await self.add_repo()
        overview = await self.overview()
        indexed = await self.indexed_paths(repo_id)
        for item in overview["suggested_question_details"]:
            for path in item["paths"]:
                self.assertIn(path, indexed, item)
            for match in FILE_PATH_PATTERN.finditer(item["question"]):
                self.assertIn(match.group(1), indexed, item["question"])
        self.assertNotIn("packages/auth/next-auth-options.ts", "\n".join(overview["suggested_questions"]))
        for path in overview["key_files"]:
            self.assertIn(path, indexed)

    async def test_model_beyond_the_named_file_context_is_asked_without_the_path(self):
        # A question that names a file is answered from its first
        # ``retrieval_top_k`` sections; a model defined further down must not
        # be paired with the file path or the answer could not cite it.
        filler = "\n".join(f"// padding {index}" for index in range(30))
        schema = filler + "\n" + PRISMA_SCHEMA
        files = {**INDEXED_FILES, "packages/prisma/schema.prisma": schema}
        await self.add_repo(files=files, manifest_only={}, lines_per_chunk=3)
        with patch.object(settings, "retrieval_top_k", 8):
            overview = await self.overview()
        questions = overview["suggested_questions"]
        self.assertIn("Where is the EventType model defined, and which models reference it?", questions)
        self.assertFalse(any("schema.prisma" in question and "EventType" in question for question in questions))

    async def test_staged_refresh_files_are_not_suggested(self):
        repo_id = await self.add_repo()
        await self.store.execute(
            "INSERT INTO repo_index_runs (repo_id, job_id, chunk_prefix, started_at) VALUES (?, ?, ?, ?)",
            [repo_id, "job-x", "stage-", datetime.now(UTC).isoformat()],
        )
        await self.store.execute(
            "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)",
            ["job-x", repo_id, USER, URL, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()],
        )
        # The refresh adds an auth file that only exists in hidden rows.
        rows = chunk_rows(repo_id, "packages/auth/next-auth-options.ts", "export const AUTH = {};")
        for row in rows:
            row["id"] = "stage-" + row["id"]
        await self.store.insert_chunks(rows)
        overview = await self.overview()
        text = "\n".join(overview["suggested_questions"])
        self.assertNotIn("packages/auth/next-auth-options.ts", text)
        self.assertIn("packages/features/auth/lib/next-auth-options.ts", text)
        self.assertEqual(
            overview["notice"],
            "A refresh is running. This overview shows the current index and updates when the refresh finishes.",
        )

    async def test_overview_keeps_its_shape_and_falls_back_when_file_facts_fail(self):
        await self.add_repo()
        with patch.object(explore, "gather_question_facts", new=AsyncMock(side_effect=RuntimeError("boom"))):
            overview = await self.overview()
        for key in ("repository", "total_files", "tree", "root_files", "key_files", "languages", "suggested_questions"):
            self.assertIn(key, overview)
        self.assertTrue(1 <= len(overview["suggested_questions"]) <= 6)
        self.assertNotIn("staging_prefix", overview["repository"])

    async def test_key_files_lead_with_readme_then_add_deeper_entry_files(self):
        await self.add_repo()
        overview = await self.overview()
        key_files = overview["key_files"]
        self.assertEqual(key_files[:2], ["README.md", "package.json"])
        self.assertIn("packages/prisma/schema.prisma", key_files)
        self.assertIn("packages/trpc/server/routers/_app.ts", key_files)
        self.assertLessEqual(len(key_files), 12)

    async def test_small_repository_gets_generic_questions_without_invented_files(self):
        repo_id = await self.add_repo(files={"README.md": "# Hello\n", "hello.py": "print('hi')\n"}, manifest_only={})
        overview = await self.overview()
        indexed = await self.indexed_paths(repo_id)
        self.assertTrue(1 <= len(overview["suggested_questions"]) <= 6)
        for question in overview["suggested_questions"]:
            for match in FILE_PATH_PATTERN.finditer(question):
                self.assertIn(match.group(1), indexed, question)


class ClassificationTests(unittest.TestCase):
    def facts(self, paths):
        return classify_repository_paths([(path, 1_000) for path in paths])

    def test_fastapi_layout(self):
        facts = self.facts([
            "README.md", "backend/main.py", "backend/api/auth.py", "backend/api/repos_router.py",
            "backend/api/query_router.py", "backend/tests/test_api.py", "backend/tests/conftest.py",
            "frontend/playwright.config.js", "turso/00_init.sql",
        ])
        self.assertEqual(facts["entry"], "backend/main.py")
        self.assertEqual(facts["auth"], "backend/api/auth.py")
        self.assertEqual(facts["schema"], {"path": "turso/00_init.sql", "kind": "sql"})
        self.assertEqual({router["path"] for router in facts["routers"]},
                         {"backend/api/repos_router.py", "backend/api/query_router.py"})
        self.assertEqual({router["subject"] for router in facts["routers"]}, {"repos", "query"})
        self.assertEqual(facts["test_configs"], ["frontend/playwright.config.js"])

    def test_django_go_and_rails_layouts(self):
        django = self.facts(["manage.py", "shop/models.py", "shop/views.py", "shop/urls.py", "shop/tests/test_x.py"])
        self.assertEqual(django["schema"]["kind"], "python")
        self.assertEqual(django["entry"], "manage.py")
        self.assertIn("shop/views.py", {router["path"] for router in django["routers"]})
        go = self.facts(["cmd/server/main.go", "internal/handlers/orders.go", "pkg/util/strings.go"])
        self.assertEqual(go["entry"], "cmd/server/main.go")
        self.assertEqual(go["routers"][0]["subject"], "orders")
        rails = self.facts(["config/routes.rb", "db/schema.rb", "app/controllers/orders_controller.rb"])
        self.assertEqual(rails["schema"], {"path": "db/schema.rb", "kind": "rails"})
        self.assertEqual({router["style"] for router in rails["routers"]}, {"rails-routes", "controller"})

    def test_library_files_named_like_entry_points_are_not_entry_points(self):
        facts = self.facts(["packages/i18n/server.ts", "apps/api/v2/src/main.ts", "packages/lib/index.ts"])
        self.assertEqual(facts["entry"], "apps/api/v2/src/main.ts")

    def test_tests_examples_and_mocks_are_ignored(self):
        facts = self.facts([
            "examples/app/main.py", "tests/models.py", "src/__mocks__/auth.ts", "src/auth.test.ts",
            "docs/schema.prisma",
        ])
        self.assertIsNone(facts["entry"])
        self.assertIsNone(facts["schema"])
        self.assertIsNone(facts["auth"])

    def test_model_ranking_puts_central_domain_models_first(self):
        models = rank_models(PRISMA_SCHEMA, "prisma")
        names = [model["name"] for model in models]
        self.assertEqual(names[:2], ["EventType", "Booking"])
        self.assertEqual(set(names[-2:]), {"User", "Team"})
        self.assertTrue(all(model["generic"] for model in models[-2:]))
        sql = rank_models(
            "CREATE TABLE IF NOT EXISTS repos (id TEXT);\n"
            "CREATE TABLE chunks (id TEXT, repo_id TEXT REFERENCES repos(id));\n"
            "CREATE TABLE \"users\" (id TEXT);\n",
            "sql",
        )
        self.assertEqual([model["name"] for model in sql], ["repos", "chunks", "users"])

    def test_generic_questions_without_facts(self):
        repo_map = {"tree": [{"path": "src", "files": 10, "children": [{"path": "src/api", "files": 4}]}],
                    "root_files": ["setup.py"], "extensions": {"py": 10}}
        questions = suggest_questions(repo_map)
        self.assertEqual(questions[0], "What is this project, and how is the code organized in src/?")
        self.assertLessEqual(len(questions), 6)
        self.assertEqual(len(questions), len(set(questions)))
        self.assertTrue(all(not list(FILE_PATH_PATTERN.finditer(question)) for question in questions[1:]))
        details = suggest_question_details(repo_map, None)
        self.assertTrue(all(item["paths"] == [] for item in details))


# ---------------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------------


def row(**overrides):
    base = {
        "id": "r1", "repo_name": "scheduler", "github_url": URL, "repo_status": "ready", "index_phase": "ready",
        "chunk_count": 50, "job_status": "completed", "semantic_progress": 100, "embedding_status": "complete",
        "selection_json": {}, "run_prefix": None, "error_message": None,
    }
    base.update(overrides)
    return base


class StatusPayloadTests(unittest.TestCase):
    def present(self, **overrides):
        return present_repository(row(**overrides), now=NOW)

    def test_ready_repository(self):
        view = self.present()
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["phase_label"], "Ready")
        self.assertEqual((view["phase_step"], view["phase_steps"], view["progress_percent"]), (7, 7, 100))
        self.assertEqual(view["next_action"], "ask")
        self.assertEqual(view["capabilities"], {
            "ask": True, "search": True, "browse_files": True, "semantic_search": True, "propose_changes": True,
        })
        self.assertIsNone(view["eta_seconds"])
        self.assertIsNone(view["message"])
        self.assertTrue(view["status_message"])

    def test_first_index_is_usable_while_semantic_search_builds_with_an_estimate(self):
        view = self.present(
            repo_status="ready", index_phase="searchable", job_status="processing", semantic_progress=40,
            embedding_status="running", run_prefix="initial:job-1-a-", run_started_at=ago(100),
            job_heartbeat_at=ago(5), job_claimed_at=ago(160),
            error_message="Building the semantic index: 400 of 1,000 code sections (40%). Keyword search already works.",
        )
        self.assertEqual(view["status"], "searchable")
        self.assertEqual(view["phase_label"], "Ready · semantic 40%")
        self.assertEqual(view["progress_percent"], 73)
        self.assertEqual(view["eta_seconds"], 150)
        self.assertEqual(view["eta_label"], "About 3 minutes left")
        self.assertEqual(view["next_action"], "ask")
        self.assertTrue(view["capabilities"]["ask"])
        self.assertFalse(view["capabilities"]["semantic_search"])
        self.assertEqual(view["status_message"], view["message"])
        self.assertEqual(view["indexing_started_at"], ago(160))
        self.assertFalse(view["stalled"])

    def test_queued_and_early_phases_have_no_estimate_and_say_when_questions_work(self):
        view = self.present(repo_status="queued", index_phase="queued", job_status="queued", chunk_count=0,
                            semantic_progress=0)
        self.assertEqual(view["phase_label"], "Queued")
        self.assertEqual((view["phase_step"], view["progress_percent"]), (1, 0))
        self.assertEqual(view["next_action"], "wait")
        self.assertIn("as soon as keyword search is ready", view["next_step"])
        self.assertFalse(any(view["capabilities"].values()))
        chunking = self.present(repo_status="chunking", index_phase="chunking", job_status="processing",
                                chunk_count=0, semantic_progress=0, job_heartbeat_at=ago(3))
        self.assertEqual(chunking["phase_label"], "Building keyword search")
        self.assertEqual(chunking["progress_percent"], 44)
        self.assertIsNone(chunking["eta_seconds"])

    def test_refresh_keeps_the_current_index_usable(self):
        view = self.present(
            repo_status="cloning", index_phase="cloning", job_status="processing", commit_sha="abc1234",
            job_heartbeat_at=ago(3),
            error_message="Refreshing the index. The previous index stays available until the new one is ready.",
        )
        self.assertTrue(view["refreshing"])
        self.assertEqual(view["phase_label"], "Refreshing · Downloading snapshot")
        self.assertTrue(view["capabilities"]["ask"])
        self.assertTrue(view["capabilities"]["semantic_search"])
        self.assertIn("current index", view["next_step"])
        self.assertIsNone(view["eta_seconds"])  # semantic_progress here is the previous index's

    def test_failed_run_with_operator_message_is_plain_language(self):
        stored = ("The refresh failed: Turso rejected the database credential. Replace TURSO_AUTH_TOKEN and "
                  "restart the backend. The previous index remains available.")
        view = self.present(repo_status="failed", index_phase="failed", job_status="failed", chunk_count=0,
                            error_message="Turso schema is not initialized. Run the SQL files in turso/ or enable "
                                          "AUTO_MIGRATE, then restart the backend.")
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["phase_label"], "Needs attention")
        self.assertEqual(view["message"], "A server problem interrupted indexing.")
        self.assertEqual(view["next_action"], "reindex")
        self.assertEqual(view["next_step"], "Re-index to try again.")
        self.assertIn("Turso", view["error_message"])  # stored value is unchanged
        for key in ("message", "status_message", "phase_detail", "next_step"):
            self.assertFalse(any(word in str(view[key]) for word in JARGON), (key, view[key]))
        self.assertEqual(
            friendly_stored_message(stored),
            "The refresh failed: A server problem interrupted indexing. The previous index remains available.",
        )

    def test_failure_next_steps_follow_the_cause(self):
        quota = self.present(repo_status="failed", index_phase="failed", job_status="failed", chunk_count=0,
                             error_message="Your Explorer workspace includes up to 50 MB of indexed source and it is full.")
        self.assertEqual(quota["next_action"], "free_space")
        missing = self.present(repo_status="failed", index_phase="failed", job_status="failed", chunk_count=0,
                               error_message="Repository not found. Check the URL and make sure the repository is public.")
        self.assertEqual(missing["next_action"], "check_url")

    def test_progress_message_left_by_a_finished_run_is_not_shown(self):
        view = self.present(error_message="Building the semantic index: 9,000 of 9,372 code sections (96%). "
                                          "Keyword search already works.")
        self.assertIsNone(view["message"])
        self.assertEqual(view["status_message"], view["phase_detail"])
        budget = "Indexed the 10 most important files. 4 lower-priority files were skipped."
        self.assertEqual(self.present(error_message=budget)["message"], budget)

    def test_stalled_worker_is_reported_with_a_way_out(self):
        view = self.present(repo_status="chunking", index_phase="chunking", job_status="processing", chunk_count=0,
                            job_heartbeat_at=ago(600), job_claimed_at=ago(900))
        self.assertTrue(view["stalled"])
        self.assertIn("stop it and re-index", view["status_message"])
        healthy = self.present(repo_status="chunking", index_phase="chunking", job_status="processing",
                               chunk_count=0, job_heartbeat_at=ago(20))
        self.assertFalse(healthy["stalled"])

    def test_stop_in_progress_and_stopped(self):
        stopping = self.present(repo_status="chunking", index_phase="chunking", job_status="cancelled",
                                chunk_count=0, error_message="Stopping indexing…")
        self.assertEqual((stopping["status"], stopping["phase_label"], stopping["next_action"]),
                         ("cancelled", "Stopping", "wait"))
        self.assertEqual(stopping["message"], "Stopping indexing…")
        stopped = self.present(repo_status="cancelled", index_phase="cancelled", job_status="cancelled",
                               chunk_count=0, error_message="Indexing stopped by you.")
        self.assertEqual((stopped["phase_label"], stopped["next_action"]), ("Stopped", "reindex"))

    def test_keyword_only_index(self):
        view = self.present(semantic_progress=60, embedding_status="degraded")
        self.assertEqual(view["phase_label"], "Ready · keyword search")
        self.assertFalse(view["capabilities"]["semantic_search"])
        self.assertIn("Re-index later", view["next_step"])

    def test_estimate_labels(self):
        view = {"is_indexing": True, "status": "searchable", "semantic_progress": 90}
        self.assertEqual(
            describe_index_state({**view, "index_phase": "searchable", "queryable": True, "refreshing": False,
                                  "embedding_status": "running", "job_status": "processing"},
                                 {"run_started_at": ago(450), "job_heartbeat_at": ago(1)}, NOW)["eta_label"],
            "Less than a minute left",
        )
        early = describe_index_state(
            {**view, "semantic_progress": 2, "index_phase": "searchable", "queryable": True, "refreshing": False,
             "embedding_status": "running", "job_status": "processing"},
            {"run_started_at": ago(450), "job_heartbeat_at": ago(1)}, NOW,
        )
        self.assertIsNone(early["eta_seconds"])  # too little progress to extrapolate honestly


# ---------------------------------------------------------------------------
# Error and status messages
# ---------------------------------------------------------------------------


class ErrorMessageTests(StoreCase):
    def assert_plain(self, detail):
        self.assertFalse(any(word in detail for word in JARGON), detail)

    def test_storage_errors_hide_operator_detail(self):
        logger = SimpleNamespace(error=lambda *a, **k: None, exception=lambda *a, **k: None)
        cases = (
            (DatabaseConfigurationError("Turso is not configured. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN."), 503,
             "Couldn't load your repositories because the service is temporarily unavailable."),
            (TransientDatabaseError("Turso request timed out."), 502,
             "Couldn't load your repositories because the database is briefly unavailable."),
            (DatabaseStatementError("no such table: repos"), 502,
             "Couldn't load your repositories because of a server problem."),
            (RuntimeError("boom"), 502, "Couldn't load your repositories right now."),
        )
        for error, status, prefix in cases:
            with self.subTest(error=error), self.assertRaises(HTTPException) as raised:
                raise_storage_error(error, "load your repositories", logger)
            self.assertEqual(raised.exception.status_code, status)
            self.assertTrue(raised.exception.detail.startswith(prefix), raised.exception.detail)
            self.assert_plain(raised.exception.detail)

    async def test_overview_database_failure_is_actionable(self):
        failing = SimpleNamespace(fetch_one=AsyncMock(side_effect=TransientDatabaseError("Turso transport failed.")))
        with patch.object(explore, "get_turso_store", return_value=failing), \
                patch.object(explore, "assert_turso_schema", new=AsyncMock()), \
                self.assertRaises(HTTPException) as raised:
            await explore.repository_overview("scheduler", self.user)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            raised.exception.detail,
            "Couldn't load the repository overview because the database is briefly unavailable. "
            "Wait a few seconds and try again.",
        )

    async def test_search_explains_empty_results(self):
        await self.add_repo()
        await self.add_repo(name="pending", status="queued", files={}, manifest_only={})
        p = self.patched()
        with p[0], p[1]:
            pending = await explore.repository_search("pending", "Booking", 20, self.user)
            empty = await explore.repository_search("scheduler", "zzqqnotathing", 20, self.user)
            found = await explore.repository_search("scheduler", "createBooking", 20, self.user)
        self.assertEqual((pending["results"], pending["searchable"]), ([], False))
        self.assertIn("as soon as keyword indexing finishes", pending["message"])
        self.assertEqual((empty["results"], empty["searchable"]), ([], True))
        self.assertIn("No matches for “zzqqnotathing”", empty["message"])
        self.assertTrue(found["results"])
        self.assertIsNone(found["message"])

    async def test_file_view_errors_say_what_to_do(self):
        await self.add_repo()
        p = self.patched()
        with p[0], p[1], p[2]:
            with self.assertRaises(HTTPException) as missing:
                await explore.repository_file("scheduler", "src/missing.ts", self.user)
            with self.assertRaises(HTTPException) as invalid:
                await explore.repository_file("scheduler", "../etc/passwd", self.user)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertIn("src/missing.ts isn't in the index", missing.exception.detail)
        self.assertIn("coverage report", missing.exception.detail)
        self.assertEqual(invalid.exception.status_code, 422)
        self.assertNotIn("proposed", invalid.exception.detail)
        self.assertIn("such as src/app.ts", invalid.exception.detail)

    async def test_repository_management_messages(self):
        await self.add_repo()
        await self.add_repo(name="other")
        p = self.patched()
        with p[3], p[4]:
            with self.assertRaises(HTTPException) as impact:
                await repos.repository_impact("scheduler", "src/nope.ts", 20, self.user)
            with self.assertRaises(HTTPException) as duplicate:
                await repos.rename_repo("scheduler", repos.RenameRepositoryRequest(repo_name="other"), self.user)
            with self.assertRaises(HTTPException) as absolute:
                await repos.repository_impact("scheduler", "/etc/passwd", 20, self.user)
            status = await repos.get_status("scheduler", self.user)
        self.assertEqual(impact.exception.status_code, 404)
        self.assertIn("No indexed file matches src/nope.ts", impact.exception.detail)
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertIn("Choose a different name", duplicate.exception.detail)
        self.assertEqual(absolute.exception.status_code, 422)
        self.assertIn("without a leading slash", absolute.exception.detail)
        # The status payload keeps its fields and adds the descriptive ones.
        for key in ("status", "is_indexing", "queryable", "message", "semantic_progress", "phase_label",
                    "next_action", "capabilities", "progress_percent"):
            self.assertIn(key, status)
        self.assertNotIn("staging_prefix", status)

    async def test_ingest_errors_are_friendly(self):
        limiter = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
        request = ingest_router.IngestRequest(github_url="https://github.com/acme/scheduler")
        cases = (
            (TransientDatabaseError("Turso returned HTTP 503."), 502, "briefly unavailable"),
            (IngestionConflictError("Repository ingestion is already in progress."), 409,
             "This repository is already being indexed. Wait for it to finish, or stop it first."),
            (RuntimeError("rate_limit"), 429, "Wait a minute, then try again."),
            (RuntimeError("unexpected"), 500, "Try again in a moment."),
        )
        for error, status, fragment in cases:
            with self.subTest(error=error), \
                    patch.object(ingest_router, "ingest_request_limiter", limiter), \
                    patch.object(ingest_router, "assert_turso_schema", new=AsyncMock()), \
                    patch.object(ingest_router, "get_turso_store", return_value=self.store), \
                    patch.object(ingest_router, "enforce_ingestion_capacity", new=AsyncMock(side_effect=error)), \
                    self.assertRaises(HTTPException) as raised:
                await ingest_router.ingest_repo(request, self.user)
            self.assertEqual(raised.exception.status_code, status)
            self.assertIn(fragment, raised.exception.detail)
            self.assert_plain(raised.exception.detail)

    async def test_ingest_success_says_what_happens_next(self):
        limiter = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
        with patch.object(ingest_router, "ingest_request_limiter", limiter), \
                patch.object(ingest_router, "assert_turso_schema", new=AsyncMock()), \
                patch.object(ingest_router, "get_turso_store", return_value=self.store), \
                patch.object(ingest_router, "enforce_ingestion_capacity", new=AsyncMock()), \
                patch.object(ingest_router, "ensure_repo_record", new=AsyncMock(return_value=("id-1", "scheduler"))), \
                patch.object(ingest_router, "enqueue_ingestion_job", new=AsyncMock()):
            response = await ingest_router.ingest_repo(
                ingest_router.IngestRequest(github_url="https://github.com/acme/scheduler"), self.user,
            )
        self.assertEqual(response["repo_name"], "scheduler")
        self.assertEqual(response["status"], "queued")
        self.assertIn("as soon as keyword search is ready", response["message"])


if __name__ == "__main__":
    unittest.main()
