"""Repository question, streaming answer, and conversation endpoints."""

import asyncio
import json
import logging
import posixpath
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from agent.agent import run_agent_loop, stream_agent_answer
from agent.code_edit import create_edit_ticket, edit_file_paths
from agent.editor import EditRequest, run_code_edit
from agent.nemotron import LLMProviderError
from api.auth import get_current_user
from api.repository_state import fetch_owned_repository
from config import ModelConfigurationError, get_editing_ticket_secret, query_request_limiter, settings
from database import DatabaseConfigurationError, assert_turso_schema, explain_database_error, get_turso_store
from ingest.cloner import RepositoryValidationError, parse_github_repository
from retrieval.retriever import (
    build_evidence_plan,
    build_repo_map,
    escape_like,
    extract_question_identifiers,
    extract_requested_file_paths,
    is_overview_file,
    retrieve_context,
    search_terms,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_GREETING_PATTERN = re.compile(
    r"^(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening))(?:\s+(?:there|everyone|again))?[!. ]*$",
    re.IGNORECASE,
)
_EDIT_ACTION_PATTERN = re.compile(
    r"\b(?:change|fix|update|replace|add|remove|delete|refactor|modify|implement|patch|make|set|rename|create|"
    r"support|handle|improve|optimi[sz]e|convert|migrate|upgrade|bump|resolve|introduce|extract|move|split|merge|"
    r"enable|disable|allow|prevent|validate|translate|document|test)\b",
    re.IGNORECASE,
)
_ISSUE_LINK = re.compile(r"github\.com/[^/\s]+/[^/\s]+/issues/\d+", re.IGNORECASE)
# Follow-up detection. A question is a follow-up only when it leans on the
# previous turn: it starts with a connective ("and where is that called?")
# or refers back with a pronoun while naming almost nothing of its own
# ("show me the tests for it"). A self-contained question that merely
# contains "it/this/that" keeps its own evidence.
_FOLLOW_UP_CUE = re.compile(
    r"^\s*(?:and|also|so|then|what about|how about|where else|same|ok(?:ay)?|now|but|plus|and then)\b",
    re.IGNORECASE,
)
_REFERENCE_PRONOUN = re.compile(
    r"\b(?:it|its|this|that|these|those|they|them|there|above|previous|earlier|the same|that file|this file)\b",
    re.IGNORECASE,
)
_SELF_REFERENCE = re.compile(
    r"\b(?:this|the|that|your|our)\s+(?:project|repo|repository|codebase|code\s*base|app|application|platform|product|system)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_FILLER = {
    "called", "call", "calls", "caller", "callers", "used", "use", "uses", "usage", "usages", "tests", "test", "tested",
    "spec", "specs", "tell", "more", "explain", "detail", "details", "work", "works", "working", "happen", "happens",
    "else", "defined", "define", "definition", "implemented", "implementation", "file", "files", "function",
    "functions", "method", "methods", "class", "example", "examples", "why", "who", "can", "could", "should", "see",
    "find", "look", "list", "mean", "means", "thanks", "thank", "line", "lines", "part", "exactly", "instead",
    "other", "another", "same", "then", "there", "here", "them", "they", "those", "these", "done", "handle",
    "handled", "handles", "change", "changed", "modify", "fix", "get", "gets", "return", "returns", "need", "needs",
    "happening", "mentioned", "previous", "earlier", "above", "one", "ones", "thing", "things", "stuff", "next",
}
_BACKTICK_IDENTIFIER = re.compile(r"`([A-Za-z_$][\w$]{2,80})(?:\(\))?`")
class QueryRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8_000)
    model_profile: Literal["fast", "detailed", "code"] = "fast"
    workflow: Literal["general", "onboarding", "security", "architecture", "contributor", "due_diligence", "editing"] = "general"


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def get_owned_repo(store, repo_name: str, user_id: str):
    return await fetch_owned_repository(store, user_id, repo_name)


async def get_conversation_history(store, repo_id: str, user_id: str, limit: int | None = None):
    rows = await store.fetch_all(
        "SELECT role, content, citations FROM chat_messages WHERE repo_id = ? AND user_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        [repo_id, user_id, limit or settings.conversation_history_messages],
    )
    remaining, retained = settings.max_conversation_history_characters, []
    for message in rows:
        content = str(message.get("content") or "").strip()
        if not content or remaining <= 0:
            continue
        content = content[:min(settings.max_conversation_message_characters, remaining)]
        citations = message.get("citations") if isinstance(message.get("citations"), list) else []
        retained.append({
            "role": message["role"],
            "content": content,
            "cited_paths": _answer_cited_paths(citations),
        })
        remaining -= len(content)
    return list(reversed(retained))


def _own_content_terms(question: str) -> list[str]:
    return [term for term in search_terms(question) if term not in _FOLLOW_UP_FILLER]


def is_follow_up_question(question: str) -> bool:
    """Whether a question needs the previous turn to be understood."""
    text = str(question or "").strip()
    if not text or len(text.split()) > 18:
        return False
    stripped = _SELF_REFERENCE.sub(" ", text)
    has_cue = bool(_FOLLOW_UP_CUE.search(stripped))
    has_reference = bool(_REFERENCE_PRONOUN.search(stripped))
    if not (has_cue or has_reference):
        return False
    # A named identifier makes the question self-contained.
    if extract_question_identifiers(stripped) or extract_requested_file_paths(stripped):
        return False
    content = _own_content_terms(stripped)
    return len(content) < (3 if has_cue else 2)


def _answer_cited_paths(citations: list) -> list[str]:
    """Files the answer actually cited (older rows without flags: all)."""
    items = [item for item in citations if isinstance(item, dict) and item.get("file_path")]
    if any("cited_in_answer" in item for item in items):
        items = [item for item in items if item.get("cited_in_answer")]
    return list(dict.fromkeys(str(item["file_path"]) for item in items))[:8]


def build_retrieval_query(question: str, history: list[dict]) -> tuple[str | None, list[str]]:
    """Carry technical context into genuine follow-up questions only.

    "Where is it called?" has no searchable terms by itself; the topic comes
    from the most recent self-contained user question (walking back through a
    chain of follow-ups), plus identifiers the previous answer named. Only
    files the previous answer actually cited are boosted.
    """
    if not history or not is_follow_up_question(question):
        return None, []
    topic: list[str] = []
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        topic.append(content[:400])
        if not is_follow_up_question(content) or len(topic) >= 3:
            break
    last_answer = next((item for item in reversed(history) if item.get("role") == "assistant"), None) or {}
    cited = list(last_answer.get("cited_paths") or [])[:4]
    identifiers = list(dict.fromkeys(
        match.group(1) for match in _BACKTICK_IDENTIFIER.finditer(str(last_answer.get("content") or ""))
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", match.group(1))
    ))[:6]
    if not topic and not cited and not identifiers:
        return None, []
    parts = [question, *topic]
    if identifiers:
        parts.append(" ".join(identifiers))
    return "\n".join(parts), cited


