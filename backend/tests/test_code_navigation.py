"""Exact code navigation: line search, file lookup, symbol jumps, outlines.

* Code search reports every line that contains the query, one result per
  file (never one per section), declarations of the searched name first. A
  highlighted line always contains the query: multi-word queries show the
  lines where the words appear together, else lines with all of them.
* File lookup finds a file by its own name or a partial path; the file tree
  lists source folders before tests, examples and dot-folders.
* Symbol lookup returns the exact declaring line, even deep inside a section.
* The file view carries an outline whose ranges give the enclosing symbol.

Every test is offline and uses a temporary embedded libSQL database seeded
through the real chunker, so sections overlap and split as in production.
"""

import random
import re
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import api.explore_router as explore
from database import TursoStore, apply_migrations
from ingest.chunker import chunk_file_content
from retrieval import code_search, retriever
from retrieval.code_search import (
    file_outline,
    find_files,
    list_directory,
    match_line,
    parse_code_query,
    path_match_tier,
    symbol_match_tier,
)

USER = "nav-user"


def numbered(total: int, lines: dict[int, str], filler: str = "    value_{n} = {n}") -> str:
    """A file of ``total`` lines with ``lines`` at fixed line numbers."""
    return "\n".join(lines.get(number, filler.format(n=number)) for number in range(1, total + 1)) + "\n"


def brute_force(files: dict[str, str], needles: list[str]) -> dict[str, list[int]]:
    """Every line of every file that contains all ``needles`` (whitespace-insensitive), read straight from the text."""
    patterns = [re.compile(r"\s+".join(re.escape(part) for part in needle.split()), re.IGNORECASE) for needle in needles]
    found: dict[str, list[int]] = {}
    for path, text in files.items():
        lines = [number for number, line in enumerate(text.split("\n"), 1) if all(p.search(line) for p in patterns)]
        if lines:
            found[path] = lines
    return found


# Laid out like pallets/click src/click/types.py: the lines the customer
# searched for sit where they sit in click.
CLICK_TYPES = numbered(480, {
    1: '"""Parameter types."""', 2: "import typing as t", 3: "",
    59: "", 60: "class ParamTypeInfoDict(t.TypedDict):", 61: "    param_type: str", 62: "    name: str", 63: "", 64: "",
    65: "class ParamType(t.Generic[_ValueT_co, _InputT_contra], abc.ABC):",
    66: '    """Represents the type of a parameter."""',
    179: "    def convert(", 180: "        self, value: t.Any, param: Parameter | None, ctx: Context | None",
    181: "    ) -> t.Any:", 182: "        return value", 240: "", 241: "",
    242: "class CompositeParamType(ParamType):", 263: "",
    264: "class FuncParamType(ParamType):", 275: "    def convert(", 276: "        self, value, param, ctx",
    277: "    ) -> t.Any:", 278: "        return self.func(value)", 289: "",
    290: "class UnprocessedParamType(ParamType):", 295: "    def convert(", 296: "        self, value, param, ctx",
    297: "    ) -> t.Any:", 298: "        return value", 303: "",
    304: "class StringParamType(ParamType):", 307: "    def convert(", 308: "        self, value, param, ctx",
    309: "    ) -> t.Any:", 310: "        return str(value)", 439: "",
    440: "class Choice(ParamType):", 454: "    def convert(", 455: "        self, value, param, ctx",
    456: "    ) -> t.Any:", 457: "        return value", 469: "",
    470: "def convert_type(ty: t.Any | None = None, default: t.Any | None = None) -> ParamType:",
    471: "    return STRING",
})
CLICK_TEST = numbered(40, {
    1: "import click", 2: "",
    3: "class FuncParamType(click.ParamType):",
    4: "    def convert(self, value, param, ctx):",
    5: "        return value",
    6: "", 7: "def test_param_type_class():",
    8: "    # class names are not searched for: ParamType is",
    9: "    assert issubclass(FuncParamType, click.ParamType)",
})
CLICK_CORE = numbered(1500, {
    1: '"""Core."""', 2: "",
    1200: "class Command:", 1201: '    """A command."""',
    1396: "    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:",
    1397: "        if not args and self.no_args_is_help:", 1398: "            raise NoArgsIsHelpError(ctx)",
    1399: "        return args",
}, filler="    attr_{n} = {n}")
HTTPX_CLIENT = numbered(1700, {
    1: "from __future__ import annotations", 2: "",
    200: "class BaseClient:", 201: '    """Shared client behaviour."""',
    584: "    def _set_timeout(self, request: Request) -> None:",
    585: "        if 'timeout' not in request.extensions:",
    586: "            request.extensions = dict(**request.extensions, timeout=self.timeout.as_dict())",
    600: "class Client(BaseClient):",
    910: "        self._set_timeout(request)",
    1036: "    def get(",
    1037: "        self,",
    1038: "        url: URL | str,",
    1039: "    ) -> Response:",
    1300: "class AsyncClient(BaseClient):",
    1625: "        self._set_timeout(request)",
}, filler="        step_{n} = {n}")
REQUESTS_MODELS = numbered(260, {
    1: '"""Request models."""', 34: "from urllib3.fields import RequestField",
    150: "class RequestEncodingMixin:",
    199: "        new_fields: list[RequestField | tuple[str, bytes]] = []",
    245: "            rf = RequestField(name=k, data=fdata, filename=fn, headers=fh)",
}, filler="        part_{n} = {n}")
EXPRESS_RESPONSE = "\n".join([
    "'use strict';",
    "var res = Object.create(http.ServerResponse.prototype);",
    "",
    "res.status = function status(code) {",
    "  this.statusCode = code;",
    "  return this;",
    "};",
    "",
    "res.send = function send(body) {",
    "  var chunk = body;",
    "  return this;",
    "};",
    "",
]) + "\n"
README = "# Project\n\nIntro.\n\n## Install\n\n```sh\n# not a heading\npip install x\n```\n\n## Usage\n\nRun it.\n"

