"""Validation and authorization primitives for the code-editing workflow.

The model never receives permission to write to GitHub. It can only propose a
small list of exact text replacements. The replacements are checked against
the retrieved evidence here and applied to the current GitHub revision in the
browser after the user reviews the result. A separate signed ticket gates the
file and PR endpoints so ordinary chat modes cannot invoke the write flow.
"""

from __future__ import annotations

import base64
import bisect
import difflib
import hashlib
import hmac
import json
import re
import time
from collections import Counter
from pathlib import PurePosixPath
from typing import Any


class CodeEditValidationError(ValueError):
    """Raised when a model proposal is not safe to present as an editable patch."""


class EditingAuthorizationError(ValueError):
    """Raised when a file or PR request lacks a valid editing-mode ticket."""


_CODE_EDIT_TAG = re.compile(r"<code_edit>\s*(.*?)\s*</code_edit>", re.IGNORECASE | re.DOTALL)
_CONTEXT_FILE = re.compile(r"(?m)^File:\s*([^\s]+)\s+\(L\d+-L\d+\)")
_MAX_CHANGES = 20
_MAX_FILES = 8
_MAX_TOTAL_CHANGES = 40
_MAX_SUMMARY_LENGTH = 800
_MAX_REASON_LENGTH = 500
_MAX_VALIDATION_ITEMS = 12
_MAX_VALIDATION_LENGTH = 300


def _normalise_path(value: str) -> str:
    candidate = str(value or "").strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        raise CodeEditValidationError("The proposed file path is invalid.")
    path = PurePosixPath(candidate)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CodeEditValidationError("The proposed file path is invalid.")
    return str(path)


def _extract_object(raw: str) -> dict[str, Any]:
    """Extract one JSON object without trusting surrounding model prose."""
    candidate = str(raw or "").strip()
    tagged = _CODE_EDIT_TAG.search(candidate)
    if tagged:
        candidate = tagged.group(1).strip()

    decoder = json.JSONDecoder()
    # Models occasionally wrap the object in a Markdown fence or one sentence
    # of explanation. Try each opening brace, but only accept an object that
    # subsequently passes the strict schema checks below.
    for offset, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise CodeEditValidationError("The model did not return a valid structured code edit.")


