"""Small, conservative source dependency extraction for impact-oriented search.

This is intentionally not an AST or build-system replacement. It records only
imports/includes that can be resolved to a file present in the same checkout;
external packages and ambiguous aliases are left out rather than presented as
facts.

JavaScript/TypeScript gets the most coverage because monorepos lean on it:
``import``/``export ... from``, side-effect imports, dynamic ``import()``,
``require``, the closing line of a multi-line import, workspace packages (the
``name`` of every ``package.json``) and ``tsconfig``/``jsconfig`` ``paths``
aliases are all resolved, and so is a package importing itself through its
directory (``require('../')`` in a test resolves to the package entry).

Python imports are resolved the way the interpreter finds them: from the
directories that hold top-level packages (``src/`` layouts included), from
the script's own directory when it is not inside a package (how pytest runs
``tests/``), and relative to the current package. ``import flask`` in
``tests/test_basic.py`` therefore resolves to ``src/flask/__init__.py``. A
package outside an installable project (``docs/examples/logging``) is only
seen from its own tree, so it never stands in for the standard library.

The graph can be built from a checkout (``build_manifest_and_dependency_manifest``)
or from file text already in the index (``DependencyScanner``), so an index
whose graph predates a resolver change can be upgraded without a download.
Every stored graph ends with a version row (``graph_version_edge``).
"""

import hashlib
import json
import os
import posixpath
import re
import sys
from collections.abc import Iterable

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
# A published entry such as ``./dist/index.js`` is usually not in the
# repository; the same path under a source folder usually is.
_BUILD_DIRS = ("dist", "distribution", "build", "lib", "out", "esm", "cjs")
_SOURCE_DIRS = ("src", "source")
JS_CONFIG_NAMES = frozenset({"package.json", "tsconfig.json", "jsconfig.json", "tsconfig.base.json"})

_PY_IMPORT_LINE = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*(?:\s+as\s+\w+)?(?:\s*,\s*[A-Za-z_][\w.]*(?:\s+as\s+\w+)?)*)\s*(?:#.*)?$")
_PY_FROM_LINE = re.compile(r"^\s*from\s+(\.+|\.*[A-Za-z_][\w.]*)\s+import\s+(.*)$")
_PY_NAME = re.compile(r"^[A-Za-z_]\w*$")
_PY_MODULE_SUFFIXES = (".py", ".pyi", "/__init__.py", "/__init__.pyi")
# A directory holding one of these is an installable project: its packages
# are importable from anywhere once installed (monorepos, ``examples/x``).
_PY_PROJECT_FILES = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})
# Source folders a project keeps its packages in (``src/flask``, ``lib/ansible``).
_PY_SOURCE_DIRS = frozenset({"src", "lib"})
_PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))

# The graph is replaced as a whole, and its last row records the resolver
# version that built it. Raise the version whenever resolution changes so
# existing indexes are rebuilt (from the indexed text) on next use. The row
# uses empty paths, which never match a real file in any graph query.
GRAPH_VERSION = 3
_GRAPH_VERSION_PREFIX = "graph-version:"


def graph_version_edge() -> dict:
    """The marker row written after every edge of a complete graph."""
    return {
        "source_file": "", "target_file": "",
        "import_name": f"{_GRAPH_VERSION_PREFIX}{GRAPH_VERSION}", "line_number": 0,
    }


