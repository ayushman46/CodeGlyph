"""Small NVIDIA NIM client boundary used by all model-backed query work."""

import asyncio
import re
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from config import ModelConfigurationError, nvidia_rate_limiter, require_nvidia_api_key, settings


class LLMProviderError(RuntimeError):
    """A safe, user-facing failure from the configured model provider."""


_THINKING_BLOCK = re.compile(r"<(?:think|analysis|reasoning)>.*?</(?:think|analysis|reasoning)>", re.IGNORECASE | re.DOTALL)
_CODE_EDIT_BLOCK = re.compile(r"<code_edit>\s*.*?</code_edit>", re.IGNORECASE | re.DOTALL)
_FINAL_LABEL = re.compile(
    r"(?ims)^\s*(?:final(?:\s+(?:answer|response))?|assistant\s+response)\s*:\s*(.+)\Z"
)
_INTERNAL_PLANNING_PREFIX = re.compile(
    r"^\s*(?:analysis\s*:|reasoning\s*:|the user (?:said|asked|is asking)|"
    r"i (?:should|need to|will)|we (?:should|need to|will)|no need to)\b",
    re.IGNORECASE,
)

_async_client: AsyncOpenAI | None = None
_async_client_loop: asyncio.AbstractEventLoop | None = None
_async_client_lock = threading.Lock()


def get_async_client() -> AsyncOpenAI:
    """Reuse one HTTP client per event loop so keep-alive connections survive requests."""
    global _async_client, _async_client_loop
    loop = asyncio.get_running_loop()
    with _async_client_lock:
        if _async_client is None or _async_client_loop is not loop:
            _async_client = AsyncOpenAI(
                base_url=settings.nvidia_base_url,
                api_key=require_nvidia_api_key(),
                timeout=settings.nvidia_timeout_seconds,
                max_retries=0,
            )
            _async_client_loop = loop
        return _async_client


async def close_async_client() -> None:
    """Close the process-scoped provider client during application shutdown."""
    global _async_client, _async_client_loop
    with _async_client_lock:
        client = _async_client
        _async_client = None
        _async_client_loop = None
    if client is not None:
        await client.close()


def final_code_edit_block(content: str) -> str | None:
    """Return the final <code_edit> block, ignoring drafts inside reasoning.

    Some templates emit reasoning in ``content`` even with thinking disabled,
    and that reasoning can contain a draft block. Reasoning is removed first
    and the last remaining block (written after any reasoning) is the final
    answer.
    """
    visible = _THINKING_BLOCK.sub("", str(content or ""))
    blocks = _CODE_EDIT_BLOCK.findall(visible)
    return blocks[-1].strip() if blocks else None


def user_facing_content(content: str) -> str:
    """Reject provider planning text instead of displaying it in the chat UI.

    Some hosted model templates can emit a planning trace in ``content`` even
    when thinking is disabled. It is not an answer and must never be saved to
    a user's conversation history.
    """
    return _user_facing_answer(_THINKING_BLOCK.sub("", content))


def _user_facing_answer(visible: str) -> str:
    """Apply the answer rules to text whose reasoning blocks are already removed.

    The streaming filter removes reasoning blocks incrementally, so it shares
    this step instead of running the block regex again over cleaned text.
    """
    cleaned = visible.strip()
    final_match = _FINAL_LABEL.search(cleaned)
    if final_match:
        cleaned = final_match.group(1).strip()
    if not cleaned or _INTERNAL_PLANNING_PREFIX.match(cleaned):
        raise LLMProviderError("NVIDIA returned internal planning instead of a user-facing answer.")
    return cleaned


def is_transient_provider_error(error: Exception) -> bool:
    return isinstance(error, (APITimeoutError, APIConnectionError)) or (
        isinstance(error, APIStatusError) and error.status_code in {429, 500, 502, 503, 504}
    )


