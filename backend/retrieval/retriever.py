"""Hybrid, source-grounded retrieval over Turso chunks."""

import asyncio
import logging
import re
from collections import defaultdict
from typing import Dict, List

from config import ModelConfigurationError, settings
from ingest.embedder import EmbeddingUnavailableError, embed_query

logger = logging.getLogger(__name__)

FILE_PATH_PATTERN = re.compile(
    # A glob such as ``*.test.ts`` or ``src/**/x.ts`` is never a file path, so
    # a match may not start right after ``*``, ``?`` or ``[``.
    r"(?<![\w./*?\[-])((?:[\w.-]+/)*[\w.-]+\.(?:"
    r"py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|vue|svelte|astro|java|go|rs|rb|php|c|cc|cpp|cxx|h|hpp|cs|swift|"
    r"kt|kts|scala|dart|ex|exs|erl|lua|clj|groovy|gradle|tf|hcl|"
    r"sql|prisma|sh|bash|zsh|ps1|yaml|yml|json|jsonc|toml|ini|cfg|conf|env|md|mdx|rst|txt|html|css|scss|"
    r"sass|less|xml|graphql|gql|proto|dockerfile|lock"
    # A sentence full stop may follow the path ("... in src/a.ts."), but a
    # longer dotted name ("a.ts.bak") is not this path.
    r"))(?![\w*-])(?!\.[\w*])",
    re.IGNORECASE,
)
# Technology names that look like file names ("Next.js", "Node.js"). Without a
# folder they are treated as words, never as an explicit file request.
DOTTED_TECHNOLOGY_NAMES = {
    "next.js", "node.js", "nodejs", "vue.js", "react.js", "chart.js", "express.js", "nuxt.js", "three.js",
    "d3.js", "ember.js", "backbone.js", "angular.js", "knockout.js", "socket.io", "nest.js", "deno.js",
    "alpine.js", "solid.js", "svelte.js", "p5.js", "moment.js", "day.js", "ext.js", "meteor.js", "electron.js",
    "anime.js", "pixi.js", "hapi.js", "koa.js", "sails.js", "gatsby.js", "remix.js", "preact.js", "lit.js",
}
EXPLORATORY_REPOSITORY_PATTERN = re.compile(
    # Keep this deliberately narrow. Technical questions such as "explain
    # authentication" need implementation evidence, not an automatically
    # injected README. This is only for genuinely repository-wide questions.
    r"(?:\bwhat is (?:this|the) (?:project|repository|codebase)\b|"
    r"\b(?:give|show|provide)\b.{0,32}\b(?:overview|architecture|high[- ]level)\b|"
    r"\b(?:overall|high[- ]level)\s+(?:architecture|overview)\b|"
    r"\b(?:what makes|how is)\b.{0,56}\b(?:different|unique)\b|"
    r"\b(?:purpose|about)\s+(?:this|the)\s+(?:project|repository|codebase)\b)",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
MAX_CHUNKS_PER_FILE = 2
# Two overlapping 150-line windows merge into one span of at most this size.
MAX_MERGED_CHUNK_LINES = 400
IMPACT_QUESTION_PATTERN = re.compile(
    r"\b(?:what\s+(?:breaks|is\s+affected)|impact|depend(?:s|encies|ent)|affected\s+files?)\b|"
    r"\bif\s+.+\s+(?:changes?|is\s+removed|is\s+renamed)\b",
    re.IGNORECASE,
)
ISSUE_REFERENCE_PATTERN = re.compile(r"\b(?:github\s+)?issue\s*#?\s*(\d{1,8})\b", re.IGNORECASE)

# These files are useful for repository-wide orientation, but they are not
# implementation evidence for a targeted question. Keep them out of normal
# sparse/dense retrieval so a question about a component cannot be cited with
# a generic README simply because it repeats a few of the same words.
OVERVIEW_FILE_NAMES = {
    "readme", "readme.md", "readme.rst", "readme.txt",
    "contributing", "contributing.md", "changelog", "changelog.md",
    "changes.md", "license", "license.md", "copying",
}

# A documentation word can be incidental in an implementation question (for
# example, "change the navbar; do not cite README.md"). Treat documentation as
# an evidence scope only when it is the subject of the request. This prevents a
# negated README mention from widening retrieval and reintroducing README
# citations beside concrete source files.
DOCUMENTATION_NAME_PATTERN = (
    r"(?:readme(?:\.(?:md|rst|txt))?|documentation|docs?|"
    r"contributing(?:\.(?:md|rst|txt))?|changelog(?:\.(?:md|rst|txt))?|"
    r"changes\.md|license(?:\.md)?|copying)"
)
DOCUMENTATION_REQUEST_PATTERN = re.compile(
    rf"(?:^\s*(?:(?:please|can\s+you|could\s+you)\s+)?(?:show|read|open|view|display|summari[sz]e|explain|give|provide|share|list|what(?:\s+(?:is|are|does))?|how(?:\s+(?:does|do|is|are))?|which|where)\b[^.?!]{{0,64}}"
    rf"\b{DOCUMENTATION_NAME_PATTERN}\b|"
    rf"^\s*{DOCUMENTATION_NAME_PATTERN}\s*[?.!]*\s*$)",
    re.IGNORECASE,
)
DOCUMENTATION_EXCLUSION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|dont|not|without|exclude|excluding|omit|omitting|avoid|ignore|"
    rf"irrelevant|unrelated)\b.{{0,60}}\b{DOCUMENTATION_NAME_PATTERN}\b|"
    rf"\b{DOCUMENTATION_NAME_PATTERN}\b.{{0,40}}\b"
    r"(?:not\s+relevant|irrelevant|unrelated|should\s+not\s+be\s+cited)\b",
    re.IGNORECASE,
)

# Natural-language component names do not always include a literal filename
# such as ``SiteHeader.jsx``. These hints bridge that gap without treating a
# whole repository as relevant.
# Hint tokens are specific folder/file words. Generic monorepo tokens such as
# ``app``, ``src``, ``main``, ``server`` or ``page`` match whole subtrees of a
# large repository, so they never act as hints for an ordinary question.
# "page" is product vocabulary ("the booking page") rather than a request for
# UI code, so only explicit UI wording selects the frontend family.
QUESTION_PATH_HINTS = (
    (re.compile(r"\b(?:frontend|front[- ]?end|user\s+interface|ui|react\s+components?|components?|styling|styles?|css|stylesheets?)\b", re.IGNORECASE),
     ("frontend", "front-end", "src", "components", "component", "ui", "styles", "style", "css")),
    (re.compile(r"\b(?:nav(?:igation)?(?:\s*bar)?|navbar|menu|header)\b", re.IGNORECASE),
     ("nav", "navbar", "navigation", "header", "siteheader")),
    (re.compile(r"\b(?:sidebar|side\s*bar)\b", re.IGNORECASE), ("sidebar", "side-bar")),
    (re.compile(r"\b(?:chat|conversation|message)\b", re.IGNORECASE), ("chat", "conversation", "message")),
    (re.compile(r"\b(?:auth|authentication|authorization|authenticate|authorize|sign[ -]?in|login|oauth)\b", re.IGNORECASE),
     ("auth", "authentication", "authorization", "authenticate", "authorize", "authprovider", "identity", "login", "signin", "oauth", "session", "security", "middleware")),
    (re.compile(r"\b(?:api|apis|endpoint|endpoints|route|routes|router|routers|rest)\b", re.IGNORECASE),
     ("api", "apis", "endpoint", "endpoints", "route", "routes", "router", "routers", "controller", "controllers")),
    (re.compile(r"\b(?:database|databases|db|schema|sql|migration|migrations)\b", re.IGNORECASE),
     ("database", "db", "schema", "sql", "migration", "migrations", "model", "models", "prisma")),
    (re.compile(r"\b(?:index|indexing|ingest|ingestion|clone|cloning|chunk|chunking|embed|embedding|worker|queue|summar(?:y|ize|izer)|manifest)\b", re.IGNORECASE),
     ("index", "indexing", "ingest", "ingestion", "clone", "cloning", "pipeline", "chunk", "chunker", "embed", "embedder", "embedding", "worker", "queue", "summarizer", "manifest")),
    (re.compile(r"\b(?:retriev(?:e|al)|semantic|keyword|vector|citation|citations|evidence)\b", re.IGNORECASE),
     ("retriev", "retriever", "search", "vector", "embed", "citation", "evidence")),
)
NARROW_COMPONENT_PATTERN = re.compile(
    r"\b(?:nav(?:igation)?(?:\s*bar)?|navbar|menu|header|sidebar|side\s*bar|chat|conversation|message)\b",
    re.IGNORECASE,
)
LOCATION_QUERY_PATTERN = re.compile(
    r"\b(?:where|which|what)\b.{0,60}\b(?:located|location|defined|implemented|live|lives|entry\s*point|file|path|endpoint|route|api)\b|"
    r"\b(?:locate|find|show)\b.{0,40}\b(?:api|endpoint|route|router|handler|entry\s*point|file)\b",
    re.IGNORECASE,
)
API_QUERY_PATTERN = re.compile(
    r"\b(?:api|apis|endpoint|endpoints|route|routes|router|routers|handler|handlers|rest)\b",
    re.IGNORECASE,
)

# Ordinary question words create very broad FTS matches (for example, a
# question containing ``where`` can match dozens of comments and README
# paragraphs). Remove them before sparse retrieval; technical terms remain.
QUERY_STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "does", "doing", "from",
    "give", "how", "into", "its", "just", "like", "located", "location", "make",
    "please", "show", "that", "the", "this", "what", "when", "where", "which",
    "with", "would", "your", "project", "repository", "codebase", "code", "work",
    # Usage verbs prefix-match unrelated identifiers ("used" -> useDebounce);
    # usage questions search call sites by identifier instead.
    "used", "uses", "called", "calls", "usage",
}


def is_overview_file(file_path: str) -> bool:
    """Return whether a path is orientation/documentation-only evidence."""
    basename = file_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return basename in OVERVIEW_FILE_NAMES


def is_explicit_overview_request(query: str) -> bool:
    """Return whether the query directly asks for documentation evidence.

    README/docs paths frequently appear in exclusion instructions or in a
    sentence explaining that documentation is not the implementation. Only a
    direct documentation request may opt those files back into retrieval.
    """
    if DOCUMENTATION_EXCLUSION_PATTERN.search(query):
        return False
    return bool(DOCUMENTATION_REQUEST_PATTERN.search(query))


def overview_file_sql(alias: str = "c") -> str:
    """Build a static SQL predicate for overview-file exclusion.

    The names are constants, not user input, so this remains parameter-safe
    while allowing both root and nested README paths.
    """
    parts = []
    for name in sorted(OVERVIEW_FILE_NAMES):
        parts.extend([f"lower({alias}.file_path) = '{name}'", f"lower({alias}.file_path) LIKE '%/{name}'"])
    return "(" + " OR ".join(parts) + ")"


