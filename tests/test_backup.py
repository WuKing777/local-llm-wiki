import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kb.backup import create_backup, validate_backup_manifest
from kb.commands import ingest_file, init_repository
from kb.locks import acquire_write_lock
from kb.product_result import ProductResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def create_root(temp: Path, root_name: str = "kb") -> tuple[Path, str]:
    root = temp / root_name
    init_repository(root)
    source = temp / "source.md"
    source.write_text(
        "# Source\n\nBackup evidence sentence supports the stable page.",
        encoding="utf-8",
    )
    metadata = ingest_file(root, source)
    source_id = metadata["source_id"]
    (root / "wiki" / "grounded.md").write_text(
        f"# Grounded\n\nBackup evidence sentence supports the stable page {source_id}.",
        encoding="utf-8",
    )
    (root / "docs" / "reviews").mkdir(parents=True)
    (root / "docs" / "reviews" / "review-note.md").write_text(
        "Durable review note without secrets.\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("Instance README.\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "export.ps1").write_text(
        "Write-Output 'local helper'\n",
        encoding="utf-8",
    )
    obsidian = root / ".obsidian"
    obsidian.mkdir()
    (obsidian / "app.json").write_text("{}\n", encoding="utf-8")
    (obsidian / "core-plugins.json").write_text("[]\n", encoding="utf-8")
    (obsidian / "templates.json").write_text("{}\n", encoding="utf-8")
    (obsidian / "workspace.json").write_text("{}\n", encoding="utf-8")
    return root, source_id


def git_commit_all(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "add", ".")
    run_git(
        root,
        "-c",
        "user.name=Backup Tests",
        "-c",
        "user.email=backup-tests@example.local",
        "commit",
        "-m",
        "baseline",
    )


def zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def zip_manifest(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("backup-manifest.json").decode("utf-8"))


