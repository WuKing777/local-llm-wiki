import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import create_self_statement, ingest_file, init_repository
from kb.context import build_context_pack
from kb.personal_compile import personal_compile_request


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


def claim_for(source_id: str, text: str, quote: str) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": quote}],
    }


def draft_envelope(source_id: str, body: str, text: str, quote: str) -> str:
    return json.dumps({"body": body, "claims": [claim_for(source_id, text, quote)]})


def create_root_with_source(temp: Path, text: str) -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


class PersonalCompileTests(unittest.TestCase):
    def test_daily_request_maps_to_daily_target(self):
        request = personal_compile_request(kind="daily", date="2026-07-01")
        self.assertEqual("2026-07-01 daily", request.query)
        self.assertEqual("2026-07-01", request.title)
        self.assertEqual(r"daily\2026-07-01", request.target)

    def test_weekly_review_request_maps_to_review_target(self):
        request = personal_compile_request(kind="weekly-review", period="2026-W27")
        self.assertEqual("2026-W27 weekly review", request.query)
        self.assertEqual("2026-W27", request.title)
        self.assertEqual(r"reviews\weekly\2026-W27", request.target)

    def test_goal_request_requires_title(self):
        with self.assertRaisesRegex(RuntimeError, "title is required"):
            personal_compile_request(kind="goal")

    def test_agent_context_request_maps_to_agent_context_target(self):
        request = personal_compile_request(kind="agent-context", title="我是谁")
        self.assertEqual("self_statement_raw confirmed", request.query)
        self.assertEqual("我是谁", request.title)
        self.assertEqual(r"agent-context\我是谁", request.target)
        self.assertEqual(25, request.context_limit)

    def test_agent_context_request_matches_confirmed_self_statement_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            first = create_self_statement(
                root,
                text="外脑是唯一长期记忆层。",
                event_date="2026-07-02",
                privacy="personal",
                confidence="confirmed",
                input_method="chat",
            )
            second = create_self_statement(
                root,
                text="AI 工具输出不能单独作为事实来源。",
                event_date="2026-07-02",
                privacy="personal",
                confidence="confirmed",
                input_method="chat",
            )

            request = personal_compile_request(kind="agent-context", title="我是谁")
            context_pack = build_context_pack(
                root, request.query, limit=request.context_limit
            )

            self.assertEqual(
                {first["source_id"], second["source_id"]},
                {source["source_id"] for source in context_pack.context_sources},
            )

    def test_unknown_kind_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Invalid compile kind"):
            personal_compile_request(kind="unsupported", title="x")

    def test_compile_kind_does_not_accept_unsafe_title_path(self):
        with self.assertRaisesRegex(RuntimeError, "Unsafe target"):
            personal_compile_request(kind="project", title=r"..\escape")

    def test_cli_compile_kind_publishes_through_nested_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "Agent Context 可作为未来 Agent 的默认启动上下文。"
            root = Path(tmpdir) / "kb"
            init_repository(root)
            metadata = create_self_statement(
                root,
                text=quote,
                event_date="2026-07-02",
                privacy="personal",
                confidence="confirmed",
                input_method="chat",
            )
            source_id = metadata["source_id"]
            body = f"# 我是谁\n\n{quote} {source_id}"
            client = FakeClient(draft_envelope(source_id, body, quote, quote))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, configured_env(), clear=True):
                with mock.patch("kb.commands.OpenAICompatibleClient", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(
                            [
                                "compile-page",
                                "--root",
                                str(root),
                                "--kind",
                                "agent-context",
                                "--title",
                                "我是谁",
                            ]
                        )

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            target = root / "wiki" / "agent-context" / "我是谁.md"
            self.assertTrue(target.is_file())
            self.assertEqual(str(target.resolve()) + "\n", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
