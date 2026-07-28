import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
QUICKSTART = PROJECT_ROOT / "docs" / "product" / "quickstart-zh.md"
DEMO_DOC = PROJECT_ROOT / "docs" / "product" / "first-run-demo.md"
COMMAND_GUIDE = PROJECT_ROOT / "docs" / "product" / "command-guide.md"
DEMO_SCRIPT = PROJECT_ROOT / "tools" / "run-demo.ps1"
PNG_ASSETS = [
    PROJECT_ROOT / "docs" / "product" / "assets" / "first-run-demo.png",
    PROJECT_ROOT / "docs" / "product" / "assets" / "product-console-demo.png",
]

DEMO_ROOT_TEXT = "examples/demo-root"
REQUIRED_COMMANDS = [
    "init",
    "doctor",
    "schema-check",
    "product-console",
    "ingest",
    "search",
    "answer",
    "llm-preflight",
    "validate-draft",
    "publish-draft",
    "backup",
    "restore",
    "migrate-check",
    "capture-candidate",
    "suggest-topics",
    "daily-workflow",
    "benchmark-add",
    "exobrain-check",
]
QUICKSTART_PHRASES = [
    "本地优先",
    "AI/LLM output is never a fact source",
    "草稿",
    "稳定内容",
    "validate-draft",
    "publish-draft",
    "默认不会调用云端或模型提供商",
    "不要把演示命令指向真实库",
    DEMO_ROOT_TEXT,
]
DEMO_DOC_PHRASES = [
    DEMO_ROOT_TEXT,
    "tools/run-demo.ps1",
    "product-console",
    "doctor",
    "schema-check",
    "search",
    "answer",
    "不代表真实提供商可用",
]
HIGH_RISK_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+(?!<)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"]?(?!<)[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"(?i)(users|administrator|appdata|localappdata)"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])"),
]


def read_text(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"{path.relative_to(PROJECT_ROOT)} is missing")
    return path.read_text(encoding="utf-8")


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path.relative_to(PROJECT_ROOT)} is not a PNG")
    if data[12:16] != b"IHDR":
        raise AssertionError(f"{path.relative_to(PROJECT_ROOT)} is missing IHDR")
    return struct.unpack(">II", data[16:24])


def powershell_executable() -> str:
    command = shutil.which("powershell") or shutil.which("pwsh")
    if not command:
        raise AssertionError("PowerShell is required for tools/run-demo.ps1 smoke tests")
    return command


