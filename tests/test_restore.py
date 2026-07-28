import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kb.backup import create_backup
from kb.locks import acquire_write_lock
from kb.restore import FILE_ATTRIBUTE_REPARSE_POINT, restore_backup

from tests.test_backup import PROJECT_ROOT, create_root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_entries(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _write_backup(path: Path, entries: dict[str, bytes], *, extra: dict[str, bytes] | None = None) -> None:
    manifest = {
        "format_version": 1,
        "created_at": "2026-07-06T00:00:00+00:00",
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(entries.items())
        ],
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
        archive.writestr("backup-manifest.json", json.dumps(manifest, sort_keys=True))


class RestoreTests(unittest.TestCase):
    def test_restores_backup_to_new_root_and_cli_reports_product_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            self.assertEqual("pass", create_backup(source, backup).status)
            restored = temp / "restored"

            result = restore_backup(backup, restored)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertEqual("backup_restored", result.classification)
            self.assertTrue((restored / "wiki" / "grounded.md").is_file())
            self.assertTrue((restored / "db" / "kb.sqlite3").is_file())
            self.assertFalse(list(temp.glob(".restore.*")))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "restore",
                    "--backup",
                    str(backup),
                    "--root",
                    str(temp / "cli-restored"),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("pass", json.loads(completed.stdout)["status"])

    def test_non_empty_target_requires_explicit_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            (target / "keep.txt").write_text("original\n", encoding="utf-8")
            before_entries = _tree_entries(target)
            before_bytes = _tree_bytes(target)

            result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("target_not_empty", result.classification)
            self.assertEqual(before_entries, _tree_entries(target))
            self.assertEqual(before_bytes, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_replace_restore_preserves_original_when_post_restore_check_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            (target / "keep.txt").write_text("original\n", encoding="utf-8")
            original = _tree_bytes(target)

            with mock.patch("kb.restore.lint_repository", side_effect=RuntimeError("lint exploded")):
                result = restore_backup(backup, target, replace=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("lint_failed", result.classification)
            self.assertEqual(original, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_replace_restore_with_partial_final_swap_failure_restores_original(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            (target / "keep.txt").write_text("original\n", encoding="utf-8")
            original = _tree_bytes(target)
            original_replace = Path.replace

            def fail_staging_replace(self: Path, target_path: Path):
                if any(part.startswith(".restore.") for part in self.parts):
                    target_path.mkdir()
                    raise OSError("injected final swap failure")
                return original_replace(self, target_path)

            with mock.patch("pathlib.Path.replace", new=fail_staging_replace):
                result = restore_backup(backup, target, replace=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("atomic_swap_failure", result.classification)
            self.assertEqual(original, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_rollback_preparation_failure_preserves_original_target_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            (target / "keep.txt").write_text("original\n", encoding="utf-8")
            (target / "nested").mkdir()
            (target / "nested" / "keep.md").write_text("nested original\n", encoding="utf-8")
            original_entries = _tree_entries(target)
            original_bytes = _tree_bytes(target)

            with mock.patch(
                "kb.restore._move_existing_content_to_rollback",
                side_effect=OSError("rollback prep failed before rollback exists"),
            ):
                result = restore_backup(backup, target, replace=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("atomic_swap_failure", result.classification)
            self.assertEqual(original_entries, _tree_entries(target))
            self.assertEqual(original_bytes, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_final_root_swap_runs_inside_restore_write_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            original_replace = Path.replace
            state = {"active": False, "staging_swap_active": None}

            class FakeRestoreLock:
                def __enter__(self):
                    (target / "meta").mkdir(parents=True, exist_ok=True)
                    state["active"] = True
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    state["active"] = False
                    return False

                def release(self):
                    state["active"] = False

            def observe_replace(self: Path, target_path: Path):
                if any(part.startswith(".restore.") for part in self.parts):
                    state["staging_swap_active"] = state["active"]
                return original_replace(self, target_path)

            with (
                mock.patch("kb.restore.acquire_write_lock", return_value=FakeRestoreLock()),
                mock.patch("kb.restore._post_restore_checks", return_value=None),
                mock.patch("pathlib.Path.replace", new=observe_replace),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertIs(state["staging_swap_active"], True)

    def test_post_restore_checks_run_inside_restore_write_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            state = {"active": False, "post_checks_active": None}

            class FakeRestoreLock:
                def __enter__(self):
                    (target / "meta").mkdir(parents=True, exist_ok=True)
                    state["active"] = True
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    state["active"] = False
                    return False

                def release(self):
                    state["active"] = False

            def observe_post_restore_checks(root: Path):
                state["post_checks_active"] = state["active"]
                return None

            with (
                mock.patch("kb.restore.acquire_write_lock", return_value=FakeRestoreLock()),
                mock.patch("kb.restore._post_restore_checks", side_effect=observe_post_restore_checks),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertIs(state["post_checks_active"], True)

    def test_real_restore_lock_file_remains_during_swap_and_post_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            lock_path = target / "meta" / ".kb-write.lock"
            original_replace = Path.replace
            state = {"swap_lock_present": None, "post_checks_lock_present": None}

            def observe_replace(self: Path, target_path: Path):
                if any(part.startswith(".restore.") for part in self.parts):
                    state["swap_lock_present"] = lock_path.exists()
                return original_replace(self, target_path)

            def observe_post_restore_checks(root: Path):
                state["post_checks_lock_present"] = lock_path.exists()
                return None

            with (
                mock.patch("pathlib.Path.replace", new=observe_replace),
                mock.patch("kb.restore._post_restore_checks", side_effect=observe_post_restore_checks),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertIs(state["swap_lock_present"], True)
            self.assertIs(state["post_checks_lock_present"], True)

    def test_final_swap_failure_preserves_missing_and_existing_empty_targets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            original_replace = Path.replace

            def fail_staging_replace(self: Path, target_path: Path):
                if any(part.startswith(".restore.") for part in self.parts):
                    target_path.mkdir(exist_ok=True)
                    raise OSError("injected final swap failure")
                return original_replace(self, target_path)

            for existing in (False, True):
                with self.subTest(existing=existing):
                    target = temp / ("existing-empty" if existing else "missing")
                    if existing:
                        target.mkdir()
                    before_exists = target.exists()
                    before_entries = _tree_entries(target)
                    before_bytes = _tree_bytes(target)

                    with mock.patch("pathlib.Path.replace", new=fail_staging_replace):
                        result = restore_backup(backup, target)

                    self.assertEqual("failed", result.status)
                    self.assertEqual("atomic_swap_failure", result.classification)
                    self.assertEqual(before_exists, target.exists())
                    self.assertEqual(before_entries, _tree_entries(target))
                    self.assertEqual(before_bytes, _tree_bytes(target))
                    self.assertFalse(list(temp.glob(".restore.*")))
                    self.assertFalse(list(temp.glob(".rollback.*")))

    def test_active_write_lock_creates_no_staging_and_leaves_target_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "locked"

            target_meta = target / "meta"
            target_meta.mkdir(parents=True)
            with acquire_write_lock(target, operation="outer"):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("write_lock_active", result.classification)
            self.assertEqual(["meta"], sorted(path.name for path in target.iterdir()))
            self.assertFalse(list(temp.glob(".restore.*")))

    def test_rejects_unsafe_archive_entries_before_creating_target(self):
        unsafe_names = [
            "../escape.md",
            "/absolute.md",
            "C:drive.md",
            "raw/file:stream.md",
            "raw/CON.md",
        ]
        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    temp = Path(tmpdir)
                    backup = temp / "unsafe.zip"
                    _write_backup(backup, {unsafe_name: b"unsafe\n"})
                    target = temp / "target"

                    result = restore_backup(backup, target)

                    self.assertEqual("failed", result.status)
                    self.assertFalse(target.exists())

    def test_rejects_manifest_extra_entries_symlink_metadata_and_secret_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            extra = temp / "extra.zip"
            _write_backup(extra, {"raw/source.md": b"clean\n"}, extra={"raw/extra.md": b"extra\n"})
            self.assertEqual("manifest_mismatch", restore_backup(extra, temp / "extra-root").classification)

            symlink = temp / "symlink.zip"
            info = zipfile.ZipInfo("raw/link.md")
            info.external_attr = (0o120777 << 16)
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [{"path": "raw/link.md", "sha256": hashlib.sha256(b"x").hexdigest()}],
            }
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(info, b"x")
                archive.writestr("backup-manifest.json", json.dumps(manifest))
            self.assertEqual("symlink_escape", restore_backup(symlink, temp / "symlink-root").classification)

            secret = temp / "secret.zip"
            sentinel = ("s" + "k-" + "restore-runtime-sentinel").encode("utf-8")
            _write_backup(secret, {"raw/secret.md": b"credential " + sentinel + b"\n"})
            self.assertEqual(
                "secret_in_backup_candidate",
                restore_backup(secret, temp / "secret-root").classification,
            )

            reparse = temp / "reparse.zip"
            info = zipfile.ZipInfo("raw/reparse.md")
            info.external_attr = FILE_ATTRIBUTE_REPARSE_POINT
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [{"path": "raw/reparse.md", "sha256": hashlib.sha256(b"x").hexdigest()}],
            }
            with zipfile.ZipFile(reparse, "w") as archive:
                archive.writestr(info, b"x")
                archive.writestr("backup-manifest.json", json.dumps(manifest))
            self.assertEqual(
                "junction_or_reparse_point",
                restore_backup(reparse, temp / "reparse-root").classification,
            )

    def test_missing_or_tampered_manifest_returns_classification_and_leaves_target_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            missing_manifest = temp / "missing-manifest.zip"
            with zipfile.ZipFile(missing_manifest, "w") as archive:
                archive.writestr("raw/source.md", b"clean\n")

            missing = restore_backup(missing_manifest, temp / "missing-root")

            self.assertEqual("failed", missing.status)
            self.assertEqual("manifest_invalid", missing.classification)
            self.assertFalse((temp / "missing-root").exists())

            tampered = temp / "tampered.zip"
            _write_backup(tampered, {"raw/source.md": b"clean\n"})
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [{"path": "raw/source.md", "sha256": "0" * 64}],
            }
            with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("raw/source.md", b"clean\n")
                archive.writestr("backup-manifest.json", json.dumps(manifest))

            mismatch = restore_backup(tampered, temp / "tampered-root")

            self.assertEqual("failed", mismatch.status)
            self.assertEqual("hash_mismatch", mismatch.classification)
            self.assertFalse((temp / "tampered-root").exists())

    def test_staging_write_failure_removes_staging_and_keeps_target_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            backup = temp / "backup.zip"
            _write_backup(backup, {"raw/source.md": b"clean\n"})
            target = temp / "target"

            with mock.patch("kb.restore._write_restored_file", side_effect=OSError("disk full")):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("staging_write_failure", result.classification)
            self.assertFalse(target.exists())
            self.assertFalse(list(temp.glob(".restore.*")))

    def test_staging_failure_does_not_manually_release_restore_lock_before_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            backup = temp / "backup.zip"
            _write_backup(backup, {"raw/source.md": b"clean\n"})
            target = temp / "target"
            lock_path = target / "meta" / ".kb-write.lock"
            state = {
                "release_called_before_exit": False,
                "staging_cleanup_lock_present": None,
            }
            original_remove_created_tree = __import__("kb.restore", fromlist=["_remove_created_tree"])._remove_created_tree

            class FakeRestoreLock:
                def __enter__(self):
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    lock_path.write_text("restore lock\n", encoding="utf-8")
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    if lock_path.exists():
                        lock_path.unlink()
                    return False

                def release(self):
                    state["release_called_before_exit"] = True
                    if lock_path.exists():
                        lock_path.unlink()

            def observe_remove_created_tree(path: Path, parent: Path):
                if path.name.startswith(".restore."):
                    state["staging_cleanup_lock_present"] = lock_path.exists()
                return original_remove_created_tree(path, parent)

            with (
                mock.patch("kb.restore.acquire_write_lock", return_value=FakeRestoreLock()),
                mock.patch("kb.restore._write_restored_file", side_effect=OSError("disk full")),
                mock.patch("kb.restore._remove_created_tree", side_effect=observe_remove_created_tree),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("staging_write_failure", result.classification)
            self.assertIs(state["staging_cleanup_lock_present"], True)
            self.assertIs(state["release_called_before_exit"], False)

    def test_staging_write_failure_preserves_existing_empty_target_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            backup = temp / "backup.zip"
            _write_backup(backup, {"raw/source.md": b"clean\n"})
            target = temp / "target"
            target.mkdir()
            before_entries = _tree_entries(target)
            before_bytes = _tree_bytes(target)

            with mock.patch("kb.restore._write_restored_file", side_effect=OSError("disk full")):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("staging_write_failure", result.classification)
            self.assertEqual(before_entries, _tree_entries(target))
            self.assertEqual(before_bytes, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))

    def test_final_swap_failure_does_not_remove_lock_scaffold_during_restore_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            resolved_target = target.resolve(strict=False)
            lock_path = target / "meta" / ".kb-write.lock"
            original_replace = Path.replace
            original_remove_created_tree = __import__("kb.restore", fromlist=["_remove_created_tree"])._remove_created_tree
            state = {"removed_root_while_lock_present": False}

            def fail_staging_replace(self: Path, target_path: Path):
                if any(part.startswith(".restore.") for part in self.parts):
                    raise OSError("injected final swap failure")
                return original_replace(self, target_path)

            def observe_remove_created_tree(path: Path, parent: Path):
                if path.resolve(strict=False) == resolved_target and lock_path.exists():
                    state["removed_root_while_lock_present"] = True
                return original_remove_created_tree(path, parent)

            with (
                mock.patch("pathlib.Path.replace", new=fail_staging_replace),
                mock.patch("kb.restore._remove_created_tree", side_effect=observe_remove_created_tree),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("atomic_swap_failure", result.classification)
            self.assertIs(state["removed_root_while_lock_present"], False)
            self.assertFalse(target.exists())
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_post_restore_failure_preserves_existing_empty_target_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            before_entries = _tree_entries(target)
            before_bytes = _tree_bytes(target)

            with mock.patch("kb.restore.lint_repository", side_effect=RuntimeError("lint exploded")):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("lint_failed", result.classification)
            self.assertEqual(before_entries, _tree_entries(target))
            self.assertEqual(before_bytes, _tree_bytes(target))
            self.assertFalse(list(temp.glob(".restore.*")))

    def test_post_restore_failure_does_not_remove_lock_scaffold_during_restore_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            resolved_target = target.resolve(strict=False)
            lock_path = target / "meta" / ".kb-write.lock"
            original_remove_created_tree = __import__("kb.restore", fromlist=["_remove_created_tree"])._remove_created_tree
            state = {"removed_root_while_lock_present": False}

            def observe_remove_created_tree(path: Path, parent: Path):
                if path.resolve(strict=False) == resolved_target and lock_path.exists():
                    state["removed_root_while_lock_present"] = True
                return original_remove_created_tree(path, parent)

            with (
                mock.patch("kb.restore.lint_repository", side_effect=RuntimeError("lint exploded")),
                mock.patch("kb.restore._remove_created_tree", side_effect=observe_remove_created_tree),
            ):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("lint_failed", result.classification)
            self.assertIs(state["removed_root_while_lock_present"], False)
            self.assertFalse(target.exists())
            self.assertFalse(list(temp.glob(".restore.*")))
            self.assertFalse(list(temp.glob(".rollback.*")))

    def test_cross_parent_staging_attempt_is_rejected_before_target_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"

            with mock.patch("kb.restore._same_parent", return_value=False):
                result = restore_backup(backup, target)

            self.assertEqual("failed", result.status)
            self.assertEqual("cross_volume_atomicity_unsupported", result.classification)
            self.assertFalse(target.exists())
            self.assertFalse(list(temp.glob(".restore.*")))

    def test_replace_restore_succeeds_and_removes_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            source, _source_id = create_root(temp, "source")
            backup = temp / "backup.zip"
            create_backup(source, backup)
            target = temp / "target"
            target.mkdir()
            (target / "old.txt").write_text("old\n", encoding="utf-8")

            result = restore_backup(backup, target, replace=True)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertFalse((target / "old.txt").exists())
            self.assertTrue((target / "wiki" / "grounded.md").is_file())
            self.assertFalse(list(temp.glob(".rollback.*")))

    def tearDown(self) -> None:
        for temp in Path(tempfile.gettempdir()).glob(".restore.test.cleanup.*"):
            if temp.is_dir():
                shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
