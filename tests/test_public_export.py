import os
import re
import subprocess
import tempfile
import tomllib
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = PROJECT_ROOT / "tools" / "create-public-export.ps1"

EXPECTED_PUBLIC_FILES = {
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "pyproject.toml",
    ".github/workflows/ci.yml",
    "docs/assets/social-preview.html",
    "docs/assets/social-preview.png",
    "docs/product/open-source-release.md",
    "kb/cli.py",
    "tests/test_open_source_release.py",
    "tests/test_public_export.py",
    "tools/create-public-export.ps1",
    "tools/run-personal-exobrain-acceptance.ps1",
    "examples/demo-root/meta/kb-manifest.json",
}

EXCLUDED_PUBLIC_PATHS = {
    ".git",
    ".obsidian",
    "raw",
    "sources",
    "wiki",
    "meta",
    "db",
    "backups",
    "reports",
    "inbox",
    "docs/superpowers",
    "tests/test_personal_exobrain_init.py",
}
PRIVATE_VAULT_BASENAME_FRAGMENTS = {
    "\u6211\u7684\u5916\u8111",
}

SECRET_OR_TOKEN_SHAPES = {
    "provider_key_shape": re.compile(
        r"(?<![A-Za-z0-9])" + re.escape("s" + "k-") + r"[A-Za-z0-9][A-Za-z0-9_-]{8,}"
    ),
    "github_token_shape": re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    "aws_access_key_shape": re.compile(r"AKIA[0-9A-Z]{16}"),
    "slack_token_shape": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "bearer_token_shape": re.compile(
        r"(?i)" + "bearer" + r"\s+(?!<|\[)[A-Za-z0-9._~+/-]{8,}"
    ),
    "api_key_assignment_shape": re.compile(
        r"(?i)api[_-]?key\s*=\s*['\"]"
        r"(?!(?:<|\[|fake-|json-|sentinel-|example-|redacted))"
        r"[A-Za-z0-9._~+/-]{16,}['\"]?"
    ),
}

CONCRETE_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_\\/.-])[A-Za-z]:[\\/](?![\\/])")
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".cmd",
    ".cfg",
    ".css",
    ".gitignore",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".lock",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
REPOSITORY_URL_PLACEHOLDER = "<repository" + "-url>"


