"""Caching and precompressed serving of the built frontend (single-service deploys)."""

import gzip
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from frontend_static import IMMUTABLE, REVALIDATE, HashedAssets, accepted_encodings, revalidated_file

SCRIPT = b"console.log('hello');\n" * 200


class FrontendStaticTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        assets = root / "assets"
        assets.mkdir()
        (assets / "app-abc123.js").write_bytes(SCRIPT)
        (assets / "app-abc123.js.gz").write_bytes(gzip.compress(SCRIPT))
        (assets / "app-abc123.js.br").write_bytes(b"pretend-brotli")
        (assets / "plain-def456.css").write_bytes(b"body{color:red}" * 100)
        (root / "index.html").write_text("<!doctype html><title>x</title>")
        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=1024)
        app.mount("/assets", HashedAssets(directory=assets), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            return revalidated_file(root / "index.html")

        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def test_hashed_assets_are_immutable_and_use_the_best_accepted_copy(self):
        brotli = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(brotli.status_code, 200)
        self.assertEqual(brotli.headers["content-encoding"], "br")
        self.assertEqual(brotli.headers["cache-control"], IMMUTABLE)
        self.assertIn("javascript", brotli.headers["content-type"])
        self.assertEqual(brotli.headers["vary"], "Accept-Encoding")
        self.assertEqual(brotli.headers["content-length"], str(len(b"pretend-brotli")))

        gzipped = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip, br;q=0"})
        self.assertEqual(gzipped.headers["content-encoding"], "gzip")
        self.assertEqual(gzipped.content, SCRIPT)  # the client decodes it
        self.assertEqual(gzipped.headers["cache-control"], IMMUTABLE)

        plain = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.content, SCRIPT)
        self.assertEqual(plain.headers["cache-control"], IMMUTABLE)

    def test_an_asset_without_copies_is_compressed_on_the_fly_once(self):
        response = self.client.get("/assets/plain-def456.css", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(response.content, b"body{color:red}" * 100)
        self.assertEqual(response.headers["cache-control"], IMMUTABLE)

    def test_revalidation_returns_304_per_encoding(self):
        first = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "br"})
        again = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "br", "If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        other = self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]})
        self.assertEqual(other.status_code, 200)

    def test_missing_assets_404_and_pages_revalidate(self):
        self.assertEqual(self.client.get("/assets/missing-000.js").status_code, 404)
        page = self.client.get("/app/r/demo")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], REVALIDATE)

    def test_accept_encoding_parsing(self):
        self.assertEqual(accepted_encodings("gzip, deflate, br"), {"gzip", "deflate", "br"})
        self.assertEqual(accepted_encodings("br;q=0, gzip;q=0.5"), {"gzip"})
        self.assertEqual(accepted_encodings("BR ; q=1.0"), {"br"})
        self.assertEqual(accepted_encodings(""), set())
        self.assertEqual(accepted_encodings("gzip;q=bogus"), set())


if __name__ == "__main__":
    unittest.main()
