import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from ingest.chunker import chunk_file, extract_symbols, iter_chunk_file
from ingest.cloner import RepositoryValidationError, get_file_selection_report, get_files_to_process, normalize_github_url
from ingest.embedder import EMBEDDING_DIMENSION, EmbeddingUnavailableError, embed_chunks


class MemoryStore:
    """Minimal async store used to test the API boundary without a live Turso database."""

    def __init__(self):
        self.rows = []
        self.executed = []

    async def execute(self, sql, args=None):
        self.executed.append((sql, args or []))
        return SimpleNamespace(rows=[], rows_affected=1)

    async def fetch_one(self, sql, args=None):
        self.executed.append((sql, args or []))
        return self.rows[0] if self.rows else None

    async def fetch_all(self, sql, args=None):
        self.executed.append((sql, args or []))
        return list(self.rows)

    async def insert_chunks(self, chunks):
        self.rows.extend(chunks)


class BackendSmokeTests(unittest.TestCase):
    def test_app_imports_and_root_endpoint_works(self):
        import main
        from main import app

        class HealthStore:
            async def fetch_one(self, _sql, _args=None):
                return {"ok": 1}

        client = TestClient(app)
        root = client.get("/")
        self.assertEqual(root.status_code, 200)
        main._dependency_cache.clear()
        # Never let the health probe reach a real database from the suite.
        with patch.object(main, "assert_turso_schema", new=AsyncMock()), \
             patch.object(main, "get_turso_store", return_value=HealthStore()):
            health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertIn(health.json()["status"], {"ok", "degraded"})
        self.assertIn("dependencies", health.json())
        self.assertTrue(health.headers.get("x-request-id"))
        self.assertEqual(root.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", root.headers["content-security-policy"])

    def test_readiness_probe_checks_turso_and_nvidia_without_exposing_secrets(self):
        import main

        class HealthStore:
            async def fetch_one(self, _sql, _args=None):
                return {"ok": 1}

        async def run_probe():
            with patch.object(main, "assert_turso_schema", new=AsyncMock()), \
                 patch.object(main, "get_turso_store", return_value=HealthStore()), \
                 patch.object(main.settings, "nvidia_api_key", "test-secret"), \
                 patch.object(main, "_probe_nvidia_sync", return_value="ok"):
                return await main._dependency_checks(probe_nvidia=True)

        checks = asyncio.run(run_probe())
        self.assertEqual(checks, {"turso": {"status": "ok"}, "nvidia": {"status": "ok"}})
        self.assertNotIn("test-secret", str(checks))

    def test_default_ingestion_limits_are_100mb_repository_and_50mb_file(self):
        from config import settings
        self.assertEqual(settings.max_repository_bytes, 100_000_000)
        self.assertEqual(settings.max_file_size_bytes, 50_000_000)

    def test_turso_store_handles_parameterized_rows_and_json_metadata(self):
        from database import TursoStore
        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")

            async def run():
                await store.execute("CREATE TABLE records (id TEXT PRIMARY KEY, symbols TEXT NOT NULL)")
                await store.execute("INSERT INTO records VALUES (?, ?)", ["one", '["login"]'])
                return await store.fetch_one("SELECT id, symbols FROM records WHERE id = ?", ["one"])

            row = asyncio.run(run())
        self.assertEqual(row, {"id": "one", "symbols": ["login"]})

    def test_turso_store_reconnects_after_expired_hrana_stream(self):
        from database import TursoStore

        class Cursor:
            description = [("value",)]
            rowcount = 1

            def fetchall(self):
                return [("recovered",)]

        class StaleClient:
            def __init__(self):
                self.closed = False

            def execute(self, _sql, _args):
                raise ValueError("Hrana: api error: status=404 Not Found, body={stream not found}")

            def commit(self):
                pass

            def close(self):
                self.closed = True

        class FreshClient:
            def execute(self, _sql, _args):
                return Cursor()

            def commit(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")
            stale = StaleClient()
            fresh = FreshClient()
            store._client = stale
            store._connect_sync = lambda: fresh
            row = asyncio.run(store.fetch_one("SELECT 1"))

        self.assertEqual(row, {"value": "recovered"})
        self.assertTrue(stale.closed)

    def test_turso_chunk_insert_supports_keyword_only_fallback(self):
        from database import TursoStore
        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")

            async def run():
                await store.execute(
                    "CREATE TABLE chunks (id TEXT PRIMARY KEY, repo_id TEXT, file_path TEXT, start_line INTEGER, "
                    "end_line INTEGER, language TEXT, symbols TEXT, content TEXT, embedding BLOB)"
                )
                await store.insert_chunks([{
                    "id": "chunk-1", "repo_id": "repo-1", "file_path": "src/auth.py", "start_line": 1,
                    "end_line": 2, "language": "py", "symbols": ["login"], "content": "def login(): pass", "embedding": None,
                }])
                return await store.fetch_one("SELECT id, symbols, content FROM chunks WHERE id = ?", ["chunk-1"])

            row = asyncio.run(run())
        self.assertEqual(row["symbols"], ["login"])
        self.assertEqual(row["content"], "def login(): pass")

    def test_turso_configuration_requires_server_only_credentials(self):
        from database import DatabaseConfigurationError, get_turso_store
        with patch("database.settings.turso_database_url", ""), patch("database.settings.turso_auth_token", ""):
            get_turso_store.cache_clear()
            with self.assertRaises(DatabaseConfigurationError):
                get_turso_store()
            get_turso_store.cache_clear()

    def test_answer_model_profiles_are_allow_listed(self):
        from api.query_router import QueryRequest
        from config import get_answer_model_options
        from pydantic import ValidationError
        self.assertEqual(get_answer_model_options("fast")[0], "nvidia/nemotron-3.5-super-vl-120b-a12b")
        self.assertEqual(get_answer_model_options("detailed")[0], "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(get_answer_model_options("fast", "editing")[0], "nvidia/nemotron-3.5-super-vl-120b-a12b")
        self.assertEqual(get_answer_model_options("code")[0], "nvidia/nemotron-3.5-super-vl-120b-a12b")
        with self.assertRaises(ValidationError):
            QueryRequest(repo_name="demo", question="Where is login?", model_profile="untrusted/model")
        with self.assertRaises(ValidationError):
            QueryRequest(repo_name="demo", question="Where is login?", workflow="untrusted")
        self.assertEqual(QueryRequest(repo_name="demo", question="Fix src/auth.py", workflow="editing").workflow, "editing")

    def test_credentials_cors_rejects_a_wildcard_origin(self):
        from config import get_cors_origins
        with patch("config.settings.cors_origins", "https://app.example.com,*"):
            with self.assertRaises(ValueError):
                get_cors_origins()

    def test_github_url_normalization_rejects_non_repository_urls(self):
        self.assertEqual(normalize_github_url(" https://github.com/octocat/Hello-World/ "), "https://github.com/octocat/Hello-World.git")
        # Browser links (http, in-repository routes) and shorthand still name
        # exactly one repository, so they resolve to the canonical clone URL.
        for value in ("http://github.com/a/b", "https://github.com/a/b/tree/main", "a/b", "git@github.com:a/b.git"):
            self.assertEqual(normalize_github_url(value), "https://github.com/a/b.git")
        for value in (
            # Other hosts, including look-alikes and scheme-less forms.
            "https://evil.test/a/b",
            "https://github.com.evil.test/a/b",
            "github.com.evil.test/a/b",
            "git@gitlab.com:a/b.git",
            "ftp://github.com/a/b",
            # Credentials and ports.
            "https://user:token@github.com/a/b",
            "https://github.com:8443/a/b",
            # Malformed or non-repository URLs.
            "",
            "not a url",
            "https://github.com/a",
            "https://github.com/a//b",
            "https://github.com/a/b c",
            "https://github.com/../b",
            "https://github.com/a/..",
            "https://github.com/a/b/unknown",
            "https://github.com/settings/profile",
        ):
            with self.subTest(value=value), self.assertRaises(RepositoryValidationError):
                normalize_github_url(value)

    def test_get_files_to_process_accepts_larger_source_files_and_skips_oversized_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "src" / "extended.py").write_text("x" * 1_100_000, encoding="utf-8")
            (root / "src" / "oversized.py").write_text("x" * 2_000_001, encoding="utf-8")
            (root / "README.md").write_text("# Hello\n", encoding="utf-8")
            (root / "link.py").symlink_to(root / "src" / "main.py")
            with patch("ingest.cloner.settings.max_file_size_bytes", 2_000_000):
                names = {Path(path).name for path in get_files_to_process(str(root))}
        self.assertEqual(names, {"main.py", "extended.py", "README.md"})

    def test_chunking_preserves_actual_line_ranges_and_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "app.py"
            file_path.write_text("class Login:\n    pass\n\ndef handle_login():\n    return True\n", encoding="utf-8")
            chunks = chunk_file(str(file_path), str(root))
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertEqual(chunks[0]["end_line"], 5)
        self.assertEqual(chunks[0]["symbols"], ["Login", "handle_login"])
        self.assertEqual(extract_symbols("const signIn = async () => {}"), ["signIn"])

    def test_large_file_chunking_streams_bounded_windows(self):
        """A file above the streaming threshold never needs a whole-file list."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "large.py"
            # Keep this just above the production streaming threshold while
            # making line ranges and overlap easy to assert.
            file_path.write_text(("value = 1\n" * 240_000), encoding="utf-8")
            chunks = list(iter_chunk_file(str(file_path), str(root)))
        self.assertGreater(len(chunks), 10)
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertTrue(all(len(chunk["content"]) <= 12_000 for chunk in chunks))
        self.assertTrue(all(chunk["start_line"] <= chunk["end_line"] for chunk in chunks))
        self.assertGreater(chunks[-1]["end_line"], 200_000)

    def test_large_file_pipeline_yields_one_chunk_at_a_time(self):
        from ingest.pipeline import iter_chunked_files

        async def collect(path, root):
            result = []
            async for emitted_path, emitted_chunks in iter_chunked_files([path], root):
                result.append((emitted_path, emitted_chunks))
            return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "large.py"
            file_path.write_text(("value = 1\n" * 240_000), encoding="utf-8")
            emitted = asyncio.run(collect(str(file_path), str(root)))
        self.assertGreater(len(emitted), 10)
        self.assertTrue(all(len(chunks) == 1 for _, chunks in emitted))
        self.assertTrue(all(path == str(file_path) for path, _ in emitted))

    def test_large_file_stream_cancels_without_leaking_a_producer_thread(self):
        from ingest.pipeline import iter_chunked_files

        async def cancel_consumer(path, root):
            async def consume():
                async for _path, _chunks in iter_chunked_files([path], root):
                    await asyncio.sleep(0)

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "large.py"
            file_path.write_text(("value = 1\n" * 240_000), encoding="utf-8")
            asyncio.run(cancel_consumer(str(file_path), str(root)))

    def test_embed_chunks_uses_hosted_nvidia_embeddings(self):
        from array import array

        chunks = [{"file_path": "src/app.py", "content": "def hello(): pass", "start_line": 1, "end_line": 1, "language": "py"}]
        fake_client = MagicMock()
        fake_client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.0] * EMBEDDING_DIMENSION)])
        with patch("ingest.embedder.get_embedding_client", return_value=fake_client):
            embedded = embed_chunks(chunks)
        self.assertEqual(len(embedded[0]["embedding"]), EMBEDDING_DIMENSION)
        self.assertIsInstance(embedded[0]["embedding"], array)
        self.assertEqual(fake_client.embeddings.create.call_args.kwargs["extra_body"]["input_type"], "passage")

    def test_database_encodes_packed_vectors_at_write_boundary(self):
        from array import array
        from database import TursoStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")
            captured = []

            async def capture(statements):
                captured.extend(statements)

            async def run():
                with patch.object(store, "batch", new=capture):
                    await store.update_chunk_embeddings([{
                        "id": "chunk-1", "repo_id": "repo-1", "embedding": array("f", [0.25, 0.5]),
                    }])
                await store.close()

            asyncio.run(run())

        # Vectors cross the database boundary as packed little-endian float32
        # blobs (8 KB per 2,048-d vector instead of ~20 KB of JSON text).
        import struct
        self.assertEqual(captured[0].args[0], struct.pack("<2f", 0.25, 0.5))

    def test_embedded_store_accepts_packed_vector_blobs(self):
        from database import TursoStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")

            async def run():
                await store.execute(
                    "CREATE TABLE chunks (id TEXT PRIMARY KEY, repo_id TEXT, file_path TEXT, start_line INTEGER, "
                    "end_line INTEGER, language TEXT, symbols TEXT, content TEXT, embedding F32_BLOB(3))"
                )
                await store.insert_chunks([{
                    "id": "chunk-1", "repo_id": "repo-1", "file_path": "src/a.py", "start_line": 1,
                    "end_line": 1, "language": "py", "symbols": [], "content": "a = 1", "embedding": [0.5, -1.0, 2.0],
                }])
                row = await store.fetch_one(
                    "SELECT vector_extract(embedding) AS v, vector_distance_cos(embedding, vector32(?)) AS d "
                    "FROM chunks WHERE id = ?", ["[0.5,-1.0,2.0]", "chunk-1"],
                )
                await store.close()
                return row

            row = asyncio.run(run())
        self.assertEqual(row["v"], "[0.5,-1,2]")
        self.assertAlmostEqual(row["d"], 0.0, places=5)

    def test_embed_chunks_splits_only_a_rejected_batch_without_dropping_chunks(self):
        from config import settings

        chunks = [
            {"file_path": f"src/{index}.py", "content": f"value = {index}", "start_line": 1,
             "end_line": 1, "language": "py"}
            for index in range(10)
        ]
        request_sizes = []

        def fake_embed(texts, *, input_type):
            self.assertEqual(input_type, "passage")
            request_sizes.append(len(texts))
            if len(texts) > 4:
                raise EmbeddingUnavailableError("payload too large")
            return [[float(index)] * EMBEDDING_DIMENSION for index in range(len(texts))]

        with patch.object(settings, "embedding_batch_size", 8), \
             patch.object(settings, "embedding_min_batch_size", 4), \
             patch("ingest.embedder.embed_texts", side_effect=fake_embed):
            embedded = embed_chunks(chunks)

        self.assertEqual(request_sizes, [8, 4, 4, 2])
        self.assertEqual(len(embedded), len(chunks))
        self.assertTrue(all("embedding" in chunk for chunk in embedded))

    def test_embed_chunks_uses_larger_bounded_batches_for_small_files(self):
        from config import settings
        chunks = [
            {"file_path": f"src/{index}.py", "content": f"value = {index}", "start_line": 1,
             "end_line": 1, "language": "py"}
            for index in range(33)
        ]
        request_sizes = []

        def fake_embed(texts, *, input_type):
            self.assertEqual(input_type, "passage")
            request_sizes.append(len(texts))
            return [[float(index)] * EMBEDDING_DIMENSION for index in range(len(texts))]

        with patch.object(settings, "embedding_batch_size", 16), \
             patch.object(settings, "embedding_min_batch_size", 4), \
             patch("ingest.embedder.embed_texts", side_effect=fake_embed):
            embedded = embed_chunks(chunks)

        self.assertEqual(request_sizes, [16, 16, 1])
        self.assertEqual(len(embedded), len(chunks))

    def test_embed_repository_chunks_records_provider_calls_without_crashing(self):
        from ingest.pipeline import embed_repository_chunks

        chunks = [{
            "id": "chunk-1", "repo_id": "repo-1", "file_path": "src/app.py",
            "start_line": 1, "end_line": 1, "language": "py", "symbols": [],
            "content": "print('ok')",
        }]
        metrics = {"embedding_requests": 0}

        def fake_embed(batch, *, initial_batch_size=None, on_batch_size_change=None, on_request=None):
            if on_request:
                on_request()
            for chunk in batch:
                chunk["embedding"] = [0.0] * EMBEDDING_DIMENSION
            return batch

        with patch("ingest.pipeline.embed_chunks", side_effect=fake_embed):
            embedded = asyncio.run(embed_repository_chunks(
                MemoryStore(), "repo-1", chunks, metrics=metrics,
            ))

        self.assertEqual(metrics["embedding_requests"], 1)
        self.assertEqual(len(embedded[0]["embedding"]), EMBEDDING_DIMENSION)

    def test_changed_file_replacement_preserves_current_job_version(self):
        from ingest.pipeline import replace_changed_file_chunks
        from database import TursoStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TursoStore(f"file:{Path(tmp) / 'test.db'}", "local-test-token")

            async def run():
                await store.execute(
                    "CREATE TABLE repos (id TEXT PRIMARY KEY)"
                )
                await store.execute(
                    "CREATE TABLE chunks (id TEXT PRIMARY KEY, repo_id TEXT, file_path TEXT)"
                )
                await store.execute("INSERT INTO repos VALUES (?)", ["repo-1"])
                await store.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?)", ["old-1", "repo-1", "src/app.py"]
                )
                await store.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?)", ["job-1-new", "repo-1", "src/app.py"]
                )
                await replace_changed_file_chunks(
                    store, "repo-1", {"src/app.py"}, set(), preserve_id_prefix="job-1-"
                )
                rows = await store.fetch_all(
                    "SELECT id FROM chunks WHERE repo_id = ? ORDER BY id", ["repo-1"]
                )
                await store.close()
                return rows

            rows = asyncio.run(run())

        self.assertEqual(rows, [{"id": "job-1-new"}])

    def test_cancelled_refresh_keeps_previous_searchable_index(self):
        from ingest.pipeline import IngestionCancelledError, run_ingestion_for_repo

        store = MemoryStore()

        async def fetch_one(sql, args=None):
            store.executed.append((sql, args or []))
            if "COUNT(*) AS count" in sql:
                return {"count": 3}
            if "COUNT(*) AS total" in sql:
                return {"total": 3, "embedded": 3}
            if "SELECT status" in sql:
                return {"status": "ready"}
            return {"rows_affected": 1}

        store.fetch_one = fetch_one
        with patch("ingest.pipeline.acquire_repository_source", side_effect=IngestionCancelledError("stop")):
            result = asyncio.run(run_ingestion_for_repo(
                store, "https://github.com/octocat/Hello-World", "user-1", repo_id="repo-1",
            ))

        self.assertFalse(result)
        self.assertTrue(any("status = ?" in sql and "ready" in args for sql, args in store.executed if args))
        self.assertFalse(any(sql == "DELETE FROM chunks WHERE repo_id = ?" for sql, _ in store.executed))

    def test_ensure_repo_record_queues_failed_repository_without_erasing_old_index(self):
        from ingest.pipeline import ensure_repo_record
        store = MemoryStore()
        store.rows = [{"id": "repo-1", "status": "failed"}]
        with patch("ingest.pipeline.timestamp", return_value="2026-01-01T00:00:00+00:00"):
            repo_id, name = asyncio.run(ensure_repo_record(store, "https://github.com/octocat/Hello-World", "user-1"))
        self.assertEqual((repo_id, name), ("repo-1", "Hello-World"))
        self.assertNotIn("DELETE FROM chunks", " ".join(sql for sql, _ in store.executed))
        self.assertTrue(all("user-1" in args for _, args in store.executed if args and "repos" in _))

    def test_reindex_preserves_a_renamed_repository_row(self):
        from ingest.pipeline import queue_existing_repo

        store = MemoryStore()
        renamed = {
            "id": "repo-1",
            "repo_name": "My renamed workspace",
            "github_url": "https://github.com/octocat/Hello-World.git",
        }
        repo_id, repo_name = asyncio.run(queue_existing_repo(store, renamed, "user-1"))

        self.assertEqual((repo_id, repo_name), ("repo-1", "My renamed workspace"))
        self.assertTrue(any("UPDATE repos SET status" in sql for sql, _ in store.executed))
        self.assertTrue(any("INSERT INTO ingestion_jobs" in sql for sql, _ in store.executed))
        self.assertFalse(any("INSERT INTO repos" in sql for sql, _ in store.executed))

    def test_file_selection_report_discloses_exclusion_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "src" / "large.py").write_text("x" * 120, encoding="utf-8")
            (root / "image.png").write_bytes(b"\x89PNG\x00")
            (root / ".env").write_text("SECRET=x", encoding="utf-8")
            with patch("ingest.cloner.settings.max_file_size_bytes", 100):
                report = get_file_selection_report(str(root))
        self.assertEqual(report["eligible_files"], 1)
        self.assertEqual(report["excluded_reasons"]["file_size_limit"], 1)
        self.assertEqual(report["excluded_reasons"]["unsupported_format"], 1)
        self.assertEqual(report["excluded_reasons"]["hidden"], 1)
        self.assertEqual(set(report["excluded_paths"]), {"src/large.py", "image.png", ".env"})

    def test_evidence_plan_targets_security_paths(self):
        from retrieval.retriever import build_evidence_plan
        plan = build_evidence_plan("How does authentication work?", "security")
        self.assertEqual(plan["workflow"], "security")
        self.assertIn("middleware", plan["path_hints"])
        self.assertIn("auth", plan["search_terms"])

    def test_issue_reference_keeps_editing_retrieval_broad_and_traceable(self):
        from retrieval.retriever import build_evidence_plan, extract_issue_reference
        plan = build_evidence_plan("Issue #1428: authentication fails after refresh", "editing")
        self.assertEqual(extract_issue_reference("Please fix GitHub issue #1428"), "1428")
        self.assertEqual(plan["issue_reference"], "1428")
        self.assertEqual(plan["query_scope"], "issue")

    def test_navigation_question_excludes_readme_and_targets_header_files(self):
        from retrieval.retriever import build_evidence_plan, is_overview_file, retrieve_context

        plan = build_evidence_plan("How do I change the NAV bar design?", "general")
        self.assertIn("header", plan["path_hints"])
        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
            "language": "md", "symbols": [], "content": "The navigation is described here.",
        }
        header = {
            "id": "header-1", "file_path": "frontend/src/components/SiteHeader.jsx", "start_line": 1,
            "end_line": 24, "language": "jsx", "symbols": ["SiteHeader"],
            "content": "export function SiteHeader() { return <nav />; }",
        }
        footer = {
            "id": "footer-1", "file_path": "frontend/src/components/Footer.jsx", "start_line": 1,
            "end_line": 12, "language": "jsx", "symbols": ["Footer"],
            "content": "export function Footer() { return <footer />; }",
        }

        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            # Simulate a broad keyword match plus targeted path-hint matches.
            return [header] if "LIKE ?" in sql else [readme, header, footer]

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", "How do I change the NAV bar design?", top_k=4))

        self.assertTrue(result)
        self.assertTrue(any(chunk["file_path"].endswith("SiteHeader.jsx") for chunk in result))
        self.assertFalse(any(is_overview_file(chunk["file_path"]) for chunk in result))
        self.assertFalse(any(chunk["file_path"].endswith("Footer.jsx") for chunk in result))

    def test_api_location_question_returns_only_api_path_evidence(self):
        from retrieval.retriever import build_evidence_plan, retrieve_context

        question = "Where is the API defined in this project?"
        plan = build_evidence_plan(question)
        self.assertEqual(plan["query_scope"], "targeted_api_location")
        self.assertIn("api", plan["path_hints"])

        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 12,
            "language": "md", "symbols": [], "content": "The API is described here.",
        }
        route = {
            "id": "route-1", "file_path": "backend/api/routes.py", "start_line": 1, "end_line": 28,
            "language": "py", "symbols": ["router"], "content": "router = APIRouter()",
        }
        unrelated = {
            "id": "service-1", "file_path": "backend/services/users.py", "start_line": 1, "end_line": 18,
            "language": "py", "symbols": ["UserService"], "content": "class UserService: pass",
        }

        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            # Path-hint queries should return only their matching path; broad
            # retrieval simulates the noisy candidates seen in production.
            if "lower(file_path) LIKE ?" in sql:
                return [route]
            return [readme, route, unrelated]

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", question, top_k=4))

        self.assertEqual([chunk["file_path"] for chunk in result], ["backend/api/routes.py"])

    def test_frontend_editing_question_ignores_negated_readme_reference(self):
        from retrieval.retriever import extract_requested_file_paths, retrieve_context

        question = "How should I edit the frontend navbar? Do not cite README.md or unrelated docs."
        self.assertEqual(extract_requested_file_paths(question), [])
        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
            "language": "md", "symbols": [], "content": "The navbar is described here.",
        }
        navbar = {
            "id": "navbar-1", "file_path": "frontend/src/components/NavBar.tsx", "start_line": 1,
            "end_line": 32, "language": "tsx", "symbols": ["NavBar"],
            "content": "export function NavBar() { return <nav />; }",
        }
        unrelated = {
            "id": "service-1", "file_path": "backend/services/users.py", "start_line": 1,
            "end_line": 18, "language": "py", "symbols": [], "content": "class UserService: pass",
        }
        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            return [readme, navbar, unrelated]

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", question, top_k=4))

        self.assertEqual([chunk["file_path"] for chunk in result], ["frontend/src/components/NavBar.tsx"])

    def test_frontend_location_question_does_not_widen_to_readme(self):
        from retrieval.retriever import retrieve_context

        question = "Where is the frontend navbar implemented? Exclude README.md from citations."
        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
            "language": "md", "symbols": [], "content": "The navbar is described here.",
        }
        navbar = {
            "id": "navbar-1", "file_path": "frontend/src/components/NavBar.tsx", "start_line": 1,
            "end_line": 32, "language": "tsx", "symbols": ["NavBar"],
            "content": "export function NavBar() { return <nav />; }",
        }
        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            return [readme, navbar]

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", question, top_k=4))

        self.assertEqual([chunk["file_path"] for chunk in result], ["frontend/src/components/NavBar.tsx"])

    def test_frontend_editing_question_without_readme_reference_uses_source_paths(self):
        from retrieval.retriever import build_evidence_plan, retrieve_context

        question = "How do I edit the frontend of this project?"
        plan = build_evidence_plan(question)
        self.assertEqual(plan["query_scope"], "targeted_topic")
        self.assertIn("src", plan["path_hints"])

        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
            "language": "md", "symbols": [], "content": "Edit the frontend in src/.",
        }
        app = {
            "id": "app-1", "file_path": "src/App.jsx", "start_line": 1, "end_line": 29,
            "language": "jsx", "symbols": ["App"], "content": "export default function App() { return null; }",
        }
        unrelated = {
            "id": "service-1", "file_path": "backend/services/users.py", "start_line": 1, "end_line": 18,
            "language": "py", "symbols": [], "content": "class UserService: pass",
        }
        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            if "lower(c.file_path) LIKE ?" in sql or "lower(file_path) LIKE ?" in sql:
                return [app]
            return [readme, app, unrelated]

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", question, top_k=4))

        self.assertEqual([chunk["file_path"] for chunk in result], ["src/App.jsx"])

    def test_authentication_path_aliases_keep_common_module_names(self):
        from retrieval.retriever import build_evidence_plan, path_matches_hints

        plan = build_evidence_plan("How does auth work?")
        self.assertEqual(plan["query_scope"], "targeted_topic")
        self.assertTrue(path_matches_hints("src/authentication.py", plan["path_hints"]))
        self.assertTrue(path_matches_hints("src/authorization.py", plan["path_hints"]))

    def test_path_hint_candidates_use_token_boundaries(self):
        from retrieval.retriever import retrieve_context

        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            if "JOIN hints h ON lower(c.file_path) LIKE h.pattern" in sql:
                return [
                    {"id": "false", "file_path": "backend/capillary.py", "start_line": 1, "end_line": 2,
                     "language": "py", "symbols": [], "content": "pass", "matched_hint": "api", "hint_order": 0},
                    {"id": "true", "file_path": "backend/api/routes.py", "start_line": 1, "end_line": 2,
                     "language": "py", "symbols": [], "content": "router = APIRouter()", "matched_hint": "api", "hint_order": 0},
                ]
            return []

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", "Show API docs", top_k=4))
        self.assertEqual([chunk["file_path"] for chunk in result], ["backend/api/routes.py"])

    def test_overview_fallback_does_not_cite_arbitrary_first_source_file(self):
        from retrieval.retriever import retrieve_context

        readme = {
            "id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
            "language": "md", "symbols": [], "content": "A repository overview.",
        }
        source = {
            "id": "source-1", "file_path": "src/implementation.py", "start_line": 1, "end_line": 8,
            "language": "py", "symbols": [], "content": "internal implementation",
        }
        store = MemoryStore()

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            if "LIKE '%readme.md'" in sql:
                return [readme]
            if "ORDER BY file_path, start_line LIMIT" in sql:
                return [source]
            return []

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", "What is this project?", top_k=4))
        self.assertEqual([chunk["file_path"] for chunk in result], ["README.md"])

    def test_sparse_terms_ignore_question_stopwords(self):
        from retrieval.retriever import search_terms

        terms = search_terms("Where does the API live in this project?")
        self.assertIn("api", terms)
        self.assertNotIn("where", terms)
        self.assertNotIn("does", terms)
        self.assertNotIn("the", terms)

    def test_targeted_topics_use_query_hints_and_boundary_matching(self):
        from retrieval.retriever import build_evidence_plan, is_strict_target_question, path_matches_hints

        indexing_plan = build_evidence_plan("How does repository indexing work?")
        self.assertEqual(indexing_plan["query_scope"], "targeted_topic")
        self.assertIn("pipeline", indexing_plan["path_hints"])
        self.assertTrue(is_strict_target_question("How does authentication work?", []))
        self.assertTrue(path_matches_hints("backend/api/routes.py", ["route"]))
        self.assertFalse(path_matches_hints("backend/capillary.py", ["api"]))
        # Documentation questions intentionally retain access to README/docs
        # evidence instead of being forced through an implementation-only set.
        self.assertFalse(is_strict_target_question("Show the API docs", [], include_overview_files=True))

    def test_dependency_manifest_resolves_only_local_imports(self):
        from ingest.dependencies import build_dependency_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            entry = root / "src" / "main.py"
            auth = root / "src" / "auth.py"
            entry.write_text("from src.auth import login\n", encoding="utf-8")
            auth.write_text("def login(): pass\n", encoding="utf-8")
            edges = build_dependency_manifest([str(entry), str(auth)], str(root))
        self.assertEqual(edges[0]["source_file"], "src/main.py")
        self.assertEqual(edges[0]["target_file"], "src/auth.py")

    def test_dependency_manifest_resolves_relative_imports(self):
        from ingest.dependencies import build_dependency_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            entry = root / "pkg" / "main.py"
            auth = root / "pkg" / "auth.py"
            entry.write_text("from .auth import login\n", encoding="utf-8")
            auth.write_text("def login(): pass\n", encoding="utf-8")
            edges = build_dependency_manifest([str(entry), str(auth)], str(root))
        self.assertEqual(edges[0]["target_file"], "pkg/auth.py")

    def test_combined_manifest_scan_hashes_and_resolves_in_one_result(self):
        from ingest.dependencies import build_manifest_and_dependency_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            entry = root / "pkg" / "main.py"
            auth = root / "pkg" / "auth.py"
            entry.write_text("from .auth import login\n", encoding="utf-8")
            auth.write_text("def login(): pass\n", encoding="utf-8")
            manifest, edges = build_manifest_and_dependency_manifest([str(entry), str(auth)], str(root))
        self.assertEqual(set(manifest), {"pkg/main.py", "pkg/auth.py"})
        self.assertEqual(edges[0]["target_file"], "pkg/auth.py")

    def test_file_manifest_changes_only_when_content_changes(self):
        from ingest.pipeline import build_file_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text("print('one')\n", encoding="utf-8")
            first = build_file_manifest([str(source)], str(root))
            source.write_text("print('two')\n", encoding="utf-8")
            second = build_file_manifest([str(source)], str(root))
        self.assertEqual(set(first), {"app.py"})
        self.assertNotEqual(first["app.py"]["content_hash"], second["app.py"]["content_hash"])

    def test_impact_retrieval_query_is_parameterized(self):
        from retrieval.retriever import dependent_file_chunks

        store = MemoryStore()
        store.fetch_all = AsyncMock(return_value=[{"id": "caller", "file_path": "src/main.py"}])
        rows = asyncio.run(dependent_file_chunks(store, "repo-1", ["auth.py"]))
        self.assertEqual(rows[0]["id"], "caller")
        sql, args = store.fetch_all.call_args.args
        self.assertIn("repo_dependencies", sql)
        self.assertEqual(args[:3], ["repo-1", "auth.py", "%/auth.py"])

    def test_repository_impact_endpoint_returns_only_resolved_dependents(self):
        from api.repos_router import repository_impact

        store = MemoryStore()
        store.fetch_all = AsyncMock(side_effect=[
            [{"file_path": "src/auth.py"}],
            [{"source_file": "src/main.py", "target_file": "src/auth.py", "import_name": "src.auth", "line_number": 4}],
            [{"id": "caller", "file_path": "src/main.py", "start_line": 1, "end_line": 8}],
            [{"target_file": "src/session.py"}],
        ])
        user = SimpleNamespace(id="user-1")
        with patch("api.repos_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.repos_router.get_turso_store", return_value=store), \
             patch("api.repos_router.owned_repo", new=AsyncMock(return_value={"id": "repo-1"})):
            response = asyncio.run(repository_impact("demo", "src/auth.py", 20, user))

        self.assertEqual(response["target_files"], ["src/auth.py"])
        self.assertEqual(response["dependent_files"], ["src/main.py"])
        self.assertEqual(response["edges"][0]["line_number"], 4)
        self.assertEqual(response["imported_files"], ["src/session.py"])

    def test_repository_impact_rejects_path_traversal(self):
        from api.repos_router import repository_impact
        user = SimpleNamespace(id="user-1")
        with self.assertRaises(Exception) as raised:
            asyncio.run(repository_impact("demo", "../secrets.env", 20, user))
        self.assertEqual(getattr(raised.exception, "status_code", None), 422)

    def test_worker_claim_keeps_turso_row_as_plain_mapping(self):
        from ingest.pipeline import claim_next_ingestion_job

        class ClaimStore(MemoryStore):
            async def fetch_all(self, sql, args=None):
                return []

            async def execute(self, sql, args=None):
                self.executed.append((sql, args or []))
                return SimpleNamespace(rows=[{
                    "id": "job-1", "repo_id": "repo-1", "user_id": "user-1",
                    "github_url": "https://github.com/octocat/Hello-World.git", "attempts": 1,
                }], rows_affected=1)

        job = asyncio.run(claim_next_ingestion_job(ClaimStore()))
        self.assertEqual(job["id"], "job-1")
        self.assertIn("claim_token", job)

    def test_retrieval_prioritizes_requested_file_and_survives_missing_embeddings(self):
        from retrieval.retriever import retrieve_context
        store = MemoryStore()
        schema = [{"id": "schema-1", "file_path": "sql/schema.sql", "start_line": 1, "end_line": 2, "language": "sql", "symbols": [], "content": "CREATE TABLE repos"}]
        store.fetch_all = AsyncMock(return_value=schema)
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", "show sql/schema.sql", top_k=2))
        self.assertEqual([chunk["id"] for chunk in result], ["schema-1"])
        self.assertEqual(store.fetch_all.await_count, 1)

    def test_editing_retrieval_keeps_all_bounded_chunks_for_a_named_file(self):
        from retrieval.retriever import retrieve_context

        store = MemoryStore()
        chunks = [
            {"id": f"chunk-{index}", "file_path": "src/auth.py", "start_line": index * 10 + 1,
             "end_line": index * 10 + 9, "language": "py", "symbols": [], "content": f"line {index}"}
            for index in range(12)
        ]
        store.fetch_all = AsyncMock(return_value=chunks)
        result = asyncio.run(retrieve_context(store, "repo-1", "Fix src/auth.py", top_k=8, workflow="editing"))
        self.assertEqual(len(result), len(chunks))
        self.assertTrue(all(chunk["_retrieval_methods"] == ["requested_file"] for chunk in result))

    def test_editing_retrieval_expands_inferred_target_to_complete_file(self):
        """Natural-language edits must not be limited to two sample chunks."""
        from retrieval.retriever import retrieve_context

        store = MemoryStore()
        sampled = {
            "id": "css-1", "file_path": "src/index.css", "start_line": 1, "end_line": 12,
            "language": "css", "symbols": [], "content": ":root { --bg: #f5f1ea; }",
        }
        complete = [
            {**sampled, "id": "css-1"},
            {"id": "css-2", "file_path": "src/index.css", "start_line": 13, "end_line": 24,
             "language": "css", "symbols": [], "content": "body { background: var(--bg); }"},
            {"id": "css-3", "file_path": "src/index.css", "start_line": 25, "end_line": 36,
             "language": "css", "symbols": [], "content": ".dashboard { color: #111; }"},
        ]

        async def fetch_all(sql, args=None):
            store.executed.append((sql, args or []))
            if "ROW_NUMBER() OVER" in sql:
                return complete
            if "JOIN hints h ON lower(c.file_path) LIKE h.pattern" in sql:
                return [sampled | {"matched_hint": "css", "hint_order": 0}]
            if "chunks_fts" in sql:
                return [sampled]
            return []

        store.fetch_all = fetch_all
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(
                store, "repo-1", "Make the dashboard background white", top_k=4, workflow="editing",
            ))

        self.assertEqual([chunk["file_path"] for chunk in result], ["src/index.css"] * 3)
        self.assertEqual([chunk["start_line"] for chunk in result], [1, 13, 25])
        self.assertTrue(all("editing_target_file" in chunk["_retrieval_methods"] for chunk in result))

    def test_requested_file_paths_escape_like_wildcards(self):
        from retrieval.retriever import requested_file_chunks

        store = MemoryStore()
        store.fetch_all = AsyncMock(return_value=[])
        asyncio.run(requested_file_chunks(store, "repo-1", "src/api_%2Fauth.py", 4))
        sql, args = store.fetch_all.call_args.args
        self.assertIn("ESCAPE", sql)
        self.assertEqual(args[1], "src/api_%2fauth.py")
        self.assertEqual(args[2], "src/api\\_\\%2fauth.py")

    def test_technical_question_does_not_force_a_readme_citation(self):
        from retrieval.retriever import is_exploratory_repository_question, retrieve_context

        self.assertFalse(is_exploratory_repository_question("Explain how authentication works", []))
        auth_chunk = [{
            "id": "auth-1", "file_path": "backend/api/auth.py", "start_line": 12, "end_line": 30,
            "language": "py", "symbols": ["get_current_user"], "content": "def get_current_user(): pass",
        }]
        store = MemoryStore()
        store.fetch_all = AsyncMock(return_value=auth_chunk)
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = asyncio.run(retrieve_context(store, "repo-1", "Explain how authentication works", top_k=4))
        self.assertEqual([chunk["file_path"] for chunk in result], ["backend/api/auth.py"])
        self.assertFalse(any("readme" in sql.lower() for sql, _ in store.executed))

    def test_query_endpoint_persists_user_and_assistant_messages(self):
        from api.query_router import query_repo
        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="Where is login?", model_profile="fast")
        chunks = [{"id": "chunk-1", "file_path": "src/auth.py", "start_line": 1, "end_line": 5, "language": "py", "symbols": ["login"], "content": "def login(): pass"}]
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])), \
             patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=("## Direct answer\nLogin is in auth.py.", []))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["mode"], "rag")
        self.assertEqual(response["citations"][0]["support_status"], "source-backed")
        self.assertTrue(response["citations"][0]["retrieval_reasons"])
        self.assertEqual(sum("INSERT INTO chat_messages" in sql for sql, _ in store.executed), 2)

    def test_query_returns_source_answer_when_history_write_is_temporarily_unavailable(self):
        from api.query_router import query_repo

        store = MemoryStore()
        original_execute = store.execute

        async def flaky_execute(sql, args=None):
            if "chat_messages" in sql:
                raise RuntimeError("Hrana stream not found")
            return await original_execute(sql, args)

        store.execute = flaky_execute
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="Where is login?", model_profile="fast")
        chunks = [{"id": "chunk-1", "file_path": "src/auth.py", "start_line": 1, "end_line": 5, "language": "py", "symbols": ["login"], "content": "def login(): pass"}]
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])), \
             patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=("## Direct answer\nLogin is in auth.py.", []))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["mode"], "rag")
        self.assertIn("Login", response["answer"])

    def test_query_continues_when_optional_history_read_fails(self):
        from api.query_router import query_repo

        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="Where is login?", model_profile="fast")
        chunks = [{"id": "chunk-1", "file_path": "src/auth.py", "start_line": 1, "end_line": 5, "language": "py", "symbols": ["login"], "content": "def login(): pass"}]
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(side_effect=RuntimeError("Hrana stream not found"))), \
             patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=("## Direct answer\nLogin is in auth.py.", []))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["mode"], "rag")
        self.assertIn("Login", response["answer"])

    def test_query_endpoint_drops_overview_citations_at_api_boundary(self):
        from api.query_router import query_repo

        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="How do I edit the frontend?", model_profile="fast")
        chunks = [
            {"id": "readme-1", "file_path": "README.md", "start_line": 1, "end_line": 8,
             "language": "md", "symbols": [], "content": "The frontend is described here."},
            {"id": "app-1", "file_path": "src/App.jsx", "start_line": 1, "end_line": 5,
             "language": "jsx", "symbols": ["App"], "content": "export default function App() {}"},
        ]
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])), \
             patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=("## Direct answer\nEdit App.jsx.", []))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual([citation["file_path"] for citation in response["citations"]], ["src/App.jsx"])

    def test_query_response_exposes_the_selected_evidence_workflow(self):
        from api.query_router import query_repo
        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="Review authentication", model_profile="fast", workflow="security")
        chunks = [{"id": "chunk-1", "file_path": "src/auth.py", "start_line": 1, "end_line": 5, "language": "py", "symbols": [], "content": "def login(): pass", "_retrieval_reasons": ["Security review target matching the 'auth' path hint"]}]
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])), \
             patch("api.query_router.run_agent_loop", new=AsyncMock(return_value=("## Direct answer\nAuthentication is in auth.py.", []))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["workflow"], "security")
        self.assertEqual(response["evidence_plan"]["workflow"], "security")

    def test_editing_query_returns_a_scoped_ticket_and_code_profile(self):
        from api.query_router import query_repo
        from agent.code_edit import verify_edit_ticket
        from config import settings

        store = MemoryStore()
        user = SimpleNamespace(id="user-edit")
        request = SimpleNamespace(repo_name="demo", question="Fix src/auth.py", model_profile="fast", workflow="editing")
        chunks = [{
            "id": "chunk-1", "file_path": "src/auth.py", "start_line": 1, "end_line": 3,
            "language": "py", "symbols": [], "content": "def login():\n    return True",
        }]
        suggestion = {
            "id": "11111111-2222-3333-4444-555555555555",
            "title": "Use the validated session helper",
            "files": [{
                "file_path": "src/auth.py", "action": "modify", "summary": "Use the validated session helper",
                "changes": [{"old": "def login():\n    return True", "new": "def login():\n    return validate_session()", "reason": ""}],
                "before": "def login():\n    return True\n", "after": "def login():\n    return validate_session()\n",
                "base_blob_sha": "abc", "additions": 1, "deletions": 1,
            }],
            "file_path": "src/auth.py",
            "validation": ["python -m compileall src/auth.py"],
        }
        with patch.object(settings, "editing_ticket_secret", "test-editing-secret"), \
             patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=chunks)), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])), \
             patch("api.query_router.build_repo_map", new=AsyncMock(return_value={"text": ""})), \
             patch("api.query_router.run_code_edit", new=AsyncMock(return_value=("## Proposed change", [], suggestion))):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["model_profile"], "code")
        self.assertEqual(response["mode"], "editing")
        self.assertEqual(response["edit_suggestion"]["files"][0]["file_path"], "src/auth.py")
        # The proposal is persisted so the review survives a reload.
        self.assertTrue(any("INSERT INTO edit_proposals" in sql for sql, _ in store.executed))
        self.assertEqual(response["workflow"], "editing")
        self.assertIsNotNone(response["edit_ticket"])
        verify_edit_ticket("test-editing-secret", response["edit_ticket"], user_id="user-edit", repo_name="demo", file_path="src/auth.py")

    def test_query_returns_guidance_without_matching_source(self):
        from api.query_router import query_repo
        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="Explain the deployment topology", model_profile="fast")
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock(return_value=[])), \
             patch("api.query_router.get_conversation_history", new=AsyncMock(return_value=[])):
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["mode"], "repository_guidance")
        self.assertIn("explore this repository", response["answer"])

    def test_greeting_skips_retrieval_and_never_calls_the_answer_model(self):
        from api.query_router import query_repo
        store = MemoryStore()
        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(repo_name="demo", question="hi", model_profile="fast")
        with patch("api.query_router.assert_turso_schema", new=AsyncMock()), \
             patch("api.query_router.get_turso_store", return_value=store), \
             patch("api.query_router.get_owned_repo", new=AsyncMock(return_value={"id": "repo-1", "status": "ready"})), \
             patch("api.query_router.retrieve_context", new=AsyncMock()) as retrieve, \
             patch("api.query_router.run_agent_loop", new=AsyncMock()) as answer_model:
            response = asyncio.run(query_repo(request, user))
        self.assertEqual(response["mode"], "greeting")
        self.assertEqual(response["citations"], [])
        self.assertIn("What would you like to understand", response["answer"])
        retrieve.assert_not_awaited()
        answer_model.assert_not_awaited()

    def test_provider_planning_is_rejected_before_it_reaches_chat_history(self):
        from agent.nemotron import LLMProviderError, user_facing_content
        with self.assertRaises(LLMProviderError):
            user_facing_content("The user said 'hi'. I should respond politely without citations.")
        self.assertEqual(user_facing_content("<think>private plan</think>\nHello."), "Hello.")

    def test_recover_stuck_repos_fixes_intermediate_status_when_job_completed(self):
        from ingest.pipeline import recover_stuck_repos

        class RecoveryStore(MemoryStore):
            async def fetch_all(self, sql, args=None):
                self.executed.append((sql, args or []))
                # Return one stuck repo: status='embedding' but job='completed'
                return [{"id": "repo-stuck", "status": "embedding", "job_status": "completed"}]

            async def fetch_one(self, sql, args=None):
                self.executed.append((sql, args or []))
                return {"count": 42, "error_message": None}

        store = RecoveryStore()
        recovered = asyncio.run(recover_stuck_repos(store))
        self.assertEqual(recovered, 1)
        # Verify the repo was updated to 'ready' with the correct chunk count
        update_calls = [(sql, args) for sql, args in store.executed if "UPDATE repos SET" in sql]
        self.assertEqual(len(update_calls), 1)
        update_sql, update_args = update_calls[0]
        self.assertIn("status = ?", update_sql)
        self.assertIn("ready", update_args)
        self.assertIn(42, update_args)

    def test_recover_stuck_repos_returns_zero_when_no_stuck_repos(self):
        from ingest.pipeline import recover_stuck_repos

        class EmptyStore(MemoryStore):
            async def fetch_all(self, sql, args=None):
                self.executed.append((sql, args or []))
                return []

        store = EmptyStore()
        recovered = asyncio.run(recover_stuck_repos(store))
        self.assertEqual(recovered, 0)
        # No repo UPDATE should have been issued
        update_calls = [(sql, args) for sql, args in store.executed if "UPDATE repos SET" in sql]
        self.assertEqual(len(update_calls), 0)

    def test_finalize_successful_job_prefetches_data_before_job_update(self):
        from ingest.pipeline import finalize_successful_job

        call_order = []

        class FinalizeStore(MemoryStore):
            async def fetch_one(self, sql, args=None):
                self.executed.append((sql, args or []))
                if "SELECT COUNT" in sql:
                    call_order.append("fetch_chunk_count")
                    return {"count": 10}
                if "SELECT error_message" in sql:
                    call_order.append("fetch_error_message")
                    return {"error_message": None}
                return None

            async def execute(self, sql, args=None):
                self.executed.append((sql, args or []))
                if "UPDATE ingestion_jobs SET status = 'completed'" in sql:
                    call_order.append("job_update")
                    return SimpleNamespace(rows=[{"id": "job-1"}], rows_affected=1)
                if "UPDATE repos SET" in sql:
                    call_order.append("repo_update")
                    return SimpleNamespace(rows=[], rows_affected=1)
                return SimpleNamespace(rows=[], rows_affected=1)

        store = FinalizeStore()
        job = {"id": "job-1", "repo_id": "repo-1", "claim_token": "token-1"}
        result = asyncio.run(finalize_successful_job(store, job))
        self.assertTrue(result)
        # Critical: data must be fetched BEFORE the job UPDATE, and repo UPDATE after
        self.assertEqual(call_order, ["fetch_chunk_count", "fetch_error_message", "job_update", "repo_update"])


if __name__ == "__main__":
    unittest.main()
