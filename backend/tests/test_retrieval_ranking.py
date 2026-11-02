"""Retrieval ranking for natural questions (customer findings BUG-04, BUG-06).

Named files are ranked by the question instead of read from the top; dotted
names (``Client.get``, ``res.redirect``) find their definitions; test
questions pair source with tests by name; a call site leads to the
definition it calls; "not tests or docs" and "lint or style rules" no longer
pull tests, docs or stylesheets. None of these tests need a network.
"""

import asyncio
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.embedder import EmbeddingUnavailableError  # noqa: E402
from retrieval import retriever  # noqa: E402
from retrieval.retriever import (  # noqa: E402
    build_evidence_plan,
    call_site_window,
    called_names_for_question,
    extract_qualified_names,
    extract_question_identifiers,
    extract_question_path_hints,
    extract_requested_file_paths,
    question_excludes_docs,
    question_excludes_tests,
    question_focus,
    question_wants_tests,
    rank_file_sections,
    search_terms,
    select_diverse_chunks,
    select_named_sections,
    window_chunk,
)


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    """Routes SQL to canned rows by a substring of the statement (first match wins)."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.executed = []

    async def fetch_all(self, sql, args=None):
        self.executed.append((sql, list(args or [])))
        for needle, rows in self.routes:
            if needle in sql:
                return [dict(row) for row in (rows(sql, args) if callable(rows) else rows)]
        return []


def chunk(path, start, content, chunk_id=None, symbols=()):
    lines = content.split("\n")
    return {
        "id": chunk_id or f"{path}:{start}", "file_path": path, "start_line": start, "end_line": start + len(lines) - 1,
        "language": "py", "symbols": list(symbols), "content": content,
    }


def windowed_file(path, special, total, size=150, overlap=40):
    """Chunks of a synthetic file the way the chunker emits them: 150-line
    windows overlapping by 40 lines, symbols = names declared in the window."""
    lines = [special.get(number, f"    value_{number} = {number}") for number in range(1, total + 1)]
    chunks, start = [], 1
    while start <= total:
        end = min(total, start + size - 1)
        body = lines[start - 1:end]
        symbols = [m.group(1) for line in body for m in [re.match(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)", line)] if m]
        chunks.append(chunk(path, start, "\n".join(body), symbols=symbols))
        if end == total:
            break
        start = end - overlap + 1
    return chunks


HTTPX_CLIENT = windowed_file("httpx/_client.py", {
    2: "import typing",
    # Words a reading instruction or a substring would wrongly match.
    470: '        """Explain why the stream must stop here."""',
    188: "class BaseClient:",
    594: "class Client(BaseClient):",
    979: "            response = self._send_single_request(request)",
    1001: "    def _send_single_request(self, request: Request) -> Response:",
    1036: "    def get(self, url, **kwargs):",
    1307: "class AsyncClient(BaseClient):",
    1694: "            response = await self._send_single_request(request)",
    1717: "    async def _send_single_request(self, request: Request) -> Response:",
    1751: "    async def get(self, url, **kwargs):",
}, 2019)


def covers(item, path, line):
    return item["file_path"] == path and item["start_line"] <= line <= item["end_line"]


class QuestionAnalysisTests(unittest.TestCase):
    def test_dotted_names_give_owner_and_member(self):
        self.assertEqual(
            extract_qualified_names("If I change Client._send_single_request in httpx/_client.py, what breaks?"),
            [("Client", "_send_single_request")],
        )
        self.assertEqual(extract_qualified_names("Trace httpx.Client.get to the network"), [("Client", "get")])
        self.assertEqual(
            extract_qualified_names("A bug in res.redirect or res.location in lib/response.js"),
            [("res", "redirect"), ("res", "location")],
        )
        for question in ("How does Next.js routing work?", "See example.com and github.com/a/b", "Read src/app.py",
                         "e.g. version 3.1.0", "Is it in README.md?"):
            self.assertEqual(extract_qualified_names(question), [], question)

    def test_private_names_are_identifiers_and_file_names_are_not(self):
        self.assertEqual(extract_question_identifiers("List every call site of _set_timeout"), ["_set_timeout"])
        self.assertIn("_send_single_request", extract_question_identifiers("Change Client._send_single_request"))
        self.assertEqual(extract_question_identifiers("Run tests/test_basic.py and _client.py"), [])
        # Unchanged behaviour.
        self.assertEqual(extract_question_identifiers("Where is the Booking model defined?"), ["Booking"])

    def test_a_bare_file_name_repeating_a_named_path_is_dropped(self):
        question = "If I change httpx/_client.py, which modules import _client.py?"
        self.assertEqual(extract_requested_file_paths(question), ["httpx/_client.py"])
        self.assertEqual(extract_requested_file_paths("Open src/a.ts. Then b.py!"), ["src/a.ts", "b.py"])

    def test_long_bug_report_keeps_its_technical_terms(self):
        question = (
            'Bug report: requests.Request("POST", "http://x", data={1: "a"}, files={"f": b"x"}).prepare() raises '
            "AttributeError: 'int' object has no attribute 'translate' from inside urllib3. Why? Walk me through "
            "the code path from prepare() to where it breaks, with citations."
        )
        terms = search_terms(question)
        for wanted in ("files", "translate", "prepare", "attributeerror"):
            self.assertIn(wanted, terms)
        for narrative in ("bug", "report", "why", "walk", "through", "citations", "http"):
            self.assertNotIn(narrative, terms)
        self.assertEqual(terms[0], "request")  # the dotted member leads the term list

    def test_negated_tests_and_docs_are_exclusions_not_requests(self):
        question = "Find every place in the library code (not tests or docs) that reads the timeout configuration."
        self.assertFalse(question_wants_tests(question))
        self.assertTrue(question_excludes_tests(question))
        self.assertTrue(question_excludes_docs(question))
        plan = build_evidence_plan(question)
        self.assertEqual((plan["tests_requested"], plan["tests_excluded"], plan["docs_excluded"]), (False, True, True))
        self.assertNotIn("tests", plan["search_terms"])
        self.assertNotIn("not", plan["search_terms"])
        # Questions about tests and coverage gaps still want tests.
        for question in ("Which test files cover res.redirect?", "Which functions are not tested?", "Which code has no tests?"):
            self.assertTrue(question_wants_tests(question), question)
            self.assertFalse(question_excludes_tests(question), question)

    def test_coverage_wording_is_not_an_exclusion(self):
        """Review finding: "without tests" and "ignore tests" removed every test from coverage questions."""
        for question in (
            "Is res.redirect covered by tests or does it ship without tests?",
            "Which helpers are without test coverage?",
            "Why does pytest ignore tests in the testing folder?",
            "How do I skip tests that need the network?",
            # A request wins over an exclusion in the same question.
            "Which library code (not tests) has no test coverage?",
        ):
            plan = build_evidence_plan(question)
            self.assertEqual((plan["tests_requested"], plan["tests_excluded"]), (True, False), question)
        self.assertIn("tests", search_terms("Is res.redirect covered by tests or does it ship without tests?"))
        # Explicit exclusion wording still excludes.
        for question in ("Without looking at the tests, where is the timeout applied?",
                         "Where is the timeout applied, excluding tests?",
                         "Which code other than the tests reads the timeout?"):
            self.assertTrue(question_excludes_tests(question), question)
            self.assertFalse(question_wants_tests(question), question)

    def test_a_bare_formatter_is_code_not_tooling(self):
        """Review finding: "help formatter" made pyproject.toml the top source."""
        tooling = retriever.TOOLING_QUESTION_PATTERN
        for question in ("How does the help formatter wrap long option descriptions?",
                         "Where is the default logging formatter configured?",
                         "Which HelpFormatter method writes the usage line?",
                         "When I run the CLI, how does the help formatter wrap text?"):
            self.assertIsNone(tooling.search(question), question)
        for question in ("Which code formatter does the project use?", "How do I format the code before committing?",
                         "What lint or style rules does the project enforce?", "How do I run the tests locally?",
                         "How do I run black?", "How do I run the formatter?"):
            self.assertIsNotNone(tooling.search(question), question)

    def test_instruction_words_are_dropped_only_where_they_instruct(self):
        """Review finding: "trace" is an httpx extension; a global stopword hid it."""
        self.assertIn("trace", search_terms("How does the trace extension work in httpx?"))
        self.assertIn("report", search_terms("Where is the coverage report generated?"))
        self.assertIn("find", search_terms("Where is db.find defined?"))
        self.assertIn("find", search_terms("Where is the cache checked? find() is slow"))
        terms = search_terms(
            "Trace the call path from httpx.Client.get down to the point where the request bytes are actually sent. "
            "List each function in order. Can you find the retry?"
        )
        for instruction in ("trace", "list", "point", "find"):
            self.assertNotIn(instruction, terms)
        self.assertNotIn("report", search_terms("Bug report: prepare() fails with files"))
        self.assertNotIn("place", search_terms("Find every place that reads the timeout"))

    def test_importer_questions_are_recognised(self):
        """Review finding: "What imports X?" got no importers although the docs promised them."""
        pattern = retriever.IMPORTER_QUESTION_PATTERN
        for question in ("What imports httpx/_client.py?", "Which modules import httpx/_client.py?",
                         "Who uses lib/router/index.js?", "Where is httpx/_client.py imported?",
                         "Is src/flask/app.py imported by anything else?"):
            self.assertIsNotNone(pattern.search(question), question)
        for question in ("What does httpx/_client.py import?", "What are the imports at the top of httpx/_client.py?",
                         "Which imports in src/flask/app.py are unused?"):
            self.assertIsNone(pattern.search(question), question)

    def test_code_style_and_citation_wording_select_no_path_family(self):
        lint = "How do I run a single test file locally, and what lint or style rules does the project enforce?"
        self.assertEqual(extract_question_path_hints(lint), [])
        self.assertEqual(extract_question_path_hints("Explain our coding style"), [])
        self.assertIn("css", extract_question_path_hints("Where are the button styles defined?"))
        self.assertEqual(extract_question_path_hints("Walk me through prepare(), with citations."), [])
        self.assertIn("retriever", extract_question_path_hints("How does citation retrieval work?"))


class NamedFileSectionTests(unittest.TestCase):
    def test_sections_are_ranked_by_declarations_then_mentions(self):
        focus = question_focus("Who calls Client._send_single_request in httpx/_client.py?", ["httpx/_client.py"])
        self.assertEqual(focus["names"], ["_send_single_request"])
        self.assertNotIn("client", focus["terms"])  # repeats the path
        ranked = rank_file_sections(HTTPX_CLIENT, focus)
        top = ranked[:2]
        self.assertTrue(any(covers(item, "httpx/_client.py", 1001) for item in top))
        self.assertTrue(any(covers(item, "httpx/_client.py", 1717) for item in top))
        self.assertIn("Defines `_send_single_request`, named in the question", top[0]["_retrieval_reasons"])

    def test_a_question_without_focus_keeps_line_order(self):
        # The fixture says "Explain ... stop" at line 470: reading
        # instructions are not focus words, so the file is read from the top.
        focus = question_focus("Explain httpx/_client.py", ["httpx/_client.py"])
        self.assertEqual((focus["names"], focus["terms"]), ([], []))
        for question in ("Give me a tour of httpx/_client.py", "Summarize httpx/_client.py for a new engineer",
                         "Can you help me understand httpx/_client.py?"):
            self.assertEqual(question_focus(question, ["httpx/_client.py"])["terms"], [], question)
        ranked = rank_file_sections(HTTPX_CLIENT, focus)
        self.assertEqual([item["start_line"] for item in ranked[:3]], [1, 111, 221])

    def test_question_words_match_whole_words(self):
        """Review finding: "top" matched ``stop`` and "explain" matched a docstring."""
        focus = question_focus("What are the imports at the top of httpx/_client.py?", ["httpx/_client.py"])
        self.assertEqual(focus["terms"], ["imports"])
        ranked = rank_file_sections(HTTPX_CLIENT, focus)
        self.assertEqual(ranked[0]["start_line"], 1)  # "import typing"; "imports" matches ``import``
        stop = chunk("a.py", 1, "    stop_here = True")
        top = chunk("a.py", 50, "    # the top of the stream")
        ranked = rank_file_sections([stop, top], {"names": [], "qualified": [], "terms": ["top"]})
        self.assertEqual([item["start_line"] for item in ranked], [50, 1])

    def test_relevant_overlaps_merge_and_filler_overlap_is_trimmed(self):
        def section(start, end, score, named):
            item = chunk("a.py", start, "\n".join(f"line {n}" for n in range(start, end + 1)))
            return {**item, "_section_score": score, "_section_named": named}
        ranked = [section(100, 249, 9, True), section(210, 359, 6, True), section(320, 469, 0.5, False), section(1, 150, 0, False)]
        leading, rest = select_named_sections(ranked, per_file_limit=8, leading_per_file=3)
        spans = [(item["start_line"], item["end_line"]) for item in [*leading, *rest]]
        # The two relevant windows merge; the filler keeps only its new lines.
        self.assertEqual(spans, [(100, 359), (360, 469), (1, 99)])
        self.assertEqual(leading[1]["content"].split("\n")[0], "line 360")

    def test_a_later_merge_never_shows_lines_twice(self):
        """1-150 merges with 111-260 after 221-370 was chosen; 221-370 keeps only 261-330."""
        def section(start, end, score):
            item = chunk("a.js", start, "\n".join(f"line {n}" for n in range(start, end + 1)))
            return {**item, "_section_score": score, "_section_named": True}
        ranked = [section(1, 150, 10), section(441, 590, 8), section(221, 370, 7.5), section(331, 480, 7.25),
                  section(111, 260, 4)]
        leading, rest = select_named_sections(ranked, per_file_limit=8, leading_per_file=3)
        spans = [(item["start_line"], item["end_line"]) for item in [*leading, *rest]]
        self.assertEqual(spans, [(1, 260), (331, 590), (261, 330)])
        shown = [line for item in [*leading, *rest] for line in item["content"].split("\n")]
        self.assertEqual(len(shown), len(set(shown)))

    def test_each_named_file_leads_with_its_best_section(self):
        first = [{**chunk("a.py", n, f"a {n}"), "_section_score": 10 - n, "_section_named": False} for n in (1, 3, 5)]
        second = [{**chunk("b.py", n, f"b {n}"), "_section_score": 1, "_section_named": False} for n in (1,)]
        leading, _ = select_named_sections([*first, *second], per_file_limit=8, leading_per_file=1)
        self.assertEqual([item["file_path"] for item in leading], ["a.py", "b.py"])

    def test_named_file_question_reaches_the_middle_of_a_long_file(self):
        """BUG-04: the evidence used to stop at line ~785 of a 2,019-line file."""
        store = FakeStore([("AS focus_score FROM chunks", HTTPX_CLIENT)])
        question = "If I change the signature of Client._send_single_request in httpx/_client.py, what else is affected?"
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", question, top_k=8))
        self.assertTrue(covers(result[0], "httpx/_client.py", 1001))
        self.assertTrue(covers(result[0], "httpx/_client.py", 979))
        self.assertTrue(any(covers(item, "httpx/_client.py", 1694) for item in result[:2]))
        self.assertLessEqual(len(result), 8)
        sql, args = next(item for item in store.executed if "AS focus_score" in item[0])
        # Sections rank by what the question is about, not by line order
        # (after the exactly named path, which comes before its namesakes).
        self.assertIn("THEN 0 ELSE 1 END, focus_score DESC, file_path, start_line", sql)
        self.assertEqual(args[0], "_send_single_request")
        self.assertNotIn("unretrieved_identifiers", result[0]["_evidence_plan"])

    def test_impact_question_adds_the_import_lines_of_dependents(self):
        api = chunk("httpx/_api.py", 1, "\n".join(
            ["import typing", "", "from ._client import Client", "from ._config import (", "    Timeout,", ")"]
            + [f"x = {n}" for n in range(20)]
        ))
        dependency_row = {**api, "source_file": "httpx/_api.py", "target_file": "httpx/_client.py", "line_number": 3}
        store = FakeStore([
            ("FROM repo_dependencies d JOIN chunks c", [dependency_row]),
            ("AS focus_score FROM chunks", HTTPX_CLIENT),
        ])
        question = "If I change Client._send_single_request in httpx/_client.py, which modules import _client.py?"
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", question, top_k=8))
        imports = [item for item in result if item["file_path"] == "httpx/_api.py"]
        self.assertEqual(len(imports), 1)
        self.assertEqual((imports[0]["start_line"], imports[0]["end_line"]), (3, 3))
        self.assertEqual(imports[0]["content"], "from ._client import Client")
        self.assertEqual(imports[0]["_retrieval_methods"], ["dependency"])
        # The best sections of the named file come first.
        self.assertTrue(covers(result[0], "httpx/_client.py", 1001))

    def test_which_modules_import_a_named_file_adds_their_import_lines(self):
        """Review finding: without "if I change" or "depends", importers were never looked up."""
        api = chunk("httpx/_api.py", 1, "import typing\n\nfrom ._client import Client\nx = 1")
        dependency_row = {**api, "source_file": "httpx/_api.py", "target_file": "httpx/_client.py", "line_number": 3}
        for question in ("Which modules import httpx/_client.py?", "What imports httpx/_client.py?"):
            store = FakeStore([
                ("FROM repo_dependencies d JOIN chunks c", [dependency_row]),
                ("AS focus_score FROM chunks", HTTPX_CLIENT),
            ])
            with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
                result = run(retriever.retrieve_context(store, "r", question, top_k=8))
            imports = [item for item in result if item["file_path"] == "httpx/_api.py"]
            self.assertEqual([(item["start_line"], item["content"]) for item in imports], [(3, "from ._client import Client")])
            self.assertTrue(covers(result[0], "httpx/_client.py", 2))  # "import" lines lead the named file
        # The file's own imports are not a question about its importers.
        store = FakeStore([("AS focus_score FROM chunks", HTTPX_CLIENT)])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            run(retriever.retrieve_context(store, "r", "What does httpx/_client.py import?", top_k=8))
        self.assertFalse(any("repo_dependencies" in sql for sql, _ in store.executed))

    def test_usage_question_adds_callers_from_other_files(self):
        app = chunk("src/flask/app.py", 1260, "class Flask:\n    def make_response(self, rv):\n        return rv",
                    symbols=["Flask", "make_response"])
        helpers = chunk("src/flask/helpers.py", 150, "\n".join([
            "def make_response(*args):",
            '    """Example:',
            "        def index():",
            "            return make_response(render())",
            '    """',
            "    if not args:",
            "        return current_app.response_class()",
            "    return current_app.make_response(args)",
        ]), symbols=["make_response"])
        store = FakeStore([("chunks_fts MATCH", [app, helpers]), ("AS focus_score FROM chunks", [app])])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", "Who calls Flask.make_response in src/flask/app.py?", top_k=8))
        self.assertEqual(result[0]["file_path"], "src/flask/app.py")
        callers = [item for item in result if item["file_path"] == "src/flask/helpers.py"]
        self.assertEqual(len(callers), 1)
        # Starts at the enclosing function, not at the docstring example.
        self.assertEqual((callers[0]["start_line"], callers[0]["end_line"]), (150, 157))
        self.assertIn("Calls `make_response` outside the named file", callers[0]["_retrieval_reasons"])

    def test_test_question_pairs_the_named_file_with_its_tests(self):
        """BUG-06: res.redirect tests were indexed but never retrieved."""
        response = chunk("lib/response.js", 790, "\n".join([
            "res.location = function location(url) {", "  return this.set('Location', url);", "};",
            "res.redirect = function redirect(url) {", "  this.location(url);", "};",
        ]))
        tests = {
            "test/res.redirect.js": chunk("test/res.redirect.js", 1, "describe('res', function(){\n  describe('.redirect(url)', function(){"),
            "test/res.location.js": chunk("test/res.location.js", 1, "describe('res', function(){\n  describe('.location(url)', function(){"),
        }
        paths = [{"file_path": path} for path in [
            *tests, "test/res.send.js", "test/fixtures/redirect.css", "examples/redirect/index.js", "lib/response.js",
        ]]

        def test_chunks(sql, args):
            return [tests[path] for path in args if path in tests]
        store = FakeStore([
            ("SELECT DISTINCT file_path FROM chunks", paths),
            ("SELECT DISTINCT source_file", []),
            ("AS focus_score FROM chunks", [response]),
            ("AND file_path IN (", test_chunks),
        ])
        question = "If I fix a small bug in res.redirect or res.location in lib/response.js, which test files cover that code?"
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", question, top_k=8, workflow="onboarding"))
        self.assertEqual(result[0]["file_path"], "lib/response.js")
        paired = [item["file_path"] for item in result if item["_retrieval_methods"] == ["paired_test"]]
        self.assertEqual(sorted(paired), ["test/res.location.js", "test/res.redirect.js"])

    def test_an_abbreviated_test_family_is_listed_not_sampled(self):
        """Review finding: two alphabetical test/res.*.js files were presented as the tests of response.js."""
        from api.query_router import build_citations, build_context_with_coverage

        response = chunk("lib/response.js", 1, "var res = Object.create(http.ServerResponse.prototype);")
        family = ["test/res.append.js", "test/res.attachment.js", "test/res.cookie.js", "test/res.send.js"]
        store = FakeStore([
            ("SELECT DISTINCT file_path FROM chunks", [{"file_path": path} for path in family]),
            ("SELECT DISTINCT source_file", []),
            ("AS focus_score FROM chunks", [response]),
            ("AND file_path IN (", lambda sql, args: [chunk(path, 1, "describe('res')") for path in args if path in family]),
        ])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", "Which tests cover lib/response.js?", top_k=8))
        listing = [item for item in result if item.get("_synthetic")]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["file_path"], "test/res.*")
        self.assertIn("4 test files are named like lib/response.js", listing[0]["content"])
        self.assertIn(", ".join(family), listing[0]["content"])
        # No member is passed off as "the" test file of response.js.
        self.assertFalse(any(item["file_path"] in family for item in result))
        text, shown = build_context_with_coverage(result)
        self.assertIn("Generated test file listing for test/res.*", text)
        self.assertNotIn("test/res.*", [item["file_path"] for item in build_citations(shown, {"repo_name": "express"})])

        # A member whose name matches the question is shown, labelled as one of the family.
        store.executed.clear()
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", "Which tests cover the cookie code in lib/response.js?", top_k=8))
        members = [item for item in result if item["file_path"] in family]
        self.assertEqual([item["file_path"] for item in members], ["test/res.cookie.js"])
        self.assertIn("One of the test files named like lib/response.js", members[0]["_retrieval_reasons"][0])

    def test_plain_named_file_request_is_still_one_round_trip(self):
        store = FakeStore([("AS focus_score FROM chunks", HTTPX_CLIENT[:2])])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "r", "Explain httpx/_client.py", top_k=8))
        self.assertEqual([item["start_line"] for item in result], [1, 151])  # the overlap is not repeated
        self.assertEqual(len(store.executed), 1)


