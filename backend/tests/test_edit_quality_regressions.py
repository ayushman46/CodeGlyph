"""Regressions found by the live evaluation of the edit-quality gates (one test class per finding).

1. The self-review harmed correct code: a redundant `ms === null ||` guard
   from a false claim, "fixed" for a test it never added, false claims in
   the PR body. Its fixes now need a numbered finding or quoted evidence, are
   re-checked, and its prose never reaches the PR.
2. Reviewer persona text leaked into PR descriptions.
3. Duplicate "## Verification" / "## How to verify" sections and garbled
   commit-body lines.
4. A broken test file was labelled "tests: pass".
5. A changed error message left test assertions with the old text, unreported.
6. A rename phrased "Rename the exported type guard X to Y" was not
   recognised; a doomed generation ran for minutes.
8-10. Stray </think> tags, provider-failure logging, the alternate chat model.
"""

import asyncio
import logging
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import test_edit_quality_data as data
from openai import APIStatusError, APITimeoutError

from agent import conventions as repo_conventions
from agent import nemotron
from agent.agent import run_agent_loop, stream_agent_answer
from agent.nemotron import ANSWER_RESTART, AnswerRestart, LLMProviderError, StreamingContentFilter, strip_reasoning, user_facing_content
from agent.quality import changed_string_literals
from agent.review import evidence_snippets, locate_evidence, redundant_guard
from config import Settings, get_alternate_chat_model, get_answer_model_options, settings
from test_edit_quality_support import B1, B2, B3, B4, HAS_ESBUILD, KyEditorCase, code_edit, code_review

REQUEST = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
DELAY_GUARD = "\tif (!Number.isFinite(ms) || ms < 0) {"
B1_REPLY = code_edit([{"file_path": "source/utils/normalize.ts", "action": "modify", "summary": "Default limit 3",
                       "changes": [{"old": "\tlimit: 2,\n\tmethods: retryMethods,", "new": "\tlimit: 3,\n\tmethods: retryMethods,"}]}],
                     title="Change default retry limit from 2 to 3")
LEAKED = ("*This review was performed by a meticulous senior reviewer. The change is ready to merge once the null "
          "check is added and test coverage for the validation is included.*")


def status_error(code):
    return APIStatusError(f"HTTP {code}", response=httpx.Response(code, request=REQUEST), body=None)


def provider_error(cause):
    error = LLMProviderError("NVIDIA could not generate the answer after retries.")
    error.__cause__ = cause
    return error


def review_calls(result):
    return [call for call in result.model.calls if call.get("structured_output") == "code_review"]


def delay_after(result):
    return next(item for item in result.proposal["files"] if item["file_path"] == "source/utils/delay.ts")["after"]


