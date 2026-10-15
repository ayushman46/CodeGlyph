"""Small, conservative source dependency extraction for impact-oriented search.

This is intentionally not an AST or build-system replacement. It records only
imports/includes that can be resolved to a file present in the same checkout;
external packages and ambiguous aliases are left out rather than presented as
facts.

JavaScript/TypeScript gets the most coverage because monorepos lean on it:
``import``/``export ... from``, side-effect imports, dynamic ``import()``,
``require``, the closing line of a multi-line import, workspace packages (the
``name`` of every ``package.json``) and ``tsconfig``/``jsconfig`` ``paths``
aliases are all resolved.
"""

import hashlib
import json
import os
import posixpath
import re
from bisect import bisect_left

_PY_FROM = re.compile(r"^\s*from\s+([.A-Za-z_][\w.]*(?:/[^\s]+)?)(?:\s+import\s+.+)?", re.MULTILINE)
_PY_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", re.MULTILINE)
_JS_IMPORT_FROM = re.compile(r"^\s*import\s+.*?\s*from\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_JS_SIDE_EFFECT = re.compile(r"^\s*import\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_JS_EXPORT_FROM = re.compile(r"^\s*export\s+.*?\bfrom\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
# The last line of a multi-line ``import {\n a,\n} from "./x"`` or
# ``export {\n a,\n} from "./x"`` statement.
_JS_CLOSING_FROM = re.compile(r"^\s*\}\s*from\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_JS_DYNAMIC = re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)", re.MULTILINE)
_REQUIRE = re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", re.MULTILINE)
_INCLUDE = re.compile(r"^\s*(?:#\s*include|include)\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE)
_RUST_USE = re.compile(r"^\s*use\s+(?:crate::)?([A-Za-z_][\w:]*)", re.MULTILINE)

# Kept for callers that iterate the generic pattern list.
IMPORT_PATTERNS = (_PY_FROM, _PY_IMPORT, _JS_IMPORT_FROM, _REQUIRE, _INCLUDE, _RUST_USE)
_JS_PATTERNS = (_JS_IMPORT_FROM, _JS_SIDE_EFFECT, _JS_EXPORT_FROM, _JS_CLOSING_FROM, _JS_DYNAMIC, _REQUIRE)
_PY_PATTERNS = (_PY_FROM, _PY_IMPORT)

SOURCE_EXTENSIONS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".kts", ".scala", ".vue", ".svelte",
)
JS_LIKE_EXTENSIONS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro", ".mdx",
})
_JS_RUNTIME_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
_TS_FOR_JS = (".ts", ".tsx", ".mts", ".cts")
# Documentation and data never import anything; scanning them only produced
# false edges from code samples.
_NON_CODE_EXTENSIONS = frozenset({
    ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".lock",
    ".svg", ".xml", ".properties", ".env", ".example", ".sample", ".template",
})
_PACKAGE_JSON_MAX_BYTES = 512 * 1024
_ENTRY_FIELDS = ("source", "module", "main", "types", "typings")


