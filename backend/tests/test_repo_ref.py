import unittest

from api.repository_state import present_repository


def _row(**overrides):
    row = {
        "id": "r1", "repo_name": "demo", "github_url": "https://github.com/acme/demo",
        "repo_status": "ready", "index_phase": "ready", "chunk_count": 5, "job_status": "completed",
        "selection_json": {}, "run_prefix": None,
    }
    row.update(overrides)
    return row


class RepositoryRefTests(unittest.TestCase):
    def test_ref_and_kind_come_from_snapshot_summary(self):
        repo = present_repository(_row(selection_json={"ref": "release/1.2", "ref_kind": "tree"}))
        self.assertEqual(repo["ref"], "release/1.2")
        self.assertEqual(repo["ref_kind"], "tree")

    def test_ref_fields_are_null_when_unknown(self):
        for selection in ({}, None, "not-json", {"ref": "", "ref_kind": "tree"}, {"ref_kind": "tree"}):
            repo = present_repository(_row(selection_json=selection))
            self.assertIn("ref", repo)
            self.assertIn("ref_kind", repo)
            self.assertIsNone(repo["ref"])
            self.assertIsNone(repo["ref_kind"])


if __name__ == "__main__":
    unittest.main()