CLICK_FILES = {
    "src/click/types.py": CLICK_TYPES,
    "src/click/core.py": CLICK_CORE,
    "tests/test_types.py": CLICK_TEST,
    "docs/parameter-types.md": "# Parameter types\n\nSubclass `ParamType` and implement `convert`.\n",
    "README.md": README,
}
HTTPX_FILES = {
    "httpx/_client.py": HTTPX_CLIENT,
    "httpx/_config.py": "class Timeout:\n    def as_dict(self):\n        return {}\n",
    "tests/client/test_client.py": "def test_client_timeout():\n    client = httpx.Client(timeout=1)\n",
    "tests/client/test_async_client.py": "async def test_async_client():\n    pass\n",
    "docs/advanced/clients.md": "# Clients\n",
}
EXPRESS_FILES = {
    "lib/response.js": EXPRESS_RESPONSE,
    "lib/request.js": "var req = {};\nmodule.exports = req;\n",
    "lib/express.js": "exports = module.exports = createApplication;\n",
    "test/res.send.js": "describe('res.send', function () {});\n",
    "examples/hello-world/index.js": "var express = require('../../');\n",
    ".github/workflows/ci.yml": "name: ci\n",
    "index.js": "module.exports = require('./lib/express');\n",
    "Readme.md": "# express\n",
    "package.json": '{"name": "express"}\n',
}


class NavigationStoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'nav.db'}", "local-test-token")
        await apply_migrations(self.store)
        self.user = SimpleNamespace(id=USER)
        code_search._PATHS_CACHE.clear()

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()
        code_search._PATHS_CACHE.clear()

    async def add_repo(self, name: str, files: dict[str, str], *, prefix: str = "") -> tuple[str, int]:
        repo_id = f"repo-{uuid.uuid4()}"
        rows = []
        for path, text in files.items():
            for chunk in chunk_file_content(text, path):
                rows.append({**chunk, "id": f"{prefix}{uuid.uuid4()}", "repo_id": repo_id, "embedding": None})
        now = datetime.now(UTC).isoformat()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ready', ?, ?, ?)",
            [repo_id, USER, name, f"https://github.com/acme/{name}.git", len(rows), now, now],
        )
        await self.store.insert_chunks(rows)
        for path, text in files.items():
            await self.store.execute(
                "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
                [repo_id, path, "h", len(text.encode()), now],
            )
        return repo_id, len(rows)

    async def search(self, repo: tuple[str, int], query: str, **options) -> list[dict]:
        """Both candidate paths (full-text index and substring scan) must agree."""
        repo_id, count = repo
        indexed = await code_search.search_code(self.store, repo_id, query, **options)
        scanned = await code_search.search_code(self.store, repo_id, query, chunk_count=count, **options)
        self.assertEqual(indexed["results"], scanned["results"], f"index and scan disagree for {query!r}")
        self.assertEqual(indexed["mode"], scanned["mode"])
        return indexed

    def patched(self):
        return (
            patch.object(explore, "get_turso_store", return_value=self.store),
            patch.object(explore, "assert_turso_schema", new=AsyncMock()),
            patch.object(explore, "fetch_raw_file", new=AsyncMock(return_value=None)),
        )