class TestPairingTests(unittest.TestCase):
    def tier(self, path, stems=(), members=(), importers=()):
        return retriever._test_pairing_tier(path, list(stems), list(members), set(importers))

    def test_tiers(self):
        self.assertEqual(self.tier("test/res.redirect.js", ["response"], [("res", "redirect")]), 3)
        self.assertEqual(self.tier("test/app.redirect.js", ["response"], [("res", "redirect")]), 0)
        self.assertEqual(self.tier("tests/test_helpers.py", ["helpers"]), 2)
        self.assertEqual(self.tier("src/foo.test.ts", ["foo"]), 2)
        self.assertEqual(self.tier("test/response.js", ["response"]), 2)
        self.assertEqual(self.tier("tests/test_types/test_convert_type.py", ["types"]), 2)
        self.assertEqual(self.tier("tests/test_apps/cliapp/app.py", ["app"]), 0)
        self.assertEqual(self.tier("tests/test_basic.py", ["app"], importers=["tests/test_basic.py"]), 2)
        self.assertEqual(self.tier("test/res.send.js", ["response"]), 1)

    def test_only_the_strongest_tier_and_test_code_are_kept(self):
        paths = [{"file_path": p} for p in ("test/res.redirect.js", "test/res.send.js", "test/res.append.js",
                                            "tests/fixtures/redirect.css")]
        store = FakeStore([
            ("SELECT DISTINCT file_path FROM chunks", paths),
            ("AND file_path IN (", lambda sql, args: [chunk(path, 1, "x") for path in args[1:]]),
        ])
        rows = run(retriever.paired_test_chunks(store, "r", ["lib/response.js"], [], [("res", "redirect")]))
        self.assertEqual([row["file_path"] for row in rows], ["test/res.redirect.js"])
        # The abbreviation tier used to return the first two files in
        # alphabetical order as "the" tests; it now lists the whole family
        # (test code only) and shows members only when their name matches.
        rows = run(retriever.paired_test_chunks(store, "r", ["lib/response.js"], [], []))
        self.assertEqual([(row["file_path"], row.get("_synthetic")) for row in rows], [("test/res.*", "test_file_listing")])
        self.assertIn("Files: test/res.append.js, test/res.redirect.js, test/res.send.js", rows[0]["content"])
        rows = run(retriever.paired_test_chunks(store, "r", ["lib/response.js"], [], [], terms=["send", "body"]))
        self.assertEqual([row["file_path"] for row in rows], ["test/res.*", "test/res.send.js"])
        # "res" is in every member's name, so it picks none of them out.
        rows = run(retriever.paired_test_chunks(store, "r", ["lib/response.js"], [], [], terms=["res", "methods"]))
        self.assertEqual([row["file_path"] for row in rows], ["test/res.*"])

    def test_a_family_in_several_folders_is_listed_by_its_source(self):
        listing = retriever._test_family_listing(["test/res.send.js", "test/unit/resp.body.js"], ["lib/response.js"])
        self.assertEqual(listing["file_path"], "tests named like lib/response.js")
        self.assertTrue(listing["content"].startswith("2 test files are named like lib/response.js. They are paired"))