def escape_like(value: str) -> str:
    """Escape user-derived path text before it is placed in a LIKE argument."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# Codebases often name authentication modules `auth`, not `authentication`.
# These small, domain-specific expansions improve keyword retrieval without
# treating a broad README as proof for an implementation-level answer.
RETRIEVAL_TERM_ALIASES = {
    "authentication": ("auth", "login", "oauth", "session"),
    "authorization": ("authorize", "auth", "permission", "role"),
    "authenticate": ("auth", "login", "oauth", "session"),
    "authorize": ("authorization", "auth", "permission", "role"),
    "signin": ("sign_in", "login", "oauth", "auth"),
    "login": ("auth", "signin", "oauth", "session"),
}

EVIDENCE_WORKFLOWS = {
    "general": {
        "label": "Repository question",
        "focus": "Answer the question from the most directly relevant source files.",
        "terms": (),
        "path_hints": (),
    },
    "onboarding": {
        "label": "New engineer onboarding",
        "focus": "Explain the entry points, main flow, configuration, and first files a new engineer should read.",
        "terms": ("entrypoint", "main", "app", "config", "route", "service", "readme"),
        "path_hints": ("readme", "main", "app", "config", "route", "server", "index"),
    },
    "security": {
        "label": "Security review",
        "focus": "Trace authentication, authorization, trust boundaries, secrets, validation, and externally reachable handlers.",
        "terms": ("auth", "authentication", "authorization", "permission", "token", "session", "secret", "middleware", "webhook", "upload", "cors", "route", "validate", "sanitize"),
        "path_hints": ("auth", "middleware", "security", "permission", "token", "session", "config", "route", "api", "test"),
    },
    "architecture": {
        "label": "Architecture interview",
        "focus": "Describe components, boundaries, data flow, persistence, and integration points without inventing relationships.",
        "terms": ("module", "package", "service", "route", "config", "schema", "model", "database", "worker", "queue"),
        "path_hints": ("app", "api", "backend", "service", "worker", "config", "schema", "model"),
    },
    "contributor": {
        "label": "Open-source contributor",
        "focus": "Point to local development, tests, conventions, CI, and the smallest safe change surface.",
        "terms": ("contributing", "readme", "test", "tests", "ci", "workflow", "package", "script", "config"),
        "path_hints": ("contribut", "test", "spec", "ci", "workflow", "package", "readme"),
    },
    "due_diligence": {
        "label": "Technical due diligence",
        "focus": "Summarize deployment, dependencies, license, tests, configuration, and operational risks supported by the index.",
        "terms": ("license", "readme", "deploy", "docker", "config", "dependency", "test", "ci", "security"),
        "path_hints": ("license", "readme", "docker", "deploy", "config", "test", "ci", "package"),
    },
    "editing": {
        "label": "Code editing and PR",
        "focus": "Review one explicitly named source file and propose the smallest exact-match change that fixes the request.",
        "terms": ("fix", "bug", "change", "edit", "patch", "review", "test", "validation"),
        "path_hints": (),
    },
}


def extract_requested_file_paths(query: str) -> list[str]:
    paths: list[str] = []
    allow_overview = is_explicit_overview_request(query)
    for match in FILE_PATH_PATTERN.finditer(query):
        path = match.group(1)
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith(".") and "/" not in path and path.count(".") == 1 and len(path) <= 6:
            # A bare extension such as ".ts" is not a file.
            continue
        if "/" not in path and path.lower() in DOTTED_TECHNOLOGY_NAMES:
            continue
        if (not is_overview_file(path) or allow_overview) and path not in paths:
            paths.append(path)
    return paths


def extract_question_path_hints(query: str) -> list[str]:
    """Infer a small set of component-name path hints from natural language."""
    hints: list[str] = []
    for pattern, candidates in QUESTION_PATH_HINTS:
        if pattern.search(query):
            hints.extend(candidates)
    return list(dict.fromkeys(hints))[:16]


def is_narrow_component_question(query: str) -> bool:
    """Identify UI-component questions where unrelated files are noise."""
    return bool(NARROW_COMPONENT_PATTERN.search(query))


def is_location_question(query: str) -> bool:
    """Identify requests for the implementation location of a feature.

    Location questions need a small, path-focused evidence set. Returning
    semantically similar files merely to fill ``top_k`` is especially harmful
    here because the user is asking *where* the implementation lives.
    """
    return bool(LOCATION_QUERY_PATTERN.search(query))


def is_api_question(query: str) -> bool:
    """Identify API/route questions so retrieval can prefer server handlers."""
    return bool(API_QUERY_PATTERN.search(query))


def is_strict_target_question(
    query: str,
    requested_paths: list[str],
    include_overview_files: bool = False,
) -> bool:
    """Return whether evidence should be limited to explicit/path-targeted files."""
    # An explicit documentation request is allowed to use README/docs
    # evidence. A named file still follows the exact-file contract below.
    if include_overview_files and not requested_paths:
        return False
    return bool(
        requested_paths
        or is_narrow_component_question(query)
        or is_location_question(query)
        or is_api_question(query)
        or bool(extract_question_path_hints(query))
    )


def path_matches_hints(file_path: str, hints: list[str]) -> bool:
    """Match a path hint on filename/directory boundaries, not arbitrary text.

    Boundary matching prevents a hint such as ``api`` from selecting unrelated
    names like ``capillary.py`` while still matching ``api_client.py`` and
    ``backend/api/routes.py``.
    """
    tokens = [token for token in re.split(r"[^a-z0-9]+", file_path.lower()) if token]
    normalized_tokens = set(tokens)
    # Treat simple plural filenames (routes.py, apis.ts) as the same path
    # family as their singular query term without using substring matching.
    normalized_tokens.update(token[:-1] for token in tokens if token.endswith("s") and len(token) > 3)
    return any(hint.lower() in normalized_tokens for hint in hints)


def is_exploratory_repository_question(query: str, requested_paths: list[str]) -> bool:
    return not requested_paths and bool(EXPLORATORY_REPOSITORY_PATTERN.search(query))


def question_requests_overview_files(query: str, requested_paths: list[str]) -> bool:
    """Allow orientation files only when the user asks for them explicitly."""
    # A path such as README.md is often mentioned to say what must *not* be
    # cited. It is not an evidence request in that case.
    if DOCUMENTATION_EXCLUSION_PATTERN.search(query):
        return False
    return any(is_overview_file(path) for path in requested_paths) or is_explicit_overview_request(query)


def is_impact_question(query: str) -> bool:
    return bool(IMPACT_QUESTION_PATTERN.search(query))


def extract_issue_reference(query: str) -> str | None:
    """Return a bounded GitHub issue number when the user supplies one."""
    match = ISSUE_REFERENCE_PATTERN.search(str(query or ""))
    return match.group(1) if match else None


def _chunk_lines(chunk: Dict) -> list[str] | None:
    """Content lines when they align exactly with the chunk's line range."""
    try:
        start, end = int(chunk["start_line"]), int(chunk["end_line"])
    except (KeyError, TypeError, ValueError):
        return None
    lines = str(chunk.get("content") or "").split("\n")
    return lines if end >= start and len(lines) == end - start + 1 else None


def merge_overlapping_chunk(selected: Dict, candidate: Dict, max_lines: int = MAX_MERGED_CHUNK_LINES) -> Dict | None:
    """Merge two overlapping/adjacent chunks of one file into one span.

    Returns the merged chunk, or ``None`` when the chunks do not touch, their
    content does not align with their line numbers, or the merged span would
    be too long. The higher-ranked ``selected`` chunk keeps its identity.
    """
    if selected.get("file_path") != candidate.get("file_path"):
        return None
    first_lines, second_lines = _chunk_lines(selected), _chunk_lines(candidate)
    if first_lines is None or second_lines is None:
        return None
    a_start, a_end = int(selected["start_line"]), int(selected["end_line"])
    b_start, b_end = int(candidate["start_line"]), int(candidate["end_line"])
    if b_start > a_end + 1 or a_start > b_end + 1:
        return None
    start, end = min(a_start, b_start), max(a_end, b_end)
    if end - start + 1 > max_lines:
        return None
    lines: dict[int, str] = {}
    for base, chunk_lines in ((b_start, second_lines), (a_start, first_lines)):
        for offset, line in enumerate(chunk_lines):
            lines[base + offset] = line
    merged = dict(selected)
    merged["start_line"], merged["end_line"] = start, end
    merged["content"] = "\n".join(lines[number] for number in range(start, end + 1))
    merged["_merged_chunk_ids"] = list(dict.fromkeys([
        *(selected.get("_merged_chunk_ids") or [selected.get("id")]), candidate.get("id"),
    ]))
    merged["_retrieval_methods"] = list(dict.fromkeys([*(selected.get("_retrieval_methods") or []), *(candidate.get("_retrieval_methods") or [])]))
    merged["_retrieval_reasons"] = list(dict.fromkeys([*(selected.get("_retrieval_reasons") or []), *(candidate.get("_retrieval_reasons") or [])]))
    return merged


def select_diverse_chunks(candidates: list[Dict], limit: int, excluded_ids: set[str] | None = None) -> list[Dict]:
    """Pick up to ``limit`` chunks, at most two per file where possible.

    Overlapping or adjacent chunks of one file (the chunker emits overlapping
    windows) are merged into one citation span instead of using two slots for
    largely duplicate text.
    """
    excluded_ids = excluded_ids or set()
    selected: list[Dict] = []
    selected_ids = set(excluded_ids)
    per_file = defaultdict(int)

    def try_merge(chunk: Dict) -> bool:
        for index, other in enumerate(selected):
            merged = merge_overlapping_chunk(other, chunk)
            if merged is not None:
                selected[index] = merged
                selected_ids.add(chunk["id"])
                return True
        return False

    for strict in (True, False):
        for chunk in candidates:
            chunk_id, file_path = chunk["id"], chunk["file_path"]
            if chunk_id in selected_ids:
                continue
            if try_merge(chunk):
                continue
            if strict and per_file[file_path] >= MAX_CHUNKS_PER_FILE:
                continue
            selected.append(chunk)
            selected_ids.add(chunk_id)
            per_file[file_path] += 1
            if len(selected) == limit:
                return selected
    return selected


def search_terms(query: str) -> list[str]:
    """Keep sparse matching resilient to punctuation and provider-unavailable embeddings."""
    terms = [
        word.lower()
        for word in WORD_PATTERN.findall(query)
        if len(word) >= 3 and word.lower() not in QUERY_STOPWORDS
    ]
    for term in list(terms):
        terms.extend(RETRIEVAL_TERM_ALIASES.get(term, ()))
    return list(dict.fromkeys(terms))[:10]


def build_evidence_plan(query: str, workflow: str = "general") -> dict:
    """Create a deterministic retrieval plan that is visible to the client.

    The plan guides sparse retrieval and gives the answer/UI an honest account
    of what was targeted. It does not claim that every target exists in a
    repository; absent targets remain an explicit evidence limitation.
    """
    profile = EVIDENCE_WORKFLOWS.get(workflow, EVIDENCE_WORKFLOWS["general"])
    # Editing requests must not search for generic process words ("fix",
    # "test", "review"): they pull tests and PR-process docs into the
    # candidate set. Those words stay available to the planner as
    # ``workflow_terms``.
    profile_terms = () if workflow == "editing" else profile["terms"]
    terms = list(dict.fromkeys([*search_terms(query), *profile_terms]))[:20]
    # Query-specific hints take precedence over workflow defaults. This keeps
    # a security or due-diligence profile from pushing the actual API/component
    # paths out of the bounded evidence plan.
    path_hints = list(dict.fromkeys([*extract_question_path_hints(query), *profile["path_hints"]]))[:16]
    requested_files = extract_requested_file_paths(query)
    issue_reference = extract_issue_reference(query)
    overview_allowed = (
        is_exploratory_repository_question(query, requested_files)
        or question_requests_overview_files(query, requested_files)
    )
    return {
        "workflow": workflow if workflow in EVIDENCE_WORKFLOWS else "general",
        "label": profile["label"],
        "focus": profile["focus"],
        "search_terms": terms,
        "workflow_terms": list(profile["terms"]),
        "path_hints": path_hints,
        "requested_files": requested_files,
        "issue_reference": issue_reference,
        "query_scope": _query_scope(query, requested_files, issue_reference),
        "overview_files_allowed": overview_allowed,
        "tests_requested": question_wants_tests(query),
        "docs_requested": question_wants_docs(query),
    }


def _query_scope(query: str, requested_files: list[str], issue_reference: str | None) -> str:
    if issue_reference:
        return "issue"
    if requested_files:
        return "explicit_file"
    if is_narrow_component_question(query):
        return "targeted_component"
    if is_location_question(query):
        return "targeted_api_location" if is_api_question(query) else "targeted_location"
    if is_exploratory_repository_question(query, requested_files):
        return "repository_overview"
    if extract_question_path_hints(query):
        return "targeted_topic"
    return "general"


_FTS_TERM_CLEAN = re.compile(r"[^0-9A-Za-z_]")


def fts_match_expression(terms: list[str]) -> str:
    """Build a safe FTS5 MATCH expression with prefix matching.

    unicode61 keeps camelCase identifiers as one token, so a natural-language
    term like ``booking`` must use a prefix query to reach ``bookingservice``
    or the plural ``bookings``. Very short terms stay exact to avoid noise.
    Every term is reduced to word characters, so user text can never inject
    FTS operators.
    """
    parts: list[str] = []
    for term in terms:
        clean = _FTS_TERM_CLEAN.sub("", str(term)).lower()
        if not clean:
            continue
        parts.append(f'"{clean}"*' if len(clean) >= 4 else f'"{clean}"')
    return " OR ".join(dict.fromkeys(parts))


def exclude_staged(rows: list[Dict], staging_prefix: str | None) -> list[Dict]:
    """Hide replacement rows of an in-progress refresh from every result."""
    if not staging_prefix:
        return rows
    return [row for row in rows if not str(row.get("id") or "").startswith(staging_prefix)]


