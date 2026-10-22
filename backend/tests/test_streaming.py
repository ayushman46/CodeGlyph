"""Tests for streamed NVIDIA answers and the incremental content filter."""

import asyncio
import contextlib
import random
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from agent import nemotron
from agent.nemotron import LLMProviderError, StreamingContentFilter, stream_complete, user_facing_content
from config import ModelConfigurationError, settings

# Captured before setUp patches it, for the test that checks real backoff.
REAL_RETRY_DELAY = nemotron.retry_delay

try:
    import httpx2
except ImportError:  # Older openai releases stream through httpx itself.
    httpx2 = None


MESSAGES = [{"role": "user", "content": "Where is caching implemented?"}]
REQUEST = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
PLANNING_MESSAGE = "NVIDIA returned internal planning instead of a user-facing answer."
EMPTY_MESSAGE = "NVIDIA returned a completion without answer content."
INTERRUPTED_MESSAGE = "The answer stream was interrupted."

# Longer than the 160-character hold-back window, and not planning.
LONG_ANSWER = (
    "Caching is implemented in `retrieval/cache.py`, which wraps every vector search in a "
    "small LRU keyed by repository and query text so repeated questions skip the database."
)
PLANNING_TEXT = (
    "The user asked where caching happens. I should inspect the cache module and its callers "
    "before answering, then summarise the invalidation path so the reply stays accurate."
)


def run_filter(chunks):
    """Feed ``chunks`` like stream_complete() does: a restart discards what was shown so far."""
    content_filter = StreamingContentFilter()
    outputs = []
    for chunk in chunks:
        output = content_filter.feed(chunk)
        if content_filter.take_restart():
            outputs = []
        outputs.append(output)
    return outputs, content_filter.finish()


def split_every(text, size):
    return [text[index:index + size] for index in range(0, len(text), size)]


def status_error(code, headers=None):
    response = httpx.Response(code, request=REQUEST, headers=headers or {})
    return APIStatusError(f"HTTP {code}", response=response, body=None)


def content_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, reasoning_content=None))])


def reasoning_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content=text))])


def usage_chunk():
    return SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=12))


BLOCK = object()


class FakeStream:
    """Async chunk stream that can fail, block, and records being closed."""

    def __init__(self, events):
        self._events = list(events)
        self.served = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        self.served += 1
        if event is BLOCK:
            await asyncio.Event().wait()
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        self.closed = True