class QualifiedDefinitionTests(unittest.TestCase):
    def test_owner_picks_the_method_under_its_class(self):
        client = chunk("httpx/_client.py", 594, "class Client(BaseClient):\n    pass", symbols=["Client"])
        headers_get = chunk("httpx/_models.py", 200, "    def get(self, key, default=None):\n        pass", symbols=["get"])
        client_get = chunk("httpx/_client.py", 1034, "\n\n    def get(self, url):\n        return self.request('GET', url)", symbols=["get"])
        async_get = chunk("httpx/_client.py", 1747, "\n\n\n\n    async def get(self, url):\n        pass", symbols=["get"])
        store = FakeStore([
            ("LIMIT 20", [client]),
            ("AND file_path IN (SELECT file_path", [client_get, async_get]),
            ("symbols LIKE", [headers_get, async_get, client_get]),
        ])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["get"], owners={"get": "Client"}, limit_per_symbol=1))
        self.assertEqual((rows[0]["file_path"], rows[0]["start_line"]), ("httpx/_client.py", 1034))
        self.assertEqual(rows[0]["_defined_symbol"], "Client.get")
        # A plain-word member skips the broad full-text scan.
        self.assertFalse(any("LIMIT 300" in sql for sql, _ in store.executed))

    def test_owner_assignment_is_a_declaration(self):
        redirect = chunk("lib/response.js", 815, "res.redirect = function redirect(url) {\n  this.location(url);\n};")
        call = chunk("test/res.redirect.js", 10, "    res.redirect('http://google.com');")
        store = FakeStore([("chunks_fts MATCH", [call, redirect])])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["redirect"], owners={"redirect": "res"}))
        self.assertEqual(rows[0]["file_path"], "lib/response.js")
        self.assertEqual(rows[0]["_defined_symbol"], "res.redirect")

    def test_a_same_named_variable_is_not_the_owners_member(self):
        """Review finding: ``var redirect = express()`` in an example was shown first as "Defines `redirect`"."""
        example = chunk("examples/vhost/index.js", 1, "var express = require('../..');\nvar redirect = express();",
                        symbols=["redirect"])
        redirect = chunk("lib/response.js", 815, "res.redirect = function redirect(url) {\n  this.location(url);\n};")
        store = FakeStore([("chunks_fts MATCH", [example, redirect]), ("symbols LIKE", [example])])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["redirect"], owners={"redirect": "res"}))
        self.assertEqual([(row["file_path"], row["_defined_symbol"]) for row in rows], [("lib/response.js", "res.redirect")])
        # Without the owner's member, the variable is still a definition of the plain name.
        store = FakeStore([("chunks_fts MATCH", [example])])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["redirect"], owners={"redirect": "res"}))
        self.assertEqual([row["_defined_symbol"] for row in rows], ["redirect"])

    def test_a_class_declared_in_between_ends_the_owner(self):
        """Review finding: AsyncClient.send was labelled ``Client.send``."""
        client = chunk("httpx/_client.py", 594, "class Client(BaseClient):\n    pass", symbols=["Client"])
        client_send = chunk("httpx/_client.py", 814, "\n" * 65 + "    def send(self, request):\n        pass",
                            symbols=["send"])
        async_send = chunk("httpx/_client.py", 1527, "\n" * 67 + "    async def send(self, request):\n        pass",
                           symbols=["send"])
        async_client = chunk("httpx/_client.py", 1307, "class AsyncClient(BaseClient):\n    pass", symbols=["AsyncClient"])
        store = FakeStore([
            ("LIMIT 20", [client]),
            ("instr(char(10) || content", [client, async_client]),
            ("AND file_path IN (SELECT file_path", [client_send, async_send]),
            ("symbols LIKE", [async_send, client_send]),
        ])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["send"], owners={"send": "Client"}))
        self.assertEqual([(row["start_line"], row["_defined_symbol"]) for row in rows], [(814, "Client.send")])
        # One query checks the lines between ``class Client`` and both sections.
        sql, args = next(item for item in store.executed if "instr(char(10) || content" in item[0])
        self.assertEqual({tuple(args[index:index + 3]) for index in (1, 4)},
                         {("httpx/_client.py", 594, 814), ("httpx/_client.py", 594, 1527)})

    def test_a_class_shown_above_the_member_decides_its_owner(self):
        async_section = chunk("httpx/_client.py", 1307, "class AsyncClient(BaseClient):\n    async def send(self):\n        pass",
                              symbols=["AsyncClient", "send"])
        client = chunk("httpx/_client.py", 594, "class Client(BaseClient):\n    pass", symbols=["Client"])
        store = FakeStore([("LIMIT 20", [client]), ("symbols LIKE", [async_section])])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["send"], owners={"send": "Client"}))
        self.assertEqual([row["_defined_symbol"] for row in rows], ["send"])  # not "Client.send"
        # The type check needed no extra query: the section shows the class.
        self.assertFalse(any("instr(char(10) || content" in sql for sql, _ in store.executed))

    def test_symbol_evidence_is_not_capped_per_file(self):
        def item(start, method):
            return {**chunk("a.py", start, "x"), "_retrieval_methods": [method]}
        selected = select_diverse_chunks(
            [item(1, "keyword"), item(100, "symbol_usage"), item(200, "symbol_usage"), item(300, "keyword"),
             {**chunk("b.py", 1, "y"), "_retrieval_methods": ["keyword"]}], 5,
        )
        starts = [(entry["file_path"], entry["start_line"]) for entry in selected]
        self.assertIn(("a.py", 200), starts[:4])
        self.assertEqual(starts.index(("b.py", 1)), 3)