def graph_version_from_marker(import_name: str | None) -> int:
    """Version recorded by a marker row; 1 for graphs written before markers."""
    text = str(import_name or "")
    if not text.startswith(_GRAPH_VERSION_PREFIX):
        return 1
    try:
        return max(1, int(text[len(_GRAPH_VERSION_PREFIX):]))
    except ValueError:
        return 1


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
        # package.json directory -> entry files, named or not, so a
        # directory import (``require('../')``) finds the package entry.
        self.directories: dict[str, list[str]] = {}
        # tsconfig directory -> [(prefix, suffix, wildcard, [target templates])]
        self.aliases: dict[str, list[tuple[str, str, bool, list[str]]]] = {}

    def __bool__(self) -> bool:
        return bool(self.packages or self.aliases or self.directories)

    def add_package(self, rel_path: str, data: dict) -> None:
        directory = posixpath.dirname(rel_path)
        entries = [str(data[field]) for field in _ENTRY_FIELDS if isinstance(data.get(field), str)]
        entries.extend(_export_entries(data.get("exports")))
        self.directories.setdefault(directory, entries)
        name = data.get("name")
        if not isinstance(name, str) or not name or len(name) > 214 or name in self.packages:
            return
        self.packages[name] = (directory, entries)

    def resolve_directory(self, directory: str, known_files: set[str]) -> str | None:
        """The entry file of the package whose ``package.json`` is in ``directory``."""
        entries = self.directories.get(directory)
        if entries is None:
            return None
        return _package_entry(directory, entries, known_files)

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
            for base in (join(rest), *(join(source, rest) for source in _SOURCE_DIRS)):
                resolved = _probe(base, known_files)
                if resolved:
                    return resolved
            return None
        return _package_entry(directory, entries, known_files)


def _export_entries(exports) -> list[str]:
    """Entry paths named by a ``package.json`` ``exports`` field for ``"."``."""
    if isinstance(exports, dict) and "." in exports:
        exports = exports["."]
    if isinstance(exports, str):
        return [exports]
    if not isinstance(exports, dict):
        return []
    found: list[str] = []
    for condition in ("source", "import", "require", "default", "node", "types"):
        value = exports.get(condition)
        if isinstance(value, dict):
            value = value.get("default") or value.get("import") or value.get("require")
        if isinstance(value, str) and value.startswith("."):
            found.append(value)
    return found


def _package_entry(directory: str, entries: list[str], known_files: set[str]) -> str | None:
    """Resolve a package's entry: declared entries, their source twins, then index."""
    join = (lambda *parts: posixpath.join(directory, *parts)) if directory else posixpath.join
    for entry in entries:
        resolved = _probe(join(entry), known_files)
        if resolved:
            return resolved
    for entry in entries:
        parts = _normalise(posixpath.normpath(entry)).split("/")
        if len(parts) > 1 and parts[0] in _BUILD_DIRS:
            for source in _SOURCE_DIRS:
                stem = posixpath.splitext("/".join([source, *parts[1:]]))[0]
                resolved = _probe(join(stem), known_files)
                if resolved:
                    return resolved
    for base in (join("index"), *(join(source, "index") for source in _SOURCE_DIRS)):
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
        elif name in JS_CONFIG_NAMES:
            data = _read_json(file_path, lenient=True)
            if data:
                resolver.add_tsconfig(_normalise(os.path.relpath(file_path, repo_path)), data)
    return resolver


def build_js_resolver_from_texts(configs: Iterable[tuple[str, str]]) -> _JsResolver:
    """The same resolver from ``(relative path, text)`` of the config files."""
    resolver = _JsResolver()
    for rel_path, text in configs:
        name = posixpath.basename(rel_path).lower()
        if name not in JS_CONFIG_NAMES or len(text) > _PACKAGE_JSON_MAX_BYTES:
            continue
        try:
            data = json.loads(text if name == "package.json" else _strip_json_comments(text))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if name == "package.json":
            resolver.add_package(_normalise(rel_path), data)
        else:
            resolver.add_tsconfig(_normalise(rel_path), data)
    return resolver


