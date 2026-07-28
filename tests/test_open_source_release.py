import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_DOCS = [
    "docs/product/installation.md",
    "docs/product/configuration.md",
    "docs/product/backup-restore-migration.md",
    "docs/product/privacy-and-secrets.md",
    "docs/product/provider-preflight.md",
    "docs/product/open-source-release.md",
    "docs/product/roadmap.md",
    "docs/product/release-checklist.md",
]
RELEASE_BOUNDARY_DOCS = [
    "docs/product/installation.md",
    "docs/product/open-source-release.md",
    "docs/product/roadmap.md",
    "docs/product/release-checklist.md",
]
PUBLIC_TEXT_FILES = [
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "examples/README.md",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    *PRODUCT_DOCS,
]
ROOT_DATA_DIRS = (".obsidian", "raw", "sources", "wiki", "meta")
SECRET_SHAPE_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+(?!<)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"]?(?!<)[A-Za-z0-9._~+/-]{12,}"),
]
ABSOLUTE_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])")


def read_text(rel_path: str) -> str:
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        raise AssertionError(f"{rel_path} is missing")
    return path.read_text(encoding="utf-8")


def provider_free_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KB_LLM_") and not key.startswith("KB_EMBEDDING_")
    }


def run_public_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=provider_free_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class OpenSourceReleaseTests(unittest.TestCase):
    def test_license_security_and_readme_publish_public_release_boundaries(self):
        license_text = read_text("LICENSE")
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)

        security = read_text("SECURITY.md")
        for phrase in (
            "Report vulnerabilities privately",
            "Do not include secrets",
            "Do not include private source content",
            "Do not include prompts, full provider responses, or raw source chunks",
            "Synthetic demo data",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, security)

        readme = read_text("README.md")
        for snippet in (
            "[LICENSE](LICENSE)",
            "[SECURITY.md](SECURITY.md)",
            "[Examples](examples/README.md)",
            "[Open Source Release](docs/product/open-source-release.md)",
            "[Roadmap](docs/product/roadmap.md)",
            "[Release Checklist](docs/product/release-checklist.md)",
            "Python 3.11+",
            "python -B -m pip install -e .",
            "python -B -m kb --help",
            "kb --help",
            'python -B -m kb doctor --root "examples/demo-root"',
            'python -B -m kb product-console --root "examples/demo-root" --json',
            'python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open',
            ".\\tools\\run-demo.ps1",
            "python -B -m unittest tests.test_public_export -v",
            "local-first",
            "No cloud or LLM provider is configured or called by default",
            "Real user vault data is not uploaded",
            "Do not point demo commands at a real user vault",
            "AI/LLM output is never a fact source",
            "clean snapshot",
            "private Git history",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, readme)

        for rel_path in RELEASE_BOUNDARY_DOCS:
            with self.subTest(product_doc=rel_path):
                doc_text = read_text(rel_path)
                self.assertIn("Real user vault data is not uploaded", doc_text)
                self.assertIn(
                    "No cloud or LLM provider is configured or called by default",
                    doc_text,
                )

    def test_release_checklist_requires_human_approved_clean_history_publication(self):
        checklist = read_text("docs/product/release-checklist.md")
        for phrase in (
            "clean snapshot",
            "new public repository",
            "squash import",
            "equivalent history-safe boundary",
            "full export scan",
            "local/offline test evidence",
            "explicit human approval before external publication",
            "Do not publish private development Git history",
            "No publication command is authorized by this checklist",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, checklist)

    def test_pyproject_declares_kb_console_script_and_entrypoints_smoke(self):
        pyproject = tomllib.loads(read_text("pyproject.toml"))
        project = pyproject["project"]
        self.assertEqual(
            "kb.cli:main",
            project["scripts"]["kb"],
        )
        self.assertEqual("MIT", project["license"])
        self.assertEqual(["LICENSE"], project["license-files"])
        self.assertEqual(
            [{"name": "Local LLM Wiki contributors"}],
            project["authors"],
        )
        for classifier in (
            "Development Status :: 3 - Alpha",
            "Operating System :: Microsoft :: Windows",
            "Programming Language :: Python :: 3",
            "Programming Language :: Python :: 3.11",
        ):
            with self.subTest(classifier=classifier):
                self.assertIn(classifier, project["classifiers"])
        self.assertGreaterEqual(
            int(pyproject["build-system"]["requires"][0].split(">=", 1)[1]),
            77,
        )

        module_help = run_public_command([sys.executable, "-B", "-m", "kb", "--help"])
        self.assertEqual(0, module_help.returncode, module_help.stderr)
        self.assertIn("usage: kb", module_help.stdout)

        self.assertIsNotNone(
            shutil.which("kb"),
            "Editable install must expose the kb console script before this test runs.",
        )
        script_help = run_public_command(["kb", "--help"])
        self.assertEqual(0, script_help.returncode, script_help.stderr)
        self.assertIn("usage: kb", script_help.stdout)

    def test_ci_workflow_is_offline_local_and_covers_release_smokes(self):
        workflow = read_text(".github/workflows/ci.yml")
        required_snippets = [
            "actions/checkout",
            "actions/setup-python",
            "python-version: \"3.11\"",
            "python -B -m pip install -e .",
            "python -B -m unittest discover -s tests -v",
            "python -B -m kb --help",
            "kb --help",
            "examples/demo-root",
            "python -B -m kb doctor --root",
            "python -B -m kb product-console --root",
            "run-productization-acceptance.ps1",
            "tests.test_public_export",
            "KB_LLM_BASE_URL: \"\"",
            "KB_LLM_API_KEY: \"\"",
            "KB_EMBEDDING_BASE_URL: \"\"",
            "KB_EMBEDDING_API_KEY: \"\"",
        ]
        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, workflow)

        forbidden_snippets = [
            "secrets.",
            "--online",
            "curl ",
            "Invoke-WebRequest",
            "Invoke-RestMethod",
            "KB_LLM_API_KEY: ${{",
            "KB_EMBEDDING_API_KEY: ${{",
        ]
        for snippet in forbidden_snippets:
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, workflow)

    def test_examples_demo_root_is_synthetic_safe_and_cli_smokeable(self):
        demo_root = PROJECT_ROOT / "examples" / "demo-root"
        self.assertTrue((PROJECT_ROOT / "examples" / "README.md").is_file())
        for directory in ("raw", "sources", "wiki", "meta", "inbox", "db"):
            with self.subTest(directory=directory):
                self.assertTrue((demo_root / directory).is_dir())
        self.assertTrue((demo_root / "meta" / "kb-manifest.json").is_file())

        demo_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((PROJECT_ROOT / "examples").rglob("*"))
            if path.is_file()
            and path.suffix.lower() in {".md", ".txt", ".json", ".jsonl", ".gitignore"}
        )
        self.assertIn("synthetic", demo_text.casefold())
        self.assertNotIn("private source", demo_text.casefold())
        self.assertNotIn("provider response", demo_text.casefold())
        self.assertIsNone(ABSOLUTE_DRIVE_PATH.search(demo_text))
        for pattern in SECRET_SHAPE_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(demo_text))

        console = run_public_command(
            [
                sys.executable,
                "-B",
                "-m",
                "kb",
                "product-console",
                "--root",
                "examples/demo-root",
                "--json",
            ]
        )
        self.assertEqual(0, console.returncode, console.stderr)
        console_payload = json.loads(console.stdout)
        self.assertEqual(1, console_payload["schema_version"])
        self.assertTrue(console_payload["root"]["exists"])

        doctor = run_public_command(
            [
                sys.executable,
                "-B",
                "-m",
                "kb",
                "doctor",
                "--root",
                "examples/demo-root",
                "--json",
            ]
        )
        self.assertIn(doctor.returncode, {0, 1}, doctor.stderr)
        self.assertEqual("", doctor.stderr)
        doctor_payload = json.loads(doctor.stdout)
        self.assertIn("checks", doctor_payload)

    def test_root_live_public_data_directories_are_not_release_content(self):
        for directory in ROOT_DATA_DIRS:
            with self.subTest(directory=directory):
                self.assertFalse(
                    (PROJECT_ROOT / directory).exists(),
                    f"{directory} must be absent from the public release boundary; use examples/demo-root instead.",
                )

    def test_public_docs_ci_and_examples_have_no_secret_shapes_or_real_paths(self):
        combined = "\n".join(read_text(rel_path) for rel_path in PUBLIC_TEXT_FILES)
        example_files = [
            path
            for path in sorted((PROJECT_ROOT / "examples").rglob("*"))
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".jsonl"}
        ]
        combined += "\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in example_files
        )

        self.assertIsNone(
            ABSOLUTE_DRIVE_PATH.search(combined),
            "Public docs and examples must use generic quoted paths, not concrete drive paths.",
        )
        for pattern in SECRET_SHAPE_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(combined))
    def test_gitignore_excludes_local_runtime_and_secret_files(self):
        gitignore = read_text(".gitignore")
        for pattern in (
            ".env",
            ".env.*",
            "*.egg-info/",
            "build/",
            "dist/",
            "/.obsidian/",
            "/backups/",
            "/inbox/",
            "/raw/",
            "/reports/",
            "/sources/",
            "/wiki/",
            "/meta/",
            "/db/",
            ".kb/",
            ".kb-*",
            "*.log",
            "db/*.sqlite3",
            "db/*.sqlite3-*",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, gitignore)


if __name__ == "__main__":
    unittest.main()
