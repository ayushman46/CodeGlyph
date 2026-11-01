"""Change impact: graph upgrades, ambiguous paths, depth, affected tests, symbols.

Covers BUG-10 (an index whose graph predates package-import resolution is
rebuilt from the indexed text), BUG-15 (ambiguous matches are listed, not
merged; extensionless paths resolve) and FEAT-05 (dependents by depth,
affected tests and the test functions for a symbol). Database tests use a
temporary embedded libSQL file.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import api.impact as impact
import api.repos_router as repos
from database import Statement, TursoStore, apply_migrations
from ingest.dependencies import GRAPH_VERSION
from ingest.pipeline import persist_dependency_manifest
from test_import_resolution import FLASK_LIKE

USER = "user-1"


# ---------------------------------------------------------------------------
# Test functions for a symbol
# ---------------------------------------------------------------------------


class SymbolTests(unittest.TestCase):
    def test_python_tests_that_reference_or_are_named_after_a_symbol(self):
        text = FLASK_LIKE["tests/test_basic.py"] + (
            "\nclass TestResponses:\n"
            "    def test_body(self):\n"
            "        src = '''\n"
            "make nothing here\n"
            "'''\n"
            "        assert make_response(src)\n"
        )
        found = {(item["name"], item["line"], item["kind"]) for item in impact.find_symbol_tests(text, "tests/test_basic.py", "make_response")}
        self.assertIn(("test_response_types", 5, "name"), found)
        self.assertIn(("test_make_response", 11, "reference"), found)
        # A column-0 line inside a string does not end the method's scope.
        self.assertIn(("TestResponses.test_body", 16, "reference"), found)
        items = {item["name"]: item for item in impact.find_symbol_tests(text, "tests/test_basic.py", "make_response")}
        self.assertEqual(items["test_make_response"]["reference_line"], 12)
        self.assertEqual(items["test_response_types"]["matched"], "response")

    def test_javascript_tests_use_their_titles(self):
        text = (
            "describe('res', function () {\n"
            "  describe('.sendStatus(statusCode)', function () {\n"
            "    it('should send the status code', function (done) {\n"
            "      var app = express();\n"
            "      app.use(function (req, res) { res.sendStatus(201) })\n"
            "    })\n"
            "  })\n"
            "})\n"
        )
        found = impact.find_symbol_tests(text, "test/res.sendStatus.js", "sendStatus")
        self.assertEqual([(item["name"], item["line"], item["kind"]) for item in found],
                         [(".sendStatus(statusCode) › should send the status code", 3, "reference")])

    def test_one_line_tests_and_dotted_titles(self):
        text = (
            "describe('math', () => {\n"
            "  test('product of two numbers', () => expect(multiply(2, 3)).toBe(6))\n"
            "  test('product with zero', () => {\n"
            "    expect(multiply(0, 3)).toBe(0)\n"
            "  })\n"
            "  test('multiply is listed in the title only', () => {})\n"
            "})\n"
        )
        found = [(item["name"], item["kind"], item["reference_line"]) for item in impact.find_symbol_tests(text, "test/math.test.js", "multiply")]
        self.assertEqual(found, [
            ("math › product of two numbers", "reference", 2),
            ("math › product with zero", "reference", 4),
            # The title names it but the body never calls it.
            ("math › multiply is listed in the title only", "name", None),
        ])
        dotted = impact.find_symbol_tests(
            "it('sendStatus should set res.statusCode', function () {\n  res.status(201)\n})\n", "test/res.js", "sendStatus",
        )
        self.assertEqual([(item["name"], item["kind"]) for item in dotted], [("sendStatus should set res.statusCode", "name")])
        # A one-line Python test, and a fixture parameter named after the symbol.
        python = impact.find_symbol_tests(
            "def test_one(): assert make_response()\ndef test_two(make_response):\n    pass\n", "tests/test_x.py", "make_response",
        )
        self.assertEqual([(item["name"], item["kind"], item["reference_line"]) for item in python],
                         [("test_one", "reference", 1), ("test_two", "reference", 2)])

    def test_symbol_words_drop_generic_verbs(self):
        self.assertEqual(impact.symbol_words("make_response"), ["response"])
        self.assertEqual(impact.symbol_words("acceptsCharsets"), ["accepts", "charsets"])
        self.assertEqual(impact.normalise_symbol(" Flask.make_response() "), "make_response")
        with self.assertRaises(ValueError):
            impact.normalise_symbol("make response; drop")

    def test_test_files_exclude_helpers_and_fixture_apps(self):
        self.assertTrue(impact.is_test_file("tests/test_basic.py"))
        self.assertTrue(impact.is_test_file("test/app.router.js"))
        self.assertTrue(impact.is_test_file("src/button.test.tsx"))
        self.assertFalse(impact.is_test_file("tests/conftest.py"))
        self.assertFalse(impact.is_test_file("tests/test_apps/cliapp/app.py"))
        self.assertFalse(impact.is_test_file("test/support/utils.js"))
        self.assertFalse(impact.is_test_file("src/flask/testing.py"))
        self.assertFalse(impact.is_test_file("cypress/support/commands.js"))
        self.assertFalse(impact.is_test_file("tests/testserver/server.py"))
        self.assertFalse(impact.is_test_file("tests/utils.py"))

    def test_test_files_next_to_code_and_unittest_names_count(self):
        # A test named as one counts in any folder; helper folders only matter
        # inside a test tree, for files that are tests only by their folder.
        for path in (
            "src/utils/date.test.ts", "src/utils/__tests__/format.spec.ts", "packages/core/src/data/store.test.ts",
            "lib/helpers/url.test.js", "src/mocks/server.test.ts", "tests/unit/utils/formatDate.test.ts",
            "src/utils/__tests__/format.js",
            # unittest and Django defaults.
            "polls/tests.py", "tests/testapi.py", "tests/api/testviews.py", "pkg/client_test.py",
        ):
            self.assertTrue(impact.is_test_file(path), path)
        for path in ("test/helpers/index.ts", "test/fixtures/app.js", "tests/__init__.py", "src/click/testing.py"):
            self.assertFalse(impact.is_test_file(path), path)


class TargetMatchingTests(unittest.TestCase):
    PATHS = [
        "src/flask/__init__.py", "src/flask/app.py", "src/flask/sansio/app.py", "tests/test_apps/cliapp/app.py",
        "lib/request.js", "lib/express.js", "README.md",
    ]

    def test_tiers(self):
        self.assertEqual(impact.match_target_paths("app.py", self.PATHS),
                         ["src/flask/app.py", "src/flask/sansio/app.py", "tests/test_apps/cliapp/app.py"])
        self.assertEqual(impact.match_target_paths("src/flask/app.py", self.PATHS), ["src/flask/app.py"])
        self.assertEqual(impact.match_target_paths("lib/request", self.PATHS), ["lib/request.js"])
        self.assertEqual(impact.match_target_paths("flask/app.py", self.PATHS), ["src/flask/app.py"])
        self.assertEqual(impact.match_target_paths("src/flask", self.PATHS), ["src/flask/__init__.py"])
        self.assertEqual(impact.match_target_paths("readme.md", self.PATHS), ["README.md"])
        self.assertEqual(impact.match_target_paths("nothing.py", self.PATHS), [])
        self.assertEqual(impact.match_target_paths("app.py", self.PATHS, suffix=False), [])

    def test_patterns_escape_like_wildcards(self):
        self.assertEqual(impact.target_patterns("httpx/_Client", suffix=False)[:2], ["httpx/\\_client", "httpx/\\_client.%"])
        self.assertTrue(all(item.startswith("%/") for item in impact.target_patterns("app.py", suffix=True)))


# ---------------------------------------------------------------------------
# The endpoint against a real (temporary) database
# ---------------------------------------------------------------------------


class ImpactEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'impact.db'}", "local-test-token")
        await apply_migrations(self.store)
        self.user = SimpleNamespace(id=USER)
        impact._REBUILDS.clear()

    async def asyncTearDown(self):
        for task in list(impact._REBUILDS.values()):
            await asyncio.gather(task, return_exceptions=True)
        await self.store.close()
        self._tmp.cleanup()

    async def add_repo(self, files: dict[str, str], name="flask", repo_id="repo-1"):
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ready', ?, 'now', 'now')",
            [repo_id, USER, name, f"https://github.com/o/{name}.git", len(files)],
        )
        chunks, manifest = [], []
        for index, (path, text) in enumerate(files.items()):
            manifest.append(Statement(
                "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, 'h', ?, 'now')",
                [repo_id, path, len(text.encode())],
            ))
            if text.strip():
                body = text[:-1] if text.endswith("\n") else text
                chunks.append({
                    "id": f"{repo_id}-c{index}", "repo_id": repo_id, "file_path": path, "start_line": 1,
                    "end_line": body.count("\n") + 1, "language": "text", "symbols": [], "content": body, "embedding": None,
                })
        await self.store.batch(manifest)
        await self.store.insert_chunks(chunks)

    def patched(self):
        return (
            patch.object(repos, "get_turso_store", return_value=self.store),
            patch.object(repos, "assert_turso_schema", new=AsyncMock()),
        )

    async def call(self, path, name="flask", **options):
        p = self.patched()
        with p[0], p[1]:
            return await repos.repository_impact(name, path, options.pop("limit", 20), self.user, **options)

    async def test_an_old_graph_is_rebuilt_from_the_index_and_tests_appear(self):
        await self.add_repo(FLASK_LIKE)
        # The graph an older resolver wrote: relative imports only, no version row.
        await self.store.execute(
            "INSERT INTO repo_dependencies (repo_id, source_file, target_file, import_name, line_number) "
            "VALUES ('repo-1', 'src/flask/__init__.py', 'src/flask/app.py', '.app', 1)", [],
        )
        response = await self.call("src/flask/__init__.py")
        self.assertTrue(response["graph"]["rebuilt"])
        self.assertIn("tests/test_basic.py", response["dependent_files"])
        self.assertIn("tests/conftest.py", response["dependent_files"])
        basic = next(node for node in response["dependents"] if node["file_path"] == "tests/test_basic.py")
        self.assertEqual((basic["depth"], basic["line_number"], basic["is_test"]), (1, 2, True))
        self.assertEqual([item["file_path"] for item in response["affected_tests"]["files"]], ["tests/test_basic.py"])
        # The other direction: the test imports the package.
        imports = await self.call("tests/test_basic.py")
        self.assertIn("src/flask/__init__.py", imports["imported_files"])
        self.assertFalse(imports["graph"]["rebuilt"])
        self.assertEqual(await impact.stored_graph_version(self.store, "repo-1"), GRAPH_VERSION)

    async def test_ambiguous_paths_list_candidates_and_analyse_nothing(self):
        await self.add_repo(FLASK_LIKE)
        response = await self.call("app.py")
        self.assertTrue(response["ambiguous"])
        self.assertEqual([item["file_path"] for item in response["candidates"]],
                         ["src/flask/app.py", "src/flask/sansio/app.py", "tests/test_apps/cliapp/app.py"])
        self.assertEqual(response["dependents"], [])
        self.assertEqual(response["target_files"], [])
        self.assertIn("Choose one", response["summary"])
        chosen = await self.call("src/flask/app.py")
        self.assertFalse(chosen["ambiguous"])
        self.assertEqual(chosen["target_files"], ["src/flask/app.py"])
        testing = next(node for node in chosen["dependents"] if node["file_path"] == "src/flask/testing.py")
        self.assertEqual(testing["line_number"], 4)
        self.assertNotIn("src/flask/app.py", chosen["dependent_files"])

    async def test_extensionless_and_module_paths_resolve(self):
        await self.add_repo({
            "index.js": "module.exports = require('./lib/express');\n",
            "lib/express.js": "var req = require('./request');\n",
            "lib/request.js": "module.exports = {};\n",
        }, name="express")
        self.assertEqual((await self.call("lib/request", name="express"))["target_file"], "lib/request.js")
        with self.assertRaises(HTTPException) as missing:
            await self.call("lib/nothing", name="express")
        self.assertEqual(missing.exception.status_code, 404)

    async def test_an_exact_path_resolves_however_many_paths_share_its_name(self):
        files = {f"packages/a{index:02d}/src/index.ts": "export {};\n" for index in range(6)}
        files.update({
            "packages/zeta/src/index.ts": "export {};\n",
            "httpx/_client.py": "class Client: pass\n",
            "httpx/xclient.py": "x = 1\n",
            **{f"pkg{index}/lib/request.test.js": "x\n" for index in range(4)},
        })
        await self.add_repo(files, name="mono")
        with patch.object(impact, "PATH_SCAN_LIMIT", 3):
            exact = await self.call("packages/zeta/src/index.ts", name="mono")
            self.assertEqual(exact["target_file"], "packages/zeta/src/index.ts")
            self.assertEqual((await self.call("zeta/src/index.ts", name="mono"))["target_file"], "packages/zeta/src/index.ts")
            self.assertEqual((await self.call("httpx._client", name="mono"))["target_file"], "httpx/_client.py")
            # Seven files end in /index.ts: more than the scan reads, so the
            # list is marked incomplete and nothing is analysed.
            many = await self.call("index.ts", name="mono")
            self.assertTrue(many["ambiguous"])
            self.assertFalse(many["candidates_complete"])
            self.assertEqual(len(many["candidates"]), 3)
            self.assertTrue(many["summary"].startswith("At least 3 indexed files match index.ts."))
            # Paths that only look alike (lib/request.test.js) fill the scan:
            # the answer asks for more of the path instead of "no file".
            with self.assertRaises(HTTPException) as vague:
                await self.call("lib/request", name="mono")
            self.assertEqual(vague.exception.status_code, 422)
            self.assertIn("Type more of the path", vague.exception.detail)
        complete = await self.call("index.ts", name="mono")
        self.assertTrue(complete["candidates_complete"])
        self.assertEqual(complete["candidate_count"], 7)

    async def test_indirect_dependents_by_depth_and_affected_tests(self):
        await self.add_repo({
            "package.json": '{"name": "express", "main": "index.js"}',
            "index.js": "module.exports = require('./lib/express');\n",
            "lib/express.js": "var req = require('./request');\n",
            "lib/request.js": "req.acceptsCharsets = function () {};\n",
            "test/req.acceptsCharsets.js": (
                "var express = require('../')\n"
                "describe('req', function () {\n"
                "  describe('.acceptsCharsets()', function () {\n"
                "    it('should return true', function () {\n"
                "      req.acceptsCharsets('utf-8')\n"
                "    })\n"
                "  })\n"
                "})\n"
            ),
            "test/support/utils.js": "var express = require('../../');\n",
        }, name="express")
        direct = await self.call("lib/request.js", name="express")
        self.assertEqual([node["file_path"] for node in direct["dependents"]], ["lib/express.js"])
        self.assertEqual(direct["affected_tests"]["count"], 0)
        self.assertEqual(direct["affected_tests"]["indirect_count"], 1)
        self.assertIn("more files import it indirectly", direct["summary"])

        deep = await self.call("lib/request.js", name="express", depth=3, symbol="acceptsCharsets")
        self.assertEqual(deep["depth_counts"], {"1": 1, "2": 1, "3": 2})
        by_path = {node["file_path"]: node for node in deep["dependents"]}
        self.assertEqual((by_path["index.js"]["depth"], by_path["index.js"]["via"]), (2, "lib/express.js"))
        test = by_path["test/req.acceptsCharsets.js"]
        self.assertEqual((test["depth"], test["via"], test["line_number"], test["is_test"]), (3, "index.js", 1, True))
        self.assertFalse(by_path["test/support/utils.js"]["is_test"])
        tests = deep["affected_tests"]
        self.assertEqual([item["file_path"] for item in tests["files"]], ["test/req.acceptsCharsets.js"])
        self.assertEqual([(item["name"], item["line"], item["kind"], item["reference_line"]) for item in tests["functions"]],
                         [(".acceptsCharsets() › should return true", 4, "reference", 5)])
        self.assertEqual(deep["symbol"]["mentions"], 1)

    async def test_the_walk_is_bounded_and_says_when_it_stopped_early(self):
        await self.add_repo({
            "index.js": "module.exports = require('./lib/express');\n",
            "lib/express.js": "var req = require('./request');\n",
            "lib/request.js": "module.exports = {};\n",
            "test/req.js": "var express = require('../');\n",
        }, name="express")
        with patch.object(impact, "_NODE_CAP", 2):
            capped = await self.call("lib/request.js", name="express", depth=3)
            shallow = await self.call("lib/request.js", name="express", depth=2)
        self.assertEqual(capped["depth_counts"], {"1": 1, "2": 1, "3": 0})
        self.assertTrue(capped["truncated"])
        self.assertFalse(shallow["truncated"])

    async def test_symbol_on_python_tests(self):
        await self.add_repo(FLASK_LIKE)
        response = await self.call("src/flask/app.py", depth=3, symbol="make_response")
        functions = {(item["file_path"], item["name"], item["line"], item["kind"]) for item in response["affected_tests"]["functions"]}
        self.assertIn(("tests/test_basic.py", "test_make_response", 11, "reference"), functions)
        self.assertIn(("tests/test_basic.py", "test_response_types", 5, "name"), functions)
        self.assertEqual(response["symbol"]["defined_at"], 8)
        with self.assertRaises(HTTPException) as invalid:
            await self.call("src/flask/app.py", symbol="drop table;")
        self.assertEqual(invalid.exception.status_code, 422)

    async def test_suggestions_list_the_most_imported_files(self):
        await self.add_repo(FLASK_LIKE)
        p = self.patched()
        with p[0], p[1]:
            response = await repos.repository_impact_suggestions("flask", self.user)
        self.assertEqual(response["files"][0]["file_path"], "src/flask/__init__.py")
        self.assertTrue(response["graph"]["rebuilt"])

    async def test_a_slow_rebuild_answers_from_the_old_graph_and_finishes_in_the_background(self):
        await self.add_repo(FLASK_LIKE)
        release = asyncio.Event()
        real = impact.rebuild_dependency_graph_from_index

        async def slow(store, repo_id, staging_prefix=None):
            await release.wait()
            return await real(store, repo_id, staging_prefix)

        repo = {"id": "repo-1", "is_indexing": False, "staging_prefix": None}
        with patch.object(impact, "rebuild_dependency_graph_from_index", slow):
            first = await impact.ensure_current_graph(self.store, repo, wait_seconds=0.01)
            self.assertEqual((first["rebuilt"], first["rebuilding"]), (False, True))
            task = impact._REBUILDS["repo-1"]
            # A second request joins the same rebuild instead of starting another.
            second = await impact.ensure_current_graph(self.store, repo, wait_seconds=0.01)
            self.assertIs(impact._REBUILDS["repo-1"], task)
            self.assertTrue(second["rebuilding"])
            release.set()
            await task
        self.assertEqual(await impact.stored_graph_version(self.store, "repo-1"), GRAPH_VERSION)

    async def test_no_rebuild_while_the_repository_is_being_indexed(self):
        await self.add_repo(FLASK_LIKE)
        result = await impact.ensure_current_graph(self.store, {"id": "repo-1", "is_indexing": True})
        self.assertEqual((result["rebuilt"], result["rebuilding"]), (False, False))
        self.assertEqual(await impact.stored_graph_version(self.store, "repo-1"), 1)

    async def test_a_graph_with_only_the_version_row_still_counts_as_no_graph_for_edit_checks(self):
        # Languages the resolver does not read (and empty graphs) keep the edit
        # completeness check's legacy fallback: the version row is not an edge.
        from agent.completeness import find_impacted_tests
        await self.add_repo({
            "src/auth.ts": "export const auth = 1;\n",
            "test/auth.test.ts": "import {auth} from '../src/auth';\n",
        }, name="tiny")
        await persist_dependency_manifest(self.store, "repo-1", [])
        found = await find_impacted_tests(self.store, "repo-1", [{"file_path": "src/auth.ts", "action": "modify"}])
        self.assertEqual([item["file_path"] for item in found], ["test/auth.test.ts"])

    async def test_persisted_graphs_end_with_the_version_row(self):
        await self.add_repo({"a.py": "import b\n", "b.py": "x = 1\n"}, name="tiny")
        await persist_dependency_manifest(self.store, "repo-1", [
            {"source_file": "a.py", "target_file": "b.py", "import_name": "b", "line_number": 1},
        ])
        self.assertEqual(await impact.stored_graph_version(self.store, "repo-1"), GRAPH_VERSION)
        rows = await self.store.fetch_all("SELECT source_file FROM repo_dependencies WHERE repo_id = 'repo-1' ORDER BY rowid", [])
        self.assertEqual([row["source_file"] for row in rows], ["a.py", ""])


if __name__ == "__main__":
    unittest.main()
