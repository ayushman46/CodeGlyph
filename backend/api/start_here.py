""""Start here": where a new engineer begins reading a repository.

Everything is read from the published index (manifests, the README, the entry
file and the modules it imports), so it costs no model call, is the same on
every load, and every fact carries the file and line it came from:

* ``entry``: the file the package or application starts from;
* ``core``: the folder that holds the main code;
* ``commands``: install, test and run commands from the project's own files;
* ``steps``: 3-5 files to read in order, each opened at a specific line;
* ``guides``: README sections about setup, tests and contributing.

The parsing and derivation functions are pure (text in, facts out) and are
tested directly; ``gather_start_here`` only reads the few files they need.
"""

from __future__ import annotations

import json
import posixpath
import re
import time
import tomllib
from typing import Any, Callable

# Root files read for commands and the project summary (lower-case names).
_MANIFESTS = frozenset({
    "package.json", "pyproject.toml", "setup.cfg", "setup.py", "makefile", "cargo.toml", "go.mod", "tox.ini",
    "pytest.ini", "requirements.txt", "requirements-dev.txt", "manage.py",
})
_README_NAMES = ("readme.md", "readme.rst", "readme.markdown", "readme.txt", "readme")
_LOCKFILES = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"))
_JS_EXTENSIONS = (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# Built output named in package.json maps back to the source folder.
_BUILD_DIRS = frozenset({"dist", "distribution", "build", "lib", "out", "esm", "cjs"})
_SOURCE_DIRS = ("src", "source", "lib")
_SHELL_FENCES = frozenset({"", "sh", "bash", "shell", "console", "zsh", "shell-session", "sh-session", "terminal", "text"})
_README_RUNNERS = frozenset({"node", "python", "python3", "deno", "bun"})
_MAX_STEPS = 5
_MIN_STEPS = 3
# Modules read to choose the reading path (some are skipped, see below).
_FOLLOW_READS = 8
_FILE_ROWS = 40
# Modules that support the code rather than explain it: read after the others.
_SUPPORTING_STEMS = re.compile(
    r"^_*(?:version|about|exceptions?|errors?|types?|typing|constants?|compat|utils?|helpers?|warnings|status_codes)_*$"
)
_CACHE_TTL_SECONDS = 600.0
_CACHE: dict[str, tuple[float, tuple, dict | None]] = {}
_NPM_DEFAULT_TEST = 'echo "Error: no test specified" && exit 1'
# README sections worth linking, most useful kind first.
_GUIDE_KINDS = (
    ("install", "setup", "set up", "getting started", "get started", "quick start", "quickstart"),
    ("running tests", "testing", "tests"),
    ("development", "developing", "contributing", "building"),
    ("usage", "example"),
)

Exists = Callable[[str], bool]


def _cite(path: str, line: int | None) -> dict:
    return {"path": path, "line": max(1, int(line or 1))}


def _truncate(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_line_matching(text: str, pattern: str) -> int | None:
    regex = re.compile(pattern)
    return next((index + 1 for index, line in enumerate(text.split("\n")) if regex.search(line)), None)


# ---------------------------------------------------------------------------
# Manifests and README (pure)
# ---------------------------------------------------------------------------


def _json_depths(lines: list[str]) -> list[int]:
    """Nesting depth at the start of each line, ignoring brackets in strings."""
    depth, in_string, escaped, depths = 0, False, False, []
    for line in lines:
        depths.append(depth)
        for char in line:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
    return depths


def package_json_facts(text: str) -> dict:
    """Fields of package.json as ``{"value", "line"}`` (1-based line of the key)."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    lines = text.split("\n")
    depths = _json_depths(lines)

    def key_line(key: str, parent: str | None = None) -> int | None:
        pattern = re.compile(rf'^\s*"{re.escape(key)}"\s*:')
        if parent is None:
            return next((index + 1 for index, line in enumerate(lines) if depths[index] == 1 and pattern.match(line)), None)
        start = key_line(parent)
        if start is None:
            return None
        for index in range(start, len(lines)):
            if depths[index] < 2:
                break
            if depths[index] == 2 and pattern.match(lines[index]):
                return index + 1
        return None

    def field(key: str) -> dict | None:
        value = data.get(key)
        return None if value in (None, "", {}, []) else {"value": value, "line": key_line(key)}

    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    return {
        "name": field("name"),
        "description": field("description"),
        "main": field("main"),
        "module": field("module"),
        "exports": field("exports"),
        "bin": field("bin"),
        "package_manager": field("packageManager"),
        "dependencies": field("dependencies"),
        "dev_dependencies": field("devDependencies"),
        "scripts": {
            name: {"value": command, "line": key_line(name, "scripts")}
            for name, command in scripts.items() if isinstance(command, str)
        },
    }


def _toml_sections(lines: list[str]) -> dict[str, int]:
    sections: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$", line)
        if match:
            sections.setdefault(match.group(1).strip().strip('"'), index + 1)
    return sections


def _toml_key_line(lines: list[str], sections: dict[str, int], section: str, key: str) -> int | None:
    start = sections.get(section)
    if start is None:
        return None
    pattern = re.compile(rf'^\s*"?{re.escape(key)}"?\s*=')
    for index in range(start, len(lines)):
        if re.match(r"^\s*\[", lines[index]):
            return None
        if pattern.match(lines[index]):
            return index + 1
    return None


def pyproject_facts(text: str) -> dict:
    """Project metadata, console scripts and tool sections of pyproject.toml, with lines."""
    lines = text.split("\n")
    sections = _toml_sections(lines)
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        data = {}  # a truncated index: section lines still work
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}

    def field(section: str, key: str, value: Any) -> dict | None:
        if value in (None, "", [], {}):
            return None
        return {"value": value, "line": _toml_key_line(lines, sections, section, key) or sections.get(section)}

    scripts, script_section = project.get("scripts"), "project.scripts"
    if not isinstance(scripts, dict) or not scripts:
        scripts, script_section = poetry.get("scripts"), "tool.poetry.scripts"
    pytest_options = (tool.get("pytest") or {}).get("ini_options") if isinstance(tool.get("pytest"), dict) else None
    flit = tool.get("flit") if isinstance(tool.get("flit"), dict) else {}
    setuptools = tool.get("setuptools") if isinstance(tool.get("setuptools"), dict) else {}
    packages = setuptools.get("packages") if isinstance(setuptools.get("packages"), dict) else {}
    find = packages.get("find") if isinstance(packages.get("find"), dict) else {}
    return {
        "name": field("project", "name", project.get("name")) or field("tool.poetry", "name", poetry.get("name")),
        "description": field("project", "description", project.get("description"))
        or field("tool.poetry", "description", poetry.get("description")),
        "scripts": {
            str(name): {"value": target, "line": _toml_key_line(lines, sections, script_section, str(name))}
            for name, target in (scripts or {}).items() if isinstance(target, str)
        },
        "sections": sections,
        "flit_module": field("tool.flit.module", "name", (flit.get("module") or {}).get("name") if isinstance(flit.get("module"), dict) else None),
        "package_where": field("tool.setuptools.packages.find", "where", find.get("where")),
        "pytest": (
            {"line": sections["tool.pytest.ini_options"], "testpaths": (pytest_options or {}).get("testpaths")}
            if "tool.pytest.ini_options" in sections else None
        ),
    }


def makefile_targets(text: str) -> dict[str, dict]:
    """``{target: {"line", "recipe"}}`` with each rule's first recipe line."""
    lines = text.split("\n")
    targets: dict[str, dict] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z0-9][\w.-]*)\s*:(?![=:])", line)
        if not match:
            continue
        recipe = ""
        for following in lines[index + 1: index + 6]:
            if following.startswith("\t"):
                recipe = following.strip().lstrip("@")
                break
            if following.strip() and not following.lstrip().startswith("#"):
                break
        targets.setdefault(match.group(1), {"line": index + 1, "recipe": recipe})
    return targets