class _PyResolver:
    """Find the file a Python import names, the way ``sys.path`` would.

    Search roots are the directories that hold a top-level package (the
    parent of the outermost ``__init__.py`` directory: ``src`` for a
    ``src/flask`` layout), plus the repository root and ``src``. A module
    outside any package (a script, or a pytest ``tests/`` directory without
    ``__init__.py``) also sees its own directory first, which is where
    Python puts it on ``sys.path``.

    Other roots are only searched from files in the same project tree, from
    a test folder that contains them, or when the root is an installable
    project (it, or the folder above its ``src``, has a
    ``pyproject.toml``/``setup.py``). A package under
    ``docs/examples/logging`` therefore never captures ``import logging``
    elsewhere, and a standard library name never resolves to another
    project's package at all.
    """

    def __init__(self, known_files: set[str]) -> None:
        self.known_files = known_files
        self.packages = {
            posixpath.dirname(path) for path in known_files
            if posixpath.basename(path) in {"__init__.py", "__init__.pyi"} and "/" in path
        }
        self.top_level: dict[str, set[str]] = {}
        for package in self.packages:
            parent = posixpath.dirname(package)
            if parent not in self.packages:
                self.top_level.setdefault(posixpath.basename(package), set()).add(parent)
        self.roots = sorted({"", "src", *(root for roots in self.top_level.values() for root in roots)})
        projects = {posixpath.dirname(path) for path in known_files if posixpath.basename(path) in _PY_PROJECT_FILES}
        self.installable = {root for root in self.roots if root in projects or _project_dir(root) in projects}

    def _visible(self, root: str, name: str, source_path: str) -> bool:
        """Whether ``import name`` in ``source_path`` may be found under ``root``."""
        project = _project_dir(root)
        if project == "" or source_path.startswith(project + "/"):
            return True
        if name in _PY_STDLIB:
            return False
        # Fixture apps inside a test folder (a conftest puts ``tests/test_apps``
        # on ``sys.path`` for ``from blueprintapp import app``).
        script = self.script_root(source_path)
        if script and root.startswith(script + "/"):
            return True
        return root in self.installable

    def module_file(self, base: str) -> str | None:
        """``base.py`` or ``base/__init__.py`` (``base`` is a path without suffix)."""
        if not base or base == "." or base.startswith("../"):
            return None
        for suffix in _PY_MODULE_SUFFIXES:
            candidate = base + suffix
            if candidate in self.known_files:
                return candidate
        return None

    def script_root(self, source_path: str) -> str:
        """The first directory above ``source_path`` that is not a package."""
        directory = posixpath.dirname(source_path)
        while directory in self.packages:
            directory = posixpath.dirname(directory)
        return directory

    def _roots_for(self, name: str, source_path: str) -> list[str]:
        own = [root for root in self.top_level.get(name, ()) if self._visible(root, name, source_path)]
        # Several copies of a package (vendored, examples): the one sharing
        # the longest path with the importing file wins, then the shortest.
        ranked = sorted(own, key=lambda root: (-_common_depth(root, source_path), len(root), root))
        others = [root for root in self.roots if self._visible(root, name, source_path)]
        ordered = [self.script_root(source_path), *ranked, "", "src", *others]
        return list(dict.fromkeys(ordered))

    def init_file(self, directory: str) -> str | None:
        for name in ("__init__.py", "__init__.pyi"):
            candidate = posixpath.join(directory, name) if directory else name
            if candidate in self.known_files:
                return candidate
        return None

    def resolve_absolute(self, dotted: str, source_path: str) -> tuple[str | None, str | None, bool]:
        """``(module file, root, complete)`` for ``a.b.c``.

        Falls back to the deepest importable prefix (``a.b`` when ``c`` is a
        name defined in ``a/b.py``); ``complete`` says whether the whole
        dotted name is a module.
        """
        parts = [part for part in dotted.split(".") if part]
        if not parts:
            return None, None, False
        for root in self._roots_for(parts[0], source_path):
            for depth in range(len(parts), 0, -1):
                found = self.module_file(posixpath.join(root, *parts[:depth]) if root else "/".join(parts[:depth]))
                if found:
                    return found, root, depth == len(parts)
        return None, None, False

    def ancestor_inits(self, module: str, root: str | None, source_path: str) -> list[str]:
        """Package ``__init__`` files Python runs before ``module``.

        Only recorded for an importer outside the top-level package (a test
        importing ``flask.testing`` also runs ``flask/__init__.py``); inside
        the package they are already loaded and would only add noise.
        """
        if root is None:
            return []
        parts = (module[len(root) + 1:] if root else module).split("/")
        if len(parts) < 2:
            return []
        top = posixpath.join(root, parts[0]) if root else parts[0]
        if source_path.startswith(top + "/"):
            return []
        found = []
        for depth in range(1, len(parts)):
            init = self.init_file(posixpath.join(root, *parts[:depth]) if root else "/".join(parts[:depth]))
            if init and init != module:
                found.append(init)
        return found

    def resolve_line(self, line: str, source_path: str) -> list[tuple[str, str]]:
        """``(import name, target file)`` for one line of Python source."""
        from_match = _PY_FROM_LINE.match(line)
        if from_match:
            return self._resolve_from(from_match.group(1), from_match.group(2), source_path)
        import_match = _PY_IMPORT_LINE.match(line)
        if not import_match:
            return []
        results: list[tuple[str, str]] = []
        for item in import_match.group(1).split(","):
            words = item.split()
            if not words:
                continue
            module, root, _complete = self.resolve_absolute(words[0], source_path)
            if module:
                results.append((words[0], module))
                results.extend((words[0], init) for init in self.ancestor_inits(module, root, source_path))
        return results

    def _resolve_from(self, target: str, names_text: str, source_path: str) -> list[tuple[str, str]]:
        names = _imported_names(names_text)
        relative = target.startswith(".")
        root = None
        package_dir = None
        if relative:
            dot_count = len(target) - len(target.lstrip("."))
            base_dir = posixpath.dirname(source_path)
            for _ in range(dot_count - 1):
                base_dir = posixpath.dirname(base_dir)
            remainder = target[dot_count:]
            base = posixpath.join(base_dir, remainder.replace(".", "/")) if remainder else base_dir
            module = self.module_file(base)
            package_dir = base or None
        else:
            module, root, complete = self.resolve_absolute(target, source_path)
            if module and complete and posixpath.basename(module).startswith("__init__."):
                package_dir = posixpath.dirname(module)
        submodules: list[str] = []
        attributes = not names or package_dir is None
        if package_dir is not None:
            for name in names:
                found = self.module_file(posixpath.join(package_dir, name))
                if found:
                    submodules.append(found)
                else:
                    attributes = True
        results = [(target, path) for path in submodules]
        # ``from pkg import name`` always runs pkg/__init__ for an outside
        # importer; inside a package only attribute imports depend on it.
        if module and (attributes or not relative or not submodules):
            results.append((target, module))
            if not relative:
                results.extend((target, init) for init in self.ancestor_inits(module, root, source_path))
        return results


