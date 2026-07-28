import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kb.commands import govern, ingest_file, init_repository
from kb.locks import acquire_write_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_SECRET = "doctor-secret-sentinel"


def checks_by_id(result: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(check["id"]): check for check in result["checks"]}


def read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


class DoctorTests(unittest.TestCase):
    def test_fresh_initialized_root_returns_status_and_required_checks(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = doctor(root)

            self.assertIn(result["status"], {"pass", "warning", "failed"})
            self.assertEqual(str(root.resolve()), result["root"])
            checks = checks_by_id(result)
            self.assertTrue(
                {
                    "root-exists",
                    "initialized",
                    "manifest",
                    "schema",
                    "write-lock",
                    "git-installed",
                    "git-repository",
                    "git-worktree-clean",
                    "python-version",
                    "package-import",
                    "sqlite-fts",
                    "index-status",
                    "vector-index-status",
                    "lint",
                    "status",
                    "governance",
                    "tesseract",
                    "obsidian",
                    "llm-config",
                    "embedding-config",
                    "backup-freshness",
                    "docs-encoding",
                    "migration-status",
                }.issubset(checks)
            )
            self.assertEqual("pass", checks["schema"]["status"])
            self.assertEqual("pass", checks["write-lock"]["status"])

    def test_default_doctor_is_no_write_and_preserves_existing_artifacts(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            quality = root / "meta" / "quality-report.md"
            quality.write_text("existing quality report\n", encoding="utf-8")
            tracked = (
                quality,
                root / "meta" / "log.md",
                root / "meta" / "review-queue.md",
                root / "db" / "kb.sqlite3",
            )
            before = {path: read_bytes(path) for path in tracked}

            doctor(root)

            after = {path: read_bytes(path) for path in tracked}
            self.assertEqual(before, after)

    def test_default_doctor_does_not_create_quality_report_when_absent(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            quality = root / "meta" / "quality-report.md"
            quality.unlink()
            self.assertFalse(quality.exists())

            doctor(root)

            self.assertFalse(quality.exists())

    def test_default_doctor_does_not_call_online_probes_or_govern_writer(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            (root / "meta" / "quality-report.md").unlink()

            with mock.patch("kb.doctor._probe_llm_online") as llm_probe, mock.patch(
                "kb.doctor._probe_embedding_online"
            ) as embedding_probe, mock.patch("kb.commands.govern") as govern_writer:
                llm_probe.side_effect = AssertionError("default doctor called LLM probe")
                embedding_probe.side_effect = AssertionError(
                    "default doctor called embedding probe"
                )
                govern_writer.side_effect = AssertionError(
                    "default doctor called govern writer"
                )

                doctor(root)

            llm_probe.assert_not_called()
            embedding_probe.assert_not_called()
            govern_writer.assert_not_called()
            self.assertFalse((root / "meta" / "quality-report.md").exists())

    def test_active_lock_returns_write_lock_check(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            with acquire_write_lock(root, operation="outer"):
                result = doctor(root)

            check = checks_by_id(result)["write-lock"]
            self.assertEqual("failed", check["status"])
            self.assertEqual("active_lock", check["classification"])

    def test_dirty_git_worktree_is_blocking_not_pass(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            result = doctor(root)

            check = checks_by_id(result)["git-worktree-clean"]
            self.assertEqual("failed", check["status"])
            self.assertEqual("blocking", check["severity"])
            self.assertEqual("dirty_worktree", check["classification"])
            self.assertEqual("failed", result["status"])

    def test_missing_optional_dependencies_are_warnings(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("KB_LLM_")
                and not key.startswith("KB_EMBEDDING_")
                and key != "KB_TESSERACT_CMD"
            }

            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "kb.doctor.shutil.which",
                side_effect=lambda name: None
                if name in {"tesseract", "obsidian", "git"}
                else None,
            ):
                result = doctor(root)

            checks = checks_by_id(result)
            for check_id in (
                "tesseract",
                "obsidian",
                "llm-config",
                "embedding-config",
            ):
                self.assertEqual("warning", checks[check_id]["status"], check_id)
                self.assertNotEqual("pass", checks[check_id]["classification"], check_id)

    def test_online_mode_calls_explicit_probes_and_redacts_provider_output(self):
        from kb.doctor import doctor

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SENTINEL_SECRET}), mock.patch(
                "kb.doctor._probe_llm_online",
                return_value={
                    "status": "pass",
                    "classification": "online_ok",
                    "summary": f"LLM provider {SENTINEL_SECRET} is reachable.",
                    "provider": f"https://example.test/{SENTINEL_SECRET}",
                },
            ) as llm_probe, mock.patch(
                "kb.doctor._probe_embedding_online",
                return_value={
                    "status": "warning",
                    "classification": "online_unavailable",
                    "summary": f"Embedding provider {SENTINEL_SECRET} unavailable.",
                    "provider": f"https://embed.example.test/{SENTINEL_SECRET}",
                },
            ) as embedding_probe:
                result = doctor(root, online=True)

            llm_probe.assert_called_once()
            embedding_probe.assert_called_once()
            payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(SENTINEL_SECRET, payload)
            checks = checks_by_id(result)
            self.assertEqual("pass", checks["llm-online"]["status"])
            self.assertEqual("warning", checks["embedding-online"]["status"])

    def test_analyze_governance_is_read_only_and_govern_still_writes_report(self):
        from kb.governance import analyze_governance, render_quality_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nGovernance analysis evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            (root / "wiki" / "grounded.md").write_text(
                f"# Grounded\n\nGovernance analysis evidence cites {source_id}.",
                encoding="utf-8",
            )
            quality = root / "meta" / "quality-report.md"
            quality.unlink()

            analysis = analyze_governance(root)
            report = render_quality_report(analysis)

            self.assertFalse(quality.exists())
            self.assertEqual(0, analysis["blocking_count"])
            self.assertIn("## Blocking Issues", report)

            result = govern(root)

            self.assertTrue(quality.is_file())
            self.assertEqual(0, result["blocking_count"])
            self.assertEqual(report, quality.read_text(encoding="utf-8"))

    def test_cli_doctor_json_and_online_json_work_without_real_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            (root / "meta" / "quality-report.md").unlink()

            default = subprocess.run(
                [sys.executable, "-B", "-m", "kb", "doctor", "--root", str(root), "--json"],
                cwd=PROJECT_ROOT,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("KB_LLM_")
                    and not key.startswith("KB_EMBEDDING_")
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            online = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "kb",
                    "doctor",
                    "--root",
                    str(root),
                    "--online",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("KB_LLM_")
                    and not key.startswith("KB_EMBEDDING_")
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertIn(default.returncode, {0, 1})
            self.assertIn(online.returncode, {0, 1})
            self.assertEqual("", default.stderr)
            self.assertEqual("", online.stderr)
            self.assertIn("checks", json.loads(default.stdout))
            self.assertIn("checks", json.loads(online.stdout))
            self.assertFalse((root / "meta" / "quality-report.md").exists())

    def test_cli_doctor_non_json_is_one_line_redacted_summary_with_failed_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / SENTINEL_SECRET / "missing"
            env = os.environ.copy()
            env["KB_LLM_API_KEY"] = SENTINEL_SECRET

            completed = subprocess.run(
                [sys.executable, "-B", "-m", "kb", "doctor", "--root", str(root)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            self.assertEqual(1, len(lines), completed.stdout)
            self.assertIn("failed=", lines[0])
            self.assertIn("root-exists", lines[0])
            self.assertNotIn("Traceback", completed.stderr)
            self.assertNotIn(SENTINEL_SECRET, completed.stdout)


if __name__ == "__main__":
    unittest.main()
