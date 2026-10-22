"""Repository exploration: overview, instant code search, and file viewing."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, OrderedDict
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from agent.code_edit import CodeEditValidationError, normalise_repository_path
from agent.editor import language_for, reconstruct_indexed_file
from api.auth import get_current_user
from api.repository_state import fetch_owned_repository, public_view, raise_storage_error
from config import settings
from database import assert_turso_schema, get_turso_store
from github.github_client import GitHubAPIError, fetch_raw_file
from ingest.chunker import LONG_LINE_OVERLAP
from ingest.cloner import RepositoryValidationError, parse_github_repository
from retrieval.retriever import build_repo_map, search_code

logger = logging.getLogger(__name__)
router = APIRouter()

_FILE_VIEW_MAX_BYTES = 1_500_000
# The index fallback reads chunk rows in pages (one huge query exceeded the
# database response limit) and stops at this much text, saying so.
_INDEX_VIEW_PAGE_ROWS = 100
_INDEX_VIEW_MAX_CHARS = 1_500_000
_FRAGMENT_OVERLAP = LONG_LINE_OVERLAP

KEY_FILE_NAMES = {
    "readme.md", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml",
    "build.gradle", "dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile", "turbo.json",
    "nx.json", "vercel.json", "render.yaml", "tsconfig.json", "contributing.md", "next.config.js",
    "next.config.mjs", "next.config.ts", "vite.config.ts", "vite.config.js", "schema.prisma",
}

_MANIFEST_NAMES = frozenset({
    "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
})

LANGUAGE_LABELS = {
    "ts": "TypeScript", "tsx": "TypeScript", "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript",
    "cjs": "JavaScript", "py": "Python", "go": "Go", "rs": "Rust", "java": "Java", "kt": "Kotlin", "rb": "Ruby",
    "php": "PHP", "cs": "C#", "swift": "Swift", "c": "C", "h": "C", "cpp": "C++", "hpp": "C++", "sql": "SQL",
    "css": "CSS", "scss": "SCSS", "html": "HTML", "vue": "Vue", "svelte": "Svelte", "md": "Markdown",
    "mdx": "MDX", "json": "JSON", "yml": "YAML", "yaml": "YAML", "toml": "TOML", "prisma": "Prisma",
    "graphql": "GraphQL", "sh": "Shell", "dart": "Dart", "lua": "Lua", "ex": "Elixir", "exs": "Elixir",
}


def _join_overlapping(left: str, right: str) -> str | None:
    """``left + right`` where ``right`` starts with the end of ``left``."""
    if not left or not right:
        return None
    lowest = max(0, len(left) - len(right))
    for size in (_FRAGMENT_OVERLAP, min(len(left), len(right), 200)):
        if size <= 0 or size > len(left) or size > len(right):
            continue
        head = right[:size]
        position = left.rfind(head, lowest)
        attempts = 0
        while position != -1 and attempts < 64:
            if right.startswith(left[position:]):
                return left[:position] + right
            attempts += 1
            position = left.rfind(head, lowest, position + size - 1)
    return None


def _merge_line_variants(variants: list[str]) -> tuple[str, bool]:
    """One line's text from every chunk that holds (part of) it.

    Line-window chunks repeat identical lines in their overlap. An over-long
    line is stored as overlapping character fragments, and indexes built
    before line-aligned chunking cut lines at arbitrary offsets; those pieces
    are stitched by their overlap. Returns ``(text, exact)``.
    """
    merged = variants[0]
    exact = True
    for variant in variants[1:]:
        if variant == merged:
            continue
        if (
            len(merged) >= _FRAGMENT_OVERLAP and len(variant) >= _FRAGMENT_OVERLAP
            and merged[-_FRAGMENT_OVERLAP:] == variant[:_FRAGMENT_OVERLAP]
        ):
            # The next fragment of an over-long line (rows arrive in order).
            merged = merged[:-_FRAGMENT_OVERLAP] + variant
            continue
        if variant in merged:
            continue
        if merged in variant:
            merged = variant
            continue
        joined = _join_overlapping(merged, variant) or _join_overlapping(variant, merged)
        if joined is None:
            exact = False
            merged = max(merged, variant, key=len)
        else:
            merged = joined
    return merged, exact


def reconstruct_file_from_rows(rows: list[dict]) -> tuple[str, int]:
    """Rebuild a file from chunk rows ordered by (start_line, insertion order).

    Returns ``(content, inexact_lines)``; gaps (whitespace-only regions the
    chunker dropped) become blank lines so later line numbers stay aligned.
    """
    variants: dict[int, list[str]] = {}
    for row in rows:
        start = int(row.get("start_line") or 1)
        end = int(row.get("end_line") or start)
        pieces = str(row.get("content") or "").split("\n")
        # Legacy character windows could end with the newline that closes
        # their last line; never invent a line past the reported range.
        for offset, line in enumerate(pieces[: max(1, end - start + 1)]):
            variants.setdefault(start + offset, []).append(line)
    if not variants:
        return "", 0
    inexact = 0
    lines: dict[int, str] = {}
    for number, texts in variants.items():
        lines[number], exact = _merge_line_variants(texts)
        inexact += 0 if exact else 1
    last = max(lines)
    return "\n".join(lines.get(number, "") for number in range(1, last + 1)) + "\n", inexact


async def _read_file_from_index(store, repo_id: str, path: str, staging_prefix: str | None) -> dict:
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    totals = await store.fetch_one(
        f"SELECT MAX(end_line) AS last_line FROM chunks WHERE repo_id = ? AND file_path = ?{stage_sql}",
        [repo_id, path, *stage_args],
    ) or {}
    total_lines = int(totals.get("last_line") or 0)
    manifest = await store.fetch_one(
        "SELECT byte_size FROM repo_files WHERE repo_id = ? AND file_path = ?", [repo_id, path],
    ) or {}
    byte_size = manifest.get("byte_size")
    rows: list[dict] = []
    characters = 0
    truncated = False
    cursor_line, cursor_end, cursor_row = 0, 0, 0
    while True:
        page = await store.fetch_all(
            "SELECT rowid AS row_order, start_line, end_line, content FROM chunks "
            f"WHERE repo_id = ? AND file_path = ?{stage_sql} "
            "AND (start_line > ? OR (start_line = ? AND (end_line > ? OR (end_line = ? AND rowid > ?)))) "
            "ORDER BY start_line, end_line, rowid LIMIT ?",
            [repo_id, path, *stage_args, cursor_line, cursor_line, cursor_end, cursor_end, cursor_row,
             _INDEX_VIEW_PAGE_ROWS],
        )
        for row in page:
            characters += len(str(row.get("content") or ""))
            if characters > _INDEX_VIEW_MAX_CHARS and rows:
                truncated = True
                break
            rows.append(row)
        if truncated or len(page) < _INDEX_VIEW_PAGE_ROWS:
            break
        cursor_line, cursor_end, cursor_row = (
            int(page[-1]["start_line"]), int(page[-1]["end_line"]), int(page[-1]["row_order"]),
        )
    content, inexact_lines = reconstruct_file_from_rows(rows)
    byte_exact = False
    if content and not truncated:
        # Lines are stitched here (fragments, legacy windows); the shared
        # editor helper then restores the final newline / CRLF endings from
        # the manifest byte size and says whether the result is byte-exact.
        rebuilt, byte_exact = reconstruct_indexed_file(
            [{"file_path": path, "start_line": 1, "end_line": content.count("\n"), "content": content[:-1]}],
            path, int(byte_size) if byte_size is not None else None,
        )
        content = rebuilt or content
    if truncated:
        # Drop the last, possibly partial, line of the covered range.
        covered = max((int(row.get("end_line") or 0) for row in rows), default=0)
        content = "\n".join(content.split("\n")[: max(0, covered - 1)]) + ("\n" if covered > 1 else "")
    return {
        "content": content, "truncated": truncated, "total_lines": total_lines, "inexact_lines": inexact_lines,
        "exact": byte_exact and inexact_lines == 0 and not truncated,
    }


def _handle(error: Exception, action: str):
    """``action`` completes "Couldn't ..." in the message the user sees."""
    raise_storage_error(error, action, logger)


# ---------------------------------------------------------------------------
# Repository-specific starter questions
# ---------------------------------------------------------------------------
#
# Suggestions name real files and models of *this* repository, chosen from
# the indexed manifest and checked against the published index, so every
# file a suggestion mentions can be opened and cited. No model call is made.

MAX_SUGGESTED_QUESTIONS = 6
# Files whose names say what they are; one bounded query fetches only these.
_FACT_PATH_PATTERNS = (
    "%.prisma", "%models.py", "%/models/%", "%schema.%", "%init.sql", "%.entity.ts", "%.entity.js",
    "%router.%", "%/routes/%", "%routes.%", "%controller%", "%/handlers/%", "%handler.go", "%views.py",
    "%urls.py", "%/api/%route.%", "%pages/api/%", "%/endpoints/%", "%/routers/%.py",
    "%main.%", "%app.%", "%server.%", "%manage.py", "%/cmd/%", "%lib.rs", "%application.%", "%program.cs",
    "src/index.%", "%/src/index.%", "%auth%", "%login%", "%session%",
    "%.config.%", "%conftest.py", "%pytest.ini", "%phpunit.xml", "%karma.conf%", "readme%",
)
_FACTS_MAX_PATHS = 4000
_FACTS_TTL_SECONDS = 600.0
_FACTS_CACHE: dict[str, tuple[float, tuple, dict]] = {}
_SCHEMA_MAX_ROWS = 80

# Folders whose files are rarely what a newcomer means by "the code".
_NOISE_DIRS = frozenset({
    "test", "tests", "__tests__", "__mocks__", "mocks", "mock", "fixtures", "fixture", "__fixtures__", "e2e",
    "spec", "specs", "example", "examples", "example-apps", "sample", "samples", "docs", "doc", "documentation",
    "vendor", "third_party", "third-party", "node_modules", "dist", "build", "out", "playground", "demo", "demos",
    "stories", "storybook", "templates", "scripts", "benchmarks", "benchmark", "testdata", "testing",
    "migrations", "seeds", "seed", "generated", "__generated__", ".github", "github", "deploy", "infra",
})
_GENERIC_MODELS = frozenset({
    "user", "account", "session", "verificationtoken", "team", "membership", "member", "organization",
    "organisation", "org", "profile", "credential", "apikey", "role", "permission", "setting", "token",
    "password", "log", "auditlog", "migration", "base", "model", "basemodel", "abstractmodel", "timestampedmodel",
    "meta", "feature", "flag", "tag", "attribute", "config", "configuration",
})
_SCHEMA_KINDS = {
    # kind: (definition pattern, noun used in questions)
    "prisma": (re.compile(r"(?m)^model\s+([A-Za-z_]\w*)\s*\{"), "model"),
    "python": (re.compile(r"(?m)^class\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*:"), "model"),
    "rails": (re.compile(r"create_table\s+[\"']([A-Za-z_]\w*)[\"']"), "table"),
    "sql": (re.compile(
        r"(?i)\bcreate\s+(?:virtual\s+)?table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(?:\w+[`\"\]]?\.[`\"\[]?)?([A-Za-z_]\w*)"
    ), "table"),
    "drizzle": (re.compile(r"export\s+const\s+([A-Za-z_]\w*)\s*=\s*\w*(?:pg|mysql|sqlite)Table\s*\("), "table"),
}
_PYTHON_MODEL_BASE = re.compile(r"Model|Base\b|Base\)|Document|SQLModel|Table", re.IGNORECASE)
_TEST_CONFIG_NAMES = (
    ("playwright.config.", 1), ("vitest.config.", 2), ("vitest.workspace.", 3), ("jest.config.", 2),
    ("cypress.config.", 1), ("pytest.ini", 2), ("conftest.py", 3), ("karma.conf.", 3), ("phpunit.xml", 2),
)
_ENTRY_NAMES = {
    "main.py": 10, "__main__.py": 8, "app.py": 9, "manage.py": 8, "server.py": 8, "asgi.py": 5, "wsgi.py": 5,
    "main.ts": 9, "main.tsx": 7, "main.js": 7, "server.ts": 8, "server.js": 8, "app.ts": 6, "app.js": 6,
    "index.ts": 5, "index.js": 5, "main.go": 10, "main.rs": 10, "lib.rs": 6, "app.module.ts": 6,
    "program.cs": 9, "application.java": 9, "application.kt": 9,
}
_ENTRY_DIRS = frozenset({"src", "app", "backend", "server", "api", "cmd"})
_AUTH_DIRS = frozenset({"auth", "authentication", "authn", "oauth", "login", "sessions", "identity", "security"})
_AUTH_WORDS = ("auth", "login", "signin", "sign-in", "session", "passport", "jwt")
_DATED_SEGMENT = re.compile(r"/(?:\d{4}-\d{2}-\d{2}|v\d+(?:[._]\d+)*)(?:/|$)")


def _is_noise(path: str) -> bool:
    parts = path.lower().split("/")
    if any(part in _NOISE_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return (
        ".test." in name or ".spec." in name or name.endswith(("_test.go", "_test.py", ".d.ts")) or name.startswith("test_")
        or ".stories." in name or "mock" in name or name.startswith(".")
    )


def _normalise_word(word: str) -> str:
    """``EventType``, ``eventTypes`` and ``event_types`` all become ``eventtype``."""
    text = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(text) > 4 and text.endswith("ies"):
        return text[:-3] + "y"
    if len(text) > 4 and text.endswith(("sses", "xes")):
        return text[:-2]
    if len(text) > 3 and text.endswith("s") and not text.endswith("ss"):
        return text[:-1]
    return text


def _depth(path: str) -> int:
    return path.count("/")


def _router_info(path: str) -> dict | None:
    """What kind of request handler a path is, or None."""
    low = path.lower()
    parts = path.split("/")
    name = parts[-1]
    lname = name.lower()
    parent = parts[-2] if len(parts) > 1 else ""
    stem, _, ext = lname.partition(".")
    if "trpc" in low and ext in {"ts", "tsx", "js"} and stem in {"_router", "router", "_app"}:
        subject = "" if stem == "_app" or parent.lower() in {"routers", "router", "server", "trpc"} else parent
        return {"style": "trpc", "subject": subject, "weight": 3.0}
    if lname.endswith((".controller.ts", ".controller.js")) and {"api", "controllers", "modules", "src"} & set(low.split("/")[:-1]):
        return {"style": "controller", "subject": name.split(".")[0], "weight": 3.0}
    route = re.search(r"(?:^|/)app/api/(.+)/route\.[jt]sx?$", low) or re.search(r"(?:^|/)pages/api/(.+)\.[jt]sx?$", low)
    if route:
        segments = [segment for segment in route.group(1).split("/") if segment and not segment.startswith(("[", "("))]
        return {"style": "api-route", "subject": segments[-1] if segments else "", "weight": 1.0}
    if ext == "py":
        if stem in {"__init__", "base", "deps", "dependencies", "utils", "schemas", "models", "conftest"}:
            return None
        if "router" in stem or stem in {"routes", "views", "urls", "api", "endpoints"} or parent.lower() in {
            "routes", "routers", "endpoints", "views",
        }:
            subject = re.sub(r"_?(?:router|routes|views|endpoints|api)$", "", stem)
            if not subject or subject in {"urls"}:
                subject = "" if parent.lower() in {"", "api", "app", "src"} else parent
            return {"style": "python", "subject": subject, "weight": 3.0}
        return None
    if ext in {"js", "ts", "mjs", "cjs"} and (parent.lower() == "routes" or stem in {"routes", "router"}):
        return {"style": "routes", "subject": "" if stem in {"index", "routes", "router"} else stem, "weight": 2.0}
    if ext == "go" and (parent.lower() == "handlers" or stem.endswith("handler") or stem in {"routes", "router"}):
        return {"style": "go", "subject": re.sub(r"_?handlers?$", "", stem) if parent.lower() == "handlers" else "", "weight": 2.0}
    if low.endswith("config/routes.rb"):
        return {"style": "rails-routes", "subject": "", "weight": 3.0}
    if lname.endswith("_controller.rb"):
        return {"style": "controller", "subject": name[: -len("_controller.rb")], "weight": 3.0}
    if lname.endswith(("controller.java", "controller.kt", "controller.php")) and lname != "controller.php":
        return {"style": "controller", "subject": re.sub(r"controller\.\w+$", "", name, flags=re.IGNORECASE), "weight": 2.5}
    return None


def _schema_kind(path: str, byte_size: int) -> tuple[str, float] | None:
    """(kind, score) when ``path`` holds data-model definitions."""
    low = path.lower()
    parts = low.split("/")
    name = parts[-1]
    parent = parts[-2] if len(parts) > 1 else ""
    if name.endswith(".prisma"):
        return "prisma", 100.0 + (5 if name == "schema.prisma" else 0)
    if name == "schema.rb" and parent == "db":
        return "rails", 95.0
    if name in {"schema.ts", "schema.js"} and parent in {"db", "database", "drizzle", "schema"}:
        return "drizzle", 75.0
    if name == "models.py":
        return "python", 80.0 + min(byte_size / 10_000, 10)
    if name.endswith(".sql") and ("schema" in name or name.endswith("init.sql")):
        return "sql", 70.0
    if name.endswith((".entity.ts", ".entity.js")):
        return "entity", 60.0
    if "models" in parts[:-1] and name.endswith((".py", ".ts", ".js", ".rb", ".go")) and not name.startswith(("index.", "__init__")):
        return "model-file", 50.0
    return None


def classify_repository_paths(rows: list[tuple[str, int]]) -> dict:
    """Pick the files a newcomer asks about first (schema, API, auth, tests...).

    ``rows`` are ``(path, byte_size)`` of indexed files. Everything returned is
    one of those paths; nothing is guessed.
    """
    schema: list[tuple[float, str, str]] = []
    model_files: list[dict] = []
    routers: list[dict] = []
    entries: list[tuple[float, str]] = []
    auth: list[tuple[float, str]] = []
    tests: list[tuple[int, int, str]] = []
    readme = None
    for path, byte_size in rows:
        if not path:
            continue
        low = path.lower()
        name = low.rsplit("/", 1)[-1]
        if "/" not in path and name.startswith("readme") and (readme is None or name == "readme.md"):
            readme = path
        if _is_noise(path):
            continue
        depth = _depth(path)
        found = _schema_kind(path, int(byte_size or 0))
        if found:
            kind, score = found
            if kind in {"entity", "model-file"}:
                stem = path.rsplit("/", 1)[-1].split(".")[0]
                if stem and not stem.lower().startswith(("base", "abstract")):
                    model_files.append({"path": path, "name": stem[:1].upper() + stem[1:], "kind": kind})
            else:
                schema.append((score - depth * 0.5, path, kind))
        info = _router_info(path)
        if info:
            dated = 1.0 if _DATED_SEGMENT.search("/" + low) else 0.0
            routers.append({**info, "path": path, "score": info["weight"] - depth * 0.5 - dated})
        if name in _ENTRY_NAMES and depth <= 4:
            segments = low.split("/")
            parent = segments[-2] if depth else ""
            if not (name in {"index.ts", "index.js"} and parent != "src"):
                score = _ENTRY_NAMES[name] - depth
                # Applications start in src/, app/, backend/, server/ or cmd/<name>/;
                # files of the same name inside shared packages are library code.
                if depth == 0 or parent in _ENTRY_DIRS or (depth >= 2 and segments[-3] == "cmd"):
                    score += 3
                if segments[0] in {"packages", "libs", "lib"}:
                    score -= 4
                entries.append((score, path))
        parents = set(low.split("/")[:-1])
        in_auth_dir = bool(parents & _AUTH_DIRS)
        named_auth = any(word in name for word in _AUTH_WORDS)
        if (in_auth_dir or named_auth) and name.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rb", ".java", ".kt", ".php", ".rs")):
            score = 3.0 * in_auth_dir + 2.0 * named_auth - depth * 0.3
            if any(word in name for word in ("option", "config", "provider", "middleware", "strategy", "service")):
                score += 1.0
            if "next-auth" in name or "nextauth" in name or name.split(".")[0] in {"auth", "authentication", "login", "session"}:
                score += 1.0
            if name.startswith(("_router.", "router.", "index.", "types.", "constants.")):
                score -= 2.0
            auth.append((score, path))
        for prefix, rank in _TEST_CONFIG_NAMES:
            if name.startswith(prefix):
                tests.append((depth, rank, path))
                break
    schema.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    routers.sort(key=lambda item: (-item["score"], len(item["path"]), item["path"]))
    entries.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    auth.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    tests.sort()
    test_configs: list[str] = []
    tools: set[str] = set()
    for _depth_, _rank, path in tests:
        tool = path.lower().rsplit("/", 1)[-1].split(".")[0]
        if tool in tools or len(test_configs) >= 2 or (test_configs and _depth(path) > _depth(test_configs[0])):
            continue
        tools.add(tool)
        test_configs.append(path)
    schemas = [{"path": path, "kind": kind} for _score, path, kind in schema[:5]]
    entry_paths = [path for _score, path in entries[:5]]
    auth_paths = [path for score, path in auth[:5] if score > 2.0]
    return {
        "readme": readme,
        "schema": schemas[0] if schemas else None,
        "model_files": sorted(model_files, key=lambda item: (len(item["path"]), item["path"]))[:200],
        "routers": routers[:200],
        "entry": entry_paths[0] if entry_paths else None,
        "auth": auth_paths[0] if auth_paths else None,
        "test_configs": test_configs,
        # Ranked alternatives, used when a first choice is not published.
        "candidates": {"schema": schemas, "entry": entry_paths, "auth": auth_paths},
    }


def rank_models(text: str, kind: str) -> list[dict]:
    """Model/table names defined in ``text``, domain models first.

    Centrality is how often other definitions mention a name; generic account
    tables (User, Session, ...) go last because they say little about what the
    project does.
    """
    pattern, _noun = _SCHEMA_KINDS[kind]
    names: list[str] = []
    lines: dict[str, int] = {}
    for match in pattern.finditer(text):
        if kind == "python" and not _PYTHON_MODEL_BASE.search(match.group(2) or ""):
            continue
        name = match.group(1)
        if name in lines or name.lower() in {"if", "meta"}:
            continue
        names.append(name)
        lines[name] = text.count("\n", 0, match.start()) + 1
    if not names:
        return []
    words = Counter(re.findall(r"[A-Za-z_]\w*", text))
    ranked = [
        {"name": name, "line": lines[name], "references": max(0, words[name] - 1),
         "generic": _normalise_word(name) in _GENERIC_MODELS}
        for name in names
    ]
    ranked.sort(key=lambda item: (item["generic"], -item["references"], item["line"]))
    return ranked


async def _schema_models(store, repo_id: str, schema: dict, staging_prefix: str | None) -> list[dict]:
    """Rank the models of the schema file and note which can be cited by name.

    A question that names a file is answered from that file's first
    ``retrieval_top_k`` sections, so a model defined further down is only
    suggested without the file path.
    """
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    rows = await store.fetch_all(
        f"SELECT start_line, end_line, content FROM chunks WHERE repo_id = ? AND file_path = ?{stage_sql} "
        "ORDER BY start_line LIMIT ?",
        [repo_id, schema["path"], *stage_args, _SCHEMA_MAX_ROWS],
    )
    if not rows:
        return []
    content, _inexact = reconstruct_file_from_rows(rows)
    models = rank_models(content, schema["kind"])
    citable_rows = rows[: max(1, int(settings.retrieval_top_k))]
    for model in models:
        model["in_named_file_context"] = any(
            int(row.get("start_line") or 0) <= model["line"] <= int(row.get("end_line") or 0) for row in citable_rows
        )
    return models


async def _published_paths(store, repo_id: str, paths: list[str], staging_prefix: str | None) -> set[str]:
    wanted = list(dict.fromkeys(path for path in paths if path))[:400]
    if not wanted:
        return set()
    found: set[str] = set()
    stage_sql, stage_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    for offset in range(0, len(wanted), 100):
        window = wanted[offset: offset + 100]
        rows = await store.fetch_all(
            f"SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND file_path IN ({', '.join('?' for _ in window)})"
            f"{stage_sql}",
            [repo_id, *window, *stage_args],
        )
        found.update(str(row["file_path"]) for row in rows)
    return found


async def gather_question_facts(store, repo: dict) -> dict:
    """Facts about the repository's real files for starter questions (cached)."""
    repo_id = str(repo["id"])
    staging = repo.get("staging_prefix")
    version = (repo.get("chunk_count"), repo.get("commit_sha"), repo.get("indexed_at"), staging)
    cached = _FACTS_CACHE.get(repo_id)
    now = time.monotonic()
    if cached and cached[1] == version and now - cached[0] < _FACTS_TTL_SECONDS:
        return cached[2]
    # LIKE is case-insensitive for ASCII; lower() here would double the cost.
    rows = await store.fetch_all(
        "SELECT file_path, byte_size FROM repo_files WHERE repo_id = ? AND ("
        + " OR ".join("file_path LIKE ?" for _ in _FACT_PATH_PATTERNS)
        + ") ORDER BY length(file_path), file_path LIMIT ?",
        [repo_id, *_FACT_PATH_PATTERNS, _FACTS_MAX_PATHS],
    )
    facts = classify_repository_paths([(str(row.get("file_path") or ""), int(row.get("byte_size") or 0)) for row in rows])
    ranked = facts.pop("candidates")
    candidates = [
        facts["readme"], *[item["path"] for item in ranked["schema"]], *ranked["entry"], *ranked["auth"],
        *facts["test_configs"], *[item["path"] for item in facts["routers"]],
        *[item["path"] for item in facts["model_files"]],
    ]
    # Only files with published sections can be opened and cited; the manifest
    # can list files whose sections are missing or still hidden by a refresh.
    published = await _published_paths(store, repo_id, [path for path in candidates if path], staging)
    facts["readme"] = facts["readme"] if facts["readme"] in published else None
    facts["schema"] = next((item for item in ranked["schema"] if item["path"] in published), None)
    facts["entry"] = next((path for path in ranked["entry"] if path in published), None)
    facts["auth"] = next((path for path in ranked["auth"] if path in published), None)
    facts["test_configs"] = [path for path in facts["test_configs"] if path in published]
    facts["routers"] = [item for item in facts["routers"] if item["path"] in published]
    facts["model_files"] = [item for item in facts["model_files"] if item["path"] in published]
    facts["models"] = await _schema_models(store, repo_id, facts["schema"], staging) if facts["schema"] else []
    _FACTS_CACHE[repo_id] = (now, version, facts)
    if len(_FACTS_CACHE) > 256:
        oldest = min(_FACTS_CACHE, key=lambda key: _FACTS_CACHE[key][0])
        _FACTS_CACHE.pop(oldest, None)
    return facts


