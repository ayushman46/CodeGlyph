"""Deterministic quality gates for a proposed change.

Everything here is pure (no database or network) so the editor, the GitHub
push path and the tests can share it:

* G1 syntax: every after-file is parsed by the strongest checker available
  (stdlib ``ast``/``json``/``tomllib``, PyYAML, the native esbuild binary for
  JS/TS/CSS, then tree-sitter grammars), with a baseline rule so a parser
  limit or a file that was already broken never blocks a change.
* G2 style: a per-file style profile from repository config or the file
  itself, a whitespace-only autofix on added lines, and warnings for quote
  and semicolon style.
* G3 imports: relative JS/TS and Python imports must resolve and the named
  imports must exist; the explicit-extension convention is autofixed.
* G4 helpers: changed literals and the line rules that find stale mentions.

This module must not import ``github.*`` or ``retrieval.*`` at module level
(``github_client`` imports it lazily for push-time checks).
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import importlib
import json
import logging
import os
import platform
import posixpath
import re
import subprocess
import threading
import tomllib
import weakref
from pathlib import Path
from typing import Any, Callable, Protocol

from agent.code_edit import _diff_lines, _line_opcodes
from config import settings

logger = logging.getLogger(__name__)

STATUS_ORDER = {"skipped": 0, "pass": 1, "warn": 2, "fail": 3}
MAX_REPORTED_ERRORS = 20


def worst_status(statuses: list[str]) -> str:
    """The most severe of pass/warn/fail ("skipped" counts as pass here)."""
    worst = "pass"
    for status in statuses:
        if STATUS_ORDER.get(status, 0) > STATUS_ORDER[worst]:
            worst = status
    return worst


def file_extension(path: str) -> str:
    name = str(path or "").rsplit("/", 1)[-1].lower()
    if name.endswith(".d.ts"):
        return "ts"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def file_name(path: str) -> str:
    return str(path or "").rsplit("/", 1)[-1]


JS_TS_EXTENSIONS = frozenset({"js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"})
PYTHON_EXTENSIONS = frozenset({"py", "pyi"})
DOC_EXTENSIONS = frozenset({"md", "mdx", "markdown", "rst", "txt", "adoc"})
CODE_EXTENSIONS = JS_TS_EXTENSIONS | PYTHON_EXTENSIONS | frozenset({
    "go", "rs", "java", "kt", "kts", "rb", "php", "cs", "swift", "c", "h", "cc", "cpp", "cxx", "hpp", "hh",
    "scala", "vue", "svelte", "sh", "bash", "ex", "exs", "dart", "lua", "m", "mm",
})

_TEST_PATH = re.compile(
    r"(?:\.|/|_|-)(?:test|tests|spec|specs|e2e-spec|e2e|integration-test|integration\.test)\.[a-z0-9]+$|"
    r"(?:^|/)(?:__tests__|tests?|e2e|playwright|cypress|testing|spec)/|(?:^|/)test_[^/]*\.py$",
    re.IGNORECASE,
)
_DOC_PATH = re.compile(r"\.(?:md|mdx|markdown|rst|txt|adoc)$|(?:^|/)(?:docs?|documentation)/", re.IGNORECASE)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(str(path or "")))


def is_doc_path(path: str) -> bool:
    return bool(_DOC_PATH.search(str(path or "")))


def is_source_path(path: str) -> bool:
    """Code that ships behaviour: a code extension, not a test, not docs."""
    return file_extension(path) in CODE_EXTENSIONS and not is_test_path(path) and not is_doc_path(path)


# ===========================================================================
# G1: syntax
# ===========================================================================

_ESBUILD_LOADERS = {
    "ts": "ts", "mts": "ts", "cts": "ts", "tsx": "tsx",
    # JSX is a superset of plain JavaScript for parsing, and React projects
    # routinely keep JSX in .js files.
    "js": "jsx", "mjs": "jsx", "cjs": "jsx", "jsx": "jsx",
    "css": "css",
}
_TREE_SITTER_GRAMMARS: dict[str, tuple[tuple[str, str], ...]] = {
    "ts": (("tree_sitter_typescript", "language_typescript"),),
    "mts": (("tree_sitter_typescript", "language_typescript"),),
    "cts": (("tree_sitter_typescript", "language_typescript"),),
    "tsx": (("tree_sitter_typescript", "language_tsx"),),
    "js": (("tree_sitter_javascript", "language"),),
    "mjs": (("tree_sitter_javascript", "language"),),
    "cjs": (("tree_sitter_javascript", "language"),),
    "jsx": (("tree_sitter_javascript", "language"),),
    "css": (("tree_sitter_css", "language"),),
    "go": (("tree_sitter_go", "language"),),
    "java": (("tree_sitter_java", "language"),),
    "rs": (("tree_sitter_rust", "language"),),
    "c": (("tree_sitter_c", "language"),),
    # A .h header may be C or C++: the grammar with fewer errors wins.
    "h": (("tree_sitter_c", "language"), ("tree_sitter_cpp", "language")),
    "cc": (("tree_sitter_cpp", "language"),),
    "cpp": (("tree_sitter_cpp", "language"),),
    "cxx": (("tree_sitter_cpp", "language"),),
    "hpp": (("tree_sitter_cpp", "language"),),
    "hh": (("tree_sitter_cpp", "language"),),
    "hxx": (("tree_sitter_cpp", "language"),),
    "cs": (("tree_sitter_c_sharp", "language"),),
    "rb": (("tree_sitter_ruby", "language"),),
    "php": (("tree_sitter_php", "language_php"),),
    "kt": (("tree_sitter_kotlin", "language"),),
    "kts": (("tree_sitter_kotlin", "language"),),
    "sh": (("tree_sitter_bash", "language"),),
    "bash": (("tree_sitter_bash", "language"),),
}
_JSONC_NAMES = re.compile(r"(?:^|/)(?:tsconfig[^/]*|jsconfig[^/]*|\.eslintrc|devcontainer)\.json$|\.jsonc$|(?:^|/)\.vscode/[^/]+\.json$|(?:^|/)\.devcontainer/[^/]+\.json$", re.IGNORECASE)
_ESBUILD_MESSAGE = re.compile(r"^\s*\S{0,2}\s*\[(ERROR|WARNING)\]\s+(.*?)\s*$")
_ESBUILD_LOCATION = re.compile(r"^\s+<stdin>:(\d+):(\d+):\s*$")
_ESBUILD_TIMEOUT_SECONDS = 10
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# One parse (or esbuild process) at a time in the whole process: parse cost
# is about 50 bytes of RSS per input byte, so concurrency would multiply the
# peak on a 512 MB instance.
_SYNTAX_LOCK = threading.Lock()
_ASYNC_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_PARSERS: dict[tuple[str, str], Any] = {}
_PARSERS_LOCK = threading.Lock()


def _esbuild_platform_dir() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64", "i386": "ia32", "i686": "ia32"}.get(machine, machine)
    if system.startswith("win"):
        return f"win32-{arch}", "esbuild.exe"
    return f"{system}-{arch}", "bin/esbuild"


def esbuild_binary() -> str | None:
    """Path of a native esbuild executable, or None when it is not installed.

    ``ESBUILD_PATH`` overrides the location ("off" disables esbuild). By
    default the binary that ``npm ci`` installs for Vite is used.
    """
    configured = str(getattr(settings, "esbuild_path", "") or "").strip()
    if configured.lower() in {"off", "none", "false", "0", "disabled"}:
        return None
    if configured:
        candidates = [configured]
    else:
        directory, executable = _esbuild_platform_dir()
        candidates = [str(_REPOSITORY_ROOT / "frontend" / "node_modules" / "@esbuild" / directory / executable)]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tree_sitter_parser(module: str, attribute: str) -> Any:
    key = (module, attribute)
    with _PARSERS_LOCK:
        if key in _PARSERS:
            return _PARSERS[key]
        try:
            import tree_sitter

            language = tree_sitter.Language(getattr(importlib.import_module(module), attribute)())
            parser = tree_sitter.Parser(language)
        except Exception:  # missing wheel or ABI mismatch: the language is skipped
            logger.debug("tree-sitter grammar %s.%s unavailable", module, attribute, exc_info=True)
            parser = None
        _PARSERS[key] = parser
        return parser


def tree_sitter_available(extension: str = "ts") -> bool:
    grammars = _TREE_SITTER_GRAMMARS.get(extension.lower().lstrip("."), ())
    return any(_tree_sitter_parser(module, attribute) is not None for module, attribute in grammars)


def _error(line: Any, column: Any, message: str) -> dict:
    return {
        "line": int(line) if isinstance(line, int) and line > 0 else None,
        "column": int(column) if isinstance(column, int) and column > 0 else None,
        "message": str(message or "syntax error").strip()[:300],
    }


def _strip_jsonc(text: str) -> str:
    """Blank out comments and trailing commas, keeping every line and column."""
    out = list(text)
    index, length = 0, len(text)
    in_string = False
    while index < length:
        character = text[index]
        if in_string:
            if character == "\\":
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            for position in range(index, end):
                out[position] = " "
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue
        if character == ",":
            following = index + 1
            while following < length and text[following] in " \t\r\n":
                following += 1
            if text.startswith("//", following) or text.startswith("/*", following):
                # A comment between the comma and the bracket: look past it.
                probe = _strip_jsonc(text[following:]).lstrip()
                if probe[:1] in ("]", "}"):
                    out[index] = " "
            elif following < length and text[following] in "]}":
                out[index] = " "
        index += 1
    return "".join(out)


def _python_errors(text: str) -> list[dict]:
    try:
        ast.parse(text)
    except SyntaxError as error:
        return [_error(error.lineno, error.offset, error.msg)]
    except ValueError as error:  # NUL bytes
        return [_error(None, None, str(error))]
    return []


def _json_errors(text: str, *, jsonc: bool) -> list[dict]:
    source = text.lstrip("﻿")
    if jsonc:
        source = _strip_jsonc(source)
    if not source.strip():
        return [] if jsonc else [_error(1, 1, "Expecting value")]
    try:
        json.loads(source)
    except json.JSONDecodeError as error:
        return [_error(error.lineno, error.colno, error.msg)]
    return []


def _toml_errors(text: str) -> list[dict]:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = str(error)
        position = re.search(r"\(at line (\d+), column (\d+)\)", message)
        clean = re.sub(r"\s*\(at line \d+, column \d+\)", "", message)
        if position:
            return [_error(int(position.group(1)), int(position.group(2)), clean)]
        return [_error(None, None, clean)]
    return []


def _yaml_errors(text: str) -> list[dict] | None:
    try:
        import yaml
    except ImportError:
        return None
    loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
    try:
        for _document in yaml.load_all(text, Loader=loader):
            pass
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
        problem = getattr(error, "problem", None) or str(error).split("\n")[0]
        if mark is not None:
            return [_error(mark.line + 1, mark.column + 1, str(problem))]
        return [_error(None, None, str(problem))]
    return []


def _esbuild_errors(binary: str, loader: str, text: str) -> list[dict] | None:
    """esbuild's syntax errors, or None when esbuild itself could not run."""
    level = "warning" if loader == "css" else "error"
    try:
        result = subprocess.run(
            [binary, f"--loader={loader}", f"--log-level={level}", "--color=false", "--log-limit=0"],
            input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=_ESBUILD_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("esbuild could not run; falling back to tree-sitter", exc_info=True)
        return None
    stderr = result.stderr.decode("utf-8", errors="replace")
    errors: list[dict] = []
    current: dict | None = None
    for line in stderr.splitlines():
        message = _ESBUILD_MESSAGE.match(line)
        if message:
            level_name, body = message.group(1), message.group(2)
            tag = re.search(r"\s\[([a-z-]+)\]$", body)
            body = body[: tag.start()] if tag else body
            # CSS syntax problems are warnings in esbuild; other CSS warnings
            # (unknown properties, unsupported at-rules) are not syntax.
            is_syntax = level_name == "ERROR" or (loader == "css" and tag is not None and tag.group(1) == "css-syntax-error")
            current = {"line": None, "column": None, "message": body} if is_syntax else None
            if current is not None:
                errors.append(current)
            continue
        location = _ESBUILD_LOCATION.match(line)
        if location and current is not None and current["line"] is None:
            current["line"] = int(location.group(1))
            current["column"] = int(location.group(2)) + 1  # esbuild columns are 0-based
    if result.returncode != 0 and not errors and loader != "css":
        errors.append({"line": None, "column": None, "message": (stderr.strip().splitlines() or ["esbuild failed"])[0][:300]})
    return [_error(item["line"], item["column"], item["message"]) for item in errors]


def _tree_sitter_errors(parser: Any, text: str) -> list[dict]:
    data = text.encode("utf-8")
    tree = parser.parse(data)
    try:
        root = tree.root_node
        if not root.has_error:
            return []
        found: list[tuple[int, int, str]] = []
        stack = [root]
        while stack and len(found) < 50:
            node = stack.pop()
            if node.is_missing:
                found.append((node.start_point[0], node.start_point[1], f"missing {node.type}"))
            elif node.is_error:
                snippet = data[node.start_byte:min(node.end_byte, node.start_byte + 40)].decode("utf-8", errors="replace")
                snippet = snippet.split("\n")[0].strip()
                found.append((node.start_point[0], node.start_point[1],
                              f"unexpected syntax near '{snippet}'" if snippet else "unexpected syntax"))
            elif node.has_error:
                stack.extend(reversed(node.children))
        lines = data.split(b"\n")
        result = []
        for row, byte_column, message in sorted(found):
            line_bytes = lines[row] if row < len(lines) else b""
            column = len(line_bytes[:byte_column].decode("utf-8", errors="replace")) + 1
            result.append(_error(row + 1, column, message))
        return result
    finally:
        del tree


def _syntax_errors_unlocked(path: str, text: str) -> tuple[str, list[dict]] | None:
    extension = file_extension(path)
    if extension in PYTHON_EXTENSIONS:
        return "ast", _python_errors(text)
    if extension in {"json", "jsonc"}:
        jsonc = bool(_JSONC_NAMES.search(path))
        return ("jsonc" if jsonc else "json"), _json_errors(text, jsonc=jsonc)
    if extension == "toml":
        return "tomllib", _toml_errors(text)
    if extension in {"yml", "yaml"}:
        errors = _yaml_errors(text)
        return None if errors is None else ("pyyaml", errors)
    if extension in _ESBUILD_LOADERS:
        binary = esbuild_binary()
        if binary:
            errors = _esbuild_errors(binary, _ESBUILD_LOADERS[extension], text)
            if errors is not None:
                return "esbuild", errors
    grammars = _TREE_SITTER_GRAMMARS.get(extension)
    if grammars:
        best: list[dict] | None = None
        for module, attribute in grammars:
            parser = _tree_sitter_parser(module, attribute)
            if parser is None:
                continue
            errors = _tree_sitter_errors(parser, text)
            if best is None or len(errors) < len(best):
                best = errors
            if not errors:
                break
        if best is not None:
            return "tree-sitter", best
    return None


def _too_large(text: str) -> bool:
    limit = int(getattr(settings, "syntax_check_max_bytes", 1_000_000) or 1_000_000)
    return len(text.encode("utf-8", errors="replace")) > limit


def syntax_errors(path: str, text: str) -> tuple[str, list[dict]] | None:
    """``(checker, errors)`` for ``text``, or None (no checker, or too large).

    Thread-safe: one parse runs at a time in the whole process.
    """
    if not isinstance(text, str) or _too_large(text):
        return None
    with _SYNTAX_LOCK:
        return _syntax_errors_unlocked(path, text)


def _syntax_result(status: str, checker: str, errors: list[dict], detail: str) -> dict:
    return {"status": status, "checker": checker, "errors": errors[:MAX_REPORTED_ERRORS], "detail": detail}


def check_syntax(path: str, after: str, *, before: str | None = None) -> dict:
    """Check one after-file with the baseline rule; sync and thread-safe.

    Returns ``{"status", "checker", "errors", "detail"}``. A modified file
    fails only when it has errors its ``before`` version did not have (or
    more of them); a created file (``before`` None or empty) fails on any
    error. Files without a checker, or over the size limit, are "skipped",
    never "pass".
    """
    if not isinstance(after, str):
        return _syntax_result("skipped", "none", [], "The file content is not available to check.")
    limit = int(getattr(settings, "syntax_check_max_bytes", 1_000_000) or 1_000_000)
    if _too_large(after):
        return _syntax_result("skipped", "none", [], f"The file is larger than the {limit:,}-byte syntax check limit.")
    result = syntax_errors(path, after)
    if result is None:
        extension = file_extension(path)
        label = f".{extension} files" if extension else "this file type"
        return _syntax_result("skipped", "none", [], f"No syntax checker is available for {label}.")
    checker, errors = result
    if not errors:
        return _syntax_result("pass", checker, [], f"Parsed with {checker}; no errors.")
    first = errors[0]
    where = f" at {first['line']}:{first['column'] or 1}" if first.get("line") else ""
    if not before:
        return _syntax_result("fail", checker, errors, f"{len(errors)} syntax error(s) ({checker}){where}: {first['message']}")
    before_result = syntax_errors(path, before)
    before_errors = before_result[1] if before_result is not None and before_result[0] == checker else None
    if before_errors is None or not before_errors or len(errors) > len(before_errors):
        return _syntax_result("fail", checker, errors, f"{len(errors)} syntax error(s) ({checker}){where}: {first['message']}")
    return _syntax_result(
        "warn", checker, errors,
        f"This file already failed to parse with {checker} before the change; the change adds no new errors.",
    )


def _async_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _ASYNC_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _ASYNC_LOCKS[loop] = lock
    return lock


async def check_syntax_many(items: list[dict]) -> dict[str, dict]:
    """Check several files one at a time, off the event loop.

    ``items`` are ``{"file_path", "after", "before"?}``. Serialised through a
    per-loop asyncio lock (and the process-wide thread lock inside
    ``check_syntax``), so concurrent requests never parse in parallel.
    """
    results: dict[str, dict] = {}
    lock = _async_lock()
    for item in items:
        path = str(item.get("file_path") or "")
        async with lock:
            results[path] = await asyncio.to_thread(check_syntax, path, item.get("after"), before=item.get("before") or None)
    return results


def syntax_messages(path: str, result: dict) -> list[str]:
    """``path:line:column message (checker)`` lines for a failed check."""
    lines = []
    for error in result.get("errors") or []:
        where = f":{error['line']}:{error.get('column') or 1}" if error.get("line") else ""
        lines.append(f"{path}{where} {error['message']} ({result.get('checker')})")
    return lines


def summarize_syntax(results: dict[str, dict]) -> tuple[str, str]:
    """Overall status and the "4 files parsed (esbuild 3, json 1); no errors" detail."""
    counts: dict[str, int] = {}
    for result in results.values():
        if result["status"] != "skipped":
            counts[result["checker"]] = counts.get(result["checker"], 0) + 1
    parsed = sum(counts.values())
    skipped = len(results) - parsed
    breakdown = ", ".join(f"{checker} {count}" for checker, count in sorted(counts.items(), key=lambda item: -item[1]))
    statuses = [result["status"] for result in results.values()]
    if parsed == 0:
        return "skipped", f"No syntax checker applies to the {len(results)} changed file(s)."
    status = worst_status(statuses)
    failing = [path for path, result in results.items() if result["status"] == "fail"]
    detail = f"{parsed} file{'s' if parsed != 1 else ''} parsed ({breakdown})"
    if failing:
        first = results[failing[0]]
        messages = syntax_messages(failing[0], first)
        detail += f"; {len(failing)} with syntax errors: " + (messages[0] if messages else failing[0])
    elif status == "warn":
        detail += "; some files already failed to parse before the change, and it adds no new errors"
    else:
        detail += "; no errors"
    if skipped:
        detail += f"; {skipped} not checked"
    return status, detail


# ===========================================================================
# G2: style
# ===========================================================================

_EDITORCONFIG_SECTION = re.compile(r"^\s*\[(.+)\]\s*$")
_EDITORCONFIG_PAIR = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*[=:]\s*(.*?)\s*$")


def parse_editorconfig(text: str) -> list[tuple[str, dict[str, str]]]:
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = _EDITORCONFIG_SECTION.match(line)
        if header:
            current = {}
            sections.append((header.group(1).strip(), current))
            continue
        pair = _EDITORCONFIG_PAIR.match(line)
        if pair and current is not None:
            current[pair.group(1).lower()] = pair.group(2).split(" #")[0].split(" ;")[0].strip().lower()
    return sections


def _editorconfig_glob_regex(glob: str) -> re.Pattern:
    pattern = glob.strip()
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    out, index = [], 0
    while index < len(pattern):
        character = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if character == "*":
            out.append("[^/]*")
        elif character == "?":
            out.append("[^/]")
        elif character == "{":
            end = pattern.find("}", index)
            if end == -1:
                out.append(re.escape(character))
            else:
                options = pattern[index + 1:end].split(",")
                out.append("(?:" + "|".join(_editorconfig_glob_regex(option).pattern[4:-1] if option else "" for option in options) + ")")
                index = end + 1
                continue
        elif character == "[":
            end = pattern.find("]", index)
            if end == -1:
                out.append(re.escape(character))
            else:
                out.append(pattern[index:end + 1].replace("[!", "[^"))
                index = end + 1
                continue
        else:
            out.append(re.escape(character))
        index += 1
    body = "".join(out)
    return re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")


def editorconfig_for(path: str, sections: list[tuple[str, dict[str, str]]] | None) -> dict[str, str]:
    """Merged properties of every matching section (later sections win)."""
    merged: dict[str, str] = {}
    for glob, values in sections or []:
        try:
            if _editorconfig_glob_regex(glob).match(path):
                merged.update(values)
        except re.error:
            continue
    return merged


def _loose_json(text: str) -> Any:
    try:
        return json.loads(_strip_jsonc(str(text or "").lstrip("﻿")))
    except (ValueError, TypeError):
        return None


def build_repo_style(*, editorconfig: str | None = None, package_json: str | dict | None = None,
                     prettier: tuple[str, str] | None = None, pyproject: str | None = None) -> dict:
    """Repository-wide style sources, read once per repository and commit.

    ``prettier`` is ``(file name, text)`` of a .prettierrc file.
    """
    style: dict[str, Any] = {"editorconfig": parse_editorconfig(editorconfig) if editorconfig else [],
                             "prettier": None, "xo": None, "black": False, "ruff": None}
    package = package_json if isinstance(package_json, dict) else (_loose_json(package_json) if package_json else None)
    if isinstance(package, dict):
        if isinstance(package.get("prettier"), dict):
            style["prettier"] = package["prettier"]
        dev = {**(package.get("devDependencies") or {}), **(package.get("dependencies") or {})} if isinstance(package.get("devDependencies") or {}, dict) else {}
        if "xo" in package or "xo" in dev:
            xo = package.get("xo") if isinstance(package.get("xo"), dict) else {}
            style["xo"] = {"space": xo.get("space", False), "semicolon": xo.get("semicolon", True)}
            if xo.get("prettier") and style["prettier"] is None:
                style["prettier"] = {}
    if prettier and prettier[1]:
        name, text = prettier
        data: Any = None
        if name.endswith((".yaml", ".yml")):
            try:
                import yaml

                data = yaml.safe_load(text)
            except Exception:
                data = None
        else:
            data = _loose_json(text)
            if data is None and name.endswith(".prettierrc"):
                try:
                    import yaml

                    data = yaml.safe_load(text)
                except Exception:
                    data = None
        if isinstance(data, dict):
            style["prettier"] = data
    if pyproject:
        try:
            tool = tomllib.loads(pyproject).get("tool") or {}
        except tomllib.TOMLDecodeError:
            tool = {}
        if isinstance(tool.get("black"), dict):
            style["black"] = True
        ruff = tool.get("ruff") if isinstance(tool.get("ruff"), dict) else None
        if ruff is not None:
            ruff_format = ruff.get("format") if isinstance(ruff.get("format"), dict) else {}
            style["ruff"] = {"indent-style": ruff_format.get("indent-style"), "quote-style": ruff_format.get("quote-style"),
                             "indent-width": ruff.get("indent-width")}
    return style


def _string_literals(line: str, extension: str) -> list[tuple[str, int, str]]:
    """``(quote, column, body)`` for the string literals on one line (comments skipped)."""
    found: list[tuple[str, int, str]] = []
    index, length = 0, len(line)
    python = extension in PYTHON_EXTENSIONS
    while index < length:
        character = line[index]
        if python and character == "#":
            break
        if not python and line.startswith("//", index):
            break
        if not python and line.startswith("/*", index):
            end = line.find("*/", index + 2)
            if end == -1:
                break
            index = end + 2
            continue
        if character in ("'", '"', "`"):
            if python and line.startswith(character * 3, index):
                end = line.find(character * 3, index + 3)
                if end == -1:
                    break
                index = end + 3
                continue
            end = index + 1
            while end < length and line[end] != character:
                end += 2 if line[end] == "\\" else 1
            if end >= length:
                break
            if character != "`":
                found.append((character, index, line[index + 1:end]))
            index = end + 1
            continue
        index += 1
    return found


def _infer_style(text: str, extension: str) -> dict:
    lines = text.replace("\r\n", "\n").split("\n")
    indented = [line for line in lines if line[:1] in (" ", "\t") and line.strip() and not line.lstrip().startswith("*")]
    tabs = sum(1 for line in indented if line.startswith("\t"))
    space_counts = [len(line) - len(line.lstrip(" ")) for line in indented if line.startswith(" ")]
    indent = width = None
    if indented:
        if tabs / len(indented) >= 0.8:
            indent = "tab"
        elif len(space_counts) / len(indented) >= 0.8:
            indent = "space"
            even = [count for count in space_counts if count % 2 == 0]
            width = 2 if any(count % 4 for count in even) else 4 if even else None
    quotes = semicolons = None
    if extension in JS_TS_EXTENSIONS or extension in PYTHON_EXTENSIONS:
        single = double = 0
        for line in lines:
            for quote, _column, body in _string_literals(line, extension):
                if quote == "'" and '"' not in body:
                    single += 1
                elif quote == '"' and "'" not in body:
                    double += 1
        total = single + double
        if total >= 3:
            quotes = "single" if single / total >= 0.8 else "double" if double / total >= 0.8 else None
    if extension in JS_TS_EXTENSIONS:
        statements = [line.rstrip() for line in lines
                      if re.match(r"^\s*(?:const|let|var|return|import|export\s+(?:const|let|default)|throw)\b", line)
                      and not line.rstrip().endswith(("{", "(", "[", ",", "=>", "=", "`"))]
        if len(statements) >= 3:
            ending = sum(1 for line in statements if line.endswith(";"))
            ratio = ending / len(statements)
            semicolons = "always" if ratio >= 0.8 else "never" if ratio <= 0.2 else None
    breaks = text.count("\n")
    eol = "crlf" if breaks and text.count("\r\n") * 2 >= breaks else "lf" if breaks else None
    return {"indent": indent, "indent_width": width, "quotes": quotes, "semicolons": semicolons, "eol": eol,
            "final_newline": text.endswith("\n") if text else None}


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 16 else None


def style_profile(path: str, text: str | None, repo_style: dict | None = None, *, exemplar: str | None = None) -> dict:
    """The style a change to ``path`` must follow.

    Precedence per field: explicit configuration (Prettier, xo, black/ruff,
    .editorconfig), then what the file itself does (>= 80 % majority), then
    the exemplar (a sibling file, for new files). ``source`` names what
    decided the indentation.
    """
    repo_style = repo_style or {}
    extension = file_extension(path)
    js_like = extension in JS_TS_EXTENSIONS
    python = extension in PYTHON_EXTENSIONS
    inferred = _infer_style(text or "", extension) if text else {}
    fallback = _infer_style(exemplar, extension) if exemplar else {}

    def infer(key: str) -> Any:
        value = inferred.get(key)
        return value if value is not None else fallback.get(key)

    editorconfig = editorconfig_for(path, repo_style.get("editorconfig"))
    prettier = repo_style.get("prettier") if isinstance(repo_style.get("prettier"), dict) else None
    prettier_applies = prettier is not None and (js_like or extension in {"css", "scss", "less", "json", "md", "mdx", "yml", "yaml", "vue", "html"})
    xo = repo_style.get("xo") if js_like and isinstance(repo_style.get("xo"), dict) else None
    ruff = repo_style.get("ruff") if python and isinstance(repo_style.get("ruff"), dict) else None
    black = python and bool(repo_style.get("black"))

    indent = width = source = None
    if prettier_applies:
        indent = "tab" if prettier.get("useTabs") else "space"
        width = _int_or_none(prettier.get("tabWidth")) or (2 if indent == "space" else None)
        source = "prettier"
    elif xo is not None:
        space = xo.get("space", False)
        if space is True or _int_or_none(space):
            indent, width = "space", _int_or_none(space) if space is not True else 2
        else:
            indent = "tab"
        source = "xo"
    elif editorconfig.get("indent_style") in {"tab", "space"}:
        indent = editorconfig["indent_style"]
        width = _int_or_none(editorconfig.get("indent_size")) or _int_or_none(editorconfig.get("tab_width"))
        source = "editorconfig"
    elif ruff is not None and ruff.get("indent-style") in {"tab", "space"}:
        indent = ruff["indent-style"]
        width = _int_or_none(ruff.get("indent-width")) or 4
        source = "ruff"
    elif black:
        indent, width, source = "space", 4, "black"
    else:
        indent, width = infer("indent"), infer("indent_width")
        source = "inferred"
    if width is None and indent == "space":
        width = infer("indent_width")

    quotes = None
    if js_like:
        if prettier_applies:
            quotes = "single" if prettier.get("singleQuote") else "double"
        elif xo is not None:
            quotes = "single"
        else:
            quotes = infer("quotes")
    elif python:
        if ruff is not None and ruff.get("quote-style") in {"single", "double"}:
            quotes = ruff["quote-style"]
        elif black:
            quotes = "double"
        else:
            quotes = infer("quotes")

    semicolons = None
    if js_like:
        if prettier_applies:
            semicolons = "never" if prettier.get("semi") is False else "always"
        elif xo is not None:
            semicolons = "never" if xo.get("semicolon") is False else "always"
        else:
            semicolons = infer("semicolons")

    eol = editorconfig.get("end_of_line") if editorconfig.get("end_of_line") in {"lf", "crlf"} else (inferred.get("eol") or "lf")
    final = editorconfig.get("insert_final_newline")
    if final in {"true", "false"}:
        final_newline = final == "true"
    elif inferred.get("final_newline") is not None:
        final_newline = bool(inferred["final_newline"])
    else:
        final_newline = True
    trim = editorconfig.get("trim_trailing_whitespace")
    return {
        "indent": indent, "indent_width": width, "quotes": quotes, "semicolons": semicolons, "eol": eol,
        "final_newline": final_newline, "source": source or "inferred",
        # Internal: not part of the published profile.
        "_trim": True if trim == "true" else False if trim == "false" else None,
    }


def public_profile(profile: dict) -> dict:
    return {key: value for key, value in profile.items() if not key.startswith("_")}


def describe_style(profile: dict) -> str:
    """"tabs, single quotes, semicolons, LF" for a FILE header."""
    parts = []
    if profile.get("indent") == "tab":
        parts.append("tabs")
    elif profile.get("indent") == "space":
        parts.append(f"{profile['indent_width']} spaces" if profile.get("indent_width") else "spaces")
    if profile.get("quotes"):
        parts.append(f"{profile['quotes']} quotes")
    if profile.get("semicolons") == "always":
        parts.append("semicolons")
    elif profile.get("semicolons") == "never":
        parts.append("no semicolons")
    parts.append((profile.get("eol") or "lf").upper())
    if profile.get("final_newline") is False:
        parts.append("no final newline")
    return ", ".join(parts)


def added_line_indices(before: str, after: str) -> set[int]:
    """0-based indices of ``after``'s lines that the change added or rewrote."""
    if not before:
        return set(range(len(_diff_lines(after))))
    added: set[int] = set()
    for tag, _i1, _i2, j1, j2 in _line_opcodes(_diff_lines(before), _diff_lines(after)):
        if tag in ("replace", "insert"):
            added.update(range(j1, j2))
    return added


_NEVER_REINDENT = re.compile(r"(?:^|/)(?:makefile|gnumakefile)$|\.(?:mk|md|mdx|markdown|go|tsv)$", re.IGNORECASE)


def _reindent(body: str, profile: dict, width_hint: int) -> str:
    stripped = body.lstrip(" \t")
    leading = body[: len(body) - len(stripped)]
    if not leading or not stripped:
        return body
    if profile.get("indent") == "tab":
        if " " not in leading:
            return body
        width = profile.get("indent_width") or width_hint
        columns = sum(width if character == "\t" else 1 for character in leading)
        return "\t" * (columns // width) + " " * (columns % width) + stripped
    if profile.get("indent") == "space":
        if "\t" not in leading:
            return body
        width = profile.get("indent_width") or width_hint
        columns = 0
        for character in leading:
            columns = (columns // width + 1) * width if character == "\t" else columns + 1
        return " " * columns + stripped
    return body


def autofix_style(path: str, before: str, after: str, profile: dict, *, created: bool) -> tuple[str, list[dict]]:
    """Whitespace-only fixes on added lines; returns ``(after, fixes)``.

    Strips trailing whitespace, converts leading indentation when the
    profile is unambiguous, and adds the final newline. Untouched lines keep
    their exact bytes, CRLF included.
    """
    if not isinstance(after, str) or not after:
        return after, []
    extension = file_extension(path)
    lines = after.split("\n")
    added = added_line_indices("" if created else before, after)
    trim = profile.get("_trim")
    if trim is None:
        trim = extension not in {"md", "mdx", "markdown"}
    reindent = profile.get("indent") in {"tab", "space"} and not _NEVER_REINDENT.search(path)
    space_counts = []
    for index in added:
        if index < len(lines):
            body = lines[index].rstrip("\r")
            lead = len(body) - len(body.lstrip(" "))
            if lead and lead % 2 == 0:
                space_counts.append(lead)
    width_hint = 2 if any(count % 4 for count in space_counts) else 4
    if extension in {"yml", "yaml"}:
        width_hint = 2
    trimmed = reindented = 0
    for index in sorted(added):
        if index >= len(lines):
            continue
        raw = lines[index]
        carriage = raw.endswith("\r")
        body = raw[:-1] if carriage else raw
        new_body = body
        if trim and new_body != new_body.rstrip():
            new_body = new_body.rstrip()
            trimmed += 1
        if reindent:
            converted = _reindent(new_body, profile, width_hint)
            if converted != new_body:
                new_body = converted
                reindented += 1
        if new_body != body:
            lines[index] = new_body + ("\r" if carriage else "")
    fixed = "\n".join(lines)
    fixes: list[dict] = []
    if trimmed:
        fixes.append({"file_path": path, "rule": "trailing_whitespace", "lines": trimmed})
    if reindented:
        fixes.append({"file_path": path, "rule": "indentation", "lines": reindented})
    last_index = len(_diff_lines(fixed)) - 1
    if fixed and not fixed.endswith("\n") and profile.get("final_newline", True) and (created or last_index in added):
        fixed += "\r\n" if "\r\n" in fixed else "\n"
        fixes.append({"file_path": path, "rule": "final_newline", "lines": 1})
    return fixed, fixes


_STATEMENT_START = re.compile(
    r"^\s*(?:const|let|var|return|throw|import|export\s+(?:const|let|var|default|type|\{|\*)|type\s+\w+\s*=|break|continue)\b"
)
_CONTINUATION_START = re.compile(r"^\s*(?:\.|\?\.|\?|:|\+|-|\*|/|&&|\|\||\?\?|,|\)|\]|}|=>|as\b|satisfies\b)")


def style_warnings(path: str, before: str, after: str, profile: dict, *, created: bool) -> list[dict]:
    """Quote style and missing semicolons on added lines (never rewritten)."""
    extension = file_extension(path)
    if extension not in JS_TS_EXTENSIONS and extension not in PYTHON_EXTENSIONS:
        return []
    lines = after.replace("\r\n", "\n").split("\n")
    added = sorted(added_line_indices("" if created else before, after))
    warnings: list[dict] = []
    quotes = profile.get("quotes")
    jsx = extension in {"jsx", "tsx"}
    for index in added:
        if index >= len(lines) or len(warnings) >= MAX_REPORTED_ERRORS:
            continue
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("*", "/*", "//", "#")):
            continue
        if quotes in {"single", "double"}:
            wrong = '"' if quotes == "single" else "'"
            other = "'" if quotes == "single" else '"'
            for quote, column, body in _string_literals(line, extension):
                if quote != wrong or other in body:
                    continue
                if jsx and line[:column].rstrip().endswith("="):
                    continue  # JSX attribute values conventionally use double quotes
                if quote == '"' and body in {"use strict", "use client", "use server"} and quotes == "double":
                    continue
                warnings.append({"file_path": path, "line": index + 1, "column": column + 1,
                                 "message": f"Uses {'double' if wrong == chr(34) else 'single'} quotes; this file uses {quotes} quotes."})
                break
        if extension in JS_TS_EXTENSIONS and profile.get("semicolons") == "always":
            code = re.sub(r"\s//.*$", "", line).rstrip()
            following = next((lines[number] for number in range(index + 1, len(lines)) if lines[number].strip()), "")
            if (_STATEMENT_START.match(code) and re.search(r"[\w)\]'\"`]$", code)
                    and not _CONTINUATION_START.match(following) and code.count("(") == code.count(")")
                    and code.count("{") == code.count("}") and code.count("[") == code.count("]")
                    and code.count("`") % 2 == 0):
                warnings.append({"file_path": path, "line": index + 1, "column": len(code) + 1,
                                 "message": "Missing semicolon; this file ends statements with semicolons."})
    return warnings[:MAX_REPORTED_ERRORS]


# ===========================================================================
# G3: imports
# ===========================================================================


class RepoFiles(Protocol):
    """What the import check may ask about the repository (index-backed)."""

    incomplete: bool

    async def existing(self, paths: list[str]) -> set[str]: ...

    async def read(self, path: str) -> str | None: ...

    async def heads(self, directory: str, extensions: frozenset[str], exclude: str, limit: int) -> dict[str, str]: ...


_JS_IMPORT = re.compile(
    r"""(?P<kind>\bimport\s+(?:type\s+)?(?P<clause>[\w$]+\s*,\s*\{[^}]*\}|[\w$]+\s*,\s*\*\s*as\s+[\w$]+|\{[^}]*\}|\*\s*as\s+[\w$]+|[\w$]+)\s*from\s*"""
    r"""|\bexport\s+(?:type\s+)?(?P<eclause>\*(?:\s*as\s+[\w$]+)?|\{[^}]*\})\s*from\s*"""
    r"""|\bimport\s*(?=['"])|\bimport\s*\(\s*|\brequire\s*\(\s*)"""
    r"""(?P<quote>['"])(?P<spec>[^'"\n]+)(?P=quote)""",
)
_SPEC_EXTENSION = re.compile(r"\.(?:[cm]?[jt]sx?)$")
_ASSET_EXTENSIONS = frozenset({
    "css", "scss", "sass", "less", "styl", "svg", "png", "jpg", "jpeg", "gif", "webp", "avif", "ico", "bmp",
    "woff", "woff2", "ttf", "otf", "eot", "mp3", "mp4", "webm", "wav", "ogg", "wasm", "txt", "html", "md", "mdx",
    "graphql", "gql", "glsl", "vue", "svelte", "yaml", "yml", "csv", "pdf", "node",
})


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _clause_names(clause: str) -> tuple[list[str], bool, bool]:
    """(named imports, has default import, is namespace import)."""
    clause = (clause or "").strip()
    names: list[str] = []
    default = namespace = False
    braces = re.search(r"\{([^}]*)\}", clause)
    head = clause[: braces.start()] if braces else clause
    head = head.strip().rstrip(",").strip()
    if head.startswith("*"):
        namespace = True
    elif head and re.fullmatch(r"[\w$]+", head) and head != "type":
        default = True
    elif "*" in clause and "as" in clause:
        namespace = True
    if braces:
        for part in braces.group(1).split(","):
            part = re.sub(r"^\s*type\s+", "", part).strip()
            if not part:
                continue
            name = part.split(" as ")[0].strip()
            if name == "default":
                default = True
            elif re.fullmatch(r"[\w$]+", name):
                names.append(name)
    return names, default, namespace


def js_imports(text: str) -> list[dict]:
    """Import/export-from/require specifiers with their names and positions."""
    found = []
    for match in _JS_IMPORT.finditer(text or ""):
        clause = match.group("clause") or match.group("eclause") or ""
        names, default, namespace = _clause_names(clause) if clause else ([], False, False)
        if match.group("eclause") and match.group("eclause").startswith("*"):
            names, namespace = [], True
        found.append({
            "spec": match.group("spec"), "names": names, "default": default and bool(match.group("clause")),
            "namespace": namespace, "start": match.start(), "end": match.end(),
            "spec_start": match.start("spec"), "spec_end": match.end("spec"),
            "line": _line_of(text, match.start("spec")), "first_line": _line_of(text, match.start()),
        })
    return found


def js_exports(text: str) -> tuple[set[str], bool]:
    """Names a JS/TS module exports, and whether that set is incomplete ("opaque")."""
    names: set[str] = set()
    opaque = False
    source = text or ""
    for match in re.finditer(
        r"^\s*export\s+(?:declare\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
        r"(?:function\s*\*?|class|const\s+enum|const|let|var|interface|type|enum|namespace|module)\s+([A-Za-z_$][\w$]*)",
        source, re.MULTILINE,
    ):
        names.add(match.group(1))
    if re.search(r"^\s*export\s+default\b", source, re.MULTILINE):
        names.add("default")
    for match in re.finditer(r"\bexport\s+(?:type\s+)?\{([^}]*)\}", source):
        for part in match.group(1).split(","):
            part = re.sub(r"^\s*type\s+", "", part).strip()
            if not part:
                continue
            alias = part.split(" as ")[-1].strip()
            if re.fullmatch(r"[\w$]+", alias):
                names.add(alias)
    for match in re.finditer(r"^\s*export\s+(?:const|let|var)\s+[{\[]([^}\]]*)[}\]]\s*=", source, re.MULTILINE):
        for part in match.group(1).split(","):
            alias = part.split(":")[-1].split("=")[0].strip().lstrip(".")
            if re.fullmatch(r"[\w$]+", alias):
                names.add(alias)
    for match in re.finditer(r"\bexport\s+\*\s*(as\s+([\w$]+)\s*)?from\b", source):
        if match.group(2):
            names.add(match.group(2))
        else:
            opaque = True
    if re.search(r"\bmodule\.exports\b|^\s*exports\.[\w$]+\s*=|^\s*export\s*=", source, re.MULTILINE):
        opaque = True
    return names, opaque


def _relative_candidates(importer: str, spec: str) -> tuple[str | None, list[str]]:
    """(normalised target, candidate repository paths) for a relative specifier."""
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    if target.startswith("..") or target in {".", ""}:
        return None, []
    root, extension = posixpath.splitext(target)
    extension = extension.lower()
    mapping = {".js": [".ts", ".tsx"], ".jsx": [".tsx"], ".mjs": [".mts"], ".cjs": [".cts"]}
    if extension in mapping:
        return target, [target, *(root + item for item in mapping[extension])]
    if extension in {".ts", ".tsx", ".mts", ".cts", ".json"} or extension.lstrip(".") in _ASSET_EXTENSIONS:
        return target, [target]
    suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts", ".json")
    return target, [target, *(target + item for item in suffixes),
                    *(f"{target}/index{item}" for item in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))]


def _runtime_extension(spec: str, resolved: str) -> str:
    """The specifier ESM resolution needs for ``resolved`` ("./is" -> "./is.js" for is.ts)."""
    stem, resolved_extension = posixpath.splitext(resolved)
    suffix = {".ts": ".js", ".tsx": ".js", ".mts": ".mjs", ".cts": ".cjs", ".js": ".js", ".jsx": ".js",
              ".mjs": ".mjs", ".cjs": ".cjs"}.get(resolved_extension.lower(), "")
    if not suffix:
        return spec
    if stem.endswith("/index") or stem == "index":
        if posixpath.basename(spec.rstrip("/")) != "index":
            # The specifier named a directory; ESM needs its index file.
            return spec.rstrip("/") + "/index" + suffix
    return spec + suffix


def _python_top_level(text: str) -> tuple[set[str], bool]:
    """Top-level names a Python module defines (``__all__`` included), and opacity."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return set(), True
    names: set[str] = set()
    opaque = False

    def visit(statements: list[ast.stmt]) -> None:
        nonlocal opaque
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                if node.name == "__getattr__":
                    opaque = True
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for item in ast.walk(target):
                        if isinstance(item, ast.Name):
                            names.add(item.id)
                if any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        names.update(element.value for element in node.value.elts
                                     if isinstance(element, ast.Constant) and isinstance(element.value, str))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        opaque = True
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With)) or type(node).__name__ == "TryStar":
                for field in ("body", "orelse", "finalbody"):
                    visit(list(getattr(node, field, []) or []))
                for handler in getattr(node, "handlers", []) or []:
                    visit(list(handler.body))

    visit(tree.body)
    return names, opaque


def _python_imports(text: str) -> list[dict]:
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append({
                "module": node.module or "", "level": node.level, "names": [alias.name for alias in node.names],
                "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno) or node.lineno,
            })
    return found


def _python_candidates(importer: str, module: str, level: int) -> list[list[str]]:
    """Candidate module files, grouped by root (nearest first)."""
    parts = [part for part in module.split(".") if part]
    if level:
        base = posixpath.dirname(importer)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        stem = posixpath.join(base, *parts) if parts else base
        return [[f"{stem}.py", f"{stem}/__init__.py"] if parts else [f"{stem}/__init__.py"]]
    if not parts:
        return []
    roots, directory = [], posixpath.dirname(importer)
    while directory:
        roots.append(directory)
        directory = posixpath.dirname(directory)
    roots.extend(["", "src"])
    groups = []
    for root in dict.fromkeys(roots):
        stem = posixpath.join(root, *parts) if root else posixpath.join(*parts)
        groups.append([f"{stem}.py", f"{stem}/__init__.py"])
    return groups


async def check_imports(files: list[dict], repo: RepoFiles, *, autofix: bool = True) -> dict:
    """G3 for the proposal's JS/TS and Python files.

    Returns ``{"status", "detail", "files": [{file_path, line, column,
    message}], "autofixed": [...], "after": {path: fixed text},
    "checked": n}``. Only created files and the added lines of modified
    files are checked.
    """
    proposal = {item["file_path"]: item for item in files if isinstance(item.get("after"), str)}
    problems: list[dict] = []
    warnings: list[dict] = []
    autofixed: list[dict] = []
    fixed_after: dict[str, str] = {}
    checked = 0
    extension_conventions: dict[str, tuple[str, int, int]] = {}

    async def exists_many(paths: list[str]) -> set[str]:
        unknown = [path for path in dict.fromkeys(paths) if path not in proposal]
        found = {path for path in paths if path in proposal}
        if unknown:
            found |= await repo.existing(unknown)
        return found

    async def content(path: str) -> str | None:
        if path in fixed_after:
            return fixed_after[path]
        if path in proposal:
            return proposal[path]["after"]
        return await repo.read(path)

    async def convention(item: dict) -> tuple[str, int, int]:
        directory = posixpath.dirname(item["file_path"])
        if directory in extension_conventions:
            return extension_conventions[directory]
        specs = [ref["spec"] for ref in js_imports(item.get("before") or "") if ref["spec"].startswith(".")]
        heads = await repo.heads(directory, JS_TS_EXTENSIONS, item["file_path"], 8)
        for text in heads.values():
            specs.extend(ref["spec"] for ref in js_imports(text) if ref["spec"].startswith("."))
        code_specs = [spec for spec in specs if posixpath.splitext(spec)[1].lstrip(".").lower() not in _ASSET_EXTENSIONS
                      and not spec.endswith(".json")]
        explicit = sum(1 for spec in code_specs if _SPEC_EXTENSION.search(spec))
        total = len(code_specs)
        if total < 5:
            result = ("unknown", explicit, total)
        elif explicit / total >= 0.8:
            result = ("explicit", explicit, total)
        elif explicit / total <= 0.2:
            result = ("none", explicit, total)
        else:
            result = ("split", explicit, total)
        extension_conventions[directory] = result
        return result

    for item in files:
        path = item["file_path"]
        after = item.get("after")
        if not isinstance(after, str):
            continue
        extension = file_extension(path)
        created = item.get("action") == "create"
        added = {index + 1 for index in added_line_indices("" if created else item.get("before") or "", after)}
        if extension in JS_TS_EXTENSIONS:
            refs = [ref for ref in js_imports(after) if ref["spec"].startswith(".")
                    and any(line in added for line in range(ref["first_line"], ref["line"] + 1))]
            if not refs:
                continue
            checked += 1
            plans = [(ref, *_relative_candidates(path, ref["spec"])) for ref in refs]
            existing = await exists_many([candidate for _ref, _target, candidates in plans for candidate in candidates])
            # Baseline: a specifier the file already imported before the
            # change keeps whatever resolution it had (generated files,
            # aliases, files outside the index); only new ones must resolve.
            before_specs = set() if created else {ref["spec"] for ref in js_imports(item.get("before") or "")}
            replacements: list[tuple[int, int, str]] = []
            for ref, target, candidates in plans:
                resolved = next((candidate for candidate in candidates if candidate in existing), None)
                spec_extension = posixpath.splitext(ref["spec"])[1].lstrip(".").lower()
                if resolved is None:
                    if target is None or spec_extension in _ASSET_EXTENSIONS or ref["spec"] in before_specs:
                        continue  # outside the repository, an asset the index does not hold, or pre-existing
                    entry = {"file_path": path, "line": ref["line"], "column": None,
                             "message": f"'{ref['spec']}' does not resolve to a file in this repository"}
                    (warnings if repo.incomplete else problems).append(entry)
                    continue
                if ref["names"] or ref["default"]:
                    target_text = await content(resolved)
                    if target_text is not None and file_extension(resolved) in JS_TS_EXTENSIONS:
                        names, opaque = js_exports(target_text)
                        if not opaque:
                            missing = [name for name in ref["names"] if name not in names]
                            if ref["default"] and "default" not in names:
                                missing.append("default")
                            for name in missing:
                                problems.append({"file_path": path, "line": ref["line"], "column": None,
                                                 "message": f"'{name}' is not exported by {resolved}"})
                if not _SPEC_EXTENSION.search(ref["spec"]) and file_extension(resolved) in JS_TS_EXTENSIONS:
                    style, explicit, total = await convention(item)
                    if style == "explicit" and autofix:
                        replacements.append((ref["spec_start"], ref["spec_end"], _runtime_extension(ref["spec"], resolved)))
                    elif style == "split":
                        warnings.append({"file_path": path, "line": ref["line"], "column": None, "message": (
                            f"'{ref['spec']}' has no file extension; {explicit} of {total} relative imports nearby use one"
                        )})
            if replacements:
                text = after
                for start, end, replacement in sorted(replacements, reverse=True):
                    text = text[:start] + replacement + text[end:]
                fixed_after[path] = text
                autofixed.append({"file_path": path, "rule": "import_extension", "lines": len(replacements)})
        elif extension in PYTHON_EXTENSIONS:
            refs = [ref for ref in _python_imports(after) if any(line in added for line in range(ref["line"], ref["end_line"] + 1))]
            if not refs:
                continue
            checked += 1
            for ref in refs:
                dots = "." * ref["level"]
                names_wanted = [name for name in ref["names"] if name != "*"]
                if ref["level"] and not ref["module"]:
                    # "from . import name": a sibling module or a name in __init__.py.
                    base = posixpath.dirname(path)
                    for _ in range(ref["level"] - 1):
                        base = posixpath.dirname(base)
                    prefix = f"{base}/" if base else ""
                    init = f"{prefix}__init__.py"
                    wanted = [init, *(f"{prefix}{name}.py" for name in names_wanted),
                              *(f"{prefix}{name}/__init__.py" for name in names_wanted)]
                    present = await exists_many(wanted)
                    init_names, opaque = _python_top_level(await content(init) or "") if init in present else (set(), False)
                    for name in names_wanted:
                        if opaque or name in init_names or f"{prefix}{name}.py" in present or f"{prefix}{name}/__init__.py" in present:
                            continue
                        problems.append({"file_path": path, "line": ref["line"], "column": None,
                                         "message": f"'{name}' is neither a module nor a name defined in package '{base or '.'}'"})
                    continue
                groups = _python_candidates(path, ref["module"], ref["level"])
                flat = [candidate for group in groups for candidate in group]
                existing = await exists_many(flat) if flat else set()
                module_file = next((candidate for group in groups for candidate in group if candidate in existing), None)
                if module_file is None:
                    if ref["level"]:
                        problems.append({"file_path": path, "line": ref["line"], "column": None,
                                         "message": f"'{dots}{ref['module']}' does not resolve to a module in this repository"})
                    continue  # absolute imports of modules outside the index are third-party
                names, opaque = _python_top_level(await content(module_file) or "")
                if opaque:
                    continue
                package_dir = posixpath.dirname(module_file) if module_file.endswith("/__init__.py") else None
                submodules: set[str] = set()
                if package_dir is not None and names_wanted:
                    wanted = [f"{package_dir}/{name}.py" for name in names_wanted] + [f"{package_dir}/{name}/__init__.py" for name in names_wanted]
                    present = await exists_many(wanted)
                    submodules = {posixpath.basename(item_path).removesuffix(".py") if not item_path.endswith("/__init__.py")
                                  else posixpath.basename(posixpath.dirname(item_path)) for item_path in present}
                for name in names_wanted:
                    if name in names or name in submodules:
                        continue
                    problems.append({"file_path": path, "line": ref["line"], "column": None,
                                     "message": f"'{name}' is not defined in {module_file}"})
    status = "fail" if problems else "warn" if warnings else ("pass" if checked else "skipped")
    if problems:
        detail = f"{len(problems)} import problem(s): " + "; ".join(f"{item['file_path']}:{item['line']} {item['message']}" for item in problems[:3])
    elif warnings:
        detail = "; ".join(f"{item['file_path']}:{item['line']} {item['message']}" for item in warnings[:3])
    elif checked:
        detail = "All added relative imports resolve and every imported name exists"
    else:
        detail = "No relative imports were added"
    fixes = sum(item["lines"] for item in autofixed)
    if fixes:
        detail += f"; added the file extension to {fixes} import{'s' if fixes != 1 else ''}"
    return {"status": status, "detail": detail, "files": (problems + warnings)[:MAX_REPORTED_ERRORS],
            "problems": problems[:MAX_REPORTED_ERRORS], "autofixed": autofixed, "after": fixed_after, "checked": checked}


def import_messages(result: dict) -> list[str]:
    """``path:line message`` for every failing import (for the repair request)."""
    return [f"{item['file_path']}:{item['line']} {item['message']}" for item in result.get("problems") or []]


# ===========================================================================
# G4: changed literals and stale mentions
# ===========================================================================

_TOKEN = re.compile(r"[A-Za-z_$][\w$]*|\d[\w.]*|\S")
_LITERAL = re.compile(r"(?:\d[\d_]*(?:\.\d+)?|true|false|True|False)$")
_KEYWORDS = frozenset("const let var return export default function class def if else for while new await async static public private readonly".split())
_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var|class|function|def|async\s+def|interface|type)\s+([A-Za-z_$][\w$]*)"
    r"|^([A-Za-z_][\w]*)\s*(?::[^=]+)?=\s*[{\[(]"
)


def _words(identifier: str) -> list[str]:
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", identifier or "")
    return [part.lower() for part in parts if len(part) > 3][:4]


def changed_literals(before: str, after: str) -> list[dict]:
    """Lines where exactly one token changed and it is a number or boolean next to a key.

    ``limit: 2`` -> ``limit: 3`` gives ``{key: "limit", old: "2", new: "3",
    words: ["default", "retry", "options"], line}`` where ``words`` come from
    the enclosing declaration (``defaultRetryOptions``).
    """
    if not before or not after:
        return []
    old_lines = before.replace("\r\n", "\n").split("\n")
    new_lines = after.replace("\r\n", "\n").split("\n")
    found: list[dict] = []
    for tag, i1, i2, j1, j2 in _line_opcodes(_diff_lines(before), _diff_lines(after)):
        if tag != "replace" or i2 - i1 != j2 - j1:
            continue
        for offset in range(i2 - i1):
            old_line, new_line = old_lines[i1 + offset], new_lines[j1 + offset]
            old_tokens, new_tokens = _TOKEN.findall(old_line), _TOKEN.findall(new_line)
            if len(old_tokens) != len(new_tokens):
                continue
            changed = [index for index, (old, new) in enumerate(zip(old_tokens, new_tokens)) if old != new]
            if len(changed) != 1:
                continue
            index = changed[0]
            old, new = old_tokens[index], new_tokens[index]
            if not _LITERAL.match(old) or not _LITERAL.match(new):
                continue
            key = None
            for back in range(index - 1, -1, -1):
                token = old_tokens[back]
                if token in {":", "=", "?", "!"}:
                    continue
                if re.fullmatch(r"[A-Za-z_$][\w$]{1,}", token) and token not in _KEYWORDS:
                    key = token
                break
            if not key:
                continue
            enclosing = ""
            for line in reversed(old_lines[: i1 + offset + 1]):
                declaration = _DECLARATION.match(line)
                if declaration:
                    enclosing = declaration.group(1) or declaration.group(2) or ""
                    break
            words = _words(enclosing) or _words(key)
            found.append({"key": key, "old": old, "new": new, "words": words, "line": j1 + offset + 1,
                          "enclosing": enclosing})
    return found[:3]


def stale_line_matcher(literal: dict) -> Callable[[str, list[str], int], bool]:
    """A predicate ``(path, lines, index) -> bool`` for lines that still state the old value."""
    key, old = literal["key"], literal["old"]
    words = [word for word in literal.get("words") or [] if word]
    value = re.escape(old) + r"(?![\w]|\.\d)"
    near = re.compile(rf"(?<![\w$]){re.escape(key)}(?![\w$])\W{{1,12}}{value}")
    default_tag = re.compile(rf"@default\s+`?{value}")
    key_declaration = re.compile(rf"(?<![\w$]){re.escape(key)}\??\s*:")
    holder = re.compile(rf"(?<![\w$])([A-Za-z_$][\w$]*)\s*[=:]\s*{value}")

    def matches(path: str, lines: list[str], index: int) -> bool:
        text = lines[index]
        if is_test_path(path):
            # Tests set explicit values constantly (``retry: {limit: 2}``);
            # only code with a name that encodes the old default counts there
            # (``defaultRetryCount = 2`` for ``defaultRetryOptions``).
            if text.lstrip().startswith(("//", "*", "/*", "#")):
                return False
            for found in holder.finditer(text):
                name = found.group(1).lower()
                if len(words) >= 2 and sum(1 for word in words if word in name) >= 2:
                    return True
            return False
        # Docs and source: the key right next to the old literal, or a
        # ``@default <old>`` tag documenting the key below it.
        if near.search(text):
            return True
        if default_tag.search(text):
            window = "\n".join(lines[index:index + 4])
            return bool(key_declaration.search(window))
        return False

    return matches


def fts_literal_queries(literal: dict) -> list[tuple[str, bool]]:
    """FTS5 queries ``(expression, tests_only)`` for chunks that may hold a stale mention.

    Docs and source: the key directly next to the old literal, or an
    ``@default <old>`` tag. Tests: a name starting with one of the enclosing
    declaration's words next to the old literal. NEAR keeps the candidate
    set small on big repositories, where key and literal are both common.
    """
    key = re.sub(r"[^0-9A-Za-z_]", "", literal["key"]).lower()
    value = '"' + (re.sub(r"[^0-9A-Za-z._]", "", literal["old"]).lower() or literal["old"]) + '"'
    queries = [(f'NEAR("{key}" {value}, 1) OR NEAR("default" {value}, 1)', False)] if key else []
    words = [re.sub(r"[^0-9a-z]", "", word) for word in (literal.get("words") or [])][:3]
    words = [word for word in words if word]
    if len(words) >= 2:
        queries.append((" OR ".join(f'NEAR("{word}"* {value}, 3)' for word in words), True))
    return queries


# ---------------------------------------------------------------------------
# G4: changed string literals (error messages, user-facing text)
# ---------------------------------------------------------------------------

_INTERPOLATION = re.compile(r"\$\{[^}]*\}|\{[A-Za-z_][\w.]*\}|%[sdrif]|%\([A-Za-z_]\w*\)[sdrif]")
_STRING_FIXTURE_PATH = re.compile(r"(?:^|/)(?:fixtures?|__fixtures__|__snapshots__|snapshots?|testdata|test-data|golden)(?:/|$)|\.snap$",
                                  re.IGNORECASE)
# A changed literal is traced only when it is distinctive: at least this many
# characters in one fixed fragment, and at least three words.
STRING_NEEDLE_MIN_CHARACTERS = 16
STRING_NEEDLE_MIN_WORDS = 3


def _all_string_literals(line: str, extension: str) -> list[tuple[str, int, str]]:
    """``(quote, column, body)`` of every one-line string literal, template literals included."""
    found: list[tuple[str, int, str]] = []
    index, length = 0, len(line)
    python = extension in PYTHON_EXTENSIONS
    while index < length:
        character = line[index]
        if python and character == "#":
            break
        if not python and line.startswith("//", index):
            break
        if character in ("'", '"', "`"):
            end = index + 1
            while end < length and line[end] != character:
                end += 2 if line[end] == "\\" else 1
            if end >= length:
                break
            found.append((character, index, line[index + 1:end]))
            index = end + 1
            continue
        index += 1
    return found


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def literal_fragments(body: str) -> list[str]:
    """The fixed text of a literal: split at interpolations (``${x}``, ``{x}``, ``%s``), escapes undone."""
    return [_unescape(part) for part in _INTERPOLATION.split(body) if part]


def is_string_fixture_path(path: str) -> bool:
    """Tests, docs and fixtures: where an old message or label is asserted or quoted."""
    return is_test_path(path) or is_doc_path(path) or bool(_STRING_FIXTURE_PATH.search(path))


def changed_string_literals(before: str, after: str, path: str) -> list[dict]:
    """String literals a change rewrote, as ``{needle, old, new, line}``.

    ``needle`` is the longest fixed fragment of the old literal that the new
    literal no longer contains (interpolations removed, escapes undone), so
    "The `${name}` option must be a non-negative number or `false`" gives
    "` option must be a non-negative number or `false`", which also matches
    tests that assert the message with ``timeout`` filled in. Short or
    one-word strings (keys, enum values, paths) are ignored.
    """
    if not before or not after:
        return []
    extension = file_extension(path)
    old_lines = before.replace("\r\n", "\n").split("\n")
    new_lines = after.replace("\r\n", "\n").split("\n")
    found: list[dict] = []
    seen: set[str] = set()
    for tag, i1, i2, j1, j2 in _line_opcodes(_diff_lines(before), _diff_lines(after)):
        if tag != "replace":
            continue
        new_block = new_lines[j1:j2]
        new_bodies = [body for line in new_block for _quote, _column, body in _all_string_literals(line, extension)]
        new_text = "\n".join(_unescape(body) for body in new_bodies)
        for offset, old_line in enumerate(old_lines[i1:i2]):
            for _quote, _column, body in _all_string_literals(old_line, extension):
                if body in new_bodies:
                    continue
                fragments = [fragment for fragment in literal_fragments(body) if fragment.strip() and fragment not in new_text]
                if not fragments:
                    continue
                needle = max(fragments, key=len).strip(" ")
                if len(needle) < STRING_NEEDLE_MIN_CHARACTERS or len(re.findall(r"[A-Za-z]{2,}", needle)) < STRING_NEEDLE_MIN_WORDS:
                    continue
                if needle in seen:
                    continue
                seen.add(needle)
                replacement = max(new_bodies, key=lambda candidate: difflib.SequenceMatcher(None, candidate, body).ratio(),
                                  default="")
                found.append({"needle": needle, "old": _unescape(body), "new": _unescape(replacement),
                              "line": j1 + min(offset, max(0, j2 - j1 - 1)) + 1})
    return found[:3]


def string_line_matcher(needle: str) -> Callable[[str], bool]:
    """A predicate for lines that still contain the old text (escapes ignored on both sides)."""
    target = needle.replace("\\", "")

    def matches(line: str) -> bool:
        return target in line.replace("\\", "")

    return matches


def fts_phrase(text: str) -> str:
    """An FTS5 phrase query for the words of ``text`` (unicode61 tokens), or ""."""
    tokens = re.findall(r"[0-9A-Za-z]+", text.lower())
    return '"' + " ".join(tokens[:24]) + '"' if len(tokens) >= 2 else ""