class ConservativeReviewTests(KyEditorCase):
    """1. Only checkable fixes are applied; "fixed" means the change is in the final diff."""

    def test_a_redundant_null_guard_from_a_false_claim_is_not_applied(self):
        # Live, twice: "Number.isFinite(null) coerces null to 0", so the review
        # added `ms === null ||` in front of a condition that already rejects null.
        review = code_review(
            issues=[{"severity": "high", "file_path": "source/utils/delay.ts", "line": 13, "evidence": DELAY_GUARD.strip(),
                     "problem": "null passes the check because Number.isFinite(null) coerces null to 0",
                     "fix": "Add ms === null"}],
            files=[{"file_path": "source/utils/delay.ts", "changes": [
                {"issue": 0, "old": DELAY_GUARD, "new": "\tif (ms === null || !Number.isFinite(ms) || ms < 0) {"}]}],
            pr={"title": "Add input validation to delay function", "body": "Number.isFinite coerces null, so null is rejected explicitly."})
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], review], evidence=self.evidence("source/utils/delay.ts"),
                           review=True)
        self.assertIn(DELAY_GUARD, delay_after(result))
        self.assertNotIn("ms === null", delay_after(result))
        issue = result.proposal["quality"]["review_issues"][0]
        self.assertFalse(issue["fixed"])
        self.assertIn("redundant", issue["note"])
        self.assertNotIn("coerces", result.proposal["pr_body"])
        self.assertEqual(self.check(result.proposal, "review")["status"], "warn")

    def test_a_missing_test_is_never_marked_fixed_without_a_test_in_the_diff(self):
        # Live: "No test exists for the new validation" came back fixed: true
        # because a hunk on delay.ts applied; no test was added.
        review = code_review(
            issues=[{"severity": "medium", "file_path": "source/utils/delay.ts", "line": 13, "finding": "F1",
                     "problem": "No test exists for the new input validation behaviour."}],
            files=[{"file_path": "source/utils/delay.ts", "changes": [
                {"issue": 0, "old": "\t\tthrow new TypeError('Expected `ms` to be a finite, non-negative number');",
                 "new": "\t\tthrow new TypeError('Expected `ms` to be a finite, non-negative number of milliseconds');"}]}])
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], review], evidence=self.evidence("source/utils/delay.ts"),
                           review=True)
        prompt = review_calls(result)[0]["messages"][1]["content"]
        self.assertIn("F1: Source behaviour changed but no test was added or updated", prompt)
        issue = result.proposal["quality"]["review_issues"][0]
        self.assertEqual((issue["fixed"], issue["finding"]), (False, "F1"))
        self.assertIn("F1 was still reported after the fix", issue["note"])
        self.assertNotIn("of milliseconds", delay_after(result))
        tool = self.tool(result, "review_patch")
        self.assertEqual((tool["input"]["issues"], tool["input"]["fixed"]), (1, 0))

    def test_a_finding_resolved_by_another_fix_counts_as_fixed(self):
        # Live (T1): "no test was added" (F4) was resolved by the fix to
        # test/retry.ts that another issue proposed; no hunk of its own.
        review = code_review(
            issues=[{"severity": "high", "file_path": "test/retry.ts", "line": 121, "finding": "F3", "problem": "Old default"},
                    {"severity": "medium", "file_path": "test/retry.ts", "line": 1, "finding": "F5",
                     "problem": "No test reflects the new default"}],
            files=[{"file_path": "test/retry.ts", "changes": [
                {"issue": 0, "old": "const defaultRetryCount = 2;", "new": "const defaultRetryCount = 3;"}]}])
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        prompt = review_calls(result)[0]["messages"][1]["content"]
        self.assertIn("F5: Source behaviour changed but no test was added or updated", prompt)
        issues = result.proposal["quality"]["review_issues"]
        self.assertEqual([(issue["finding"], issue["fixed"]) for issue in issues], [("F3", True), ("F5", True)])
        self.assertEqual(issues[1]["note"], "resolved by another fix in this review")

    def test_a_claim_without_a_finding_or_quoted_code_is_a_suggestion_only(self):
        review = code_review(
            issues=[{"severity": "high", "file_path": "source/utils/delay.ts", "line": 13,
                     "problem": "null is accepted as a valid ms value instead of being reported as an error"}],
            files=[{"file_path": "source/utils/delay.ts", "changes": [
                {"old": "\t\tthrow new TypeError('Expected `ms` to be a finite, non-negative number');",
                 "new": "\t\tthrow new TypeError('Expected `ms` to be a finite number');"}]}])
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"], review], evidence=self.evidence("source/utils/delay.ts"),
                           review=True)
        self.assertIn("finite, non-negative number');", delay_after(result))
        issue = result.proposal["quality"]["review_issues"][0]
        self.assertEqual((issue["fixed"], issue["verified"]), (False, False))
        # A suggestion is listed, not counted as an unfixed problem.
        self.assertEqual(self.check(result.proposal, "review")["status"], "pass")
        self.assertEqual(self.check(result.proposal, "review")["files"], [])

    def test_a_quoted_bug_in_a_new_test_is_fixed_and_counted(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"], data.REPLIES["review_reply_verify_new_test"]],
                           evidence=self.evidence("source/utils/is.ts"), review=True)
        created = next(item for item in result.proposal["files"] if item["file_path"] == "test/is.ts")
        self.assertIn("t.true(isNonNegativeNumber(Infinity));", created["after"])
        issues = result.proposal["quality"]["review_issues"]
        self.assertTrue(all(issue["verified"] and issue["fixed"] for issue in issues), issues)

    def test_reviewer_text_never_reaches_the_pr_and_the_review_prompt_has_no_persona(self):
        review = code_review(verdict="ok", summary="Looks good; ready to merge.",
                             pr={"title": "Review: ready to merge", "body": LEAKED})
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        proposal = result.proposal
        self.assertEqual(proposal["title"], "Change default retry limit from 2 to 3")
        for text in (proposal["pr_body"], proposal["commit_message"], proposal["title"]):
            self.assertNotIn("meticulous", text)
            self.assertNotIn("ready to merge", text)
        self.assertNotIn("ready to merge", proposal["quality"]["review_summary"])
        system = review_calls(result)[0]["messages"][0]["content"]
        self.assertNotIn("senior reviewer", system)
        self.assertNotIn('"pr"', system)

    @unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
    def test_a_fix_for_a_finding_that_breaks_syntax_is_reverted(self):
        review = code_review(files=[
            {"file_path": "source/types/retry.ts", "changes": [{"old": "\t@default 2\n\t*/\n\tlimit?: number | undefined;",
                                                                "new": "\t@default 3\n\t*/\n\tlimit?: number | undefined;;{"}]},
            {"file_path": "readme.md", "changes": [{"old": "- `limit`: `2`", "new": "- `limit`: `3`"}]},
        ], issues=[{"severity": "medium", "file_path": "source/types/retry.ts", "line": 26, "problem": "JSDoc says 2"},
                   {"severity": "medium", "file_path": "readme.md", "line": 264, "problem": "Readme says 2"}])
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        files = {item["file_path"]: item for item in result.proposal["files"]}
        self.assertEqual(sorted(files), ["readme.md", "source/utils/normalize.ts"])
        fixed = {issue["file_path"]: (issue["fixed"], issue["note"]) for issue in result.proposal["quality"]["review_issues"]}
        self.assertEqual(fixed["readme.md"][0], True)
        self.assertEqual(fixed["source/types/retry.ts"], (False, "not applied: it introduced a syntax error"))

    def test_a_fix_that_adds_a_style_problem_is_dropped(self):
        review = code_review(files=[{"file_path": "readme.md", "changes": [{"old": "- `limit`: `2`", "new": "- `limit`: `3`"}]},
                                    {"file_path": "test/retry.ts", "changes": [
                                        {"old": "const defaultRetryCount = 2;", "new": 'const defaultRetryCount = Number("3");'}]}],
                             issues=[{"severity": "medium", "file_path": "readme.md", "line": 264, "problem": "Readme says 2"},
                                     {"severity": "medium", "file_path": "test/retry.ts", "line": 121, "problem": "Old default"}])
        result = self.edit(B1, [B1_REPLY, review], evidence=self.evidence("source/utils/normalize.ts"), review=True)
        notes = {issue["file_path"]: issue["note"] for issue in result.proposal["quality"]["review_issues"]}
        self.assertEqual(notes["test/retry.ts"], "not applied: it introduced a style problem")
        self.assertNotIn("test/retry.ts", [item["file_path"] for item in result.proposal["files"]])


