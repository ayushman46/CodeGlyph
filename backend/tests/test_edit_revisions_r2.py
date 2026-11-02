"""Round 2 (R2-16): revising a specific proposal.

* "Revise: ...", "Revise the proposal ..." and "Update your patch ..." amend
  the latest proposal of the conversation; unrelated requests still do not.
* A request that names an earlier proposal of the conversation by its title
  (quoted or not, any case, small differences allowed) revises that proposal,
  not merely the latest one; only this conversation and user count.
* A revision starts from the revised proposal's files, and PR text that says a
  file "was not provided" or names a function that exists nowhere is removed
  from the PR text and reported (the "PR text" check).
"""

import unittest

import test_answers_and_editing as base
from agent.editor import drop_sentences, previous_proposal_for, title_match, wants_revision
from test_edit_honesty import FALLBACK_FIX, FILE_NAME_FIX, ISSUE, NAME_FIX, NEW_TEST, EditHonestyCase, edit, plan, run
from test_edit_quality_support import seed_index

FIRST_TITLE = "Fix non-string form field names"

# A second test file, changed only by the newer proposal in the "named title" tests.
MULTIPART_TESTS = "def test_multipart():\n    assert True\n"
MULTIPART_TEST = {"old": "def test_multipart():\n    assert True\n",
                  "new": "def test_multipart():\n    assert True\n\n\ndef test_bytes_name():\n    assert body({b'k': 'v'})\n"}


class RevisionCueTests(unittest.TestCase):
    def test_revise_and_update_your_patch_open_a_revision(self):
        for question in ("Revise: fix the bytes-key regression and add a test", "revise: add a test",
                         "Please revise: drop the docs change", "Revise - convert bytes keys too",
                         "Revise the proposal so bytes keys work", "Revise the proposal to add a test",
                         'Revise the proposal "Fix non-string form field names" to add a test',
                         "Amend the patch to use str()", "Rework the patch so bytes keys are decoded",
                         "Update the proposal, it should decode bytes keys", "Update your patch so int keys work",
                         "OK, revise: also handle bytes"):
            self.assertTrue(wants_revision(question), question)

    def test_ordinary_requests_with_these_verbs_are_still_new_requests(self):
        for question in ("Update the patch version to 1.2.3", "Revise the docs to mention timeouts",
                         "Update: the README needs a section on proxies", "Revise the diff algorithm for large files",
                         "Update the diff viewer colors", "Update the patch for large files",
                         "Revise error messages in the CLI", "Revisit the retry logic in adapters.py",
                         "Update the PR template to add a checklist", "Start over: revise the proposal from scratch"):
            self.assertFalse(wants_revision(question), question)


class TitleMatchTests(unittest.TestCase):
    TITLE = "Fix non-string form field names in multipart requests"

    def test_quoted_titles_match_with_any_case_and_small_differences(self):
        for question in (f'Also revise the previous proposal "{self.TITLE}"',
                         'Revise \u201cfix non string form-field names in multipart requests\u201d to add a test',
                         "Add a test to 'Fix non-string form field name in multipart request'",
                         'Revise "fix: non-string form field names in multipart requests"'):
            score, quoted, _rest = title_match(question, self.TITLE)
            self.assertGreaterEqual(score, 0.84, question)
            self.assertTrue(quoted, question)

    def test_unquoted_titles_match_as_a_run_of_words_and_leave_the_rest(self):
        score, quoted, rest = title_match("Revise fix non-string form field name in multipart requests: add a test", self.TITLE)
        self.assertGreaterEqual(score, 0.84)
        self.assertFalse(quoted)
        self.assertEqual(rest, "revise add a test")
        # Unrelated wording and short titles do not match loosely.
        self.assertLess(title_match("Add retries to Session.send", self.TITLE)[0], 0.84)
        self.assertLess(title_match("Fix the forms parser", "Fix the form")[0], 0.84)
        self.assertEqual(title_match("Please fix the form now", "Fix the form")[0], 1.0)