def build_context_with_coverage(chunks: list[dict], *, max_characters: int | None = None) -> tuple[str, list[dict]]:
    """Serialize evidence with real line numbers and report what was shown.

    Returns the prompt text and the chunks actually rendered, each with
    ``end_line``/``content`` trimmed to the last line that fit the budget.
    Citations are built from that list, so a source the model never saw is
    never presented as backing the answer.
    """
    remaining, sections, shown = max(1, int(max_characters or settings.max_context_characters)), [], []
    for chunk in chunks:
        if chunk.get("_synthetic"):
            # Generated evidence (a folder listing) has no source lines.
            block = f"Generated {str(chunk['_synthetic']).replace('_', ' ')} for {chunk['file_path']}:\n{chunk.get('content') or ''}"
            if len(block) + 2 > remaining:
                block = block[: max(0, remaining - 2)]
            if block:
                sections.append(block)
                remaining -= len(block) + 2
                shown.append(dict(chunk))
            continue
        start = int(chunk.get("start_line") or 1)
        lines = str(chunk.get("content") or "").split("\n")
        header_end = start + len(lines) - 1

        def make_header(last_line: int) -> str:
            return (
                f"File: {chunk['file_path']} (L{start}-L{last_line})"
                f"\nSymbols: {', '.join(chunk.get('symbols') or []) or 'not detected'}"
                f"\nRetrieval reason: {'; '.join(chunk.get('_retrieval_reasons') or []) or 'source match'}\n"
            )
        budget = remaining - len(make_header(header_end)) - 2
        if budget <= 0:
            break
        numbered: list[str] = []
        used = 0
        for offset, line in enumerate(lines):
            rendered = f"{start + offset:>5}| {line}"
            if used + len(rendered) + 1 > budget:
                break
            numbered.append(rendered)
            used += len(rendered) + 1
        if not numbered:
            break
        last_line = start + len(numbered) - 1
        truncated = last_line < header_end
        # The header states the lines actually shown, so the model cannot
        # cite past a truncation point.
        header = make_header(last_line)
        body = "\n".join(numbered)
        sections.append(f"{header}{body}")
        remaining -= len(header) + len(body) + 2
        visible = dict(chunk)
        if truncated:
            visible["end_line"] = last_line
            visible["content"] = "\n".join(lines[: len(numbered)])
            visible["_truncated"] = True
        shown.append(visible)
        if remaining <= 0:
            break
    return "\n\n".join(sections), shown


def build_context(chunks: list[dict], *, max_characters: int | None = None) -> str:
    """Serialize evidence with real line numbers so citations can be exact."""
    return build_context_with_coverage(chunks, max_characters=max_characters)[0]


def build_retrieval_fallback(chunks: list[dict], workflow: str = "general") -> str:
    lines = [
        "## Retrieved code context",
        "Live answer generation is temporarily unavailable. The most relevant repository evidence is below.",
        "",
        "## Relevant files",
    ]
    for chunk in [item for item in chunks if not item.get("_synthetic")][:6]:
        summary = " ".join(str(chunk.get("content") or "").split())[:220]
        lines.append(f"- `{chunk['file_path']}:L{chunk['start_line']}-L{chunk['end_line']}` — {summary or 'Indexed source evidence.'}")
    lines.extend(["", "## Next step", "Try the question again in a moment. The citations remain available for direct inspection."])
    if workflow == "editing":
        lines.insert(0, "No files were changed and no branch or pull request was created.\n")
    return "\n".join(lines)


def build_no_evidence_response() -> str:
    return (
        "I couldn't find source evidence for that in the indexed files, so I won't guess. I can help you explore this repository: "
        "ask about its architecture, a feature, a file, or how something is implemented — or use the code search in the overview."
    )


_SKIP_REASON_TEXT = {
    "budget": "the repository's indexing size budget was reached before this file",
    "file_budget": "the repository's indexing size budget was reached before this file",
    "byte_budget": "the repository's indexing size budget was reached before this file",
    "chunk_budget": "the per-repository chunk budget was reached before this file",
    "chunking_failed": "the file could not be split into searchable chunks",
}


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def find_skipped_files(store, repo_id: str, requested_paths: list[str]) -> list[dict]:
    """Requested files that indexing skipped (size budget or filters).

    Reads the coverage ledger the pipeline stores: ``repo_coverage``
    (excluded paths and reasons) and the snapshot's
    ``budget_omitted_sample`` (path, category, reason).
    """
    wanted = [str(path).strip().lstrip("./") for path in requested_paths if str(path).strip()]
    if not wanted:
        return []
    entries: dict[str, dict] = {}
    try:
        snapshot = await store.fetch_one("SELECT selection_json FROM repo_snapshots WHERE repo_id = ?", [repo_id]) or {}
        selection = _json_value(snapshot.get("selection_json")) or {}
        for item in (selection.get("budget_omitted_sample") or []) if isinstance(selection, dict) else []:
            if isinstance(item, dict) and item.get("path"):
                entries.setdefault(str(item["path"]), {
                    "reason": str(item.get("reason") or "budget"), "category": item.get("category"),
                })
    except Exception as error:
        logger.debug("Snapshot coverage unavailable (%s)", type(error).__name__)
    try:
        row = await store.fetch_one("SELECT excluded_paths FROM repo_coverage WHERE repo_id = ?", [repo_id]) or {}
        for path in _json_value(row.get("excluded_paths")) or []:
            if isinstance(path, str):
                entries.setdefault(path, {"reason": "excluded", "category": None})
            elif isinstance(path, dict) and path.get("path"):
                entries.setdefault(str(path["path"]), {"reason": str(path.get("reason") or "excluded"), "category": path.get("category")})
    except Exception as error:
        logger.debug("Coverage ledger unavailable (%s)", type(error).__name__)
    skipped: list[dict] = []
    for path in wanted:
        lowered = path.lower()
        match = next((known for known in entries if known.lower() == lowered or known.lower().endswith("/" + lowered)), None)
        if match:
            skipped.append({"path": match, **entries[match]})
    return skipped


