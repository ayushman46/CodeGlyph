"""Round 2 (R2-13): PR text and self-review never overstate what was verified.

* The testing / verification section of every PR body says the listed
  commands were not run by Codebase Intel, whatever the template.
* A changelog entry the PR template or CONTRIBUTING asks for, and the change
  does not add, is a convention gap ("changelog entry not added").
* The self-review must point at the line that produces each string an added
  test asserts, and report a problem when it cannot (checked with a scripted
  model: the prompt, then the parsed reply).
"""

import unittest

from agent import conventions as repo_conventions
from agent.review import REVIEW_SYSTEM, build_review_messages, message_assertions
from test_edit_honesty import MODELS, NAME_FIX, TESTS, EditHonestyCase, edit, plan, run
from test_edit_quality_support import seed_index

NOTE = repo_conventions.TESTS_NOT_RUN_NOTE

# Flask's pull request template: its instructions are HTML comments.
FLASK_TEMPLATE_FULL = """<!--
Before opening a PR, open a ticket describing the issue or feature the
PR will address. An issue is not required for fixing typos in
documentation, or other simple non-code changes.

Replace this comment with a description of the change. Describe how it
addresses the linked ticket.
-->

<!--
Link to relevant issues or previous PRs, one per line. Use "fixes" to
automatically close an issue.

fixes #<issue number>
-->

<!--
Ensure each step in CONTRIBUTING.rst is complete, especially the following:

- Add tests that demonstrate the correct behavior of the change. Tests
  should fail without the change.
- Add or update relevant docs, in the docs folder and in code.
- Add an entry in CHANGES.rst summarizing the change and linking to the
  issue.
- Add `.. versionchanged::` entries in any relevant code docs.
-->
"""
TESTED_TEMPLATE = """## Description

<!-- What does this change? -->

## How Has This Been Tested?

<!-- Commands you ran -->

## Checklist

- [ ] I added a changelog entry
"""
CHANGES = "Version 2.35.0\n--------------\n\nUnreleased\n"
TEMPLATE_PATH = ".github/pull_request_template.md"

# An added test that asserts an exception message.
MESSAGE_TEST = {"old": "def test_post():\n    assert True\n",
                "new": ("def test_post():\n    assert True\n\n\ndef test_bad_field_name_message():\n"
                        "    with pytest.raises(TypeError) as e:\n        prepared_body({object(): 'a'})\n"
                        "    assert \"field names must be str\" in str(e.value)\n")}
MESSAGE_ASSERTION = 'assert "field names must be str" in str(e.value)'


class TestsNotRunNoteTests(unittest.TestCase):
    FILES = [{"file_path": "src/flask/app.py", "action": "modify", "summary": "Name the endpoint"}]

    def test_every_template_shape_says_the_commands_were_not_run(self):
        steps = ["Run `pytest tests/test_basic.py`"]
        generated, _ = repo_conventions.finalize_pr_body("## Summary\n\nDone.", summary="S", files=self.FILES, validation=steps)
        filled, _ = repo_conventions.finalize_pr_body(FLASK_TEMPLATE_FULL, template=FLASK_TEMPLATE_FULL, summary="S",
                                                      files=self.FILES, validation=steps)
        tested, _ = repo_conventions.finalize_pr_body("", template=TESTED_TEMPLATE, summary="S", files=self.FILES,
                                                      validation=steps)
        model_testing = "## Summary\n\nDone.\n\n### Testing\n\n- Added `test_x`; all tests pass locally."
        for body, heading in ((generated, "## How to verify"), (filled, "## How to verify"),
                              (tested, "## How Has This Been Tested?"), (model_testing, "### Testing")):
            noted = repo_conventions.note_tests_not_run(body)
            self.assertIn(f"{heading}\n\n{NOTE}\n\n", noted)
            self.assertEqual(noted.count(NOTE), 1)
            self.assertEqual(repo_conventions.note_tests_not_run(noted), noted)
        # A per-file heading for a test file is not the testing section (live round 2 run).
        per_file = "## Changes\n\n### src/requests/models.py\n\n- Decode keys.\n\n### tests/test_requests.py\n\n- Add tests."
        self.assertEqual(repo_conventions.note_tests_not_run(per_file), f"{per_file}\n\n## How to verify\n\n{NOTE}")
        # A body without a testing section gains one that says so.
        self.assertEqual(repo_conventions.note_tests_not_run("Just a summary."), f"Just a summary.\n\n## How to verify\n\n{NOTE}")


