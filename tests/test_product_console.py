import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import init_repository
from kb.profile_registry import add_or_update_profile
from kb.product_paths import registry_path


REQUIRED_ACTIONS = {
    "record-memory",
    "ingest-inbox",
    "rebuild-index",
    "generate-draft",
    "validate-draft",
    "publish-draft",
    "run-governance",
    "create-backup",
    "restore-to-new-directory",
    "run-eval-search",
    "inspect-trust-report",
    "open-obsidian",
    "create-import-knowledge-base",
}
FORBIDDEN_ACTIONS = {
    "write-stable-wiki",
    "put-wiki-page",
    "direct-stable-wiki-body-write",
    "write-wiki-page",
}
CONFIRMATION_REQUIRED_ACTIONS = {
    "record-memory",
    "ingest-inbox",
    "rebuild-index",
    "generate-draft",
    "publish-draft",
    "run-governance",
    "create-backup",
    "restore-to-new-directory",
    "create-import-knowledge-base",
}
SENTINEL = "".join(["s", "k", "-", "task12", "-sentinel", "-000000000000"])


def create_root(base: Path) -> Path:
    root = base / "kb"
    init_repository(root)
    return root


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


class ProductConsoleTests(unittest.TestCase):
    def test_state_returns_json_serializable_summaries_and_required_actions(self):
        from kb.product_console import product_console_state

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = create_root(base)
            appdata = base / "appdata"
            registry = registry_path(env={"APPDATA": str(appdata)})
            add_or_update_profile(
                registry,
                name="Synthetic Root",
                root=root,
                kind="test",
            )

            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=False):
                state = product_console_state(root)

            payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
            self.assertIsInstance(state, dict)
            self.assertEqual(str(root.resolve()), state["root"]["path"])
            self.assertIn(state["health"]["status"], {"pass", "warning", "failed"})
            self.assertIn("llm", state["dependencies"])
            self.assertIn("ocr", state["dependencies"])
            self.assertIn("embedding", state["dependencies"])
            self.assertIn("backup", state)
            self.assertIn("governance", state)
            self.assertIn("obsidian", state)
            self.assertIn("AI is not a fact source", " ".join(state["notices"]))
            self.assertEqual(1, state["profile_registry"]["profile_count"])
            self.assertEqual("test", state["profile_registry"]["selected_profile_kind"])
            self.assertNotIn("Synthetic Root", payload)
            self.assertNotIn(str(root.resolve()), payload.replace(state["root"]["path"], ""))

            action_ids = {action["id"] for action in state["actions"]}
            self.assertTrue(REQUIRED_ACTIONS.issubset(action_ids))
            self.assertTrue(FORBIDDEN_ACTIONS.isdisjoint(action_ids))

    def test_state_uses_doctor_offline_and_does_not_write_by_default(self):
        from kb.product_console import product_console_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))
            before = tree_snapshot(root)
            fake_report = {
                "root": str(root),
                "status": "warning",
                "checks": [
                    {
                        "id": "llm-config",
                        "status": "warning",
                        "classification": "llm_config_missing_or_invalid",
                        "severity": "advisory",
                        "summary": f"LLM unavailable {SENTINEL}",
                        "details": {"api_key": SENTINEL},
                    },
                    {
                        "id": "tesseract",
                        "status": "warning",
                        "classification": "tesseract_missing",
                        "severity": "advisory",
                        "summary": "OCR missing.",
                    },
                    {
                        "id": "embedding-config",
                        "status": "warning",
                        "classification": "embedding_config_missing_or_invalid",
                        "severity": "advisory",
                        "summary": "Embedding missing.",
                    },
                ],
            }

            with mock.patch("kb.product_console.doctor", return_value=fake_report) as doctor_mock:
                state = product_console_state(root)

            doctor_mock.assert_called_once()
            self.assertEqual(str(root), str(doctor_mock.call_args.args[0]))
            self.assertEqual({"online": False}, doctor_mock.call_args.kwargs)
            self.assertEqual(before, tree_snapshot(root))
            serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn("Traceback", serialized)

    def test_actions_are_descriptors_routed_through_gateway_or_command_wrappers(self):
        from kb.product_console import product_console_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))

            state = product_console_state(root)

            actions = {action["id"]: action for action in state["actions"]}
            for action_id, action in actions.items():
                if action_id == "open-obsidian":
                    self.assertEqual("local_open_descriptor", action["transport"])
                    self.assertFalse(action["executes"])
                else:
                    self.assertIn(action["transport"], {"policy_gateway", "kb_command"})
                if action_id in CONFIRMATION_REQUIRED_ACTIONS:
                    self.assertTrue(action["requires_confirmation"], action_id)
                self.assertNotIn("stable wiki body", json.dumps(action, ensure_ascii=False).lower())

    def test_state_redacts_registry_doctor_and_environment_secret_shapes(self):
        from kb.product_console import product_console_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))
            fake_profile = {
                "id": "profile-redacted",
                "name": f"Unsafe {SENTINEL}",
                "root": str(root),
                "kind": "test",
                "created_at": "2026-07-06T00:00:00+08:00",
                "last_health_status": SENTINEL,
                "last_health_at": None,
            }
            fake_report = {
                "root": str(root),
                "status": "warning",
                "checks": [
                    {
                        "id": "llm-config",
                        "status": "warning",
                        "classification": "llm_config_missing_or_invalid",
                        "summary": f"provider returned {SENTINEL}",
                        "details": {"token": SENTINEL},
                    }
                ],
            }

            with mock.patch.dict(
                os.environ,
                {
                    "KB_LLM_API_KEY": SENTINEL,
                    "KB_EMBEDDING_API_KEY": SENTINEL,
                },
                clear=False,
            ), mock.patch("kb.product_console.list_profiles", return_value=[fake_profile]), mock.patch(
                "kb.product_console.doctor", return_value=fake_report
            ):
                state = product_console_state(root)

            serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn("api_key", serialized.casefold())
            self.assertNotIn("bearer", serialized.casefold())
            self.assertNotIn("capability", serialized.casefold())
            self.assertNotIn("prompt", serialized.casefold())
            self.assertNotIn("response", serialized.casefold())
            self.assertNotIn("source_text", serialized.casefold())
            self.assertNotIn("chunk_text", serialized.casefold())

    def test_cli_product_console_json_smoke_exits_zero_with_redacted_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = create_root(base)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {"APPDATA": str(base / "appdata"), "KB_LLM_API_KEY": SENTINEL},
                clear=False,
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["product-console", "--root", str(root), "--json"])

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual(str(root.resolve()), data["root"]["path"])
            self.assertNotIn(SENTINEL, stdout.getvalue())
            self.assertIn("actions", data)


if __name__ == "__main__":
    unittest.main()
