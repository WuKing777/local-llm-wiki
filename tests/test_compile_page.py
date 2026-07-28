import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.cli import main
from kb.commands import ingest_file, init_repository


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


def configured_env() -> dict[str, str]:
    return {
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "fake-local-model",
    }


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


def claim_for(source_id: str, text: str, quote: str) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": quote}],
    }


def draft_envelope(
    source_id: str,
    body: str,
    text: str,
    quote: str,
) -> str:
    return json.dumps(
        {"body": body, "claims": [claim_for(source_id, text, quote)]}
    )


def create_root_with_source(temp: Path, text: str) -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


class CompilePageTests(unittest.TestCase):
    def test_compile_page_publishes_already_valid_draft_without_repair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            body = f"# Persistent Wiki\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))

            result = commands.compile_page(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=configured_env(),
            )

            self.assertEqual([], result["issues"])
            self.assertEqual("", result["repaired_draft"])
            self.assertEqual(1, len(client.calls))
            target = root / "wiki" / "persistent-wiki.md"
            self.assertTrue(target.is_file())
            self.assertEqual(body, target.read_text(encoding="utf-8"))
            self.assertFalse((root / "wiki" / "_drafts" / "persistent-wiki.repaired.md").exists())

    def test_compile_page_repairs_paraphrased_draft_then_publishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            body = f"# Persistent Wiki\n\nThe wiki compounds over time {source_id}."
            client = FakeClient(
                draft_envelope(source_id, body, "The wiki compounds over time", quote)
            )

            result = commands.compile_page(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=configured_env(),
            )

            self.assertEqual([], result["issues"])
            self.assertTrue(Path(result["repaired_draft"]).is_file())
            target = root / "wiki" / "persistent-wiki.md"
            self.assertTrue(target.is_file())
            self.assertIn(quote, target.read_text(encoding="utf-8"))
            self.assertNotIn("compounds over time", target.read_text(encoding="utf-8"))

    def test_compile_page_leaves_stable_wiki_unchanged_when_repair_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir), "real evidence")
            client = FakeClient(
                draft_envelope(
                    source_id,
                    f"Unsupported model text {source_id}.",
                    "Unsupported model text",
                    "missing quote",
                )
            )

            result = commands.compile_page(
                root,
                "real evidence",
                "Unsupported Page",
                client=client,
                env=configured_env(),
            )

            self.assertIn("draft-not-repairable", issue_types(result["issues"]))
            self.assertEqual("", result["target"])
            self.assertFalse((root / "wiki" / "unsupported-page.md").exists())

    def test_compile_page_archive_existing_moves_prior_draft_before_regenerating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            drafts = root / "wiki" / "_drafts"
            drafts.mkdir(parents=True, exist_ok=True)
            existing_draft = drafts / "persistent-wiki.md"
            existing_draft.write_text("old draft body\n", encoding="utf-8")
            body = f"# Persistent Wiki\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))

            result = commands.compile_page(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=configured_env(),
                archive_existing=True,
            )

            self.assertEqual([], result["issues"])
            self.assertEqual(1, len(client.calls))
            self.assertEqual(body, (root / "wiki" / "persistent-wiki.md").read_text(encoding="utf-8"))
            self.assertTrue(Path(result["archived_draft"]).is_file())
            self.assertIn(
                root.resolve() / "wiki" / "_drafts" / "_archive",
                Path(result["archived_draft"]).resolve().parents,
            )
            self.assertEqual(
                "old draft body\n",
                Path(result["archived_draft"]).read_text(encoding="utf-8"),
            )
            self.assertTrue(existing_draft.is_file())
            self.assertNotEqual(
                "old draft body\n",
                existing_draft.read_text(encoding="utf-8"),
            )

    def test_compile_page_without_archive_existing_preserves_collision_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            client = FakeClient(
                draft_envelope(source_id, f"# Persistent Wiki\n\n{quote} {source_id}", quote, quote)
            )

            with self.assertRaisesRegex(RuntimeError, "Draft already exists"):
                commands.compile_page(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual(0, len(client.calls))
            self.assertEqual("old draft body\n", draft_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "wiki" / "persistent-wiki.md").exists())

    def test_compile_page_archive_existing_rejects_non_file_collision_before_llm_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            draft_path.mkdir(parents=True)
            client = FakeClient(
                draft_envelope(source_id, f"# Persistent Wiki\n\n{quote} {source_id}", quote, quote)
            )

            with self.assertRaisesRegex(RuntimeError, "Existing draft path is not a file"):
                commands.compile_page(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                    archive_existing=True,
                )

            self.assertEqual(0, len(client.calls))
            self.assertFalse((root / "wiki" / "persistent-wiki.md").is_file())

    def test_compile_page_archive_existing_restores_prior_draft_when_new_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            log_path = root / "meta" / "log.md"
            review_path = root / "meta" / "review-queue.md"
            log_before = log_path.read_text(encoding="utf-8")
            review_before = review_path.read_text(encoding="utf-8")
            client = FakeClient(
                draft_envelope(source_id, f"# Persistent Wiki\n\n{quote} {source_id}", quote, quote)
            )

            with mock.patch.object(
                commands, "write_draft", side_effect=RuntimeError("write failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    commands.compile_page(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                        archive_existing=True,
                    )

            self.assertEqual(1, len(client.calls))
            self.assertEqual("old draft body\n", draft_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "wiki" / "persistent-wiki.md").exists())
            self.assertFalse((root / "wiki" / "_drafts" / "_archive").exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))
            self.assertEqual(review_before, review_path.read_text(encoding="utf-8"))

    def test_compile_page_archive_existing_archives_failed_replacement_and_restores_prior_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir), "real evidence")
            draft_path = root / "wiki" / "_drafts" / "unsupported-page.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            client = FakeClient(
                draft_envelope(
                    source_id,
                    f"Unsupported model text {source_id}.",
                    "Unsupported model text",
                    "missing quote",
                )
            )

            result = commands.compile_page(
                root,
                "real evidence",
                "Unsupported Page",
                client=client,
                env=configured_env(),
                archive_existing=True,
            )

            self.assertIn("draft-not-repairable", issue_types(result["issues"]))
            self.assertEqual("", result["target"])
            self.assertEqual("old draft body\n", draft_path.read_text(encoding="utf-8"))
            failed_draft = Path(result["failed_draft"])
            self.assertTrue(failed_draft.is_file())
            self.assertIn(
                root.resolve() / "wiki" / "_drafts" / "_archive",
                failed_draft.resolve().parents,
            )
            self.assertIn(
                "Unsupported model text",
                failed_draft.read_text(encoding="utf-8"),
            )
            review_queue = (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
            self.assertIn(
                failed_draft.resolve().relative_to(root.resolve()).as_posix(),
                review_queue,
            )
            self.assertNotIn(
                "Review wiki/_drafts/unsupported-page.md title=Unsupported Page",
                review_queue,
            )
            self.assertFalse((root / "wiki" / "unsupported-page.md").exists())

    def test_compile_page_publish_issue_after_repair_archives_generated_drafts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            body = f"# Persistent Wiki\n\nThe wiki compounds over time {source_id}."
            client = FakeClient(
                draft_envelope(source_id, body, "The wiki compounds over time", quote)
            )

            with mock.patch.object(
                commands,
                "publish_draft",
                return_value={
                    "target": str(root / "wiki" / "persistent-wiki.md"),
                    "issues": [{"type": "publish-lint-issue"}],
                },
            ):
                result = commands.compile_page(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertIn("publish-lint-issue", issue_types(result["issues"]))
            self.assertFalse((root / "wiki" / "persistent-wiki.md").exists())
            self.assertFalse((root / "wiki" / "_drafts" / "persistent-wiki.md").exists())
            self.assertFalse((root / "wiki" / "_drafts" / "persistent-wiki.repaired.md").exists())
            failed_draft = Path(result["failed_draft"])
            self.assertTrue(failed_draft.is_file())
            self.assertIn("the wiki is a persistent, compounding artifact.", failed_draft.read_text(encoding="utf-8"))
            archived = list((root / "wiki" / "_drafts" / "_archive").rglob("persistent-wiki*.md"))
            self.assertEqual(2, len(archived))
            review_queue = (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
            self.assertIn(
                failed_draft.resolve().relative_to(root.resolve()).as_posix(),
                review_queue,
            )
            self.assertNotIn("persistent-wiki.repaired.md; validate", review_queue)

    def test_compile_page_publish_exception_restores_prior_draft_and_archives_failed_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            body = f"# Persistent Wiki\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))

            with mock.patch.object(
                commands, "publish_draft", side_effect=RuntimeError("publish crashed")
            ):
                with self.assertRaisesRegex(RuntimeError, "publish crashed"):
                    commands.compile_page(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                        archive_existing=True,
                    )

            self.assertEqual("old draft body\n", draft_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "wiki" / "persistent-wiki.md").exists())
            archived = list((root / "wiki" / "_drafts" / "_archive").rglob("persistent-wiki*.md"))
            self.assertEqual(1, len([path for path in archived if path.read_text(encoding="utf-8") != "old draft body\n"]))
            review_queue = (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
            self.assertNotIn("Review wiki/_drafts/persistent-wiki.md title=Persistent Wiki", review_queue)

    def test_compile_page_repair_exception_restores_prior_draft_and_archives_failed_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir), "real evidence")
            draft_path = root / "wiki" / "_drafts" / "unsupported-page.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            client = FakeClient(
                draft_envelope(
                    source_id,
                    f"Unsupported model text {source_id}.",
                    "Unsupported model text",
                    "missing quote",
                )
            )

            with mock.patch.object(
                commands, "repair_draft_file", side_effect=RuntimeError("repair crashed")
            ):
                with self.assertRaisesRegex(RuntimeError, "repair crashed"):
                    commands.compile_page(
                        root,
                        "real evidence",
                        "Unsupported Page",
                        client=client,
                        env=configured_env(),
                        archive_existing=True,
                    )

            self.assertEqual("old draft body\n", draft_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "wiki" / "unsupported-page.md").exists())
            archived = list((root / "wiki" / "_drafts" / "_archive").rglob("unsupported-page*.md"))
            self.assertEqual(1, len([path for path in archived if path.read_text(encoding="utf-8") != "old draft body\n"]))
            review_queue = (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
            self.assertNotIn("Review wiki/_drafts/unsupported-page.md title=Unsupported Page", review_queue)

    def test_compile_page_cli_publishes_with_fake_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            body = f"# Persistent Wiki\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "compile-page",
                "--root",
                str(root),
                "--query",
                "persistent wiki",
                "--title",
                "Persistent Wiki",
            ]

            with mock.patch.dict(os.environ, configured_env(), clear=True):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(argv)

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(f"{(root / 'wiki' / 'persistent-wiki.md').resolve()}\n", stdout.getvalue())

    def test_compile_page_cli_archive_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "the wiki is a persistent, compounding artifact."
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text("old draft body\n", encoding="utf-8")
            body = f"# Persistent Wiki\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "compile-page",
                "--root",
                str(root),
                "--query",
                "persistent wiki",
                "--title",
                "Persistent Wiki",
                "--archive-existing",
            ]

            with mock.patch.dict(os.environ, configured_env(), clear=True):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(argv)

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(f"{(root / 'wiki' / 'persistent-wiki.md').resolve()}\n", stdout.getvalue())
            archived = list((root / "wiki" / "_drafts" / "_archive").rglob("persistent-wiki*.md"))
            self.assertEqual(1, len(archived))
            self.assertEqual("old draft body\n", archived[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
