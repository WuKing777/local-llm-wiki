import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path

from kb.commands import govern, ingest_file, init_repository


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


class GovernanceTests(unittest.TestCase):
    def test_govern_writes_deterministic_report_without_rewriting_wiki_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nGrounded governance evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            wiki_page = root / "wiki" / "grounded.md"
            wiki_page.write_text(
                f"# Grounded\n\nGrounded governance evidence cites {source_id}.",
                encoding="utf-8",
            )
            wiki_before = wiki_page.read_bytes()

            first = govern(root)
            first_report = (root / "meta" / "quality-report.md").read_text(
                encoding="utf-8"
            )
            second = govern(root)
            second_report = (root / "meta" / "quality-report.md").read_text(
                encoding="utf-8"
            )

            self.assertEqual(first_report, second_report)
            self.assertEqual(wiki_before, wiki_page.read_bytes())
            self.assertEqual(0, first["blocking_count"])
            self.assertEqual(0, second["blocking_count"])
            self.assertIn("## Blocking Issues\n\n(none)", first_report)
            self.assertIn("source-review-missing", first_report)
            self.assertIn("orphan-wiki-page", first_report)

    def test_govern_maps_lint_issues_to_blocking_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nLinked governance evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            (root / "wiki" / "ungrounded.md").write_text(
                "This factual page has no citation.",
                encoding="utf-8",
            )
            (root / "wiki" / "broken-link.md").write_text(
                f"Linked governance evidence cites {source_id} and [[Missing Page]].",
                encoding="utf-8",
            )

            result = govern(root)
            report = (root / "meta" / "quality-report.md").read_text(
                encoding="utf-8"
            )

            self.assertEqual(2, result["blocking_count"])
            self.assertIn("missing-citation", issue_types(result["blocking"]))
            self.assertIn("broken-wiki-link", issue_types(result["blocking"]))
            self.assertIn("wiki/ungrounded.md", report)
            self.assertIn("wiki/broken-link.md", report)
            self.assertIn("target=Missing Page", report)
            self.assertIn("reason=lint issue missing-citation", report)

    def test_govern_reports_duplicate_conflict_stale_review_and_source_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nOriginal source evidence.", encoding="utf-8")
            ingested = ingest_file(root, source)
            source_id = ingested["source_id"]
            (root / ingested["raw_path"]).write_text(
                "# Source\n\nChanged source evidence.",
                encoding="utf-8",
            )
            (root / "wiki" / "alpha.md").write_text(
                f"# Shared Title\n\nConflict: Alpha cites {source_id}.",
                encoding="utf-8",
            )
            (root / "wiki" / "beta.md").write_text(
                f"# Shared Title\n\nBeta cites {source_id}.",
                encoding="utf-8",
            )
            with (root / "meta" / "review-queue.md").open(
                "a", encoding="utf-8"
            ) as review:
                review.write("- [ ] Investigate unsupported governance item.\n")

            result = govern(root)
            blocking_types = issue_types(result["blocking"])
            advisory_types = issue_types(result["advisory"])

            self.assertIn("stale-source-card", blocking_types)
            self.assertIn("stale-wiki-page", blocking_types)
            self.assertIn("duplicate-wiki-title", advisory_types)
            self.assertIn("possible-conflict-marker", advisory_types)
            self.assertIn("open-review-item", advisory_types)
            self.assertIn("source-review-missing", advisory_types)
            self.assertIn("orphan-wiki-page", advisory_types)

    def test_govern_missing_root_does_not_initialize_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"

            with self.assertRaisesRegex(RuntimeError, "Knowledge base is not initialized"):
                govern(root)

            self.assertFalse(root.exists())

    def test_cli_govern_writes_report_and_uses_exit_code_for_blockers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nCLI governance evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            (root / "wiki" / "grounded.md").write_text(
                f"CLI governance evidence cites {source_id}.",
                encoding="utf-8",
            )
            project_root = Path(__file__).resolve().parents[1]

            clean = subprocess.run(
                [sys.executable, "-m", "kb", "govern", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, clean.returncode, clean.stderr)
            self.assertIn("blocking: 0", clean.stdout)
            self.assertIn("advisory:", clean.stdout)
            self.assertTrue((root / "meta" / "quality-report.md").is_file())
            self.assertNotIn("Traceback", clean.stderr)

            (root / "wiki" / "bad.md").write_text(
                "CLI governance blocker has no citation.",
                encoding="utf-8",
            )
            blocked = subprocess.run(
                [sys.executable, "-m", "kb", "govern", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, blocked.returncode)
            self.assertIn("blocking: 1", blocked.stdout)
            self.assertNotIn("Traceback", blocked.stderr)

    def test_govern_redacts_secret_before_truncating_review_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            secret = "s" + "k-" + "governance-secret-that-crosses-the-review-truncation-boundary"
            with (root / "meta" / "review-queue.md").open(
                "a", encoding="utf-8"
            ) as review:
                review.write(
                    "- [ ] "
                    + ("prefix " * 20)
                    + secret
                    + " trailing review text that forces truncation.\n"
                )
            previous = os.environ.get("KB_LLM_API_KEY")
            os.environ["KB_LLM_API_KEY"] = secret
            try:
                govern(root)
            finally:
                if previous is None:
                    os.environ.pop("KB_LLM_API_KEY", None)
                else:
                    os.environ["KB_LLM_API_KEY"] = previous

            report = (root / "meta" / "quality-report.md").read_text(
                encoding="utf-8"
            )
            self.assertFalse(secret in report, "secret value leaked")
            self.assertFalse(secret[:16] in report, "secret prefix leaked")
            self.assertIn("[redacted]", report)

    def test_cli_govern_redacts_secret_from_success_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            secret = "s" + "k-" + "governance-path-secret"
            root = Path(tmpdir) / secret / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]
            env = os.environ.copy()
            env["KB_LLM_API_KEY"] = secret

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "govern", "--root", str(root)],
                cwd=project_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(secret in completed.stdout, "secret path value leaked")
            self.assertIn("[redacted]", completed.stdout)


if __name__ == "__main__":
    unittest.main()
