import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PRODUCT_DOCS = [
    "docs/product/quickstart-zh.md",
    "docs/product/first-run-demo.md",
    "docs/product/local-web-console.md",
    "docs/product/command-guide.md",
    "docs/product/installation.md",
    "docs/product/configuration.md",
    "docs/product/backup-restore-migration.md",
    "docs/product/privacy-and-secrets.md",
    "docs/product/provider-preflight.md",
    "docs/product/open-source-release.md",
    "docs/product/roadmap.md",
    "docs/product/release-checklist.md",
]

DOC_AND_README_FILES = [
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    *PRODUCT_DOCS,
]
DEMO_STORY_FILES = [
    "examples/demo-story/01-offline-boundary.md",
    "examples/demo-story/02-source-review.md",
    "examples/demo-story/03-backup-migration.md",
]
TEXT_FILES_WITH_STYLE_RULES = [
    *DOC_AND_README_FILES,
    "examples/README.md",
    *DEMO_STORY_FILES,
    "tools/run-demo.ps1",
    "tests/test_first_run_demo.py",
    "tests/test_docs_encoding.py",
    "tests/test_open_source_distribution.py",
    "tests/test_synthetic_demo_story.py",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
]

REQUIRED_COMMANDS = [
    "init",
    "doctor",
    "schema-check",
    "lock-check",
    "recover-lock",
    "product-console",
    "web-console",
    "ingest",
    "search",
    "answer",
    "backup",
    "restore",
    "migrate-check",
    "llm-preflight",
    "eval-search",
    "gateway-check",
    "validate-draft",
    "publish-draft",
    "capture-candidate",
    "review-candidate",
    "publish-memory",
    "suggest-topics",
    "daily-workflow",
    "benchmark-add",
    "exobrain-check",
]

README_LINKS = [
    "[Chinese README](README.zh-CN.md)",
    "[中文快速开始](docs/product/quickstart-zh.md)",
    "[首次运行演示](docs/product/first-run-demo.md)",
    "[本地网页控制台](docs/product/local-web-console.md)",
    "[命令选择指南](docs/product/command-guide.md)",
    "[CONTRIBUTING.md](CONTRIBUTING.md)",
    "[Roadmap](docs/product/roadmap.md)",
    "[Release Checklist](docs/product/release-checklist.md)",
    "[Installation](docs/product/installation.md)",
    "[Configuration](docs/product/configuration.md)",
    "[Backup, Restore, and Migration](docs/product/backup-restore-migration.md)",
    "[Privacy and Secrets](docs/product/privacy-and-secrets.md)",
    "[Provider Preflight](docs/product/provider-preflight.md)",
    "[Open Source Release](docs/product/open-source-release.md)",
]

REQUIRED_BOUNDARY_PHRASES = [
    "AI/LLM output is never a fact source",
    "DeepSeek and other LLMs may organize, summarize, reason, and draft",
    "Stable wiki content must pass validate and publish gates with local quote evidence",
    "Local bge-m3 embeddings are retrieval accelerators only",
    "not reasoning authority or citation authority",
    "Personal use is local-first",
    "Another Windows computer is supported by reinstalling prerequisites",
    "Future commercialization requires a separate product, legal, security, privacy, support, and release process",
    "Real user-vault draft validate/publish operations require a later exact-path PM operation task with explicit root and item paths",
    "Real retrieval benchmark operations require a later exact-path PM operation task with explicit root and item paths",
    "Cloud or LLM use is off by default",
    "No cloud or LLM provider is configured or called by default",
    "Do not persist prompts, full provider responses, API keys, bearer tokens, private source text, or source chunks outside approved local evidence",
    "Real-provider readiness is limited to configuration and preflight checks",
    "Do not publish private development Git history",
    "Repository URL placeholders are allowed only in the private source tree",
    "the final public artifact must contain none",
]

def joined(*parts: str) -> str:
    return "".join(parts)


FORBIDDEN_CLAIMS = [
    "run-productization-acceptance.ps1",
    "run-personal-exobrain-acceptance.ps1",
    "acceptance script",
    joined("final verification ", "complete"),
    joined("final acceptance ", "complete"),
    joined("real-provider readiness ", "complete"),
    joined("real user-vault operations are ", "complete"),
    joined("published to ", "github"),
    joined("published to ", "pypi"),
]

