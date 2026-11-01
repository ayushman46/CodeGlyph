"""Change impact: which files and tests reach a file, and where a symbol is used.

Everything here is static analysis of the resolved import graph
(``repo_dependencies``) and of the indexed text. It never runs code, so it
says which files import a file and which tests mention a name; it makes no
claim about runtime coverage. Results are deterministic for one index.

An index whose graph was built by an older resolver (see
``ingest.dependencies.GRAPH_VERSION``) is rebuilt from the indexed text the
first time impact is used, without downloading the repository again.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections.abc import AsyncIterator, Iterable

from agent.editor import reconstruct_indexed_file
from agent.quality import is_test_path
from ingest.dependencies import (
    GRAPH_VERSION,
    JS_CONFIG_NAMES,
    SOURCE_EXTENSIONS,
    DependencyScanner,
    build_js_resolver_from_texts,
    graph_version_from_marker,
    scans_imports,
    triple_quote_state,
)
from ingest.pipeline import persist_dependency_manifest

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
# Bounds that keep one request fast on very large repositories.
_NODE_CAP = 3000
_IN_WINDOW = 200
_TEXT_WINDOW = 40
_TEXT_PAGE_ROWS = 200
_TEST_SCAN_FILES = 200
_TEST_FUNCTION_LIMIT = 300
CANDIDATE_LIMIT = 50
PATH_SCAN_LIMIT = 5000
# How long a request waits for an outdated graph to be rebuilt before it
# answers from the old graph (the rebuild carries on in the background).
REBUILD_WAIT_SECONDS = 20.0

_SYMBOL = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
# pytest (``test_*.py``, ``*_test.py``) and unittest/Django (``test*.py``,
# ``tests.py``) defaults; a bare ``test*.py`` only counts inside a test folder,
# so library modules such as ``src/flask/testing.py`` stay out.
_PY_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test|tests?)\.py$")
_PY_UNITTEST_FILE = re.compile(r"(?:^|/)tests?/(?:[^/]+/)*test[^/]*\.py$")
# A test file's own marker (``date.test.ts``, ``app.spec.js``, ``url-test.js``).
_JS_TEST_NAME = re.compile(r"[._-](?:test|tests|spec|specs|e2e-spec|e2e)\.[a-z0-9]+$", re.IGNORECASE)
_TEST_ROOT_DIRS = frozenset({"__tests__", "test", "tests", "e2e", "playwright", "cypress", "testing", "spec", "specs"})
_TEST_HELPER_DIRS = frozenset({
    "support", "helper", "helpers", "fixture", "fixtures", "__fixtures__", "mock", "mocks", "__mocks__",
    "util", "utils", "data", "snapshot", "snapshots", "__snapshots__",
})
_PY_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_PY_TEST_CLASS = re.compile(r"^\s*class\s+(Test\w*)\b")
_JS_TEST_CALL = re.compile(
    r"^\s*(it|test|specify|describe|suite|context)(?:\.(?:only|skip|concurrent|serial|failing|todo|each\s*\(.*?\)))*"
    r"\s*\(\s*(['\"`])(.+?)\2"
)
# Words too common in names to say that a test is about a symbol.
_GENERIC_WORDS = frozenset({
    "get", "set", "make", "create", "build", "init", "new", "do", "run", "handle", "add", "remove", "update",
    "load", "save", "process", "apply", "to", "from", "with", "the", "and", "for", "on", "is", "has", "of",
    "in", "by", "as", "test", "tests", "self", "cls", "async", "sync",
})

STATIC_NOTE = (
    "Static analysis of resolved imports in the indexed code. External packages, computed imports and "
    "runtime behaviour are not included, and no tests are run, so this is not test coverage."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _windows(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _stage_filter(staging_prefix: str | None, column: str = "id") -> tuple[str, list]:
    """Hide the rows of a refresh in progress, exactly like search and chat."""
    if not staging_prefix:
        return "", []
    return f" AND {column} NOT LIKE ?", [f"{staging_prefix}%"]


def is_test_file(path: str) -> bool:
    """A file a test runner collects (not a fixture, helper or conftest).

    A JS/TS file named as a test (``src/utils/date.test.ts``) always counts;
    one that is a test only by its folder is a helper when it sits in a
    helper folder inside the test tree (``test/support/utils.js``).
    """
    extension = posixpath.splitext(path)[1].lower()
    if extension in {".py", ".pyi"}:
        if posixpath.basename(path) in {"conftest.py", "__init__.py"}:
            return False
        return bool(_PY_TEST_FILE.search(path) or _PY_UNITTEST_FILE.search(path))
    if not scans_imports(path) or not is_test_path(path):
        return False
    if _JS_TEST_NAME.search(path):
        return True
    folders = [part.lower() for part in path.split("/")[:-1]]
    root = next((index for index, part in enumerate(folders) if part in _TEST_ROOT_DIRS), None)
    return root is None or not any(part in _TEST_HELPER_DIRS for part in folders[root + 1:])


def normalise_symbol(raw: str | None) -> str | None:
    """The identifier to search for, or None. ``Flask.make_response`` -> ``make_response``."""
    value = (raw or "").strip()
    if not value:
        return None
    value = value.removesuffix("()")
    if len(value) > 120 or not _SYMBOL.match(value):
        raise ValueError("Enter a function, class or variable name, such as make_response.")
    return value.rsplit(".", 1)[-1]


def symbol_words(symbol: str) -> list[str]:
    """Distinctive words of a name: ``make_response`` -> ``["response"]``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", symbol)
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", spaced) if word]
    return [word for word in words if len(word) >= 3 and word not in _GENERIC_WORDS]


