"""Repository conventions for pull requests (G6a) and PR metadata helpers (G6b).

``detect_conventions`` reads, best effort and within a 3 s budget, the files
that decide whether a pull request is mergeable as written: the PR template,
CONTRIBUTING rules, agent/coding rules (AGENTS.md and friends), commit-style
tooling (commitlint, commitizen, semantic-release, release-please, a
semantic-PR-title workflow) and formatter configs for the style gate. The
result is cached per (repository, commit) for 30 minutes.

The PR helpers turn a model title into an imperative (and, when the
repository requires it, Conventional Commits) title, fill a PR template
deterministically, and build the commit message. None of them ever ticks a
checklist box.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from agent.quality import build_repo_style, describe_style, is_doc_path, is_test_path, style_profile

logger = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[bytes | None]]

CONVENTIONS_BUDGET_SECONDS = 3.0
CONVENTIONS_CACHE_SECONDS = 30 * 60
CONVENTIONS_CACHE_SIZE = 64
FILE_LIMIT_BYTES = 64 * 1024
CONTRIBUTING_EXCERPT = 1_500
AGENT_EXCERPT = 2_000
PR_BODY_LIMIT = 20_000
COMMIT_MESSAGE_LIMIT = 500
TITLE_LIMIT = 72

PR_TEMPLATE_PATHS = (
    ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md", "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md",
)
CONTRIBUTING_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md")
AGENT_PATHS = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
COMMITLINT_PATHS = (
    "commitlint.config.js", "commitlint.config.cjs", "commitlint.config.mjs", "commitlint.config.ts",
    ".commitlintrc", ".commitlintrc.json", ".commitlintrc.yml", ".commitlintrc.yaml", ".commitlintrc.js",
    ".commitlintrc.cjs",
)
VISIBLE_CONFIG_PATHS = ("package.json", "pyproject.toml", ".github/dco.yml", "release-please-config.json")
LOCKFILES = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn",
             "bun.lockb": "bun", "bun.lock": "bun"}
HIDDEN_CONFIG_PATHS = (
    ".editorconfig", ".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml", ".releaserc",
    ".releaserc.json", ".releaserc.yml", ".czrc", ".cz.toml", ".release-please-manifest.json",
)
CONVENTIONAL_TITLE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: \S")
_CONTRIBUTING_LINE = re.compile(
    r"commit|pull request|\bPR\b|title|sign(?:ed)?[- ]off|\bDCO\b|changelog|\btests?\b|conventional|squash|lint|format|rebase",
    re.IGNORECASE,
)

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_conventions_cache() -> None:
    _CACHE.clear()


def empty_conventions(note: str = "") -> dict:
    return {
        "pr_template_path": None, "pr_template": "", "commit_style": "unknown", "commit_style_source": "",
        "contributing_path": None, "contributing_excerpt": "", "agent_instructions_path": None,
        "agent_instructions": "", "dco_required": False, "notes": [note] if note else [],
        "repo_style": build_repo_style(), "sources": [], "test_command": "", "package_manager": "", "complete": False,
    }


def public_conventions(conventions: dict | None) -> dict:
    """The ``proposal.conventions`` contract."""
    data = conventions or empty_conventions()
    return {
        "pr_template_path": data.get("pr_template_path"),
        "commit_style": data.get("commit_style") or "unknown",
        "commit_style_source": data.get("commit_style_source") or "",
        "contributing_path": data.get("contributing_path"),
        "agent_instructions_path": data.get("agent_instructions_path"),
        "dco_required": bool(data.get("dco_required")),
        "notes": list(data.get("notes") or [])[:12],
    }


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _loose_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _manifest_hits(store: Any, repo_id: str, paths: list[str]) -> tuple[set[str], list[str], list[str]]:
    """(listed candidate paths, PR template directory entries, workflow files) from the index."""
    listed: set[str] = set()
    templates: list[str] = []
    workflows: list[str] = []
    if store is None or not repo_id:
        return listed, templates, workflows
    try:
        placeholders = ",".join("?" for _ in paths)
        rows = await store.fetch_all(
            f"SELECT file_path FROM repo_files WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id, *paths],
        )
        listed = {str(row.get("file_path")) for row in rows}
        rows = await store.fetch_all(
            "SELECT file_path FROM repo_files WHERE repo_id = ? AND (file_path LIKE '.github/PULL_REQUEST_TEMPLATE/%' "
            "OR file_path LIKE '.github/pull_request_template/%' OR file_path LIKE '.github/workflows/%') "
            "ORDER BY file_path LIMIT 60",
            [repo_id],
        )
        for row in rows:
            path = str(row.get("file_path"))
            (workflows if path.startswith(".github/workflows/") else templates).append(path)
    except Exception:
        logger.debug("Convention manifest lookup failed", exc_info=True)
    return listed, templates, workflows


async def _coverage(store: Any, repo_id: str) -> tuple[set[str] | None, int]:
    """(excluded paths when the list is complete, budget-omitted file count)."""
    excluded: set[str] | None = None
    budget_omitted = 0
    if store is None or not repo_id:
        return None, 0
    try:
        row = await store.fetch_one(
            "SELECT excluded_files, excluded_paths FROM repo_coverage WHERE repo_id = ?", [repo_id],
        )
        if row:
            paths = row.get("excluded_paths")
            paths = json.loads(paths) if isinstance(paths, str) else paths
            if isinstance(paths, list) and int(row.get("excluded_files") or 0) <= len(paths):
                excluded = {str(path) for path in paths}
        snapshot = await store.fetch_one("SELECT selection_json FROM repo_snapshots WHERE repo_id = ?", [repo_id])
        if snapshot:
            selection = snapshot.get("selection_json")
            selection = json.loads(selection) if isinstance(selection, str) else selection
            if isinstance(selection, dict):
                budget_omitted = int(selection.get("budget_omitted_files") or 0)
    except Exception:
        logger.debug("Coverage lookup failed", exc_info=True)
    return excluded, budget_omitted


async def _workflow_signals(store: Any, repo_id: str) -> dict[str, str]:
    """Commit-style tooling named in indexed GitHub workflow files."""
    signals: dict[str, str] = {}
    if store is None or not repo_id:
        return signals
    try:
        rows = await store.fetch_all(
            "SELECT file_path, content FROM chunks WHERE repo_id = ? AND file_path LIKE '.github/workflows/%' "
            "AND (content LIKE '%action-semantic-pull-request%' OR content LIKE '%semantic-release%' "
            "OR content LIKE '%release-please%' OR content LIKE '%commitlint%') LIMIT 20",
            [repo_id],
        )
    except Exception:
        logger.debug("Workflow lookup failed", exc_info=True)
        return signals
    for row in rows:
        content = str(row.get("content") or "")
        path = str(row.get("file_path") or "")
        if "action-semantic-pull-request" in content:
            signals.setdefault("semantic_pr", path)
        if "commitlint" in content:
            signals.setdefault("commitlint", path)
        if "semantic-release" in content:
            signals.setdefault("semantic_release", path)
        if "release-please" in content:
            signals.setdefault("release_please", path)
    return signals


async def _recent_commit_style(owner: str, name: str, token: str, timeout: float) -> tuple[str, str] | None:
    """("conventional"|"plain", source) from the last 30 default-branch commits."""
    from github import github_client  # read the base URL at call time (tests patch it)

    url = f"{github_client.GITHUB_API_BASE}/repos/{owner}/{name}/commits"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Codebase-Intelligence-App"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params={"per_page": 30}, headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        commits = response.json()
    except ValueError:
        return None
    if not isinstance(commits, list):
        return None
    subjects = []
    for commit in commits[:30]:
        message = ((commit or {}).get("commit") or {}).get("message") if isinstance(commit, dict) else None
        if isinstance(message, str) and message.strip():
            subjects.append(message.strip().split("\n", 1)[0])
    if len(subjects) < 10:
        return None
    matching = sum(1 for subject in subjects if CONVENTIONAL_TITLE.match(subject))
    source = f"recent commits ({matching} of {len(subjects)})"
    return ("conventional" if matching / len(subjects) >= 0.6 else "plain"), source


def _contributing_excerpt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and _CONTRIBUTING_LINE.search(line)]
    excerpt = "\n".join(lines)
    return excerpt[:CONTRIBUTING_EXCERPT]


async def detect_conventions(*, owner: str, name: str, repo_id: str, commit_sha: str | None, store: Any,
                             fetch: Fetch, token: str | None = None,
                             budget_seconds: float | None = None) -> dict:
    """Read the repository's PR and commit conventions (best effort, cached)."""
    if budget_seconds is None:
        budget_seconds = CONVENTIONS_BUDGET_SECONDS
    key = (str(repo_id or f"{owner}/{name}"), str(commit_sha or ""))
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < CONVENTIONS_CACHE_SECONDS:
        return cached[1]
    started = time.monotonic()
    candidates = [*PR_TEMPLATE_PATHS, *CONTRIBUTING_PATHS, *AGENT_PATHS, *COMMITLINT_PATHS, *VISIBLE_CONFIG_PATHS]
    listed, template_dir, _workflows = await _manifest_hits(store, repo_id, [*candidates, *LOCKFILES])
    excluded, budget_omitted = await _coverage(store, repo_id)
    signals = await _workflow_signals(store, repo_id)
    manifest_known = bool(listed) or bool(template_dir) or await _has_manifest(store, repo_id)

    to_fetch: list[str] = []
    for path in candidates:
        hidden = path.rsplit("/", 1)[-1].startswith(".") and "/" not in path
        if hidden:
            # Hidden root files are never indexed; coverage lists the ones that exist.
            if excluded is None or path in excluded:
                to_fetch.append(path)
        elif path in listed or not manifest_known or budget_omitted:
            to_fetch.append(path)
    for path in HIDDEN_CONFIG_PATHS:
        if excluded is None or path in excluded:
            to_fetch.append(path)
    template_entries = sorted(template_dir, key=lambda item: (not item.lower().endswith("/default.md"), item))
    to_fetch.extend(path for path in template_entries[:2] if path.lower().endswith(".md"))
    to_fetch = list(dict.fromkeys(to_fetch))

    texts: dict[str, str] = {}
    timed_out = False
    semaphore = asyncio.Semaphore(6)

    async def read(path: str) -> None:
        async with semaphore:
            try:
                data = await fetch(path)
            except Exception:
                return
            text = _decode(data)
            if text is not None and text.strip():
                texts[path] = text[:FILE_LIMIT_BYTES]

    history: dict[str, tuple[str, str] | None] = {}

    async def read_history() -> None:
        history["result"] = await _recent_commit_style(owner, name, token or "", max(0.5, budget_seconds - 0.5))

    tasks = [asyncio.create_task(read(path)) for path in to_fetch]
    if token and owner and name:
        tasks.append(asyncio.create_task(read_history()))
    if tasks:
        remaining = max(0.1, budget_seconds - (time.monotonic() - started))
        _done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            task.cancel()
        if pending:
            timed_out = True
            await asyncio.gather(*pending, return_exceptions=True)

    conventions = empty_conventions()
    notes: list[str] = []
    sources: list[str] = []

    template_path = next((path for path in (*PR_TEMPLATE_PATHS, *template_entries) if path in texts), None)
    if template_path:
        conventions["pr_template_path"] = template_path
        conventions["pr_template"] = texts[template_path]
        sources.append(template_path)
        notes.append(f"Pull requests follow {template_path}; its checklist boxes are left for you to tick.")
    contributing_path = next((path for path in CONTRIBUTING_PATHS if path in texts), None)
    contributing = texts.get(contributing_path or "", "")
    if contributing_path:
        conventions["contributing_path"] = contributing_path
        conventions["contributing_excerpt"] = _contributing_excerpt(contributing)
        sources.append(contributing_path)
    agent_path = next((path for path in AGENT_PATHS if path in texts), None)
    if agent_path:
        conventions["agent_instructions_path"] = agent_path
        conventions["agent_instructions"] = texts[agent_path].strip()[:AGENT_EXCERPT]
        sources.append(agent_path)
        notes.append(f"{agent_path} rules were given to the editor and the reviewer.")

    package = _loose_json(texts.get("package.json"))
    package = package if isinstance(package, dict) else {}
    dependencies: dict = {}
    for field in ("devDependencies", "dependencies"):
        if isinstance(package.get(field), dict):
            dependencies.update(package[field])
    pyproject = texts.get("pyproject.toml") or ""
    style_source = None
    commit_style, commit_source = "unknown", ""
    commitlint_file = next((path for path in COMMITLINT_PATHS if path in texts), None)
    if commitlint_file:
        commit_style, commit_source = "conventional", "commitlint config"
        style_source = commitlint_file
    elif "commitlint" in package or any(dep.startswith("@commitlint/") for dep in dependencies):
        commit_style, commit_source = "conventional", "commitlint config"
        style_source = "package.json"
    elif signals.get("semantic_pr"):
        commit_style, commit_source = "conventional", "semantic PR title check (amannn/action-semantic-pull-request)"
        style_source = signals["semantic_pr"]
    elif ".czrc" in texts or ".cz.toml" in texts or "commitizen" in dependencies or "cz-conventional-changelog" in dependencies \
            or (isinstance(package.get("config"), dict) and "commitizen" in package["config"]) or "[tool.commitizen]" in pyproject:
        commit_style, commit_source = "conventional", "commitizen"
    elif any(path in texts for path in (".releaserc", ".releaserc.json", ".releaserc.yml")) or "semantic-release" in dependencies \
            or "release" in package and isinstance(package.get("release"), dict) or "[tool.semantic_release]" in pyproject \
            or signals.get("semantic_release"):
        commit_style, commit_source = "conventional", "semantic-release"
    elif "release-please-config.json" in texts or ".release-please-manifest.json" in texts or signals.get("release_please"):
        commit_style, commit_source = "conventional", "release-please"
    elif signals.get("commitlint"):
        commit_style, commit_source = "conventional", "commitlint config"
        style_source = signals["commitlint"]
    else:
        result = history.get("result")
        if result:
            commit_style, commit_source = result
    if style_source:
        sources.append(style_source)
    conventions["commit_style"] = commit_style
    conventions["commit_style_source"] = commit_source
    if commit_style == "conventional":
        notes.append(f"Titles and commits use Conventional Commits ({commit_source}).")
    if "Signed-off-by" in contributing or re.search(r"\bDCO\b|Developer Certificate of Origin", contributing) or ".github/dco.yml" in texts:
        conventions["dco_required"] = True
        notes.append("Commits need a DCO sign-off (Signed-off-by: Name <email>); GitHub's DCO check fails without it.")
        if ".github/dco.yml" in texts:
            sources.append(".github/dco.yml")

    prettier_name = next((path for path in (".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml") if path in texts), None)
    conventions["repo_style"] = build_repo_style(
        editorconfig=texts.get(".editorconfig"), package_json=package or None,
        prettier=(prettier_name, texts[prettier_name]) if prettier_name else None, pyproject=pyproject or None,
    )
    for path in (".editorconfig", prettier_name, "package.json" if (package.get("xo") is not None or "xo" in dependencies
                                                                    or isinstance(package.get("prettier"), dict)) else None,
                 "pyproject.toml" if ("[tool.black]" in pyproject or "[tool.ruff" in pyproject) else None):
        if path and path in texts:
            sources.append(path)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    manager = _package_manager(package, listed | (excluded or set()), texts)
    conventions["package_manager"] = manager
    if isinstance(scripts.get("test"), str) and "no test specified" not in scripts["test"]:
        conventions["test_command"] = f"{manager or 'npm'} test"
    elif "[tool.pytest" in pyproject:
        conventions["test_command"] = "pytest"
    if timed_out:
        notes.append(f"Some convention files could not be read within {budget_seconds:g} s; the change follows what was read.")
    conventions["notes"] = notes
    conventions["sources"] = list(dict.fromkeys(sources))
    conventions["complete"] = not timed_out
    if not timed_out:
        if len(_CACHE) >= CONVENTIONS_CACHE_SIZE:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic(), conventions)
    return conventions


