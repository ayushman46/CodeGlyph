"""Import resolution for the change-impact graph (BUG-10).

Package imports never resolved, so tests never appeared as dependents:
``import flask`` in a test, ``src/`` layouts, ``require('../')`` in a test and
a package importing itself by name. Examples in docstrings and doc comments
are not imports. Offline; each test builds a small checkout in a temp dir.
"""

import tempfile
import unittest
from pathlib import Path

from ingest.dependencies import (
    GRAPH_VERSION,
    build_manifest_and_dependency_manifest,
    graph_version_edge,
    graph_version_from_marker,
)


def build_graph(files: dict[str, str]) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel_path, content in files.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _manifest, edges = build_manifest_and_dependency_manifest([str(root / path) for path in files], str(root))
    return edges


def pairs(edges):
    return {(edge["source_file"], edge["target_file"]) for edge in edges}


FLASK_LIKE = {
    "pyproject.toml": "[project]\nname = 'flask'\n",
    "src/flask/__init__.py": "from .app import Flask as Flask\nfrom .testing import FlaskClient as FlaskClient\n",
    "src/flask/app.py": (
        "import typing\n"
        "from . import cli\n"
        "from .globals import request\n"
        '"""Example:\n'
        "from flask import Flask\n"
        '"""\n'
        "class Flask:\n"
        "    def make_response(self, rv):\n"
        "        return rv\n"
    ),
    "src/flask/cli.py": "def main():\n    from . import Flask\n",
    "src/flask/globals.py": "request = None\n",
    "src/flask/typing.py": "ResponseValue = str\n",
    "src/flask/testing.py": "import typing as t\n\nif t.TYPE_CHECKING:\n    from .app import Flask\n",
    "src/flask/sansio/__init__.py": "",
    "src/flask/sansio/app.py": "from ..globals import request\n",
    "tests/conftest.py": "import pytest\nimport flask\n",
    "tests/test_basic.py": (
        "import pytest\n"
        "import flask\n"
        "from flask.testing import FlaskClient\n"
        "\n"
        "def test_response_types(app, client):\n"
        "    @app.route('/text')\n"
        "    def from_text():\n"
        "        return 'Hello'\n"
        "    assert client.get('/text').data == b'Hello'\n"
        "\n"
        "def test_make_response(app):\n"
        "    rv = flask.make_response()\n"
        "    assert rv.status_code == 200\n"
    ),
    "tests/test_apps/cliapp/__init__.py": "",
    "tests/test_apps/cliapp/app.py": "from flask import Flask\n\ntestapp = Flask('testapp')\n",
    "tests/test_apps/helloworld/hello.py": "from flask import Flask\napp = Flask(__name__)\n",
    "tests/test_apps/helloworld/wsgi.py": "from hello import app\n",
}


# ---------------------------------------------------------------------------
# BUG-10: the resolver
# ---------------------------------------------------------------------------


