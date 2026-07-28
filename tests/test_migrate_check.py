import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kb.backup import create_backup
from kb.migrate import migrate_check
from kb.restore import restore_backup

from tests.test_backup import PROJECT_ROOT, create_root


class MigrateCheckTests(unittest.TestCase):
    def test_source_backup_restore_passes_hash_migration_check_and_cli_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            self.assertEqual("pass", create_backup(source, backup).status)
            restored = temp / "restored"
            self.assertEqual("pass", restore_backup(backup, restored).status)

            result = migrate_check(source, restored)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertEqual("migrate_check_passed", result.classification)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "migrate-check",
                    "--source",
                    str(source),
                    "--restored",
                    str(restored),
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("pass", json.loads(completed.stdout)["status"])

    def test_raw_source_wiki_or_meta_hash_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            restored = temp / "restored"
            restore_backup(backup, restored)
            (restored / "meta" / "review-queue.md").write_text(
                "# Review Queue\n\nchanged\n",
                encoding="utf-8",
            )

            result = migrate_check(source, restored)

            self.assertEqual("failed", result.status)
            self.assertEqual("hash_mismatch", result.classification)
            self.assertIn("meta/review-queue.md", result.to_dict()["details"]["mismatched"])

    def test_generated_reports_are_excluded_from_hash_comparison(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            restored = temp / "restored"
            shutil.copytree(source, restored)
            (source / "meta" / "quality-report.md").write_text("source timestamp\n", encoding="utf-8")
            (restored / "meta" / "quality-report.md").write_text("restored timestamp\n", encoding="utf-8")

            result = migrate_check(source, restored)

            self.assertNotEqual("hash_mismatch", result.classification, result.to_json())

    def test_vector_rebuild_missing_endpoint_is_not_reported_as_fake_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            restored = temp / "restored"
            restore_backup(backup, restored)

            with mock.patch.dict(
                os.environ,
                {"KB_MIGRATE_CHECK_VECTOR": "1", "KB_EMBEDDING_MODEL": "test-model"},
                clear=True,
            ):
                with mock.patch("kb.migrate.vector_rebuild", side_effect=RuntimeError("KB_EMBEDDING_BASE_URL missing")):
                    result = migrate_check(source, restored)

            self.assertEqual("failed", result.status)
            self.assertEqual("external_dependency_missing", result.classification)
            self.assertEqual("pass", result.to_dict()["details"]["hash_check"]["status"])


if __name__ == "__main__":
    unittest.main()