async def sparse_search(
    store,
    repo_id: str,
    query: str,
    limit: int = 20,
    plan: dict | None = None,
    include_overview_files: bool = False,
    staging_prefix: str | None = None,
) -> list[Dict]:
    terms = list(dict.fromkeys([*search_terms(query), *(plan or {}).get("search_terms", [])]))[:20]
    if not terms:
        return []
    # FTS5 is maintained with triggers in turso/00_init.sql. It avoids a
    # repository-wide LIKE scan for ordinary natural-language questions. Keep
    # the LIKE implementation as a compatibility fallback for databases that
    # were created before this migration.
    fts_expression = fts_match_expression(terms)
    if not fts_expression:
        return []
    overview_filter = "" if include_overview_files else f" AND NOT {overview_file_sql('c')}"
    staging_filter, staging_args = ("", []) if not staging_prefix else (" AND c.id NOT LIKE ?", [f"{staging_prefix}%"])
    try:
        # bm25 column weights: a match in the file path (4x) is stronger
        # evidence of the right folder than an incidental word in content.
        rows = await store.fetch_all(
            "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content, "
            "bm25(chunks_fts, 1.0, 4.0) AS score FROM chunks_fts "
            "JOIN chunks c ON c.rowid = chunks_fts.rowid "
            f"WHERE chunks_fts MATCH ? AND c.repo_id = ?{overview_filter}{staging_filter} "
            "ORDER BY score ASC, c.file_path ASC, c.start_line ASC LIMIT ?",
            [fts_expression, repo_id, *staging_args, limit],
        )
        return exclude_staged(rows, staging_prefix)
    except Exception as error:
        logger.warning("Turso FTS unavailable; using bounded keyword fallback (%s)", type(error).__name__)
    score_parts = ["CASE WHEN lower(content) LIKE ? OR lower(file_path) LIKE ? THEN 1 ELSE 0 END" for _ in terms]
    matching_parts = ["(lower(content) LIKE ? OR lower(file_path) LIKE ?)" for _ in terms]
    score_args = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
    sql = (
        "SELECT id, file_path, start_line, end_line, language, symbols, content, "
        f"({' + '.join(score_parts)}) AS score FROM chunks c WHERE c.repo_id = ? "
        f"AND ({' OR '.join(matching_parts)}){overview_filter} "
        "ORDER BY score DESC, file_path ASC, start_line ASC LIMIT ?"
    )
    rows = await store.fetch_all(sql, [*score_args, repo_id, *score_args, limit])
    # The compatibility query uses LIKE for broad SQLite support. LIKE can
    # mistake a substring in a filename (``capillary.py``) for an API path;
    # retain such a row only when its content matches a query term or its path
    # matches one of the evidence-plan tokens on a real boundary.
    path_hints = (plan or {}).get("path_hints") or []
    if path_hints:
        rows = [
            row for row in rows
            if any(term in str(row.get("content") or "").lower() for term in terms)
            or any(path_matches_hints(str(row.get("file_path") or ""), [hint]) for hint in path_hints)
        ]
    return exclude_staged(rows, staging_prefix)


async def dense_search(
    store,
    repo_id: str,
    query_embedding: list[float],
    limit: int = 20,
    include_overview_files: bool = False,
    candidate_terms: list[str] | None = None,
    candidate_limit: int | None = None,
) -> list[Dict]:
    """Dense-rerank a bounded lexical candidate set.

    Turso does not provide a usable ANN index for the current 2048-dimensional
    embeddings.  Running cosine distance over every chunk makes each question
    proportional to the whole repository.  Candidate-first retrieval keeps
    semantic reranking bounded while sparse retrieval remains the fallback for
    genuinely semantic queries with no lexical hit.
    """
    import json
    overview_filter = "" if include_overview_files else f" AND NOT {overview_file_sql('chunks')}"
    terms = list(dict.fromkeys(
        str(term).strip().lower() for term in (candidate_terms or [])
        if str(term).strip()
    ))[:20]
    if not terms:
        return []
    candidate_limit = max(limit, min(settings.dense_candidate_limit, int(candidate_limit or settings.dense_candidate_limit)))
    matching_parts = ["(lower(content) LIKE ? OR lower(file_path) LIKE ?)" for _ in terms]
    term_args = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
    candidate_rows = await store.fetch_all(
        "SELECT id FROM chunks WHERE repo_id = ? AND embedding IS NOT NULL"
        f"{overview_filter} AND ({' OR '.join(matching_parts)}) LIMIT ?",
        [repo_id, *term_args, candidate_limit],
    )
    ids = [row.get("id") for row in candidate_rows if row.get("id")]
    if not ids:
        # Preserve semantic recall for a genuinely abstract question whose
        # terms occur nowhere in source. This is the exceptional slow path;
        # ordinary questions use the bounded candidate query above, while
        # sparse/overview retrieval still supplies a safe fallback.
        return await store.fetch_all(
            "SELECT id, file_path, start_line, end_line, language, symbols, content, "
            "vector_distance_cos(embedding, vector32(?)) AS distance "
            f"FROM chunks WHERE repo_id = ? AND embedding IS NOT NULL{overview_filter} "
            "ORDER BY distance ASC LIMIT ?",
            [json.dumps(query_embedding), repo_id, limit],
        )
    placeholders = ",".join("?" for _ in ids)
    return await store.fetch_all(
        "SELECT id, file_path, start_line, end_line, language, symbols, content, "
        "vector_distance_cos(embedding, vector32(?)) AS distance "
        f"FROM chunks WHERE repo_id = ? AND id IN ({placeholders}) AND embedding IS NOT NULL{overview_filter} "
        "ORDER BY distance ASC LIMIT ?",
        [json.dumps(query_embedding), repo_id, *ids, limit],
    )


async def semantic_file_search(store, repo_id: str, query_embedding, limit: int) -> list[Dict]:
    """Rank files by their centroid vector (true semantic recall, bounded).

    One row per file keeps this proportional to the number of files rather
    than chunks, and it finds the right file even when the question shares no
    words with the code (for example "how are users charged?" -> billing).
    """
    from database import embedding_blob

    try:
        return await store.fetch_all(
            "SELECT file_path, chunk_count, vector_distance_cos(embedding, vector32(?)) AS distance "
            "FROM file_vectors WHERE repo_id = ? AND embedding IS NOT NULL ORDER BY distance ASC LIMIT ?",
            [embedding_blob(query_embedding), repo_id, max(1, int(limit))],
        )
    except Exception as error:
        # Databases created before migration 06 have no file_vectors table.
        logger.debug("File-level semantic search unavailable (%s)", type(error).__name__)
        return []


async def semantic_chunks_in_files(
    store,
    repo_id: str,
    query_embedding,
    file_paths: list[str],
    limit: int,
    staging_prefix: str | None = None,
    per_file_limit: int = 3,
) -> list[Dict]:
    """Rerank only the chunks of the best files by exact cosine distance."""
    from database import embedding_blob

    paths = list(dict.fromkeys(path for path in file_paths if path))
    if not paths:
        return []
    placeholders = ",".join("?" for _ in paths)
    staging_filter, staging_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    args = [embedding_blob(query_embedding), repo_id, *paths, *staging_args]
    try:
        # Cap each file so one large file (often a test suite) cannot take
        # every slot of the global top-k.
        rows = await store.fetch_all(
            "WITH scored AS (SELECT id, file_path, start_line, end_line, language, symbols, content, "
            "vector_distance_cos(embedding, vector32(?)) AS distance FROM chunks "
            f"WHERE repo_id = ? AND file_path IN ({placeholders}) AND embedding IS NOT NULL{staging_filter}), "
            "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY file_path ORDER BY distance ASC) AS file_rank FROM scored) "
            "SELECT id, file_path, start_line, end_line, language, symbols, content, distance FROM ranked "
            "WHERE file_rank <= ? ORDER BY distance ASC LIMIT ?",
            [*args, max(1, int(per_file_limit)), max(1, int(limit))],
        )
    except Exception as error:
        logger.debug("Windowed semantic chunk search unavailable (%s)", type(error).__name__)
        rows = await store.fetch_all(
            "SELECT id, file_path, start_line, end_line, language, symbols, content, "
            "vector_distance_cos(embedding, vector32(?)) AS distance FROM chunks "
            f"WHERE repo_id = ? AND file_path IN ({placeholders}) AND embedding IS NOT NULL{staging_filter} "
            "ORDER BY distance ASC LIMIT ?",
            [*args, max(1, int(limit))],
        )
    return exclude_staged(rows, staging_prefix)


async def semantic_rerank_ids(
    store,
    repo_id: str,
    query_embedding,
    chunk_ids: list[str],
    limit: int,
) -> list[Dict]:
    """Exact cosine rerank of a bounded candidate set (keyword/path hits)."""
    from database import embedding_blob

    ids = list(dict.fromkeys(str(value) for value in chunk_ids if value))[: settings.dense_candidate_limit]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return await store.fetch_all(
        "SELECT id, file_path, start_line, end_line, language, symbols, content, "
        "vector_distance_cos(embedding, vector32(?)) AS distance FROM chunks "
        f"WHERE repo_id = ? AND id IN ({placeholders}) AND embedding IS NOT NULL "
        "ORDER BY distance ASC LIMIT ?",
        [embedding_blob(query_embedding), repo_id, *ids, max(1, int(limit))],
    )


async def requested_file_chunks(store, repo_id: str, file_path: str, limit: int) -> list[Dict]:
    exact_or_suffix = escape_like(file_path.lower()) if "/" in file_path else f"%/{escape_like(file_path.lower())}"
    return await store.fetch_all(
        "SELECT id, file_path, start_line, end_line, language, symbols, content FROM chunks "
        "WHERE repo_id = ? AND (lower(file_path) = ? OR lower(file_path) LIKE ? ESCAPE '\\') ORDER BY file_path, start_line LIMIT ?",
        [repo_id, file_path.lower(), exact_or_suffix, limit],
    )


async def requested_files_chunks(store, repo_id: str, file_paths: list[str], limit: int, staging_prefix: str | None = None) -> list[Dict]:
    """Fetch all explicitly requested files in one database round trip.

    Explicit paths are an exact citation contract: the caller will return only
    these rows. A single OR query retains the previous per-path candidate
    budget while avoiding one network round trip per path.
    """
    if not file_paths:
        return []
    conditions: list[str] = []
    args: list[object] = [repo_id]
    for file_path in file_paths:
        exact_or_suffix = escape_like(file_path.lower()) if "/" in file_path else f"%/{escape_like(file_path.lower())}"
        conditions.append("(lower(file_path) = ? OR lower(file_path) LIKE ? ESCAPE '\\')")
        args.extend([file_path.lower(), exact_or_suffix])
    # Hidden refresh rows are excluded in SQL so they never consume the limit.
    staging_filter = ""
    if staging_prefix:
        staging_filter = " AND id NOT LIKE ?"
        args.append(f"{staging_prefix}%")
    args.append(max(1, limit) * len(file_paths))
    return await store.fetch_all(
        "SELECT id, file_path, start_line, end_line, language, symbols, content FROM chunks "
        f"WHERE repo_id = ? AND ({' OR '.join(conditions)}){staging_filter} ORDER BY file_path, start_line LIMIT ?",
        args,
    )


async def complete_files_chunks(
    store,
    repo_id: str,
    file_paths: list[str],
    per_file_limit: int,
    staging_prefix: str | None = None,
) -> list[Dict]:
    """Fetch a bounded, complete source sequence for each selected file.

    Ordinary retrieval intentionally samples chunks. Code editing cannot: a
    patch generated from only the first two chunks can miss an import, a
    caller, or the second half of the requested function. Windowing applies
    the limit independently per file in one Turso round trip, preserving line
    order without allowing a large file to starve the other edit targets.
    """
    paths = list(dict.fromkeys(str(path).strip() for path in file_paths if str(path).strip()))
    if not paths:
        return []
    placeholders = ",".join("?" for _ in paths)
    # Filter hidden refresh rows before windowing so a changed file's
    # replacement rows cannot use up its per-file budget.
    staging_filter, staging_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    args: list[object] = [repo_id, *paths, *staging_args, max(1, int(per_file_limit))]
    try:
        return await store.fetch_all(
            "WITH ranked AS ("
            "SELECT id, file_path, start_line, end_line, language, symbols, content, "
            "ROW_NUMBER() OVER (PARTITION BY file_path ORDER BY start_line, id) AS file_rank "
            f"FROM chunks WHERE repo_id = ? AND file_path IN ({placeholders}){staging_filter}"
            ") SELECT id, file_path, start_line, end_line, language, symbols, content "
            "FROM ranked WHERE file_rank <= ? ORDER BY file_path, start_line, id",
            args,
        )
    except Exception as error:
        # Older SQLite/libSQL deployments may not support window functions.
        # The compatibility path is deliberately bounded and still returns
        # the complete prefix for every selected file.
        logger.warning("Windowed editing retrieval unavailable; using bounded file queries (%s)", type(error).__name__)
        rows: list[Dict] = []
        for path in paths:
            rows.extend(await requested_file_chunks(store, repo_id, path, per_file_limit))
        rows = exclude_staged(rows, staging_prefix)
        return sorted(rows, key=lambda row: (str(row.get("file_path", "")), int(row.get("start_line", 0))))