class CallSiteExpansionTests(unittest.TestCase):
    PREPARE_BODY = chunk("src/requests/models.py", 600, "\n".join([
        "    def prepare_body(self, data, files, json=None):",
        "        if files:",
        "            (body, content_type) = self._encode_files(files, data)",
        "        self.prepare_content_length(body)",
        "        x = request(data)",
        '        """self._encode_params(files) in a docstring"""',
    ]), chunk_id="call-site", symbols=["prepare_body"])

    def test_compound_names_that_share_question_words_are_followed(self):
        names = called_names_for_question([self.PREPARE_BODY], ["files", "data", "prepare", "request"], {"prepare_body"})
        self.assertEqual(names, [("_encode_files", "call-site", 602)])

    def test_the_definition_is_found_and_preferred_in_the_calling_file(self):
        definition = chunk("src/requests/models.py", 183, "    def _encode_files(files, data):\n        pass",
                           symbols=["_encode_files"])
        elsewhere = chunk("tests/test_models.py", 10, "def _encode_files(files, data):\n    pass", symbols=["_encode_files"])
        store = FakeStore([("symbols LIKE", [elsewhere, definition])])
        found = run(retriever.called_definition_chunks(store, "r", [self.PREPARE_BODY], ["files", "data"]))
        self.assertEqual(len(found), 1)
        row, call_site, name, location = found[0]
        self.assertEqual((row["file_path"], row["start_line"], call_site, name), ("src/requests/models.py", 183, "call-site", "_encode_files"))
        self.assertEqual(location, "src/requests/models.py:602")

    def test_followed_definition_and_its_call_site_are_both_kept(self):
        """BUG-06: evidence had models.py:637 but not the _encode_files body."""
        definition = chunk("src/requests/models.py", 108, "    def _encode_files(files, data):\n        pass",
                           symbols=["_encode_files"])
        other = [chunk(f"src/requests/f{n}.py", 1, "files data prepare") for n in range(8)]
        neighbour = chunk("src/requests/models.py", 1, "files data prepare head")
        sparse = [neighbour, self.PREPARE_BODY, chunk("src/requests/models.py", 300, "files data prepare tail"), *other]
        store = FakeStore([("symbols LIKE", [definition])])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")), \
             patch.object(retriever, "sparse_search", AsyncMock(return_value=sparse)), \
             patch.object(retriever, "absent_question_terms", AsyncMock(return_value=[])):
            result = run(retriever.retrieve_context(store, "r", "Why does prepare() fail when files and data are passed?", top_k=6))
        by_start = {(item["file_path"], item["start_line"]): item for item in result}
        self.assertIn(("src/requests/models.py", 108), by_start)
        self.assertIn("called_definition", by_start[("src/requests/models.py", 108)]["_retrieval_methods"])
        self.assertIn(("src/requests/models.py", 600), by_start)

    def test_followed_definition_ranks_right_below_its_call_site(self):
        """Live finding: with semantic votes on every keyword hit, a definition
        scored at a fraction of its call site fell behind them and was cut by
        the context budget."""
        definition = chunk("src/requests/models.py", 108, "    def _encode_files(files, data):\n        pass",
                           symbols=["_encode_files"])
        others = [chunk(f"src/requests/f{n}.py", 1, "files data prepare") for n in range(6)]
        ranked = [self.PREPARE_BODY, *others]
        store = FakeStore([("symbols LIKE", [definition])])
        with patch.object(retriever, "embed_query", return_value=[0.0] * 4), \
             patch.object(retriever, "sparse_search", AsyncMock(return_value=ranked)), \
             patch.object(retriever, "semantic_rerank_ids", AsyncMock(return_value=ranked)), \
             patch.object(retriever, "semantic_file_search", AsyncMock(return_value=[])), \
             patch.object(retriever, "semantic_chunks_in_files", AsyncMock(return_value=[])), \
             patch.object(retriever, "absent_question_terms", AsyncMock(return_value=[])):
            result = run(retriever.retrieve_context(store, "r", "Why does prepare() fail when files and data are passed?", top_k=8))
        order = [(item["file_path"], item["start_line"]) for item in result]
        self.assertEqual(order[:2], [("src/requests/models.py", 600), ("src/requests/models.py", 108)])

    def test_call_window_skips_docstring_examples(self):
        source = chunk("src/flask/helpers.py", 151, "\n".join([
            "def make_response(*args):", '    """Example::', "", "        @app.errorhandler(404)",
            "        def not_found(error):", "            return make_response(render())", '    """',
            "    return current_app.make_response(args)",
        ]))
        window = call_site_window(source, ["make_response"])
        self.assertEqual((window["start_line"], window["end_line"]), (151, 158))
        self.assertIsNone(window_chunk({**source, "content": "misaligned"}, 152, 153))