def _top_directories(repo_map: dict) -> list[str]:
    """Top-level source folders that hold a real share of the files."""
    tree = repo_map.get("tree") or []
    total = sum(int(node.get("files") or 0) for node in tree) or 1
    folders = [
        node["path"] for node in tree
        if node.get("path") and node["path"].lower() not in _NOISE_DIRS and int(node.get("files") or 0) / total >= 0.05
    ]
    return folders[:3]


def _join_names(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _router_question(router: dict) -> str:
    path, subject, style = router["path"], router["subject"], router["style"]
    if style == "trpc":
        if subject:
            return f"What procedures does the {subject} tRPC router in {path} expose, and where are their handlers?"
        return f"How is the tRPC API in {path} organized, and where do the procedure handlers live?"
    if style == "api-route":
        return f"How does the API route in {path} handle a request?"
    if style == "rails-routes":
        return f"Which routes does {path} define, and which controllers handle them?"
    if style == "go":
        return f"How does {path} handle an incoming request?"
    noun = "routes" if style == "routes" else "endpoints"
    return f"What {noun} does {path} define, and how is a request handled?"


def _pick_router(routers: list[dict], models: list[dict], avoid: str | None) -> tuple[dict | None, dict | None]:
    """The best request handler, preferring one about a central domain model."""
    if not routers:
        return None, None
    domain = [model for model in models if not model["generic"]][:5]
    for model in sorted(domain, key=lambda item: item["name"] == avoid):
        wanted = _normalise_word(model["name"])
        for router in routers:
            if router["subject"] and _normalise_word(router["subject"]) == wanted:
                return router, model
    return routers[0], None


def suggest_question_details(repo_map: dict, facts: dict | None = None) -> list[dict]:
    """Up to six varied starter questions (overview, where, how, change).

    Each is ``{"question", "kind", "paths"}``; ``paths`` are the indexed files
    the question names (empty when it names none).
    """
    facts = facts or {}
    questions: list[dict] = []

    def add(question: str, kind: str, paths: list[str] | None = None) -> None:
        if question not in {item["question"] for item in questions}:
            questions.append({"question": question, "kind": kind, "paths": [path for path in (paths or []) if path]})

    folders = _top_directories(repo_map)
    where = ""
    if len(folders) >= 2:
        where = " across " + _join_names([f"{folder}/" for folder in folders])
    elif folders:
        where = f" in {folders[0]}/"
    readme = facts.get("readme")
    if readme:
        add(f"What is this project, according to {readme}, and how is the code organized{where}?", "overview", [readme])
    else:
        add(f"What is this project, and how is the code organized{where}?", "overview")

    schema = facts.get("schema")
    models = facts.get("models") or []
    noun = _SCHEMA_KINDS[schema["kind"]][1] if schema and schema.get("kind") in _SCHEMA_KINDS else "model"
    domain = [model for model in models if not model["generic"]] or models
    where_model = None
    if schema and domain:
        named = [model for model in domain[:4] if model.get("in_named_file_context")]
        where_model = named[0] if named else domain[0]
        if where_model.get("in_named_file_context"):
            add(f"Where is the {where_model['name']} {noun} defined in {schema['path']}, and which {noun}s reference it?",
                "where", [schema["path"]])
        else:
            add(f"Where is the {where_model['name']} {noun} defined, and which {noun}s reference it?", "where")
    elif schema:
        add(f"What does the data model in {schema['path']} look like?", "where", [schema["path"]])
    elif facts.get("model_files"):
        files = facts["model_files"]
        chosen = next((item for item in files if _normalise_word(item["name"]) not in _GENERIC_MODELS), files[0])
        where_model = {"name": chosen["name"], "generic": False}
        add(f"What does the {chosen['name']} model in {chosen['path']} store, and what refers to it?", "where", [chosen["path"]])

    router, router_model = _pick_router(facts.get("routers") or [], models, where_model["name"] if where_model else None)
    if router:
        add(_router_question(router), "how", [router["path"]])

    change_model = router_model or (domain[1] if len(domain) > 1 and domain[0] is where_model else None) or where_model
    if change_model:
        add(f"Which files would I need to change to add a new field to the {change_model['name']} {noun}?", "change")
    elif router and router["subject"]:
        add(f"Which files would I need to change to add a new endpoint to the {router['subject']} API?", "change")

    if facts.get("auth"):
        add(f"How does authentication work, starting from {facts['auth']}?", "how", [facts["auth"]])
    configs = facts.get("test_configs") or []
    if configs:
        add(f"How are tests configured in {_join_names(configs)}, and how do I run them?", "how", configs)
    if facts.get("entry"):
        add(f"What does {facts['entry']} set up when the application starts?", "how", [facts["entry"]])

    if len(questions) < MAX_SUGGESTED_QUESTIONS:
        for question, kind in _generic_questions(repo_map):
            if len(questions) >= MAX_SUGGESTED_QUESTIONS:
                break
            covered = {item["kind"] for item in questions}
            if kind == "change" and "change" in covered:
                continue
            add(question, kind)
    return questions[:MAX_SUGGESTED_QUESTIONS]


def _generic_questions(repo_map: dict) -> list[tuple[str, str]]:
    """Folder-name based questions for repositories without telling file names."""
    names: set[str] = set()
    for node in repo_map.get("tree") or []:
        for path in [node["path"], *[child["path"] for child in node.get("children") or []]]:
            names.update(part.lower() for part in path.split("/"))
    names.update(path.lower() for path in repo_map.get("root_files") or [])
    extensions = set((repo_map.get("extensions") or {}).keys())
    questions: list[tuple[str, str]] = []
    rules = (
        ({"auth", "authentication", "login", "session", "sessions", "oauth"}, "How does authentication work, end to end?", "how"),
        ({"api", "routes", "router", "routers", "trpc", "server", "controllers", "handlers", "endpoints"},
         "Where are the API routes defined, and how is a request handled?", "where"),
        ({"prisma", "schema.prisma", "migrations", "models", "db", "database", "drizzle"}, "What does the data model look like?", "where"),
        ({"components", "ui", "design-system", "widgets"}, "How are the UI components organized?", "how"),
        ({"test", "tests", "__tests__", "e2e", "playwright", "cypress", "spec"}, "How are tests organized, and how do I run them?", "how"),
        ({".github", "dockerfile", "docker-compose.yml", "vercel.json", "render.yaml", "deploy", "infra", "terraform"},
         "How is the project built and deployed?", "how"),
        ({"i18n", "locales", "translations"}, "How does internationalization work?", "how"),
        ({"packages", "apps", "libs"}, "How do the packages in this monorepo depend on each other?", "how"),
        ({"webhooks", "webhook", "queue", "jobs", "workers", "cron"}, "How are background jobs and webhooks processed?", "how"),
        ({"payments", "billing", "stripe", "checkout"}, "How do payments and billing work?", "how"),
    )
    for markers, question, kind in rules:
        if names & markers:
            questions.append((question, kind))
    if "py" in extensions and len(questions) < 3:
        questions.append(("What are the main Python entry points?", "where"))
    questions.append(("Where would I start to add a new feature, and which files would it touch?", "change"))
    questions.append(("What would a new contributor need to know first?", "overview"))
    return questions


def suggest_questions(repo_map: dict, facts: dict | None = None) -> list[str]:
    """Deterministic, repository-specific starter questions (no model call)."""
    return [item["question"] for item in suggest_question_details(repo_map, facts)]


def key_files_for(repo_map: dict, facts: dict | None, limit: int = 12) -> list[str]:
    """Root project files first, then the deeper files a newcomer opens first."""
    root = sorted(
        (path for path in repo_map.get("root_files") or [] if path.lower() in KEY_FILE_NAMES),
        key=lambda path: (not path.lower().startswith("readme"), path.lower() not in _MANIFEST_NAMES, path.lower()),
    )
    deeper: list[str] = []
    if facts:
        router = next((item["path"] for item in facts.get("routers") or [] if not item.get("subject")), None)
        deeper = [
            (facts.get("schema") or {}).get("path"), facts.get("entry"), router, facts.get("auth"),
            *(facts.get("test_configs") or []),
        ]
    ordered = list(dict.fromkeys(path for path in [*root, *deeper] if path))
    return ordered[:limit]


def _overview_notice(repo: dict) -> str | None:
    if repo.get("refreshing"):
        return "A refresh is running. This overview shows the current index and updates when the refresh finishes."
    if repo.get("is_indexing"):
        return "Indexing is still running, so this overview covers the files indexed so far."
    return None


def language_breakdown(repo_map: dict) -> list[dict]:
    totals: dict[str, int] = {}
    for extension, count in (repo_map.get("extensions") or {}).items():
        label = LANGUAGE_LABELS.get(extension)
        if label:
            totals[label] = totals.get(label, 0) + int(count)
    total = sum(totals.values()) or 1
    return [
        {"language": label, "files": count, "percent": round(count * 100 / total, 1)}
        for label, count in sorted(totals.items(), key=lambda item: -item[1])[:8]
    ]


@router.get("/repos/{repo_name}/overview")
async def repository_overview(repo_name: str, current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await fetch_owned_repository(store, current_user.id, repo_name)
        repo_map = await build_repo_map(store, repo["id"])
        facts: dict | None = None
        try:
            facts = await gather_question_facts(store, repo)
        except Exception:
            # Starter questions are a convenience; folder-based ones still work.
            logger.warning("Could not read file facts for starter questions", exc_info=True)
        details = suggest_question_details(repo_map, facts)
        return {
            "repository": public_view(repo),
            "total_files": repo_map.get("total_files", 0),
            "tree": repo_map.get("tree") or [],
            "root_files": repo_map.get("root_files") or [],
            "key_files": key_files_for(repo_map, facts),
            "languages": language_breakdown(repo_map),
            "suggested_questions": [item["question"] for item in details],
            # The same questions with their kind ("overview", "where", "how",
            # "change") and the indexed files each one names.
            "suggested_question_details": details,
            "notice": _overview_notice(repo),
        }
    except Exception as error:
        _handle(error, "load the repository overview")


@router.get("/repos/{repo_name}/search")
async def repository_search(
    repo_name: str,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    current_user=Depends(get_current_user),
):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await fetch_owned_repository(store, current_user.id, repo_name)
        if not repo["queryable"]:
            return {
                "results": [], "query": q, "searchable": False,
                "message": "Code search starts working as soon as keyword indexing finishes. Check back in a moment.",
            }
        results = await search_code(store, repo["id"], q, limit=limit, staging_prefix=repo.get("staging_prefix"))
        return {
            "results": results, "query": q, "searchable": True,
            "message": None if results else (
                f"No matches for \u201c{q.strip()}\u201d. Search matches code text: try an exact name "
                "(a function, class or variable) or fewer words."
            ),
        }
    except Exception as error:
        _handle(error, "search the code")


# A file at a commit SHA never changes, so its GitHub text is cached in
# memory: opening the same file again (a citation, then "open file", then a
# preview) no longer waits on raw.githubusercontent.com each time. Bounded by
# total size so a 512 MB instance stays safe.
_RAW_FILE_CACHE: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
_RAW_FILE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_raw_file_cache_bytes = 0
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def _cached_raw_file(key: tuple[str, str, str, str]) -> str | None:
    text = _RAW_FILE_CACHE.get(key)
    if text is not None:
        _RAW_FILE_CACHE.move_to_end(key)
    return text


def _cache_raw_file(key: tuple[str, str, str, str], text: str) -> None:
    global _raw_file_cache_bytes
    size = len(text)
    if size > _RAW_FILE_CACHE_MAX_BYTES // 4 or key in _RAW_FILE_CACHE:
        return
    _RAW_FILE_CACHE[key] = text
    _raw_file_cache_bytes += size
    while _raw_file_cache_bytes > _RAW_FILE_CACHE_MAX_BYTES and _RAW_FILE_CACHE:
        _old_key, old_text = _RAW_FILE_CACHE.popitem(last=False)
        _raw_file_cache_bytes -= len(old_text)


@router.get("/repos/{repo_name}/file")
async def repository_file(
    repo_name: str,
    path: str = Query(min_length=1, max_length=500),
    current_user=Depends(get_current_user),
    response: Response = None,
):
    """Return one indexed file at the indexed commit (GitHub raw, else the index)."""
    try:
        clean_path = normalise_repository_path(path)
    except CodeEditValidationError as error:
        raise HTTPException(
            status_code=422,
            detail="That file path isn't valid. Use a path from the repository root, such as src/app.ts.",
        ) from error
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await fetch_owned_repository(store, current_user.id, repo_name)
        staging = repo.get("staging_prefix")
        # A refresh's replacement rows stay hidden until the swap, so a file
        # that only exists in the unpublished version is not served.
        indexed = await store.fetch_one(
            "SELECT 1 AS ok FROM chunks WHERE repo_id = ? AND file_path = ?"
            + (" AND id NOT LIKE ?" if staging else "") + " LIMIT 1",
            [repo["id"], clean_path, *([f"{staging}%"] if staging else [])],
        )
        if not indexed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{clean_path} isn't in the index. It may have been skipped while indexing (the coverage "
                    "report lists skipped files), or the path may be misspelled."
                ),
            )
        ref = str(repo.get("commit_sha") or "HEAD")
        github_url = None
        try:
            owner, name = parse_github_repository(str(repo.get("github_url") or ""))
            github_url = f"https://github.com/{owner}/{name}/blob/{quote(ref, safe='')}/{quote(clean_path, safe='/')}"
        except (RepositoryValidationError, ValueError):
            owner = name = ""
        content: str | None = None
        source = "index"
        truncated = False
        total_lines: int | None = None
        exact = True
        pinned = bool(_COMMIT_SHA.fullmatch(ref))
        cache_key = (owner, name, ref, clean_path)
        if owner and name and pinned:
            cached = _cached_raw_file(cache_key)
            if cached is not None:
                content, source = cached, "github"
        if owner and name and content is None:
            try:
                data = await fetch_raw_file(owner, name, clean_path, ref=ref, max_bytes=_FILE_VIEW_MAX_BYTES)
                if data is not None:
                    content = data.decode("utf-8", errors="replace")
                    source = "github"
                    if pinned:
                        _cache_raw_file(cache_key, content)
            except GitHubAPIError as error:
                logger.info("Raw file view fell back to the index for %s: %s", clean_path, error)
            except Exception:
                logger.warning("Raw file view failed for %s", clean_path, exc_info=True)
        if content is None:
            view = await _read_file_from_index(store, repo["id"], clean_path, staging)
            content = view["content"]
            truncated = view["truncated"]
            total_lines = view["total_lines"] or None
            exact = view["exact"]
        line_count = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        if pinned and source == "github" and response is not None:
            # Same commit, same bytes: the browser may reuse it for this user.
            response.headers["Cache-Control"] = "private, max-age=3600"
        return {
            "path": clean_path,
            "content": content,
            "source": source,
            "ref": ref,
            "language": language_for(clean_path),
            "line_count": line_count,
            # The index view is bounded; ``total_lines`` is the file's real
            # length when only part of it is shown. ``exact`` is False when the
            # text could not be rebuilt byte-for-byte (label it normalised).
            "truncated": truncated,
            "total_lines": total_lines if total_lines is not None else line_count,
            "exact": exact,
            "github_url": github_url,
        }
    except Exception as error:
        _handle(error, "open this file")