def _package_manager(package: dict, paths: set[str], texts: dict[str, str]) -> str:
    """npm, pnpm, yarn or bun from package.json#packageManager, a lockfile or .npmrc; "" when unknown."""
    if not package:
        return ""
    declared = str(package.get("packageManager") or "").split("@", 1)[0].strip().lower()
    if declared in {"npm", "pnpm", "yarn", "bun"}:
        return declared
    found = {manager for name, manager in LOCKFILES.items() if name in paths}
    if len(found) == 1:
        return found.pop()
    if not found and (".npmrc" in paths or ".npmrc" in texts):
        return "npm"
    return ""


async def _has_manifest(store: Any, repo_id: str) -> bool:
    if store is None or not repo_id:
        return False
    try:
        row = await store.fetch_one("SELECT 1 AS present FROM repo_files WHERE repo_id = ? LIMIT 1", [repo_id])
    except Exception:
        return False
    return bool(row)


def conventions_summary(conventions: dict) -> str:
    """"PR template; Conventional Commits (commitlint); tabs, single quotes" for the tool chip."""
    parts = []
    if conventions.get("pr_template_path"):
        parts.append("PR template")
    if conventions.get("commit_style") == "conventional":
        source = str(conventions.get("commit_style_source") or "").split(" (")[0]
        parts.append(f"Conventional Commits ({source})" if source else "Conventional Commits")
    elif conventions.get("commit_style") == "plain":
        parts.append("plain commit subjects")
    if conventions.get("agent_instructions_path"):
        parts.append(f"{conventions['agent_instructions_path']} rules")
    if conventions.get("dco_required"):
        parts.append("DCO sign-off required")
    style = conventions.get("repo_style") or {}
    if style.get("editorconfig") or style.get("prettier") is not None or style.get("xo") is not None:
        probe = style_profile("probe.ts", None, style)
        parts.append(describe_style(probe).replace(", LF", "").replace(", CRLF", ""))
    if not parts:
        return "No PR template, commit convention or formatter config found; style is inferred from each file"
    return "; ".join(parts)[:400]


