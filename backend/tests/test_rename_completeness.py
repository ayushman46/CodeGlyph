"""A rename (or removal) of an export must update, or at least report, every
file that still uses it — found live on cal.com, where renaming a helper
changed only its definition and broke six importers."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_answers_and_editing as base
from agent.code_edit import apply_hunks
from agent.editor import EditRequest, exact_hunks, removed_exports, rename_identifiers, rename_targets, run_code_edit
from api.repository_state import fetch_owned_repository
from retrieval.retriever import build_evidence_plan

run = asyncio.run
USER, REPO_NAME = base.USER, base.REPO_NAME

DEFINITION = "export const extractBaseEmail = (email: string) => {\n  return email.split('+')[0];\n};\n"
USER_A = "import { extractBaseEmail } from '../lib/email';\n\nexport const a = (value: string) => extractBaseEmail(value);\n"
USER_B = "import { extractBaseEmail } from '../lib/email';\n\nexport function b(value: string) {\n  return extractBaseEmail(value);\n}\n"

RENAME_DEFINITION = {
    "file_path": "src/lib/email.ts", "action": "modify", "summary": "Rename",
    "changes": [{"old": "export const extractBaseEmail =", "new": "export const getBaseEmail ="}],
}
RENAME_A = {
    "file_path": "src/a.ts", "action": "modify", "summary": "Use new name",
    "changes": [
        {"old": "import { extractBaseEmail } from '../lib/email';", "new": "import { getBaseEmail } from '../lib/email';"},
        {"old": "=> extractBaseEmail(value);", "new": "=> getBaseEmail(value);"},
    ],
}
RENAME_B = {
    "file_path": "src/b.ts", "action": "modify", "summary": "Use new name",
    "changes": [
        {"old": "import { extractBaseEmail } from '../lib/email';", "new": "import { getBaseEmail } from '../lib/email';"},
        {"old": "  return extractBaseEmail(value);", "new": "  return getBaseEmail(value);"},
    ],
}


class HelperTests(unittest.TestCase):
    def test_rename_targets(self):
        self.assertEqual(rename_targets("Rename `extractBaseEmail` to `getBaseEmail` everywhere"), [("extractBaseEmail", "getBaseEmail")])
        self.assertEqual(rename_targets("rename the function foo_bar as bazQux"), [("foo_bar", "bazQux")])
        self.assertEqual(rename_targets("Make the button blue"), [])

    def test_removed_exports(self):
        after = DEFINITION.replace("extractBaseEmail", "getBaseEmail")
        self.assertEqual(removed_exports(DEFINITION, after), {"extractBaseEmail"})
        self.assertEqual(removed_exports(DEFINITION, DEFINITION.replace("split('+')", "split('@')")), set())
        self.assertEqual(removed_exports("def helper():\n    pass\n", "def helper2():\n    pass\n"), {"helper"})
        self.assertEqual(removed_exports("export { one, two as three };\n", "export { one };\n"), {"three"})
        # Local (non-exported) names never count.
        self.assertEqual(removed_exports("function local() {}\n", "function other() {}\n"), set())


class ExactHunkTests(unittest.TestCase):
    def test_hunks_apply_exactly_and_stay_unique(self):
        before = "".join(f"line {index}\nuse(extractBaseEmail)\n" for index in range(30))
        after = rename_identifiers(before, [("extractBaseEmail", "getBaseEmail")])
        hunks = exact_hunks(before, after)
        self.assertIsNotNone(hunks)
        self.assertEqual(apply_hunks(before, hunks), after)

    def test_crlf_and_missing_final_newline_are_preserved(self):
        before = "import { extractBaseEmail } from 'x';\r\nconst a = extractBaseEmail(b);"
        after = rename_identifiers(before, [("extractBaseEmail", "getBaseEmail")])
        self.assertEqual(apply_hunks(before, exact_hunks(before, after)), after)
        self.assertIn("\r\n", after)

    def test_word_boundaries(self):
        self.assertEqual(rename_identifiers("extractBaseEmails extractBaseEmail $extractBaseEmail", [("extractBaseEmail", "x")]),
                         "extractBaseEmails x $extractBaseEmail")


class RenameCompletenessTests(base.DatabaseTestCase):
    def setUp(self):
        super().setUp()
        run(base.seed_repo(self.store))
        self.repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        files = {"src/lib/email.ts": DEFINITION, "src/a.ts": USER_A, "src/b.ts": USER_B}
        run(base.seed_files(self.store, files))
        self.raw = {path: text.encode() for path, text in files.items()}
        self.usage_rows = [
            {"file_path": "src/a.ts", "start_line": 1, "end_line": 3, "content": USER_A},
            {"file_path": "src/b.ts", "start_line": 1, "end_line": 5, "content": USER_B},
        ]

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        return self.raw.get(path)

    async def usages(self, store, repo_id, symbols, *, exclude_paths=None, limit=12, staging_prefix=None):
        excluded = exclude_paths or set()
        return [row for row in self.usage_rows
                if row["file_path"] not in excluded and any(name in row["content"] for name in symbols)]

    def edit(self, question, responses):
        request = EditRequest(
            store=self.store, repo=self.repo, user_id=USER, question=question,
            evidence_chunks=[base.chunk("src/lib/email.ts", 1, DEFINITION.split("\n")[0])],
            evidence_plan=build_evidence_plan(question, "editing"), repo_map_text="",
        )
        model = base.ScriptedModel(responses)
        with patch("agent.editor.complete", model), patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", AsyncMock(return_value=None)), \
             patch("agent.editor.dependency_file_paths", AsyncMock(return_value=[])), \
             patch("agent.editor.find_symbol_usages", self.usages):
            markdown, tool_calls, proposal = run(run_code_edit(request))
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model)

    def test_rename_request_puts_every_usage_in_front_of_the_model(self):
        complete = base.edit_response([RENAME_DEFINITION, RENAME_A, RENAME_B])
        result = self.edit("In src/lib/email.ts rename `extractBaseEmail` to `getBaseEmail` and update every usage", [complete])
        prompt = result.model.calls[0]["messages"][1]["content"]
        for path in ("src/lib/email.ts", "src/a.ts", "src/b.ts"):
            self.assertIn(f"===== FILE: {path}", prompt)
        self.assertEqual(sorted(item["file_path"] for item in result.proposal["files"]), ["src/a.ts", "src/b.ts", "src/lib/email.ts"])
        self.assertFalse(result.proposal["incomplete"])
        self.assertEqual(result.proposal["unchanged_files"], [])

    def test_definition_only_rename_is_repaired_with_the_importers(self):
        # Not phrased as "rename X to Y", so the importers are only found by
        # the post-validation check; the repair round must receive them.
        definition_only = base.edit_response([RENAME_DEFINITION])
        complete = base.edit_response([RENAME_DEFINITION, RENAME_A, RENAME_B])
        result = self.edit("In src/lib/email.ts call the helper getBaseEmail instead of extractBaseEmail", [definition_only, complete])
        repair = result.model.calls[1]["messages"][-1]["content"]
        self.assertIn("still reference it", repair)
        self.assertIn("===== FILE: src/a.ts", repair)
        self.assertIn("===== FILE: src/b.ts", repair)
        self.assertEqual(len(result.proposal["files"]), 3)
        self.assertFalse(result.proposal["incomplete"])

    def test_importers_left_unchanged_are_reported_not_hidden(self):
        definition_only = base.edit_response([RENAME_DEFINITION])
        result = self.edit("In src/lib/email.ts call the helper getBaseEmail instead of extractBaseEmail", [definition_only, definition_only])
        self.assertTrue(result.proposal["incomplete"])
        reported = {item["file_path"]: item["reason"] for item in result.proposal["unchanged_files"]}
        self.assertEqual(sorted(reported), ["src/a.ts", "src/b.ts"])
        self.assertIn("extractBaseEmail", reported["src/a.ts"])
        self.assertIn("still reference renamed or removed names", result.tool_calls[-1]["result_summary"])

    def test_rename_survives_model_hunks_that_miss(self):
        # The model copies a hunk that does not exist in src/b.ts (as happens
        # with partially shown large files); the rename still completes.
        wrong_b = {**RENAME_B, "changes": [{"old": "return extractBaseEmail(value)  // not in file", "new": "x"}]}
        result = self.edit("In src/lib/email.ts rename `extractBaseEmail` to `getBaseEmail` and update every usage",
                           [base.edit_response([RENAME_DEFINITION, RENAME_A, wrong_b])])
        by_path = {item["file_path"]: item for item in result.proposal["files"]}
        self.assertEqual(sorted(by_path), ["src/a.ts", "src/b.ts", "src/lib/email.ts"])
        for item in by_path.values():
            self.assertNotIn("extractBaseEmail", item["after"])
            self.assertIn("getBaseEmail", item["after"])
        self.assertFalse(result.proposal["incomplete"])

    def test_unrelated_edit_does_not_trigger_usage_checks(self):
        tweak = base.edit_response([{
            "file_path": "src/lib/email.ts", "action": "modify", "summary": "Split on @",
            "changes": [{"old": "email.split('+')[0]", "new": "email.split('@')[0]"}],
        }])
        result = self.edit("In src/lib/email.ts split on @ instead of +", [tweak])
        self.assertEqual(len(result.model.calls), 1)
        self.assertFalse(result.proposal["incomplete"])


if __name__ == "__main__":
    unittest.main()
