"""Answer trust: cut-off answers and Continue, code names that are not in the
index, per-reason reference checks, named model failures, and the prompt
rules behind them (BUG-02, BUG-05, BUG-06 wording, BUG-07, BUG-09).

Model calls are scripted; the identifier lookup, the stored answer and its
continuation run against an embedded libsql database with the real schema.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from openai import APIStatusError, APITimeoutError

import test_answers_and_editing as answers
from agent import nemotron
from agent.agent import (
    CONTINUE_INSTRUCTION,
    ModelSwitch,
    answer_word_budget,
    build_answer_messages,
    stream_agent_answer,
    stream_answer_continuation,
)
from agent.nemotron import (
    ANSWER_TRUNCATED,
    LLMProviderError,
    StreamingContentFilter,
    TruncatedAnswer,
    complete,
    stream_complete,
    user_facing_content,
)
from api import query_router
from api.answer_trust import (
    annotate_history_names,
    answer_check_entry,
    answer_code_names,
    continuation_base,
    describe_answer_failure,
    describe_model_switch,
    find_unverified_identifiers,
    join_continuation,
    restore_answer_check,
)
from api.query_router import QueryRequest, validate_answer_references
from config import Settings, get_answer_model_options, settings

run = asyncio.run
REQUEST = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
MESSAGES = [{"role": "user", "content": "How does click parse options?"}]

# A walk-through that stopped mid-sentence, as sim-first's click answer did.
CUT_ANSWER = (
    "## Direct answer\n\nClick parses options in `src/session.py:L1-L2`.\n\n"
    "## How it works\n\n1. The parser reads each token. If a token starts with a prefix, it is an option; otherwise it"
)


def chunk_event(text=None, finish_reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish_reason)])


class FakeStream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self):
        self.closed = True


class ProviderTestCase(unittest.TestCase):
    """NVIDIA client, key and rate limiter patched; no network."""

    def setUp(self):
        self.client = MagicMock()
        self.create = AsyncMock()
        self.client.chat.completions.create = self.create
        for patcher in (
            patch("agent.nemotron.get_async_client", return_value=self.client),
            patch("agent.nemotron.require_nvidia_api_key", return_value="test-key"),
            patch.object(nemotron.nvidia_rate_limiter, "acquire", new_callable=AsyncMock),
            patch("agent.nemotron.retry_delay", return_value=0.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stream(self, **kwargs):
        async def scenario():
            return [piece async for piece in stream_complete(MESSAGES, **kwargs)]
        return run(scenario())

    def stream_error(self):
        async def scenario():
            received = []
            try:
                async for piece in stream_complete(MESSAGES):
                    received.append(piece)
            except LLMProviderError as error:
                return received, error
            self.fail("stream_complete() did not raise")
        return run(scenario())


# ---------------------------------------------------------------------------
# BUG-02: an answer that reaches the output limit is marked, never "complete"
# ---------------------------------------------------------------------------


class TruncationDetectionTests(ProviderTestCase):
    def test_a_stream_that_stops_at_the_limit_ends_with_the_truncation_marker(self):
        text = "## Direct answer\n\nClick parses options in the parser module, which reads every token in order; otherwise it"
        self.create.return_value = FakeStream([chunk_event(text[:60]), chunk_event(text[60:]), chunk_event(None, "length")])
        received = self.stream()
        self.assertEqual("".join(received), text)
        self.assertIs(received[-1], ANSWER_TRUNCATED)
        self.assertIsInstance(received[-1], TruncatedAnswer)

    def test_a_stream_that_finishes_normally_has_no_marker(self):
        text = "## Direct answer\n\nClick parses options in the parser module and passes the values to your callback."
        self.create.return_value = FakeStream([chunk_event(text), chunk_event(None, "stop")])
        received = self.stream()
        self.assertEqual("".join(received), text)
        self.assertFalse(any(isinstance(piece, TruncatedAnswer) for piece in received))

    def test_reasoning_that_runs_out_of_budget_is_a_length_failure_not_an_answer(self):
        # Ultra with thinking on: untagged reasoning, cut off before </think>.
        reasoning = ("The user wants a go/no-go risk summary for changing how make_response converts tuples. "
                     "I need to use only the evidence provided. The repository evidence below is the sole source")
        self.create.return_value = FakeStream([chunk_event(reasoning), chunk_event(None, "length")])
        received, error = self.stream_error()
        self.assertEqual(received, [])
        self.assertEqual(error.reason, "length")

    def test_an_empty_stream_at_the_limit_is_a_length_failure(self):
        self.create.return_value = FakeStream([chunk_event(None, "length")])
        _, error = self.stream_error()
        self.assertEqual(error.reason, "length")

    def test_complete_returns_a_truncated_answer_for_plain_answers(self):
        choice = SimpleNamespace(message=SimpleNamespace(content="## Direct answer\nIt reads tokens; otherwise it"),
                                 finish_reason="length")
        self.create.return_value = SimpleNamespace(choices=[choice])
        answer = run(complete(MESSAGES))
        self.assertIsInstance(answer, TruncatedAnswer)
        self.assertEqual(answer, "## Direct answer\nIt reads tokens; otherwise it")
        choice.finish_reason = "stop"
        self.assertNotIsInstance(run(complete(MESSAGES)), TruncatedAnswer)

    def test_complete_with_only_reasoning_at_the_limit_is_a_length_failure(self):
        choice = SimpleNamespace(message=SimpleNamespace(content="The user wants a summary. I need to check app.py"),
                                 finish_reason="length")
        self.create.return_value = SimpleNamespace(choices=[choice])
        with self.assertRaises(LLMProviderError) as caught:
            run(complete(MESSAGES))
        self.assertEqual(caught.exception.reason, "length")


# ---------------------------------------------------------------------------
# BUG-09: reasoning text is never shown; failures carry a reason
# ---------------------------------------------------------------------------


class ReasoningLeakTests(ProviderTestCase):
    LEAK = (
        "The user wants a go/no-go risk summary for changing how `make_response` converts tuple and dict view return "
        "values. I need to use only the evidence provided. Let me analyze what I have.\n\n"
        "**src/flask/app.py (L1267-L1357)** - `Flask.make_response`"
    )

    def test_untagged_reasoning_preambles_are_recognised(self):
        for opening in (
            "The user wants a go/no-go risk summary for the change.",
            "The user is requesting a list of every call site.",
            "Okay, so the question is about make_response.",
            "Let me analyze the evidence before answering.",
            "We need to list the tests first.",
        ):
            with self.subTest(opening=opening), self.assertRaises(LLMProviderError) as caught:
                user_facing_content(opening + " More planning text follows here.")
            self.assertEqual(caught.exception.reason, "planning")
        for answer in ("## Direct answer\nThe user-facing API is `make_response`.", "Okay is a status, not planning."):
            self.assertEqual(user_facing_content(answer), answer)

    def test_the_streaming_filter_never_shows_the_leaked_reasoning(self):
        content_filter = StreamingContentFilter()
        shown = [content_filter.feed(self.LEAK[index:index + 17]) for index in range(0, len(self.LEAK), 17)]
        self.assertEqual("".join(shown), "")
        with self.assertRaises(LLMProviderError) as caught:
            content_filter.finish()
        self.assertEqual(caught.exception.reason, "planning")

    def test_provider_failures_name_their_cause(self):
        cases = {
            APITimeoutError(request=REQUEST): ("timeout", None),
            APIStatusError("busy", response=httpx.Response(503, request=REQUEST), body=None): ("unavailable", 503),
            APIStatusError("slow down", response=httpx.Response(429, request=REQUEST), body=None): ("rate_limited", 429),
            APIStatusError("bad", response=httpx.Response(400, request=REQUEST), body=None): ("http_error", 400),
        }
        for error, (reason, status) in cases.items():
            with self.subTest(reason=reason):
                self.create.reset_mock()
                self.create.side_effect = error
                _, failure = self.stream_error()
                self.assertEqual((failure.reason, failure.status), (reason, status))
        self.create.side_effect = None

    def test_detailed_answers_run_without_thinking_and_with_larger_budgets(self):
        defaults = {name: field.default for name, field in Settings.model_fields.items()}
        self.assertFalse(defaults["detailed_nvidia_enable_thinking"])
        self.assertFalse(defaults["nvidia_enable_thinking"])
        self.assertEqual((defaults["answer_max_tokens"], defaults["detailed_answer_max_tokens"]), (2_400, 4_096))
        with patch.object(settings, "detailed_nvidia_enable_thinking", False):
            self.assertFalse(get_answer_model_options("detailed")[1])


class FailureWordingTests(unittest.TestCase):
    def test_each_reason_reads_as_a_plain_sentence(self):
        timeout = LLMProviderError("x", reason="timeout")
        self.assertEqual(describe_answer_failure(timeout, "detailed"), {
            "reason": "timeout", "message": "The Detailed model did not respond in time.", "model_profile": "detailed",
        })
        busy = LLMProviderError("x", reason="unavailable", status=503)
        self.assertEqual(describe_answer_failure(busy, "fast")["message"],
                         "NVIDIA's Fast model endpoint is busy or down (HTTP 503).")
        planning = LLMProviderError("x", reason="planning")
        self.assertEqual(describe_answer_failure(planning, "detailed")["message"],
                         "The Detailed model returned its reasoning instead of an answer.")
        from config import ModelConfigurationError
        self.assertEqual(describe_answer_failure(ModelConfigurationError("missing"), "fast")["reason"], "not_configured")

    def test_both_models_are_named_when_the_other_model_also_failed(self):
        error = LLMProviderError("x", reason="unavailable", status=503)
        error.previous = LLMProviderError("x", reason="timeout")
        failure = describe_answer_failure(error, "detailed")
        self.assertEqual(failure["reason"], "timeout")
        self.assertEqual(failure["message"], "The Detailed model did not respond in time, and NVIDIA's Fast model "
                                             "endpoint is busy or down (HTTP 503).")

    def test_a_model_switch_says_which_model_wrote_the_answer(self):
        notice = describe_model_switch(ModelSwitch("fast", LLMProviderError("x", reason="timeout")), "detailed")
        self.assertEqual(notice["message"], "The Detailed model did not respond in time, so the Fast model wrote this answer.")
        self.assertEqual((notice["kind"], notice["model_profile"]), ("model_switch", "fast"))


# ---------------------------------------------------------------------------
# Prompt rules: length budget, code names, absence wording, continuation
# ---------------------------------------------------------------------------


class PromptRuleTests(unittest.TestCase):
    def test_the_answer_has_a_word_budget_matched_to_its_token_budget(self):
        self.assertEqual(answer_word_budget(2_400), 950)
        self.assertEqual(answer_word_budget(4_096), 1_600)
        system = build_answer_messages("Q?", "ctx", max_tokens=2_400)[0]["content"]
        self.assertIn("about 950 words", system)
        self.assertIn("end with a complete sentence", system)

    def test_missing_evidence_is_described_as_not_retrieved(self):
        system = build_answer_messages("Q?", "ctx")[0]["content"]
        self.assertIn('"I did not retrieve `X`"', system)
        self.assertIn('Never write "the repository has no X", "X does not exist"', system)
        self.assertIn("check that the exact name appears in the evidence", system)
        self.assertIn("false premises", system)
        user = build_answer_messages("Where is RabbitMQ used?", "ctx", evidence_plan={"absent_terms": ["rabbitmq"]})[-1]["content"]
        self.assertIn("not found anywhere in the indexed code", user)
        self.assertNotIn("the repository does not contain", user)

    def test_continuation_replays_the_answer_and_asks_for_the_rest(self):
        captured = {}

        async def fake_stream(messages, **kwargs):
            captured.update(kwargs, messages=messages)
            yield "the rest."

        async def collect():
            return [text async for text in stream_answer_continuation("Q?", "File: a.py (L1-L2)", "So far.", "fast")]

        with patch("agent.agent.stream_complete", fake_stream):
            self.assertEqual(run(collect()), ["the rest."])
        messages = captured["messages"]
        self.assertEqual(messages[-2], {"role": "assistant", "content": "So far."})
        self.assertEqual(messages[-1], {"role": "user", "content": CONTINUE_INSTRUCTION})
        self.assertIn("Question: Q?", messages[-3]["content"])
        self.assertEqual(captured["max_tokens"], get_answer_model_options("fast")[2])

    def test_the_other_model_is_tried_with_its_own_thinking_and_budget(self):
        calls = []

        def fake_stream(messages, *, model, enable_thinking, max_tokens, **kwargs):
            calls.append((model, enable_thinking, max_tokens, messages[0]["content"]))

            async def generate():
                if model == "nvidia/detailed":
                    raise LLMProviderError("x", reason="planning")
                yield "From fast."
            return generate()

        async def collect():
            return [text async for text in stream_agent_answer("Q?", "ctx", model_profile="detailed")]

        with patch.object(settings, "nemotron_model", "nvidia/fast"), \
             patch.object(settings, "detailed_nemotron_model", "nvidia/detailed"), \
             patch.object(settings, "detailed_nvidia_enable_thinking", True), \
             patch("agent.agent.stream_complete", fake_stream):
            parts = run(collect())
        self.assertIsInstance(parts[0], ModelSwitch)
        self.assertEqual(parts[1:], ["From fast."])
        self.assertEqual([call[:3] for call in calls], [
            ("nvidia/detailed", True, settings.detailed_answer_max_tokens),
            ("nvidia/fast", settings.nvidia_enable_thinking, settings.answer_max_tokens),
        ])
        self.assertIn(f"about {answer_word_budget(settings.answer_max_tokens)} words", calls[1][3])


# ---------------------------------------------------------------------------
# Continuation text
# ---------------------------------------------------------------------------


class ContinuationTextTests(unittest.TestCase):
    def test_the_unfinished_sentence_is_dropped_before_continuing(self):
        self.assertEqual(
            continuation_base("1. The parser reads each token. If it starts with a prefix it is an option; otherwise it"),
            "1. The parser reads each token. ",
        )
        # No sentence end in the last line: back to the line start.
        self.assertEqual(continuation_base("## How it works\n\n1. Then `parse_args` calls"), "## How it works\n\n")
        # Already at a line break: nothing is dropped.
        self.assertEqual(continuation_base("Done.\n"), "Done.\n")

    def test_joins_read_as_one_answer(self):
        self.assertEqual(join_continuation("First sentence. ", "Second sentence."), "First sentence. Second sentence.")
        self.assertEqual(join_continuation("Intro paragraph.\n", "## Next"), "Intro paragraph.\n\n## Next")
        self.assertEqual(join_continuation("- one\n", "- two"), "- one\n- two")
        self.assertEqual(join_continuation("Para.\n\n", "More."), "Para.\n\nMore.")
        self.assertEqual(join_continuation("```py\nx = 1\n", "y = 2\n```"), "```py\nx = 1\ny = 2\n```")

    def test_text_the_model_repeated_is_not_duplicated(self):
        base = "1. The parser reads each token and decides whether it is an option. "
        repeated = "The parser reads each token and decides whether it is an option. 2. Values reach the callback."
        self.assertEqual(join_continuation(base, repeated), base + "2. Values reach the callback.")
        line = "- `src/click/core.py:L10-L20` — the command entry point.\n"
        self.assertEqual(join_continuation(line, "- `src/click/core.py:L10-L20` — the command entry point.\n- next"),
                         line + "- next")


# ---------------------------------------------------------------------------
# BUG-05: code names are looked up in the indexed code
# ---------------------------------------------------------------------------


HTTPX_CLIENT = (
    "class BaseClient:\n"
    "    def _set_timeout(self, request):\n"
    "        request.extensions['timeout'] = self.timeout.as_dict()\n\n"
    "    def build_request(self, method, url):\n"
    "        return Request(method, url)\n"
)
HTTPX_SEND = (
    "class Client(BaseClient):\n"
    "    def send(self, request):\n"
    "        self._set_timeout(request)\n"
    "        return self._send_single_request(request)\n"
)
HTTPX_DOCS = "# http11.send_request_headers.started\n* `\"http11.send_request_body\"`\n"


class IdentifierShapeTests(unittest.TestCase):
    def test_only_symbol_shaped_names_are_checked(self):
        answer = (
            "Calls `self._set_timeout(request)` inside `BaseClient.send_request`; see `httpx/_client.py:L584-L593`, "
            "`request`, `Flask`, `__init__`, `TypeError`, `setTimeout`, `npm run build`, `--password`, `L612`, "
            "`make_response()`, `@app.route`, `MAX_REDIRECTS` and `sendRequest`.\n"
            "```py\nnot_checked_inside_fences()\n```"
        )
        names = {item["text"]: item["names"] for item in answer_code_names(answer)}
        self.assertEqual(names, {
            "self._set_timeout(request)": ["_set_timeout"],
            "BaseClient.send_request": ["BaseClient", "send_request"],
            "make_response()": ["make_response"],
            "MAX_REDIRECTS": ["MAX_REDIRECTS"],
            "sendRequest": ["sendRequest"],
        })


class IdentifierLookupTests(answers.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(answers.seed_repo(self.store))
        run(self.store.insert_chunks([
            answers.chunk("httpx/_client.py", 580, HTTPX_CLIENT.rstrip("\n")),
            answers.chunk("httpx/_main.py", 1, HTTPX_SEND.rstrip("\n")),
            answers.chunk("docs/advanced/extensions.md", 1, HTTPX_DOCS.rstrip("\n")),
            answers.chunk("tests/test_apps/cliapp/app.py", 1, 'from flask import Flask\n\ntestapp = Flask("testapp")'),
        ]))

    def check(self, answer, evidence=()):
        return run(find_unverified_identifiers(self.store, answers.REPO_ID, answer, list(evidence)))

    def test_invented_methods_and_subclasses_are_flagged_and_real_ones_are_not(self):
        flagged = self.check(
            "`_set_timeout` is called inside `BaseClient.send_request` and `_send_single_request`. "
            "`CliApp` subclasses `Flask`; `BlueprintApp` too. `testapp` is plain."
        )
        self.assertEqual(flagged, [
            {"text": "BaseClient.send_request", "names": ["send_request"]},
            {"text": "CliApp", "names": ["CliApp"]},
            {"text": "BlueprintApp", "names": ["BlueprintApp"]},
        ])

    def test_a_name_found_only_inside_a_longer_identifier_does_not_count(self):
        # http11.send_request_headers is in the docs; send_request is not a name anywhere.
        self.assertEqual(self.check("Uses `send_request`."), [{"text": "send_request", "names": ["send_request"]}])
        self.assertEqual(self.check("Uses `send_request_headers`."), [])

    def test_earlier_answers_are_marked_with_the_names_they_invented(self):
        # The question was asked before and that answer invented subclasses;
        # replayed as history they were repeated as fact.
        history = [
            {"role": "user", "content": "Does anything subclass Flask?"},
            {"role": "assistant", "content": "Yes: `BlueprintApp(Flask)` and `CliApp`, near `_set_timeout`."},
            {"role": "assistant", "content": "Only `_send_single_request`."},
        ]
        run(annotate_history_names(self.store, answers.REPO_ID, history))
        self.assertEqual(history[1]["unverified_names"], ["BlueprintApp", "CliApp"])
        self.assertNotIn("unverified_names", history[2])
        self.assertNotIn("unverified_names", history[0])
        messages = build_answer_messages("Does anything subclass Flask?", "ctx", history)
        self.assertIn("[Checked after this answer: `BlueprintApp`, `CliApp` occur nowhere in the indexed code, so this "
                      "answer was wrong about them. Do not repeat them.]", messages[2]["content"])
        self.assertEqual(messages[3]["content"], "Only `_send_single_request`.")
        self.assertIn("Earlier answers in this conversation can be wrong.", messages[0]["content"])

    def test_names_in_the_evidence_need_no_lookup_and_a_failed_lookup_flags_nothing(self):
        evidence = [{"file_path": "x.py", "content": "def made_up_name(): pass"}]
        self.assertEqual(self.check("Calls `made_up_name`.", evidence), [])
        broken = SimpleNamespace(fetch_all=AsyncMock(side_effect=RuntimeError("no fts")))
        self.assertEqual(run(find_unverified_identifiers(broken, "r", "Calls `BlueprintApp`.", [])), [])


# ---------------------------------------------------------------------------
# BUG-07: each unverified reference says what is actually wrong
# ---------------------------------------------------------------------------


class ReferenceReasonTests(answers.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(answers.seed_repo(self.store))
        package = "\n".join(f'  "line{n}": {n},' for n in range(1, 100))
        run(answers.seed_files(self.store, {"package.json": package, "lib/view.js": "module.exports = View;",
                                            "test/view.js": "describe('view')", "lib/router.js": "a\nb\nc"}))

    def test_outside_evidence_ambiguous_missing_and_past_the_end_are_told_apart(self):
        evidence = [answers.chunk("lib/router.js", 1, "a\nb\nc")]
        answer = ("See `package.json:L92-L98`, `view.js`, `lib/router.js:L1-L2`, `lib/missing.js:L3`, "
                  "`package.json:L140-L150` and `package.json:L95-L120`.")
        _, refs = run(validate_answer_references(self.store, answers.REPO_ID, answer, evidence))
        by_text = {item["text"]: item for item in refs}
        outside = by_text["package.json:L92-L98"]
        self.assertEqual((outside["valid"], outside["reason"], outside["file_path"]), (False, "range_outside_evidence", "package.json"))
        ambiguous = by_text["view.js"]
        self.assertEqual((ambiguous["reason"], ambiguous["candidates"]), ("ambiguous", ["lib/view.js", "test/view.js"]))
        self.assertTrue(by_text["lib/router.js:L1-L2"]["valid"])
        self.assertEqual(by_text["lib/missing.js:L3"]["reason"], "not_in_index")
        past_end = by_text["package.json:L140-L150"]
        self.assertEqual((past_end["reason"], past_end["file_lines"]), ("lines_outside_file", 99))
        # Starts inside the file but runs past its end.
        self.assertEqual(by_text["package.json:L95-L120"]["reason"], "lines_outside_file")

    def test_an_exact_indexed_path_wins_over_same_named_files_deeper_down(self):
        # express: `index.js` is the root entry file, not one of 29 examples/*/index.js.
        files = {"index.js": "module.exports = require('./lib/express');", "test/fixtures/deep/server.js": "x"}
        files.update({f"examples/demo{n}/{name}": "app.listen(3000)" for n in range(10) for name in ("index.js", "server.js")})
        run(answers.seed_files(self.store, files))
        _, refs = run(validate_answer_references(self.store, answers.REPO_ID, "Entry: `index.js`. Demo: `demo3/index.js`.", []))
        self.assertEqual([(item["valid"], item["file_path"]) for item in refs],
                         [(True, "index.js"), (True, "examples/demo3/index.js")])
        _, refs = run(validate_answer_references(self.store, answers.REPO_ID, "See `view.js` and `server.js`.", []))
        self.assertEqual(refs[0]["candidates"], ["lib/view.js", "test/view.js"])
        ambiguous = refs[1]
        self.assertEqual((ambiguous["reason"], ambiguous["candidate_count"]), ("ambiguous", 11))
        # Shallowest first, so the deep fixture is cut by the cap of 8.
        self.assertEqual(ambiguous["candidates"][:2], ["examples/demo0/server.js", "examples/demo1/server.js"])
        self.assertEqual(len(ambiguous["candidates"]), 8)
        self.assertNotIn("test/fixtures/deep/server.js", ambiguous["candidates"])


# ---------------------------------------------------------------------------
# The answer pipeline: stored truncated flag, Continue, checks after reload
# ---------------------------------------------------------------------------


def stream_of(*pieces, error=None):
    async def fake(*args, **kwargs):
        for piece in pieces:
            yield piece
        if error is not None:
            raise error
    return fake


class AnswerTrustEndpointTests(answers.EndpointTestCase):
    def ask(self, stream_fn, question=answers.QUESTION, **body):
        with patch("api.query_router.retrieve_context", AsyncMock(return_value=[answers.AUTH_CHUNK, answers.SESSION_CHUNK])), \
             patch("api.query_router.stream_agent_answer", stream_fn):
            response = self.client.post("/api/query/stream", json={"repo_name": answers.REPO_NAME, "question": question, **body})
        self.assertEqual(response.status_code, 200)
        return answers.parse_sse(response.text)[0]

    def continue_answer(self, message_id, stream_fn, **body):
        with patch("api.query_router.stream_answer_continuation", stream_fn):
            response = self.client.post("/api/query/stream", json={
                "repo_name": answers.REPO_NAME, "question": answers.QUESTION, "continue_from": message_id, **body,
            })
        self.assertEqual(response.status_code, 200)
        return answers.parse_sse(response.text)[0]

    def stored(self, message_id):
        return run(self.store.fetch_one("SELECT content, mode, tool_calls, citations FROM chat_messages WHERE id = ?", [message_id]))

    def history(self):
        return self.client.get(f"/api/conversations/{answers.REPO_NAME}").json()["messages"]

    def test_a_cut_off_answer_is_stored_as_truncated_and_continue_appends_the_rest(self):
        events = self.ask(stream_of(CUT_ANSWER, ANSWER_TRUNCATED))
        done = events[-1][1]
        self.assertEqual((done["mode"], done["truncated"]), ("truncated", True))
        message_id = done["message_id"]
        self.assertEqual(self.stored(message_id)["mode"], "truncated")
        # Reloading keeps the flag (and the reference check) with the message.
        reloaded = self.history()[-1]
        self.assertEqual((reloaded["id"], reloaded["mode"]), (message_id, "truncated"))
        self.assertTrue(reloaded["answer_references"][0]["valid"])
        self.assertEqual(reloaded["tool_calls"], [])

        captured = []

        def continuation(question, context, partial, *args):
            captured.append((question, context, partial, args))
            return stream_of("Otherwise it is a value and is stored. ", "2. Values reach your callback as keyword arguments.")()

        events = self.continue_answer(message_id, continuation)
        self.assertEqual([name for name, _ in events], ["stage", "continue", "token", "token", "done"])
        base = events[1][1]["base"]
        self.assertTrue(CUT_ANSWER.startswith(base))
        self.assertTrue(base.endswith("1. The parser reads each token. "))
        question, context, partial, _ = captured[0]
        self.assertEqual((question, partial), (answers.QUESTION, base))
        self.assertIn("    1| def check(user):", context)  # the stored evidence, with its line numbers
        done = events[-1][1]
        self.assertEqual((done["mode"], done["truncated"], done["message_id"], done["continued"]),
                         ("rag", False, message_id, True))
        expected = base + "Otherwise it is a value and is stored. 2. Values reach your callback as keyword arguments."
        self.assertEqual(done["answer"], expected)
        row = self.stored(message_id)
        self.assertEqual((row["content"], row["mode"]), (expected, "rag"))
        self.assertEqual(len([r for r in self.chat_rows() if r["role"] == "assistant"]), 1)
        # A complete answer cannot be continued again.
        events = self.continue_answer(message_id, stream_of("never"))
        self.assertEqual(events[-1], ("error", {"detail": "That answer is already complete.", "status": 409}))

    def test_a_failed_continuation_leaves_the_stored_answer_unchanged(self):
        message_id = self.ask(stream_of(CUT_ANSWER, ANSWER_TRUNCATED))[-1][1]["message_id"]
        events = self.continue_answer(message_id, stream_of(error=LLMProviderError("x", reason="timeout")))
        name, error = events[-1]
        self.assertEqual((name, error["status"]), ("error", 503))
        self.assertIn("The Fast model did not respond in time.", error["detail"])
        self.assertIn("The answer so far is unchanged.", error["detail"])
        row = self.stored(message_id)
        self.assertEqual((row["content"], row["mode"]), (CUT_ANSWER, "truncated"))
        events = self.continue_answer("not-a-stored-id", stream_of("never"))
        self.assertEqual(events[-1][1]["status"], 404)

    def test_names_not_in_the_index_are_flagged_and_kept_after_reload(self):
        run(self.store.insert_chunks([answers.AUTH_CHUNK, answers.SESSION_CHUNK]))
        answer = "Sessions are checked by `check` via `SessionValidator.validate_session` in `src/session.py:L1-L2`."
        done = self.ask(stream_of(answer))[-1][1]
        self.assertEqual(done["unverified_identifiers"], [
            {"text": "SessionValidator.validate_session", "names": ["SessionValidator", "validate_session"]},
        ])
        self.assertEqual(done["evidence_plan"]["reference_check"]["identifiers_not_found"], 1)
        self.assertEqual(done["tool_calls"], [])
        reloaded = self.history()[-1]
        self.assertEqual(reloaded["unverified_identifiers"], done["unverified_identifiers"])
        self.assertEqual(reloaded["mode"], "rag")

    def test_a_model_failure_is_named_and_kept_with_the_message(self):
        error = LLMProviderError("x", reason="unavailable", status=503)
        error.previous = LLMProviderError("x", reason="timeout")
        done = self.ask(stream_of(error=error), model_profile="detailed")[-1][1]
        self.assertEqual(done["mode"], "retrieval_fallback")
        self.assertEqual(done["answer_failure"]["reason"], "timeout")
        self.assertIn("No answer was written. The Detailed model did not respond in time, and NVIDIA's Fast model endpoint "
                      "is busy or down (HTTP 503).", done["answer"])
        self.assertEqual(self.history()[-1]["answer_failure"], done["answer_failure"])

    def test_a_model_switch_is_reported_with_the_answer(self):
        switch = ModelSwitch("fast", LLMProviderError("x", reason="planning"))
        done = self.ask(stream_of(switch, "The answer is in `src/auth.py:L1-L2`."), model_profile="detailed")[-1][1]
        self.assertEqual(done["answer"], "The answer is in `src/auth.py:L1-L2`.")
        self.assertEqual(done["answer_notice"]["message"], "The Detailed model returned its reasoning instead of an answer, "
                                                           "so the Fast model wrote this answer.")
        self.assertEqual(self.history()[-1]["answer_notice"], done["answer_notice"])


class StoredCheckTests(unittest.TestCase):
    def test_round_trip_through_tool_calls(self):
        refs = [{"text": "a.py:L1", "file_path": "a.py", "start_line": 1, "end_line": 1, "valid": True}]
        entry = answer_check_entry(answer_references=refs, unverified_identifiers=[], answer_failure=None)
        self.assertEqual(entry["tool"], "answer_check")
        self.assertEqual(entry["result_summary"], "1 of 1 file references verified")
        message = restore_answer_check({"tool_calls": json.loads(json.dumps([{"tool": "lookup"}, entry]))})
        self.assertEqual(message["tool_calls"], [{"tool": "lookup"}])
        self.assertEqual(message["answer_references"], refs)
        self.assertIsNone(answer_check_entry(answer_references=[], unverified_identifiers=[]))
        self.assertEqual(restore_answer_check({"tool_calls": [{"tool": "lookup"}]}), {"tool_calls": [{"tool": "lookup"}]})

    def test_the_continue_request_field_is_bounded(self):
        self.assertIsNone(QueryRequest(repo_name="r", question="q").continue_from)
        with self.assertRaises(Exception):
            QueryRequest(repo_name="r", question="q", continue_from="x" * 65)
        self.assertTrue(hasattr(query_router, "continue_answer_events"))


if __name__ == "__main__":
    unittest.main()