def conventions_prompt_block(conventions: dict | None, style_lines: list[str] | None = None) -> str:
    """The "Repository conventions" block for the edit and review prompts."""
    if not conventions:
        return ""
    lines = []
    if style_lines:
        lines.extend(style_lines)
    if conventions.get("commit_style") == "conventional":
        lines.append("Commit messages and PR titles use Conventional Commits (type: description).")
    if conventions.get("dco_required"):
        lines.append("Commits need a DCO sign-off.")
    if conventions.get("contributing_excerpt"):
        lines.append(f"From {conventions['contributing_path']}:\n{conventions['contributing_excerpt']}")
    if conventions.get("agent_instructions"):
        lines.append(f"From {conventions['agent_instructions_path']} (coding rules for this repository):\n{conventions['agent_instructions']}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# G6b: title, body and commit message
# ---------------------------------------------------------------------------

_IMPERATIVE = {
    "adds": "add", "added": "add", "adding": "add", "fixes": "fix", "fixed": "fix", "fixing": "fix",
    "updates": "update", "updated": "update", "updating": "update", "removes": "remove", "removed": "remove",
    "renames": "rename", "renamed": "rename", "changes": "change", "changed": "change", "makes": "make",
    "made": "make", "uses": "use", "used": "use", "replaces": "replace", "replaced": "replace", "moves": "move",
    "moved": "move", "improves": "improve", "improved": "improve", "implements": "implement",
    "implemented": "implement", "refactors": "refactor", "refactored": "refactor", "validates": "validate",
    "validated": "validate", "throws": "throw", "handles": "handle", "handled": "handle", "creates": "create",
    "created": "create", "deletes": "delete", "deleted": "delete", "supports": "support", "supported": "support",
    "prevents": "prevent", "prevented": "prevent", "ensures": "ensure", "ensured": "ensure", "allows": "allow",
    "allowed": "allow", "converts": "convert", "converted": "convert", "sets": "set", "bumps": "bump",
    "bumped": "bump", "introduces": "introduce", "introduced": "introduce", "documents": "document",
    "documented": "document", "corrects": "correct", "corrected": "correct", "clarifies": "clarify",
    "clarified": "clarify", "simplifies": "simplify", "simplified": "simplify", "extracts": "extract",
    "extracted": "extract", "restores": "restore", "restored": "restore", "returns": "return",
    "returned": "return", "clears": "clear", "cleared": "clear", "covers": "cover", "tests": "test",
}


def _shorten(text: str, limit: int = TITLE_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut or text[:limit]


def imperative_title(title: str) -> str:
    """"Adds X." -> "Add X": imperative mood, no trailing period, at most 72 characters."""
    text = re.sub(r"\s+", " ", str(title or "")).strip().strip("`\"'").rstrip(".").strip()
    if not text:
        return ""
    match = CONVENTIONAL_TITLE.match(text)
    prefix = ""
    body = text
    if match:
        prefix = text[: text.index(":") + 2]
        body = text[len(prefix):]
    words = body.split(" ", 1)
    first = words[0]
    replacement = _IMPERATIVE.get(first.lower())
    if replacement:
        replacement = replacement.capitalize() if first[:1].isupper() else replacement
        body = replacement + (" " + words[1] if len(words) > 1 else "")
    if not prefix and body[:1].islower():
        body = body[:1].upper() + body[1:]
    return _shorten(prefix + body)


def change_type(files: list[dict], *, renames: list | None = None, question: str = "", closes_issue: bool = False) -> str:
    """Conventional Commits type for a proposal."""
    paths = [str(item.get("file_path") or "") for item in files]
    if paths and all(is_test_path(path) for path in paths):
        return "test"
    if paths and all(is_doc_path(path) for path in paths):
        return "docs"
    if renames:
        return "refactor"
    lowered = (question or "").lower()
    if closes_issue or re.search(r"\b(?:fix|bug|broken|crash|error|incorrect|wrong|regression)\w*\b", lowered):
        return "fix"
    if re.search(r"\b(?:refactor|rename|clean ?up|simplif|extract|reorganis|reorganiz)\w*\b", lowered):
        return "refactor"
    if re.search(r"\b(?:docs?|documentation|readme|comment)\b", lowered) and all(not item.get("action") == "create" for item in files):
        return "docs"
    if re.search(r"\b(?:perf|performance|faster|speed up|optimi[sz])\w*\b", lowered):
        return "perf"
    if any(item.get("action") == "create" for item in files) or re.search(
        r"\b(?:add|new|support|implement|introduce|allow|enable|create|validat)\w*\b", lowered
    ):
        return "feat"
    return "fix"


def conventional_title(title: str, kind: str) -> str:
    """A title that matches the Conventional Commits regex (kept when it already does)."""
    clean = imperative_title(title)
    if CONVENTIONAL_TITLE.match(clean):
        return clean
    description = clean[:1].lower() + clean[1:] if clean[:2] != clean[:2].upper() else clean
    return _shorten(f"{kind}: {description}")


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CHECKBOX = re.compile(r"^(\s*[-*]\s*)\[[xX]\]", re.MULTILINE)
_VERIFY_HEADING = re.compile(
    r"^#{1,6}\s*(?:how\s+(?:to\s+)?(?:verify|test)|how\s+has\s+this\s+been\s+tested\??|testing|tests?|test\s+plan|"
    r"verification|validation|steps\s+to\s+(?:verify|test))\b",
    re.IGNORECASE | re.MULTILINE,
)


def untick(body: str) -> str:
    """Never claim a checklist item was done on the user's behalf."""
    return _CHECKBOX.sub(r"\1[ ]", body or "")


def template_headings(template: str) -> list[str]:
    return [match.group(2).strip() for match in _HEADING.finditer(_HTML_COMMENT.sub("", template or ""))]


def follows_template(body: str, template: str) -> bool:
    """True when the body has every heading of the template."""
    headings = [heading.lower() for heading in template_headings(template)]
    if not headings:
        return True
    present = {match.group(2).strip().lower() for match in _HEADING.finditer(body or "")}
    return all(heading in present for heading in headings)


def has_verify_section(body: str) -> bool:
    return bool(_VERIFY_HEADING.search(body or ""))


def _section_kind(heading: str) -> str:
    text = heading.lower()
    if re.search(r"how has this been tested|\btest|verif|validat|how to (?:test|verify)|qa\b", text):
        return "testing"
    if re.search(r"related|issue|fixes|closes|ticket|link|reference", text):
        return "issue"
    if re.search(r"checklist|check list|before (?:you )?submit|requirements", text):
        return "checklist"
    if re.search(r"\bchanges?\b|what changed|implementation|list of", text):
        return "changes"
    if re.search(r"summary|description|what|why|motivation|purpose|overview|context|about", text):
        return "summary"
    return "unknown"


def _bullets(items: list[str], *, boxes: bool = False) -> str:
    prefix = "- [ ] " if boxes else "- "
    return "\n".join(f"{prefix}{item}" for item in items if str(item).strip())


def change_lines(files: list[dict]) -> list[str]:
    lines = []
    for item in files:
        verb = "Add" if item.get("action") == "create" else "Update"
        summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip().rstrip(".")
        summary = "" if has_meta_text(summary) else _DOUBLE_TICKS.sub(lambda match: f"`{match.group(1)}`", summary)
        lines.append(f"`{item['file_path']}`: {summary}" if summary else f"{verb} `{item['file_path']}`")
    return lines


def fill_pr_template(template: str, *, summary: str, files: list[dict], validation: list[str], issue_line: str = "") -> str:
    """Fill a PR template deterministically.

    HTML comments are removed and sections are mapped by heading keyword:
    summary/description -> the summary, changes -> per-file bullets,
    testing/verification -> the validation steps as unchecked boxes,
    related issue -> ``Fixes #N``/``Refs #N``. Checklists keep their items,
    unticked. Unknown sections keep only their (unticked) checkboxes.
    """
    text = _HTML_COMMENT.sub("", template or "").replace("\r\n", "\n")
    matches = list(_HEADING.finditer(text))
    if not matches:
        return default_pr_body(summary=summary, files=files, validation=validation, issue_line=issue_line)
    parts = [text[: matches[0].start()].strip()]
    used: set[str] = set()
    issue_placed = False
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        original = text[match.end():end].strip("\n")
        kind = _section_kind(match.group(2))
        content = ""
        if kind == "summary" and "summary" not in used:
            content = summary.strip()
            used.add("summary")
        elif kind == "changes" and "changes" not in used:
            content = _bullets(change_lines(files))
            used.add("changes")
        elif kind == "testing" and "testing" not in used:
            content = _bullets(validation, boxes=True)
            used.add("testing")
        elif kind == "issue" and not issue_placed:
            content = issue_line
            issue_placed = bool(issue_line)
        else:
            boxes = [line.rstrip() for line in original.split("\n") if re.match(r"^\s*[-*]\s*\[[ xX]\]", line)]
            content = "\n".join(boxes)
        parts.append(f"{match.group(0).strip()}\n\n{content}".rstrip() if content else match.group(0).strip())
    if "summary" not in used and summary.strip():
        parts.insert(1 if parts[0] else 0, summary.strip())
    body = "\n\n".join(part for part in parts if part)
    if issue_line and not issue_placed:
        body = f"{issue_line}\n\n{body}"
    return untick(body).strip()[:PR_BODY_LIMIT]


def default_pr_body(*, summary: str, files: list[dict], validation: list[str], issue_line: str = "") -> str:
    sections = [f"## Summary\n\n{summary.strip() or 'See the changes below.'}",
                f"## Changes\n\n{_bullets(change_lines(files))}",
                f"## How to verify\n\n{_bullets(validation)}"]
    body = "\n\n".join(sections)
    if issue_line:
        body = f"{issue_line}\n\n{body}"
    return body[:PR_BODY_LIMIT]


# ---------------------------------------------------------------------------
# Sanitising generated PR text
# ---------------------------------------------------------------------------

# Text about the review or the reviewer, not about the change: it never
# belongs in a PR title, body or commit message ("This review was performed
# by a meticulous senior reviewer. The change is ready to merge once ...").
_META_TEXT = re.compile(
    r"\b(?:this|my|our)\s+(?:code\s+)?review\b|\bself-?review(?:ed|er)?\b"
    r"|\b(?:senior|meticulous|careful|thorough|expert)\s+(?:code\s+)?(?:reviewer|engineer|developer)\b|\bmeticulous(?:ly)?\b"
    r"|\breview(?:er)?\s+(?:found|flagged|noted|suggests|recommends|was\s+performed)\b|\bready\s+to\s+merge\b|\bLGTM\b"
    r"|\bI\s+(?:have\s+)?(?:reviewed|checked|verified|analy[sz]ed)\b|\bas\s+an?\s+(?:AI|language\s+model|assistant)\b"
    r"|\bautomatic\s+checks?\s+(?:found|flagged|noted|reported)\b"
    r"|\b(?:this|the)\s+(?:change|PR|pull\s+request)\s+(?:was|has\s+been)\s+reviewed\b",
    re.IGNORECASE,
)
_RULE_LINE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def has_meta_text(text: str) -> bool:
    return bool(_META_TEXT.search(text or ""))


def strip_meta_text(text: str) -> str:
    """Drop paragraphs and list items that talk about the review instead of the change.

    Headings are kept; a paragraph is removed whole, a list item alone.
    Leftover horizontal rules at the end are removed too.
    """
    if not text or not _META_TEXT.search(text):
        return str(text or "")
    blocks = re.split(r"\n\s*\n", str(text).replace("\r\n", "\n"))
    kept: list[str] = []
    for block in blocks:
        lines = block.split("\n")
        if any(line.lstrip().startswith(("-", "*", "+")) and not _RULE_LINE.match(line) for line in lines) \
                or any(_HEADING.match(line) for line in lines):
            lines = [line for line in lines if _HEADING.match(line) or not _META_TEXT.search(line)]
            if any(line.strip() for line in lines):
                kept.append("\n".join(lines))
            continue
        if not _META_TEXT.search(block):
            kept.append(block)
    while kept and _RULE_LINE.match(kept[-1]):
        kept.pop()
    return "\n\n".join(kept).strip()


def clean_title(title: str) -> str:
    """A model title, or "" when it describes the review rather than the change."""
    text = re.sub(r"^\s*(?:review|code review|self-review)\s*[:\-]\s*", "", str(title or ""), flags=re.IGNORECASE).strip()
    return "" if has_meta_text(text) else text


def _sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """(text before the first heading, [(heading line, content)])."""
    matches = list(_HEADING.finditer(body))
    if not matches:
        return body, []
    preamble = body[: matches[0].start()].strip("\n")
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group(0).strip(), body[match.end():end].strip("\n")))
    return preamble, sections


