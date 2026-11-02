"""Exact evidence for exhaustive, subclass and change-risk questions (round 2).

* R2-05: "find every place that reads the timeout configuration" gets every
  line an exact search finds (``self._timeout``, ``_set_timeout``,
  ``self.timeout``, ``"timeout"``) as compact windows labelled with the
  declaration they are in; past the budget the plan counts what was left out.
* R2-12: "does anything subclass Flask or override make_response?" gets the
  declarations that inherit from Flask and the same-named methods in them.
* R2-11: a go/no-go question about a file gets the Change impact report as
  generated evidence (never cited) and the docs that mention the name.

Database tests use a temporary embedded libSQL file seeded through the real
chunker, so sections overlap and split as in production. No network.
"""

import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent.agent import build_answer_messages, exact_search_notes
from api import query_router
from api.query_router import build_citations, build_context_with_coverage
from database import TursoStore, apply_migrations
from ingest.chunker import chunk_file_content
from ingest.embedder import EmbeddingUnavailableError
from retrieval import code_search, retriever
from retrieval.retriever import (
    build_evidence_plan,
    exhaustive_pattern,
    exhaustive_search_names,
    is_exhaustive_question,
    is_impact_question,
    subclass_question_targets,
)


def _filler(count: int, indent: str = "        ") -> str:
    return "".join(f"{indent}step_{index} = {index}\n" for index in range(count))


HTTPX_CLIENT = (
    "from ._config import DEFAULT_TIMEOUT_CONFIG, Timeout\n"
    "\n"
    "\n"
    "class BaseClient:\n"
    "    def __init__(self, *, timeout=DEFAULT_TIMEOUT_CONFIG):\n"
    "        self._timeout = Timeout(timeout)\n"
    + _filler(40)
    + "\n"
    "    @property\n"
    "    def timeout(self):\n"
    "        return self._timeout\n"
    "\n"
    "    @timeout.setter\n"
    "    def timeout(self, timeout):\n"
    "        self._timeout = Timeout(timeout)\n"
    "\n"
    "    def build_request(self, method, url, timeout=None, extensions=None):\n"
    "        extensions = {} if extensions is None else extensions\n"
    "        if \"timeout\" not in extensions:\n"
    "            timeout = self.timeout if timeout is None else Timeout(timeout)\n"
    "            extensions = dict(**extensions, timeout=timeout.as_dict())\n"
    "        return extensions\n"
    "\n"
    "    def _set_timeout(self, request):\n"
    "        if \"timeout\" not in request.extensions:\n"
    "            timeout = self.timeout\n"
    "            request.extensions = dict(**request.extensions, timeout=timeout.as_dict())\n"
    "\n"
    "\n"
    "class Client(BaseClient):\n"
    "    def __init__(self, *, timeout=DEFAULT_TIMEOUT_CONFIG):\n"
    "        super().__init__(timeout=timeout)\n"
    + _filler(170)
    + "        self._timeout_retries = 0\n"
    "\n"
    "    def get(self, url, *, timeout=None):\n"
    "        return self.request(\"GET\", url, timeout=timeout)\n"
    "\n"
    "    def send(self, request):\n"
    "        self._set_timeout(request)\n"
    "        return request\n"
)
HTTPX_CONFIG = (
    "class Timeout:\n"
    "    \"\"\"Timeout configuration.\"\"\"\n"
    "\n"
    "    def __init__(self, timeout=None, *, connect=None):\n"
    "        if isinstance(timeout, Timeout):\n"
    "            self.connect = timeout.connect\n"
    "        else:\n"
    "            self.connect = connect\n"
    "\n"
    "\n"
    "DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)\n"
)
HTTPX_FILES = {
    "httpx/_client.py": HTTPX_CLIENT,
    "httpx/_config.py": HTTPX_CONFIG,
    "tests/test_client.py": "def test_timeout(client):\n    assert client.timeout.connect == 5.0\n",
    "docs/timeouts.md": "# Timeouts\n\nSet `client.timeout` to change it.\n",
}