def _name_match(name: str, symbol: str, words: list[str]) -> str | None:
    """What a test name shares with the symbol (the symbol or its words), if anything."""
    lowered = name.lower()
    if symbol.lower() in lowered:
        return symbol
    if words and all(word in lowered for word in words):
        return " ".join(words)
    return None


def match_target_paths(query: str, paths: Iterable[str], *, suffix: bool = True) -> list[str]:
    """The indexed files a typed path names, from the best tier that matches.

    Tiers: the exact path; the path without its extension (``lib/request``),
    or a package directory (``src/flask`` for ``src/flask/__init__.py``);
    then the same forms as a suffix after a ``/`` (``app.py`` matches every
    ``*/app.py``; left out when ``suffix`` is false). More than one result
    means the caller must choose.
    """
    paths = list(dict.fromkeys(paths))
    lowered = query.lower()
    exact = [path for path in paths if path == query]
    if exact:
        return exact
    folded = [path for path in paths if path.lower() == lowered]
    if folded:
        return sorted(folded)

    def forms(path: str) -> set[str]:
        lower = path.lower()
        found = {lower}
        stem, extension = posixpath.splitext(lower)
        if extension:
            found.add(stem)
            if posixpath.basename(stem) in {"index", "__init__"} and "/" in stem:
                found.add(posixpath.dirname(stem))
        return found

    variants = {path: forms(path) for path in paths}
    whole = [path for path, names in variants.items() if lowered in names]
    if whole or not suffix:
        return sorted(whole)
    ending = "/" + lowered
    return sorted(path for path, names in variants.items() if any(name.endswith(ending) for name in names))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def target_patterns(query: str, *, suffix: bool) -> list[str]:
    """SQL LIKE patterns for the forms ``match_target_paths`` accepts.

    Whole forms (``q``, ``q.ext``, ``q/__init__.ext``, ``q/index.ext``), or
    the same after any folder when ``suffix`` is true. They over-match a
    little (``q.test.js``), which ``match_target_paths`` then drops.
    """
    base = ("%/" if suffix else "") + _like_escape(query.lower())
    return [base, base + ".%", base + "/" + _like_escape("__init__") + ".%", base + "/index.%"]


# ---------------------------------------------------------------------------
# Indexed text
# ---------------------------------------------------------------------------


