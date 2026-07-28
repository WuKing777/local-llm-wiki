import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.commands import ingest_file, init_repository


SECRET = "repair-secret-token"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


def claim_for(
    source_id: str,
    text: str,
    quote: str,
    *,
    chunk: str | None = None,
    paragraph: int = 1,
) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": paragraph,
        "text": text,
        "evidence": [
            {
                "chunk": chunk or f"{source_id}#0",
                "quote": quote,
            }
        ],
    }


def metadata_for(source_id: str, claims: list[dict[str, object]]) -> dict[str, object]:
    return {
        "draft_id": "draft-test",
        "title": "Persistent Wiki",
        "query": "persistent wiki",
        "created_at": "2026-06-26T00:00:00Z",
        "model": "fake-model",
        "prompt_hash": "a" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": claims,
    }


def write_draft(
    root: Path,
    metadata: dict[str, object],
    body: str,
    name: str = "persistent-wiki.md",
) -> Path:
    draft_path = root / "wiki" / "_drafts" / name
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in metadata.items()
    )
    lines.extend(["---", "", body.rstrip(), ""])
    draft_path.write_text("\n".join(lines), encoding="utf-8")
    return draft_path


def create_root_with_source(temp: Path, text: str) -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


def audit_snapshot(root: Path) -> tuple[str, str, list[str]]:
    drafts = root / "wiki" / "_drafts"
    draft_files = (
        sorted(path.name for path in drafts.glob("*.md")) if drafts.exists() else []
    )
    return (
        (root / "meta" / "log.md").read_text(encoding="utf-8"),
        (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
        draft_files,
    )


def run_repair_cli(root: Path, draft_path: Path, target: str):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "kb",
            "repair-draft",
            "--root",
            str(root),
            str(draft_path),
            "--target",
            target,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class DraftRepairTests(unittest.TestCase):
    def test_repairs_paraphrased_draft_into_extractive_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    [
                        claim_for(
                            source_id,
                            "The wiki compounds over time",
                            quote,
                        )
                    ],
                ),
                f"# Persistent Wiki\n\nThe wiki compounds over time {source_id}.",
            )

            result = commands.repair_draft_file(
                root, draft_path, target="Persistent Wiki"
            )

            self.assertEqual([], result["issues"])
            repaired_path = Path(result["path"])
            self.assertNotEqual(draft_path, repaired_path)
            self.assertTrue(repaired_path.is_file())
            self.assertEqual(
                [],
                commands.validate_draft_file(
                    root, repaired_path, target="Persistent Wiki"
                ),
            )
            repaired_text = repaired_path.read_text(encoding="utf-8")
            self.assertIn(quote, repaired_text)
            self.assertIn(source_id, repaired_text)
            self.assertTrue(draft_path.is_file())

    def test_refuses_unrepairable_quote_without_writing_repaired_draft_or_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir), "real evidence")
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    [
                        claim_for(
                            source_id,
                            "Unsupported model text",
                            "missing evidence quote",
                        )
                    ],
                ),
                f"Unsupported model text {source_id}.",
            )
            before = audit_snapshot(root)

            result = commands.repair_draft_file(root, draft_path, target="Repair Page")

            self.assertIn("draft-not-repairable", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertEqual(before, audit_snapshot(root))

    def test_rejects_draft_path_outside_drafts_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "outside evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            outside = root / "wiki" / "outside.md"
            outside.write_text(
                f"Outside draft should not be repaired {source_id}.",
                encoding="utf-8",
            )
            before = audit_snapshot(root)

            result = commands.repair_draft_file(root, outside, target="Outside")

            self.assertIn("draft-path-outside-drafts", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertEqual(before, audit_snapshot(root))

    def test_repair_does_not_persist_configured_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = f"safe evidence includes {SECRET}."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    [claim_for(source_id, "safe evidence", quote)],
                ),
                f"safe evidence {source_id}.",
            )
            before = audit_snapshot(root)

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                result = commands.repair_draft_file(
                    root, draft_path, target="Secret Repair"
                )

            self.assertIn("secret-leak", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertEqual(before, audit_snapshot(root))

    def test_repair_rejects_secret_in_root_path_before_writing_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "root path evidence"
            secret_parent = Path(tmpdir) / SECRET
            secret_parent.mkdir()
            root, source_id = create_root_with_source(secret_parent, quote)
            draft_path = write_draft(
                root,
                metadata_for(source_id, [claim_for(source_id, "root path", quote)]),
                f"root path {source_id}.",
            )
            before = audit_snapshot(root)

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                result = commands.repair_draft_file(
                    root, draft_path, target="Root Secret"
                )

            self.assertIn("secret-leak", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertNotIn(SECRET, str(result))
            self.assertEqual(before, audit_snapshot(root))

    def test_repair_rejects_secret_in_draft_filename_before_writing_repaired_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "filename evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(source_id, [claim_for(source_id, "filename", quote)]),
                f"filename {source_id}.",
                name=f"{SECRET}.md",
            )
            before = audit_snapshot(root)

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                result = commands.repair_draft_file(
                    root, draft_path, target="Filename Secret"
                )

            self.assertIn("secret-leak", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertNotIn(SECRET, str(result))
            self.assertEqual(before, audit_snapshot(root))
            self.assertFalse((draft_path.parent / f"{SECRET}.repaired.md").exists())

    def test_repair_rejects_symlinked_wiki_directory_before_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            outside_wiki = base / "outside-wiki"
            outside_wiki.mkdir()
            shutil.rmtree(root / "wiki")
            try:
                os.symlink(outside_wiki, root / "wiki", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "[Cc]anonical"):
                commands.repair_draft_file(
                    root, root / "wiki" / "_drafts" / "draft.md", target="Symlink"
                )

            self.assertFalse((outside_wiki / "_drafts").exists())

    def test_repair_validation_failure_removes_repaired_draft_and_keeps_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "validation rollback evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(source_id, [claim_for(source_id, "rollback", quote)]),
                f"rollback {source_id}.",
            )
            before = audit_snapshot(root)

            with mock.patch.object(
                commands,
                "validate_draft",
                return_value=[{"type": "forced-validation-failure"}],
            ):
                result = commands.repair_draft_file(
                    root, draft_path, target="Rollback Page"
                )

            self.assertIn("forced-validation-failure", issue_types(result["issues"]))
            self.assertEqual("", result["path"])
            self.assertEqual(before, audit_snapshot(root))

    def test_repair_audit_failure_removes_repaired_draft_and_restores_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "audit rollback evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(source_id, [claim_for(source_id, "audit", quote)]),
                f"audit {source_id}.",
            )
            before = audit_snapshot(root)

            def partial_audit(paths, _draft, _repaired, _metadata):
                with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
                    log.write("partial repair audit\n")
                raise RuntimeError("repair audit failed")

            with mock.patch.object(
                commands, "_append_repair_audit", side_effect=partial_audit
            ):
                with self.assertRaisesRegex(RuntimeError, "repair audit failed"):
                    commands.repair_draft_file(
                        root, draft_path, target="Audit Rollback"
                    )

            self.assertEqual(before, audit_snapshot(root))

    def test_cli_repairs_draft_and_prints_repaired_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    [claim_for(source_id, "The wiki compounds", quote)],
                ),
                f"The wiki compounds {source_id}.",
            )

            completed = run_repair_cli(root, draft_path, "Persistent Wiki")

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stderr)
            repaired_path = Path(completed.stdout.strip())
            self.assertTrue(repaired_path.is_file())
            self.assertEqual(
                [],
                commands.validate_draft_file(
                    root, repaired_path, target="Persistent Wiki"
                ),
            )

    def test_cli_outputs_issue_without_traceback_for_unrepairable_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir), "real evidence")
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    [claim_for(source_id, "Unsupported", "missing quote")],
                ),
                f"Unsupported {source_id}.",
            )

            completed = run_repair_cli(root, draft_path, "Repair Page")

            self.assertEqual(1, completed.returncode)
            self.assertIn("draft-not-repairable", completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertNotIn("Traceback", completed.stdout)

    def test_cli_missing_draft_outputs_one_error_line_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            missing = root / "wiki" / "_drafts" / "missing.md"

            completed = run_repair_cli(root, missing, "Missing")

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertEqual(1, len(completed.stderr.splitlines()))
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
