import tempfile
import unittest
from pathlib import Path

from kb.commands import govern, init_repository, lint_repository, status_repository
from kb.paths import KnowledgeBasePaths
from kb.wiki import target_path_for_title, wiki_link_exists


class WikiSubpathAndRecursiveGateTests(unittest.TestCase):
    def test_target_path_for_title_allows_safe_subpaths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            paths = KnowledgeBasePaths(root)

            self.assertEqual(
                (root / "wiki" / "daily" / "2026-07-01.md").resolve(),
                target_path_for_title(paths, r"daily\2026-07-01").resolve(),
            )
            self.assertEqual(
                (root / "wiki" / "agent-context" / "我是谁.md").resolve(),
                target_path_for_title(paths, r"agent-context\我是谁").resolve(),
            )

    def test_target_path_for_title_rejects_unsafe_subpaths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            paths = KnowledgeBasePaths(root)
            unsafe_targets = [
                r"..\escape",
                r"wiki\..\escape",
                r"\absolute",
                "/absolute",
                "C" + r":\escape",
                r"_drafts\x",
                r"raw\x",
                r"sources\x",
                r"meta\x",
                r"db\x",
                r"daily\..\x",
                r"daily\\x",
            ]

            for target in unsafe_targets:
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "Unsafe target"):
                        target_path_for_title(paths, target)

    def test_wiki_link_exists_rejects_traversal_even_when_resolved_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            paths = KnowledgeBasePaths(root)
            (root / "wiki" / "safe.md").write_text("# Safe\n", encoding="utf-8")

            self.assertFalse(wiki_link_exists(paths, r"folder/../safe"))
            self.assertFalse(wiki_link_exists(paths, r"folder\..\safe"))

    def test_lint_recursively_checks_nested_stable_wiki_and_excludes_drafts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            nested = root / "wiki" / "daily" / "2026-07-01.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("# Daily\n\nThis uncited fact should fail lint.\n", encoding="utf-8")
            draft = root / "wiki" / "_drafts" / "draft.md"
            draft.parent.mkdir(parents=True)
            draft.write_text("# Draft\n\nThis draft body has no citation.\n", encoding="utf-8")

            issues = lint_repository(root)

            self.assertIn(
                {"type": "missing-citation", "path": "wiki/daily/2026-07-01.md"},
                issues,
            )
            self.assertFalse(
                any(issue.get("path") == "wiki/_drafts/draft.md" for issue in issues)
            )

    def test_status_counts_nested_stable_wiki_and_excludes_drafts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            (root / "wiki" / "daily").mkdir(parents=True)
            (root / "wiki" / "daily" / "2026-07-01.md").write_text(
                "# Daily\n", encoding="utf-8"
            )
            (root / "wiki" / "_drafts").mkdir(parents=True)
            (root / "wiki" / "_drafts" / "draft.md").write_text(
                "# Draft\n", encoding="utf-8"
            )

            status = status_repository(root)

            self.assertEqual(1, status["wiki_pages"])

    def test_governance_uses_recursive_stable_wiki_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            (root / "wiki" / "projects").mkdir(parents=True)
            (root / "wiki" / "projects" / "外脑.md").write_text(
                "# Same Title\n\nThis uncited project fact should fail lint.\n",
                encoding="utf-8",
            )

            result = govern(root)

            self.assertGreaterEqual(result["blocking_count"], 1)
            report = (root / "meta" / "quality-report.md").read_text(encoding="utf-8")
            self.assertIn("wiki/projects/外脑.md", report)


if __name__ == "__main__":
    unittest.main()
