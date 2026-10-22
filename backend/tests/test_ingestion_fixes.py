"""Regression tests for ingestion, index-integrity and explore-endpoint fixes.

Covers the QA findings IH-1..IH-6, F1..F9, EXP-1..EXP-5 and cite-where-07
(line-aligned chunks for files with one long line). Every test is offline and
uses a temporary embedded libSQL file database.
"""

import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import ingest.pipeline as pipeline
from config import settings
from database import Statement, TursoStore, apply_migrations
from ingest import cloner
from ingest.chunker import MAX_CHARS_PER_CHUNK, chunk_file_content, split_by_characters
from ingest.cloner import (
    RepositoryCloneError,
    RepositorySnapshot,
    RepositoryValidationError,
    SourceFetchCancelled,
    file_priority,
    get_file_selection_report,
    parse_github_ref,
    repository_source_url,
)
from ingest.dependencies import build_manifest_and_dependency_manifest

USER = "user-1"
URL = "https://github.com/octocat/Hello-World.git"


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")


def iso(seconds_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'ingest.db'}", "local-test-token")
        await apply_migrations(self.store)

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    async def add_repo(self, repo_id="repo-1", name="Hello-World", status="queued", chunk_count=0):
        now = iso()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [repo_id, USER, name, URL, status, chunk_count, now, now],
        )

    async def add_job(self, repo_id="repo-1", status="processing", heartbeat_seconds_ago=5, claim="claim-1",
                      updated_seconds_ago=0, github_url=URL):
        await self.store.execute(
            "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, attempts, claimed_at, heartbeat_at, "
            "claim_token, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            [f"job-{repo_id}", repo_id, USER, github_url, status, iso(heartbeat_seconds_ago),
             iso(heartbeat_seconds_ago) if status == "processing" else None,
             claim if status == "processing" else None, iso(600), iso(updated_seconds_ago)],
        )

    async def add_chunks(self, repo_id, prefix, path, count, start=1, content="x = 1"):
        await self.store.insert_chunks([
            {"id": f"{prefix}{index}", "repo_id": repo_id, "file_path": path, "start_line": start + index,
             "end_line": start + index, "language": "py", "symbols": [], "content": content, "embedding": None}
            for index in range(count)
        ])

    async def count(self, sql, args):
        row = await self.store.fetch_one(sql, args)
        return int((row or {}).get("count") or 0)

    async def repo_view(self, name="Hello-World"):
        from api.repository_state import fetch_owned_repository
        return await fetch_owned_repository(self.store, USER, name)


# ---------------------------------------------------------------------------
# cite-where-07 / EXP-5 / RET-8: line-aligned chunks
# ---------------------------------------------------------------------------


class LineAlignedChunkTests(unittest.TestCase):
    def assert_aligned(self, text, chunks):
        lines = text.split("\n")
        for chunk in chunks:
            expected = "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]])
            if chunk["start_line"] == chunk["end_line"] and len(expected) > MAX_CHARS_PER_CHUNK:
                self.assertIn(chunk["content"], expected)
            else:
                self.assertEqual(chunk["content"], expected, (chunk["start_line"], chunk["end_line"]))
            self.assertLessEqual(len(chunk["content"]), MAX_CHARS_PER_CHUNK)

    def test_one_long_comment_keeps_an_ordinary_file_line_aligned(self):
        text = "\n".join(
            [f"export const value{i} = computeSomething({i});" for i in range(600)]
            + ["// " + "x" * 2_100]
            + [f"export const tail{i} = {i};" for i in range(50)]
        )
        chunks = chunk_file_content(text, "src/constants.ts")
        self.assert_aligned(text, chunks)
        covered = set()
        for chunk in chunks:
            covered.update(range(chunk["start_line"], chunk["end_line"] + 1))
        self.assertEqual(covered, set(range(1, 652)))

    def test_only_a_line_over_the_ceiling_is_split_by_characters(self):
        long_line = "".join(chr(ord("a") + index % 26) for index in range(30_000))
        text = "\n".join([f"line {i}" for i in range(300)] + [long_line] + [f"after {i}" for i in range(300)])
        chunks = chunk_file_content(text, "openapi.json")
        self.assert_aligned(text, chunks)
        fragments = [chunk for chunk in chunks if chunk["start_line"] == chunk["end_line"] == 301]
        self.assertGreaterEqual(len(fragments), 3)
        # No window repeats lines already held by the previous chunk.
        multi = [chunk for chunk in chunks if chunk["start_line"] != chunk["end_line"]]
        self.assertLessEqual(len(multi), 8)