class StreamingContentFilterTests(unittest.TestCase):
    def test_short_answer_is_held_until_finish(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed("  Hello"), "")
        self.assertEqual(content_filter.feed(" there.\n"), "")
        self.assertEqual(content_filter.finish(), "Hello there.")

    def test_answer_is_released_at_160_visible_characters_then_streams_immediately(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed("a" * 159), "")
        self.assertEqual(content_filter.feed("b"), "a" * 159 + "b")
        self.assertEqual(content_filter.feed(" next"), " next")
        self.assertEqual(content_filter.feed(" words"), " words")
        self.assertEqual(content_filter.finish(), "")

    def test_hidden_reasoning_and_leading_whitespace_do_not_count_as_visible(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed("  \n<think>" + "x" * 500 + "</think>"), "")
        self.assertEqual(content_filter.feed("a" * 159), "")
        self.assertEqual(content_filter.feed("b"), "a" * 159 + "b")

    def test_double_newline_after_40_characters_releases_early(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed("p" * 40), "")
        # The paragraph break is held as possibly trailing whitespace.
        self.assertEqual(content_filter.feed("\n\n"), "p" * 40)
        self.assertEqual(content_filter.feed("Next"), "\n\nNext")
        self.assertEqual(content_filter.finish(), "")

    def test_double_newline_within_first_40_characters_keeps_holding(self):
        content_filter = StreamingContentFilter()
        # The break starts at index 39, so it does not end the hold-back even
        # after the text grows past 40 characters.
        self.assertEqual(content_filter.feed("p" * 39 + "\n\n"), "")
        self.assertEqual(content_filter.feed("x" * 100), "")
        self.assertEqual(content_filter.finish(), "p" * 39 + "\n\n" + "x" * 100)

    def test_reasoning_blocks_split_across_feeds_are_removed(self):
        text = (
            "<THINK>plan the reply</think>Overview: <analysis>private notes</Analysis>alpha "
            "<reasoning>more <think> nested</reasoning>beta <think>mismatched</analysis>gamma "
            + LONG_ANSWER
            + " <Reasoning>late private</REASONING>delta"
        )
        expected = user_facing_content(text)
        for size in (1, 2, 3, 7, 50):
            with self.subTest(size=size):
                outputs, final = run_filter(split_every(text, size))
                streamed = "".join(outputs) + final
                self.assertEqual(streamed, expected)
                self.assertNotIn("private", streamed)
                self.assertNotIn("plan the reply", streamed)
                self.assertTrue(any(outputs), "long answers must stream before finish()")

    def test_reasoning_block_after_streaming_started_is_removed(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed(LONG_ANSWER), LONG_ANSWER)
        self.assertEqual(content_filter.feed(" Then <thi"), " Then")
        self.assertEqual(content_filter.feed("nk>secret</th"), "")
        self.assertEqual(content_filter.feed("ink> it returns."), "  it returns.")
        self.assertEqual(content_filter.finish(), "")

    def test_unclosed_reasoning_block_is_never_emitted(self):
        with self.subTest("while streaming"):
            outputs, final = run_filter([LONG_ANSWER, " Done. <think>private", " plan", " </thi"])
            self.assertEqual("".join(outputs) + final, LONG_ANSWER + " Done.")
        with self.subTest("while holding"):
            outputs, final = run_filter(["Short ", "<reason", "ing>private plan ", "still private"])
            self.assertEqual(outputs, ["", "", "", ""])
            self.assertEqual(final, "Short")
        with self.subTest("planning capture"):
            outputs, final = run_filter([PLANNING_TEXT, "\nFinal answer: Cache.py. <think>private"])
            self.assertEqual(outputs, ["", ""])
            self.assertEqual(final, "Cache.py.")

    def test_partial_tag_at_the_end_is_ordinary_text(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed(LONG_ANSWER), LONG_ANSWER)
        self.assertEqual(content_filter.feed(" if a <thi"), " if a")
        self.assertEqual(content_filter.finish(), " <thi")
        self.assertEqual(user_facing_content(LONG_ANSWER + " if a <thi"), LONG_ANSWER + " if a <thi")

    def test_angle_brackets_and_stray_closing_tags_stream_like_complete(self):
        text = LONG_ANSWER + " When x < y and a<b the </think> tag stays, as does <b>bold</b>."
        outputs, final = run_filter(split_every(text, 4))
        self.assertEqual("".join(outputs) + final, user_facing_content(text))
        self.assertIn("</think> tag stays", "".join(outputs) + final)

    def test_trailing_whitespace_is_dropped_and_inner_whitespace_kept(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed(LONG_ANSWER + "  "), LONG_ANSWER)
        self.assertEqual(content_filter.feed("\n\n"), "")
        self.assertEqual(content_filter.feed("Next line."), "  \n\nNext line.")
        self.assertEqual(content_filter.feed(" \t\n"), "")
        self.assertEqual(content_filter.finish(), "")

    def test_planning_prefix_is_captured_and_final_answer_extracted(self):
        text = PLANNING_TEXT + "\n\nFinal answer: Caching happens in `cache.py`."
        outputs, final = run_filter(split_every(text, 7))
        self.assertEqual(set(outputs), {""})
        self.assertEqual(final, "Caching happens in `cache.py`.")
        self.assertEqual(final, user_facing_content(text))

    def test_planning_without_final_answer_raises(self):
        content_filter = StreamingContentFilter()
        for chunk in split_every(PLANNING_TEXT + " Then I will cite files.", 9):
            self.assertEqual(content_filter.feed(chunk), "")
        with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
            content_filter.finish()

    def test_planning_after_hidden_reasoning_block_is_captured(self):
        text = "<think>scratch</think>\n" + PLANNING_TEXT + "\nAssistant response: Use `cache.py`."
        outputs, final = run_filter(split_every(text, 5))
        self.assertEqual(set(outputs), {""})
        self.assertEqual(final, "Use `cache.py`.")

    def test_short_output_gets_complete_rules_at_finish(self):
        with self.subTest("short planning"):
            content_filter = StreamingContentFilter()
            self.assertEqual(content_filter.feed("I should greet them."), "")
            with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
                content_filter.finish()
        with self.subTest("short planning with final answer"):
            self.assertEqual(run_filter(["We will answer.\nFinal answer: ", "Hi!"]), (["", ""], "Hi!"))
        with self.subTest("short final label"):
            self.assertEqual(run_filter(["Final answer: 42"]), ([""], "42"))
        with self.subTest("short answer whose final section is planning"):
            content_filter = StreamingContentFilter()
            self.assertEqual(content_filter.feed("Hello.\nFinal answer: I should go."), "")
            with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
                content_filter.finish()

    def test_label_in_streamed_answer_is_shown_as_written(self):
        # Streaming already showed the opening, so the label cannot be applied.
        text = LONG_ANSWER + "\nFinal answer: cache.py."
        outputs, final = run_filter([LONG_ANSWER, "\nFinal answer: cache.py."])
        self.assertEqual(outputs[0], LONG_ANSWER)
        self.assertEqual("".join(outputs) + final, text)
        self.assertEqual(user_facing_content(text), "cache.py.")

    def test_final_label_at_the_start_of_a_long_answer_is_removed(self):
        text = "Final answer: " + LONG_ANSWER + " Entries expire after ten minutes."
        for size in (1, 5, 13, len(text)):
            with self.subTest(size=size):
                outputs, final = run_filter(split_every(text, size))
                streamed = "".join(outputs) + final
                self.assertEqual(streamed, user_facing_content(text))
                self.assertNotIn("Final answer", streamed)
                self.assertTrue(any(outputs), "the answer after the label still streams before finish()")

    def test_text_before_a_label_inside_the_opening_is_never_shown(self):
        text = "Here is what I found.\nFinal answer: " + LONG_ANSWER
        outputs, final = run_filter(split_every(text, 6))
        self.assertEqual(user_facing_content(text), LONG_ANSWER)
        self.assertEqual("".join(outputs) + final, LONG_ANSWER)

    def test_only_the_first_label_inside_the_opening_is_applied(self):
        # complete() keeps everything after the first label, later labels included.
        text = "Final answer: " + "first " * 30 + "\nFinal answer: second"
        outputs, final = run_filter(split_every(text, 4))
        self.assertEqual("".join(outputs) + final, user_facing_content(text))
        self.assertIn("\nFinal answer: second", user_facing_content(text))

    def test_planning_after_a_label_inside_the_opening_is_never_shown(self):
        text = "Short intro.\nFinal answer: " + PLANNING_TEXT
        content_filter = StreamingContentFilter()
        for chunk in split_every(text, 8):
            self.assertEqual(content_filter.feed(chunk), "")
        with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
            content_filter.finish()
        with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
            user_facing_content(text)

    def test_label_split_at_the_hold_back_boundary_is_still_honoured(self):
        # The opening reaches 160 characters while the label is still arriving.
        opening = "o" * 158 + "\n"
        label = "Final answer: "
        text = opening + label + LONG_ANSWER
        for cut in range(len(opening), len(opening) + len(label) + 1):
            with self.subTest(cut=cut):
                outputs, final = run_filter([text[:cut], text[cut:]])
                self.assertEqual(outputs[0], "")
                self.assertEqual(outputs[1] + final, LONG_ANSWER)
        outputs, final = run_filter(list(text))
        self.assertEqual("".join(outputs) + final, LONG_ANSWER)

    def test_unfinished_label_that_becomes_prose_releases_the_opening(self):
        opening = "o" * 158 + "\n"
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed(opening + "Fin"), "")
        self.assertEqual(content_filter.feed("ally, the cache is cleared."), opening + "Finally, the cache is cleared.")
        self.assertEqual(content_filter.finish(), "")

    def test_label_section_gets_its_own_hold_back_window(self):
        content_filter = StreamingContentFilter()
        self.assertEqual(content_filter.feed("Final answer: " + "s" * 159), "")
        self.assertEqual(content_filter.feed("t"), "s" * 159 + "t")
        self.assertEqual(content_filter.feed(" more"), " more")
        self.assertEqual(content_filter.finish(), "")

    def test_unfinished_label_detection_agrees_with_the_final_label_regex(self):
        rng = random.Random(5)
        pending_seen = 0
        for _ in range(3000):
            prefix = "".join(rng.choice(LABEL_PIECES) for _ in range(rng.randint(1, 6))).lstrip() or "x"
            pending = nemotron._final_label_may_follow(prefix)
            complete = nemotron._FINAL_LABEL.search(prefix.rstrip()) is not None
            completed_inside = any(label_keyword_start(prefix + rest) < len(prefix) for rest in LABEL_COMPLETIONS)
            if pending:
                pending_seen += 1
                # Waiting is only justified when more text really can finish a label.
                self.assertTrue(completed_inside, repr(prefix))
            elif not complete:
                # Releasing is only safe when no continuation turns this text into a label.
                self.assertFalse(completed_inside, repr(prefix))
        self.assertGreater(pending_seen, 200)

    def test_random_labels_inside_the_opening_match_user_facing_content(self):
        rng = random.Random(41)
        raised = streamed = 0
        for iteration in range(200):
            text = random_labelled_answer(rng)
            chunks = random_chunks(rng, text)
            content_filter = StreamingContentFilter()
            outputs = [content_filter.feed(chunk) for chunk in chunks]
            try:
                expected = user_facing_content(text)
            except LLMProviderError:
                raised += 1
                # Nothing may be shown when complete() would reject the reply.
                self.assertEqual(set(outputs), {""}, f"iteration {iteration}: {chunks!r}")
                with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
                    content_filter.finish()
                continue
            if any(outputs):
                streamed += 1
            self.assertEqual("".join(outputs) + content_filter.finish(), expected, f"iteration {iteration}: {chunks!r}")
        self.assertGreater(raised, 20)
        self.assertGreater(streamed, 50)

    def test_analysis_label_with_long_whitespace_waits_for_the_deciding_character(self):
        label = "Analysis" + " " * 200
        cases = (
            # The prefix regex needs a word character right after the colon.
            (":", "Check the router first.", True),
            (":", " Check the router first.", False),
            ("", "of the router follows.", False),
        )
        for colon, rest, planning in cases:
            with self.subTest(colon=colon, rest=rest):
                full = label + colon + rest
                content_filter = StreamingContentFilter()
                # Past 160 characters, but the verdict still depends on what follows.
                self.assertEqual(content_filter.feed(label), "")
                if colon:
                    self.assertEqual(content_filter.feed(colon), "")
                streamed = content_filter.feed(rest)
                if planning:
                    self.assertEqual(streamed, "")
                    with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
                        content_filter.finish()
                    with self.assertRaises(LLMProviderError):
                        user_facing_content(full)
                else:
                    self.assertTrue(streamed, "a decided non-planning prefix is released at once")
                    self.assertEqual(streamed + content_filter.finish(), user_facing_content(full))

    def test_empty_or_reasoning_only_output_finishes_empty(self):
        for chunks in ([], [""], ["  \n "], ["<think>only private</think>", "  "], ["<analysis>never closed"]):
            with self.subTest(chunks=chunks):
                outputs, final = run_filter(chunks)
                self.assertEqual("".join(outputs) + final, "")

    def test_filter_rejects_use_after_finish(self):
        content_filter = StreamingContentFilter()
        content_filter.feed("Hello")
        self.assertEqual(content_filter.finish(), "Hello")
        with self.assertRaises(RuntimeError):
            content_filter.finish()
        with self.assertRaises(RuntimeError):
            content_filter.feed("again")

    def test_random_splits_match_user_facing_content(self):
        rng = random.Random(20261011)
        streamed_before_finish = 0
        resolved_at_finish = 0
        for iteration in range(200):
            text = random_answer(rng)
            chunks = random_chunks(rng, text)
            cleaned = nemotron._THINKING_BLOCK.sub("", text).strip()
            # The generator must only produce texts the property covers.
            self.assertIsNone(nemotron._FINAL_LABEL.search(cleaned), text)
            self.assertIsNone(nemotron._INTERNAL_PLANNING_PREFIX.match(cleaned), text)
            expected = user_facing_content(text)
            outputs, final = run_filter(chunks)
            self.assertEqual("".join(outputs) + final, expected, f"iteration {iteration}: {chunks!r}")
            if any(outputs):
                streamed_before_finish += 1
            else:
                resolved_at_finish += 1
        # Both the streaming path and the whole-text-at-finish path were exercised.
        self.assertGreater(streamed_before_finish, 50)
        self.assertGreater(resolved_at_finish, 20)

    def test_random_splits_never_show_planning(self):
        # Safety half of (b): however a planning reply is chunked, feed() shows
        # nothing and finish() gives exactly what complete() would.
        rng = random.Random(1337)
        converted = rejected = 0
        for iteration in range(200):
            parts = []
            if rng.random() < 0.3:
                parts.append(random_block(rng))
            parts.append(rng.choice(PLANNING_OPENERS))
            for _ in range(rng.randint(0, 60)):
                parts.append(random_block(rng) if rng.random() < 0.1 else rng.choice(SEPARATORS) + rng.choice(WORDS))
            if rng.random() < 0.5:
                parts.append(rng.choice(["\n", "\n\n", " "]) + rng.choice(LABELS) + rng.choice(SAFE_OPENERS + PLANNING_SECTIONS))
            text = "".join(parts)
            chunks = random_chunks(rng, text)
            self.assertIsNotNone(
                nemotron._INTERNAL_PLANNING_PREFIX.match(nemotron._THINKING_BLOCK.sub("", text)), text
            )
            content_filter = StreamingContentFilter()
            outputs = [content_filter.feed(chunk) for chunk in chunks]
            self.assertEqual(set(outputs), {""}, f"iteration {iteration}: {chunks!r}")
            try:
                expected = user_facing_content(text)
            except LLMProviderError:
                rejected += 1
                with self.assertRaisesRegex(LLMProviderError, PLANNING_MESSAGE):
                    content_filter.finish()
                continue
            converted += 1
            self.assertEqual(content_filter.finish(), expected, f"iteration {iteration}: {chunks!r}")
        self.assertGreater(converted, 20)
        self.assertGreater(rejected, 20)

    def test_random_splits_never_leak_reasoning(self):
        rng = random.Random(7)
        for iteration in range(200):
            parts = [rng.choice(SAFE_OPENERS)]
            for _ in range(rng.randint(0, 40)):
                if rng.random() < 0.2:
                    parts.append(rng.choice(OPEN_TAGS) + "§ private § " + rng.choice(CLOSE_TAGS))
                else:
                    parts.append(rng.choice(SEPARATORS) + rng.choice(WORDS))
            if rng.random() < 0.5:
                parts.append(rng.choice(OPEN_TAGS) + "§ unclosed § </thi")
            text = "".join(parts)
            outputs, final = run_filter(random_chunks(rng, text))
            self.assertNotIn("§", "".join(outputs) + final, f"iteration {iteration}: {text!r}")