def run_export(
    output_path: Path,
    repository_url: str | None = None,
    script_path: Path = EXPORT_SCRIPT,
) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-OutputPath",
        str(output_path),
    ]
    if repository_url is not None:
        command.extend(["-RepositoryUrl", repository_url])
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def exported_text_files(export_root: Path) -> list[Path]:
    files = []
    for path in sorted(export_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        data.decode("utf-8")
        files.append(path)
    return files


class PublicExportTests(unittest.TestCase):
    def test_materialized_public_export_can_be_rematerialized(self):
        first_url = "https://github.com/example/local-llm-wiki.git"
        second_url = "https://github.com/other-owner/renamed-wiki.git"

        with tempfile.TemporaryDirectory() as tmpdir:
            first_root = Path(tmpdir) / "first-export"
            second_root = Path(tmpdir) / "second-export"

            first = run_export(first_root, first_url)
            self.assertEqual(0, first.returncode, first.stderr)

            second = run_export(
                second_root,
                second_url,
                script_path=first_root / "tools" / "create-public-export.ps1",
            )

            self.assertEqual(0, second.returncode, second.stderr)
            for rel_path in (
                "README.md",
                "README.zh-CN.md",
                "CONTRIBUTING.md",
                "docs/product/installation.md",
            ):
                text = (second_root / rel_path).read_text(encoding="utf-8")
                with self.subTest(rel_path=rel_path):
                    self.assertIn(second_url, text)
                    self.assertNotIn(first_url, text)
                    self.assertNotIn(REPOSITORY_URL_PLACEHOLDER, text)
            pyproject = tomllib.loads(
                (second_root / "pyproject.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "https://github.com/other-owner/renamed-wiki",
                pyproject["project"]["urls"]["Repository"],
            )

    def test_repository_url_materializes_release_files_without_mutating_source(self):
        repository_url = "https://github.com/example/local-llm-wiki.git"
        canonical_url = "https://github.com/example/local-llm-wiki"
        release_files = [
            Path("README.md"),
            Path("README.zh-CN.md"),
            Path("CONTRIBUTING.md"),
            Path("docs/product/installation.md"),
            Path("pyproject.toml"),
        ]
        source_contents = {
            path: (PROJECT_ROOT / path).read_bytes()
            for path in release_files
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            export_root = Path(tmpdir) / "public-export"

            completed = run_export(export_root, repository_url)

            self.assertEqual(0, completed.returncode, completed.stderr)
            for path in release_files[:4]:
                text = (export_root / path).read_text(encoding="utf-8")
                with self.subTest(path=path):
                    self.assertNotIn(REPOSITORY_URL_PLACEHOLDER, text)
                    self.assertIn(repository_url, text)

            exported_pyproject = tomllib.loads(
                (export_root / "pyproject.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    "Homepage": canonical_url,
                    "Repository": canonical_url,
                    "Issues": f"{canonical_url}/issues",
                },
                exported_pyproject["project"]["urls"],
            )
            for path in exported_text_files(export_root):
                with self.subTest(no_repository_placeholder=path):
                    self.assertNotIn(
                        REPOSITORY_URL_PLACEHOLDER,
                        path.read_text(encoding="utf-8"),
                    )

        for path, before in source_contents.items():
            with self.subTest(source_unchanged=path):
                self.assertEqual(before, (PROJECT_ROOT / path).read_bytes())

    def test_repository_url_rejects_unsafe_or_non_github_values_before_writing(self):
        invalid_urls = [
            "http://github.com/example/local-llm-wiki",
            "https://user@example.com/repository",
            "https://user@github.com/example/local-llm-wiki",
            "https://github.com/example/local-llm-wiki?token=value",
            "https://github.com/example/local-llm-wiki#fragment",
            "https://gitlab.com/example/local-llm-wiki",
            "https://github.com/example/../local-llm-wiki",
            "https://github.com/example/local-llm-wiki/extra",
            "C" + r":\repositories\local-llm-wiki",
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            for index, repository_url in enumerate(invalid_urls):
                export_root = Path(tmpdir) / f"public-export-{index}"

                completed = run_export(export_root, repository_url)

                with self.subTest(repository_url=repository_url):
                    self.assertNotEqual(0, completed.returncode)
                    self.assertFalse(export_root.exists())

    def test_public_export_snapshot_includes_public_files_excludes_private_boundaries_and_scans_all_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            export_root = Path(tmpdir) / "public-export"

            completed = run_export(export_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(export_root.is_dir())

            for rel_path in EXPECTED_PUBLIC_FILES:
                with self.subTest(included=rel_path):
                    self.assertTrue((export_root / rel_path).is_file(), rel_path)

            for rel_path in EXCLUDED_PUBLIC_PATHS:
                with self.subTest(excluded=rel_path):
                    self.assertFalse((export_root / rel_path).exists(), rel_path)

            text_files = exported_text_files(export_root)
            scanned = {
                path.relative_to(export_root).as_posix()
                for path in text_files
            }
            for rel_path in (
                "kb/cli.py",
                "tests/test_backup.py",
                "tools/run-productization-acceptance.ps1",
                "examples/demo-root/raw/synthetic-demo-source.md",
            ):
                with self.subTest(scanned=rel_path):
                    self.assertIn(rel_path, scanned)
            self.assertGreater(len(text_files), 50)

            failures = []
            for path in text_files:
                text = path.read_text(encoding="utf-8")
                rel_path = path.relative_to(export_root).as_posix()
                if CONCRETE_DRIVE_PATH.search(text):
                    failures.append({"path": rel_path, "class": "concrete_drive_path"})
                for label, pattern in SECRET_OR_TOKEN_SHAPES.items():
                    if pattern.search(text):
                        failures.append({"path": rel_path, "class": label})
                for fragment in PRIVATE_VAULT_BASENAME_FRAGMENTS:
                    if fragment in text:
                        failures.append(
                            {"path": rel_path, "class": "private_vault_basename"}
                        )

            self.assertEqual(
                [],
                failures,
                "Exported text scan found blocked classes without exposing matched values.",
            )

    def test_public_export_refuses_existing_output_and_source_repo_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_output = Path(tmpdir) / "already-present"
            existing_output.mkdir()

            existing = run_export(existing_output)

            self.assertNotEqual(0, existing.returncode)
            self.assertTrue(existing_output.is_dir())

        inside_output = PROJECT_ROOT / f".public-export-inside-source-{uuid.uuid4().hex}"
        try:
            inside = run_export(inside_output)

            self.assertNotEqual(0, inside.returncode)
            self.assertFalse(inside_output.exists())
        finally:
            if inside_output.exists():
                resolved = inside_output.resolve()
                if resolved.parent == PROJECT_ROOT.resolve() and resolved.name.startswith(
                    ".public-export-inside-source-"
                ):
                    for child in sorted(resolved.rglob("*"), reverse=True):
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    resolved.rmdir()

    def test_public_export_excludes_ignored_local_secret_files(self):
        local_secret_files = [
            PROJECT_ROOT / ".env",
            PROJECT_ROOT / ".env.local",
            PROJECT_ROOT / "local-test.key",
            PROJECT_ROOT / "local-test.pem",
        ]
        for path in local_secret_files:
            self.assertFalse(path.exists(), f"test fixture path already exists: {path}")

        try:
            for path in local_secret_files:
                path.write_text("local-only-secret-material\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as tmpdir:
                export_root = Path(tmpdir) / "public-export"

                completed = run_export(export_root)

                self.assertEqual(0, completed.returncode, completed.stderr)
                for path in local_secret_files:
                    with self.subTest(excluded=path.name):
                        self.assertFalse((export_root / path.name).exists())
        finally:
            for path in local_secret_files:
                if path.exists():
                    path.unlink()

    def test_public_export_skips_reparse_point_files(self):
        link_path = PROJECT_ROOT / "public-export-test-link.txt"
        if os.name != "nt":
            self.skipTest("Windows reparse point coverage only")
        self.assertFalse(link_path.exists(), f"test fixture path already exists: {link_path}")

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "outside-target.txt"
            target_path.write_text("outside content\n", encoding="utf-8")
            try:
                completed_link = subprocess.run(
                    ["cmd", "/c", "mklink", str(link_path), str(target_path)],
                    cwd=PROJECT_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if completed_link.returncode != 0:
                    self.skipTest(
                        "mklink unavailable for this account: "
                        + completed_link.stderr
                    )

                export_root = Path(tmpdir) / "public-export"
                completed = run_export(export_root)

                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertFalse((export_root / link_path.name).exists())
            finally:
                if link_path.exists():
                    link_path.unlink()

    def test_open_source_release_doc_declares_private_history_is_not_publishable(self):
        doc_path = PROJECT_ROOT / "docs" / "product" / "open-source-release.md"
        self.assertTrue(doc_path.is_file(), "open-source-release guidance is missing")
        text = doc_path.read_text(encoding="utf-8")

        required_phrases = [
            "The private development Git history is not a public release artifact",
            "Do not push the private repository history to a public remote",
            "clean snapshot",
            "new public repository",
            "squash import",
            "Run the public export and scan checks before publication",
            "Do not point release checks at real user vaults",
            "Do not call real providers",
        ]
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
