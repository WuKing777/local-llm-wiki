"""Local-only read-only web console for product state."""

from __future__ import annotations

import html
import json
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .product_console import product_console_state
from .redaction import summarize_text


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_DOC_ROUTES = {
    "/docs/product/quickstart-zh.md": PROJECT_ROOT / "docs" / "product" / "quickstart-zh.md",
    "/docs/product/first-run-demo.md": PROJECT_ROOT
    / "docs"
    / "product"
    / "first-run-demo.md",
    "/docs/product/command-guide.md": PROJECT_ROOT / "docs" / "product" / "command-guide.md",
}
ROUTES = ("/", "/state.json", *PRODUCT_DOC_ROUTES.keys())
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\|/)[^\r\n\"<>|?*]+")
_OBSIDIAN_OPEN_RE = re.compile(r"(?i)obsidian://open\?vault=[^\s\"'<>]+")


def is_loopback_host(host: str) -> bool:
    return host.casefold() in _LOOPBACK_HOSTS


def _safe_text(value: object, *, limit: int = 500) -> str:
    text = summarize_text(value, limit=limit)
    text = _WINDOWS_PATH_RE.sub("<local-path>", text)
    text = _OBSIDIAN_OPEN_RE.sub("obsidian://open?vault=<local-root>", text)
    text = re.sub(r"(?i)\bUsers\b", "<user-dir>", text)
    text = re.sub(r"(?i)\bAdministrator\b", "<account>", text)
    return text


def _safe_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(_safe_text(key)): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _safe_text(value)


def _safe_actions(actions: object) -> list[dict[str, object]]:
    safe: list[dict[str, object]] = []
    if not isinstance(actions, list):
        return safe
    for item in actions:
        if not isinstance(item, dict):
            continue
        action = _safe_json_value(item)
        if not isinstance(action, dict):
            continue
        action["executes"] = False
        safe.append(action)
    return safe


def web_console_state(
    product_state: dict[str, object], *, host: str, port: int
) -> dict[str, object]:
    state = _safe_json_value(product_state)
    if not isinstance(state, dict):
        state = {}
    state["actions"] = _safe_actions(state.get("actions", []))
    state["web_console"] = {
        "mode": "read_only",
        "host": host,
        "port": port,
        "loopback_only": True,
        "routes": list(ROUTES),
        "mutating_browser_actions": False,
    }
    return state


def _html(value: object) -> str:
    return html.escape(_safe_text(value), quote=True)


def _section_status(section: object, key: str = "status") -> str:
    if isinstance(section, dict):
        return _html(section.get(key, "unknown"))
    return "unknown"


def _summary(section: object) -> str:
    if isinstance(section, dict):
        return _html(section.get("summary", section.get("classification", "")))
    return ""


def _command_for(action: dict[str, object]) -> str:
    if action.get("command"):
        command = str(action["command"])
        if command.startswith("obsidian://"):
            return "obsidian://open?vault=<local-root>"
        return f'python -B -m kb {command} --root "<root>"'
    if action.get("gateway_operation"):
        return 'python -B -m kb gateway-check --root "<root>" --json'
    return "Descriptor only"


def _render_actions(actions: object) -> str:
    if not isinstance(actions, list):
        return "<p>No action descriptors available.</p>"
    cards: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        label = _html(action.get("label", action.get("id", "action")))
        transport = _html(action.get("transport", "descriptor"))
        requires = "Requires confirmation" if action.get("requires_confirmation") else "Read-only descriptor"
        command = _html(_command_for(action))
        cards.append(
            '<article class="action">'
            f"<h3>{label}</h3>"
            f"<p>{_html(requires)} · {transport}</p>"
            f"<label>Copy command</label><code>{command}</code>"
            "</article>"
        )
    return "\n".join(cards)


