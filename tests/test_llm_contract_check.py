import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import kb.commands as commands
from kb.commands import ingest_file, init_repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_API_KEY = "fake-sentinel-key-for-contract-check-tests"


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["KB_LLM_API_KEY"] = SENTINEL_API_KEY
    return env


def run_contract_check(
    response_path: Path,
    title: str = "Draft Title",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "kb",
            "llm-contract-check",
            "--response",
            str(response_path),
            "--title",
            title,
        ],
        cwd=PROJECT_ROOT,
        env=subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def claim(
    source_id: str = "src-012345abcdef",
    text: str = "Alpha beta evidence",
) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": text}],
    }


def envelope(
    body: str = "# Draft Title\n\nAlpha beta evidence src-012345abcdef.",
    claims: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps({"body": body, "claims": claims or [claim()]})


def write_response(directory: Path, content: str) -> Path:
    path = directory / "response.json"
    path.write_text(content, encoding="utf-8")
    return path


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


class RuntimeErrorClient:
    def complete(self, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("LLM request failed while reading response")


def configured_env() -> dict[str, str]:
    return {
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "fake-local-model",
        "KB_LLM_API_KEY": SENTINEL_API_KEY,
    }


def create_root_with_source(temp: Path, text: str) -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


class LLMContractCheckTests(unittest.TestCase):
    def assert_status(
        self,
        completed: subprocess.CompletedProcess[str],
        status: str,
        returncode: int,
    ) -> None:
        self.assertEqual(returncode, completed.returncode, completed.stderr)
        self.assertEqual(f"status: {status}\n", completed.stdout)
        self.assertEqual("", completed.stderr)
        self.assertNotIn(SENTINEL_API_KEY, completed.stdout)
        self.assertNotIn(SENTINEL_API_KEY, completed.stderr)

    def test_valid_response_passes_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "meta").mkdir()
            (root / "wiki").mkdir()
            (root / "meta" / "log.md").write_text("log before\n", encoding="utf-8")
            (root / "meta" / "review-queue.md").write_text(
                "review before\n", encoding="utf-8"
            )
            response = write_response(root, envelope())
            before = tree_snapshot(root)

            completed = run_contract_check(response)

            self.assert_status(completed, "pass", 0)
            self.assertEqual(before, tree_snapshot(root))

    def test_invalid_json_is_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            response = write_response(Path(tmpdir), "{not json " + SENTINEL_API_KEY)

            completed = run_contract_check(response)

            self.assert_status(completed, "invalid_json", 1)

    def test_invalid_claim_shape_is_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            response = write_response(
                Path(tmpdir),
                json.dumps(
                    {
                        "body": "# Draft Title\n\nAlpha beta evidence.",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "paragraph": 1,
                                "text": "Alpha beta evidence",
                                "evidence": [{"chunk": "src-012345abcdef#0"}],
                            }
                        ],
                    }
                ),
            )

            completed = run_contract_check(response)

            self.assert_status(completed, "invalid_claim_shape", 1)

    def test_unsupported_heading_is_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            response = write_response(
                Path(tmpdir),
                envelope(
                    body=(
                        "# Draft Title\n\n"
                        "Alpha beta evidence src-012345abcdef.\n\n"
                        "## Unsupported section"
                    )
                ),
            )

            completed = run_contract_check(response)

            self.assert_status(completed, "unsupported_heading", 1)

    def test_read_failure_is_classified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_response = Path(tmpdir) / "missing.json"

            completed = run_contract_check(missing_response)

            self.assert_status(completed, "read_failure", 1)

    def test_live_contract_check_calls_adapter_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "Alpha beta evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            before = tree_snapshot(root)
            client = FakeClient(
                envelope(
                    body=f"# Draft Title\n\n{quote} {source_id}.",
                    claims=[claim(source_id, quote)],
                )
            )

            result = commands.llm_contract_check(
                title="Draft Title",
                root=root,
                query="Alpha beta",
                client=client,
                env=configured_env(),
            )

            self.assertEqual({"status": "pass"}, result)
            self.assertEqual(1, len(client.calls))
            self.assertEqual(before, tree_snapshot(root))

    def test_live_contract_check_classifies_adapter_read_failure_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir), "Alpha beta evidence")
            before = tree_snapshot(root)

            result = commands.llm_contract_check(
                title="Draft Title",
                root=root,
                query="Alpha beta",
                client=RuntimeErrorClient(),
                env=configured_env(),
            )

            self.assertEqual({"status": "read_failure"}, result)
            self.assertEqual(before, tree_snapshot(root))

    def test_live_contract_check_rejects_claim_evidence_outside_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            quote = "Alpha beta evidence"
            root, source_id = create_root_with_source(Path(tmpdir), quote)
            before = tree_snapshot(root)
            client = FakeClient(
                envelope(
                    body=f"# Draft Title\n\n{quote} {source_id}.",
                    claims=[
                        claim(
                            source_id,
                            quote,
                        )
                        | {
                            "evidence": [
                                {"chunk": f"{source_id}#999", "quote": quote}
                            ]
                        }
                    ],
                )
            )

            result = commands.llm_contract_check(
                title="Draft Title",
                root=root,
                query="Alpha beta",
                client=client,
                env=configured_env(),
            )

            self.assertEqual({"status": "invalid_claim_shape"}, result)
            self.assertEqual(1, len(client.calls))
            self.assertEqual(before, tree_snapshot(root))

    def test_live_contract_check_empty_context_is_allowed_status_without_llm_call_or_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir), "Alpha beta evidence")
            before = tree_snapshot(root)
            client = FakeClient(envelope())

            result = commands.llm_contract_check(
                title="Draft Title",
                root=root,
                query="no matching evidence",
                client=client,
                env=configured_env(),
            )

            self.assertEqual({"status": "read_failure"}, result)
            self.assertEqual(0, len(client.calls))
            self.assertEqual(before, tree_snapshot(root))


if __name__ == "__main__":
    unittest.main()
