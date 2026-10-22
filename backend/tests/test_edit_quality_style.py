"""G2: style profiles, whitespace autofix on added lines, style warnings and the prompt's style lines."""

import unittest

import test_edit_quality_data as data
from agent.code_edit import apply_hunks, exact_hunks
from agent.quality import autofix_style, build_repo_style, describe_style, style_profile, style_warnings
from test_edit_quality_support import B2, B4, KyEditorCase, code_edit

DELAY = data.KY["source/utils/delay.ts"]
KY_STYLE = build_repo_style(editorconfig=data.KY_HIDDEN[".editorconfig"], package_json=data.KY["package.json"])
SPACE_GUARD = ("    if (!Number.isFinite(ms) || ms < 0) {   \n"
               "        throw new TypeError('Expected `ms` to be a finite, non-negative number');\n    }\n\n")


class StyleProfileTests(unittest.TestCase):
    def test_ky_profile_comes_from_xo_and_editorconfig(self):
        profile = style_profile("source/utils/delay.ts", DELAY, KY_STYLE)
        self.assertEqual((profile["indent"], profile["quotes"], profile["semicolons"], profile["eol"], profile["final_newline"]),
                         ("tab", "single", "always", "lf", True))
        self.assertEqual(profile["source"], "xo")
        self.assertEqual(describe_style(profile), "tabs, single quotes, semicolons, LF")

    def test_editorconfig_yml_section_applies_to_workflow_files(self):
        profile = style_profile(".github/workflows/main.yml", "name: CI\n", KY_STYLE)
        self.assertEqual((profile["indent"], profile["indent_width"], profile["source"]), ("space", 2, "editorconfig"))
        markdown = style_profile("readme.md", "# ky\n", KY_STYLE)
        self.assertEqual((markdown["indent"], markdown["source"]), ("tab", "editorconfig"))

    def test_prettier_black_and_inference(self):
        prettier = build_repo_style(prettier=(".prettierrc", '{"singleQuote": true, "semi": false, "tabWidth": 4}'))
        profile = style_profile("src/a.ts", "const a = 1\n", prettier)
        self.assertEqual((profile["indent"], profile["indent_width"], profile["quotes"], profile["semicolons"], profile["source"]),
                         ("space", 4, "single", "never", "prettier"))
        black = build_repo_style(pyproject="[tool.black]\nline-length = 100\n")
        self.assertEqual(style_profile("pkg/a.py", "x = 1\n", black)["quotes"], "double")
        inferred = style_profile("src/b.js", "function a() {\n  return 'x';\n}\nconst b = 'y';\nconst c = 'z';\n", {})
        self.assertEqual((inferred["indent"], inferred["indent_width"], inferred["quotes"], inferred["source"]),
                         ("space", 2, "single", "inferred"))


class AutofixTests(unittest.TestCase):
    def test_space_indented_added_lines_become_tabs_and_round_trip(self):
        after = DELAY.replace("): Promise<void> {\n", "): Promise<void> {\n" + SPACE_GUARD, 1)
        profile = style_profile("source/utils/delay.ts", DELAY, KY_STYLE)
        fixed, fixes = autofix_style("source/utils/delay.ts", DELAY, after, profile, created=False)
        self.assertIn("\tif (!Number.isFinite(ms) || ms < 0) {\n\t\tthrow new TypeError", fixed)
        self.assertNotIn("    ", fixed)
        rules = {fix["rule"]: fix["lines"] for fix in fixes}
        self.assertEqual(rules, {"indentation": 3, "trailing_whitespace": 1})
        hunks = exact_hunks(DELAY, fixed)
        self.assertEqual(apply_hunks(DELAY, hunks), fixed)
        # Untouched lines keep their bytes.
        self.assertEqual(fixed.replace(SPACE_GUARD.replace("    ", "\t").replace("{   \n", "{\n"), ""), DELAY)

    def test_created_files_get_a_final_newline_unless_editorconfig_says_no(self):
        profile = style_profile("test/is.ts", None, KY_STYLE)
        fixed, fixes = autofix_style("test/is.ts", "", "import test from 'ava';", profile, created=True)
        self.assertEqual(fixed, "import test from 'ava';\n")
        self.assertEqual(fixes, [{"file_path": "test/is.ts", "rule": "final_newline", "lines": 1}])
        no_newline = build_repo_style(editorconfig="[*]\ninsert_final_newline = false\n")
        fixed, fixes = autofix_style("a.txt", "", "x", style_profile("a.txt", None, no_newline), created=True)
        self.assertEqual((fixed, fixes), ("x", []))

    def test_crlf_files_keep_crlf_on_added_lines_and_untouched_bytes(self):
        before = "def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 2\r\n"
        changed = apply_hunks(before, [{"old": "def b():\n    return 2", "new": "def b():\n\treturn 3  \n\n\ndef c():\n    return 4"}])
        self.assertIn("\r\n", changed)
        profile = style_profile("pkg/m.py", before, {})
        fixed, fixes = autofix_style("pkg/m.py", before, changed, profile, created=False)
        self.assertEqual(fixed, "def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 3\r\n\r\n\r\ndef c():\r\n    return 4\r\n")
        self.assertTrue(fixed.startswith(before.split("def b")[0]))
        self.assertEqual(apply_hunks(before, exact_hunks(before, fixed)), fixed)
        self.assertEqual({fix["rule"] for fix in fixes}, {"indentation", "trailing_whitespace"})

    def test_makefiles_are_never_reindented(self):
        profile = {"indent": "space", "indent_width": 4, "final_newline": True, "_trim": None}
        fixed, fixes = autofix_style("Makefile", "all:\n\techo a\n", "all:\n\techo a\n\techo b\n", profile, created=False)
        self.assertEqual(fixed, "all:\n\techo a\n\techo b\n")
        self.assertEqual(fixes, [])


