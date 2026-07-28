import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "run-productization-acceptance.ps1"
PROVIDER_ENV_NAMES = [
    "KB_LLM_BASE_URL",
    "KB_LLM_MODEL",
    "KB_LLM_API_KEY",
    "KB_EMBEDDING_BASE_URL",
    "KB_EMBEDDING_MODEL",
    "KB_EMBEDDING_API_KEY",
]
OFFLINE_ENV_VALUES = {
    "KB_LLM_BASE_URL": "http://127.0.0.1:9/kb-acceptance-offline-llm",
    "KB_LLM_MODEL": "kb-acceptance-offline-llm",
    "KB_LLM_API_KEY": "kb-acceptance-offline-key",
    "KB_EMBEDDING_BASE_URL": "",
    "KB_EMBEDDING_MODEL": "",
    "KB_EMBEDDING_API_KEY": "",
}


def _write_ps1(path: Path, body: str) -> None:
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n" + body,
        encoding="utf-8-sig",
    )


def _run_acceptance(fake_bin: Path, args=None, env_updates=None, script_path=None, cwd=None):
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    if env_updates:
        env.update(env_updates)
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path or SCRIPT_PATH),
        "-PythonCommand",
        str(fake_bin / "python.ps1"),
        "-GitCommand",
        str(fake_bin / "git.ps1"),
    ]
    if args:
        command.extend(str(arg) for arg in args)
    return subprocess.run(
        command,
        cwd=cwd or PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _load_report(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _successful_python_script() -> str:
    return (
        "$joined = $args -join ' '\n"
        'Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value ("PY " + $joined)\n'
        '$providerLine = "ENV " + ($env:KB_LLM_BASE_URL, $env:KB_LLM_MODEL, $env:KB_LLM_API_KEY, $env:KB_EMBEDDING_BASE_URL, $env:KB_EMBEDDING_MODEL, $env:KB_EMBEDDING_API_KEY -join "|")\n'
        "Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value $providerLine\n"
        '$extraLine = "EXTRAENV " + ($env:KB_LLM_PROVIDER_SECRET, $env:KB_EMBEDDING_PROVIDER_SECRET -join "|")\n'
        "Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value $extraLine\n"
        "if ($joined.Contains('llm-preflight')) {\n"
        "  if ($env:KB_LLM_BASE_URL -ne 'http://127.0.0.1:9/kb-acceptance-offline-llm' -or $env:KB_LLM_MODEL -ne 'kb-acceptance-offline-llm' -or $env:KB_LLM_API_KEY -ne 'kb-acceptance-offline-key') { [Console]::Error.WriteLine('missing_config'); exit 9 }\n"
        "}\n"
        "$meta = Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta'\n"
        "New-Item -ItemType Directory -Force -Path $meta | Out-Null\n"
        "Set-Content -LiteralPath (Join-Path $meta 'quality-report.md') -Encoding UTF8 -Value 'governed'\n"
        "Write-Output 'ok'\n"
        "exit 0\n"
    )


def _successful_git_script() -> str:
    return (
        "$joined = $args -join ' '\n"
        'Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value ("GIT " + $joined)\n'
        "if ($args.Count -gt 2 -and $args[2] -eq 'add') {\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\evals\\retrieval-benchmark.jsonl')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'BENCHMARK_PRESENT' }\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\quality-report.md')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'GOVERN_PRESENT' }\n"
        "}\n"
        "exit 0\n"
    )


def _successful_git_script_with_untracked_markdown(relative_path: str) -> str:
    cmd_path = relative_path.replace("/", "\\")
    return (
        "$joined = $args -join ' '\n"
        'Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value ("GIT " + $joined)\n'
        f"if ($joined.Contains('ls-files')) {{ Write-Output '{relative_path}' }}\n"
        "if ($args.Count -gt 2 -and $args[2] -eq 'add') {\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\evals\\retrieval-benchmark.jsonl')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'BENCHMARK_PRESENT' }\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\quality-report.md')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'GOVERN_PRESENT' }\n"
        "}\n"
        f"if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_TEMP_REPO '{cmd_path}')) {{ Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'UNTRACKED_MARKDOWN_PRESENT' }}\n"
        "exit 0\n"
    )


def _successful_git_script_with_staged_markdown(relative_path: str) -> str:
    cmd_path = relative_path.replace("/", "\\")
    return (
        "$joined = $args -join ' '\n"
        'Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value ("GIT " + $joined)\n'
        f"if ($joined.Contains('diff') -and $joined.Contains('--cached')) {{ Write-Output '{relative_path}' }}\n"
        "if ($args.Count -gt 2 -and $args[2] -eq 'add') {\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\evals\\retrieval-benchmark.jsonl')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'BENCHMARK_PRESENT' }\n"
        "  if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_FAKE_ROOT 'meta\\quality-report.md')) { Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'GOVERN_PRESENT' }\n"
        "}\n"
        f"if (Test-Path -LiteralPath (Join-Path $env:KB_ACCEPTANCE_TEMP_REPO '{cmd_path}')) {{ Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value 'STAGED_MARKDOWN_PRESENT' }}\n"
        "exit 0\n"
    )


class ProductizationAcceptanceScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows no-console encoding coverage")
    def test_fake_command_log_remains_utf8_without_console(self):
        target = (
            "tests.test_productization_acceptance."
            "ProductizationAcceptanceScriptTests."
            "test_clean_fake_python_and_git_run_exits_zero_and_writes_jsonl_report"
        )

        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", target, "-v"],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_script_declares_safe_parameters_offline_scrubbers_and_required_commands(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('[string]$Root = ""', script)
        self.assertIn('[string]$ReportPath = ""', script)
        self.assertIn('[string]$PythonCommand = ""', script)
        self.assertIn('[string]$GitCommand = ""', script)
        self.assertIn("[switch]$Online", script)
        self.assertIn("function Get-Sha256Hex", script)
        self.assertNotIn("Get-FileHash", script)
        self.assertIn("function Invoke-CommandOverrideProcess", script)
        self.assertIn("RedirectStandardOutput = $true", script)
        self.assertIn("RedirectStandardError = $true", script)
        self.assertIn("$capturedOutput = @(& $Command *>&1)", script)
        self.assertNotIn("New-TemporaryFile", script)
        self.assertIsNone(re.search(r'\[string\]\$Root\s*=\s*"[A-Za-z]:\\', script))
        self.assertNotIn('$ErrorActionPreference = "Stop"', script)
        self.assertIn("function Set-OfflineProviderEnvironment", script)
        self.assertIn("function Redact-Text", script)
        self.assertIn("unsafe_report_path", script)
        self.assertIn("dirty_worktree", script)
        self.assertIn('$runOutputDir = Join-Path (Split-Path -Parent $Root)', script)
        self.assertIn('$sourcePath = Join-Path $runOutputDir "productization-smoke-source.md"', script)
        self.assertIn('$backupPath = Join-Path $runOutputDir "acceptance-backup.zip"', script)
        self.assertNotIn('$sourcePath = Join-Path $Root', script)
        self.assertNotIn('$backupPath = Join-Path $Root', script)
        for name in PROVIDER_ENV_NAMES:
            self.assertIn(name, script)
        for value in OFFLINE_ENV_VALUES.values():
            self.assertIn(value, script)

        required_commands = [
            "python -B -m kb init --root $Root",
            "python -B -m kb ingest $sourcePath --root $Root",
            "python -B -m kb rebuild-index --root $Root",
            "python -B -m kb schema-check --root $Root --json",
            "python -B -m kb lint --root $Root",
            "python -B -m kb status --root $Root",
            "python -B -m kb govern --root $Root",
            "git -C $Root init",
            "git -C $Root add .",
            "git -C $Root -c user.name=ProductizationAcceptance -c user.email=productization-acceptance@example.invalid commit -m",
            "python -B -m unittest discover -s tests -v",
            "python -B -m kb doctor --root $Root --json",
            "python -B -m kb lock-check --root $Root --json",
            "python -B -m kb backup --root $Root --output $backupPath",
            "python -B -m kb restore --backup $backupPath --root $restoreRoot",
            "python -B -m kb migrate-check --source $Root --restored $restoreRoot --json",
            'python -B -m kb llm-preflight --root $Root --query "productization smoke" --title "Productization Smoke" --offline --json',
            'python -B -m kb eval-search --root $Root --benchmark "meta/evals/retrieval-benchmark.jsonl" --json',
            "python -B -m kb gateway-check --root $Root --json",
            "python -B -m kb product-console --root $Root --json",
            "git diff --check",
            "Invoke-DocsWhitespaceQa",
            "git -C $Root diff --check",
            "git -C $Root status --short",
        ]
        for command in required_commands:
            self.assertIn(command, script)

    def test_deterministic_setup_order_is_visible_in_script_text(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        ordered_needles = [
            'Invoke-AcceptanceStep "setup-init" { python -B -m kb init --root $Root }',
            'Write-MinimalSmokeSource $sourcePath',
            'Invoke-AcceptanceStep "setup-ingest" { python -B -m kb ingest $sourcePath --root $Root }',
            'Invoke-AcceptanceStep "setup-rebuild-index" { python -B -m kb rebuild-index --root $Root }',
            'Write-RetrievalBenchmark $benchmarkPath $sourceId',
            'Invoke-AcceptanceStep "setup-schema-check" { python -B -m kb schema-check --root $Root --json }',
            'Invoke-AcceptanceStep "setup-lint" { python -B -m kb lint --root $Root }',
            'Invoke-AcceptanceStep "setup-status" { python -B -m kb status --root $Root }',
            'Invoke-AcceptanceStep "setup-govern" { python -B -m kb govern --root $Root }',
            'Invoke-AcceptanceStep "git-init" { git -C $Root init }',
            'Invoke-AcceptanceStep "git-add-baseline" { git -C $Root add . }',
            'Invoke-AcceptanceStep "git-commit-baseline"',
        ]
        position = 0
        for needle in ordered_needles:
            found = script.find(needle, position)
            self.assertNotEqual(-1, found, needle)
            position = found + len(needle)

    def test_offline_default_uses_temp_root_and_fake_local_provider_env_for_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            fake_root_marker = temp / "unused-real-vault-marker"
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-ReportPath", report],
                {
                    "KB_ACCEPTANCE_FAKE_LOG": str(log),
                    "KB_ACCEPTANCE_FAKE_ROOT": str(fake_root_marker),
                    "KB_LLM_BASE_URL": "https://real-llm.invalid",
                    "KB_LLM_MODEL": "real-model",
                    "KB_LLM_API_KEY": "fake-sentinel-llm-key",
                    "KB_LLM_PROVIDER_SECRET": "fake-sentinel-llm-provider-secret",
                    "KB_EMBEDDING_BASE_URL": "https://real-embedding.invalid",
                    "KB_EMBEDDING_MODEL": "real-embedding-model",
                    "KB_EMBEDDING_API_KEY": "fake-sentinel-embedding-key",
                    "KB_EMBEDDING_PROVIDER_SECRET": "fake-sentinel-embedding-provider-secret",
                },
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            log_text = log.read_text(encoding="utf-8-sig")
            self.assertIn("--root", log_text)
            for sentinel in [
                "https://real-llm.invalid",
                "real-model",
                "fake-sentinel-llm-key",
                "fake-sentinel-llm-provider-secret",
                "https://real-embedding.invalid",
                "real-embedding-model",
                "fake-sentinel-embedding-key",
                "fake-sentinel-embedding-provider-secret",
            ]:
                self.assertNotIn(sentinel, log_text)
            expected_env_line = "ENV " + "|".join(OFFLINE_ENV_VALUES[name] for name in PROVIDER_ENV_NAMES)
            self.assertIn(expected_env_line, log_text)
            self.assertIn("EXTRAENV |", log_text)

    def test_backup_output_path_is_outside_synthetic_root_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {"KB_ACCEPTANCE_FAKE_LOG": str(log), "KB_ACCEPTANCE_FAKE_ROOT": str(root)},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            backup_line = next(
                line
                for line in log.read_text(encoding="utf-8-sig").splitlines()
                if "PY -B -m kb backup " in line
            )
            output_marker = " --output "
            self.assertIn(output_marker, backup_line)
            backup_path = Path(backup_line.split(output_marker, 1)[1])
            self.assertFalse(
                backup_path.resolve().is_relative_to(root.resolve()),
                f"backup path must be outside root: {backup_path}",
            )

    def test_clean_fake_python_and_git_run_exits_zero_and_writes_jsonl_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {"KB_ACCEPTANCE_FAKE_LOG": str(log), "KB_ACCEPTANCE_FAKE_ROOT": str(root)},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            entries = _load_report(report)
            self.assertGreater(len(entries), 10)
            for entry in entries:
                self.assertEqual(
                    {"name", "exit_code", "classification", "stdout_summary", "stderr_summary", "no_write_unchanged"},
                    set(entry.keys()),
                )
            self.assertFalse([entry for entry in entries if entry["exit_code"] != 0])
            log_text = log.read_text(encoding="utf-8-sig")
            self.assertIn("BENCHMARK_PRESENT", log_text)
            self.assertIn("GOVERN_PRESENT", log_text)

    def test_runtime_setup_order_baselines_generated_files_before_phase_b(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {"KB_ACCEPTANCE_FAKE_LOG": str(log), "KB_ACCEPTANCE_FAKE_ROOT": str(root)},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            lines = [
                line
                for line in log.read_text(encoding="utf-8-sig").splitlines()
                if line.startswith(("PY ", "GIT "))
            ]
            ordered_needles = [
                "PY -B -m kb init --root",
                "PY -B -m kb ingest",
                "PY -B -m kb rebuild-index --root",
                "PY -B -m kb schema-check --root",
                "PY -B -m kb lint --root",
                "PY -B -m kb status --root",
                "PY -B -m kb govern --root",
                "GIT -C",
                "GIT -C",
                "GIT -C",
                "PY -B -m unittest discover -s tests -v",
            ]
            cursor = 0
            for needle in ordered_needles:
                match_index = next(
                    (index for index in range(cursor, len(lines)) if needle in lines[index]),
                    -1,
                )
                self.assertNotEqual(-1, match_index, needle)
                cursor = match_index + 1
            self.assertIn(" init", lines[7])
            self.assertIn(" add .", lines[8])
            self.assertIn(" commit", lines[9])

    def test_untracked_markdown_outside_product_docs_gets_explicit_whitespace_qa(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            temp_repo = temp / "repo"
            script_copy = temp_repo / "tools" / "run-productization-acceptance.ps1"
            untracked_relative = "docs/superpowers/plans/untracked-plan.md"
            untracked_doc = temp_repo / untracked_relative
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            script_copy.parent.mkdir(parents=True)
            script_copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            untracked_doc.parent.mkdir(parents=True)
            untracked_doc.write_text("# Plan\nline with trailing space ", encoding="utf-8")
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(
                fake_bin / "git.ps1",
                _successful_git_script_with_untracked_markdown(untracked_relative),
            )

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {
                    "KB_ACCEPTANCE_FAKE_LOG": str(log),
                    "KB_ACCEPTANCE_FAKE_ROOT": str(root),
                    "KB_ACCEPTANCE_TEMP_REPO": str(temp_repo),
                },
                script_path=script_copy,
                cwd=temp_repo,
            )

            self.assertEqual(1, completed.returncode)
            by_name = {entry["name"]: entry for entry in _load_report(report)}
            docs_entry = by_name["docs-whitespace-qa"]
            self.assertEqual("docs_whitespace_failed", docs_entry["classification"])
            normalized_summary = docs_entry["stderr_summary"].replace("\n", "")
            self.assertIn("docs\\superpowers\\plans\\untracked-plan.md", normalized_summary)
            self.assertIn("missing_final_newline", normalized_summary)
            self.assertIn("trailing_whitespace", normalized_summary)

    def test_staged_new_markdown_outside_product_docs_gets_explicit_whitespace_qa(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            temp_repo = temp / "repo"
            script_copy = temp_repo / "tools" / "run-productization-acceptance.ps1"
            staged_relative = "docs/superpowers/specs/staged-spec.md"
            staged_doc = temp_repo / staged_relative
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            script_copy.parent.mkdir(parents=True)
            script_copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            staged_doc.parent.mkdir(parents=True)
            staged_doc.write_text("# Spec\nline with trailing space ", encoding="utf-8")
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(
                fake_bin / "git.ps1",
                _successful_git_script_with_staged_markdown(staged_relative),
            )

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {
                    "KB_ACCEPTANCE_FAKE_LOG": str(log),
                    "KB_ACCEPTANCE_FAKE_ROOT": str(root),
                    "KB_ACCEPTANCE_TEMP_REPO": str(temp_repo),
                },
                script_path=script_copy,
                cwd=temp_repo,
            )

            self.assertEqual(1, completed.returncode)
            by_name = {entry["name"]: entry for entry in _load_report(report)}
            docs_entry = by_name["docs-whitespace-qa"]
            self.assertEqual("docs_whitespace_failed", docs_entry["classification"])
            normalized_summary = docs_entry["stderr_summary"].replace("\n", "")
            self.assertIn("docs\\superpowers\\specs\\staged-spec.md", normalized_summary)
            self.assertIn("missing_final_newline", normalized_summary)
            self.assertIn("trailing_whitespace", normalized_summary)

    def test_final_non_empty_git_status_is_dirty_worktree_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(
                fake_bin / "git.ps1",
                "$joined = $args -join ' '\n"
                'Add-Content -LiteralPath $env:KB_ACCEPTANCE_FAKE_LOG -Encoding UTF8 -Value ("GIT " + $joined)\n'
                "if ($args.Count -gt 2 -and $args[2] -eq 'status') { [Console]::Out.WriteLine('M dirty.md') }\n"
                "exit 0\n",
            )

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {"KB_ACCEPTANCE_FAKE_LOG": str(log), "KB_ACCEPTANCE_FAKE_ROOT": str(root)},
            )

            self.assertEqual(1, completed.returncode)
            by_name = {entry["name"]: entry for entry in _load_report(report)}
            self.assertEqual("dirty_worktree", by_name["git-status"]["classification"])

    def test_report_redacts_secret_shapes_configured_values_and_truncates_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            report = temp / "acceptance-run.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            long_tail = "A" * 700 + "-TAIL_SHOULD_NOT_PERSIST"
            api_key_shape = "s" + "k-testsecret"
            bearer_value = "abc" + ".def" + ".ghi"
            inline_key_value = "abc" + "123"
            private_prompt = "private-prompt-" + "fragment"
            private_source = "private-source-" + "fragment"
            private_response = "private-response-" + "fragment"
            json_api_key = "json-api-key-" + "fragment"
            json_prompt = "json prompt " + "fragment"
            json_source = "json source " + "fragment"
            json_response = "json response " + "fragment"
            long_json_prompt = "long json prompt " + ("x" * 220) + " fragment"
            multiline_prompt = "multi word prompt " + "fragment"
            multiline_source = "multi word source " + "fragment"
            multiline_response = "multi word response " + "fragment"
            long_unquoted_prompt = "long unquoted prompt " + ("y" * 220) + "TAIL_UNQUOTED_SHOULD_NOT_PERSIST"
            password_value = "super" + "secret"
            token_value = "tok" + "123"
            llm_env_value = "fake-sentinel-llm-" + "key"
            embedding_env_value = "fake-sentinel-embedding-" + "key"
            _write_ps1(
                fake_bin / "python.ps1",
                f"[Console]::Out.WriteLine('longcolon prompt: {long_unquoted_prompt}')\n"
                f"[Console]::Out.WriteLine('safe-long {long_tail}')\n"
                f"""[Console]::Out.WriteLine('longjson {{"prompt":"{long_json_prompt}"}}')\n"""
                f'[Console]::Out.WriteLine("stdout $env:KB_LLM_API_KEY {api_key_shape} Authorization: Bearer {bearer_value} api_key={inline_key_value} prompt={private_prompt} source_text={private_source} provider_response={private_response}")\n'
                f"""[Console]::Out.WriteLine('json {{"api_key":"{json_api_key}","prompt":"{json_prompt}","source_text":"{json_source}","provider_response":"{json_response}"}}')\n"""
                f"[Console]::Out.WriteLine('colon prompt: {multiline_prompt} source_text: {multiline_source} provider_response: {multiline_response}')\n"
                f'[Console]::Error.WriteLine("stderr $env:KB_EMBEDDING_API_KEY password={password_value} token={token_value} {long_tail}")\n'
                "exit 0\n",
            )
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", report],
                {
                    "KB_ACCEPTANCE_FAKE_LOG": str(log),
                    "KB_ACCEPTANCE_FAKE_ROOT": str(root),
                    "KB_LLM_API_KEY": llm_env_value,
                    "KB_EMBEDDING_API_KEY": embedding_env_value,
                },
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            report_text = report.read_text(encoding="utf-8")
            for secret in [
                llm_env_value,
                embedding_env_value,
                api_key_shape,
                bearer_value,
                inline_key_value,
                private_prompt,
                private_source,
                private_response,
                json_api_key,
                json_prompt,
                json_source,
                json_response,
                long_json_prompt[:160],
                multiline_prompt,
                multiline_source,
                multiline_response,
                long_unquoted_prompt[:160],
                "TAIL_UNQUOTED_SHOULD_NOT_PERSIST",
                password_value,
                token_value,
                "TAIL_SHOULD_NOT_PERSIST",
            ]:
                self.assertNotIn(secret, report_text)
                self.assertNotIn(secret, completed.stdout + completed.stderr)
            self.assertIn("[redacted]", report_text)
            self.assertIn("[truncated]", report_text)
            self.assertTrue(
                any(
                    "[redacted]" in entry["stderr_summary"]
                    for entry in _load_report(report)
                )
            )
            self.assertNotIn('"stdout":', report_text)
            self.assertNotIn('"stderr":', report_text)

    def test_report_path_traversal_is_rejected_without_writing_escape_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "synthetic-root"
            escaped = temp / "reports" / ".." / "escape.jsonl"
            log = temp / "calls.log"
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _write_ps1(fake_bin / "python.ps1", _successful_python_script())
            _write_ps1(fake_bin / "git.ps1", _successful_git_script())

            completed = _run_acceptance(
                fake_bin,
                ["-Root", root, "-ReportPath", escaped],
                {"KB_ACCEPTANCE_FAKE_LOG": str(log), "KB_ACCEPTANCE_FAKE_ROOT": str(root)},
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("unsafe_report_path", completed.stdout + completed.stderr)
            self.assertFalse((temp / "escape.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