class ReviewSkipTests(KyEditorCase):
    """1/7. Trivial changes skip the review (no model call, no forced warning)."""

    def test_one_small_hunk_with_every_check_passing_skips_the_review(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"),
                           review=True, trivial_lines=8)
        self.assertEqual(review_calls(result), [])
        self.assertNotIn("reviewing", self.stages)
        review = self.check(result.proposal, "review")
        self.assertEqual((review["status"], review.get("intentional")), ("skipped", True))
        self.assertEqual(result.proposal["quality"]["review_summary"],
                         "Self-review skipped: the change is one small hunk in one file and every automatic check passed")
        # Only the advisory tests check warns (no test was added).
        warned = {check["id"] for check in result.proposal["quality"]["checks"] if check["status"] == "warn"}
        self.assertEqual(warned, {"tests"})

    def test_a_pure_rename_by_the_codemod_skips_the_review(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}],
                          title="Rename isNonArrayObject to isPlainObjectLike")
        result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"), review=True, trivial_lines=8)
        self.assertEqual(review_calls(result), [])
        self.assertIn("rename codemod", result.proposal["quality"]["review_summary"])

    def test_stale_references_keep_the_review(self):
        result = self.edit(B1, [B1_REPLY, code_review(verdict="ok")], evidence=self.evidence("source/utils/normalize.ts"),
                           review=True, trivial_lines=8)
        self.assertEqual(len(review_calls(result)), 1)
        self.assertLessEqual(review_calls(result)[0]["timeout"], 45)

    def test_the_review_timeout_default_comes_from_the_measured_latencies(self):
        self.assertEqual(Settings.model_fields["editing_review_timeout_seconds"].default, 45.0)


class PrTextTests(unittest.TestCase):
    """2/3. No reviewer meta text, one verification section, clean commit lines."""

    def test_meta_paragraphs_are_dropped_and_change_text_is_kept(self):
        body = ("## Summary\n\nAdd input validation to `delay`.\n\n## Verification\n\nRun `npm test`.\n\n---\n\n" + LEAKED)
        clean, _ = repo_conventions.finalize_pr_body(body, summary="s", files=[{"file_path": "a.ts", "summary": "x"}],
                                                     validation=["npm test"])
        self.assertNotIn("meticulous", clean)
        self.assertNotIn("---", clean)
        self.assertIn("Add input validation to `delay`.", clean)
        self.assertEqual(repo_conventions.clean_title("Review: ready to merge"), "")
        self.assertEqual(repo_conventions.clean_title("Fix the review page layout"), "Fix the review page layout")

    def test_one_verify_section(self):
        body = ("## Summary\n\nx\n\n## Verification\n\nRun the test suite, especially `test/retry.ts`.\n\n"
                "## How to verify\n\nRun `npm test` and confirm all retry tests pass.")
        clean, _ = repo_conventions.finalize_pr_body(body, summary="s", files=[], validation=["npm test"])
        headings = [line for line in clean.split("\n") if line.startswith("## ")]
        self.assertEqual(headings, ["## Summary", "## Verification"])
        self.assertIn("Run `npm test` and confirm all retry tests pass.", clean)

    def test_template_headings_are_never_merged(self):
        template = "## Description\n\n## Testing\n\n## How to verify\n"
        body = "## Description\n\nx\n\n## Testing\n\n- [ ] a\n\n## How to verify\n\n- [ ] b"
        clean, source = repo_conventions.finalize_pr_body(body, template=template, summary="s", files=[], validation=["a"])
        self.assertEqual(source, "template")
        self.assertEqual([line for line in clean.split("\n") if line.startswith("## ")],
                         ["## Description", "## Testing", "## How to verify"])

    def test_commit_body_lines_are_plain_text(self):
        files = [{"file_path": "source/utils/normalize.ts", "summary": "Change default retry limit from 2 to 3"},
                 {"file_path": "readme.md", "summary": "Change ``2`` to ``3`` on that line"},
                 {"file_path": "source/types/retry.ts", "summary": "Change `@default 2` to `@default 3` in source/types/retry.ts"},
                 {"file_path": "test/retry.ts", "summary": "Updated by the self-review"}]
        message = repo_conventions.commit_message("Change default retry limit from 2 to 3", files)
        self.assertEqual(message.split("\n")[2:], [
            "- source/utils/normalize.ts: Change default retry limit from 2 to 3",
            "- readme.md: Change 2 to 3",
            "- source/types/retry.ts: Change @default 2 to @default 3",
            "- test/retry.ts: Update",
        ])

    def test_test_commands_use_the_repository_package_manager(self):
        self.assertEqual(repo_conventions.normalise_command("Run `pnpm test`.", {"package_manager": "npm"}), "Run `npm test`.")
        self.assertEqual(repo_conventions.normalise_command("Run `pnpm test`.", {"package_manager": ""}), "Run `pnpm test`.")


class EditorPrTextTests(KyEditorCase):
    def test_the_edit_body_is_cleaned_in_the_proposal(self):
        body = ("## Summary\n\nAdd validation.\n\n## Verification\n\nRun `pnpm test`.\n\n## How to verify\n\nRun `npm test`.\n\n"
                "---\n\n" + LEAKED)
        reply = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Add the guard",
                            "changes": [{"old": "): Promise<void> {\n", "new": "): Promise<void> {\n\tif (!Number.isFinite(ms) || ms < 0) {\n\t\tthrow new TypeError('Expected `ms` to be a finite, non-negative number');\n\t}\n\n"}]}],
                          title="Add input validation to delay", pr_body=body)
        result = self.edit(B2, [reply], evidence=self.evidence("source/utils/delay.ts"))
        pr_body = result.proposal["pr_body"]
        self.assertNotIn("meticulous", pr_body)
        self.assertEqual(sum(1 for line in pr_body.split("\n") if line.startswith("## ")), 2)
        # ky has no lockfile but an .npmrc: npm.
        self.assertNotIn("pnpm", pr_body)