def parse_code_edit(
    raw: str,
    *,
    allowed_files: set[str] | list[str],
    source_context: str,
    max_change_bytes: int,
) -> dict[str, Any]:
    """Validate a minimal exact-match patch against retrieved source evidence.

    The output deliberately uses search/replace pairs rather than a complete
    replacement file. This prevents a short retrieved chunk from truncating a
    larger file and lets the UI apply the proposal to the latest GitHub blob.
    """
    payload = _extract_object(raw)
    normalised_allowed = {_normalise_path(path) for path in allowed_files}
    context = str(source_context or "")

    # The legacy one-file shape remains accepted for compatibility. New issue
    # fixes use ``files`` so a bounded multi-file change can be reviewed and
    # committed atomically rather than silently producing a partial fix.
    raw_files = payload.get("files")
    if raw_files is None:
        raw_files = [{
            "file_path": payload.get("file_path"),
            "summary": payload.get("summary"),
            "changes": payload.get("changes"),
            "validation": payload.get("validation"),
        }]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > _MAX_FILES:
        raise CodeEditValidationError(f"The code edit must contain between one and {_MAX_FILES} files.")

    files: list[dict[str, Any]] = []
    total_bytes = 0
    total_changes = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise CodeEditValidationError("Every edited file must be an object.")
        try:
            file_path = _normalise_path(raw_file.get("file_path"))
        except (AttributeError, TypeError) as error:
            raise CodeEditValidationError("Each edited file must name an indexed file.") from error
        if file_path not in normalised_allowed:
            suffix_matches = [path for path in normalised_allowed if path.endswith(f"/{file_path}")]
            if len(suffix_matches) == 1:
                file_path = suffix_matches[0]
        if file_path not in normalised_allowed:
            raise CodeEditValidationError("A proposed file was not part of the targeted indexed evidence.")
        if any(item["file_path"] == file_path for item in files):
            raise CodeEditValidationError("The code edit lists the same file more than once.")

        summary = str(raw_file.get("summary") or payload.get("summary") or "").strip()
        if not summary or len(summary) > _MAX_SUMMARY_LENGTH:
            raise CodeEditValidationError("Each edited file needs a short summary.")
        raw_changes = raw_file.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes or len(raw_changes) > _MAX_CHANGES:
            raise CodeEditValidationError(f"Each edited file must contain between one and {_MAX_CHANGES} changes.")

        target_context = _file_context(context, file_path)
        if not target_context:
            raise CodeEditValidationError("A selected file has no retrieved source evidence.")
        changes: list[dict[str, str]] = []
        seen_old: set[str] = set()
        for item in raw_changes:
            if not isinstance(item, dict):
                raise CodeEditValidationError("Every code edit change must be an object.")
            old = item.get("old")
            new = item.get("new")
            if not isinstance(old, str) or not old.strip() or not isinstance(new, str):
                raise CodeEditValidationError("Each code edit needs non-empty old text and string replacement text.")
            if "\x00" in old or "\x00" in new or old in seen_old:
                raise CodeEditValidationError("The code edit contains invalid or duplicate search text.")
            # Exact source grounding: the model cannot invent a search hunk
            # that was not present in this file's bounded context.
            if old not in target_context:
                raise CodeEditValidationError("A proposed search hunk was not present in the indexed evidence.")
            if old == new:
                raise CodeEditValidationError("A code edit must change the matched text.")
            if len(old.encode("utf-8")) > max_change_bytes or len(new.encode("utf-8")) > max_change_bytes:
                raise CodeEditValidationError("A single code edit is too large to review safely.")
            total_bytes += len(new.encode("utf-8"))
            total_changes += 1
            if total_changes > _MAX_TOTAL_CHANGES:
                raise CodeEditValidationError(f"The code edit must contain at most {_MAX_TOTAL_CHANGES} changes.")
            reason = str(item.get("reason") or "").strip()[:_MAX_REASON_LENGTH]
            changes.append({"old": old, "new": new, "reason": reason})
            seen_old.add(old)
        files.append({
            "file_path": file_path,
            "summary": summary,
            "changes": changes,
            "validation": _validation_steps(raw_file.get("validation") or payload.get("validation") or []),
        })
    if total_bytes > max(1, int(max_change_bytes)):
        raise CodeEditValidationError("The total proposed change is too large to review safely.")

    if len(files) == 1:
        # Preserve the response contract consumed by older clients/tests while
        # exposing ``files`` for clients that understand multi-file reviews.
        return {**files[0], "files": files}
    return {
        "files": files,
        "file_path": files[0]["file_path"],
        "summary": f"{len(files)} files: " + "; ".join(file["summary"] for file in files)[:_MAX_SUMMARY_LENGTH],
        "changes": [],
        "validation": [item for file in files for item in file["validation"]][:_MAX_VALIDATION_ITEMS],
    }


def _validation_steps(raw_validation: Any) -> list[str]:
    if not isinstance(raw_validation, list):
        raise CodeEditValidationError("Validation steps must be a list.")
    return [
        str(item).strip()[:_MAX_VALIDATION_LENGTH]
        for item in raw_validation[:_MAX_VALIDATION_ITEMS]
        if str(item).strip()
    ]


def edit_file_paths(edit: dict[str, Any]) -> list[str]:
    """Return normalized target paths from either edit response shape."""
    raw_files = edit.get("files") if isinstance(edit, dict) else None
    if isinstance(raw_files, list):
        paths = [str(item.get("file_path") or "").strip() for item in raw_files if isinstance(item, dict)]
    else:
        paths = [str(edit.get("file_path") or "").strip()] if isinstance(edit, dict) else []
    result: list[str] = []
    for path in paths:
        if not path:
            continue
        normalized = _normalise_path(path)
        if normalized not in result:
            result.append(normalized)
    return result


