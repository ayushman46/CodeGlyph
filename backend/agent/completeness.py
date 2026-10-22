"""Index-backed helpers for the quality gates (G3 resolution, G4 completeness).

* ``IndexedRepoFiles`` answers the import check's questions (does a path
  exist, what does a file contain, which sibling files share a directory)
  from the repository manifest and indexed chunks.
* ``find_stale_mentions`` finds lines elsewhere that still state a literal
  the change replaced (``limit: 2`` -> ``limit: 3``): docs, JSDoc
  ``@default`` tags and test constants named after the changed default.
* ``find_impacted_tests`` lists test files that import a changed module.

Every query hides rows staged by an in-progress refresh, like the editor's
own index reads.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import Any

from agent.quality import (
    CODE_EXTENSIONS,
    _relative_candidates,
    changed_literals,
    changed_string_literals,
    file_extension,
    fts_literal_queries,
    fts_phrase,
    is_string_fixture_path,
    is_test_path,
    js_imports,
    stale_line_matcher,
    string_line_matcher,
)

logger = logging.getLogger(__name__)

_NOT_STAGED = (
    "NOT EXISTS (SELECT 1 FROM repo_index_runs runs WHERE runs.repo_id = c.repo_id AND runs.chunk_prefix <> '' "
    "AND substr(c.id, 1, length(runs.chunk_prefix)) = runs.chunk_prefix)"
)
_PAGE = 500
# A superset of agent.quality.is_test_path in SQL (rows are re-checked in Python).
_TEST_PATH_SQL = (
    "(c.file_path LIKE 'test/%' OR c.file_path LIKE 'tests/%' OR c.file_path LIKE '%/test/%' OR c.file_path LIKE '%/tests/%' "
    "OR c.file_path LIKE '%__tests__%' OR c.file_path LIKE '%.test.%' OR c.file_path LIKE '%.spec.%' "
    "OR c.file_path LIKE '%\\_test.%' ESCAPE '\\' OR c.file_path LIKE '%-test.%' OR c.file_path LIKE '%test\\_%' ESCAPE '\\' "
    "OR c.file_path LIKE 'e2e/%' OR c.file_path LIKE '%/e2e/%' OR c.file_path LIKE 'spec/%' OR c.file_path LIKE '%/spec/%' "
    "OR c.file_path LIKE '%/testing/%' OR c.file_path LIKE 'testing/%' OR c.file_path LIKE '%.tests.%' OR c.file_path LIKE '%.specs.%')"
)


def _merge_lines(rows: list[dict]) -> str:
    """Rebuild text from chunk rows ordered by start line (overlaps merged by line number)."""
    lines: dict[int, str] = {}
    for row in rows:
        start = int(row.get("start_line") or 1)
        for offset, line in enumerate(str(row.get("content") or "").split("\n")):
            lines.setdefault(start + offset, line)
    if not lines:
        return ""
    last = max(lines)
    return "\n".join(lines.get(number, "") for number in range(1, last + 1)) + "\n"


class IndexedRepoFiles:
    """The ``RepoFiles`` protocol of ``agent.quality`` over the index."""

    def __init__(self, store: Any, repo_id: str, *, incomplete: bool = False):
        self.store = store
        self.repo_id = repo_id
        self.incomplete = incomplete
        self._existing: dict[str, bool] = {}
        self._texts: dict[str, str | None] = {}

    async def existing(self, paths: list[str]) -> set[str]:
        unknown = [path for path in dict.fromkeys(paths) if path not in self._existing]
        for offset in range(0, len(unknown), 200):
            window = unknown[offset:offset + 200]
            placeholders = ",".join("?" for _ in window)
            found: set[str] = set()
            try:
                rows = await self.store.fetch_all(
                    f"SELECT file_path FROM repo_files WHERE repo_id = ? AND file_path IN ({placeholders})",
                    [self.repo_id, *window],
                )
                found = {str(row.get("file_path")) for row in rows}
                if len(found) < len(window):
                    rows = await self.store.fetch_all(
                        f"SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND file_path IN ({placeholders})",
                        [self.repo_id, *window],
                    )
                    found |= {str(row.get("file_path")) for row in rows}
            except Exception:
                logger.debug("Path lookup failed", exc_info=True)
            for path in window:
                self._existing[path] = path in found
        return {path for path in paths if self._existing.get(path)}

    async def read(self, path: str) -> str | None:
        if path in self._texts:
            return self._texts[path]
        text: str | None = None
        try:
            rows = await self.store.fetch_all(
                f"SELECT c.start_line, c.content FROM chunks c WHERE c.repo_id = ? AND c.file_path = ? AND {_NOT_STAGED} "
                "ORDER BY c.start_line LIMIT 400",
                [self.repo_id, path],
            )
            text = _merge_lines(rows) if rows else None
        except Exception:
            logger.debug("Could not read %s from the index", path, exc_info=True)
        self._texts[path] = text
        return text

    async def heads(self, directory: str, extensions: frozenset[str], exclude: str, limit: int) -> dict[str, str]:
        prefix = f"{directory}/" if directory else ""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            rows = await self.store.fetch_all(
                "SELECT file_path FROM repo_files WHERE repo_id = ? AND file_path LIKE ? ESCAPE '\\' "
                "AND instr(substr(file_path, ?), '/') = 0 ORDER BY file_path LIMIT 200",
                [self.repo_id, f"{escaped}%", len(prefix) + 1],
            )
        except Exception:
            logger.debug("Sibling lookup failed", exc_info=True)
            return {}
        paths = [str(row.get("file_path")) for row in rows
                 if str(row.get("file_path")) != exclude and file_extension(str(row.get("file_path"))) in extensions][:limit]
        heads: dict[str, str] = {}
        for path in paths:
            try:
                row = await self.store.fetch_one(
                    f"SELECT c.content FROM chunks c WHERE c.repo_id = ? AND c.file_path = ? AND {_NOT_STAGED} "
                    "ORDER BY c.start_line LIMIT 1",
                    [self.repo_id, path],
                )
            except Exception:
                row = None
            if row and row.get("content"):
                heads[path] = str(row["content"])[:6_000]
        return heads


async def find_stale_mentions(store: Any, repo_id: str, files: list[dict], *, limit: int = 10) -> list[dict]:
    """Lines outside the changed hunks that still state a replaced literal (at most ``limit``).

    Two kinds: a number or boolean next to a key (``limit: 2 -> 3``), and a
    changed string literal such as an error message, which tests, docs and
    fixtures that still contain the old text would now contradict
    (``kind: "string"``).
    """
    literals: list[dict] = []
    strings: list[dict] = []
    for item in files:
        # A test that adopts the new value (``defaultRetryCount = 3``) follows
        # the change; only source, config and docs define the value.
        if is_test_path(item["file_path"]):
            continue
        if item.get("action") == "modify" and isinstance(item.get("before"), str) and isinstance(item.get("after"), str):
            for literal in changed_literals(item["before"], item["after"]):
                literal["file_path"] = item["file_path"]
                literals.append(literal)
            if file_extension(item["file_path"]) in CODE_EXTENSIONS:
                for literal in changed_string_literals(item["before"], item["after"], item["file_path"]):
                    literal["file_path"] = item["file_path"]
                    strings.append(literal)
    if not literals and not strings:
        return []
    in_proposal = {item["file_path"]: item.get("after") for item in files if isinstance(item.get("after"), str)}
    findings: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(path: str, line: int, text: str, literal: dict, kind: str = "literal") -> None:
        if (path, line) in seen or len(findings) >= limit:
            return
        seen.add((path, line))
        finding = {"file_path": path, "line": line, "text": text.strip()[:160], "key": literal.get("key"),
                   "old": literal.get("needle") or literal["old"], "new": literal["new"], "kind": kind}
        if kind == "string":
            finding["source"] = literal["file_path"]
        findings.append(finding)

    for literal in literals[:3]:
        matcher = stale_line_matcher(literal)
        for path, after in in_proposal.items():
            lines = after.replace("\r\n", "\n").split("\n")
            for index in range(len(lines)):
                if matcher(path, lines, index):
                    add(path, index + 1, lines[index], literal)
        for expression, tests_only in fts_literal_queries(literal):
            path_filter = f" AND {_TEST_PATH_SQL}" if tests_only else ""
            for page in range(3):
                try:
                    rows = await store.fetch_all(
                        "SELECT c.file_path, c.start_line, c.content FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                        f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED}{path_filter} "
                        "ORDER BY c.rowid LIMIT ? OFFSET ?",
                        [expression, repo_id, _PAGE, page * _PAGE],
                    )
                except Exception:
                    logger.debug("Stale-mention search failed", exc_info=True)
                    break
                for row in rows:
                    path = str(row.get("file_path") or "")
                    if not path or path in in_proposal or (tests_only and not is_test_path(path)):
                        continue
                    lines = str(row.get("content") or "").split("\n")
                    start = int(row.get("start_line") or 1)
                    for index in range(len(lines)):
                        if matcher(path, lines, index):
                            add(path, start + index, lines[index], literal)
                if len(rows) < _PAGE or len(findings) >= limit:
                    break
    for literal in strings[:3]:
        matcher = string_line_matcher(literal["needle"])
        candidates: list[tuple[str, int, str]] = []
        # When other source still produces the old text (another throw with
        # the same message), tests that expect it are not stale.
        still_used = False
        for path, after in in_proposal.items():
            for index, line in enumerate(after.replace("\r\n", "\n").split("\n")):
                if not matcher(line):
                    continue
                if is_string_fixture_path(path):
                    candidates.append((path, index + 1, line))
                else:
                    still_used = True
        phrase = fts_phrase(literal["needle"])
        for page in range(3 if phrase and not still_used else 0):
            try:
                rows = await store.fetch_all(
                    "SELECT c.file_path, c.start_line, c.content FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED} ORDER BY c.rowid LIMIT ? OFFSET ?",
                    [phrase, repo_id, _PAGE, page * _PAGE],
                )
            except Exception:
                logger.debug("Stale-text search failed", exc_info=True)
                break
            for row in rows:
                path = str(row.get("file_path") or "")
                if not path or path in in_proposal:
                    continue
                start = int(row.get("start_line") or 1)
                for index, line in enumerate(str(row.get("content") or "").split("\n")):
                    if not matcher(line):
                        continue
                    if is_string_fixture_path(path):
                        candidates.append((path, start + index, line))
                    else:
                        still_used = True
            if still_used or len(rows) < _PAGE:
                break
        if not still_used:
            for path, line_number, line in candidates:
                add(path, line_number, line, literal, "string")
    findings.sort(key=lambda item: (item["file_path"], item["line"]))
    return findings[:limit]


async def find_impacted_tests(store: Any, repo_id: str, files: list[dict], *, limit: int = 5,
                              skip_paths: set[str] | None = None) -> list[dict]:
    """Test files (not in the proposal) that import a modified module."""
    proposal_paths = {item["file_path"] for item in files}
    changed = [item["file_path"] for item in files if item.get("action") == "modify" and not is_test_path(item["file_path"])
               and file_extension(item["file_path"]) in CODE_EXTENSIONS and item["file_path"] not in (skip_paths or set())]
    if not changed:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    try:
        placeholders = ",".join("?" for _ in changed)
        rows = await store.fetch_all(
            f"SELECT DISTINCT source_file, target_file FROM repo_dependencies WHERE repo_id = ? AND target_file IN ({placeholders}) "
            "ORDER BY source_file LIMIT 400",
            [repo_id, *changed],
        )
        has_graph = bool(rows) or bool(await store.fetch_one(
            "SELECT 1 AS present FROM repo_dependencies WHERE repo_id = ? LIMIT 1", [repo_id]))
    except Exception:
        logger.debug("Dependency lookup failed", exc_info=True)
        rows, has_graph = [], False
    for row in rows:
        source = str(row.get("source_file") or "")
        if source and is_test_path(source) and source not in proposal_paths and source not in seen:
            seen.add(source)
            found.append({"file_path": source, "imports": str(row.get("target_file"))})
    if not has_graph:
        # Legacy index without an import graph: resolve imports in test chunks
        # that mention a changed module's file name.
        for target in changed[:4]:
            stem = re.sub(r"[^0-9A-Za-z_]", "", posixpath.splitext(posixpath.basename(target))[0])
            if len(stem) < 2:
                continue
            try:
                candidates = await store.fetch_all(
                    "SELECT c.file_path, c.content FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED} LIMIT 300",
                    [f'"{stem.lower()}"', repo_id],
                )
            except Exception:
                candidates = []
            for row in candidates:
                source = str(row.get("file_path") or "")
                if not is_test_path(source) or source in proposal_paths or source in seen:
                    continue
                for ref in js_imports(str(row.get("content") or "")):
                    if ref["spec"].startswith(".") and target in _relative_candidates(source, ref["spec"])[1]:
                        seen.add(source)
                        found.append({"file_path": source, "imports": target})
                        break
    return found[:limit]


async def repository_has_tests(store: Any, repo_id: str) -> bool:
    patterns = ("test/%", "tests/%", "%/test/%", "%/tests/%", "%__tests__%", "%.test.%", "%.spec.%", "%_test.%", "test_%.py", "%/test_%.py", "spec/%")
    clause = " OR ".join("file_path LIKE ?" for _ in patterns)
    for table in ("repo_files", "chunks"):
        try:
            row = await store.fetch_one(f"SELECT 1 AS present FROM {table} WHERE repo_id = ? AND ({clause}) LIMIT 1", [repo_id, *patterns])
        except Exception:
            continue
        if row:
            return True
    return False


async def unindexed_source_count(store: Any, repo_id: str) -> int:
    """Files of the repository the index does not hold (budget omissions plus excluded code files)."""
    total = 0
    try:
        snapshot = await store.fetch_one("SELECT selection_json FROM repo_snapshots WHERE repo_id = ?", [repo_id])
        if snapshot:
            selection = snapshot.get("selection_json")
            selection = json.loads(selection) if isinstance(selection, str) else selection
            if isinstance(selection, dict):
                total += int(selection.get("budget_omitted_files") or 0)
        coverage = await store.fetch_one("SELECT excluded_paths FROM repo_coverage WHERE repo_id = ?", [repo_id])
        if coverage:
            paths = coverage.get("excluded_paths")
            paths = json.loads(paths) if isinstance(paths, str) else paths
            if isinstance(paths, list):
                total += sum(1 for path in paths if isinstance(path, str)
                             and not posixpath.basename(path).startswith(".")
                             and file_extension(path) in CODE_EXTENSIONS)
    except Exception:
        logger.debug("Coverage lookup failed", exc_info=True)
    return total