FLASK_FILES = {
    "src/flask/app.py": (
        "class Flask(App):\n"
        "    def make_response(self, rv):\n"
        "        return rv\n"
        "\n"
        "    def finalize_request(self, rv):\n"
        "        return self.make_response(rv)\n"
    ),
    "src/flask/helpers.py": "def make_response(*args):\n    return current_app.make_response(args)\n",
    "tests/test_subclassing.py": (
        "import flask\n\n\ndef test_suppressed_exception_logging():\n"
        "    class SuppressedFlask(flask.Flask):\n"
        "        def log_exception(self, ctx, exc_info):\n"
        "            pass\n"
    ),
    "tests/test_config.py": (
        "import flask\n\n\ndef test_custom_config_class():\n"
        "    class Flask(flask.Flask):\n"
        "        config_class = Config\n"
    ),
    "tests/test_override.py": (
        "from flask import Flask\n\n\nclass MyFlask(Flask):\n"
        "    def make_response(self, rv):\n"
        "        return super().make_response(rv)\n"
    ),
    "tests/test_basic.py": (
        "import flask\n\n\ndef test_make_response(app):\n    rv = flask.make_response()\n    assert rv.status_code == 200\n"
    ),
    "static/app.js": "export class Shell extends Flask {\n  render() {}\n}\n",
    "docs/patterns/subclassing.rst": "Subclassing\n===========\n\n    class MyFlask(Flask):\n        pass\n",
    "docs/api.rst": "API\n===\n\n.. autofunction:: make_response\n",
    "CHANGES.rst": "Version 1\n---------\n\n-   ``make_response`` accepts a tuple.\n",
}


class ExactEvidenceStoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'exact.db'}", "local-test-token")
        await apply_migrations(self.store)

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    async def add_repo(self, files: dict[str, str]) -> str:
        repo_id = f"repo-{uuid.uuid4()}"
        rows = []
        for path, text in files.items():
            for chunk in chunk_file_content(text, path):
                rows.append({**chunk, "id": str(uuid.uuid4()), "repo_id": repo_id, "embedding": None})
        now = datetime.now(UTC).isoformat()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, 'user-1', 'demo', 'https://github.com/acme/demo.git', 'ready', ?, ?, ?)",
            [repo_id, len(rows), now, now],
        )
        await self.store.insert_chunks(rows)
        return repo_id

    async def retrieve(self, repo_id: str, question: str) -> list[dict]:
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            return await retriever.retrieve_context(self.store, repo_id, question, top_k=8)


