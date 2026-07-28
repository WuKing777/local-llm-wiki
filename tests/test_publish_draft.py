import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kb.commands as commands
import kb.wiki as wiki
from kb.commands import ingest_file, init_repository
from kb.paths import KnowledgeBasePaths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET = "publish-secret-token"


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


def claim_for(
    source_id: str,
    text: str,
    *,
    paragraph: int = 1,
    quote: str | None = None,
    chunk: str | None = None,
    claim_id: str = "claim-1",
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "paragraph": paragraph,
        "text": text,
        "evidence": [
            {
                "chunk": chunk or f"{source_id}#0",
                "quote": quote or text,
            }
        ],
    }


def metadata_for(source_id: str, **overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "draft_id": "draft-test",
        "title": "Draft Title",
        "query": "publish draft",
        "created_at": "2026-06-24T00:00:00Z",
        "model": "test-model",
        "prompt_hash": "a" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": [claim_for(source_id, "Grounded paragraph cites")],
    }
    metadata.update(overrides)
    return metadata


def write_draft(
    root: Path,
    metadata: dict[str, object],
    body: str,
    name: str = "draft-title.md",
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


def create_root_with_source(temp: Path) -> tuple[Path, str]:
    root = temp / "kb"
    init_repository(root)
    source = temp / "source.md"
    source.write_text(
        "# Source\n\nGrounded paragraph cites. Publish evidence. Unrelated quote exists.",
        encoding="utf-8",
    )
    source_id = ingest_file(root, source)["source_id"]
    return root, source_id


def valid_body(source_id: str) -> str:
    return f"# Draft Title\n\nGrounded paragraph cites {source_id}."


def run_publish_cli(
    root: Path,
    draft_path: Path,
    target: str,
    env: dict[str, str] | None = None,
):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "kb",
            "publish-draft",
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
        env={**os.environ, **(env or {})},
        check=False,
    )