class FirstRunDemoTests(unittest.TestCase):
    def test_readme_links_to_chinese_quickstart_and_demo_script(self):
        readme = read_text(README)

        self.assertIn("[中文快速开始](docs/product/quickstart-zh.md)", readme)
        self.assertIn("[首次运行演示](docs/product/first-run-demo.md)", readme)
        self.assertIn("[命令选择指南](docs/product/command-guide.md)", readme)
        self.assertIn("tools/run-demo.ps1", readme)
        self.assertIn(DEMO_ROOT_TEXT, readme)

    def test_quickstart_documents_safe_first_run_boundaries(self):
        quickstart = read_text(QUICKSTART)

        for phrase in QUICKSTART_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, quickstart)
        self.assertIn("docs/product/assets/first-run-demo.png", quickstart)
        self.assertIn("command-guide.md", quickstart)

    def test_command_guide_covers_required_user_intents_and_links_back(self):
        guide = read_text(COMMAND_GUIDE)

        for command in REQUIRED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(f"python -B -m kb {command}", guide)
        for intent in ("导入", "搜索", "草稿", "验证", "发布", "备份", "恢复", "健康检查"):
            with self.subTest(intent=intent):
                self.assertIn(intent, guide)
        self.assertIn("quickstart-zh.md", guide)
        self.assertIn("first-run-demo.md", guide)

    def test_first_run_demo_walkthrough_is_synthetic_local_and_bounded(self):
        walkthrough = read_text(DEMO_DOC)

        for phrase in DEMO_DOC_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, walkthrough)
        self.assertIn("docs/product/assets/product-console-demo.png", walkthrough)
        self.assertNotIn("--online", walkthrough)

    def test_demo_script_static_safety_boundaries(self):
        script = read_text(DEMO_SCRIPT)

        self.assertIn('param(', script)
        self.assertIn('examples\\demo-root', script)
        self.assertIn("AllowCustomRoot", script)
        self.assertIn("APPDATA", script)
        self.assertIn("LOCALAPPDATA", script)
        self.assertIn("KB_LLM_", script)
        self.assertIn("KB_EMBEDDING_", script)
        self.assertIn("init-temp-root", script)
        self.assertIn("ingest-source-$number", script)
        self.assertIn("capture-candidate", script)
        self.assertIn("review-candidate", script)
        self.assertIn("publish-memory", script)
        self.assertIn("validate-draft", script)
        self.assertIn("publish-draft", script)
        self.assertIn("trust-report", script)
        self.assertIn("govern", script)
        self.assertIn("backup", script)
        self.assertIn("restore", script)
        self.assertIn("migrate-check", script)
        self.assertNotIn("--online", script)

    def test_demo_script_smoke_writes_redacted_report_with_isolated_env(self):
        sentinel = "sk-" + "first-run-demo-sentinel-" + ("0" * 12)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            report = base / "reports" / "demo-report.json"
            env = os.environ.copy()
            env.update(
                {
                    "APPDATA": str(base / "parent-appdata"),
                    "LOCALAPPDATA": str(base / "parent-localappdata"),
                    "KB_LLM_API_KEY": sentinel,
                    "KB_EMBEDDING_API_KEY": sentinel,
                }
            )

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DEMO_SCRIPT),
                    "-ReportPath",
                    str(report),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(report.is_file(), completed.stdout)
            payload_text = report.read_text(encoding="utf-8")
            payload = json.loads(payload_text)
            self.assertEqual("synthetic-demo-story-v1", payload["schema_version"])
            self.assertEqual("<temp-synthetic-root>", payload["demo_root"])
            self.assertEqual("examples/demo-story", payload["source_fixture"])
            self.assertTrue(payload["offline"])
            self.assertFalse(payload["writes_real_user_state"])
            self.assertTrue(payload["provider_environment_cleared"])
            self.assertTrue(payload["no_provider_calls"])
            self.assertTrue(payload["no_real_user_vault"])
            self.assertTrue(payload["redaction_applied"])
            self.assertFalse(payload["tracked_fixtures_mutated"])
            self.assertIn("story_steps", payload)
            command_names = {entry["name"] for entry in payload["story_steps"]}
            self.assertTrue(
                {
                    "init-temp-root",
                    "ingest-source-1",
                    "capture-candidate",
                    "review-candidate",
                    "publish-memory",
                    "validate-draft",
                    "publish-draft",
                    "trust-report",
                    "govern",
                    "backup",
                    "restore",
                    "migrate-check",
                }.issubset(command_names)
            )
            self.assertNotIn(sentinel, payload_text)
            self.assertNotIn(str(base), payload_text)
            for pattern in HIGH_RISK_PATTERNS:
                with self.subTest(pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(payload_text))

    def test_demo_script_refuses_custom_root_without_explicit_switch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "custom-root"
            root.mkdir()
            report = Path(tmpdir) / "report.json"

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DEMO_SCRIPT),
                    "-DemoRoot",
                    str(root),
                    "-ReportPath",
                    str(report),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(report.exists())
            self.assertIn("AllowCustomRoot", completed.stderr + completed.stdout)

    def test_demo_script_refuses_existing_nonempty_custom_root_even_with_switch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "custom-root"
            root.mkdir()
            sentinel = root / "existing.md"
            sentinel.write_text("existing content\n", encoding="utf-8")
            report = Path(tmpdir) / "report.json"

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DEMO_SCRIPT),
                    "-DemoRoot",
                    str(root),
                    "-AllowCustomRoot",
                    "-ReportPath",
                    str(report),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(report.exists())
            self.assertEqual("existing content\n", sentinel.read_text(encoding="utf-8"))
            self.assertIn("empty or missing disposable path", completed.stderr + completed.stdout)

    def test_demo_script_refuses_forbidden_real_vault_path_even_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_name = "".join(chr(code) for code in (0x6211, 0x7684, 0x5916, 0x8111))
            drive_root = "F:" + "\\"
            forbidden_root = drive_root + vault_name
            report = Path(tmpdir) / "report.json"

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DEMO_SCRIPT),
                    "-DemoRoot",
                    forbidden_root,
                    "-AllowCustomRoot",
                    "-ReportPath",
                    str(report),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(report.exists())
            self.assertIn("forbidden real user vault", completed.stderr + completed.stdout)

    def test_forbidden_path_check_does_not_require_f_drive_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_name = "".join(chr(code) for code in (0x6211, 0x7684, 0x5916, 0x8111))
            forbidden_root = "F:" + "\\" + vault_name
            report = Path(tmpdir) / "report.json"
            command = (
                "function global:Join-Path { "
                "param([string]$Path, [string]$ChildPath); "
                "if ($Path -eq 'F:\\') { throw 'simulated missing F drive provider' }; "
                "Microsoft.PowerShell.Management\\Join-Path $Path $ChildPath "
                "}; "
                f"& '{DEMO_SCRIPT}' -DemoRoot '{forbidden_root}' "
                f"-AllowCustomRoot -ReportPath '{report}'"
            )

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(report.exists())
            self.assertIn("forbidden real user vault", completed.stderr + completed.stdout)

    def test_demo_script_does_not_require_get_file_hash_cmdlet(self):
        sentinel = "sk-" + "hash-compat-sentinel-" + ("0" * 12)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            report = base / "reports" / "demo-report.json"
            env = os.environ.copy()
            env.update(
                {
                    "APPDATA": str(base / "parent-appdata"),
                    "LOCALAPPDATA": str(base / "parent-localappdata"),
                    "KB_LLM_API_KEY": sentinel,
                    "KB_EMBEDDING_API_KEY": sentinel,
                }
            )
            command = (
                "function global:Get-FileHash { "
                "throw 'simulated unavailable Get-FileHash cmdlet' "
                "}; "
                f"& '{DEMO_SCRIPT}' -ReportPath '{report}'"
            )

            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(report.is_file(), completed.stdout)
            self.assertNotIn(sentinel, report.read_text(encoding="utf-8"))

    def test_png_assets_are_valid_nonempty_and_sanitized(self):
        for path in PNG_ASSETS:
            with self.subTest(path=path.relative_to(PROJECT_ROOT).as_posix()):
                self.assertTrue(path.is_file(), f"{path} is missing")
                self.assertGreater(path.stat().st_size, 1024)
                width, height = png_dimensions(path)
                self.assertGreaterEqual(width, 640)
                self.assertGreaterEqual(height, 360)
                data = path.read_bytes()
                for marker in (
                    b"APPDATA",
                    b"LOCALAPPDATA",
                    b"Users",
                    b"Administrator",
                    b"KB_LLM_API_KEY",
                    b"KB_EMBEDDING_API_KEY",
                    b"sk-",
                    b"Bearer ",
                ):
                    self.assertNotIn(marker, data)


if __name__ == "__main__":
    unittest.main()
