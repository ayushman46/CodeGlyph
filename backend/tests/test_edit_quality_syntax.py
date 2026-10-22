"""G1: syntax validation of every after-file (checkers, baseline rule, editor repair, serialisation)."""

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

import test_edit_quality_data as data
from agent import quality
from agent.quality import check_syntax, check_syntax_many
from test_edit_quality_support import B2, HAS_ESBUILD, HAS_TREE_SITTER, KyEditorCase, code_edit

run = asyncio.run
DELAY = data.KY["source/utils/delay.ts"]
# The prototype's broken brace: the function's closing "}" is removed.
BROKEN_DELAY = DELAY.replace("\t});\n}", "\t});\n", 1)
MODERN = "export default abstract class X<in out T> {}\n"


class CheckerTests(unittest.TestCase):
    @unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
    def test_a_removed_brace_fails_with_esbuild_at_30_1(self):
        result = check_syntax("source/utils/delay.ts", BROKEN_DELAY, before=DELAY)
        self.assertEqual((result["status"], result["checker"]), ("fail", "esbuild"))
        self.assertEqual(result["errors"][0], {"line": 30, "column": 1, "message": "Unexpected end of file"})
        self.assertEqual(quality.syntax_messages("source/utils/delay.ts", result),
                         ["source/utils/delay.ts:30:1 Unexpected end of file (esbuild)"])

    @unittest.skipUnless(HAS_TREE_SITTER, "tree-sitter wheels not installed")
    def test_without_esbuild_tree_sitter_reports_the_missing_brace(self):
        with patch.object(quality, "esbuild_binary", lambda: None):
            result = check_syntax("source/utils/delay.ts", BROKEN_DELAY, before=DELAY)
        self.assertEqual((result["status"], result["checker"]), ("fail", "tree-sitter"))
        self.assertEqual(result["errors"][0]["line"], 28)
        self.assertEqual(result["errors"][0]["message"], "missing }")

    @unittest.skipUnless(HAS_TREE_SITTER, "tree-sitter wheels not installed")
    def test_a_tree_sitter_false_positive_present_before_is_only_a_warning(self):
        with patch.object(quality, "esbuild_binary", lambda: None):
            result = check_syntax("src/x.ts", MODERN, before=MODERN)
        self.assertEqual((result["status"], result["checker"]), ("warn", "tree-sitter"))
        self.assertIn("already failed to parse with tree-sitter before the change", result["detail"])

    @unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
    def test_esbuild_accepts_modern_typescript(self):
        modern = MODERN + "const y = {a: 1} satisfies Record<string, number>;\nclass C { accessor x = 1; }\n"
        self.assertEqual(check_syntax("src/x.ts", modern, before=None)["status"], "pass")

    @unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
    def test_esbuild_rejects_mismatched_jsx_tags(self):
        result = check_syntax("src/a.tsx", "export const A = () => <div>hi</span>;\n")
        self.assertEqual(result["status"], "fail")

    def test_python_missing_colon_fails_with_ast_on_line_1(self):
        result = check_syntax("pkg/mod.py", "def f(x)\n    return x\n", before="def f(x):\n    return x\n")
        self.assertEqual((result["status"], result["checker"]), ("fail", "ast"))
        self.assertEqual(result["errors"][0]["line"], 1)

    def test_yaml_tags_present_before_and_after_warn_instead_of_failing(self):
        before = "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n    Properties:\n      Name: !Ref BucketName\n"
        after = before.replace("BucketName", "OtherName")
        result = check_syntax("template.yml", after, before=before)
        self.assertEqual((result["status"], result["checker"]), ("warn", "pyyaml"))

    def test_tsconfig_with_comments_and_trailing_commas_passes_as_jsonc(self):
        text = '{\n\t// compiler options\n\t"compilerOptions": {"strict": true, /* yes */},\n}\n'
        result = check_syntax("tsconfig.json", text)
        self.assertEqual((result["status"], result["checker"]), ("pass", "jsonc"))
        self.assertEqual(check_syntax("package.json", text)["status"], "fail")

    def test_a_file_over_the_limit_is_skipped_not_passed(self):
        big = "export const value = 1;\n" * 50_000  # 1.2 MB
        self.assertGreater(len(big), 1_000_000)
        result = check_syntax("src/big.ts", big)
        self.assertEqual((result["status"], result["checker"]), ("skipped", "none"))
        self.assertIn("larger than the 1,000,000-byte syntax check limit", result["detail"])

    def test_unknown_file_types_are_skipped(self):
        self.assertEqual(check_syntax("docs/guide.md", "# Title\n")["status"], "skipped")

    def test_created_files_fail_on_any_error_and_toml_reports_its_position(self):
        result = check_syntax("pyproject.toml", "[tool.black\nline-length = 1\n")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["errors"][0]["line"], 1)

    def test_check_syntax_many_never_parses_two_files_at_once(self):
        active = {"now": 0, "max": 0}
        guard = threading.Lock()

        def counting(path, text):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.005)
            with guard:
                active["now"] -= 1
            return "json", []

        items = [{"file_path": f"f{index}.json", "after": "{}"} for index in range(20)]

        async def both():
            return await asyncio.gather(check_syntax_many(items), check_syntax_many(items))

        with patch.object(quality, "_syntax_errors_unlocked", counting):
            first, second = run(both())
        self.assertEqual(active["max"], 1)
        self.assertEqual(len(first), 20)
        self.assertTrue(all(result["status"] == "pass" for result in second.values()))