def build_skipped_files_response(skipped: list[dict]) -> str:
    lines = ["I can't answer from source for this: the file you named was not indexed, so I won't guess about its contents.", ""]
    for item in skipped:
        reason = _SKIP_REASON_TEXT.get(str(item.get("reason")), None)
        if reason is None:
            reason = "it was excluded by the indexing filters (generated, vendored, binary, or over the size limit)"
        category = f" ({item['category']})" if item.get("category") else ""
        lines.append(f"- `{item['path']}`{category} was skipped during indexing because {reason}.")
    lines.extend(["", "The repository's coverage view lists every skipped file. You can ask about the code that calls this file, "
                  "or re-index with a smaller scope so it fits the budget."])
    return "\n".join(lines)


def is_greeting(question: str) -> bool:
    """Avoid calling retrieval and an answer model for a simple greeting."""
    return bool(_GREETING_PATTERN.fullmatch(question.strip()))


def build_greeting_response(repo_name: str) -> str:
    return f"Hello. What would you like to understand about **{repo_name}**?"


def _github_parts(repo: dict) -> tuple[str, str]:
    try:
        return parse_github_repository(str(repo.get("github_url") or ""))
    except (RepositoryValidationError, ValueError):
        return "", ""


def build_citations(chunks: list[dict], repo: dict) -> list[dict]:
    owner, name = _github_parts(repo)
    ref = str(repo.get("commit_sha") or "HEAD")
    citations = []
    for chunk in chunks:
        if chunk.get("_synthetic"):
            continue
        path = str(chunk["file_path"])
        citations.append({
            "file_path": path,
            "directory": posixpath.dirname(path),
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "content": chunk["content"],
            "language": chunk["language"],
            "symbols": chunk.get("symbols", []),
            "retrieval_methods": chunk.get("_retrieval_methods") or ["retrieval"],
            "retrieval_reasons": chunk.get("_retrieval_reasons") or ["Relevant source match"],
            "relevance_score": chunk.get("_relevance_score", 0),
            "support_status": "source-backed",
            "github_url": (
                f"https://github.com/{owner}/{name}/blob/{quote(ref, safe='')}/{quote(path, safe='/')}"
                f"#L{chunk['start_line']}-L{chunk['end_line']}"
            ) if owner and name else None,
        })
    return citations


# ---------------------------------------------------------------------------
# Answer reference validation
# ---------------------------------------------------------------------------
# The grammar mirrors frontend/src/lib/citations.js so the server checks the
# same references the client turns into chips.
_KNOWN_FILENAMES = {
    "dockerfile", "makefile", "procfile", "gemfile", "rakefile", "jenkinsfile", "vagrantfile", "brewfile", "readme",
    "license", "licence", "changelog", "codeowners", "package.json", "readme.md", "tsconfig.json", "go.mod", "go.sum",
    "cargo.toml", "cargo.lock", "pyproject.toml", "yarn.lock", "gemfile.lock", "poetry.lock", ".env", ".env.example",
    ".env.local", ".env.sample", ".gitignore", ".gitattributes", ".dockerignore", ".npmrc", ".nvmrc", ".prettierrc",
    ".eslintrc", ".editorconfig", ".babelrc",
}
_SOURCE_EXTENSIONS = {
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "py", "pyi", "ipynb", "go", "rs", "java", "kt", "kts", "scala",
    "groovy", "gradle", "rb", "erb", "php", "cs", "cshtml", "razor", "fsx", "swift", "c", "h", "cc", "cpp", "cxx", "hpp",
    "hh", "hxx", "dart", "lua", "jl", "exs", "erl", "hrl", "clj", "cljs", "elm", "hs", "ml", "mli", "zig", "nim", "sol",
    "vue", "svelte", "astro", "md", "mdx", "markdown", "rst", "adoc", "txt", "json", "jsonc", "json5", "yml", "yaml",
    "toml", "ini", "cfg", "conf", "xml", "plist", "html", "htm", "css", "scss", "sass", "less", "styl", "prisma", "sql",
    "graphql", "gql", "proto", "sh", "bash", "zsh", "fish", "ps1", "bat", "tf", "tfvars", "hcl", "nix", "dockerfile",
    "properties", "lock", "svg", "hbs", "ejs", "pug", "njk", "liquid", "twig", "mk", "cmake", "csv", "tsv",
}
_REFERENCE = re.compile(
    r"^(?P<path>[^\s:`'\"<>]+?|[^:`'\"<>]*/[^:`'\"<>]+?)"
    r"(?::L?(?P<start>\d+)(?:\s*[-\u2013]\s*L?(?P<end>\d+))?|\s*\(L(?P<pstart>\d+)(?:\s*[-\u2013]\s*L?(?P<pend>\d+))?\))?$"
)
_SEGMENT = r"(?:\[\[?[\w.-]+\]\]?|\([\w.-]+\)|[\w@.+-])"
_PLAIN_PATH = re.compile(
    rf"(?:^|(?<=[\s(\[])|(?<=\*\*))((?:{_SEGMENT}+/)+{_SEGMENT}+\.[A-Za-z0-9]{{1,8}}):L?(\d+)(?:\s*[-\u2013]\s*L?(\d+))?(?=$|[\s),.;:!?\]*])",
    re.MULTILINE,
)
_BOLD_RANGE = re.compile(r"\*\*([^*\n`]+?)\s*\(L(\d+)(?:\s*[-\u2013]\s*L?(\d+))?\):?\*\*")
_PROSE_RANGE = re.compile(r"\s+\(?(?:from\s+)?(?:on\s+)?lines?\s+(\d+)(?:\s*(?:-|\u2013|to|through)\s*(\d+))?", re.IGNORECASE)
_FENCED = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_MAX_REFERENCES = 80


def looks_like_path(value: str) -> bool:
    raw = str(value or "").strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE) or raw.startswith("//"):
        return False
    text = re.sub(r"^\.?/", "", raw)
    if not text or len(text) > 300 or text.startswith("@") or re.search(r"\s", text):
        return False
    segments = text.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        return False
    name = segments[-1]
    if name.lower() in _KNOWN_FILENAMES:
        return True
    dot = name.rfind(".")
    extension = name[dot + 1:] if dot > 0 else ""
    if not extension:
        return False
    if extension in _SOURCE_EXTENSIONS:
        return bool(re.fullmatch(r"[\w.()\[\]@+\-]+", name))
    if len(segments) > 1 and extension.lower() in _SOURCE_EXTENSIONS:
        return True
    return len(segments) > 1 and bool(re.fullmatch(r"[a-z0-9]{1,8}", extension)) and bool(re.search(r"[a-z]", extension))


