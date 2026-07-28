import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PUBLIC_MARKDOWN_FILES = [
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "docs/product/installation.md",
    "docs/product/open-source-release.md",
    "docs/product/roadmap.md",
    "docs/product/release-checklist.md",
]

ISSUE_TEMPLATE_FILES = [
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
]

CHANGED_PUBLIC_FILES = [*PUBLIC_MARKDOWN_FILES, *ISSUE_TEMPLATE_FILES]

SECRET_SHAPE_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+(?!<)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"]?(?!<)[A-Za-z0-9._~+/-]{12,}"),
]

ABSOLUTE_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
GITHUB_REPOSITORY_URL = re.compile(
    r"https://github\.com/"
    r"(?:<owner>|[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9._-]+(?:\.git)?(?:/issues)?"
)

SAFE_INTAKE_WARNING_PHRASES = [
    "Do not include secrets",
    "Do not include private source text",
    "Do not include raw chunks",
    "Do not include prompts",
    "Do not include full provider responses",
    "Do not include concrete private paths",
    "Do not include real vault artifacts",
    "Do not include private Git history",
]

REPOSITORY_URL_PLACEHOLDER = "<repository" + "-url>"

README_REQUIRED_SNIPPETS = [
    "Python 3.11+",
    "python -B -m pip install -e .",
    "python -B -m kb --help",
    "kb --help",
    'python -B -m kb doctor --root "examples/demo-root"',
    'python -B -m kb product-console --root "examples/demo-root" --json',
    'python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open',
    ".\\tools\\run-demo.ps1",
    "[Chinese README](README.zh-CN.md)",
    "[CONTRIBUTING.md](CONTRIBUTING.md)",
    "[Roadmap](docs/product/roadmap.md)",
    "[Release Checklist](docs/product/release-checklist.md)",
    "[Installation](docs/product/installation.md)",
    "[Open Source Release](docs/product/open-source-release.md)",
    "[Privacy and Secrets](docs/product/privacy-and-secrets.md)",
    "[Provider Preflight](docs/product/provider-preflight.md)",
    "local-first",
    "No cloud or LLM provider is configured or called by default",
    "Do not point demo commands at a real user vault",
    "synthetic demo data only",
    "AI/LLM output is never a fact source",
    "stable claims require local evidence",
    "validate-draft",
    "publish-draft",
    "clean snapshot",
    "private Git history",
]

ZH_README_REQUIRED_SNIPPETS = [
    "[English README](README.md)",
    "Python 3.11+",
    "python -B -m pip install -e .",
    "python -B -m kb --help",
    "kb --help",
    'python -B -m kb doctor --root "examples/demo-root"',
    'python -B -m kb product-console --root "examples/demo-root" --json',
    'python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open',
    ".\\tools\\run-demo.ps1",
    "[中文快速开始](docs/product/quickstart-zh.md)",
    "[首次运行演示](docs/product/first-run-demo.md)",
    "[CONTRIBUTING.md](CONTRIBUTING.md)",
    "[Roadmap](docs/product/roadmap.md)",
    "[Release Checklist](docs/product/release-checklist.md)",
    "[Installation](docs/product/installation.md)",
    "[Open Source Release](docs/product/open-source-release.md)",
    "本地优先",
    "默认不会配置或调用云端/LLM provider",
    "不要把演示命令指向真实用户库",
    "仅使用 synthetic demo 数据",
    "LLM 输出永远不是事实来源",
    "稳定知识必须有本地证据",
]
CLONE_COMMAND = re.compile(
    r'git clone "(?:'
    + re.escape(REPOSITORY_URL_PLACEHOLDER)
    + r"|https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/"
    r'[A-Za-z0-9._-]+\.git)" "local-llm-wiki"'
)

ROADMAP_REQUIRED_SNIPPETS = [
    "Available now: local/offline",
    "Synthetic first-run demo",
    "Local doctor, schema-check, product-console, and web-console",
    "Evidence-gated draft validation and publish gates",
    "Planned work",
    "Not ready or not certified",
    "No hosted service",
    "No installer",
    "No PyPI package",
    "No GitHub release",
    "Real-provider readiness is not certified",
    "Real-vault readiness is not certified",
    "Obsidian integration is not certified as a public product surface",
]

RELEASE_CHECKLIST_REQUIRED_SNIPPETS = [
    "clean snapshot",
    "new public repository",
    "squash import",
    "equivalent history-safe boundary",
    "Do not publish private development Git history",
    "full export scan",
    "local/offline test evidence",
    "secret",
    "path",
    "privacy",
    "source",
    "explicit human approval before external publication",
    "No publication command is authorized by this checklist",
]

FINAL_EXPORT_COMMAND_SNIPPETS = [
    ".\\tools\\create-public-export.ps1",
    '-OutputPath "<public-export-root>"',
    '-RepositoryUrl "https://github.com/<owner>/local-llm-wiki.git"',
]


def joined(*parts: str) -> str:
    return "".join(parts)


FORBIDDEN_PUBLIC_CLAIMS = [
    joined("published to ", "github"),
    joined("github release ", "is ready"),
    joined("published to ", "pypi"),
    joined("pypi package ", "is ready"),
    joined("hosted service ", "is ready"),
    joined("installer ", "is ready"),
    joined("real-provider readiness ", "complete"),
    joined("real-vault readiness ", "complete"),
    joined("final productization ", "acceptance"),
]


def read_text(rel_path: str) -> str:
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        raise AssertionError(f"{rel_path} is missing")
    return path.read_text(encoding="utf-8")