# ---------------------------------------------------------------------------
# IH-1 / EXP-1 / EXP-2: manifest keys and the dependency graph
# ---------------------------------------------------------------------------


class DependencyManifestTests(unittest.TestCase):
    def build(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, files)
            return build_manifest_and_dependency_manifest([str(root / path) for path in files], str(root))

    def test_hidden_paths_keep_their_leading_dot(self):
        manifest, edges = self.build({
            ".github/workflows/ci.yml": "name: ci\n",
            ".env.example": "A=1\n",
            ".storybook/main.ts": "import { a } from '../src/a'\n",
            "src/a.ts": "export const a = 1\n",
        })
        self.assertEqual(set(manifest), {".github/workflows/ci.yml", ".env.example", ".storybook/main.ts", "src/a.ts"})
        self.assertEqual([(edge["source_file"], edge["target_file"]) for edge in edges], [(".storybook/main.ts", "src/a.ts")])

    def test_every_common_javascript_import_form_resolves(self):
        _manifest, edges = self.build({
            "src/app.module.ts": (
                "import { AppController } from './app.controller';\n"
                "export * from './selects';\n"
                "export { b } from \"./b\";\n"
                "import {\n  c,\n  d,\n} from './c';\n"
                "import './side';\n"
                "const lazy = () => import('./lazy');\n"
                "import type { T } from './types.js';\n"
            ),
            "src/app.controller.ts": "export class AppController {}\n",
            "src/selects/index.ts": "export const s = 1\n",
            "src/b.ts": "", "src/c.tsx": "", "src/side.ts": "", "src/lazy.ts": "", "src/types.ts": "",
        })
        targets = {edge["target_file"] for edge in edges if edge["source_file"] == "src/app.module.ts"}
        self.assertEqual(targets, {
            "src/app.controller.ts", "src/selects/index.ts", "src/b.ts", "src/c.tsx", "src/side.ts",
            "src/lazy.ts", "src/types.ts",
        })

    def test_workspace_packages_and_tsconfig_aliases_resolve(self):
        _manifest, edges = self.build({
            "packages/lib/package.json": '{"name": "@calcom/lib", "main": "index.ts"}',
            "packages/lib/index.ts": "export {}\n",
            "packages/lib/constants.ts": "export const A = 1\n",
            "packages/prisma/package.json": '{"name": "@calcom/prisma"}',
            "packages/prisma/index.ts": "export {}\n",
            "apps/web/tsconfig.json": '{\n  // comment\n  "compilerOptions": {"baseUrl": ".", "paths": {"@components/*": ["components/*"],},},\n}',
            "apps/web/components/Button.tsx": "export {}\n",
            "apps/web/pages/index.tsx": (
                "import { A } from '@calcom/lib/constants';\n"
                "import prisma from '@calcom/prisma';\n"
                "import lib from '@calcom/lib';\n"
                "import Button from '@components/Button';\n"
                "import React from 'react';\n"
            ),
        })
        targets = {edge["target_file"] for edge in edges if edge["source_file"] == "apps/web/pages/index.tsx"}
        self.assertEqual(targets, {
            "packages/lib/constants.ts", "packages/prisma/index.ts", "packages/lib/index.ts",
            "apps/web/components/Button.tsx",
        })

    def test_python_relative_and_absolute_imports_still_resolve(self):
        _manifest, edges = self.build({
            "pkg/__init__.py": "",
            "pkg/models.py": "x = 1\n",
            "pkg/views.py": "from .models import x\nimport pkg.models\n",
            "README.md": "import x from './pkg/models'\n",
        })
        pairs = {(edge["source_file"], edge["target_file"]) for edge in edges}
        self.assertEqual(pairs, {("pkg/views.py", "pkg/models.py")})