class TestsTouchedTests(KyEditorCase):
    """4. The tests check says what it verifies (touched, not run)."""

    def test_a_created_test_with_the_review_skipped_is_not_shown_as_fine(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"], LLMProviderError("HTTP 500")],
                           evidence=self.evidence("source/utils/is.ts"), review=True)
        tests = self.check(result.proposal, "tests")
        self.assertEqual((tests["label"], tests["status"]), ("Tests touched", "warn"))
        self.assertIn("test/is.ts was added", tests["detail"])
        self.assertIn("tests were not run", tests["detail"])
        self.assertIn("the self-review was skipped (the model was unavailable)", tests["detail"])
        self.assertNotIn("A test is part of the change", tests["detail"])
        self.assertNotIn("(passed)", result.markdown.split("Tests touched")[1].split("\n")[0])

    def test_a_reviewed_test_still_says_it_was_not_run(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"], data.REPLIES["review_reply_verify_new_test"]],
                           evidence=self.evidence("source/utils/is.ts"), review=True)
        tests = self.check(result.proposal, "tests")
        self.assertEqual(tests["status"], "pass")
        self.assertIn("tests were not run", tests["detail"])
        self.assertIn("**Tests touched** (ok, not run)", result.markdown)


VALIDATE = (
    "export const validateTimeoutOption = (value: unknown, name: 'timeout' | 'totalTimeout'): void => {\n"
    "\tif (typeof value !== 'number' || value < 0) {\n"
    "\t\tthrow new TypeError(`The \\`${name}\\` option must be a non-negative number or \\`false\\``);\n"
    "\t}\n"
    "};\n"
)
VALIDATION_TEST = (
    "import test from 'ava';\n"
    "import {validateTimeoutOption} from '../source/utils/validate.js';\n\n"
    "test('rejects a negative timeout', t => {\n"
    "\tt.throws(() => {\n"
    "\t\tvalidateTimeoutOption(-1, 'timeout');\n"
    "\t}, {message: 'The `timeout` option must be a non-negative number or `false`'});\n"
    "});\n\n"
    "test('rejects a negative totalTimeout', t => {\n"
    "\tt.throws(() => {\n"
    "\t\tvalidateTimeoutOption(-1, 'totalTimeout');\n"
    "\t}, {message: 'The `totalTimeout` option must be a non-negative number or `false`'});\n"
    "});\n"
)
MESSAGE_REQUEST = ("In source/utils/validate.ts, change the TypeError message thrown by validateTimeoutOption so it says the "
                   "option must be a non-negative number of milliseconds or `false`.")
MESSAGE_REPLY = code_edit([{"file_path": "source/utils/validate.ts", "action": "modify", "summary": "Mention milliseconds",
                            "changes": [{"old": "option must be a non-negative number or", "new": "option must be a non-negative number of milliseconds or"}]}],
                          title="Mention milliseconds in the timeout option error")


class StringLiteralCompletenessTests(KyEditorCase):
    """5. Tests that assert the old text of a changed message are reported (and fixable by the review)."""

    FILES = {**data.KY, "source/utils/validate.ts": VALIDATE, "test/validation.ts": VALIDATION_TEST}

    def test_changed_string_literals(self):
        before, after = VALIDATE, VALIDATE.replace("number or", "number of milliseconds or")
        self.assertEqual([item["needle"] for item in changed_string_literals(before, after, "source/utils/validate.ts")],
                         ["` option must be a non-negative number or `false`"])
        # Short or one-word strings are ignored.
        self.assertEqual(changed_string_literals("x = 'timeout';\n", "x = 'deadline';\n", "a.ts"), [])
        self.assertEqual(changed_string_literals("throw new Error('Bad');\n", "throw new Error('Wrong');\n", "a.ts"), [])

    def test_assertions_with_the_old_message_are_listed(self):
        result = self.edit(MESSAGE_REQUEST, [MESSAGE_REPLY], evidence=self.evidence("source/utils/validate.ts"))
        completeness = self.check(result.proposal, "completeness")
        self.assertEqual(completeness["status"], "warn")
        self.assertEqual([(entry["file_path"], entry["line"]) for entry in completeness["files"]],
                         [("test/validation.ts", 7), ("test/validation.ts", 13)])
        self.assertIn("Still has the old text", completeness["files"][0]["message"])
        self.assertIn("source/utils/validate.ts", completeness["files"][0]["message"])
        self.assertNotEqual(completeness["detail"], "No stale references to the changed code were found")

    def test_the_review_fixes_them_and_the_finding_is_rechecked(self):
        new = "The `{}` option must be a non-negative number of milliseconds or `false`"
        review = code_review(issues=[
            {"severity": "high", "file_path": "test/validation.ts", "line": 7, "finding": "F1", "problem": "Old message"},
            {"severity": "high", "file_path": "test/validation.ts", "line": 13, "finding": "F2", "problem": "Old message"},
        ], files=[{"file_path": "test/validation.ts", "changes": [
            {"issue": 0, "old": "message: 'The `timeout` option must be a non-negative number or `false`'",
             "new": f"message: '{new.format('timeout')}'"},
            {"issue": 1, "old": "message: 'The `totalTimeout` option must be a non-negative number or `false`'",
             "new": f"message: '{new.format('totalTimeout')}'"},
        ]}])
        result = self.edit(MESSAGE_REQUEST, [MESSAGE_REPLY, review], evidence=self.evidence("source/utils/validate.ts"), review=True)
        prompt = review_calls(result)[0]["messages"][1]["content"]
        self.assertIn("F1: test/validation.ts:7 Still has the old text", prompt)
        self.assertIn("===== CURRENT CONTENT: test/validation.ts", prompt)
        self.assertEqual(sorted(item["file_path"] for item in result.proposal["files"]),
                         ["source/utils/validate.ts", "test/validation.ts"])
        self.assertEqual(self.check(result.proposal, "completeness")["status"], "pass")
        self.assertEqual([issue["fixed"] for issue in result.proposal["quality"]["review_issues"]], [True, True])
        test_file = next(item for item in result.proposal["files"] if item["file_path"] == "test/validation.ts")
        self.assertEqual(test_file["summary"], "Update the old message text to match source/utils/validate.ts")


