import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.cli import main
from kb.commands import ingest_file, init_repository
from kb.paths import KnowledgeBasePaths
from kb.wiki import write_draft


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_API_KEY = "fake-sentinel-key-for-llm-draft-tests"


class FakeClient:
    def __init__(self, content: str = "Draft cites local evidence. [src-placeholder]"):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


class SequenceClient:
    def __init__(self, contents: list[str]):
        self.contents = contents
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if len(self.calls) > len(self.contents):
            return self.contents[-1]
        return self.contents[len(self.calls) - 1]


class RuntimeErrorClient:
    def __init__(self, message: str):
        self.message = message
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        raise RuntimeError(self.message)


def claim_for(
    source_id: str,
    text: str = "Persistent wiki evidence cites",
    quote: str = "Persistent wiki evidence cites",
) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": quote}],
    }


def draft_envelope(
    source_id: str,
    body: str | None = None,
    claims: list[dict[str, object]] | None = None,
) -> str:
    if body is None:
        body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
    if claims is None:
        claims = [claim_for(source_id)]
    return json.dumps({"body": body, "claims": claims})


def configured_env(api_key: str | None = None) -> dict[str, str]:
    env = {
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "fake-local-model",
    }
    if api_key is not None:
        env["KB_LLM_API_KEY"] = api_key
    return env


def deepseek_env(api_key: str = SENTINEL_API_KEY) -> dict[str, str]:
    return {
        "KB_LLM_BASE_URL": "https://api.deepseek.com/v1",
        "KB_LLM_MODEL": "deepseek-v4-flash",
        "KB_LLM_API_KEY": api_key,
    }


def subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = os.environ.copy()
    for key in (
        "KB_LLM_BASE_URL",
        "KB_LLM_MODEL",
        "KB_LLM_API_KEY",
        "KB_LLM_TIMEOUT_SECONDS",
        "KB_LLM_RESPONSE_FORMAT",
        "KB_LLM_MAX_TOKENS",
        "KB_LLM_THINKING",
        "KB_LLM_REASONING_EFFORT",
    ):
        merged.pop(key, None)
    if env:
        merged.update(env)
    return merged


