import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import kb.locks as locks_module
from kb.commands import init_repository
from kb.locks import WriteLockError, acquire_write_lock, lock_check, recover_lock


def stale_lock_payload(root: Path) -> dict[str, object]:
    return {
        "pid": 0,
        "process_name": "python",
        "started_at": "2000-01-01T00:00:00+00:00",
        "operation": "stale-test",
        "engine_version": "0.1.0",
        "nonce": "old-nonce",
        "host": "test-host",
        "cwd": str(root),
        "heartbeat_at": "2000-01-01T00:00:00+00:00",
        "lease_seconds": 1,
    }


def write_stale_lock(root: Path) -> Path:
    lock_path = root / "meta" / ".kb-write.lock"
    lock_path.write_text(
        json.dumps(stale_lock_payload(root), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return lock_path


class RootWriteLockTests(unittest.TestCase):
    def test_acquire_write_lock_creates_expected_runtime_lock_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"

            with acquire_write_lock(root, operation="test") as lock:
                lock_path = root / "meta" / ".kb-write.lock"
                self.assertTrue(lock_path.samefile(lock.path))
                self.assertTrue(lock_path.is_file())
                data = json.loads(lock_path.read_text(encoding="utf-8"))

                for field in (
                    "pid",
                    "process_name",
                    "started_at",
                    "operation",
                    "engine_version",
                    "nonce",
                    "host",
                    "cwd",
                    "heartbeat_at",
                    "lease_seconds",
                ):
                    self.assertIn(field, data)
                self.assertEqual("test", data["operation"])
                self.assertEqual(lock.nonce, data["nonce"])

            self.assertFalse((root / "meta" / ".kb-write.lock").exists())

    def test_nested_acquisition_fails_no_write_with_active_classification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with acquire_write_lock(root, operation="outer"):
                lock_path = root / "meta" / ".kb-write.lock"
                before = lock_path.read_text(encoding="utf-8")
                with self.assertRaises(WriteLockError) as raised:
                    with acquire_write_lock(root, operation="inner"):
                        pass
                after = lock_path.read_text(encoding="utf-8")

            self.assertEqual("write_lock_active", raised.exception.classification)
            self.assertEqual(before, after)

    def test_lock_file_redacts_secret_environment_values_and_key_shapes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            llm_secret = "llm-" + uuid.uuid4().hex
            embedding_secret = "embedding-" + uuid.uuid4().hex
            key_shape = "s" + "k-" + "runtime-key-shape-" + uuid.uuid4().hex

            with patch.dict(
                os.environ,
                {
                    "KB_LLM_API_KEY": llm_secret,
                    "KB_EMBEDDING_API_KEY": embedding_secret,
                },
            ):
                with acquire_write_lock(
                    root,
                    operation=f"test {llm_secret} {embedding_secret} {key_shape}",
                ):
                    text = (root / "meta" / ".kb-write.lock").read_text(
                        encoding="utf-8"
                    )

            self.assertNotIn("KB_LLM_API_KEY", text)
            self.assertNotIn("KB_EMBEDDING_API_KEY", text)
            self.assertNotIn(llm_secret, text)
            self.assertNotIn(embedding_secret, text)
            self.assertNotIn("s" + "k-", text)

    def test_generated_root_gitignore_contains_runtime_only_lock_and_cache_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"

            init_repository(root)

            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            for entry in (
                "meta/.kb-write.lock",
                "*.tmp",
                "meta/audit/",
                "meta/cache/",
                "meta/runtime/",
            ):
                self.assertIn(entry, gitignore)

    def test_existing_gitignore_comment_mentions_do_not_satisfy_runtime_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            root.mkdir()
            required = {
                "meta/.kb-write.lock",
                "*.tmp",
                "meta/audit/",
                "meta/cache/",
                "meta/runtime/",
            }
            (root / ".gitignore").write_text(
                "\n".join(f"# mentioned only: {entry}" for entry in sorted(required))
                + "\n",
                encoding="utf-8",
            )

            init_repository(root)

            active_entries = {
                line.strip()
                for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            self.assertTrue(required.issubset(active_entries))

    def test_init_rejects_broken_gitignore_symlink_without_escape_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            root.mkdir()
            outside = Path(tmpdir) / "outside" / ".gitignore"
            try:
                (root / ".gitignore").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, ".gitignore"):
                init_repository(root)

            self.assertFalse(outside.exists())

    def test_lock_operations_reject_broken_meta_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            root.mkdir()
            outside = Path(tmpdir) / "missing-meta"
            try:
                (root / "meta").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            checked = lock_check(root).to_dict()
            self.assertEqual("failed", checked["status"])
            self.assertEqual("lock_path_escape", checked["classification"])

            with self.assertRaises(WriteLockError) as raised:
                acquire_write_lock(root, operation="test")
            self.assertEqual("path_invalid", raised.exception.classification)

    def test_lock_check_classifies_absent_active_and_stale_locks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            no_lock = lock_check(root).to_dict()
            self.assertEqual("pass", no_lock["status"])
            self.assertEqual("no_lock", no_lock["classification"])

            with acquire_write_lock(root, operation="test"):
                active = lock_check(root).to_dict()
                self.assertEqual("failed", active["status"])
                self.assertEqual("active_lock", active["classification"])

            stale_path = write_stale_lock(root)
            stale = lock_check(root).to_dict()
            self.assertEqual("failed", stale["status"])
            self.assertEqual("stale_lock_candidate", stale["classification"])
            self.assertTrue(stale_path.is_file())

    def test_wrong_nonce_release_fails_and_preserves_lock_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with acquire_write_lock(root, operation="test") as lock:
                with self.assertRaises(WriteLockError) as raised:
                    lock.release(nonce="wrong-nonce")

                self.assertEqual("write_lock_nonce_mismatch", raised.exception.classification)
                self.assertTrue((root / "meta" / ".kb-write.lock").is_file())

    def test_release_refuses_tampered_lock_file_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            lock = acquire_write_lock(root, operation="test")
            lock_path = root / "meta" / ".kb-write.lock"
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["operation"] = "tampered-operation"
            lock_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            try:
                with self.assertRaises(WriteLockError) as raised:
                    lock.release()
                self.assertEqual(
                    "write_lock_identity_mismatch",
                    raised.exception.classification,
                )
                self.assertTrue(lock_path.is_file())
            finally:
                if lock_path.exists():
                    lock_path.unlink()
                lock.released = True

    def test_recover_lock_refuses_changed_lock_after_classification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            lock_path = write_stale_lock(root)
            original_parse = locks_module._parse_lock_payload_bytes

            def parse_and_replace(path, data):
                result = original_parse(path, data)
                replacement = stale_lock_payload(root)
                replacement["nonce"] = "replacement-nonce"
                path.write_text(
                    json.dumps(replacement, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                return result

            with patch(
                "kb.locks._parse_lock_payload_bytes",
                side_effect=parse_and_replace,
            ):
                result = recover_lock(root, manual_confirm=True).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("write_lock_identity_mismatch", result["classification"])
            self.assertTrue(lock_path.is_file())

    def test_cli_lock_check_and_recover_lock_print_classified_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]

            lock_check_pass = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "lock-check",
                    "--root",
                    str(root),
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, lock_check_pass.returncode, lock_check_pass.stderr)
            self.assertEqual("no_lock", json.loads(lock_check_pass.stdout)["classification"])

            stale_path = write_stale_lock(root)
            recover_refused = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "recover-lock",
                    "--root",
                    str(root),
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, recover_refused.returncode)
            self.assertEqual(
                "manual_confirmation_required",
                json.loads(recover_refused.stdout)["classification"],
            )
            self.assertTrue(stale_path.is_file())

            recover_confirmed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "recover-lock",
                    "--root",
                    str(root),
                    "--manual-confirm",
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, recover_confirmed.returncode, recover_confirmed.stderr)
            self.assertEqual(
                "lock_recovered",
                json.loads(recover_confirmed.stdout)["classification"],
            )
            self.assertFalse(stale_path.exists())

    def test_recover_lock_api_requires_manual_confirm_for_uncertain_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            lock_path = root / "meta" / ".kb-write.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "heartbeat_at": datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                        "lease_seconds": 900,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            uncertain = lock_check(root).to_dict()
            self.assertEqual("failed", uncertain["status"])
            self.assertEqual("uncertain_lock", uncertain["classification"])

            refused = recover_lock(root).to_dict()
            self.assertEqual("failed", refused["status"])
            self.assertEqual("manual_confirmation_required", refused["classification"])
            self.assertTrue(lock_path.is_file())

            recovered = recover_lock(root, manual_confirm=True).to_dict()
            self.assertEqual("pass", recovered["status"])
            self.assertEqual("lock_recovered", recovered["classification"])
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