SECRET_SHAPE_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+(?!<)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"]?(?!<)[A-Za-z0-9._~+/-]{12,}"),
]

UNQUOTED_PLACEHOLDER_PATH = re.compile(
    r"--(?:root|source|restored|backup|output|benchmark|original|text)\s+<[^>\s]+>"
)
ABSOLUTE_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])")


def read_utf8(rel_path: str) -> str:
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        raise AssertionError(f"{rel_path} is missing")
    data = path.read_bytes()
    return data.decode("utf-8")


def existing_doc_texts() -> dict[str, str]:
    texts = {}
    for rel_path in DOC_AND_README_FILES:
        path = PROJECT_ROOT / rel_path
        if path.exists():
            texts[rel_path] = read_utf8(rel_path)
    return texts


def product_text() -> str:
    return "\n".join(read_utf8(rel_path) for rel_path in PRODUCT_DOCS)


def mojibake_variants() -> set[str]:
    source_phrases = [
        "\u6211\u7684\u5916\u8111",
        "\u77e5\u8bc6\u5e93",
        "\u9879\u76ee\u540d\u79f0",
        "\u51b3\u7b56\u540d\u79f0",
        "\u504f\u597d",
        "\u4e2d\u6587\u5feb\u901f\u5f00\u59cb",
        "\u9996\u6b21\u8fd0\u884c\u6f14\u793a",
        "\u672c\u5730\u7f51\u9875\u63a7\u5236\u53f0",
    ]
    variants = {"\ufffd"}
    for phrase in source_phrases:
        for codec in ("gbk", "cp936", "big5"):
            try:
                decoded = phrase.encode("utf-8").decode(codec)
            except UnicodeDecodeError:
                continue
            if decoded != phrase:
                variants.add(decoded)
    return variants


class DocsEncodingTests(unittest.TestCase):
    def test_product_docs_readme_and_test_decode_as_utf8(self):
        for rel_path in TEXT_FILES_WITH_STYLE_RULES:
            with self.subTest(path=rel_path):
                path = PROJECT_ROOT / rel_path
                self.assertTrue(path.exists(), f"{rel_path} is missing")
                path.read_bytes().decode("utf-8")

    def test_docs_and_test_have_final_newline_and_no_trailing_whitespace(self):
        for rel_path in TEXT_FILES_WITH_STYLE_RULES:
            with self.subTest(path=rel_path):
                text = read_utf8(rel_path)
                self.assertTrue(text.endswith("\n"), f"{rel_path} must end with a newline")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    self.assertEqual(
                        line.rstrip(" \t"),
                        line,
                        f"{rel_path}:{line_number} has trailing whitespace",
                    )

    def test_readme_links_to_product_docs(self):
        readme = read_utf8("README.md")
        for link in README_LINKS:
            with self.subTest(link=link):
                self.assertIn(link, readme)

    def test_required_command_examples_are_present_and_paths_are_quoted(self):
        docs = product_text()
        for command in REQUIRED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(f"python -B -m kb {command}", docs)
        self.assertIn('python -B -m kb doctor --root "<root>"', docs)
        self.assertIsNone(
            UNQUOTED_PLACEHOLDER_PATH.search("\n".join(existing_doc_texts().values())),
            "PowerShell placeholder paths must be quoted, for example --root \"<root>\"",
        )
        self.assertNotIn("--root <dir>", docs)
        self.assertNotIn("--root <root>", docs)

    def test_required_product_boundaries_are_documented(self):
        docs = product_text()
        for phrase in REQUIRED_BOUNDARY_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, docs)

    def test_no_real_vault_path_mojibake_secret_shapes_or_bearer_tokens(self):
        combined = "\n".join(existing_doc_texts().values())
        self.assertIsNone(
            ABSOLUTE_DRIVE_PATH.search(combined),
            "Product docs must use quoted placeholder roots instead of concrete drive paths",
        )
        for marker in mojibake_variants():
            with self.subTest(marker=marker.encode("unicode_escape").decode("ascii")):
                self.assertNotIn(marker, combined)
        for pattern in SECRET_SHAPE_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(combined))

    def test_no_acceptance_script_or_completion_claim(self):
        combined = "\n".join(existing_doc_texts().values()).lower()
        for claim in FORBIDDEN_CLAIMS:
            with self.subTest(claim=claim):
                self.assertNotIn(claim.lower(), combined)


if __name__ == "__main__":
    unittest.main()