SAFE_OPENERS = ["Alpha", "Caching", "Routes", "Index", "Search", "Paths"]
PLANNING_OPENERS = [
    "I should", "i need to", "I will", "We should", "we need to", "We will", "The user said", "the user asked",
    "The user is asking", "No need to", "Analysis:Check", "reasoning :look", "  \n I should",
]
WORDS = [
    "alpha", "beta", "cache", "router", "x < y", "a<b", "<", "<t", "<thi", "<ANALYS", "<reasoni",
    # A closing tag in running text is ordinary text; one that ends a line
    # ends reasoning (covered by the stray-tag tests below).
    "</", "</thin", "</think> tag", "<b>", "</b>", "think", "->", "é", "→", "`code`", "1.", "-",
]
SEPARATORS = [" ", " ", " ", "\n", "\n\n", "\t", "  "]
OPEN_TAGS = ["<think>", "<Think>", "<analysis>", "<REASONING>", "<reasoning>"]
CLOSE_TAGS = ["</think>", "</THINK>", "</analysis>", "</Reasoning>", "</reasoning>"]
BLOCK_WORDS = ["private", "plan", "<think>", "<analysis>", "</thi", "</reas", "<", "I should", "Final answer: no", "\n\n"]


def random_block(rng):
    words = [rng.choice(BLOCK_WORDS) for _ in range(rng.randint(0, 6))]
    return rng.choice(OPEN_TAGS) + " ".join(words) + " " + rng.choice(CLOSE_TAGS)