def run_llm_draft_cli(root: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "kb",
            "llm-draft",
            "--root",
            str(root),
            "--query",
            "persistent wiki",
            "--title",
            "Persistent Wiki",
        ],
        cwd=PROJECT_ROOT,
        env=subprocess_env(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def create_indexed_root(
    temp: Path, text: str = "Persistent wiki evidence cites."
) -> tuple[Path, str]:
    root = temp / "wiki"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


def event_count(root: Path) -> int | None:
    database = root / "db" / "kb.sqlite3"
    if not database.exists():
        return None
    with closing(sqlite3.connect(database)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def event_messages(root: Path) -> str:
    database = root / "db" / "kb.sqlite3"
    if not database.exists():
        return ""
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT event_type, message FROM events").fetchall()
    return "\n".join(f"{row[0]} {row[1]}" for row in rows)


def snapshot_paths(root: Path) -> dict[str, object]:
    paths = {
        "drafts_exists": (root / "wiki" / "_drafts").exists(),
        "drafts_files": sorted(
            path.relative_to(root).as_posix()
            for path in (root / "wiki" / "_drafts").rglob("*")
            if path.exists()
        )
        if (root / "wiki" / "_drafts").exists()
        else [],
        "log": (root / "meta" / "log.md").read_text(encoding="utf-8")
        if (root / "meta" / "log.md").exists()
        else None,
        "review": (root / "meta" / "review-queue.md").read_text(encoding="utf-8")
        if (root / "meta" / "review-queue.md").exists()
        else None,
        "database": (root / "db" / "kb.sqlite3").read_bytes()
        if (root / "db" / "kb.sqlite3").exists()
        else None,
        "events": event_count(root),
    }
    return paths


class LLMDraftTests(unittest.TestCase):
    def assert_one_error_line(self, completed) -> None:
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertRegex(completed.stderr, r"^error: .+\n$")
        self.assertEqual(1, len(completed.stderr.splitlines()))
        self.assertNotIn("Traceback", completed.stderr)

    def assert_no_write(self, root: Path, before: dict[str, object]) -> None:
        self.assertEqual(before, snapshot_paths(root))

    def assert_model_response_rejected_without_writes(
        self,
        content: str,
        *,
        api_key: str | None = None,
        error_pattern: str = "Invalid LLM draft response",
    ) -> RuntimeError:
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            client = FakeClient(content)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaisesRegex(RuntimeError, error_pattern) as raised:
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(api_key),
                    )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            if api_key is not None:
                self.assertNotIn(api_key, str(raised.exception))
                persistent_text = "\n".join(
                    [
                        (root / "meta" / "log.md").read_text(encoding="utf-8"),
                        (root / "meta" / "review-queue.md").read_text(
                            encoding="utf-8"
                        ),
                        event_messages(root),
                    ]
                )
                self.assertNotIn(api_key, persistent_text)
                self.assertNotIn(
                    api_key.encode("utf-8"),
                    (root / "db" / "kb.sqlite3").read_bytes(),
                )
            return raised.exception

    def test_missing_config_cli_writes_one_error_line_and_does_not_touch_initialized_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            drafts = root / "wiki" / "_drafts"
            drafts.mkdir()
            (drafts / "existing.md").write_text("keep me\n", encoding="utf-8")
            before = snapshot_paths(root)

            completed = run_llm_draft_cli(root)

            self.assert_one_error_line(completed)
            self.assertIn("KB_LLM_BASE_URL", completed.stderr)
            self.assert_no_write(root, before)

    def test_missing_config_cli_does_not_create_missing_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing-root"

            completed = run_llm_draft_cli(root)

            self.assert_one_error_line(completed)
            self.assertFalse(root.exists())

    def test_missing_config_is_checked_before_init_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            client = FakeClient()

            with mock.patch.object(
                commands, "init_repository", side_effect=AssertionError("must not init")
            ):
                with self.assertRaisesRegex(RuntimeError, "KB_LLM_BASE_URL"):
                    commands.llm_draft(root, "query", "Title", client=client, env={})

            self.assertEqual([], client.calls)
            self.assertFalse(root.exists())

    def test_configured_missing_root_fails_without_creating_root_or_calling_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing-root"
            client = FakeClient()

            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual([], client.calls)
            self.assertFalse(root.exists())

    def test_direct_uninitialized_root_error_does_not_expose_api_key_in_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / SENTINEL_API_KEY / "missing-root"
            client = FakeClient()

            with self.assertRaises(RuntimeError) as raised:
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(SENTINEL_API_KEY),
                )

            self.assertIn("not initialized", str(raised.exception))
            self.assertNotIn(SENTINEL_API_KEY, str(raised.exception))
            self.assertEqual([], client.calls)
            self.assertFalse(root.exists())

    def test_cli_uninitialized_root_error_sanitizes_api_key_in_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / SENTINEL_API_KEY / "missing-root"
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "llm-draft",
                "--root",
                str(root),
                "--query",
                "persistent wiki",
                "--title",
                "Persistent Wiki",
            ]

            with mock.patch.dict(
                os.environ, subprocess_env(configured_env(SENTINEL_API_KEY)), clear=True
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(argv)

            self.assertEqual(1, code)
            self.assertEqual("", stdout.getvalue())
            self.assertRegex(stderr.getvalue(), r"^error: .+\n$")
            self.assertEqual(1, len(stderr.getvalue().splitlines()))
            self.assertNotIn(SENTINEL_API_KEY, stderr.getvalue())
            self.assertFalse(root.exists())

    def test_empty_context_no_source_cards_writes_nothing_and_does_not_call_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)
            before = snapshot_paths(root)
            client = FakeClient()

            with self.assertRaisesRegex(RuntimeError, "no-source-cards"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual([], client.calls)
            self.assert_no_write(root, before)
            completed = run_llm_draft_cli(root, configured_env())
            self.assert_one_error_line(completed)

    def test_empty_context_missing_database_writes_nothing_and_does_not_recreate_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            database = root / "db" / "kb.sqlite3"
            database.unlink()
            before = snapshot_paths(root)
            client = FakeClient()

            with self.assertRaisesRegex(RuntimeError, "empty-index"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual([], client.calls)
            self.assert_no_write(root, before)
            self.assertFalse(database.exists())

    def test_cli_empty_index_writes_one_error_line_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            database = root / "db" / "kb.sqlite3"
            database.unlink()
            before = snapshot_paths(root)

            completed = run_llm_draft_cli(root, configured_env())

            self.assert_one_error_line(completed)
            self.assertIn("empty-index", completed.stderr)
            self.assert_no_write(root, before)
            self.assertFalse(database.exists())

    def test_empty_context_no_matching_chunks_writes_nothing_and_does_not_call_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir), "apple banana")
            before = snapshot_paths(root)
            client = FakeClient()

            with self.assertRaisesRegex(RuntimeError, "no-matching-chunks"):
                commands.llm_draft(
                    root,
                    "totally absent",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual([], client.calls)
            self.assert_no_write(root, before)
            self.assertFalse((root / "wiki" / "_drafts").exists())

    def test_model_auth_failure_writes_one_redacted_error_line_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            api_key = "fake-sentinel-key-for-redaction"
            client = RuntimeErrorClient(f"401 unauthorized {api_key}")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, configured_env(api_key), clear=True):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(
                            [
                                "llm-draft",
                                "--root",
                                str(root),
                                "--query",
                                "persistent wiki",
                                "--title",
                                "Persistent Wiki",
                            ]
                        )

            self.assertEqual(1, code)
            self.assertEqual(1, len(client.calls))
            self.assertEqual("", stdout.getvalue())
            self.assertRegex(stderr.getvalue(), r"^error: .+\n$")
            self.assertNotIn(api_key, stderr.getvalue())
            self.assert_no_write(root, before)

    def test_cli_no_matching_chunks_does_not_create_client_or_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir), "apple banana")
            before = snapshot_paths(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "llm-draft",
                "--root",
                str(root),
                "--query",
                "totally absent",
                "--title",
                "Persistent Wiki",
            ]

            with mock.patch.dict(os.environ, subprocess_env(configured_env()), clear=True):
                with mock.patch(
                    "kb.commands.OpenAICompatibleClient",
                    side_effect=AssertionError("must not call LLM"),
                ):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(argv)

            self.assertEqual(1, code)
            self.assertEqual("", stdout.getvalue())
            self.assertRegex(stderr.getvalue(), r"^error: .+\n$")
            self.assertEqual(1, len(stderr.getvalue().splitlines()))
            self.assertIn("no-matching-chunks", stderr.getvalue())
            self.assert_no_write(root, before)

    def test_empty_model_content_writes_nothing_after_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            client = FakeClient("  \n")

            with self.assertRaisesRegex(RuntimeError, "empty"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)

    def test_raw_markdown_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes(
            "# Persistent Wiki\n\nRaw markdown is no longer accepted."
        )

    def test_invalid_json_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes("{not valid json")

    def test_missing_body_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes(
            json.dumps({"claims": [claim_for("src-000000000000")]})
        )

    def test_missing_claims_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes(
            json.dumps({"body": "# Persistent Wiki\n\nGrounded draft."})
        )

    def test_empty_claims_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes(
            json.dumps({"body": "# Persistent Wiki\n\nGrounded draft.", "claims": []})
        )

    def test_malformed_claim_record_response_writes_nothing_after_client_call(self):
        self.assert_model_response_rejected_without_writes(
            json.dumps(
                {
                    "body": "# Persistent Wiki\n\nGrounded draft.",
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "paragraph": 1,
                            "text": "Grounded draft",
                            "evidence": [{"chunk": "src-000000000000#0"}],
                        }
                    ],
                }
            )
        )

    def test_secret_in_json_body_fails_without_leaking_or_writing(self):
        self.assert_model_response_rejected_without_writes(
            draft_envelope(
                "src-000000000000",
                body=f"# Persistent Wiki\n\nleaked {SENTINEL_API_KEY}",
            ),
            api_key=SENTINEL_API_KEY,
            error_pattern="configured secret",
        )

    def test_secret_in_json_claim_text_fails_without_leaking_or_writing(self):
        self.assert_model_response_rejected_without_writes(
            draft_envelope(
                "src-000000000000",
                claims=[claim_for("src-000000000000", text=SENTINEL_API_KEY)],
            ),
            api_key=SENTINEL_API_KEY,
            error_pattern="configured secret",
        )

    def test_secret_in_json_evidence_quote_fails_without_leaking_or_writing(self):
        self.assert_model_response_rejected_without_writes(
            draft_envelope(
                "src-000000000000",
                claims=[claim_for("src-000000000000", quote=SENTINEL_API_KEY)],
            ),
            api_key=SENTINEL_API_KEY,
            error_pattern="configured secret",
        )

    def test_unreadable_audit_snapshot_fails_before_client_or_draft_write(self):
        for filename in ("log.md", "review-queue.md"):
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root, source_id = create_indexed_root(Path(tmpdir))
                    (root / "meta" / filename).write_bytes(b"\xff\xfe")
                    client = FakeClient(f"Draft cites local evidence. [{source_id}]")
                    draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"

                    with self.assertRaises(Exception):
                        commands.llm_draft(
                            root,
                            "persistent wiki",
                            "Persistent Wiki",
                            client=client,
                            env=configured_env(),
                        )

                    self.assertEqual([], client.calls)
                    self.assertFalse(draft_path.exists())

    def test_existing_draft_collision_does_not_rewrite_log_or_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            drafts = root / "wiki" / "_drafts"
            drafts.mkdir()
            draft_path = drafts / "persistent-wiki.md"
            draft_path.write_text("existing draft\n", encoding="utf-8")
            log_path = root / "meta" / "log.md"
            review_path = root / "meta" / "review-queue.md"
            old_ns = 1_700_000_000_000_000_000
            os.utime(log_path, ns=(old_ns, old_ns))
            os.utime(review_path, ns=(old_ns, old_ns))
            log_before = log_path.read_text(encoding="utf-8")
            review_before = review_path.read_text(encoding="utf-8")
            log_mtime = log_path.stat().st_mtime_ns
            review_mtime = review_path.stat().st_mtime_ns
            draft_before = draft_path.read_text(encoding="utf-8")
            client = FakeClient(draft_envelope(source_id))

            with self.assertRaisesRegex(RuntimeError, "Draft already exists"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual([], client.calls)
            self.assertEqual(draft_before, draft_path.read_text(encoding="utf-8"))
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))
            self.assertEqual(review_before, review_path.read_text(encoding="utf-8"))
            self.assertEqual(log_mtime, log_path.stat().st_mtime_ns)
            self.assertEqual(review_mtime, review_path.stat().st_mtime_ns)

    def test_write_draft_failure_does_not_delete_concurrently_created_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(draft_envelope(source_id))
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"

            def create_then_fail(_paths, _metadata, _body):
                draft_path.parent.mkdir(parents=True, exist_ok=True)
                draft_path.write_text("concurrent draft\n", encoding="utf-8")
                raise RuntimeError("write failed")

            with mock.patch.object(commands, "write_draft", side_effect=create_then_fail):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                    )

            self.assertEqual(1, len(client.calls))
            self.assertEqual("concurrent draft\n", draft_path.read_text(encoding="utf-8"))

    def test_write_draft_failure_removes_empty_drafts_directory_created_by_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(draft_envelope(source_id))
            drafts = root / "wiki" / "_drafts"

            def create_empty_dir_then_fail(_paths, _metadata, _body):
                drafts.mkdir(parents=True, exist_ok=True)
                raise RuntimeError("write failed")

            with mock.patch.object(
                commands, "write_draft", side_effect=create_empty_dir_then_fail
            ):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                    )

            self.assertEqual(1, len(client.calls))
            self.assertFalse(drafts.exists())

    def test_success_creates_draft_front_matter_log_and_review_without_secret_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            claims = [claim_for(source_id)]
            client = FakeClient(draft_envelope(source_id, body=body, claims=claims))
            env = configured_env(SENTINEL_API_KEY)

            result = commands.llm_draft(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=env,
            )

            from kb.context import prompt_hash
            from kb.wiki import read_draft

            draft_path = Path(result["path"])
            self.assertEqual(
                root.resolve() / "wiki" / "_drafts" / "persistent-wiki.md",
                draft_path,
            )
            self.assertTrue(draft_path.is_file())
            self.assertEqual(1, len(client.calls))
            metadata, draft_body = read_draft(draft_path)

            for field in (
                "draft_id",
                "title",
                "query",
                "created_at",
                "model",
                "prompt_hash",
                "context_sources",
                "context_chunks",
                "claims",
            ):
                self.assertIn(field, metadata)
            self.assertEqual("Persistent Wiki", metadata["title"])
            self.assertEqual("persistent wiki", metadata["query"])
            self.assertEqual("fake-local-model", metadata["model"])
            self.assertEqual(prompt_hash(client.calls[0]), metadata["prompt_hash"])
            self.assertEqual([source_id], metadata["context_sources"])
            self.assertEqual([f"{source_id}#0"], metadata["context_chunks"])
            self.assertEqual(claims, metadata["claims"])
            self.assertIn(body, draft_body)

            log_text = (root / "meta" / "log.md").read_text(encoding="utf-8")
            persistent_text = "\n".join(
                [
                    draft_path.read_text(encoding="utf-8"),
                    log_text,
                    (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
                    event_messages(root),
                ]
            )
            self.assertNotIn(SENTINEL_API_KEY, persistent_text)
            self.assertNotIn(SENTINEL_API_KEY.encode("utf-8"), (root / "db" / "kb.sqlite3").read_bytes())
            self.assertIn("llm-draft", log_text)
            self.assertIn("query=persistent wiki", log_text)
            self.assertIn("validate-draft", (root / "meta" / "review-queue.md").read_text(encoding="utf-8"))

    def test_deepseek_retries_with_sanitized_feedback_and_writes_only_retried_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            invalid_body = f"# Persistent Wiki\n\nThe wiki keeps durable evidence {source_id}."
            valid_body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            invalid_claims = [
                claim_for(
                    source_id,
                    text="The wiki keeps durable evidence",
                    quote="Persistent wiki evidence cites",
                )
            ]
            valid_claims = [claim_for(source_id)]
            client = SequenceClient(
                [
                    draft_envelope(source_id, body=invalid_body, claims=invalid_claims),
                    draft_envelope(source_id, body=valid_body, claims=valid_claims),
                ]
            )

            result = commands.llm_draft(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=deepseek_env(),
            )

            self.assertEqual(2, len(client.calls))
            self.assertIn("DeepSeek compatibility mode", client.calls[0][0]["content"])
            retry_message = client.calls[1][-1]["content"]
            self.assertIn("Previous response failed local validation", retry_message)
            self.assertIn("claim-text-not-supported-by-quote", retry_message)
            self.assertNotIn(SENTINEL_API_KEY, retry_message)
            draft_path = Path(result["path"])
            self.assertEqual(
                valid_body,
                draft_path.read_text(encoding="utf-8").split("---\n\n", 1)[1].strip(),
            )
            self.assertNotIn(
                "The wiki keeps durable evidence",
                draft_path.read_text(encoding="utf-8"),
            )

    def test_deepseek_retry_feedback_uses_full_draft_validation_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            invalid_body = (
                "# Persistent Wiki\n\n"
                f"Persistent wiki evidence cites {source_id} src-000000000000."
            )
            valid_body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            client = SequenceClient(
                [
                    draft_envelope(
                        source_id,
                        body=invalid_body,
                        claims=[claim_for(source_id)],
                    ),
                    draft_envelope(
                        source_id,
                        body=valid_body,
                        claims=[claim_for(source_id)],
                    ),
                ]
            )

            result = commands.llm_draft(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=deepseek_env(),
            )

            self.assertEqual(2, len(client.calls))
            retry_message = client.calls[1][-1]["content"]
            self.assertIn("citation-outside-context", retry_message)
            self.assertNotIn("src-000000000000", Path(result["path"]).read_text(encoding="utf-8"))

    def test_deepseek_retry_exhaustion_writes_nothing_when_claim_evidence_still_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            invalid_body = f"# Persistent Wiki\n\nThe wiki keeps durable evidence {source_id}."
            invalid_claims = [
                claim_for(
                    source_id,
                    text="The wiki keeps durable evidence",
                    quote="Persistent wiki evidence cites",
                )
            ]
            client = SequenceClient(
                [
                    draft_envelope(source_id, body=invalid_body, claims=invalid_claims),
                    draft_envelope(source_id, body=invalid_body, claims=invalid_claims),
                ]
            )

            with self.assertRaisesRegex(RuntimeError, "LLM draft failed local contract"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=deepseek_env(),
                )

            self.assertEqual(2, len(client.calls))
            self.assert_no_write(root, before)

    def test_deepseek_secret_in_first_response_fails_without_retry_or_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            secret = SENTINEL_API_KEY
            body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {secret} {source_id}."
            claims = [
                claim_for(
                    source_id,
                    text=f"Persistent wiki evidence cites {secret}",
                    quote=f"Persistent wiki evidence cites {secret}",
                )
            ]
            client = SequenceClient([draft_envelope(source_id, body=body, claims=claims)])

            with self.assertRaisesRegex(RuntimeError, "LLM draft contains configured secret"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=deepseek_env(secret),
                )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)
            persistent_text = "\n".join(
                [
                    (root / "meta" / "log.md").read_text(encoding="utf-8"),
                    (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
                    event_messages(root),
                ]
            )
            self.assertNotIn(secret, persistent_text)

    def test_deepseek_secret_in_invalid_json_fails_without_retry_or_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            secret = SENTINEL_API_KEY
            valid_body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            client = SequenceClient(
                [
                    f"raw leaked {secret}",
                    draft_envelope(source_id, body=valid_body, claims=[claim_for(source_id)]),
                ]
            )

            with self.assertRaisesRegex(RuntimeError, "LLM draft contains configured secret"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=deepseek_env(secret),
                )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)

    def test_deepseek_invalid_json_fails_without_retry_or_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            valid_body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            client = SequenceClient(
                [
                    "not json",
                    draft_envelope(source_id, body=valid_body, claims=[claim_for(source_id)]),
                ]
            )

            with self.assertRaisesRegex(RuntimeError, "Invalid LLM draft response"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=deepseek_env(),
                )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)

    def test_deepseek_empty_response_fails_without_retry_or_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            valid_body = f"# Persistent Wiki\n\nPersistent wiki evidence cites {source_id}."
            client = SequenceClient(
                [
                    "",
                    draft_envelope(source_id, body=valid_body, claims=[claim_for(source_id)]),
                ]
            )

            with self.assertRaisesRegex(RuntimeError, "LLM response content was empty"):
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=deepseek_env(),
                )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)

    def test_generic_provider_does_not_retry_invalid_claim_evidence_before_writing_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            invalid_body = f"# Persistent Wiki\n\nThe wiki keeps durable evidence {source_id}."
            invalid_claims = [
                claim_for(
                    source_id,
                    text="The wiki keeps durable evidence",
                    quote="Persistent wiki evidence cites",
                )
            ]
            client = SequenceClient(
                [
                    draft_envelope(source_id, body=invalid_body, claims=invalid_claims),
                    draft_envelope(source_id),
                ]
            )

            result = commands.llm_draft(
                root,
                "persistent wiki",
                "Persistent Wiki",
                client=client,
                env=configured_env(),
            )

            self.assertEqual(1, len(client.calls))
            self.assertIn(
                "The wiki keeps durable evidence",
                Path(result["path"]).read_text(encoding="utf-8"),
            )

    def test_provider_detection_limits_retry_to_deepseek_configs(self):
        self.assertEqual(
            "deepseek",
            commands._llm_provider(
                type("Config", (), {"base_url": "https://api.deepseek.com/v1", "model": "x"})()
            ),
        )
        self.assertEqual(
            "deepseek",
            commands._llm_provider(
                type("Config", (), {"base_url": "https://example.test/v1", "model": "deepseek-v4-flash"})()
            ),
        )
        self.assertIsNone(
            commands._llm_provider(
                type("Config", (), {"base_url": "https://example.test/v1", "model": "fake-local-model"})()
            )
        )
        self.assertEqual(
            2,
            commands._llm_draft_attempts(
                type("Config", (), {"base_url": "https://api.deepseek.com/v1", "model": "x"})()
            ),
        )
        self.assertEqual(
            1,
            commands._llm_draft_attempts(
                type("Config", (), {"base_url": "https://example.test/v1", "model": "fake-local-model"})()
            ),
        )

    def test_audit_failure_rolls_back_new_draft_and_metadata_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            client = FakeClient(draft_envelope(source_id))

            with mock.patch.object(
                commands,
                "_append_draft_audit",
                side_effect=RuntimeError("audit failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                    )

            self.assertEqual(1, len(client.calls))
            self.assert_no_write(root, before)

    def test_audit_immediate_failure_does_not_rewrite_unchanged_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            log_path = root / "meta" / "log.md"
            review_path = root / "meta" / "review-queue.md"
            old_ns = 1_700_000_001_000_000_000
            os.utime(log_path, ns=(old_ns, old_ns))
            os.utime(review_path, ns=(old_ns, old_ns))
            log_before = log_path.read_text(encoding="utf-8")
            review_before = review_path.read_text(encoding="utf-8")
            log_mtime = log_path.stat().st_mtime_ns
            review_mtime = review_path.stat().st_mtime_ns
            client = FakeClient(draft_envelope(source_id))
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"

            with mock.patch.object(
                commands,
                "_append_draft_audit",
                side_effect=RuntimeError("audit failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                    commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                    )

            self.assertEqual(1, len(client.calls))
            self.assertFalse(draft_path.exists())
            self.assertEqual(log_before, log_path.read_text(encoding="utf-8"))
            self.assertEqual(review_before, review_path.read_text(encoding="utf-8"))
            self.assertEqual(log_mtime, log_path.stat().st_mtime_ns)
            self.assertEqual(review_mtime, review_path.stat().st_mtime_ns)

    def test_write_draft_removes_file_it_created_when_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            paths = KnowledgeBasePaths(root)
            body = "partial draft body"
            metadata = {
                "draft_id": "draft-test",
                "title": "Persistent Wiki",
                "query": "persistent wiki",
                "created_at": "2026-06-24T00:00:00Z",
                "model": "fake-local-model",
                "prompt_hash": "a" * 64,
                "context_sources": ["src-000000000000"],
                "context_chunks": ["src-000000000000#0"],
                "claims": [claim_for("src-000000000000")],
            }
            draft_path = root / "wiki" / "_drafts" / "persistent-wiki.md"
            real_open = Path.open

            class FailingWriter:
                def __init__(self, path, mode, *args, **kwargs):
                    self.file = real_open(path, mode, *args, **kwargs)

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.file.close()
                    return False

                def write(self, text):
                    self.file.write(text[:8])
                    raise OSError("write failed")

            def failing_open(path, mode="r", *args, **kwargs):
                if Path(path).resolve() == draft_path.resolve() and mode in {"w", "x"}:
                    return FailingWriter(path, mode, *args, **kwargs)
                return real_open(path, mode, *args, **kwargs)

            with mock.patch("pathlib.Path.open", new=failing_open):
                with self.assertRaisesRegex(OSError, "write failed"):
                    write_draft(paths, metadata, body)

            self.assertFalse(draft_path.exists())
            self.assertFalse(draft_path.parent.exists())

    def test_secret_in_model_content_fails_without_leaking_or_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            client = FakeClient(f"leaked {SENTINEL_API_KEY}")

            with self.assertRaises(RuntimeError) as raised:
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(SENTINEL_API_KEY),
                )

            self.assertEqual(1, len(client.calls))
            self.assertNotIn(SENTINEL_API_KEY, str(raised.exception))
            self.assert_no_write(root, before)
            persistent_text = "\n".join(
                [
                    (root / "meta" / "log.md").read_text(encoding="utf-8"),
                    (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
                    event_messages(root),
                ]
            )
            self.assertNotIn(SENTINEL_API_KEY, persistent_text)
            self.assertNotIn(
                SENTINEL_API_KEY.encode("utf-8"),
                (root / "db" / "kb.sqlite3").read_bytes(),
            )

    def test_short_secret_in_model_content_fails_without_leaking_or_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_secret = "sek"
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = snapshot_paths(root)
            client = FakeClient(f"leaked {short_secret}")

            with self.assertRaises(RuntimeError) as raised:
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(short_secret),
                )

            self.assertEqual(1, len(client.calls))
            self.assertNotIn(short_secret, str(raised.exception))
            self.assert_no_write(root, before)
            persistent_text = "\n".join(
                [
                    (root / "meta" / "log.md").read_text(encoding="utf-8"),
                    (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
                    event_messages(root),
                ]
            )
            self.assertNotIn(short_secret, persistent_text)
            self.assertNotIn(
                short_secret.encode("utf-8"),
                (root / "db" / "kb.sqlite3").read_bytes(),
            )

    def test_secret_in_root_path_fails_without_leaking_to_persistent_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / SENTINEL_API_KEY / "wiki"
            source = temp / "source.md"
            source.write_text("# Source\n\npersistent wiki evidence\n", encoding="utf-8")
            init_repository(root)
            source_id = ingest_file(root, source)["source_id"]
            before = snapshot_paths(root)
            client = FakeClient(f"Draft cites local evidence. [{source_id}]")

            with self.assertRaises(RuntimeError) as raised:
                commands.llm_draft(
                    root,
                    "persistent wiki",
                    "Persistent Wiki",
                    client=client,
                    env=configured_env(SENTINEL_API_KEY),
                )

            self.assertEqual([], client.calls)
            self.assertNotIn(SENTINEL_API_KEY, str(raised.exception))
            self.assert_no_write(root, before)
            persistent_text = "\n".join(
                [
                    (root / "meta" / "log.md").read_text(encoding="utf-8"),
                    (root / "meta" / "review-queue.md").read_text(encoding="utf-8"),
                    event_messages(root),
                ]
            )
            self.assertNotIn(SENTINEL_API_KEY, persistent_text)
            self.assertNotIn(
                SENTINEL_API_KEY.encode("utf-8"),
                (root / "db" / "kb.sqlite3").read_bytes(),
            )

    def test_cli_success_outputs_path_and_does_not_leak_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(draft_envelope(source_id))
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "llm-draft",
                "--root",
                str(root),
                "--query",
                "persistent wiki",
                "--title",
                "Persistent Wiki",
            ]

            with mock.patch.dict(os.environ, subprocess_env(configured_env(SENTINEL_API_KEY)), clear=True):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(argv)

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertIn("persistent-wiki.md", stdout.getvalue())
            self.assertNotIn(SENTINEL_API_KEY, stdout.getvalue())
            self.assertNotIn(SENTINEL_API_KEY, stderr.getvalue())

    def test_source_backed_dry_run_validates_and_publishes_with_fake_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "external brain uses source evidence"
            root, source_id = create_indexed_root(Path(tmpdir), quote)
            body = f"# External Brain\n\n{quote} {source_id}."
            client = FakeClient(
                draft_envelope(
                    source_id,
                    body=body,
                    claims=[claim_for(source_id, quote, quote)],
                )
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(
                os.environ,
                configured_env("fake-sentinel-key-for-dry-run"),
                clear=True,
            ):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        draft_code = main(
                            [
                                "llm-draft",
                                "--root",
                                str(root),
                                "--query",
                                "external brain",
                                "--title",
                                "External Brain",
                            ]
                        )

            self.assertEqual(0, draft_code, stderr.getvalue())
            draft = root / "wiki" / "_drafts" / "external-brain.md"
            self.assertTrue(draft.is_file())
            validate_stdout = io.StringIO()
            validate_stderr = io.StringIO()
            with redirect_stdout(validate_stdout), redirect_stderr(validate_stderr):
                validate_code = main(
                    [
                        "validate-draft",
                        "--root",
                        str(root),
                        str(draft),
                        "--target",
                        "External Brain",
                    ]
                )
            self.assertEqual(0, validate_code, validate_stderr.getvalue())
            publish_stdout = io.StringIO()
            publish_stderr = io.StringIO()
            with redirect_stdout(publish_stdout), redirect_stderr(publish_stderr):
                publish_code = main(
                    [
                        "publish-draft",
                        "--root",
                        str(root),
                        str(draft),
                        "--target",
                        "External Brain",
                    ]
                )
            self.assertEqual(0, publish_code, publish_stderr.getvalue())
            self.assertTrue((root / "wiki" / "external-brain.md").is_file())

    def test_unsafe_titles_are_sanitized_under_drafts_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)

            from kb.paths import KnowledgeBasePaths
            from kb.wiki import draft_path_for_title

            paths = KnowledgeBasePaths(root)
            drafts = (root / "wiki" / "_drafts").resolve()
            unsafe_titles = [
                "../Escape",
                "..\\Escape",
                "/absolute/Escape",
                "C:\\absolute\\Escape",
                "raw/escape",
                "sources\\escape",
            ]

            for title in unsafe_titles:
                with self.subTest(title=title):
                    draft_path = draft_path_for_title(paths, title)
                    draft_path.resolve().relative_to(drafts)
                    self.assertEqual(".md", draft_path.suffix)
                    self.assertNotIn("..", draft_path.name)
                    self.assertNotIn("/", draft_path.name)
                    self.assertNotIn("\\", draft_path.name)

    def test_unicode_titles_keep_distinct_meaningful_slugs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "wiki"
            init_repository(root)

            from kb.paths import KnowledgeBasePaths
            from kb.wiki import draft_path_for_title

            paths = KnowledgeBasePaths(root)
            first = draft_path_for_title(paths, "个人知识库")
            second = draft_path_for_title(paths, "本地知识库")

            self.assertEqual("个人知识库.md", first.name)
            self.assertEqual("本地知识库.md", second.name)
            self.assertNotEqual(first.name, second.name)
            self.assertNotEqual("draft.md", first.name)
            self.assertNotEqual("draft.md", second.name)

    def test_llm_draft_does_not_call_init_or_initialize_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(draft_envelope(source_id))

            with mock.patch.object(
                commands, "init_repository", side_effect=AssertionError("must not init")
            ):
                with mock.patch.object(
                    commands,
                    "initialize_database",
                    side_effect=AssertionError("must not initialize"),
                ):
                    result = commands.llm_draft(
                        root,
                        "persistent wiki",
                        "Persistent Wiki",
                        client=client,
                        env=configured_env(),
                    )

            self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