def readme_facts(text: str) -> dict:
    """Headings, the first descriptive line and shell commands, each with its line."""
    lines = text.split("\n")
    headings: list[dict] = []
    commands: list[dict] = []
    summary = None
    fence: str | None = None
    shell = False
    heading = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            elif shell and stripped and not stripped.startswith("#"):
                commands.append({"command": stripped[2:] if stripped.startswith("$ ") else stripped,
                                 "line": index + 1, "heading": heading})
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            shell = stripped[3:].strip().lower() in _SHELL_FENCES
            continue
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        atx = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        setext = bool(stripped) and bool(re.fullmatch(r"=+|-+|~+|\^+", following)) and len(following) >= 3
        if atx or setext:
            heading = re.sub(r"[*_`]", "", atx.group(2) if atx else stripped).strip()
            level = len(atx.group(1)) if atx else (1 if following.startswith("=") else 2)
            headings.append({"title": heading, "line": index + 1, "level": level})
            continue
        if stripped.startswith("$ "):
            commands.append({"command": stripped[2:], "line": index + 1, "heading": heading})
            continue
        if summary is None and len(stripped) >= 30 and not re.fullmatch(r"[=\-~^]+", stripped) and not stripped.startswith(
            ("<", "[!", "![", "[![", "|", ">", "- ", "* ", "..", ":", "#")
        ):
            summary = {"value": stripped, "line": index + 1}
    return {"headings": headings, "commands": commands, "summary": summary}


def readme_guides(readme_path: str | None, readme: dict, limit: int = 4) -> list[dict]:
    """README sections about setup, tests and contributing, in document order."""
    if not readme_path:
        return []
    picked: list[tuple[int, int, str]] = []
    for heading in readme.get("headings") or []:
        title = heading["title"]
        lower = title.lower()
        rank = next((rank for rank, words in enumerate(_GUIDE_KINDS) if any(word in lower for word in words)), None)
        if rank is not None and heading.get("level", 2) <= 3 and len(title) <= 60 and "table of contents" not in lower:
            picked.append((rank, heading["line"], title))
    chosen = sorted(picked)[:limit]
    return [{"title": title, "path": readme_path, "line": line} for _rank, line, title in sorted(chosen, key=lambda item: item[1])]


# ---------------------------------------------------------------------------
# Imports and definitions (pure)
# ---------------------------------------------------------------------------


