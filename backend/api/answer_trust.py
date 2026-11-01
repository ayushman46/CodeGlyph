"""What an answer can honestly claim: code names that exist, incomplete
answers, and why a model failed.

* ``find_unverified_identifiers`` looks every backticked code name of an
  answer up in the indexed code (an FTS probe plus an exact-word check), so
  an invented method or subclass is flagged instead of sitting under the
  reference check.
* ``continuation_base`` / ``join_continuation`` append the rest of an answer
  that stopped at the output limit to the same message.
* ``describe_answer_failure`` / ``describe_model_switch`` name the model
  failure in words the chat can show.
* ``answer_check_entry`` / ``restore_answer_check`` keep these results with
  the stored message (in its ``tool_calls`` JSON, so no schema change), so a
  reloaded conversation shows the same checks and notices.
"""

import builtins
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Code names that occur nowhere in the indexed code
# ---------------------------------------------------------------------------

_FENCED = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_CALL_ARGUMENTS = re.compile(r"\([^()]*\)$")
_RECEIVER = re.compile(r"^(?:self|cls|this)\.")
_DOTTED_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)*")
_NAME_SEPARATOR = re.compile(r"\.|::")
_MAX_NAMES = 40
_MAX_FLAGS = 20

# Names an answer can mention that are not repository code: language
# builtins, standard-library and platform names. Only names that pass the
# symbol shape test (snake_case, camelCase, multi-hump PascalCase) need to be
# listed. Python's builtins (TypeError, isinstance, ...) are added below.
_KNOWN_NAMES = frozenset({
    # JavaScript and web platform
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "setImmediate", "queueMicrotask",
    "requestAnimationFrame", "cancelAnimationFrame", "addEventListener", "removeEventListener", "dispatchEvent",
    "hasOwnProperty", "toString", "valueOf", "toLowerCase", "toUpperCase", "forEach", "indexOf", "lastIndexOf",
    "startsWith", "endsWith", "padStart", "padEnd", "toFixed", "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "structuredClone", "getElementById",
    "querySelector", "querySelectorAll", "createElement", "appendChild", "innerHTML", "textContent",
    "localStorage", "sessionStorage", "readFileSync", "writeFileSync", "existsSync", "readFile", "writeFile",
    "createServer", "createReadStream", "createWriteStream", "nextTick", "fromEntries", "getOwnPropertyNames",
    "defineProperty", "getPrototypeOf", "setPrototypeOf", "isArray", "AbortController", "AbortSignal",
    "ReadableStream", "WritableStream", "TransformStream", "TextDecoder", "TextEncoder", "ArrayBuffer",
    "Uint8Array", "URLSearchParams", "FormData", "XMLHttpRequest", "WeakMap", "WeakSet", "WeakRef", "BigInt",
    "EventEmitter", "useState", "useEffect", "useMemo", "useCallback", "useRef", "useContext", "useReducer",
    "useLayoutEffect", "console.log", "module.exports", "process.env", "instanceof", "typeof",
    # Python standard library and typing
    "exc_info", "get_type_hints", "lru_cache", "cached_property", "update_wrapper", "default_factory",
    "TypeVar", "ParamSpec", "TypedDict", "NamedTuple", "TypeAlias", "TypeGuard", "AsyncIterator",
    "AsyncGenerator", "AsyncIterable", "ContextManager", "AsyncContextManager", "MutableMapping",
    "MutableSequence", "OrderedDict", "DefaultDict", "ChainMap", "SimpleNamespace", "ThreadPoolExecutor",
    "ProcessPoolExecutor", "BytesIO", "StringIO", "TextIOWrapper", "IntEnum", "StrEnum", "IntFlag",
    "PurePath", "PosixPath", "HTTPStatus", "ABCMeta", "ContextVar", "TracebackType", "ModuleType",
    "FunctionType", "MethodType", "MappingProxyType", "NoneType", "TestCase", "MagicMock", "AsyncMock",
    "setUp", "tearDown", "assertEqual", "assertRaises", "assertTrue", "assertFalse", "assertIn",
    "async_generator", "contextmanager", "dataclass", "abstractmethod", "staticmethod", "classmethod",
})
_BUILTIN_NAMES = frozenset(name for name in dir(builtins) if not name.startswith("_"))


