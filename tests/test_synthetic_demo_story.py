import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = PROJECT_ROOT / "tools" / "run-demo.ps1"
DEMO_ROOT = PROJECT_ROOT / "examples" / "demo-root"
DEMO_STORY = PROJECT_ROOT / "examples" / "demo-story"
FIRST_RUN_DOC = PROJECT_ROOT / "docs" / "product" / "first-run-demo.md"
QUICKSTART_DOC = PROJECT_ROOT / "docs" / "product" / "quickstart-zh.md"
COMMAND_GUIDE = PROJECT_ROOT / "docs" / "product" / "command-guide.md"
EXAMPLES_README = PROJECT_ROOT / "examples" / "README.md"

EXPECTED_STORY_STEPS = [
    "init-temp-root",
    "ingest-source-1",
    "ingest-source-2",
    "ingest-source-3",
    "review-source-1",
    "review-source-2",
    "review-source-3",
    "search-local-evidence",
    "answer-with-local-evidence",
    "capture-candidate",
    "review-candidate",
    "publish-memory",
    "write-deterministic-draft",
    "validate-draft",
    "publish-draft",
    "trust-report",
    "govern",
    "backup",
    "restore",
    "migrate-check",
]

REQUIRED_BOUNDARIES = {
    "offline": True,
    "synthetic_data": True,
    "writes_real_user_state": False,
    "provider_environment_cleared": True,
    "tracked_fixtures_mutated": False,
}

HIGH_RISK_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"https?://[^\s,}\"]+"),
    re.compile(r"(?i)\bprompt\s*[:=]"),
    re.compile(r"(?i)\b(full_)?provider[_ -]?response\s*[:=]"),
    re.compile(r"(?i)\bprivate[_ -]?source[_ -]?text\s*[:=]"),
    re.compile(r"(?i)\bsource[_ -]?chunk\s*[:=]"),
    re.compile(r"(?i)bearer\s+(?!<)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"]?(?!<)[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"(?i)(users|administrator|appdata|localappdata)"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])"),
]


def powershell_executable() -> str:
    command = shutil.which("powershell") or shutil.which("pwsh")
    if not command:
        raise AssertionError("PowerShell is required for tools/run-demo.ps1 smoke tests")
    return command


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def read_text(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"{path.relative_to(PROJECT_ROOT)} is missing")
    return path.read_text(encoding="utf-8")


