"""Repository question, streaming answer, and conversation endpoints."""

import asyncio
import hashlib
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
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask

from agent.agent import ModelSwitch, run_agent_loop, stream_agent_answer, stream_answer_continuation
from agent.code_edit import create_edit_ticket, edit_file_paths
from agent.editor import EditRequest, recent_proposal_for, run_code_edit
from agent.nemotron import AnswerRestart, LLMProviderError, TruncatedAnswer
from api.answer_trust import (
    annotate_history_names,
    answer_check_entry,
    continuation_base,
    describe_answer_failure,
    describe_model_switch,
    find_unverified_identifiers,
    join_continuation,
    restore_answer_check,
)
from api import impact
from api.auth import get_current_user
from api.repository_state import fetch_accessible_repository, repository_access_by_id, role_allows
from config import ModelConfigurationError, get_editing_ticket_secret, query_request_limiter, settings
from database import DatabaseConfigurationError, assert_turso_schema, explain_database_error, get_turso_store
from ingest.cloner import RepositoryValidationError, parse_github_repository
from retrieval.code_search import find_symbols, indexed_file_lines, is_support_path
from retrieval.retriever import (
    build_evidence_plan,
    build_repo_map,
    escape_like,
    extract_qualified_names,
    extract_question_identifiers,
    extract_requested_file_paths,
    is_impact_question,
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
# A question about the change proposed in this conversation ("how does the
# revised patch handle bytes keys?"). Its diff is not in the index.
_PROPOSAL_QUESTION = re.compile(
    r"\b(?:proposal|proposed|patch|diff|revision|revised|your (?:fix|change|changes|edit)|the (?:fix|change) you)\b",
    re.IGNORECASE,
)
_PROPOSAL_EVIDENCE_CHARACTERS = 8000


def proposal_evidence(proposal: dict) -> dict:
    """The conversation's latest proposal as generated evidence: each file's
    changes as search/replace text. Not a citation (it is not indexed code)."""
    parts = [f"Proposal: {proposal.get('title') or 'untitled'}"]
    if proposal.get("summary"):
        parts.append(str(proposal["summary"]))
    for item in proposal.get("files") or []:
        parts.append(f"--- {item.get('file_path')} ({item.get('action') or 'modify'})")
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                continue
            old, new = str(change.get("old") or ""), str(change.get("new") or "")
            parts.append(f"Replaces:\n{old}\nWith:\n{new}" if old else f"Adds:\n{new}")
    text = "\n".join(parts)
    if len(text) > _PROPOSAL_EVIDENCE_CHARACTERS:
        text = text[:_PROPOSAL_EVIDENCE_CHARACTERS] + "\n[The rest of the proposal is not shown.]"
    paths = [str(item.get("file_path")) for item in proposal.get("files") or [] if item.get("file_path")]
    return {"_synthetic": "proposal_changes", "file_path": ", ".join(paths) or "the proposal", "content": text,
            "start_line": 1, "end_line": 1}
# Change impact as evidence. A go/no-go or "what would break" question about
# a file (and a name in it) sees what the Change impact report knows: the
# files that import it, the test files that reach it, and the test functions
# that use the name. Static analysis of the import graph, not coverage.
IMPACT_EVIDENCE_DEPTH = 2
IMPACT_EVIDENCE_LIMIT = 12
IMPACT_EVIDENCE_TEST_FILES = 15
IMPACT_EVIDENCE_TEST_FUNCTIONS = 12
# Test functions shown as citable windows (the rest are only listed).
IMPACT_EVIDENCE_TEST_WINDOWS = 6
IMPACT_GRAPH_WAIT_SECONDS = 5.0


def _impact_symbol_candidates(question: str) -> list[str]:
    """Code names a change question is about, ``Flask.make_response`` as ``make_response``."""
    return list(dict.fromkeys([
        *(member for _, member in extract_qualified_names(question)), *extract_question_identifiers(question),
    ]))[:3]


async def _impact_target(store, repo: dict, question: str, requested_files: list[str], names: list[str]) -> str | None:
    """The one indexed file a change question is about: the file it names, or
    the only source file that declares the name it asks about."""
    for requested in requested_files[:2]:
        matches, complete = await impact.resolve_target(store, repo, requested)
        if len(matches) == 1 and complete:
            return matches[0]
    if requested_files or not names:
        return None
    declared = {
        item["path"] for item in await find_symbols(store, repo["id"], names[0], limit=8,
                                                    staging_prefix=repo.get("staging_prefix"))
        if item["name"] == names[0] and not is_support_path(item["path"])
    }
    return next(iter(declared)) if len(declared) == 1 else None


def _impact_test_windows(functions: list[dict], files: dict[str, dict[int, str]], symbol: str, evidence_plan: dict) -> list[dict]:
    """Short, citable windows of the first test functions that use ``symbol``."""
    windows: list[dict] = []
    for item in functions[:IMPACT_EVIDENCE_TEST_WINDOWS]:
        lines = files.get(item["file_path"]) or {}
        start = int(item["line"])
        reference = int(item.get("reference_line") or start)
        spans = [(start, reference + 1)] if reference - start <= 10 else [(start, start + 1), (reference - 1, reference + 1)]
        for first, last in spans:
            last = min(last, max(lines) if lines else last)
            if not lines or any(number not in lines for number in range(first, last + 1)):
                continue
            reason = (
                f"Test function `{item['name']}` uses `{symbol}`" if item.get("kind") == "reference"
                else f"Test function `{item['name']}` is named after `{symbol}`"
            ) + " (Change impact)"
            windows.append({
                "id": f"impact_test:{item['file_path']}#L{first}-L{last}",
                "file_path": item["file_path"], "start_line": first, "end_line": last, "language": "",
                "symbols": [str(item["name"])], "content": "\n".join(lines[number] for number in range(first, last + 1)),
                "_retrieval_methods": ["change_impact"], "_retrieval_reasons": [reason],
                "_evidence_plan": evidence_plan, "_relevance_score": 1.0,
            })
    return windows


def _impact_summary_text(report: dict, symbol: str | None) -> str:
    target = report["target_file"]
    lines = [
        f"Static change impact for {target}: resolved imports of the indexed code, {IMPACT_EVIDENCE_DEPTH} levels deep. "
        "No tests were run, so this is not measured coverage.",
        f"Summary: {report['summary']}",
    ]
    direct = [node for node in report.get("dependents") or [] if node.get("depth") == 1]
    if direct:
        shown = ", ".join(f"{node['file_path']}:{node['line_number']}" for node in direct[:IMPACT_EVIDENCE_LIMIT])
        more = report["dependent_count"] - min(len(direct), IMPACT_EVIDENCE_LIMIT)
        lines.append(f"Direct importers ({report['dependent_count']}, path:line of the import): {shown}"
                     + (f", and {more} more" if more > 0 else ""))
    tests = report.get("affected_tests") or {}
    files = tests.get("files") or []
    if files:
        shown = ", ".join(
            f"{item['file_path']}" + (f" (via {item['via']})" if item.get("depth", 1) > 1 else f":{item['line_number']}")
            for item in files[:IMPACT_EVIDENCE_TEST_FILES]
        )
        more = int(tests.get("count") or len(files)) - min(len(files), IMPACT_EVIDENCE_TEST_FILES)
        lines.append(f"Affected test files ({tests.get('count', len(files))}, they import {target} within "
                     f"{IMPACT_EVIDENCE_DEPTH} levels): {shown}" + (f", and {more} more" if more > 0 else ""))
    else:
        lines.append(f"Affected test files: none of the test files imports {target} within {IMPACT_EVIDENCE_DEPTH} levels "
                     f"({tests.get('indirect_count', 0)} more reach it further away).")
    if symbol:
        info = report.get("symbol") or {}
        if info.get("defined_at"):
            lines.append(f"`{symbol}` is defined at {target}:{info['defined_at']}.")
        functions = tests.get("functions") or []
        using = [item for item in functions if item.get("kind") == "reference"]
        named = [item for item in functions if item.get("kind") == "name"]
        if using:
            lines.append(f"Test functions that use `{symbol}` ({len(using)}): " + "; ".join(
                f"{item['file_path']}:{item['line']} {item['name']} (uses it at line {item['reference_line']})"
                for item in using[:IMPACT_EVIDENCE_TEST_FUNCTIONS]
            ) + ("; and more" if len(using) > IMPACT_EVIDENCE_TEST_FUNCTIONS else ""))
        else:
            lines.append(f"Test functions that use `{symbol}`: none found in the affected test files.")
        if named:
            lines.append(f"Test functions named after `{symbol}` without using the name ({len(named)}): " + "; ".join(
                f"{item['file_path']}:{item['line']} {item['name']}" for item in named[:IMPACT_EVIDENCE_TEST_FUNCTIONS]
            ))
    return "\n".join(lines)


async def change_impact_evidence(store, repo: dict, question: str, evidence_plan: dict) -> list[dict]:
    """Generated impact evidence (never cited) plus citable windows of the test
    functions that use the named symbol, for an impact/risk question."""
    if not is_impact_question(question):
        return []
    names = _impact_symbol_candidates(question)
    target = await _impact_target(store, repo, question, list(evidence_plan.get("requested_files") or []), names)
    if not target:
        return []
    graph = await impact.ensure_current_graph(store, repo, wait_seconds=IMPACT_GRAPH_WAIT_SECONDS)
    report, symbol = None, None
    for name in names[:2] or [None]:
        report = await impact.analyse_impact(
            store, repo, target, depth=IMPACT_EVIDENCE_DEPTH, symbol=name, limit=IMPACT_EVIDENCE_LIMIT,
        )
        if name and (report.get("symbol") or {}).get("mentions"):
            symbol = name
            break
    if names and symbol is None:
        # None of the names occurs in the file: report the file alone.
        report = await impact.analyse_impact(store, repo, target, depth=IMPACT_EVIDENCE_DEPTH, symbol=None,
                                             limit=IMPACT_EVIDENCE_LIMIT)
    tests = report.get("affected_tests") or {}
    functions = list(tests.get("functions") or []) if symbol else []
    evidence_plan["change_impact"] = {
        "target": target, "symbol": symbol, "depth": IMPACT_EVIDENCE_DEPTH,
        "direct_importers": report.get("dependent_count", 0), "affected_test_files": tests.get("count", 0),
        "test_functions": sum(1 for item in functions if item.get("kind") == "reference"),
        "graph_rebuilding": bool(graph.get("rebuilding")),
    }
    evidence = [{
        "_synthetic": "change_impact_summary", "file_path": target, "content": _impact_summary_text(report, symbol),
        "start_line": 1, "end_line": 1,
    }]
    if functions:
        shown = functions[:IMPACT_EVIDENCE_TEST_WINDOWS]
        files = await indexed_file_lines(store, repo["id"], [item["file_path"] for item in shown],
                                         staging_prefix=repo.get("staging_prefix"))
        evidence.extend(_impact_test_windows(shown, files, symbol, evidence_plan))
    return evidence


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
# Conversation ids are 32 lowercase hex characters (uuid4().hex, or
# hex(randomblob(16)) for conversations made by turso/07_conversations.sql).
CONVERSATION_ID_PATTERN = r"^[0-9a-f]{32}$"
CONVERSATION_TITLE_MAX = 80


class QueryRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8_000)
    model_profile: Literal["fast", "detailed", "code"] = "fast"
    workflow: Literal["general", "onboarding", "security", "architecture", "contributor", "due_diligence", "editing"] = "general"
    # The stored id of an answer that stopped at the output limit: write the
    # rest of that answer instead of answering ``question`` again.
    continue_from: str | None = Field(default=None, min_length=1, max_length=64)
    # The conversation to continue. `new_conversation` starts a new one
    # instead; with neither, the most recent conversation continues (the
    # behaviour of clients from before conversations existed).
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN)
    new_conversation: bool = False

    @model_validator(mode="after")
    def _one_conversation_target(self):
        if self.new_conversation and self.conversation_id:
            raise ValueError("Send either conversation_id or new_conversation, not both.")
        return self


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def get_owned_repo(store, repo_name: str, user_id: str, permission: str = "view"):
    return await fetch_accessible_repository(store, user_id, repo_name, permission=permission)