# ---------------------------------------------------------------------------
# BUG-03: code search
# ---------------------------------------------------------------------------


class CodeSearchTests(NavigationStoreCase):
    async def test_class_definition_is_listed_first_and_every_line_contains_the_phrase(self):
        click = await self.add_repo("click", CLICK_FILES)
        found = await self.search(click, "class ParamType")
        self.assertEqual(found["mode"], "phrase")
        first = found["results"][0]
        self.assertEqual((first["file_path"], first["matches"][0]["line"]), ("src/click/types.py", 65))
        self.assertTrue(first["matches"][0]["definition"])
        for result in found["results"]:
            for match in result["matches"]:
                self.assertIn("class paramtype", match["text"].lower(), f"{result['file_path']}:{match['line']}")
                start, end = match["spans"][0]
                self.assertEqual(match["text"][start:end].lower(), "class paramtype")
        # "class FuncParamType(click.ParamType):" has both words, not the phrase.
        self.assertNotIn("tests/test_types.py", [result["file_path"] for result in found["results"]])

    async def test_every_definition_of_a_method_is_listed_once_per_file(self):
        click = await self.add_repo("click", CLICK_FILES)
        found = await self.search(click, "def convert")
        paths = [result["file_path"] for result in found["results"]]
        self.assertEqual(len(paths), len(set(paths)), "a file appears once")
        types = found["results"][0]
        self.assertEqual(types["file_path"], "src/click/types.py")
        lines = [match["line"] for match in types["matches"]]
        self.assertEqual(lines[:5], [179, 275, 295, 307, 454])
        self.assertTrue(all(match["definition"] for match in types["matches"][:5]))
        # ``def convert_type`` contains the text but declares another name.
        self.assertEqual(lines[-1], 470)
        self.assertFalse(types["matches"][-1]["definition"])
        self.assertEqual(types["match_count"], 6)
        self.assertEqual(found["line_count"], sum(result["match_count"] for result in found["results"]))
        # Source before tests.
        self.assertLess(paths.index("src/click/types.py"), paths.index("tests/test_types.py"))

    async def test_private_method_and_its_calls(self):
        httpx = await self.add_repo("httpx", HTTPX_FILES)
        found = await self.search(httpx, "_set_timeout")
        self.assertEqual(found["mode"], "exact")
        self.assertEqual([result["file_path"] for result in found["results"]], ["httpx/_client.py"])
        matches = found["results"][0]["matches"]
        self.assertEqual([match["line"] for match in matches], [584, 910, 1625])
        self.assertEqual([match["definition"] for match in matches], [True, False, False])
        self.assertEqual(matches[1]["text"], "self._set_timeout(request)")
        self.assertEqual(matches[1]["spans"], [[5, 17]])

    async def test_identifier_lines_in_one_file(self):
        requests = await self.add_repo("requests", {"src/requests/models.py": REQUESTS_MODELS})
        found = await self.search(requests, "RequestField")
        self.assertEqual([match["line"] for match in found["results"][0]["matches"]], [34, 199, 245])

    async def test_quoted_phrase_is_exact_and_words_fall_back_to_all_on_one_line(self):
        click = await self.add_repo("click", CLICK_FILES)
        quoted = await self.search(click, '"ParamType):"')
        texts = [match["text"] for result in quoted["results"] for match in result["matches"]]
        self.assertTrue(texts and all("ParamType):" in text for text in texts))
        self.assertNotIn("class ParamType(t.Generic[_ValueT_co, _InputT_contra], abc.ABC):", texts)
        # No line has "ParamType assert" together: every word, anywhere on the line.
        loose = await self.search(click, "ParamType assert")
        self.assertEqual(loose["mode"], "all_words")
        self.assertEqual(
            [(result["file_path"], match["line"]) for result in loose["results"] for match in result["matches"]],
            [("tests/test_types.py", 9)],
        )
        # A quoted phrase and a word: both on the line.
        both = await self.search(click, '"def convert" ctx')
        self.assertEqual([(r["file_path"], [m["line"] for m in r["matches"]]) for r in both["results"]], [("tests/test_types.py", [4])])
        nothing = await self.search(click, '"class ParamType assert"')
        self.assertEqual(nothing["results"], [])

    async def test_matches_inside_longer_identifiers_are_found_by_the_scan(self):
        click = await self.add_repo("click", CLICK_FILES)
        repo_id, count = click
        scanned = await code_search.search_code(self.store, repo_id, "aramTypeInfo", chunk_count=count)
        self.assertEqual([(r["file_path"], r["matches"][0]["line"]) for r in scanned["results"]], [("src/click/types.py", 60)])
        # A large repository falls back to the scan when the word index finds nothing.
        indexed = await code_search.search_code(self.store, repo_id, "aramTypeInfo")
        self.assertEqual(indexed["results"], scanned["results"])

    async def test_refresh_rows_stay_hidden(self):
        repo_id, _count = await self.add_repo("httpx", HTTPX_FILES)
        staged = [{**chunk, "id": f"run-2:{uuid.uuid4()}", "repo_id": repo_id, "embedding": None}
                  for chunk in chunk_file_content("def _set_timeout_staged():\n    pass\n", "httpx/_new.py")]
        await self.store.insert_chunks(staged)
        found = await code_search.search_code(self.store, repo_id, "_set_timeout", staging_prefix="run-2:")
        self.assertEqual([result["file_path"] for result in found["results"]], ["httpx/_client.py"])

    async def test_one_result_per_file_and_the_limit_counts_files(self):
        files = {f"pkg/module_{n}.py": numbered(400, {10 * k: "    marker_call()" for k in range(1, 30)}) for n in range(6)}
        repo = await self.add_repo("many", files)
        found = await self.search(repo, "marker_call", limit=4)
        self.assertEqual(len(found["results"]), 4)
        self.assertTrue(found["more_files"])
        self.assertEqual(len({result["file_path"] for result in found["results"]}), 4)
        self.assertTrue(all(result["match_count"] == 29 for result in found["results"]))

    async def test_long_lines_are_cut_around_the_match(self):
        line = "x = '" + "a" * 600 + "needle_here" + "b" * 600 + "'"
        repo = await self.add_repo("long", {"gen.py": f"{line}\n"})
        match = (await self.search(repo, "needle_here"))["results"][0]["matches"][0]
        self.assertLessEqual(len(match["text"]), 245)
        start, end = match["spans"][0]
        self.assertEqual(match["text"][start:end], "needle_here")
        self.assertTrue(match["text"].startswith("…") and match["text"].endswith("…"))

    async def test_the_retriever_keeps_its_list_api(self):
        repo_id, _count = await self.add_repo("httpx", HTTPX_FILES)
        results = await retriever.search_code(self.store, repo_id, "_set_timeout", limit=5)
        self.assertEqual(results[0]["file_path"], "httpx/_client.py")
        self.assertEqual(results[0]["start_line"], 584)

    async def test_a_phrase_deep_in_a_long_file_is_found(self):
        # Like httpx tests/models/whatwg.json: 36 sections that all mention
        # "in" and "base" apart, and one line, past section 30, that has
        # them together. Another file has both words on one line, apart.
        files = {
            "tests/models/whatwg.json": numbered(4000, {3900: '  "# Credentials in base",'}, filler="  \"base_{n}\": \"in\","),
            "httpx/_urls.py": "def join(base, url):\n    return base if url in schemes else url\n",
        }
        repo = await self.add_repo("httpx", files)
        self.assertGreater(repo[1], 30)
        for query, mode in (("in base", "phrase"), ('"in base"', "exact")):
            found = await self.search(repo, query)
            self.assertEqual(found["mode"], mode, query)
            self.assertEqual([(r["file_path"], [m["line"] for m in r["matches"]]) for r in found["results"]],
                             [("tests/models/whatwg.json", [3900])], query)
            self.assertFalse(found["more_files"])

    async def test_common_word_phrases_agree_with_reading_every_line(self):
        # Long files of common words (27 sections each, all of them read):
        # each phrase is on a few lines, while its words are in every section.
        rng = random.Random(3)
        vocabulary = ["if", "not", "in", "base", "return", "self", "import", "os", "value", "name", "path", "data",
                      "for", "with", "none", "is", "and", "or", "type", "key"]
        files = {
            f"pkg/module_{n}.py": "\n".join(f"    # {' '.join(rng.sample(vocabulary, 4))}" for _ in range(3000)) + "\n"
            for n in range(12)
        }
        files["pkg/small.py"] = "def run(base):\n    if not base:\n        return self.base in path\n"
        repo = await self.add_repo("words", files)
        for query in ("if not", "in base", "return self", "import os", "key type", '"not in"', "data path name"):
            found = await self.search(repo, query)
            truth = brute_force(files, code_search.parse_code_query(query).needles(found["mode"]))
            if found["mode"] == "all_words":
                self.assertEqual(brute_force(files, code_search.parse_code_query(query).needles("phrase")), {}, query)
            self.assertEqual({r["file_path"] for r in found["results"]}, set(truth), query)
            self.assertFalse(found["more_files"], query)
            for result in found["results"]:
                lines = [m["line"] for m in result["matches"]]
                expected = truth[result["file_path"]]
                # Lines in order from the top of the file; a long file may
                # stop early, and says so.
                self.assertEqual(lines, expected[: len(lines)], f"{query} {result['file_path']}")
                if not result["truncated"]:
                    self.assertEqual(result["match_count"], len(expected), f"{query} {result['file_path']}")

    async def test_search_reads_on_until_a_line_matches(self):
        # Every section has both words, on separate lines; only the last
        # section of one file has them on one line. A small read budget
        # must not end the search with "no matches".
        filler = numbered(1200, {}, filler="    alpha_{n} = 1\n    beta_{n} = 2")
        files = {f"pkg/module_{n}.py": filler for n in range(4)}
        files["pkg/zz_last.py"] = numbered(1200, {1190: "    beta = alpha"}, filler="    alpha_{n} = 1\n    beta_{n} = 2")
        repo = await self.add_repo("budget", files)
        with patch.object(code_search, "_READ_ROWS_TOTAL", 8):
            found = await self.search(repo, "alpha beta")
        self.assertEqual(found["mode"], "all_words")
        self.assertEqual([(r["file_path"], [m["line"] for m in r["matches"]]) for r in found["results"]],
                         [("pkg/zz_last.py", [2379])])

    async def test_unread_candidate_files_keep_more_files(self):
        files = {f"pkg/module_{n}.py": "def handler():\n    return marker_call()\n" for n in range(6)}
        repo_id, count = await self.add_repo("many", files)
        with patch.object(code_search, "_READ_ROWS_TOTAL", 2), patch.object(code_search, "_ROW_BATCH", 1):
            found = await code_search.search_code(self.store, repo_id, "marker_call", limit=5, chunk_count=count)
        self.assertEqual(len(found["results"]), 2)
        self.assertTrue(found["more_files"])

    async def test_a_name_typed_with_capitals_ranks_its_exact_declaration_first(self):
        files = {
            "httpx/_client.py": "class Client:\n    @property\n    def headers(self) -> Headers:\n        return self._headers\n",
            "httpx/_models.py": "class Headers(typing.MutableMapping[str, str]):\n    pass\n",
        }
        repo = await self.add_repo("httpx", files)
        upper = await self.search(repo, "Headers")
        self.assertEqual([r["file_path"] for r in upper["results"]], ["httpx/_models.py", "httpx/_client.py"])
        self.assertEqual([(m["line"], m["definition"]) for m in upper["results"][0]["matches"]], [(1, True)])
        # "def headers" declares another name, as far as "Headers" goes.
        self.assertEqual([(m["line"], m["definition"]) for m in upper["results"][1]["matches"]], [(3, False), (4, False)])
        self.assertEqual(upper["results"][1]["definitions"], 0)
        # Typed in lower case, both declare it; the exact case ranks first.
        lower = await self.search(repo, "headers")
        self.assertEqual([r["file_path"] for r in lower["results"]], ["httpx/_client.py", "httpx/_models.py"])
        self.assertEqual([r["matches"][0]["definition"] for r in lower["results"]], [True, True])

    async def test_spans_count_like_javascript_strings(self):
        repo = await self.add_repo("httpx", {"README.md": "# HTTPX\n\n## 🚀 Quick start with httpx\n"})
        match = (await self.search(repo, "httpx"))["results"][0]["matches"]
        line = next(item for item in match if item["line"] == 3)
        start, end = line["spans"][0]
        # JavaScript indexes UTF-16 code units: the rocket counts twice.
        self.assertEqual(line["text"].encode("utf-16-le")[start * 2: end * 2].decode("utf-16-le"), "httpx")
        self.assertEqual((start, end), (23, 28))