def parse_reference(value: str) -> dict | None:
    text = _TRAILING_PUNCTUATION.sub("", str(value or "").strip().strip("*").strip())
    match = _REFERENCE.match(text)
    if not match or not looks_like_path(match.group("path")):
        return None
    start = int(match.group("start") or match.group("pstart") or 0) or None
    end = int(match.group("end") or match.group("pend") or 0) or start
    if start and end and end < start:
        end = start
    return {"path": re.sub(r"^\.?/", "", match.group("path")), "start": start, "end": end}


def extract_answer_references(answer: str) -> list[dict]:
    """File references in an answer, with their exact text and position.

    Recognises inline code (`path:L1-L9`, `path`, `path` lines 3-9), plain
    ``dir/file.ext:L1-L9`` prose and the legacy ``**path (L1-L9)**`` form.
    Fenced code blocks are ignored.
    """
    text = str(answer or "")
    masked = list(text)
    for fence in _FENCED.finditer(text):
        for index in range(fence.start(), fence.end()):
            masked[index] = " "
    visible = "".join(masked)
    references: list[dict] = []
    taken: list[tuple[int, int]] = []

    def add(span_start: int, span_end: int, parsed: dict) -> None:
        if any(span_start < end and start < span_end for start, end in taken):
            return
        taken.append((span_start, span_end))
        references.append({"text": text[span_start:span_end], "span": (span_start, span_end), **parsed})

    for match in _INLINE_CODE.finditer(visible):
        parsed = parse_reference(match.group(1))
        if not parsed:
            continue
        if parsed["start"] is None:
            prose = _PROSE_RANGE.match(visible, match.end())
            if prose:
                start = int(prose.group(1))
                end = int(prose.group(2) or start)
                parsed = {**parsed, "start": start, "end": max(start, end), "prose_range": prose.group(0)}
        add(match.start(1), match.end(1), parsed)
    for match in _BOLD_RANGE.finditer(visible):
        if looks_like_path(match.group(1).strip()):
            start = int(match.group(2))
            end = int(match.group(3) or start)
            add(match.start(), match.end(), {"path": match.group(1).strip(), "start": start, "end": max(start, end)})
    for match in _PLAIN_PATH.finditer(visible):
        if any(match.start() >= start and match.end() <= end for start, end in taken):
            continue
        path = match.group(1)
        if not looks_like_path(path):
            continue
        start = int(match.group(2))
        end = int(match.group(3) or start)
        add(match.start(), match.end(), {"path": path, "start": start, "end": max(start, end)})
    references.sort(key=lambda item: item["span"][0])
    return references[:_MAX_REFERENCES]


