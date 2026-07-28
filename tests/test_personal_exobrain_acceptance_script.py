import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "run-personal-exobrain-acceptance.ps1"


def _write_ps1(path: Path, body: str) -> None:
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n" + body,
        encoding="utf-8-sig",
    )


def _run_acceptance(root: Path, report: Path, fake_bin: Path, env_updates=None):
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_PATH),
            "-Root",
            str(root),
            "-ReportPath",
            str(report),
            "-PythonCommand",
            str(fake_bin / "python.ps1"),
            "-GitCommand",
            str(fake_bin / "git.ps1"),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _load_report(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_absent(test_case: unittest.TestCase, text: str, value: str, label: str) -> None:
    test_case.assertFalse(value in text, f"{label} leaked into persisted report")


class PersonalExobrainAcceptanceScriptTests(unittest.TestCase):
    def test_acceptance_script_contains_required_commands_and_no_secret(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        required = [
            "Root is required",
            "python -B -m kb lint --root $Root",
            "python -B -m kb status --root $Root",
            "python -B -m kb govern --root $Root",
            "python -B -m kb ocr-check",
            "python -B -m kb ocr-fixture --output $tmpImage",
            "python -B -m kb ingest-ocr --root $tmpRoot --lang chi_sim+eng $tmpImage",
            "python -B -m kb embedding-check",
            "python -B -m kb vector-rebuild --root $Root",
            'python -B -m kb semantic-search "我是谁" --root $Root',
            'python -B -m kb hybrid-search "我是谁" --root $Root',
            "python -B -m kb llm-check",
            "python -B -m kb self-statement --root $Root",
            "python -B -m kb llm-draft --root $Root",
            "python -B -m kb validate-draft",
            "python -B -m kb publish-draft",
            "git -C $Root status --short",
            "git -C $Root diff --check",
        ]
        for command in required:
            self.assertIn(command, script)
        self.assertIsNone(
            re.search(r"\[string\]\$Root\s*=\s*['\"][^'\"]+['\"]", script)
        )
        self.assertNotIn("s" + "k-", script)
        self.assertNotIn('$ErrorActionPreference = "Stop"', script)
        self.assertIn("function Invoke-KbStep", script)
        self.assertIn("function Get-NoWriteSnapshot", script)
        self.assertIn("function Redact-Text", script)
        self.assertIn("function Summarize-Text", script)
        self.assertIn("classification", script)
        self.assertIn("dirty_worktree", script)
        self.assertIn("exit 1", script)
        self.assertIn("StartsWith($tmpParentResolved", script)
        self.assertIn('[string]$PythonCommand = ""', script)
        self.assertIn('[string]$GitCommand = ""', script)
        self.assertIn("function Get-Sha256Hex", script)
        self.assertNotIn("Get-FileHash", script)
        self.assertIn("function Invoke-CommandOverrideProcess", script)
        self.assertIn("RedirectStandardOutput = $true", script)
        self.assertIn("RedirectStandardError = $true", script)
        self.assertIn("$capturedOutput = @(& $Command *>&1)", script)
        self.assertNotIn("New-TemporaryFile", script)

    def test_acceptance_script_requires_explicit_root(self):
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT_PATH),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("Root is required", completed.stdout + completed.stderr)

    def test_acceptance_script_classifies_external_dependency_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "vault"
            root.mkdir()
            report = temp / "acceptance-run.jsonl"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(
                fake_bin / "python.ps1",
                "$joined = $args -join ' '\n"
                "if ($joined.Contains('ocr-check')) { [Console]::Error.WriteLine('tesseract missing'); exit 2 }\n"
                "if ($joined.Contains('embedding-check')) { [Console]::Error.WriteLine('embedding endpoint failed'); exit 3 }\n"
                "if ($joined.Contains('llm-check')) { [Console]::Error.WriteLine('DeepSeek rejected request'); exit 4 }\n"
                "if ($joined.Contains('llm-draft')) { [Console]::Error.WriteLine('LLM dry run invalid response'); exit 5 }\n"
                "Write-Output 'ok'\n"
                "exit 0\n",
            )
            _write_ps1(fake_bin / "git.ps1", "exit 0\n")

            completed = _run_acceptance(root, report, fake_bin)

            self.assertEqual(1, completed.returncode)
            by_name = {entry["name"]: entry for entry in _load_report(report)}
            self.assertEqual("ocr_failed", by_name["ocr-check"]["classification"])
            self.assertEqual("embedding_failed", by_name["embedding-check"]["classification"])
            self.assertEqual("deepseek_failed", by_name["llm-check"]["classification"])
            self.assertEqual("llm_dry_run_failed", by_name["llm-draft"]["classification"])
            self.assertIn("tesseract missing", by_name["ocr-check"]["stderr_summary"])
            self.assertIsNotNone(by_name["llm-check"]["no_write_unchanged"])
            self.assertIsNotNone(by_name["llm-draft"]["no_write_unchanged"])

    def test_acceptance_script_redacts_fake_sentinel_key_and_detects_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "vault"
            root.mkdir()
            report = temp / "acceptance-run.jsonl"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            api_key_shape = "s" + "k-" + "testsecret"
            bearer_value = "abc" + ".def" + ".ghi"
            inline_key_value = "abc" + "123"
            _write_ps1(
                fake_bin / "python.ps1",
                f'Write-Output "stdout $env:KB_LLM_API_KEY {api_key_shape} Authorization: Bearer {bearer_value} api_key={inline_key_value}"\n'
                '[Console]::Error.WriteLine("stderr $env:KB_EMBEDDING_API_KEY password=supersecret token=tok123")\n'
                "exit 0\n",
            )
            _write_ps1(
                fake_bin / "git.ps1",
                "if ($args.Count -gt 2 -and $args[2] -eq 'status') { [Console]::Out.WriteLine('M dirty.md') }\n"
                "exit 0\n",
            )
            completed = _run_acceptance(
                root,
                report,
                fake_bin,
                {
                    "KB_LLM_API_KEY": "fake-sentinel-key-for-report",
                    "KB_EMBEDDING_API_KEY": "fake-embedding-key-for-report",
                },
            )

            self.assertEqual(1, completed.returncode)
            report_text = report.read_text(encoding="utf-8")
            _assert_absent(self, report_text, "fake-sentinel-key-for-report", "llm key")
            _assert_absent(self, report_text, "fake-embedding-key-for-report", "embedding key")
            _assert_absent(self, report_text, api_key_shape, "provider key shape")
            _assert_absent(self, report_text, bearer_value, "bearer token shape")
            _assert_absent(self, report_text, "supersecret", "password value")
            _assert_absent(self, report_text, "tok123", "token value")
            process_output = completed.stdout + completed.stderr
            _assert_absent(self, process_output, "fake-sentinel-key-for-report", "parent llm key")
            _assert_absent(self, process_output, "fake-embedding-key-for-report", "parent embedding key")
            _assert_absent(self, process_output, "supersecret", "parent password value")
            _assert_absent(self, process_output, "tok123", "parent token value")
            by_name = {entry["name"]: entry for entry in _load_report(report)}
            self.assertEqual("dirty_worktree", by_name["git-status"]["classification"])
            self.assertIn("[redacted]", by_name["lint"]["stderr_summary"])

    def test_acceptance_script_persists_summaries_not_complete_stdout_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "vault"
            root.mkdir()
            report = temp / "acceptance-run.jsonl"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            long_stdout = "stdout-" + ("A" * 700) + "-TAIL_SHOULD_NOT_PERSIST"
            long_stderr = "stderr-" + ("B" * 700) + "-ERRTAIL_SHOULD_NOT_PERSIST"
            _write_ps1(
                fake_bin / "python.ps1",
                f"[Console]::Out.WriteLine('{long_stdout}')\n"
                f"[Console]::Error.WriteLine('{long_stderr}')\n"
                "exit 0\n",
            )
            _write_ps1(fake_bin / "git.ps1", "exit 0\n")

            completed = _run_acceptance(root, report, fake_bin)

            self.assertEqual(0, completed.returncode)
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("stdout_summary", report_text)
            self.assertIn("stderr_summary", report_text)
            self.assertNotIn('"stdout":', report_text)
            self.assertNotIn('"stderr":', report_text)
            self.assertIn("[truncated]", report_text)
            self.assertNotIn("TAIL_SHOULD_NOT_PERSIST", report_text)
            self.assertNotIn("ERRTAIL_SHOULD_NOT_PERSIST", report_text)


if __name__ == "__main__":
    unittest.main()
