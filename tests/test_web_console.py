import http.client
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import init_repository


SENTINEL = "".join(["s", "k", "-", "web-console", "-sentinel", "-", "0" * 12])


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


def get_text(host: str, port: int, path: str) -> tuple[int, str, str]:
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type") or ""
        return response.status, content_type, body
    finally:
        connection.close()


@contextmanager
def isolated_product_env(base: Path):
    with mock.patch.dict(
        os.environ,
        {
            "APPDATA": str(base / "isolated-appdata"),
            "LOCALAPPDATA": str(base / "isolated-localappdata"),
        },
    ):
        yield


class WebConsoleTests(unittest.TestCase):
    def test_loopback_server_exposes_standalone_html_and_redacted_json(self):
        from kb.web_console import start_web_console_server

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = create_root(base)
            with isolated_product_env(base):
                server = start_web_console_server(root, host="127.0.0.1", port=0)
                try:
                    host, port = server.server_address
                    html_status, html_type, html = get_text(host, port, "/")
                    json_status, json_type, payload = get_text(host, port, "/state.json")
                finally:
                    server.shutdown()

        self.assertEqual(200, html_status)
        self.assertIn("text/html", html_type)
        self.assertIn("Local Web Console", html)
        self.assertIn("Local-first", html)
        self.assertIn("AI is not a fact source", html)
        self.assertIn("Copy command", html)
        self.assertIn("No browser route executes mutating actions", html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("https://", html.lower())
        self.assertNotIn("http://", html.lower().replace("http://127.0.0.1", ""))

        self.assertEqual(200, json_status)
        self.assertIn("application/json", json_type)
        data = json.loads(payload)
        self.assertEqual(0, data["profile_registry"]["profile_count"])
        self.assertEqual("read_only", data["web_console"]["mode"])
        self.assertEqual("127.0.0.1", data["web_console"]["host"])
        self.assertTrue(data["web_console"]["loopback_only"])
        self.assertIn("/", data["web_console"]["routes"])
        self.assertIn("/state.json", data["web_console"]["routes"])
        self.assertTrue(data["actions"])
        self.assertTrue(all(action.get("executes") is False for action in data["actions"]))
        self.assertNotIn(SENTINEL, payload)

    def test_rejects_non_loopback_hosts_by_default(self):
        from kb.web_console import start_web_console_server

        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))
            for host in ("0.0.0.0", "::", "192.168.1.10"):
                with self.subTest(host=host):
                    with self.assertRaisesRegex(RuntimeError, "loopback"):
                        start_web_console_server(root, host=host, port=0)

    def test_first_run_links_resolve_inside_loopback_console(self):
        from kb.web_console import start_web_console_server

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = create_root(base)
            with isolated_product_env(base):
                server = start_web_console_server(root, host="127.0.0.1", port=0)
                try:
                    host, port = server.server_address
                    html_status, html_type, html = get_text(host, port, "/")
                    links = re.findall(r'href="([^"]+)"', html)
                    first_run_links = [
                        link
                        for link in links
                        if link.startswith("docs/product/")
                    ]
                    link_results = {
                        link: get_text(host, port, f"/{link}") for link in first_run_links
                    }
                finally:
                    server.shutdown()

        self.assertEqual(200, html_status)
        self.assertIn("text/html", html_type)
        self.assertEqual(
            [
                "docs/product/quickstart-zh.md",
                "docs/product/first-run-demo.md",
                "docs/product/command-guide.md",
            ],
            first_run_links,
        )
        for link, (status, content_type, body) in link_results.items():
            with self.subTest(link=link):
                self.assertEqual(200, status)
                self.assertIn("charset=utf-8", content_type)
                self.assertTrue(body.strip())
                self.assertNotIn("<script", body.lower())

    def test_browser_methods_do_not_execute_mutating_actions(self):
        from kb.web_console import start_web_console_server

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = create_root(base)
            before = tree_snapshot(root)
            with isolated_product_env(base):
                server = start_web_console_server(root, host="127.0.0.1", port=0)
                try:
                    host, port = server.server_address
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    try:
                        connection.request("POST", "/actions/publish-draft")
                        response = connection.getresponse()
                        body = response.read().decode("utf-8")
                    finally:
                        connection.close()
                finally:
                    server.shutdown()

            self.assertEqual(405, response.status)
            self.assertIn("read-only", body)
            self.assertEqual(before, tree_snapshot(root))

    def test_html_escapes_dynamic_state_and_redacts_secret_shapes(self):
        from kb.web_console import render_web_console_html, web_console_state

        fake_state = {
            "schema_version": 1,
            "redaction_version": "redaction-v1",
            "root": {
                "path": "C:\\Users\\Alice\\Secret, Vault; private notes",
                "exists": True,
                "initialized": True,
            },
            "profile_registry": {
                "status": "pass",
                "classification": "ok",
                "profile_count": 1,
                "selected_profile_id": "<img src=x onerror=alert(2)>",
            },
            "health": {
                "status": "warning",
                "check_count": 1,
                "failed_check_ids": [],
                "warning_check_ids": ["llm-config"],
            },
            "dependencies": {
                "llm": {"status": "warning", "summary": SENTINEL},
                "ocr": {"status": "pass", "summary": "ok"},
                "embedding": {"status": "warning", "summary": "missing"},
            },
            "backup": {"status": "warning", "summary": "missing"},
            "governance": {
                "lint": {"status": "pass", "summary": "ok"},
                "status": {"status": "pass", "summary": "ok"},
                "governance": {"status": "warning", "summary": "advisory"},
            },
            "obsidian": {
                "status": "warning",
                "summary": "not configured",
                "open_command": {
                    "kind": "obsidian_uri",
                    "uri": "obsidian://open?vault=Secret%20Vault",
                    "executes": False,
                },
            },
            "actions": [
                {
                    "id": "publish-draft",
                    "label": "<b>Publish</b>",
                    "command": "publish-draft",
                    "executes": False,
                    "requires_confirmation": True,
                },
                {
                    "id": "open-obsidian",
                    "label": "Open Obsidian",
                    "command": "obsidian://open?vault=Secret%20Vault",
                    "executes": False,
                    "requires_confirmation": False,
                }
            ],
            "notices": ["AI is not a fact source"],
        }

        state = web_console_state(fake_state, host="127.0.0.1", port=8765)
        html = render_web_console_html(state)
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)

        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertNotIn("<b>Publish</b>", html)
        self.assertIn("&lt;b&gt;Publish&lt;/b&gt;", html)
        self.assertIn("<local-path>", payload)
        self.assertIn("obsidian://open?vault=<local-root>", payload)
        self.assertNotIn("Secret Vault", html)
        self.assertNotIn("Secret%20Vault", html)
        self.assertNotIn("Secret, Vault", html)
        self.assertNotIn("private notes", html)
        self.assertNotIn("Secret Vault", payload)
        self.assertNotIn("Secret%20Vault", payload)
        self.assertNotIn("Secret, Vault", payload)
        self.assertNotIn("private notes", payload)
        self.assertNotIn(SENTINEL, html)
        self.assertNotIn(SENTINEL, payload)

    def test_cli_web_console_dispatches_loopback_no_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch("kb.cli.serve_web_console") as serve_mock, redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                serve_mock.side_effect = lambda *args, **kwargs: print(
                    "Local web console: http://127.0.0.1:8765/"
                )
                code = main(
                    [
                        "web-console",
                        "--root",
                        str(root),
                        "--port",
                        "0",
                        "--no-open",
                    ]
                )

            self.assertEqual(0, code, stderr.getvalue())
            serve_mock.assert_called_once()
            call = serve_mock.call_args
            self.assertEqual(str(root), str(call.kwargs["root"]))
            self.assertEqual("127.0.0.1", call.kwargs["host"])
            self.assertEqual(0, call.kwargs["port"])
            self.assertFalse(call.kwargs["open_browser"])
            self.assertIn("http://127.0.0.1", stdout.getvalue())

    def test_cli_rejects_non_loopback_host_without_starting_server(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = create_root(Path(tmpdir))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch("kb.cli.serve_web_console") as serve_mock, redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                code = main(
                    [
                        "web-console",
                        "--root",
                        str(root),
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "0",
                        "--no-open",
                    ]
                )

            self.assertNotEqual(0, code)
            serve_mock.assert_not_called()
            self.assertIn("loopback", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