class FusionNoiseTests(unittest.TestCase):
    def fused(self, question, sparse, **routes):
        store = FakeStore(list(routes.get("routes", [])))
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")), \
             patch.object(retriever, "sparse_search", AsyncMock(return_value=sparse)), \
             patch.object(retriever, "absent_question_terms", AsyncMock(return_value=[])):
            return run(retriever.retrieve_context(store, "r", question, top_k=8)), store

    def test_excluded_tests_and_docs_are_removed(self):
        sparse = [chunk("docs/timeouts.md", 1, "timeout"), chunk("tests/test_timeouts.py", 1, "timeout"),
                  chunk("httpx/_config.py", 72, "class Timeout:")]
        result, _ = self.fused("Which library code (not tests or docs) applies the timeout?", sparse)
        self.assertEqual([item["file_path"] for item in result], ["httpx/_config.py"])

    def test_tooling_question_reads_the_root_manifest(self):
        manifest = chunk("package.json", 1, '{\n  "scripts": {"test": "mocha", "lint": "eslint ."}\n}')
        css = [chunk(f"examples/{n}/style.css", 1, "body { }") for n in range(4)]
        result, store = self.fused(
            "How do I run a single test file locally, and what lint or style rules does the project enforce?",
            css, routes=[("file_path IN (", [manifest])],
        )
        self.assertEqual(result[0]["file_path"], "package.json")
        self.assertIn("tooling_manifest", result[0]["_retrieval_methods"])
        self.assertTrue(any("package.json" in args for _, args in store.executed))

    def test_named_identifiers_missing_from_the_evidence_are_reported(self):
        from api.query_router import _merge_retrieval_plan

        result, _ = self.fused("How does _encode_files handle integer keys?", [chunk("src/a.py", 1, "def other(): pass")])
        self.assertEqual(result[0]["_evidence_plan"]["unretrieved_identifiers"], ["_encode_files"])
        # The signal reaches the plan the client and the answer model see.
        plan = build_evidence_plan("How does _encode_files handle integer keys?")
        _merge_retrieval_plan(plan, result)
        self.assertEqual(plan["unretrieved_identifiers"], ["_encode_files"])

    def test_path_hint_samples_rank_by_question_terms(self):
        store = FakeStore()
        run(retriever.path_hint_chunks_batch(store, "r", ["auth"], terms=["refresh", "token"]))
        sql, args = store.executed[0]
        self.assertIn("instr(lower(c.content), ?)", sql)
        self.assertEqual(args[3:], ["refresh", "token", "r", 2])


