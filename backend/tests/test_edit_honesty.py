"""Edit & PR honesty (BUG-08, BUG-16) and revisions that build on the previous proposal (FEAT-13).

* Nothing is run, so a proposal never claims "checks passed" or "No problems
  found" without saying so: ``proposal.verification`` names the repository's
  own test command and every reproduction case the issue quotes, which also
  become "How to verify" steps.
* The PR body never stays only a template's HTML comments, and it closes the
  issue with GitHub's exact syntax ("Fixes #N", not "Fixes issue #N").
* A follow-up Edit & PR request in the same conversation starts from the
  previous proposal's files and keeps its hunks; every file is diffed against
  the repository again, so blob SHAs and tickets still cover the final change.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_answers_and_editing as base
from agent import conventions as repo_conventions
from agent.code_edit import apply_hunks, git_blob_sha
from agent.conventions import clear_conventions_cache
from agent.editor import EditRequest, format_proposal_markdown, run_code_edit, wants_revision
from agent.quality import case_covered, reproduction_cases
from agent.review import NO_PROBLEMS_FOUND, build_review_messages
from api.query_router import get_conversation_history, persist_proposal, save_message
from api.repository_state import fetch_owned_repository
from config import settings
from retrieval.retriever import build_evidence_plan
from test_edit_quality_support import assert_hunk_invariant, seed_index

run = asyncio.run

# psf/requests#7634, as GitHub returns it (shortened system information).
ISSUE_BODY = """A non-string key in `data` works for a urlencoded body, but the same `data` fails once `files` is added and the body becomes multipart. A non-string key in `files` fails the same way.

## Expected Result

The request prepares in both cases (the urlencoded path already accepts `{1: "a"}` and sends `1=a`), or a `TypeError` that names the field.

## Actual Result

```
AttributeError: 'int' object has no attribute 'translate'
```

raised from inside urllib3. `_encode_files` passes the field name straight to `RequestField(name=k, ...)` (`models.py:245`), which expects a `str`.

## Reproduction Steps

```python
import requests

requests.Request("POST", "http://x", data={1: "a"}).prepare()
# works

requests.Request("POST", "http://x", data={1: "a"}, files={"f": b"x"}).prepare()
# AttributeError: 'int' object has no attribute 'translate'

requests.Request("POST", "http://x", files={1: b"x"}).prepare()
# AttributeError: 'int' object has no attribute 'translate'
```

## System Information

    $ python -m requests.help

```json
{
  "requests": {
    "version": "2.34.2"
  }
}
```
"""
ISSUE = {"number": 7634, "title": "Non-string form field names raise AttributeError when files are attached",
         "body": ISSUE_BODY, "state": "open", "labels": [], "html_url": "https://github.com/psf/requests/issues/7634"}
CASES = [
    ('requests.Request("POST", "http://x", data={1: "a"}).prepare()', "works"),
    ('requests.Request("POST", "http://x", data={1: "a"}, files={"f": b"x"}).prepare()',
     "AttributeError: 'int' object has no attribute 'translate'"),
    ('requests.Request("POST", "http://x", files={1: b"x"}).prepare()', "AttributeError: 'int' object has no attribute 'translate'"),
]

MODELS = """def _encode_files(files, data):
    new_fields = []
    for field, val in data.items():
        new_fields.append(
            (
                field.decode("utf-8") if isinstance(field, bytes) else field,
                val,
            )
        )

    for k, v in files.items():
        fn = guess_filename(v) or k
        rf = RequestField(name=k, data=v, filename=fn)
        new_fields.append(rf)
    return new_fields
"""
TESTS = """def test_post():
    assert True
"""
MAKEFILE = "init:\n\tpython -m pip install -r requirements-dev.txt\ntest:\n\tpython -m pytest tests\n\nci:\n\tpython -m pytest tests --junitxml=report.xml\n"
FILES = {"src/requests/models.py": MODELS, "tests/test_requests.py": TESTS, "Makefile": MAKEFILE}

NAME_FIX = {"old": 'field.decode("utf-8") if isinstance(field, bytes) else field,',
            "new": 'field.decode("utf-8") if isinstance(field, bytes) else str(field),'}
FILE_NAME_FIX = {"old": "rf = RequestField(name=k, data=v, filename=fn)", "new": "rf = RequestField(name=str(k), data=v, filename=fn)"}
FALLBACK_FIX = {"old": "fn = guess_filename(v) or k", "new": "fn = guess_filename(v) or str(k)"}
NEW_TEST = {"old": "def test_post():\n    assert True\n",
            "new": "def test_post():\n    assert True\n\n\ndef test_int_field_name():\n    assert b'name=\"1\"' in prepared_body({1: 'a'})\n"}

FLASK_TEMPLATE = """<!--
Before opening a PR, open a ticket describing the issue or feature the
PR will address.

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

