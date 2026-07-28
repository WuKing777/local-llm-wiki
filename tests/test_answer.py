import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path

from kb.commands import answer, ingest_file, init_repository


class AnswerTests(unittest.TestCase):
    def test_answer_uses_matching_stable_wiki_before_source_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            raw = temp / "raw-source.md"
            raw.write_text(
                "# Raw Source\n\npersistent wiki raw fallback evidence",
                encoding="utf-8",
            )
            source_id = ingest_file(root, raw)["source_id"]
            (root / "wiki" / "curated.md").write_text(
                f"Persistent wiki curated answer evidence cites {source_id}.",
                encoding="utf-8",
            )

            result = answer(root, "persistent wiki")

            self.assertEqual("answered", result["status"])
            self.assertEqual("low", result["uncertainty"])
            self.assertIn("curated answer evidence", result["answer"])
            self.assertEqual("wiki", result["evidence"][0]["kind"])
            self.assertEqual("wiki/curated.md", result["evidence"][0]["path"])
            self.assertEqual([source_id], result["source_ids"])

    def test_answer_falls_back_to_source_chunks_when_wiki_has_no_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            raw = temp / "source.md"
            raw.write_text(
                "# Source\n\nsource chunk fallback answer evidence",
                encoding="utf-8",
            )
            source_id = ingest_file(root, raw)["source_id"]

            result = answer(root, "fallback answer")

            self.assertEqual("answered", result["status"])
            self.assertEqual("medium", result["uncertainty"])
            self.assertIn("source chunk fallback answer evidence", result["answer"])
            self.assertEqual("source", result["evidence"][0]["kind"])
            self.assertEqual(source_id, result["evidence"][0]["source_id"])
            self.assertIn("source chunk fallback answer evidence", result["evidence"][0]["quote"])

    def test_answer_without_evidence_is_unsupported_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            paths = [
                root / "meta" / "source-map.jsonl",
                root / "meta" / "log.md",
                root / "meta" / "review-queue.md",
                root / "db" / "kb.sqlite3",
            ]
            before = {path: path.read_bytes() for path in paths}

            result = answer(root, "missing evidence")

            self.assertEqual("unsupported", result["status"])
            self.assertEqual("high", result["uncertainty"])
            self.assertEqual([], result["source_ids"])
            self.assertEqual([], result["evidence"])
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_answer_missing_root_does_not_initialize_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"

            with self.assertRaisesRegex(RuntimeError, "Knowledge base is not initialized"):
                answer(root, "anything")

            self.assertFalse(root.exists())

    @unittest.skipIf(
        not hasattr(os, "symlink"), "symlink support is required for this test"
    )
    def test_answer_rejects_stable_wiki_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\ntrusted phrase", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            outside = temp / "outside.md"
            outside.write_text(
                f"outside secret answer phrase cites {source_id}.",
                encoding="utf-8",
            )
            link = root / "wiki" / "outside.md"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "Wiki page outside wiki"):
                answer(root, "outside secret")

    def test_cli_answer_outputs_sources_and_evidence_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            raw = temp / "source.md"
            raw.write_text("# Source\n\ncli answer evidence phrase", encoding="utf-8")
            source_id = ingest_file(root, raw)["source_id"]
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "answer",
                    "cli answer",
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("status: answered", completed.stdout)
            self.assertIn(source_id, completed.stdout)
            self.assertIn("cli answer evidence phrase", completed.stdout)
            self.assertNotIn("Traceback", completed.stderr)

    def test_cli_answer_without_evidence_refuses_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "answer",
                    "absent topic",
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("status: unsupported", completed.stdout)
            self.assertIn("No local evidence found.", completed.stdout)
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
