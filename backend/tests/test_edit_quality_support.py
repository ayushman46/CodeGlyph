"""Shared harness for test_edit_quality*.py (defines no tests).

``KyEditorCase`` indexes real ky files (test_edit_quality_data.KY) into a fresh
embedded database, serves them as the raw GitHub content of
sindresorhus/ky, and runs ``run_code_edit`` with a scripted model. Every
proposal it returns is checked for the invariant the review UI relies on:
``apply_hunks(before, changes) == after`` for every modified file.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_answers_and_editing as base
import test_edit_quality_data as data
from agent.code_edit import apply_hunks
from agent.conventions import clear_conventions_cache
from agent.editor import EditRequest, run_code_edit
from agent.quality import _relative_candidates, esbuild_binary, js_imports, tree_sitter_available
from api.repository_state import fetch_owned_repository
from config import settings
from github.github_client import GitHubFileTooLargeError
from retrieval.retriever import build_evidence_plan

run = asyncio.run
KY_URL = "https://github.com/sindresorhus/ky"
HAS_ESBUILD = esbuild_binary() is not None
HAS_TREE_SITTER = tree_sitter_available("ts")

B1 = "In source/utils/normalize.ts change the default retry limit from 2 to 3."
B2 = ("Add input validation to the delay function in source/utils/delay.ts: throw a TypeError when ms is not a "
      "finite, non-negative number.")
B3 = "Rename isNonArrayObject to isPlainObjectLike in source/utils/is.ts and update every file that imports it."
B4 = ("Add a new ava unit test file test/is.ts that covers isObject, isNonArrayObject and isNonNegativeNumber "
      "from source/utils/is.ts.")


def chunk_rows(path: str, text: str, size: int = 120) -> list[dict]:
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    return [base.chunk(path, start + 1, "\n".join(lines[start:start + size])) for start in range(0, len(lines), size)]


async def seed_index(store, files: dict[str, str], *, repo_id: str = base.REPO_ID, excluded_paths=None,
                     selection: dict | None = None) -> None:
    """Chunks, manifest and import graph for ``files`` (like a real index)."""
    rows = [row for path, text in files.items() for row in chunk_rows(path, text)]
    for offset in range(0, len(rows), 500):
        await store.insert_chunks(rows[offset:offset + 500])
    for path, text in files.items():
        await store.execute(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
            [repo_id, path, hashlib.sha256(text.encode()).hexdigest(), len(text.encode()), "2026-10-01T00:00:00+00:00"],
        )
    for path, text in files.items():
        if not path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
            continue
        for ref in js_imports(text):
            if not ref["spec"].startswith("."):
                continue
            target = next((candidate for candidate in _relative_candidates(path, ref["spec"])[1] if candidate in files), None)
            if target:
                await store.execute(
                    "INSERT OR IGNORE INTO repo_dependencies (repo_id, source_file, target_file, import_name, line_number) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [repo_id, path, target, ref["spec"], ref["line"]],
                )
    if excluded_paths is not None:
        await store.execute(
            "INSERT INTO repo_coverage (repo_id, total_seen_files, eligible_files, indexed_files, excluded_files, "
            "excluded_bytes, excluded_reasons, excluded_paths, updated_at) VALUES (?, ?, ?, ?, ?, 0, '{}', ?, ?)",
            [repo_id, len(files) + len(excluded_paths), len(files), len(files), len(excluded_paths),
             json.dumps(list(excluded_paths)), "2026-10-01T00:00:00+00:00"],
        )
    if selection is not None:
        await store.execute("UPDATE repo_snapshots SET selection_json = ? WHERE repo_id = ?", [json.dumps(selection), repo_id])


def assert_hunk_invariant(case, proposal: dict | None) -> None:
    if not proposal:
        return
    for item in proposal["files"]:
        if item.get("action") == "modify" and isinstance(item.get("before"), str):
            case.assertEqual(apply_hunks(item["before"], item["changes"]), item["after"], item["file_path"])
        if item.get("action") == "create":
            case.assertEqual(item["changes"], [])


class KyEditorCase(base.DatabaseTestCase):
    """Real ky files indexed locally and served as raw GitHub content."""

    FILES = data.KY
    EXCLUDED = (".editorconfig", ".gitattributes", ".gitignore", ".npmrc")

    def setUp(self):
        super().setUp()
        clear_conventions_cache()
        self.addCleanup(clear_conventions_cache)
        run(base.seed_repo(self.store, github_url=KY_URL))
        run(seed_index(self.store, self.FILES, excluded_paths=self.EXCLUDED))
        self.repo = run(fetch_owned_repository(self.store, base.USER, base.REPO_NAME))
        self.raw = {path: text.encode() for path, text in {**self.FILES, **data.KY_HIDDEN}.items()}
        self.fetch_calls: list[str] = []
        self.stages: list[str] = []

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        self.fetch_calls.append(path)
        value = self.raw.get(path)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return await value()
        if value is not None and len(value) > max_bytes:
            raise GitHubFileTooLargeError(f"{path} is too large")
        return value

    def evidence(self, *paths: str) -> list[dict]:
        rows = []
        for path in paths:
            rows.extend(chunk_rows(path, self.FILES[path])[:2])
        return rows

    def edit(self, question: str, responses: list, *, evidence: list[dict] | None = None, review: bool = False,
             token: str | None = None, trivial_lines: int = 0):
        """Run one edit; ``trivial_lines`` > 0 lets a small change skip the review (off by default here)."""
        async def emit(stage, _detail):
            self.stages.append(stage)

        request = EditRequest(
            store=self.store, repo=self.repo, user_id=base.USER, question=question,
            evidence_chunks=list(evidence or []), evidence_plan=build_evidence_plan(question, "editing"),
            repo_map_text="", emit=emit, github_token=token,
        )
        model = base.ScriptedModel(responses)
        with patch("agent.editor.complete", model), patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", AsyncMock(return_value=None)), \
             patch.object(settings, "editing_self_review", review), \
             patch.object(settings, "editing_review_trivial_lines", trivial_lines):
            markdown, tool_calls, proposal = run(run_code_edit(request))
        assert_hunk_invariant(self, proposal)
        for call in tool_calls:
            self.assertLessEqual(len(call.get("result_summary") or ""), 400, call)
            self.assertIn(call.get("status", "ok"), {"ok", "warn", "fail", "skipped"}, call)
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model)

    @staticmethod
    def check(proposal: dict, check_id: str) -> dict:
        return next(item for item in proposal["quality"]["checks"] if item["id"] == check_id)

    @staticmethod
    def tool(result, name: str) -> dict:
        return next(call for call in result.tool_calls if call["tool"] == name)


def code_edit(files: list[dict], *, title: str = "Change", **extra) -> str:
    payload = {"title": title, "summary": "A focused change.", "files": files, "validation": ["npm test"], **extra}
    return "<code_edit>\n" + json.dumps(payload) + "\n</code_edit>"


def code_review(*, files=None, issues=None, verdict="changes", summary="Reviewed.", pr=None) -> str:
    payload = {"verification": ["checked"], "verdict": verdict, "summary": summary, "issues": issues or [],
               "files": files or [], "pr": pr or {}}
    return "<code_review>\n" + json.dumps(payload) + "\n</code_review>"