# ---------------------------------------------------------------------------
# IH-2 / IH-3 / IH-4: selection fairness, categories and estimates
# ---------------------------------------------------------------------------


class SelectionPolicyTests(unittest.TestCase):
    def test_migrations_of_any_language_and_conftest_are_deprioritised(self):
        self.assertLess(file_priority("posthog/migrations/0001_initial.py", 1_000), 60)
        self.assertLess(file_priority("posthog/conftest.py", 1_000), 80)
        self.assertLess(file_priority("src/api.mock.ts", 1_000), 80)
        # Depth is only a tie-break inside the code band.
        self.assertGreaterEqual(file_priority("frontend/src/scenes/flags/featureFlagLogic.ts", 1_000), 76)

    def test_budget_is_shared_across_directories_instead_of_by_depth(self):
        files = {f"frontend/src/lib/file{i:02d}.ts": "x\n" for i in range(20)}
        files.update({f"frontend/src/scenes/flags/logic{i:02d}.ts": "x\n" for i in range(20)})
        files.update({f"rust/flags/src/match{i:02d}.rs": "x\n" for i in range(20)})
        files.update({f"posthog/migrations/{i:04d}_m.py": "x\n" for i in range(20)})
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(Path(tmp), files)
            report = get_file_selection_report(tmp, chunk_budget=30)
        selected = [os.path.relpath(path, tmp) for path in report["files"]]
        self.assertEqual(len(selected), 30)
        self.assertGreaterEqual(sum(path.startswith("frontend/src/scenes/") for path in selected), 8)
        self.assertGreaterEqual(sum(path.startswith("rust/") for path in selected), 8)
        self.assertFalse(any("migrations" in path for path in selected))
        categories = report["budget_omitted_by_category"]
        self.assertEqual(categories.get("migrations"), 20)
        self.assertEqual(sum(categories.values()), report["budget_omitted_files"])
        sample = report["budget_omitted_sample"]
        self.assertEqual(sample[0]["category"], "source")
        self.assertEqual(sample[-1]["category"], "migrations")

    def test_completion_message_reports_the_real_breakdown(self):
        message = pipeline._completion_message(
            {"budget_omitted_files": 30, "selected_files": 70,
             "budget_omitted_by_category": {"source": 20, "tests": 10}}, None,
        )
        self.assertIn("20 source", message)
        self.assertIn("10 tests", message)
        self.assertNotIn("generated code, or data", message)

    def test_chunk_estimate_is_calibrated(self):
        self.assertEqual(cloner.estimate_chunks(0), 0)
        self.assertEqual(cloner.estimate_chunks(5_000), 1)
        self.assertAlmostEqual(cloner.estimate_chunks(42_000), 10)


# ---------------------------------------------------------------------------
# F8: branch / commit links
# ---------------------------------------------------------------------------


