"""G5: one verification-first self-review whose corrective hunks are applied one at a time and re-checked."""

import unittest

import httpx
import test_edit_quality_data as data
from agent.nemotron import LLMProviderError
from openai import APITimeoutError
from agent.editor import CodeEditor
from agent.review import apply_review_hunks, build_review_messages, locate_evidence
from test_edit_quality_support import B1, B2, B4, HAS_ESBUILD, KyEditorCase, code_edit, code_review

B1_REPLY = code_edit([{"file_path": "source/utils/normalize.ts", "action": "modify", "summary": "Default limit 3",
                       "changes": [{"old": "\tlimit: 2,\n\tmethods: retryMethods,", "new": "\tlimit: 3,\n\tmethods: retryMethods,"}]}],
                     title="Change default retry limit from 2 to 3")


def review_calls(result):
    return [call for call in result.model.calls if call.get("structured_output") == "code_review"]


class ReviewHunkTests(unittest.TestCase):
    def test_hunks_apply_one_at_a_time_and_already_applied_ones_count(self):
        content = "import {a} from './a.js';\nconst x = 1;\nconst y = 2;\n"
        updated, results = apply_review_hunks(content, [
            {"old": "import {a} from './a';", "new": "import {a} from './a.js';"},
            {"old": "const x = 1;", "new": "const x = 10;"},
            {"old": "const z = 3;", "new": "const z = 30;"},
            {"old": "const x = 10;\nconst y = 2;", "new": "const x = 10;\nconst y = 20;"},
        ])
        self.assertEqual(updated, "import {a} from './a.js';\nconst x = 10;\nconst y = 20;\n")
        self.assertEqual([result["status"] for result in results], ["already", "applied", "dropped", "applied"])

    def test_hunks_are_attributed_to_the_issues_of_their_file_and_lines(self):
        issues = [{"index": 0, "file_path": "a.ts", "line": 3}, {"index": 1, "file_path": "a.ts", "line": 41},
                  {"index": 2, "file_path": "b.ts", "line": 1}]
        by_index = {issue["index"]: issue for issue in issues}
        near = CodeEditor._hunk_issues({"file_path": "a.ts", "start": 2, "end": 2, "issue_index": None}, issues, by_index)
        self.assertEqual([issue["index"] for issue in near], [0])
        explicit = CodeEditor._hunk_issues({"file_path": "a.ts", "start": 2, "end": 2, "issue_index": 1}, issues, by_index)
        self.assertEqual([issue["index"] for issue in explicit], [1])
        far = CodeEditor._hunk_issues({"file_path": "a.ts", "start": 20, "end": 20, "issue_index": None}, issues, by_index)
        self.assertEqual(far, [])
        evidenced = {"finding": None, "evidence_line": locate_evidence({"problem": "`const x = 1;` is wrong"}, "a\nconst x = 1;\n"),
                     "file_path": "a.ts"}
        self.assertTrue(CodeEditor._issue_allows(evidenced, {"file_path": "a.ts", "start": 2, "end": 2}))
        self.assertFalse(CodeEditor._issue_allows(evidenced, {"file_path": "a.ts", "start": 40, "end": 40}))

    def test_the_review_prompt_is_bounded_and_verification_first(self):
        files = [{"file_path": "big.ts", "action": "modify", "before": "x\n" * 40_000, "after": "x\n" * 39_999 + "y\n"}]
        messages = build_review_messages(request_text="r", conventions_text="tabs", findings=["a finding"], files=files,
                                         related=[], title="T")
        self.assertIn('"verification"', messages[0]["content"])
        self.assertLessEqual(sum(len(message["content"]) for message in messages), 120_000)
        self.assertIn("regions around the change", messages[1]["content"])


