"""Isolate the test suite from real services before any app module loads.

unittest discovery imports test modules in sorted order, so this module runs
first. Environment variables take precedence over the project .env file, so
the suite never reaches a developer's production Turso database, NVIDIA key,
or GitHub OAuth app — even for the health endpoint, which bootstraps the
schema. Individual tests still patch stores and providers explicitly.
"""

import atexit
import os
import shutil
import tempfile
import unittest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="codebase-intel-tests-")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

os.environ["TURSO_DATABASE_URL"] = f"file:{os.path.join(_TEST_DATA_DIR, 'suite.db')}"
os.environ["TURSO_AUTH_TOKEN"] = "local-test-token"
os.environ["NVIDIA_API_KEY"] = ""
os.environ["LOCAL_INGESTION_WORKER"] = "false"
os.environ["KEEPALIVE_DURING_INGESTION"] = "false"
os.environ["GITHUB_CLIENT_ID"] = ""
os.environ["GITHUB_CLIENT_SECRET"] = ""
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""


class EnvironmentIsolationTests(unittest.TestCase):
    def test_suite_uses_a_local_database(self):
        from config import settings

        self.assertTrue(settings.turso_database_url.startswith("file:"))
        self.assertEqual(settings.nvidia_api_key, "")