class RefTests(unittest.TestCase):
    def test_tree_blob_and_commit_links_are_parsed(self):
        tree = parse_github_ref("https://github.com/sindresorhus/ky/tree/feature/with/slashes")
        self.assertEqual(tree.candidates(), ["feature/with/slashes", "feature/with", "feature"])
        blob = parse_github_ref("https://github.com/o/r/blob/main/src/app.py")
        self.assertEqual(blob.candidates(), ["main/src", "main"])
        commit = parse_github_ref("https://github.com/o/r/commit/0123456789abcdef0123")
        self.assertEqual(commit.candidates(), ["0123456789abcdef0123"])
        self.assertIsNone(parse_github_ref("https://github.com/o/r"))
        self.assertIsNone(parse_github_ref("https://github.com/o/r/issues/3"))
        with self.assertRaises(RepositoryValidationError):
            parse_github_ref("https://github.com/o/r/commit/not-a-sha")
        with self.assertRaises(RepositoryValidationError):
            parse_github_ref("https://github.com/o/r/tree/a..b/../x")

    def test_source_url_round_trips_and_keeps_the_repository_identity(self):
        source = repository_source_url("github.com/o/r/tree/release/v2")
        self.assertEqual(source, "https://github.com/o/r/tree/release/v2")
        self.assertEqual(cloner.normalize_github_url(source), "https://github.com/o/r.git")
        self.assertEqual(parse_github_ref(source).candidates()[0], "release/v2")
        self.assertEqual(repository_source_url("o/r"), "https://github.com/o/r.git")

    def test_archive_fetch_tries_each_possible_ref_split(self):
        requested = []

        def fake_download(request, destination, deadline, should_stop=None):
            requested.append(request.full_url.rsplit("/tar.gz/", 1)[1])
            if len(requested) < 2:
                raise RepositoryCloneError("missing", not_found=True)
            return RepositorySnapshot(path=destination, commit_sha="abc1234", source="archive")

        with patch.object(cloner, "_download_and_extract", side_effect=fake_download):
            snapshot = cloner.acquire_repository_source("https://github.com/o/r/tree/feature/x/src")
        cloner.cleanup_repo(snapshot.path)
        self.assertEqual(requested, ["feature/x/src", "feature/x"])
        self.assertEqual(snapshot.ref, "feature/x")

    def test_unknown_ref_is_reported_clearly(self):
        with patch.object(cloner, "_download_and_extract", side_effect=RepositoryCloneError("x", not_found=True)):
            with self.assertRaises(RepositoryCloneError) as raised:
                cloner.fetch_repository_archive("o/r", parse_github_ref("o/r/tree/nope"))
        self.assertIn("'nope'", str(raised.exception))

    def test_stop_aborts_the_archive_stream(self):
        reader = cloner._ArchiveStreamReader(SimpleNamespace(read=lambda size: b"x" * 10), 10**9, 10**12, lambda: True)
        with self.assertRaises(SourceFetchCancelled):
            reader.read(10)


# ---------------------------------------------------------------------------
# F7 / F9: names and request validation
# ---------------------------------------------------------------------------


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dot_only_repository_names_are_rejected(self):
        from api.repos_router import RenameRepositoryRequest, rename_repo
        for name in (".", "..", "...", " .. "):
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as raised:
                    await rename_repo("demo", RenameRepositoryRequest(repo_name=name), SimpleNamespace(id=USER))
                self.assertEqual(raised.exception.status_code, 422)
        self.assertTrue(pipeline.is_safe_repo_label(".github"))
        self.assertFalse(pipeline.is_safe_repo_label(".."))

    async def test_invalid_urls_do_not_use_the_rate_limit(self):
        from api import ingest_router
        limiter = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
        with patch.object(ingest_router, "ingest_request_limiter", limiter):
            for _ in range(3):
                with self.assertRaises(HTTPException) as raised:
                    await ingest_router.ingest_repo(ingest_router.IngestRequest(github_url="not a url"), SimpleNamespace(id=USER))
                self.assertEqual(raised.exception.status_code, 400)
        limiter.acquire.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pipeline end to end on a local database
# ---------------------------------------------------------------------------


async def fake_embed(chunks, **kwargs):
    for chunk in chunks:
        chunk["embedding"] = [0.01] * settings.embedding_dimension
    return chunks


def fake_embed_sync(chunks, **kwargs):
    for chunk in chunks:
        chunk["embedding"] = [0.01] * settings.embedding_dimension
    return chunks