def _join_sections(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts = [preamble.strip()] if preamble.strip() else []
    for heading, content in sections:
        parts.append(f"{heading}\n\n{content.strip()}" if content.strip() else heading)
    return "\n\n".join(parts).strip()


def dedupe_sections(body: str, *, protected: set[str] | None = None) -> str:
    """One section per heading and one "how to verify" section.

    A second testing/verification section ("## Verification" after "## How
    to verify") is merged into the first: its lines are appended unless
    already present. Headings of the PR template (``protected``) stay as
    they are.
    """
    preamble, sections = _sections(str(body or "").replace("\r\n", "\n"))
    if len(sections) < 2:
        return str(body or "").strip()
    keep = {heading.lower() for heading in (protected or set())}
    merged: list[tuple[str, str]] = []
    first_by_key: dict[str, int] = {}
    for heading, content in sections:
        title = _HEADING.match(heading).group(2).strip().lower()
        key = "testing" if _VERIFY_HEADING.match(heading) and title not in keep else f"heading:{title}"
        if title in keep:
            key = f"protected:{title}:{len(merged)}"
        if key in first_by_key:
            index = first_by_key[key]
            existing = merged[index][1]
            seen = {line.strip() for line in existing.split("\n") if line.strip()}
            extra = [line for line in content.split("\n") if line.strip() and line.strip() not in seen]
            merged[index] = (merged[index][0], (existing.rstrip() + ("\n\n" + "\n".join(extra) if extra else "")).strip("\n"))
            continue
        first_by_key[key] = len(merged)
        merged.append((heading, content))
    return _join_sections(preamble, merged)


def replace_changes_section(body: str, files: list[dict]) -> str:
    """The body with its "Changes" section listing the final files (added before verification when missing)."""
    preamble, sections = _sections(str(body or "").replace("\r\n", "\n"))
    bullets = _bullets(change_lines(files))
    for index, (heading, _content) in enumerate(sections):
        if _section_kind(_HEADING.match(heading).group(2)) == "changes":
            sections[index] = (heading, bullets)
            return _join_sections(preamble, sections)
    insert_at = next((index for index, (heading, _content) in enumerate(sections) if _VERIFY_HEADING.match(heading)), len(sections))
    sections.insert(insert_at, ("## Changes", bullets))
    return _join_sections(preamble, sections)


_PACKAGE_MANAGER_TEST = re.compile(r"\b(npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b")


def normalise_command(text: str, conventions: dict | None) -> str:
    """Use the repository's own package manager in test commands ("pnpm test" in an npm repository is wrong)."""
    manager = (conventions or {}).get("package_manager") or ""
    if not text or manager not in {"npm", "pnpm", "yarn", "bun"}:
        return str(text or "")
    return _PACKAGE_MANAGER_TEST.sub(lambda match: match.group(0) if match.group(1) == manager else f"{manager} test", str(text))


def finalize_pr_body(body: str, *, template: str = "", summary: str, files: list[dict], validation: list[str],
                     regenerate_changes: bool = False) -> tuple[str, str]:
    """Return (body, how it was produced).

    The model's text is cleaned first: paragraphs about the review are
    dropped and duplicate sections merged (one "how to verify"). A
    template-following body is kept (boxes unticked); otherwise the
    template is filled deterministically. Without a template the body keeps
    the model's text and gains a "How to verify" section when it has none.
    ``regenerate_changes`` rewrites the Changes section from the final files
    (the self-review changed or added some after the body was written).
    """
    text = strip_meta_text(str(body or "")).strip()
    protected = set(template_headings(template)) if template else set()
    text = dedupe_sections(text, protected=protected) if text else ""
    if template:
        if text and follows_template(text, template):
            if regenerate_changes:
                text = replace_changes_section(text, files) if any(
                    _section_kind(heading) == "changes" for heading in template_headings(template)) else text
            return untick(text)[:PR_BODY_LIMIT], "template"
        return fill_pr_template(template, summary=summary, files=files, validation=validation), "template_filled"
    if not text:
        return default_pr_body(summary=summary, files=files, validation=validation), "generated"
    if regenerate_changes:
        text = replace_changes_section(text, files)
    if not has_verify_section(text):
        text = f"{text}\n\n## How to verify\n\n{_bullets(validation)}"
        return untick(text)[:PR_BODY_LIMIT], "verify_added"
    return untick(text)[:PR_BODY_LIMIT], "model"


_DOUBLE_TICKS = re.compile(r"`{2,}\s?(.*?)\s?`{2,}")


def commit_line_summary(summary: str, path: str) -> str:
    """A plain-text file summary for a commit body: no Markdown code spans, no repeated file name."""
    text = re.sub(r"\s+", " ", str(summary or "")).strip()
    text = _DOUBLE_TICKS.sub(lambda match: match.group(1), text).replace("`", "")
    base = path.rsplit("/", 1)[-1]
    for name in (path, base):
        text = re.sub(rf"\s+(?:in|of|from)\s+{re.escape(name)}\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:on|at)\s+(?:that|this|the same)\s+line\b", "", text, flags=re.IGNORECASE)
    text = text.strip().rstrip(".;:,")
    if not text or has_meta_text(text):
        return ""
    return text[:1].upper() + text[1:]


def commit_message(title: str, files: list[dict]) -> str:
    """The title, plus one line per file for a multi-file change (at most 500 characters)."""
    subject = str(title or "").strip()[:COMMIT_MESSAGE_LIMIT]
    if len(files) <= 1:
        return subject
    lines = [f"- {item['file_path']}: "
             + (commit_line_summary(str(item.get("summary") or ""), item["file_path"])
                or ("Add new file" if item.get("action") == "create" else "Update"))
             for item in files]
    message = subject + "\n"
    for line in lines:
        line = line if len(line) <= 120 else line[:117] + "..."
        if len(message) + 1 + len(line) > COMMIT_MESSAGE_LIMIT:
            remaining = len(files) - (message.count("\n- "))
            suffix = f"\n- and {remaining} more file(s)"
            if len(message) + len(suffix) <= COMMIT_MESSAGE_LIMIT:
                message += suffix
            break
        message += "\n" + line
    return message[:COMMIT_MESSAGE_LIMIT]
