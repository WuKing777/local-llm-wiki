import http.client
import io
import json
import secrets
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kb.cli import main
from kb.commands import ingest_file, init_repository
from kb.locks import acquire_write_lock
from kb.paths import KnowledgeBasePaths
from kb.sources import read_source_card, write_source_card
from kb.wiki import target_path_for_title


ALLOWED_ROUTES = {
    "health",
    "summary",
    "draft-create",
    "draft-validate",
    "draft-publish",
    "backup",
    "restore-to-new-root",
    "eval-search",
    "preflight",
}
FORBIDDEN_ROUTES = {
    "write-stable-wiki",
    "put-wiki-page",
    "direct-stable-wiki-body-write",
    "write-wiki-page",
}
SENTINEL = "".join(["s", "k", "-", "task11", "-", "sentinel", "-", "000000000000"])


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.content


def create_root_with_source(temp: Path, text: str = "Gateway evidence quote.") -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(f"# Source\n\n{text}\n", encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


def set_source_review(root: Path, source_id: str, status: str) -> None:
    paths = KnowledgeBasePaths(root)
    card = read_source_card(paths.sources / f"{source_id}.md")
    card["review_status"] = status
    write_source_card(paths, card)


def set_source_privacy(root: Path, source_id: str, privacy: str) -> None:
    paths = KnowledgeBasePaths(root)
    card = read_source_card(paths.sources / f"{source_id}.md")
    card["privacy"] = privacy
    write_source_card(paths, card)


def claim_for(source_id: str, text: str = "Gateway evidence quote") -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": text}],
    }


def draft_metadata(source_id: str) -> dict[str, object]:
    return {
        "draft_id": "gateway-draft",
        "title": "Gateway Page",
        "query": "gateway evidence",
        "created_at": "2026-07-06T00:00:00Z",
        "model": "test-model",
        "prompt_hash": "b" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": [claim_for(source_id)],
    }


def write_draft(root: Path, source_id: str, body: str | None = None) -> Path:
    if body is None:
        body = f"# Gateway Page\n\nGateway evidence quote {source_id}."
    draft = root / "wiki" / "_drafts" / "gateway-page.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in draft_metadata(source_id).items()
    )
    lines.extend(["---", "", body.rstrip(), ""])
    draft.write_text("\n".join(lines), encoding="utf-8")
    return draft


def envelope(source_id: str) -> str:
    body = f"# Gateway Page\n\nGateway evidence quote {source_id}."
    return json.dumps({"body": body, "claims": [claim_for(source_id)]})