if __name__ == "__main__":
    unittest.main()


class AbsentTermTests(unittest.TestCase):
    """Round 2: answers said 'the term "tour" does not appear in the indexed
    code' because every unmatched question word was probed and reported."""

    def test_only_words_that_name_something_are_probed(self):
        names = retriever.term_names_something
        for term, question in (("rabbitmq", "Where does this publish to RabbitMQ?"),
                               ("kafka", "Does it consume from Kafka topics?"),
                               ("tour", "Where is `tour` defined?"),
                               ("my_handler", "who calls my_handler")):
            with self.subTest(term=term):
                self.assertTrue(names(term, question))
        for term, question in (("tour", "Give me a tour of the request lifecycle."),
                               ("summary", "Summary of the blast radius of changing make_response?"),
                               ("blast", "Summary of the blast radius of changing make_response?"),
                               ("radius", "Summary of the blast radius of changing make_response?")):
            with self.subTest(term=term):
                self.assertFalse(names(term, question))

    def test_ordinary_words_never_reach_the_index_probe(self):
        sparse = [chunk("src/app.py", 1, "def make_response(rv):\n    return rv")]
        probe = AsyncMock(return_value=[])
        with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("offline")), \
             patch.object(retriever, "sparse_search", AsyncMock(return_value=sparse)), \
             patch.object(retriever, "absent_question_terms", probe):
            run(retriever.retrieve_context(FakeStore([]), "r", "Give a tour and summary of make_response; does it use RabbitMQ?", top_k=4))
        probed = [term for call in probe.await_args_list for term in call.args[2]]
        self.assertIn("rabbitmq", probed)
        for word in ("tour", "summary"):
            self.assertNotIn(word, probed)