def retry_delay(error: Exception, attempt: int) -> float:
    response = getattr(error, "response", None)
    retry_after = response.headers.get("retry-after") if response is not None else None
    try:
        if retry_after is not None:
            return min(60.0, max(0.1, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return min(30.0, settings.embedding_retry_base_seconds * (2 ** attempt))


def _can_try_fallback(error: Exception) -> bool:
    """Allow a configured fallback for outages and retired model IDs only."""
    if is_transient_provider_error(error):
        return True
    return isinstance(error, APIStatusError) and error.status_code in {404, 410}


async def complete(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    enable_thinking: bool | None = None,
    max_tokens: int | None = None,
    fallback_models: list[str] | None = None,
    retry_attempts: int | None = None,
    structured_output: bool = False,
) -> str:
    """Return final answer content while intentionally discarding private reasoning.

    ``fallback_models`` is intentionally opt-in. The normal answer path keeps
    one deterministic model, while code editing can fail over to another
    allow-listed NVIDIA catalog model when a free endpoint is busy, retired, or
    temporarily unavailable.
    """
    api_key = require_nvidia_api_key()
    # Validate configuration before entering the retry loop, then reuse the
    # process-scoped client to avoid a DNS/TLS connection setup on every query.
    if not api_key:
        raise ModelConfigurationError("NVIDIA is not configured.")
    client = get_async_client()
    last_error: Exception | None = None
    model_names = list(dict.fromkeys([model or settings.nemotron_model, *(fallback_models or [])]))
    attempts = max(1, int(retry_attempts or settings.answer_retry_attempts))
    try:
        for model_index, model_name in enumerate(model_names):
            for attempt in range(attempts):
                try:
                    await nvidia_rate_limiter.acquire()
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        temperature=0.1,
                        top_p=0.95,
                        max_tokens=max_tokens or settings.answer_max_tokens,
                        extra_body={
                            "chat_template_kwargs": {
                                "enable_thinking": (
                                    settings.nvidia_enable_thinking if enable_thinking is None else enable_thinking
                                ),
                                "force_nonempty_content": True,
                            },
                        },
                    )
                    if not response.choices:
                        raise LLMProviderError("NVIDIA returned an empty completion.")
                    content = response.choices[0].message.content
                    if not isinstance(content, str) or not content.strip():
                        raise LLMProviderError("NVIDIA returned a completion without answer content.")
                    if structured_output:
                        structured = final_code_edit_block(content)
                        if structured:
                            # Code editing consumes only the machine-readable
                            # patch. Any model reasoning or wrapper prose is
                            # discarded before exact-source validation.
                            return structured
                    return user_facing_content(content)
                except LLMProviderError:
                    raise
                except Exception as error:
                    last_error = error
                    # A retired or invalid model will never recover by
                    # retrying. Switch to the configured fallback immediately.
                    if isinstance(error, APIStatusError) and error.status_code in {404, 410}:
                        break
                    if not _can_try_fallback(error) or attempt == attempts - 1:
                        break
                    await asyncio.sleep(retry_delay(error, attempt))
            # A fallback is useful only after the primary has exhausted its
            # retries. Never fall back after auth, validation, or content errors.
            if model_index < len(model_names) - 1 and not _can_try_fallback(last_error or Exception()):
                break

        if isinstance(last_error, APITimeoutError):
            raise LLMProviderError("NVIDIA timed out while generating the answer after retries.") from last_error
        if isinstance(last_error, APIConnectionError):
            raise LLMProviderError("Could not connect to NVIDIA while generating the answer after retries.") from last_error
        if isinstance(last_error, APIStatusError):
            raise LLMProviderError(
                f"NVIDIA could not generate the answer after retries (HTTP {last_error.status_code})."
            ) from last_error
        raise LLMProviderError("NVIDIA returned an invalid response.") from last_error
    except LLMProviderError:
        raise
    except Exception as error:
        raise LLMProviderError("NVIDIA returned an invalid response.") from error


# Streaming has to find reasoning tags one at a time, because an opening tag
# and its closing tag can arrive in different chunks. The tag names mirror
# _THINKING_BLOCK so streamed and complete() answers hide the same text.
_REASONING_OPEN_TAGS = ("<think>", "<analysis>", "<reasoning>")
_REASONING_CLOSE_TAGS = ("</think>", "</analysis>", "</reasoning>")
_REASONING_OPEN_TAG = re.compile("|".join(map(re.escape, _REASONING_OPEN_TAGS)), re.IGNORECASE)
_REASONING_CLOSE_TAG = re.compile("|".join(map(re.escape, _REASONING_CLOSE_TAGS)), re.IGNORECASE)


def _partial_tag_pattern(tags: tuple[str, ...]) -> re.Pattern[str]:
    """Match text that could still grow into one of ``tags`` but is not one yet."""
    prefixes = sorted({tag[:size] for tag in tags for size in range(1, len(tag))}, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, prefixes)), re.IGNORECASE)