class EditorReviewTests(KyEditorCase):
    def test_the_review_fixes_the_new_test_after_the_import_was_already_fixed(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"], data.REPLIES["review_reply_verify_new_test"]],
                           evidence=self.evidence("source/utils/is.ts"), review=True)
        proposal = result.proposal
        self.assertEqual(len(review_calls(result)), 1)
        created = next(item for item in proposal["files"] if item["file_path"] == "test/is.ts")
        self.assertIn("t.true(isNonNegativeNumber(Infinity));", created["after"])
        self.assertNotIn("t.false(isNonNegativeNumber(Infinity));", created["after"])
        self.assertNotIn("(null)", created["after"])
        self.assertIn("from '../source/utils/is.js';", created["after"])
        self.assertTrue(created["after"].endswith("\n"))
        quality = proposal["quality"]
        self.assertTrue(quality["reviewed"])
        self.assertGreaterEqual(sum(1 for issue in quality["review_issues"] if issue["fixed"]), 2)
        self.assertEqual(self.check(proposal, "review")["status"], "pass")
        tool = self.tool(result, "review_patch")
        self.assertEqual((tool["input"]["issues"], tool["input"]["fixed"]), (6, 6))
        self.assertIn("reviewing", self.stages)
        prompt = review_calls(result)[0]["messages"][1]["content"]
        self.assertIn("===== CURRENT CONTENT: source/utils/is.ts (related, not changed by the proposal; the source under test", prompt)
        self.assertIn("===== PROPOSED CONTENT: test/is.ts (after the change) =====", prompt)
        # The source under test is read before the proposed test.
        self.assertLess(prompt.index("CURRENT CONTENT: source/utils/is.ts"), prompt.index("PROPOSED CONTENT: test/is.ts"))
        self.assertIn("From AGENTS.md", prompt)

    def test_the_review_brings_in_stale_docs_types_and_tests(self):
        result = self.edit(B1, [B1_REPLY, data.REPLIES["review_reply_trailing_comma"]],
                           evidence=self.evidence("source/utils/normalize.ts"), review=True)
        proposal = result.proposal
        self.assertEqual(sorted(item["file_path"] for item in proposal["files"]),
                         ["readme.md", "source/types/retry.ts", "source/utils/normalize.ts", "test/retry.ts"])
        added = {item["file_path"]: item for item in proposal["files"]}
        self.assertIn("- `limit`: `3`", added["readme.md"]["after"])
        self.assertEqual(added["readme.md"]["before"], data.KY["readme.md"])
        self.assertIsNotNone(added["test/retry.ts"]["base_blob_sha"])
        completeness = self.check(proposal, "completeness")
        self.assertEqual(completeness["status"], "pass", completeness)
        self.assertEqual(proposal["title"], "Change default retry limit from 2 to 3")
        # The reviewer's PR text never enters the PR; its Changes list the final files.
        self.assertNotIn("across source, types, docs, and tests", proposal["pr_body"])
        for path in ("source/utils/normalize.ts", "readme.md", "source/types/retry.ts", "test/retry.ts"):
            self.assertIn(f"- `{path}`: ", proposal["pr_body"])
        self.assertIn("- `readme.md`: Update limit from 2 to 3 to match source/utils/normalize.ts", proposal["pr_body"])
        self.assertIn("## How to verify", proposal["pr_body"])
        self.assertEqual([issue["fixed"] for issue in proposal["quality"]["review_issues"]], [True, True, True])
        self.assertEqual({issue["finding"] for issue in proposal["quality"]["review_issues"]} - {None},
                         {issue["finding"] for issue in proposal["quality"]["review_issues"]})
        self.assertEqual(len(review_calls(result)), 1)

    def test_a_review_hunk_that_does_not_match_changes_nothing(self):
        review = code_review(files=[{"file_path": "source/utils/delay.ts", "changes": [{"old": "not in the file", "new": "x"}]}],
                             issues=[{"severity": "high", "file_path": "source/utils/delay.ts", "line": 13, "problem": "Wrong check"}])
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], review], evidence=self.evidence("source/utils/delay.ts"), review=True)
        before_review = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        self.assertEqual(result.proposal["files"][0]["after"], before_review.proposal["files"][0]["after"])
        issue = result.proposal["quality"]["review_issues"][0]
        self.assertEqual({key: issue[key] for key in ("severity", "file_path", "line", "problem", "fixed", "verified", "finding")},
                         {"severity": "high", "file_path": "source/utils/delay.ts", "line": 13, "problem": "Wrong check",
                          "fixed": False, "verified": False, "finding": None})
        # A claim that cites no finding and quotes no code is a suggestion, not a warning.
        self.assertEqual(self.check(result.proposal, "review")["status"], "pass")
        self.assertIn("1 unverified suggestion not applied", self.check(result.proposal, "review")["detail"])

    @unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
    def test_a_review_fix_that_breaks_syntax_reverts_only_that_file(self):
        review = code_review(files=[
            {"file_path": "source/utils/normalize.ts", "changes": [{"old": "\tlimit: 3,\n", "new": "\tlimit: 3,,{\n"}]},
            {"file_path": "readme.md", "changes": [{"old": "- `limit`: `2`", "new": "- `limit`: `3`"}]},
        ], issues=[{"severity": "medium", "file_path": "source/utils/normalize.ts", "line": 26, "problem": "x"},
                   {"severity": "medium", "file_path": "readme.md", "line": 264, "problem": "Readme says 2"}])
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        files = {item["file_path"]: item for item in result.proposal["files"]}
        self.assertEqual(sorted(files), ["readme.md", "source/utils/normalize.ts"])
        self.assertIn("\tlimit: 3,\n\tmethods", files["source/utils/normalize.ts"]["after"])
        self.assertEqual(files["source/utils/normalize.ts"]["syntax"]["status"], "pass")
        fixed = {issue["file_path"]: issue["fixed"] for issue in result.proposal["quality"]["review_issues"]}
        self.assertEqual(fixed, {"source/utils/normalize.ts": False, "readme.md": True})

    def test_escaped_backticks_in_a_review_hunk_still_match_markdown(self):
        # Lenient parsing keeps "\`" literally; the Markdown line has plain backticks.
        review = ('<code_review>{"verification": ["x"], "verdict": "changes", "summary": "Readme says 2.", '
                  '"issues": [{"severity": "high", "file_path": "readme.md", "line": 264, "problem": "Stale", "fix": "Say 3"}], '
                  '"files": [{"file_path": "readme.md", "changes": [{"old": "- \\`limit\\`: \\`2\\`", "new": "- \\`limit\\`: \\`3\\`"}]}], '
                  '"pr": {}}</code_review>')
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        readme = next(item for item in result.proposal["files"] if item["file_path"] == "readme.md")
        self.assertIn("- `limit`: `3`", readme["after"])
        self.assertNotIn("\\`", readme["after"])
        # A file the review brings in is described from the finding it fixed, not the review's prose.
        self.assertEqual(readme["summary"], "Update limit from 2 to 3 to match source/utils/normalize.ts")

    def test_a_partly_applied_review_does_not_lend_its_pr_body(self):
        review = code_review(
            files=[{"file_path": "source/types/retry.ts", "changes": [{"old": "\t@default 2\n\t*/\n\tlimit?: number | undefined;",
                                                                      "new": "\t@default 3\n\t*/\n\tlimit?: number | undefined;"}]},
                   {"file_path": "readme.md", "changes": [{"old": "- `limit`: `two`", "new": "- `limit`: `3`"}]}],
            issues=[{"severity": "high", "file_path": "source/types/retry.ts", "line": 26, "problem": "Stale JSDoc",
                     "fix": "Change @default 2 to @default 3"},
                    {"severity": "high", "file_path": "readme.md", "line": 264, "problem": "Stale readme"}],
            pr={"title": "Change default retry limit from 2 to 3", "body": "Also updates readme.md."})
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        body = result.proposal["pr_body"]
        self.assertNotIn("Also updates readme.md.", body)
        self.assertIn("`source/types/retry.ts`: Update limit from 2 to 3 to match source/utils/normalize.ts", body)
        self.assertNotIn("readme.md", body)
        self.assertEqual([line for line in body.split("\n") if line.startswith("## ")], ["## Summary", "## Changes", "## How to verify"])

    def test_an_unavailable_model_keeps_the_proposal_and_skips_the_review(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], LLMProviderError("HTTP 500")],
                           evidence=self.evidence("source/utils/delay.ts"), review=True)
        quality = result.proposal["quality"]
        self.assertFalse(quality["reviewed"])
        self.assertEqual(quality["review_summary"], "Self-review skipped: the model was unavailable")
        self.assertEqual(self.check(result.proposal, "review")["status"], "skipped")
        self.assertIn(quality["status"], {"warn", "fail"})
        self.assertEqual(self.tool(result, "review_patch")["status"], "skipped")

    def test_a_timed_out_review_says_it_took_too_long(self):
        # complete() raises its own timeout as a provider error caused by the
        # SDK's APITimeoutError (seen live on a slow shared endpoint).
        timeout = LLMProviderError("NVIDIA timed out while generating the answer after retries.")
        timeout.__cause__ = APITimeoutError(request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], timeout],
                           evidence=self.evidence("source/utils/delay.ts"), review=True)
        quality = result.proposal["quality"]
        self.assertFalse(quality["reviewed"])
        self.assertEqual(quality["review_summary"], "Self-review skipped: the review took longer than 45 s")
        self.assertEqual(self.tool(result, "review_patch")["result_summary"], "Skipped: the review took longer than 45 s")

    def test_a_review_of_an_unshown_file_is_ignored(self):
        review = code_review(files=[{"file_path": "source/utils/merge.ts", "changes": [{"old": "import", "new": "export"}]}],
                             issues=[{"severity": "low", "file_path": "source/utils/merge.ts", "line": 1, "problem": "x"}])
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], review], evidence=self.evidence("source/utils/delay.ts"), review=True)
        self.assertEqual([item["file_path"] for item in result.proposal["files"]], ["source/utils/delay.ts"])
        self.assertFalse(result.proposal["quality"]["review_issues"][0]["fixed"])

    def test_an_unparseable_review_is_skipped_with_its_reason(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], "I think it looks fine."],
                           evidence=self.evidence("source/utils/delay.ts"), review=True)
        self.assertEqual(result.proposal["quality"]["review_summary"], "Self-review skipped: the review reply could not be parsed")
        self.assertEqual(len(review_calls(result)), 1)

    def test_a_slow_edit_skips_the_review_without_a_call(self):
        from unittest.mock import patch

        from config import settings

        with patch.object(settings, "editing_review_deadline_seconds", -1):
            result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"), review=True)
        self.assertEqual(review_calls(result), [])
        self.assertIn("review deadline", result.proposal["quality"]["review_summary"])


if __name__ == "__main__":
    unittest.main()