class OpenSourceDistributionTests(unittest.TestCase):
    def test_public_entry_files_exist_and_are_hygienic(self):
        for rel_path in CHANGED_PUBLIC_FILES:
            with self.subTest(path=rel_path):
                path = PROJECT_ROOT / rel_path
                self.assertTrue(path.is_file(), f"{rel_path} is missing")
                data = path.read_bytes()
                text = data.decode("utf-8")
                self.assertTrue(text.endswith("\n"), f"{rel_path} needs final newline")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    self.assertEqual(
                        line.rstrip(" \t"),
                        line,
                        f"{rel_path}:{line_number} has trailing whitespace",
                    )

    def test_readmes_have_reciprocal_clone_run_navigation_and_boundaries(self):
        readme = read_text("README.md")
        zh_readme = read_text("README.zh-CN.md")
        self.assertRegex(readme, CLONE_COMMAND)
        self.assertRegex(zh_readme, CLONE_COMMAND)
        for snippet in README_REQUIRED_SNIPPETS:
            with self.subTest(readme_snippet=snippet):
                self.assertIn(snippet, readme)
        for snippet in ZH_README_REQUIRED_SNIPPETS:
            with self.subTest(zh_snippet=snippet):
                self.assertIn(snippet, zh_readme)

    def test_markdown_links_in_public_entry_docs_resolve_locally(self):
        for rel_path in PUBLIC_MARKDOWN_FILES:
            text = read_text(rel_path)
            base = (PROJECT_ROOT / rel_path).parent
            for target in MARKDOWN_LINK.findall(text):
                if target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                target_path = target.split("#", 1)[0]
                if not target_path:
                    continue
                with self.subTest(source=rel_path, target=target):
                    self.assertTrue(
                        (base / target_path).resolve().exists(),
                        f"{rel_path} links to missing local target {target}",
                    )

    def test_contributing_and_issue_forms_request_only_safe_public_data(self):
        contributing = read_text("CONTRIBUTING.md")
        for snippet in (
            "python -B -m pip install -e .",
            "python -B -m unittest tests.test_open_source_distribution tests.test_open_source_release tests.test_docs_encoding -v",
            "python -B -m unittest discover -s tests -v",
            "Documentation changes need matching tests",
            "Use synthetic demo data",
            "Do not include secrets",
            "Do not include private source text",
            "Do not include raw chunks",
            "Do not include prompts",
            "Do not include full provider responses",
            "Do not include concrete private paths",
            "Do not include real vault artifacts",
            "Do not claim publication",
            "Do not publish private Git history",
        ):
            with self.subTest(contributing_snippet=snippet):
                self.assertIn(snippet, contributing)

        for rel_path in ISSUE_TEMPLATE_FILES:
            form = read_text(rel_path)
            with self.subTest(form=rel_path):
                self.assertIn("name:", form)
                self.assertIn("description:", form)
                self.assertIn("title:", form)
                self.assertIn("labels:", form)
                self.assertIn("body:", form)
                self.assertIn("- type: markdown", form)
                self.assertRegex(form, r"id: [a-z0-9_-]+")
                for phrase in SAFE_INTAKE_WARNING_PHRASES:
                    self.assertIn(phrase, form)
                self.assertNotIn("real vault path", form.casefold())

    def test_roadmap_and_release_checklist_are_conservative(self):
        roadmap = read_text("docs/product/roadmap.md")
        release_checklist = read_text("docs/product/release-checklist.md")
        for snippet in ROADMAP_REQUIRED_SNIPPETS:
            with self.subTest(roadmap_snippet=snippet):
                self.assertIn(snippet, roadmap)
        for snippet in RELEASE_CHECKLIST_REQUIRED_SNIPPETS:
            with self.subTest(checklist_snippet=snippet):
                self.assertIn(snippet, release_checklist)

    def test_release_docs_define_final_url_history_and_account_gates(self):
        release_guide = read_text("docs/product/open-source-release.md")
        release_checklist = read_text("docs/product/release-checklist.md")
        combined = release_guide + "\n" + release_checklist

        for snippet in FINAL_EXPORT_COMMAND_SNIPPETS:
            with self.subTest(export_command=snippet):
                self.assertIn(snippet, release_guide)
        for phrase in (
            "Repository URL placeholders are allowed only in the private source tree",
            "the final public artifact must contain none",
            "exactly one initial commit",
            "Rotate any provider credential that was disclosed before publication",
            "gh auth status",
            "explicit human approval before external publication",
        ):
            with self.subTest(release_gate=phrase):
                self.assertIn(phrase, combined)

    def test_public_distribution_text_has_no_private_data_shapes_or_overclaims(self):
        combined = "\n".join(read_text(rel_path) for rel_path in CHANGED_PUBLIC_FILES)
        self.assertIsNone(
            ABSOLUTE_DRIVE_PATH.search(combined),
            "Public distribution docs must not contain concrete drive paths.",
        )
        self.assertNotIn(
            joined("https://", "github.com/"),
            GITHUB_REPOSITORY_URL.sub("", combined),
        )
        self.assertNotIn(joined("F:", "\\"), combined)
        for pattern in SECRET_SHAPE_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(combined))
        lower = combined.casefold()
        for claim in FORBIDDEN_PUBLIC_CLAIMS:
            with self.subTest(claim=claim):
                self.assertNotIn(claim, lower)


if __name__ == "__main__":
    unittest.main()