async def dependency_file_paths(store, repo_id: str, target_paths: list[str], limit: int = 8) -> list[str]:
    """Return local import/caller paths for an editing target.

    The graph is conservative and contains only resolved same-repository
    edges. Including both directions gives the code model the imports a file
    uses and the callers that must continue to compile after the change.
    """
    paths = list(dict.fromkeys(str(path).strip() for path in target_paths if str(path).strip()))
    if not paths:
        return []
    placeholders = ",".join("?" for _ in paths)
    rows = await store.fetch_all(
        "SELECT source_file, target_file FROM repo_dependencies WHERE repo_id = ? AND "
        f"(source_file IN ({placeholders}) OR target_file IN ({placeholders})) "
        "ORDER BY source_file, target_file LIMIT ?",
        [repo_id, *paths, *paths, max(1, int(limit))],
    )
    related: list[str] = []
    for row in rows:
        for value in (row.get("source_file"), row.get("target_file")):
            value = str(value or "").strip()
            if value and value not in paths and value not in related:
                related.append(value)
    return related[: max(1, int(limit))]


def _editing_candidate_paths(
    chunks: list[Dict],
    requested_paths: list[str],
    max_files: int,
) -> list[str]:
    """Rank files, rather than chunks, before expanding editing evidence."""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for rank, chunk in enumerate(chunks):
        path = str(chunk.get("file_path") or "").strip()
        if not path or is_overview_file(path):
            continue
        order.setdefault(path, rank)
        score = float(chunk.get("_relevance_score") or 0.0)
        methods = set(chunk.get("_retrieval_methods") or [])
        if "workflow_target" in methods:
            score += 1.0
        # Earlier ranked chunks and multiple independent matches are useful
        # signals, but never outweigh an explicitly named target.
        contribution = score + 1.0 / (rank + 1)
        plan = chunk.get("_evidence_plan") or {}
        # A change request targets implementation code: tests, specs and
        # prose docs are edit targets only when the request is about them.
        if (is_test_path(path) and not plan.get("tests_requested")) or (is_doc_path(path) and not plan.get("docs_requested")):
            contribution *= 0.1
        scores[path] = scores.get(path, 0.0) + contribution
    explicit = [path for path in requested_paths if path and not is_overview_file(path)]
    ranked = sorted(scores, key=lambda path: (-scores[path], order[path], path))
    result: list[str] = []
    for path in [*explicit, *ranked]:
        if path not in result:
            result.append(path)
        if len(result) >= max(1, int(max_files)):
            break
    return result


async def expand_editing_evidence(
    store,
    repo_id: str,
    candidate_chunks: list[Dict],
    requested_paths: list[str],
    *,
    issue_request: bool,
    staging_prefix: str | None = None,
) -> list[Dict]:
    """Replace sampled editing candidates with complete bounded file context."""
    if not candidate_chunks and not requested_paths:
        return []
    max_files = max(1, int(settings.editing_max_files))
    # Reserve a couple of slots for resolved callers/imports on issue-driven
    # edits. Without this reservation the ranked candidates could fill the
    # entire file budget before the dependency graph was consulted.
    candidate_limit = max(1, max_files - 2) if issue_request else max_files
    candidate_limit = max(candidate_limit, len(requested_paths))
    target_paths = _editing_candidate_paths(candidate_chunks, requested_paths, candidate_limit)
    if issue_request and target_paths:
        try:
            related = await dependency_file_paths(
                store, repo_id, target_paths[:max_files], limit=max_files * 2,
            )
        except Exception as error:
            logger.warning("Editing dependency retrieval unavailable for %s (%s)", repo_id, type(error).__name__)
            related = []
        for path in related:
            if path not in target_paths and len(target_paths) < max_files:
                target_paths.append(path)
    if not target_paths:
        return candidate_chunks
    rows = await complete_files_chunks(
        store, repo_id, target_paths, settings.editing_max_chunks_per_file, staging_prefix=staging_prefix,
    )
    if not rows:
        return candidate_chunks
    candidate_by_id = {str(chunk.get("id")): chunk for chunk in candidate_chunks}
    path_rank = {path: index for index, path in enumerate(target_paths)}
    expanded: list[Dict] = []
    for row in sorted(rows, key=lambda item: (path_rank.get(str(item.get("file_path")), max_files), int(item.get("start_line", 0)))):
        base = dict(candidate_by_id.get(str(row.get("id")), row))
        base.update({
            "id": row.get("id"), "file_path": row.get("file_path"),
            "start_line": row.get("start_line"), "end_line": row.get("end_line"),
            "language": row.get("language"), "symbols": row.get("symbols") or [],
            "content": row.get("content") or "",
        })
        is_explicit_target = str(row.get("file_path")) in requested_paths
        methods = list(dict.fromkeys([
            *(base.get("_retrieval_methods") or []),
            # Preserve the exact-file method for compatibility and clearer
            # citations. The additional method is useful only for inferred
            # candidates that were expanded from sampled evidence.
            *([] if is_explicit_target else ["editing_target_file"]),
        ]))
        reasons = list(dict.fromkeys([
            *(base.get("_retrieval_reasons") or []),
            "Complete bounded source for a proposed code edit",
        ]))
        if is_explicit_target:
            reasons.append("Explicit file path requested in the question")
        base["_retrieval_methods"] = methods
        base["_retrieval_reasons"] = list(dict.fromkeys(reasons))
        expanded.append(base)
    return expanded


async def path_hint_chunks(
    store,
    repo_id: str,
    hint: str,
    limit: int = 2,
    include_overview_files: bool = False,
) -> list[Dict]:
    """Fetch a small representative sample for an evidence-plan path hint."""
    overview_filter = "" if include_overview_files else f" AND NOT {overview_file_sql('chunks')}"
    return await store.fetch_all(
        "SELECT id, file_path, start_line, end_line, language, symbols, content FROM chunks "
        f"WHERE repo_id = ? AND lower(file_path) LIKE ?{overview_filter} ORDER BY file_path, start_line LIMIT ?",
        [repo_id, f"%{hint.lower()}%", limit],
    )


async def path_hint_chunks_batch(
    store,
    repo_id: str,
    hints: list[str],
    limit: int = 2,
    include_overview_files: bool = False,
) -> list[Dict]:
    """Fetch the bounded result for every path hint in one SQL statement.

    The window function applies the same ``LIMIT`` independently to each hint
    that the former one-query-per-hint implementation used. Rows carry the
    matching hint so callers can preserve the evidence-plan explanations.
    """
    hints = list(dict.fromkeys(hints))
    if not hints:
        return []
    values = ", ".join("(?, ?, ?)" for _ in hints)
    args: list[object] = []
    for hint_index, hint in enumerate(hints):
        args.extend([hint_index, hint, f"%{hint.lower()}%"])
    args.extend([repo_id, max(1, limit)])
    overview_filter = "" if include_overview_files else f" AND NOT {overview_file_sql('c')}"
    try:
        rows = await store.fetch_all(
            "WITH hints(hint_order, hint, pattern) AS (VALUES " + values + "), "
            "ranked AS ("
            "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content, "
            "h.hint AS matched_hint, h.hint_order AS hint_order, "
            "ROW_NUMBER() OVER (PARTITION BY h.hint ORDER BY c.file_path, c.start_line) AS hint_rank "
            "FROM chunks c JOIN hints h ON lower(c.file_path) LIKE h.pattern "
            f"WHERE c.repo_id = ?{overview_filter}) "
            "SELECT id, file_path, start_line, end_line, language, symbols, content, matched_hint, hint_order "
            "FROM ranked WHERE hint_rank <= ? ORDER BY hint_order, file_path, start_line",
            args,
        )
        return rows
    except Exception as error:
        # Keep repositories created on older SQLite/libSQL versions working;
        # the compatibility path has identical per-hint ordering and limits.
        logger.warning("Batched path-hint retrieval unavailable; using compatibility queries (%s)", type(error).__name__)
        rows: list[Dict] = []
        for hint_order, hint in enumerate(hints):
            for chunk in await path_hint_chunks(
                store, repo_id, hint, limit=limit, include_overview_files=include_overview_files
            ):
                row = dict(chunk)
                row["matched_hint"] = hint
                row["hint_order"] = hint_order
                rows.append(row)
        return rows


async def dependent_file_chunks(store, repo_id: str, target_paths: list[str], limit: int = 20) -> list[Dict]:
    """Retrieve files that import a target according to the resolved graph."""
    if not target_paths:
        return []
    target_conditions = []
    target_args = []
    for path in target_paths:
        escaped_path = escape_like(path)
        target_conditions.append("(d.target_file = ? OR d.target_file LIKE ? ESCAPE '\\')")
        target_args.extend([path, f"%/{escaped_path}"])
    return await store.fetch_all(
        "SELECT DISTINCT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content "
        "FROM repo_dependencies d JOIN chunks c ON c.repo_id = d.repo_id AND c.file_path = d.source_file "
        f"WHERE d.repo_id = ? AND ({' OR '.join(target_conditions)}) "
        "ORDER BY c.file_path, c.start_line LIMIT ?",
        [repo_id, *target_args, limit],
    )


_LOCALE_PATH = re.compile(r"(?:^|/)(?:locales?|i18n|l10n|translations|lang)/")
_MIGRATION_PATH = re.compile(r"(?:^|/)migrations?/")
_I18N_QUESTION = re.compile(r"\b(?:i18n|l10n|translat\w*|locali[sz]\w*|locale\w*|language strings?|copy|wording)\b", re.IGNORECASE)
_MIGRATION_QUESTION = re.compile(r"\bmigrat\w*\b", re.IGNORECASE)
# Tests, specs, end-to-end suites, mocks and fixtures repeat the domain
# vocabulary of the code they exercise, so they collect keyword and semantic
# votes at once. They are evidence only when the question is about tests.
TEST_PATH_PATTERN = re.compile(
    r"(?:\.|/|_|-)(?:test|tests|spec|specs|e2e-spec|e2e|integration-test|integration\.test|stories|story)\.[a-z0-9]+$|"
    r"(?:^|/)(?:__tests__|__mocks__|__fixtures__|tests?|e2e|playwright|cypress|fixtures?|mocks?|testing|test-utils|__snapshots__)/",
    re.IGNORECASE,
)
TEST_QUESTION_PATTERN = re.compile(
    r"\b(?:tests?|testing|tested|unit[- ]tests?|spec|specs|e2e|end[- ]to[- ]end tests?|playwright|cypress|vitest|jest|"
    r"mocks?|mocked|fixtures?|coverage|storybook|stor(?:y|ies))\b",
    re.IGNORECASE,
)
# Prose documentation and agent/skill instruction files describe behaviour
# but are not the implementation. They are opt-in like README is.
DOC_PATH_PATTERN = re.compile(
    r"\.(?:md|mdx|markdown|rst|txt|adoc)$|(?:^|/)(?:docs?|documentation|agents|\.agents|\.claude|\.cursor|guides?|specs)/",
    re.IGNORECASE,
)
DOC_QUESTION_PATTERN = re.compile(
    r"\b(?:docs?|documentation|documented|readme|guides?|markdown|mdx|skills?|agents?\.md|claude\.md|"
    r"changelog|contributing|instructions?|rules?)\b",
    re.IGNORECASE,
)
# Scale of the demotion: larger than any single reciprocal-rank-fusion vote
# (1/61 ~ 0.016), so a test or doc only wins when nothing else matched.
_TEST_DOC_PENALTY = 0.05


def is_test_path(file_path: str) -> bool:
    return bool(TEST_PATH_PATTERN.search(str(file_path or "")))


def is_doc_path(file_path: str) -> bool:
    return bool(DOC_PATH_PATTERN.search(str(file_path or "")))


def question_wants_tests(query: str) -> bool:
    return bool(TEST_QUESTION_PATTERN.search(query or ""))


def question_wants_docs(query: str) -> bool:
    return bool(DOC_QUESTION_PATTERN.search(query or "")) or is_explicit_overview_request(query or "")


def _path_noise_penalties(query: str) -> list[tuple[re.Pattern, float]]:
    """Demote evidence that matches many questions but rarely answers them.

    Translation catalogues repeat every product word, historical SQL
    migrations repeat every table name, and tests/docs restate the domain
    vocabulary, so on large repositories they crowd out the implementation.
    They stay eligible (and unpenalised) when the question is about them.
    Only the first matching pattern applies to a path.
    """
    penalties: list[tuple[re.Pattern, float]] = []
    if not _I18N_QUESTION.search(query):
        penalties.append((_LOCALE_PATH, 0.03))
    if not _MIGRATION_QUESTION.search(query):
        # The live schema file should win over historical migrations.
        penalties.append((_MIGRATION_PATH, 0.8))
    if not question_wants_tests(query):
        penalties.append((TEST_PATH_PATTERN, _TEST_DOC_PENALTY))
    if not question_wants_docs(query):
        penalties.append((DOC_PATH_PATTERN, _TEST_DOC_PENALTY))
    return penalties