async def get_conversation_history(store, repo_id: str, user_id: str, conversation_id: str | None, limit: int | None = None):
    """Recent messages of one conversation, for follow-up questions.

    Only the conversation being continued counts: a new chat has no history,
    and another conversation's topic never leaks into retrieval.
    """
    if not conversation_id:
        return []
    rows = await store.fetch_all(
        "SELECT role, content, citations FROM chat_messages WHERE conversation_id = ? AND repo_id = ? AND user_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        [conversation_id, repo_id, user_id, limit or settings.conversation_history_messages],
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


def build_retrieval_fallback(chunks: list[dict], workflow: str = "general", reason: str | None = None) -> str:
    """The evidence-only reply when no model could write an answer; ``reason`` says why."""
    lines = [
        "## Retrieved code context",
        f"No answer was written. {reason} The most relevant repository evidence is below."
        if reason else "Live answer generation is temporarily unavailable. The most relevant repository evidence is below.",
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


async def _indexed_line_counts(store, repo_id: str, paths: list[str]) -> dict[str, int]:
    """The last indexed line of each file (one grouped query); {} when unavailable."""
    wanted = list(dict.fromkeys(path for path in paths if path))[:40]
    if not wanted or not repo_id:
        return {}
    try:
        rows = await store.fetch_all(
            f"SELECT file_path, MAX(end_line) AS last_line FROM chunks WHERE repo_id = ? "
            f"AND file_path IN ({', '.join('?' for _ in wanted)}) GROUP BY file_path",
            [repo_id, *wanted],
        )
    except Exception as error:
        logger.debug("Reference line lookup unavailable (%s)", type(error).__name__)
        return {}
    counts = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("file_path"):
            with suppress(TypeError, ValueError):
                counts[str(row["file_path"])] = int(row.get("last_line") or 0)
    return counts


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
    ``reason`` says why a reference is not valid, so the chat can say what is
    actually wrong: "range_outside_evidence" (the file is indexed and
    ``file_path`` is set, but the lines were not in this answer's evidence),
    "lines_outside_file" (the file has only ``file_lines`` lines),
    "ambiguous" (``candidates`` lists the files the name matches),
    "not_in_index", or "lookup_failed".
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
        several = len(reference["_evidence_matches"]) > 1 and not any(
            item.lower() == reference["path"].lower() for item in reference["_evidence_matches"])
        if not reference["_evidence_matches"] or several:
            # Several evidence files share a bare name (examples/*/index.js):
            # the index may hold the file it names exactly (the root index.js).
            unresolved.append(reference["path"])
    indexed = await _indexed_paths_for(store, repo_id, unresolved) if (unresolved and repo_id) else {}

    replacements: list[tuple[tuple[int, int], str]] = []
    results: list[dict] = []
    for reference in references:
        path, start, end = reference["path"], reference["start"], reference["end"]
        resolved, reason, repaired = None, None, False
        ambiguous_files: list[str] = []
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
            in_index = (indexed or {}).get(path) or []
            exact_in_index = [item for item in in_index if item.lower() == path.lower()]
            if len(exact_in_index) == 1:
                resolved = exact_in_index[0]
            else:
                reason, ambiguous_files = "ambiguous", list(dict.fromkeys([*candidates, *in_index]))
        else:
            in_index = (indexed or {}).get(path) if indexed is not None else None
            # `index.js` is the root index.js even when examples/*/index.js exist.
            exact_in_index = [item for item in in_index or [] if item.lower() == path.lower()]
            if in_index and (len(in_index) == 1 or len(exact_in_index) == 1):
                resolved = exact_in_index[0] if exact_in_index else in_index[0]
            elif in_index and len(in_index) > 1:
                reason, ambiguous_files = "ambiguous", in_index
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
        result = {
            "text": text,
            "file_path": resolved,
            "start_line": start,
            "end_line": end,
            "valid": bool(valid),
            "reason": reason,
            "repaired": bool(repaired and valid),
        }
        if ambiguous_files:
            # Shallowest first: the files a reader most likely meant.
            ranked = sorted(dict.fromkeys(ambiguous_files), key=lambda item: (item.count("/"), item))
            result["candidates"], result["candidate_count"] = ranked[:8], len(ranked)
        results.append(result)
    # An indexed file referenced outside the evidence: lines past its end do
    # not exist at all (`index.js:L1-L99` for an 11-line file), which is a
    # different problem from lines that exist but were not shown.
    outside = [item for item in results if item["reason"] == "range_outside_evidence" and item["file_path"]]
    if outside and repo_id:
        line_counts = await _indexed_line_counts(store, repo_id, [item["file_path"] for item in outside])
        for item in outside:
            last_line = line_counts.get(item["file_path"])
            if last_line and item["start_line"] and (item["end_line"] or item["start_line"]) > last_line:
                item["reason"], item["file_lines"] = "lines_outside_file", last_line
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
                       mode: str | None = None, latency_ms: int | None = None, message_id: str | None = None,
                       conversation_id: str | None = None) -> str:
    """Store one chat message and return its id."""
    message_id = message_id or str(uuid4())
    executor = getattr(store, "execute_idempotent", store.execute)
    await executor(
        "INSERT INTO chat_messages (id, repo_id, user_id, role, content, citations, tool_calls, mode, latency_ms, created_at, conversation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        [message_id, repo_id, user_id, role, content, json.dumps(citations or []), json.dumps(tool_calls or []), mode, latency_ms, timestamp(),
         conversation_id],
    )
    return message_id


# ---------------------------------------------------------------------------
# Conversations: several per repository, private to the user who asked.
# ---------------------------------------------------------------------------


def conversation_title(question: str) -> str:
    """A new conversation's title: its first question on one line, cut at a word."""
    text = " ".join(str(question or "").split())
    if len(text) <= CONVERSATION_TITLE_MAX:
        return text or "Untitled conversation"
    cut = text[:CONVERSATION_TITLE_MAX - 1]
    space = cut.rfind(" ")
    if space >= CONVERSATION_TITLE_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


_CONVERSATION_COLUMNS = "c.id, c.title, c.created_at, c.updated_at"
# Shown in the history list. A reply holds a proposal when its tool calls
# include the code editor's (see answer_events).
_CONVERSATION_COUNTS = (
    "(SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id AND m.repo_id = c.repo_id "
    "AND m.user_id = c.user_id) AS message_count, "
    "(SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id AND m.repo_id = c.repo_id "
    "AND m.user_id = c.user_id AND m.role = 'assistant' AND instr(m.tool_calls, '\"code_edit\"') > 0) AS proposal_count"
)
# An answer whose messages could not be saved leaves an empty conversation
# behind; it is never listed or opened.
_HAS_MESSAGES = (
    "EXISTS (SELECT 1 FROM chat_messages e WHERE e.conversation_id = c.id AND e.repo_id = c.repo_id AND e.user_id = c.user_id)"
)


def _conversation_payload(row: dict | None) -> dict | None:
    if not row or not row.get("id"):
        return None
    payload = {key: row.get(key) for key in ("id", "title", "created_at", "updated_at")}
    if row.get("deleted_at"):
        payload["deleted_at"] = row["deleted_at"]
    for key in ("message_count", "proposal_count"):
        if key in row:
            payload[key] = int(row.get(key) or 0)
    return payload


async def find_conversation(store, repo_id: str, user_id: str, conversation_id: str, *,
                            include_deleted: bool = False, counts: bool = False) -> dict | None:
    where = "c.id = ? AND c.repo_id = ? AND c.user_id = ?" + ("" if include_deleted else " AND c.deleted_at IS NULL")
    columns = f"{_CONVERSATION_COLUMNS}, c.deleted_at" + (f", {_CONVERSATION_COUNTS}" if counts else "")
    return await store.fetch_one(f"SELECT {columns} FROM conversations c WHERE {where}", [conversation_id, repo_id, user_id])


async def latest_conversation(store, repo_id: str, user_id: str, *, include_empty: bool = False) -> dict | None:
    """The conversation a repository opens with: the one most recently asked in.

    `include_empty` also counts a conversation with no messages yet, such as
    the new chat of DELETE /conversations/{repo}.
    """
    has_messages = "" if include_empty else f" AND {_HAS_MESSAGES}"
    return await store.fetch_one(
        f"SELECT {_CONVERSATION_COLUMNS} FROM conversations c WHERE c.repo_id = ? AND c.user_id = ? "
        f"AND c.deleted_at IS NULL{has_messages} ORDER BY c.updated_at DESC, c.id DESC LIMIT 1",
        [repo_id, user_id],
    )


async def adopt_unthreaded_messages(store, repo_id: str, user_id: str) -> None:
    """Give messages saved without a conversation one of their own.

    turso/07_conversations.sql threads every message that exists when it runs;
    a message written afterwards by an older server (during a deploy) has no
    conversation and would otherwise never be shown. The id comes from the
    oldest such message, so two requests adopting at once agree on it.
    """
    row = await store.fetch_one(
        "SELECT MIN(created_at) AS first_at, MAX(created_at) AS last_at, COUNT(*) AS total FROM chat_messages "
        "WHERE conversation_id IS NULL AND repo_id = ? AND user_id = ?",
        [repo_id, user_id],
    )
    if not row or not int(row.get("total") or 0):
        return
    question = await store.fetch_one(
        "SELECT content FROM chat_messages WHERE conversation_id IS NULL AND repo_id = ? AND user_id = ? AND role = 'user' "
        "ORDER BY created_at, id LIMIT 1",
        [repo_id, user_id],
    )
    conversation_id = hashlib.sha256(f"{repo_id}\0{user_id}\0{row.get('first_at')}".encode()).hexdigest()[:32]
    executor = getattr(store, "execute_idempotent", store.execute)
    await executor(
        "INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        [conversation_id, repo_id, user_id,
         conversation_title((question or {}).get("content") or "Earlier conversation"),
         row.get("first_at") or timestamp(), row.get("last_at") or timestamp()],
    )
    await executor(
        "UPDATE chat_messages SET conversation_id = ? WHERE conversation_id IS NULL AND repo_id = ? AND user_id = ?",
        [conversation_id, repo_id, user_id],
    )