def render_web_console_html(state: dict[str, object]) -> str:
    root = state.get("root", {})
    health = state.get("health", {})
    profile = state.get("profile_registry", {})
    dependencies = state.get("dependencies", {})
    governance = state.get("governance", {})
    backup = state.get("backup", {})
    obsidian = state.get("obsidian", {})
    actions = state.get("actions", [])
    notices = state.get("notices", [])

    dependency_rows: list[str] = []
    if isinstance(dependencies, dict):
        for name in ("llm", "embedding", "ocr"):
            dependency_rows.append(
                "<li>"
                f"<strong>{_html(name)}</strong>: {_section_status(dependencies.get(name))} "
                f"<span>{_summary(dependencies.get(name))}</span>"
                "</li>"
            )

    governance_rows: list[str] = []
    if isinstance(governance, dict):
        for name in ("lint", "status", "governance"):
            governance_rows.append(
                "<li>"
                f"<strong>{_html(name)}</strong>: {_section_status(governance.get(name))} "
                f"<span>{_summary(governance.get(name))}</span>"
                "</li>"
            )

    notice_rows = "".join(f"<li>{_html(notice)}</li>" for notice in notices)
    root_path = root.get("path", "") if isinstance(root, dict) else ""
    check_count = health.get("check_count", "0") if isinstance(health, dict) else "0"
    profile_count = profile.get("profile_count", "0") if isinstance(profile, dict) else "0"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local Web Console</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1b1f27;
      --muted: #5a6472;
      --line: #d9dee8;
      --panel: #f7f9fc;
      --accent: #116466;
      --warn: #8a5a00;
    }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: #ffffff;
      line-height: 1.5;
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    header {{ border-bottom: 1px solid var(--line); padding-bottom: 18px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 28px 0 10px; font-size: 18px; }}
    h3 {{ margin: 0 0 6px; font-size: 15px; }}
    p, li {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
    .panel, .action {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 14px;
    }}
    .status {{ font-weight: 700; color: var(--accent); }}
    .warning {{ color: var(--warn); }}
    code {{
      display: block;
      margin-top: 6px;
      padding: 8px;
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #f8fafc;
      border-radius: 4px;
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 13px;
    }}
    label {{ display: block; color: var(--muted); font-size: 12px; }}
    a {{ color: var(--accent); }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Local Web Console</h1>
    <p>Local-first, loopback-only, read-only browser view for knowledge-base state.</p>
    <p class="warning">No browser route executes mutating actions. Copy commands, review them, then run them yourself in a terminal.</p>
  </header>
  <section>
    <h2>Safety Notices</h2>
    <ul>
      <li>AI is not a fact source; stable wiki claims require local source evidence.</li>
      <li>Cloud/provider calls are off by default and this page does not call providers.</li>
      <li>Do not point demos at a real vault unless a separate exact-path task approves it.</li>
      {notice_rows}
    </ul>
  </section>
  <section class="grid">
    <article class="panel">
      <h2>Root Health</h2>
      <p>Path summary: {_html(root_path)}</p>
      <p>Status: <span class="status">{_section_status(health)}</span></p>
      <p>Checks: {_html(check_count)}</p>
    </article>
    <article class="panel">
      <h2>Profile</h2>
      <p>Status: <span class="status">{_section_status(profile)}</span></p>
      <p>Profiles: {_html(profile_count)}</p>
    </article>
    <article class="panel">
      <h2>Backup</h2>
      <p>Status: <span class="status">{_section_status(backup)}</span></p>
      <p>{_summary(backup)}</p>
    </article>
    <article class="panel">
      <h2>Obsidian</h2>
      <p>Status: <span class="status">{_section_status(obsidian)}</span></p>
      <p>{_summary(obsidian)}</p>
    </article>
  </section>
  <section>
    <h2>Dependencies</h2>
    <ul>{''.join(dependency_rows)}</ul>
  </section>
  <section>
    <h2>Governance</h2>
    <ul>{''.join(governance_rows)}</ul>
  </section>
  <section>
    <h2>Available Actions</h2>
    <div class="grid">{_render_actions(actions)}</div>
  </section>
  <section>
    <h2>First-Run Links</h2>
    <ul>
      <li><a href="docs/product/quickstart-zh.md">Chinese quick start</a></li>
      <li><a href="docs/product/first-run-demo.md">First-run demo</a></li>
      <li><a href="docs/product/command-guide.md">Command decision guide</a></li>
      <li><a href="/state.json">Redacted JSON state</a></li>
    </ul>
  </section>
</main>
</body>
</html>
"""


class WebConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], root: str | Path):
        super().__init__(server_address, WebConsoleRequestHandler)
        self.root = Path(root)
        self.serve_thread: threading.Thread | None = None

    def shutdown(self) -> None:
        super().shutdown()
        if self.serve_thread is not None:
            self.serve_thread.join(timeout=5)
        self.server_close()


class WebConsoleRequestHandler(BaseHTTPRequestHandler):
    server: WebConsoleServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _state(self) -> dict[str, object]:
        address = self.server.server_address
        host, port = address[0], address[1]
        return web_console_state(
            product_console_state(self.server.root),
            host=str(host),
            port=int(port),
        )

    def do_GET(self) -> None:
        if self.path in {"", "/"}:
            self._write(HTTPStatus.OK, render_web_console_html(self._state()), "text/html")
            return
        if self.path == "/state.json":
            body = json.dumps(self._state(), ensure_ascii=False, sort_keys=True)
            self._write(HTTPStatus.OK, body + "\n", "application/json")
            return
        if self.path in PRODUCT_DOC_ROUTES:
            body = PRODUCT_DOC_ROUTES[self.path].read_text(encoding="utf-8")
            self._write(HTTPStatus.OK, body, "text/markdown")
            return
        self._write(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")

    def _reject_mutation(self) -> None:
        self._write(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "This local web console is read-only; browser routes do not execute actions.\n",
            "text/plain",
        )

    do_POST = _reject_mutation
    do_PUT = _reject_mutation
    do_PATCH = _reject_mutation
    do_DELETE = _reject_mutation


def start_web_console_server(
    root: str | Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> WebConsoleServer:
    if not is_loopback_host(host):
        raise RuntimeError("Local web console must bind to a loopback host by default.")
    server = WebConsoleServer((host, port), root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.serve_thread = thread
    thread.start()
    return server


def serve_web_console(
    *,
    root: str | Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    server = start_web_console_server(root, host=host, port=port)
    address = server.server_address
    actual_host, actual_port = address[0], address[1]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"Local web console: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        if server.serve_thread is not None:
            server.serve_thread.join()
    except KeyboardInterrupt:
        print("Stopping local web console.")
        server.shutdown()
    finally:
        server.server_close()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "is_loopback_host",
    "render_web_console_html",
    "serve_web_console",
    "start_web_console_server",
    "web_console_state",
]
