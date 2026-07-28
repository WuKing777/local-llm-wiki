import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from kb.commands import init_repository
from kb.schema import initialize_database


REQUIRED_DIRECTORIES = ("raw", "inbox", "wiki", "sources", "meta", "db")
REQUIRED_METADATA_FILES = (
    "index.md",
    "log.md",
    "source-map.jsonl",
    "review-queue.md",
    "quality-report.md",
)
REQUIRED_TABLES = {"documents", "chunks", "events", "chunk_fts"}


class InitRepositoryTests(unittest.TestCase):
    def test_init_creates_directories_metadata_and_database_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"

            result = init_repository(root)

            self.assertEqual(Path(result["root"]), root.resolve())
            for directory in REQUIRED_DIRECTORIES:
                self.assertTrue((root / directory).is_dir(), directory)
            for filename in REQUIRED_METADATA_FILES:
                self.assertTrue((root / "meta" / filename).is_file(), filename)

            database = root / "db" / "kb.sqlite3"
            self.assertTrue(database.is_file())
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
                    )
                }
            self.assertTrue(REQUIRED_TABLES.issubset(tables))

    def test_init_is_idempotent_and_preserves_existing_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            meta = root / "meta"
            meta.mkdir(parents=True)
            log = meta / "log.md"
            log.write_text("existing audit trail\n", encoding="utf-8")

            first = init_repository(root)
            second = init_repository(root)

            self.assertIn(str((root / "raw").resolve()), first["created_dirs"])
            self.assertEqual([], second["created_dirs"])
            self.assertEqual([], second["created_files"])
            self.assertEqual("existing audit trail\n", log.read_text(encoding="utf-8"))

    def test_cli_init_creates_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue((root / "db" / "kb.sqlite3").is_file())
            self.assertTrue((root / "meta" / "source-map.jsonl").is_file())

    def test_cli_init_reports_runtime_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.mkdir()
            (root / "raw").write_text("not a directory\n", encoding="utf-8")
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("error:", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_cli_init_rejects_root_path_that_is_file_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.write_text("not a directory\n", encoding="utf-8")
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertNotIn("Traceback", completed.stderr)

    def test_init_rejects_root_path_that_is_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.write_text("not a directory\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Expected root directory"):
                init_repository(root)

    def test_init_rejects_directory_name_collision_with_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.mkdir()
            (root / "raw").write_text("not a directory\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Expected directory"):
                init_repository(root)

    def test_init_rejects_metadata_name_collision_with_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            (root / "meta" / "log.md").mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "Expected metadata file"):
                init_repository(root)

    def test_init_rejects_database_path_that_is_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            (root / "db" / "kb.sqlite3").mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "Expected database file"):
                init_repository(root)

    def test_cli_init_rejects_database_path_that_is_directory_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            (root / "db" / "kb.sqlite3").mkdir(parents=True)
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertNotIn("Traceback", completed.stderr)

    def test_init_rejects_canonical_directory_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.mkdir()
            shared = Path(tmpdir) / "shared"
            shared.mkdir()
            try:
                (root / "raw").symlink_to(shared, target_is_directory=True)
                (root / "inbox").symlink_to(shared, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "Canonical path collision"):
                init_repository(root)

    def test_init_rejects_canonical_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            root.mkdir()
            external = Path(tmpdir) / "external-raw"
            external.mkdir()
            try:
                (root / "raw").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "inside root"):
                init_repository(root)

    def test_initialize_database_requires_fts5(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "kb.sqlite3"

            class FakeConnection:
                def execute(self, sql):
                    if "fts5" in sql.lower():
                        raise sqlite3.OperationalError("no such module: fts5")

                def executescript(self, _sql):
                    return None

                def commit(self):
                    return None

                def close(self):
                    return None

            with patch("kb.schema.sqlite3.connect", return_value=FakeConnection()):

                with self.assertRaisesRegex(RuntimeError, "SQLite FTS5 is required"):
                    initialize_database(database)


if __name__ == "__main__":
    unittest.main()