async def resolve_conversation(store, repo: dict, req, user_id: str) -> dict:
    """The conversation a question belongs to; ``{"id": None}`` starts a new one."""
    if getattr(req, "new_conversation", False):
        return {"id": None}
    conversation_id = getattr(req, "conversation_id", None)
    if conversation_id:
        row = await find_conversation(store, repo["id"], user_id, conversation_id)
        if not row:
            raise HTTPException(
                status_code=404, detail="This conversation was deleted or does not exist. Start a new chat to keep asking.",
            )
        return row
    try:
        # Clients that send no id continue the newest conversation, including
        # the empty one their "New chat" (DELETE /conversations/{repo}) made.
        return await latest_conversation(store, repo["id"], user_id, include_empty=True) or {"id": None}
    except Exception as error:
        # Continuity is not required to answer: the answer starts a new conversation.
        logger.warning("Latest conversation unavailable for repository %s: %s", repo["id"], type(error).__name__)
        return {"id": None}


async def save_exchange(store, *, repo_id: str, user_id: str, conversation: dict, question: str, answer: str,
                        citations: list, tool_calls: list, mode: str, latency_ms: int) -> tuple[dict, str]:
    """Save a question and its answer in their conversation, creating it for a new chat.

    Returns the conversation and the stored answer's id (Continue addresses it)."""
    executor = getattr(store, "execute_idempotent", store.execute)
    now = timestamp()
    conversation_id = conversation.get("id")
    if not conversation_id:
        conversation_id = uuid4().hex
        conversation = {"id": conversation_id, "title": conversation_title(question), "created_at": now}
        await executor(
            "INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            [conversation_id, repo_id, user_id, conversation["title"], now, now],
        )
    else:
        # A conversation made empty by DELETE /conversations/{repo} takes its
        # title from its first question.
        title = conversation.get("title") or conversation_title(question)
        conversation = {**conversation, "title": title}
        await executor(
            "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ? AND user_id = ?",
            [now, title, conversation_id, user_id],
        )
    await save_message(store, repo_id=repo_id, user_id=user_id, role="user", content=question, conversation_id=conversation_id)
    message_id = await save_message(
        store, repo_id=repo_id, user_id=user_id, role="assistant", content=answer,
        citations=citations, tool_calls=tool_calls, mode=mode, latency_ms=latency_ms, conversation_id=conversation_id,
    )
    return _conversation_payload({**conversation, "updated_at": now}), message_id