def context_file_paths(source_context: str) -> set[str]:
    """Extract only the file labels emitted by the server's context builder."""
    return {match.group(1).strip() for match in _CONTEXT_FILE.finditer(str(source_context or ""))}


def _file_context(source_context: str, file_path: str) -> str:
    """Return only the retrieved source sections belonging to one file.

    Exact text grounding must be file-scoped. A common hunk can legitimately
    occur in several files, and checking the whole multi-file prompt would let
    the model name one file while borrowing evidence from another. The
    no-header fallback keeps this helper compatible with direct unit callers;
    production context is always emitted with ``File:`` headers.
    """
    context = str(source_context or "")
    headers = list(_CONTEXT_FILE.finditer(context))
    if not headers:
        return context
    sections: list[str] = []
    for index, header in enumerate(headers):
        try:
            path = _normalise_path(header.group(1).strip())
        except CodeEditValidationError:
            continue
        if path != file_path:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(context)
        sections.append(context[header.end():end])
    return "\n".join(sections)


def format_code_edit_response(edit: dict[str, Any]) -> str:
    """Render a safe, concise explanation while keeping patch data structured."""
    files = edit.get("files") if isinstance(edit.get("files"), list) else [edit]
    lines = [
        "## Proposed change",
        "",
        f"**{len(files)} file{'s' if len(files) != 1 else ''}**: "
        + "; ".join(f"{file['file_path']} — {file['summary']}" for file in files),
        "",
        "The generated patch contains exact replacements from the indexed evidence. Review every current file before creating a pull request.",
        "",
        "## Planned edits",
    ]
    index = 1
    for file in files:
        for change in file.get("changes") or []:
            reason = f" — {change['reason']}" if change.get("reason") else ""
            lines.append(f"{index}. In **{file['file_path']}**, replace the matched source text{reason}.")
            index += 1
    validation = [item for file in files for item in file.get("validation", [])]
    if validation:
        lines.extend(["", "## Validation to run"])
        lines.extend(f"- {item}" for item in validation[:_MAX_VALIDATION_ITEMS])
    return "\n".join(lines)


def format_invalid_code_edit_response() -> str:
    """Do not expose malformed model output or present an unsafe push action."""
    return (
        "## Code review\n\n"
        "I could not produce a safe patch from the indexed evidence. Ask for one specific file and a focused change; "
        "the editor will only appear when every proposed replacement exactly matches the retrieved source."
    )


def _ticket_key(secret: str) -> bytes:
    value = str(secret or "").strip()
    if len(value) < 16:
        raise EditingAuthorizationError("Code editing is not configured on the server.")
    return value.encode("utf-8")