class PipelineTests(DatabaseCase):
    async def ingest(self, files, *, repo_id="repo-1", url=URL, claim=None):
        source = Path(self._tmp.name) / f"src-{len(os.listdir(self._tmp.name))}"
        write_tree(source, files)
        snapshot = RepositorySnapshot(path=str(source), commit_sha="abc1234", source="archive")
        with patch.object(pipeline, "acquire_repository_source", return_value=snapshot) as acquire, \
             patch.object(pipeline, "cleanup_repo"), \
             patch.object(pipeline, "embed_chunks", side_effect=fake_embed_sync), \
             patch.object(pipeline, "spawn_background", side_effect=lambda coroutine: coroutine.close()):
            result = await pipeline.run_ingestion_for_repo(
                self.store, url, USER, repo_id, job_id=f"job-{repo_id}" if claim else None, claim_token=claim,
            )
        return result, acquire

    async def test_hidden_files_are_chunked_under_their_real_paths(self):
        await self.add_repo()
        ok, _ = await self.ingest({
            ".github/workflows/e2e.yml": "name: e2e\non: push\njobs: {}\n",
            ".env.example": "DATABASE_URL=postgres://localhost\n",
            "src/app.py": "def main():\n    return 1\n",
        })
        self.assertTrue(ok)
        rows = await self.store.fetch_all("SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? ORDER BY file_path", ["repo-1"])
        self.assertEqual([row["file_path"] for row in rows], [".env.example", ".github/workflows/e2e.yml", "src/app.py"])
        coverage = await self.store.fetch_one("SELECT excluded_reasons FROM repo_coverage WHERE repo_id = ?", ["repo-1"])
        self.assertNotIn("empty_or_unreadable", coverage["excluded_reasons"] or {})
        # The finished first index is published: no unpublished-run marker.
        self.assertEqual(await self.count("SELECT COUNT(*) AS count FROM repo_index_runs WHERE repo_id = ?", ["repo-1"]), 0)
        view = await self.repo_view()
        self.assertEqual(view["status"], "ready")
        self.assertFalse(view["refreshing"])

    async def test_job_url_ref_is_fetched_and_remembered_for_reindex(self):
        await self.add_repo()
        ok, acquire = await self.ingest({"src/app.py": "x = 1\n"}, url="https://github.com/octocat/Hello-World/tree/dev")
        self.assertTrue(ok)
        self.assertEqual(acquire.call_args.args[0], "https://github.com/octocat/Hello-World/tree/dev")

    async def test_refresh_swap_is_one_transaction(self):
        await self.add_repo()
        await self.ingest({"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})
        batches = []
        original = self.store.batch

        async def recording_batch(statements):
            statements = list(statements)
            batches.append([statement.sql for statement in statements])
            await original(statements)

        with patch.object(self.store, "batch", side_effect=recording_batch):
            ok, _ = await self.ingest({"src/a.py": "a = 2\n", "src/c.py": "c = 1\n"})
        self.assertTrue(ok)
        swap = [batch for batch in batches if "DELETE FROM repo_index_runs WHERE repo_id = ?" in batch]
        self.assertEqual(len(swap), 1)
        self.assertTrue(any(sql.startswith("DELETE FROM chunks") for sql in swap[0]))
        files = await self.store.fetch_all("SELECT file_path, content FROM chunks WHERE repo_id = ? ORDER BY file_path", ["repo-1"])
        self.assertEqual([(row["file_path"], row["content"]) for row in files], [("src/a.py", "a = 2"), ("src/c.py", "c = 1")])


class LifecycleTests(DatabaseCase):
    async def test_stop_after_restart_never_publishes_a_partial_first_index(self):
        # A first index was being built when the worker died; its rows are
        # keyword-visible and the repository says "ready" (searchable).
        await self.add_repo(status="ready", chunk_count=3)
        await self.add_job(heartbeat_seconds_ago=30)  # still looks alive
        await pipeline.begin_index_run(self.store, "repo-1", "job-repo-1", "job-repo-1-run-", initial=True)
        await self.add_chunks("repo-1", "job-repo-1-run-", "src/a.py", 3)
        outcome = await pipeline.cancel_repository_indexing(self.store, "repo-1", USER)
        self.assertEqual(outcome, "stopping")
        view = await self.repo_view()
        self.assertEqual(view["status"], "cancelled")
        self.assertFalse(view["queryable"])
        # Nobody finishes the cleanup; the sweep does once the stop is stale.
        await self.store.execute("UPDATE ingestion_jobs SET updated_at = ? WHERE repo_id = ?", [iso(600), "repo-1"])
        self.assertEqual(await pipeline.recover_orphaned_runs(self.store), 1)
        self.assertEqual(await self.count("SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ?", ["repo-1"]), 0)
        view = await self.repo_view()
        self.assertEqual(view["status"], "cancelled")
        self.assertEqual(view["chunk_count"], 0)

    async def test_stop_of_a_released_first_index_cleans_up_immediately(self):
        await self.add_repo(status="ready", chunk_count=2)
        await self.add_job(status="queued")
        await pipeline.begin_index_run(self.store, "repo-1", "job-repo-1", "p1-", initial=True)
        await self.add_chunks("repo-1", "p1-", "src/a.py", 2)
        self.assertEqual(await pipeline.cancel_repository_indexing(self.store, "repo-1", USER), "stopped")
        self.assertEqual(await self.count("SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ?", ["repo-1"]), 0)
        self.assertEqual((await self.repo_view())["status"], "cancelled")

    async def test_stopped_refresh_keeps_the_previous_index(self):
        await self.add_repo(status="chunking", chunk_count=2)
        await self.add_chunks("repo-1", "old-", "src/a.py", 2)
        await self.add_job(heartbeat_seconds_ago=600)
        await pipeline.begin_index_run(self.store, "repo-1", "job-repo-1", "new-")
        await self.add_chunks("repo-1", "new-", "src/a.py", 2)
        self.assertEqual(await pipeline.cancel_repository_indexing(self.store, "repo-1", USER), "stopped")
        rows = await self.store.fetch_all("SELECT id FROM chunks WHERE repo_id = ? ORDER BY id", ["repo-1"])
        self.assertEqual([row["id"] for row in rows], ["old-0", "old-1"])
        self.assertEqual((await self.repo_view())["status"], "ready")

    async def test_dead_worker_lease_is_requeued_within_the_liveness_window(self):
        await self.add_repo(status="chunking")
        await self.add_job(heartbeat_seconds_ago=pipeline.WORKER_LIVENESS_SECONDS + 5)
        with patch.object(settings, "ingestion_job_timeout_seconds", 900):
            await pipeline.requeue_stale_jobs(self.store)
        job = await self.store.fetch_one("SELECT status FROM ingestion_jobs WHERE repo_id = ?", ["repo-1"])
        self.assertEqual(job["status"], "queued")

    async def test_shutdown_hands_the_claimed_job_back(self):
        await self.add_repo()
        await self.add_job(status="queued")

        async def interrupted(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(pipeline, "run_ingestion_for_repo", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await pipeline.process_one_queued_ingestion(self.store)
        job = await self.store.fetch_one("SELECT status, claim_token, attempts FROM ingestion_jobs WHERE repo_id = ?", ["repo-1"])
        self.assertEqual((job["status"], job["claim_token"], job["attempts"]), ("queued", None, 1))

    async def test_new_attempt_discards_rows_of_an_interrupted_first_index(self):
        await self.add_repo(status="ready", chunk_count=2)
        await pipeline.begin_index_run(self.store, "repo-1", "job-repo-1", "dead-", initial=True)
        await self.add_chunks("repo-1", "dead-", "src/old.py", 2)
        await pipeline.discard_staged_rows(self.store, "repo-1")
        self.assertEqual(await pipeline.get_repo_chunk_count(self.store, "repo-1"), 0)


class PresentationTests(unittest.TestCase):
    def present(self, **row):
        from api.repository_state import present_repository
        base = {"repo_status": "ready", "index_phase": "ready", "chunk_count": 5, "github_url": URL}
        base.update(row)
        return present_repository(base)

    def test_stop_during_download_is_never_failed(self):
        view = self.present(repo_status="cloning", index_phase="cloning", chunk_count=0, job_status="cancelled")
        self.assertEqual(view["status"], "cancelled")
        self.assertFalse(view["is_indexing"])

    def test_ready_phase_with_open_job_does_not_flash_queued(self):
        view = self.present(index_phase="ready", job_status="processing")
        self.assertEqual(view["status"], "searchable")

    def test_first_index_is_not_called_a_refresh(self):
        first = self.present(index_phase="searchable", job_status="processing", run_prefix="initial:job-1-x-")
        self.assertFalse(first["refreshing"])
        refresh = self.present(index_phase="embedding", job_status="processing", run_prefix="job-2-y-",
                               commit_sha="abc1234")
        self.assertTrue(refresh["refreshing"])
        self.assertEqual(refresh["status"], "embedding")


# ---------------------------------------------------------------------------
# Explore endpoints: F5, EXP-3, EXP-4, EXP-5
# ---------------------------------------------------------------------------


class ExploreTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_repo(status="ready", chunk_count=1)
        await self.store.execute(
            "INSERT INTO repo_snapshots (repo_id, commit_sha, source, selection_json, fetched_at) VALUES (?, ?, 'archive', '{}', ?)",
            ["repo-1", "abc1234", iso()],
        )
        self.user = SimpleNamespace(id=USER)

    def patches(self):
        import api.explore_router as explore
        import api.repos_router as repos
        return (
            patch.object(explore, "get_turso_store", return_value=self.store),
            patch.object(explore, "assert_turso_schema", new=AsyncMock()),
            patch.object(explore, "fetch_raw_file", new=AsyncMock(return_value=None)),
            patch.object(repos, "get_turso_store", return_value=self.store),
            patch.object(repos, "assert_turso_schema", new=AsyncMock()),
        )

    async def add_manifest(self, path, byte_size):
        await self.store.execute(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
            ["repo-1", path, "h", byte_size, iso()],
        )

    async def view_file(self, path):
        from api.explore_router import repository_file
        p = self.patches()
        with p[0], p[1], p[2], p[3], p[4]:
            return await repository_file("Hello-World", path, self.user)

    async def test_file_view_rebuilds_long_line_fragments_and_legacy_character_windows(self):
        long_line = "".join(chr(ord("a") + index % 26) for index in range(30_000))
        text = "\n".join([f"line {i}," for i in range(200)] + [long_line] + [f"tail {i}" for i in range(100)]) + "\n"
        chunks = chunk_file_content(text, "new.json")
        legacy = split_by_characters(text, "old.json", "json")
        rows = []
        for path, items in (("new.json", chunks), ("old.json", legacy)):
            for index, chunk in enumerate(items):
                rows.append({**chunk, "id": f"{path}-{index}", "repo_id": "repo-1", "file_path": path, "embedding": None})
        await self.store.insert_chunks(rows)
        for path in ("new.json", "old.json"):
            await self.add_manifest(path, len(text.encode("utf-8")))
        for path in ("new.json", "old.json"):
            response = await self.view_file(path)
            self.assertEqual(response["content"], text, path)
            self.assertTrue(response["exact"])
            self.assertFalse(response["truncated"])
            self.assertEqual(response["line_count"], 301)

    async def test_file_view_restores_line_endings_and_labels_inexact_text(self):
        # EXP-6: chunks hold LF text without the final newline; the manifest
        # byte size decides CRLF / final newline, and says whether it is exact.
        await self.store.insert_chunks([{
            "id": "w-0", "repo_id": "repo-1", "file_path": "win.py", "start_line": 1, "end_line": 2,
            "language": "py", "symbols": [], "content": "a = 1\nb = 2", "embedding": None,
        }])
        await self.add_chunks("repo-1", "w2-", "win.py", 1, start=3, content="c = 3")
        await self.add_manifest("win.py", len(b"a = 1\r\nb = 2\r\nc = 3\r\n"))
        response = await self.view_file("win.py")
        self.assertEqual(response["content"], "a = 1\r\nb = 2\r\nc = 3\r\n")
        self.assertTrue(response["exact"])
        await self.add_chunks("repo-1", "n-", "nomanifest.py", 1, content="x = 1")
        response = await self.view_file("nomanifest.py")
        self.assertEqual(response["content"], "x = 1\n")
        self.assertFalse(response["exact"])

    async def test_file_view_pages_and_reports_truncation(self):
        import api.explore_router as explore
        await self.store.insert_chunks([
            {"id": f"c{index:04d}", "repo_id": "repo-1", "file_path": "big.ts", "start_line": index * 10 + 1,
             "end_line": index * 10 + 10, "language": "ts", "symbols": [],
             "content": "\n".join(f"{index}:{line}" + "y" * 90 for line in range(10)), "embedding": None}
            for index in range(450)
        ])
        with patch.object(explore, "_INDEX_VIEW_MAX_CHARS", 200_000):
            response = await self.view_file("big.ts")
        self.assertTrue(response["truncated"])
        self.assertEqual(response["total_lines"], 4_500)
        self.assertLess(response["line_count"], 4_500)
        full = await self.view_file("big.ts")
        self.assertFalse(full["truncated"])
        self.assertEqual(full["line_count"], 4_500)

    async def test_commit_pinned_github_file_is_fetched_once_and_marked_cacheable(self):
        import api.explore_router as explore
        from fastapi import Response
        await self.add_chunks("repo-1", "p-", "app/pinned.py", 1, content="X = 1")
        fetch = AsyncMock(return_value=b"X = 1\n")
        p = self.patches()
        explore._RAW_FILE_CACHE.clear()
        explore._raw_file_cache_bytes = 0
        try:
            # The fixture snapshot is a short SHA: never cached, no cache header.
            with p[0], p[1], patch.object(explore, "fetch_raw_file", fetch):
                first = Response()
                await explore.repository_file("Hello-World", "app/pinned.py", self.user, first)
                await explore.repository_file("Hello-World", "app/pinned.py", self.user, Response())
            self.assertEqual(fetch.await_count, 2)
            self.assertNotIn("cache-control", first.headers)
            sha = "a" * 40
            await self.store.execute("UPDATE repo_snapshots SET commit_sha = ? WHERE repo_id = ?", [sha, "repo-1"])
            fetch.reset_mock()
            with p[0], p[1], patch.object(explore, "fetch_raw_file", fetch):
                responses = [Response(), Response()]
                views = [await explore.repository_file("Hello-World", "app/pinned.py", self.user, r) for r in responses]
            fetch.assert_awaited_once()
            self.assertEqual(fetch.await_args.kwargs["ref"], sha)
            self.assertTrue(all(view["content"] == "X = 1\n" and view["source"] == "github" for view in views))
            self.assertTrue(all(r.headers["cache-control"] == "private, max-age=3600" for r in responses))
            # The cache is bounded by size, oldest first.
            with patch.object(explore, "_RAW_FILE_CACHE_MAX_BYTES", 40):
                for index in range(5):
                    explore._cache_raw_file(("o", "n", sha, f"f{index}"), "y" * 10)
                self.assertLessEqual(explore._raw_file_cache_bytes, 40)
                self.assertNotIn(("o", "n", sha, "f0"), explore._RAW_FILE_CACHE)
                self.assertIn(("o", "n", sha, "f4"), explore._RAW_FILE_CACHE)
        finally:
            explore._RAW_FILE_CACHE.clear()
            explore._raw_file_cache_bytes = 0

    async def test_hidden_refresh_rows_are_not_served(self):
        await self.add_chunks("repo-1", "old-", "app/core.py", 1, content="OLD = 1")
        await pipeline.begin_index_run(self.store, "repo-1", "job-1", "new-")
        await self.add_chunks("repo-1", "new-", "app/core.py", 1, content="NEW = 2")
        await self.add_chunks("repo-1", "new-x", "app/newmod.py", 1, content="NEW = 3")
        response = await self.view_file("app/core.py")
        self.assertEqual(response["content"], "OLD = 1\n")
        with self.assertRaises(HTTPException) as raised:
            await self.view_file("app/newmod.py")
        self.assertEqual(raised.exception.status_code, 404)
        from api.repos_router import repository_impact
        p = self.patches()
        with p[3], p[4], self.assertRaises(HTTPException) as raised:
            await repository_impact("Hello-World", "app/newmod.py", 20, self.user)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_impact_counts_distinct_dependent_files(self):
        from api.repos_router import repository_impact
        await self.add_chunks("repo-1", "t-", "lib/fixtures.ts", 1)
        statements = []
        for index in range(12):
            path = f"tests/spec{index:02d}.ts"
            await self.add_chunks("repo-1", f"s{index}-", path, 6)
            statements.append(Statement(
                "INSERT INTO repo_dependencies (repo_id, source_file, target_file, import_name, line_number) VALUES (?, ?, ?, ?, ?)",
                ["repo-1", path, "lib/fixtures.ts", "../lib/fixtures", 1],
            ))
        await self.store.batch(statements)
        p = self.patches()
        with p[3], p[4]:
            response = await repository_impact("Hello-World", "lib/fixtures.ts", 5, self.user)
        self.assertEqual(len(response["dependent_files"]), 5)
        self.assertEqual(response["dependent_count"], 12)
        self.assertTrue(response["truncated"])
        self.assertIn("12 indexed file(s)", response["summary"])
        self.assertIn("showing 5", response["summary"])


if __name__ == "__main__":
    unittest.main()