def random_answer(rng):
    """Build a non-planning answer whose reasoning blocks are all closed."""
    parts = []
    if rng.random() < 0.3:
        parts.append(rng.choice([" ", "\n", "\n\n  "]))
    if rng.random() < 0.3:
        parts.append(random_block(rng))
    parts.append(rng.choice(SAFE_OPENERS))
    for _ in range(rng.randint(0, 60)):
        if rng.random() < 0.12:
            parts.append(random_block(rng))
        else:
            parts.append(rng.choice(SEPARATORS) + rng.choice(WORDS))
    if rng.random() < 0.3:
        parts.append(rng.choice([" ", "\n", "\n\n", " \t "]))
    return "".join(parts)


LABEL_PIECES = [
    "f", "fin", "final", "Final", "FINAL", "finally", " ", "  ", "\n", "\n\n", "\t", "answer", "ans", "Answer",
    "response", "resp", "assistant", "assist", "a", ":", " :", "x", "word", "-",
]
LABEL_COMPLETIONS = sorted(
    {keyword[size:] + ": x" for keyword in ("final", "final answer", "final response", "assistant response")
     for size in range(1, len(keyword) + 1)}
    | {"x", " x", "\nx"}
)
LABEL_INTROS = ["", "Summary.\n", "Here is the answer:\n", "<think>private</think>", "Okay.\n\n  ", "Note: ", "We will see.\n"]
LABELS = ["Final answer: ", "final:", "FINAL RESPONSE :", "Assistant response:\n", "Final\nanswer:  ", "Final answer"]
PLANNING_SECTIONS = ["I should check the cache module first.", "The user asked about caching, so I will look.", "No need to cite."]