_PARTIAL_OPEN_TAG = _partial_tag_pattern(_REASONING_OPEN_TAGS)
_PARTIAL_CLOSE_TAG = _partial_tag_pattern(_REASONING_CLOSE_TAGS)
_LONGEST_PARTIAL_TAG = max(map(len, _REASONING_OPEN_TAGS + _REASONING_CLOSE_TAGS)) - 1

# The opening of an answer is held back until it is long enough to recognise
# provider planning. Every fixed phrase in _INTERNAL_PLANNING_PREFIX is shorter
# than the paragraph threshold, so the planning check is final once either
# threshold is reached (see _starts_with_planning for the one exception).
_HOLD_BACK_CHARS = 160
_PARAGRAPH_HOLD_BACK_CHARS = 40
# "analysis"/"reasoning" may be followed by any amount of whitespace before the
# colon, and the prefix regex needs a word character right after that colon.
_UNFINISHED_PLANNING_LABEL = re.compile(r"(?:analysis|reasoning)\s*:?", re.IGNORECASE)


def _proper_prefixes(word: str) -> str:
    """Return a regex alternation of the non-empty proper prefixes of ``word``."""
    return "|".join(re.escape(word[:size]) for size in range(len(word) - 1, 0, -1))


# Every unfinished form of a _FINAL_LABEL label word plus its colon. A label
# inside the held-back opening is honoured like complete() does, but it can
# be split across chunks, so the filter must recognise one that has only
# partly arrived. This is fullmatched on every chunk while that is possible,
# so the alternatives ending in open-ended whitespace come first: a long blank
# run after "final", "final:" or "assistant" then matches without backtracking.
_UNFINISHED_FINAL_LABEL = re.compile(
    r"(?:final\s*(?::\s*)?"
    r"|assistant\s*"
    r"|(?:final\s+(?:answer|response)|assistant\s+response)\s*(?::\s*)?"
    rf"|final\s+(?:{_proper_prefixes('answer')}|{_proper_prefixes('response')})"
    rf"|assistant\s+(?:{_proper_prefixes('response')})"
    rf"|{_proper_prefixes('final')}|{_proper_prefixes('assistant')})",
    re.IGNORECASE,
)
# "final", "answer" and ":" are the most whitespace-separated pieces an
# unfinished label can have, so only that many trailing tokens are checked.
_FINAL_LABEL_MAX_TOKENS = 3

_HOLDING = "holding"
_STREAMING = "streaming"
_CAPTURING = "capturing"


def _partial_tag_length(text: str, partial_tag: re.Pattern[str]) -> int:
    """Return how many trailing characters could still be the start of a tag."""
    # Every tag starts with "<", and the leftmost candidate is the longest one.
    for start in range(max(0, len(text) - _LONGEST_PARTIAL_TAG), len(text)):
        if text[start] == "<" and partial_tag.fullmatch(text, start):
            return len(text) - start
    return 0


def _hold_back_window_full(prefix: str) -> bool:
    """Return whether enough of the answer arrived to judge how it starts.

    A paragraph break only counts once it comes after the first 40 visible
    characters, so a short heading cannot end the hold-back before the
    planning check has enough text.
    """
    return len(prefix) >= _HOLD_BACK_CHARS or prefix.find("\n\n", _PARAGRAPH_HOLD_BACK_CHARS) != -1


