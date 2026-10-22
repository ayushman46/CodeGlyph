"""G3: relative imports must resolve, named imports must exist, and the extension convention is autofixed."""

import asyncio
import unittest

import test_edit_quality_data as data
from agent.quality import check_imports, js_exports
from test_edit_quality_support import B3, B4, KyEditorCase, code_edit

run = asyncio.run


class FakeRepo:
    """An in-memory RepoFiles for pure import-check tests."""

    def __init__(self, files: dict[str, str], *, incomplete: bool = False):
        self.files = files
        self.incomplete = incomplete

    async def existing(self, paths):
        return {path for path in paths if path in self.files}

    async def read(self, path):
        return self.files.get(path)

    async def heads(self, directory, extensions, exclude, limit):
        prefix = f"{directory}/" if directory else ""
        found = sorted(path for path in self.files if path.startswith(prefix) and "/" not in path[len(prefix):]
                       and path != exclude and path.rsplit(".", 1)[-1] in extensions)
        return {path: self.files[path] for path in found[:limit]}


def created(path, text):
    return {"file_path": path, "action": "create", "before": "", "after": text, "changes": []}


class ImportCheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo(dict(data.KY))

    def test_missing_extension_is_autofixed_to_js_when_neighbours_use_it(self):
        text = "import test from 'ava';\nimport {isObject} from '../source/utils/is';\n\ntest('x', t => {\n\tt.true(isObject({}));\n});\n"
        result = run(check_imports([created("test/is.ts", text)], self.repo))
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["after"]["test/is.ts"].split("\n")[1], "import {isObject} from '../source/utils/is.js';")
        self.assertEqual(result["autofixed"], [{"file_path": "test/is.ts", "rule": "import_extension", "lines": 1}])

    def test_a_name_the_target_does_not_export_fails(self):
        text = "import {nope} from '../source/utils/is.js';\n"
        result = run(check_imports([created("test/is.ts", text)], self.repo))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["problems"][0]["message"], "'nope' is not exported by source/utils/is.ts")
        self.assertEqual(result["problems"][0]["line"], 1)

    def test_an_unresolved_relative_import_fails(self):
        result = run(check_imports([created("source/utils/new.ts", "import x from './missing.js';\n")], self.repo))
        self.assertEqual(result["status"], "fail")
        self.assertIn("'./missing.js' does not resolve", result["problems"][0]["message"])
        # In a partially indexed repository the same finding is only a warning.
        partial = run(check_imports([created("source/utils/new.ts", "import x from './missing.js';\n")], FakeRepo(dict(data.KY), incomplete=True)))
        self.assertEqual(partial["status"], "warn")

    def test_imports_on_unchanged_lines_and_pre_existing_specifiers_are_not_judged(self):
        before = "import {a} from './gone.js';\nexport const b = a;\n"
        after = "import {a} from './gone.js';\nexport const b = a + 1;\n"
        item = {"file_path": "source/x.ts", "action": "modify", "before": before, "after": after, "changes": []}
        self.assertEqual(run(check_imports([item], self.repo))["status"], "skipped")

    def test_python_imports_resolve_and_names_must_exist(self):
        repo = FakeRepo({"pkg/__init__.py": "", "pkg/util.py": "def helper():\n    return 1\n\nVALUE = 2\n"})
        good = created("pkg/main.py", "from pkg.util import helper, VALUE\nfrom .util import helper as h\nimport os\n")
        self.assertEqual(run(check_imports([good], repo))["status"], "pass")
        bad = created("pkg/main.py", "from pkg.util import missing_name\n")
        result = run(check_imports([bad], repo))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["problems"][0]["message"], "'missing_name' is not defined in pkg/util.py")
        third_party = created("pkg/main.py", "from requests import get\n")
        self.assertEqual(run(check_imports([third_party], repo))["status"], "pass")

    def test_export_detection_covers_lists_defaults_and_reexports(self):
        names, opaque = js_exports("export const a = 1;\nexport {b as c, type D};\nexport default function e() {}\n")
        self.assertEqual((names, opaque), ({"a", "c", "D", "default", "e"}, False))
        self.assertTrue(js_exports("export * from './x.js';\n")[1])


B3_REPLY = code_edit([
    {"file_path": "source/utils/is.ts", "action": "modify", "summary": "Rename the export",
     "changes": [{"old": "export const isNonArrayObject =", "new": "export const isPlainObjectLike ="}]},
], title="Rename isNonArrayObject to isPlainObjectLike")


class EditorImportTests(KyEditorCase):
    def test_the_recovered_new_test_reply_imports_with_the_js_extension(self):
        result = self.edit(B4, [data.REPLIES["edit_reply_raw_tabs_new_test"]], evidence=self.evidence("source/utils/is.ts"))
        created_file = next(item for item in result.proposal["files"] if item["file_path"] == "test/is.ts")
        self.assertIn("from '../source/utils/is.js';", created_file["after"])
        self.assertEqual(self.check(result.proposal, "imports")["status"], "pass")
        self.assertIn({"file_path": "test/is.ts", "rule": "import_extension", "lines": 1}, result.proposal["quality"]["autofixed"])

    def test_the_rename_passes_the_import_check_in_all_four_files(self):
        result = self.edit(B3, [B3_REPLY], evidence=self.evidence("source/utils/is.ts"))
        proposal = result.proposal
        self.assertIsNotNone(proposal, result.markdown)
        self.assertEqual(sorted(item["file_path"] for item in proposal["files"]),
                         ["source/core/Ky.ts", "source/utils/is.ts", "source/utils/merge.ts", "source/utils/normalize.ts"])
        self.assertEqual(self.check(proposal, "imports")["status"], "pass")
        for item in proposal["files"]:
            self.assertNotIn("isNonArrayObject", item["after"])
            self.assertEqual(item["syntax"]["status"], "pass", item["file_path"])

    def test_a_wrong_import_triggers_the_repair_round(self):
        bad = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Use isNonNegativeNumber",
                          "changes": [{"old": "import {type InternalOptions} from '../types/options.js';",
                                       "new": "import {type InternalOptions} from '../types/options.js';\nimport {nope} from './is.js';"}]}])
        good = code_edit([{"file_path": "source/utils/delay.ts", "action": "modify", "summary": "Use isNonNegativeNumber",
                           "changes": [{"old": "import {type InternalOptions} from '../types/options.js';",
                                        "new": "import {type InternalOptions} from '../types/options.js';\nimport {isNonNegativeNumber} from './is.js';"}]}])
        result = self.edit("In source/utils/delay.ts import isNonNegativeNumber", [bad, good], evidence=self.evidence("source/utils/delay.ts"))
        repair = result.model.calls[1]["messages"][-1]["content"]
        self.assertIn("source/utils/delay.ts:4 'nope' is not exported by source/utils/is.ts", repair)
        self.assertEqual(self.check(result.proposal, "imports")["status"], "pass")


if __name__ == "__main__":
    unittest.main()