class SyntheticDemoStoryTests(unittest.TestCase):
    def test_public_synthetic_story_fixture_contains_at_least_three_sources(self):
        self.assertTrue(DEMO_STORY.is_dir(), "examples/demo-story must exist")
        sources = sorted(DEMO_STORY.glob("*.md"))
        self.assertGreaterEqual(len(sources), 3)

        combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        for phrase in (
            "Synthetic public demo material",
            "No real user data",
            "safe for public release",
            "offline local workflow",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

        for path in sources:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("Synthetic public demo material", text)
                self.assertIn("privacy: public", text)
                self.assertIn("No real user data", text)
                self.assertNotRegex(text, r"(?i)private|personal|secret|token")

    def test_default_demo_runs_complete_story_in_temp_root_with_redacted_report(self):
        demo_root_before = tree_bytes(DEMO_ROOT)
        demo_story_before = tree_bytes(DEMO_STORY)
        sentinel = "sk-" + "synthetic-demo-story-sentinel-" + ("0" * 12)

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            report = base / "reports" / "synthetic-demo-story-report.json"
            env = os.environ.copy()
            env.update(
                {
                    "APPDATA": str(base / "parent-appdata"),
                    "LOCALAPPDATA": str(base / "parent-localappdata"),
                    "KB_LLM_API_KEY": sentinel,
                    "KB_LLM_BASE_URL": f"https://example.invalid/{sentinel}",
                    "KB_LLM_MODEL": "provider-model-that-must-not-be-used",
                    "KB_EMBEDDING_API_KEY": sentinel,
                    "KB_EMBEDDING_BASE_URL": f"https://example.invalid/{sentinel}",
                    "KB_EMBEDDING_MODEL": "embedding-model-that-must-not-be-used",
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

            self.assertNotIn(str(base), payload_text)
            self.assertNotIn(sentinel, payload_text)
            for pattern in HIGH_RISK_PATTERNS:
                with self.subTest(pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(payload_text))

        self.assertEqual(demo_root_before, tree_bytes(DEMO_ROOT))
        self.assertEqual(demo_story_before, tree_bytes(DEMO_STORY))

        self.assertEqual("synthetic-demo-story-v1", payload["schema_version"])
        self.assertEqual("<temp-synthetic-root>", payload["demo_root"])
        self.assertEqual("examples/demo-story", payload["source_fixture"])
        expected_boundaries = {
            **REQUIRED_BOUNDARIES,
            "no_provider_calls": True,
            "no_real_user_vault": True,
            "redaction_applied": True,
        }
        self.assertEqual(expected_boundaries, payload["boundaries"])
        self.assertTrue(payload["no_provider_calls"])
        self.assertTrue(payload["no_real_user_vault"])
        self.assertTrue(payload["redaction_applied"])

        steps = payload["story_steps"]
        self.assertEqual(EXPECTED_STORY_STEPS, [step["name"] for step in steps])
        for step in steps:
            with self.subTest(step=step["name"]):
                self.assertEqual(0, step["exit_code"])
                self.assertIn(step["status"], {"completed", "pass", "warning"})
                self.assertTrue(step["classification"])
                self.assertIn("stdout_summary", step)
                self.assertIn("stderr_summary", step)

        ingested = payload["ingested_sources"]
        self.assertEqual(3, len(ingested))
        for item in ingested:
            with self.subTest(source=item["fixture_path"]):
                self.assertRegex(item["source_id"], r"^src-[0-9a-f]{12}$")
                self.assertTrue(item["fixture_path"].startswith("examples/demo-story/"))
                self.assertEqual("reviewed", item["review_status"])

        candidate = payload["candidate_memory"]
        self.assertRegex(candidate["candidate_id"], r"^mem-[0-9a-f]{16}$")
        self.assertRegex(candidate["published_source_id"], r"^src-[0-9a-f]{12}$")
        self.assertEqual("published", candidate["status"])

        draft = payload["deterministic_draft"]
        self.assertEqual("wiki/_drafts/synthetic-demo-story.md", draft["draft_path"])
        self.assertEqual("wiki/synthetic-demo-story.md", draft["published_path"])
        self.assertRegex(draft["source_id"], r"^src-[0-9a-f]{12}$")
        self.assertIn("offline local workflow", draft["evidence_quote"])
        self.assertEqual("pass", payload["trust_report"]["status"])
        self.assertEqual("pass", payload["backup_restore_migrate"]["backup_status"])
        self.assertEqual("pass", payload["backup_restore_migrate"]["restore_status"])
        self.assertEqual("pass", payload["backup_restore_migrate"]["migrate_status"])

    def test_product_docs_describe_expanded_synthetic_demo_story(self):
        combined = "\n".join(
            read_text(path)
            for path in (FIRST_RUN_DOC, QUICKSTART_DOC, COMMAND_GUIDE, EXAMPLES_README)
        )

        for phrase in (
            "examples/demo-story",
            "synthetic-demo-story-v1",
            "ingested_sources",
            "source ids are generated",
            "capture-candidate",
            "review-candidate",
            "publish-memory",
            "trust-report",
            "migrate-check",
            "temp synthetic root",
            "tracked_fixtures_mutated=false",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

        stale_source_id = "src-" + "124acde044e6"
        self.assertNotIn(stale_source_id, read_text(FIRST_RUN_DOC))


if __name__ == "__main__":
    unittest.main()