DUPLICATE_TEST = VALIDATION_TEST + (
    "\ntest('rejects a string timeout', t => {\n"
    "\tt.throws(() => {\n"
    "\t\tvalidateTimeoutOption('1', 'timeout');\n"
    "\t}, {message: 'The `timeout` option must be a non-negative number or `false`'});\n"
    "});\n"
)


class RepeatedAssertionTests(KyEditorCase):
    """5. Two identical assertions: each review hunk lands on the line its finding names."""

    FILES = {**data.KY, "source/utils/validate.ts": VALIDATE, "test/validation.ts": DUPLICATE_TEST}

    def test_identical_lines_are_fixed_at_their_finding_lines(self):
        line = "\t}, {message: 'The `timeout` option must be a non-negative number or `false`'});"
        fixed = line.replace("number or", "number of milliseconds or")
        review = code_review(issues=[
            {"severity": "high", "file_path": "test/validation.ts", "line": 7, "finding": "F1", "problem": "Old message"},
            {"severity": "high", "file_path": "test/validation.ts", "line": 19, "finding": "F3", "problem": "Old message"},
        ], files=[{"file_path": "test/validation.ts", "changes": [
            {"issue": 0, "old": line, "new": fixed}, {"issue": 1, "old": line, "new": fixed}]}])
        result = self.edit(MESSAGE_REQUEST, [MESSAGE_REPLY, review], evidence=self.evidence("source/utils/validate.ts"), review=True)
        prompt = review_calls(result)[0]["messages"][1]["content"]
        self.assertIn("F3: test/validation.ts:19 Still has the old text", prompt)
        test_file = next(item for item in result.proposal["files"] if item["file_path"] == "test/validation.ts")
        self.assertEqual(test_file["after"].count("number of milliseconds or"), 2)
        issues = result.proposal["quality"]["review_issues"]
        self.assertEqual([issue["fixed"] for issue in issues], [True, True])
        # The totalTimeout assertion (F2) is still reported: it was not fixed.
        self.assertEqual([(entry["file_path"], entry["line"]) for entry in self.check(result.proposal, "completeness")["files"]],
                         [("test/validation.ts", 13)])

    def test_a_message_other_source_still_throws_is_not_stale(self):
        self.FILES = {**self.FILES}
        other = "export const check = () => {\n\tthrow new TypeError('The `timeout` option must be a non-negative number or `false`');\n};\n"
        from test_edit_quality_support import run, seed_index

        run(seed_index(self.store, {"source/utils/other.ts": other}))
        result = self.edit(MESSAGE_REQUEST, [MESSAGE_REPLY], evidence=self.evidence("source/utils/validate.ts"))
        self.assertEqual(self.check(result.proposal, "completeness")["status"], "pass")


class RenameDetectionTests(unittest.TestCase):
    """6. Any wording between "rename" and the identifier; no false positives on prose."""

    def test_phrasings(self):
        from agent.editor import rename_candidates, rename_targets

        cases = {
            "Rename the exported type guard isHTTPError to isHttpError in source/utils/type-guards.ts and update every file that uses it.":
                [("isHTTPError", "isHttpError")],
            "Rename isHTTPError in source/utils/type-guards.ts to isHttpError": [("isHTTPError", "isHttpError")],
            "rename isFoo → isBar": [("isFoo", "isBar")],
            "rename fooBar -> bazQux": [("fooBar", "bazQux")],
            "Change the name of normalizeRetryOptions to normalizeRetry.": [("normalizeRetryOptions", "normalizeRetry")],
            "Rename fooBar to bazQux and barFoo to quxBaz.": [("fooBar", "bazQux"), ("barFoo", "quxBaz")],
            "rename delay to wait": [("delay", "wait")],
            "rename the function foo_bar as bazQux": [("foo_bar", "bazQux")],
            "Make the button blue": [],
            "rename the button label to Save": [],
            "Please rename it to something clearer": [],
            "Rename the helper used to parse dates to parseDate": [],
            "In source/utils/normalize.ts change the default retry limit from 2 to 3.": [],
            "Rename the file. Then change the limit to 3.": [],
        }
        for question, expected in cases.items():
            with self.subTest(question):
                self.assertEqual(rename_targets(question), expected)
        self.assertEqual(rename_candidates("Rename the exported helper delay to wait")[1], [("delay", "wait")])