def _looks_like_repo_symbol(part: str) -> bool:
    """Whether a name is shaped like a repository code symbol worth looking up.

    Conservative on purpose: snake_case, UPPER_SNAKE, _private, camelCase and
    multi-hump PascalCase names (``send_request``, ``BlueprintApp``) are
    checked; plain words (``request``, ``Flask``), dunders, short names and
    well-known builtins never are, because flagging them would be noise.
    """
    if len(part) < 4 or part in _KNOWN_NAMES or part in _BUILTIN_NAMES:
        return False
    if part.startswith("__") and part.endswith("__"):
        return False
    core = part.strip("_$")
    if len(core) < 3 or not re.search(r"[A-Za-z]", core):
        return False
    if "_" in core:
        return True
    if part.startswith("_"):
        return True
    if not re.search(r"[a-z]", core):
        return False  # an acronym such as HTTP or JSON
    return bool(re.search(r"[A-Z]", core[1:]))


def answer_code_names(answer: str) -> list[dict]:
    """Backticked code names in an answer (outside fenced code) that look like repository symbols.

    Returns ``{text, names}``: the exact backticked text and the symbol-like
    parts to look up. ``self._set_timeout(request)`` checks ``_set_timeout``;
    ``BaseClient.send_request`` checks both parts. File references, shell
    commands and expressions are skipped.
    """
    from api.query_router import parse_reference

    visible = _FENCED.sub(" ", str(answer or ""))
    found: dict[str, list[str]] = {}
    for match in _INLINE_CODE.finditer(visible):
        text = match.group(1).strip()
        if not text or text in found or parse_reference(text):
            continue
        name = _CALL_ARGUMENTS.sub("", text).lstrip("@")
        name = _RECEIVER.sub("", name)
        if not _DOTTED_NAME.fullmatch(name):
            continue
        parts = [part for part in _NAME_SEPARATOR.split(name) if _looks_like_repo_symbol(part)]
        if parts:
            found[text] = list(dict.fromkeys(parts))
    return [{"text": text, "names": names} for text, names in found.items()]


def _word_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


async def _names_missing_from_index(store, repo_id: str, names: list[str]) -> set[str] | None:
    """Names that occur nowhere in the repository's indexed code, or None when the lookup failed.

    One statement of ``NOT EXISTS`` probes: FTS5 narrows the candidate
    chunks (it tokenizes ``send_request`` as the phrase "send request"), and
    a case-sensitive GLOB with identifier boundaries confirms the exact name
    in the content or the file path, so ``http11.send_request_headers`` does
    not count as ``send_request``.
    """
    if not names:
        return set()
    parts, args = [], []
    for name in names:
        boundary = f"*[^A-Za-z0-9_$]{name}[^A-Za-z0-9_$]*"
        parts.append(
            "SELECT ? AS name WHERE NOT EXISTS (SELECT 1 FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND c.repo_id = ? "
            "AND ((' ' || c.content || ' ') GLOB ? OR ('/' || c.file_path || '/') GLOB ?) LIMIT 1)"
        )
        args.extend([name, f'"{name}"', repo_id, boundary, boundary])
    try:
        rows = await store.fetch_all(" UNION ALL ".join(parts), args)
    except Exception as error:
        logger.debug("Identifier lookup unavailable (%s)", type(error).__name__)
        return None
    return {str(row.get("name") or "") for row in rows or [] if isinstance(row, dict)}


async def find_unverified_identifiers(store, repo_id: str | None, answer: str, evidence_chunks: list[dict]) -> list[dict]:
    """Backticked code names in ``answer`` that occur nowhere in the indexed code.

    Names shown in the evidence are known to exist and are not looked up.
    Returns ``[{text, names}]`` with only the missing names; an empty list
    when everything was found or the lookup itself failed (never a guess).
    """
    candidates = answer_code_names(answer)
    if not candidates or not repo_id:
        return []
    evidence = "\n".join(str(chunk.get("content") or "") for chunk in evidence_chunks if not chunk.get("_synthetic"))
    evidence_paths = "\n".join(str(chunk.get("file_path") or "") for chunk in evidence_chunks)
    unknown: list[str] = []
    for item in candidates:
        for name in item["names"]:
            pattern = _word_pattern(name)
            if name not in unknown and not pattern.search(evidence) and not pattern.search(evidence_paths):
                unknown.append(name)
    missing = await _names_missing_from_index(store, repo_id, unknown[:_MAX_NAMES])
    if not missing:
        return []
    flagged = []
    for item in candidates:
        names = [name for name in item["names"] if name in missing]
        if names:
            flagged.append({"text": item["text"], "names": names})
    return flagged[:_MAX_FLAGS]