class QueryParsingTests(unittest.TestCase):
    def test_phrases_and_words(self):
        parsed = parse_code_query('  "def  convert"  self ctx "unterminated ')
        self.assertEqual(parsed.phrases, ("def convert",))
        self.assertEqual(parsed.words, ("self", "ctx", "unterminated"))
        self.assertEqual(parsed.needles("phrase"), ["def convert", "self ctx unterminated"])
        self.assertEqual(parsed.needles("all_words"), ["def convert", "self", "ctx", "unterminated"])

    def test_line_matching_is_case_insensitive_and_marks_whole_words(self):
        patterns = [code_search._needle_pattern("class  paramtype")]
        spans, whole = match_line("class ParamType(abc.ABC):", patterns)
        self.assertEqual((spans, whole), ([(0, 15)], True))
        spans, whole = match_line("class ParamTypeInfoDict(t.TypedDict):", patterns)
        self.assertFalse(whole)
        self.assertIsNone(match_line("class FuncParamType(ParamType):", patterns))


# ---------------------------------------------------------------------------
# FEAT-01 / BUG-11 / FEAT-03: files
# ---------------------------------------------------------------------------


class FileLookupTests(unittest.TestCase):
    HTTPX = [(path, 1) for path in sorted(HTTPX_FILES)]

    def test_a_file_is_found_by_its_own_name_and_by_a_partial_path(self):
        for query in ("_client", "_client.py", "httpx/_cl", "HTTPX/_CLIENT"):
            files, _total = find_files(self.HTTPX, query)
            self.assertEqual(files[0]["path"], "httpx/_client.py", query)
        files, total = find_files(self.HTTPX, "client")
        self.assertEqual(files[0]["path"], "httpx/_client.py")
        self.assertEqual(total, 4)

    def test_tiers(self):
        self.assertEqual(path_match_tier("src/click/types.py", "types.py"), 0)
        self.assertEqual(path_match_tier("lib/response.js", "resp"), 1)
        self.assertEqual(path_match_tier("lib/response.js", "rspns"), 5)
        self.assertIsNone(path_match_tier("tests/test_types/__init__.py", "types.py"))
        self.assertIsNone(path_match_tier("lib/response.js", ""))

    def test_tree_lists_source_folders_before_tests_examples_and_dot_folders(self):
        paths = [(path, 1) for path in sorted(EXPRESS_FILES)]
        root = list_directory(paths, "")
        self.assertEqual([item["name"] for item in root["directories"]], ["lib", "examples", "test", ".github"])
        self.assertEqual([item["name"] for item in root["files"]], ["index.js", "package.json", "Readme.md"])
        lib = list_directory(paths, "/lib/")
        self.assertEqual(lib["path"], "lib")
        self.assertEqual([item["path"] for item in lib["files"]], ["lib/express.js", "lib/request.js", "lib/response.js"])
        self.assertEqual(list_directory(paths, "examples")["directories"], [{"path": "examples/hello-world", "name": "hello-world", "files": 1}])


