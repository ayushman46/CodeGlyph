"""Embedding must survive inputs the provider permanently rejects."""

import unittest
from unittest.mock import patch

from config import settings
from ingest.embedder import (
    EMBEDDING_DIMENSION,
    EmbeddingUnavailableError,
    embed_chunks,
    is_rejected_input,
    sanitize_embedding_text,
)


def _chunks(count):
    return [
        {"file_path": f"src/{index}.ts", "content": f"export const value{index} = {index};", "start_line": 1, "end_line": 1, "language": "ts"}
        for index in range(count)
    ]


class EmbedderResilienceTests(unittest.TestCase):
    def test_data_uris_are_neutralised_but_other_text_is_untouched(self):
        # NVIDIA parses "data:image/..." as an image input and rejects the
        # request with HTTP 400 ("image inputs require VLM serving").
        self.assertEqual(sanitize_embedding_text('if (u.startsWith("data:image/")) {}'), 'if (u.startsWith("data :image/")) {}')
        self.assertEqual(sanitize_embedding_text("DATA: video/mp4"), "DATA : video/mp4")
        self.assertEqual(sanitize_embedding_text("data:audio/ogg;base64,AAA"), "data :audio/ogg;base64,AAA")
        for unchanged in ("metadata:image/png", "data:text/plain", "const data = 1", "database:image/x"):
            self.assertEqual(sanitize_embedding_text(unchanged), unchanged)

    def test_rejection_classification(self):
        self.assertTrue(is_rejected_input(EmbeddingUnavailableError("bad", status_code=400)))
        self.assertTrue(is_rejected_input(EmbeddingUnavailableError("bad", status_code=422)))
        self.assertFalse(is_rejected_input(EmbeddingUnavailableError("busy", status_code=429)))
        self.assertFalse(is_rejected_input(EmbeddingUnavailableError("down", status_code=503)))
        self.assertFalse(is_rejected_input(EmbeddingUnavailableError("timeout")))

    def test_sanitised_text_reaches_the_provider(self):
        captured = []

        class FakeEmbeddings:
            def create(self, **kwargs):
                captured.extend(kwargs["input"])
                from types import SimpleNamespace
                return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[0.0] * EMBEDDING_DIMENSION) for i in range(len(kwargs["input"]))])

        class FakeClient:
            embeddings = FakeEmbeddings()

        chunk = {"file_path": "src/ssrf.ts", "content": 'return url.startsWith("data:image/")', "start_line": 1, "end_line": 1, "language": "ts"}
        with patch("ingest.embedder.get_embedding_client", return_value=FakeClient()):
            embed_chunks([chunk])
        self.assertIn('"data :image/"', captured[0])
        self.assertEqual(chunk["content"], 'return url.startsWith("data:image/")', "stored text must not change")

    def test_one_rejected_passage_is_skipped_and_the_rest_are_embedded(self):
        chunks = _chunks(10)
        poison = "src/3.ts"

        def fake_embed(texts, *, input_type):
            if any(poison in text for text in texts):
                raise EmbeddingUnavailableError("rejected", status_code=400)
            return [[1.0] * EMBEDDING_DIMENSION for _ in texts]

        with patch.object(settings, "embedding_batch_size", 8), \
             patch.object(settings, "embedding_min_batch_size", 4), \
             patch("ingest.embedder.embed_texts", side_effect=fake_embed):
            result = embed_chunks(chunks)

        skipped = [chunk["file_path"] for chunk in result if chunk.get("_embedding_skipped")]
        self.assertEqual(skipped, [poison])
        self.assertTrue(all(chunk.get("embedding") is not None for chunk in result if chunk["file_path"] != poison))
        self.assertIsNone(next(chunk for chunk in result if chunk["file_path"] == poison).get("embedding"))

    def test_provider_outage_still_raises_so_the_job_degrades_once(self):
        def outage(texts, *, input_type):
            raise EmbeddingUnavailableError("down", status_code=503)

        with patch.object(settings, "embedding_batch_size", 8), \
             patch.object(settings, "embedding_min_batch_size", 4), \
             patch("ingest.embedder.embed_texts", side_effect=outage):
            with self.assertRaises(EmbeddingUnavailableError):
                embed_chunks(_chunks(6))

    def test_outage_during_isolation_is_not_mistaken_for_a_bad_passage(self):
        calls = {"count": 0}

        def flaky(texts, *, input_type):
            calls["count"] += 1
            if len(texts) > 1:
                raise EmbeddingUnavailableError("rejected", status_code=400)
            raise EmbeddingUnavailableError("down", status_code=502)

        with patch.object(settings, "embedding_batch_size", 4), \
             patch.object(settings, "embedding_min_batch_size", 4), \
             patch("ingest.embedder.embed_texts", side_effect=flaky):
            with self.assertRaises(EmbeddingUnavailableError):
                embed_chunks(_chunks(4))


if __name__ == "__main__":
    unittest.main()
