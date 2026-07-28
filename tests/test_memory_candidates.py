import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import init_repository
from kb.locks import WriteLockError, acquire_write_lock
from kb.memory_candidates import capture, publish, review


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _candidate_files(root: Path) -> list[Path]:
    return sorted((root / "meta" / "memory-candidates").glob("*.json"))


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sentinel(label: str) -> str:
    return "sk" + f"-{label}1234567890abcdef"


def _snapshot_selected(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in ("raw", "sources", "meta", "db", "wiki"):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _capture_kwargs(text: str = "I prefer explicit review before memory publish."):
    return {
        "type": "preference",
        "text": text,
        "event_date": "2026-07-01",
        "privacy": "personal",
        "confidence": "confirmed",
        "value_reason": "Useful long-term collaboration preference.",
        "suggested_source_type": "self_statement",
    }


class MemoryCandidateTests(unittest.TestCase):
    def test_capture_writes_candidate_only_without_source_raw_or_wiki(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = capture(root, **_capture_kwargs())

            files = _candidate_files(root)
            self.assertEqual(1, len(files))
            data = _read_json(files[0])
            self.assertEqual(result["id"], data["id"])
            self.assertEqual("pending", data["status"])
            self.assertTrue(data["needs_confirmation"])
            self.assertEqual([], list((root / "sources").glob("src-*.md")))
            self.assertEqual([], list((root / "raw").rglob("*.md")))
            self.assertEqual([], list((root / "wiki").glob("*.md")))

    def test_cli_capture_review_publish_happy_path_creates_source_not_wiki(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            code, stdout, stderr = _run_cli(
                [
                    "capture-candidate",
                    "--root",
                    str(root),
                    "--type",
                    "preference",
                    "--text",
                    "I prefer local evidence for stable memory.",
                    "--event-date",
                    "2026-07-01",
                    "--privacy",
                    "personal",
                    "--confidence",
                    "confirmed",
                    "--value-reason",
                    "Preserves auditability.",
                    "--suggested-source-type",
                    "self_statement",
                ]
            )
            self.assertEqual(0, code, stderr)
            candidate_id = stdout.strip()
            self.assertRegex(candidate_id, r"^mem-[0-9a-f]{16}$")

            code, stdout, stderr = _run_cli(
                [
                    "review-candidate",
                    candidate_id,
                    "--root",
                    str(root),
                    "--status",
                    "approved",
                ]
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(f"{candidate_id} status=approved\n", stdout)

            code, stdout, stderr = _run_cli(
                ["publish-memory", candidate_id, "--root", str(root), "--confirm"]
            )
            self.assertEqual(0, code, stderr)
            source_id = stdout.strip()
            self.assertRegex(source_id, r"^src-[0-9a-f]{12}$")
            self.assertTrue((root / "sources" / f"{source_id}.md").is_file())
            self.assertEqual(1, len(list((root / "raw" / "self-statements").rglob("*.md"))))
            self.assertEqual([], list((root / "wiki").glob("*.md")))

    def test_publish_without_confirm_fails_without_source_or_raw_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            candidate = capture(root, **_capture_kwargs())
            review(root, candidate["id"], status="approved")
            before = _snapshot_selected(root)

            code, stdout, stderr = _run_cli(
                ["publish-memory", candidate["id"], "--root", str(root)]
            )

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("confirm", stderr)
            self.assertEqual(before, _snapshot_selected(root))

    def test_secret_capture_and_cli_errors_are_redacted_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            secret = _sentinel("SENTINELMEMORY")

            with self.assertRaisesRegex(RuntimeError, "suspected secret"):
                capture(root, **_capture_kwargs(text=f"token {secret}"))
            self.assertFalse((root / "meta" / "memory-candidates").exists())

            code, stdout, stderr = _run_cli(
                [
                    "capture-candidate",
                    "--root",
                    str(root),
                    "--type",
                    "preference",
                    "--text",
                    "safe text",
                    "--event-date",
                    secret,
                    "--privacy",
                    "personal",
                    "--confidence",
                    "confirmed",
                    "--value-reason",
                    "safe reason",
                    "--suggested-source-type",
                    "self_statement",
                ]
            )
            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertNotIn(secret, stderr)
            self.assertIn("Invalid event_date", stderr)

            code, _stdout, stderr = _run_cli(
                [
                    "capture-candidate",
                    "--root",
                    str(root),
                    "--type",
                    "preference",
                    "--text",
                    "safe text",
                    "--event-date",
                    "2026-07-01",
                    "--privacy",
                    secret,
                    "--confidence",
                    "confirmed",
                    "--value-reason",
                    "safe reason",
                    "--suggested-source-type",
                    "self_statement",
                ]
            )
            self.assertEqual(2, code)
            self.assertNotIn(secret, stderr)
            self.assertIn("invalid choice", stderr)

    def test_cli_os_errors_are_redacted_without_traceback(self):
        secret = _sentinel("SENTINELOSERROR")

        with mock.patch("kb.cli.init_repository", side_effect=OSError(secret)):
            code, stdout, stderr = _run_cli(["init", "--root", "unused"])

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("error:", stderr)
        self.assertNotIn(secret, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_cli_os_errors_redact_generic_environment_secret_values(self):
        secret = "plain-env-" + "token-value"

        with mock.patch.dict(
            os.environ, {"ACCESS_TOKEN_FOR_REVIEW": secret}, clear=False
        ), mock.patch("kb.cli.init_repository", side_effect=OSError(secret)):
            code, stdout, stderr = _run_cli(["init", "--root", "unused"])

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("error:", stderr)
        self.assertNotIn(secret, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_mutating_apis_fail_under_active_write_lock_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with acquire_write_lock(root, operation="outer"):
                before = _snapshot_selected(root)
                with self.assertRaises(WriteLockError):
                    capture(root, **_capture_kwargs())
                self.assertEqual(before, _snapshot_selected(root))

            candidate = capture(root, **_capture_kwargs("I prefer one writer at a time."))
            with acquire_write_lock(root, operation="outer"):
                before = _snapshot_selected(root)
                with self.assertRaises(WriteLockError):
                    review(root, candidate["id"], status="approved")
                self.assertEqual(before, _snapshot_selected(root))

            review(root, candidate["id"], status="approved")
            with acquire_write_lock(root, operation="outer"):
                before = _snapshot_selected(root)
                with self.assertRaises(WriteLockError):
                    publish(root, candidate["id"], confirm=True)
                self.assertEqual(before, _snapshot_selected(root))

    def test_publish_rolls_back_source_side_effects_when_candidate_marking_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            candidate = capture(root, **_capture_kwargs("I prefer rollback on publish failures."))
            review(root, candidate["id"], status="approved")
            before = _snapshot_selected(root)

            from kb import memory_candidates

            real_write = memory_candidates._write_json_atomic

            def fail_when_published(path, data):
                if data.get("status") == "published":
                    raise RuntimeError("candidate mark failed")
                return real_write(path, data)

            with mock.patch("kb.memory_candidates._write_json_atomic", side_effect=fail_when_published):
                with self.assertRaisesRegex(RuntimeError, "candidate mark failed"):
                    publish(root, candidate["id"], confirm=True)

            self.assertEqual(before, _snapshot_selected(root))

    def test_review_and_publish_reject_damaged_candidate_schema_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            candidate_dir = root / "meta" / "memory-candidates"
            candidate_dir.mkdir()
            candidate_id = "mem-1111111111111111"
            data = {
                "id": candidate_id,
                "type": "preference",
                "text": "I prefer valid records.",
                "original_text": "I prefer valid records.",
                "event_date": "2026-07-01",
                "privacy": "private",
                "confidence": "confirmed",
                "needs_confirmation": True,
                "value_reason": "Keeps review safe.",
                "suggested_source_type": "self_statement",
                "created_at": "2026-07-01T00:00:00",
                "status": "pending",
            }
            path = candidate_dir / f"{candidate_id}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            before = _snapshot_selected(root)

            with self.assertRaisesRegex(RuntimeError, "Invalid memory candidate"):
                review(root, candidate_id, status="approved")
            self.assertEqual(before, _snapshot_selected(root))

            data["status"] = "approved"
            path.write_text(json.dumps(data), encoding="utf-8")
            before = _snapshot_selected(root)
            with self.assertRaisesRegex(RuntimeError, "Invalid memory candidate"):
                publish(root, candidate_id, confirm=True)
            self.assertEqual(before, _snapshot_selected(root))

    def test_path_traversal_and_symlink_candidate_dir_are_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            with self.assertRaisesRegex(RuntimeError, "Invalid candidate id"):
                review(root, "../outside", status="approved")

            external = base / "external"
            external.mkdir()
            candidate_dir = root / "meta" / "memory-candidates"
            try:
                os.symlink(external, candidate_dir, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "memory candidate path is unsafe"):
                capture(root, **_capture_kwargs("I prefer safe candidate paths."))
            self.assertEqual([], list(external.iterdir()))

    def test_uninitialized_root_is_no_write_for_all_memory_operations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"
            with self.assertRaisesRegex(RuntimeError, "root is not initialized"):
                capture(root, **_capture_kwargs())
            self.assertFalse(root.exists())

            with self.assertRaisesRegex(RuntimeError, "root is not initialized"):
                review(root, "mem-1111111111111111", status="approved")
            self.assertFalse(root.exists())

            with self.assertRaisesRegex(RuntimeError, "root is not initialized"):
                publish(root, "mem-1111111111111111", confirm=True)
            self.assertFalse(root.exists())

    def test_write_json_atomic_cleans_temp_file_after_write_and_replace_failure(self):
        from kb import memory_candidates

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "candidate.json"

            class FailingNamedTemporaryFile:
                def __init__(self, *args, **kwargs):
                    self.handle = open(root / ".candidate.json.failure.tmp", "wb")
                    self.name = self.handle.name

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.handle.close()
                    return False

                def write(self, data):
                    self.handle.write(b"partial")
                    raise OSError("write failed")

            with mock.patch(
                "kb.memory_candidates.tempfile.NamedTemporaryFile",
                side_effect=FailingNamedTemporaryFile,
            ):
                with self.assertRaisesRegex(OSError, "write failed"):
                    memory_candidates._write_json_atomic(target, {"status": "pending"})
            self.assertEqual([], list(root.glob("*.tmp")))
            self.assertFalse(target.exists())

            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    memory_candidates._write_json_atomic(target, {"status": "pending"})
            self.assertEqual([], list(root.glob("*.tmp")))
            self.assertFalse(target.exists())

    def test_first_candidate_write_failure_removes_new_metadata_directories(self):
        from kb import memory_candidates

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "meta" / "memory-candidates" / "candidate.json"

            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    memory_candidates._write_json_atomic(target, {"status": "pending"})

            self.assertFalse(target.exists())
            self.assertFalse((root / "meta" / "memory-candidates").exists())
            self.assertFalse((root / "meta").exists())


if __name__ == "__main__":
    unittest.main()