async def iter_indexed_texts(store, repo_id: str, paths: list[str], staging_prefix: str | None = None) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(path, text)`` rebuilt from chunk rows, a bounded page at a time."""
    stage_sql, stage_args = _stage_filter(staging_prefix)
    for window in _windows(sorted(set(paths)), _TEXT_WINDOW):
        placeholders = ",".join("?" for _ in window)
        cursor = ("", 0, 0)
        current_path: str | None = None
        rows: list[dict] = []
        while True:
            page = await store.fetch_all(
                "SELECT rowid AS row_order, file_path, start_line, end_line, content FROM chunks "
                f"WHERE repo_id = ? AND file_path IN ({placeholders}){stage_sql} "
                "AND (file_path > ? OR (file_path = ? AND (start_line > ? OR (start_line = ? AND rowid > ?)))) "
                "ORDER BY file_path, start_line, rowid LIMIT ?",
                [repo_id, *window, *stage_args, cursor[0], cursor[0], cursor[1], cursor[1], cursor[2], _TEXT_PAGE_ROWS],
            )
            for row in page:
                path = str(row["file_path"])
                if path != current_path:
                    if current_path is not None and rows:
                        text, _exact = reconstruct_indexed_file(rows, current_path)
                        yield current_path, text or ""
                    current_path, rows = path, []
                rows.append(row)
            if len(page) < _TEXT_PAGE_ROWS:
                break
            last = page[-1]
            cursor = (str(last["file_path"]), int(last["start_line"]), int(last["row_order"]))
        if current_path is not None and rows:
            text, _exact = reconstruct_indexed_file(rows, current_path)
            yield current_path, text or ""


async def indexed_subset(store, repo_id: str, paths: Iterable[str], staging_prefix: str | None) -> set[str]:
    """The paths that have searchable rows in the published index."""
    stage_sql, stage_args = _stage_filter(staging_prefix)
    found: set[str] = set()
    for window in _windows(sorted(set(paths)), _IN_WINDOW):
        placeholders = ",".join("?" for _ in window)
        rows = await store.fetch_all(
            f"SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND file_path IN ({placeholders}){stage_sql}",
            [repo_id, *window, *stage_args],
        )
        found.update(str(row["file_path"]) for row in rows)
    return found


# ---------------------------------------------------------------------------
# Graph version and rebuild from the index
# ---------------------------------------------------------------------------


async def stored_graph_version(store, repo_id: str) -> int:
    row = await store.fetch_one(
        "SELECT import_name FROM repo_dependencies WHERE repo_id = ? AND source_file = '' AND target_file = '' LIMIT 1",
        [repo_id],
    )
    return graph_version_from_marker(row.get("import_name")) if row else 1


async def rebuild_dependency_graph_from_index(store, repo_id: str, staging_prefix: str | None = None) -> dict:
    """Rebuild the import graph from the text already in the index.

    Uses the same scanner as indexing; known paths are the manifest plus the
    indexed files (empty ``__init__.py`` files are in the manifest only).
    """
    stage_sql, stage_args = _stage_filter(staging_prefix)
    manifest_rows = await store.fetch_all("SELECT file_path FROM repo_files WHERE repo_id = ?", [repo_id])
    indexed_rows = await store.fetch_all(
        f"SELECT DISTINCT file_path FROM chunks WHERE repo_id = ?{stage_sql}", [repo_id, *stage_args],
    )
    indexed = sorted({str(row["file_path"]) for row in indexed_rows})
    known = {str(row["file_path"]) for row in manifest_rows} | set(indexed)
    config_paths = [path for path in indexed if posixpath.basename(path).lower() in JS_CONFIG_NAMES]
    configs = [item async for item in iter_indexed_texts(store, repo_id, config_paths, staging_prefix)]
    scanner = DependencyScanner(known, build_js_resolver_from_texts(configs))
    code_paths = [path for path in indexed if scans_imports(path)]
    async for path, text in iter_indexed_texts(store, repo_id, code_paths, staging_prefix):
        scanner.scan_lines(path, text.split("\n"))
    edges = scanner.sorted_edges()
    await persist_dependency_manifest(store, repo_id, edges)
    return {"files_scanned": len(code_paths), "edges": len(edges)}


_REBUILDS: dict[str, asyncio.Task] = {}


def _log_rebuild_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Dependency graph rebuild failed", exc_info=task.exception())


async def ensure_current_graph(store, repo: dict, wait_seconds: float = REBUILD_WAIT_SECONDS) -> dict:
    """Make sure the stored graph was built by the current resolver.

    An outdated graph is rebuilt from the index once per process; requests
    wait up to ``wait_seconds`` and otherwise answer from the old graph. A
    repository being indexed is left alone: the run writes a fresh graph.
    """
    repo_id = str(repo["id"])
    version = await stored_graph_version(store, repo_id)
    if version >= GRAPH_VERSION:
        return {"version": version, "rebuilt": False, "rebuilding": False}
    if repo.get("is_indexing"):
        return {"version": version, "rebuilt": False, "rebuilding": False}
    task = _REBUILDS.get(repo_id)
    if task is None or task.done():
        task = asyncio.create_task(rebuild_dependency_graph_from_index(store, repo_id, repo.get("staging_prefix")))
        task.add_done_callback(_log_rebuild_failure)
        task.add_done_callback(lambda done: _REBUILDS.pop(repo_id, None) if _REBUILDS.get(repo_id) is done else None)
        _REBUILDS[repo_id] = task
    try:
        stats = await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except asyncio.TimeoutError:
        return {"version": version, "rebuilt": False, "rebuilding": True}
    except Exception:
        # Already logged by the done callback; the old graph still answers.
        return {"version": version, "rebuilt": False, "rebuilding": False}
    return {"version": GRAPH_VERSION, "rebuilt": True, "rebuilding": False, **stats}


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


async def resolve_target(store, repo: dict, query: str) -> tuple[list[str], bool]:
    """Indexed paths the typed ``query`` names, and whether that list is complete.

    An exact path is looked up directly. The other tiers are read with
    patterns that match only their own forms, whole forms first, so the
    scan limit is reached only when thousands of files really match (a
    bare ``index.ts``); the list is then marked incomplete.
    """
    stage_sql, stage_args = _stage_filter(repo.get("staging_prefix"))
    exact = await store.fetch_one(
        f"SELECT file_path FROM chunks WHERE repo_id = ? AND file_path = ?{stage_sql} LIMIT 1",
        [repo["id"], query, *stage_args],
    )
    if exact:
        return [query], True
    attempts = [query]
    if "/" not in query and "." in query and posixpath.splitext(query)[1].lower() not in SOURCE_EXTENSIONS:
        # A Python module path: ``flask.app`` or ``httpx._client``.
        attempts.append(query.replace(".", "/"))
    for attempt in attempts:
        for suffix in (False, True):
            patterns = target_patterns(attempt, suffix=suffix)
            clauses = " OR ".join("lower(file_path) LIKE ? ESCAPE '\\'" for _ in patterns)
            rows = await store.fetch_all(
                f"SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND ({clauses}){stage_sql} "
                "ORDER BY file_path LIMIT ?",
                [repo["id"], *patterns, *stage_args, PATH_SCAN_LIMIT + 1],
            )
            complete = len(rows) <= PATH_SCAN_LIMIT
            paths = [str(row["file_path"]) for row in rows[:PATH_SCAN_LIMIT]]
            matches = match_target_paths(attempt, paths, suffix=suffix)
            if matches or not complete:
                return matches, complete
    return [], True


async def most_imported_files(store, repo: dict, count: int = 6) -> list[dict]:
    """The indexed files imported by the most other files."""
    rows = await store.fetch_all(
        "SELECT target_file, COUNT(DISTINCT source_file) AS count FROM repo_dependencies "
        "WHERE repo_id = ? AND target_file != '' GROUP BY target_file ORDER BY count DESC, target_file LIMIT ?",
        [repo["id"], count * 4],
    )
    indexed = await indexed_subset(
        store, str(repo["id"]), [str(row["target_file"]) for row in rows], repo.get("staging_prefix"),
    )
    return [
        {"file_path": str(row["target_file"]), "dependent_count": int(row.get("count") or 0)}
        for row in rows if str(row["target_file"]) in indexed
    ][:count]


async def dependent_counts(store, repo_id: str, paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for window in _windows(paths, _IN_WINDOW):
        placeholders = ",".join("?" for _ in window)
        rows = await store.fetch_all(
            "SELECT target_file, COUNT(DISTINCT source_file) AS count FROM repo_dependencies "
            f"WHERE repo_id = ? AND target_file IN ({placeholders}) GROUP BY target_file",
            [repo_id, *window],
        )
        counts.update({str(row["target_file"]): int(row.get("count") or 0) for row in rows})
    return counts


# ---------------------------------------------------------------------------
# Dependents (breadth-first, bounded)
# ---------------------------------------------------------------------------


async def dependents_by_depth(store, repo_id: str, target: str, max_depth: int = MAX_DEPTH) -> tuple[dict[str, dict], int]:
    """Every file that imports ``target`` within ``max_depth`` hops.

    Each file is reported once, at its shortest distance, with the import
    line that reaches the previous level (``via``) so it can be opened there.
    Also returns the deepest level explored completely: the walk stops
    after the level that passes the node cap.
    """
    nodes: dict[str, dict] = {}
    seen = {target}
    frontier = [target]
    complete_through = max_depth
    for depth in range(1, max_depth + 1):
        found: dict[str, dict] = {}
        for window in _windows(frontier, _IN_WINDOW):
            placeholders = ",".join("?" for _ in window)
            rows = await store.fetch_all(
                "SELECT source_file, target_file, import_name, line_number FROM repo_dependencies "
                f"WHERE repo_id = ? AND target_file IN ({placeholders}) AND source_file != '' "
                "ORDER BY source_file, line_number, target_file",
                [repo_id, *window],
            )
            for row in rows:
                source = str(row["source_file"])
                if source in seen:
                    continue
                line = int(row.get("line_number") or 0)
                current = found.get(source)
                if current is None or (line, str(row["target_file"])) < (current["line_number"], current["via"]):
                    found[source] = {
                        "file_path": source, "depth": depth, "via": str(row["target_file"]),
                        "line_number": line, "import_name": str(row.get("import_name") or ""),
                    }
        if not found:
            break
        seen.update(found)
        nodes.update(found)
        frontier = sorted(found)
        if len(nodes) >= _NODE_CAP:
            complete_through = depth
            break
    return nodes, complete_through


# ---------------------------------------------------------------------------
# Tests and symbols
# ---------------------------------------------------------------------------


def _reference_pattern(symbol: str) -> re.Pattern:
    return re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")


def find_symbol_tests(text: str, path: str, symbol: str) -> list[dict]:
    """Test functions in one file that use ``symbol`` or are named after it.

    ``kind`` is ``reference`` when the test body uses the name, ``name`` when
    only the test's name contains the symbol's distinctive words (for
    ``make_response``: tests named ``*response*``; ``matched`` says which). Scopes come from
    indentation, so a reference inside a nested helper counts for the test
    that defines it. The declaration line counts too (a one-line test), but
    a JS title is a name, not a use, so only the code after it is searched.
    """
    python = posixpath.splitext(path)[1].lower() in {".py", ".pyi"}
    reference = _reference_pattern(symbol)
    words = symbol_words(symbol)
    # Open scopes: (indent, kind, name, line)
    stack: list[tuple[int, str, str, int]] = []
    found: dict[tuple[str, int], dict] = {}
    open_quote: str | None = None
    for number, line in enumerate(text.split("\n"), 1):
        inside_string = open_quote is not None
        if python and ('"""' in line or "'''" in line):
            open_quote = triple_quote_state(line, open_quote)
        stripped = line.strip()
        if not stripped:
            continue
        code = line
        if not inside_string:
            indent = len(line) - len(line.lstrip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
            declared = None
            short = ""
            if python:
                class_match = _PY_TEST_CLASS.match(line)
                if class_match:
                    stack.append((indent, "class", class_match.group(1), number))
                    continue
                def_match = _PY_TEST_DEF.match(line)
                if def_match:
                    owner = next((item[2] for item in reversed(stack) if item[1] == "class"), None)
                    short = def_match.group(1)
                    declared = f"{owner}.{short}" if owner else short
                    stack.append((indent, "test", declared, number))
            else:
                call = _JS_TEST_CALL.match(line)
                if call:
                    kind = "group" if call.group(1) in {"describe", "suite", "context"} else "test"
                    title = call.group(3)
                    if kind == "test":
                        group = next((item[2] for item in reversed(stack) if item[1] == "group"), None)
                        declared = f"{group} › {title}" if group else title
                        short = title
                    stack.append((indent, kind, declared or title, number))
                    code = line[call.end():]
            if declared is not None:
                # Only the test's own name: the method (not ``Class.``), or the
                # title (not its groups), so dots in a title do not split it.
                matched = _name_match(short, symbol, words)
                if matched:
                    found[(declared, number)] = {
                        "file_path": path, "name": declared, "line": number, "kind": "name",
                        "matched": matched, "reference_line": None, "references": 0,
                    }
        if not reference.search(code):
            continue
        test = next((item for item in reversed(stack) if item[1] == "test"), None)
        if test is None:
            continue
        key = (test[2], test[3])
        entry = found.setdefault(key, {
            "file_path": path, "name": test[2], "line": test[3], "kind": "reference",
            "matched": symbol, "reference_line": number, "references": 0,
        })
        if entry["kind"] != "reference":
            entry.update(kind="reference", matched=symbol, reference_line=number)
        entry["references"] += 1
    return sorted(found.values(), key=lambda item: item["line"])


def symbol_in_text(text: str, symbol: str) -> dict:
    """Where ``symbol`` is defined in (or at least mentioned by) one file."""
    definition = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function\*?|const|let|var|interface|type|enum)\s+"
        + re.escape(symbol) + r"\b"
    )
    reference = _reference_pattern(symbol)
    defined_at = None
    mentions = 0
    for number, line in enumerate(text.split("\n"), 1):
        if reference.search(line):
            mentions += 1
            if defined_at is None and definition.match(line):
                defined_at = number
    return {"name": symbol, "defined_at": defined_at, "mentions": mentions}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def analyse_impact(store, repo: dict, target: str, *, depth: int, symbol: str | None, limit: int) -> dict:
    """The whole impact report for one resolved, indexed target file."""
    repo_id = str(repo["id"])
    staging = repo.get("staging_prefix")
    nodes, complete_through = await dependents_by_depth(store, repo_id, target, MAX_DEPTH)
    indexed = await indexed_subset(store, repo_id, nodes, staging)
    listed = sorted(
        (node for node in nodes.values() if node["file_path"] in indexed),
        key=lambda node: (node["depth"], node["file_path"]),
    )
    for node in listed:
        node["is_test"] = is_test_file(node["file_path"])

    within = [node for node in listed if node["depth"] <= depth]
    depth_counts = {str(level): sum(1 for node in within if node["depth"] == level) for level in range(1, depth + 1)}
    dependents: list[dict] = []
    for level in range(1, depth + 1):
        dependents.extend([node for node in within if node["depth"] == level][:limit])
    truncated = len(dependents) < len(within) or complete_through < depth

    direct = [node for node in listed if node["depth"] == 1]
    edges = await store.fetch_all(
        "SELECT source_file, target_file, import_name, line_number FROM repo_dependencies "
        "WHERE repo_id = ? AND target_file = ? AND source_file != '' ORDER BY source_file, line_number LIMIT ?",
        [repo_id, target, limit * 4],
    )
    imports = await store.fetch_all(
        "SELECT target_file, MIN(line_number) AS line_number, import_name FROM repo_dependencies "
        "WHERE repo_id = ? AND source_file = ? AND target_file != '' GROUP BY target_file ORDER BY target_file LIMIT ?",
        [repo_id, target, limit],
    )

    tests_within = [node for node in within if node["is_test"]]
    tests_beyond = [node for node in listed if node["is_test"] and node["depth"] > depth]
    affected_tests: dict = {
        "files": [
            {key: node[key] for key in ("file_path", "depth", "via", "line_number")} for node in tests_within[:limit * 2]
        ],
        "count": len(tests_within),
        "truncated": len(tests_within) > limit * 2,
        "indirect_count": len(tests_beyond),
        "functions": [],
        "functions_truncated": False,
        "scanned_files": 0,
    }

    symbol_info = None
    if symbol:
        target_text = ""
        async for _path, text in iter_indexed_texts(store, repo_id, [target], staging):
            target_text = text
        symbol_info = symbol_in_text(target_text, symbol)
        scan = [node["file_path"] for node in tests_within[:_TEST_SCAN_FILES]]
        functions: list[dict] = []
        async for path, text in iter_indexed_texts(store, repo_id, scan, staging):
            functions.extend(find_symbol_tests(text, path, symbol))
        functions.sort(key=lambda item: (item["kind"] != "reference", item["file_path"], item["line"]))
        affected_tests["functions"] = functions[:_TEST_FUNCTION_LIMIT]
        affected_tests["functions_truncated"] = len(functions) > _TEST_FUNCTION_LIMIT or len(tests_within) > len(scan)
        affected_tests["scanned_files"] = len(scan)
        affected_tests["reference_count"] = sum(1 for item in functions if item["kind"] == "reference")
        affected_tests["name_count"] = sum(1 for item in functions if item["kind"] == "name")

    beyond_count = sum(1 for node in listed if node["depth"] > depth)
    return {
        "target_file": target,
        "target_files": [target],
        "depth": depth,
        "dependents": dependents,
        "depth_counts": depth_counts,
        "total_dependents": len(within),
        "indirect_beyond_depth": beyond_count,
        "dependent_files": [node["file_path"] for node in direct[:limit]],
        "dependent_count": len(direct),
        "truncated": truncated,
        "imported_files": [str(row["target_file"]) for row in imports],
        "imports": [
            {"file_path": str(row["target_file"]), "line_number": int(row.get("line_number") or 0),
             "import_name": str(row.get("import_name") or "")} for row in imports
        ],
        "edges": edges,
        "affected_tests": affected_tests,
        "symbol": symbol_info,
        "summary": _summary(target, direct, min(len(direct), limit), within, depth, beyond_count, tests_within, symbol_info),
        "method": STATIC_NOTE,
    }


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _verb(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _summary(target, direct, shown, within, depth, beyond_count, tests, symbol_info) -> str:
    if direct:
        text = f"{_plural(len(direct), 'indexed file')} {_verb(len(direct), 'imports', 'import')} {target} directly"
        if shown < len(direct):
            text += f" (showing {shown:,})"
        indirect = len(within) - len(direct)
        if depth > 1 and indirect:
            text += (
                f", and {_plural(indirect, 'more file')} {_verb(indirect, 'reaches', 'reach')} it through them "
                f"(up to {depth} levels)."
            )
        elif depth == 1 and beyond_count:
            text += (
                f". {_plural(beyond_count, 'more file')} {_verb(beyond_count, 'imports', 'import')} it indirectly "
                f"(within {MAX_DEPTH} levels)."
            )
        else:
            text += "."
    else:
        text = f"No other indexed file imports {target}."
    if tests:
        text += f" {_plural(len(tests), 'test file')} {_verb(len(tests), 'is', 'are')} affected."
    if symbol_info is not None and not symbol_info["mentions"]:
        text += f" {symbol_info['name']} does not appear in {target}; check the name."
    return text