def _merged_intervals(chunks: list[dict]) -> dict[str, list[tuple[int, int]]]:
    by_path: dict[str, list[tuple[int, int]]] = {}
    for chunk in chunks:
        if chunk.get("_synthetic"):
            continue
        try:
            start, end = int(chunk["start_line"]), int(chunk["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        by_path.setdefault(str(chunk.get("file_path") or ""), []).append((start, end))
    merged: dict[str, list[tuple[int, int]]] = {}
    for path, intervals in by_path.items():
        result: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if result and start <= result[-1][1] + 1:
                result[-1] = (result[-1][0], max(result[-1][1], end))
            else:
                result.append((start, end))
        merged[path] = result
    return merged


def _range_inside(intervals: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(low <= start and end <= high for low, high in intervals)


def _suffix_matches(paths, reference_path: str) -> list[str]:
    wanted = reference_path.lower()
    return [path for path in paths if path.lower() == wanted or path.lower().endswith("/" + wanted)]


async def _indexed_paths_for(store, repo_id: str, reference_paths: list[str]) -> dict[str, list[str]] | None:
    """Indexed files matching each referenced path exactly or by suffix.

    One bounded query over ``repo_files`` (and ``chunks`` for indexes without
    a file table). ``None`` means the lookup itself failed.
    """
    wanted = list(dict.fromkeys(path for path in reference_paths if path))[:40]
    if not wanted:
        return {}
    conditions, args = [], []
    for path in wanted:
        conditions.append("(lower(file_path) = ? OR lower(file_path) LIKE ? ESCAPE '\\')")
        args.extend([path.lower(), f"%/{escape_like(path.lower())}"])
    where = " OR ".join(conditions)
    try:
        rows = await store.fetch_all(
            f"SELECT file_path FROM repo_files WHERE repo_id = ? AND ({where}) "
            f"UNION SELECT DISTINCT file_path FROM chunks WHERE repo_id = ? AND ({where}) LIMIT 400",
            [repo_id, *args, repo_id, *args],
        )
    except Exception as error:
        logger.debug("Reference index lookup unavailable (%s)", type(error).__name__)
        return None
    found = [str(row.get("file_path") or "") for row in rows or [] if isinstance(row, dict) and row.get("file_path")]
    return {path: _suffix_matches(found, path) for path in wanted}


async def validate_answer_references(
    store,
    repo_id: str | None,
    answer: str,
    evidence_chunks: list[dict],
    *,
    extra_valid_paths: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Check every file/line reference in an answer against the evidence.

    A reference is valid when its file exists in the repository index and,
    if it carries a line range, the range lies inside evidence the model was
    actually given. A path with a wrong folder whose file name (and range)
    matches exactly one evidence file is repaired in the answer text.

    Returns the (possibly repaired) answer and the ``answer_references``
    list: ``{text, file_path, start_line, end_line, valid, reason, repaired}``.
    """
    references = extract_answer_references(answer)
    if not references:
        return answer, []
    intervals = _merged_intervals(evidence_chunks)
    evidence_paths = list(intervals)
    extra = [path for path in (extra_valid_paths or []) if path]
    unresolved: list[str] = []
    for reference in references:
        matches = _suffix_matches([*evidence_paths, *extra], reference["path"])
        reference["_evidence_matches"] = list(dict.fromkeys(matches))
        if not reference["_evidence_matches"]:
            unresolved.append(reference["path"])
    indexed = await _indexed_paths_for(store, repo_id, unresolved) if (unresolved and repo_id) else {}

    replacements: list[tuple[tuple[int, int], str]] = []
    results: list[dict] = []
    for reference in references:
        path, start, end = reference["path"], reference["start"], reference["end"]
        resolved, reason, repaired = None, None, False
        matches = reference.pop("_evidence_matches")
        exact = [item for item in matches if item.lower() == path.lower()]
        candidates = exact or matches
        if len(candidates) > 1 and start is not None:
            # Several evidence files share the name: the one whose shown lines
            # contain the range is the intended one.
            covering = [item for item in candidates if _range_inside(intervals.get(item, []), start, end)]
            candidates = covering or candidates
        if len(candidates) == 1:
            resolved = candidates[0]
        elif len(candidates) > 1:
            reason = "ambiguous"
        else:
            in_index = (indexed or {}).get(path) if indexed is not None else None
            if in_index and len(in_index) == 1:
                resolved = in_index[0]
            elif in_index and len(in_index) > 1:
                reason = "ambiguous"
            else:
                # Re-anchor a wrong folder: the same file name (and range) in
                # exactly one evidence file.
                basename = path.rsplit("/", 1)[-1].lower()
                same_name = [item for item in evidence_paths if item.rsplit("/", 1)[-1].lower() == basename]
                if start is not None:
                    same_name = [item for item in same_name if _range_inside(intervals.get(item, []), start, end)]
                if len(same_name) == 1 and "/" in path:
                    resolved, repaired = same_name[0], True
                else:
                    reason = "lookup_failed" if indexed is None else "not_in_index"
        valid = resolved is not None and reason is None
        if valid and start is not None and resolved not in extra:
            if not _range_inside(intervals.get(resolved, []), start, end):
                valid, reason = False, "range_outside_evidence"
                repaired = False if repaired and resolved not in evidence_paths else repaired
        text = reference["text"]
        if repaired and valid:
            new_text = text.replace(path, resolved, 1)
            replacements.append((reference["span"], new_text))
            text = new_text
        results.append({
            "text": text,
            "file_path": resolved,
            "start_line": start,
            "end_line": end,
            "valid": bool(valid),
            "reason": reason,
            "repaired": bool(repaired and valid),
        })
    if replacements:
        pieces, cursor = [], 0
        for (span_start, span_end), new_text in sorted(replacements, key=lambda item: item[0][0]):
            pieces.append(answer[cursor:span_start])
            pieces.append(new_text)
            cursor = span_end
        pieces.append(answer[cursor:])
        answer = "".join(pieces)
    return answer, results


def rank_citations_by_answer(answer: str, citations: list[dict], references: list[dict] | None = None) -> list[dict]:
    """Mark and rank first the sources the answer actually cites.

    A citation is cited when a verified reference names its file (exactly or
    by a unique suffix such as ``find.handler.ts``) and, when the reference
    has a line range, that range overlaps the citation's lines.
    """
    if references is None:
        evidence = [{"file_path": c["file_path"], "start_line": c["start_line"], "end_line": c["end_line"]} for c in citations]
        references = []
        for reference in extract_answer_references(answer):
            matches = _suffix_matches([c["file_path"] for c in evidence], reference["path"])
            exact = [item for item in matches if item.lower() == reference["path"].lower()]
            candidates = list(dict.fromkeys(exact or matches))
            if len(candidates) == 1:
                references.append({"file_path": candidates[0], "start_line": reference["start"],
                                   "end_line": reference["end"], "valid": True})
    valid = [item for item in references if item.get("valid") and item.get("file_path")]
    for citation in citations:
        start, end = int(citation.get("start_line") or 0), int(citation.get("end_line") or 0)
        citation["cited_in_answer"] = any(
            item["file_path"] == citation["file_path"]
            and (item.get("start_line") is None or (int(item["start_line"]) <= end and int(item.get("end_line") or item["start_line"]) >= start))
            for item in valid
        )
    return sorted(citations, key=lambda item: (not item["cited_in_answer"],))


async def save_message(store, *, repo_id: str, user_id: str, role: str, content: str,
                       citations: list | None = None, tool_calls: list | None = None,
                       mode: str | None = None, latency_ms: int | None = None) -> None:
    executor = getattr(store, "execute_idempotent", store.execute)
    await executor(
        "INSERT INTO chat_messages (id, repo_id, user_id, role, content, citations, tool_calls, mode, latency_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        [str(uuid4()), repo_id, user_id, role, content, json.dumps(citations or []), json.dumps(tool_calls or []), mode, latency_ms, timestamp()],
    )


def _compact_proposal(proposal: dict, max_bytes: int = 3_000_000) -> str:
    payload = json.dumps(proposal, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) <= max_bytes:
        return payload
    slim = {**proposal, "files": [{**item, "before": None, "after": None, "truncated": True} for item in proposal.get("files") or []]}
    return json.dumps(slim, separators=(",", ":"), ensure_ascii=False)


async def persist_proposal(store, proposal: dict, *, repo: dict, repo_name: str, user_id: str) -> None:
    await store.execute(
        "INSERT INTO edit_proposals (id, user_id, repo_id, repo_name, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [proposal["id"], user_id, repo["id"], repo_name, _compact_proposal(proposal), timestamp()],
    )


def issue_edit_ticket(user_id: str, repo_name: str, proposal: dict) -> str | None:
    paths = edit_file_paths(proposal)
    if not paths:
        return None
    try:
        return create_edit_ticket(
            get_editing_ticket_secret(), user_id=user_id, repo_name=repo_name,
            file_path=paths, ttl_seconds=settings.editing_ticket_ttl_seconds,
        )
    except ValueError:
        logger.warning("Editing ticket secret is not configured; disabling the PR action")
        return None


async def load_user_github_token(store, user_id: str) -> str | None:
    """The user's connected GitHub token, or ``None`` (not connected, not
    configured, or unreadable). Issue reads then fall back to anonymous."""
    try:
        from api.github_router import _load_token

        token, _ = await _load_token(store, user_id)
        return token or None
    except Exception as error:  # HTTPException 401/503, missing table, crypto errors
        logger.debug("GitHub token unavailable for editing (%s)", type(error).__name__)
        return None


def _repo_is_queryable(repo: dict) -> bool:
    if "queryable" in repo:
        return bool(repo["queryable"])
    return repo.get("status") in {"ready", "searchable"}


def _check_answerable(repo: dict, req) -> None:
    """Reject a request that needs no retrieval or model work to refuse.

    The streaming endpoint runs this before its 200 response starts, so a bad
    request fails there as the same plain HTTP error /api/query returns.
    """
    if not _repo_is_queryable(repo):
        raise HTTPException(status_code=409, detail="This repository is still being indexed. Questions open as soon as keyword search is ready.")
    if not str(getattr(req, "question", "")).strip():
        raise HTTPException(status_code=422, detail="Question must contain non-whitespace text.")
    workflow = getattr(req, "workflow", "general") or "general"
    if (getattr(req, "model_profile", "fast") or "fast") == "code" and workflow != "editing":
        raise HTTPException(status_code=422, detail="The code model is available only in Code editing and PR mode.")


async def answer_events(store, repo: dict, req, user_id: str, *, stream: bool) -> AsyncIterator[tuple[str, dict]]:
    """The single answer pipeline behind the JSON and the streaming endpoints."""
    started = time.time()
    repo_name = str(getattr(req, "repo_name"))
    _check_answerable(repo, req)
    question = str(getattr(req, "question", "")).strip()
    workflow = getattr(req, "workflow", "general") or "general"
    model_profile = getattr(req, "model_profile", "fast") or "fast"
    effective_model_profile = "code" if workflow == "editing" else model_profile
    evidence_plan = build_evidence_plan(question, workflow)
    edit_suggestion = None
    edit_ticket = None
    tool_calls: list[dict] = []
    citations: list[dict] = []
    answer_references: list[dict] = []
    mode = "rag"

    if is_greeting(question):
        answer, mode = build_greeting_response(repo_name), "greeting"
    elif (
        workflow == "editing"
        and not _EDIT_ACTION_PATTERN.search(question)
        and not extract_requested_file_paths(question)
        and not _ISSUE_LINK.search(question)
    ):
        # A conversational follow-up such as "does this fix the issue?" has
        # no actionable target; ask for a concrete change instead.
        answer = (
            "## Code editing and PR\n\n"
            "Describe the change you want, for example `Add a loading spinner to the booking form` or "
            "`Fix the timezone bug in apps/web/lib/dates.ts`, or paste a GitHub issue URL. "
            "I'll read the relevant files, write a validated patch, and show you the diff before any pull request."
        )
        mode = "editing_guidance"
    else:
        try:
            history = await get_conversation_history(store, repo["id"], user_id)
        except Exception as error:
            # History improves continuity but is not required to answer.
            logger.warning("Conversation history unavailable for repository %s: %s", repo["id"], type(error).__name__)
            history = []
        retrieval_query, boost_paths = build_retrieval_query(question, history)
        yield "stage", {"stage": "retrieving", "detail": "Searching the indexed code"}
        chunks = await retrieve_context(
            store,
            repo["id"],
            question,
            top_k=(settings.editing_retrieval_top_k if workflow == "editing" else settings.retrieval_top_k),
            workflow=workflow,
            staging_prefix=repo.get("staging_prefix"),
            retrieval_query=retrieval_query,
            boost_paths=boost_paths,
        )
        # Final citation boundary: orientation files only for explicit
        # documentation or repository-overview requests.
        if not evidence_plan.get("overview_files_allowed"):
            chunks = [chunk for chunk in chunks if not is_overview_file(str(chunk.get("file_path", "")))]
        _merge_retrieval_plan(evidence_plan, chunks)
        if retrieval_query:
            evidence_plan["follow_up"] = True
        if not chunks and workflow != "editing":
            skipped = await find_skipped_files(store, repo["id"], evidence_plan.get("requested_files") or [])
            if skipped:
                evidence_plan["skipped_files"] = skipped
                answer, mode = build_skipped_files_response(skipped), "repository_guidance"
            else:
                answer, mode = build_no_evidence_response(), "repository_guidance"
        else:
            try:
                repo_map = await build_repo_map(store, repo["id"])
                repo_map_text = repo_map.get("text", "")
            except Exception:
                logger.debug("Repository map unavailable", exc_info=True)
                repo_map_text = ""
            # Editing evidence includes complete files; cite only a bounded,
            # file-diverse subset in the UI.
            context = ""
            if workflow != "editing":
                # Citations are exactly the evidence the model receives: a
                # chunk cut by the context budget is not shown as a source,
                # and a truncated chunk is cited only up to its last line.
                context, citation_chunks = build_context_with_coverage(chunks)
                if not context:
                    raise HTTPException(status_code=422, detail="Repository evidence exceeded the configured context limit.")
                omitted = len(chunks) - len(citation_chunks)
                if omitted > 0:
                    evidence_plan["evidence_omitted_for_budget"] = omitted
            else:
                citation_chunks = _diverse_citation_chunks(_rank_by_request_terms(chunks, question), 10)
            citations = build_citations(citation_chunks, repo)
            yield "citations", {
                "citations": citations, "evidence_plan": evidence_plan,
                "snapshot": {"commit_sha": repo.get("commit_sha"), "github_url": repo.get("github_url")},
            }
            if workflow == "editing":
                stage_queue: asyncio.Queue = asyncio.Queue()

                async def emit(stage: str, detail: str) -> None:
                    await stage_queue.put((stage, detail))

                edit_request = EditRequest(
                    store=store, repo=repo, user_id=user_id, question=question, evidence_chunks=chunks,
                    evidence_plan=evidence_plan, repo_map_text=repo_map_text, conversation_history=history, emit=emit,
                    github_token=await load_user_github_token(store, user_id),
                )
                edit_task = asyncio.create_task(run_code_edit(edit_request))
                try:
                    while not edit_task.done() or not stage_queue.empty():
                        try:
                            stage, detail = await asyncio.wait_for(stage_queue.get(), timeout=0.25)
                        except asyncio.TimeoutError:
                            continue
                        yield "stage", {"stage": stage, "detail": detail}
                    answer, tool_calls, edit_suggestion = await edit_task
                    mode = "editing"
                except (LLMProviderError, ModelConfigurationError):
                    logger.warning("Code editing unavailable for repository %s", repo["id"])
                    answer, tool_calls, mode, edit_suggestion = build_retrieval_fallback(citation_chunks, workflow), [], "retrieval_fallback", None
                finally:
                    if not edit_task.done():
                        edit_task.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await edit_task
                if edit_suggestion:
                    edit_ticket = issue_edit_ticket(user_id, repo_name, edit_suggestion)
                    try:
                        await persist_proposal(store, edit_suggestion, repo=repo, repo_name=repo_name, user_id=user_id)
                        tool_calls = [*tool_calls, {"tool": "code_edit", "input": {"proposal_id": edit_suggestion["id"]},
                                                    "result_summary": edit_suggestion.get("title", "")}]
                    except Exception:
                        logger.warning("Could not persist the edit proposal", exc_info=True)
            else:
                yield "stage", {"stage": "generating", "detail": "Writing the answer"}
                if stream:
                    parts: list[str] = []
                    try:
                        async for text in stream_agent_answer(
                            question, context, history, effective_model_profile, workflow, evidence_plan, repo_map_text,
                        ):
                            parts.append(text)
                            yield "token", {"text": text}
                        answer = "".join(parts)
                    except (LLMProviderError, ModelConfigurationError):
                        if parts:
                            answer = "".join(parts) + "\n\n> The answer was interrupted. Ask again to regenerate it."
                            mode = "partial"
                        else:
                            logger.warning("Live answer generation unavailable for repository %s; returning evidence", repo["id"])
                            answer, mode = build_retrieval_fallback(citation_chunks, workflow), "retrieval_fallback"
                else:
                    try:
                        result = await run_agent_loop(
                            store, repo["id"], question, context, history, effective_model_profile,
                            workflow=workflow, evidence_plan=evidence_plan, repo_map_text=repo_map_text,
                        )
                        answer, tool_calls = result[0], list(result[1] or [])
                    except (LLMProviderError, ModelConfigurationError):
                        logger.warning("Live answer generation unavailable for repository %s; returning evidence", repo["id"])
                        answer, tool_calls, mode = build_retrieval_fallback(citation_chunks, workflow), [], "retrieval_fallback"
            # Verify every file/line reference in the answer against the
            # evidence the model was given; repair a wrong folder when the
            # file name and range identify one evidence file.
            evidence_for_check = citation_chunks if workflow != "editing" else chunks
            region_citations = _edit_region_citations(chunks, edit_suggestion, repo) if edit_suggestion else []
            try:
                answer, answer_references = await validate_answer_references(
                    store, repo.get("id"), answer, evidence_for_check,
                    extra_valid_paths=edit_file_paths(edit_suggestion) if edit_suggestion else None,
                )
            except Exception:
                logger.warning("Answer reference validation failed", exc_info=True)
                answer_references = []
            if answer_references:
                invalid = [item for item in answer_references if not item["valid"]]
                evidence_plan["reference_check"] = {
                    "checked": len(answer_references), "unverified": len(invalid),
                    "repaired": sum(1 for item in answer_references if item.get("repaired")),
                }
            if region_citations:
                # An edit cites the lines its hunks change.
                citations = region_citations
            else:
                citations = rank_citations_by_answer(answer, citations, answer_references)

    latency_ms = int((time.time() - started) * 1000)
    try:
        await save_message(store, repo_id=repo["id"], user_id=user_id, role="user", content=question)
        await save_message(
            store, repo_id=repo["id"], user_id=user_id, role="assistant", content=answer,
            citations=citations, tool_calls=tool_calls, mode=mode, latency_ms=latency_ms,
        )
    except Exception:
        # Chat history is durable telemetry, not a prerequisite for the answer.
        logger.warning("Could not persist chat history for repository %s", repo["id"], exc_info=True)
    yield "done", {
        "answer": answer, "mode": mode, "citations": citations, "tool_calls": tool_calls,
        "latency_ms": latency_ms, "tokens_used": 0, "model_profile": effective_model_profile,
        "workflow": workflow, "evidence_plan": evidence_plan, "answer_references": answer_references,
        "edit_suggestion": edit_suggestion, "edit_ticket": edit_ticket,
        "snapshot": {"commit_sha": repo.get("commit_sha"), "github_url": repo.get("github_url")},
    }


_PLAN_KEYS_FROM_RETRIEVAL = (
    "follow_up_terms", "absent_terms", "ignored_file_like_terms", "missing_files", "strict_target_relaxed",
    "semantic_search", "directory_listing",
)


def _merge_retrieval_plan(evidence_plan: dict, chunks: list[dict]) -> None:
    """Carry what retrieval learned (absent terms, ignored dotted names, ...)
    into the plan the model and the client see."""
    retrieved = next((chunk.get("_evidence_plan") for chunk in chunks if isinstance(chunk.get("_evidence_plan"), dict)), None)
    if not retrieved:
        return
    for key in _PLAN_KEYS_FROM_RETRIEVAL:
        if retrieved.get(key):
            evidence_plan[key] = retrieved[key]
    if retrieved.get("ignored_file_like_terms"):
        evidence_plan["requested_files"] = list(retrieved.get("requested_files") or [])
        evidence_plan["query_scope"] = retrieved.get("query_scope") or evidence_plan.get("query_scope")


def _rank_by_request_terms(chunks: list[dict], question: str) -> list[dict]:
    """Order editing evidence by how many request terms each chunk contains,
    keeping file order as the tie-breaker, so citations show the relevant
    region of a large file instead of its first lines."""
    terms = [term for term in search_terms(question) if len(term) >= 3]
    if not terms:
        return list(chunks)
    file_order: dict[str, int] = {}
    for chunk in chunks:
        file_order.setdefault(str(chunk.get("file_path")), len(file_order))

    def hits(chunk: dict) -> int:
        content = str(chunk.get("content") or "").lower()
        return sum(1 for term in terms if term in content)
    return sorted(chunks, key=lambda chunk: (file_order[str(chunk.get("file_path"))], -hits(chunk), int(chunk.get("start_line") or 0)))


def _changed_line_spans(chunks: list[dict], proposal_file: dict) -> list[tuple[int, int]]:
    """Line spans (in indexed line numbers) that a proposal's hunks replace."""
    path = str(proposal_file.get("file_path") or "")
    file_chunks = sorted((c for c in chunks if str(c.get("file_path")) == path), key=lambda c: int(c.get("start_line") or 0))
    before = proposal_file.get("before")
    spans: list[tuple[int, int]] = []
    for change in proposal_file.get("changes") or []:
        old = change.get("old") if isinstance(change, dict) else None
        if not isinstance(old, str) or not old:
            continue
        located = None
        for chunk in file_chunks:
            content = str(chunk.get("content") or "")
            index = content.find(old)
            if index >= 0:
                line = int(chunk.get("start_line") or 1) + content[:index].count("\n")
                located = (line, line + old.count("\n"))
                break
        if located is None and isinstance(before, str):
            index = before.find(old)
            if index >= 0:
                line = before[:index].count("\n") + 1
                located = (line, line + old.count("\n"))
        if located:
            spans.append(located)
    return spans


def _edit_region_citations(chunks: list[dict], proposal: dict, repo: dict) -> list[dict]:
    """Citations for an edit proposal: the indexed chunks its hunks change,
    marked as cited, followed by the remaining file-diverse evidence."""
    selected: list[dict] = []
    for item in proposal.get("files") or []:
        if not isinstance(item, dict):
            continue
        for start, end in _changed_line_spans(chunks, item):
            for chunk in chunks:
                if (
                    str(chunk.get("file_path")) == str(item.get("file_path"))
                    and int(chunk.get("start_line") or 0) <= end and int(chunk.get("end_line") or 0) >= start
                    and chunk not in selected
                ):
                    selected.append(chunk)
                    break
    if not selected:
        return []
    cited = build_citations(selected[:10], repo)
    for citation in cited:
        citation["cited_in_answer"] = True
    seen = {(c["file_path"], c["start_line"]) for c in cited}
    rest = [c for c in _diverse_citation_chunks(chunks, 10) if (c["file_path"], c["start_line"]) not in seen]
    others = build_citations(rest[: max(0, 10 - len(cited))], repo)
    for citation in others:
        citation["cited_in_answer"] = False
    return [*cited, *others]


def _diverse_citation_chunks(chunks: list[dict], limit: int) -> list[dict]:
    selected, per_file = [], {}
    for chunk in chunks:
        path = str(chunk.get("file_path"))
        if per_file.get(path, 0) >= 2:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        selected.append(chunk)
        if len(selected) >= limit:
            break
    return selected


def _map_errors(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, (ModelConfigurationError, DatabaseConfigurationError)) and not str(error).startswith("The Turso database connection"):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, RuntimeError) and str(error) in {"rate_limit", "concurrency_limit"}:
        return HTTPException(status_code=429, detail="Too many questions are already being processed. Please wait a moment and try again.")
    logger.exception("Repository query failed", exc_info=error)
    return HTTPException(status_code=500, detail=explain_database_error(error))


@router.post("/query")
async def query_repo(req: QueryRequest, current_user=Depends(get_current_user)):
    limiter_acquired = False
    try:
        await query_request_limiter.acquire(current_user.id)
        limiter_acquired = True
        await assert_turso_schema()
        store = get_turso_store()
        repo = await get_owned_repo(store, req.repo_name, current_user.id)
        result: dict | None = None
        async for event, data in answer_events(store, repo, req, current_user.id, stream=False):
            if event == "done":
                result = data
        if result is None:  # pragma: no cover - the pipeline always finishes with done
            raise HTTPException(status_code=500, detail="The answer could not be completed.")
        return result
    except Exception as error:
        raise _map_errors(error) from error
    finally:
        if limiter_acquired:
            await query_request_limiter.release(current_user.id)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n"


class _ReleaseOnce:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.released = False

    async def __call__(self) -> None:
        if not self.released:
            self.released = True
            await query_request_limiter.release(self.user_id)


class _EventStreamResponse(StreamingResponse):
    """An event stream whose cleanup runs however the response ends.

    When the client disconnects while Starlette is waiting in ``send``, it
    cancels the send and leaves the event generator suspended at a ``yield``.
    Its cleanup (stop generating, then release the per-user slot) would wait
    for garbage collection while the background task had already freed the
    slot, so the model kept generating outside the concurrency limit. With
    ASGI 2.4 servers a disconnect raises instead and skips the background task.
    """

    def __init__(self, content, *, on_close, **kwargs):
        super().__init__(content, background=BackgroundTask(on_close), **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_close()  # idempotent; a no-op after the background task


@router.post("/query/stream")
async def query_repo_stream(req: QueryRequest, current_user=Depends(get_current_user)):
    """Server-sent events: stage, citations, token, done, or error."""
    try:
        await query_request_limiter.acquire(current_user.id)
    except Exception as error:
        raise _map_errors(error) from error
    release = _ReleaseOnce(current_user.id)
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await get_owned_repo(store, req.repo_name, current_user.id)
        _check_answerable(repo, req)
    except BaseException as error:
        # BaseException: a cancelled request (shutdown, timeout) must not keep
        # one of the user's few concurrent slots until the process restarts.
        await release()
        if isinstance(error, Exception):
            raise _map_errors(error) from error
        raise

    async def event_source():
        queue: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async for event, data in answer_events(store, repo, req, current_user.id, stream=True):
                    await queue.put((event, data))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                mapped = _map_errors(error)
                await queue.put(("error", {"detail": mapped.detail, "status": mapped.status_code}))
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            yield ": connected\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Comment frames keep proxies from closing a quiet stream
                    # while the editor reads files or the model thinks.
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield _sse(*item)
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await producer
            await release()

    events = event_source()

    async def close() -> None:
        # Closing the generator runs its ``finally``: cancel the producer,
        # then release. A finished or never-started generator closes at once.
        try:
            with suppress(Exception):
                await events.aclose()
        finally:
            await release()

    return _EventStreamResponse(
        events,
        on_close=close,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/conversations/{repo_name}")
async def get_conversation(repo_name: str, limit: int = Query(default=100, ge=1, le=200), current_user=Depends(get_current_user)):
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await get_owned_repo(store, repo_name, current_user.id)
        messages = await store.fetch_all(
            "SELECT id, role, content, citations, tool_calls, mode, latency_ms, created_at "
            "FROM chat_messages WHERE repo_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            [repo["id"], current_user.id, limit],
        )
        return {"messages": list(reversed(messages))}
    except HTTPException:
        raise
    except DatabaseConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Could not load conversation history")
        raise HTTPException(status_code=500, detail=explain_database_error(error)) from error


@router.delete("/conversations/{repo_name}")
async def clear_conversation(repo_name: str, current_user=Depends(get_current_user)):
    """Start a fresh conversation for one repository."""
    try:
        await assert_turso_schema()
        store = get_turso_store()
        repo = await get_owned_repo(store, repo_name, current_user.id)
        await store.execute("DELETE FROM chat_messages WHERE repo_id = ? AND user_id = ?", [repo["id"], current_user.id])
        return {"cleared": True}
    except HTTPException:
        raise
    except DatabaseConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Could not clear conversation history")
        raise HTTPException(status_code=500, detail=explain_database_error(error)) from error


@router.get("/edits/{proposal_id}")
async def get_edit_proposal(proposal_id: str, current_user=Depends(get_current_user)):
    """Reopen a saved proposal with a fresh short-lived editing ticket."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", proposal_id):
        raise HTTPException(status_code=404, detail="Proposal not found.")
    try:
        await assert_turso_schema()
        store = get_turso_store()
        row = await store.fetch_one(
            "SELECT p.payload_json, p.repo_name, r.repo_name AS current_name FROM edit_proposals p "
            "JOIN repos r ON r.id = p.repo_id WHERE p.id = ? AND p.user_id = ? AND r.user_id = ?",
            [proposal_id, current_user.id, current_user.id],
        )
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found.")
        proposal = json.loads(row["payload_json"])
        repo_name = str(row.get("current_name") or row.get("repo_name"))
        return {"proposal": proposal, "edit_ticket": issue_edit_ticket(current_user.id, repo_name, proposal), "repo_name": repo_name}
    except HTTPException:
        raise
    except DatabaseConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Could not load edit proposal")
        raise HTTPException(status_code=500, detail=explain_database_error(error)) from error