class StreamProgress:
    """What a streamed answer has sent so far: its sources and text, and whether it finished."""

    def __init__(self) -> None:
        self.citations: list = []
        self.parts: list[str] = []
        self.finished = False

    def see(self, event: str, data: dict) -> None:
        if event == "citations":
            self.citations = list(data.get("citations") or [])
        elif event == "token":
            self.parts.append(str(data.get("text") or ""))
        elif event in {"continue", "done", "error"}:
            # Continue updates a stored message; done and error end the
            # exchange themselves (done after it was saved).
            self.finished = True


INTERRUPTED_BY_READER = {
    "reason": "interrupted",
    "cause": "client",
    "message": "The answer was stopped before it finished (Stop, a reload or a closed tab).",
}


async def save_interrupted_exchange(store, repo: dict, req, user_id: str, conversation: dict | None,
                                    progress: StreamProgress, timeout: float = 10.0) -> None:
    """Save a question whose streamed answer the reader left before it was saved.

    Without this the question vanished: a reload mid-answer cancels the
    request, and the exchange was only written once the answer was complete.
    It is stored as a partial answer with the reason, so the chat shows the
    text that was written and offers Try again. Edit & PR runs are not
    stored this way (a proposal is all or nothing).
    """
    question = str(getattr(req, "question", "") or "").strip()
    if (progress.finished or not question or getattr(req, "continue_from", None)
            or (getattr(req, "workflow", "general") or "general") == "editing"):
        return
    written = "".join(progress.parts).strip()
    answer = written + "\n\n> The answer was stopped here. Ask again to regenerate it." if written else (
        "> The answer was stopped before any text was written. Ask again to regenerate it."
    )
    check = answer_check_entry(answer_failure=INTERRUPTED_BY_READER)

    async def save() -> None:
        await save_exchange(
            store, repo_id=repo["id"], user_id=user_id, conversation=conversation or {}, question=question,
            answer=answer, citations=progress.citations, tool_calls=[check] if check else [], mode="partial", latency_ms=0,
        )

    try:
        # Shielded: the request is being cancelled, the save must still finish.
        await asyncio.wait_for(asyncio.shield(save()), timeout=timeout)
    except BaseException:
        logger.warning("Could not save an interrupted answer for repository %s", repo.get("id"), exc_info=True)


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


