import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kb.commands import (
    ingest_file,
    init_repository,
    lint_repository,
    status_repository,
)


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


class LintStatusTests(unittest.TestCase):
    def test_lint_reports_missing_citation_invalid_source_and_broken_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nGrounded source text.", encoding="utf-8")
            result = ingest_file(root, source)

            (root / "wiki" / "missing-citation.md").write_text(
                "This claim has no source id citation.",
                encoding="utf-8",
            )
            (root / "wiki" / "invalid-source.md").write_text(
                "This claim cites src-deadbeef0000 but not a real source.",
                encoding="utf-8",
            )
            (root / "wiki" / "broken-link.md").write_text(
                f"This claim cites {result['source_id']} and links [[Missing Page]].",
                encoding="utf-8",
            )

            issues = lint_repository(root)

            self.assertIn("missing-citation", issue_types(issues))
            self.assertIn("invalid-source-reference", issue_types(issues))
            self.assertIn("broken-wiki-link", issue_types(issues))
            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "Missing Page"
                    for issue in issues
                )
            )

    def test_lint_rejects_traversal_wiki_link_even_if_outside_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nGrounded source text.", encoding="utf-8")
            result = ingest_file(root, source)
            (root.parent / "escape.md").write_text(
                "Outside file must not satisfy wiki link.",
                encoding="utf-8",
            )
            (root / "wiki" / "traversal-link.md").write_text(
                f"This claim cites {result['source_id']} and links [[../../escape]].",
                encoding="utf-8",
            )

            issues = lint_repository(root)

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "../../escape"
                    for issue in issues
                )
            )

    def test_lint_does_not_count_front_matter_source_id_as_body_citation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nGrounded source text.", encoding="utf-8")
            result = ingest_file(root, source)

            (root / "wiki" / "frontmatter-only.md").write_text(
                "\n".join(
                    [
                        "---",
                        f"source_id: {result['source_id']}",
                        "---",
                        "",
                        "This body has no inline citation.",
                    ]
                ),
                encoding="utf-8",
            )

            issues = lint_repository(root)

            self.assertTrue(
                any(
                    issue["type"] == "missing-citation"
                    and issue["path"] == "wiki/frontmatter-only.md"
                    for issue in issues
                )
            )

    def test_lint_scans_page_with_unclosed_front_matter_as_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            (root / "wiki" / "bad-frontmatter.md").write_text(
                "\n".join(
                    [
                        "---",
                        "source_id: src-deadbeef0000",
                        "This body has no inline citation and [[Missing Page]].",
                    ]
                ),
                encoding="utf-8",
            )

            issues = lint_repository(root)

            self.assertIn("missing-citation", issue_types(issues))
            self.assertIn("broken-wiki-link", issue_types(issues))

    def test_lint_reports_missing_raw_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.txt"
            source.write_text("A raw file that will disappear.", encoding="utf-8")
            result = ingest_file(root, source)
            (root / result["raw_path"]).unlink()

            issues = lint_repository(root)

            self.assertTrue(
                any(
                    issue["type"] == "missing-raw-file"
                    and issue["source_id"] == result["source_id"]
                    for issue in issues
                )
            )

    def test_status_counts_sources_wiki_pages_indexed_rows_and_lint_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Status Source\n\nStatus searchable phrase.", encoding="utf-8")
            result = ingest_file(root, source)

            (root / "wiki" / "grounded.md").write_text(
                f"A grounded note cites {result['source_id']}.",
                encoding="utf-8",
            )
            (root / "wiki" / "ungrounded.md").write_text(
                "An ungrounded note has no citation.",
                encoding="utf-8",
            )

            status = status_repository(root)

            self.assertEqual(1, status["raw_files"])
            self.assertEqual(1, status["source_cards"])
            self.assertEqual(2, status["wiki_pages"])
            self.assertEqual(1, status["indexed_documents"])
            self.assertEqual(1, status["chunks"])
            self.assertEqual(1, status["lint_issues"])

    def test_cli_lint_and_status_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# CLI Source\n\nCLI status phrase.", encoding="utf-8")
            result = ingest_file(root, source)
            (root / "wiki" / "grounded.md").write_text(
                f"A grounded CLI note cites {result['source_id']}.",
                encoding="utf-8",
            )
            project_root = Path(__file__).resolve().parents[1]

            lint_completed = subprocess.run(
                [sys.executable, "-m", "kb", "lint", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, lint_completed.returncode, lint_completed.stderr)
            self.assertIn("No lint issues", lint_completed.stdout)

            status_completed = subprocess.run(
                [sys.executable, "-m", "kb", "status", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, status_completed.returncode, status_completed.stderr)
            self.assertIn("raw_files: 1", status_completed.stdout)
            self.assertIn("source_cards: 1", status_completed.stdout)
            self.assertIn("wiki_pages: 1", status_completed.stdout)
            self.assertIn("indexed_documents: 1", status_completed.stdout)
            self.assertIn("chunks: 1", status_completed.stdout)
            self.assertIn("lint_issues: 0", status_completed.stdout)

            (root / "wiki" / "ungrounded.md").write_text(
                "A CLI note with no citation.",
                encoding="utf-8",
            )
            lint_with_issue = subprocess.run(
                [sys.executable, "-m", "kb", "lint", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, lint_with_issue.returncode)
            self.assertIn("missing-citation", lint_with_issue.stdout)
            self.assertNotIn("Traceback", lint_with_issue.stderr)

    def test_cli_lint_and_status_report_unreadable_wiki_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            (root / "wiki" / "bad.md").write_bytes(b"\xff\xfe\xfa")
            project_root = Path(__file__).resolve().parents[1]

            for command in ("lint", "status"):
                completed = subprocess.run(
                    [sys.executable, "-m", "kb", command, "--root", str(root)],
                    cwd=project_root,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertEqual(1, completed.returncode)
                self.assertRegex(completed.stderr, r"^error: .+\n$")
                self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