async def annotate_history_names(store, repo_id: str | None, history: list[dict]) -> None:
    """Note, on each earlier answer, the code names it used that occur nowhere in the indexed code.

    Earlier answers are replayed to the model as conversation context, and a
    name an earlier answer invented (Flask "subclasses" ``BlueprintApp`` and
    ``CliApp``) was repeated as fact when the question was asked again. The
    names of every earlier answer are looked up in one statement and stored
    as ``unverified_names``, which the prompt shows next to that answer.
    """
    answers = [item for item in history if item.get("role") == "assistant" and item.get("content")]
    if not answers or not repo_id:
        return
    names_by_answer = [
        list(dict.fromkeys(name for entry in answer_code_names(item["content"]) for name in entry["names"]))
        for item in answers
    ]
    every_name = list(dict.fromkeys(name for names in names_by_answer for name in names))[:_MAX_NAMES]
    missing = await _names_missing_from_index(store, repo_id, every_name)
    if not missing:
        return
    for item, names in zip(answers, names_by_answer):
        flagged = [name for name in names if name in missing]
        if flagged:
            item["unverified_names"] = flagged[:12]


# ---------------------------------------------------------------------------
# Continuing an answer that stopped at the output limit
# ---------------------------------------------------------------------------

# A full stop after a word (not the "1." of a list item), then the next sentence.
_SENTENCE_END = re.compile(r"(?<=[^\s\d])[.!?:][)\"'*_`]*\s+(?=\S)")


def continuation_base(text: str) -> str:
    """The part of a cut-off answer to keep before its continuation.

    The answer stopped mid-sentence ("...); otherwise it"), so the unfinished
    tail is dropped and rewritten by the continuation: back to the last
    sentence end in the last line, or to the start of that line. A reply
    that already ends at a line break is kept whole.
    """
    value = str(text or "").rstrip(" \t")
    if not value or value.endswith("\n"):
        return value
    line_start = value.rfind("\n") + 1
    line = value[line_start:]
    ends = list(_SENTENCE_END.finditer(line))
    if ends:
        return value[: line_start + ends[-1].end()]
    return value[:line_start]


def _drop_repeated_start(base: str, addition: str) -> str:
    """Remove text the continuation repeated from the end of the answer so far."""
    kept = base.rstrip()
    lines = [line.strip() for line in kept.split("\n") if line.strip()]
    if lines and addition.startswith(lines[-1]) and len(lines[-1]) >= 12:
        return addition[len(lines[-1]):].lstrip()
    for size in range(min(len(kept), len(addition), 400), 19, -1):
        if kept.endswith(addition[:size]):
            return addition[size:].lstrip()
    return addition


def join_continuation(base: str, addition: str) -> str:
    """Append a continuation so the Markdown reads as one answer.

    The continuation starts at a new sentence or line. After a paragraph or
    list item a new block (a heading, paragraph or list) needs a blank line;
    inside an open code fence the text continues as written.
    """
    addition = _drop_repeated_start(base, str(addition or "").lstrip())
    if not addition:
        return base
    if not base:
        return addition
    if base.count("```") % 2:
        return base + addition
    if not base.endswith("\n"):
        separator = "" if base[-1].isspace() else " "
        return base + separator + addition
    previous = base.rstrip("\n").rsplit("\n", 1)[-1].lstrip()
    continues_list = bool(re.match(r"(?:[-*+]|\d+[.)])\s", addition)) and bool(re.match(r"(?:[-*+]|\d+[.)])\s", previous))
    continues_table = addition.startswith("|") and previous.startswith("|")
    if base.endswith("\n\n") or continues_list or continues_table:
        return base + addition
    return base + "\n" + addition


# ---------------------------------------------------------------------------
# Naming model failures
# ---------------------------------------------------------------------------

_PROFILE_NAMES = {"fast": "Fast", "detailed": "Detailed", "code": "Code"}


