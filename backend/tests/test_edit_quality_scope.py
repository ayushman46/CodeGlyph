"""G7: exact usage counts, mechanical renames, split plans, definition anchoring and index coverage."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_answers_and_editing as base
from agent.conventions import clear_conventions_cache
from agent.editor import EditRequest, run_code_edit
from agent.scope import requested_names, split_plan, symbol_reference_files
from api.repository_state import fetch_owned_repository
from config import settings
from retrieval.retriever import build_evidence_plan
from test_edit_quality_support import B3, KyEditorCase, assert_hunk_invariant, chunk_rows, code_edit, seed_index

run = asyncio.run
DEFINITION = "export function fooHelper(value: string): string {\n\treturn value.trim();\n}\n"


def big_repository(importers: int) -> dict[str, str]:
    files = {"src/lib/foo.ts": DEFINITION}
    for index in range(importers):
        files[f"src/features/f{index:02d}/use.ts"] = (
            "import {fooHelper} from '../../lib/foo.js';\n\n"
            f"export const use{index} = (value: string): string => fooHelper(value);\n"
        )
    for index in range(20):  # mentioned only in comments: not a usage
        files[f"src/misc/m{index:02d}.ts"] = f"// fooHelper used to live here.\nexport const m{index} = {index};\n"
    for index in range(20):  # mentioned in docs: not a usage
        files[f"docs/d{index:02d}.md"] = f"Call `fooHelper` to trim input ({index}).\n"
    for index in range(3000 - len(files)):
        files[f"src/generated/g{index:04d}.ts"] = f"export const g{index} = {index};\n"
    return files


class BigRepositoryCase(base.DatabaseTestCase):
    IMPORTERS = 12

    def setUp(self):
        super().setUp()
        clear_conventions_cache()
        self.addCleanup(clear_conventions_cache)
        run(base.seed_repo(self.store))
        self.files = big_repository(self.IMPORTERS)
        run(seed_index(self.store, self.files))
        self.repo = run(fetch_owned_repository(self.store, base.USER, base.REPO_NAME))

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        text = self.files.get(path)
        return text.encode() if text is not None else None

    def edit(self, question, responses, evidence=None):
        request = EditRequest(
            store=self.store, repo=self.repo, user_id=base.USER, question=question,
            evidence_chunks=evidence if evidence is not None else chunk_rows("src/lib/foo.ts", DEFINITION),
            evidence_plan=build_evidence_plan(question, "editing"), repo_map_text="",
        )
        model = base.ScriptedModel(responses)
        with patch("agent.editor.complete", model), patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", AsyncMock(return_value=None)), \
             patch.object(settings, "editing_self_review", False):
            markdown, tool_calls, proposal = run(run_code_edit(request))
        assert_hunk_invariant(self, proposal)
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model)


RENAME = "In src/lib/foo.ts rename fooHelper to barHelper and update every usage."
DEFINITION_ONLY = code_edit([{"file_path": "src/lib/foo.ts", "action": "modify", "summary": "Rename",
                              "changes": [{"old": "export function fooHelper(", "new": "export function barHelper("}]}],
                            title="Rename fooHelper to barHelper")


class RenameAtScaleTests(BigRepositoryCase):
    def test_usages_are_counted_exactly_and_comment_or_doc_mentions_are_not_usages(self):
        references = run(symbol_reference_files(self.store, base.REPO_ID, ["fooHelper"]))["fooHelper"]
        self.assertEqual(sum(1 for kind in references.values() if kind == "code"), 13)
        self.assertEqual(sum(1 for kind in references.values() if kind == "comment"), 20)
        self.assertEqual(sum(1 for kind in references.values() if kind == "doc"), 20)

    def test_a_13_file_rename_edits_8_files_with_the_model_and_5_with_the_codemod(self):
        result = self.edit(RENAME, [DEFINITION_ONLY])
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(len(proposal["files"]), 13)
        self.assertEqual(sum(1 for item in proposal["files"] if item.get("mechanical")), 5)
        self.assertEqual(proposal["files"][0]["file_path"], "src/lib/foo.ts")
        for item in proposal["files"]:
            self.assertNotRegex(item["after"], r"\bfooHelper\b")
            self.assertIn("barHelper", item["after"])
        scope = next(call for call in result.tool_calls if call["tool"] == "check_scope")
        self.assertEqual((scope["input"]["files"], scope["input"]["mechanical"], scope["input"]["symbols"]),
                         (13, 5, {"fooHelper": 13}))
        self.assertEqual(scope["result_summary"], "13 files: 8 edited by the model, 5 by the rename codemod")
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertEqual(prompt.count("===== FILE: "), 8)
        self.assertEqual(KyEditorCase.check(proposal, "imports")["status"], "pass")
        self.assertEqual(KyEditorCase.check(proposal, "syntax")["status"], "pass")
        # Comment and doc mentions are reported, not edited.
        related = KyEditorCase.check(proposal, "completeness")
        self.assertEqual(related["status"], "warn")
        self.assertTrue(any("comment or doc" in entry["message"] for entry in related["files"]))


class TooLargeRenameTests(BigRepositoryCase):
    IMPORTERS = 30

    def test_a_31_file_rename_returns_a_four_part_plan_without_a_model_call(self):
        result = self.edit(RENAME, [])
        self.assertIsNone(result.proposal)
        self.assertEqual(result.model.calls, [])
        self.assertIn("too large for one reviewable proposal", result.markdown)
        scope = next(call for call in result.tool_calls if call["tool"] == "check_scope")
        parts = scope["input"]["parts"]
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(len(part["files"]) <= 8 for part in parts))
        self.assertEqual(parts[0]["files"][0], "src/lib/foo.ts")
        self.assertEqual(sum(len(part["files"]) for part in parts), 31)
        self.assertEqual(scope["input"]["files"], 31)
        self.assertEqual(scope["status"], "fail")
        self.assertEqual(scope["result_summary"], "Too large: 31 files (limit 8); split into 4 parts")
        for part in parts:
            # A part must not itself trigger another whole-repository rename.
            self.assertNotRegex(part["prompt"], r"(?i)\brename\b")
        self.assertIn("deprecated alias", parts[0]["prompt"])
        self.assertIn("merge after part 3", parts[3]["prompt"])


class AnchoringTests(BigRepositoryCase):
    IMPORTERS = 0

    def setUp(self):
        super().setUp()
        extra = {"src/core/retry-limit.ts": "export function retryLimit(attempt: number): number {\n\treturn attempt - 1;\n}\n"}
        extra.update({f"test/t{index:02d}.ts": f"// retryLimit case {index}\nexport const t{index} = 'retryLimit';\n" for index in range(50)})
        self.files.update(extra)
        run(base.seed_files(self.store, {}, extra_chunks=[row for path, text in extra.items() for row in chunk_rows(path, text)]))

    def test_the_defining_file_leads_the_candidates(self):
        question = "Update the `retryLimit` function so it never returns a negative number."
        evidence = [row for path in sorted(self.files) if path.startswith("test/t") for row in chunk_rows(path, self.files[path])][:20]
        plan = base.plan_response(files_to_modify=["src/core/retry-limit.ts"], files_to_create=[], symbols_to_trace=[], approach="Clamp.")
        reply = code_edit([{"file_path": "src/core/retry-limit.ts", "action": "modify", "summary": "Clamp",
                            "changes": [{"old": "\treturn attempt - 1;", "new": "\treturn Math.max(0, attempt - 1);"}]}])
        result = self.edit(question, [plan, reply], evidence=evidence)
        prompt = result.model.calls[0]["messages"][1]["content"]
        ranked = prompt.split("Most relevant files (ranked):\n", 1)[1]
        self.assertTrue(ranked.startswith("### src/core/retry-limit.ts"), ranked[:200])
        self.assertIsNotNone(result.proposal, result.markdown)

    def test_an_unknown_name_stops_before_any_model_call(self):
        result = self.edit("Rename `doesNotExist` to x", [])
        self.assertIsNone(result.proposal)
        self.assertEqual(result.model.calls, [])
        indexed = len(self.files)
        self.assertIn(f"`doesNotExist` does not appear in the {indexed:,} indexed files. Check the name, or re-index if it was added recently.",
                      result.markdown)


class CoverageWarningTests(KyEditorCase):
    def setUp(self):
        super().setUp()
        run(self.store.execute("UPDATE repo_snapshots SET selection_json = ? WHERE repo_id = ?",
                               ['{"budget_omitted_files": 120}', base.REPO_ID]))

    def test_a_rename_in_a_partly_indexed_repository_warns_about_unchecked_usages(self):
        reply = code_edit([{"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename",
                            "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]}])
        result = self.edit(B3, [reply], evidence=self.evidence("source/utils/is.ts"))
        scope = self.check(result.proposal, "scope")
        self.assertEqual(scope["status"], "warn")
        self.assertEqual(scope["detail"], "120 files of this repository are not indexed, so usages there were not checked.")
        self.assertEqual(self.tool(result, "check_scope")["status"], "warn")


class HelperTests(unittest.TestCase):
    def test_requested_names(self):
        anchors, must = requested_names("Update the `retryLimit` function in the retry module", [])
        self.assertEqual((anchors, must), (["retryLimit"], ["retryLimit"]))
        anchors, must = requested_names("Rename `doesNotExist` to x", [])
        self.assertEqual(must, ["doesNotExist"])
        anchors, must = requested_names("Add a new `formatDate` helper in src/utils.ts", [])
        self.assertEqual(must, [])
        anchors, must = requested_names("rename the button label to Save", [])
        self.assertEqual(must, [])

    def test_split_plan_for_a_generic_change(self):
        files = [f"pkg{index % 3}/file{index}.py" for index in range(10)]
        parts = split_plan("Use the new logger everywhere.", files, definitions=["pkg0/file0.py"], limit=4)
        self.assertEqual([len(part["files"]) for part in parts], [4, 4, 2])
        self.assertEqual(parts[0]["files"][0], "pkg0/file0.py")
        self.assertTrue(parts[1]["prompt"].startswith("Part 2 of 3 (merge after part 1): Use the new logger everywhere."))


if __name__ == "__main__":
    unittest.main()