class WarningTests(unittest.TestCase):
    def test_an_added_double_quoted_string_warns_with_its_line_and_is_not_rewritten(self):
        after = DELAY.replace("\treturn new Promise(", "\tconst message = \"double\";\n\treturn new Promise(", 1)
        profile = style_profile("source/utils/delay.ts", DELAY, KY_STYLE)
        warnings = style_warnings("source/utils/delay.ts", DELAY, after, profile, created=False)
        self.assertEqual([(warning["line"], warning["message"]) for warning in warnings],
                         [(13, "Uses double quotes; this file uses single quotes.")])
        fixed, _fixes = autofix_style("source/utils/delay.ts", DELAY, after, profile, created=False)
        self.assertIn('"double"', fixed)

    def test_a_missing_semicolon_warns_but_continuations_do_not(self):
        after = DELAY.replace("\treturn new Promise(", "\tconst value = ms\n\tconst chained = value\n\t\t.toString();\n\treturn new Promise(", 1)
        profile = style_profile("source/utils/delay.ts", DELAY, KY_STYLE)
        warnings = style_warnings("source/utils/delay.ts", DELAY, after, profile, created=False)
        self.assertEqual([warning["line"] for warning in warnings], [13])


class EditorStyleTests(KyEditorCase):
    def test_the_editor_reindents_space_lines_and_reports_the_fix(self):
        reply = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Validate ms",
                            "changes": [{"old": "): Promise<void> {\n", "new": "): Promise<void> {\n" + SPACE_GUARD}]}],
                          title="Validate the delay input")
        result = self.edit(B2, [reply], evidence=self.evidence("source/utils/delay.ts"))
        item = result.proposal["files"][0]
        self.assertIn("\tif (!Number.isFinite(ms) || ms < 0) {\n\t\tthrow", item["after"])
        self.assertIn({"file_path": "source/utils/delay.ts", "rule": "indentation", "lines": 3}, result.proposal["quality"]["autofixed"])
        self.assertEqual(result.proposal["quality"]["style"]["source/utils/delay.ts"]["indent"], "tab")
        self.assertEqual(apply_hunks(item["before"], item["changes"]), item["after"])

    def test_the_prompt_states_each_files_style_and_shows_a_sibling_for_new_files(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"]], evidence=self.evidence("source/utils/is.ts"))
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("===== FILE: source/utils/is.ts (style: tabs, single quotes, semicolons, LF) =====", prompt)
        self.assertIn("New files you may create: test/is.ts", prompt)
        self.assertIn("===== STYLE EXAMPLE (read-only, for the new file test/is.ts; style: tabs, single quotes, semicolons, LF): test/normalize.ts =====", prompt)
        self.assertIn("import {normalizeRetryMethod, normalizeRetryOptions} from '../source/utils/normalize.js';", prompt)
        self.assertIn("Repository conventions:\n*.ts files: tabs, single quotes, semicolons, LF (from xo)", prompt)
        self.assertIn("From AGENTS.md (coding rules for this repository):", prompt)
        created = next(item for item in result.proposal["files"] if item["file_path"] == "test/is.ts")
        self.assertTrue(created["after"].endswith("});\n"))
        self.assertIn({"file_path": "test/is.ts", "rule": "final_newline", "lines": 1}, result.proposal["quality"]["autofixed"])


if __name__ == "__main__":
    unittest.main()
