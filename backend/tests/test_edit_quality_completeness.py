"""G4: stale literal mentions, impacted tests and missing tests."""

import asyncio
import time
import unittest

import test_answers_and_editing as base
import test_edit_quality_data as data
from agent.completeness import find_impacted_tests, find_stale_mentions
from agent.quality import changed_literals
from test_edit_quality_support import B1, B2, B3, KyEditorCase, code_edit, seed_index

run = asyncio.run
NORMALIZE = data.KY["source/utils/normalize.ts"]
B1_REPLY = code_edit([{"file_path": "source/utils/normalize.ts", "action": "modify", "summary": "Default limit 3",
                       "changes": [{"old": "\tlimit: 2,\n\tmethods: retryMethods,", "new": "\tlimit: 3,\n\tmethods: retryMethods,"}]}],
                     title="Change default retry limit from 2 to 3")
B1_FILE = {"file_path": "source/utils/normalize.ts", "action": "modify", "before": NORMALIZE,
           "after": NORMALIZE.replace("\tlimit: 2,", "\tlimit: 3,", 1)}


class ChangedLiteralTests(unittest.TestCase):
    def test_one_changed_number_next_to_a_key_is_a_changed_literal(self):
        literals = changed_literals(NORMALIZE, B1_FILE["after"])
        self.assertEqual(len(literals), 1)
        self.assertEqual({key: literals[0][key] for key in ("key", "old", "new", "enclosing")},
                         {"key": "limit", "old": "2", "new": "3", "enclosing": "defaultRetryOptions"})
        self.assertEqual(literals[0]["words"], ["default", "retry", "options"])

    def test_test_files_only_match_code_lines_with_a_default_name(self):
        from agent.quality import stale_line_matcher

        matcher = stale_line_matcher(changed_literals(NORMALIZE, B1_FILE["after"])[0])
        lines = ["const defaultRetryCount = 2;", "// default retryCount = 2 (after 2 retries)", "retry: {limit: 2},"]
        self.assertEqual([matcher("test/retry.ts", lines, index) for index in range(3)], [True, False, False])
        self.assertTrue(matcher("readme.md", ["- `limit`: `2`"], 0))

    def test_other_changes_are_not_literals(self):
        self.assertEqual(changed_literals("foo(2);\n", "foo(3);\n"), [])
        self.assertEqual(changed_literals("a: 2, b: 3\n", "a: 3, b: 4\n"), [])
        self.assertEqual(changed_literals("name: 'x'\n", "name: 'y'\n"), [])


class IndexedCompletenessTests(KyEditorCase):
    def test_b1_finds_exactly_the_three_stale_mentions_and_no_explicit_test_values(self):
        findings = run(find_stale_mentions(self.store, base.REPO_ID, [B1_FILE]))
        self.assertEqual([(item["file_path"], item["line"]) for item in findings],
                         [("readme.md", 264), ("source/types/retry.ts", 26), ("test/retry.ts", 121)])
        self.assertEqual(findings[2]["text"], "const defaultRetryCount = 2;")
        # test/retry.ts:222 sets an explicit `limit: 2` and must not be reported.
        self.assertEqual(data.KY["test/retry.ts"].split("\n")[221].strip(), "limit: 2,")

    def test_b1_lists_test_normalize_as_an_impacted_test(self):
        impacted = run(find_impacted_tests(self.store, base.REPO_ID, [B1_FILE]))
        self.assertEqual(impacted, [{"file_path": "test/normalize.ts", "imports": "source/utils/normalize.ts"}])

    def test_the_editor_reports_b1_completeness_and_tests_as_warnings(self):
        result = self.edit(B1, [B1_REPLY], evidence=self.evidence("source/utils/normalize.ts"))
        proposal = result.proposal
        completeness = self.check(proposal, "completeness")
        self.assertEqual(completeness["status"], "warn")
        self.assertEqual([(entry["file_path"], entry["line"]) for entry in completeness["files"]],
                         [("readme.md", 264), ("source/types/retry.ts", 26), ("test/retry.ts", 121)])
        # A line that itself has backticks is quoted with a longer fence, so
        # the reviewer copies "- `limit`: `2`" and not "- `limit`: `2``".
        self.assertTrue(completeness["files"][0]["message"].startswith("Still says `` - `limit`: `2` ``;"),
                        completeness["files"][0]["message"])
        self.assertTrue(completeness["files"][2]["message"].startswith("Still says `const defaultRetryCount = 2;`;"),
                        completeness["files"][2]["message"])
        tests = self.check(proposal, "tests")
        self.assertEqual(tests["status"], "warn")
        self.assertIn("test/normalize.ts", tests["detail"])
        self.assertNotEqual(proposal["quality"]["status"], "pass")
        # Stale mentions are quality warnings, not "incomplete".
        self.assertFalse(proposal["incomplete"])
        summary = self.tool(result, "check_completeness")["result_summary"]
        self.assertIn("readme.md:264", summary)

    def test_b2_without_a_test_warns_that_no_test_was_added(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        tests = self.check(result.proposal, "tests")
        self.assertEqual(tests["status"], "warn")
        self.assertIn("no test was added or updated", tests["detail"])

    def test_b3_rename_has_no_stale_mentions_and_completeness_passes(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}],
                          title="Rename isNonArrayObject to isPlainObjectLike")
        result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"))
        self.assertEqual(self.check(result.proposal, "completeness")["status"], "pass")
        self.assertEqual(self.check(result.proposal, "tests")["status"], "pass")


class LargeIndexPerformanceTests(base.DatabaseTestCase):
    def test_completeness_signals_run_under_half_a_second_on_3000_files(self):
        run(base.seed_repo(self.store))
        files = {f"src/module{index:04d}.ts": (
            f"import {{retry}} from './module{(index + 1) % 3000:04d}.js';\n"
            # Source files that merely hold a name with "default"/"retry" are not stale
            # (only tests match on names); none states `limit: 2`.
            f"export const retryLimit{index} = {{limit: {index % 7 + 10}, defaultRetryDelay: 2}};\n"
            f"export function run{index}() {{\n\treturn retry({index});\n}}\n"
        ) for index in range(3000)}
        files["src/config.ts"] = "export const defaultRetryOptions = {\n\tlimit: 2,\n\tbackoff: 10,\n};\n"
        files["test/config.ts"] = "import {defaultRetryOptions} from '../src/config.js';\nconst defaultRetryCount = 2;\n"
        files["docs/retry.md"] = "The default `limit`: `2` applies to every request.\n"
        run(seed_index(self.store, files))
        after = files["src/config.ts"].replace("limit: 2", "limit: 3")
        changed = [{"file_path": "src/config.ts", "action": "modify", "before": files["src/config.ts"], "after": after}]
        started = time.perf_counter()
        stale = run(find_stale_mentions(self.store, base.REPO_ID, changed))
        impacted = run(find_impacted_tests(self.store, base.REPO_ID, changed))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual([(item["file_path"], item["line"]) for item in stale], [("docs/retry.md", 1), ("test/config.ts", 2)])
        self.assertEqual([item["file_path"] for item in impacted], ["test/config.ts"])


if __name__ == "__main__":
    unittest.main()
