import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import ingest_file, init_repository
from kb.locks import WriteLockError, acquire_write_lock
from kb.topic_suggestions import create_topic_suggestions


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in ("wiki", "raw", "sources", "meta", "db"):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "meta/topic-suggestions/" not in path.relative_to(root).as_posix():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _read_suggestion(root: Path) -> dict[str, object]:
    suggestions = sorted((root / "meta" / "topic-suggestions").glob("*.json"))
    assert len(suggestions) == 1
    return json.loads(suggestions[0].read_text(encoding="utf-8"))


def _sentinel(label: str) -> str:
    return "sk" + f"-{label}1234567890abcdef"


class TopicSuggestionTests(unittest.TestCase):
    def test_source_cards_generate_suggestion_json_without_writing_content_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "learning-notes.md"
            init_repository(root)
            source.write_text(
                "# Retrieval Learning Notes\n\nConflict marker: conflict in imported notes.\n",
                encoding="utf-8",
            )
            metadata = ingest_file(root, source)
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            suggestion_path = Path(stdout.strip())
            self.assertTrue(suggestion_path.is_file())
            self.assertEqual(before, _snapshot(root))
            data = json.loads(suggestion_path.read_text(encoding="utf-8"))
            self.assertRegex(data["suggestion_id"], r"^topic-[0-9a-f]{16}$")
            self.assertEqual([metadata["source_id"]], data["source_ids"])
            self.assertTrue(data["suggested_pages"])
            page = data["suggested_pages"][0]
            self.assertIn(page["kind"], {"new_page", "update_page"})
            self.assertTrue(page["title"])
            self.assertTrue(str(page["target"]).startswith("wiki/"))
            self.assertEqual([metadata["source_id"]], page["supporting_source_ids"])
            self.assertEqual([metadata["source_id"]], data["conflict_markers"][0]["source_ids"])
            self.assertTrue(data["next_actions"])

    def test_source_id_filter_only_includes_selected_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            first = Path(tmpdir) / "alpha.md"
            second = Path(tmpdir) / "beta.md"
            init_repository(root)
            first.write_text("# Alpha Topic\n\nAlpha local text.\n", encoding="utf-8")
            second.write_text("# Beta Topic\n\nBeta local text.\n", encoding="utf-8")
            first_meta = ingest_file(root, first)
            second_meta = ingest_file(root, second)

            code, stdout, stderr = _run_cli(
                [
                    "suggest-topics",
                    "--root",
                    str(root),
                    "--source-id",
                    second_meta["source_id"],
                ]
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            data = json.loads(Path(stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual([second_meta["source_id"]], data["source_ids"])
            self.assertNotIn(first_meta["source_id"], json.dumps(data))

    def test_unknown_source_id_fails_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(
                ["suggest-topics", "--root", str(root), "--source-id", "src-ffffffffffff"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("Unknown source id", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_no_source_cards_fails_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("No source cards", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_path_safety_malicious_source_title_cannot_escape_wiki(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "evil.md"
            init_repository(root)
            source.write_text("# ../raw/../../escape\n\nDo not trust source titles.\n", encoding="utf-8")
            ingest_file(root, source)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(0, code, stderr)
            data = json.loads(Path(stdout.strip()).read_text(encoding="utf-8"))
            for page in data["suggested_pages"]:
                target = page["target"]
                self.assertTrue(target.startswith("wiki/"))
                self.assertNotIn("..", target)
                self.assertNotIn("/raw/", target)
                self.assertFalse((Path(tmpdir) / "escape.md").exists())

    def test_cli_json_output_is_parseable_and_redacts_source_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "secret-shaped.md"
            init_repository(root)
            secret = _sentinel("SENTINELSOURCE")
            source.write_text(
                f"# Secret Shaped Source\n\nThis source mentions {secret} in raw text.\n",
                encoding="utf-8",
            )
            metadata = ingest_file(root, source)

            code, stdout, stderr = _run_cli(
                ["suggest-topics", "--root", str(root), "--source-id", metadata["source_id"], "--json"]
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            payload = json.loads(stdout)
            self.assertEqual(metadata["source_id"], payload["source_ids"][0])
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertNotIn(secret, stdout)

    def test_invalid_requested_source_id_format_fails_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(
                ["suggest-topics", "--root", str(root), "--source-id", "src-NOTVALID"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("Invalid source id", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_invalid_source_card_metadata_source_id_format_fails_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            metadata = ingest_file(root, source)
            card = root / "sources" / f"{metadata['source_id']}.md"
            card.write_text(
                card.read_text(encoding="utf-8").replace(
                    f"source_id: {metadata['source_id']}", "source_id: src-NOTVALID"
                ),
                encoding="utf-8",
            )
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("Invalid source id", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_source_card_raw_hash_and_kind_are_validated_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            metadata = ingest_file(root, source)
            raw_path = root / metadata["raw_path"]
            raw_path.write_text("# Known Topic\n\nChanged local text.\n", encoding="utf-8")
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("mismatch", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_symlinked_source_card_is_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            external = Path(tmpdir) / "external-card.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            metadata = ingest_file(root, source)
            real_card = root / "sources" / f"{metadata['source_id']}.md"
            external.write_bytes(real_card.read_bytes())
            real_card.unlink()
            try:
                os.symlink(external, real_card)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlink unavailable: {exc}")
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("source card path is unsafe", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_stable_wiki_symlink_outside_root_is_not_read_for_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            outside = Path(tmpdir) / "outside.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            outside.write_text("# Known Topic\n\nOutside wiki text.\n", encoding="utf-8")
            link = root / "wiki" / "outside.md"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlink unavailable: {exc}")

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(0, code, stderr)
            data = json.loads(Path(stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual([], data["duplicate_candidates"])

    def test_topic_suggestions_symlink_directory_is_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            external = Path(tmpdir) / "external-suggestions"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            external.mkdir()
            suggestion_dir = root / "meta" / "topic-suggestions"
            try:
                os.symlink(external, suggestion_dir, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("Topic suggestion directory is unsafe", stderr)
            self.assertEqual([], list(external.iterdir()))

    def test_active_write_lock_blocks_suggestions_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            before = _snapshot(root)

            with acquire_write_lock(root, operation="outer"):
                code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("write_lock_active", stderr)
            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_active_write_lock_blocks_direct_api_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)
            before = _snapshot(root)

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaises(WriteLockError):
                    create_topic_suggestions(root)

            self.assertFalse((root / "meta" / "topic-suggestions").exists())
            self.assertEqual(before, _snapshot(root))

    def test_atomic_write_replace_failure_cleans_temporary_file(self):
        from kb import topic_suggestions

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "topic-abc.json"

            with mock.patch("kb.topic_suggestions.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    topic_suggestions._write_json_atomic(target, {"status": "pending"})

            self.assertEqual([], list(Path(tmpdir).glob("*.tmp")))
            self.assertFalse(target.exists())

    def test_first_suggestion_write_failure_removes_new_metadata_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "known.md"
            init_repository(root)
            source.write_text("# Known Topic\n\nKnown local text.\n", encoding="utf-8")
            ingest_file(root, source)

            with mock.patch(
                "kb.topic_suggestions.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    create_topic_suggestions(root)

            self.assertFalse((root / "meta" / "topic-suggestions").exists())

    def test_chinese_explicit_conflict_markers_are_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            source = Path(tmpdir) / "zh.md"
            init_repository(root)
            source.write_text(
                "# 中文标记\n\n这里写明冲突、矛盾、以及不一致这三个显式标记。\n",
                encoding="utf-8",
            )
            metadata = ingest_file(root, source)

            code, stdout, stderr = _run_cli(["suggest-topics", "--root", str(root)])

            self.assertEqual(0, code, stderr)
            data = json.loads(Path(stdout.strip()).read_text(encoding="utf-8"))
            markers = {item["marker"] for item in data["conflict_markers"]}
            self.assertIn("冲突", markers)
            self.assertIn("矛盾", markers)
            self.assertIn("不一致", markers)
            for item in data["conflict_markers"]:
                self.assertEqual([metadata["source_id"]], item["source_ids"])


if __name__ == "__main__":
    unittest.main()