def label_keyword_start(text):
    """Return where _FINAL_LABEL's keyword starts in ``text``, or infinity without a label."""
    cleaned = text.strip()
    match = nemotron._FINAL_LABEL.search(cleaned)
    if match is None:
        return float("inf")
    return match.start() + len(match.group(0)) - len(match.group(0).lstrip())


def random_labelled_answer(rng):
    """Build a reply whose first label, if any, starts inside the held-back opening."""
    if rng.random() < 0.25:
        section = rng.choice(PLANNING_SECTIONS) + " " + " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 40)))
    else:
        section = rng.choice(SAFE_OPENERS)
        for _ in range(rng.randint(0, 60)):
            section += random_block(rng) if rng.random() < 0.1 else rng.choice(SEPARATORS) + rng.choice(WORDS)
    return rng.choice(LABEL_INTROS) + rng.choice(LABELS) + section


def random_chunks(rng, text):
    if len(text) <= 1 or rng.random() < 0.1:
        chunks = list(text) or [""]
    else:
        cuts = sorted(rng.sample(range(1, len(text)), rng.randint(0, min(len(text) - 1, 25))))
        bounds = [0, *cuts, len(text)]
        chunks = [text[start:end] for start, end in zip(bounds, bounds[1:])]
    # Real streams contain empty deltas (role-only and usage chunks).
    if rng.random() < 0.3:
        chunks.insert(rng.randint(0, len(chunks)), "")
    return chunks


class StreamCompleteTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.create = AsyncMock()
        self.client.chat.completions.create = self.create
        patchers = (
            patch("agent.nemotron.get_async_client", return_value=self.client),
            patch("agent.nemotron.require_nvidia_api_key", return_value="test-key"),
            patch.object(nemotron.nvidia_rate_limiter, "acquire", new_callable=AsyncMock),
            patch("agent.nemotron.retry_delay", return_value=0.0),
        )
        started = []
        for patcher in patchers:
            started.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.acquire = started[2]
        self.retry_delay = started[3]

    def collect(self, **kwargs):
        async def scenario():
            return [piece async for piece in stream_complete(MESSAGES, **kwargs)]

        return asyncio.run(scenario())

    def collect_until_error(self, **kwargs):
        async def scenario():
            received = []
            try:
                async for piece in stream_complete(MESSAGES, **kwargs):
                    received.append(piece)
            except LLMProviderError as error:
                return received, error
            self.fail("stream_complete() did not raise")

        return asyncio.run(scenario())

    def test_streams_text_progressively_with_the_complete_request(self):
        answer = LONG_ANSWER + " Entries are invalidated whenever a repository is re-indexed and expire after ten minutes."
        chunks = split_every(answer, 40)
        stream = FakeStream([content_chunk(piece) for piece in chunks])
        self.create.return_value = stream

        async def scenario():
            received = []
            async for piece in stream_complete(MESSAGES):
                received.append((piece, stream.served))
            return received

        received = asyncio.run(scenario())
        self.assertEqual("".join(piece for piece, _ in received), answer)
        # Text first arrives once 160 characters (4 chunks) were seen, then
        # every later chunk is yielded as soon as it is read.
        self.assertEqual([served for _, served in received], list(range(4, len(chunks) + 1)))
        self.assertGreater(len(chunks), 5)
        self.assertEqual(self.create.await_count, 1)
        self.assertEqual(
            self.create.await_args.kwargs,
            {
                "model": settings.nemotron_model,
                "messages": MESSAGES,
                "temperature": 0.1,
                "top_p": 0.95,
                "max_tokens": settings.answer_max_tokens,
                "extra_body": {
                    "chat_template_kwargs": {
                        "enable_thinking": settings.nvidia_enable_thinking,
                        "force_nonempty_content": True,
                    },
                },
                "stream": True,
            },
        )
        self.acquire.assert_awaited_once_with()
        self.assertTrue(stream.closed)

    def test_request_overrides_are_forwarded(self):
        self.create.return_value = FakeStream([content_chunk("Hello.")])
        thinking = not settings.nvidia_enable_thinking
        self.assertEqual(self.collect(model="nvidia/other-model", enable_thinking=thinking, max_tokens=321), ["Hello."])
        kwargs = self.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "nvidia/other-model")
        self.assertEqual(kwargs["max_tokens"], 321)
        self.assertEqual(kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"], thinking)

    def test_reasoning_content_and_empty_chunks_are_ignored(self):
        self.create.return_value = FakeStream(
            [
                reasoning_chunk("I should look at cache.py first."),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
                usage_chunk(),
                content_chunk("Caching lives "),
                SimpleNamespace(choices=[SimpleNamespace(delta=None)]),
                content_chunk("in cache.py."),
                usage_chunk(),
            ]
        )
        self.assertEqual(self.collect(), ["Caching lives in cache.py."])

    def test_reasoning_blocks_split_across_chunks_are_removed(self):
        pieces = ["Hel", "lo <th", "ink>secret ", "plan</thi", "nk> world, ", LONG_ANSWER]
        self.create.return_value = FakeStream([content_chunk(piece) for piece in pieces])
        received = self.collect()
        self.assertEqual("".join(received), user_facing_content("".join(pieces)))
        self.assertNotIn("secret", "".join(received))
        self.assertNotIn("plan", "".join(received))

    def test_planning_output_is_converted_through_final_answer(self):
        text = PLANNING_TEXT + "\nFinal answer: Caching lives in `cache.py`."
        self.create.return_value = FakeStream([content_chunk(piece) for piece in split_every(text, 11)])
        self.assertEqual(self.collect(), ["Caching lives in `cache.py`."])

    def test_planning_output_without_final_answer_raises(self):
        self.create.return_value = FakeStream([content_chunk(piece) for piece in split_every(PLANNING_TEXT, 11)])
        received, error = self.collect_until_error()
        self.assertEqual(received, [])
        self.assertEqual(str(error), PLANNING_MESSAGE)
        self.assertEqual(self.create.await_count, 1)

    def test_transient_error_before_first_token_is_retried(self):
        busy = status_error(503)
        self.create.side_effect = [busy, FakeStream([content_chunk("Recovered answer.")])]
        self.assertEqual(self.collect(), ["Recovered answer."])
        self.assertEqual(self.create.await_count, 2)
        self.assertEqual(self.acquire.await_count, 2)
        self.retry_delay.assert_called_once_with(busy, 0)

    def test_failure_after_held_back_text_is_retried_without_duplicates(self):
        first = FakeStream([content_chunk("Partial answer still held back"), APIConnectionError(request=REQUEST)])
        second = FakeStream([content_chunk("Fresh answer.")])
        self.create.side_effect = [first, second]
        self.assertEqual(self.collect(), ["Fresh answer."])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_raw_transport_errors_mid_stream_are_retried_like_sdk_errors(self):
        raw_errors = [httpx.ReadTimeout("read timed out", request=REQUEST), httpx.RemoteProtocolError("peer closed")]
        if httpx2 is not None:
            raw_errors.append(httpx2.ReadTimeout("read timed out", request=httpx2.Request("POST", str(REQUEST.url))))
        for raw in raw_errors:
            with self.subTest(error=type(raw)):
                self.retry_delay.reset_mock()
                self.create.reset_mock()
                self.create.side_effect = [FakeStream([raw]), FakeStream([content_chunk("Recovered.")])]
                self.assertEqual(self.collect(), ["Recovered."])
                self.assertEqual(self.create.await_count, 2)
                retried_error = self.retry_delay.call_args.args[0]
                expected_type = APITimeoutError if "Timeout" in type(raw).__name__ else APIConnectionError
                self.assertIs(type(retried_error), expected_type)
                self.assertIs(retried_error.__cause__, raw)

    def test_error_after_first_token_raises_interrupted(self):
        dropped = httpx.RemoteProtocolError("peer closed connection")
        stream = FakeStream([content_chunk(LONG_ANSWER), content_chunk(" More."), dropped])
        self.create.side_effect = [stream, FakeStream([content_chunk("Should never be requested.")])]
        received, error = self.collect_until_error()
        self.assertEqual(received, [LONG_ANSWER, " More."])
        self.assertEqual(str(error), INTERRUPTED_MESSAGE)
        self.assertIs(error.__cause__, dropped)
        self.assertEqual(self.create.await_count, 1)
        self.retry_delay.assert_not_called()
        self.assertTrue(stream.closed)

    def test_status_error_after_first_token_raises_interrupted(self):
        failure = status_error(503)
        self.create.side_effect = [FakeStream([content_chunk(LONG_ANSWER), failure])]
        received, error = self.collect_until_error()
        self.assertEqual(received, [LONG_ANSWER])
        self.assertEqual(str(error), INTERRUPTED_MESSAGE)
        self.assertIs(error.__cause__, failure)
        self.assertEqual(self.create.await_count, 1)

    def test_empty_stream_raises(self):
        for events in ([], [usage_chunk()], [reasoning_chunk("thinking"), content_chunk("<think>only private</think>  ")]):
            with self.subTest(events=len(events)):
                self.create.reset_mock()
                self.create.side_effect = [FakeStream(events), FakeStream([content_chunk("unused")])]
                received, error = self.collect_until_error()
                self.assertEqual(received, [])
                self.assertEqual(str(error), EMPTY_MESSAGE)
                self.assertEqual(self.create.await_count, 1)

    def test_consumer_breaking_early_closes_the_stream(self):
        stream = FakeStream([content_chunk(piece) for piece in split_every(LONG_ANSWER * 3, 20)])
        self.create.return_value = stream

        async def scenario():
            async with contextlib.aclosing(stream_complete(MESSAGES)) as answer:
                async for piece in answer:
                    return piece

        first = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertTrue(stream.closed)
        self.assertLess(stream.served, len(split_every(LONG_ANSWER * 3, 20)))

    def test_cancelled_consumer_closes_the_stream_and_stays_cancelled(self):
        stream = FakeStream([content_chunk(LONG_ANSWER), BLOCK])
        self.create.return_value = stream

        async def scenario():
            received = []
            first_piece = asyncio.Event()

            async def consume():
                async for piece in stream_complete(MESSAGES):
                    received.append(piece)
                    first_piece.set()

            task = asyncio.create_task(consume())
            await first_piece.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return task, received

        task, received = asyncio.run(scenario())
        self.assertTrue(task.cancelled())
        self.assertEqual(received, [LONG_ANSWER])
        self.assertTrue(stream.closed)

    def test_exhausted_retries_map_to_complete_messages(self):
        cases = (
            (lambda: status_error(503), "NVIDIA could not generate the answer after retries (HTTP 503).", 2),
            (lambda: APITimeoutError(request=REQUEST), "NVIDIA timed out while generating the answer after retries.", 2),
            (lambda: APIConnectionError(request=REQUEST), "Could not connect to NVIDIA while generating the answer after retries.", 2),
            (lambda: status_error(404), "NVIDIA could not generate the answer after retries (HTTP 404).", 1),
            (lambda: status_error(410), "NVIDIA could not generate the answer after retries (HTTP 410).", 1),
            (lambda: status_error(400), "NVIDIA could not generate the answer after retries (HTTP 400).", 1),
            (lambda: ValueError("malformed chunk"), "NVIDIA returned an invalid response.", 1),
        )
        for make_error, message, expected_calls in cases:
            with self.subTest(message=message):
                self.create.reset_mock()
                self.retry_delay.reset_mock()
                errors = [make_error(), make_error()]
                self.create.side_effect = errors
                received, error = self.collect_until_error(retry_attempts=2)
                self.assertEqual(received, [])
                self.assertEqual(str(error), message)
                self.assertIs(error.__cause__, errors[expected_calls - 1])
                self.assertEqual(self.create.await_count, expected_calls)
                self.assertEqual(self.retry_delay.call_count, expected_calls - 1)

    def test_retry_attempts_controls_backoff_and_attempt_count(self):
        errors = [status_error(503), status_error(502), status_error(429)]
        self.create.side_effect = errors
        received, error = self.collect_until_error(retry_attempts=3)
        self.assertEqual(str(error), "NVIDIA could not generate the answer after retries (HTTP 429).")
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(self.acquire.await_count, 3)
        self.assertEqual(
            [call.args for call in self.retry_delay.call_args_list],
            [(errors[0], 0), (errors[1], 1)],
        )

    def test_default_retry_attempts_come_from_settings(self):
        attempts = max(1, settings.answer_retry_attempts)
        self.create.side_effect = [status_error(503) for _ in range(attempts + 1)]
        received, error = self.collect_until_error()
        self.assertEqual(self.create.await_count, attempts)

    def test_raw_transport_errors_map_to_complete_messages_after_retries(self):
        self.create.side_effect = [
            FakeStream([httpx.ReadTimeout("slow", request=REQUEST)]),
            FakeStream([httpx.ReadTimeout("slow", request=REQUEST)]),
        ]
        received, error = self.collect_until_error(retry_attempts=2)
        self.assertEqual(str(error), "NVIDIA timed out while generating the answer after retries.")
        self.assertIsInstance(error.__cause__, APITimeoutError)
        self.assertIsInstance(error.__cause__.__cause__, httpx.ReadTimeout)

    def test_raw_transport_error_without_request_is_still_classified(self):
        self.create.side_effect = [FakeStream([httpx.RemoteProtocolError("closed")])]
        received, error = self.collect_until_error(retry_attempts=1)
        self.assertEqual(str(error), "Could not connect to NVIDIA while generating the answer after retries.")

    def test_stream_close_failure_does_not_hide_the_answer(self):
        stream = FakeStream([content_chunk("Hello.")])
        stream.close = AsyncMock(side_effect=RuntimeError("already closed"))
        self.create.return_value = stream
        self.assertEqual(self.collect(), ["Hello."])
        stream.close.assert_awaited_once()

    def test_rate_limiter_is_awaited_before_every_request(self):
        calls = []
        self.acquire.side_effect = lambda: calls.append("acquire")

        async def create(**kwargs):
            calls.append("create")
            if calls.count("create") == 1:
                raise status_error(503)
            return FakeStream([content_chunk("Recovered answer.")])

        self.create.side_effect = create
        self.assertEqual(self.collect(), ["Recovered answer."])
        self.assertEqual(calls, ["acquire", "create", "acquire", "create"])

    def test_consumer_breaking_without_aclosing_closes_the_stream(self):
        pieces = split_every(LONG_ANSWER * 3, 20)
        stream = FakeStream([content_chunk(piece) for piece in pieces])
        self.create.return_value = stream

        async def scenario():
            async for piece in stream_complete(MESSAGES):
                break
            # asyncio finalises the abandoned generator on the next loop turns.
            for _ in range(3):
                await asyncio.sleep(0)
            return piece, stream.closed

        first, closed = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertTrue(closed)
        self.assertLess(stream.served, len(pieces))

    def test_cancellation_during_retry_backoff_is_not_swallowed(self):
        self.retry_delay.return_value = 30.0
        self.create.side_effect = [status_error(503), FakeStream([content_chunk("Never requested.")])]

        async def scenario():
            async def consume():
                return [piece async for piece in stream_complete(MESSAGES)]

            task = asyncio.create_task(consume())
            # Bounded so a regression fails the test instead of hanging it.
            for _ in range(100):
                if self.retry_delay.called:
                    break
                await asyncio.sleep(0)
            self.assertTrue(self.retry_delay.called, "the first failure was never backed off")
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return task

        task = asyncio.run(scenario())
        self.assertTrue(task.cancelled())
        self.assertEqual(self.create.await_count, 1)

    def test_backoff_sleeps_for_the_retry_delay_of_the_failed_attempt(self):
        # Use the real retry_delay so a Retry-After header drives the wait.
        self.retry_delay.side_effect = REAL_RETRY_DELAY
        limited = status_error(429, headers={"retry-after": "1.5"})
        self.create.side_effect = [limited, FakeStream([content_chunk("Recovered answer.")])]
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(self.collect(), ["Recovered answer."])
        sleep.assert_awaited_once_with(1.5)
        self.retry_delay.assert_called_once_with(limited, 0)

    def test_cancellation_before_the_first_token_closes_the_stream_without_retrying(self):
        # The reply is still inside the hold-back window when the consumer goes away.
        stream = FakeStream([content_chunk("Partial answer still held"), BLOCK])
        self.create.side_effect = [stream, FakeStream([content_chunk("Never requested.")])]

        async def scenario():
            received = []

            async def consume():
                async for piece in stream_complete(MESSAGES):
                    received.append(piece)

            task = asyncio.create_task(consume())
            for _ in range(100):
                if stream.served == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(stream.served, 2, "the stream never reached the blocking chunk")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return task, received

        task, received = asyncio.run(scenario())
        self.assertTrue(task.cancelled())
        self.assertEqual(received, [])
        self.assertTrue(stream.closed)
        self.assertEqual(self.create.await_count, 1)
        self.retry_delay.assert_not_called()

    def test_held_back_text_is_discarded_when_retries_run_out(self):
        first = FakeStream([content_chunk("Held answer one"), APIConnectionError(request=REQUEST)])
        second = FakeStream([content_chunk("Held answer two"), httpx.ReadTimeout("slow", request=REQUEST)])
        self.create.side_effect = [first, second]
        received, error = self.collect_until_error(retry_attempts=2)
        self.assertEqual(received, [])
        self.assertEqual(str(error), "NVIDIA timed out while generating the answer after retries.")
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_provider_error_event_before_first_token_is_not_retried(self):
        # The SDK raises a plain APIError for an error event inside the stream;
        # complete() never retries anything that is not a transient outage.
        event = APIError("upstream rejected the request", REQUEST, body={"message": "bad"})
        self.create.side_effect = [FakeStream([event]), FakeStream([content_chunk("Never requested.")])]
        received, error = self.collect_until_error()
        self.assertEqual(received, [])
        self.assertEqual(str(error), "NVIDIA returned an invalid response.")
        self.assertIs(error.__cause__, event)
        self.assertEqual(self.create.await_count, 1)

    def test_short_planning_reply_raises_without_yielding_or_retrying(self):
        self.create.side_effect = [
            FakeStream([content_chunk("I should "), content_chunk("greet them.")]),
            FakeStream([content_chunk("Never requested.")]),
        ]
        received, error = self.collect_until_error()
        self.assertEqual(received, [])
        self.assertEqual(str(error), PLANNING_MESSAGE)
        self.assertEqual(self.create.await_count, 1)

    def test_missing_api_key_fails_before_any_request(self):
        with patch(
            "agent.nemotron.require_nvidia_api_key",
            side_effect=ModelConfigurationError("NVIDIA is not configured."),
        ):
            with self.assertRaises(ModelConfigurationError):
                self.collect()
        self.acquire.assert_not_awaited()
        self.create.assert_not_awaited()

    def test_final_label_inside_the_opening_is_removed_from_the_stream(self):
        text = "Final answer: " + LONG_ANSWER
        stream = FakeStream([content_chunk(piece) for piece in split_every(text, 9)])
        self.create.return_value = stream
        received = self.collect()
        self.assertEqual("".join(received), LONG_ANSWER)
        self.assertTrue(stream.closed)

    def test_planning_after_a_label_inside_the_opening_raises_without_yielding(self):
        text = "Short intro.\nFinal answer: " + PLANNING_TEXT
        stream = FakeStream([content_chunk(piece) for piece in split_every(text, 9)])
        self.create.side_effect = [stream, FakeStream([content_chunk("Should never be requested.")])]
        received, error = self.collect_until_error()
        self.assertEqual(received, [])
        self.assertEqual(str(error), PLANNING_MESSAGE)
        self.assertEqual(self.create.await_count, 1)
        self.assertTrue(stream.closed)


class StreamingFilterResourceTests(unittest.TestCase):
    """Adversarial provider output must not stall the event loop or grow hidden state."""

    # The largest answer budget is 8,192 tokens, and a hostile or broken model
    # can spend each token on a one-character chunk that keeps the opening held.
    TOKEN_BUDGET = 8192

    def _feed_all(self, first: str, chunk: str) -> float:
        content_filter = StreamingContentFilter()
        started = time.perf_counter()
        content_filter.feed(first)
        for _ in range(self.TOKEN_BUDGET):
            content_filter.feed(chunk)
        try:
            content_filter.finish()
        except LLMProviderError:
            pass
        return time.perf_counter() - started

    def test_chunks_that_keep_the_opening_held_stay_fast_at_the_token_budget(self):
        cases = {
            "leading whitespace": ("", " "),
            "unfinished final label": ("x" * 50 + "\nFinal", " "),
            "label colon then blank lines": ("x" * 50 + "\nFinal answer:", "\n"),
            "unfinished analysis label": ("Analysis", " "),
            "repeated partial tags": ("", "<"),
        }
        for name, (first, chunk) in cases.items():
            with self.subTest(name):
                # Measured around 0.1 s; the bound only catches a regression to
                # catastrophic backtracking or a much worse per-chunk cost.
                self.assertLess(self._feed_all(first, chunk), 3.0)

    def test_unclosed_reasoning_does_not_retain_discarded_text(self):
        content_filter = StreamingContentFilter()
        content_filter.feed("<think>")
        for _ in range(1000):
            self.assertEqual(content_filter.feed("x" * 1000 + "</thi"), "")
        # Only a possible split closing tag is kept, never the reasoning itself.
        self.assertLessEqual(len(content_filter._raw_tail), len("</reasoning>"))
        self.assertEqual(content_filter._visible, "")
        self.assertEqual(content_filter.finish(), "")


if __name__ == "__main__":
    unittest.main()