class NamedRevisionTests(EditHonestyCase):
    def setUp(self):
        super().setUp()
        run(seed_index(self.store, {"tests/test_multipart.py": MULTIPART_TESTS}))
        self.raw["tests/test_multipart.py"] = MULTIPART_TESTS.encode()

    def two_proposals(self):
        """Proposal A (models.py and its test), then an unrelated proposal B (only tests/test_multipart.py)."""
        question, first = self.first_proposal()
        history = self.remember(question, first)
        later = "Add a regression test for bytes form field names"
        second = self.edit(later, [plan("tests/test_multipart.py"),
                                   edit([{"file_path": "tests/test_multipart.py", "action": "modify",
                                          "summary": "Bytes names", "changes": [MULTIPART_TEST]}],
                                        title="Add a test for bytes form field names", pr_body="## Summary\n\nA test.")],
                           history=history)
        self.assertIsNone(second.proposal["revision_of"])
        history = self.remember(later, second)
        return first, second, history

    def test_revise_colon_revises_the_latest_proposal_from_its_files(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        revision = "Revise: fix the bytes-key regression and add a test"
        result = self.edit(revision, [plan("src/requests/models.py"),
                                      edit([{"file_path": "src/requests/models.py", "action": "modify",
                                             "summary": "Also convert the filename fallback", "changes": [FALLBACK_FIX]}])],
                           issue=ISSUE, history=history)
        self.assertEqual(result.proposal["revision_of"]["id"], first.proposal["id"])
        # The revised proposal's file is loaded: the model saw models.py with the earlier change.
        prompt = result.model.calls[-1]["messages"][1]["content"]
        self.assertIn("===== FILE: src/requests/models.py", prompt)
        self.assertIn(FILE_NAME_FIX["new"], prompt.split("===== FILE: src/requests/models.py")[1])
        models = next(item for item in result.proposal["files"] if item["file_path"] == "src/requests/models.py")
        for fix in (NAME_FIX, FILE_NAME_FIX, FALLBACK_FIX):
            self.assertIn(fix["new"], models["after"])

    def test_a_title_names_an_earlier_proposal_rather_than_the_latest(self):
        first, second, history = self.two_proposals()
        repo_id = self.repo["id"]
        for question in (f'Also revise the previous proposal "{FIRST_TITLE}"',
                         f'Revise the proposal "{FIRST_TITLE.lower()}" to convert the filename fallback',
                         "Revise fix non string form-field names: convert the filename fallback too",
                         "In the proposal Fix non-string form field name, also convert the fallback"):
            found = run(previous_proposal_for(self.store, repo_id, base.USER, question, history))
            self.assertEqual(found and found["id"], first.proposal["id"], question)
        # Without a title, a revision still amends the latest proposal.
        latest = run(previous_proposal_for(self.store, repo_id, base.USER, "Revise: also test str keys", history))
        self.assertEqual(latest["id"], second.proposal["id"])
        # Repeating a title's wording as a new request, another conversation or another user: no revision.
        self.assertIsNone(run(previous_proposal_for(self.store, repo_id, base.USER, FIRST_TITLE + " in data", history)))
        self.assertIsNone(run(previous_proposal_for(self.store, repo_id, base.USER, f'Revise "{FIRST_TITLE}"', [])))
        self.assertIsNone(run(previous_proposal_for(self.store, repo_id, base.OTHER, f'Revise "{FIRST_TITLE}"', history)))

    def test_the_named_proposal_is_the_one_revised_end_to_end(self):
        first, second, history = self.two_proposals()
        question = f'Also revise the previous proposal "{FIRST_TITLE}": convert the filename fallback too'
        result = self.edit(question, [plan("src/requests/models.py"),
                                      edit([{"file_path": "src/requests/models.py", "action": "modify",
                                             "summary": "Fallback", "changes": [FALLBACK_FIX]}])],
                           issue=ISSUE, history=history)
        proposal = result.proposal
        self.assertEqual(proposal["revision_of"]["id"], first.proposal["id"])
        self.assertNotEqual(proposal["revision_of"]["id"], second.proposal["id"])
        self.assertEqual(sorted(item["file_path"] for item in proposal["files"]),
                         ["src/requests/models.py", "tests/test_requests.py"])
        self.assertTrue(result.markdown.startswith(f'> **Revises the previous proposal "{FIRST_TITLE}".**'))


class UnsupportedClaimTests(EditHonestyCase):
    BODY = ("## Summary\n\nAdd a regression test for bytes keys. Cannot implement the fix because src/requests/models.py "
            "(which contains encode_multipart_data) was not provided in the FILE sections.\n\n"
            "- `_encode_files` keeps bytes names.\n- The helper `encode_multipart_data` now decodes keys.")

    def test_drop_sentences_keeps_headings_code_and_other_sentences(self):
        text = "## Summary\n\nKeep this. Drop that.\n\n```\nDrop that.\n```\n- [ ] Drop that.\n- Keep."
        cleaned, removed = drop_sentences(text, lambda sentence: sentence.startswith("Drop"))
        self.assertEqual(cleaned, "## Summary\n\nKeep this.\n\n```\nDrop that.\n```\n- Keep.")
        self.assertEqual(removed, ["Drop that.", "Drop that."])

    def test_a_missing_file_claim_and_an_invented_function_leave_the_pr_text(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        reply = base.edit_response(
            [{"file_path": "tests/test_requests.py", "action": "modify", "summary": "Add a bytes-key test",
              "changes": [{"old": NEW_TEST["new"], "new": NEW_TEST["new"] + "\n\ndef test_bytes_key():\n    assert True\n"}]}],
            title=FIRST_TITLE, pr_body=self.BODY, summary="Adds a test. Calls encode_multipart_data for bytes keys.",
            validation=["pytest tests/test_requests.py"])
        result = self.edit("Revise: fix the bytes-key regression and add a test",
                           [plan("src/requests/models.py", "tests/test_requests.py"), reply], issue=ISSUE, history=history)
        proposal = result.proposal
        self.assertEqual(proposal["revision_of"]["id"], first.proposal["id"])
        body = proposal["pr_body"]
        self.assertNotIn("FILE sections", body)
        self.assertNotIn("encode_multipart_data", body)
        self.assertIn("Add a regression test for bytes keys.", body)
        self.assertIn("- `_encode_files` keeps bytes names.", body)
        self.assertEqual(proposal["summary"], "Adds a test.")
        check = next(check for check in proposal["quality"]["checks"] if check["id"] == "claims")
        self.assertEqual((check["label"], check["status"]), ("PR text", "warn"))
        messages = [entry["message"] for entry in check["files"]]
        self.assertTrue(any("was not provided in the FILE sections" in message and "src/requests/models.py was given"
                            in message for message in messages), messages)
        self.assertTrue(any("`encode_multipart_data`, which is not in the files given" in message for message in messages))
        self.assertEqual(proposal["quality"]["status"], "warn")
        self.assertIn("**PR text** (warning): Removed 3 statements", result.markdown)

    def test_existing_names_and_ordinary_prose_are_kept(self):
        body = ("## Summary\n\n`_encode_files` now passes `str(k)` to `RequestField`. If a timeout is not provided, "
                "nothing changes. Run `python -m pytest tests` (see `test_int_field_name`). "
                # A -k filter or a quoted string is not a code name (live round 2 run).
                'Run `pytest tests/test_requests.py -k "non_string"` to confirm the new tests, which send "form_name".')
        responses = [plan("src/requests/models.py", "tests/test_requests.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]},
                           {"file_path": "tests/test_requests.py", "action": "modify", "changes": [NEW_TEST]}], pr_body=body)]
        result = self.edit("Fix https://github.com/psf/requests/issues/7634", responses, issue=ISSUE)
        self.assertIn(body.split("\n\n", 1)[1], result.proposal["pr_body"])
        self.assertFalse(any(check["id"] == "claims" for check in result.proposal["quality"]["checks"]))


if __name__ == "__main__":
    unittest.main()