BROKEN_HUNK = {"old": "\t\t}, ms);\n\t});\n}", "new": "\t\t}, ms);\n\t});"}
GOOD_HUNK = {"old": "\t\t}, ms);\n\t});\n}", "new": "\t\t}, Math.max(0, ms));\n\t});\n}"}


def delay_edit(hunk):
    return code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Clamp the delay", "changes": [hunk]}],
                     title="Clamp the delay")


@unittest.skipUnless(HAS_ESBUILD, "esbuild binary not installed (npm ci in frontend/)")
class EditorSyntaxTests(KyEditorCase):
    def test_a_broken_brace_is_repaired_with_the_exact_error(self):
        result = self.edit(B2, [delay_edit(BROKEN_HUNK), delay_edit(GOOD_HUNK)], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNotNone(result.proposal, result.markdown)
        self.assertEqual(len(result.model.calls), 2)
        repair = result.model.calls[1]["messages"][-1]["content"]
        self.assertIn("source/utils/delay.ts:29:1 Unexpected end of file (esbuild)", repair)
        self.assertIn("YOUR PATCHED source/utils/delay.ts", repair)
        self.assertEqual(self.check(result.proposal, "syntax")["status"], "pass")
        self.assertEqual(result.proposal["files"][0]["syntax"]["status"], "pass")
        self.assertIn("Math.max(0, ms)", result.proposal["files"][0]["after"])
        self.assertEqual(self.stages.count("checking"), 2)

    def test_the_same_broken_reply_twice_keeps_the_proposal_with_syntax_fail(self):
        result = self.edit(B2, [delay_edit(BROKEN_HUNK), delay_edit(BROKEN_HUNK)], evidence=self.evidence("source/utils/delay.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(self.check(proposal, "syntax")["status"], "fail")
        self.assertEqual(proposal["quality"]["status"], "fail")
        syntax = proposal["files"][0]["syntax"]
        self.assertEqual((syntax["status"], syntax["checker"]), ("fail", "esbuild"))
        self.assertEqual(syntax["errors"][0]["message"], "Unexpected end of file")
        self.assertTrue(result.markdown.startswith("> **Needs attention: syntax errors.**"), result.markdown[:200])
        self.assertEqual(self.tool(result, "check_syntax")["status"], "fail")

    def test_an_unusable_repair_keeps_the_first_draft_with_its_failure(self):
        missing = delay_edit({"old": "not in the file", "new": "x"})
        result = self.edit(B2, [delay_edit(BROKEN_HUNK), missing], evidence=self.evidence("source/utils/delay.ts"))
        self.assertIsNotNone(result.proposal, result.markdown)
        self.assertEqual(self.check(result.proposal, "syntax")["status"], "fail")


if __name__ == "__main__":
    unittest.main()