class RenameEditorTests(KyEditorCase):
    def test_a_plain_word_is_a_rename_when_the_index_defines_it(self):
        reply = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export default async function delay(", "new": "export default async function wait("}]}],
                          title="Rename delay to wait")
        result = self.edit("Rename the exported helper delay to wait in source/utils/delay.ts", [reply],
                           evidence=self.evidence("source/utils/delay.ts"))
        trace = next(call for call in result.tool_calls if call["tool"] == "trace_symbols")
        self.assertEqual(trace["input"]["symbols"], ["delay"])
        files = {item["file_path"]: item for item in result.proposal["files"]}
        self.assertIn("export default async function wait(", files["source/utils/delay.ts"]["after"])
        # The importer is renamed, but never its module path, an option key or a property.
        self.assertIn("import wait from '../utils/delay.js';", files["source/core/Ky.ts"]["after"])
        for item in files.values():
            self.assertNotIn("utils/wait", item["after"])
            self.assertNotIn("wait:", item["after"])

    def test_the_codemod_keeps_paths_keys_and_prose(self):
        from agent.editor import rename_identifiers

        text = ("import delay from '../utils/delay.js';\nconst options = {limit: 2, delay: 3};\n// wait for the delay\n"
                "await delay(5);\noptions.delay = 1;\nthrow new Error('delay must be positive');\n")
        self.assertEqual(rename_identifiers(text, [("delay", "wait")]),
                         "import wait from '../utils/delay.js';\nconst options = {limit: 2, delay: 3};\n// wait for the delay\n"
                         "await wait(5);\noptions.delay = 1;\nthrow new Error('delay must be positive');\n")
        self.assertEqual(rename_identifiers("test('isHTTPError works', () => isHTTPError(e));", [("isHTTPError", "isHttpError")]),
                         "test('isHttpError works', () => isHttpError(e));")

    def test_an_unrecognised_rename_of_an_export_is_traced_by_the_codemod(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Use the clearer name",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}],
                          title="Use a clearer name for the object guard")
        result = self.edit("In source/utils/is.ts give isNonArrayObject a clearer name, isPlainObjectLike, everywhere",
                           [reply], evidence=self.evidence("source/utils/is.ts"))
        self.assertEqual(len(result.model.calls), 1)
        paths = sorted(item["file_path"] for item in result.proposal["files"])
        self.assertEqual(paths, ["source/core/Ky.ts", "source/utils/is.ts", "source/utils/merge.ts", "source/utils/normalize.ts"])
        for item in result.proposal["files"]:
            self.assertNotIn("isNonArrayObject", item["after"])
        trace = next(call for call in result.tool_calls if call["tool"] == "trace_symbols" and call["input"].get("inferred"))
        self.assertIn("isNonArrayObject → isPlainObjectLike", trace["result_summary"])
        self.assertEqual(self.check(result.proposal, "completeness")["status"], "pass")


API_DOC = "# API\n\n## isNonArrayObject(value)\n\nUse `isNonArrayObject` to check for a plain object.\n"


class RenameDocsTests(KyEditorCase):
    """6. A renamed code identifier is renamed in docs too (within the file limit); prose words are not."""

    FILES = {**data.KY, "docs/api.md": API_DOC}

    def test_doc_mentions_are_renamed_by_the_codemod(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}])
        result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"))
        docs = next(item for item in result.proposal["files"] if item["file_path"] == "docs/api.md")
        self.assertEqual(docs["after"], API_DOC.replace("isNonArrayObject", "isPlainObjectLike"))
        self.assertTrue(docs["mechanical"])
        self.assertEqual(self.check(result.proposal, "completeness")["status"], "pass")
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertNotIn("===== FILE: docs/api.md", prompt)

    def test_docs_beyond_the_file_limit_stay_reported(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}])
        # Four code files use the name and four files is the limit: no room for the doc.
        with patch.object(settings, "editing_max_mechanical_files", 4), patch.object(settings, "editing_max_files", 4):
            result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"))
        self.assertNotIn("docs/api.md", [item["file_path"] for item in result.proposal["files"]])
        completeness = self.check(result.proposal, "completeness")
        self.assertIn(("docs/api.md", "Mentions `isNonArrayObject` in a comment or doc"),
                      [(entry["file_path"], entry["message"]) for entry in completeness["files"]])


class RenameFallbackTests(KyEditorCase):
    """6. A rename never waits for, or fails on, an unusable model reply: the codemod is exact."""

    def test_the_prompt_says_the_codemod_renames(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}])
        result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"))
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("Rename handled automatically: `isNonArrayObject` → `isPlainObjectLike`", prompt)

    def test_invalid_json_for_a_rename_gives_the_codemod_proposal_without_a_second_call(self):
        result = self.edit(B3, ['<code_edit>{"title": "Rename", "files": [{"file_path": "x", } </code_edit>'],
                           evidence=self.evidence("source/utils/is.ts"))
        self.assertEqual(len(result.model.calls), 1)
        paths = sorted(item["file_path"] for item in result.proposal["files"])
        self.assertEqual(paths, ["source/core/Ky.ts", "source/utils/is.ts", "source/utils/merge.ts", "source/utils/normalize.ts"])
        self.assertEqual(result.proposal["title"], "Rename isNonArrayObject to isPlainObjectLike")
        self.assertIn("the exact rename codemod made this change", result.proposal["summary"])
        self.assertEqual(self.tool(result, "rename_codemod")["status"], "warn")

    def test_a_truncated_rename_reply_gives_the_codemod_proposal(self):
        from agent.nemotron import LLMOutputTruncatedError

        result = self.edit(B3, [LLMOutputTruncatedError("cut")], evidence=self.evidence("source/utils/is.ts"))
        self.assertIsNotNone(result.proposal)
        self.assertIn("output limit", result.proposal["summary"])
        for item in result.proposal["files"]:
            self.assertNotIn("isNonArrayObject", item["after"])