def _starts_with_planning(prefix: str) -> bool | None:
    """Return the planning verdict for an answer prefix, or None while more text could change it.

    ``prefix`` must already be left-stripped. With at least 40 characters, a
    match can only be decided by later text when the prefix is just
    "analysis"/"reasoning" plus whitespace and maybe the colon, because the
    regex allows unbounded whitespace before the colon and checks the
    character after it.
    """
    if len(prefix) < _PARAGRAPH_HOLD_BACK_CHARS or _UNFINISHED_PLANNING_LABEL.fullmatch(prefix):
        return None
    return _INTERNAL_PLANNING_PREFIX.match(prefix) is not None


def _at_line_start(text: str, index: int) -> bool:
    """Return whether _FINAL_LABEL's ``^\\s*`` can reach a keyword starting at ``index``."""
    gap_start = len(text[:index].rstrip())
    return gap_start == 0 or "\n" in text[gap_start:index]


def _final_label_may_follow(prefix: str) -> bool:
    """Return whether the end of ``prefix`` could still grow into a "Final answer:" label.

    A label's keyword starts a line, so it begins at one of the last few
    whitespace-separated tokens. Its colon also needs non-whitespace after
    it before _FINAL_LABEL treats it as a label. This runs on every chunk
    while the answer is held, so whitespace is skipped with str methods
    rather than a per-character loop.
    """
    end = len(prefix)
    for _ in range(_FINAL_LABEL_MAX_TOKENS):
        head = prefix[:end].rstrip()
        if not head:
            return False
        start = len(head) - len(head.rsplit(None, 1)[-1])
        if _at_line_start(prefix, start) and _UNFINISHED_FINAL_LABEL.fullmatch(prefix, start):
            return True
        end = start
    return False