def write_crafted_backup(path: Path, archive_name: str, data: bytes) -> None:
    manifest = {
        "format_version": 1,
        "created_at": "2026-07-06T00:00:00+00:00",
        "files": [
            {
                "path": archive_name,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(archive_name, data)
        archive.writestr(
            "backup-manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )


class BackupTests(unittest.TestCase):
    def test_backup_contains_only_allowlisted_durable_assets_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("pass", result.status, result.to_json())
            names = zip_names(output)
            self.assertIn("backup-manifest.json", names)
            self.assertIn(".gitignore", names)
            self.assertIn("meta/kb-manifest.json", names)
            self.assertIn("meta/review-queue.md", names)
            self.assertIn("docs/reviews/review-note.md", names)
            self.assertIn(".obsidian/app.json", names)
            self.assertIn(".obsidian/core-plugins.json", names)
            self.assertIn(".obsidian/templates.json", names)
            self.assertTrue(any(name.startswith("raw/") for name in names))
            self.assertTrue(any(name.startswith("sources/") for name in names))
            self.assertTrue(any(name.startswith("wiki/") for name in names))
            self.assertTrue(any(name.startswith("meta/") for name in names))
            self.assertNotIn(".obsidian/workspace.json", names)
            self.assertTrue(validate_backup_manifest(output).status == "pass")

    def test_backup_excludes_runtime_database_logs_cache_locks_and_temp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            (root / "db" / "kb.sqlite3-journal").write_text("journal", encoding="utf-8")
            (root / "meta" / "cache").mkdir()
            (root / "meta" / "cache" / "runtime.bin").write_bytes(b"cache")
            (root / "meta" / "audit").mkdir()
            (root / "meta" / "audit" / "runtime.jsonl").write_text("{}", encoding="utf-8")
            (root / "meta" / "logs").mkdir()
            (root / "meta" / "logs" / "run.log").write_text("log", encoding="utf-8")
            (root / "meta" / "ocr").mkdir()
            (root / "meta" / "ocr" / "scan.txt").write_text("ocr", encoding="utf-8")
            (root / "raw" / "scan.ocr.txt").write_text("ocr temp", encoding="utf-8")
            (root / "raw" / "scratch.tmp").write_text("temp", encoding="utf-8")
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("pass", result.status, result.to_json())
            names = zip_names(output)
            excluded = {
                "db/kb.sqlite3",
                "db/kb.sqlite3-journal",
                "meta/.kb-write.lock",
                "meta/log.md",
                "meta/cache/runtime.bin",
                "meta/audit/runtime.jsonl",
                "meta/logs/run.log",
                "meta/ocr/scan.txt",
                "raw/scan.ocr.txt",
                "raw/scratch.tmp",
            }
            for name in excluded:
                self.assertNotIn(name, names)

    def test_unlisted_inbox_file_fails_as_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            (root / "inbox" / "stray.md").write_text(
                "Unreviewed inbox file should not be silently ignored.\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("outside_allowlist", result.classification)
            self.assertEqual("inbox/stray.md", result.to_dict()["details"]["path"])
            self.assertFalse(output.exists())

    def test_unlisted_non_runtime_meta_file_fails_as_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            (root / "meta" / "non-allowlisted-note.md").write_text(
                "Durable-looking meta file is not in the backup allowlist.\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("outside_allowlist", result.classification)
            self.assertEqual(
                "meta/non-allowlisted-note.md", result.to_dict()["details"]["path"]
            )
            self.assertFalse(output.exists())

    def test_secret_in_candidate_fails_without_output_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            sentinel = "sk-" + "backup-test-secret-sentinel"
            (root / "raw" / "secret-note.md").write_text(
                f"temporary credential {sentinel}\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)
            self.assertFalse(output.exists())

    def test_secret_shaped_candidate_filename_fails_without_output_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            sentinel = "s" + "k-" + "review-filename-sentinel"
            (root / "raw" / f"{sentinel}.md").write_text(
                "Clean content in a secret-shaped filename.\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)
            self.assertFalse(output.exists())

    def test_active_write_lock_fails_without_archive_or_temp_package(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = temp / "backup.zip"

            with acquire_write_lock(root, operation="outer"):
                result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("write_lock_active", result.classification)
            self.assertFalse(output.exists())
            leftovers = [path.name for path in temp.iterdir() if path.name.startswith(".backup")]
            self.assertEqual([], leftovers)

    def test_dirty_git_requires_confirmation_and_does_not_broadly_suppress_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            git_commit_all(root)
            (root / "meta" / "review-queue.md").write_text(
                "# Review Queue\n\n- [ ] follow up\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("dirty_worktree_unconfirmed", result.classification)
            self.assertFalse(output.exists())
            dirty_entries = result.to_dict()["details"]["dirty_entries"]
            self.assertIn("meta/review-queue.md", dirty_entries)

    def test_allow_dirty_records_dirty_state_and_filters_runtime_only_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, source_id = create_root(temp)
            git_commit_all(root)
            (root / "wiki" / "grounded.md").write_text(
                f"# Grounded\n\nChanged durable asset still cites {source_id}.",
                encoding="utf-8",
            )
            (root / "meta" / "cache").mkdir(exist_ok=True)
            (root / "meta" / "cache" / "runtime.txt").write_text(
                "ignored runtime cache\n",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output, allow_dirty=True)

            self.assertEqual("pass", result.status, result.to_json())
            manifest = zip_manifest(output)
            self.assertEqual(True, manifest["git"]["dirty"])
            self.assertIn("wiki/grounded.md", manifest["git"]["dirty_entries"])
            self.assertNotIn("meta/.kb-write.lock", manifest["git"]["dirty_entries"])
            self.assertNotIn("meta/cache/runtime.txt", manifest["git"]["dirty_entries"])

    def test_backup_rejects_output_over_existing_root_asset_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = root / "wiki" / "grounded.md"
            original = output.read_bytes()

            result = create_backup(root, output, allow_dirty=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("unsafe_backup_output_path", result.classification)
            self.assertEqual(original, output.read_bytes())

    def test_backup_rejects_new_non_runtime_output_inside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = root / "backups" / "archive.zip"

            result = create_backup(root, output, allow_dirty=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("unsafe_backup_output_path", result.classification)
            self.assertFalse(output.exists())
            self.assertFalse(output.parent.exists())

    def test_backup_rejects_new_runtime_output_inside_root_without_creating_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = root / "meta" / "cache" / "archive.zip"

            result = create_backup(root, output, allow_dirty=True)

            self.assertEqual("failed", result.status)
            self.assertEqual("unsafe_backup_output_path", result.classification)
            self.assertFalse(output.exists())
            self.assertFalse(output.parent.exists())

    def test_blocking_governance_issue_is_classified_before_packaging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            (root / "wiki" / "ungrounded.md").write_text(
                "# Ungrounded\n\nThis stable page has no source citation.",
                encoding="utf-8",
            )
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("blocking_governance_issue", result.classification)
            self.assertFalse(output.exists())

    def test_symlink_or_reparse_candidate_returns_classified_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = temp / "backup.zip"
            unsafe = root / "raw" / "unsafe-link.md"
            outside = temp / "outside.md"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                unsafe.symlink_to(outside)
                result = create_backup(root, output)
            except (OSError, NotImplementedError):
                unsafe.write_text("fallback\n", encoding="utf-8")

                def fake_safety(root_path: Path, path: Path, archive_name: str):
                    if archive_name == "raw/unsafe-link.md":
                        return ProductResult(
                            status="failed",
                            classification="symlink_escape",
                            summary="Unsafe link rejected.",
                            severity="blocking",
                        )
                    return None

                with mock.patch("kb.backup._path_safety_failure", side_effect=fake_safety):
                    result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertIn(
                result.classification,
                {"symlink_escape", "junction_or_reparse_point", "canonical_escape"},
            )
            self.assertFalse(output.exists())

    def test_manifest_validation_detects_hash_mismatch_and_malformed_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = temp / "backup.zip"
            create_backup(root, output)
            manifest = zip_manifest(output)
            manifest["files"][0]["sha256"] = "0" * 64
            tampered = temp / "tampered.zip"
            with zipfile.ZipFile(output) as source, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_DEFLATED
            ) as target:
                for name in source.namelist():
                    if name == "backup-manifest.json":
                        target.writestr(
                            name,
                            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                        )
                    else:
                        target.writestr(name, source.read(name))

            mismatch = validate_backup_manifest(tampered)

            self.assertEqual("failed", mismatch.status)
            self.assertEqual("hash_mismatch", mismatch.classification)

            malformed = temp / "malformed.zip"
            with zipfile.ZipFile(malformed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("backup-manifest.json", "{not-json")
            invalid = validate_backup_manifest(malformed)
            self.assertEqual("failed", invalid.status)
            self.assertEqual("manifest_invalid", invalid.classification)

    def test_manifest_validation_rejects_control_character_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            malformed_path = "raw/bad\nname.md"
            data = b"bad path\n"
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [
                    {
                        "path": malformed_path,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            }
            archive_path = temp / "control-char.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(malformed_path, data)
                archive.writestr(
                    "backup-manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                )

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("control_character", result.classification)

    def test_manifest_validation_rejects_outside_allowlist_archive_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "outside-allowlist.zip"
            write_crafted_backup(archive_path, "inbox/stray.md", b"clean stray\n")

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("outside_allowlist", result.classification)

    def test_manifest_validation_rejects_secret_payload_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "secret-payload.zip"
            sentinel = "s" + "k-" + "manifest-payload-sentinel"
            data = f"temporary credential {sentinel}\n".encode("utf-8")
            write_crafted_backup(archive_path, "raw/secret-payload.md", data)

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)

    def test_manifest_validation_rejects_duplicate_zip_member_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "duplicate-member.zip"
            archive_name = "raw/source.md"
            sentinel = "s" + "k-" + "duplicate-hidden-secret"
            secret_data = f"temporary credential {sentinel}\n".encode("utf-8")
            clean_data = b"clean content that matches the manifest\n"
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [
                    {
                        "path": archive_name,
                        "sha256": hashlib.sha256(clean_data).hexdigest(),
                    }
                ],
            }
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(archive_name, secret_data)
                archive.writestr(archive_name, clean_data)
                archive.writestr(
                    "backup-manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                )

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("duplicate_normalized_path", result.classification)

    def test_manifest_validation_rejects_duplicate_manifest_members(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "duplicate-manifest.zip"
            data = b"clean content\n"
            manifest = {
                "format_version": 1,
                "created_at": "2026-07-06T00:00:00+00:00",
                "files": [
                    {
                        "path": "raw/source.md",
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("raw/source.md", data)
                archive.writestr("backup-manifest.json", manifest_bytes)
                archive.writestr("backup-manifest.json", manifest_bytes)

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("duplicate_normalized_path", result.classification)

    def test_manifest_validation_rejects_secret_shaped_archive_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "secret-path.zip"
            sentinel = "s" + "k-" + "manifest-path-sentinel"
            write_crafted_backup(
                archive_path,
                f"raw/{sentinel}.md",
                b"clean content in secret-shaped path\n",
            )

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)

    def test_backup_manifest_omits_secret_shaped_root_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            sentinel = "sk-" + "root-label-sentinel"
            root, _source_id = create_root(temp, root_name=sentinel)
            output = temp / "backup.zip"

            result = create_backup(root, output)

            self.assertEqual("pass", result.status, result.to_json())
            manifest = zip_manifest(output)
            manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            self.assertNotEqual(sentinel, manifest.get("root_label"))
            self.assertNotIn(sentinel, manifest_text)
            self.assertEqual("pass", validate_backup_manifest(output).status)

    def test_short_generic_environment_secret_does_not_poison_backup_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(
                temp,
                root_name="sk-" + "generic-env-root-label",
            )
            output = temp / "backup.zip"

            with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "root"}, clear=False):
                result = create_backup(root, output)
                validation = validate_backup_manifest(output)

            self.assertEqual("pass", result.status, result.to_json())
            self.assertEqual("pass", validation.status, validation.to_json())

    def test_explicit_api_key_environment_value_is_scanned_even_when_short(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, _source_id = create_root(temp)
            output = temp / "backup.zip"

            with mock.patch.dict(
                "os.environ",
                {"KB_LLM_API_KEY": "Source"},
                clear=False,
            ):
                result = create_backup(root, output)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)
            self.assertFalse(output.exists())

    def test_manifest_validation_rejects_secret_shaped_root_label_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            archive_path = temp / "secret-root-label.zip"
            write_crafted_backup(archive_path, "raw/source.md", b"clean content\n")
            sentinel = "sk-" + "root-label-sentinel"
            manifest = zip_manifest(archive_path)
            manifest["root_label"] = sentinel
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("raw/source.md", b"clean content\n")
                archive.writestr(
                    "backup-manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                )

            result = validate_backup_manifest(archive_path)

            self.assertEqual("failed", result.status)
            self.assertEqual("secret_in_backup_candidate", result.classification)

    def test_cli_backup_exit_code_matches_product_result_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, source_id = create_root(temp)
            git_commit_all(root)
            output = temp / "backup.zip"

            passing = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "backup",
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, passing.returncode, passing.stderr)
            self.assertEqual("pass", json.loads(passing.stdout)["status"])

            (root / "wiki" / "grounded.md").write_text(
                f"# Grounded\n\nDirty durable asset still cites {source_id}.",
                encoding="utf-8",
            )
            dirty_output = temp / "dirty.zip"
            failing = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "backup",
                    "--root",
                    str(root),
                    "--output",
                    str(dirty_output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, failing.returncode)
            self.assertEqual("dirty_worktree_unconfirmed", json.loads(failing.stdout)["classification"])
            self.assertEqual("", failing.stderr)

            allow_dirty = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "backup",
                    "--root",
                    str(root),
                    "--output",
                    str(dirty_output),
                    "--allow-dirty",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, allow_dirty.returncode, allow_dirty.stderr)
            self.assertEqual("pass", json.loads(allow_dirty.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