class FailFastTests(KyEditorCase):
    """6. A doomed generation is bounded by size, and by the edit deadline."""

    def test_the_output_budget_and_timeout_follow_the_files(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        call = result.model.calls[0]
        # delay.ts is 690 characters: the floor, and a timeout of 30 s + 4,096 / 40 tokens/s.
        self.assertEqual(call["max_tokens"], 4_096)
        self.assertAlmostEqual(call["timeout"], 30 + 4_096 / 40)
        self.assertGreater(call["deadline"], time.monotonic())

    def test_a_truncated_small_reply_stops_with_an_answer_not_minutes_later(self):
        from agent.nemotron import LLMOutputTruncatedError

        result = self.edit(B2, [LLMOutputTruncatedError("cut")], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNone(result.proposal)
        self.assertIn("4,096-token output limit", result.markdown)
        self.assertEqual(len(result.model.calls), 1)

    def test_no_repair_round_is_started_without_time_left(self):
        with patch.object(settings, "editing_deadline_seconds", 10):
            result = self.edit(B2, ["<code_edit>{\"files\": [ broken </code_edit>"], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNone(result.proposal)
        self.assertEqual(len(result.model.calls), 1)
        self.assertIn("no time was left to ask the model to repair it", result.markdown)


class CompleteDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.create = AsyncMock()
        self.client.chat.completions.create = self.create
        for patcher in (patch("agent.nemotron.get_async_client", return_value=self.client),
                        patch("agent.nemotron.require_nvidia_api_key", return_value="test-key"),
                        patch.object(nemotron.nvidia_rate_limiter, "acquire", new_callable=AsyncMock),
                        patch("agent.nemotron.retry_delay", return_value=0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_passed_deadline_makes_no_call_and_fails_as_a_timeout(self):
        with self.assertRaises(LLMProviderError) as raised:
            asyncio.run(nemotron.complete([{"role": "user", "content": "x"}], model="m", fallback_models=["f"],
                                          timeout=100, deadline=time.monotonic() + 1))
        self.create.assert_not_called()
        self.assertIsInstance(raised.exception.__cause__, APITimeoutError)

    def test_each_attempt_gets_at_most_the_time_left(self):
        self.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="<code_edit>{}</code_edit>"),
                                                                finish_reason="stop")])
        asyncio.run(nemotron.complete([{"role": "user", "content": "x"}], model="m", structured_output=True, timeout=300,
                                      deadline=time.monotonic() + 60))
        self.assertLessEqual(self.create.call_args.kwargs["timeout"], 60)


class StrayReasoningTagTests(unittest.TestCase):
    """8. An unpaired closing tag that ends a line ends reasoning; one in prose or code is text."""

    ANSWER = ("Caching lives in `retrieval/cache.py`, which wraps every vector search in a small LRU keyed by "
              "repository and query text so repeated questions skip the database.")

    def test_complete_keeps_only_the_answer_after_a_stray_tag(self):
        doubled = f"{self.ANSWER}\n</think>\n\n{self.ANSWER}"
        self.assertEqual(user_facing_content(doubled), self.ANSWER)
        self.assertEqual(user_facing_content(f"The user asks about caching. I should look.</think>\n\n{self.ANSWER}"), self.ANSWER)
        self.assertEqual(user_facing_content(f"{self.ANSWER}</think>"), self.ANSWER)
        self.assertEqual(strip_reasoning("</analysis>\nAnswer"), "\nAnswer")

    def test_tags_in_prose_or_code_stay(self):
        prose = f"{self.ANSWER} The </think> tag closes a block."
        self.assertEqual(user_facing_content(prose), prose)
        code = f"{self.ANSWER}\nThe filter drops `</think>`\nand keeps the rest."
        self.assertEqual(user_facing_content(code), code)
        fenced = f"{self.ANSWER}\n```\nreply = reply.split('</think>\\n')[-1]\n</think>\n```\nDone."
        self.assertEqual(user_facing_content(fenced), fenced)

    def test_streaming_restarts_when_shown_text_was_reasoning(self):
        text = f"{self.ANSWER}\n</think>\n\n{self.ANSWER}"
        for size in (1, 3, 7, 40, len(text)):
            with self.subTest(size=size):
                content_filter = StreamingContentFilter()
                shown, restarts = "", 0
                for start in range(0, len(text), size):
                    output = content_filter.feed(text[start:start + size])
                    if content_filter.take_restart():
                        shown, restarts = "", restarts + 1
                    shown += output
                shown += content_filter.finish()
                self.assertEqual(shown, user_facing_content(text))
                self.assertLessEqual(restarts, 1)

    def test_stream_complete_yields_a_restart_marker(self):
        chunks = [f"{self.ANSWER}\n", "</think>\n\n", "The answer ", "comes after the tag."]

        class Stream:
            def __init__(self):
                self.items = [MagicMock(choices=[MagicMock(delta=MagicMock(content=text))]) for text in chunks]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.items:
                    raise StopAsyncIteration
                return self.items.pop(0)

            async def close(self):
                return None

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=Stream())

        async def collect():
            return [text async for text in nemotron.stream_complete([{"role": "user", "content": "q"}], model="m")]

        with patch("agent.nemotron.get_async_client", return_value=client), \
             patch("agent.nemotron.require_nvidia_api_key", return_value="k"), \
             patch.object(nemotron.nvidia_rate_limiter, "acquire", new_callable=AsyncMock):
            parts = asyncio.run(collect())
        self.assertIn(ANSWER_RESTART, parts)
        self.assertIsInstance(ANSWER_RESTART, AnswerRestart)
        after = parts[len(parts) - parts[::-1].index(ANSWER_RESTART):]
        self.assertEqual("".join(after), "The answer comes after the tag.")