def _normalise(path: str) -> str:
    """Repository-relative POSIX path; keeps leading dots of real names.

    ``lstrip("./")`` used to strip every leading ``.`` and ``/``, which turned
    ``.github/workflows/ci.yml`` into ``github/workflows/ci.yml`` and
    ``.env.example`` into ``env.example``.
    """
    normalised = path.replace(os.sep, "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _probe(base: str, known_files: set[str]) -> str | None:
    """Return the first existing file for an import base path.

    Extensions are always probed, even when the base already has a suffix:
    ``./app.controller`` refers to ``app.controller.ts``.
    """
    base = _normalise(posixpath.normpath(base)) if base else base
    if not base or base == "." or base.startswith("../"):
        return None
    candidates = [base]
    stem, suffix = posixpath.splitext(base)
    if suffix in _JS_RUNTIME_SUFFIXES:
        # TypeScript ESM imports name the emitted ``.js`` file.
        candidates.extend(stem + extension for extension in _TS_FOR_JS)
    candidates.extend(base + extension for extension in SOURCE_EXTENSIONS)
    candidates.extend(posixpath.join(base, "index" + extension) for extension in SOURCE_EXTENSIONS)
    for candidate in candidates:
        if candidate in known_files:
            return candidate
    return None


class _JsResolver:
    """Workspace package names and tsconfig ``paths`` aliases."""

    def __init__(self) -> None:
        self.packages: dict[str, tuple[str, list[str]]] = {}
        # tsconfig directory -> [(prefix, suffix, wildcard, [target templates])]
        self.aliases: dict[str, list[tuple[str, str, bool, list[str]]]] = {}

    def __bool__(self) -> bool:
        return bool(self.packages or self.aliases)

    def add_package(self, rel_path: str, data: dict) -> None:
        name = data.get("name")
        if not isinstance(name, str) or not name or len(name) > 214 or name in self.packages:
            return
        directory = posixpath.dirname(rel_path)
        entries = [str(data[field]) for field in _ENTRY_FIELDS if isinstance(data.get(field), str)]
        self.packages[name] = (directory, entries)

    def add_tsconfig(self, rel_path: str, data: dict) -> None:
        options = data.get("compilerOptions")
        if not isinstance(options, dict) or not isinstance(options.get("paths"), dict):
            return
        directory = posixpath.dirname(rel_path)
        base_url = options.get("baseUrl") if isinstance(options.get("baseUrl"), str) else "."
        root = posixpath.normpath(posixpath.join(directory, base_url)) if directory else posixpath.normpath(base_url)
        rules = []
        for pattern, targets in options["paths"].items():
            if not isinstance(pattern, str) or not isinstance(targets, list) or pattern.count("*") > 1:
                continue
            prefix, star, suffix = pattern.partition("*")
            templates = [
                posixpath.join(root, target) for target in targets
                if isinstance(target, str) and target.count("*") <= 1
            ]
            if templates:
                rules.append((prefix, suffix, bool(star), templates))
        if rules:
            # Longest (most specific) prefix first, like TypeScript.
            rules.sort(key=lambda rule: -len(rule[0]))
            self.aliases[directory] = rules

    def resolve(self, specifier: str, source_path: str, known_files: set[str]) -> str | None:
        resolved = self._resolve_alias(specifier, source_path, known_files)
        if resolved:
            return resolved
        return self._resolve_package(specifier, known_files)

    def _resolve_alias(self, specifier: str, source_path: str, known_files: set[str]) -> str | None:
        if not self.aliases:
            return None
        directory = posixpath.dirname(source_path)
        while True:
            rules = self.aliases.get(directory)
            if rules:
                for prefix, suffix, wildcard, templates in rules:
                    if wildcard:
                        if not (specifier.startswith(prefix) and specifier.endswith(suffix)
                                and len(specifier) >= len(prefix) + len(suffix)):
                            continue
                        captured = specifier[len(prefix):len(specifier) - len(suffix) if suffix else None]
                    elif specifier != prefix:
                        continue
                    else:
                        captured = ""
                    for template in templates:
                        resolved = _probe(template.replace("*", captured), known_files)
                        if resolved:
                            return resolved
                # The nearest tsconfig with ``paths`` decides, as in tsc.
                return None
            if not directory:
                return None
            directory = posixpath.dirname(directory)

    def _resolve_package(self, specifier: str, known_files: set[str]) -> str | None:
        if not self.packages:
            return None
        name = specifier
        rest = ""
        while name:
            if name in self.packages:
                break
            if "/" not in name:
                return None
            name, _, tail = name.rpartition("/")
            rest = f"{tail}/{rest}" if rest else tail
        else:
            return None
        directory, entries = self.packages[name]
        join = (lambda *parts: posixpath.join(directory, *parts)) if directory else posixpath.join
        if rest:
            for base in (join(rest), join("src", rest)):
                resolved = _probe(base, known_files)
                if resolved:
                    return resolved
            return None
        for entry in entries:
            resolved = _probe(join(entry), known_files)
            if resolved:
                return resolved
        for base in (join("index"), join("src", "index")):
            resolved = _probe(base, known_files)
            if resolved:
                return resolved
        return None


def _strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas (tsconfig is JSONC)."""
    output: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        character = text[index]
        if in_string:
            output.append(character)
            if character == "\\" and index + 1 < length:
                output.append(text[index + 1])
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        output.append(character)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(output))


def _read_json(path: str, *, lenient: bool = False) -> dict | None:
    try:
        if os.path.getsize(path) > _PACKAGE_JSON_MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read()
        data = json.loads(_strip_json_comments(text) if lenient else text)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_js_resolver(files: list[str], repo_path: str) -> _JsResolver:
    resolver = _JsResolver()
    for file_path in files:
        name = os.path.basename(file_path).lower()
        if name == "package.json":
            data = _read_json(file_path)
            if data:
                resolver.add_package(_normalise(os.path.relpath(file_path, repo_path)), data)
        elif name in {"tsconfig.json", "jsconfig.json", "tsconfig.base.json"}:
            data = _read_json(file_path, lenient=True)
            if data:
                resolver.add_tsconfig(_normalise(os.path.relpath(file_path, repo_path)), data)
    return resolver


def _resolve_import(raw_target: str, source_path: str, known_files: set[str], resolver: _JsResolver | None = None) -> str | None:
    target = raw_target.strip().split("?", 1)[0].split("#", 1)[0]
    if not target or target.startswith(("http://", "https://", "data:", "node:")):
        return None
    source_dir = posixpath.dirname(source_path)
    if target.startswith("."):
        dot_count = len(target) - len(target.lstrip("."))
        relative_target = target[dot_count:].lstrip("/")
        if target.startswith(("./", "../")) or target in {".", ".."}:
            # Path-style (JavaScript, C) relative import.
            base = posixpath.normpath(posixpath.join(source_dir, target))
        else:
            # Python relative import: one dot is the current package.
            base_dir = source_dir
            for _ in range(max(0, dot_count - 1)):
                base_dir = posixpath.dirname(base_dir)
            base = posixpath.normpath(posixpath.join(base_dir, relative_target.replace(".", "/")))
        return _probe(base, known_files)
    if resolver:
        resolved = resolver.resolve(target, source_path, known_files)
        if resolved:
            return resolved
    if target.startswith("@"):
        return None
    if "::" in target:
        base = target.split("::", 1)[0].replace(".", "/")
    elif "." in target and "/" not in target:
        base = target.replace(".", "/")
    elif "/" in target:
        base = posixpath.normpath(target)
    else:
        return None
    return _probe(base, known_files)


def _patterns_for(source_path: str):
    extension = posixpath.splitext(source_path)[1].lower()
    if extension in JS_LIKE_EXTENSIONS:
        return _JS_PATTERNS, True
    if extension in {".py", ".pyi"}:
        return _PY_PATTERNS, False
    if extension in _NON_CODE_EXTENSIONS or posixpath.basename(source_path).startswith(".env"):
        return (), False
    return IMPORT_PATTERNS, False


def _record(edges: list[dict], seen: set, source_path: str, target_path: str | None, import_name: str, line_number: int) -> None:
    if not target_path or target_path == source_path:
        return
    key = (source_path, target_path, line_number)
    if key in seen:
        return
    seen.add(key)
    edges.append({
        "source_file": source_path,
        "target_file": target_path,
        "import_name": import_name,
        "line_number": line_number,
    })


def _scan_dependency_edges(content: str, source_path: str, known_files: set[str], edges: list[dict], seen: set[tuple[str, str, int]], resolver: _JsResolver | None = None) -> None:
    """Append resolved imports for one already-read source file."""
    newline_offsets = [index for index, character in enumerate(content) if character == "\n"]
    patterns, use_resolver = _patterns_for(source_path)
    for pattern in patterns:
        for match in pattern.finditer(content):
            target_path = _resolve_import(match.group(1), source_path, known_files, resolver if use_resolver else None)
            line_number = bisect_left(newline_offsets, match.start()) + 1
            _record(edges, seen, source_path, target_path, match.group(1), line_number)


def _scan_dependency_edges_lines(lines, source_path: str, known_files: set[str], edges: list[dict], seen: set[tuple[str, str, int]], resolver: _JsResolver | None = None) -> None:
    """Scan a text iterator without materialising a large source file.

    Matching stays line-oriented to keep memory flat for 50 MB files; the
    closing ``} from "x"`` line of a multi-line import carries the specifier,
    so multi-line statements are still resolved (at that line).
    """
    patterns, use_resolver = _patterns_for(source_path)
    if not patterns:
        return
    active_resolver = resolver if use_resolver else None
    for line_number, line in enumerate(lines, 1):
        if len(line) > 20_000:
            # Minified/generated content; imports are never this long.
            continue
        for pattern in patterns:
            for match in pattern.finditer(line):
                target_path = _resolve_import(match.group(1), source_path, known_files, active_resolver)
                _record(edges, seen, source_path, target_path, match.group(1), line_number)


def build_manifest_and_dependency_manifest(files: list[str], repo_path: str) -> tuple[dict[str, dict[str, int | str]], list[dict]]:
    """Hash files and resolve local imports with bounded memory.

    Hashing uses raw bytes so incremental re-indexing remains exact. A second
    text pass extracts local imports one line at a time; this avoids keeping a
    50 MB decoded string, ``splitlines`` list, and newline-offset array alive
    during the manifest stage. Manifest keys are exactly the repository
    relative paths the pipeline uses (``.github/...`` keeps its dot).
    """
    known_files = {_normalise(os.path.relpath(path, repo_path)) for path in files}
    resolver = build_js_resolver(files, repo_path)
    manifest: dict[str, dict[str, int | str]] = {}
    edges: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for file_path in files:
        source_path = _normalise(os.path.relpath(file_path, repo_path))
        try:
            digest = hashlib.sha256()
            with open(file_path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                _scan_dependency_edges_lines(handle, source_path, known_files, edges, seen, resolver)
        except OSError:
            continue
        manifest[source_path] = {"content_hash": digest.hexdigest(), "byte_size": os.path.getsize(file_path)}
    return manifest, sorted(edges, key=lambda edge: (edge["source_file"], edge["line_number"], edge["target_file"]))


def build_dependency_manifest(files: list[str], repo_path: str) -> list[dict]:
    """Build a dependency graph for callers that do not need file hashes."""
    _manifest, dependencies = build_manifest_and_dependency_manifest(files, repo_path)
    return dependencies