def create_edit_ticket(secret: str, *, user_id: str, repo_name: str, file_path: str | list[str], ttl_seconds: int) -> str:
    """Create an opaque, short-lived ticket scoped to one user/repository/file set."""
    key = _ticket_key(secret)
    paths = edit_file_paths({"files": [{"file_path": path} for path in file_path]}) if isinstance(file_path, list) else edit_file_paths({"file_path": file_path})
    if not paths:
        raise EditingAuthorizationError("Code editing needs at least one target file.")
    payload = {
        "v": 1,
        "u": str(user_id),
        "r": str(repo_name),
        "p": paths[0] if len(paths) == 1 else paths,
        "e": int(time.time()) + max(60, int(ttl_seconds)),
        "n": hashlib.sha256(f"{user_id}:{repo_name}:{file_path}:{time.time_ns()}".encode()).hexdigest()[:16],
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"v1.{body}.{signature}"


def verify_edit_ticket(secret: str, ticket: str, *, user_id: str, repo_name: str, file_path: str) -> dict[str, Any]:
    """Verify signature, expiry, and exact scope using constant-time comparison."""
    key = _ticket_key(secret)
    parts = str(ticket or "").split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise EditingAuthorizationError("Open this change from Code editing mode before continuing.")
    body, signature = parts[1], parts[2]
    expected = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise EditingAuthorizationError("The editing session is invalid. Start the review again.")
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise EditingAuthorizationError("The editing session is invalid. Start the review again.") from error
    try:
        if not isinstance(payload, dict) or "e" not in payload:
            raise EditingAuthorizationError("The editing session is invalid. Start the review again.")
        if int(payload.get("e", 0)) <= int(time.time()):
            raise EditingAuthorizationError("The editing session expired. Generate the change again.")
        if str(payload.get("u")) != str(user_id) or str(payload.get("r")) != str(repo_name):
            raise EditingAuthorizationError("The editing session is not valid for this repository.")
        raw_paths = payload.get("p")
        if isinstance(raw_paths, list):
            ticket_paths = edit_file_paths({"files": [{"file_path": path} for path in raw_paths]})
        else:
            ticket_paths = edit_file_paths({"file_path": raw_paths})
        requested_path = _normalise_path(file_path)
        if requested_path not in ticket_paths:
            raise EditingAuthorizationError("The editing session is not valid for this file.")
    except EditingAuthorizationError:
        # The specific expiry/scope messages are subclasses of ValueError;
        # they must not be masked by the generic malformed-payload message.
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise EditingAuthorizationError("The editing session is invalid. Start the review again.") from error
    return payload


# ---------------------------------------------------------------------------
# Server-side patch application
#
# The browser applies reviewed hunks for the preview. These helpers let the
# server re-apply the same hunks to the blob it fetched from GitHub instead of
# trusting a client-supplied "final" file, so a stale or tampered preview
# cannot be committed. They follow the browser's exact-match rules with two
# deliberate differences (overlapping repeats of a hunk count as ambiguous,
# and untouched lines keep their original line endings) and add a narrowly
# scoped tolerance for invisible trailing whitespace.
# ---------------------------------------------------------------------------

# Characters treated as invisible trailing whitespace before a line break:
# everything ``str.rstrip()`` strips except the line feed itself, so the
# tolerance matches the plain "rstrip every line" reading of the rule (a
# trailing NBSP or a stray ``\r`` left by mixed endings is just as invisible
# in review as a space). Hunk segments never contain ``\n``, so a bare
# ``rstrip()`` on them strips exactly this set.
_TRAILING_WHITESPACE_CLASS = r"[^\S\n]"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# Paths are echoed into diff headers and commit metadata, where a newline or
# other control character could forge an extra line.
_PATH_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DIFF_CONTEXT_LINES = 3
# Equal-line pairs one longest-match search may visit (about 0.15 s per
# million in CPython). Below this the full, spec-exact comparison runs.
_DIFF_PAIR_BUDGET = 1_000_000
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_INVALID_PATH_MESSAGE = "The proposed file path is invalid."


def git_blob_sha(data: bytes) -> str:
    """Return the blob SHA-1 that Git and GitHub's contents API report for ``data``.

    GitHub rejects a contents update unless the caller names the blob it is
    replacing, so computing the SHA locally lets the server prove the file it
    patched is the file it is about to overwrite.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        # ``bytes(5)`` would silently hash five NUL bytes and ``str`` has no
        # canonical encoding, so callers must pass the exact blob bytes.
        raise TypeError("git_blob_sha expects the raw file bytes.")
    payload = bytes(data)
    header = b"blob " + str(len(payload)).encode("ascii") + b"\0"
    # SHA-1 is Git's content address here, not a security primitive; the flag
    # keeps the call working on FIPS-restricted interpreters.
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def locate_hunk(content: str, old: str) -> list[tuple[int, int]]:
    """Return every non-overlapping exact ``(start, end)`` span of ``old``.

    Both arguments must already be LF-normalised. An empty search string
    matches between every character, which would make "exactly one match"
    meaningless, so it yields no spans instead.
    """
    if not old:
        return []
    spans: list[tuple[int, int]] = []
    start = content.find(old)
    while start != -1:
        end = start + len(old)
        spans.append((start, end))
        start = content.find(old, end)
    return spans


# Bounds the per-location scans used only to word ambiguity errors.
_OVERLAP_SCAN_LIMIT = 1000


def _occurrence_count(content: str, old: str) -> int:
    """Count every start offset of ``old``, including overlapping ones.

    ``locate_hunk`` skips an occurrence that overlaps an earlier one, yet
    ``"}\\n}\\n"`` inside ``"}\\n}\\n}\\n"`` can be applied at two offsets with
    two different results. Uniqueness has to count both or the server would
    silently pick one of them.
    """
    count = 0
    index = content.find(old)
    while index != -1:
        count += 1
        if count >= _OVERLAP_SCAN_LIMIT:
            # A self-overlapping hunk inside a run of itself (``"a" * 1000`` in
            # ``"a" * 2_000_000``) costs O(len(old)) per step, so an exhaustive
            # overlapping scan is a hostile-input DoS. Past the cap the exact
            # figure is irrelevant to the user; the C-level non-overlapping
            # count is linear and still a truthful lower bound.
            return max(count, content.count(old))
        index = content.find(old, index + 1)
    return count


def _lf_view(content: str) -> tuple[str, list[int]]:
    """Return LF-normalised text plus the normalised offset of each former CRLF.

    Matching happens on the LF view, but splicing happens on the original
    string so untouched lines keep their exact bytes. A mixed-ending file
    therefore never turns into a whole-file line-ending rewrite.
    """
    if "\r\n" not in content:
        return content, []
    parts = content.split("\r\n")
    positions: list[int] = []
    offset = 0
    for part in parts[:-1]:
        offset += len(part)
        positions.append(offset)
        offset += 1
    return "\n".join(parts), positions


def _original_offset(index: int, crlf_positions: list[int]) -> int:
    """Map an LF-view offset back to the original string.

    Every CRLF that starts before ``index`` contributed one extra character.
    An offset that sits on a former CRLF maps to its ``\\r``, so a span
    boundary can never split a CRLF pair.
    """
    return index + bisect.bisect_left(crlf_positions, index)


def _tolerant_pattern(old: str) -> re.Pattern[str] | None:
    """Compile ``old`` into a regex that ignores trailing whitespace per line.

    Only whitespace directly before a line break may differ; indentation and
    interior whitespace stay exact. Spans are chosen so they cover the same
    logical text an exact match would, which keeps a replacement from
    swallowing whitespace that belongs to a neighbouring line.
    """
    if not old.strip():
        # A whitespace-only hunk would match nearly every line once trailing
        # whitespace is ignored, so it can only ever be applied exactly.
        return None
    ws = _TRAILING_WHITESPACE_CLASS
    segments = old.split("\n")
    last = len(segments) - 1
    parts: list[str] = []
    for index, segment in enumerate(segments):
        stripped = segment.rstrip()
        if index < last:
            if index == 0 and not segment:
                # The hunk starts at a line break: claim none of the previous
                # line's trailing whitespace, exactly like an exact match.
                parts.append("\n")
            elif index == 0 and not stripped:
                # The hunk starts with that line's trailing whitespace. Anchor
                # at the start of the whitespace run so one location is never
                # counted once per whitespace character.
                parts.append(f"(?<!{ws}){ws}*\n")
            else:
                parts.append(f"{re.escape(stripped)}{ws}*\n")
        elif segment and stripped == segment:
            parts.append(re.escape(segment))
        elif segment:
            # The hunk ends without a newline but with whitespace. At a line
            # end that whitespace is trailing and may differ; mid-line it is
            # significant (``"call(a, "``) and must match exactly, otherwise
            # the replacement would double the following space.
            parts.append(f"(?:{re.escape(stripped)}{ws}*(?=\n|\\Z)|{re.escape(segment)})")
    return re.compile("".join(parts))


def _tolerant_spans(content: str, old: str) -> list[tuple[int, int]]:
    """Return every distinct trailing-whitespace-tolerant match of ``old``."""
    pattern = _tolerant_pattern(old)
    if pattern is None:
        return []
    spans: list[tuple[int, int]] = []
    match = pattern.search(content)
    while match is not None:
        spans.append(match.span())
        if len(spans) >= _OVERLAP_SCAN_LIMIT:
            # Any count above one is already an ambiguity error; scanning on
            # through a huge repetitive file would only burn CPU.
            break
        # Restart one character later rather than after the match so that
        # overlapping locations count towards ambiguity as they do for exact
        # matches.
        match = pattern.search(content, match.start() + 1)
    return spans


def _ambiguous_hunk_message(hunk_number: int, count: int) -> str:
    return f"Hunk {hunk_number} matches {count} locations; include more surrounding lines."


def _unique_hunk_span(content: str, old: str, hunk_number: int) -> tuple[int, int]:
    """Locate the single span ``old`` refers to, or explain why it cannot."""
    exact = locate_hunk(content, old)
    if exact:
        count = _occurrence_count(content, old)
        if count == 1:
            return exact[0]
        raise CodeEditValidationError(_ambiguous_hunk_message(hunk_number, count))
    # Only when the exact text is absent: editors and models routinely drop or
    # add invisible trailing whitespace, and refusing those hunks would make
    # otherwise correct edits fail for a reason the user cannot see.
    tolerant = _tolerant_spans(content, old)
    if len(tolerant) == 1:
        return tolerant[0]
    if tolerant:
        raise CodeEditValidationError(_ambiguous_hunk_message(hunk_number, len(tolerant)))
    raise CodeEditValidationError(f"Hunk {hunk_number} was not found in the current file.")


def _hunk_texts(change: Any, hunk_number: int) -> tuple[str, str]:
    """Validate one change and return its LF-normalised ``(old, new)`` pair."""
    if not isinstance(change, dict):
        raise CodeEditValidationError(f"Hunk {hunk_number} must be an object with old and new text.")
    old = change.get("old")
    new = change.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise CodeEditValidationError(f"Hunk {hunk_number} needs non-empty old text and string replacement text.")
    # Normalise even for LF files: a CRLF hunk can then still match, and its
    # replacement adopts the file's own line endings instead of mixing them.
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == new:
        raise CodeEditValidationError(f"Hunk {hunk_number} does not change the matched text.")
    return old, new


def apply_hunks(content: str, changes: list[dict[str, Any]]) -> str:
    """Apply exact search/replace hunks to the current file and return the result.

    Every hunk is located in the original file before anything is replaced,
    so one replacement can never create or destroy the text another hunk
    targets. Untouched text keeps its original bytes, including line endings
    and the presence or absence of a final newline; replacement text uses
    CRLF when the file contains CRLF.
    """
    if not isinstance(content, str):
        raise TypeError("apply_hunks expects the current file content as text.")
    if not isinstance(changes, (list, tuple)) or not changes:
        raise CodeEditValidationError("The patch must contain at least one change.")

    uses_crlf = "\r\n" in content
    normalised, crlf_positions = _lf_view(content)
    located: list[tuple[int, int, str, int]] = []
    for hunk_number, change in enumerate(changes, start=1):
        old, new = _hunk_texts(change, hunk_number)
        start, end = _unique_hunk_span(normalised, old, hunk_number)
        located.append((start, end, new, hunk_number))

    located.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(located, located[1:]):
        if current[0] < previous[1]:
            first, second = sorted((previous[3], current[3]))
            raise CodeEditValidationError(
                f"Hunks {first} and {second} overlap; combine them into one change."
            )

    # Splice from the end of the file backwards so every earlier offset into
    # the original string stays valid while pieces are collected.
    pieces: list[str] = []
    cursor = len(content)
    for start, end, new, _ in reversed(located):
        original_start = _original_offset(start, crlf_positions)
        original_end = _original_offset(end, crlf_positions)
        pieces.append(content[original_end:cursor])
        pieces.append(new.replace("\n", "\r\n") if uses_crlf else new)
        cursor = original_start
    pieces.append(content[:cursor])
    return "".join(reversed(pieces))


def _diff_lines(text: str) -> list[str]:
    """Split text into LF-normalised lines that keep their ``\\n`` terminator.

    Keeping the terminator makes a final line without a newline differ from
    the same line with one, which is how Git counts and displays that change.
    ``str.splitlines`` is avoided because it also splits on form feeds and
    Unicode separators that Git treats as ordinary characters.
    """
    normalised = (text or "").replace("\r\n", "\n")
    if not normalised:
        return []
    raw_lines = normalised.split("\n")
    lines = [line + "\n" for line in raw_lines[:-1]]
    if raw_lines[-1]:
        lines.append(raw_lines[-1])
    return lines


def _equal_line_pairs(old_lines: list[str], new_lines: list[str]) -> int:
    """Count ``(i, j)`` pairs with equal lines: the work of one longest-match search.

    ``SequenceMatcher.find_longest_match`` visits every such pair, so this
    linear-time count predicts its cost before any quadratic work starts.
    """
    new_counts = Counter(new_lines)
    return sum(new_counts.get(line, 0) for line in old_lines)


def _line_opcodes(old_lines: list[str], new_lines: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Return line opcodes from ``SequenceMatcher(autojunk=False)``.

    ``autojunk=False`` keeps blank lines and braces matchable in files longer
    than 200 lines. Whenever the comparison is affordable it runs over every
    line exactly as specified, so results equal plain SequenceMatcher (and
    ``difflib.unified_diff`` for files under 200 lines). Trimming the shared
    head and tail first is not equivalent: it changes the alignment on
    repetitive input and can even produce a larger diff.

    Without autojunk the search is quadratic in repeated lines, and a 2 MB
    lockfile could block the event loop for seconds. Beyond the budget the
    identical head and tail are trimmed (a reviewed edit usually touches a
    small region), and if the remaining middle is still too repetitive its
    popular lines are junked so the cost stays bounded.
    """
    if _equal_line_pairs(old_lines, new_lines) <= _DIFF_PAIR_BUDGET:
        return difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()

    limit = min(len(old_lines), len(new_lines))
    prefix = 0
    while prefix < limit and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old_lines[-1 - suffix] == new_lines[-1 - suffix]:
        suffix += 1
    old_end = len(old_lines) - suffix
    new_end = len(new_lines) - suffix

    opcodes: list[tuple[str, int, int, int, int]] = []
    if prefix:
        opcodes.append(("equal", 0, prefix, 0, prefix))
    if prefix < old_end or prefix < new_end:
        old_middle = old_lines[prefix:old_end]
        new_middle = new_lines[prefix:new_end]
        # Edits at both far ends leave the whole repetitive body in the
        # middle; junking popular lines there trades exact alignment of the
        # repeats for a bounded running time.
        autojunk = _equal_line_pairs(old_middle, new_middle) > _DIFF_PAIR_BUDGET
        matcher = difflib.SequenceMatcher(None, old_middle, new_middle, autojunk=autojunk)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            opcodes.append((tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix))
    if suffix:
        opcodes.append(("equal", old_end, len(old_lines), new_end, len(new_lines)))
    return opcodes


def _grouped_opcodes(
    opcodes: list[tuple[str, int, int, int, int]], context: int
) -> list[list[tuple[str, int, int, int, int]]]:
    """Group opcodes into hunks with ``context`` lines around each change.

    This mirrors ``difflib.SequenceMatcher.get_grouped_opcodes`` line for line
    so hunk boundaries and context match ``difflib.unified_diff`` for the same
    opcodes; it exists only because that method cannot accept precomputed
    opcodes.
    """
    codes = list(opcodes) or [("equal", 0, 1, 0, 1)]
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))

    groups: list[list[tuple[str, int, int, int, int]]] = []
    group: list[tuple[str, int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > context * 2:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def _unified_range(start: int, stop: int) -> str:
    """Format a hunk range exactly as ``difflib.unified_diff`` does."""
    beginning = start + 1
    length = stop - start
    if length == 1:
        return str(beginning)
    if not length:
        # An empty range is reported at the line just before it.
        beginning -= 1
    return f"{beginning},{length}"


def _append_diff_lines(output: list[str], prefix: str, lines: list[str]) -> None:
    for line in lines:
        if line.endswith("\n"):
            output.append(prefix + line[:-1])
        else:
            # Only a file's last line can lack a newline; Git's marker keeps
            # that difference visible instead of rendering an empty diff.
            output.append(prefix + line)
            output.append(_NO_NEWLINE_MARKER)


def line_change_counts(before: str, after: str) -> tuple[int, int]:
    """Return ``(additions, deletions)`` by line between two file versions.

    Line endings are normalised so a CRLF file and its LF view count as
    identical, while adding or removing the final newline counts as one
    changed line, as it does in Git. The counts always equal the ``+`` and
    ``-`` lines rendered by ``unified_diff_text`` for the same inputs.
    """
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in _line_opcodes(_diff_lines(before), _diff_lines(after)):
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return additions, deletions


def unified_diff_text(before: str, after: str, path: str, *, is_new: bool = False) -> str:
    """Render a Git-style unified diff with three lines of context.

    The format is ``difflib.unified_diff`` with ``lineterm=""`` joined by
    ``"\\n"``, plus Git's "No newline at end of file" marker. Lines are matched
    with the same ``autojunk=False`` comparison as ``line_change_counts`` (see
    ``_line_opcodes``), so the ``+``/``-`` lines always agree with its counts.
    For files under 200 lines that end with a newline the output is
    byte-identical to ``difflib.unified_diff``; longer files can differ only
    because difflib cannot turn autojunk off.

    Returns ``""`` when the LF-normalised versions are identical. A new file
    is always diffed from empty content so the ``/dev/null`` header and the
    hunk body can never contradict each other.
    """
    old_lines = [] if is_new else _diff_lines(before)
    new_lines = _diff_lines(after)
    if old_lines == new_lines:
        return ""
    output = [
        "--- /dev/null" if is_new else f"--- a/{path}",
        f"+++ b/{path}",
    ]
    for group in _grouped_opcodes(_line_opcodes(old_lines, new_lines), _DIFF_CONTEXT_LINES):
        first, last = group[0], group[-1]
        output.append(
            f"@@ -{_unified_range(first[1], last[2])} +{_unified_range(first[3], last[4])} @@"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                _append_diff_lines(output, " ", old_lines[i1:i2])
                continue
            if tag in ("replace", "delete"):
                _append_diff_lines(output, "-", old_lines[i1:i2])
            if tag in ("replace", "insert"):
                _append_diff_lines(output, "+", new_lines[j1:j2])
    return "\n".join(output)


def normalise_repository_path(value: str) -> str:
    """Return a canonical repository-relative POSIX path or raise.

    This is the public form of ``_normalise_path`` for the write endpoints.
    It additionally rejects Windows drive paths, which ``PurePosixPath``
    would accept as an ordinary ``"C:"`` directory, control characters, and
    ``"."``, which names the repository root rather than a file.
    """
    if not isinstance(value, str):
        raise CodeEditValidationError(_INVALID_PATH_MESSAGE)
    candidate = value.strip().replace("\\", "/")
    if _PATH_CONTROL_CHARACTERS.search(candidate):
        raise CodeEditValidationError(_INVALID_PATH_MESSAGE)
    path = _normalise_path(candidate)
    # The drive check runs on the canonical form: normalisation drops a
    # leading "./", so checking only the raw input would let "./C:/x" (or
    # ".\\C:\\x") come back as the drive path "C:/x".
    if path == "." or _WINDOWS_DRIVE.match(path):
        raise CodeEditValidationError(_INVALID_PATH_MESSAGE)
    return path
