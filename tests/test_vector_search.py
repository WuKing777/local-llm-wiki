import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from kb.commands import (
    ingest_file,
    init_repository,
    refresh_source,
    search,
    semantic_search,
    hybrid_search,
    vector_rebuild,
)


class FakeEmbeddingClient:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            if "galaxy" in lowered or "space" in lowered:
                vectors.append([1.0, 0.0])
            elif "recipe" in lowered:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.5, 0.5])
        return vectors


class VectorSearchTests(unittest.TestCase):
    def test_vector_rebuild_and_semantic_search_use_local_source_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            astronomy = temp / "astronomy.md"
            cooking = temp / "cooking.md"
            astronomy.write_text("# Astronomy\n\nA galaxy contains stars.", encoding="utf-8")
            cooking.write_text("# Cooking\n\nA recipe contains ingredients.", encoding="utf-8")
            astronomy_id = ingest_file(root, astronomy)["source_id"]
            ingest_file(root, cooking)
            client = FakeEmbeddingClient()
            env = {"KB_EMBEDDING_BASE_URL": "http://fake.local/v1", "KB_EMBEDDING_MODEL": "fake-embed"}

            rebuild = vector_rebuild(root, client=client, env=env)
            results = semantic_search(root, "space question", limit=1, client=client, env=env)

            self.assertEqual(2, rebuild["chunks"])
            self.assertEqual(astronomy_id, results[0]["source_id"])
            self.assertEqual("raw/", results[0]["raw_path"][:4])
            self.assertGreater(results[0]["score"], 0.99)
            self.assertGreaterEqual(len(client.calls), 2)

    def test_vector_rebuild_missing_config_does_not_mutate_existing_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nStable text.", encoding="utf-8")
            ingest_file(root, source)
            tracked = [
                root / "meta" / "log.md",
                root / "meta" / "review-queue.md",
                root / "db" / "kb.sqlite3",
            ]
            before = {path: path.read_bytes() for path in tracked}

            with self.assertRaisesRegex(RuntimeError, "KB_EMBEDDING_BASE_URL is required"):
                vector_rebuild(root, env={})

            self.assertEqual(before, {path: path.read_bytes() for path in tracked})

    def test_semantic_search_empty_vector_index_does_not_call_embedding_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            client = FakeEmbeddingClient()
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }

            results = semantic_search(root, "anything", client=client, env=env)

            self.assertEqual([], results)
            self.assertEqual([], client.calls)

    def test_refresh_source_removes_stale_vector_hits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "analysis.md"
            source.write_text("# Analysis\n\nDamaged galaxy text.", encoding="utf-8")
            old_source_id = ingest_file(root, source)["source_id"]
            client = FakeEmbeddingClient()
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }
            vector_rebuild(root, client=client, env=env)
            imported = next((root / "raw" / "imports").rglob("analysis.md"))
            imported.write_text("# Analysis\n\nValid recipe text.", encoding="utf-8")
            refreshed = refresh_source(root, old_source_id)

            results = semantic_search(root, "space question", client=client, env=env)

            self.assertNotIn(old_source_id, [result["source_id"] for result in results])
            self.assertNotIn("Damaged galaxy text", "\n".join(str(r["snippet"]) for r in results))
            self.assertNotEqual(old_source_id, refreshed["source_id"])

    def test_semantic_search_rejects_malformed_vector_index_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nA galaxy contains stars.", encoding="utf-8")
            ingest_file(root, source)
            client = FakeEmbeddingClient()
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }
            vector_rebuild(root, client=client, env=env)
            with closing(sqlite3.connect(root / "db" / "kb.sqlite3")) as connection:
                connection.execute("UPDATE chunk_vectors SET vector_json = ?", ("not-json",))
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "Invalid vector index; run vector-rebuild"):
                semantic_search(root, "space", client=client, env=env)

    def test_semantic_search_rejects_malformed_vector_schema_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nA galaxy contains stars.", encoding="utf-8")
            ingest_file(root, source)
            with closing(sqlite3.connect(root / "db" / "kb.sqlite3")) as connection:
                connection.execute("CREATE TABLE chunk_vectors (bad_column TEXT)")
                connection.commit()
            client = FakeEmbeddingClient()
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }

            with self.assertRaisesRegex(RuntimeError, "Invalid vector index; run vector-rebuild"):
                semantic_search(root, "space", client=client, env=env)

    def test_hybrid_search_filters_fts_results_without_current_source_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nA galaxy contains stars.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            (root / "sources" / f"{source_id}.md").unlink()
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }

            results = hybrid_search(root, "galaxy", client=FakeEmbeddingClient(), env=env)

            self.assertEqual([], results)

    def test_hybrid_search_keeps_valid_fts_result_without_vector_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nA galaxy contains stars.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            env = {
                "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
                "KB_EMBEDDING_MODEL": "fake-embed",
            }

            results = hybrid_search(root, "galaxy", client=FakeEmbeddingClient(), env=env)

            self.assertEqual(source_id, results[0]["source_id"])
            self.assertEqual("fts", results[0]["retrieval"])

    def test_refresh_source_malformed_vector_schema_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "analysis.md"
            source.write_text("# Analysis\n\nDamaged galaxy text.", encoding="utf-8")
            old_source_id = ingest_file(root, source)["source_id"]
            imported = next((root / "raw" / "imports").rglob("analysis.md"))
            imported.write_text("# Analysis\n\nValid recipe text.", encoding="utf-8")
            with closing(sqlite3.connect(root / "db" / "kb.sqlite3")) as connection:
                connection.execute("CREATE TABLE chunk_vectors (bad_column TEXT)")
                connection.commit()
            tracked = [
                root / "meta" / "source-map.jsonl",
                root / "meta" / "log.md",
                root / "db" / "kb.sqlite3",
                root / "sources" / f"{old_source_id}.md",
            ]
            before = {path: path.read_bytes() for path in tracked}
            source_names_before = sorted(path.name for path in (root / "sources").glob("src-*.md"))

            with self.assertRaisesRegex(RuntimeError, "Invalid vector index; run vector-rebuild"):
                refresh_source(root, old_source_id)

            self.assertEqual(before, {path: path.read_bytes() for path in tracked})
            self.assertEqual(
                source_names_before,
                sorted(path.name for path in (root / "sources").glob("src-*.md")),
            )
            self.assertEqual(old_source_id, search(root, "Damaged galaxy")[0]["source_id"])

    def test_cli_embedding_check_redacts_api_key(self):
        project_root = Path(__file__).resolve().parents[1]
        secret = "s" + "k-" + "test-embedding-secret"
        env = os.environ.copy()
        env["KB_EMBEDDING_BASE_URL"] = "http://fake.local/v1"
        env["KB_EMBEDDING_MODEL"] = "fake-embed"
        env["KB_EMBEDDING_API_KEY"] = secret

        completed = subprocess.run(
            [sys.executable, "-m", "kb", "embedding-check"],
            cwd=project_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Embedding config ok", completed.stdout)
        self.assertIn("KB_EMBEDDING_API_KEY=set", completed.stdout)
        self.assertFalse(secret in completed.stdout, "embedding key leaked to stdout")
        self.assertFalse(secret in completed.stderr, "embedding key leaked to stderr")


if __name__ == "__main__":
    unittest.main()