class PublishDraftTests(unittest.TestCase):
    def test_rejects_target_containing_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            with self.assertRaises(ValueError):
                wiki.target_path_for_title(paths, "../page")

    def test_rejects_absolute_and_drive_qualified_targets(self):
        unsafe_targets = [
            "/absolute/page",
            r"\absolute\page",
            "C" + ":/absolute/page",
            "C" + r":\absolute\page",
            "C" + ":drive-qualified",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            for target in unsafe_targets:
                with self.subTest(target=target):
                    with self.assertRaises(ValueError):
                        wiki.target_path_for_title(paths, target)

    def test_rejects_publish_into_drafts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            with self.assertRaises(ValueError):
                wiki.target_path_for_title(paths, "_drafts/page")

    def test_rejects_paths_outside_wiki(self):
        unsafe_targets = ["wiki/../../outside", "folder/../../../outside"]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            for target in unsafe_targets:
                with self.subTest(target=target):
                    with self.assertRaises(ValueError):
                        wiki.target_path_for_title(paths, target)

    def test_allows_safe_nested_targets(self):
        safe_targets = ["folder/page", r"folder\page"]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            for target in safe_targets:
                with self.subTest(target=target):
                    self.assertEqual(
                        paths.wiki / "folder" / "page.md",
                        wiki.target_path_for_title(paths, target),
                    )

    def test_rejects_explicit_reserved_area_targets(self):
        unsafe_targets = ["raw/page", "sources/page", "meta/page", "db/page"]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = KnowledgeBasePaths(Path(tmpdir) / "kb")

            for target in unsafe_targets:
                with self.subTest(target=target):
                    with self.assertRaises(ValueError):
                        wiki.target_path_for_title(paths, target)

    def test_publish_valid_draft_to_slugged_wiki_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            write_draft(root, metadata_for(source_id), valid_body(source_id))

            result = commands.publish_draft(
                root,
                Path("wiki") / "_drafts" / "draft-title.md",
                "Page Title",
            )

            target = root / "wiki" / "page-title.md"
            self.assertEqual([], result["issues"])
            self.assertEqual(str(target.resolve()), result["target"])
            self.assertEqual(valid_body(source_id), target.read_text(encoding="utf-8"))

    def test_publish_valid_draft_to_safe_nested_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            write_draft(root, metadata_for(source_id), valid_body(source_id))

            result = commands.publish_draft(
                root,
                Path("wiki") / "_drafts" / "draft-title.md",
                r"daily\2026-07-01",
            )

            target = root / "wiki" / "daily" / "2026-07-01.md"
            self.assertEqual([], result["issues"])
            self.assertEqual(str(target.resolve()), result["target"])
            self.assertEqual(valid_body(source_id), target.read_text(encoding="utf-8"))

    def test_publish_rejects_symlinked_drafts_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root, source_id = create_root_with_source(base)
            external_drafts = base / "external-drafts"
            external_drafts.mkdir()
            try:
                os.symlink(
                    external_drafts,
                    root / "wiki" / "_drafts",
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            result = commands.publish_draft(root, draft_path, "Escaped Draft")

            self.assertIn("draft-path-outside-drafts", issue_types(result["issues"]))
            self.assertFalse((root / "wiki" / "escaped-draft.md").exists())

    def test_publish_allows_target_aware_self_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            body = (
                f"# Self Page\n\n"
                f"Grounded paragraph cites {source_id} and links [[Self Page]]."
            )
            draft_path = write_draft(root, metadata_for(source_id), body)

            result = commands.publish_draft(root, draft_path, "Self Page")

            target = root / "wiki" / "self-page.md"
            self.assertEqual([], result["issues"])
            self.assertTrue(target.is_file())
            self.assertIn("[[Self Page]]", target.read_text(encoding="utf-8"))

    def test_publish_rejects_factuality_failure_before_creating_new_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            target = root / "wiki" / "unsupported-page.md"
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Grounded paragraph cites",
                            quote="Missing quote text",
                        )
                    ],
                ),
                valid_body(source_id),
            )

            result = commands.publish_draft(root, draft_path, "Unsupported Page")

            self.assertIn("claim-quote-not-in-chunk", issue_types(result["issues"]))
            self.assertFalse(target.exists())

    def test_publish_rejects_whitespace_mutated_quote_before_target_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            target = root / "wiki" / "mutated-quote.md"
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Grounded paragraph cites",
                            quote="Grounded\nparagraph cites",
                        )
                    ],
                ),
                valid_body(source_id),
            )

            result = commands.publish_draft(root, draft_path, "Mutated Quote")

            self.assertIn("claim-quote-not-in-chunk", issue_types(result["issues"]))
            self.assertFalse(target.exists())

    def test_publish_rejects_unmatched_heading_before_target_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            target = root / "wiki" / "heading-fact.md"
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"# Unclaimed heading fact\n\nGrounded paragraph cites {source_id}.",
            )

            result = commands.publish_draft(root, draft_path, "Heading Fact")

            self.assertIn("unsupported-draft-heading", issue_types(result["issues"]))
            self.assertFalse(target.exists())

    def test_publish_keeps_existing_target_on_factuality_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            target = root / "wiki" / "existing-page.md"
            previous = b"previous bytes"
            target.write_bytes(previous)
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Grounded paragraph cites",
                            quote="Unrelated quote exists",
                        )
                    ],
                ),
                valid_body(source_id),
            )

            result = commands.publish_draft(root, draft_path, "Existing Page")

            self.assertIn(
                "claim-text-not-supported-by-quote", issue_types(result["issues"])
            )
            self.assertEqual(previous, target.read_bytes())

    def test_rejects_other_missing_link_during_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            body = (
                f"# Self Page\n\nGrounded paragraph cites {source_id}, "
                "links [[Self Page]], and links [[Other Missing]]."
            )
            draft_path = write_draft(root, metadata_for(source_id), body)

            result = commands.publish_draft(root, draft_path, "Self Page")

            self.assertIn("broken-wiki-link", issue_types(result["issues"]))
            self.assertFalse((root / "wiki" / "self-page.md").exists())

    def test_rejects_traversal_link_during_publish_even_if_outside_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            (root.parent / "escape.md").write_text(
                "Outside file must not satisfy wiki link.",
                encoding="utf-8",
            )
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[../../escape]].",
            )

            result = commands.publish_draft(root, draft_path, "Escaped Link")

            self.assertIn("broken-wiki-link", issue_types(result["issues"]))
            self.assertFalse((root / "wiki" / "escaped-link.md").exists())

    def test_publish_rejects_separator_target_without_target_or_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            result = commands.publish_draft(root, draft_path, "folder//page")

            self.assertIn("unsafe-target", issue_types(result["issues"]))
            self.assertFalse((root / "wiki" / "folder" / "page.md").exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_restores_existing_target_if_post_publish_status_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            target = root / "wiki" / "existing-page.md"
            target.write_bytes(b"previous bytes")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            with mock.patch.object(
                commands, "status_repository", side_effect=RuntimeError("status boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "status boom"):
                    commands.publish_draft(root, draft_path, "Existing Page")

            self.assertEqual(b"previous bytes", target.read_bytes())

    def test_removes_new_target_if_post_publish_status_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))
            target = root / "wiki" / "new-page.md"

            with mock.patch.object(
                commands, "status_repository", side_effect=RuntimeError("status boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "status boom"):
                    commands.publish_draft(root, draft_path, "New Page")

            self.assertFalse(target.exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_removes_new_nested_target_dirs_if_post_publish_status_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            with mock.patch.object(
                commands, "status_repository", side_effect=RuntimeError("status boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "status boom"):
                    commands.publish_draft(root, draft_path, r"daily\2026-07-01")

            self.assertFalse((root / "wiki" / "daily").exists())

    def test_removes_new_target_if_post_publish_lint_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))
            target = root / "wiki" / "new-page.md"

            with mock.patch.object(
                commands,
                "lint_repository",
                side_effect=[[], RuntimeError("lint boom")],
            ):
                with self.assertRaisesRegex(RuntimeError, "lint boom"):
                    commands.publish_draft(root, draft_path, "New Page")

            self.assertFalse(target.exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_restores_existing_target_if_post_publish_lint_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            target = root / "wiki" / "existing-page.md"
            target.write_bytes(b"previous bytes")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            with mock.patch.object(
                commands,
                "lint_repository",
                side_effect=[[], RuntimeError("lint boom")],
            ):
                with self.assertRaisesRegex(RuntimeError, "lint boom"):
                    commands.publish_draft(root, draft_path, "Existing Page")

            self.assertEqual(b"previous bytes", target.read_bytes())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_removes_new_target_if_post_publish_lint_adds_issue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))
            target = root / "wiki" / "new-page.md"

            with mock.patch.object(
                commands,
                "lint_repository",
                side_effect=[
                    [],
                    [{"type": "missing-citation", "path": "wiki/new-page.md"}],
                ],
            ), mock.patch.object(commands, "status_repository", return_value={}):
                result = commands.publish_draft(root, draft_path, "New Page")

            self.assertIn("publish-lint-issue", issue_types(result["issues"]))
            self.assertFalse(target.exists())

    def test_removes_new_target_and_restores_log_if_publish_audit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))
            target = root / "wiki" / "new-page.md"

            def partial_audit(*_args):
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("partial publish audit\n")
                raise RuntimeError("audit boom")

            with mock.patch.object(
                commands, "_append_publish_audit", side_effect=partial_audit
            ):
                with self.assertRaisesRegex(RuntimeError, "audit boom"):
                    commands.publish_draft(root, draft_path, "New Page")

            self.assertFalse(target.exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_restores_existing_target_and_log_if_publish_audit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            target = root / "wiki" / "existing-page.md"
            target.write_bytes(b"previous bytes")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            def partial_audit(*_args):
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("partial publish audit\n")
                raise RuntimeError("audit boom")

            with mock.patch.object(
                commands, "_append_publish_audit", side_effect=partial_audit
            ):
                with self.assertRaisesRegex(RuntimeError, "audit boom"):
                    commands.publish_draft(root, draft_path, "Existing Page")

            self.assertEqual(b"previous bytes", target.read_bytes())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_allows_publish_when_pre_existing_lint_issues_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            (root / "wiki" / "pre-existing.md").write_text(
                "This old page has no citation.",
                encoding="utf-8",
            )
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            result = commands.publish_draft(root, draft_path, "Clean Page")

            self.assertEqual([], result["issues"])
            self.assertTrue((root / "wiki" / "clean-page.md").is_file())

    def test_appends_publish_audit_log_on_success_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            good_draft = write_draft(
                root,
                metadata_for(source_id),
                valid_body(source_id),
                name="good.md",
            )

            result = commands.publish_draft(root, good_draft, "Audit Page")

            self.assertEqual([], result["issues"])
            success_log = log_path.read_text(encoding="utf-8")
            self.assertIn("[publish-draft]", success_log)
            self.assertIn("wiki/_drafts/good.md", success_log)
            self.assertIn("wiki/audit-page.md", success_log)
            self.assertIn(source_id, success_log)

            bad_draft = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[Missing]].",
                name="bad.md",
            )
            failed = commands.publish_draft(root, bad_draft, "Bad Page")

            self.assertIn("broken-wiki-link", issue_types(failed["issues"]))
            self.assertEqual(success_log, log_path.read_text(encoding="utf-8"))

    def test_publish_audit_redacts_configured_secret_from_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                valid_body(source_id),
                name=f"{SECRET}.md",
            )

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                result = commands.publish_draft(root, draft_path, f"Page {SECRET}")

            self.assertEqual([], result["issues"])
            log_text = (root / "meta" / "log.md").read_text(encoding="utf-8")
            self.assertIn("[publish-draft]", log_text)
            self.assertNotIn(SECRET, log_text)

    def test_cli_success_prints_target_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            completed = run_publish_cli(root, draft_path, "CLI Page")

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                f"{(root / 'wiki' / 'cli-page.md').resolve()}\n",
                completed.stdout,
            )
            self.assertEqual("", completed.stderr)

    def test_cli_success_redacts_secret_in_target_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))
            target = root / "wiki" / f"page-{SECRET}.md"

            completed = run_publish_cli(
                root,
                draft_path,
                f"Page {SECRET}",
                env={"KB_LLM_API_KEY": SECRET},
            )

            expected_stdout = str(target.resolve()).replace(SECRET, "[redacted]")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(f"{expected_stdout}\n", completed.stdout)
            self.assertNotIn(SECRET, completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertTrue(target.is_file())
            self.assertNotIn(SECRET, log_path.read_text(encoding="utf-8"))

    def test_cli_issue_output_prints_one_issue_per_line_to_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            completed = run_publish_cli(root, draft_path, "../bad")

            self.assertEqual(1, completed.returncode)
            self.assertIn("unsafe-target", completed.stdout)
            self.assertEqual(1, len(completed.stdout.splitlines()))
            self.assertEqual("", completed.stderr)

    def test_cli_separator_target_outputs_issue_without_target_or_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            log_before = log_path.read_text(encoding="utf-8")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            completed = run_publish_cli(root, draft_path, r"folder\\page")

            self.assertEqual(1, completed.returncode)
            self.assertIn("unsafe-target", completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertFalse((root / "wiki" / "folder" / "page.md").exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))

    def test_cli_runtime_error_prints_one_stderr_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            draft_path = root / "wiki" / "_drafts" / "missing.md"

            completed = run_publish_cli(root, draft_path, "Runtime Page")

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertEqual(1, len(completed.stderr.splitlines()))
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
