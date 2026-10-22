"""Cross-owner contracts: proposal.quality/conventions shape, tool_calls, shared helpers and settings."""

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path

import test_edit_quality_data as data
from agent import code_edit, editor, quality
from agent.code_edit import apply_hunks, exact_hunks, reapply_change
from config import Settings
from test_edit_quality_support import B4, KyEditorCase

run = asyncio.run
BACKEND = Path(__file__).resolve().parents[1]
BASE = "line one\nline two\nline three\nline four\nline five\nline six\nline seven\nline eight\n"
EDITED = BASE.replace("line six\n", "line six (edited)\n")


class ReapplyChangeTests(unittest.TestCase):
    def test_an_upstream_change_elsewhere_is_kept_and_the_reviewed_change_reapplied(self):
        latest = "header added upstream\n" + BASE
        self.assertEqual(reapply_change(BASE, EDITED, latest), "header added upstream\n" + EDITED)

    def test_an_upstream_change_to_the_same_lines_is_a_conflict(self):
        latest = BASE.replace("line six\n", "line six (changed upstream)\n")
        self.assertIsNone(reapply_change(BASE, EDITED, latest))

    def test_only_exact_matches_are_reapplied(self):
        # Upstream added trailing whitespace to the hunk's context: apply_hunks
        # alone would match it tolerantly, but a refresh must match exactly.
        latest = BASE.replace("line five\n", "line five   \n")
        self.assertIsNone(reapply_change(BASE, EDITED, latest))
        # The hunk's text now occurs twice upstream: ambiguous, so refused.
        latest = BASE + "line four\nline five\nline six\nline seven\nline eight\n"
        self.assertIsNone(reapply_change(BASE, EDITED, latest))

    def test_crlf_upstream_files_keep_crlf(self):
        latest = ("new first line\n" + BASE).replace("\n", "\r\n")
        merged = reapply_change(BASE, EDITED, latest)
        self.assertEqual(merged, ("new first line\n" + EDITED).replace("\n", "\r\n"))

    def test_trivial_cases(self):
        self.assertEqual(reapply_change(BASE, EDITED, BASE), EDITED)
        self.assertEqual(reapply_change(BASE, BASE, "anything\n"), "anything\n")
        self.assertIsNone(reapply_change(BASE, EDITED, None))


class SharedHelperTests(unittest.TestCase):
    def test_exact_hunks_moved_to_code_edit_and_is_re_exported(self):
        self.assertIs(editor.exact_hunks, code_edit.exact_hunks)
        self.assertEqual(apply_hunks(BASE, exact_hunks(BASE, EDITED)), EDITED)

    def test_check_syntax_contract(self):
        result = quality.check_syntax("a.json", '{"a": 1}\n')
        self.assertEqual(set(result), {"status", "checker", "errors", "detail"})
        many = run(quality.check_syntax_many([{"file_path": "a.json", "after": "{"}, {"file_path": "b.py", "after": "x = 1\n", "before": "x = 0\n"}]))
        self.assertEqual({path: result["status"] for path, result in many.items()}, {"a.json": "fail", "b.py": "pass"})

    def test_quality_and_code_edit_do_not_import_github_or_retrieval(self):
        script = ("import sys; import agent.quality, agent.code_edit; "
                  "bad = [m for m in sys.modules if m == 'github' or m.startswith(('github.', 'retrieval'))]; "
                  "print(','.join(bad)); sys.exit(1 if bad else 0)")
        completed = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True, timeout=60,
                                   env={"PATH": "/usr/bin:/bin", "TURSO_DATABASE_URL": "file:/tmp/unused.db", "NVIDIA_API_KEY": ""})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class TicketSizeTests(unittest.TestCase):
    SECRET = "test-editing-secret-0123456789"

    def test_a_24_file_ticket_fits_the_2000_character_fields(self):
        from agent.code_edit import EditingAuthorizationError, create_edit_ticket, verify_edit_ticket

        paths = [f"packages/features/bookings/lib/handleNewBooking/helpers/module{index:02d}.ts" for index in range(24)]
        ticket = create_edit_ticket(self.SECRET, user_id="u1", repo_name="cal.com", file_path=paths, ttl_seconds=600)
        self.assertTrue(ticket.startswith("v2."))
        self.assertLess(len(ticket), 2000)
        for path in paths:
            verify_edit_ticket(self.SECRET, ticket, user_id="u1", repo_name="cal.com", file_path=path)
        with self.assertRaises(EditingAuthorizationError):
            verify_edit_ticket(self.SECRET, ticket, user_id="u1", repo_name="cal.com", file_path="other.ts")
        version, body, signature = ticket.split(".")
        with self.assertRaises(EditingAuthorizationError):  # a v2 body presented as v1 never verifies
            verify_edit_ticket(self.SECRET, f"v1.{body}.{signature}", user_id="u1", repo_name="cal.com", file_path=paths[0])
        small = create_edit_ticket(self.SECRET, user_id="u1", repo_name="r", file_path=["a.ts", "b.ts"], ttl_seconds=600)
        self.assertTrue(small.startswith("v1."))