- Add tests that demonstrate the correct behavior of the change.
-->
"""
HTTPX_TEMPLATE = """<!-- Thanks for contributing to HTTPX! -->

# Summary

<!-- Write a small summary about what is happening here. -->

# Checklist

- [ ] I understand that this PR may be closed in case there was no previous discussion.
- [ ] I've added a test for each change that was introduced.
"""


def plan(*paths: str) -> str:
    return base.plan_response(files_to_modify=list(paths), files_to_create=[], symbols_to_trace=[], approach="")


def edit(files: list[dict], *, title: str = "Fix non-string form field names", pr_body: str = "## Summary\nConvert names.",
         validation: list[str] | None = None) -> str:
    return base.edit_response(files, title=title, pr_body=pr_body,
                              validation=validation if validation is not None else ["Prepare a multipart request with an int field name"])


def review_ok() -> str:
    return '<code_review>{"verification": ["each case traced"], "verdict": "ok", "issues": [], "files": []}</code_review>'


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class ReproductionCaseTests(unittest.TestCase):
    def test_every_case_of_the_issue_with_its_reported_result(self):
        cases = reproduction_cases(ISSUE_BODY)
        self.assertEqual([(case["code"], case["note"]) for case in cases], CASES)
        # Output, data and version listings are not cases.
        self.assertFalse(any("translate'" == case["code"][-10:] or "version" in case["code"] for case in cases))

    def test_repl_transcripts_scripts_shell_and_inline_calls(self):
        repl = "```pycon\n>>> import requests\n>>> requests.get('http://x', timeout=-1)\nTraceback (most recent call last):\nValueError: bad\n```"
        self.assertEqual(reproduction_cases(repl), [{"code": "requests.get('http://x', timeout=-1)", "note": "ValueError: bad"}])
        script = "```js\nimport ky from 'ky';\nconst api = ky.create({prefixUrl: 'x'});\nawait api.get('a'); // throws\n```"
        self.assertEqual(reproduction_cases(script), [{"code": "const api = ky.create({prefixUrl: 'x'});\nawait api.get('a');", "note": "throws"}])
        shell = "```console\n$ pip install requests\n$ curl -s 'http://localhost:8000/a?x=1'\n{\"a\": 1}\n```"
        self.assertEqual([case["code"] for case in reproduction_cases(shell)], ["curl -s 'http://localhost:8000/a?x=1'"])
        floor = "```python\nresult = total // 2  # wrong when total is odd\n```"
        self.assertEqual(reproduction_cases(floor), [{"code": "result = total // 2", "note": "wrong when total is odd"}])
        inline = "Calling `foo.bar(1, 'a')` crashes, while `RequestField(name=k, ...)` and `str()` are fine."
        self.assertEqual([case["code"] for case in reproduction_cases(inline)], ["foo.bar(1, 'a')"])
        self.assertEqual(reproduction_cases("No code here."), [])
        # pallets/flask#6178: a call the issue only mentions is not a case.
        mention = ("`tests/test_basic.py::test_werkzeug_passthrough_errors` mocks `werkzeug.serving.run_simple` and stores "
                   "`kwargs.get(\"passthrough_errors\")` in `rv`, but nothing reads `rv` afterwards.")
        self.assertEqual(reproduction_cases(mention), [])

    def test_a_step_that_already_runs_a_case_covers_it(self):
        step = "python -c \"import requests; requests.Request('POST', 'http://x', files={1: b'x'}).prepare(); print('ok')\""
        self.assertTrue(case_covered(CASES[2][0], [step]))
        self.assertFalse(case_covered(CASES[1][0], [step]))


class TestSetupTests(unittest.TestCase):
    def test_the_repository_says_how_its_tests_run(self):
        setup = repo_conventions.test_setup
        self.assertEqual(setup({"Makefile": MAKEFILE}), ("python -m pytest tests", "the test target in the Makefile", "pytest"))
        self.assertEqual(setup({"Makefile": "test:\n\t$(PYTHON) -m pytest\n"})[0], "make test")
        self.assertEqual(setup({"tox.ini": "[tox]\nenvlist = py3\n\n[testenv]\ndeps = x\ncommands =\n    pytest {posargs:tests}\n"}),
                         ("pytest tests", "the commands in tox.ini", "pytest"))
        self.assertEqual(setup({"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"})[0], "pytest")
        self.assertEqual(setup({}, package={"scripts": {"test": "xo && ava"}}, manager="npm")[0], "npm test")
        self.assertEqual(setup({}, package={"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}), ("", "", ""))
        self.assertEqual(setup({"go.mod": "module x\n"})[0], "go test ./...")

    def test_the_command_is_narrowed_to_the_changed_tests(self):
        conventions = {"test_command": "python -m pytest tests", "test_runner": "pytest"}
        self.assertEqual(repo_conventions.targeted_test_command(conventions, ["tests/test_requests.py"]),
                         "python -m pytest tests/test_requests.py")
        self.assertEqual(repo_conventions.targeted_test_command(conventions, []), "python -m pytest tests")
        node = {"test_command": "npm test", "test_runner": "node"}
        self.assertEqual(repo_conventions.targeted_test_command(node, ["test/is.ts"]), "npm test")
        go = {"test_command": "go test ./...", "test_runner": "go"}
        self.assertEqual(repo_conventions.targeted_test_command(go, ["pkg/api/handler_test.go"]), "go test ./pkg/api")


class PullRequestBodyTests(unittest.TestCase):
    FILES = [{"file_path": "src/flask/app.py", "action": "modify", "summary": "Name the endpoint in the error"}]

    def test_a_body_that_is_only_template_comments_is_filled(self):
        body, source = repo_conventions.finalize_pr_body(
            FLASK_TEMPLATE, template=FLASK_TEMPLATE, summary="Name the endpoint when a view returns None.",
            files=self.FILES, validation=["pytest tests/test_basic.py"], issue_line="Fixes #5812",
        )
        self.assertEqual(source, "template_filled")
        self.assertNotIn("<!--", body)
        self.assertTrue(body.startswith("Fixes #5812\n\n"))
        self.assertIn("Name the endpoint when a view returns None.", body)
        self.assertIn("`src/flask/app.py`: Name the endpoint in the error", body)
        self.assertIn("## How to verify\n\n- pytest tests/test_basic.py", body)

    def test_a_template_following_body_gets_its_empty_sections_and_the_steps(self):
        model_body = ("# Summary\n\n<!-- Write a small summary about what is happening here. -->\n\n# Checklist\n\n"
                      "- [x] I understand that this PR may be closed in case there was no previous discussion.\n"
                      "- [ ] I've added a test for each change that was introduced.")
        body, source = repo_conventions.finalize_pr_body(
            model_body, template=HTTPX_TEMPLATE, summary="Close the transport on error.", files=self.FILES,
            validation=["pytest tests/test_client.py"],
        )
        self.assertEqual(source, "template")
        self.assertIn("# Summary\n\nClose the transport on error.", body)
        self.assertIn("## How to verify\n\n- pytest tests/test_client.py", body)
        self.assertNotIn("[x]", body)
        self.assertNotIn("<!--", body)
        # The template's headings are kept, in order.
        headings = [line for line in body.split("\n") if line.startswith("#")]
        self.assertEqual(headings, ["# Summary", "# Checklist", "## How to verify"])

    def test_filling_a_template_without_a_testing_section_adds_the_steps_before_the_checklist(self):
        body = repo_conventions.fill_pr_template(HTTPX_TEMPLATE, summary="S.", files=self.FILES, validation=["pytest"],
                                                 issue_line="Fixes #3")
        self.assertEqual([line for line in body.split("\n") if line.startswith("#")], ["# Summary", "## How to verify", "# Checklist"])

    def test_closing_references_use_githubs_exact_syntax(self):
        ensure = repo_conventions.ensure_issue_reference
        self.assertEqual(ensure("## Summary\n\nFixes GitHub issue #7634 where names fail.", "#7634", closes=True),
                         "## Summary\n\nFixes #7634 where names fail.")
        self.assertEqual(ensure("Fixes issue #7634.", "#7634", closes=True), "Fixes #7634.")
        self.assertEqual(ensure("Closes: #7634", "#7634", closes=True), "Closes: #7634")
        # A mention without a closing keyword closes nothing.
        self.assertEqual(ensure("See #7634 for context.", "#7634", closes=True), "Fixes #7634\n\nSee #7634 for context.")
        self.assertEqual(ensure("Fixes issue #76345.", "#7634", closes=True), "Fixes #7634\n\nFixes issue #76345.")
        self.assertEqual(ensure("Fixes psf/requests#7634", "psf/requests#7634", closes=True), "Fixes psf/requests#7634")
        self.assertEqual(ensure("Body.", "#12", closes=False), "Refs #12\n\nBody.")
        self.assertEqual(ensure("Related to #12.", "#12", closes=False), "Related to #12.")
        self.assertTrue(repo_conventions.closes_reference("resolved #9", "#9"))
        self.assertFalse(repo_conventions.closes_reference("fixes issue #9", "#9"))

    def test_a_long_title_is_not_cut_inside_a_parenthesis(self):
        # The live revision title ended in "... encoding (including int".
        title = "Fix non-string form field names in multipart encoding (including int keys used as the filename fallback)"
        self.assertEqual(repo_conventions.imperative_title(title), "Fix non-string form field names in multipart encoding")
        self.assertEqual(repo_conventions.imperative_title("Fix (a) and (b)"), "Fix (a) and (b)")


class RevisionIntentTests(unittest.TestCase):
    def test_follow_ups_amend_and_start_over_does_not(self):
        for question in ("also handle the case where the key is an int", "Also cover the filename fallback",
                         "Now add a test for it", "Drop the test change", "Revise the patch to use str()",
                         "handle int keys in the files loop too", "Keep the data loop change but rename the helper"):
            self.assertTrue(wants_revision(question), question)
        for question in ("Fix https://github.com/psf/requests/issues/7634", "Add a timeout option to Session.send",
                         "Start over: also convert the keys", "Make a new proposal that also converts keys"):
            self.assertFalse(wants_revision(question), question)


class ReviewPromptTests(unittest.TestCase):
    def test_the_review_traces_each_case_and_a_clean_review_says_it_ran_nothing(self):
        files = [{"file_path": "src/requests/models.py", "action": "modify", "before": MODELS,
                  "after": MODELS.replace(NAME_FIX["old"], NAME_FIX["new"])}]
        cases = [{"code": code, "note": note} for code, note in CASES]
        prompt = build_review_messages(request_text="Fix #7634", conventions_text="", findings=[], files=files,
                                       related=[], cases=cases)[1]["content"]
        self.assertIn("Reproduction cases quoted in the issue. Nothing was run", prompt)
        self.assertIn(f"- {CASES[2][0]}  (reported: {CASES[2][1]})", prompt)
        self.assertIn("nothing was run", NO_PROBLEMS_FOUND)
        self.assertNotIn("Reproduction cases", build_review_messages(request_text="x", conventions_text="", findings=[],
                                                                     files=files, related=[])[1]["content"])


# ---------------------------------------------------------------------------
# The editor end to end (scripted model, requests-like repository)
# ---------------------------------------------------------------------------


class EditHonestyCase(base.DatabaseTestCase):
    GITHUB_URL = "https://github.com/psf/requests"

    def setUp(self):
        super().setUp()
        clear_conventions_cache()
        self.addCleanup(clear_conventions_cache)
        run(base.seed_repo(self.store, github_url=self.GITHUB_URL))
        run(seed_index(self.store, FILES))
        self.repo = run(fetch_owned_repository(self.store, base.USER, base.REPO_NAME))
        self.raw = {path: text.encode() for path, text in FILES.items()}

    async def fake_fetch(self, owner, repo, path, *, ref="HEAD", max_bytes):
        return self.raw.get(path)

    def edit(self, question: str, responses: list, *, issue=None, history=None, review=False, user=base.USER):
        request = EditRequest(
            store=self.store, repo=self.repo, user_id=user, question=question,
            evidence_chunks=[base.chunk("src/requests/models.py", 1, MODELS)],
            evidence_plan=build_evidence_plan(question, "editing"), repo_map_text="",
            conversation_history=list(history or []),
        )
        model = base.ScriptedModel(responses)
        with patch("agent.editor.complete", model), patch("agent.editor.fetch_raw_file", self.fake_fetch), \
             patch("agent.editor.fetch_issue", AsyncMock(return_value=issue)), \
             patch("agent.editor.dependency_file_paths", AsyncMock(return_value=[])), \
             patch("agent.editor.find_symbol_usages", AsyncMock(return_value=[])), \
             patch.object(settings, "editing_self_review", review):
            markdown, tool_calls, proposal = run(run_code_edit(request))
        assert_hunk_invariant(self, proposal)
        return SimpleNamespace(markdown=markdown, tool_calls=tool_calls, proposal=proposal, model=model)

    def remember(self, question: str, result, *, user=base.USER) -> list[dict]:
        """Store the turn the way the query router does, and return the conversation history it would pass next."""
        async def save():
            await save_message(self.store, repo_id=self.repo["id"], user_id=user, role="user", content=question)
            calls = list(result.tool_calls)
            if result.proposal:
                await persist_proposal(self.store, result.proposal, repo=self.repo, repo_name=base.REPO_NAME, user_id=user)
                calls.append({"tool": "code_edit", "input": {"proposal_id": result.proposal["id"]},
                              "result_summary": result.proposal["title"]})
            await save_message(self.store, repo_id=self.repo["id"], user_id=user, role="assistant", content=result.markdown,
                               tool_calls=calls, mode="editing")
            return await get_conversation_history(self.store, self.repo["id"], user)
        return run(save())

    def first_proposal(self, *, review=False):
        responses = [plan("src/requests/models.py", "tests/test_requests.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "summary": "Convert names",
                            "changes": [NAME_FIX, FILE_NAME_FIX]},
                           {"file_path": "tests/test_requests.py", "action": "modify", "summary": "Add a test",
                            "changes": [NEW_TEST]}],
                          pr_body="## Summary\n\nFixes GitHub issue #7634 where non-string names fail.")]
        if review:
            responses.append(review_ok())
        question = "Fix https://github.com/psf/requests/issues/7634"
        result = self.edit(question, responses, issue=ISSUE, review=review)
        return question, result


class VerificationTests(EditHonestyCase):
    def test_nothing_run_is_said_with_the_command_and_every_issue_case(self):
        _question, result = self.first_proposal(review=True)
        proposal = result.proposal
        verification = proposal["verification"]
        self.assertFalse(verification["tests_run"])
        self.assertEqual(verification["command"], "python -m pytest tests/test_requests.py")
        self.assertEqual(verification["suite_command"], "python -m pytest tests")
        self.assertEqual(verification["command_source"], "the test target in the Makefile")
        self.assertEqual(verification["test_files"], ["tests/test_requests.py"])
        self.assertEqual([(case["code"], case["note"]) for case in verification["cases"]], CASES)
        # "How to verify": the test run first, the model's step, then every case.
        steps = proposal["validation"]
        self.assertEqual(steps[0], "Run `python -m pytest tests/test_requests.py`, then the whole suite with `python -m pytest tests`")
        self.assertEqual(steps[1], "Prepare a multipart request with an int field name")
        for code, note in CASES:
            self.assertIn(f"Check the case from #7634: `{code}` (the issue reports: {note})", steps)
        self.assertIn('files={1: b"x"}', "\n".join(steps))
        # The review traced the cases and its clean verdict is qualified everywhere.
        review_prompt = result.model.calls[-1]["messages"][1]["content"]
        self.assertIn(CASES[2][0], review_prompt)
        self.assertEqual(proposal["quality"]["review_summary"], NO_PROBLEMS_FOUND)
        review_call = next(call for call in result.tool_calls if call["tool"] == "review_patch")
        self.assertEqual(review_call["result_summary"], NO_PROBLEMS_FOUND)
        markdown = result.markdown
        self.assertNotIn("No problems found\n", markdown + "\n")
        self.assertIn("## Static checks (nothing was run)", markdown)
        self.assertIn("## Tests not run\n\nNothing in this proposal was executed. Run `python -m pytest tests/test_requests.py` "
                      "(from the test target in the Makefile) before merging.", markdown)
        self.assertIn(f"- Check the case from #7634: `{CASES[2][0]}`", markdown)
        # The PR body closes the issue with GitHub's syntax and lists the cases.
        body = proposal["pr_body"]
        self.assertIn("Fixes #7634 where non-string names fail.", body)
        self.assertNotIn("issue #7634", body)
        self.assertIn(f"- Check the case from #7634: `{CASES[1][0]}`", body)

    def test_a_case_the_model_already_runs_is_not_repeated_and_no_command_means_saying_so(self):
        self.raw.pop("Makefile")
        covered = "python -c \"import requests; requests.Request('POST', 'http://x', files={1: b'x'}).prepare()\""
        responses = [plan("src/requests/models.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]}],
                          validation=[covered])]
        result = self.edit("Fix https://github.com/psf/requests/issues/7634", responses, issue=ISSUE)
        verification = result.proposal["verification"]
        self.assertEqual(verification["command"], "")
        self.assertEqual([case["covered"] for case in verification["cases"]], [False, False, True])
        steps = result.proposal["validation"]
        self.assertEqual(steps[0], covered)
        self.assertEqual(len(steps), 3)
        self.assertIn("no test command was found in the repository's setup files", result.markdown)

    def test_a_step_that_already_runs_the_tests_is_not_doubled(self):
        responses = [plan("src/requests/models.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [NAME_FIX]}],
                          validation=["python -m pytest tests -k multipart"])]
        result = self.edit("Fix https://github.com/psf/requests/issues/7634", responses, issue=ISSUE)
        self.assertEqual(result.proposal["verification"]["command"], "python -m pytest tests")
        self.assertEqual(result.proposal["validation"][0], "python -m pytest tests -k multipart")
        self.assertEqual(len(result.proposal["validation"]), 1 + len(CASES))

    def test_a_small_change_is_still_reviewed_when_the_issue_quotes_cases(self):
        responses = [plan("src/requests/models.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [FALLBACK_FIX]}]),
                     review_ok()]
        with patch.object(settings, "editing_review_trivial_lines", 8):
            result = self.edit("Fix https://github.com/psf/requests/issues/7634", responses, issue=ISSUE, review=True)
        self.assertTrue(result.proposal["quality"]["reviewed"])
        self.assertEqual(len(result.model.calls), 3)


class RevisionTests(EditHonestyCase):
    REVISION = "also handle the case where the key is an int in the filename fallback"

    def revise(self, history, *, responses=None, question=None):
        responses = responses or [plan("src/requests/models.py"),
                                  edit([{"file_path": "src/requests/models.py", "action": "modify",
                                         "summary": "Also convert the filename fallback", "changes": [FALLBACK_FIX]}],
                                       title="Convert non-string form field names and file names", pr_body="## Summary\n\nAll names.")]
        return self.edit(question or self.REVISION, responses, issue=ISSUE, history=history)

    def test_a_follow_up_starts_from_the_previous_proposal_and_keeps_its_hunks(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        result = self.revise(history)
        proposal = result.proposal
        revision = proposal["revision_of"]
        self.assertEqual(revision["id"], first.proposal["id"])
        self.assertEqual(revision["title"], first.proposal["title"])
        self.assertEqual(revision["kept_files"], ["src/requests/models.py", "tests/test_requests.py"])
        self.assertEqual((revision["dropped_files"], revision["not_reapplied"], revision["added_files"]), ([], [], []))
        by_path = {item["file_path"]: item for item in proposal["files"]}
        models = by_path["src/requests/models.py"]
        # Diffed against the repository: the earlier hunks and the new one.
        self.assertEqual(models["before"], MODELS)
        self.assertEqual(models["base_blob_sha"], git_blob_sha(MODELS.encode()))
        for fix in (NAME_FIX, FILE_NAME_FIX, FALLBACK_FIX):
            self.assertIn(fix["new"], models["after"])
        self.assertEqual(apply_hunks(MODELS, models["changes"]), models["after"])
        # The test file the model did not touch keeps its earlier change.
        self.assertEqual(by_path["tests/test_requests.py"]["after"], first.proposal["files"][1]["after"])
        self.assertEqual(by_path["tests/test_requests.py"]["before"], TESTS)
        # The model saw the previous result and its diff, and the issue is carried over.
        prompt = result.model.calls[-1]["messages"][1]["content"]
        self.assertIn("Revision request: " + self.REVISION, prompt)
        self.assertIn("ALREADY CONTAIN the previous proposal's changes", prompt)
        self.assertIn("+                field.decode(\"utf-8\") if isinstance(field, bytes) else str(field),", prompt)
        self.assertIn("rf = RequestField(name=str(k), data=v, filename=fn)\n", prompt.split("===== FILE: src/requests/models.py")[1])
        self.assertEqual(proposal["issue_reference"], "7634")
        self.assertIn("Fixes #7634", proposal["pr_body"])
        self.assertTrue(result.markdown.startswith("> **Revises the previous proposal \"Fix non-string form field names\".**"))
        call = next(call for call in result.tool_calls if call["tool"] == "revise_proposal")
        self.assertEqual(call["status"], "ok")

    def test_a_request_to_drop_an_earlier_change_removes_that_file(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        revert = {"old": NEW_TEST["new"], "new": NEW_TEST["old"]}
        responses = [plan("src/requests/models.py", "tests/test_requests.py"),
                     edit([{"file_path": "tests/test_requests.py", "action": "modify", "changes": [revert]}])]
        result = self.revise(history, responses=responses, question="Drop the test change, keep the rest")
        self.assertEqual([item["file_path"] for item in result.proposal["files"]], ["src/requests/models.py"])
        self.assertEqual(result.proposal["revision_of"]["dropped_files"], ["tests/test_requests.py"])
        self.assertIn("the earlier change was dropped, as asked", result.markdown)

    def test_an_earlier_change_that_no_longer_applies_is_reported(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        # Upstream rewrote the lines the earlier test change touched.
        self.raw["tests/test_requests.py"] = b"def test_post():\n    assert 1 == 1\n"
        result = self.revise(history)
        revision = result.proposal["revision_of"]
        self.assertEqual(revision["not_reapplied"][0]["file_path"], "tests/test_requests.py")
        self.assertIn("no longer applies", revision["not_reapplied"][0]["reason"])
        self.assertEqual([item["file_path"] for item in result.proposal["files"]], ["src/requests/models.py"])
        call = next(call for call in result.tool_calls if call["tool"] == "revise_proposal")
        self.assertEqual(call["status"], "warn")

    def test_only_this_conversation_and_this_user_are_revised(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        fresh = [plan("src/requests/models.py"),
                 edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [FALLBACK_FIX]}])]
        # A new conversation (its history does not hold the proposal).
        self.assertIsNone(self.revise([], responses=list(fresh)).proposal["revision_of"])
        # "Start over" asks for a separate change.
        result = self.revise(history, responses=list(fresh), question="Start over: also convert the filename fallback")
        self.assertIsNone(result.proposal["revision_of"])
        self.assertEqual(result.proposal["files"][0]["after"], MODELS.replace(FALLBACK_FIX["old"], FALLBACK_FIX["new"]))
        # Another user, even with the same history text, never reaches this user's proposal.
        other = self.edit(self.REVISION, list(fresh), issue=ISSUE, history=history, user=base.OTHER)
        self.assertIsNone(other.proposal["revision_of"])

    def test_a_different_issue_is_a_separate_change(self):
        question, first = self.first_proposal()
        history = self.remember(question, first)
        responses = [plan("src/requests/models.py"),
                     edit([{"file_path": "src/requests/models.py", "action": "modify", "changes": [FALLBACK_FIX]}])]
        result = self.edit("Also fix https://github.com/psf/requests/issues/7700", responses, issue=None, history=history)
        self.assertIsNone(result.proposal["revision_of"])


class MarkdownTests(unittest.TestCase):
    def test_an_older_proposal_without_verification_renders_as_before(self):
        markdown = format_proposal_markdown({"title": "T", "files": [], "validation": ["pytest"]})
        self.assertNotIn("Tests not run", markdown)
        self.assertIn("## How to verify\n- pytest", markdown)


if __name__ == "__main__":
    unittest.main()