def configured_env(api_key: str | None = None) -> dict[str, str]:
    env = {
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "gateway-fake-model",
    }
    if api_key is not None:
        env["KB_LLM_API_KEY"] = api_key
    return env


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def post_json(port: int, token: str | None, payload: dict[str, object], origin: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-KB-Gateway-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/gateway", json.dumps(payload), headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return response.status, dict(response.getheaders()), json.loads(body)
    finally:
        connection.close()


def get_json(port: int, token: str | None = None):
    headers = {}
    if token is not None:
        headers["X-KB-Gateway-Token"] = token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/gateway", headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return response.status, dict(response.getheaders()), json.loads(body)
    finally:
        connection.close()


def options_json(port: int, origin: str):
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-KB-Gateway-Token, Content-Type",
    }
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("OPTIONS", "/gateway", headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return response.status, dict(response.getheaders()), json.loads(body)
    finally:
        connection.close()


class GatewayTests(unittest.TestCase):
    def test_gateway_check_returns_pass_product_result(self):
        from kb.gateway import gateway_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))

            result = gateway_check(root)

            self.assertEqual("pass", result.status)
            self.assertEqual("gateway_ready", result.classification)
            self.assertNotIn(SENTINEL, result.to_json())

    def test_route_names_are_limited_to_policy_operations(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))

            routes = set(PolicyGateway(root).route_names())

            self.assertEqual(ALLOWED_ROUTES, routes)
            self.assertTrue(FORBIDDEN_ROUTES.isdisjoint(routes))

    def test_gateway_check_cli_prints_redacted_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["gateway-check", "--root", str(root), "--json"])

            self.assertEqual(0, code, stderr.getvalue())
            self.assertEqual("", stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual("pass", data["status"])
            self.assertEqual("gateway_ready", data["classification"])

    def test_http_gateway_rejects_non_local_bind_and_enforces_token_origin_and_cors(self):
        from kb.gateway import start_local_http_gateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))
            token = secrets.token_urlsafe(24)
            wrong_token = secrets.token_urlsafe(24)

            with self.assertRaisesRegex(RuntimeError, "gateway must bind localhost"):
                start_local_http_gateway(root, host="0.0.0.0", capability_token=token)
            with self.assertRaisesRegex(RuntimeError, "open CORS"):
                start_local_http_gateway(
                    root,
                    capability_token=token,
                    allowed_origins={"*"},
                )

            server = start_local_http_gateway(
                root,
                host="127.0.0.1",
                port=0,
                capability_token=token,
                allowed_origins={"http://127.0.0.1"},
            )
            try:
                missing = post_json(server.port, None, {"operation": "health", "payload": {}})
                wrong = post_json(server.port, wrong_token, {"operation": "health", "payload": {}})
                bad_origin = post_json(
                    server.port,
                    token,
                    {"operation": "health", "payload": {}},
                    origin="http://example.invalid",
                )
                ok_status, ok_headers, ok_body = post_json(
                    server.port,
                    token,
                    {"operation": "health", "payload": {}},
                    origin="http://127.0.0.1",
                )
                get_status, get_headers, get_body = get_json(server.port)
                options_status, options_headers, options_body = options_json(
                    server.port, "http://127.0.0.1"
                )
                bad_options_status, bad_options_headers, bad_options_body = options_json(
                    server.port, "http://example.invalid"
                )
            finally:
                server.shutdown()

            self.assertEqual(401, missing[0])
            self.assertEqual("capability_token_required", missing[2]["classification"])
            self.assertEqual(401, wrong[0])
            self.assertEqual("capability_token_invalid", wrong[2]["classification"])
            self.assertEqual(403, bad_origin[0])
            self.assertEqual("origin_not_allowed", bad_origin[2]["classification"])
            self.assertEqual(200, ok_status)
            self.assertEqual("pass", ok_body["status"])
            self.assertNotEqual("*", ok_headers.get("Access-Control-Allow-Origin"))
            serialized = json.dumps([missing, wrong, bad_origin, ok_body], sort_keys=True)
            self.assertNotIn(token, serialized)
            self.assertNotIn(wrong_token, serialized)
            self.assertEqual(401, get_status)
            self.assertEqual("capability_token_required", get_body["classification"])
            self.assertNotEqual("*", get_headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(200, options_status)
            self.assertEqual("preflight_allowed", options_body["classification"])
            self.assertEqual(
                "http://127.0.0.1",
                options_headers.get("Access-Control-Allow-Origin"),
            )
            self.assertIn("POST", options_headers.get("Access-Control-Allow-Methods", ""))
            self.assertIn("OPTIONS", options_headers.get("Access-Control-Allow-Methods", ""))
            self.assertIn(
                "X-KB-Gateway-Token",
                options_headers.get("Access-Control-Allow-Headers", ""),
            )
            self.assertNotEqual("*", options_headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(403, bad_options_status)
            self.assertEqual("origin_not_allowed", bad_options_body["classification"])
            self.assertNotEqual("*", bad_options_headers.get("Access-Control-Allow-Origin"))
            root_bytes = b"".join(
                path.read_bytes() for path in root.rglob("*") if path.is_file()
            )
            self.assertNotIn(token.encode("utf-8"), root_bytes)
            self.assertNotIn(wrong_token.encode("utf-8"), root_bytes)

            default_server = start_local_http_gateway(root, capability_token=token)
            try:
                self.assertIn(default_server.host, {"127.0.0.1", "localhost"})
            finally:
                default_server.shutdown()

    def test_http_gateway_delivers_auth_rejection_after_large_post_body(self):
        from kb.gateway import start_local_http_gateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))
            token = secrets.token_urlsafe(24)
            wrong_token = secrets.token_urlsafe(24)
            server = start_local_http_gateway(
                root,
                host="127.0.0.1",
                port=0,
                capability_token=token,
            )
            try:
                status, _headers, body = post_json(
                    server.port,
                    wrong_token,
                    {
                        "operation": "health",
                        "payload": {"padding": "x" * (16 * 1024 * 1024)},
                    },
                )
            finally:
                server.shutdown()

            self.assertEqual(401, status)
            self.assertEqual("capability_token_invalid", body["classification"])

    def test_publish_route_uses_existing_publish_validation_and_source_review_blocks(self):
        from kb.gateway import PolicyGateway

        for review_status in ("rejected", "needs_reingest"):
            with self.subTest(review_status=review_status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root, source_id = create_root_with_source(Path(tmpdir))
                    draft = write_draft(root, source_id)
                    target = target_path_for_title(KnowledgeBasePaths(root), "Gateway Page")
                    gateway = PolicyGateway(root)

                    set_source_review(root, source_id, review_status)
                    result = gateway.handle(
                        "draft-publish",
                        {"draft": str(draft), "target": "Gateway Page"},
                    )

                    self.assertEqual("failed", result["status"])
                    self.assertEqual("publish_failed", result["classification"])
                    self.assertIn("source-review-blocking", result["issue_types"])
                    self.assertFalse(target.exists())

    def test_publish_route_uses_existing_draft_validation_before_writing_target(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft = write_draft(root, source_id, body="# Gateway Page\n\nNo citation here.")
            target = target_path_for_title(KnowledgeBasePaths(root), "Gateway Page")

            result = PolicyGateway(root).handle(
                "draft-publish",
                {"draft": str(draft), "target": "Gateway Page"},
            )

            self.assertEqual("failed", result["status"])
            self.assertEqual("publish_failed", result["classification"])
            self.assertIn("missing-paragraph-citation", result["issue_types"])
            self.assertFalse(target.exists())

    def test_publish_route_handles_missing_source_as_classified_validation_issue(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))
            missing_source_id = "src-000000000000"
            draft = write_draft(root, missing_source_id)
            target = target_path_for_title(KnowledgeBasePaths(root), "Gateway Page")

            result = PolicyGateway(root).handle(
                "draft-publish",
                {"draft": str(draft), "target": "Gateway Page"},
            )

            serialized = json.dumps(result, ensure_ascii=False)
            self.assertEqual("failed", result["status"])
            self.assertEqual("publish_failed", result["classification"])
            self.assertIn("invalid-context-source", result["issue_types"])
            self.assertNotIn("Traceback", serialized)
            self.assertFalse(target.exists())

    def test_publish_route_preserves_rollback_on_post_publish_failure(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft = write_draft(root, source_id)
            target = target_path_for_title(KnowledgeBasePaths(root), "Gateway Page")
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous gateway bytes"
            target.write_bytes(previous)

            with mock.patch(
                "kb.commands.status_repository", side_effect=RuntimeError(f"boom {SENTINEL}")
            ):
                result = PolicyGateway(root).handle(
                    "draft-publish",
                    {"draft": str(draft), "target": "Gateway Page"},
                )

            self.assertEqual("failed", result["status"])
            self.assertEqual("operation_failed", result["classification"])
            self.assertNotIn(SENTINEL, json.dumps(result, ensure_ascii=False))
            self.assertNotIn("Traceback", json.dumps(result, ensure_ascii=False))
            self.assertEqual(previous, target.read_bytes())

    def test_active_write_lock_blocks_gateway_write_operation_without_mutation(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            draft = write_draft(root, source_id)
            set_source_review(root, source_id, "rejected")
            before = tree_snapshot(root)

            with acquire_write_lock(root, operation="outer-test"):
                result = PolicyGateway(root).handle(
                    "draft-publish",
                    {"draft": str(draft), "target": "Gateway Page"},
                )

            self.assertEqual("write_lock_active", result["classification"])
            self.assertEqual(before, tree_snapshot(root))

    def test_preflight_route_preserves_privacy_policy_and_minimized_metadata(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            set_source_privacy(root, source_id, "restricted")
            client = FakeClient(envelope(source_id))
            gateway = PolicyGateway(root)

            blocked = gateway.handle(
                "preflight",
                {
                    "query": "gateway evidence",
                    "title": "Gateway Page",
                    "client": client,
                    "env": configured_env(SENTINEL),
                },
            )
            confirmed = gateway.handle(
                "preflight",
                {
                    "query": "gateway evidence",
                    "title": "Gateway Page",
                    "client": client,
                    "env": configured_env(SENTINEL),
                    "privacy_confirmation": {
                        "provider": "openai_compatible",
                        "source_ids": [source_id],
                        "restricted_source_ids": [source_id],
                        "timestamp": "2026-07-06T00:00:00Z",
                        "summary": "User approved gateway restricted preflight.",
                    },
                },
            )

            self.assertEqual("policy_blocked", blocked["classification"])
            self.assertEqual("pass", confirmed["classification"])
            self.assertEqual(1, len(client.calls))
            serialized = json.dumps(confirmed, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("messages", serialized)
            self.assertNotIn('"prompt"', serialized)
            self.assertNotIn('"response"', serialized)
            self.assertNotIn('"source_text"', serialized)
            self.assertNotIn('"chunk_text"', serialized)
            self.assertNotIn(SENTINEL, serialized)

    def test_draft_create_route_enforces_preflight_privacy_before_provider_call(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(Path(tmpdir))
            set_source_privacy(root, source_id, "restricted")
            client = FakeClient(envelope(source_id))
            before = tree_snapshot(root)

            result = PolicyGateway(root).handle(
                "draft-create",
                {
                    "query": "gateway evidence",
                    "title": "Gateway Page",
                    "client": client,
                    "env": configured_env(SENTINEL),
                },
            )

            self.assertEqual("policy_blocked", result["classification"])
            self.assertEqual([], client.calls)
            self.assertEqual(before, tree_snapshot(root))

    def test_gateway_error_response_is_classified_redacted_and_traceback_free(self):
        from kb.gateway import PolicyGateway

        with tempfile.TemporaryDirectory() as tmpdir:
            root, _source_id = create_root_with_source(Path(tmpdir))

            result = PolicyGateway(root).handle("missing-route", {"value": SENTINEL})

            self.assertEqual("failed", result["status"])
            self.assertEqual("unknown_operation", result["classification"])
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn("Traceback", serialized)


if __name__ == "__main__":
    unittest.main()