def python_imports(text: str) -> list[dict]:
    """``from .x import a, b`` and ``from pkg.x import a``: ``{"line", "dots", "module", "names"}``."""
    imports: list[dict] = []
    for index, line in enumerate(text.split("\n")):
        match = re.match(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+\(?\s*(.*)$", line)
        if not match or not (match.group(1) or match.group(2)):
            continue
        names = []
        for part in match.group(3).rstrip(")\\ ").split(","):
            word = part.strip().split(" ")[0]
            if word and (word[0].isalpha() or word[0] == "_"):
                names.append(word)
        if match.group(2):
            imports.append({"line": index + 1, "dots": len(match.group(1)), "module": match.group(2), "names": names})
        else:
            # ``from . import utils, json``: each name is a module of the package.
            imports.extend({"line": index + 1, "dots": len(match.group(1)), "module": name, "names": []} for name in names)
    return imports


def python_candidates(path: str, dots: int, module: str) -> list[str]:
    """Indexed files ``from <dots><module> import`` can mean inside ``path``'s package."""
    if not module:
        return []
    parts = module.split(".")
    if dots:
        directory = posixpath.dirname(path)
        for _ in range(dots - 1):
            directory = posixpath.dirname(directory)
        base = posixpath.join(directory, *parts) if directory else posixpath.join(*parts)
        return [f"{base}.py", f"{base}/__init__.py"]
    # Absolute: only modules of the package this file belongs to.
    segments = path.split("/")
    if parts[0] not in segments[:-1]:
        return []
    root = "/".join(segments[: segments.index(parts[0])])
    base = posixpath.join(root, *parts) if root else posixpath.join(*parts)
    return [f"{base}.py", f"{base}/__init__.py"]


_JS_IMPORT_PATTERNS = (
    re.compile(r"""require\(\s*['"](\.{1,2}(?:/[^'"]*)?)['"]\s*\)"""),
    re.compile(r"""^\s*(?:import|export)\b[^'"]*?\bfrom\s+['"](\.{1,2}/[^'"]+)['"]"""),
    re.compile(r"""^\s*import\s+['"](\.{1,2}/[^'"]+)['"]"""),
)


def js_imports(text: str) -> list[dict]:
    """Relative ``require``/``import``/``export … from``: ``{"line", "spec", "names"}``."""
    imports: list[dict] = []
    for index, line in enumerate(text.split("\n")):
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        for pattern in _JS_IMPORT_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            braces = re.search(r"\{([^}]*)\}", line)
            names = []
            for part in (braces.group(1).split(",") if braces else []):
                word = part.replace("type ", "").split(" as ")[0].strip()
                if word:
                    names.append(word)
            imports.append({"line": index + 1, "spec": match.group(1), "names": names})
            break
    return imports


def js_candidates(base_dir: str, spec: str) -> list[str]:
    """Paths a relative JS/TS specifier can resolve to, most likely first."""
    target = posixpath.normpath(posixpath.join(base_dir, spec))
    if target.startswith(".."):
        return []
    if target == ".":
        return [f"index{extension}" for extension in (".js", ".ts", ".mjs", ".tsx")]
    stem, extension = posixpath.splitext(target)
    candidates: list[str] = []
    if extension in _JS_EXTENSIONS:
        candidates.append(target)
        if extension in {".js", ".jsx", ".mjs", ".cjs"}:
            # TypeScript sources are imported with the compiled ".js" suffix.
            candidates += [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
    else:
        candidates += [target + suffix for suffix in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")]
    candidates += [f"{target}/index{suffix}" for suffix in (".js", ".ts", ".tsx", ".mjs")]
    return list(dict.fromkeys(candidates))


def import_candidates(path: str, text: str) -> list[str]:
    """Every path the imports of ``path`` could resolve to (for one existence query)."""
    if path.endswith(".py"):
        return [candidate for item in python_imports(text) for candidate in python_candidates(path, item["dots"], item["module"])]
    base_dir = posixpath.dirname(path)
    return [candidate for item in js_imports(text) for candidate in js_candidates(base_dir, item["spec"])]


def local_imports(path: str, text: str, exists: Exists) -> list[dict]:
    """Indexed files ``path`` imports, in source order: ``{"line", "path", "names"}``."""
    found: dict[str, dict] = {}
    if path.endswith(".py"):
        items = [(item, python_candidates(path, item["dots"], item["module"])) for item in python_imports(text)]
    else:
        base_dir = posixpath.dirname(path)
        items = [(item, js_candidates(base_dir, item["spec"])) for item in js_imports(text)]
    for item, candidates in items:
        target = next((candidate for candidate in candidates if exists(candidate)), None)
        if target and target != path:
            entry = found.setdefault(target, {"line": item["line"], "path": target, "names": []})
            entry["names"].extend(name for name in item["names"] if name not in entry["names"])
    return list(found.values())


def _definition_patterns(path: str, name: str | None) -> list[re.Pattern]:
    """Top-level definitions; each pattern captures the defined name as ``name``."""
    word = re.escape(name) if name else None
    if path.endswith(".py"):
        return [re.compile(rf"^(?:async\s+def|def|class)\s+(?P<name>{word or r'[A-Za-z_]\w*'})\b")]
    if word:
        return [
            re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(?P<name>{word})\b"),
            re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>{word})\b"),
            re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>{word})\s*[=:]"),
        ]
    return [
        re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(?P<name>[A-Za-z_$][\w$]*)"),
        re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"),
        re.compile(r"^(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*=\s*(?:[\w$.]+\s*=\s*)*(?:async\s+)?function\b"),
        re.compile(r"^(?:export\s+)?const\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function\b|[A-Za-z_$][\w$]*\s*=>)"),
    ]


def definition_line(path: str, text: str, names: list[str] | None = None) -> tuple[int, str] | None:
    """``(line, name)``: the earliest definition of one of ``names``, else the file's first definition."""
    lines = text.split("\n")
    best: tuple[int, str] | None = None
    for name in names or []:
        for pattern in _definition_patterns(path, name):
            found = next((index + 1 for index, line in enumerate(lines) if pattern.match(line)), None)
            if found and (best is None or found < best[0]):
                best = (found, name)
    if best:
        return best
    patterns = _definition_patterns(path, None)
    for index, line in enumerate(lines):
        for pattern in patterns:
            match = pattern.match(line)
            if match:
                return index + 1, match.group("name")
    return None


def js_exported_name(text: str) -> str | None:
    """``createApplication`` for ``exports = module.exports = createApplication;`` or ``export default``."""
    for line in text.split("\n"):
        match = (
            re.match(r"^\s*(?:exports\s*=\s*)?module\.exports\s*=\s*(?:exports\s*=\s*)?([A-Za-z_$][\w$]*)\s*;?\s*$", line)
            or re.match(r"^\s*export\s+default\s+(?:async\s+)?(?:function\*?\s+|class\s+)?([A-Za-z_$][\w$]*)", line)
        )
        if match and match.group(1) not in {"function", "class"}:
            return match.group(1)
    return None


def is_supporting(path: str) -> bool:
    """Errors, utilities, types, constants and version files."""
    stem = posixpath.basename(path).split(".")[0].lower()
    return bool(_SUPPORTING_STEMS.match(stem)) or "/types/" in f"/{path.lower()}"


def _is_thin(text: str, imports: list[dict]) -> bool:
    """A file that only re-exports one module (``module.exports = require('./lib/x')``)."""
    if len(imports) != 1:
        return False
    code = [
        line for line in text.split("\n")
        if line.strip() and not line.strip().startswith(("//", "/*", "*", "#", "'use", '"use'))
    ]
    return len(code) <= 12


# ---------------------------------------------------------------------------
# Deriving the card (pure)
# ---------------------------------------------------------------------------


def detect_ecosystem(manifests: dict[str, str], extensions: dict[str, int]) -> str | None:
    names = {path.lower() for path in manifests}
    node = "package.json" in names
    python = bool(names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "manage.py"})
    if node and python:
        scripts = sum(int(extensions.get(extension, 0)) for extension in ("js", "ts", "jsx", "tsx", "mjs", "cjs"))
        return "python" if int(extensions.get("py", 0)) >= scripts else "node"
    if node:
        return "node"
    if python:
        return "python"
    if "go.mod" in names:
        return "go"
    if "cargo.toml" in names:
        return "rust"
    return None


def parse_manifests(manifests: dict[str, str]) -> dict:
    parsed: dict[str, Any] = {}
    for path, text in manifests.items():
        name = path.lower()
        if name == "package.json":
            parsed["package"], parsed["package_path"] = package_json_facts(text), path
        elif name == "pyproject.toml":
            parsed["pyproject"], parsed["pyproject_path"] = pyproject_facts(text), path
        elif name == "makefile":
            parsed["makefile"], parsed["makefile_path"] = makefile_targets(text), path
    return parsed


def _manifest(manifests: dict[str, str], name: str) -> str | None:
    return next((path for path in manifests if path.lower() == name), None)


def _command(kind: str, command: str, source: dict, detail: str | None = None) -> dict:
    label = {"install": "Install", "test": "Run the tests", "run": "Run it", "build": "Build"}[kind]
    return {"kind": kind, "label": label, "command": command, "detail": detail, "source": source}


def _node_commands(path: str, facts: dict, root_files: list[str]) -> list[dict]:
    manager, manager_source = "npm", None
    declared = (facts.get("package_manager") or {}).get("value")
    if isinstance(declared, str) and declared.split("@")[0] in {"npm", "yarn", "pnpm", "bun"}:
        manager, manager_source = declared.split("@")[0], _cite(path, facts["package_manager"]["line"])
    else:
        lowered = {name.lower(): name for name in root_files}
        for lockfile, name in _LOCKFILES:
            if lockfile in lowered:
                manager, manager_source = name, _cite(lowered[lockfile], 1)
                break
    deps = facts.get("dev_dependencies") or facts.get("dependencies")
    commands = [_command(
        "install", f"{manager} install", manager_source or _cite(path, (deps or facts.get("name") or {}).get("line")),
        f"Installs the dependencies declared in {path}." if deps else None,
    )]

    def script_command(name: str) -> str:
        if manager == "bun":
            return f"bun run {name}"
        if name in {"test", "start"}:
            return f"{manager} {name}"
        return f"npm run {name}" if manager == "npm" else f"{manager} {name}"

    scripts = facts.get("scripts") or {}
    test = scripts.get("test")
    if test and test.get("line") and test["value"].strip() != _NPM_DEFAULT_TEST:
        commands.append(_command("test", script_command("test"), _cite(path, test["line"]), _truncate(test["value"])))
    for name in ("start", "dev", "serve"):
        script = scripts.get(name)
        if script and script.get("line"):
            commands.append(_command("run", script_command(name), _cite(path, script["line"]), _truncate(script["value"])))
            break
    return commands


def _python_commands(manifests: dict[str, str], parsed: dict, root_files: list[str]) -> list[dict]:
    pyproject = parsed.get("pyproject") or {}
    pyproject_path = parsed.get("pyproject_path")
    sections = pyproject.get("sections") or {}
    targets = parsed.get("makefile") or {}
    make_path = parsed.get("makefile_path")
    uses_uv = "tool.uv" in sections or any(name.lower() == "uv.lock" for name in root_files)
    runner = "uv run " if uses_uv else ""
    commands: list[dict] = []

    target = next((name for name in ("install", "init", "setup", "dev", "develop", "bootstrap") if name in targets), None)
    if target:
        commands.append(_command("install", f"make {target}", _cite(make_path, targets[target]["line"]), targets[target]["recipe"] or None))
    elif pyproject_path and "tool.uv" in sections:
        commands.append(_command("install", "uv sync", _cite(pyproject_path, sections["tool.uv"]),
                                 "Creates the virtual environment and installs the project with uv."))
    elif pyproject_path and "tool.poetry" in sections:
        commands.append(_command("install", "poetry install", _cite(pyproject_path, sections["tool.poetry"])))
    elif pyproject_path and "tool.pdm" in sections:
        commands.append(_command("install", "pdm install", _cite(pyproject_path, sections["tool.pdm"])))
    elif pyproject_path and ("build-system" in sections or "project" in sections):
        commands.append(_command("install", "pip install -e .", _cite(pyproject_path, sections.get("build-system") or sections.get("project")),
                                 "Installs the project in editable mode."))
    elif _manifest(manifests, "setup.py"):
        setup = _manifest(manifests, "setup.py")
        commands.append(_command("install", "pip install -e .", _cite(setup, _first_line_matching(manifests[setup], r"\bsetup\(")),
                                 "Installs the project in editable mode."))
    else:
        requirements = _manifest(manifests, "requirements-dev.txt") or _manifest(manifests, "requirements.txt")
        if requirements:
            commands.append(_command("install", f"pip install -r {requirements}", _cite(requirements, 1)))

    setup_cfg = _manifest(manifests, "setup.cfg")
    cfg_line = _first_line_matching(manifests[setup_cfg], r"^\[tool:pytest\]") if setup_cfg else None
    if "test" in targets:
        commands.append(_command("test", "make test", _cite(make_path, targets["test"]["line"]), targets["test"]["recipe"] or None))
    elif pyproject.get("pytest"):
        paths = pyproject["pytest"].get("testpaths")
        where = ", ".join(map(str, paths)) if isinstance(paths, list) else str(paths or "")
        commands.append(_command("test", f"{runner}pytest", _cite(pyproject_path, pyproject["pytest"]["line"]),
                                 f"pytest is configured here{f'; tests live in {where}' if where else ''}."))
    elif _manifest(manifests, "pytest.ini"):
        commands.append(_command("test", f"{runner}pytest", _cite(_manifest(manifests, "pytest.ini"), 1)))
    elif cfg_line:
        commands.append(_command("test", f"{runner}pytest", _cite(setup_cfg, cfg_line)))
    elif "tool.tox" in sections:
        commands.append(_command("test", "tox", _cite(pyproject_path, sections["tool.tox"])))
    elif _manifest(manifests, "tox.ini"):
        commands.append(_command("test", "tox", _cite(_manifest(manifests, "tox.ini"), 1)))

    scripts = pyproject.get("scripts") or {}
    manage = _manifest(manifests, "manage.py")
    if scripts:
        name, script = next(iter(scripts.items()))
        commands.append(_command("run", f"{runner}{name} --help", _cite(pyproject_path, script["line"]),
                                 f"The {name} command runs {script['value']}."))
    elif manage:
        commands.append(_command("run", "python manage.py runserver",
                                 _cite(manage, _first_line_matching(manifests[manage], r"execute_from_command_line"))))
    else:
        target = next((name for name in ("run", "serve", "start", "dev") if name in targets), None)
        if target:
            commands.append(_command("run", f"make {target}", _cite(make_path, targets[target]["line"]), targets[target]["recipe"] or None))
    return commands


def build_commands(ecosystem: str | None, manifests: dict[str, str], parsed: dict, root_files: list[str]) -> list[dict]:
    """Install, test and run commands, each cited to the line that defines it."""
    if ecosystem == "node" and parsed.get("package"):
        return _node_commands(parsed["package_path"], parsed["package"], root_files)
    if ecosystem == "python":
        return _python_commands(manifests, parsed, root_files)
    if ecosystem == "go":
        path = _manifest(manifests, "go.mod")
        return [_command("install", "go mod download", _cite(path, 1)), _command("test", "go test ./...", _cite(path, 1))]
    if ecosystem == "rust":
        path = _manifest(manifests, "cargo.toml")
        line = _first_line_matching(manifests[path], r"^\[package\]")
        return [_command("build", "cargo build", _cite(path, line)), _command("test", "cargo test", _cite(path, line))]
    targets = parsed.get("makefile") or {}
    commands = []
    for kind, names in (("install", ("install", "setup", "init", "deps")), ("test", ("test", "check")),
                        ("run", ("run", "serve", "start", "dev"))):
        target = next((name for name in names if name in targets), None)
        if target:
            commands.append(_command(kind, f"make {target}", _cite(parsed["makefile_path"], targets[target]["line"]),
                                     targets[target]["recipe"] or None))
    return commands


def _readme_run_targets(command: str) -> list[str]:
    tokens = command.split()
    if len(tokens) < 2 or tokens[0] not in _README_RUNNERS or tokens[1].startswith("-"):
        return []
    clean = tokens[1].removeprefix("./").rstrip("/")
    return [clean, *(f"{clean}{suffix}" for suffix in (".js", ".mjs", ".ts", ".py")),
            *(f"{clean}/{name}" for name in ("index.js", "index.mjs", "index.ts", "__main__.py", "main.py"))]


def readme_run_paths(readme: dict) -> list[str]:
    """Files README commands such as ``node examples/x`` would run (to check they exist)."""
    return [path for item in readme.get("commands") or [] for path in _readme_run_targets(item["command"])][:200]


def readme_run_command(readme_path: str | None, readme: dict, exists: Exists) -> dict | None:
    """The first README command that runs a file of this repository (checked to exist)."""
    if not readme_path:
        return None
    for item in readme.get("commands") or []:
        if any(exists(path) for path in _readme_run_targets(item["command"])):
            section = f" ({item['heading']})" if item.get("heading") else ""
            return _command("run", item["command"], _cite(readme_path, item["line"]), f"From the README{section}.")
    return None


def _node_paths(value: str) -> list[str]:
    """Indexed files a package.json path can name; built output maps back to source."""
    clean = value.strip()
    clean = clean[2:] if clean.startswith("./") else clean.lstrip("/")
    if not clean or clean.endswith(".d.ts"):
        return []
    candidates = js_candidates("", clean)
    parts = clean.split("/")
    if len(parts) > 1 and parts[0] in _BUILD_DIRS:
        for source_dir in _SOURCE_DIRS:
            if source_dir != parts[0]:
                candidates += js_candidates("", "/".join([source_dir, *parts[1:]]))
    return list(dict.fromkeys(candidates))


def _export_target(exports: Any) -> str | None:
    """The main target of a package.json ``exports`` field."""
    root = exports.get(".", exports) if isinstance(exports, dict) else exports
    if isinstance(root, str):
        return root
    if isinstance(root, dict):
        for key in ("source", "import", "default", "require", "node"):
            value = root.get(key)
            if isinstance(value, dict):
                value = value.get("default") or value.get("import")
            if isinstance(value, str):
                return value
    return None


def entry_candidates(ecosystem: str | None, parsed: dict, fallback: str | None) -> list[dict]:
    """Ordered ``{"paths", "source", "reason"}`` options for the entry file."""
    options: list[dict] = []
    package = parsed.get("package") or {}
    package_path = parsed.get("package_path")
    if ecosystem == "node" and package_path:
        for key in ("main", "module"):
            field = package.get(key)
            if field and isinstance(field["value"], str):
                options.append({"paths": _node_paths(field["value"]), "source": _cite(package_path, field["line"]),
                                "reason": f'the "{key}" field of {package_path}'})
        exports = package.get("exports")
        target = _export_target(exports["value"]) if exports else None
        if target:
            options.append({"paths": _node_paths(target), "source": _cite(package_path, exports["line"]),
                            "reason": f'the "exports" field of {package_path}'})
        options.append({"paths": ["index.js", "index.ts", "index.mjs", "index.cjs", "src/index.ts", "src/index.tsx",
                                  "src/index.js", "src/main.ts", "src/main.tsx", "src/main.js", "server.js", "app.js"],
                        "source": None, "reason": "Node's default entry file"})
        binary = package.get("bin")
        value = binary["value"] if binary else None
        value = next(iter(value.values()), None) if isinstance(value, dict) else value
        if isinstance(value, str):
            options.append({"paths": _node_paths(value), "source": _cite(package_path, binary["line"]),
                            "reason": f'the "bin" field of {package_path}'})
    if ecosystem == "python":
        pyproject = parsed.get("pyproject") or {}
        pyproject_path = parsed.get("pyproject_path")
        where = (pyproject.get("package_where") or {}).get("value")
        roots = [where] if isinstance(where, str) else [item for item in where or [] if isinstance(item, str)]
        seen: set[str] = set()
        for field in (pyproject.get("flit_module"), pyproject.get("name")):
            if not field or not isinstance(field["value"], str):
                continue
            name = field["value"].strip().lower().replace("-", "_").replace(".", "_")
            if name in seen:
                continue
            seen.add(name)
            paths = [f"{root}/{name}/__init__.py" for root in roots]
            paths += [f"src/{name}/__init__.py", f"{name}/__init__.py", f"src/{name}.py", f"{name}.py"]
            options.append({"paths": paths, "source": _cite(pyproject_path, field["line"]),
                            "reason": f"the package named in {pyproject_path}"})
        for name, script in (pyproject.get("scripts") or {}).items():
            base = "/".join(script["value"].split(":")[0].strip().split("."))
            options.append({"paths": [f"src/{base}.py", f"{base}.py", f"src/{base}/__init__.py", f"{base}/__init__.py"],
                            "source": _cite(pyproject_path, script["line"]), "reason": f"the {name} command"})
            break
        options.append({"paths": ["manage.py", "main.py", "app.py", "__main__.py", "src/main.py", "src/app.py"],
                        "source": None, "reason": "a conventional entry file"})
    if ecosystem == "go":
        options.append({"paths": ["main.go", "cmd/main.go"], "source": None, "reason": "the main package"})
    if ecosystem == "rust":
        options.append({"paths": ["src/main.rs", "src/lib.rs"], "source": None, "reason": "the crate root"})
    if fallback:
        options.append({"paths": [fallback], "source": None, "reason": "the most likely entry file"})
    return options


def build_steps(entry_path: str, texts: dict[str, str], exists: Exists) -> tuple[list[dict], str | None]:
    """Reading steps from the entry through the modules it loads.

    A thin entry (``module.exports = require('./lib/express')``) leads to its
    main module first. ``texts`` holds the indexed text of the entry and of
    the files it (or its main module) imports. Returns ``(steps, main_path)``.
    """
    entry_text = texts.get(entry_path, "")
    imports = local_imports(entry_path, entry_text, exists)
    steps: list[dict] = []
    main_path = None
    if _is_thin(entry_text, imports):
        main_path = imports[0]["path"]
        steps.append({"title": "Start at the entry point", "path": entry_path, "line": imports[0]["line"],
                      "detail": f"{entry_path} loads {main_path} on line {imports[0]['line']}."})
        main_text = texts.get(main_path, "")
        exported = None if main_path.endswith(".py") else js_exported_name(main_text)
        line, name = definition_line(main_path, main_text, [exported] if exported else None) or (1, "")
        if exported and name == exported:
            detail = f"Defines {name}, which {entry_path} exports."
        else:
            detail = f"The main module; it starts with {name}." if name else "The main module."
        steps.append({"title": "Read the main module", "path": main_path, "line": line, "detail": detail})
        follow, source_path = local_imports(main_path, main_text, exists), main_path
    else:
        if imports:
            line = imports[0]["line"]
            detail = (f"Loads {imports[0]['path']} on line {line}." if len(imports) == 1
                      else f"Loads {len(imports)} of the project's modules, starting on line {line}.")
        else:
            line = (definition_line(entry_path, entry_text) or (1, ""))[0]
            detail = "The file the project starts from."
        steps.append({"title": "Start at the entry point", "path": entry_path, "line": line, "detail": detail})
        follow, source_path = imports, entry_path
    seen = {step["path"] for step in steps}
    # Import order, with supporting modules (errors, utils, types) last.
    for item in sorted(follow, key=lambda item: is_supporting(item["path"])):
        if len(steps) >= _MAX_STEPS:
            break
        if item["path"] in seen or item["path"] not in texts:
            continue
        seen.add(item["path"])
        found = definition_line(item["path"], texts[item["path"]], item["names"])
        if found is None:
            continue  # only types, constants or a version string: nothing to read in order
        line, name = found
        steps.append({
            "title": f"Then {posixpath.basename(item['path'])}", "path": item["path"], "line": line,
            "detail": f"Imported by {source_path} on line {item['line']}" + (f"; see {name}." if name else "."),
        })
    return steps, main_path


_CODE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java", ".kt", ".php", ".cs")
# Module names that usually hold a package's central object.
_CENTRAL_STEMS = ("app", "application", "applications", "core", "main", "client", "server", "api", "engine", "routing", "router")


def central_modules(rows: list[tuple[str, int]], exclude: set[str], limit: int = 4) -> list[str]:
    """Files of the core folder to read when the entry imports nothing.

    ``rows`` are ``(path, bytes)`` of the folder's own files. Conventional
    central names come first, then the largest modules; package markers,
    tests and supporting modules (errors, utils, types) are left out.
    """
    ranked = []
    for path, size in rows:
        name = posixpath.basename(path)
        stem = name.split(".")[0].lower()
        if (path in exclude or not name.endswith(_CODE_EXTENSIONS) or stem in {"__init__", "index", "__main__", "conftest"}
                or stem.startswith("test_") or ".test." in name or ".spec." in name or is_supporting(path)):
            continue
        central = _CENTRAL_STEMS.index(stem) if stem in _CENTRAL_STEMS else len(_CENTRAL_STEMS)
        ranked.append((central, -int(size or 0), path))
    return [path for _central, _size, path in sorted(ranked)[:limit]]


def _tree_counts(tree: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    stack = list(tree or [])
    while stack:
        node = stack.pop()
        counts[node["path"]] = int(node.get("files") or 0)
        stack.extend(node.get("children") or [])
    return counts


def core_folder(anchor: str | None, tree: list[dict], source: dict | None, noise: frozenset[str] = frozenset()) -> dict | None:
    """The folder holding the main code (the main module's folder), with its file count."""
    counts = _tree_counts(tree)
    folder = posixpath.dirname(anchor) if anchor else ""
    if folder:
        files = counts.get(folder)
        detail = f"{anchor} lives here" + (f", with {files} indexed files." if files else ".")
        return {"path": folder, "files": files, "detail": detail, "source": source}
    # A root-level entry: the largest top-level folder that isn't tests, docs or examples.
    top = [(files, path) for path, files in counts.items() if "/" not in path and path.lower() not in noise and not path.startswith(".")]
    if not top:
        return None
    files, folder = max(top)
    return {"path": folder, "files": files, "detail": f"The largest source folder, with {files} indexed files.", "source": source}


def summary_for(parsed: dict, readme_path: str | None, readme: dict) -> dict | None:
    """The project's one-line description, from its manifest or README."""
    for key, path_key in (("package", "package_path"), ("pyproject", "pyproject_path")):
        description = (parsed.get(key) or {}).get("description")
        if description and isinstance(description["value"], str) and description.get("line"):
            return {"text": _truncate(description["value"], 200), "source": _cite(parsed[path_key], description["line"])}
    summary = readme.get("summary")
    if readme_path and summary:
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", summary["value"])
        return {"text": _truncate(re.sub(r"[*_`]", "", text), 200), "source": _cite(readme_path, summary["line"])}
    return None


# ---------------------------------------------------------------------------
# Reading the index
# ---------------------------------------------------------------------------


async def _indexed_texts(store, repo_id: str, paths: list[str], staging_prefix: str | None, rebuild) -> dict[str, str]:
    """The indexed text of each path (its first sections), rebuilt from chunks."""
    texts: dict[str, str] = {}
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    for path in dict.fromkeys(paths):
        rows = await store.fetch_all(
            f"SELECT start_line, end_line, content FROM chunks WHERE repo_id = ? AND file_path = ?{stage_sql} "
            "ORDER BY start_line LIMIT ?",
            [repo_id, path, *stage_args, _FILE_ROWS],
        )
        if rows:
            texts[path] = rebuild(rows)[0]
    return texts


def _reading_order(imports: list[dict]) -> list[dict]:
    """The imports ``build_steps`` may use, in the order it considers them."""
    return sorted(imports, key=lambda item: is_supporting(item["path"]))[:_FOLLOW_READS]


async def gather_start_here(store, repo: dict, repo_map: dict, facts: dict | None) -> dict | None:
    """The "Start here" card for an indexed repository, cached per index version.

    Reads the root manifests and README, the entry file, and the modules on
    the reading path: a handful of small queries, no model call.
    """
    # Shared helpers of the overview endpoint (imported here: that module imports this one).
    from api.explore_router import _NOISE_DIRS, _published_paths, reconstruct_file_from_rows

    repo_id = str(repo["id"])
    staging = repo.get("staging_prefix")
    version = (repo.get("chunk_count"), repo.get("commit_sha"), repo.get("indexed_at"), staging, repo.get("is_indexing"))
    cached = _CACHE.get(repo_id)
    now = time.monotonic()
    if cached and cached[1] == version and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[2]

    async def read(paths: list[str]) -> dict[str, str]:
        return await _indexed_texts(store, repo_id, paths, staging, reconstruct_file_from_rows)

    root_files = list(repo_map.get("root_files") or [])
    texts = await read([path for path in root_files if path.lower() in _MANIFESTS or path.lower() in _README_NAMES])
    manifests = {path: text for path, text in texts.items() if path.lower() in _MANIFESTS}
    readme_path = next((path for name in _README_NAMES for path in texts if path.lower() == name), None)
    readme = readme_facts(texts[readme_path]) if readme_path else {}
    parsed = parse_manifests(manifests)
    ecosystem = detect_ecosystem(manifests, repo_map.get("extensions") or {})

    options = entry_candidates(ecosystem, parsed, (facts or {}).get("entry"))
    known = set(await _published_paths(store, repo_id, [path for option in options for path in option["paths"]], staging))
    entry = None
    for option in options:
        path = next((candidate for candidate in option["paths"] if candidate in known), None)
        if path:
            entry = {**option, "path": path}
            break

    def exists(path: str) -> bool:
        return path in known

    steps: list[dict] = []
    main_path = None
    if entry:
        texts.update(await read([entry["path"]]))
        entry_text = texts.get(entry["path"], "")
        known.update(await _published_paths(store, repo_id, import_candidates(entry["path"], entry_text), staging))
        imports = local_imports(entry["path"], entry_text, exists)
        texts.update(await read([item["path"] for item in _reading_order(imports) if item["path"] not in texts]))
        if _is_thin(entry_text, imports):
            main = imports[0]["path"]
            known.update(await _published_paths(store, repo_id, import_candidates(main, texts.get(main, "")), staging))
            follow = _reading_order(local_imports(main, texts.get(main, ""), exists))
            texts.update(await read([item["path"] for item in follow if item["path"] not in texts]))
        steps, main_path = build_steps(entry["path"], texts, exists)
        folder = posixpath.dirname(entry["path"])
        if not imports and folder:
            # An entry that imports nothing (``__version__`` only): read the
            # folder's central modules instead.
            stage_sql, stage_args = ("", []) if not staging else (" AND c.id NOT LIKE ?", [f"{staging}%"])
            rows = await store.fetch_all(
                "SELECT f.file_path, f.byte_size FROM repo_files f WHERE f.repo_id = ? AND f.file_path LIKE ? "
                "AND f.file_path NOT LIKE ? AND EXISTS (SELECT 1 FROM chunks c WHERE c.repo_id = f.repo_id "
                f"AND c.file_path = f.file_path{stage_sql}) LIMIT 400",
                [repo_id, f"{folder}/%", f"{folder}/%/%", *stage_args],
            )
            modules = central_modules([(str(row["file_path"]), int(row.get("byte_size") or 0)) for row in rows],
                                      {step["path"] for step in steps}, _MAX_STEPS - len(steps))
            texts.update(await read(modules))
            for path in modules:
                found = definition_line(path, texts.get(path, ""))
                if found:
                    steps.append({"title": f"Then {posixpath.basename(path)}", "path": path, "line": found[0],
                                  "detail": f"One of the main modules in {folder}/; see {found[1]}."})

    commands = build_commands(ecosystem, manifests, parsed, root_files)
    if not any(command["kind"] == "run" for command in commands) and readme_path:
        known.update(await _published_paths(store, repo_id, readme_run_paths(readme), staging))
        run = readme_run_command(readme_path, readme, exists)
        if run:
            commands.append(run)

    # Fewer than three steps: finish with files a newcomer opens anyway.
    if entry and len(steps) < _MIN_STEPS:
        extras = [((facts or {}).get("schema") or {}).get("path"), (facts or {}).get("auth"), *((facts or {}).get("test_configs") or [])]
        for path in extras:
            if path and len(steps) < _MIN_STEPS and path not in {step["path"] for step in steps}:
                steps.append({"title": f"Then {posixpath.basename(path)}", "path": path, "line": 1,
                              "detail": "A file newcomers usually need early."})
        if len(steps) < _MIN_STEPS and readme_path and readme_path not in {step["path"] for step in steps}:
            steps.append({"title": "Skim the README", "path": readme_path, "line": 1,
                          "detail": "What the project is for, in its own words."})

    result = None
    if entry or commands:
        entry_view = None
        if entry:
            first = steps[0]
            entry_view = {
                "path": entry["path"],
                # The module a thin entry hands over to (index.js -> lib/express.js).
                "main": main_path,
                "line": first["line"],
                "detail": (f"Found through {entry['reason']}. " if entry["source"] else "") + first["detail"],
                "source": entry["source"] or _cite(entry["path"], first["line"]),
            }
        core = None
        if entry:
            # Cited to the line that leads into the folder (the entry's import of the main module).
            core_source = _cite(steps[0]["path"], steps[0]["line"]) if main_path else entry_view["source"]
            core = core_folder(main_path or entry["path"], repo_map.get("tree") or [], core_source, _NOISE_DIRS)
        result = {
            "summary": summary_for(parsed, readme_path, readme),
            "ecosystem": ecosystem,
            "entry": entry_view,
            "core": core,
            "commands": commands,
            "steps": steps[:_MAX_STEPS],
            "guides": readme_guides(readme_path, readme),
        }
    _CACHE[repo_id] = (now, version, result)
    if len(_CACHE) > 256:
        oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest, None)
    return result
