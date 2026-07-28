import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kb.commands import init_repository, lint_repository, rebuild_index, refresh_source
from kb.self_statement import create_self_statement
from kb.sources import read_source_card, source_id_and_sha256


class SelfStatementTests(unittest.TestCase):
    def test_self_statement_creates_raw_source_card_and_source_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = create_self_statement(
                root,
                text="我希望把这个知识库作为外脑使用。",
                event_date="2026-07-01",
                privacy="personal",
                confidence="confirmed",
                input_method="chat",
            )

            raw_path = root / result["raw_path"]
            source_path = root / "sources" / f"{result['source_id']}.md"
            self.assertTrue(raw_path.is_file())
            self.assertTrue(source_path.is_file())
            raw_text = raw_path.read_text(encoding="utf-8")
            self.assertIn("我希望把这个知识库作为外脑使用。", raw_text)
            self.assertNotIn(result["source_id"], raw_text)
            metadata = read_source_card(source_path)
            expected_source_id, expected_sha256 = source_id_and_sha256(raw_path.read_bytes())
            self.assertEqual(expected_source_id, metadata["source_id"])
            self.assertEqual(expected_sha256, metadata["sha256"])
            self.assertEqual("self_statement", metadata["source_type"])
            self.assertEqual("personal", metadata["privacy"])
            self.assertEqual("confirmed", metadata["confidence"])
            source_map = (root / "meta" / "source-map.jsonl").read_text(encoding="utf-8")
            entry = json.loads(source_map.strip())
            self.assertEqual(result["source_id"], entry["source_id"])
            self.assertEqual("self_statement", entry["source_type"])
            self.assertEqual([], list((root / "wiki").glob("*.md")))

    def test_uncertain_statement_writes_review_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = create_self_statement(
                root,
                text="我可能更适合早上做深度工作。",
                event_date="2026-07-01",
                privacy="personal",
                confidence="uncertain",
                input_method="chat",
            )

            review = (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
            self.assertIn(result["source_id"], review)
            self.assertIn("confidence=uncertain", review)

    def test_self_statement_source_is_compatible_with_index_refresh_and_lint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = create_self_statement(
                root,
                text="我希望把这个知识库作为外脑使用。",
                event_date="2026-07-01",
                privacy="personal",
                confidence="confirmed",
                input_method="chat",
            )

            self.assertEqual([], lint_repository(root))
            rebuilt = rebuild_index(root)
            self.assertEqual(1, rebuilt["sources"])
            refreshed = refresh_source(root, result["source_id"])
            self.assertEqual(result["source_id"], refreshed["source_id"])
            self.assertFalse(refreshed["changed"])
            metadata = read_source_card(root / "sources" / f"{result['source_id']}.md")
            self.assertEqual("self_statement", metadata["source_type"])
            self.assertEqual("personal", metadata["privacy"])

    def test_invalid_privacy_is_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with self.assertRaisesRegex(RuntimeError, "Invalid privacy"):
                create_self_statement(
                    root,
                    text="test",
                    event_date="2026-07-01",
                    privacy="private",
                    confidence="confirmed",
                    input_method="chat",
                )

            self.assertEqual([], list((root / "raw").rglob("*.md")))
            self.assertEqual([], list((root / "sources").glob("src-*.md")))
            self.assertEqual("", (root / "meta" / "source-map.jsonl").read_text(encoding="utf-8"))

    def test_missing_root_does_not_create_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"

            with self.assertRaisesRegex(RuntimeError, "root is not initialized"):
                create_self_statement(
                    root,
                    text="test",
                    event_date="2026-07-01",
                    privacy="personal",
                    confidence="confirmed",
                    input_method="chat",
                )

            self.assertFalse(root.exists())

    def test_suspected_secret_is_blocked_without_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            secret = "s" + "k-" + "1234567890abcdef1234567890abcdef"

            with self.assertRaisesRegex(RuntimeError, "suspected secret"):
                create_self_statement(
                    root,
                    text=f"DeepSeek key {secret}",
                    event_date="2026-07-01",
                    privacy="restricted",
                    confidence="confirmed",
                    input_method="chat",
                )

            all_text = "\n".join(
                p.read_text(encoding="utf-8")
                for p in root.rglob("*")
                if p.is_file() and p.suffix in {".md", ".jsonl", ".txt"}
            )
            self.assertFalse(secret in all_text, "secret self-statement text leaked")

    def test_cli_allow_secret_is_not_supported_and_persists_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]
            secret = "s" + "k-" + "1234567890abcdef1234567890abcdef"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "self-statement",
                    "--root",
                    str(root),
                    "--text",
                    f"secret {secret}",
                    "--event-date",
                    "2026-07-01",
                    "--privacy",
                    "restricted",
                    "--confidence",
                    "confirmed",
                    "--input-method",
                    "chat",
                    "--allow-secret",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(2, completed.returncode)
            self.assertEqual([], list((root / "raw").rglob("*.md")))
            self.assertEqual([], list((root / "sources").glob("src-*.md")))

    def test_failure_rolls_back_raw_directories_source_card_map_log_and_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            log_before = (root / "meta" / "log.md").read_text(encoding="utf-8")
            db_before = (root / "db" / "kb.sqlite3").read_bytes()

            with mock.patch(
                "kb.commands._index_source", side_effect=RuntimeError("index boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "index boom"):
                    create_self_statement(
                        root,
                        text="我希望把这个知识库作为外脑使用。",
                        event_date="2026-07-01",
                        privacy="personal",
                        confidence="confirmed",
                        input_method="chat",
                    )

            self.assertFalse((root / "raw" / "self-statements").exists())
            self.assertEqual([], list((root / "sources").glob("src-*.md")))
            self.assertEqual("", (root / "meta" / "source-map.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(log_before, (root / "meta" / "log.md").read_text(encoding="utf-8"))
            self.assertEqual(db_before, (root / "db" / "kb.sqlite3").read_bytes())

    def test_self_statement_rejects_canonical_directory_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            external = base / "external-sources"
            init_repository(root)
            external.mkdir()
            shutil.rmtree(root / "sources")
            try:
                os.symlink(external, root / "sources", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "root is not initialized"):
                create_self_statement(
                    root,
                    text="我希望把这个知识库作为外脑使用。",
                    event_date="2026-07-01",
                    privacy="personal",
                    confidence="confirmed",
                    input_method="chat",
                )

            self.assertEqual([], list(external.iterdir()))

    def test_cli_self_statement_outputs_source_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "self-statement",
                    "--root",
                    str(root),
                    "--text",
                    "我希望把这个知识库作为外脑使用。",
                    "--event-date",
                    "2026-07-01",
                    "--privacy",
                    "personal",
                    "--confidence",
                    "confirmed",
                    "--input-method",
                    "chat",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertRegex(completed.stdout, r"^src-[0-9a-f]{12}\n$")
            self.assertEqual("", completed.stderr)


if __name__ == "__main__":
    unittest.main()