def _project_dir(root: str) -> str:
    """The project folder of a search root: ``packages/a`` for ``packages/a/src``."""
    return posixpath.dirname(root) if posixpath.basename(root) in _PY_SOURCE_DIRS else root


def _common_depth(directory: str, source_path: str) -> int:
    left = [part for part in directory.split("/") if part]
    right = source_path.split("/")[:-1]
    depth = 0
    for a, b in zip(left, right):
        if a != b:
            break
        depth += 1
    return depth


def _imported_names(text: str) -> list[str]:
    """Plain names from ``a, b as c`` (the part after ``from x import``)."""
    text = text.split("#", 1)[0].replace("(", " ").replace(")", " ").replace("\\", " ")
    names = []
    for item in text.split(","):
        words = item.split()
        if words and _PY_NAME.match(words[0]):
            names.append(words[0])
    return names[:32]


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
            if base.startswith("../") or base == "..":
                return None
            directory = "" if base == "." else base
            # A file first, then the directory's package entry, then its
            # index: ``require('../')`` from test/ is the package itself.
            resolved = _probe(base, known_files) if directory else None
            if not resolved and resolver is not None:
                resolved = resolver.resolve_directory(directory, known_files)
            if not resolved and not directory:
                resolved = _probe("index", known_files)
            return resolved
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


def triple_quote_state(line: str, open_quote: str | None) -> str | None:
    """The triple-quote delimiter still open after ``line`` (None when closed)."""
    index = 0
    while True:
        if open_quote is not None:
            end = line.find(open_quote, index)
            if end == -1:
                return open_quote
            index, open_quote = end + 3, None
            continue
        starts = [(position, quote) for quote in ('"""', "'''") if (position := line.find(quote, index)) != -1]
        hash_at = line.find("#", index)
        if not starts:
            return None
        position, quote = min(starts)
        if hash_at != -1 and hash_at < position:
            return None
        index, open_quote = position + 3, quote