async def answer_events(store, repo: dict, req, user_id: str, *, stream: bool,
                        conversation: dict | None = None) -> AsyncIterator[tuple[str, dict]]:
    """The single answer pipeline behind the JSON and the streaming endpoints.

    `conversation` is the result of resolve_conversation when the caller
    already checked it (the streaming endpoint does, before its 200).
    """
    started = time.time()
    repo_name = str(getattr(req, "repo_name"))
    _check_answerable(repo, req)
    if getattr(req, "continue_from", None):
        async for event in continue_answer_events(store, repo, req, user_id, stream=stream):
            yield event
        return
    if conversation is None:
        conversation = await resolve_conversation(store, repo, req, user_id)
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
    unverified_identifiers: list[dict] = []
    # Stopped at the output limit; why no model answered; which model did.
    truncated = False
    answer_failure: dict | None = None
    answer_notice: dict | None = None
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
            history = await get_conversation_history(store, repo["id"], user_id, conversation.get("id"))
        except Exception as error:
            # History improves continuity but is not required to answer.
            logger.warning("Conversation history unavailable for repository %s: %s", repo["id"], type(error).__name__)
            history = []
        if workflow != "editing":
            await annotate_history_names(store, repo["id"], history)
        retrieval_query, boost_paths = build_retrieval_query(question, history)
        yield "stage", {"stage": "retrieving", "detail": "Searching the indexed code"}
        # Change impact (importers, affected tests, tests using the name) is
        # computed beside retrieval for an impact or go/no-go question.
        impact_task = asyncio.create_task(
            change_impact_evidence(store, repo, question, evidence_plan)
        ) if workflow != "editing" and is_impact_question(question) else None
        try:
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
        except BaseException:
            if impact_task is not None:
                impact_task.cancel()
            raise
        # Final citation boundary: orientation files only for explicit
        # documentation or repository-overview requests.
        if not evidence_plan.get("overview_files_allowed"):
            chunks = [chunk for chunk in chunks if not is_overview_file(str(chunk.get("file_path", "")))]
        if impact_task is not None:
            try:
                impact_chunks = await impact_task
            except Exception:
                logger.warning("Change impact evidence unavailable for repository %s", repo["id"], exc_info=True)
                impact_chunks = []
            if impact_chunks:
                chunks = [*impact_chunks, *chunks]
        if workflow != "editing" and history and _PROPOSAL_QUESTION.search(question):
            # Questions about the change proposed in this conversation see
            # its diff (round 2: "how does the revised proposal handle bytes
            # keys?" got "cannot determine").
            try:
                proposal = await recent_proposal_for(store, repo["id"], user_id, history)
            except Exception:
                logger.debug("Proposal lookup for a follow-up failed", exc_info=True)
                proposal = None
            if proposal:
                chunks = [proposal_evidence(proposal), *chunks]
                evidence_plan["proposal_in_evidence"] = proposal.get("id")
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
                            if isinstance(text, AnswerRestart):
                                # The text shown so far was model reasoning
                                # (a stray closing tag followed it).
                                parts.clear()
                                yield "reset", {}
                                continue
                            if isinstance(text, TruncatedAnswer):
                                truncated = True
                                continue
                            if isinstance(text, ModelSwitch):
                                answer_notice = describe_model_switch(text, effective_model_profile)
                                continue
                            parts.append(text)
                            yield "token", {"text": text}
                        answer = "".join(parts)
                    except (LLMProviderError, ModelConfigurationError) as error:
                        answer_failure = describe_answer_failure(error, effective_model_profile)
                        if parts:
                            # Named here too, for API clients and copied text;
                            # the chat shows answer_failure as a notice instead.
                            answer = "".join(parts) + (
                                f"\n\n> The answer was interrupted. {answer_failure['message']} Ask again to regenerate it."
                            )
                            mode = "partial"
                        else:
                            # The other model failed too: nothing was written by it.
                            answer_notice = None
                            logger.warning("Live answer generation unavailable for repository %s (%s); returning evidence",
                                           repo["id"], answer_failure["reason"])
                            answer, mode = build_retrieval_fallback(citation_chunks, workflow, answer_failure["message"]), "retrieval_fallback"
                else:
                    try:
                        result = await run_agent_loop(
                            store, repo["id"], question, context, history, effective_model_profile,
                            workflow=workflow, evidence_plan=evidence_plan, repo_map_text=repo_map_text,
                        )
                        answer, tool_calls = result[0], list(result[1] or [])
                        truncated = isinstance(answer, TruncatedAnswer)
                    except (LLMProviderError, ModelConfigurationError) as error:
                        answer_failure = describe_answer_failure(error, effective_model_profile)
                        logger.warning("Live answer generation unavailable for repository %s (%s); returning evidence",
                                       repo["id"], answer_failure["reason"])
                        answer, tool_calls, mode = (
                            build_retrieval_fallback(citation_chunks, workflow, answer_failure["message"]), [], "retrieval_fallback",
                        )
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
            if workflow != "editing" and mode in {"rag", "partial"}:
                # A model-written answer: every backticked code name must
                # exist somewhere in the indexed code.
                try:
                    unverified_identifiers = await find_unverified_identifiers(
                        store, repo.get("id"), answer, evidence_for_check,
                    )
                except Exception:
                    logger.warning("Answer identifier check failed", exc_info=True)
                    unverified_identifiers = []
            if answer_references or unverified_identifiers:
                invalid = [item for item in answer_references if not item["valid"]]
                evidence_plan["reference_check"] = {
                    "checked": len(answer_references), "unverified": len(invalid),
                    "repaired": sum(1 for item in answer_references if item.get("repaired")),
                    "identifiers_not_found": len(unverified_identifiers),
                }
            if region_citations:
                # An edit cites the lines its hunks change.
                citations = region_citations
            else:
                citations = rank_citations_by_answer(answer, citations, answer_references)
            if truncated and mode == "rag":
                # Stored as incomplete until the rest is written (Continue).
                mode = "truncated"

    latency_ms = int((time.time() - started) * 1000)
    stored_tool_calls = tool_calls
    if workflow != "editing":
        check = answer_check_entry(
            answer_references=answer_references, unverified_identifiers=unverified_identifiers,
            answer_failure=answer_failure, answer_notice=answer_notice,
        )
        stored_tool_calls = [*tool_calls, check] if check else tool_calls
    message_id: str | None = None
    saved_conversation = _conversation_payload(conversation)
    try:
        saved_conversation, message_id = await save_exchange(
            store, repo_id=repo["id"], user_id=user_id, conversation=conversation, question=question, answer=answer,
            citations=citations, tool_calls=stored_tool_calls, mode=mode, latency_ms=latency_ms,
        )
    except Exception:
        # Chat history is durable telemetry, not a prerequisite for the answer.
        logger.warning("Could not persist chat history for repository %s", repo["id"], exc_info=True)
        message_id = None
    yield "done", {
        # The conversation the exchange was saved in (null when a new chat
        # could not be saved), so the client can continue it.
        "conversation": saved_conversation,
        "answer": answer, "mode": mode, "citations": citations, "tool_calls": tool_calls,
        "latency_ms": latency_ms, "tokens_used": 0, "model_profile": effective_model_profile,
        "workflow": workflow, "evidence_plan": evidence_plan, "answer_references": answer_references,
        "unverified_identifiers": unverified_identifiers, "truncated": mode == "truncated",
        "answer_failure": answer_failure, "answer_notice": answer_notice, "message_id": message_id,
        "edit_suggestion": edit_suggestion, "edit_ticket": edit_ticket,
        "snapshot": {"commit_sha": repo.get("commit_sha"), "github_url": repo.get("github_url")},
    }


