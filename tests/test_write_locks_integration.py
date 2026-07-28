import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.locks import WriteLockError, acquire_write_lock
from kb.paths import KnowledgeBasePaths
from kb.wiki import write_draft


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_SECRET = "write-lock-secret-sentinel"


class FakeLock:
    def __init__(self, calls: list[str], operation: str):
        self.calls = calls
        self.operation = operation

    def __enter__(self):
        self.calls.append(self.operation)
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


def counting_acquire(calls: list[str]):
    def acquire(root, *, operation: str, lease_seconds: int = 900):
        return FakeLock(calls, operation)

    return acquire


def source_file(directory: Path, name: str = "source.md") -> Path:
    path = directory / name
    path.write_text(
        "# Source\n\nPersistent wiki evidence cites local source material.",
        encoding="utf-8",
    )
    return path


def snapshot_selected(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in ("raw", "sources", "meta", "db", "wiki"):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def create_indexed_root(temp: Path) -> tuple[Path, str]:
    root = temp / "kb"
    commands.init_repository(root)
    metadata = commands.ingest_file(root, source_file(temp))
    return root, metadata["source_id"]


def claim_for(source_id: str) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": "Persistent wiki evidence cites",
        "evidence": [
            {
                "chunk": f"{source_id}#0",
                "quote": "Persistent wiki evidence cites",
            }
        ],
    }


def draft_envelope(source_id: str) -> str:
    body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
    return json.dumps({"body": body, "claims": [claim_for(source_id)]})


def metadata_for(source_id: str) -> dict[str, object]:
    return {
        "draft_id": "draft-test",
        "title": "Draft Title",
        "query": "publish draft",
        "created_at": "2026-07-04T00:00:00Z",
        "model": "test-model",
        "prompt_hash": "a" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": [
            {
                "claim_id": "claim-1",
                "paragraph": 1,
                "text": "Grounded paragraph cites",
                "evidence": [
                    {
                        "chunk": f"{source_id}#0",
                        "quote": "Grounded paragraph cites",
                    }
                ],
            }
        ],
    }


def valid_draft_body(source_id: str) -> str:
    return f"# Draft Title\n\nGrounded paragraph cites {source_id}."


