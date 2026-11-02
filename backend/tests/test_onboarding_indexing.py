"""The first minutes with a new repository.

* FEAT-14 "Start here": the overview names the entry file, the core folder,
  install / test / run commands and a 3-5 step reading path, derived from the
  indexed files only (no model call), with a file and line for every fact.
* BUG-13: the folder map is rebuilt as soon as the index it describes
  changes, and its total counts the searchable files ("Indexed files").
* BUG-14: while the semantic index builds, the percentage, the detail line,
  the file count and the step count agree with each other and with the API.
* FEAT-08: a job waiting for a worker reports how many are ahead of it and
  an estimate from recent run times, labelled as an estimate.
* UX-07: a repository GitHub doesn't have is refused before anything is
  stored; when GitHub can't be reached, indexing proceeds as before.

Every test is offline and uses a temporary embedded libSQL database.
"""

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import api.explore_router as explore
import api.repository_state as repository_state
import api.start_here as start_here
from api import ingest_router
from api.repository_state import (
    attach_queue_positions,
    estimate_queue_wait,
    list_repositories,
    present_repository,
    queue_label,
)
from config import settings
from database import TursoStore, apply_migrations
from ingest import pipeline
from ingest.cloner import RepositorySnapshot
from retrieval import retriever

USER = "onboard-user"
URL = "https://github.com/expressjs/express.git"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def ago(seconds: float, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(seconds=seconds)).isoformat()


# An Express-shaped package: a thin index.js, the framework in lib/, a README
# with setup sections and an example the README runs.
PACKAGE_JSON = """{
  "name": "express",
  "description": "Fast, unopinionated, minimalist web framework",
  "version": "5.3.0",
  "license": "MIT",
  "dependencies": {
    "router": "^2.2.0"
  },
  "devDependencies": {
    "mocha": "^11.7.5",
    "jest": {"test": "not a script"}
  },
  "files": [
    "index.js",
    "lib/"
  ],
  "scripts": {
    "lint": "eslint .",
    "test": "mocha --require test/support/env --reporter spec --check-leaks test/ test/acceptance/",
    "test-ci": "nyc npm test"
  }
}
"""
INDEX_JS = """/*!
 * express
 * MIT Licensed
 */

'use strict';

module.exports = require('./lib/express');
"""
EXPRESS_JS = """'use strict';

var bodyParser = require('body-parser')
var proto = require('./application');
var Router = require('router');
var req = require('./request');
var res = require('./response');

exports = module.exports = createApplication;

/**
 * Create an express application.
 */

function createApplication() {
  var app = function(req, res, next) {
    app.handle(req, res, next);
  };
  return app;
}
"""
APPLICATION_JS = """'use strict';

var utils = require('./utils');

var app = exports = module.exports = {};

app.init = function init() {
  this.cache = Object.create(null);
};

app.handle = function handle(req, res, callback) {
  return callback();
};
"""
REQUEST_JS = """'use strict';

var req = Object.create(null);

module.exports = req;

req.get =
req.header = function header(name) {
  return name;
};
"""
RESPONSE_JS = """'use strict';

var res = Object.create(null);

module.exports = res;

res.status = function status(code) {
  this.statusCode = code;
  return this;
};
"""
UTILS_JS = "'use strict';\n\nexports.etag = function etag() {};\n"
README = """<a href="https://expressjs.com/"><img alt="Express Logo" src="logo.svg"></a>

**Fast, unopinionated, minimalist web framework for Node.js.**

```js
const app = express()
node not-a-command.js
```

## Installation

```bash
npm install express
```

## Examples

```bash
node examples/hello
```

## Contributing

### Running Tests

```bash
npm test
```

#### How do I mock it in tests?

Not a setup section.
"""
EXPRESS_FILES = {
    "package.json": PACKAGE_JSON,
    "index.js": INDEX_JS,
    "Readme.md": README,
    "lib/express.js": EXPRESS_JS,
    "lib/application.js": APPLICATION_JS,
    "lib/request.js": REQUEST_JS,
    "lib/response.js": RESPONSE_JS,
    "lib/utils.js": UTILS_JS,
    "examples/hello/index.js": "var express = require('../../');\nexpress().listen(3000);\n",
    "test/app.js": "var express = require('../');\ndescribe('app', function () {});\n",
}

PYPROJECT = """[project]
name = "clickish"
version = "1.0.0"
description = "Composable command line interface toolkit"
requires-python = ">=3.10"

[project.scripts]
clickish = "clickish.cli:main"

[dependency-groups]
tests = ["pytest"]

[build-system]
requires = ["flit_core>=3.11,<4"]
build-backend = "flit_core.buildapi"

[tool.flit.module]
name = "clickish"

[tool.uv]
default-groups = ["tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
PY_INIT = """from .core import Command as Command
from .core import Context as Context
from .decorators import command as command
from .exceptions import UsageError as UsageError
from . import utils
from .__about__ import __version__ as __version__
"""
PY_CORE = """import enum


class ParameterSource(enum.Enum):
    DEFAULT = 1


class Context:
    def __init__(self, command):
        self.command = command


class Command:
    def invoke(self, ctx):
        return ctx
