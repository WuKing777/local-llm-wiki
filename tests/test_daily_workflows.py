import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import daily_workflow, init_repository
from kb.locks import acquire_write_lock
from kb.memory_candidates import capture, review


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _snapshot_content_dirs(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in ("wiki", "raw", "sources"):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _sentinel(label: str) -> str:
    return "sk" + f"-{label}1234567890abcdef"


class DailyWorkflowTests(unittest.TestCase):
    def test_plan_writes_only_meta_workflow_daily_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            before_content = _snapshot_content_dirs(root)

            result = daily_workflow(root, workflow_date="2026-07-07")

            plan_path = root / "meta" / "workflows" / "daily" / "2026-07-07.json"
            self.assertEqual(plan_path.resolve(), Path(str(result["path"])).resolve())
            self.assertTrue(plan_path.is_file())
            self.assertEqual(before_content, _snapshot_content_dirs(root))
            self.assertEqual([], list((root / "wiki" / "_drafts").glob("*")))
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            for key in (
                "workflow_id",
                "date",
                "created_at",
                "status",
                "commands",
                "open_items",
                "source_counts",
                "candidate_counts",
                "review_targets",
            ):
                self.assertIn(key, data)

    def test_commands_include_compile_page_gates_and_suggest_topics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = daily_workflow(root, workflow_date="2026-07-07")
            commands = "\n".join(result["commands"])

            self.assertIn("capture-candidate", commands)
            self.assertIn("suggest-topics --root <root>", commands)
            self.assertIn(
                "compile-page --root <root>", commands
            )
            self.assertNotIn(str(root), commands)
            self.assertIn("--kind daily --date 2026-07-07 --archive-existing", commands)
            self.assertIn("--kind weekly-review --period 2026-W28 --archive-existing", commands)
            self.assertIn("--kind monthly-review --period 2026-07 --archive-existing", commands)
            self.assertIn("--kind goal", commands)
            self.assertIn("--kind project", commands)
            self.assertIn("--kind decision", commands)
            self.assertIn("--kind preference-summary", commands)

    def test_candidate_counts_do_not_leak_candidate_text_or_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            pending = capture(
                root,
                type="preference",
                text="I prefer private workflow notes.",
                event_date="2026-07-07",
                privacy="personal",
                confidence="confirmed",
                value_reason="Useful for daily planning.",
                suggested_source_type="self_statement",
            )
            approved = capture(
                root,
                type="preference",
                text="Approved text must stay out of plans.",
                event_date="2026-07-07",
                privacy="personal",
                confidence="confirmed",
                value_reason="Useful for review.",
                suggested_source_type="self_statement",
            )
            review(root, approved["id"], status="approved")
            rejected = capture(
                root,
                type="preference",
                text="Rejected text must stay out too.",
                event_date="2026-07-07",
                privacy="personal",
                confidence="confirmed",
                value_reason="Useful for review.",
                suggested_source_type="self_statement",
            )
            review(root, rejected["id"], status="rejected")
            candidate_dir = root / "meta" / "memory-candidates"
            published_data = dict(pending)
            published_data["id"] = "mem-1111111111111111"
            published_data["status"] = "published"
            published_data["source_id"] = "src-111111111111"
            (candidate_dir / "mem-1111111111111111.json").write_text(
                json.dumps(published_data),
                encoding="utf-8",
            )
            secret = _sentinel("SENTINELPLAN")
            (candidate_dir / "broken.json").write_text(
                f'{{"text": "{secret}",',
                encoding="utf-8",
            )

            result = daily_workflow(root, workflow_date="2026-07-07")
            payload = json.dumps(result, ensure_ascii=False, sort_keys=True)

            self.assertEqual(
                {"pending": 1, "approved": 1, "rejected": 1, "published": 1, "damaged": 1},
                result["candidate_counts"],
            )
            self.assertNotIn("private workflow notes", payload)
            self.assertNotIn("Approved text", payload)
            self.assertNotIn("Rejected text", payload)
            self.assertNotIn(secret, payload)
            self.assertTrue(
                any(item["type"] == "damaged-memory-candidate" for item in result["open_items"])
            )

    def test_uninitialized_root_is_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"

            with self.assertRaisesRegex(RuntimeError, "Knowledge base is not initialized"):
                daily_workflow(root, workflow_date="2026-07-07")

            self.assertFalse(root.exists())

    def test_active_lock_is_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaisesRegex(Exception, "A write lock is already active"):
                    daily_workflow(root, workflow_date="2026-07-07")

            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
            self.assertEqual(before, after)

    def test_workflows_symlink_directory_is_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            external = base / "external"
            init_repository(root)
            external.mkdir()
            try:
                os.symlink(external, root / "meta" / "workflows", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "workflow path is unsafe"):
                daily_workflow(root, workflow_date="2026-07-07")

            self.assertEqual([], list(external.iterdir()))

    def test_cli_json_is_parseable_redacted_and_deterministic_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            secret = _sentinel("SENTINELCLI")
            candidate_dir = root / "meta" / "memory-candidates"
            candidate_dir.mkdir()
            (candidate_dir / "broken.json").write_text(
                f'{{"text": "{secret}",',
                encoding="utf-8",
            )

            code, stdout, stderr = _run_cli(
                ["daily-workflow", "--root", str(root), "--date", "2026-07-07", "--json"]
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            payload = json.loads(stdout)
            self.assertEqual("2026-07-07", payload["date"])
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertNotIn(secret, stdout)
            self.assertIn("candidate_counts", payload)

    def test_first_plan_write_failure_removes_new_workflow_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with mock.patch(
                "kb.daily_workflows.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    daily_workflow(root, workflow_date="2026-07-07")

            self.assertFalse((root / "meta" / "workflows").exists())


if __name__ == "__main__":
    unittest.main()
