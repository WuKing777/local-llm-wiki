import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from kb.commands import ingest_file, ingest_inbox, init_repository, rebuild_index, search
from kb.sources import read_source_card


def source_id_for(path: Path) -> str:
    return "src-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class IngestSearchTests(unittest.TestCase):
    def test_ingest_inbox_ingests_supported_files_recursively_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            inbox = root / "inbox"
            nested = inbox / "nested"
            nested.mkdir()
            first = inbox / "alpha.md"
            second = nested / "beta.txt"
            first.write_text(
                "# Alpha\n\nfirst inbox searchable phrase",
                encoding="utf-8",
            )
            second.write_text("second inbox searchable phrase", encoding="utf-8")

            result = ingest_inbox(root)

            self.assertEqual(2, result["count"])
            self.assertEqual(
                [source_id_for(first), source_id_for(second)],
                [metadata["source_id"] for metadata in result["ingested"]],
            )
            self.assertEqual(
                ["inbox/alpha.md", "inbox/nested/beta.txt"],
                [metadata["inbox_path"] for metadata in result["ingested"]],
            )
            self.assertEqual(
                source_id_for(first),
                search(root, "first inbox searchable")[0]["source_id"],
            )
            self.assertEqual(
                source_id_for(second),
                search(root, "second inbox searchable")[0]["source_id"],
            )

    def test_empty_initialized_inbox_is_noop_for_existing_metadata_and_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            paths = [
                root / "meta" / "source-map.jsonl",
                root / "meta" / "log.md",
                root / "meta" / "review-queue.md",
                root / "db" / "kb.sqlite3",
            ]
            before = {path: path.read_bytes() for path in paths}
            source_cards_before = sorted(path.name for path in (root / "sources").glob("*"))

            result = ingest_inbox(root)

            self.assertEqual(0, result["count"])
            self.assertEqual([], result["ingested"])
            self.assertEqual(
                source_cards_before,
                sorted(path.name for path in (root / "sources").glob("*")),
            )
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_ingest_inbox_rejects_unsupported_file_before_partial_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            inbox = root / "inbox"
            supported = inbox / "alpha.md"
            unsupported = inbox / "zeta.pdf"
            supported.write_text("preflight supported searchable phrase", encoding="utf-8")
            unsupported.write_bytes(b"%PDF unsupported")

            with self.assertRaisesRegex(RuntimeError, "Unsupported inbox file"):
                ingest_inbox(root)

            self.assertEqual([], list((root / "sources").glob("src-*.md")))
            self.assertEqual([], read_jsonl(root / "meta" / "source-map.jsonl"))
            self.assertFalse(search(root, "preflight supported"))

    def test_ingest_inbox_preflight_failure_does_not_initialize_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            inbox = root / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "notes.pdf").write_bytes(b"%PDF unsupported")

            with self.assertRaisesRegex(RuntimeError, "Unsupported inbox file"):
                ingest_inbox(root)

            self.assertFalse((root / "meta").exists())
            self.assertFalse((root / "db").exists())
            self.assertFalse((root / "sources").exists())

    @unittest.skipIf(
        not hasattr(os, "symlink"), "symlink support is required for this test"
    )
    def test_ingest_inbox_rejects_symlink_escape_before_ingest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            supported = root / "inbox" / "alpha.md"
            supported.write_text("symlink preflight searchable phrase", encoding="utf-8")
            outside = temp / "outside.md"
            outside.write_text("outside symlink phrase", encoding="utf-8")
            link = root / "inbox" / "zeta.md"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "Inbox path escapes inbox"):
                ingest_inbox(root)

            self.assertEqual([], list((root / "sources").glob("src-*.md")))
            self.assertFalse(search(root, "symlink preflight"))

    def test_cli_ingest_inbox_failure_is_one_line_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            (root / "inbox" / "notes.pdf").write_bytes(b"%PDF unsupported")
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "ingest-inbox",
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertNotIn("Traceback", completed.stderr)

    def test_ingest_external_markdown_copies_cards_dedupes_and_rebuilds_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            outside = temp / "outside"
            outside.mkdir()
            source = outside / "field-notes.md"
            phrase = "The persistent wiki keeps retrieval grounded."
            source.write_text(f"# Field Notes\n\n{phrase}\n", encoding="utf-8")
            expected_source_id = source_id_for(source)
            expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()

            init_repository(root)
            result = ingest_file(root, source)

            imported = root / "raw" / "imports" / date.today().isoformat() / "field-notes.md"
            self.assertEqual(expected_source_id, result["source_id"])
            self.assertEqual(source.read_bytes(), imported.read_bytes())

            source_card = root / "sources" / f"{expected_source_id}.md"
            card_text = source_card.read_text(encoding="utf-8")
            self.assertIn(f"source_id: {expected_source_id}", card_text)
            self.assertIn("title: Field Notes", card_text)
            self.assertIn("raw_path: raw/", card_text)
            self.assertIn(f"sha256: {expected_sha}", card_text)
            self.assertIn("kind: markdown", card_text)

            map_path = root / "meta" / "source-map.jsonl"
            self.assertEqual(
                1,
                [entry["source_id"] for entry in read_jsonl(map_path)].count(
                    expected_source_id
                ),
            )

            ingest_file(root, source)
            self.assertEqual(
                1,
                [entry["source_id"] for entry in read_jsonl(map_path)].count(
                    expected_source_id
                ),
            )

            results = search(root, "persistent wiki")
            self.assertGreaterEqual(len(results), 1)
            self.assertEqual(expected_source_id, results[0]["source_id"])
            self.assertEqual("Field Notes", results[0]["title"])
            self.assertIn("raw/", results[0]["raw_path"])
            self.assertIn("persistent wiki", results[0]["snippet"].lower())

            (root / "db" / "kb.sqlite3").unlink()
            rebuild_index(root)
            rebuilt = search(root, "persistent wiki")
            self.assertGreaterEqual(len(rebuilt), 1)
            self.assertEqual(expected_source_id, rebuilt[0]["source_id"])
            self.assertIn("persistent wiki", rebuilt[0]["snippet"].lower())

    def test_ingest_preserves_files_already_under_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            raw_source = root / "raw" / "manual.txt"
            raw_source.write_text(
                "A local raw source should stay exactly where it is.",
                encoding="utf-8",
            )

            result = ingest_file(root, raw_source)

            self.assertEqual("raw/manual.txt", result["raw_path"])
            self.assertFalse((root / "raw" / "imports").exists())
            results = search(root, "local raw source")
            self.assertEqual(result["source_id"], results[0]["source_id"])
            self.assertEqual("raw/manual.txt", results[0]["raw_path"])

    def test_ingest_rejects_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            source = Path(tmpdir) / "notes.pdf"
            source.write_bytes(b"%PDF unsupported")

            with self.assertRaisesRegex(RuntimeError, "Unsupported extension: .pdf"):
                ingest_file(root, source)

    def test_cli_ingest_index_and_search_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            source = temp / "cli-note.html"
            phrase = "CLI search should expose the source id and snippet."
            source.write_text(f"<h1>CLI Note</h1><p>{phrase}</p>", encoding="utf-8")
            expected_source_id = source_id_for(source)
            project_root = Path(__file__).resolve().parents[1]

            init_repository(root)
            ingest_completed = subprocess.run(
                [sys.executable, "-m", "kb", "ingest", str(source), "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, ingest_completed.returncode, ingest_completed.stderr)
            self.assertIn(expected_source_id, ingest_completed.stdout)

            index_completed = subprocess.run(
                [sys.executable, "-m", "kb", "rebuild-index", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, index_completed.returncode, index_completed.stderr)

            search_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "search",
                    "source id",
                    "--root",
                    str(root),
                    "--limit",
                    "1",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, search_completed.returncode, search_completed.stderr)
            self.assertIn(expected_source_id, search_completed.stdout)
            self.assertIn("raw/", search_completed.stdout)
            self.assertIn("source id", search_completed.stdout.lower())

    def test_rebuild_rejects_source_card_raw_path_escape_without_losing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("safe searchable phrase", encoding="utf-8")
            ingest_file(root, valid)
            self.assertTrue(search(root, "safe searchable"))

            outside = temp / "outside.txt"
            outside.write_text("secret outside phrase", encoding="utf-8")
            malicious_card = root / "sources" / "src-000000000000.md"
            malicious_card.write_text(
                "\n".join(
                    [
                        "---",
                        "source_id: src-000000000000",
                        "title: malicious",
                        "raw_path: ../outside.txt",
                        "sha256: " + "0" * 64,
                        "imported_at: 2026-06-24T00:00:00",
                        "kind: text",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "raw_path"):
                rebuild_index(root)

            self.assertTrue(search(root, "safe searchable"))
            self.assertFalse(search(root, "secret outside"))

    def test_rebuild_rejects_malformed_source_card_without_losing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("durable index phrase", encoding="utf-8")
            ingest_file(root, valid)

            malformed = root / "sources" / "src-111111111111.md"
            malformed.write_text(
                "\n".join(
                    [
                        "---",
                        "source_id: src-111111111111",
                        "title: missing raw path",
                        "sha256: " + "1" * 64,
                        "imported_at: 2026-06-24T00:00:00",
                        "kind: markdown",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Missing source card field"):
                rebuild_index(root)

            self.assertTrue(search(root, "durable index"))

    def test_rebuild_failure_after_preflight_preserves_existing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            source = temp / "valid.md"
            source.write_text("stable searchable phrase", encoding="utf-8")
            ingest_file(root, source)
            self.assertTrue(search(root, "stable searchable"))

            from unittest import mock

            with mock.patch("kb.commands.extract_text", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    rebuild_index(root)

            self.assertTrue(search(root, "stable searchable"))

    @unittest.skipIf(
        not hasattr(os, "symlink"), "symlink support is required for this test"
    )
    def test_ingest_rejects_symlink_import_target_without_writing_source_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            source = temp / "source.md"
            source.write_text("symlink collision phrase", encoding="utf-8")
            expected_source_id = source_id_for(source)

            outside_target = root / "wiki" / "target.md"
            outside_target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            import_dir = root / "raw" / "imports" / date.today().isoformat()
            import_dir.mkdir(parents=True)
            link = import_dir / "source.md"
            try:
                os.symlink(outside_target, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "Import target"):
                ingest_file(root, source)

            self.assertFalse((root / "sources" / f"{expected_source_id}.md").exists())

    def test_cli_ingest_rejects_directory_import_target_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            source = temp / "note.md"
            source.write_text("directory collision phrase", encoding="utf-8")
            import_target = root / "raw" / "imports" / date.today().isoformat() / "note.md"
            import_target.mkdir(parents=True)
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "ingest",
                    str(source),
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertRegex(completed.stderr, r"^error: Import target .+\n$")
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse((root / "sources" / f"{source_id_for(source)}.md").exists())

    def test_rebuild_rejects_raw_file_sha_mismatch_without_losing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("trusted index phrase", encoding="utf-8")
            result = ingest_file(root, valid)
            self.assertTrue(search(root, "trusted index"))

            tampered = root / result["raw_path"]
            tampered.write_text("tampered outside phrase", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "sha256"):
                rebuild_index(root)

            self.assertTrue(search(root, "trusted index"))
            self.assertFalse(search(root, "tampered outside"))

    def test_rebuild_db_write_failure_rolls_back_existing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("rollback guard phrase", encoding="utf-8")
            ingest_file(root, valid)
            self.assertTrue(search(root, "rollback guard"))

            from unittest import mock

            with mock.patch(
                "kb.commands._write_index_rows", side_effect=RuntimeError("db boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "db boom"):
                    rebuild_index(root)

            self.assertTrue(search(root, "rollback guard"))

    def test_rebuild_rejects_source_id_mismatch_without_losing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("source id guard phrase", encoding="utf-8")
            result = ingest_file(root, valid)

            card = root / "sources" / f"{result['source_id']}.md"
            card_text = card.read_text(encoding="utf-8")
            card.write_text(
                card_text.replace(
                    f"source_id: {result['source_id']}",
                    "source_id: src-000000000000",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "source_id"):
                rebuild_index(root)

            self.assertTrue(search(root, "source id guard"))

    def test_rebuild_rejects_source_id_matching_filename_but_not_raw_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("filename consistent guard phrase", encoding="utf-8")
            result = ingest_file(root, valid)

            fake_source_id = "src-000000000000"
            old_card = root / "sources" / f"{result['source_id']}.md"
            new_card = root / "sources" / f"{fake_source_id}.md"
            old_text = old_card.read_text(encoding="utf-8")
            new_card.write_text(
                old_text.replace(
                    f"source_id: {result['source_id']}",
                    f"source_id: {fake_source_id}",
                ),
                encoding="utf-8",
            )
            old_card.unlink()

            with self.assertRaisesRegex(RuntimeError, "source_id"):
                rebuild_index(root)

            self.assertTrue(search(root, "filename consistent guard"))

    def test_rebuild_rejects_kind_mismatch_without_losing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            valid = temp / "valid.md"
            valid.write_text("kind guard phrase", encoding="utf-8")
            result = ingest_file(root, valid)

            card = root / "sources" / f"{result['source_id']}.md"
            card_text = card.read_text(encoding="utf-8")
            card.write_text(
                card_text.replace("kind: markdown", "kind: text"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "kind"):
                rebuild_index(root)

            self.assertTrue(search(root, "kind guard"))

    def test_same_content_different_filenames_reuse_existing_raw_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            init_repository(root)
            first = temp / "a.md"
            second = temp / "b.md"
            content = "# Duplicate\n\nsame content phrase"
            first.write_text(content, encoding="utf-8")
            second.write_text(content, encoding="utf-8")

            first_result = ingest_file(root, first)
            second_result = ingest_file(root, second)

            self.assertEqual(first_result["source_id"], second_result["source_id"])
            self.assertEqual(first_result["raw_path"], second_result["raw_path"])
            import_dir = root / "raw" / "imports" / date.today().isoformat()
            imported_files = list(import_dir.glob("*.md"))
            self.assertEqual(1, len(imported_files))

    def test_source_card_reader_rejects_missing_closing_front_matter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            card = Path(tmpdir) / "src-222222222222.md"
            card.write_text(
                "\n".join(
                    [
                        "---",
                        "source_id: src-222222222222",
                        "title: broken",
                        "raw_path: raw/broken.md",
                        "sha256: " + "2" * 64,
                        "imported_at: 2026-06-24T00:00:00",
                        "kind: markdown",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Missing closing front matter"):
                read_source_card(card)

    def test_cli_search_handles_fts_operator_query_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            source = temp / "note.md"
            source.write_text("alpha beta gamma", encoding="utf-8")
            project_root = Path(__file__).resolve().parents[1]
            init_repository(root)
            ingest_file(root, source)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "search",
                    "alpha AND",
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertIn(completed.returncode, {0, 1})
            self.assertNotIn("Traceback", completed.stderr)
            if completed.returncode == 1:
                self.assertRegex(completed.stderr, r"^error: .+\n$")

    def test_cli_search_handles_utf8_bom_source_under_gbk_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "wiki"
            source = temp / "bom-note.md"
            source.write_text("\ufeffbom searchable phrase", encoding="utf-8")
            expected_source_id = source_id_for(source)
            project_root = Path(__file__).resolve().parents[1]
            init_repository(root)
            ingest_file(root, source)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "gbk:strict"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "search",
                    "bom searchable",
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertIn(expected_source_id, completed.stdout)


if __name__ == "__main__":
    unittest.main()