class ProviderFailureLoggingTests(unittest.TestCase):
    """9. The HTTP status and error class are logged; prompts and keys never are."""

    def test_a_failed_call_logs_status_and_class_only(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=status_error(503))
        secret_prompt = "SECRET-PROMPT-TEXT"
        with patch("agent.nemotron.get_async_client", return_value=client), \
             patch("agent.nemotron.require_nvidia_api_key", return_value="nvapi-SECRET-KEY"), \
             patch.object(nemotron.nvidia_rate_limiter, "acquire", new_callable=AsyncMock), \
             patch("agent.nemotron.retry_delay", return_value=0.0), \
             self.assertLogs("agent.nemotron", level=logging.WARNING) as logs, \
             self.assertRaises(LLMProviderError):
            asyncio.run(nemotron.complete([{"role": "user", "content": secret_prompt}], model="nvidia/m", retry_attempts=2))
        joined = "\n".join(logs.output)
        self.assertIn("model=nvidia/m attempt=1/2 error=APIStatusError http_status=503", joined)
        self.assertIn("attempt=2/2", joined)
        self.assertNotIn(secret_prompt, joined)
        self.assertNotIn("SECRET-KEY", joined)


class AlternateChatModelTests(unittest.TestCase):
    """10. A question whose model fails is tried once on the other configured chat model."""

    def setUp(self):
        for patcher in (patch.object(settings, "nemotron_model", "nvidia/fast"),
                        patch.object(settings, "detailed_nemotron_model", "nvidia/detailed")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_other_model(self):
        self.assertEqual(get_alternate_chat_model("fast"), "nvidia/detailed")
        self.assertEqual(get_alternate_chat_model("detailed"), "nvidia/fast")
        with patch.object(settings, "detailed_nemotron_model", "nvidia/fast"):
            self.assertEqual(get_alternate_chat_model("fast"), "")

    def test_a_non_retryable_failure_tries_the_other_model_once(self):
        model = AsyncMock(side_effect=[provider_error(status_error(400)), "The answer."])
        with patch("agent.agent.complete", model):
            answer, _calls = asyncio.run(run_agent_loop(None, "r", "Where?", "ctx"))
        self.assertEqual(answer, "The answer.")
        self.assertEqual([call.kwargs["model"] for call in model.call_args_list], ["nvidia/fast", "nvidia/detailed"])
        self.assertEqual(model.call_args_list[1].kwargs["retry_attempts"], 1)

    def test_an_authentication_failure_or_an_edit_is_not_retried(self):
        model = AsyncMock(side_effect=provider_error(status_error(401)))
        with patch("agent.agent.complete", model), self.assertRaises(LLMProviderError):
            asyncio.run(run_agent_loop(None, "r", "Where?", "ctx"))
        self.assertEqual(model.call_count, 1)
        model = AsyncMock(side_effect=provider_error(status_error(400)))
        with patch("agent.agent.complete", model), self.assertRaises(LLMProviderError):
            asyncio.run(run_agent_loop(None, "r", "Fix it", "ctx", workflow="editing"))
        self.assertEqual(model.call_count, 1)

    def test_a_stream_that_failed_before_any_text_tries_the_other_model(self):
        calls = []

        def fake_stream(messages, *, model, **kwargs):
            calls.append(model)

            async def generate():
                if model == "nvidia/fast":
                    raise provider_error(status_error(503))
                yield "From the detailed model."
            return generate()

        async def collect():
            return [text async for text in stream_agent_answer("Where?", "ctx", evidence_plan={})]

        with patch("agent.agent.stream_complete", fake_stream):
            parts = asyncio.run(collect())
        self.assertEqual(parts, ["From the detailed model."])
        self.assertEqual(calls, ["nvidia/fast", "nvidia/detailed"])

    def test_an_interrupted_stream_is_not_restarted_on_another_model(self):
        def fake_stream(messages, *, model, **kwargs):
            async def generate():
                yield "Partial"
                raise LLMProviderError("The answer stream was interrupted.")
            return generate()

        async def collect():
            return [text async for text in stream_agent_answer("Where?", "ctx", evidence_plan={})]

        with patch("agent.agent.stream_complete", fake_stream), self.assertRaises(LLMProviderError):
            asyncio.run(collect())


class ReviewHelperTests(unittest.TestCase):
    def test_redundant_guards(self):
        self.assertIn("redundant", redundant_guard(DELAY_GUARD, "\tif (ms === null || !Number.isFinite(ms) || ms < 0) {"))
        self.assertIn("redundant", redundant_guard("if (typeof x !== 'string') {", "if (x == null || typeof x !== 'string') {"))
        self.assertEqual(redundant_guard("if (ms < 0) {", "if (ms === null || ms < 0) {"), "")
        self.assertEqual(redundant_guard("t.false(f(Infinity));", "t.true(f(Infinity));"), "")

    def test_evidence_must_be_in_the_code_shown(self):
        content = "a\n\tt.false(isNonNegativeNumber(Infinity));\n"
        self.assertEqual(locate_evidence({"problem": "t.false(isNonNegativeNumber(Infinity)) is wrong"}, content), 2)
        self.assertIsNone(locate_evidence({"problem": "Number.isFinite(null) coerces null to 0"}, DELAY_GUARD))
        self.assertIsNone(locate_evidence({"problem": "null passes the check"}, DELAY_GUARD))
        self.assertEqual(evidence_snippets({"evidence": "ms"}), [])


class EditingModelDefaultsTests(unittest.TestCase):
    """11. Ultra edits, Super VL is the fallback; retired IDs still remap."""

    def test_defaults(self):
        defaults = Settings.model_fields
        self.assertEqual(defaults["code_editing_model"].default, "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(defaults["code_editing_fallback_model"].default, "nvidia/nemotron-3.5-super-vl-120b-a12b")
        with patch.object(settings, "code_editing_model", defaults["code_editing_model"].default):
            self.assertEqual(get_answer_model_options("fast", "editing")[0], "nvidia/nemotron-3-ultra-550b-a55b")
        with patch.object(settings, "code_editing_model", "qwen/qwen2.5-coder-32b-instruct"):
            self.assertEqual(get_answer_model_options("code")[0], "nvidia/nemotron-3-ultra-550b-a55b")


if __name__ == "__main__":
    unittest.main()