class ChangelogConventionTests(EditHonestyCase):
    def test_the_requirement_is_read_from_comments_and_checklists(self):
        self.assertEqual(repo_conventions.changelog_requirement(FLASK_TEMPLATE_FULL), "CHANGES.rst")
        self.assertEqual(repo_conventions.changelog_requirement(TESTED_TEMPLATE), "")
        self.assertEqual(repo_conventions.changelog_requirement("- [ ] Updated `docs/CHANGELOG.md`"), "docs/CHANGELOG.md")
        for text in ("Do not edit CHANGELOG.md, it is generated on release.", "Summarize the changes below.",
                     "- [ ] I added tests", "Ensure each step in CONTRIBUTING.rst is complete"):
            self.assertIsNone(repo_conventions.changelog_requirement(text), text)
        conventions = {"changelog": {"path": "CHANGES.rst", "source": TEMPLATE_PATH}}
        self.assertEqual(repo_conventions.changelog_gap(conventions, ["src/flask/app.py"]), "CHANGES.rst")
        self.assertEqual(repo_conventions.changelog_gap(conventions, ["src/flask/app.py", "CHANGES.rst"]), "")
        self.assertEqual(repo_conventions.changelog_gap({"changelog": {"path": "", "source": "x"}}, ["HISTORY.md"]), "")
        self.assertEqual(repo_conventions.changelog_gap({}, ["src/flask/app.py"]), "")

    def seed_template(self):
        run(seed_index(self.store, {TEMPLATE_PATH: FLASK_TEMPLATE_FULL, "CHANGES.rst": CHANGES}))
        self.raw[TEMPLATE_PATH] = FLASK_TEMPLATE_FULL.encode()
        self.raw["CHANGES.rst"] = CHANGES.encode()

    def test_a_missing_changelog_entry_is_a_convention_gap(self):
        self.seed_template()
        responses = [plan("src/requests/models.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]}])]
        result = self.edit("Convert non-string form field names in _encode_files", responses)
        check = next(check for check in result.proposal["quality"]["checks"] if check["id"] == "conventions")
        self.assertEqual(check["status"], "warn")
        self.assertIn("Changelog entry not added (.github/pull_request_template.md asks for one in CHANGES.rst)", check["detail"])
        self.assertEqual(check["files"], [{"file_path": "CHANGES.rst", "line": None, "column": None,
                                           "message": "changelog entry not added"}])
        self.assertIn("**Conventions** (warning): ", result.markdown)
        self.assertNotIn("**Conventions** (passed)", result.markdown)
        # The body's testing section says nothing was run.
        self.assertIn(f"## How to verify\n\n{NOTE}\n\n", result.proposal["pr_body"])

    def test_a_change_that_adds_the_entry_passes(self):
        self.seed_template()
        entry = {"old": "Unreleased\n", "new": "Unreleased\n\n-   Convert non-string form field names.\n"}
        responses = [plan("src/requests/models.py", "CHANGES.rst"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]},
                           {"file_path": "CHANGES.rst", "action": "modify", "changes": [entry]}])]
        result = self.edit("Convert non-string form field names in _encode_files and note it in the changelog", responses)
        check = next(check for check in result.proposal["quality"]["checks"] if check["id"] == "conventions")
        self.assertEqual(check["status"], "pass", check)
        self.assertNotIn("changelog", check["detail"].lower())


class MessageAssertionReviewTests(EditHonestyCase):
    def test_message_assertions_are_listed_from_added_test_lines_only(self):
        after = TESTS.replace(MESSAGE_TEST["old"], MESSAGE_TEST["new"])
        found = message_assertions([{"file_path": "tests/test_requests.py", "action": "modify", "before": TESTS, "after": after},
                                    {"file_path": "src/requests/models.py", "action": "modify", "before": MODELS,
                                     "after": MODELS + 'assert "x" in y\n'}])
        self.assertEqual(found, [{"file_path": "tests/test_requests.py", "line": 8, "code": MESSAGE_ASSERTION,
                                  "texts": ["field names must be str"]}])
        self.assertIn("(c) an added test asserts a message or string that no line shown produces", REVIEW_SYSTEM)
        prompt = build_review_messages(request_text="r", conventions_text="", findings=[], related=[],
                                       files=[{"file_path": "tests/test_requests.py", "action": "modify", "before": TESTS,
                                               "after": after}])[1]["content"]
        self.assertIn("Strings the added tests assert. Nothing was run", prompt)
        self.assertIn(f"- tests/test_requests.py:8 {MESSAGE_ASSERTION}", prompt)

    def test_a_scripted_review_that_cannot_find_the_producing_line_is_reported(self):
        review = ('<code_review>{"verification": ["test asserts \'field names must be str\' (tests/test_requests.py:8): '
                  'no line of src/requests/models.py produces it; _encode_files raises nothing: WRONG"], "verdict": "changes", '
                  '"issues": [{"finding": null, "evidence": "' + MESSAGE_ASSERTION.replace('"', '\\"') + '", "severity": "high", '
                  '"file_path": "tests/test_requests.py", "line": 8, "problem": "No line shown produces '
                  '\'field names must be str\'; the patched _encode_files never raises TypeError", "fix": "Assert the '
                  'converted name instead"}], "files": []}</code_review>')
        responses = [plan("src/requests/models.py", "tests/test_requests.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]},
                           {"file_path": "tests/test_requests.py", "action": "modify", "changes": [MESSAGE_TEST]}]),
                     review]
        result = self.edit("Convert non-string form field names in _encode_files and test the error", responses, review=True)
        call = result.model.calls[-1]
        self.assertEqual(call.get("structured_output"), "code_review")
        self.assertIn("5. Every added test assertion that checks a message or string", call["messages"][0]["content"])
        self.assertIn(f"- tests/test_requests.py:8 {MESSAGE_ASSERTION}", call["messages"][1]["content"])
        quality = result.proposal["quality"]
        issue = quality["review_issues"][0]
        self.assertEqual((issue["file_path"], issue["verified"], issue["fixed"]), ("tests/test_requests.py", True, False))
        review_check = next(check for check in quality["checks"] if check["id"] == "review")
        self.assertEqual(review_check["status"], "warn")
        self.assertEqual(quality["status"], "warn")


if __name__ == "__main__":
    unittest.main()