def _line_of(text: str, needle: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(text.split("\n"), 1):
        if needle in line:
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(f"{needle!r} not in text")


def _covered(chunks: list[dict], path: str, line: int) -> list[dict]:
    return [chunk for chunk in chunks if chunk.get("file_path") == path
            and int(chunk["start_line"]) <= line <= int(chunk["end_line"])]


# ---------------------------------------------------------------------------
# Question analysis
# ---------------------------------------------------------------------------


class QuestionAnalysisTests(unittest.TestCase):
    def test_exhaustive_wording(self):
        for question in (
            "Find every place that reads or applies the timeout configuration",
            "List all call sites of _set_timeout",
            "Show me all usages of make_response",
            "Where is process_value used?",
            "Where are retries applied?",
            "Where else is this referenced?",
        ):
            self.assertTrue(is_exhaustive_question(question), question)
        for question in (
            "Trace the call path from Client.get. List each function in order with its file and line.",
            "How does the timeout work?",
            "Where is Client.get defined?",
        ):
            self.assertFalse(is_exhaustive_question(question), question)

    def test_code_names_come_first_and_a_setting_word_is_used_without_one(self):
        self.assertEqual(exhaustive_search_names("List every call site of _set_timeout"), (["_set_timeout"], []))
        self.assertEqual(exhaustive_search_names("Find every caller of Flask.make_response")[0], ["make_response"])
        self.assertEqual(
            exhaustive_search_names("Find every place that reads or applies the timeout configuration, from the Client constructor"),
            ([], ["timeout"]),
        )
        self.assertEqual(exhaustive_search_names("Where are retries used?"), ([], ["retries"]))
        self.assertEqual(exhaustive_search_names("Find every place that reads the default configuration"), ([], []))

    def test_setting_word_matches_its_code_forms_but_not_pass_through_arguments(self):
        pattern = exhaustive_pattern([], ["timeout"])
        for line in (
            "        self._timeout = Timeout(timeout)",
            "    def _set_timeout(self, request):",
            "            timeout = self.timeout",
            "            extensions = dict(**extensions, timeout=timeout.as_dict())",
            "    @timeout.setter",
            '        if "timeout" not in extensions:',
            "DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)",
        ):
            self.assertTrue(pattern.search(line), line)
        for line in (
            '        return self.request("GET", url, timeout=timeout)',
            "    def get(self, url, *, timeout=None):",
            '    REQUEST_TIMEOUT = 408, "Request Timeout"',
            "        self.timeouts = []",
        ):
            self.assertFalse(pattern.search(line), line)
        names = exhaustive_pattern(["make_response"], [])
        self.assertTrue(names.search("rv = flask.make_response()"))
        self.assertFalse(names.search("rv = flask.make_response_later()"))

    def test_subclass_and_override_targets(self):
        self.assertEqual(
            subclass_question_targets("Does anything subclass Flask or override make_response or finalize_request?"),
            (["Flask"], ["make_response", "finalize_request"]),
        )
        self.assertEqual(subclass_question_targets("Which classes extend `BaseHandler`?"), (["BaseHandler"], []))
        self.assertEqual(subclass_question_targets("What overrides Flask.make_response?"), (["Flask"], ["make_response"]))
        self.assertEqual(subclass_question_targets("List the implementations of Transport"), (["Transport"], []))
        # "Which base class do I subclass" names no class.
        self.assertEqual(
            subclass_question_targets("How do I write a custom parameter type? Which base class do I subclass?"), ([], []),
        )
        self.assertEqual(subclass_question_targets("How does the session work?"), ([], []))

    def test_go_no_go_and_risk_wording_is_an_impact_question(self):
        for question in (
            "Give me a go/no-go risk summary for changing make_response in src/flask/app.py",
            "What would break if I rename finalize_request?",
            "Is it safe to change src/flask/app.py?",
            "What is the blast radius of this change?",
            "What are the risks of modifying make_response?",
        ):
            self.assertTrue(is_impact_question(question), question)
        for question in ("What security risks does the auth middleware have?", "How does make_response work?"):
            self.assertFalse(is_impact_question(question), question)

    def test_subclass_declarations_in_python_and_javascript(self):
        pattern = code_search.subclass_pattern("Flask")
        for line in (
            "    class SuppressedFlask(flask.Flask):",
            "class MyFlask(Flask):",
            "class FlaskProxy(ProxyMixin[Flask], Flask): ...",
            "export class Shell extends Flask {",
            "class Shell extends ui.Flask<Props> implements Flask {",
            "class Foo implements Runnable, Flask {",
            "class Foo : Flask()",
            "class Foo < Flask",
        ):
            self.assertTrue(pattern.search(line), line)
        for line in ("class Flask(App):", "app = flask.Flask(__name__)", "class FlaskApp(Flask2):", "class X(metaclass=Flask):"):
            self.assertFalse(pattern.search(line), line)

    def test_enclosing_declaration_comes_from_the_whole_file(self):
        lines = {number: text for number, text in enumerate(HTTPX_CLIENT.split("\n"), 1)}
        outline = code_search.outline_from_lines(lines, "httpx/_client.py")
        late = _line_of(HTTPX_CLIENT, "self._timeout_retries")
        early = _line_of(HTTPX_CLIENT, "self._timeout = Timeout(timeout)")
        self.assertEqual(code_search.enclosing_declaration(outline, late), "Client.__init__")
        self.assertEqual(code_search.enclosing_declaration(outline, early), "BaseClient.__init__")
        self.assertIsNone(code_search.enclosing_declaration(outline, 1))


# ---------------------------------------------------------------------------
# R2-05: every place
# ---------------------------------------------------------------------------


class ExhaustiveUsageTests(ExactEvidenceStoreCase):
    async def test_every_timeout_site_is_a_labelled_window_ahead_of_ranked_sections(self):
        repo_id = await self.add_repo(HTTPX_FILES)
        chunks = await self.retrieve(repo_id, "Find every place that reads or applies the timeout configuration")
        windows = [chunk for chunk in chunks if "exact_search" in (chunk.get("_retrieval_methods") or [])]
        self.assertTrue(windows)
        self.assertEqual(chunks[:len(windows)], windows, "exact windows come first")
        for needle, occurrence, label in (
            ("self._timeout = Timeout(timeout)", 1, "BaseClient.__init__"),
            ("return self._timeout", 1, "BaseClient.timeout"),
            ("self._timeout = Timeout(timeout)", 2, "BaseClient.timeout"),
            ('if "timeout" not in extensions', 1, "BaseClient.build_request"),
            ("def _set_timeout", 1, "BaseClient._set_timeout"),
            ("self._timeout_retries", 1, "Client.__init__"),
            ("self._set_timeout(request)", 1, "Client.send"),
        ):
            line = _line_of(HTTPX_CLIENT, needle, occurrence)
            covering = _covered(windows, "httpx/_client.py", line)
            self.assertTrue(covering, f"{needle} (line {line}) is not in an exact window")
            self.assertIn(label, covering[0]["symbols"], needle)
            self.assertLessEqual(covering[0]["end_line"] - covering[0]["start_line"], 16)
        # A pass-through argument is not a site.
        passing = _line_of(HTTPX_CLIENT, 'return self.request("GET"')
        self.assertFalse(_covered(windows, "httpx/_client.py", passing))
        # The type that holds the setting, from its declaration.
        self.assertTrue(_covered(windows, "httpx/_config.py", 1))
        # Docs are left out unless asked for; tests come after the library code.
        self.assertFalse([chunk for chunk in windows if chunk["file_path"].startswith("docs/")])
        paths = [chunk["file_path"] for chunk in windows]
        self.assertGreater(paths.index("tests/test_client.py"), paths.index("httpx/_client.py"))
        plan = chunks[0]["_evidence_plan"]
        self.assertEqual(plan["exact_search"]["names"], ["timeout"])
        self.assertEqual(plan["exact_search"]["omitted"], 0)
        self.assertEqual(plan["exact_search"]["shown"], plan["exact_search"]["matches"])
        # Every window renders with its real line numbers and declaration.
        context, shown = build_context_with_coverage(chunks)
        self.assertIn("Symbols: Client.send", context)
        self.assertEqual(len(shown), len(chunks))

    async def test_matches_past_the_budget_are_counted_as_left_out(self):
        repo_id = await self.add_repo(HTTPX_FILES)
        with patch.object(retriever, "EXACT_EVIDENCE_LINES", 3):
            chunks = await self.retrieve(repo_id, "Find every place in the library code that applies the timeout configuration")
        summary = chunks[0]["_evidence_plan"]["exact_search"]
        self.assertLessEqual(summary["shown"], 3)
        self.assertGreater(summary["omitted"], 0)
        self.assertEqual(summary["shown"] + summary["omitted"], summary["matches"])
        notes = " ".join(exact_search_notes({"exact_search": summary}))
        self.assertIn("partial", notes)
        self.assertIn(f"{summary['omitted']} more matching lines", notes)

    async def test_excluded_tests_are_neither_shown_nor_counted(self):
        repo_id = await self.add_repo(HTTPX_FILES)
        chunks = await self.retrieve(
            repo_id, "Find every place in the library code (not tests or docs) that reads the timeout configuration",
        )
        windows = [chunk for chunk in chunks if "exact_search" in (chunk.get("_retrieval_methods") or [])]
        self.assertTrue(windows)
        self.assertFalse([chunk for chunk in windows if chunk["file_path"].startswith(("tests/", "docs/"))])

    async def test_call_sites_of_a_named_function(self):
        repo_id = await self.add_repo(HTTPX_FILES)
        chunks = await self.retrieve(repo_id, "List every call site of _set_timeout")
        windows = [chunk for chunk in chunks if "exact_search" in (chunk.get("_retrieval_methods") or [])]
        self.assertTrue(_covered(windows, "httpx/_client.py", _line_of(HTTPX_CLIENT, "def _set_timeout")))
        self.assertTrue(_covered(windows, "httpx/_client.py", _line_of(HTTPX_CLIENT, "self._set_timeout(request)")))
        self.assertEqual(chunks[0]["_evidence_plan"]["exact_search"]["kind"], "identifier")

    async def test_an_ordinary_question_runs_no_exact_search(self):
        repo_id = await self.add_repo(HTTPX_FILES)
        chunks = await self.retrieve(repo_id, "How does the client apply a timeout?")
        self.assertFalse([chunk for chunk in chunks if chunk["id"].startswith(("exact_search:", "subclass:"))])
        self.assertNotIn("exact_search", chunks[0]["_evidence_plan"])


# ---------------------------------------------------------------------------
# R2-12: subclasses and overrides
# ---------------------------------------------------------------------------


class SubclassTests(ExactEvidenceStoreCase):
    async def test_subclasses_and_overrides_are_evidence(self):
        repo_id = await self.add_repo(FLASK_FILES)
        chunks = await self.retrieve(repo_id, "Does anything subclass Flask or override make_response or finalize_request?")
        subclasses = [chunk for chunk in chunks if chunk["id"].startswith("subclass:")]
        found = {(chunk["file_path"], chunk["start_line"]) for chunk in subclasses}
        self.assertEqual(found, {
            ("tests/test_subclassing.py", 5), ("tests/test_config.py", 5), ("tests/test_override.py", 4),
            ("static/app.js", 1),
        })
        # The definition of Flask itself and the docs example are not subclasses here.
        self.assertFalse(_covered(subclasses, "src/flask/app.py", 1))
        labels = {chunk["file_path"]: chunk["symbols"] for chunk in subclasses}
        self.assertEqual(labels["tests/test_subclassing.py"], ["test_suppressed_exception_logging.SuppressedFlask"])
        overrides = [chunk for chunk in chunks if chunk["id"].startswith("override:")]
        self.assertEqual([(chunk["file_path"], chunk["start_line"]) for chunk in overrides], [("tests/test_override.py", 5)])
        self.assertIn("declares `make_response` in `MyFlask`", overrides[0]["_retrieval_reasons"][0])
        plan = chunks[0]["_evidence_plan"]["subclass_search"]
        self.assertEqual((plan["bases"], plan["found"], plan["overrides"]), (["Flask"], 4, 1))
        notes = " ".join(exact_search_notes({"subclass_search": plan}))
        self.assertIn("found 4 class declarations that inherit from `Flask`", notes)
        self.assertIn("never say that nothing subclasses it", notes)

    async def test_docs_examples_count_when_docs_are_asked_for(self):
        repo_id = await self.add_repo(FLASK_FILES)
        chunks = await self.retrieve(repo_id, "Which classes in the code or the docs subclass Flask?")
        self.assertIn(("docs/patterns/subclassing.rst", 4),
                      {(chunk["file_path"], chunk["start_line"]) for chunk in chunks if chunk["id"].startswith("subclass:")})

    async def test_no_subclass_is_reported_as_a_search_result(self):
        repo_id = await self.add_repo(FLASK_FILES)
        chunks = await self.retrieve(repo_id, "Does anything subclass Blueprint?")
        plan = (chunks[0]["_evidence_plan"] if chunks else {}).get("subclass_search") or {}
        self.assertEqual(plan.get("found"), 0)
        notes = " ".join(exact_search_notes({"subclass_search": plan}))
        self.assertIn("found no class declaration that inherits from `Blueprint`", notes)


# ---------------------------------------------------------------------------
# R2-11: change impact and docs for a go/no-go question
# ---------------------------------------------------------------------------


class ChangeRiskTests(ExactEvidenceStoreCase):
    async def test_docs_that_mention_the_name_are_retrieved_reference_docs_first(self):
        repo_id = await self.add_repo(FLASK_FILES)
        chunks = await self.retrieve(repo_id, "Give me a go/no-go risk summary for changing make_response in src/flask/app.py")
        docs = [chunk for chunk in chunks if chunk["id"].startswith("doc_mention:")]
        self.assertEqual([chunk["file_path"] for chunk in docs], ["docs/api.rst", "CHANGES.rst"])
        self.assertEqual(chunks[0]["file_path"], "src/flask/app.py", "the named file still leads")
        self.assertEqual(chunks[0]["_evidence_plan"]["docs_mentions"]["files"], 2)

    async def test_change_impact_is_generated_evidence_and_test_functions_are_citable(self):
        repo_id = await self.add_repo(FLASK_FILES)
        repo = {"id": repo_id, "staging_prefix": None, "github_url": "https://github.com/acme/demo"}
        report = {
            "target_file": "src/flask/app.py", "summary": "1 indexed file imports src/flask/app.py directly.",
            "dependents": [{"file_path": "src/flask/__init__.py", "depth": 1, "line_number": 2}], "dependent_count": 1,
            "affected_tests": {
                "files": [{"file_path": "tests/test_basic.py", "depth": 2, "via": "src/flask/__init__.py", "line_number": 1}],
                "count": 1, "indirect_count": 0,
                "functions": [{"file_path": "tests/test_basic.py", "name": "test_make_response", "line": 4,
                               "kind": "reference", "reference_line": 5}],
            },
            "symbol": {"name": "make_response", "defined_at": 2, "mentions": 2},
        }
        question = "Give me a go/no-go risk summary for changing make_response in src/flask/app.py"
        plan = build_evidence_plan(question)
        with patch.object(query_router.impact, "resolve_target", new=AsyncMock(return_value=(["src/flask/app.py"], True))), \
                patch.object(query_router.impact, "ensure_current_graph", new=AsyncMock(return_value={"rebuilding": False})), \
                patch.object(query_router.impact, "analyse_impact", new=AsyncMock(return_value=report)) as analyse:
            evidence = await query_router.change_impact_evidence(self.store, repo, question, plan)
        self.assertEqual(analyse.await_args.kwargs["symbol"], "make_response")
        summary, *windows = evidence
        self.assertEqual(summary["_synthetic"], "change_impact_summary")
        self.assertIn("tests/test_basic.py:4 test_make_response (uses it at line 5)", summary["content"])
        self.assertIn("tests/test_basic.py (via src/flask/__init__.py)", summary["content"])
        self.assertIn("not measured coverage", summary["content"])
        self.assertEqual([(item["file_path"], item["start_line"], item["end_line"]) for item in windows],
                         [("tests/test_basic.py", 4, 6)])
        context = build_context_with_coverage(evidence)[0]
        self.assertTrue(context.startswith("Generated change impact summary for src/flask/app.py:"))
        # The generated summary is never a citation; the test window is.
        self.assertEqual([item["file_path"] for item in build_citations(evidence, repo)], ["tests/test_basic.py"])
        self.assertEqual(plan["change_impact"]["test_functions"], 1)
        notes = " ".join(exact_search_notes(plan))
        self.assertIn("static change impact summary for src/flask/app.py and `make_response`", notes)
        self.assertIn("name every other file, importer or test function without a line number", notes)
        messages = build_answer_messages(question, context, evidence_plan=plan)
        self.assertIn("Use it for the tests and coverage part", messages[-1]["content"])

    async def test_no_impact_evidence_without_an_impact_question_or_a_single_target(self):
        repo = {"id": "repo-1", "staging_prefix": None}
        self.assertEqual(await query_router.change_impact_evidence(self.store, repo, "How does make_response work?", {}), [])
        with patch.object(query_router.impact, "resolve_target",
                          new=AsyncMock(return_value=(["a/app.py", "b/app.py"], True))):
            self.assertEqual(await query_router.change_impact_evidence(
                self.store, repo, "What would break if I change app.py?", {"requested_files": ["app.py"]}), [])

    async def test_a_change_question_without_a_file_uses_the_only_declaring_source_file(self):
        repo_id = await self.add_repo(FLASK_FILES)
        repo = {"id": repo_id, "staging_prefix": None}
        target = await query_router._impact_target(self.store, repo, "", [], ["finalize_request"])
        self.assertEqual(target, "src/flask/app.py")
        # make_response is declared in two source files: no single target.
        self.assertIsNone(await query_router._impact_target(self.store, repo, "", [], ["make_response"]))


if __name__ == "__main__":
    unittest.main()
