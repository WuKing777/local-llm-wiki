import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import ingest_file, init_repository, schema_check_repository
from kb.eval_search import eval_search
from kb.locks import acquire_write_lock
from kb.retrieval_benchmark import add_benchmark_case


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    if not root.exists():
        return []
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        elif path.is_symlink():
            snapshot.append((relative, "symlink", os.readlink(path).encode()))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def read_records(root: Path) -> list[dict[str, object]]:
    benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
    return [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def create_search_root(temp: Path) -> tuple[Path, str]:
    root = temp / "kb"
    init_repository(root)
    source = temp / "astronomy.md"
    source.write_text(
        "# Astronomy\n\nA galaxy contains stars and nebula retrieval facts.\n",
        encoding="utf-8",
    )
    source_id = ingest_file(root, source)["source_id"]
    (root / "wiki" / "astronomy.md").write_text(
        f"# Astronomy\n\nGalaxy retrieval facts are covered by {source_id}.\n",
        encoding="utf-8",
    )
    return root, source_id


def _sk(value: str = "") -> str:
    return "sk" + f"-{value}"


def _bearer(value: str) -> str:
    return "Bear" + "er " + value


def _authorization_bearer(value: str) -> str:
    return "Authorization: " + _bearer(value)


def _private_key_marker() -> str:
    return "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def _secret_field(name: str) -> str:
    return "query " + name + "=abc123"


class RetrievalBenchmarkWorkflowTests(unittest.TestCase):
    def test_api_appends_eval_search_compatible_record_and_fts_smoke_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            result = add_benchmark_case(
                root,
                "galaxy nebula",
                [source_id],
                expected_wiki_paths=["wiki/astronomy.md"],
            )

            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            self.assertEqual(benchmark.resolve(), Path(str(result["path"])).resolve())
            self.assertEqual("retrieval-benchmark", result["id"])
            self.assertEqual(
                [
                    {
                        "expected_source_ids": [source_id],
                        "expected_wiki_paths": ["wiki/astronomy.md"],
                        "privacy": "public",
                        "query": "galaxy nebula",
                    }
                ],
                read_records(root),
            )

            evaluation = eval_search(root, None, env={})
            self.assertEqual("pass", evaluation["status"])
            self.assertEqual("pass", evaluation["classification"])

    def test_commands_wrapper_requires_initialized_repository_before_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"

            before = tree_snapshot(root)
            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                from kb.commands import benchmark_add

                benchmark_add(root, query="galaxy", expected_source_ids=["src-000000000000"])

            self.assertEqual(before, tree_snapshot(root))

    def test_api_requires_initialized_repository_before_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"

            before = tree_snapshot(root)
            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                add_benchmark_case(root, "galaxy", ["src-000000000000"])

            self.assertEqual(before, tree_snapshot(root))

    def test_active_write_lock_blocks_wrapper_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            before = tree_snapshot(root)

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaises(Exception):
                    from kb.commands import benchmark_add

                    benchmark_add(root, query="galaxy", expected_source_ids=[source_id])

            self.assertEqual(before, tree_snapshot(root))
            self.assertFalse((root / "meta" / "evals").exists())

    def test_active_write_lock_blocks_direct_api_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            before = tree_snapshot(root)

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaises(Exception):
                    add_benchmark_case(root, "galaxy", [source_id])

            self.assertEqual(before, tree_snapshot(root))
            self.assertFalse((root / "meta" / "evals").exists())

    def test_rejects_unsafe_benchmark_storage_shapes_without_partial_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            bad_file_dir = root / "meta" / "evals"
            bad_file_dir.write_text("not a directory", encoding="utf-8")
            before = tree_snapshot(root)
            with self.assertRaisesRegex(RuntimeError, "Benchmark"):
                add_benchmark_case(root, "galaxy", [source_id])
            self.assertEqual(before, tree_snapshot(root))

        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, source_id = create_search_root(temp)
            outside = temp / "outside"
            outside.mkdir()
            evals = root / "meta" / "evals"
            try:
                os.symlink(outside, evals, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            before = tree_snapshot(root)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                add_benchmark_case(root, "galaxy", [source_id])
            self.assertEqual(before, tree_snapshot(root))

    def test_rejects_invalid_source_ids_and_missing_source_cards_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_search_root(Path(tmpdir))

            for expected_sources in (["src-notvalid"], ["src-000000000000"]):
                with self.subTest(expected_sources=expected_sources):
                    before = tree_snapshot(root)
                    with self.assertRaisesRegex(RuntimeError, "source"):
                        add_benchmark_case(root, "galaxy", expected_sources)
                    self.assertEqual(before, tree_snapshot(root))

    def test_rejects_unsafe_wiki_paths_without_writing(self):
        unsafe_paths = [
            "/wiki/astronomy.md",
            "wiki/../sources/src-000000000000.md",
            "wiki//astronomy.md",
            r"wiki\..\sources\src-000000000000.md",
            "wiki/_drafts/draft.md",
            "raw/file.md",
            "sources/src-000000000000.md",
            "meta/evals/retrieval-benchmark.jsonl",
            "db/kb.sqlite3",
            "wiki/missing.md",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            for wiki_path in unsafe_paths:
                with self.subTest(wiki_path=wiki_path):
                    before = tree_snapshot(root)
                    with self.assertRaisesRegex(RuntimeError, "wiki path"):
                        add_benchmark_case(
                            root,
                            "galaxy",
                            [source_id],
                            expected_wiki_paths=[wiki_path],
                        )
                    self.assertEqual(before, tree_snapshot(root))

    def test_rejects_empty_secret_shaped_and_unconfirmed_private_queries_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            secret_env = {
                "KB_LLM_API_KEY": "llm-secret-value",
                "KB_EMBEDDING_API_KEY": "embedding-secret-value",
            }
            cases = [
                ("", "public", False),
                (_sk(), "public", False),
                (_sk("a"), "public", False),
                (_sk("short"), "public", False),
                (_sk("test-secret-000000"), "public", False),
                (_authorization_bearer("token.example.value"), "public", False),
                (_bearer("token.example.value"), "public", False),
                (_private_key_marker(), "public", False),
                (_secret_field("api" + "_key"), "public", False),
                (_secret_field("pass" + "word"), "public", False),
                (_secret_field("to" + "ken"), "public", False),
                ("uses llm-secret-value", "public", False),
                ("uses embedding-secret-value", "public", False),
                ("private galaxy", "sensitive", False),
                ("restricted galaxy", "restricted", False),
            ]

            for query, privacy, confirmed in cases:
                with self.subTest(query=query, privacy=privacy):
                    before = tree_snapshot(root)
                    with self.assertRaises(RuntimeError):
                        add_benchmark_case(
                            root,
                            query,
                            [source_id],
                            privacy=privacy,
                            confirmed=confirmed,
                            env=secret_env,
                        )
                    self.assertEqual(before, tree_snapshot(root))

    def test_confirmed_private_record_adds_confirmation_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            add_benchmark_case(
                root,
                "personal galaxy",
                [source_id],
                privacy="sensitive",
                confirmed=True,
            )

            self.assertEqual(
                {
                    "expected_source_ids": [source_id],
                    "privacy": "sensitive",
                    "confirmed": True,
                    "query": "personal galaxy",
                },
                read_records(root)[0],
            )

    def test_generated_record_passes_schema_check_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            add_benchmark_case(
                root,
                "galaxy nebula",
                [source_id],
            )

            result = schema_check_repository(root)
            self.assertEqual("pass", result.status)
            self.assertEqual("schema_ok", result.classification)

    def test_replace_revalidates_storage_and_preserves_existing_file_on_swap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            add_benchmark_case(root, "galaxy", [source_id])
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            before_bytes = benchmark.read_bytes()

            with mock.patch(
                "kb.retrieval_benchmark._validate_benchmark_storage_for_write",
                side_effect=[
                    benchmark,
                    benchmark,
                    RuntimeError("Benchmark path swapped"),
                ],
            ), mock.patch("kb.retrieval_benchmark.os.replace") as replace:
                with self.assertRaisesRegex(RuntimeError, "Benchmark path swapped"):
                    add_benchmark_case(root, "nebula", [source_id])

            replace.assert_not_called()
            self.assertEqual(before_bytes, benchmark.read_bytes())
            self.assertEqual(
                [],
                [
                    path.name
                    for path in (root / "meta" / "evals").iterdir()
                    if path.name.endswith(".tmp")
                ],
            )

    def test_cli_json_and_plain_output_are_redacted_and_do_not_leak_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "benchmark-add",
                        "--root",
                        str(root),
                        "--query",
                        "galaxy nebula",
                        "--expected-source-id",
                        source_id,
                        "--expected-wiki-path",
                        "wiki/astronomy.md",
                        "--json",
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual("retrieval-benchmark", data["id"])
            self.assertEqual(
                "meta/evals/retrieval-benchmark.jsonl", data["benchmark"]
            )
            self.assertNotIn("galaxy nebula", stdout.getvalue())

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "benchmark-add",
                        "--root",
                        str(root),
                        "--query",
                        "another galaxy",
                        "--expected-source-id",
                        source_id,
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("", stderr.getvalue())
            self.assertIn("meta/evals/retrieval-benchmark.jsonl", stdout.getvalue())
            self.assertNotIn("another galaxy", stdout.getvalue())

    def test_failed_append_preserves_existing_jsonl_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))
            add_benchmark_case(root, "galaxy", [source_id])
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            before_bytes = benchmark.read_bytes()

            with self.assertRaises(RuntimeError):
                add_benchmark_case(root, _sk("test-secret-000000"), [source_id])

            self.assertEqual(before_bytes, benchmark.read_bytes())
            self.assertEqual(
                [],
                [
                    path.name
                    for path in (root / "meta" / "evals").iterdir()
                    if path.name.endswith(".tmp")
                ],
            )


    def test_first_append_replace_failure_removes_new_eval_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_search_root(Path(tmpdir))

            with mock.patch(
                "kb.retrieval_benchmark.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    add_benchmark_case(root, "galaxy", [source_id])

            self.assertFalse((root / "meta" / "evals").exists())


if __name__ == "__main__":
    unittest.main()