"""
PY_FILES = {
    "pyproject.toml": PYPROJECT,
    "README.md": "# Clickish\n\nClickish is a package for creating command line interfaces quickly.\n",
    "src/clickish/__init__.py": PY_INIT,
    "src/clickish/core.py": PY_CORE,
    "src/clickish/decorators.py": "from .core import Command\n\n\ndef command(name=None):\n    return Command\n",
    "src/clickish/exceptions.py": "class UsageError(Exception):\n    pass\n",
    "src/clickish/utils.py": "def echo(message):\n    print(message)\n",
    "src/clickish/__about__.py": '__version__ = "1.0.0"\n',
    "src/clickish/cli.py": "def main():\n    return 0\n",
    "tests/test_core.py": "def test_context():\n    assert True\n",
}

MAKEFILE = """.PHONY: docs
init:
\tpython -m pip install -r requirements-dev.txt
test:
\tpython -m pytest tests

coverage:
\tpython -m pytest --cov
"""


def chunk_rows(repo_id: str, path: str, text: str, lines_per_chunk: int = 200, prefix: str = "") -> list[dict]:
    lines = text.rstrip("\n").split("\n")
    rows = []
    for offset in range(0, len(lines), lines_per_chunk):
        window = lines[offset: offset + lines_per_chunk]
        rows.append({
            "id": f"{prefix}{uuid.uuid4()}", "repo_id": repo_id, "file_path": path, "start_line": offset + 1,
            "end_line": offset + len(window), "language": path.rsplit(".", 1)[-1], "symbols": [],
            "content": "\n".join(window), "embedding": None,
        })
    return rows


def line_of(text: str, fragment: str) -> int:
    return next(index + 1 for index, line in enumerate(text.split("\n")) if fragment in line)


class StoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TursoStore(f"file:{Path(self._tmp.name) / 'onboarding.db'}", "local-test-token")
        await apply_migrations(self.store)
        self.user = SimpleNamespace(id=USER)
        start_here._CACHE.clear()
        repository_state._durations_cache = None

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    async def add_repo(self, name="express", files=None, *, manifest=True, manifest_only=None, status="ready",
                       url=URL, user=USER, lines_per_chunk=200):
        repo_id = f"repo-{uuid.uuid4()}"
        files = EXPRESS_FILES if files is None else files
        rows = [row for path, text in files.items() for row in chunk_rows(repo_id, path, text, lines_per_chunk)]
        now = datetime.now(UTC).isoformat()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [repo_id, user, name, url, status, len(rows), now, now],
        )
        if rows:
            await self.store.insert_chunks(rows)
        if manifest:
            for path, text in {**files, **(manifest_only or {})}.items():
                await self.add_manifest_file(repo_id, path, text)
        return repo_id

    async def add_manifest_file(self, repo_id, path, text=""):
        await self.store.execute(
            "INSERT INTO repo_files (repo_id, file_path, content_hash, byte_size, updated_at) VALUES (?, ?, ?, ?, ?)",
            [repo_id, path, "h", len(text.encode()), datetime.now(UTC).isoformat()],
        )

    async def overview(self, name="express"):
        async def noop():
            return None

        with patch.object(explore, "get_turso_store", return_value=self.store), \
                patch.object(explore, "assert_turso_schema", new=noop):
            return await explore.repository_overview(name, self.user)


# ---------------------------------------------------------------------------
# FEAT-14: Start here
# ---------------------------------------------------------------------------


class StartHereTests(StoreCase):
    def assert_cited(self, source, files):
        """A citation names an indexed file and a line that exists in it."""
        self.assertIn(source["path"], files)
        self.assertGreaterEqual(source["line"], 1)
        self.assertLessEqual(source["line"], len(files[source["path"]].rstrip("\n").split("\n")))

    async def test_node_package_entry_core_commands_and_reading_path(self):
        await self.add_repo()
        overview = await self.overview()
        card = overview["start_here"]
        self.assertIsNotNone(card)
        self.assertEqual(card["ecosystem"], "node")
        self.assertEqual(card["summary"]["text"], "Fast, unopinionated, minimalist web framework")
        self.assertEqual(card["summary"]["source"], {"path": "package.json", "line": 3})

        # index.js is Node's entry (no "main") and hands over to lib/express.js.
        entry = card["entry"]
        require_line = line_of(INDEX_JS, "require('./lib/express')")
        self.assertEqual((entry["path"], entry["main"], entry["line"]), ("index.js", "lib/express.js", require_line))
        self.assertEqual(entry["source"], {"path": "index.js", "line": require_line})
        self.assertEqual(card["core"]["path"], "lib")
        self.assertEqual(card["core"]["files"], sum(1 for path in EXPRESS_FILES if path.startswith("lib/")))

        commands = {item["kind"]: item for item in card["commands"]}
        self.assertEqual(commands["install"]["command"], "npm install")
        self.assertEqual(commands["install"]["source"], {"path": "package.json", "line": line_of(PACKAGE_JSON, '"devDependencies"')})
        # The "test" script, not the "test" key nested under devDependencies.
        test_line = line_of(PACKAGE_JSON, '"test": "mocha')
        self.assertEqual(commands["test"]["command"], "npm test")
        self.assertEqual(commands["test"]["source"], {"path": "package.json", "line": test_line})
        self.assertIn("mocha --require test/support/env", commands["test"]["detail"])
        # No start script: the README command that runs a file of this repository.
        self.assertEqual(commands["run"]["command"], "node examples/hello")
        self.assertEqual(commands["run"]["source"], {"path": "Readme.md", "line": line_of(README, "node examples/hello")})

        steps = card["steps"]
        self.assertTrue(3 <= len(steps) <= 5)
        self.assertEqual([step["path"] for step in steps],
                         ["index.js", "lib/express.js", "lib/application.js", "lib/request.js", "lib/response.js"])
        self.assertEqual(steps[1]["line"], line_of(EXPRESS_JS, "function createApplication()"))
        self.assertEqual(steps[1]["detail"], "Defines createApplication, which index.js exports.")
        self.assertEqual(steps[2]["line"], line_of(APPLICATION_JS, "app.init = function init()"))
        self.assertEqual(steps[3]["line"], line_of(REQUEST_JS, "req.header = function"))
        self.assertEqual(steps[4]["line"], line_of(RESPONSE_JS, "res.status = function"))
        self.assertIn("Imported by lib/express.js on line", steps[2]["detail"])

        # Every fact can be opened at its line.
        for source in [card["summary"]["source"], entry["source"], card["core"]["source"],
                       *(item["source"] for item in card["commands"]),
                       *({"path": step["path"], "line": step["line"]} for step in steps),
                       *({"path": guide["path"], "line": guide["line"]} for guide in card["guides"])]:
            self.assert_cited(source, EXPRESS_FILES)
        # Setup sections of the README (deep FAQ headings are not guides).
        self.assertEqual([guide["title"] for guide in card["guides"]], ["Installation", "Examples", "Contributing", "Running Tests"])

    async def test_python_package_with_uv_pytest_and_a_console_script(self):
        await self.add_repo(name="clickish", files=PY_FILES, url="https://github.com/acme/clickish.git")
        card = (await self.overview("clickish"))["start_here"]
        self.assertEqual(card["ecosystem"], "python")
        self.assertEqual(card["entry"]["path"], "src/clickish/__init__.py")
        # The package flit builds ([tool.flit.module] name = "clickish").
        self.assertEqual(card["entry"]["source"], {"path": "pyproject.toml", "line": line_of(PYPROJECT, "[tool.flit.module]") + 1})
        self.assertEqual(card["core"]["path"], "src/clickish")
        commands = {item["kind"]: (item["command"], item["source"]["line"]) for item in card["commands"]}
        self.assertEqual(commands["install"], ("uv sync", line_of(PYPROJECT, "[tool.uv]")))
        self.assertEqual(commands["test"], ("uv run pytest", line_of(PYPROJECT, "[tool.pytest.ini_options]")))
        self.assertEqual(commands["run"], ("uv run clickish --help", line_of(PYPROJECT, 'clickish = "clickish.cli:main"')))
        steps = card["steps"]
        self.assertEqual(steps[0]["path"], "src/clickish/__init__.py")
        # Import order, supporting modules (exceptions, utils) after the others,
        # and the version file (no definitions) is not a reading step.
        self.assertEqual([step["path"] for step in steps[1:]], [
            "src/clickish/core.py", "src/clickish/decorators.py", "src/clickish/exceptions.py", "src/clickish/utils.py",
        ])
        # The earliest definition among the names the entry imports.
        self.assertEqual((steps[1]["line"], steps[1]["detail"].rsplit("see ", 1)[-1]), (line_of(PY_CORE, "class Context"), "Context."))

    async def test_makefile_targets_are_the_commands_when_present(self):
        files = {
            "pyproject.toml": '[project]\nname = "requestsish"\ndescription = "HTTP for Humans."\n\n[build-system]\nrequires = ["setuptools"]\n',
            "Makefile": MAKEFILE,
            "src/requestsish/__init__.py": "from .api import get\n",
            "src/requestsish/api.py": "def get(url):\n    return url\n",
        }
        await self.add_repo(name="requestsish", files=files, url="https://github.com/acme/requestsish.git")
        card = (await self.overview("requestsish"))["start_here"]
        commands = {item["kind"]: item for item in card["commands"]}
        self.assertEqual((commands["install"]["command"], commands["install"]["source"]), ("make init", {"path": "Makefile", "line": 2}))
        self.assertEqual(commands["install"]["detail"], "python -m pip install -r requirements-dev.txt")
        self.assertEqual((commands["test"]["command"], commands["test"]["source"]["line"], commands["test"]["detail"]),
                         ("make test", 4, "python -m pytest tests"))
        self.assertNotIn("run", commands)
        # One import: the thin entry hands over to api.py; README fallback fills the path.
        self.assertEqual([step["path"] for step in card["steps"]][:2], ["src/requestsish/__init__.py", "src/requestsish/api.py"])

    async def test_built_output_in_package_json_maps_back_to_typescript_source(self):
        files = {
            "package.json": '{\n  "name": "ky",\n  "main": "./distribution/index.js",\n  "scripts": {\n    "test": "xo && ava"\n  }\n}\n',
            "source/index.ts": "import {Ky} from './core/Ky.js';\nimport type {Options} from './types/options.js';\nexport default Ky;\n",
            "source/core/Ky.ts": "export class Ky {\n  static create() {}\n}\n",
            "source/types/options.ts": "export type Options = {retry?: number};\n",
            "readme.md": "# ky\n\n> Tiny and elegant HTTP client based on the Fetch API\n",
        }
        await self.add_repo(name="ky", files=files, url="https://github.com/sindresorhus/ky.git")
        card = (await self.overview("ky"))["start_here"]
        self.assertEqual(card["entry"]["path"], "source/index.ts")
        self.assertEqual(card["entry"]["source"], {"path": "package.json", "line": 3})
        self.assertIn('the "main" field of package.json', card["entry"]["detail"])
        self.assertEqual(card["core"]["path"], "source")
        paths = [step["path"] for step in card["steps"]]
        self.assertEqual(paths[:2], ["source/index.ts", "source/core/Ky.ts"])
        self.assertNotIn("source/types/options.ts", paths)  # types only: nothing to read in order
        self.assertGreaterEqual(len(paths), 3)  # topped up with the README

    async def test_start_here_is_derived_without_a_model_and_failures_do_not_break_the_overview(self):
        await self.add_repo()
        no_call = AssertionError("the Start here card must not call a model or the network")
        with patch("ingest.embedder.embed_query", side_effect=no_call), \
                patch("agent.nemotron.get_async_client", side_effect=no_call), \
                patch.object(httpx.AsyncClient, "send", side_effect=no_call):
            first = await self.overview()
        self.assertIsNotNone(first["start_here"])
        start_here._CACHE.clear()
        with patch.object(explore, "gather_start_here", new=AsyncMock(side_effect=RuntimeError("boom"))):
            overview = await self.overview()
        self.assertIsNone(overview["start_here"])
        self.assertTrue(overview["tree"])

    async def test_an_entry_that_imports_nothing_leads_to_the_central_modules(self):
        # starlette/__init__.py only holds the version string.
        files = {
            "pyproject.toml": '[project]\nname = "starlettish"\ndescription = "The little ASGI library."\n',
            "starlettish/__init__.py": '__version__ = "1.0.0"\n',
            "starlettish/applications.py": "class Starlettish:\n    def __init__(self, routes=None):\n        self.routes = routes\n",
            "starlettish/routing.py": "class Route:\n    pass\n" + "\n".join(f"# padding {index}" for index in range(40)) + "\n",
            "starlettish/exceptions.py": "class HTTPException(Exception):\n    pass\n",
            "starlettish/_utils.py": "def is_async(obj):\n    return False\n",
            "tests/test_applications.py": "def test_app():\n    assert True\n",
        }
        await self.add_repo(name="starlettish", files=files, url="https://github.com/acme/starlettish.git")
        steps = (await self.overview("starlettish"))["start_here"]["steps"]
        self.assertEqual([step["path"] for step in steps], [
            "starlettish/__init__.py", "starlettish/applications.py", "starlettish/routing.py",
        ])
        self.assertEqual(steps[1]["detail"], "One of the main modules in starlettish/; see Starlettish.")

    def test_central_modules_rank_conventional_names_then_size(self):
        rows = [("pkg/routing.py", 900), ("pkg/applications.py", 300), ("pkg/big.py", 5000), ("pkg/__init__.py", 50),
                ("pkg/exceptions.py", 9000), ("pkg/test_x.py", 100), ("pkg/data.json", 10)]
        self.assertEqual(start_here.central_modules(rows, set()), ["pkg/applications.py", "pkg/routing.py", "pkg/big.py"])
        self.assertEqual(start_here.central_modules(rows, {"pkg/applications.py"}, limit=1), ["pkg/routing.py"])

    async def test_repository_without_manifests_or_entry_has_no_card(self):
        await self.add_repo(name="notes", files={"notes/a.md": "# A\n", "notes/b.md": "# B\n"},
                            url="https://github.com/acme/notes.git")
        self.assertIsNone((await self.overview("notes"))["start_here"])


class StartHereParsingTests(unittest.TestCase):
    def test_package_json_lines_follow_nesting(self):
        facts = start_here.package_json_facts(PACKAGE_JSON)
        self.assertEqual(facts["scripts"]["test"]["line"], line_of(PACKAGE_JSON, '"test": "mocha'))
        self.assertEqual(facts["scripts"]["lint"]["line"], line_of(PACKAGE_JSON, '"lint"'))
        self.assertEqual(facts["description"]["line"], 3)
        self.assertIsNone(facts["main"])
        self.assertEqual(start_here.package_json_facts("{ not json"), {})

    def test_readme_commands_come_only_from_shell_blocks(self):
        readme = start_here.readme_facts(README)
        commands = [item["command"] for item in readme["commands"]]
        self.assertEqual(commands, ["npm install express", "node examples/hello", "npm test"])
        self.assertEqual(readme["commands"][1]["heading"], "Examples")
        self.assertEqual(readme["summary"]["line"], 3)

    def test_relative_imports_resolve_like_node_and_typescript(self):
        self.assertEqual(start_here.js_candidates("lib", "./application")[0], "lib/application.js")
        self.assertIn("source/core/Ky.ts", start_here.js_candidates("source", "./core/Ky.js"))
        self.assertEqual(start_here.js_candidates("test", ".."), ["index.js", "index.ts", "index.mjs", "index.tsx"])
        self.assertEqual(start_here.js_candidates("", "../outside"), [])
        self.assertEqual(start_here.python_candidates("src/pkg/__init__.py", 1, "core"), ["src/pkg/core.py", "src/pkg/core/__init__.py"])
        self.assertEqual(start_here.python_candidates("src/pkg/sub/mod.py", 2, "core"), ["src/pkg/core.py", "src/pkg/core/__init__.py"])
        self.assertEqual(start_here.python_candidates("src/pkg/__init__.py", 0, "pkg.app"), ["src/pkg/app.py", "src/pkg/app/__init__.py"])
        self.assertEqual(start_here.python_candidates("src/pkg/__init__.py", 0, "os.path"), [])

    def test_definitions_and_exports(self):
        self.assertEqual(start_here.js_exported_name(EXPRESS_JS), "createApplication")
        self.assertIsNone(start_here.js_exported_name(INDEX_JS))
        self.assertEqual(start_here.definition_line("lib/express.js", EXPRESS_JS, ["createApplication"]),
                         (line_of(EXPRESS_JS, "function createApplication"), "createApplication"))
        self.assertEqual(start_here.definition_line("lib/application.js", APPLICATION_JS),
                         (line_of(APPLICATION_JS, "app.init"), "app.init"))
        self.assertIsNone(start_here.definition_line("src/pkg/__about__.py", '__version__ = "1"\n'))
        self.assertTrue(start_here.is_supporting("src/pkg/exceptions.py"))
        self.assertTrue(start_here.is_supporting("source/types/options.ts"))
        self.assertFalse(start_here.is_supporting("src/pkg/core.py"))

    def test_makefile_targets_and_npm_placeholder_test(self):
        targets = start_here.makefile_targets(MAKEFILE)
        self.assertEqual(targets["init"], {"line": 2, "recipe": "python -m pip install -r requirements-dev.txt"})
        self.assertNotIn(".PHONY", targets)
        placeholder = '{\n  "name": "x",\n  "scripts": {\n    "test": "echo \\"Error: no test specified\\" && exit 1"\n  }\n}\n'
        commands = start_here.build_commands("node", {"package.json": placeholder},
                                             start_here.parse_manifests({"package.json": placeholder}), ["package.json"])
        self.assertEqual([item["kind"] for item in commands], ["install"])
        yarn = start_here.build_commands("node", {"package.json": PACKAGE_JSON},
                                         start_here.parse_manifests({"package.json": PACKAGE_JSON}), ["package.json", "yarn.lock"])
        self.assertEqual([item["command"] for item in yarn][:2], ["yarn install", "yarn test"])
        self.assertEqual(yarn[0]["source"], {"path": "yarn.lock", "line": 1})


# ---------------------------------------------------------------------------
# BUG-13: the folder map follows the published index
# ---------------------------------------------------------------------------


class OverviewFreshnessTests(StoreCase):
    async def test_overview_right_after_indexing_shows_the_full_map(self):
        # A first index in progress: keyword search works for 2 files, the
        # manifest is written at the end, so the map falls back to chunks.
        repo_id = await self.add_repo(files={"index.js": INDEX_JS, "lib/express.js": EXPRESS_JS}, manifest=False, status="ready")
        await self.store.execute(
            "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, attempts, claimed_at, heartbeat_at, "
            "claim_token, created_at, updated_at) VALUES (?, ?, ?, ?, 'processing', 1, ?, ?, 'claim', ?, ?)",
            [f"job-{repo_id}", repo_id, USER, URL, ago(30), ago(2), ago(60), ago(2)],
        )
        await self.store.execute(
            "INSERT INTO repo_index_state (repo_id, phase, keyword_files, keyword_chunks, semantic_progress, "
            "embedding_status, updated_at) VALUES (?, 'searchable', 2, 2, 40, 'running', ?)",
            [repo_id, ago(2)],
        )
        during = await self.overview()
        self.assertTrue(during["repository"]["is_indexing"])
        self.assertEqual(during["total_files"], 2)

        # The run finishes: every file is published, the manifest written, the job closed.
        rest = {path: text for path, text in EXPRESS_FILES.items() if path not in {"index.js", "lib/express.js"}}
        rows = [row for path, text in rest.items() for row in chunk_rows(repo_id, path, text)]
        await self.store.insert_chunks(rows)
        for path, text in EXPRESS_FILES.items():
            await self.add_manifest_file(repo_id, path, text)
        total_chunks = await self.store.fetch_one("SELECT COUNT(*) AS count FROM chunks WHERE repo_id = ?", [repo_id])
        await self.store.execute("UPDATE repos SET status = 'ready', chunk_count = ? WHERE id = ?", [total_chunks["count"], repo_id])
        await self.store.execute("UPDATE ingestion_jobs SET status = 'completed', claimed_at = NULL, heartbeat_at = NULL WHERE repo_id = ?", [repo_id])
        await self.store.execute(
            "UPDATE repo_index_state SET phase = 'ready', keyword_files = ?, semantic_progress = 100, "
            "embedding_status = 'complete' WHERE repo_id = ?", [len(EXPRESS_FILES), repo_id],
        )
        await self.store.execute(
            "INSERT INTO repo_coverage (repo_id, total_seen_files, eligible_files, indexed_files, excluded_files, "
            "excluded_bytes, excluded_reasons, excluded_paths, updated_at) VALUES (?, ?, ?, ?, 0, 0, '{}', '[]', ?)",
            [repo_id, len(EXPRESS_FILES), len(EXPRESS_FILES), len(EXPRESS_FILES), ago(0)],
        )
        after = await self.overview()  # well within the map's 120 s cache
        self.assertFalse(after["repository"]["is_indexing"])
        self.assertEqual(after["total_files"], len(EXPRESS_FILES))
        self.assertEqual(after["total_files"], after["repository"]["indexed_files"])
        self.assertEqual({node["path"] for node in after["tree"]}, {"lib", "examples", "test"})
        self.assertTrue(after["languages"])
        self.assertIn("package.json", after["key_files"])

    async def test_folder_total_counts_searchable_files_only(self):
        # An empty file is hashed into the manifest but has no sections.
        await self.add_repo(manifest_only={"lib/empty.js": "", "EMPTY.md": ""})
        overview = await self.overview()
        self.assertEqual(overview["total_files"], len(EXPRESS_FILES))
        lib = next(node for node in overview["tree"] if node["path"] == "lib")
        self.assertEqual(lib["files"], sum(1 for path in EXPRESS_FILES if path.startswith("lib/")))
        self.assertNotIn("EMPTY.md", overview["root_files"])
        self.assertEqual(sum(item["files"] for item in overview["languages"]), sum(
            1 for path in EXPRESS_FILES if path.rsplit(".", 1)[-1] in {"js", "json", "md"}
        ))

    def test_without_files_does_not_change_the_cached_map(self):
        cached = {"total_files": 3, "root_files": ["a.md"], "extensions": {"md": 1, "js": 2},
                  "tree": [{"path": "lib", "files": 2, "bytes": 10, "children": [
                      {"path": "lib/x", "files": 1, "bytes": 4, "children": []}]}]}
        trimmed = explore.without_files(cached, [("lib/x/empty.js", 4), ("a.md", 0)])
        self.assertEqual(trimmed["total_files"], 1)
        self.assertEqual(trimmed["tree"], [{"path": "lib", "files": 1, "bytes": 6, "children": []}])
        self.assertEqual(trimmed["root_files"], [])
        self.assertEqual(trimmed["extensions"], {"js": 1})
        self.assertEqual(cached["total_files"], 3)
        self.assertEqual(cached["tree"][0]["files"], 2)

    async def test_a_finished_run_drops_the_cached_folder_map(self):
        repo_id = "repo-pipeline"
        now = datetime.now(UTC).isoformat()
        await self.store.execute(
            "INSERT INTO repos (id, user_id, repo_name, github_url, status, chunk_count, created_at, updated_at) "
            "VALUES (?, ?, 'hello', 'https://github.com/octocat/Hello-World.git', 'queued', 0, ?, ?)",
            [repo_id, USER, now, now],
        )
        retriever._REPO_MAP_CACHE[repo_id] = (10**12, {"total_files": 1, "tree": [], "root_files": [], "extensions": {}, "text": ""})
        source = Path(self._tmp.name) / "source"
        for path, text in {"src/app.py": "def main():\n    return 1\n", "README.md": "# Hello\n"}.items():
            (source / path).parent.mkdir(parents=True, exist_ok=True)
            (source / path).write_text(text, encoding="utf-8")

        def fake_embed(chunks, **kwargs):
            for chunk in chunks:
                chunk["embedding"] = [0.01] * settings.embedding_dimension
            return chunks

        snapshot = RepositorySnapshot(path=str(source), commit_sha="abc1234", source="archive")
        with patch.object(pipeline, "acquire_repository_source", return_value=snapshot), \
                patch.object(pipeline, "cleanup_repo"), \
                patch.object(pipeline, "embed_chunks", side_effect=fake_embed), \
                patch.object(pipeline, "spawn_background", side_effect=lambda coroutine: coroutine.close()):
            ok = await pipeline.run_ingestion_for_repo(self.store, "https://github.com/octocat/Hello-World.git", USER, repo_id)
        self.assertTrue(ok)
        self.assertNotIn(repo_id, retriever._REPO_MAP_CACHE)


# ---------------------------------------------------------------------------
# BUG-14: one set of numbers while indexing
# ---------------------------------------------------------------------------


def status_row(**overrides):
    base = {
        "id": "repo-1", "repo_name": "click", "github_url": "https://github.com/pallets/click.git",
        "repo_status": "ready", "chunk_count": 258, "error_message": None, "created_at": ago(300, NOW),
        "updated_at": ago(10, NOW), "index_phase": "searchable", "semantic_progress": 66,
        "embedding_status": "running", "keyword_files": 56, "keyword_chunks": 390, "searchable_at": ago(60, NOW),
        "semantic_ready_at": None, "job_status": "processing", "job_attempts": 1, "total_seen_files": 180,
        "eligible_files": 166, "indexed_files": 0, "excluded_files": 14, "excluded_bytes": 0, "excluded_reasons": {},
        "commit_sha": None, "selection_json": {}, "indexed_at": None, "run_prefix": "initial:job-1-a-",
        "run_started_at": ago(100, NOW), "index_updated_at": ago(3, NOW), "job_created_at": ago(120, NOW),
        "job_claimed_at": ago(110, NOW), "job_heartbeat_at": ago(4, NOW),
    }
    base.update(overrides)
    return base


class ProgressConsistencyTests(unittest.TestCase):
    def test_semantic_percentage_matches_the_embedded_and_total_counts(self):
        view = present_repository(status_row(
            error_message="Building the semantic index: 258 of 465 code sections (55%). Keyword search already works.",
        ), now=NOW)
        self.assertEqual(view["semantic_sections"], {"embedded": 258, "total": 465})
        self.assertEqual(view["semantic_progress"], round(258 * 100 / 465))
        self.assertEqual(view["phase_label"], "Ready · semantic 55%")
        self.assertEqual(view["progress_percent"], round(55 + 55 * 0.44))
        self.assertEqual((view["phase_step"], view["phase_steps"]), (6, 7))

    def test_rounding_is_half_up_like_the_ui(self):
        view = present_repository(status_row(
            error_message="Building the semantic index: 1 of 200 code sections (0%). Keyword search already works.",
        ), now=NOW)
        self.assertEqual(view["semantic_progress"], 1)  # Math.round(0.5) in the browser

    def test_without_counts_the_stored_progress_is_kept(self):
        for overrides in (
            {"error_message": "Ready to explore with keyword search. The semantic index is still being built."},
            # A refresh reports new rows against the whole index: not a whole-index ratio.
            {"error_message": "Building the semantic index: 10 of 400 code sections (2%).", "commit_sha": "abc",
             "index_phase": "embedding", "run_prefix": "job-1-b-", "chunk_count": 400},
            {"job_status": "completed", "index_phase": "ready", "semantic_progress": 100,
             "error_message": "Building the semantic index: 465 of 465 code sections (100%)."},
        ):
            with self.subTest(overrides=overrides):
                view = present_repository(status_row(**overrides), now=NOW)
                self.assertIsNone(view["semantic_sections"])
                self.assertEqual(view["semantic_progress"], overrides.get("semantic_progress", 66))

    def test_searchable_files_while_indexing_and_after(self):
        indexing = present_repository(status_row(), now=NOW)
        self.assertEqual(indexing["searchable_files"], 56)  # coverage's indexed_files is still 0
        ready = present_repository(status_row(job_status="completed", index_phase="ready", indexed_files=161,
                                              keyword_files=161, semantic_progress=100), now=NOW)
        self.assertEqual(ready["searchable_files"], 161)
        refresh = present_repository(status_row(commit_sha="abc", run_prefix="job-1-b-", index_phase="cloning",
                                                indexed_files=0, keyword_files=161), now=NOW)
        self.assertTrue(refresh["refreshing"])
        self.assertEqual(refresh["searchable_files"], 161)
        queued = present_repository(status_row(job_status="queued", index_phase="queued", chunk_count=0, keyword_files=0,
                                               run_prefix=None), now=NOW)
        self.assertEqual(queued["searchable_files"], 0)
        self.assertEqual((queued["phase_step"], queued["phase_steps"]), (1, 7))
        self.assertIsNone(queued["queue"])


# ---------------------------------------------------------------------------
# FEAT-08: place in the queue
# ---------------------------------------------------------------------------


class QueueTests(StoreCase):
    async def add_job(self, repo_id, user, status, created_seconds_ago, claimed_seconds_ago=None, priority=10):
        job_id = f"job-{repo_id}"
        await self.store.execute(
            "INSERT INTO ingestion_jobs (id, repo_id, user_id, github_url, status, attempts, claimed_at, heartbeat_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            [job_id, repo_id, user, URL, status,
             ago(claimed_seconds_ago) if claimed_seconds_ago is not None else None,
             ago(2) if status == "processing" else None, ago(created_seconds_ago), ago(created_seconds_ago)],
        )
        await self.store.execute(
            "INSERT INTO ingestion_job_meta (job_id, priority, phase, progress, updated_at) VALUES (?, ?, 'queued', 0, ?)",
            [job_id, priority, ago(created_seconds_ago)],
        )
        return job_id

    async def add_history(self, seconds: list[float]):
        for index, value in enumerate(seconds):
            repo_id = await self.add_repo(name=f"done-{index}", files={}, manifest=False, user=f"other-{index}")
            job_id = await self.add_job(repo_id, f"other-{index}", "completed", 3600)
            await self.store.execute(
                "INSERT INTO ingestion_metrics (job_id, repo_id, metrics_json, updated_at) VALUES (?, ?, ?, ?)",
                [job_id, repo_id, json.dumps({"total_ms": value * 1000}), ago(600 - index)],
            )

    async def test_waiting_job_reports_jobs_ahead_and_an_estimate(self):
        await self.add_history([30, 20, 40])  # median 30 s
        running = await self.add_repo(name="running", files={}, manifest=False, user="u-running", status="cloning")
        await self.add_job(running, "u-running", "processing", 120, claimed_seconds_ago=10)
        earlier = await self.add_repo(name="earlier", files={}, manifest=False, user="u-earlier", status="queued")
        await self.add_job(earlier, "u-earlier", "queued", 60)
        mine = await self.add_repo(name="mine", files={}, manifest=False, status="queued")
        await self.add_job(mine, USER, "queued", 30)

        [view] = await list_repositories(self.store, USER)
        queue = view["queue"]
        self.assertEqual((queue["ahead"], queue["running"], queue["position"]), (2, 1, 3))
        # 20 s left of the running job (30 s typical, 10 s in) + one typical run.
        self.assertAlmostEqual(queue["estimated_wait_seconds"], 50, delta=2)
        self.assertEqual(queue["label"], "2 repositories ahead of yours · usually starts in about 1 min")
        self.assertEqual(queue["estimate_note"], "Estimate based on how long the last 3 indexing runs took.")

        # The running job finishes and the earlier one starts: one ahead now.
        await self.store.execute("UPDATE ingestion_jobs SET status = 'completed' WHERE repo_id = ?", [running])
        await self.store.execute("UPDATE ingestion_jobs SET status = 'processing', claimed_at = ? WHERE repo_id = ?", [ago(1), earlier])
        [view] = await list_repositories(self.store, USER)
        self.assertEqual(view["queue"]["ahead"], 1)
        self.assertTrue(view["queue"]["label"].startswith("1 repository ahead of yours · usually starts in about"))

    async def test_without_history_there_is_no_estimate_and_other_states_have_no_queue(self):
        running = await self.add_repo(name="running", files={}, manifest=False, user="u-running", status="cloning")
        await self.add_job(running, "u-running", "processing", 120, claimed_seconds_ago=10)
        mine = await self.add_repo(name="mine", files={}, manifest=False, status="queued")
        await self.add_job(mine, USER, "queued", 30)
        ready = await self.add_repo(name="express", status="ready")
        await self.add_job(ready, USER, "completed", 900)
        views = {view["repo_name"]: view for view in await list_repositories(self.store, USER)}
        self.assertEqual(views["mine"]["queue"]["label"], "1 repository ahead of yours")
        self.assertIsNone(views["mine"]["queue"]["estimated_wait_seconds"])
        self.assertIsNone(views["mine"]["queue"]["estimate_note"])
        self.assertIsNone(views["express"]["queue"])

    async def test_next_in_line_and_queue_failures_never_break_the_status(self):
        mine = await self.add_repo(name="mine", files={}, manifest=False, status="queued")
        await self.add_job(mine, USER, "queued", 30)
        [view] = await list_repositories(self.store, USER)
        self.assertEqual(view["queue"]["ahead"], 0)
        self.assertEqual(view["queue"]["label"], "Next in line for an indexing worker")
        with patch.object(repository_state, "attach_queue_positions", new=AsyncMock(side_effect=RuntimeError("boom"))):
            [view] = await list_repositories(self.store, USER)
        self.assertIsNone(view["queue"])
        self.assertEqual(view["status"], "queued")

    def test_estimate_and_label_rules(self):
        self.assertIsNone(estimate_queue_wait([10], 1, []))
        self.assertEqual(estimate_queue_wait([], 2, [60, 30, 90]), 120)
        # A running job past the typical time still counts a little, never zero.
        self.assertEqual(estimate_queue_wait([500], 0, [60]), 6)
        # Two busy workers share the queue.
        self.assertEqual(estimate_queue_wait([0, 0], 2, [60]), 120)
        self.assertEqual(queue_label(3, 61), "3 repositories ahead of yours · usually starts in about 2 min")
        self.assertEqual(queue_label(1, 10), "1 repository ahead of yours · usually starts in about 1 min")
        self.assertEqual(queue_label(9, 3 * 3600), "9 repositories ahead of yours · usually starts in about 3 hours")
        self.assertEqual(queue_label(0, None), "Next in line for an indexing worker")

    async def test_attach_ignores_repositories_that_are_not_waiting(self):
        views = [{"id": "x", "status": "ready", "job_status": "completed", "queue": None}]
        await attach_queue_positions(self.store, views)
        self.assertIsNone(views[0]["queue"])


# ---------------------------------------------------------------------------
# UX-07: a mistyped repository is refused before anything is stored
# ---------------------------------------------------------------------------


class RepositoryExistenceTests(StoreCase):
    def limiter(self):
        return SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())

    def ingest_patches(self, exists):
        async def noop():
            return None

        return (
            patch.object(ingest_router, "ingest_request_limiter", self.limiter()),
            patch.object(ingest_router, "public_repository_exists", new=AsyncMock(return_value=exists)),
            patch.object(ingest_router, "assert_turso_schema", new=noop),
            patch.object(ingest_router, "get_turso_store", return_value=self.store),
        )

    async def repo_count(self):
        row = await self.store.fetch_one("SELECT COUNT(*) AS count FROM repos WHERE user_id = ?", [USER])
        return int(row["count"])

    async def test_missing_repository_is_reported_and_nothing_is_created(self):
        patches = self.ingest_patches(False)
        with patches[0], patches[1] as check, patches[2], patches[3], self.assertRaises(HTTPException) as raised:
            await ingest_router.ingest_repo(
                ingest_router.IngestRequest(github_url="pallets/clik-does-not-exist"), self.user,
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertTrue(raised.exception.detail.startswith("Repository not found"))
        self.assertIn("pallets/clik-does-not-exist", raised.exception.detail)
        check.assert_awaited_once_with("pallets", "clik-does-not-exist")
        self.assertEqual(await self.repo_count(), 0)
        jobs = await self.store.fetch_one("SELECT COUNT(*) AS count FROM ingestion_jobs")
        self.assertEqual(int(jobs["count"]), 0)

    async def test_existing_or_unknown_repository_is_queued_as_before(self):
        # True: GitHub has it. None: GitHub could not be asked, so indexing proceeds unchanged.
        for exists in (True, None):
            with self.subTest(exists=exists):
                user = SimpleNamespace(id=f"user-{exists}")
                patches = self.ingest_patches(exists)
                with patches[0], patches[1], patches[2], patches[3]:
                    response = await ingest_router.ingest_repo(
                        ingest_router.IngestRequest(github_url="https://github.com/pallets/click"), user,
                    )
                self.assertEqual(response["status"], "queued")
                row = await self.store.fetch_one(
                    "SELECT COUNT(*) AS count FROM repos r JOIN ingestion_jobs j ON j.repo_id = r.id "
                    "WHERE r.user_id = ? AND j.status = 'queued'", [user.id],
                )
                self.assertEqual(int(row["count"]), 1)

    async def test_existence_check_asks_the_archive_host_and_never_blocks_on_errors(self):
        requests = []

        def responder(status=None, error=None):
            def handler(request):
                requests.append((request.method, str(request.url)))
                if error:
                    raise error
                return httpx.Response(status)
            return handler

        real_client = httpx.AsyncClient
        cases = (
            (responder(404), False), (responder(200), True), (responder(403), None), (responder(500), None),
            (responder(error=httpx.ConnectError("offline")), None), (responder(error=httpx.ReadTimeout("slow")), None),
        )
        for handler, expected in cases:
            with self.subTest(expected=expected), patch.object(
                ingest_router.httpx, "AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
            ):
                self.assertIs(await ingest_router.public_repository_exists("pallets", "click"), expected)
        method, url = requests[0]
        self.assertEqual(method, "HEAD")
        self.assertEqual(url, f"{settings.github_codeload_base_url.rstrip('/')}/pallets/click/tar.gz/HEAD")

    async def test_url_mistakes_still_fail_before_any_network_call(self):
        check = AsyncMock(return_value=True)
        with patch.object(ingest_router, "public_repository_exists", new=check), self.assertRaises(HTTPException) as raised:
            await ingest_router.ingest_repo(ingest_router.IngestRequest(github_url="not a url"), self.user)
        self.assertEqual(raised.exception.status_code, 400)
        check.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