class StreamingContentFilter:
    """Apply user_facing_content() rules to an answer while it is streamed.

    Reasoning blocks are removed as they arrive, even when a tag is split
    across chunks, and text inside an unclosed block is never shown. The start
    of the answer is held back until it can be checked for provider planning;
    planning is captured instead of shown so finish() can still recover a
    "Final answer:" section or reject the reply, exactly like complete().

    A "Final answer:" label that starts inside the held-back opening is
    honoured like complete() does: only the text after it is shown, and that
    text gets its own planning check. Text that was already shown cannot be
    retracted, so a label arriving after the opening was released is shown as
    written. Captured planning and replies that end inside the hold-back
    window get the complete() rules in full at finish(). So, when every
    reasoning block is closed and no label starts after the released opening,
    the concatenated output equals user_facing_content() of the full text. The
    one exception is a reply with no visible text: finish() returns "" so the
    caller can report an empty completion rather than a planning failure.
    """

    def __init__(self) -> None:
        # Raw text that may still be the start of a reasoning tag.
        self._raw_tail = ""
        self._inside_reasoning = False
        self._mode = _HOLDING
        # Every visible character while holding or capturing.
        self._visible = ""
        # Where the answer begins in _visible once a "Final answer:" label was
        # found in the held-back opening; text before the label is never shown.
        self._answer_start: int | None = None
        # Whitespace is released only once later text proves it is not trailing,
        # so the streamed answer matches user_facing_content()'s strip().
        self._held_whitespace = ""
        self._released_text = False
        self._finished = False

    def feed(self, text: str) -> str:
        """Return the part of the answer that is safe to show now."""
        self._ensure_open()
        visible = self._strip_reasoning(text)
        if not visible:
            return ""
        if self._mode == _STREAMING:
            return self._release(visible)
        self._visible += visible
        if self._mode == _CAPTURING:
            return ""
        return self._decide_prefix()

    def finish(self) -> str:
        """Return the remaining safe text, or raise when no user-facing answer exists."""
        self._ensure_open()
        self._finished = True
        # A partial opening tag at the very end was ordinary text. Anything
        # still inside a reasoning block was never closed and stays private.
        visible = "" if self._inside_reasoning else self._raw_tail
        self._raw_tail = ""
        if self._mode == _STREAMING:
            released = self._release(visible)
            self._held_whitespace = ""
            return released
        # Nothing has been shown yet, so the complete text gets exactly the
        # rules complete() applies, including "Final answer:" extraction.
        text = self._visible + visible
        self._visible = ""
        if not text.strip():
            return ""
        return _user_facing_answer(text)

    def _ensure_open(self) -> None:
        # A second finish() would return the held-back answer twice.
        if self._finished:
            raise RuntimeError("StreamingContentFilter cannot be used after finish().")

    def _strip_reasoning(self, text: str) -> str:
        """Remove reasoning blocks from raw model text and return what is visible."""
        data = self._raw_tail + text
        visible: list[str] = []
        while data:
            if self._inside_reasoning:
                closing = _REASONING_CLOSE_TAG.search(data)
                if closing is None:
                    # Reasoning text is discarded; only a split closing tag is kept.
                    held = _partial_tag_length(data, _PARTIAL_CLOSE_TAG)
                    self._raw_tail = data[len(data) - held:]
                    return "".join(visible)
                data = data[closing.end():]
                self._inside_reasoning = False
            else:
                opening = _REASONING_OPEN_TAG.search(data)
                if opening is None:
                    held = _partial_tag_length(data, _PARTIAL_OPEN_TAG)
                    visible.append(data[: len(data) - held])
                    self._raw_tail = data[len(data) - held:]
                    return "".join(visible)
                visible.append(data[: opening.start()])
                data = data[opening.end():]
                self._inside_reasoning = True
        self._raw_tail = ""
        return "".join(visible)

    def _decide_prefix(self) -> str:
        """Choose between streaming and capturing once the answer opening is known."""
        if self._answer_start is None:
            prefix = self._visible.lstrip()
            if not self._opening_is_answer(prefix):
                return ""
            # Trailing whitespace cannot complete a label, and stripping it
            # matches the stripped text complete() searches.
            label = _FINAL_LABEL.search(prefix.rstrip())
            if label is None:
                if _final_label_may_follow(prefix):
                    return ""
                return self._start_streaming(prefix)
            # complete() answers with only the text after the first label, and
            # that text must pass the planning check on its own.
            self._answer_start = len(self._visible) - len(prefix) + label.start(1)
        answer = self._visible[self._answer_start:]
        if not self._opening_is_answer(answer):
            return ""
        return self._start_streaming(answer)

    def _opening_is_answer(self, opening: str) -> bool:
        """Return True once ``opening`` is known not to be planning, capturing it if it is."""
        if not _hold_back_window_full(opening):
            return False
        planning = _starts_with_planning(opening)
        if planning:
            self._mode = _CAPTURING
        return planning is False

    def _start_streaming(self, opening: str) -> str:
        """Release the checked opening and show later text as soon as it arrives."""
        self._mode = _STREAMING
        self._visible = ""
        return self._release(opening)

    def _release(self, visible: str) -> str:
        """Return visible text without leading or (so far) trailing whitespace."""
        text = self._held_whitespace + visible
        if not self._released_text:
            text = text.lstrip()
        body = text.rstrip()
        self._held_whitespace = text[len(body):]
        if body:
            self._released_text = True
        return body


def _transport_error_types() -> tuple[tuple[type[Exception], ...], tuple[type[Exception], ...]]:
    """Collect the raw HTTP errors that can escape while a stream body is read.

    The SDK converts transport failures into APITimeoutError/APIConnectionError
    only while sending a request. Reads after create() returns raise the HTTP
    library's own errors: httpx for older SDKs, the httpx2 fork for newer ones.
    """
    timeouts: list[type[Exception]] = [httpx.TimeoutException]
    transport: list[type[Exception]] = [httpx.TransportError]
    try:
        import httpx2
    except ImportError:
        pass
    else:
        timeouts.append(httpx2.TimeoutException)
        transport.append(httpx2.TransportError)
    return tuple(timeouts), tuple(transport)


_STREAM_TIMEOUT_ERRORS, _STREAM_TRANSPORT_ERRORS = _transport_error_types()


def _failed_request(error: Exception, stream: Any) -> Any:
    """Find the HTTP request behind a transport failure, if one is attached."""
    for source in (error, getattr(stream, "response", None)):
        try:
            request = getattr(source, "request", None)
        except RuntimeError:
            # httpx raises instead of returning None when no request is attached.
            continue
        if request is not None:
            return request
    return None