def _failure_reason(error: Exception | None) -> str:
    if type(error).__name__ == "ModelConfigurationError":
        return "not_configured"
    return getattr(error, "reason", None) or "invalid"


def _failure_sentence(error: Exception | None, model: str) -> str:
    reason = _failure_reason(error)
    status = getattr(error, "status", None)
    if reason == "timeout":
        return f"the {model} model did not respond in time"
    if reason == "connection":
        return "the server could not connect to NVIDIA"
    if reason == "rate_limited":
        return "NVIDIA is limiting how many requests this server can make right now (HTTP 429)"
    if reason == "unavailable":
        return f"NVIDIA's {model} model endpoint is busy or down (HTTP {status})"
    if reason == "http_error":
        return f"NVIDIA rejected the request to the {model} model (HTTP {status})"
    if reason == "planning":
        return f"the {model} model returned its reasoning instead of an answer"
    if reason == "length":
        return f"the {model} model used its whole output budget before it wrote an answer"
    if reason == "empty":
        return f"the {model} model returned an empty reply"
    if reason == "interrupted":
        return f"the {model} model's answer stream was interrupted"
    if reason == "not_configured":
        return "no NVIDIA API key is configured on this server"
    return f"the {model} model returned a reply that could not be used"


def describe_answer_failure(error: Exception, model_profile: str) -> dict:
    """Why no (complete) answer was written, for the chat: ``{reason, message, model_profile}``.

    When the other chat model was tried after the requested one failed
    (``error.previous``), both failures are named.
    """
    from agent.agent import alternate_profile

    previous = getattr(error, "previous", None)
    requested = _PROFILE_NAMES.get(model_profile, "answer")
    if previous is not None:
        other = _PROFILE_NAMES.get(alternate_profile(model_profile), "other")
        if _failure_reason(previous) == _failure_reason(error) == "timeout":
            message = f"neither the {requested} nor the {other} model responded in time."
        else:
            message = f"{_failure_sentence(previous, requested)}, and {_failure_sentence(error, other)}."
    else:
        message = f"{_failure_sentence(error, requested)}."
    return {
        "reason": _failure_reason(previous if previous is not None else error),
        "message": message[0].upper() + message[1:],
        "model_profile": model_profile,
    }


def describe_model_switch(marker, requested_profile: str) -> dict:
    """A notice that the other chat model wrote the answer, and why."""
    requested = _PROFILE_NAMES.get(requested_profile, "requested")
    used = _PROFILE_NAMES.get(getattr(marker, "profile", ""), "other")
    error = getattr(marker, "error", None)
    sentence = _failure_sentence(error, requested)
    return {
        "kind": "model_switch",
        "reason": _failure_reason(error),
        "model_profile": getattr(marker, "profile", None),
        "message": f"{sentence[0].upper() + sentence[1:]}, so the {used} model wrote this answer.",
    }


# ---------------------------------------------------------------------------
# Storing the checks with the message
# ---------------------------------------------------------------------------

ANSWER_CHECK_TOOL = "answer_check"
_STORED_FIELDS = ("answer_references", "unverified_identifiers", "answer_failure", "answer_notice")


def answer_check_entry(**fields) -> dict | None:
    """The ``tool_calls`` entry that keeps an answer's checks with the stored message, or None."""
    data = {key: fields.get(key) for key in _STORED_FIELDS if fields.get(key)}
    if not data:
        return None
    references = data.get("answer_references") or []
    summary = f"{sum(1 for item in references if item.get('valid'))} of {len(references)} file references verified"
    if data.get("unverified_identifiers"):
        summary += f"; {len(data['unverified_identifiers'])} code names not found in the indexed code"
    return {"tool": ANSWER_CHECK_TOOL, "input": {}, "result_summary": summary, "data": data}


def restore_answer_check(message: dict) -> dict:
    """Move a stored answer check out of ``tool_calls`` onto the message, as live answers carry it."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return message
    entry = next((call for call in calls if isinstance(call, dict) and call.get("tool") == ANSWER_CHECK_TOOL), None)
    if entry is None:
        return message
    message["tool_calls"] = [call for call in calls if call is not entry]
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    for key in _STORED_FIELDS:
        if data.get(key) is not None:
            message[key] = data[key]
    return message