# ---------------------------------------------------------------------------
# FEAT-01 / FEAT-02: symbols and outlines
# ---------------------------------------------------------------------------


class SymbolTests(NavigationStoreCase):
    async def test_symbols_point_at_the_exact_declaring_line(self):
        repo_id, _count = await self.add_repo("click", CLICK_FILES)
        symbols = await code_search.find_symbols(self.store, repo_id, "ParamType")
        self.assertEqual(
            {key: symbols[0][key] for key in ("kind", "name", "path", "line")},
            {"kind": "class", "name": "ParamType", "path": "src/click/types.py", "line": 65},
        )
        names = [symbol["name"] for symbol in symbols]
        self.assertIn("ParamTypeInfoDict", names)
        self.assertIn("FuncParamType", names)
        # Source declarations before the test file's.
        func = [symbol["path"] for symbol in symbols if symbol["name"] == "FuncParamType"]
        self.assertEqual(func[0], "src/click/types.py")
        parse = await code_search.find_symbols(self.store, repo_id, "parse_args")
        self.assertEqual((parse[0]["path"], parse[0]["line"], parse[0]["kind"]), ("src/click/core.py", 1396, "def"))
        converts = await code_search.find_symbols(self.store, repo_id, "convert", limit=10)
        self.assertEqual(
            [symbol["line"] for symbol in converts if symbol["path"] == "src/click/types.py" and symbol["name"] == "convert"],
            [179, 275, 295, 307, 454],
        )

    def test_match_tiers(self):
        self.assertEqual(symbol_match_tier("ParamType", "ParamType"), 0)
        self.assertEqual(symbol_match_tier("ParamType", "paramtype"), 1)
        self.assertEqual(symbol_match_tier("ParamTypeInfoDict", "ParamType"), 2)
        self.assertEqual(symbol_match_tier("_set_timeout", "timeout"), 3)
        self.assertEqual(symbol_match_tier("FuncParamType", "ParamType"), 3)
        self.assertEqual(symbol_match_tier("myconvert", "convert"), 4)
        self.assertIsNone(symbol_match_tier("convert", "parse"))

    def test_outline_ranges_give_the_enclosing_symbol(self):
        outline = file_outline(CLICK_CORE, "src/click/core.py")
        chain = [f"{entry['kind']} {entry['name']}" for entry in outline if entry["line"] <= 1398 <= entry["end_line"]]
        self.assertEqual(chain, ["class Command", "def parse_args"])
        parse = next(entry for entry in outline if entry["name"] == "parse_args")
        self.assertEqual((parse["line"], parse["end_line"], parse["depth"]), (1396, 1399, 1))
        types = file_outline(CLICK_TYPES, "src/click/types.py")
        convert = next(entry for entry in types if entry["line"] == 179)
        # The multi-line signature's ") -> t.Any:" does not end the method.
        self.assertEqual((convert["end_line"], convert["depth"]), (182, 1))
        express = file_outline(EXPRESS_RESPONSE, "lib/response.js")
        self.assertEqual([(entry["name"], entry["line"], entry["end_line"]) for entry in express], [("status", 4, 7), ("send", 9, 12)])

    def test_markdown_outline_lists_headings_outside_code_fences(self):
        outline = file_outline(README, "README.md")
        self.assertEqual([(entry["name"], entry["kind"], entry["depth"]) for entry in outline],
                         [("Project", "h1", 0), ("Install", "h2", 1), ("Usage", "h2", 1)])
        self.assertEqual(outline[1]["end_line"], 11)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class NavigationApiTests(NavigationStoreCase):
    async def test_search_files_symbols_and_file_outline_endpoints(self):
        await self.add_repo("httpx", HTTPX_FILES)
        await self.add_repo("express", EXPRESS_FILES)
        store_patch, schema_patch, raw_patch = self.patched()
        with store_patch, schema_patch, raw_patch:
            search = await explore.repository_search("httpx", q="_set_timeout", limit=25, current_user=self.user)
            self.assertEqual((search["mode"], search["searchable"], search["message"]), ("exact", True, None))
            self.assertEqual([m["line"] for m in search["results"][0]["matches"]], [584, 910, 1625])
            missing = await explore.repository_search("httpx", q="no_such_name_here", limit=25, current_user=self.user)
            self.assertIn("lines that contain this exact text", missing["message"])

            found = await explore.repository_files("httpx", q="_client", directory=None, limit=20, current_user=self.user)
            self.assertEqual(found["files"][0]["path"], "httpx/_client.py")
            self.assertEqual(found["total_files"], len(HTTPX_FILES))
            root = await explore.repository_files("express", q=None, directory=None, limit=20, current_user=self.user)
            self.assertEqual([item["name"] for item in root["directories"]], ["lib", "examples", "test", ".github"])
            lib = await explore.repository_files("express", q=None, directory="lib", limit=20, current_user=self.user)
            self.assertIn("lib/response.js", [item["path"] for item in lib["files"]])
            with self.assertRaises(HTTPException) as refused:
                await explore.repository_files("express", q=None, directory="lib/../..", limit=20, current_user=self.user)
            self.assertEqual(refused.exception.status_code, 422)

            symbols = await explore.repository_symbols("httpx", q="_set_timeout", limit=20, current_user=self.user)
            self.assertEqual([(s["path"], s["line"], s["container"]) for s in symbols["symbols"]], [("httpx/_client.py", 584, None)])

            view = await explore.repository_file("httpx", path="httpx/_client.py", current_user=self.user, response=None)
            chain = [entry["name"] for entry in view["outline"] if entry["line"] <= 1036 <= entry["end_line"]]
            self.assertEqual(chain, ["Client", "get"])
