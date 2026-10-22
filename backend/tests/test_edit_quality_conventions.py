"""G6a/G6b: repository conventions and the PR title, body and commit message that follow them."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

import test_edit_quality_data as data
from agent import conventions
from agent.conventions import (
    CONVENTIONAL_TITLE,
    change_type,
    commit_message,
    conventional_title,
    detect_conventions,
    fill_pr_template,
    finalize_pr_body,
    imperative_title,
    untick,
)
from github import github_client
from test_edit_quality_support import B2, KyEditorCase, code_edit

run = asyncio.run
TEMPLATE = ("<!-- Thanks for contributing! -->\n## Description\n<!-- What does this change? -->\n\n"
            "## How Has This Been Tested?\n<!-- Steps to reproduce -->\n\n"
            "## Checklist\n- [x] I have read CONTRIBUTING.md\n- [ ] I have added tests\n")
KY_SUBJECTS = ["Fix retry with `Request` input", "Add `context` option", "Meta tweaks", "Improve types", "2.2.0",
               "Document `retry.jitter`", "Refactor hooks", "Fix stream support detection", "Readme tweaks", "Add test",
               "Tweak error messages", "Require Node.js 22"]


def commits(subjects):
    return [{"sha": f"{index:040d}", "commit": {"message": subject + "\n\nbody"}} for index, subject in enumerate(subjects)]


class HelperTests(unittest.TestCase):
    def test_titles_become_imperative_and_conventional(self):
        self.assertEqual(imperative_title("Adds validation to delay."), "Add validation to delay")
        self.assertEqual(imperative_title("fixed: x"), "Fixed: x")
        self.assertLessEqual(len(imperative_title("Update " + "word " * 40)), 72)
        title = conventional_title("Add validation to delay", "feat")
        self.assertEqual(title, "feat: add validation to delay")
        self.assertRegex(title, CONVENTIONAL_TITLE)
        self.assertEqual(conventional_title("fix(retry): handle 0", "feat"), "fix(retry): handle 0")
        self.assertEqual(conventional_title("CSS tweaks", "style"), "style: CSS tweaks")

    def test_change_types(self):
        source = {"file_path": "src/a.ts", "action": "modify"}
        self.assertEqual(change_type([{"file_path": "test/a.ts", "action": "create"}]), "test")
        self.assertEqual(change_type([{"file_path": "readme.md", "action": "modify"}]), "docs")
        self.assertEqual(change_type([source], renames=[("a", "b")]), "refactor")
        self.assertEqual(change_type([source], question="Fix the crash on empty input"), "fix")
        self.assertEqual(change_type([source], question="Add input validation to delay"), "feat")

    def test_template_filling_maps_sections_and_never_ticks_a_box(self):
        files = [{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Validate ms."}]
        body = fill_pr_template(TEMPLATE, summary="Validate the delay input.", files=files, validation=["npm test"],
                                issue_line="Fixes #12")
        self.assertNotIn("<!--", body)
        self.assertIn("## Description\n\nValidate the delay input.", body)
        self.assertIn("## How Has This Been Tested?\n\n- [ ] npm test", body)
        self.assertIn("## Checklist\n\n- [ ] I have read CONTRIBUTING.md\n- [ ] I have added tests", body)
        self.assertNotIn("[x]", body)
        self.assertTrue(body.startswith("Fixes #12"))
        self.assertEqual(untick("- [X] done\n* [x] also"), "- [ ] done\n* [ ] also")

    def test_bodies_without_a_template_keep_a_how_to_verify_section(self):
        files = [{"file_path": "a.ts", "action": "modify", "summary": "x"}]
        body, source = finalize_pr_body("", summary="S", files=files, validation=["npm test"])
        self.assertEqual(source, "generated")
        self.assertEqual([line for line in body.split("\n") if line.startswith("## ")], ["## Summary", "## Changes", "## How to verify"])
        body, source = finalize_pr_body("## Summary\nDone.", summary="S", files=files, validation=["npm test"])
        self.assertEqual((body, source), ("## Summary\nDone.\n\n## How to verify\n\n- npm test", "verify_added"))
        body, source = finalize_pr_body("## Summary\nDone.\n\n## Testing\nRan it.", summary="S", files=files, validation=["x"])
        self.assertEqual(source, "model")
        long_body, _ = finalize_pr_body("x" * 30_000, summary="S", files=files, validation=["x"])
        self.assertEqual(len(long_body), 20_000)

    def test_commit_messages_list_files_within_500_characters(self):
        files = [{"file_path": f"src/file{index}.ts", "summary": "Rename the helper " * 4} for index in range(20)]
        message = commit_message("refactor: rename helper", files)
        self.assertLessEqual(len(message), 500)
        self.assertTrue(message.startswith("refactor: rename helper\n\n- src/file0.ts: "))
        self.assertIn("more file(s)", message)
        self.assertEqual(commit_message("Fix x", files[:1]), "Fix x")


class CommitHistoryTests(unittest.TestCase):
    def setUp(self):
        conventions.clear_conventions_cache()
        self.addCleanup(conventions.clear_conventions_cache)

    def detect(self, subjects):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=commits(subjects))

        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        async def fetch(_path):
            return None

        with patch.object(github_client, "GITHUB_API_BASE", "https://github.fake.test/api"), \
             patch.object(conventions.httpx, "AsyncClient", client):
            result = run(detect_conventions(owner="o", name="r", repo_id=f"repo-{len(subjects)}-{subjects[0]}", commit_sha="c",
                                            store=None, fetch=fetch, token="token"))
        return result, seen

    def test_mostly_conventional_history_is_conventional(self):
        subjects = [f"fix: thing {index}" for index in range(25)] + [f"Thing {index}" for index in range(5)]
        result, seen = self.detect(subjects)
        self.assertEqual((result["commit_style"], result["commit_style_source"]), ("conventional", "recent commits (25 of 30)"))
        self.assertEqual(seen, ["https://github.fake.test/api/repos/o/r/commits?per_page=30"])

    def test_ky_like_history_is_plain(self):
        result, _ = self.detect(KY_SUBJECTS)
        self.assertEqual((result["commit_style"], result["commit_style_source"]), ("plain", "recent commits (0 of 12)"))


class TemplateRepoTests(KyEditorCase):
    FILES = {**data.KY, ".github/pull_request_template.md": TEMPLATE, "CONTRIBUTING.md":
             "# Contributing\n\nEvery commit needs a Signed-off-by line (DCO).\nRun npm test before opening a pull request.\n"}

    def test_the_pr_body_follows_the_template_with_unticked_boxes(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        proposal = result.proposal
        body = proposal["pr_body"]
        headings = [line for line in body.split("\n") if line.startswith("## ")]
        self.assertEqual(headings, ["## Description", "## How Has This Been Tested?", "## Checklist"])
        tested = body.split("## How Has This Been Tested?")[1].split("## Checklist")[0]
        self.assertIn("- [ ] npm test", tested)
        self.assertNotIn("[x]", body)
        self.assertEqual(proposal["conventions"]["pr_template_path"], ".github/pull_request_template.md")
        self.assertEqual(proposal["conventions"]["contributing_path"], "CONTRIBUTING.md")
        self.assertTrue(proposal["conventions"]["dco_required"])
        self.assertEqual(self.check(proposal, "conventions")["status"], "warn")
        prompt = result.model.calls[0]["messages"][1]["content"]
        self.assertIn("PR template (.github/pull_request_template.md):", prompt)
        self.assertIn("Every commit needs a Signed-off-by line (DCO).", prompt)


class CommitlintRepoTests(KyEditorCase):
    FILES = {**data.KY, "commitlint.config.js": "export default {extends: ['@commitlint/config-conventional']};\n"}

    def test_commitlint_makes_the_title_conventional(self):
        result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        proposal = result.proposal
        self.assertRegex(proposal["title"], CONVENTIONAL_TITLE)
        self.assertEqual(proposal["title"], "feat: add input validation to delay function")
        self.assertEqual(proposal["conventions"]["commit_style"], "conventional")
        self.assertEqual(proposal["conventions"]["commit_style_source"], "commitlint config")
        self.assertEqual(proposal["commit_message"], proposal["title"])
        self.assertIn("Conventional Commits", result.model.calls[0]["messages"][0]["content"])


class PlainRepoTests(KyEditorCase):
    def test_no_template_gives_summary_changes_and_how_to_verify(self):
        reply = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Validate ms",
                            "changes": [{"old": "\treturn new Promise(", "new": "\tif (ms < 0) {\n\t\tthrow new TypeError('ms');\n\t}\n\n\treturn new Promise("}]}],
                          title="Validate delay input", pr_body="")
        result = self.edit(B2, [reply], evidence=self.evidence("source/utils/delay.ts"))
        headings = [line for line in result.proposal["pr_body"].split("\n") if line.startswith("## ")]
        self.assertEqual(headings, ["## Summary", "## Changes", "## How to verify"])
        self.assertIn("- npm test", result.proposal["pr_body"])
        self.assertEqual(result.proposal["conventions"]["commit_style"], "unknown")
        self.assertEqual(result.proposal["conventions"]["agent_instructions_path"], "AGENTS.md")

    def test_a_raw_server_timeout_gives_no_conventions_and_no_error(self):
        async def slow():
            await asyncio.sleep(1.0)
            return b"never"

        self.raw["AGENTS.md"] = slow
        self.raw["package.json"] = slow
        self.raw[".editorconfig"] = slow
        with patch.object(conventions, "CONVENTIONS_BUDGET_SECONDS", 0.2):
            result = self.edit(B2, [data.REPLIES["edit_reply_raw_tabs_delay"]], evidence=self.evidence("source/utils/delay.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(proposal["conventions"]["commit_style"], "unknown")
        self.assertIsNone(proposal["conventions"]["agent_instructions_path"])
        self.assertEqual(self.tool(result, "read_conventions")["status"], "warn")


if __name__ == "__main__":
    unittest.main()