def scans_imports(source_path: str) -> bool:
    """Whether import lines of this file are read at all (code, not docs or data)."""
    return bool(_patterns_for(source_path)[0])


class DependencyScanner:
    """Resolve imports file by file against one set of repository paths.

    Matching stays line-oriented to keep memory flat for 50 MB files; the
    closing ``} from "x"`` line of a multi-line import carries the specifier,
    so multi-line statements are still resolved (at that line). The same
    scanner reads a checkout or text rebuilt from the index.
    """

    def __init__(self, known_files: set[str], js_resolver: _JsResolver | None = None) -> None:
        self.known_files = known_files
        self.js_resolver = js_resolver
        self.py_resolver = _PyResolver(known_files)
        self.edges: list[dict] = []
        self.seen: set[tuple[str, str, int]] = set()

    def scan_lines(self, source_path: str, lines: Iterable[str]) -> None:
        patterns, use_resolver = _patterns_for(source_path)
        if not patterns:
            return
        python = patterns is _PY_PATTERNS
        resolver = self.js_resolver if use_resolver else None
        # Docstrings and doc comments are full of usage examples (``from
        # flask import Flask``, a JSDoc ``@example`` with ``import ky from
        # 'ky'``); lines inside them are not imports. A JS block comment
        # counts only when it opens a line, so ``'src/**/*.ts'`` is safe.
        open_quote: str | None = None
        in_comment = False
        for line_number, line in enumerate(lines, 1):
            if len(line) > 20_000:
                # Minified/generated content; imports are never this long.
                continue
            if python:
                if open_quote is None and "import" in line:
                    for import_name, target_path in self.py_resolver.resolve_line(line, source_path):
                        _record(self.edges, self.seen, source_path, target_path, import_name, line_number)
                if '"""' in line or "'''" in line:
                    open_quote = triple_quote_state(line, open_quote)
                continue
            if use_resolver:
                if in_comment:
                    in_comment = "*/" not in line
                    continue
                stripped = line.lstrip()
                if stripped.startswith("/*") and "*/" not in stripped[2:]:
                    in_comment = True
                    continue
            for pattern in patterns:
                for match in pattern.finditer(line):
                    target_path = _resolve_import(match.group(1), source_path, self.known_files, resolver)
                    _record(self.edges, self.seen, source_path, target_path, match.group(1), line_number)

    def sorted_edges(self) -> list[dict]:
        return sorted(self.edges, key=lambda edge: (edge["source_file"], edge["line_number"], edge["target_file"]))


def build_manifest_and_dependency_manifest(files: list[str], repo_path: str) -> tuple[dict[str, dict[str, int | str]], list[dict]]:
    """Hash files and resolve local imports with bounded memory.

    Hashing uses raw bytes so incremental re-indexing remains exact. A second
    text pass extracts local imports one line at a time; this avoids keeping a
    50 MB decoded string, ``splitlines`` list, and newline-offset array alive
    during the manifest stage. Manifest keys are exactly the repository
    relative paths the pipeline uses (``.github/...`` keeps its dot).
    """
    known_files = {_normalise(os.path.relpath(path, repo_path)) for path in files}
    scanner = DependencyScanner(known_files, build_js_resolver(files, repo_path))
    manifest: dict[str, dict[str, int | str]] = {}
    for file_path in files:
        source_path = _normalise(os.path.relpath(file_path, repo_path))
        try:
            digest = hashlib.sha256()
            with open(file_path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                scanner.scan_lines(source_path, handle)
        except OSError:
            continue
        manifest[source_path] = {"content_hash": digest.hexdigest(), "byte_size": os.path.getsize(file_path)}
    return manifest, scanner.sorted_edges()


def build_dependency_manifest(files: list[str], repo_path: str) -> list[dict]:
    """Build a dependency graph for callers that do not need file hashes."""
    _manifest, dependencies = build_manifest_and_dependency_manifest(files, repo_path)
    return dependencies
