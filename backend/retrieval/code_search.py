"""Exact code navigation without a model: line search, files and symbols.

* ``search_code`` returns every line that contains the query, one entry per
  file. Quoted phrases match exactly; other words must all be on the line,
  and when they also appear together as typed, only those lines are shown.
  Lines that declare a searched name come first.
* ``find_files`` ranks indexed paths for a typed name or partial path, and
  ``list_directory`` lists one folder for the file tree.
* ``find_symbols`` finds declarations by name and the exact line that
  declares them, with the declaration pattern the chunker used to record
  ``chunks.symbols``.
* ``file_outline`` lists a file's declarations and the lines each spans, for
  the viewer's outline and its "enclosing symbol" breadcrumb.

Candidate sections come from SQL (FTS5, or a substring scan for small
repositories); every reported line is checked against the query in Python,
so a highlighted line always contains what was searched for.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from ingest.chunker import SYMBOL_REGEX

logger = logging.getLogger(__name__)

MAX_QUERY_CHARACTERS = 200
# Repositories up to this many sections are searched by substring (exact
# recall, including matches inside longer identifiers); larger ones use the
# full-text index and fall back to substring only when it finds nothing.
SUBSTRING_SCAN_MAX_CHUNKS = 4000
# Candidate sections considered per search (metadata only), and how many of
# them are read in full. Files are chosen before their text is read.
_CANDIDATE_ROWS = 2000
_READ_ROWS_PER_FILE = 30
_READ_ROWS_TOTAL = 300
MAX_LINES_PER_FILE = 50
_LINE_TEXT_CHARACTERS = 240
_ROW_BATCH = 100

# unicode61 (chunks_fts) splits on everything that is not a letter or digit,
# including ``_``: ``_set_timeout`` is indexed as ``set``, ``timeout``.
_FTS_TOKEN = re.compile(r"[^\W_]+")
_QUOTED = re.compile(r'"([^"]*)"')
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")

# Folders whose files are rarely what a reader means by "the code": listed
# and ranked after source folders. Dot-folders (.github) come last.
_SUPPORT_DIRS = frozenset({
    "test", "tests", "__tests__", "spec", "specs", "e2e", "testing", "testdata", "fixtures", "__fixtures__",
    "__mocks__", "mocks", "example", "examples", "sample", "samples", "demo", "demos", "docs", "doc",
    "documentation", "benchmark", "benchmarks", "bench", "playground", "stories", "storybook", "vendor",
    "third_party", "third-party", "node_modules", "dist", "build", "out", "coverage", "artwork",
})
_MARKDOWN_EXTENSIONS = (".md", ".mdx", ".markdown")


def _folder_rank(name: str) -> int:
    low = name.lower()
    if low.startswith("."):
        return 2
    return 1 if low in _SUPPORT_DIRS else 0


def is_support_path(path: str) -> bool:
    """Tests, examples, docs, vendored and generated files."""
    parts = path.lower().split("/")
    if any(part.startswith(".") or part in _SUPPORT_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return (
        name.startswith("test_") or name.endswith(("_test.py", "_test.go", "_spec.rb", ".d.ts"))
        or ".test." in name or ".spec." in name or name.endswith(_MARKDOWN_EXTENSIONS)
    )


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

_KIND_WORD = re.compile(
    r"\b(class|interface|trait|struct|enum|union|record|protocol|object|module|mod|impl|typealias|type|model|"
    r"message|service|input|function|def|fn|fun|func|const|let|var|rpc|macro_rules|table|view|procedure|trigger)\b",
    re.IGNORECASE,
)
_NOT_NAMES = frozenset({
    "if", "else", "elif", "for", "while", "do", "switch", "case", "catch", "try", "finally", "return", "throw",
    "await", "yield", "extends", "implements", "import", "export", "typeof", "instanceof",
})
# Generated one-line files are not scanned for declarations.
_MAX_DECLARATION_LINE = 1000
# CommonJS methods (``res.send = function send(body) {``,
# ``Router.prototype.use = function``), which the chunker does not record.
_ASSIGNED_FUNCTION = re.compile(
    r"^[ \t]*(?:[A-Za-z_$][\w$]*\.){1,4}([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]+)?function\b"
)


def line_declaration(line: str) -> tuple[str, str] | None:
    """``(name, kind)`` when ``line`` declares something, else None.

    Uses the chunker's declaration pattern, so a name found here is the name
    stored in ``chunks.symbols``. ``kind`` is the declaring keyword (``class``,
    ``def``, ``function``...) or ``method`` for a bare method signature.
    """
    if not line or len(line) > _MAX_DECLARATION_LINE:
        return None
    match = SYMBOL_REGEX.match(line)
    if not match:
        assigned = _ASSIGNED_FUNCTION.match(line)
        return (assigned.group(1), "function") if assigned else None
    raw = next((group for group in match.groups() if group), None)
    if not raw:
        return None
    name = raw.rsplit(".", 1)[-1].strip("`\"[] ")
    if not name or name in _NOT_NAMES:
        return None
    kind = _KIND_WORD.search(match.group(0))
    return name, kind.group(1).lower() if kind else "method"


def _indent(line: str) -> int:
    stripped = line.lstrip(" \t")
    return len(line[: len(line) - len(stripped)].expandtabs(4))


def _block_end(lines: list[str], index: int, indent: int) -> int:
    """Last line (1-based) of the declaration on ``lines[index]``.

    A block ends before the first non-blank line indented no deeper than the
    declaration. At the same depth, ``)``/``]`` (the end of a multi-line
    signature) and ``{`` (a brace on its own line) continue it, and ``}`` or
    ``end`` close it.
    """
    last = index
    for position in range(index + 1, len(lines)):
        text = lines[position]
        stripped = text.strip()
        if not stripped:
            continue
        depth = _indent(text)
        if depth > indent:
            last = position
            continue
        if depth == indent and stripped[0] in ")]{":
            last = position
            continue
        if depth == indent and (stripped[0] == "}" or stripped in {"end", "end;", "end,"}):
            return position + 1
        break
    return last + 1


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _markdown_outline(lines: list[str], limit: int) -> list[dict]:
    entries: list[dict] = []
    fence = ""
    for index, line in enumerate(lines):
        opened = _FENCE.match(line)
        if opened:
            marker = opened.group(1)
            if not fence:
                fence = marker[0] * len(marker)
            elif marker.startswith(fence):
                fence = ""
            continue
        if fence:
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            entries.append({"name": heading.group(2).strip(), "kind": f"h{level}", "line": index + 1, "level": level})
            if len(entries) >= limit:
                break
    for position, entry in enumerate(entries):
        following = next((item for item in entries[position + 1:] if item["level"] <= entry["level"]), None)
        entry["end_line"] = following["line"] - 1 if following else len(lines)
    levels = sorted({entry["level"] for entry in entries})
    for entry in entries:
        entry["depth"] = levels.index(entry.pop("level"))
    return entries


def file_outline(content: str, path: str, *, limit: int = 3000) -> list[dict]:
    """Declarations of a whole file: ``{name, kind, line, end_line, depth}``.

    Markdown files list their headings instead. ``depth`` counts the
    declarations that contain this one (a method inside a class is 1).
    """
    lines = str(content or "").split("\n")
    if path.lower().endswith(_MARKDOWN_EXTENSIONS):
        return _markdown_outline(lines, limit)
    entries: list[dict] = []
    for index, line in enumerate(lines):
        found = line_declaration(line)
        if not found:
            continue
        name, kind = found
        indent = _indent(line)
        entries.append({"name": name, "kind": kind, "line": index + 1, "end_line": _block_end(lines, index, indent)})
        if len(entries) >= limit:
            break
    stack: list[dict] = []
    for entry in entries:
        while stack and stack[-1]["end_line"] < entry["line"]:
            stack.pop()
        entry["depth"] = len(stack)
        stack.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Code search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeQuery:
    """A search query: quoted phrases (exact) and the remaining words."""

    phrases: tuple[str, ...]
    words: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.words)

    def needles(self, mode: str) -> list[str]:
        """What every matching line must contain in ``mode``."""
        if mode == "phrase":
            return [*self.phrases, *([self.text] if self.words else [])]
        return [*self.phrases, *self.words]

    @property
    def identifiers(self) -> set[str]:
        """Names a declaration line may declare (case-insensitive)."""
        names: set[str] = set()
        for needle in (*self.phrases, *self.words):
            names.update(name.lower() for name in _IDENTIFIER.findall(needle))
        return names


def parse_code_query(raw: str) -> CodeQuery:
    """``"exact phrase" other words`` → phrases and words (whitespace-normalised)."""
    text = str(raw or "")[:MAX_QUERY_CHARACTERS]
    phrases = [" ".join(found.split()) for found in _QUOTED.findall(text)]
    rest = _QUOTED.sub(" ", text).replace('"', " ")
    return CodeQuery(tuple(phrase for phrase in dict.fromkeys(phrases) if phrase), tuple(rest.split()))


def _needle_pattern(needle: str) -> re.Pattern:
    # Any run of whitespace in the query matches any run on the line.
    return re.compile(r"\s+".join(re.escape(part) for part in needle.split()), re.IGNORECASE)


def _is_word(character: str) -> bool:
    return character.isalnum() or character in "_$"


def _bounded(line: str, start: int, end: int) -> bool:
    """The match does not start or end in the middle of an identifier."""
    left = start == 0 or not _is_word(line[start]) or not _is_word(line[start - 1])
    right = end >= len(line) or not _is_word(line[end - 1]) or not _is_word(line[end])
    return left and right


def match_line(line: str, patterns: list[re.Pattern]) -> tuple[list[tuple[int, int]], bool] | None:
    """Spans of every needle on ``line`` and whether each one occurs as a whole word.

    None when any needle is missing from the line.
    """
    spans: list[tuple[int, int]] = []
    whole = True
    for pattern in patterns:
        found = [match.span() for match in pattern.finditer(line) if match.end() > match.start()]
        if not found:
            return None
        whole = whole and any(_bounded(line, start, end) for start, end in found)
        spans.extend(found)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged, whole


def _display(line: str, spans: list[tuple[int, int]]) -> tuple[str, list[list[int]]]:
    """The line without its indentation, cut to a window around the first match."""
    stripped = line.rstrip()
    offset = len(stripped) - len(stripped.lstrip())
    text = stripped[offset:]
    shifted = [(max(0, start - offset), min(len(text), end - offset)) for start, end in spans]
    if len(text) > _LINE_TEXT_CHARACTERS:
        first = shifted[0][0] if shifted else 0
        begin = max(0, min(first - 60, len(text) - _LINE_TEXT_CHARACTERS))
        window = text[begin: begin + _LINE_TEXT_CHARACTERS]
        prefix = "…" if begin else ""
        suffix = "…" if begin + len(window) < len(text) else ""
        text = prefix + window + suffix
        shifted = [
            (start - begin + len(prefix), min(end - begin, len(window)) + len(prefix))
            for start, end in shifted if begin <= start < begin + len(window)
        ]
    return text, [[start, end] for start, end in shifted if end > start]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(needle: str) -> str | None:
    tokens = _FTS_TOKEN.findall(needle.lower())
    if not tokens:
        return None
    # A needle that ends inside a word ("ParamTy") may continue in the code.
    prefix = "*" if _is_word(needle[-1]) else ""
    return f'content : "{" ".join(tokens)}"{prefix}'


def _symbol_names(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


async def _candidate_rows(store, repo_id: str, needles: list[str], staging_prefix: str | None, source: str) -> list[dict]:
    """Section metadata (no text) that may hold every needle, best first."""
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND c.id NOT LIKE ?", [f"{staging_prefix}%"])
    columns = "c.rowid AS row_id, c.file_path, c.start_line, c.language, c.symbols"
    if source == "fts":
        phrases = [phrase for phrase in (_fts_phrase(needle) for needle in needles) if phrase]
        if not phrases:
            return []
        return await store.fetch_all(
            f"SELECT {columns} FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            f"WHERE chunks_fts MATCH ? AND c.repo_id = ?{stage_sql} "
            "ORDER BY bm25(chunks_fts, 1.0, 4.0) ASC LIMIT ?",
            [" AND ".join(phrases), repo_id, *stage_args, _CANDIDATE_ROWS],
        )
    parts = list(dict.fromkeys(part for needle in needles for part in needle.split()))
    if not parts:
        return []
    likes = "".join(" AND c.content LIKE ? ESCAPE '\\'" for _ in parts)
    return await store.fetch_all(
        f"SELECT {columns} FROM chunks c WHERE c.repo_id = ?{likes}{stage_sql} "
        "ORDER BY c.file_path, c.start_line LIMIT ?",
        [repo_id, *[f"%{_escape_like(part)}%" for part in parts], *stage_args, _CANDIDATE_ROWS],
    )


async def _read_rows(store, row_ids: list[int]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for offset in range(0, len(row_ids), _ROW_BATCH):
        window = row_ids[offset: offset + _ROW_BATCH]
        rows = await store.fetch_all(
            "SELECT rowid AS row_id, start_line, end_line, content FROM chunks "
            f"WHERE rowid IN ({', '.join('?' for _ in window)})",
            window,
        )
        found.update({int(row["row_id"]): row for row in rows})
    return found


def _choose_files(rows: list[dict], identifiers: set[str], limit: int, source: str) -> tuple[list[tuple[str, list[dict]]], bool]:
    """Files worth reading, best first, and whether more were left out."""
    files: OrderedDict[str, list[dict]] = OrderedDict()
    for row in rows:
        path = str(row.get("file_path") or "")
        if path:
            files.setdefault(path, []).append(row)

    def prescore(item: tuple[int, tuple[str, list[dict]]]):
        order, (path, sections) = item
        declares = any(name.lower() in identifiers for row in sections for name in _symbol_names(row.get("symbols")))
        # FTS rows arrive best first; substring rows arrive by path, so the
        # number of matching sections stands in for relevance.
        relevance = order if source == "fts" else -len(sections)
        return (0 if declares else 1, is_support_path(path), relevance)

    ranked = [item for _order, item in sorted(enumerate(files.items()), key=prescore)]
    wanted = max(limit + 10, limit * 2)
    chosen: list[tuple[str, list[dict]]] = []
    budget = _READ_ROWS_TOTAL
    for path, sections in ranked[:wanted]:
        if budget <= 0:
            break
        take = sorted(sections, key=lambda row: int(row.get("start_line") or 0))[: min(_READ_ROWS_PER_FILE, budget)]
        budget -= len(take)
        chosen.append((path, take if len(take) == len(sections) else [*take, {"_capped": True}]))
    return chosen, len(ranked) > len(chosen)


def _scan_file(path: str, sections: list[dict], texts: dict[int, dict], patterns: list[re.Pattern],
               identifiers: set[str]) -> dict | None:
    capped = any(section.get("_capped") for section in sections)
    seen: dict[int, dict] = {}
    language = next((section.get("language") for section in sections if section.get("language")), None)
    for section in sections:
        if section.get("_capped"):
            continue
        row = texts.get(int(section["row_id"]))
        if not row:
            continue
        start = int(row.get("start_line") or 1)
        end = int(row.get("end_line") or start)
        for offset, line in enumerate(str(row.get("content") or "").split("\n")[: max(1, end - start + 1)]):
            number = start + offset
            if number in seen:
                continue
            found = match_line(line, patterns)
            if not found:
                continue
            spans, whole = found
            declared = line_declaration(line)
            definition = bool(declared and declared[0].lower() in identifiers and whole)
            text, display_spans = _display(line, spans)
            seen[number] = {"line": number, "text": text, "spans": display_spans, "definition": definition, "_whole": whole}
    if not seen:
        return None
    # Declarations of the searched name first, then every other line in order.
    ordered = sorted(seen.values(), key=lambda match: (not match["definition"], match["line"]))
    shown = ordered[:MAX_LINES_PER_FILE]
    whole = any([match.pop("_whole") for match in ordered])
    return {
        "file_path": path,
        "language": language,
        "start_line": shown[0]["line"],
        "end_line": max(match["line"] for match in shown),
        "matches": shown,
        "match_count": len(ordered),
        "definitions": sum(1 for match in ordered if match["definition"]),
        # More lines than listed, or sections left unread in a huge file.
        "truncated": len(ordered) > len(shown) or capped,
        "_whole": whole,
    }


async def _search_once(store, repo_id: str, query: CodeQuery, mode: str, *, limit: int, staging_prefix: str | None,
                       sources: list[str]) -> tuple[list[dict], bool]:
    needles = query.needles(mode)
    patterns = [_needle_pattern(needle) for needle in needles]
    identifiers = query.identifiers
    for source in sources:
        try:
            rows = await _candidate_rows(store, repo_id, needles, staging_prefix, source)
        except Exception as error:
            # An index built before chunks_fts existed: fall through to the scan.
            logger.debug("Code search %s candidates unavailable (%s)", source, type(error).__name__)
            continue
        if not rows:
            continue
        chosen, more = _choose_files(rows, identifiers, limit, source)
        texts = await _read_rows(store, [int(row["row_id"]) for _path, sections in chosen for row in sections if "row_id" in row])
        results = [
            result for result in (_scan_file(path, sections, texts, patterns, identifiers) for path, sections in chosen)
            if result
        ]
        if not results:
            continue
        results.sort(key=lambda result: (
            0 if result["definitions"] else 1,
            is_support_path(result["file_path"]),
            0 if result["_whole"] else 1,
            -min(result["match_count"], MAX_LINES_PER_FILE),
            len(result["file_path"]),
            result["file_path"],
        ))
        for result in results:
            result.pop("_whole", None)
        return results[:limit], more or len(results) > limit
    return [], False


async def search_code(
    store,
    repo_id: str,
    query: str,
    *,
    limit: int = 25,
    staging_prefix: str | None = None,
    chunk_count: int | None = None,
) -> dict:
    """Every line that contains ``query``, grouped per file (no model call).

    Returns ``{"results", "mode", "more_files", "line_count"}``. ``mode`` is
    ``exact`` (one word or only quoted phrases), ``phrase`` (several words,
    found together as typed) or ``all_words`` (several words, each somewhere
    on the line, because no line has them together).
    """
    parsed = parse_code_query(query)
    if sum(len(part) for part in (*parsed.phrases, *parsed.words)) < 2:
        return {"results": [], "mode": "exact", "more_files": False, "line_count": 0}
    small = chunk_count is not None and int(chunk_count) <= SUBSTRING_SCAN_MAX_CHUNKS
    modes = ["phrase", "all_words"] if len(parsed.words) > 1 else ["exact"]
    results: list[dict] = []
    more = False
    mode = modes[0]
    for mode in modes:
        # A large repository is scanned by substring only as the last resort
        # (text inside longer identifiers, which the word index cannot see).
        sources = ["substring"] if small else (["fts", "substring"] if mode == modes[-1] else ["fts"])
        results, more = await _search_once(
            store, repo_id, parsed, mode, limit=max(1, limit), staging_prefix=staging_prefix, sources=sources,
        )
        if results:
            break
    return {
        "results": results,
        "mode": mode,
        "more_files": more,
        "line_count": sum(result["match_count"] for result in results),
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

MAX_LISTED_FILES = 25000
_PATHS_TTL_SECONDS = 120.0
_PATHS_CACHE_REPOS = 8
_PATHS_CACHE: OrderedDict[str, tuple[float, tuple, list[tuple[str, int]]]] = OrderedDict()


def invalidate_paths(repo_id: str) -> None:
    _PATHS_CACHE.pop(repo_id, None)


async def repository_paths(store, repo_id: str, *, version: tuple = (), staging_prefix: str | None = None) -> list[tuple[str, int]]:
    """Every indexed ``(path, byte_size)``, sorted by path, cached briefly per repository version."""
    now = time.monotonic()
    cached = _PATHS_CACHE.get(repo_id)
    if cached and cached[1] == version and now - cached[0] < _PATHS_TTL_SECONDS:
        _PATHS_CACHE.move_to_end(repo_id)
        return cached[2]
    rows = await store.fetch_all(
        "SELECT file_path, byte_size FROM repo_files WHERE repo_id = ? ORDER BY file_path LIMIT ?",
        [repo_id, MAX_LISTED_FILES],
    )
    if not rows:
        stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
        rows = await store.fetch_all(
            f"SELECT DISTINCT file_path, 0 AS byte_size FROM chunks WHERE repo_id = ?{stage_sql} ORDER BY file_path LIMIT ?",
            [repo_id, *stage_args, MAX_LISTED_FILES],
        )
    paths = [(str(row["file_path"]), int(row.get("byte_size") or 0)) for row in rows if row.get("file_path")]
    _PATHS_CACHE[repo_id] = (now, version, paths)
    _PATHS_CACHE.move_to_end(repo_id)
    while len(_PATHS_CACHE) > _PATHS_CACHE_REPOS:
        _PATHS_CACHE.popitem(last=False)
    return paths


def _subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for character in needle:
        position = haystack.find(character, position)
        if position == -1:
            return False
        position += 1
    return True


def path_match_tier(path: str, query: str) -> int | None:
    """How well ``path`` matches a typed name or path (0 best), or None.

    0 the file name or its stem (``_client`` or ``client`` for
    ``_client.py``); 1 the name starts with it (leading ``_`` ignored); 2 the
    name contains it; 3 the path starts with it or a folder name does; 4 the
    path contains it; 5 a plain word whose characters appear in order in the
    name (``rspns`` → ``response.js``).
    """
    needle = query.strip().lower().lstrip("./")
    if not needle:
        return None
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name[1:] else name
    if needle in {name, stem, stem.lstrip("_")}:
        return 0
    if name.startswith(needle) or name.lstrip("_").startswith(needle):
        return 1
    if needle in name:
        return 2
    if low.startswith(needle) or f"/{needle}" in low:
        return 3
    if needle in low:
        return 4
    # A typed path or file name with an extension is specific: no guessing.
    if len(needle) >= 3 and not any(mark in needle for mark in "./") and _subsequence(needle, name):
        return 5
    return None


def find_files(paths: list[tuple[str, int]], query: str, *, limit: int = 20) -> tuple[list[dict], int]:
    """Indexed files for a typed name or path, best first, and how many matched."""
    matched = []
    for path, size in paths:
        tier = path_match_tier(path, query)
        if tier is not None:
            matched.append((tier, is_support_path(path), len(path), path, size))
    matched.sort()
    return [
        {"path": path, "name": path.rsplit("/", 1)[-1], "bytes": size}
        for _tier, _support, _length, path, size in matched[: max(1, limit)]
    ], len(matched)


def list_directory(paths: list[tuple[str, int]], directory: str = "") -> dict:
    """One folder of the file tree: its sub-folders (with file counts) and files.

    Source folders come first, then tests, examples, docs and the like, then
    dot-folders such as ``.github``; files follow, alphabetically.
    """
    prefix = directory.strip().strip("/")
    base = f"{prefix}/" if prefix else ""
    folders: dict[str, int] = {}
    files: list[dict] = []
    for path, size in paths:
        if not path.startswith(base):
            continue
        rest = path[len(base):]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            folders[name] = folders.get(name, 0) + 1
        elif rest:
            files.append({"path": path, "name": rest, "bytes": size})
    return {
        "path": prefix,
        "directories": [
            {"path": f"{base}{name}", "name": name, "files": count}
            for name, count in sorted(folders.items(), key=lambda item: (_folder_rank(item[0]), item[0].lower()))
        ],
        "files": sorted(files, key=lambda item: (item["name"].startswith("."), item["name"].lower())),
    }


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

_SYMBOL_CANDIDATE_ROWS = 2000
_SYMBOL_READ_ROWS = 60


def symbol_match_tier(name: str, query: str) -> int | None:
    """0 the exact name, 1 the same name in another case, 2 a prefix, 3 the
    start of a word inside the name (``timeout`` in ``_set_timeout``,
    ``Type`` in ``ParamType``), 4 anywhere in it; None otherwise."""
    if not query:
        return None
    if name == query:
        return 0
    low, needle = name.lower(), query.lower()
    if low == needle:
        return 1
    if low.startswith(needle):
        return 2
    position = low.find(needle)
    if position < 0:
        return None
    while position >= 0:
        before = name[position - 1]
        if before in "_$" or (name[position].isupper() and before.islower()):
            return 3
        position = low.find(needle, position + 1)
    return 4


def _container(lines: list[str], index: int) -> str | None:
    """The declaration a line sits in, within one section (``ParamType`` for its methods)."""
    indent = _indent(lines[index])
    if indent == 0:
        return None
    for position in range(index - 1, -1, -1):
        text = lines[position]
        if not text.strip() or _indent(text) >= indent:
            continue
        found = line_declaration(text)
        if found:
            return found[0]
        if _indent(text) == 0:
            return None
    return None


async def find_symbols(store, repo_id: str, query: str, *, limit: int = 20, staging_prefix: str | None = None) -> list[dict]:
    """Declarations whose name matches ``query``, each with the line that declares it.

    Returns ``{name, kind, path, line, container}`` items, best first: exact
    names, then prefixes, then names containing the query; source before
    tests and docs.
    """
    needle = str(query or "").strip()
    if len(needle) < 2 or len(needle) > 120:
        return []
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    rows = await store.fetch_all(
        f"SELECT rowid AS row_id, file_path, start_line, symbols FROM chunks WHERE repo_id = ?{stage_sql} "
        "AND symbols LIKE ? ESCAPE '\\' LIMIT ?",
        [repo_id, *stage_args, f"%{_escape_like(needle)}%", _SYMBOL_CANDIDATE_ROWS],
    )
    hits: list[tuple] = []
    for row in rows:
        path = str(row.get("file_path") or "")
        for name in _symbol_names(row.get("symbols")):
            tier = symbol_match_tier(name, needle)
            if tier is not None:
                hits.append((tier, is_support_path(path), len(name), path, int(row.get("start_line") or 0), name, int(row["row_id"])))
    hits.sort()
    wanted: list[tuple] = []
    reads: list[int] = []
    for hit in hits:
        if hit[-1] not in reads:
            if len(reads) >= _SYMBOL_READ_ROWS:
                continue
            reads.append(hit[-1])
        wanted.append(hit)
        if len(wanted) >= limit * 2:
            break
    texts = await _read_rows(store, reads)
    results: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for _tier, _support, _length, path, _start, name, row_id in wanted:
        row = texts.get(row_id)
        if not row:
            continue
        start = int(row.get("start_line") or 1)
        lines = str(row.get("content") or "").split("\n")
        found_any = False
        for index, line in enumerate(lines):
            declared = line_declaration(line)
            if not declared or declared[0] != name:
                continue
            found_any = True
            key = (path, start + index)
            if key in seen:
                continue
            seen.add(key)
            results.append({"name": name, "kind": declared[1], "path": path, "line": start + index,
                            "container": _container(lines, index)})
        if not found_any:
            # The chunker saw a declaration this line-by-line check cannot
            # (it spans lines): point at the first whole-word mention.
            word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
            index = next((position for position, line in enumerate(lines) if word.search(line)), None)
            if index is not None and (path, start + index) not in seen:
                seen.add((path, start + index))
                results.append({"name": name, "kind": "symbol", "path": path, "line": start + index,
                                "container": _container(lines, index)})
        if len(results) >= limit:
            break
    return results[:limit]