async def continue_answer_events(store, repo: dict, req, user_id: str, *, stream: bool) -> AsyncIterator[tuple[str, dict]]:
    """Write the rest of a stored answer that stopped at the output limit.

    The model gets the original question, the evidence stored with the answer
    (its citations hold exactly the lines the model was shown) and the answer
    so far, cut back to its last complete sentence. The joined answer is
    checked like a new one and replaces the stored message, which stays
    marked "truncated" only if the continuation reached the limit too.
    Events: stage, ``continue`` (the kept text, which the client shows in
    place of the cut-off answer), token, done; a failure leaves the stored
    answer unchanged.
    """
    started = time.time()
    message_id = str(getattr(req, "continue_from") or "")
    model_profile = getattr(req, "model_profile", "fast") or "fast"
    workflow = getattr(req, "workflow", "general") or "general"
    if workflow == "editing" or model_profile == "code":
        raise HTTPException(status_code=422, detail="Only answers to questions can be continued.")
    row = await store.fetch_one(
        "SELECT id, role, content, citations, tool_calls, mode, created_at FROM chat_messages "
        "WHERE id = ? AND repo_id = ? AND user_id = ?",
        [message_id, repo["id"], user_id],
    )
    if not row or row.get("role") != "assistant":
        raise HTTPException(status_code=404, detail="That answer is not in this conversation any more.")
    if row.get("mode") != "truncated":
        raise HTTPException(status_code=409, detail="That answer is already complete.")
    stored_citations = [item for item in row.get("citations") or [] if isinstance(item, dict)]
    evidence = [
        {**item, "_retrieval_reasons": item.get("retrieval_reasons") or []}
        for item in stored_citations if item.get("file_path") and isinstance(item.get("content"), str)
    ]
    if not evidence:
        raise HTTPException(status_code=409, detail="The evidence for that answer is no longer stored. Ask the question again.")
    question_row = await store.fetch_one(
        "SELECT content FROM chat_messages WHERE repo_id = ? AND user_id = ? AND role = 'user' AND created_at <= ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        [repo["id"], user_id, row.get("created_at") or ""],
    )
    question = str((question_row or {}).get("content") or getattr(req, "question", "")).strip()
    evidence_plan = build_evidence_plan(question, workflow)
    context, shown = build_context_with_coverage(evidence)
    try:
        repo_map_text = (await build_repo_map(store, repo["id"])).get("text", "")
    except Exception:
        logger.debug("Repository map unavailable", exc_info=True)
        repo_map_text = ""
    base = continuation_base(str(row.get("content") or ""))
    yield "stage", {"stage": "generating", "detail": "Writing the rest of the answer"}
    yield "continue", {"base": base, "message_id": message_id}
    parts: list[str] = []
    truncated = False
    try:
        async for text in stream_answer_continuation(
            question, context, base, model_profile, workflow, evidence_plan, repo_map_text,
        ):
            if isinstance(text, AnswerRestart):
                parts.clear()
                if stream:
                    yield "continue", {"base": base, "message_id": message_id}
                continue
            if isinstance(text, TruncatedAnswer):
                truncated = True
                continue
            parts.append(text)
            if stream:
                yield "token", {"text": text}
    except (LLMProviderError, ModelConfigurationError) as error:
        failure = describe_answer_failure(error, model_profile)
        logger.warning("Answer continuation failed for repository %s (%s)", repo["id"], failure["reason"])
        raise HTTPException(
            status_code=503, detail=f"The rest of the answer could not be written. {failure['message']} The answer so far is unchanged.",
        ) from error
    addition = "".join(parts)
    if not addition.strip():
        raise HTTPException(status_code=503, detail="The model returned nothing more to add. The answer so far is unchanged.")
    answer = join_continuation(base, addition)
    try:
        answer, answer_references = await validate_answer_references(store, repo["id"], answer, shown)
    except Exception:
        logger.warning("Answer reference validation failed", exc_info=True)
        answer_references = []
    try:
        unverified_identifiers = await find_unverified_identifiers(store, repo["id"], answer, shown)
    except Exception:
        logger.warning("Answer identifier check failed", exc_info=True)
        unverified_identifiers = []
    citations = rank_citations_by_answer(answer, [dict(item) for item in stored_citations], answer_references)
    mode = "truncated" if truncated else "rag"
    previous = restore_answer_check({"tool_calls": row.get("tool_calls") or []})
    answer_notice = previous.get("answer_notice")
    check = answer_check_entry(
        answer_references=answer_references, unverified_identifiers=unverified_identifiers, answer_notice=answer_notice,
    )
    evidence_plan["reference_check"] = {
        "checked": len(answer_references), "unverified": sum(1 for item in answer_references if not item["valid"]),
        "repaired": sum(1 for item in answer_references if item.get("repaired")),
        "identifiers_not_found": len(unverified_identifiers),
    }
    try:
        await store.execute(
            "UPDATE chat_messages SET content = ?, citations = ?, tool_calls = ?, mode = ? "
            "WHERE id = ? AND repo_id = ? AND user_id = ?",
            [answer, json.dumps(citations), json.dumps([*previous.get("tool_calls", []), *([check] if check else [])]),
             mode, message_id, repo["id"], user_id],
        )
    except Exception:
        logger.warning("Could not store the continued answer for repository %s", repo["id"], exc_info=True)
    yield "done", {
        "answer": answer, "mode": mode, "citations": citations, "tool_calls": [],
        "latency_ms": int((time.time() - started) * 1000), "tokens_used": 0, "model_profile": model_profile,
        "workflow": workflow, "evidence_plan": evidence_plan, "answer_references": answer_references,
        "unverified_identifiers": unverified_identifiers, "truncated": truncated,
        "answer_failure": None, "answer_notice": answer_notice, "message_id": message_id, "continued": True,
        "edit_suggestion": None, "edit_ticket": None,
        "snapshot": {"commit_sha": repo.get("commit_sha"), "github_url": repo.get("github_url")},
    }