# Identifier-shaped tokens: camelCase, PascalCase with an inner capital,
# snake_case, SCREAMING_CASE and anything in backticks.
_IDENTIFIER_TOKEN = re.compile(
    r"\b(?:[a-z_$][A-Za-z0-9_$]*[A-Z][A-Za-z0-9_$]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9_]*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b"
)
_BACKTICK_TOKEN = re.compile(r"`([A-Za-z_$][\w$.]{1,80})`")
# A single capitalised word is a symbol only when the question calls it one:
# "the Booking model", "the Stripe class", "where is Booking defined".
_TYPED_SYMBOL = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,})\s+(?:model|class|interface|type|enum|struct|trait|function|component|service|"
    r"hook|schema|table|module|controller|handler|router|repository|decorator|guard|provider|object|constant)\b|"
    r"\b(?:model|class|interface|type|enum|struct|function|component|service|hook|table|controller)\s+"
    r"([A-Z][A-Za-z0-9_]{2,})\b"
)
DEFINITION_QUESTION_PATTERN = re.compile(
    r"\b(?:defined|define[sd]?|definition|declared|declaration|declares|implemented|implementation|where\s+is|"
    r"which\s+file|show\s+(?:me\s+)?the|find\s+the|what\s+(?:does|is))\b",
    re.IGNORECASE,
)
_SYMBOL_STOPWORDS = {"API", "URL", "HTTP", "JSON", "SQL", "UI", "ID", "OK", "README"}


def extract_question_identifiers(query: str) -> list[str]:
    """Return code identifiers named in a question (exact case), at most six."""
    text = str(query or "")
    found: list[str] = []
    for match in _BACKTICK_TOKEN.finditer(text):
        found.append(match.group(1).split(".")[-1])
    for match in _IDENTIFIER_TOKEN.finditer(text):
        found.append(match.group(0))
    for match in _TYPED_SYMBOL.finditer(text):
        found.append(match.group(1) or match.group(2))
    if len(text.split()) == 1 and re.fullmatch(r"[A-Za-z_$][\w$]{2,}", text.strip()):
        found.append(text.strip())
    result: list[str] = []
    for name in found:
        name = name.strip("$")
        if len(name) < 3 or name.upper() in _SYMBOL_STOPWORDS or name in result:
            continue
        # A path or extension fragment (``schema.prisma``) is not an identifier.
        if re.search(r"\.(?:\w{1,6})$", name):
            continue
        result.append(name)
    return result[:6]


# Declaration keywords that satisfy a kind the question names ("the Booking
# model" should find ``model Booking {``, not a local ``type Booking = ...``).
_KIND_DECLARATIONS = {
    "model": r"model|table|class|entity|schema|interface",
    "table": r"model|table",
    "schema": r"model|table|schema|const",
    "class": r"class",
    "interface": r"interface|type",
    "type": r"type|interface|enum|class",
    "enum": r"enum",
    "struct": r"struct",
    "trait": r"trait",
    "function": r"function|def|fn|func|const|let|var",
    "hook": r"function|const|let|var",
    "component": r"function|const|class",
    "service": r"class",
    "controller": r"class",
    "repository": r"class",
    "module": r"module|class",
}


def extract_question_symbol_kinds(query: str) -> dict[str, str]:
    """Map identifiers to the kind the question names ("Booking" -> "model")."""
    kinds: dict[str, str] = {}
    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s+(model|table|schema|class|interface|type|enum|struct|trait|function|hook|"
        r"component|service|controller|repository|module)\b|"
        r"\b(model|table|class|interface|type|enum|struct|function|component|service|controller)\s+([A-Za-z_][A-Za-z0-9_]{2,})\b",
        str(query or ""),
    ):
        name, kind = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
        kinds.setdefault(name, kind.lower())
    return kinds


def _symbol_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip().startswith("["):
        import json
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _definition_regex(name: str) -> re.Pattern:
    """Declarations of ``name``: keyword forms, modified methods, one-line
    method signatures, and function-valued assignments/properties."""
    escaped = re.escape(name)
    modifiers = r"(?:public|private|protected|static|async|readonly|override|abstract|get|set|internal|open|suspend)"
    return re.compile(
        rf"\b(?:class|interface|type|enum|struct|trait|function\*?|def|fn|func|const|let|var|model|object|module|"
        rf"namespace|record|table)\s+\"?{escaped}\b"
        rf"|^\s*(?:export\s+)?(?:default\s+)?(?:{modifiers}\s+)+{escaped}\s*[<(]"
        rf"|^\s*{escaped}\s*(?:<[^>\n]*>)?\s*\([^)\n]*\)\s*(?::[^{{=\n]+)?\{{"
        rf"|^\s*(?:export\s+)?{escaped}\s*[:=]\s*(?:async\b|function\b|\([^)\n]*\)\s*(?::[^=\n]+)?=>|[A-Za-z_$][\w$.]*\()",
        re.MULTILINE,
    )


async def symbol_definition_chunks(
    store,
    repo_id: str,
    identifiers: list[str],
    *,
    limit_per_symbol: int = 2,
    staging_prefix: str | None = None,
    kinds: dict[str, str] | None = None,
) -> list[Dict]:
    """Chunks that define an identifier named in the question.

    Two bounded lookups: the stored ``chunks.symbols`` list (exact name), and
    an exact (non-prefix) FTS match filtered by a declaration regex, which
    also finds class methods the chunker does not list as symbols.
    """
    names = [name for name in identifiers if _FTS_TERM_CLEAN.sub("", name)][:4]
    if not names:
        return []
    staging_filter, staging_args = ("", []) if not staging_prefix else (" AND id NOT LIKE ?", [f"{staging_prefix}%"])
    columns = "id, file_path, start_line, end_line, language, symbols, content"
    found: dict[str, Dict] = {}
    for name in names:
        candidates: list[Dict] = []
        try:
            candidates.extend(await store.fetch_all(
                f"SELECT {columns} FROM chunks WHERE repo_id = ? AND symbols LIKE ? ESCAPE '\\'{staging_filter} LIMIT 60",
                [repo_id, f'%"{escape_like(name)}"%', *staging_args],
            ))
        except Exception as error:
            logger.debug("Symbol lookup unavailable (%s)", type(error).__name__)
        try:
            fts_staging = staging_filter.replace(" id ", " c.id ")
            candidates.extend(await store.fetch_all(
                "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content FROM chunks_fts "
                f"JOIN chunks c ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? AND c.repo_id = ?{fts_staging} "
                "ORDER BY bm25(chunks_fts, 1.0, 4.0) ASC LIMIT 300",
                [f'"{_FTS_TERM_CLEAN.sub("", name).lower()}"', repo_id, *staging_args],
            ))
        except Exception as error:
            logger.debug("Exact identifier FTS unavailable (%s)", type(error).__name__)
        declaration = _definition_regex(name)
        kind = (kinds or {}).get(name)
        kind_declaration = re.compile(
            rf"\b(?:{_KIND_DECLARATIONS[kind]})\s+\"?{re.escape(name)}\b", re.IGNORECASE,
        ) if kind in _KIND_DECLARATIONS else None
        matches: list[tuple[tuple, Dict]] = []
        seen: set[str] = set()
        for row in exclude_staged(candidates, staging_prefix):
            chunk_id = str(row.get("id") or "")
            path = str(row.get("file_path") or "")
            if not chunk_id or chunk_id in seen or is_overview_file(path):
                continue
            seen.add(chunk_id)
            content = str(row.get("content") or "")
            in_symbols = name in _symbol_list(row.get("symbols"))
            declares = bool(declaration.search(content))
            if not (in_symbols or declares):
                continue
            # Prefer, in order: implementation code over tests/docs/migrations,
            # the kind the question names, a declaration the chunker also
            # recorded as a symbol, an exported declaration, and a file
            # named after the symbol.
            stem = path.rsplit("/", 1)[-1].split(".", 1)[0].lower()
            noisy = is_test_path(path) or is_doc_path(path) or bool(_MIGRATION_PATH.search(path.lower()))
            quality = (
                1 if noisy else 0,
                0 if kind_declaration is None or kind_declaration.search(content) else 1,
                0 if declares and in_symbols else 1 if declares else 2,
                0 if re.search(rf"\bexport\b[^\n]*\b{re.escape(name)}\b", content) else 1,
                0 if stem == name.lower() else 1,
            )
            matches.append((quality, row))
        matches.sort(key=lambda item: (item[0], str(item[1].get("file_path")), int(item[1].get("start_line") or 0)))
        for _, row in matches[: max(1, limit_per_symbol)]:
            enriched = dict(row)
            enriched["_defined_symbol"] = name
            found.setdefault(str(row["id"]), enriched)
    return list(found.values())


async def symbol_call_sites(
    store,
    repo_id: str,
    names: list[str],
    *,
    limit: int = 10,
    staging_prefix: str | None = None,
) -> list[Dict]:
    """Chunks that call (not declare or import) the named identifiers.

    One chunk per file: the best-ranked chunk whose content contains a call
    such as ``name(`` or ``obj.name(`` on a line that is not the declaration.
    """
    clean = [name for name in dict.fromkeys(_FTS_TERM_CLEAN.sub("", str(n)) for n in names) if len(name) >= 3][:3]
    if not clean:
        return []
    expression = " OR ".join(f'"{name.lower()}"' for name in clean)
    rows = await store.fetch_all(
        "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content FROM chunks_fts "
        "JOIN chunks c ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? AND c.repo_id = ? "
        "ORDER BY bm25(chunks_fts, 1.0, 4.0) ASC LIMIT 300",
        [expression, repo_id],
    )
    patterns = [
        (re.compile(rf"(?<![\w$]){re.escape(name)}\s*(?:<[^>\n]*>)?\s*\("), _definition_regex(name))
        for name in clean
    ]
    best: dict[str, Dict] = {}
    for row in exclude_staged(rows, staging_prefix):
        path = str(row.get("file_path") or "")
        if not path or path in best or is_overview_file(path):
            continue
        for line in str(row.get("content") or "").split("\n"):
            stripped = line.strip()
            if stripped.startswith(("import ", "export {", "export *", "from ", "//", "*", "#")):
                continue
            if any(call.search(line) and not declaration.search(line) for call, declaration in patterns):
                best[path] = row
                break
        if len(best) >= limit * 2:
            break
    ordered = sorted(best.values(), key=lambda row: is_test_path(str(row.get("file_path"))) or is_doc_path(str(row.get("file_path"))))
    return ordered[:limit]


async def absent_question_terms(store, repo_id: str, terms: list[str]) -> list[str]:
    """Question terms with no match anywhere in this repository's index.

    One bounded statement: each term is an ``EXISTS`` probe on the FTS index.
    Telling the model which named things are absent prevents false-premise
    answers ("publishes to RabbitMQ") built from loosely related evidence.
    """
    probes = [term for term in dict.fromkeys(_FTS_TERM_CLEAN.sub("", str(t)).lower() for t in terms) if len(term) >= 4][:8]
    if not probes:
        return []
    parts, args = [], []
    for term in probes:
        parts.append(
            "SELECT ? AS term WHERE NOT EXISTS (SELECT 1 FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND c.repo_id = ? LIMIT 1)"
        )
        args.extend([term, f'"{term}"*', repo_id])
    try:
        rows = await store.fetch_all(" UNION ALL ".join(parts), args)
    except Exception as error:
        logger.debug("Absent-term probe unavailable (%s)", type(error).__name__)
        return []
    absent = {str(row.get("term") or "") for row in rows or [] if isinstance(row, dict)}
    return [term for term in probes if term in absent]


async def retrieve_context(
    store,
    repo_id: str,
    query: str,
    top_k: int = 8,
    workflow: str = "general",
    *,
    staging_prefix: str | None = None,
    retrieval_query: str | None = None,
    boost_paths: list[str] | None = None,
) -> List[Dict]:
    """Plan, retrieve, fuse, and bound the evidence for one question.

    ``query`` is the user's question and drives classification (explicit file,
    component, overview, impact). ``retrieval_query`` may add technical terms
    from the previous turn so short follow-ups ("where is it called?") still
    find the right code. ``boost_paths`` are files cited in the previous answer.
    ``staging_prefix`` hides the rows of an in-progress refresh.
    """
    chunks = await _retrieve_context(
        store, repo_id, query, top_k, workflow,
        staging_prefix=staging_prefix, retrieval_query=retrieval_query, boost_paths=boost_paths,
    )
    return exclude_staged(chunks, staging_prefix)


