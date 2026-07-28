import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kb.commands import ingest_file, init_repository, vector_rebuild
from kb.eval_search import eval_search
from kb.retrieval_benchmark import add_benchmark_case
from kb.sources import read_source_card


class FakeEmbeddingClient:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            if "aurora" in lowered:
                vectors.append([1.0, 0.0])
            elif "duplicate" in lowered:
                vectors.append([0.7, 0.3])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def configured_env() -> dict[str, str]:
    return {
        "KB_EMBEDDING_BASE_URL": "http://fake.local/v1",
        "KB_EMBEDDING_MODEL": "fake-retrieval-quality",
    }


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def write_benchmark(path: Path, *records: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def create_quality_root(temp: Path) -> tuple[Path, str, str]:
    root = temp / "kb"
    init_repository(root)
    primary = temp / "aurora.md"
    duplicate = temp / "aurora-copy.md"
    primary.write_text(
        "# Aurora Study\n\nThe aurora quote anchors local retrieval quality.\n",
        encoding="utf-8",
    )
    duplicate.write_text(
        "# Aurora Study Copy\n\nThe aurora quote anchors local retrieval quality.\n",
        encoding="utf-8",
    )
    primary_id = ingest_file(root, primary)["source_id"]
    duplicate_id = ingest_file(root, duplicate)["source_id"]
    primary_card = root / "sources" / f"{primary_id}.md"
    primary_card_lines = primary_card.read_text(encoding="utf-8").splitlines()
    primary_card_lines.insert(primary_card_lines.index("---", 1), "review_status: reviewed")
    primary_card.write_text("\n".join(primary_card_lines) + "\n", encoding="utf-8")
    (root / "wiki" / "aurora.md").write_text(
        "# Aurora\n\nStable page placeholder.\n",
        encoding="utf-8",
    )
    return root, primary_id, duplicate_id


class RetrievalQualityTests(unittest.TestCase):
    def test_eval_search_returns_redacted_quality_report_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "aurora quote",
                    "expected_source_ids": [source_id],
                    "expected_wiki_paths": ["wiki/aurora.md"],
                    "expected_quotes": [
                        "The aurora quote anchors local retrieval quality."
                    ],
                    "privacy": "public",
                },
            )
            before = tree_snapshot(root)

            result = eval_search(root, benchmark, env={})

            self.assertEqual("pass", result["status"])
            report = result["benchmark_report"]
            self.assertEqual(1, report["query_count"])
            self.assertEqual("metric_only", report["quote_support"]["authority"])
            self.assertEqual(1.0, report["quote_support"]["hit_rate"])
            self.assertEqual([], report["duplicate_warnings"])
            self.assertEqual([], report["low_quality_source_markers"])
            self.assertEqual([], report["residual_risks"])
            self.assertEqual(before, tree_snapshot(root))
            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("aurora quote", serialized)
            self.assertNotIn(
                "The aurora quote anchors local retrieval quality.", serialized
            )

    def test_quote_support_requires_retrieved_evidence_not_full_source_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "nonmatching term",
                    "expected_source_ids": [source_id],
                    "expected_quotes": [
                        "The aurora quote anchors local retrieval quality."
                    ],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            self.assertEqual("failed", result["status"])
            self.assertEqual(0.0, result["fts_top_k_hit_rate"])
            self.assertEqual(0.0, result["benchmark_report"]["quote_support"]["hit_rate"])
            marker_types = {
                marker["type"]
                for marker in result["benchmark_report"]["low_quality_source_markers"]
            }
            self.assertIn("missing_expected_quote", marker_types)

    def test_eval_search_reports_duplicates_and_low_quality_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, duplicate_id = create_quality_root(Path(tmpdir))
            source_card = root / "sources" / f"{source_id}.md"
            lines = source_card.read_text(encoding="utf-8").splitlines()
            closing = lines.index("---", 1)
            lines[closing:closing] = [
                "review_status: rejected",
                "privacy: sensitive",
            ]
            source_card.write_text("\n".join(lines) + "\n", encoding="utf-8")
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            duplicate_record = {
                "query": "aurora quote",
                "expected_source_ids": [source_id, duplicate_id],
                "expected_wiki_paths": ["wiki/aurora.md"],
                "expected_quotes": ["missing local quote"],
                "privacy": "public",
            }
            write_benchmark(benchmark, duplicate_record, duplicate_record)

            result = eval_search(root, benchmark, env={})

            report = result["benchmark_report"]
            self.assertIn(
                {"type": "duplicate_query", "first_line": 1, "line_number": 2},
                report["duplicate_warnings"],
            )
            self.assertIn(
                {
                    "type": "duplicate_expected_sources",
                    "first_line": 1,
                    "line_number": 2,
                },
                report["duplicate_warnings"],
            )
            marker_types = {
                marker["type"] for marker in report["low_quality_source_markers"]
            }
            self.assertIn("source_review_blocker", marker_types)
            self.assertIn("private_source_in_public_benchmark", marker_types)
            self.assertIn("missing_expected_quote", marker_types)
            self.assertIn("unreviewed_source", marker_types)

    def test_eval_search_reports_duplicate_source_cards_without_private_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            duplicate_source_id = "src-111111111111"
            original_card = root / "sources" / f"{source_id}.md"
            duplicate_card = root / "sources" / f"{duplicate_source_id}.md"
            duplicate_card.write_text(
                original_card.read_text(encoding="utf-8").replace(
                    f"source_id: {source_id}",
                    f"source_id: {duplicate_source_id}",
                    1,
                ),
                encoding="utf-8",
            )
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "aurora quote",
                    "expected_source_ids": [source_id, duplicate_source_id],
                    "expected_quotes": [
                        "The aurora quote anchors local retrieval quality."
                    ],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            warnings = result["benchmark_report"]["duplicate_warnings"]
            self.assertIn(
                {
                    "type": "duplicate_source_card",
                    "field": "sha256",
                    "source_ids": [duplicate_source_id, source_id],
                },
                warnings,
            )
            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("raw/imports", serialized)

    def test_eval_search_rejects_secret_shaped_expected_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            sentinel = "".join(["s", "k", "-task22-secret-000000"])
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "aurora quote",
                    "expected_source_ids": [source_id],
                    "expected_quotes": [sentinel],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertEqual("failed", result["status"])
            self.assertEqual("secret_in_benchmark", result["classification"])
            self.assertNotIn(sentinel, serialized)

    def test_eval_search_rejects_short_sk_shapes_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "sk-a",
                    "expected_source_ids": [source_id],
                    "privacy": "public",
                },
            )
            client = FakeEmbeddingClient()

            result = eval_search(root, benchmark, client=client, env=configured_env())

            self.assertEqual("failed", result["status"])
            self.assertEqual("secret_in_benchmark", result["classification"])
            self.assertEqual([], client.calls)

    def test_low_quality_markers_include_short_and_unsupported_private_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "tiny.md"
            source.write_text("# Tiny\n\nTiny.\n", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "tiny",
                    "expected_source_ids": [source_id],
                    "expected_quotes": ["Tiny."],
                    "privacy": "sensitive",
                    "confirmed": True,
                },
            )

            result = eval_search(root, benchmark, env={})

            marker_types = {
                marker["type"]
                for marker in result["benchmark_report"]["low_quality_source_markers"]
            }
            self.assertIn("very_short_content", marker_types)
            self.assertIn("unreviewed_source", marker_types)
            self.assertIn("unsupported_privacy_confirmation", marker_types)

    def test_eval_search_reports_stale_raw_and_vector_hints_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            client = FakeEmbeddingClient()
            vector_rebuild(root, client=client, env=configured_env())
            source_card = root / "sources" / f"{source_id}.md"
            raw_path = root / read_source_card(source_card)["raw_path"]
            raw_path.write_text(
                "# Aurora Study\n\nChanged local raw bytes after indexing.\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(root / "db" / "kb.sqlite3")
            try:
                connection.execute(
                    "UPDATE chunk_vectors SET content = ? WHERE source_id = ?",
                    ("stale vector content", source_id),
                )
                connection.commit()
            finally:
                connection.close()
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "aurora quote",
                    "expected_source_ids": [source_id],
                    "privacy": "public",
                },
            )
            before = tree_snapshot(root)

            result = eval_search(
                root, benchmark, client=client, env=configured_env()
            )

            warning_types = {
                warning["type"]
                for warning in result["benchmark_report"]["stale_index_warnings"]
            }
            self.assertIn("raw_source_hash_mismatch", warning_types)
            self.assertIn("stale_vector_row", warning_types)
            self.assertEqual(before, tree_snapshot(root))

    def test_eval_search_reports_malformed_vector_index_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))
            database = root / "db" / "kb.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE IF EXISTS chunk_vectors")
                connection.execute(
                    "CREATE TABLE chunk_vectors (source_id TEXT, chunk_index INTEGER)"
                )
                connection.commit()
            finally:
                connection.close()
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            write_benchmark(
                benchmark,
                {
                    "query": "aurora quote",
                    "expected_source_ids": [source_id],
                    "privacy": "public",
                },
            )

            result = eval_search(root, benchmark, env={})

            self.assertIn(
                {"type": "malformed_vector_index", "table": "chunk_vectors"},
                result["benchmark_report"]["stale_index_warnings"],
            )

    def test_benchmark_add_supports_optional_expected_quotes_and_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _duplicate_id = create_quality_root(Path(tmpdir))

            result = add_benchmark_case(
                root,
                "aurora quote",
                [source_id],
                expected_quotes=[
                    "The aurora quote anchors local retrieval quality."
                ],
            )

            self.assertEqual("retrieval-benchmark", result["id"])
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            records = [
                json.loads(line)
                for line in benchmark.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                ["The aurora quote anchors local retrieval quality."],
                records[0]["expected_quotes"],
            )
            evaluation = eval_search(root, benchmark, env={})
            self.assertEqual(
                1.0,
                evaluation["benchmark_report"]["quote_support"]["hit_rate"],
            )


if __name__ == "__main__":
    unittest.main()