_PLAN_KEYS_FROM_RETRIEVAL = (
    "follow_up_terms", "absent_terms", "ignored_file_like_terms", "missing_files", "strict_target_relaxed",
    "semantic_search", "directory_listing", "unretrieved_identifiers", "exact_search", "subclass_search", "docs_mentions",
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
        repo = await get_owned_repo(store, req.repo_name, current_user.id, "edit" if getattr(req, "workflow", "") == "editing" else "view")
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
        repo = await get_owned_repo(store, req.repo_name, current_user.id, "edit" if getattr(req, "workflow", "") == "editing" else "view")
        _check_answerable(repo, req)
        # A deleted or unknown conversation is a plain 404, like the checks above.
        conversation = await resolve_conversation(store, repo, req, current_user.id)
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
            progress = StreamProgress()
            try:
                async for event, data in answer_events(store, repo, req, current_user.id, stream=True, conversation=conversation):
                    progress.see(event, data)
                    await queue.put((event, data))
            except asyncio.CancelledError:
                # The reader went away (reload, closed tab, Stop) before the
                # answer was saved: keep the question and what was written.
                await save_interrupted_exchange(store, repo, req, current_user.id, conversation, progress)
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


def _history_citations(citations) -> list:
    """Citations for a stored answer without the cited code itself.

    The code text is ~95% of a stored answer (about 35 KB each) and the UI
    only needs it for a hover preview, which already fetches the file when a
    citation carries no excerpt. Everything else the UI shows is kept.
    """
    if not isinstance(citations, list):
        return []
    return [
        {key: value for key, value in citation.items() if key != "content"}
        if isinstance(citation, dict) else citation
        for citation in citations
    ]


def _history_cursor(message: dict) -> str:
    return f"{message.get('created_at') or ''}|{message.get('id') or ''}"


def _conversation_http_error(error: Exception, action: str) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, DatabaseConfigurationError):
        return HTTPException(status_code=503, detail=str(error))
    logger.exception("Could not %s", action)
    return HTTPException(status_code=500, detail=explain_database_error(error))


async def _repository_for(repo_name: str, user_id: str):
    await assert_turso_schema()
    store = get_turso_store()
    return store, await get_owned_repo(store, repo_name, user_id)


async def _existing_conversation(store, repo: dict, user_id: str, conversation_id: str, **options) -> dict:
    if not re.fullmatch(CONVERSATION_ID_PATTERN, conversation_id or ""):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    row = await find_conversation(store, repo["id"], user_id, conversation_id, **options)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return row


