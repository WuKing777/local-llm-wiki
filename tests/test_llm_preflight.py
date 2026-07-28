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
from kb.llm_preflight import llm_preflight
from kb.locks import acquire_write_lock
from kb.paths import KnowledgeBasePaths
from kb.sources import read_source_card, write_source_card


SENTINEL_API_KEY = "".join(
    ["s", "k", "-", "task9", "-", "sentinel", "-", "0000000000000000"]
)
ALLOWED_AUDIT_KEYS = {
    "timestamp",
    "provider_class",
    "model_label",
    "prompt_hash",
    "context_sources",
    "context_chunks",
    "privacy_levels",
    "policy_decision",
    "confirmation_summary",
    "classification",
    "redaction_version",
    "latency_ms",
    "attempt_count",
}
FORBIDDEN_AUDIT_KEYS = {
    "messages",
    "prompt",
    "response",
    "content",
    "source_text",
    "chunk_text",
    "raw_text",
    "query",
    "exception",
    "api_key",
    "authorization",
    "bearer",
}


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


class RuntimeErrorClient:
    def __init__(self, message: str):
        self.message = message
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        raise RuntimeError(self.message)


def configured_env(api_key: str | None = None) -> dict[str, str]:
    env = {
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "fake-local-model",
    }
    if api_key is not None:
        env["KB_LLM_API_KEY"] = api_key
    return env


def create_indexed_root(temp: Path, text: str = "Alpha beta evidence.") -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


def set_source_privacy(root: Path, source_id: str, privacy: str) -> None:
    paths = KnowledgeBasePaths(root)
    card = read_source_card(paths.sources / f"{source_id}.md")
    card["privacy"] = privacy
    write_source_card(paths, card)


def claim_for(source_id: str, text: str = "Alpha beta evidence") -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": text}],
    }


def envelope(
    source_id: str,
    *,
    body: str | None = None,
    claims: list[dict[str, object]] | None = None,
) -> str:
    if body is None:
        body = f"# Alpha Page\n\nAlpha beta evidence {source_id}."
    return json.dumps({"body": body, "claims": claims or [claim_for(source_id)]})


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    if not root.exists():
        return []
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def assert_no_forbidden_audit(testcase: unittest.TestCase, metadata: dict[str, object]) -> None:
    testcase.assertEqual(ALLOWED_AUDIT_KEYS, set(metadata))
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True).casefold()
    for key in FORBIDDEN_AUDIT_KEYS:
        testcase.assertNotIn(f'"{key}"', serialized)
    testcase.assertNotIn(SENTINEL_API_KEY.casefold(), serialized)


