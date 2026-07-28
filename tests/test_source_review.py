import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from kb.commands import (
    govern,
    ingest_file,
    init_repository,
    refresh_source,
    review_source,
    search,
)
from kb.sources import read_source_card


def source_id_for(path: Path) -> str:
    return "src-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]


class SourceReviewTests(unittest.TestCase):
    def test_review_source_records_status_and_clears_missing_review_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("# Source\n\nReviewable evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]

            result = review_source(
                root,
                source_id,
                status="reviewed",
                reviewer="tester",
                note="encoding and provenance checked",
            )

            self.assertEqual(source_id, result["source_id"])
            card = read_source_card(root / "sources" / f"{source_id}.md")
            self.assertEqual("reviewed", card["review_status"])
            self.assertEqual("tester", card["reviewer"])
            self.assertIn("encoding and provenance checked", card["review_note"])
            self.assertIn("T", card["reviewed_at"])
            issue_types = {issue["type"] for issue in govern(root)["advisory"]}
            self.assertNotIn("source-review-missing", issue_types)

    def test_rejected_source_review_is_blocking_governance_issue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nQuestionable evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]

            review_source(root, source_id, status="rejected", reviewer="tester")

            blocking_types = {issue["type"] for issue in govern(root)["blocking"]}
            self.assertIn("source-review-blocking", blocking_types)

    def test_refresh_source_replaces_stale_card_and_index_after_raw_repair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            source = temp / "analysis.md"
            source.write_text("# Analysis\n\nDamaged text.", encoding="utf-8")
            old_source_id = ingest_file(root, source)["source_id"]
            raw_path = root / "raw" / "imports"
            imported = next(raw_path.rglob("analysis.md"))
            imported.write_text("# Analysis\n\nValid UTF-8 repaired evidence.", encoding="utf-8")
            new_source_id = source_id_for(imported)

            result = refresh_source(root, old_source_id)

            self.assertEqual(old_source_id, result["old_source_id"])
            self.assertEqual(new_source_id, result["source_id"])
            self.assertFalse((root / "sources" / f"{old_source_id}.md").exists())
            self.assertTrue((root / "sources" / f"{new_source_id}.md").is_file())
            self.assertFalse(search(root, "Damaged text"))
            self.assertEqual(new_source_id, search(root, "repaired evidence")[0]["source_id"])

    def test_refresh_source_preserves_review_when_content_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nReviewed evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            review_source(root, source_id, status="reviewed", reviewer="tester")

            refresh_source(root, source_id)

            card = read_source_card(root / "sources" / f"{source_id}.md")
            self.assertEqual("reviewed", card["review_status"])
            self.assertEqual("tester", card["reviewer"])

    def test_review_source_redacts_embedding_secret_from_review_note(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            source = Path(tmpdir) / "source.md"
            source.write_text("# Source\n\nSecret hygiene evidence.", encoding="utf-8")
            source_id = ingest_file(root, source)["source_id"]
            secret = "s" + "k-" + "source-review-embedding-secret"
            previous = os.environ.get("KB_EMBEDDING_API_KEY")
            os.environ["KB_EMBEDDING_API_KEY"] = secret
            try:
                review_source(root, source_id, status="reviewed", note=f"checked {secret}")
            finally:
                if previous is None:
                    os.environ.pop("KB_EMBEDDING_API_KEY", None)
                else:
                    os.environ["KB_EMBEDDING_API_KEY"] = previous

            card_text = (root / "sources" / f"{source_id}.md").read_text(
                encoding="utf-8"
            )
            self.assertFalse(secret in card_text, "secret review note leaked")
            self.assertIn("[redacted]", card_text)


if __name__ == "__main__":
    unittest.main()
