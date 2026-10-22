"""Scope of a change on big repositories (G7).

* Exact usage counts: every indexed file that references a symbol, found
  with FTS and verified by a whole-word regex in pages of 500 rows (the old
  usage search read at most 64 chunk rows and silently truncated).
* Definition anchoring: names the request treats as existing are located;
  the defining file leads the candidates, and a name that appears nowhere
  stops the edit before any model call.
* Split plans: a change too large for one reviewable proposal gets an
  ordered plan of parts, each with a ready-to-send request.
"""

from __future__ import annotations

import logging
import math
import posixpath
import re
from typing import Any

from agent.quality import CODE_EXTENSIONS, DOC_EXTENSIONS, file_extension, is_doc_path, is_test_path

logger = logging.getLogger(__name__)

_PAGE = 500
_MAX_PAGES = 20
_NOT_STAGED = (
    "NOT EXISTS (SELECT 1 FROM repo_index_runs runs WHERE runs.repo_id = c.repo_id AND runs.chunk_prefix <> '' "
    "AND substr(c.id, 1, length(runs.chunk_prefix)) = runs.chunk_prefix)"
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_JS = re.compile(r"(?m)(^|[^:\"'\\])//.*$")
_LINE_COMMENT_HASH = re.compile(r"(?m)#.*$")
_JSDOC_LINE = re.compile(r"(?m)^\s*\*.*$")
_HASH_COMMENT_EXTENSIONS = frozenset({"py", "pyi", "rb", "sh", "bash", "yml", "yaml", "toml", "ex", "exs"})


def _fts_term(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "", name or "").lower()


def strip_comments(text: str, path: str) -> str:
    """Code with comments removed (approximate, per chunk) for usage classification."""
    extension = file_extension(path)
    if extension in _HASH_COMMENT_EXTENSIONS:
        return _LINE_COMMENT_HASH.sub("", text)
    stripped = _BLOCK_COMMENT.sub(" ", text)
    stripped = _JSDOC_LINE.sub("", stripped)
    return _LINE_COMMENT_JS.sub(r"\1", stripped)


async def fts_file_count(store: Any, repo_id: str, name: str) -> int:
    """``COUNT(DISTINCT file_path)`` of chunks whose FTS tokens match ``name`` (an upper bound)."""
    term = _fts_term(name)
    if len(term) < 2:
        return 0
    try:
        row = await store.fetch_one(
            "SELECT COUNT(DISTINCT c.file_path) AS files FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED}",
            [f'"{term}"', repo_id],
        )
    except Exception:
        logger.debug("FTS count failed for %s", name, exc_info=True)
        return -1
    return int((row or {}).get("files") or 0)


async def symbol_reference_files(store: Any, repo_id: str, names: list[str], *,
                                 max_pages: int = _MAX_PAGES) -> dict[str, dict[str, str]]:
    """For each name: ``{path: "code" | "comment" | "doc"}`` of every indexed file that mentions it.

    "code" means the name occurs outside comments in a code file; "comment"
    only inside comments; "doc" in documentation or data files. Rows are
    read in pages of 500 (at most ``max_pages`` pages per name).
    """
    result: dict[str, dict[str, str]] = {}
    rank = {"doc": 0, "comment": 1, "code": 2}
    for name in dict.fromkeys(names):
        term = _fts_term(name)
        files: dict[str, str] = {}
        result[name] = files
        if len(term) < 2:
            continue
        pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
        for page in range(max(1, max_pages)):
            try:
                rows = await store.fetch_all(
                    "SELECT c.file_path, c.content FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED} ORDER BY c.rowid LIMIT ? OFFSET ?",
                    [f'"{term}"', repo_id, _PAGE, page * _PAGE],
                )
            except Exception:
                logger.warning("Usage search failed for %s", name, exc_info=True)
                break
            for row in rows:
                path = str(row.get("file_path") or "")
                content = str(row.get("content") or "")
                if not path or not pattern.search(content):
                    continue
                extension = file_extension(path)
                if is_doc_path(path) or extension in DOC_EXTENSIONS or extension not in CODE_EXTENSIONS:
                    kind = "doc"
                elif pattern.search(strip_comments(content, path)):
                    kind = "code"
                else:
                    kind = "comment"
                if rank[kind] > rank.get(files.get(path, ""), -1):
                    files[path] = kind
            if len(rows) < _PAGE:
                break
    return result


def definition_patterns(name: str) -> list[re.Pattern]:
    escaped = re.escape(name)
    return [
        re.compile(r"\b(?:class|function\*?|def|const|let|var|model|enum|interface|type|struct|trait|fn|func|module|"
                   r"export\s+default\s+function)\s+" + escaped + r"\b"),
        re.compile(r"\bfunc\s+\([^)]*\)\s*" + escaped + r"\b"),
        re.compile(r"(?m)^\s*(?:(?:public|private|protected|static|async|override|readonly|abstract|get|set)\s+)*"
                   + escaped + r"\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;=]+)?\{"),
        re.compile(r"(?m)^\s*(?:export\s+)?" + escaped + r"\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\(|function\b)"),
    ]


async def find_definition_files(store: Any, repo_id: str, name: str) -> list[str]:
    """Indexed files that define ``name`` (source before tests)."""
    term = _fts_term(name)
    if len(term) < 2:
        return []
    patterns = definition_patterns(name)
    found: list[str] = []
    for page in range(4):
        try:
            rows = await store.fetch_all(
                "SELECT c.file_path, c.content FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                f"WHERE chunks_fts MATCH ? AND c.repo_id = ? AND {_NOT_STAGED} ORDER BY c.rowid LIMIT ? OFFSET ?",
                [f'"{term}"', repo_id, _PAGE, page * _PAGE],
            )
        except Exception:
            logger.debug("Definition search failed for %s", name, exc_info=True)
            break
        for row in rows:
            path = str(row.get("file_path") or "")
            if path in found or file_extension(path) not in CODE_EXTENSIONS:
                continue
            content = str(row.get("content") or "")
            if any(pattern.search(content) for pattern in patterns):
                found.append(path)
        if len(rows) < _PAGE:
            break
    return sorted(found, key=lambda path: (is_test_path(path), is_doc_path(path), path))


async def indexed_file_count(store: Any, repo_id: str) -> int:
    try:
        row = await store.fetch_one("SELECT COUNT(*) AS files FROM repo_files WHERE repo_id = ?", [repo_id])
        count = int((row or {}).get("files") or 0)
        if count:
            return count
        row = await store.fetch_one("SELECT COUNT(DISTINCT file_path) AS files FROM chunks WHERE repo_id = ?", [repo_id])
        return int((row or {}).get("files") or 0)
    except Exception:
        return 0


_VERBS = r"(?:update|change|fix|modify|refactor|remove|delete|rename|move|replace|deprecate|inline|extract|rewrite|edit|debug|simplify|optimi[sz]e|call|use)"
_NAMED_KIND = re.compile(
    r"\bthe\s+(`?)([A-Za-z_$][\w$]{2,})\1\s+(?:function|method|class|component|hook|type|helper|constant|variable|enum|interface|struct|decorator)\b",
    re.IGNORECASE,
)
_VERB_BACKTICK = re.compile(rf"\b{_VERBS}\s+(?:the\s+)?(?:\w+\s+)?`([A-Za-z_$][\w$]*)`", re.IGNORECASE)
_RENAME_SOURCE = re.compile(r"\brename\s+(?:the\s+)?(?:[a-z]+\s+)?([`'\"]?)([A-Za-z_$][\w$]{1,})\1", re.IGNORECASE)
_IDENTIFIER_LIKE = re.compile(r"^[A-Za-z_$][a-z0-9$]*(?:[A-Z_$][\w$]*)$|^[A-Za-z_$]\w*\d\w*$")
_BACKTICK = re.compile(r"`([A-Za-z_$][\w$]{2,})`")
_NEW_NAME_CONTEXT = re.compile(r"\b(?:add|create|new|introduce|implement|write|define)\b[^.`]{0,40}$", re.IGNORECASE)


def requested_names(question: str, renames: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(names to anchor on, names that must already exist in the index)."""
    text = question or ""
    new_names = {new for _old, new in renames}
    must: list[str] = [old for old, _new in renames]
    anchors: list[str] = list(must)
    for match in _RENAME_SOURCE.finditer(text):
        name = match.group(2)
        # Only an exact identifier: quoted, or camelCase/snake_case/with digits.
        if name not in new_names and (match.group(1) or _IDENTIFIER_LIKE.match(name)):
            must.append(name)
            anchors.append(name)
    for match in _NAMED_KIND.finditer(text):
        name = match.group(2)
        if name in new_names or _NEW_NAME_CONTEXT.search(text[: match.start()]):
            continue
        anchors.append(name)
        if match.group(1):  # backticked: the user is naming an exact identifier
            must.append(name)
    for match in _VERB_BACKTICK.finditer(text):
        name = match.group(1)
        if name not in new_names and len(name) >= 3:
            must.append(name)
            anchors.append(name)
    for match in _BACKTICK.finditer(text):
        name = match.group(1)
        if name not in new_names and not _NEW_NAME_CONTEXT.search(text[: match.start()]):
            anchors.append(name)
    keep = lambda names: [name for name in dict.fromkeys(names) if "." not in name and "/" not in name]  # noqa: E731
    return keep(anchors)[:6], keep(must)[:4]


def split_parts(files: list[str], *, definitions: list[str], limit: int) -> list[list[str]]:
    """Definitions first, then dependents grouped by top-level directory, ``limit`` files per part."""
    ordered = [path for path in definitions if path in files]
    rest = [path for path in files if path not in ordered]
    rest.sort(key=lambda path: (path.split("/", 1)[0] if "/" in path else "", path))
    sequence = ordered + rest
    size = max(1, int(limit))
    return [sequence[index:index + size] for index in range(0, len(sequence), size)]


def _file_list(paths: list[str]) -> str:
    return ", ".join(paths)


def split_plan(question: str, files: list[str], *, definitions: list[str], limit: int,
               renames: list[tuple[str, str]] | None = None) -> list[dict]:
    """Ordered parts ``[{title, files, prompt}]`` for a change that is too large."""
    groups = split_parts(files, definitions=definitions, limit=limit)
    count = len(groups)
    parts: list[dict] = []
    if renames:
        old, new = renames[0]
        definition = next((path for path in definitions if path in files), groups[0][0] if groups and groups[0] else "")
        for index, group in enumerate(groups, start=1):
            others = [path for path in group if path != definition]
            if index == 1:
                prompt = (
                    f"Part 1 of {count}: in {definition}, export `{old}` under the new name `{new}` and keep `{old}` "
                    f"as a deprecated alias of `{new}` so files that are not updated yet keep working."
                    + (f" Then use `{new}` instead of `{old}` in {_file_list(others)}." if others else "")
                )
                title = f"Introduce `{new}` with a deprecated `{old}` alias"
            else:
                prompt = (
                    f"Part {index} of {count} (merge after part {index - 1}): use `{new}` instead of `{old}` in "
                    f"{_file_list(group)}. `{new}` is exported by {definition}."
                )
                if index == count:
                    prompt += f" After this part is merged, remove the deprecated `{old}` alias from {definition}."
                title = f"Use `{new}` in {len(group)} file{'s' if len(group) != 1 else ''}"
            parts.append({"title": title, "files": group, "prompt": prompt})
        return parts
    request = re.sub(r"\s+", " ", question or "").strip()
    for index, group in enumerate(groups, start=1):
        order = f" (merge after part {index - 1})" if index > 1 else ""
        title = "Definitions and exports" if index == 1 and any(path in definitions for path in group) else (
            "Update " + (group[0].split("/", 1)[0] + "/" if "/" in group[0] else group[0]))
        prompt = (
            f"Part {index} of {count}{order}: {request} In this part change only {_file_list(group)}, "
            "and keep the code working for the files that later parts update."
        )
        parts.append({"title": title, "files": group, "prompt": prompt})
    return parts


def split_markdown(parts: list[dict], *, total: int, limit: int, reason: str) -> str:
    lines = ["## This change is too large for one reviewable proposal", "", reason, "",
             "Smaller pull requests are reviewed and merged more readily. Send these parts one at a time and "
             "merge them in order:"]
    for index, part in enumerate(parts, start=1):
        lines.extend(["", f"### Part {index} of {len(parts)}: {part['title']}",
                      "Files: " + ", ".join(f"`{path}`" for path in part["files"]), "", f"> {part['prompt']}"])
    lines.extend(["", "No files were changed."])
    return "\n".join(lines)


def part_count(total: int, limit: int) -> int:
    return max(1, math.ceil(total / max(1, limit)))


def top_directory(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else posixpath.dirname(path)