# Fusion constants. Reciprocal-rank fusion votes are 1/(RRF_K + rank), about
# 0.016 for a first place; every boost below is expressed on that scale.
RRF_K = 60
PATH_HINT_BOOST = 1.0 / (RRF_K + 1)
PATH_SAMPLE_WEIGHT = 0.25
# File-level semantic rank and keyword/cosine agreement count as half votes.
FILE_SEMANTIC_WEIGHT = 0.5
SEMANTIC_AGREEMENT_WEIGHT = 0.5
PREVIOUS_CITATION_BOOST = 0.008
# An exact definition of an identifier the question names outranks any
# combination of fuzzy votes for a definition/location question.
SYMBOL_DEFINITION_BONUS = 0.08
SYMBOL_MENTION_BONUS = 0.035
# Strict (path-targeted) questions still keep the best semantic chunks of the
# few files that file-level semantic search ranks highest.
STRICT_KEEP_SEMANTIC_FILES = 3
USAGE_WEIGHT = 1.5
USAGE_QUESTION_PATTERN = re.compile(
    r"\b(?:called|calls?|callers?|used|uses|usages?|invoked|invokes|referenced|references|imported|imports|consumed|consumers?)\b",
    re.IGNORECASE,
)
# Deadline for the interactive query embedding before keyword-only retrieval.
QUERY_EMBEDDING_TIMEOUT_SECONDS = 4.0


DIRECTORY_TOKEN_PATTERN = re.compile(r"(?<![\w./*?@-])((?:[\w@.-]+/)+[\w@.-]+/?|[\w@.-]+/)(?=$|[\s,;:!?)`'\"]|\.(?:\s|$))")
MAX_DIRECTORY_LISTING_FILES = 6000


def extract_directory_candidates(query: str) -> list[str]:
    """Folder-like tokens ("packages/app-store", "src/") in a question."""
    found: list[str] = []
    for match in DIRECTORY_TOKEN_PATTERN.finditer(str(query or "")):
        token = match.group(1).strip("/").removeprefix("./")
        if not token or "//" in token or token.startswith("http") or FILE_PATH_PATTERN.fullmatch(token):
            continue
        last = token.rsplit("/", 1)[-1]
        if "." in last and re.search(r"\.[A-Za-z0-9]{1,8}$", last):
            continue  # a file, not a folder
        if token not in found:
            found.append(token)
    return found[:3]


async def directory_listing_chunk(store, repo_id: str, directory: str) -> Dict | None:
    """A generated evidence block describing an indexed folder.

    Lists child folders with file counts and the files directly inside, from
    the repository's file index, so "what lives in X/" is answered from the
    folder's real contents rather than from whichever chunks matched.
    """
    prefix = directory.strip("/")
    try:
        rows = await store.fetch_all(
            "SELECT file_path FROM repo_files WHERE repo_id = ? AND file_path LIKE ? ESCAPE '\\' ORDER BY file_path LIMIT ?",
            [repo_id, f"{escape_like(prefix)}/%", MAX_DIRECTORY_LISTING_FILES],
        )
        if not rows:
            rows = await store.fetch_all(
                "SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND file_path LIKE ? ESCAPE '\\' ORDER BY file_path LIMIT ?",
                [repo_id, f"{escape_like(prefix)}/%", MAX_DIRECTORY_LISTING_FILES],
            )
    except Exception as error:
        logger.debug("Directory listing unavailable (%s)", type(error).__name__)
        return None
    paths = [str(row.get("file_path") or "") for row in rows or [] if isinstance(row, dict) and row.get("file_path")]
    paths = [path for path in paths if path.startswith(prefix + "/")]
    if not paths:
        return None
    children: dict[str, int] = defaultdict(int)
    direct_files: list[str] = []
    for path in paths:
        rest = path[len(prefix) + 1:]
        if "/" in rest:
            children[rest.split("/", 1)[0]] += 1
        else:
            direct_files.append(rest)
    truncated = len(paths) >= MAX_DIRECTORY_LISTING_FILES
    lines = [
        f"Directory listing for {prefix}/ from the repository file index: "
        f"{len(paths):,}{'+' if truncated else ''} indexed files, {len(children)} subfolders, {len(direct_files)} files directly inside.",
    ]
    if children:
        ordered = sorted(children.items(), key=lambda item: (-item[1], item[0]))
        lines.append("Subfolders (file count): " + ", ".join(f"{name}/ ({count})" for name, count in ordered[:200]))
        if len(ordered) > 200:
            lines.append(f"... and {len(ordered) - 200} more subfolders.")
    if direct_files:
        lines.append("Files directly in this folder: " + ", ".join(f"{prefix}/{name}" for name in direct_files[:60]))
    content = "\n".join(lines)
    return {
        "id": f"directory-listing:{prefix}",
        "file_path": prefix + "/",
        "start_line": 1,
        "end_line": len(lines),
        "language": "text",
        "symbols": [],
        "content": content,
        "_synthetic": "directory_listing",
        "_retrieval_methods": ["directory_listing"],
        "_retrieval_reasons": ["Generated listing of a folder named in the question"],
    }


def _path_matches_request(file_path: str, requested: str) -> bool:
    path, wanted = file_path.lower(), requested.lower()
    return path == wanted or path.endswith("/" + wanted)


def _plan_scope_without_paths(query: str) -> str:
    return _query_scope(query, [], extract_issue_reference(query))