class SettingsTests(unittest.TestCase):
    def test_new_limits_and_defaults(self):
        defaults = {name: field.default for name, field in Settings.model_fields.items()}
        expected = {
            "code_editing_answer_max_tokens": 16_384, "editing_context_characters": 240_000,
            "code_editing_timeout_seconds": 300, "editing_self_review": True, "editing_review_max_tokens": 4_096,
            # 45 s: every completed review measured on live edits took 4.8-32.7 s.
            "editing_review_timeout_seconds": 45, "editing_review_deadline_seconds": 240,
            "editing_review_trivial_lines": 8, "editing_deadline_seconds": 300,
            "editing_max_mechanical_files": 20, "github_max_pr_files": 24, "syntax_check_max_bytes": 1_000_000,
            "esbuild_path": "", "editing_max_files": 8,
        }
        self.assertEqual({name: defaults[name] for name in expected}, expected)

    def test_requirements_pin_the_checkers(self):
        requirements = (BACKEND / "requirements.txt").read_text().splitlines()
        for pin in ("PyYAML==6.0.3", "tree-sitter==0.26.0", "tree-sitter-typescript==0.23.2", "tree-sitter-javascript==0.25.0",
                    "tree-sitter-go==0.25.0", "tree-sitter-java==0.23.5", "tree-sitter-rust==0.24.2", "tree-sitter-css==0.25.0",
                    "tree-sitter-c==0.24.2", "tree-sitter-cpp==0.23.4", "tree-sitter-c-sharp==0.23.5", "tree-sitter-ruby==0.23.1",
                    "tree-sitter-php==0.25.1", "tree-sitter-kotlin==1.1.0", "tree-sitter-bash==0.25.1"):
            self.assertIn(pin, requirements)


CHECK_IDS = ["scope", "syntax", "style", "imports", "completeness", "tests", "review", "conventions"]
NEW_TOOLS = ["check_scope", "read_conventions", "check_syntax", "check_style", "check_imports", "check_completeness", "review_patch"]


class ProposalContractTests(KyEditorCase):
    def test_the_proposal_carries_quality_conventions_and_commit_message(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"]], evidence=self.evidence("source/utils/is.ts"))
        proposal = result.proposal
        quality_block = proposal["quality"]
        self.assertEqual(set(quality_block), {"status", "checks", "reviewed", "review_summary", "review_issues", "autofixed", "style"})
        self.assertIn(quality_block["status"], {"pass", "warn", "fail"})
        self.assertEqual([check["id"] for check in quality_block["checks"]], CHECK_IDS)
        for check in quality_block["checks"]:
            self.assertEqual(set(check), {"id", "label", "status", "detail", "files"})
            self.assertIn(check["status"], {"pass", "warn", "fail", "skipped"})
            self.assertLessEqual(len(check["files"]), 20)
            for entry in check["files"]:
                self.assertEqual(set(entry), {"file_path", "line", "column", "message"})
        # A skipped review counts as a warning.
        self.assertEqual(quality_block["status"], "warn")
        self.assertEqual(quality_block["review_summary"], "Self-review skipped: self-review is turned off (EDITING_SELF_REVIEW)")
        for fix in quality_block["autofixed"]:
            self.assertIn(fix["rule"], {"final_newline", "trailing_whitespace", "indentation", "import_extension"})
        profile = quality_block["style"]["test/is.ts"]
        self.assertEqual(set(profile), {"indent", "indent_width", "quotes", "semicolons", "eol", "final_newline", "source"})
        for item in proposal["files"]:
            self.assertEqual(set(item["syntax"]), {"status", "checker", "errors"})
            self.assertIn(item["syntax"]["checker"], {"esbuild", "tree-sitter", "ast", "json", "jsonc", "tomllib", "pyyaml", "none"})
        self.assertEqual(set(proposal["conventions"]), {"pr_template_path", "commit_style", "commit_style_source", "contributing_path",
                                                         "agent_instructions_path", "dco_required", "notes"})
        self.assertLessEqual(len(proposal["commit_message"]), 500)
        self.assertLessEqual(len(proposal["pr_body"]), 20_000)
        tools = [call["tool"] for call in result.tool_calls]
        self.assertEqual([tool for tool in tools if tool in NEW_TOOLS], NEW_TOOLS)
        self.assertEqual(tools[-1], "validate_patch")
        self.assertEqual(self.stages[-1], "checking")


if __name__ == "__main__":
    unittest.main()