class LLMPreflightTests(unittest.TestCase):
    def test_missing_config_returns_missing_config_without_creating_missing_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing-root"
            client = FakeClient("{}")

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                client=client,
                env={},
            )

            self.assertEqual("failed", result.status)
            self.assertEqual("missing_config", result.classification)
            self.assertEqual([], client.calls)
            self.assertFalse(root.exists())

    def test_empty_context_returns_empty_context_without_call_or_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir), "unrelated material")
            before = tree_snapshot(root)
            client = FakeClient("{}")

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                client=client,
                env=configured_env(),
            )

            self.assertEqual("empty_context", result.classification)
            self.assertEqual([], client.calls)
            self.assertEqual(before, tree_snapshot(root))

    def test_invalid_json_is_classified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient("{not json")

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                client=client,
                env=configured_env(),
            )

            self.assertEqual("invalid_json", result.classification)
            self.assertEqual(1, len(client.calls))
            self.assertEqual(before, tree_snapshot(root))

    def test_unsupported_heading_is_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(
                envelope(
                    source_id,
                    body=f"# Alpha Page\n\nAlpha beta evidence {source_id}.\n\n## Extra",
                )
            )

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                client=client,
                env=configured_env(),
            )

            self.assertEqual("unsupported_heading", result.classification)

    def test_invalid_claim_evidence_is_classified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient(
                envelope(
                    source_id,
                    claims=[
                        {
                            "claim_id": "claim-1",
                            "paragraph": 1,
                            "text": "Alpha beta evidence",
                            "evidence": [
                                {"chunk": f"{source_id}#999", "quote": "Alpha beta evidence"}
                            ],
                        }
                    ],
                )
            )

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                client=client,
                env=configured_env(),
            )

            self.assertEqual("invalid_claim_evidence", result.classification)
            self.assertEqual(before, tree_snapshot(root))

    def test_secret_leak_blocked_without_audit_or_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient(f"leaked {SENTINEL_API_KEY}")

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                write_audit=True,
                client=client,
                env=configured_env(SENTINEL_API_KEY),
            )

            self.assertEqual("secret_leak_blocked", result.classification)
            self.assertEqual(1, len(client.calls))
            self.assertFalse((root / "meta" / "llm-audit.jsonl").exists())
            self.assertEqual(before, tree_snapshot(root))

    def test_outbound_configured_secret_blocks_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient(envelope(source_id))

            result = llm_preflight(
                root,
                f"alpha beta {SENTINEL_API_KEY}",
                "Alpha Page",
                write_audit=True,
                client=client,
                env=configured_env(SENTINEL_API_KEY),
            )

            self.assertEqual("secret_leak_blocked", result.classification)
            self.assertEqual([], client.calls)
            self.assertFalse((root / "meta" / "llm-audit.jsonl").exists())
            self.assertEqual(before, tree_snapshot(root))

    def test_constructed_prompt_secret_blocks_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient(envelope(source_id))

            with mock.patch(
                "kb.llm_preflight.build_prompt_messages",
                return_value=[
                    {"role": "system", "content": "contract"},
                    {"role": "user", "content": f"alpha beta {SENTINEL_API_KEY}"},
                ],
            ):
                result = llm_preflight(
                    root,
                    "alpha beta",
                    "Alpha Page",
                    write_audit=True,
                    client=client,
                    env=configured_env(SENTINEL_API_KEY),
                )

            self.assertEqual("secret_leak_blocked", result.classification)
            self.assertEqual([], client.calls)
            self.assertFalse((root / "meta" / "llm-audit.jsonl").exists())
            self.assertEqual(before, tree_snapshot(root))

    def test_valid_source_backed_pass_returns_redacted_allowlisted_metadata_without_persisting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            query = "alpha beta"
            root, source_id = create_indexed_root(Path(tmpdir))
            before = tree_snapshot(root)
            client = FakeClient(envelope(source_id))

            result = llm_preflight(
                root,
                query,
                "Alpha Page",
                client=client,
                env=configured_env(SENTINEL_API_KEY),
            )

            self.assertEqual("pass", result.status)
            self.assertEqual("pass", result.classification)
            metadata = result.details["audit_metadata"]
            assert_no_forbidden_audit(self, metadata)
            self.assertEqual(["public"], metadata["privacy_levels"])
            self.assertEqual([source_id], metadata["context_sources"])
            self.assertNotIn(query, json.dumps(result.to_dict(), ensure_ascii=False))
            self.assertFalse((root / "meta" / "llm-audit.jsonl").exists())
            self.assertEqual(before, tree_snapshot(root))

    def test_active_lock_blocks_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(envelope(source_id))

            with acquire_write_lock(root, operation="outer"):
                result = llm_preflight(
                    root,
                    "alpha beta",
                    "Alpha Page",
                    client=client,
                    env=configured_env(),
                )

            self.assertEqual("write_lock_active", result.classification)
            self.assertEqual([], client.calls)

    def test_stale_lock_candidate_blocks_before_fake_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            payload = {
                "pid": os.getpid(),
                "process_name": "test",
                "started_at": "2000-01-01T00:00:00+00:00",
                "operation": "outer",
                "engine_version": "test",
                "nonce": "stale-lock-test",
                "host": "localhost",
                "cwd": str(root),
                "heartbeat_at": "2000-01-01T00:00:00+00:00",
                "lease_seconds": 1,
            }
            (root / "meta" / ".kb-write.lock").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            client = FakeClient(envelope(source_id))

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                write_audit=True,
                client=client,
                env=configured_env(),
            )

            self.assertEqual("stale_lock_candidate", result.classification)
            self.assertEqual([], client.calls)
            self.assertFalse((root / "meta" / "llm-audit.jsonl").exists())

    def test_sensitive_source_requires_confirmation_and_then_allows_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            set_source_privacy(root, source_id, "sensitive")
            client = FakeClient(envelope(source_id))

            with mock.patch(
                "kb.llm_preflight.build_prompt_messages",
                side_effect=AssertionError("prompt must not be built before policy gate"),
            ):
                blocked = llm_preflight(
                    root,
                    "alpha beta",
                    "Alpha Page",
                    client=client,
                    env=configured_env(),
                )
            confirmed = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                privacy_confirmation={
                    "provider": "openai_compatible",
                    "source_ids": [source_id],
                    "timestamp": "2026-07-06T00:00:00Z",
                    "summary": "User approved sensitive source preflight.",
                },
                client=client,
                env=configured_env(),
            )

            self.assertEqual("policy_confirmation_required", blocked.classification)
            self.assertEqual("pass", confirmed.classification)
            self.assertEqual(1, len(client.calls))

    def test_restricted_source_is_blocked_unless_explicitly_confirmed_for_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            set_source_privacy(root, source_id, "restricted")
            client = FakeClient(envelope(source_id))

            blocked = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                privacy_confirmation={
                    "provider": "wrong-provider",
                    "source_ids": [source_id],
                    "restricted_source_ids": [source_id],
                    "timestamp": "2026-07-06T00:00:00Z",
                    "summary": "Wrong provider.",
                },
                client=client,
                env=configured_env(),
            )
            confirmed = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                privacy_confirmation={
                    "provider": "openai_compatible",
                    "source_ids": [source_id],
                    "restricted_source_ids": [source_id],
                    "timestamp": "2026-07-06T00:00:00Z",
                    "summary": "User approved restricted source for this provider.",
                },
                client=client,
                env=configured_env(),
            )

            self.assertEqual("policy_blocked", blocked.classification)
            self.assertEqual("pass", confirmed.classification)
            self.assertEqual(1, len(client.calls))

    def test_restricted_source_rejects_boolean_cli_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            set_source_privacy(root, source_id, "restricted")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, configured_env(), clear=True):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "llm-preflight",
                            "--root",
                            str(root),
                            "--query",
                            "alpha beta",
                            "--title",
                            "Alpha Page",
                            "--confirm-privacy",
                            "true",
                            "--offline",
                            "--json",
                        ]
                    )

            self.assertEqual(1, code)
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual("policy_blocked", data["classification"])
            assert_no_forbidden_audit(self, data["details"]["audit_metadata"])

    def test_write_audit_persists_single_allowlisted_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(envelope(source_id))

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                write_audit=True,
                client=client,
                env=configured_env(SENTINEL_API_KEY),
            )

            self.assertEqual("pass", result.classification)
            audit_path = root / "meta" / "llm-audit.jsonl"
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            record = json.loads(lines[0])
            assert_no_forbidden_audit(self, record)
            self.assertEqual("pass", record["classification"])
            self.assertEqual([source_id], record["context_sources"])

    def test_offline_mode_returns_configured_but_unverified_without_client_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_indexed_root(Path(tmpdir))
            client = FakeClient(envelope(source_id))

            result = llm_preflight(
                root,
                "alpha beta",
                "Alpha Page",
                offline=True,
                client=client,
                env=configured_env(),
            )

            self.assertEqual("pass", result.status)
            self.assertEqual("configured_but_unverified", result.classification)
            self.assertEqual([], client.calls)

    def test_provider_failures_are_classified_without_trace_details(self):
        cases = {
            "401 unauthorized": "auth_failure",
            "request timed out": "timeout",
            "connection refused": "network_failure",
            "provider exploded": "provider_error",
        }
        for message, classification in cases.items():
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root, _source_id = create_indexed_root(Path(tmpdir))
                    client = RuntimeErrorClient(message)

                    result = llm_preflight(
                        root,
                        "alpha beta",
                        "Alpha Page",
                        client=client,
                        env=configured_env(SENTINEL_API_KEY),
                    )

                    self.assertEqual(classification, result.classification)
                    serialized = result.to_json()
                    self.assertNotIn(SENTINEL_API_KEY, serialized)
                    self.assertNotIn("Traceback", serialized)

    def test_cli_json_offline_success_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_indexed_root(Path(tmpdir))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, configured_env(), clear=True):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "llm-preflight",
                            "--root",
                            str(root),
                            "--query",
                            "alpha beta",
                            "--title",
                            "Alpha Page",
                            "--offline",
                            "--json",
                        ]
                    )

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual("configured_but_unverified", data["classification"])
            assert_no_forbidden_audit(self, data["details"]["audit_metadata"])

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "llm-preflight",
                            "--root",
                            str(root),
                            "--query",
                            "alpha beta",
                            "--title",
                            "Alpha Page",
                            "--offline",
                            "--json",
                        ]
                    )

            self.assertEqual(1, code)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual("missing_config", json.loads(stdout.getvalue())["classification"])


if __name__ == "__main__":
    unittest.main()