async def _retrieve_context(
    store,
    repo_id: str,
    query: str,
    top_k: int,
    workflow: str,
    *,
    staging_prefix: str | None,
    retrieval_query: str | None,
    boost_paths: list[str] | None,
) -> List[Dict]:
    requested_paths = extract_requested_file_paths(query)
    include_overview = is_exploratory_repository_question(query, requested_paths)
    evidence_plan = build_evidence_plan(query, workflow)
    search_text = query
    if retrieval_query and retrieval_query.strip() and retrieval_query.strip() != query.strip():
        follow_up_terms = [term for term in search_terms(retrieval_query) if term not in evidence_plan["search_terms"]]
        if follow_up_terms:
            evidence_plan["search_terms"] = list(dict.fromkeys([*evidence_plan["search_terms"], *follow_up_terms]))[:20]
            evidence_plan["follow_up_terms"] = follow_up_terms[:8]
        search_text = retrieval_query
    include_overview_files = include_overview or question_requests_overview_files(query, requested_paths)
    evidence_plan["overview_files_allowed"] = include_overview_files
    editing_mode = workflow == "editing"
    issue_request = bool(evidence_plan.get("issue_reference"))
    previously_cited = {str(path) for path in (boost_paths or []) if path}

    # A named file is an exact request. Running broad semantic/keyword searches
    # here only adds latency because the final evidence contract already
    # discards those results. Returning an empty set when a real path is
    # absent is also safer than filling the answer with similarly-worded files.
    if requested_paths:
        # Editing needs the complete bounded set of chunks for the named file
        # so the model can form exact search/replace hunks. Normal questions
        # retain their small citation budget and latency.
        per_file_limit = settings.editing_max_chunks_per_file if editing_mode else top_k
        rows = exclude_staged(await requested_files_chunks(store, repo_id, requested_paths, per_file_limit, staging_prefix), staging_prefix)
        resolved = [
            path for path in requested_paths
            if any(_path_matches_request(str(row.get("file_path") or ""), path) for row in rows)
        ]
        unresolved = [path for path in requested_paths if path not in resolved]
        # A slash-less dotted word that is not an indexed file ("Next.js",
        # "Chart.js") is vocabulary, not a file request: continue with normal
        # hybrid retrieval for it. A path with folders stays an exact request.
        if not rows and all("/" not in path for path in unresolved):
            requested_paths = []
            evidence_plan["requested_files"] = []
            evidence_plan["ignored_file_like_terms"] = unresolved
            evidence_plan["query_scope"] = _plan_scope_without_paths(query)
        elif not rows:
            evidence_plan["missing_files"] = unresolved
            return []
        else:
            if unresolved:
                evidence_plan["missing_files"] = unresolved
            requested_paths = resolved or requested_paths
    if requested_paths:
        actual_paths = list(dict.fromkeys(str(chunk.get("file_path") or "") for chunk in rows if chunk.get("file_path")))
        if editing_mode and actual_paths:
            rows = exclude_staged(
                await complete_files_chunks(store, repo_id, actual_paths, settings.editing_max_chunks_per_file, staging_prefix=staging_prefix),
                staging_prefix,
            )
        requested_chunks = []
        for chunk in sorted(rows, key=lambda item: (item["file_path"], item["start_line"])):
            enriched = dict(chunk)
            enriched["_retrieval_methods"] = ["requested_file"]
            enriched["_retrieval_reasons"] = ["Explicit file path requested in the question"]
            enriched["_evidence_plan"] = evidence_plan
            enriched["_relevance_score"] = 2.0
            requested_chunks.append(enriched)
        if not editing_mode:
            return requested_chunks[: per_file_limit * len(requested_paths)]

        # An issue body often names the primary file but describes a change
        # that depends on callers, imports, or tests. Add a small supporting
        # candidate pass for issue requests; normal explicit-file edits stay
        # exact-file-only and therefore remain fast and citation-precise.
        candidates = list(requested_chunks)
        if issue_request:
            # The named file is already authoritative. For issue support use
            # lexical matches from the pasted issue text rather than generic
            # path-family samples.
            support = await sparse_search(
                store, repo_id, query, limit=settings.editing_retrieval_top_k, plan=evidence_plan,
                staging_prefix=staging_prefix,
            )
            for chunk in support:
                path = str(chunk.get("file_path") or "")
                if not path or is_overview_file(path):
                    continue
                enriched = dict(chunk)
                enriched["_retrieval_methods"] = ["issue_support"]
                enriched["_retrieval_reasons"] = ["Supporting source matched the referenced issue"]
                enriched["_evidence_plan"] = evidence_plan
                enriched["_relevance_score"] = 1.0
                candidates.append(enriched)
        return await expand_editing_evidence(
            store, repo_id, candidates, actual_paths or requested_paths, issue_request=issue_request, staging_prefix=staging_prefix,
        )

    directory_candidates = [] if editing_mode else extract_directory_candidates(query)
    directory_task = asyncio.create_task(
        directory_listing_chunk(store, repo_id, directory_candidates[0])
    ) if directory_candidates else None
    identifiers = extract_question_identifiers(query)
    definition_question = bool(identifiers) and bool(DEFINITION_QUESTION_PATTERN.search(query))
    lowered_query = query.lower()
    question_terms = [term for term in search_terms(query) if term in lowered_query]

    # Issue text often mentions one subsystem while the actual fix also needs
    # tests, configuration, callers, or a dependency. Keep the bounded hybrid
    # retrieval set broad for an explicit issue reference.
    sparse_limit = 60 if staging_prefix else 40
    sparse_task = asyncio.create_task(
        sparse_search(
            store, repo_id, search_text, limit=sparse_limit, plan=evidence_plan,
            include_overview_files=include_overview_files, staging_prefix=staging_prefix,
        )
    )
    plan_hints = evidence_plan.get("path_hints", [])[:8]
    plan_task = asyncio.create_task(
        path_hint_chunks_batch(store, repo_id, plan_hints, include_overview_files=include_overview_files)
    ) if plan_hints else None
    readme_task = (
        asyncio.create_task(store.fetch_all(
            "SELECT id, file_path, start_line, end_line, language, symbols, content FROM chunks "
            "WHERE repo_id = ? AND lower(file_path) LIKE '%readme.md' ORDER BY start_line LIMIT 1", [repo_id]
        )) if include_overview else None
    )
    overview_task = (
        asyncio.create_task(store.fetch_all(
            "SELECT id, file_path, start_line, end_line, language, symbols, content FROM chunks "
            "WHERE repo_id = ? ORDER BY file_path, start_line LIMIT ?", [repo_id, settings.overview_retrieval_candidates]
        )) if include_overview else None
    )
    # "Where is X called/used?" also searches for the call sites of the
    # identifiers in the question (or, for a follow-up, in the earlier topic).
    usage_names = (extract_question_identifiers(query) or extract_question_identifiers(search_text))[:2] \
        if USAGE_QUESTION_PATTERN.search(query) and not editing_mode else []
    usage_task = asyncio.create_task(
        symbol_call_sites(store, repo_id, usage_names, limit=8, staging_prefix=staging_prefix)
    ) if usage_names else None
    symbol_task = asyncio.create_task(
        symbol_definition_chunks(
            store, repo_id, identifiers, staging_prefix=staging_prefix, kinds=extract_question_symbol_kinds(query),
        )
    ) if identifiers else None
    # The query embedding is computed while keyword retrieval runs. A follow-up
    # embeds the expanded text so "where is that called?" keeps its topic. A
    # provider outage or a saturated limiter degrades to keyword + path
    # evidence within a short deadline instead of stalling the question.
    embedding_task = asyncio.create_task(asyncio.to_thread(embed_query, search_text))

    pending = [sparse_task]
    for task in (plan_task, readme_task, overview_task, symbol_task, usage_task):
        if task:
            pending.append(task)
    results = await asyncio.gather(*pending, return_exceptions=True)

    successful = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning("A retrieval strategy failed for %s (%s)", repo_id, type(result).__name__)
            successful.append([])
        else:
            successful.append(result)
    cursor = 0
    sparse_chunks = successful[cursor]
    cursor += 1

    def take(task):
        nonlocal cursor
        if not task:
            return []
        value = successful[cursor]
        cursor += 1
        return value

    plan_results = take(plan_task)
    readme_chunks = take(readme_task)
    overview_chunks = take(overview_task)
    symbol_chunks = exclude_staged(take(symbol_task), staging_prefix)
    usage_chunks = exclude_staged(take(usage_task), staging_prefix)
    plan_results = exclude_staged(plan_results, staging_prefix)
    # Question terms that no keyword result contains are probed against the
    # whole index (while the query embedding is still in flight). A term
    # with no match anywhere is reported so the answer cannot treat a false
    # premise ("publishes to RabbitMQ") as fact.
    if question_terms and not editing_mode:
        haystack = " ".join(
            f"{chunk.get('file_path') or ''} {chunk.get('content') or ''}".lower() for chunk in sparse_chunks
        )
        unmatched = [term for term in question_terms if term not in haystack]
        if unmatched:
            try:
                absent_terms = await absent_question_terms(store, repo_id, unmatched)
            except Exception as error:
                logger.debug("Absent-term probe failed (%s)", type(error).__name__)
                absent_terms = []
            if absent_terms:
                evidence_plan["absent_terms"] = absent_terms

    query_embedding = None
    timeout = max(0.5, float(getattr(settings, "query_embedding_timeout_seconds", QUERY_EMBEDDING_TIMEOUT_SECONDS)))
    try:
        done, _ = await asyncio.wait({embedding_task}, timeout=timeout)
        if not done:
            # The worker thread cannot be cancelled; let it finish in the
            # background (its result warms the cache) and answer from keyword
            # evidence now.
            embedding_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            evidence_plan["semantic_search"] = "timed_out"
            logger.warning("Query embedding exceeded %.1fs for %s; using keyword retrieval", timeout, repo_id)
        else:
            query_embedding = embedding_task.result()
    except (EmbeddingUnavailableError, ModelConfigurationError):
        evidence_plan["semantic_search"] = "unavailable"
        logger.warning("NVIDIA query embedding unavailable; falling back to keyword retrieval")
    except Exception as error:
        evidence_plan["semantic_search"] = "unavailable"
        logger.warning("Query embedding failed for %s (%s); using keyword retrieval", repo_id, type(error).__name__)

    semantic_file_ranks: dict[str, int] = {}
    dense_chunks: list[Dict] = []
    file_semantic_chunks: list[Dict] = []
    if query_embedding is not None:
        candidate_ids = [str(chunk.get("id")) for chunk in [*sparse_chunks[:60], *plan_results[:16]] if chunk.get("id")]
        dense_jobs = [
            semantic_file_search(store, repo_id, query_embedding, settings.file_vector_top_files),
            semantic_rerank_ids(store, repo_id, query_embedding, candidate_ids, 24),
        ]
        dense_results = await asyncio.gather(*dense_jobs, return_exceptions=True)
        file_hits = dense_results[0] if not isinstance(dense_results[0], Exception) else []
        dense_chunks = dense_results[1] if not isinstance(dense_results[1], Exception) else []
        rank = 0
        for hit in file_hits:
            path = str(hit.get("file_path") or "")
            if not path or path in semantic_file_ranks:
                continue
            if not include_overview_files and is_overview_file(path):
                continue
            semantic_file_ranks[path] = rank
            rank += 1
        semantic_paths = [*semantic_file_ranks, *[path for path in previously_cited if path not in semantic_file_ranks]]
        if semantic_paths:
            try:
                file_semantic_chunks = await semantic_chunks_in_files(
                    store, repo_id, query_embedding, semantic_paths, 24, staging_prefix=staging_prefix,
                )
            except Exception as error:
                logger.warning("Chunk-level semantic rerank failed for %s (%s)", repo_id, type(error).__name__)
        if not file_hits and not candidate_ids:
            # Legacy indexes without file vectors and no lexical candidate:
            # fall back to the bounded term-candidate semantic search.
            try:
                dense_chunks = await dense_search(
                    store, repo_id, query_embedding, include_overview_files=include_overview_files,
                    candidate_terms=[*evidence_plan.get("search_terms", []), *evidence_plan.get("path_hints", [])],
                )
            except Exception as error:
                logger.warning("Fallback semantic search failed for %s (%s)", repo_id, type(error).__name__)
        dense_chunks = exclude_staged(dense_chunks, staging_prefix)

    # Keep this guard in addition to the SQL predicates. It protects callers
    # that provide a compatibility/mock store and makes the citation contract
    # explicit at the final evidence boundary.
    if not include_overview_files:
        dense_chunks = [chunk for chunk in dense_chunks if not is_overview_file(str(chunk.get("file_path", "")))]
        file_semantic_chunks = [chunk for chunk in file_semantic_chunks if not is_overview_file(str(chunk.get("file_path", "")))]
        sparse_chunks = [chunk for chunk in sparse_chunks if not is_overview_file(str(chunk.get("file_path", "")))]
        plan_results = [chunk for chunk in plan_results if not is_overview_file(str(chunk.get("file_path", "")))]

    # Path-hint SQL uses a LIKE pattern for portability. Confirm every hint on
    # path-token boundaries before it can become a citation, so ``api`` never
    # matches ``capillary.py``.
    plan_results = [
        chunk for chunk in plan_results
        if not chunk.get("matched_hint")
        or path_matches_hints(str(chunk.get("file_path", "")), [str(chunk["matched_hint"])])
    ]

    def annotate(chunk: Dict, method: str, reason: str) -> Dict:
        enriched = dict(chunk)
        enriched["_retrieval_methods"] = list(dict.fromkeys([*(enriched.get("_retrieval_methods") or []), method]))
        enriched["_retrieval_reasons"] = list(dict.fromkeys([*(enriched.get("_retrieval_reasons") or []), reason]))
        enriched["_evidence_plan"] = evidence_plan
        for transient in ("distance", "score", "matched_hint", "hint_rank", "hint_order"):
            enriched.pop(transient, None)
        return enriched

    scores: dict[str, float] = {}
    chunk_map: dict[str, Dict] = {}

    def vote(chunk: Dict, method: str, reason: str, value: float) -> None:
        chunk_id = chunk["id"]
        chunk_map[chunk_id] = annotate(chunk_map.get(chunk_id, chunk), method, reason)
        scores[chunk_id] = scores.get(chunk_id, 0.0) + value

    noise_penalties = _path_noise_penalties(query)

    def demoted(chunk_or_path) -> bool:
        path = chunk_or_path if isinstance(chunk_or_path, str) else str(chunk_or_path.get("file_path") or "")
        lowered = path.lower()
        return any(pattern.search(lowered) for pattern, _ in noise_penalties)

    def by_demotion(items: list) -> list:
        # Demoted evidence (tests, docs, locales, migrations) must not use up
        # the best ranks of a strategy: rank everything else first.
        return sorted(items, key=demoted)

    # Reciprocal-rank fusion, every term on the 1/(K + rank) scale.
    #
    # 1. Keyword: BM25 candidates re-ordered by their exact cosine distance
    #    (when an embedding exists). The cosine rerank only re-orders the
    #    keyword candidates and adds half a vote for agreement; it is not an
    #    independent full vote, which used to give every keyword hit twice the
    #    weight of a chunk found only by semantic search.
    sparse_chunks = by_demotion(sparse_chunks)
    dense_ranked = by_demotion(dense_chunks)
    rerank_position = {str(chunk.get("id")): index for index, chunk in enumerate(dense_ranked)}
    keyword_order = sorted(
        enumerate(sparse_chunks),
        key=lambda item: -(
            1.0 / (RRF_K + item[0] + 1)
            + (1.0 / (RRF_K + rerank_position[str(item[1].get("id"))] + 1) if str(item[1].get("id")) in rerank_position else 0.0)
        ),
    )
    for rank, (_, chunk) in enumerate(keyword_order):
        value = 1.0 / (RRF_K + rank + 1)
        position = rerank_position.get(str(chunk.get("id")))
        if position is not None:
            value += SEMANTIC_AGREEMENT_WEIGHT / (RRF_K + position + 1)
        vote(chunk, "keyword", "Keyword match in file path or source content", value)
    # Semantic matches among path-hint samples / legacy dense search that the
    # keyword pass did not return.
    sparse_ids = {str(chunk.get("id")) for chunk in sparse_chunks}
    for rank, chunk in enumerate(chunk for chunk in dense_ranked if str(chunk.get("id")) not in sparse_ids):
        vote(chunk, "semantic", "Semantic similarity to the question", 1.0 / (RRF_K + rank + 1))
    # 2. Chunk-level semantic search inside the semantically closest files.
    for rank, chunk in enumerate(by_demotion(file_semantic_chunks)):
        vote(chunk, "semantic", "Semantically closest code in the most relevant files", 1.0 / (RRF_K + rank + 1))
    # 3. Exact definitions of identifiers named in the question.
    for chunk in symbol_chunks:
        bonus = SYMBOL_DEFINITION_BONUS if definition_question else SYMBOL_MENTION_BONUS
        vote(chunk, "symbol_definition", f"Defines `{chunk.get('_defined_symbol')}`, named in the question", bonus)
        chunk_map[chunk["id"]].pop("_defined_symbol", None)
    # 3b. Call sites / references of the identifiers a usage question names.
    for rank, chunk in enumerate(by_demotion(usage_chunks)):
        vote(chunk, "symbol_usage", "References an identifier the question asks about", USAGE_WEIGHT / (RRF_K + rank + 1))
    # 4. Path-hint samples are alphabetical, so they only get a weak vote.
    for rank, chunk in enumerate(plan_results):
        matched_hint = str(chunk.get("matched_hint") or "path")
        vote(chunk, "workflow_target", f"Evidence-plan target matching the '{matched_hint}' path hint", PATH_SAMPLE_WEIGHT / (RRF_K + rank + 1))
    file_ranks = {path: rank for rank, path in enumerate(by_demotion(list(semantic_file_ranks)))}

    path_hints = evidence_plan.get("path_hints") or []
    for chunk_id, chunk in chunk_map.items():
        path = str(chunk.get("file_path") or "")
        lowered_path = path.lower()
        # 5. File-level semantic rank is a real fusion term for every chunk
        #    of a semantically relevant file.
        file_rank = file_ranks.get(path)
        if file_rank is not None:
            scores[chunk_id] = scores.get(chunk_id, 0.0) + FILE_SEMANTIC_WEIGHT / (RRF_K + file_rank + 1)
        # 6. A path hint breaks ties once per chunk, at fusion scale; a folder
        #    named in the question counts the same way.
        if (path_hints and path_matches_hints(path, path_hints)) or any(
            path.startswith(directory + "/") for directory in directory_candidates
        ):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + PATH_HINT_BOOST
        for pattern, penalty in noise_penalties:
            if pattern.search(lowered_path):
                scores[chunk_id] = scores.get(chunk_id, 0.0) - penalty
                break
        if path in previously_cited:
            scores[chunk_id] = scores.get(chunk_id, 0.0) + PREVIOUS_CITATION_BOOST
            chunk_map[chunk_id] = annotate(chunk, "conversation", "Cited in the previous answer")
    ranked_chunks = [chunk_map[chunk_id] for chunk_id in sorted(scores, key=lambda key: (-scores[key], key))]

    # Impact questions get one extra, explicitly labelled pass over the
    # resolved dependency graph. If no file is named, use only a few directly
    # matched files as targets; unresolved package aliases are never invented.
    if is_impact_question(query):
        target_paths = list(requested_paths)
        if not target_paths:
            target_paths = list(dict.fromkeys(chunk["file_path"] for chunk in [*sparse_chunks[:3], *file_semantic_chunks[:2]]))
        dependency_chunks: list[Dict] = []
        try:
            dependency_chunks = exclude_staged(await dependent_file_chunks(store, repo_id, target_paths), staging_prefix)
        except Exception as error:
            logger.warning("Dependency impact retrieval unavailable for %s (%s)", repo_id, type(error).__name__)
        for chunk in dependency_chunks:
            vote(chunk, "dependency", "Depends on a directly matched file in the indexed repository graph", 0.02)
        ranked_chunks = [chunk_map[chunk_id] for chunk_id in sorted(scores, key=lambda key: (-scores[key], key))]

    if editing_mode:
        # The ordinary final selector intentionally limits each file to two
        # representative chunks. That is correct for chat citations but would
        # silently omit most of a file from an edit prompt. Expand the chosen
        # target files before the final evidence boundary instead.
        for chunk in ranked_chunks:
            chunk["_relevance_score"] = round(scores.get(chunk["id"], 0.0), 6)
        ranked_chunks = await expand_editing_evidence(
            store, repo_id, ranked_chunks, [], issue_request=issue_request, staging_prefix=staging_prefix,
        )

    # Final citation boundary: documentation is opt-in only for an explicit
    # README/docs request or a repository-wide overview question.
    if not include_overview_files:
        ranked_chunks = [
            chunk for chunk in ranked_chunks
            if not is_overview_file(str(chunk.get("file_path", "")))
        ]

    # A targeted component or implementation-location question keeps only
    # files whose paths identify the requested area, plus direct evidence that
    # is independently strong (an exact definition, or the best chunks of the
    # semantically closest files). When no path matches (the navbar lives in
    # Layout.tsx, say), fall back to the best direct keyword/semantic evidence.
    if is_strict_target_question(query, requested_paths, include_overview_files) and not issue_request:
        target_hints = extract_question_path_hints(query)
        strong_files = {path for path, rank in file_ranks.items() if rank < STRICT_KEEP_SEMANTIC_FILES and not demoted(path)}
        targeted_chunks = [
            chunk for chunk in ranked_chunks
            if (target_hints and path_matches_hints(str(chunk.get("file_path", "")), target_hints))
            or "symbol_definition" in (chunk.get("_retrieval_methods") or [])
        ]
        # A hint match does not rescue demoted evidence (a migration matching
        # "database", a doc matching "auth") when real code also matched.
        targeted_chunks = [chunk for chunk in targeted_chunks if not demoted(chunk)] or targeted_chunks
        if targeted_chunks:
            kept = {chunk["id"] for chunk in targeted_chunks}
            ranked_chunks = [
                chunk for chunk in ranked_chunks
                if chunk["id"] in kept
                or (str(chunk.get("file_path")) in strong_files and "semantic" in (chunk.get("_retrieval_methods") or []))
            ]
        else:
            evidence_plan["strict_target_relaxed"] = True
            ranked_chunks = [
                chunk for chunk in ranked_chunks
                if {"keyword", "semantic", "symbol_definition"} & set(chunk.get("_retrieval_methods") or [])
            ]
    # Broad overview evidence is a fallback, never an automatic citation.
    if include_overview and not ranked_chunks:
        fallback_by_id: dict[str, Dict] = {}
        for chunk in [*readme_chunks, *overview_chunks]:
            # Only orientation documents are valid fallback sources; an
            # arbitrary first implementation file is not evidence.
            if not is_overview_file(str(chunk.get("file_path", ""))):
                continue
            fallback_by_id.setdefault(chunk["id"], annotate(chunk, "overview", "Repository-wide overview fallback; no direct term match"))
        ranked_chunks = list(fallback_by_id.values())

    if editing_mode:
        # Keep the response bounded even when a repository contains a very
        # large target file. Chunks are ordered by target-file confidence and
        # line number, so this never mixes arbitrary late-repository files
        # into the edit context.
        final_chunks = []
        budget = max(1, int(settings.editing_context_characters))
        for chunk in ranked_chunks:
            content_size = len(str(chunk.get("content") or ""))
            if final_chunks and content_size + 256 > budget:
                break
            final_chunks.append(chunk)
            budget -= content_size + 256
            if budget <= 0:
                break
    else:
        final_chunks = select_diverse_chunks(ranked_chunks, top_k)
    for chunk in final_chunks:
        chunk["_relevance_score"] = round(scores.get(chunk["id"], 0.0), 6)
    if directory_task is not None:
        try:
            listing = await directory_task
        except Exception as error:
            logger.debug("Directory listing failed (%s)", type(error).__name__)
            listing = None
        if listing:
            listing["_evidence_plan"] = evidence_plan
            listing["_relevance_score"] = 1.0
            evidence_plan["directory_listing"] = listing["file_path"]
            final_chunks = [listing, *final_chunks[: max(0, top_k - 1)]]
    return final_chunks