class WriteLockIntegrationTests(unittest.TestCase):
    def test_ingest_file_active_lock_fails_before_repository_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            commands.init_repository(root)
            source = source_file(temp)

            with acquire_write_lock(root, operation="outer"):
                before = snapshot_selected(root)
                with self.assertRaises(WriteLockError) as raised:
                    commands.ingest_file(root, source)
                after = snapshot_selected(root)

            self.assertEqual("write_lock_active", raised.exception.classification)
            self.assertEqual(before, after)

    def test_initialized_init_commands_active_lock_fail_no_write(self):
        initializers = (
            ("init", commands.init_repository),
            ("obsidian-init", commands.init_obsidian_vault),
            ("exobrain-init", commands.init_personal_exobrain),
        )
        for name, initializer in initializers:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir) / "kb"
                    initializer(root)

                    with acquire_write_lock(root, operation="outer"):
                        before = snapshot_selected(root)
                        with self.assertRaises(WriteLockError) as raised:
                            initializer(root)
                        after = snapshot_selected(root)

                    self.assertEqual("write_lock_active", raised.exception.classification)
                    self.assertEqual(before, after)

    def test_fresh_init_bootstrap_creates_lock_after_meta_and_releases_on_success_or_failure(self):
        real_acquire = acquire_write_lock

        def recording_acquire(calls: list[str]):
            def acquire(root, *, operation: str, lease_seconds: int = 900):
                root_path = Path(root)
                self.assertTrue((root_path / "meta").is_dir())
                lock = real_acquire(
                    root_path, operation=operation, lease_seconds=lease_seconds
                )
                self.assertTrue((root_path / "meta" / ".kb-write.lock").is_file())
                calls.append(operation)
                return lock

            return acquire

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            calls: list[str] = []
            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=recording_acquire(calls),
                create=True,
            ):
                commands.init_repository(root)

            self.assertEqual(["init"], calls)
            self.assertFalse((root / "meta" / ".kb-write.lock").exists())

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            (root / "db" / "kb.sqlite3").mkdir(parents=True)
            calls = []
            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=recording_acquire(calls),
                create=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "database file"):
                    commands.init_repository(root)

            self.assertEqual(["init"], calls)
            self.assertFalse((root / "meta" / ".kb-write.lock").exists())

    def test_ingest_composites_acquire_one_outer_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)

            pdf_root = temp / "pdf-root"
            pdf = temp / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            pdf_calls: list[str] = []
            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=counting_acquire(pdf_calls),
                create=True,
            ), mock.patch("kb.commands._extract_pdf_text", return_value="PDF text"):
                commands.ingest_pdf(pdf_root, pdf)
            self.assertEqual(["ingest-pdf"], pdf_calls)

            ocr_root = temp / "ocr-root"
            image = temp / "scan.png"
            image.write_bytes(b"png")
            ocr_calls: list[str] = []
            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=counting_acquire(ocr_calls),
                create=True,
            ):
                commands.ingest_ocr(
                    ocr_root,
                    image,
                    runner=lambda args: "OCR text",
                    env={"KB_TESSERACT_CMD": "tesseract"},
                )
            self.assertEqual(["ingest-ocr"], ocr_calls)

            inbox_root = temp / "inbox-root"
            commands.init_repository(inbox_root)
            source_file(inbox_root / "inbox", "inbox.md")
            inbox_calls: list[str] = []
            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=counting_acquire(inbox_calls),
                create=True,
            ):
                commands.ingest_inbox(inbox_root)
            self.assertEqual(["ingest-inbox"], inbox_calls)

    def test_ingest_inbox_preflight_failure_does_not_bootstrap_lock_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            inbox = root / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "notes.pdf").write_bytes(b"%PDF unsupported")

            with self.assertRaisesRegex(RuntimeError, "Unsupported inbox file"):
                commands.ingest_inbox(root)

            self.assertFalse((root / "meta").exists())
            self.assertFalse((root / "db").exists())
            self.assertFalse((root / "sources").exists())

    def test_compile_page_acquires_one_outer_lock_and_uses_unlocked_helpers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            calls: list[str] = []
            draft = root / "wiki" / "_drafts" / "persistent-wiki.md"
            repaired = root / "wiki" / "_drafts" / "persistent-wiki-repaired.md"
            target = root / "wiki" / "Persistent Wiki.md"

            def set_helper(name: str, fallback: str, value):
                selected = name if hasattr(commands, name) else fallback
                original = getattr(commands, selected)
                setattr(commands, selected, value)
                self.addCleanup(lambda: setattr(commands, selected, original))

            set_helper(
                "_llm_draft_unlocked",
                "llm_draft",
                lambda *args, **kwargs: {"path": str(draft), "archived_draft": ""},
            )
            set_helper(
                "_repair_draft_file_unlocked",
                "repair_draft_file",
                lambda *args, **kwargs: {"path": str(repaired), "issues": []},
            )
            set_helper(
                "_publish_draft_unlocked",
                "publish_draft",
                lambda *args, **kwargs: {"target": str(target), "issues": []},
            )

            with mock.patch(
                "kb.commands.acquire_write_lock",
                side_effect=counting_acquire(calls),
                create=True,
            ), mock.patch(
                "kb.commands._require_initialized_repository",
                return_value=None,
            ), mock.patch(
                "kb.commands._read_audit_snapshots",
                return_value=(root / "meta" / "log.md", root / "meta" / "review.md", "", ""),
            ), mock.patch(
                "kb.commands.validate_draft_file",
                return_value=[{"type": "force-repair"}],
            ):
                result = commands.compile_page(root, "query", "Persistent Wiki")

            self.assertEqual(["compile-page"], calls)
            self.assertEqual(str(target), result["target"])
            self.assertEqual(str(repaired), result["repaired_draft"])

    def test_llm_draft_active_lock_fails_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root, source_id = create_indexed_root(temp)
            client = FakeClient(draft_envelope(source_id))

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaises(WriteLockError) as raised:
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env={
                            "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                            "KB_LLM_MODEL": "fake-local-model",
                        },
                    )

            self.assertEqual("write_lock_active", raised.exception.classification)
            self.assertEqual([], client.calls)

    def test_publish_draft_active_lock_fails_before_target_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            commands.init_repository(root)
            source = temp / "source.md"
            source.write_text(
                "# Source\n\nGrounded paragraph cites. Publish evidence.",
                encoding="utf-8",
            )
            source_id = commands.ingest_file(root, source)["source_id"]
            draft = write_draft(
                KnowledgeBasePaths(root),
                metadata_for(source_id),
                valid_draft_body(source_id),
            )
            target = root / "wiki" / "Draft Title.md"

            with acquire_write_lock(root, operation="outer"):
                with self.assertRaises(WriteLockError) as raised:
                    commands.publish_draft(root, draft, "Draft Title")
                self.assertFalse(target.exists())

            self.assertEqual("write_lock_active", raised.exception.classification)

    def test_read_only_lint_and_status_run_under_active_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            commands.init_repository(root)

            with acquire_write_lock(root, operation="outer"):
                issues = commands.lint_repository(root)
                status = commands.status_repository(root)

            self.assertEqual([], issues)
            self.assertEqual(0, status["lint_issues"])

    def test_cli_write_lock_conflict_is_one_line_redacted_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            commands.init_repository(root)
            secret_dir = temp / SENTINEL_SECRET
            secret_dir.mkdir()
            source = source_file(secret_dir)
            env = os.environ.copy()
            env["KB_LLM_API_KEY"] = SENTINEL_SECRET

            with acquire_write_lock(root, operation=f"outer {SENTINEL_SECRET}"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "kb",
                        "ingest",
                        "--root",
                        str(root),
                        str(source),
                    ],
                    cwd=PROJECT_ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

            self.assertEqual(1, completed.returncode)
            lines = [line for line in completed.stderr.splitlines() if line.strip()]
            self.assertEqual(1, len(lines), completed.stderr)
            self.assertIn("write_lock_active", lines[0])
            self.assertNotIn("Traceback", completed.stderr)
            self.assertNotIn(SENTINEL_SECRET, completed.stderr)


if __name__ == "__main__":
    unittest.main()
