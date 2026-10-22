"""G0: lenient model-output parsing, truncation and empty replies."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import test_edit_quality_data as data
from agent.code_edit import CodeEditValidationError, ModelOutputParseError, parse_model_object, repair_json_text
from agent.nemotron import LLMOutputTruncatedError, LLMProviderError, complete
from test_edit_quality_support import B2, B4, KyEditorCase, code_edit

run = asyncio.run


class ParseModelObjectTests(unittest.TestCase):
    def test_raw_tab_replies_parse_with_the_required_key(self):
        delay = parse_model_object(data.REPLIES["edit_reply_raw_tabs_delay"], "code_edit", "files")
        self.assertEqual(delay["title"], "Add input validation to delay function")
        self.assertEqual(len(delay["files"]), 1)
        self.assertIn("\t", delay["files"][0]["changes"][0]["old"])
        new_test = parse_model_object(data.REPLIES["edit_reply_raw_tabs_new_test"], "code_edit", "files")
        self.assertEqual(new_test["files"][0]["action"], "create")

    def test_review_replies_with_invalid_escapes_and_trailing_commas_parse(self):
        escaped = parse_model_object(data.REPLIES["review_reply_invalid_escape"], "code_review", "verdict")
        self.assertEqual((escaped["verdict"], len(escaped["files"])), ("changes", 2))
        # Lossless: the backslash the model wrote is kept.
        self.assertIn("\\`limit\\`", escaped["verification"][2])
        comma = parse_model_object(data.REPLIES["review_reply_trailing_comma"], "code_review", "verdict")
        self.assertEqual((comma["verdict"], len(comma["files"])), ("changes", 3))
        self.assertEqual(len(comma["verification"]), 6)

    def test_an_inner_object_without_the_required_key_is_never_accepted(self):
        reply = '<code_edit>{"files": [{"file_path": "a.ts", "reason": "x"}, oops]}</code_edit>'
        with self.assertRaises(ModelOutputParseError) as context:
            parse_model_object(reply, "code_edit", "files")
        message = str(context.exception)
        self.assertIn("JSON was invalid", message)
        self.assertIn("line 1 column", message)
        self.assertNotIn("could not produce a safe change", message)

    def test_repair_keeps_strings_and_valid_escapes_intact(self):
        text = '{"a": "x, ]", "b": "tab\\there \\u00e9 \\\\ \\"q\\"", "c": [1, 2,], "d": "\\d+",}'
        value = json.loads(repair_json_text(text))
        self.assertEqual(value, {"a": "x, ]", "b": 'tab\there é \\ "q"', "c": [1, 2], "d": "\\d+"})

    def test_the_last_tagged_block_wins_and_an_unclosed_block_still_parses(self):
        reply = '<code_edit>{"files": [], "title": "draft"}</code_edit> then <code_edit>{"files": [], "title": "final"}</code_edit>'
        self.assertEqual(parse_model_object(reply, "code_edit", "files")["title"], "final")
        cut = '<code_edit>\n{"title": "cut", "files": []}\n'
        self.assertEqual(parse_model_object(cut, "code_edit", "files")["title"], "cut")

    def test_no_json_at_all_names_what_is_missing(self):
        with self.assertRaises(CodeEditValidationError) as context:
            parse_model_object("I cannot do that.", "code_edit", "files")
        self.assertIn("no JSON object", str(context.exception))


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)])


class ProviderTests(unittest.TestCase):
    def call(self, responses, **kwargs):
        completions = _FakeCompletions(responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch("agent.nemotron.require_nvidia_api_key", return_value="key"), \
             patch("agent.nemotron.get_async_client", return_value=client), \
             patch("agent.nemotron.nvidia_rate_limiter", MagicMock(acquire=AsyncMock())):
            return run(complete([{"role": "user", "content": "x"}], **kwargs)), completions

    def test_a_structured_reply_cut_at_max_tokens_raises_truncated(self):
        with self.assertRaises(LLMOutputTruncatedError):
            self.call([_response("<code_edit>{\"files\": [", finish_reason="length")], structured_output=True, max_tokens=100)
        self.assertTrue(issubclass(LLMOutputTruncatedError, LLMProviderError))

    def test_an_empty_primary_reply_falls_back_to_the_next_model(self):
        text, completions = self.call(
            [_response(""), _response("<code_edit>{\"files\": []}</code_edit>")],
            model="primary", fallback_models=["fallback"], structured_output=True, retry_attempts=3,
        )
        self.assertEqual(text, "<code_edit>{\"files\": []}</code_edit>")
        self.assertEqual([call["model"] for call in completions.calls], ["primary", "fallback"])

    def test_empty_replies_from_every_model_still_raise(self):
        with self.assertRaisesRegex(LLMProviderError, "without answer content"):
            self.call([_response("  "), _response(None)], model="a", fallback_models=["b"])

    def test_the_editing_timeout_and_tag_reach_the_request(self):
        text, completions = self.call([_response("<code_review>{\"verdict\": \"ok\"}</code_review>")],
                                      structured_output="code_review", timeout=90)
        self.assertEqual(text, "<code_review>{\"verdict\": \"ok\"}</code_review>")
        self.assertEqual(completions.calls[0]["timeout"], 90.0)

    def test_a_call_that_used_its_explicit_timeout_is_not_retried_on_the_same_model(self):
        import httpx
        from openai import APITimeoutError

        timeout = APITimeoutError(request=httpx.Request("POST", "https://example.test"))
        text, completions = self.call([timeout, _response("<code_edit>{\"files\": []}</code_edit>")],
                                      model="primary", fallback_models=["fallback"], structured_output=True,
                                      retry_attempts=3, timeout=300)
        self.assertEqual([call["model"] for call in completions.calls], ["primary", "fallback"])
        self.assertIn("files", text)

    def test_a_structured_reply_without_a_block_is_returned_for_the_parser(self):
        text, _ = self.call([_response("Sorry, here is my plan: {\"oops\": 1}")], structured_output=True)
        self.assertIn("oops", text)


class EditorParsingTests(KyEditorCase):
    def test_raw_tab_reply_produces_the_delay_validation_proposal(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(len(result.model.calls), 1)
        self.assertEqual(proposal["title"], "Add input validation to delay function")
        self.assertEqual(len(proposal["files"]), 1)
        item = proposal["files"][0]
        self.assertEqual(item["before"], data.KY["source/utils/delay.ts"])
        guard = ("\tif (!Number.isFinite(ms) || ms < 0) {\n"
                 "\t\tthrow new TypeError('Expected `ms` to be a finite, non-negative number');\n\t}\n\n")
        self.assertEqual(item["after"], item["before"].replace("): Promise<void> {\n", "): Promise<void> {\n" + guard, 1))
        self.assertEqual((item["additions"], item["deletions"]), (4, 0))

    def test_raw_tab_reply_produces_the_new_test_file(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"]], evidence=self.evidence("source/utils/is.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        created = [item for item in proposal["files"] if item["action"] == "create"]
        self.assertEqual([item["file_path"] for item in created], ["test/is.ts"])

    def test_a_no_op_hunk_for_the_source_under_test_keeps_the_new_test_file(self):
        # Live B4 (in both the edit and the repair reply): the request names
        # source/utils/is.ts, and the model "kept" it with a hunk whose new
        # text equals its old text, which used to sink the whole edit.
        keep = "export const isObject = (value: unknown): value is object => value !== null && typeof value === 'object';"
        self.assertIn(keep, data.KY["source/utils/is.ts"])
        reply = code_edit([
            {"file_path": "source/utils/is.ts", "action": "modify", "summary": "Source under test",
             "changes": [{"old": keep, "new": keep}]},
            {"file_path": "test/is.ts", "action": "create", "summary": "New tests",
             "content": "import test from 'ava';\nimport {isObject} from '../source/utils/is.js';\n\n"
                        "test('isObject', t => {\n\tt.true(isObject({}));\n});\n"},
        ])
        result = self.edit(B4, [reply], evidence=self.evidence("source/utils/is.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(len(result.model.calls), 1)  # accepted without a repair round
        self.assertEqual([(item["file_path"], item["action"]) for item in proposal["files"]], [("test/is.ts", "create")])
        unchanged = {item["file_path"]: item["reason"] for item in proposal["unchanged_files"]}
        self.assertEqual(unchanged["source/utils/is.ts"], "The proposed hunks for this file changed nothing, so it is left as it is.")
        self.assertFalse(proposal["incomplete"])

    def test_a_reply_without_the_files_object_names_the_json_error(self):
        only_inner = '<code_edit>{"files": [{"file_path": "source/utils/delay.ts", "reason": "x"} oops]}</code_edit>'
        result = self.edit(B2, [only_inner, only_inner], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNone(result.proposal)
        repair = result.model.calls[1]["messages"][-1]["content"]
        self.assertTrue(repair.startswith("Your JSON was invalid: Expecting ',' delimiter at line 1 column"), repair[:200])
        self.assertIn("Escape tabs as \\t and newlines as \\n", repair)
        self.assertIn("JSON was invalid", result.markdown)
        self.assertNotIn("could not produce a safe change", result.markdown)

    def test_truncated_output_gives_the_too_large_answer(self):
        result = self.edit(B2, [LLMOutputTruncatedError("cut")], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNone(result.proposal)
        self.assertIn("too large for one reviewable proposal", result.markdown)
        self.assertIn("output limit", result.markdown)
        scope = self.tool(result, "check_scope")
        # The run stops here (no proposal), so the step is a failure.
        self.assertEqual(scope["status"], "fail")
        self.assertIn("exceeded the output limit", scope["result_summary"])
        self.assertTrue(scope["input"]["parts"])
        self.assertEqual(scope["input"]["parts"][0]["files"], ["source/utils/delay.ts"])

    def test_a_valid_reply_after_a_parse_failure_is_accepted(self):
        broken = "<code_edit>{\"title\": \"x\", \"files\": [}</code_edit>"
        result = self.edit(B2, [broken, data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNotNone(result.proposal, result.markdown)
        self.assertEqual(self.stages.count("repairing"), 1)


if __name__ == "__main__":
    unittest.main()