def _as_sdk_error(error: Exception, stream: Any) -> Exception:
    """Classify a stream failure the way complete() would receive it from the SDK."""
    if not isinstance(error, _STREAM_TRANSPORT_ERRORS):
        return error
    request = _failed_request(error, stream)
    sdk_error: Exception = (
        APITimeoutError(request=request)
        if isinstance(error, _STREAM_TIMEOUT_ERRORS)
        else APIConnectionError(request=request)
    )
    sdk_error.__cause__ = error
    return sdk_error


def _provider_failure(last_error: Exception | None) -> LLMProviderError:
    """Build the same user-facing failure complete() raises after its retries."""
    if isinstance(last_error, APITimeoutError):
        return LLMProviderError("NVIDIA timed out while generating the answer after retries.")
    if isinstance(last_error, APIConnectionError):
        return LLMProviderError("Could not connect to NVIDIA while generating the answer after retries.")
    if isinstance(last_error, APIStatusError):
        return LLMProviderError(f"NVIDIA could not generate the answer after retries (HTTP {last_error.status_code}).")
    return LLMProviderError("NVIDIA returned an invalid response.")


def _chunk_content(chunk: Any) -> str:
    """Return the answer text in one stream chunk, ignoring reasoning-only deltas."""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "delta", None), "content", None)
    return content if isinstance(content, str) else ""


async def _close_stream(stream: Any) -> None:
    """Release the provider connection without hiding why the stream ended."""
    try:
        await stream.close()
    except Exception:
        # The answer or its failure is already decided; a failed close must
        # not replace that outcome with a less useful error.
        return


async def stream_complete(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    enable_thinking: bool | None = None,
    max_tokens: int | None = None,
    retry_attempts: int | None = None,
) -> AsyncIterator[str]:
    """Yield the user-facing answer while NVIDIA generates it.

    The request and the reasoning/planning rules match complete(). A failed
    attempt is retried only while nothing has been yielded, because a retry
    would restart an answer the user is already reading. Consumers that may
    stop early should iterate inside ``contextlib.aclosing`` so the provider
    stream closes immediately rather than when the generator is collected.
    """
    api_key = require_nvidia_api_key()
    if not api_key:
        raise ModelConfigurationError("NVIDIA is not configured.")
    client = get_async_client()
    model_name = model or settings.nemotron_model
    attempts = max(1, int(retry_attempts or settings.answer_retry_attempts))
    yielded = False
    last_error: Exception | None = None
    for attempt in range(attempts):
        # Text held back by a failed attempt was never shown, so each attempt
        # starts with a fresh filter instead of mixing two generations.
        content_filter = StreamingContentFilter()
        stream: Any = None
        try:
            await nvidia_rate_limiter.acquire()
            stream = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.1,
                top_p=0.95,
                max_tokens=max_tokens or settings.answer_max_tokens,
                extra_body={
                    "chat_template_kwargs": {
                        "enable_thinking": (
                            settings.nvidia_enable_thinking if enable_thinking is None else enable_thinking
                        ),
                        "force_nonempty_content": True,
                    },
                },
                stream=True,
            )
            try:
                async for chunk in stream:
                    safe_text = content_filter.feed(_chunk_content(chunk))
                    if safe_text:
                        yielded = True
                        yield safe_text
            finally:
                # Runs on errors, early consumer exit (GeneratorExit) and
                # cancellation alike; none of them are swallowed here.
                await _close_stream(stream)
            remaining = content_filter.finish()
            if remaining:
                yielded = True
                yield remaining
            if not yielded:
                raise LLMProviderError("NVIDIA returned a completion without answer content.")
            return
        except LLMProviderError:
            raise
        except Exception as error:
            if yielded:
                raise LLMProviderError("The answer stream was interrupted.") from error
            last_error = _as_sdk_error(error, stream)
            # Same rule as complete(): retired models (404/410), auth and
            # validation errors never recover, so only outages are retried.
            if not is_transient_provider_error(last_error) or attempt == attempts - 1:
                break
            await asyncio.sleep(retry_delay(last_error, attempt))
    raise _provider_failure(last_error) from last_error