@router.get("/conversations/{repo_name}")
async def get_conversation(
    repo_name: str,
    limit: int = Query(default=100, ge=1, le=200),
    before: str | None = Query(default=None, max_length=200),
    conversation_id: str | None = Query(default=None, max_length=64),
    current_user=Depends(get_current_user),
):
    """One conversation's newest `limit` messages, oldest first; `before` pages back through older ones.

    `conversation_id` picks the conversation; without it the most recently
    active one opens (`conversation` is null when there is none yet).
    `before` is the `next_before` cursor of the previous page. The response
    says whether older messages exist so the client can load them on demand.
    """
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        if conversation_id:
            conversation = await _existing_conversation(store, repo, current_user.id, conversation_id)
        else:
            await adopt_unthreaded_messages(store, repo["id"], current_user.id)
            conversation = await latest_conversation(store, repo["id"], current_user.id)
            if not conversation:
                return {"messages": [], "has_more": False, "next_before": None, "conversation": None}
        where = "conversation_id = ? AND repo_id = ? AND user_id = ?"
        params = [conversation["id"], repo["id"], current_user.id]
        if before:
            created_at, _, message_id = before.partition("|")
            where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params += [created_at, created_at, message_id]
        rows = await store.fetch_all(
            "SELECT id, role, content, citations, tool_calls, mode, latency_ms, created_at "
            f"FROM chat_messages WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit + 1],
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        for message in page:
            message["citations"] = _history_citations(message.get("citations"))
            restore_answer_check(message)
        messages = list(reversed(page))
        return {
            "messages": messages,
            "has_more": has_more,
            "next_before": _history_cursor(messages[0]) if has_more and messages else None,
            "conversation": _conversation_payload(conversation),
        }
    except Exception as error:
        raise _conversation_http_error(error, "load conversation history") from error


@router.delete("/conversations/{repo_name}")
async def clear_conversation(repo_name: str, current_user=Depends(get_current_user)):
    """Start a new chat for clients from before conversations existed.

    Nothing is hidden or erased: every conversation stays in the list. This
    adds an empty conversation (listed once it has messages), and the next
    question sent without a conversation_id goes into it.
    """
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        latest = await latest_conversation(store, repo["id"], current_user.id, include_empty=True)
        # An empty title means a new chat nobody has asked in yet: reuse it.
        if not latest or latest.get("title"):
            now = timestamp()
            await store.execute(
                "INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, '', ?, ?)",
                [uuid4().hex, repo["id"], current_user.id, now, now],
            )
        return {"cleared": True}
    except Exception as error:
        raise _conversation_http_error(error, "clear conversation history") from error


@router.get("/conversations/{repo_name}/threads")
async def list_conversations(
    repo_name: str,
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None, max_length=200),
    deleted: bool = Query(default=False),
    current_user=Depends(get_current_user),
):
    """The user's saved conversations for one repository, most recently active first.

    Each has its title, dates, and message and proposal counts. `before` is
    the `next_before` cursor of the previous page. `deleted=true` lists the
    deleted ones instead, most recently deleted first, so they can be restored.
    """
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        await adopt_unthreaded_messages(store, repo["id"], current_user.id)
        order = "deleted_at" if deleted else "updated_at"
        where = "c.repo_id = ? AND c.user_id = ? AND c.deleted_at " + ("IS NOT NULL" if deleted else "IS NULL")
        params = [repo["id"], current_user.id]
        if before:
            value, _, last_id = before.partition("|")
            where += f" AND (c.{order} < ? OR (c.{order} = ? AND c.id < ?))"
            params += [value, value, last_id]
        rows = await store.fetch_all(
            f"SELECT {_CONVERSATION_COLUMNS}, c.deleted_at, {_CONVERSATION_COUNTS} FROM conversations c "
            f"WHERE {where} AND {_HAS_MESSAGES} ORDER BY c.{order} DESC, c.id DESC LIMIT ?",
            [*params, limit + 1],
        )
        has_more = len(rows) > limit
        page = [_conversation_payload(row) for row in rows[:limit]]
        return {
            "conversations": page,
            "has_more": has_more,
            "next_before": f"{page[-1][order]}|{page[-1]['id']}" if has_more and page else None,
        }
    except Exception as error:
        raise _conversation_http_error(error, "list conversations") from error


@router.patch("/conversations/{repo_name}/threads/{conversation_id}")
async def rename_conversation(
    repo_name: str, conversation_id: str, body: ConversationRenameRequest, current_user=Depends(get_current_user),
):
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(status_code=422, detail="Enter a title.")
    if len(title) > 120:
        raise HTTPException(status_code=422, detail="Titles can be at most 120 characters.")
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        await _existing_conversation(store, repo, current_user.id, conversation_id)
        await store.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND repo_id = ? AND user_id = ?",
            [title, conversation_id, repo["id"], current_user.id],
        )
        return {"conversation": _conversation_payload(await find_conversation(
            store, repo["id"], current_user.id, conversation_id, counts=True,
        ))}
    except Exception as error:
        raise _conversation_http_error(error, "rename a conversation") from error


@router.delete("/conversations/{repo_name}/threads/{conversation_id}")
async def delete_conversation(repo_name: str, conversation_id: str, current_user=Depends(get_current_user)):
    """Hide one conversation. Nothing is erased; .../restore brings it back."""
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        row = await _existing_conversation(store, repo, current_user.id, conversation_id, include_deleted=True)
        if not row.get("deleted_at"):
            await store.execute(
                "UPDATE conversations SET deleted_at = ? WHERE id = ? AND repo_id = ? AND user_id = ? AND deleted_at IS NULL",
                [timestamp(), conversation_id, repo["id"], current_user.id],
            )
        return {"deleted": True, "conversation": _conversation_payload(row)}
    except Exception as error:
        raise _conversation_http_error(error, "delete a conversation") from error


@router.post("/conversations/{repo_name}/threads/{conversation_id}/restore")
async def restore_conversation(repo_name: str, conversation_id: str, current_user=Depends(get_current_user)):
    """Bring back a deleted conversation, in its old place in the list."""
    try:
        store, repo = await _repository_for(repo_name, current_user.id)
        row = await _existing_conversation(store, repo, current_user.id, conversation_id, include_deleted=True)
        if row.get("deleted_at"):
            await store.execute(
                "UPDATE conversations SET deleted_at = NULL WHERE id = ? AND repo_id = ? AND user_id = ?",
                [conversation_id, repo["id"], current_user.id],
            )
        return {"restored": True, "conversation": _conversation_payload(await find_conversation(
            store, repo["id"], current_user.id, conversation_id, counts=True,
        ))}
    except Exception as error:
        raise _conversation_http_error(error, "restore a conversation") from error


@router.get("/edits/{proposal_id}")
async def get_edit_proposal(proposal_id: str, current_user=Depends(get_current_user)):
    """Reopen a saved proposal with a fresh short-lived editing ticket."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", proposal_id):
        raise HTTPException(status_code=404, detail="Proposal not found.")
    try:
        await assert_turso_schema()
        store = get_turso_store()
        row = await store.fetch_one(
            "SELECT payload_json, repo_id FROM edit_proposals WHERE id = ? AND user_id = ?", [proposal_id, current_user.id],
        )
        access = await repository_access_by_id(store, current_user.id, str(row["repo_id"])) if row else None
        if not row or not access:
            raise HTTPException(status_code=404, detail="Proposal not found.")
        proposal = json.loads(row["payload_json"])
        repo_name = str(access["repo_name"])
        ticket = issue_edit_ticket(current_user.id, repo_name, proposal) if role_allows(access["role"], "edit") else None
        return {"proposal": proposal, "edit_ticket": ticket, "repo_name": repo_name}
    except HTTPException:
        raise
    except DatabaseConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Could not load edit proposal")
        raise HTTPException(status_code=500, detail=explain_database_error(error)) from error
