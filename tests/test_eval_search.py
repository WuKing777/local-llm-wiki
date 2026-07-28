import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import ingest_file, init_repository, vector_rebuild
from kb.eval_search import eval_search


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


class CookingBiasedEmbeddingClient:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.0, 1.0] for _text in texts]


def configured_env() -> dict[str, str]:
    return {
        "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
        "KB_EMBEDDING_MODEL": "fake-eval-embed",
    }


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    if not root.exists():
        return []
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def sqlite_table_names(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
    finally:
        connection.close()


def write_benchmark(path: Path, *records: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def create_search_root(temp: Path) -> tuple[Path, str, str]:
    root = temp / "kb"
    init_repository(root)
    astronomy = temp / "astronomy.md"
    cooking = temp / "cooking.md"
    astronomy.write_text(
        "# Astronomy\n\nA galaxy contains stars and nebula retrieval facts.\n",
        encoding="utf-8",
    )
    cooking.write_text(
        "# Cooking\n\nA recipe contains flour and ingredients.\n",
        encoding="utf-8",
    )
    astronomy_id = ingest_file(root, astronomy)["source_id"]
    cooking_id = ingest_file(root, cooking)["source_id"]
    (root / "wiki" / "astronomy.md").write_text(
        "# Astronomy\n\nStable local page backed by sources.\n",
        encoding="utf-8",
    )
    return root, astronomy_id, cooking_id


class EvalSearchTests(unittest.TestCase):
    def test_fts_passes_without_embedding_config_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy nebula",
                    "expected_sources": [astronomy_id],
                    "expected_wiki_paths": ["wiki/astronomy.md"],
                    "privacy": "public",
                },
            )
            before = tree_snapshot(root)

            result = eval_search(root, benchmark, env={})

            self.assertEqual("pass", result["status"])
            self.assertEqual("pass", result["classification"])
            self.assertEqual(1, result["query_count"])
            self.assertEqual(1.0, result["fts_top_k_hit_rate"])
            self.assertEqual(
                "skipped_external_dependency",
                result["modes"]["semantic"]["classification"],
            )
            self.assertEqual(
                "skipped_external_dependency",
                result["modes"]["hybrid"]["classification"],
            )
            self.assertEqual(before, tree_snapshot(root))
            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("galaxy nebula", serialized)

    def test_semantic_and_hybrid_metrics_use_fake_embedding_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "expected_wiki_paths": ["wiki/astronomy.md"],
                    "privacy": "public",
                },
            )
            client = FakeEmbeddingClient()
            vector_rebuild(root, client=client, env=configured_env())
            client.calls.clear()

            result = eval_search(root, benchmark, client=client, env=configured_env())

            self.assertEqual("pass", result["status"])
            self.assertEqual(1.0, result["semantic_top_k_hit_rate"])
            self.assertEqual(1.0, result["hybrid_top_k_hit_rate"])
            self.assertEqual("pass", result["modes"]["semantic"]["classification"])
            self.assertEqual("pass", result["modes"]["hybrid"]["classification"])
            self.assertEqual(
                [["galaxy"], ["galaxy"]],
                client.calls,
            )

    def test_fts_failure_blocks_overall_pass_even_when_embeddings_hit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "space",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            client = FakeEmbeddingClient()
            vector_rebuild(root, client=client, env=configured_env())
            client.calls.clear()

            result = eval_search(root, benchmark, client=client, env=configured_env())

            self.assertEqual("failed", result["status"])
            self.assertEqual("fts_expectation_failed", result["classification"])
            self.assertEqual(0.0, result["fts_top_k_hit_rate"])
            self.assertEqual("pass", result["modes"]["semantic"]["classification"])
            self.assertEqual("pass", result["modes"]["hybrid"]["classification"])

    def test_semantic_failure_is_reported_when_fts_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            vector_rebuild(root, client=FakeEmbeddingClient(), env=configured_env())
            client = CookingBiasedEmbeddingClient()

            result = eval_search(
                root,
                benchmark,
                limit=1,
                client=client,
                env=configured_env(),
            )

            self.assertEqual("failed", result["status"])
            self.assertEqual("semantic_expectation_failed", result["classification"])
            self.assertEqual("pass", result["modes"]["fts"]["classification"])
            self.assertEqual(
                "semantic_expectation_failed",
                result["modes"]["semantic"]["classification"],
            )

    def test_unavailable_local_embedding_endpoint_skips_semantic_and_hybrid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            vector_rebuild(root, client=FakeEmbeddingClient(), env=configured_env())
            unavailable_env = {
                "KB_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
                "KB_EMBEDDING_MODEL": "fake-eval-embed",
                "KB_EMBEDDING_TIMEOUT_SECONDS": "0.01",
            }

            result = eval_search(root, benchmark, env=unavailable_env)

            self.assertEqual("pass", result["status"])
            self.assertEqual(
                "skipped_external_dependency",
                result["modes"]["semantic"]["classification"],
            )
            self.assertEqual(
                "skipped_external_dependency",
                result["modes"]["hybrid"]["classification"],
            )

    def test_source_card_present_but_missing_from_fts_reports_stale_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            connection = sqlite3.connect(root / "db" / "kb.sqlite3")
            try:
                connection.execute(
                    "DELETE FROM chunk_fts WHERE source_id = ?",
                    (astronomy_id,),
                )
                connection.commit()
            finally:
                connection.close()
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy nebula",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("fts_expectation_failed", result["classification"])
            self.assertEqual([astronomy_id], result["missing_expected_sources"])
            self.assertIn(
                {"type": "expected_source_not_indexed", "source_id": astronomy_id},
                result["stale_index_warnings"],
            )

    def test_missing_wiki_only_failure_has_specific_classification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy nebula",
                    "expected_sources": [astronomy_id],
                    "expected_wiki_paths": ["wiki/missing.md"],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("missing_expected_wiki_paths", result["classification"])
            self.assertEqual(["wiki/missing.md"], result["missing_expected_wiki_paths"])

    def test_secret_benchmark_is_rejected_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            sentinel = "".join(
                ["s", "k", "-", "task10", "-", "sentinel", "-", "0000000000000000"]
            )
            bearer = "token" + ".example" + ".value"
            private_block = (
                "-----BEGIN "
                + "PRIVATE KEY-----\nnot-real-material\n-----END "
                + "PRIVATE KEY-----"
            )
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": f"galaxy {sentinel}",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
                {
                    "query": "Authorization: " + "Bearer " + bearer,
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
                {
                    "query": private_block,
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertEqual("failed", result["status"])
            self.assertEqual("secret_in_benchmark", result["classification"])
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(bearer, serialized)
            self.assertNotIn("not-real-material", serialized)

    def test_bearer_and_private_key_markers_stop_before_client_call(self):
        bearer_query = "Bearer " + ("token" + ".example" + ".value")
        private_key_query = "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----"
        secret_cases = (
            (bearer_query, "token.example.value"),
            (private_key_query, "OPENSSH PRIVATE KEY"),
        )
        for query, forbidden_text in secret_cases:
            with self.subTest(query=query):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root, astronomy_id, _ = create_search_root(Path(tmpdir))
                    benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
                    write_benchmark(
                        benchmark,
                        {
                            "query": query,
                            "expected_sources": [astronomy_id],
                            "privacy": "public",
                        },
                    )
                    client = FakeEmbeddingClient()

                    result = eval_search(
                        root,
                        benchmark,
                        client=client,
                        env=configured_env(),
                    )

                    serialized = json.dumps(
                        result, ensure_ascii=False, sort_keys=True
                    )
                    self.assertEqual("failed", result["status"])
                    self.assertEqual("secret_in_benchmark", result["classification"])
                    self.assertEqual([], client.calls)
                    self.assertNotIn(forbidden_text, serialized)

    def test_unconfirmed_sensitive_or_restricted_samples_stop_before_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "space question",
                    "expected_sources": [astronomy_id],
                    "privacy": "sensitive",
                },
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "restricted",
                },
            )
            client = FakeEmbeddingClient()

            result = eval_search(root, benchmark, client=client, env=configured_env())

            self.assertEqual("failed", result["status"])
            self.assertEqual("policy_confirmation_required", result["classification"])
            self.assertEqual([], client.calls)

    def test_reports_missing_expected_assets_and_stale_index_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy nebula",
                    "expected_sources": ["src-000000000000"],
                    "expected_wiki_paths": ["wiki/missing.md"],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("fts_expectation_failed", result["classification"])
            self.assertEqual(["src-000000000000"], result["missing_expected_sources"])
            self.assertEqual(["wiki/missing.md"], result["missing_expected_wiki_paths"])
            self.assertGreaterEqual(len(result["stale_index_warnings"]), 2)

    def test_missing_search_index_fails_without_creating_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            database = root / "db" / "kb.sqlite3"
            database.unlink()

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("search_index_missing", result["classification"])
            self.assertFalse(database.exists())

    def test_existing_empty_search_index_fails_without_mutating_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            database = root / "db" / "kb.sqlite3"
            database.write_bytes(b"")

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("search_index_missing", result["classification"])
            self.assertEqual(b"", database.read_bytes())

    def test_partial_search_schema_fails_without_completing_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            database = root / "db" / "kb.sqlite3"
            database.unlink()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE documents ("
                    "id INTEGER PRIMARY KEY, "
                    "source_id TEXT NOT NULL UNIQUE, "
                    "raw_path TEXT NOT NULL, "
                    "title TEXT, "
                    "sha256 TEXT)"
                )
                connection.execute(
                    "CREATE VIRTUAL TABLE chunk_fts USING fts5("
                    "content, source_id UNINDEXED, document_id UNINDEXED, "
                    "chunk_id UNINDEXED)"
                )
                connection.commit()
            finally:
                connection.close()
            before_bytes = database.read_bytes()
            before_tables = sqlite_table_names(database)

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("search_index_missing", result["classification"])
            self.assertEqual(before_bytes, database.read_bytes())
            self.assertEqual(before_tables, sqlite_table_names(database))

    def test_malformed_search_schema_returns_classified_json_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            database = root / "db" / "kb.sqlite3"
            database.unlink()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE documents ("
                    "id INTEGER PRIMARY KEY, "
                    "source_id TEXT NOT NULL UNIQUE, "
                    "raw_path TEXT NOT NULL, "
                    "title TEXT, "
                    "sha256 TEXT)"
                )
                connection.execute(
                    "CREATE TABLE chunks ("
                    "id INTEGER PRIMARY KEY, "
                    "document_id INTEGER NOT NULL, "
                    "source_id TEXT NOT NULL, "
                    "chunk_index INTEGER NOT NULL, "
                    "content TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE events ("
                    "id INTEGER PRIMARY KEY, "
                    "event_type TEXT NOT NULL, "
                    "message TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE chunk_fts ("
                    "content TEXT, source_id TEXT, document_id INTEGER, chunk_id INTEGER)"
                )
                connection.commit()
            finally:
                connection.close()
            before_bytes = database.read_bytes()
            before_tables = sqlite_table_names(database)

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("search_index_missing", result["classification"])
            self.assertEqual(before_bytes, database.read_bytes())
            self.assertEqual(before_tables, sqlite_table_names(database))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "eval-search",
                        "--root",
                        str(root),
                        "--benchmark",
                        str(benchmark),
                        "--json",
                    ]
                )

            self.assertEqual(1, code)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(
                "search_index_missing",
                json.loads(stdout.getvalue())["classification"],
            )

    def test_malformed_documents_schema_returns_classified_json_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )
            database = root / "db" / "kb.sqlite3"
            database.unlink()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE documents ("
                    "id INTEGER PRIMARY KEY, "
                    "source_id TEXT NOT NULL UNIQUE)"
                )
                connection.execute(
                    "CREATE TABLE chunks ("
                    "id INTEGER PRIMARY KEY, "
                    "document_id INTEGER NOT NULL, "
                    "source_id TEXT NOT NULL, "
                    "chunk_index INTEGER NOT NULL, "
                    "content TEXT NOT NULL, "
                    "created_at TEXT)"
                )
                connection.execute(
                    "CREATE TABLE events ("
                    "id INTEGER PRIMARY KEY, "
                    "event_type TEXT NOT NULL, "
                    "message TEXT NOT NULL, "
                    "created_at TEXT)"
                )
                connection.execute(
                    "CREATE VIRTUAL TABLE chunk_fts USING fts5("
                    "content, source_id UNINDEXED, document_id UNINDEXED, "
                    "chunk_id UNINDEXED)"
                )
                connection.commit()
            finally:
                connection.close()
            before_bytes = database.read_bytes()
            before_tables = sqlite_table_names(database)

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("search_index_missing", result["classification"])
            self.assertEqual(before_bytes, database.read_bytes())
            self.assertEqual(before_tables, sqlite_table_names(database))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "eval-search",
                        "--root",
                        str(root),
                        "--benchmark",
                        str(benchmark),
                        "--json",
                    ]
                )

            self.assertEqual(1, code)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(
                "search_index_missing",
                json.loads(stdout.getvalue())["classification"],
            )

    def test_invalid_benchmark_encoding_and_paths_are_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, astronomy_id, _ = create_search_root(temp)
            invalid_utf8 = root / "meta" / "evals" / "invalid-utf8.jsonl"
            invalid_utf8.parent.mkdir(parents=True, exist_ok=True)
            invalid_utf8.write_bytes(b"\xff\xfe\x00")

            result = eval_search(root, invalid_utf8, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("invalid_benchmark", result["classification"])

            outside = temp / "outside.jsonl"
            write_benchmark(
                outside,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "privacy": "public",
                },
            )

            result = eval_search(root, outside, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("invalid_benchmark", result["classification"])

            malformed_wiki = root / "meta" / "evals" / "bad-wiki.jsonl"
            write_benchmark(
                malformed_wiki,
                {
                    "query": "galaxy",
                    "expected_sources": [astronomy_id],
                    "expected_wiki_paths": ["wiki//astronomy.md"],
                    "privacy": "public",
                },
            )

            result = eval_search(root, malformed_wiki, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual("invalid_benchmark", result["classification"])

    def test_expected_quotes_shape_matches_schema_check_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            base_record = {
                "query": "galaxy nebula",
                "expected_sources": [astronomy_id],
                "privacy": "public",
            }

            write_benchmark(benchmark, base_record)
            omitted = eval_search(root, benchmark, env={})
            self.assertNotEqual("invalid_benchmark", omitted["classification"])

            valid_record = dict(base_record)
            valid_record["expected_quotes"] = ["galaxy contains stars"]
            write_benchmark(benchmark, valid_record)
            valid = eval_search(root, benchmark, env={})
            self.assertNotEqual("invalid_benchmark", valid["classification"])

            invalid_values = [
                "galaxy contains stars",
                [],
                ["valid", ""],
                ["valid", 123],
            ]
            for value in invalid_values:
                with self.subTest(expected_quotes=value):
                    record = dict(base_record)
                    record["expected_quotes"] = value
                    write_benchmark(benchmark, record)

                    result = eval_search(root, benchmark, env={})

                    self.assertEqual("failed", result["status"])
                    self.assertEqual("invalid_benchmark", result["classification"])

    def test_benchmark_without_expected_sources_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "unmatched query",
                    "expected_wiki_paths": ["wiki/astronomy.md"],
                    "privacy": "public",
                },
            )
            client = FakeEmbeddingClient()

            result = eval_search(root, benchmark, client=client, env=configured_env())

            self.assertEqual("failed", result["status"])
            self.assertEqual("invalid_benchmark", result["classification"])
            self.assertEqual([], client.calls)

    def test_cli_json_redacts_output_and_sets_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, astronomy_id, _ = create_search_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "galaxy nebula",
                    "expected_sources": [astronomy_id],
                    "expected_wiki_paths": ["wiki/astronomy.md"],
                    "privacy": "public",
                },
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, {}, clear=True):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "eval-search",
                            "--root",
                            str(root),
                            "--benchmark",
                            str(benchmark),
                            "--json",
                        ]
                    )

            self.assertEqual(0, code)
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual("pass", data["status"])
            self.assertNotIn("galaxy nebula", stdout.getvalue())

            invalid = root / "meta" / "evals" / "invalid.jsonl"
            invalid.write_text("{not-json\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                failing_code = main(
                    [
                        "eval-search",
                        "--root",
                        str(root),
                        "--benchmark",
                        str(invalid),
                        "--json",
                    ]
                )

            self.assertEqual(1, failing_code)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(
                "invalid_benchmark", json.loads(stdout.getvalue())["classification"]
            )


if __name__ == "__main__":
    unittest.main()