# ---------------------------------------------------------------------------
# Code search, symbol usages, and the repository map
# ---------------------------------------------------------------------------

_CODE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


def _matching_lines(content: str, start_line: int, needles: list[str], limit: int = 3) -> list[dict]:
    matches: list[dict] = []
    lowered = [needle.lower() for needle in needles if needle]
    for offset, line in enumerate(str(content or "").splitlines()):
        lower_line = line.lower()
        if any(needle in lower_line for needle in lowered):
            matches.append({"line": int(start_line) + offset, "text": line[:400]})
            if len(matches) >= limit:
                break
    return matches


async def search_code(
    store,
    repo_id: str,
    query: str,
    *,
    limit: int = 30,
    staging_prefix: str | None = None,
) -> list[dict]:
    """Instant keyword search across the indexed source (no model call)."""
    raw = str(query or "").strip()[:200]
    tokens = list(dict.fromkeys(token for token in _CODE_TOKEN.findall(raw) if len(token) >= 2))[:8]
    if not tokens:
        return []
    staging_filter, staging_args = ("", []) if not staging_prefix else (" AND c.id NOT LIKE ?", [f"{staging_prefix}%"])

    fetch_limit = max(1, limit) * 4

    async def run(expression: str) -> list[dict]:
        # Cap each file to two chunks inside SQL so one heavily matching file
        # (a large test suite) cannot use up the whole result budget.
        try:
            return await store.fetch_all(
                "WITH matched AS (SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.content, "
                "bm25(chunks_fts, 1.0, 4.0) AS score FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                f"WHERE chunks_fts MATCH ? AND c.repo_id = ?{staging_filter}), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY file_path ORDER BY score ASC, start_line ASC) AS file_rank FROM matched) "
                "SELECT id, file_path, start_line, end_line, language, content, score FROM ranked "
                "WHERE file_rank <= 2 ORDER BY score ASC, file_path ASC, start_line ASC LIMIT ?",
                [expression, repo_id, *staging_args, fetch_limit],
            )
        except Exception as error:
            logger.debug("Windowed code search unavailable (%s)", type(error).__name__)
            return await store.fetch_all(
                "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.content, "
                "bm25(chunks_fts, 1.0, 4.0) AS score FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                f"WHERE chunks_fts MATCH ? AND c.repo_id = ?{staging_filter} ORDER BY score ASC LIMIT ?",
                [expression, repo_id, *staging_args, fetch_limit],
            )

    # All terms first (precise), then any term (recall).
    strict = " AND ".join(fts_match_expression([token]) for token in tokens)
    rows = await run(strict)
    if not rows and len(tokens) > 1:
        rows = await run(fts_match_expression(tokens))
    rows = exclude_staged(rows, staging_prefix)
    results: list[dict] = []
    seen_files: dict[str, int] = {}
    seen_lines: set[tuple[str, int]] = set()
    # Best chunk of every file first, then second chunks: distinct files
    # fill the result budget before any file repeats.
    first_seen: set[str] = set()
    firsts, seconds = [], []
    for row in rows:
        path = str(row.get("file_path") or "")
        (seconds if path in first_seen else firsts).append(row)
        first_seen.add(path)
    for row in [*firsts, *seconds]:
        path = str(row.get("file_path") or "")
        if seen_files.get(path, 0) >= 2:
            continue
        # Overlapping chunks repeat lines: report each file:line once.
        matches = [
            match for match in _matching_lines(row.get("content") or "", int(row.get("start_line") or 1), tokens, limit=6)
            if (path, match["line"]) not in seen_lines
        ][:3]
        if not matches:
            continue
        seen_lines.update((path, match["line"]) for match in matches)
        seen_files[path] = seen_files.get(path, 0) + 1
        results.append({
            "file_path": path,
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "language": row.get("language"),
            "matches": matches,
        })
        if len(results) >= limit:
            break
    return results


async def find_symbol_usages(
    store,
    repo_id: str,
    symbols: list[str],
    *,
    exclude_paths: set[str] | None = None,
    limit: int = 12,
    staging_prefix: str | None = None,
) -> list[dict]:
    """Return chunks that reference exact identifiers (callers, importers)."""
    names = [name for name in dict.fromkeys(_FTS_TERM_CLEAN.sub("", str(symbol)) for symbol in symbols) if len(name) >= 3][:8]
    if not names:
        return []
    expression = " OR ".join(f'"{name.lower()}"' for name in names)
    rows = await store.fetch_all(
        "SELECT c.id, c.file_path, c.start_line, c.end_line, c.language, c.symbols, c.content FROM chunks_fts "
        "JOIN chunks c ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? AND c.repo_id = ? "
        "ORDER BY bm25(chunks_fts, 1.0, 4.0) ASC LIMIT ?",
        [expression, repo_id, max(1, limit) * 4],
    )
    excluded = exclude_paths or set()
    results: list[dict] = []
    seen: set[str] = set()
    for row in exclude_staged(rows, staging_prefix):
        path = str(row.get("file_path") or "")
        if not path or path in excluded or path in seen or is_overview_file(path):
            continue
        content = str(row.get("content") or "")
        if not any(re.search(rf"\b{re.escape(name)}\b", content) for name in names):
            continue
        seen.add(path)
        results.append(row)
        if len(results) >= limit:
            break
    return results


_REPO_MAP_CACHE: dict[str, tuple[float, dict]] = {}
_REPO_MAP_TTL_SECONDS = 120.0


def invalidate_repo_map(repo_id: str) -> None:
    _REPO_MAP_CACHE.pop(repo_id, None)


async def build_repo_map(store, repo_id: str) -> dict:
    """A compact directory tree of the indexed files, cached briefly.

    The text form is added to every answer prompt so the model can name exact
    folders; the structured form powers the repository overview.
    """
    import time as _time

    cached = _REPO_MAP_CACHE.get(repo_id)
    now = _time.monotonic()
    if cached and now - cached[0] < _REPO_MAP_TTL_SECONDS:
        return cached[1]
    rows = await store.fetch_all(
        "SELECT file_path, byte_size FROM repo_files WHERE repo_id = ? ORDER BY file_path LIMIT 25000", [repo_id]
    )
    if not rows:
        rows = await store.fetch_all(
            "SELECT DISTINCT file_path, 0 AS byte_size FROM chunks WHERE repo_id = ? ORDER BY file_path LIMIT 25000",
            [repo_id],
        )
    directories: dict[str, dict] = {}
    root_files: list[str] = []
    extensions: dict[str, int] = defaultdict(int)
    for row in rows:
        path = str(row.get("file_path") or "")
        if not path:
            continue
        size = int(row.get("byte_size") or 0)
        parts = path.split("/")
        extension = path.rsplit(".", 1)[-1].lower() if "." in parts[-1] else parts[-1].lower()
        extensions[extension] += 1
        if len(parts) == 1:
            root_files.append(path)
            continue
        for depth in range(1, min(len(parts), 4)):
            prefix = "/".join(parts[:depth])
            entry = directories.setdefault(prefix, {"path": prefix, "files": 0, "bytes": 0, "depth": depth})
            entry["files"] += 1
            entry["bytes"] += size

    def children(prefix: str, depth: int) -> list[dict]:
        found = [
            entry for path, entry in directories.items()
            if entry["depth"] == depth and (depth == 1 or path.startswith(prefix + "/"))
        ]
        return sorted(found, key=lambda entry: (-entry["files"], entry["path"]))

    tree = []
    for top in children("", 1):
        node = {"path": top["path"], "files": top["files"], "bytes": top["bytes"], "children": []}
        for second in children(top["path"], 2)[:12]:
            node["children"].append({
                "path": second["path"], "files": second["files"], "bytes": second["bytes"],
                "children": [
                    {"path": third["path"], "files": third["files"], "bytes": third["bytes"], "children": []}
                    for third in children(second["path"], 3)[:8]
                ],
            })
        tree.append(node)

    budget = max(400, int(settings.repo_map_max_characters))
    lines = [f"{len(rows):,} indexed files."]
    if root_files:
        lines.append("Root files: " + ", ".join(sorted(root_files)[:15]))
    for node in tree:
        lines.append(f"{node['path']}/ ({node['files']} files)")
        for child in node["children"][:8]:
            lines.append(f"  {child['path']}/ ({child['files']})")
            for grandchild in child["children"][:4]:
                lines.append(f"    {grandchild['path']}/ ({grandchild['files']})")
    text_lines: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            text_lines.append("  …")
            break
        text_lines.append(line)
        used += len(line) + 1
    result = {
        "total_files": len(rows),
        "root_files": sorted(root_files)[:60],
        "tree": tree[:40],
        # Every extension is kept so language percentages are computed over
        # all indexed files; display code truncates, not this map.
        "extensions": dict(sorted(extensions.items(), key=lambda item: (-item[1], item[0]))),
        "text": "\n".join(text_lines),
    }
    _REPO_MAP_CACHE[repo_id] = (now, result)
    if len(_REPO_MAP_CACHE) > 256:
        oldest = min(_REPO_MAP_CACHE, key=lambda key: _REPO_MAP_CACHE[key][0])
        _REPO_MAP_CACHE.pop(oldest, None)
    return result