class PythonResolutionTests(unittest.TestCase):
    def test_package_imports_resolve_in_src_layouts(self):
        edges = build_graph(FLASK_LIKE)
        found = pairs(edges)
        self.assertIn(("tests/test_basic.py", "src/flask/__init__.py"), found)
        self.assertIn(("tests/test_basic.py", "src/flask/testing.py"), found)
        self.assertIn(("tests/conftest.py", "src/flask/__init__.py"), found)
        # Fixture apps inside a package still import the real flask.
        self.assertIn(("tests/test_apps/cliapp/app.py", "src/flask/__init__.py"), found)
        # A script directory sees its siblings first, as Python does.
        self.assertIn(("tests/test_apps/helloworld/wsgi.py", "tests/test_apps/helloworld/hello.py"), found)
        lines = {}
        for edge in edges:
            lines.setdefault((edge["source_file"], edge["target_file"]), []).append(edge["line_number"])
        # ``import flask`` and, because it runs the package first, ``from flask.testing import``.
        self.assertEqual(lines[("tests/test_basic.py", "src/flask/__init__.py")], [2, 3])
        self.assertEqual(lines[("tests/test_basic.py", "src/flask/testing.py")], [3])
        self.assertEqual(lines[("src/flask/testing.py", "src/flask/app.py")], [4])

    def test_inside_a_package_absolute_names_are_not_siblings_and_docstrings_are_not_imports(self):
        found = pairs(build_graph(FLASK_LIKE))
        # ``import typing`` is the standard library, not src/flask/typing.py.
        self.assertNotIn(("src/flask/app.py", "src/flask/typing.py"), found)
        # The ``from flask import Flask`` in the docstring is an example.
        self.assertNotIn(("src/flask/app.py", "src/flask/__init__.py"), found)
        # ``from . import cli`` imports the submodule, not the package init.
        self.assertIn(("src/flask/app.py", "src/flask/cli.py"), found)
        # ...while ``from . import Flask`` (an attribute) does use the init.
        self.assertIn(("src/flask/cli.py", "src/flask/__init__.py"), found)
        self.assertIn(("src/flask/sansio/app.py", "src/flask/globals.py"), found)

    def test_flat_layout_with_a_tests_package(self):
        found = pairs(build_graph({
            "httpx/__init__.py": "from ._client import Client\n",
            "httpx/_client.py": "class Client: pass\n",
            "tests/__init__.py": "",
            "tests/common.py": "FIXTURES = 1\n",
            "tests/client/__init__.py": "",
            "tests/client/test_client.py": "import httpx\nfrom tests.common import FIXTURES\nfrom httpx._client import Client\n",
        }))
        self.assertIn(("tests/client/test_client.py", "httpx/__init__.py"), found)
        self.assertIn(("tests/client/test_client.py", "tests/common.py"), found)
        self.assertIn(("tests/client/test_client.py", "httpx/_client.py"), found)

    def test_from_package_import_records_submodules_and_the_package(self):
        found = pairs(build_graph({
            "pkg/__init__.py": "VALUE = 1\n",
            "pkg/models.py": "x = 1\n",
            "app/main.py": "from pkg import models, VALUE\n",
        }))
        self.assertEqual(found, {("app/main.py", "pkg/models.py"), ("app/main.py", "pkg/__init__.py")})

    def test_a_package_in_docs_never_stands_in_for_the_standard_library_or_other_projects(self):
        found = pairs(build_graph({
            "pyproject.toml": "[project]\nname = 'app'\n",
            "src/app/__init__.py": "",
            "src/app/main.py": "import logging\nimport logging.handlers\nimport utils\n",
            "tests/test_main.py": "import logging\nimport logging.handlers\nimport app\n",
            "docs/examples/logging/__init__.py": "",
            "docs/examples/logging/handlers.py": "",
            "docs/examples/run.py": "import logging\n",
            "scripts/utils/__init__.py": "",
        }))
        self.assertEqual(found, {
            ("tests/test_main.py", "src/app/__init__.py"),
            # Inside its own tree the example package is what Python finds.
            ("docs/examples/run.py", "docs/examples/logging/__init__.py"),
        })

    def test_installed_projects_and_fixture_apps_in_a_test_folder_still_resolve(self):
        found = pairs(build_graph({
            # A monorepo: every project is installed, so any file can import it.
            "packages/core/pyproject.toml": "[project]\nname = 'core'\n",
            "packages/core/src/core/__init__.py": "",
            "packages/core/tests/test_core.py": "import core\n",
            "apps/web/main.py": "import core\n",
            # Flask's conftest puts tests/test_apps on sys.path.
            "tests/test_blueprints.py": "from blueprintapp import app\n",
            "tests/test_apps/blueprintapp/__init__.py": "app = 1\n",
        }))
        self.assertEqual(found, {
            ("packages/core/tests/test_core.py", "packages/core/src/core/__init__.py"),
            ("apps/web/main.py", "packages/core/src/core/__init__.py"),
            ("tests/test_blueprints.py", "tests/test_apps/blueprintapp/__init__.py"),
        })


class JavaScriptResolutionTests(unittest.TestCase):
    def test_tests_that_require_the_package_directory_resolve_to_its_entry(self):
        edges = build_graph({
            "package.json": '{"name": "express", "main": "index.js"}',
            "index.js": "module.exports = require('./lib/express');\n",
            "lib/express.js": "var req = require('./request');\n",
            "lib/request.js": "module.exports = {};\n",
            "test/req.get.js": "var express = require('../')\n  , request = require('supertest');\n",
            "examples/hello/index.js": "var express = require('../../');\n",
        })
        found = pairs(edges)
        self.assertIn(("test/req.get.js", "index.js"), found)
        self.assertIn(("examples/hello/index.js", "index.js"), found)
        self.assertIn(("lib/express.js", "lib/request.js"), found)

    def test_a_directory_without_package_json_uses_its_index(self):
        found = pairs(build_graph({
            "index.js": "module.exports = 1;\n",
            "test/a.test.js": "const lib = require('..');\n",
        }))
        self.assertEqual(found, {("test/a.test.js", "index.js")})

    def test_examples_in_doc_comments_are_not_imports(self):
        found = pairs(build_graph({
            "package.json": '{"name": "ky", "main": "./distribution/index.js"}',
            "source/index.ts": "export default {};\n",
            "source/helper.ts": "export const helper = 1;\n",
            "source/constants.ts": (
                "/**\n"
                "Retry a request.\n"
                "@example\n"
                "```\n"
                "import ky from 'ky';\n"
                "```\n"
                "*/\n"
                "export const glob = 'src/**/*.ts';\n"
                "import {helper} from './helper.js';\n"
            ),
        }))
        self.assertEqual(found, {("source/constants.ts", "source/helper.ts")})

    def test_a_package_importing_itself_by_name_finds_the_source_of_a_built_entry(self):
        found = pairs(build_graph({
            "package.json": '{"name": "ky", "exports": {"types": "./distribution/index.d.ts", "default": "./distribution/index.js"}}',
            "source/index.ts": "export default {};\n",
            "test/main.ts": "import ky from 'ky';\n",
        }))
        self.assertEqual(found, {("test/main.ts", "source/index.ts")})


class GraphVersionTests(unittest.TestCase):
    def test_marker_round_trip(self):
        marker = graph_version_edge()
        self.assertEqual((marker["source_file"], marker["target_file"]), ("", ""))
        self.assertEqual(graph_version_from_marker(marker["import_name"]), GRAPH_VERSION)
        self.assertEqual(graph_version_from_marker(None), 1)
        self.assertEqual(graph_version_from_marker("graph-version:x"), 1)


if __name__ == "__main__":
    unittest.main()
