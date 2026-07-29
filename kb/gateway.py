"""Policy-enforced local gateway for product-facing integrations."""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from . import commands
from .locks import WriteLockError, lock_check
from .paths import KnowledgeBasePaths
from .product_result import ProductResult
from .redaction import redact_text, summarize_text


REDACTION_VERSION = "redaction-v1"
LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost"}
TOKEN_HEADER = "X-KB-Gateway-Token"
ALLOWED_ROUTE_NAMES = (
    "health",
    "summary",
    "draft-create",
    "draft-validate",
    "draft-publish",
    "backup",
    "restore-to-new-root",
    "eval-search",
    "preflight",
)
WRITE_ROUTES = {
    "draft-create",
    "draft-publish",
    "backup",
    "restore-to-new-root",
}
SOURCE_ID_RE = re.compile(r"\bsrc-[0-9a-f]{12}\b")
BLOCKING_SOURCE_STATUSES = {"needs_reingest", "rejected"}


def _result(
    status: str,
    classification: str,
    summary: str,
    *,
    http_status: int = 200,
    **details: object,
) -> dict[str, object]:
    data: dict[str, object] = {
        "status": redact_text(status),
        "classification": redact_text(classification),
        "summary": summarize_text(summary, limit=240),
        "redaction_version": REDACTION_VERSION,
    }
    if details:
        data.update(_json_safe(details))
    if http_status != 200:
        data["http_status"] = http_status
    return data


def _product_result_to_dict(result: ProductResult) -> dict[str, object]:
    return result.to_dict()


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {redact_text(str(key)): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, Path)):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[non-json:{type(value).__name__}]"


def _issue_types(issues: object) -> list[str]:
    if not isinstance(issues, list):
        return []
    return sorted(
        {
            str(issue.get("type", "unknown"))
            for issue in issues
            if isinstance(issue, dict)
        }
    )


def _payload_str(payload: Mapping[str, object], key: str, *, required: bool = True) -> str:
    value = payload.get(key)
    if required and (value is None or str(value) == ""):
        raise RuntimeError(f"Missing gateway payload field: {key}")
    return "" if value is None else str(value)


def _payload_int(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid gateway payload field: {key}") from exc


class PolicyGateway:
    """Single in-process authority for product-facing policy operations."""

    def __init__(self, root: str | Path, capability_token: str | None = None) -> None:
        self.root = Path(root)
        self.capability_token = capability_token or secrets.token_urlsafe(32)

    def route_names(self) -> list[str]:
        return list(ALLOWED_ROUTE_NAMES)

    def handle(self, operation: str, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        operation_name = str(operation)
        safe_payload = dict(payload or {})
        if operation_name not in ALLOWED_ROUTE_NAMES:
            return _result(
                "failed",
                "unknown_operation",
                f"Unknown gateway operation: {operation_name}",
            )
        if operation_name in WRITE_ROUTES:
            lock_result = lock_check(self.root)
            if lock_result.status != "pass":
                classification = (
                    "write_lock_active"
                    if lock_result.classification == "active_lock"
                    else lock_result.classification
                )
                return _result(
                    "failed",
                    classification,
                    str(lock_result.summary),
                    details=lock_result.details,
                )
        try:
            return self._handle(operation_name, safe_payload)
        except WriteLockError as exc:
            classification = (
                "write_lock_active"
                if exc.classification == "write_lock_active"
                else exc.classification
            )
            return _result(
                "failed",
                classification,
                exc.summary,
                details=exc.details,
            )
        except RuntimeError as exc:
            return _result(
                "failed",
                "operation_failed",
                summarize_text(str(exc), limit=240),
            )

    def _handle(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        if operation == "health":
            return _result("pass", "gateway_health", "Gateway policy layer is reachable.")
        if operation == "summary":
            return self._summary()
        if operation == "draft-validate":
            return self._draft_validate(payload)
        if operation == "draft-publish":
            return self._draft_publish(payload)
        if operation == "draft-create":
            return self._draft_create(payload)
        if operation == "backup":
            return _product_result_to_dict(
                commands.backup_repository(
                    self.root,
                    _payload_str(payload, "output"),
                    allow_dirty=bool(payload.get("allow_dirty", False)),
                )
            )
        if operation == "restore-to-new-root":
            return _product_result_to_dict(
                commands.restore_repository(
                    _payload_str(payload, "backup"),
                    _payload_str(payload, "root"),
                    replace=False,
                )
            )
        if operation == "eval-search":
            result = commands.eval_search_repository(
                self.root,
                _payload_str(payload, "benchmark"),
                limit=_payload_int(payload, "limit", 10),
                client=payload.get("client"),
                env=payload.get("env") if isinstance(payload.get("env"), dict) else None,
            )
            return _json_safe(result)  # type: ignore[return-value]
        if operation == "preflight":
            return _product_result_to_dict(
                commands.llm_preflight_repository(
                    self.root,
                    _payload_str(payload, "query"),
                    _payload_str(payload, "title"),
                    target=payload.get("target") if isinstance(payload.get("target"), str) else None,
                    context_limit=_payload_int(payload, "context_limit", 5),
                    write_audit=bool(payload.get("write_audit", False)),
                    privacy_confirmation=payload.get("privacy_confirmation"),
                    client=payload.get("client"),
                    env=payload.get("env") if isinstance(payload.get("env"), dict) else None,
                    offline=bool(payload.get("offline", False)),
                )
            )
        raise RuntimeError(f"Unknown gateway operation: {operation}")

    def _summary(self) -> dict[str, object]:
        from .governance import analyze_governance

        status = commands.status_repository(self.root)
        issues = commands.lint_repository(self.root)
        analysis = analyze_governance(self.root)
        return _result(
            "pass" if not issues and not analysis["blocking_count"] else "failed",
            "summary_ready",
            "Read-only gateway summary completed.",
            status=status,
            lint_issue_count=len(issues),
            governance_blocking_count=analysis["blocking_count"],
            governance_advisory_count=analysis["advisory_count"],
        )

    def _draft_validate(self, payload: Mapping[str, object]) -> dict[str, object]:
        issues = commands.validate_draft_file(
            self.root,
            _payload_str(payload, "draft"),
            target=payload.get("target") if isinstance(payload.get("target"), str) else None,
        )
        return _result(
            "pass" if not issues else "failed",
            "draft_valid" if not issues else "draft_invalid",
            "Draft validation completed.",
            issues=issues,
            issue_types=_issue_types(issues),
        )

    def _draft_publish(self, payload: Mapping[str, object]) -> dict[str, object]:
        draft = _payload_str(payload, "draft")
        target = _payload_str(payload, "target")
        validation_issues = commands.validate_draft_file(
            self.root,
            draft,
            target=target,
        )
        if validation_issues:
            return _result(
                "failed",
                "publish_failed",
                "Draft publish failed policy gates.",
                issue_types=_issue_types(validation_issues),
                issues=validation_issues,
            )
        blocking_issues = self._draft_source_review_issues(
            draft
        )
        if blocking_issues:
            return _result(
                "failed",
                "publish_failed",
                "Draft publish failed policy gates.",
                issue_types=_issue_types(blocking_issues),
                issues=blocking_issues,
            )
        result = commands.publish_draft(
            self.root,
            draft,
            target,
        )
        issues = result.get("issues", [])
        return _result(
            "pass" if not issues else "failed",
            "draft_published" if not issues else "publish_failed",
            "Draft publish completed." if not issues else "Draft publish failed policy gates.",
            target=result.get("target", ""),
            issue_types=_issue_types(issues),
            issues=issues,
        )

    def _draft_create(self, payload: Mapping[str, object]) -> dict[str, object]:
        query = _payload_str(payload, "query")
        title = _payload_str(payload, "title")
        target = payload.get("target") if isinstance(payload.get("target"), str) else None
        context_limit = _payload_int(payload, "context_limit", 5)
        env = payload.get("env") if isinstance(payload.get("env"), dict) else None
        client = payload.get("client")
        preflight = commands.llm_preflight_repository(
            self.root,
            query,
            title,
            target=target,
            context_limit=context_limit,
            write_audit=False,
            privacy_confirmation=payload.get("privacy_confirmation"),
            client=client,
            env=env,
            offline=bool(payload.get("offline", False)),
        )
        if preflight.classification != "pass":
            return _product_result_to_dict(preflight)
        result = commands.llm_draft(
            self.root,
            query,
            title,
            client=client,
            env=env,
            context_limit=context_limit,
            archive_existing=bool(payload.get("archive_existing", False)),
        )
        return _result(
            "pass",
            "draft_created",
            "Draft creation completed.",
            path=result.get("path", ""),
            archived_draft=result.get("archived_draft", ""),
        )

    def _draft_source_review_issues(self, draft_path: str) -> list[dict[str, str]]:
        from .sources import read_source_card
        from .wiki import read_draft

        paths = KnowledgeBasePaths(self.root)
        draft = Path(draft_path)
        if not draft.is_absolute():
            draft = paths.root / draft
        metadata, body = read_draft(draft)
        source_ids = {
            str(source_id)
            for source_id in metadata.get("context_sources", [])
            if isinstance(source_id, str)
        }
        source_ids.update(SOURCE_ID_RE.findall(body))

        issues: list[dict[str, str]] = []
        for source_id in sorted(source_ids):
            try:
                card = read_source_card(paths.sources / f"{source_id}.md")
            except OSError:
                issues.append(
                    {
                        "type": "invalid-context-source",
                        "source_id": source_id,
                    }
                )
                continue
            status = card.get("review_status", "").casefold()
            if status in BLOCKING_SOURCE_STATUSES:
                issues.append(
                    {
                        "type": "source-review-blocking",
                        "source_id": source_id,
                        "review_status": status,
                    }
                )
        return issues


def gateway_check(root: str | Path) -> ProductResult:
    paths = KnowledgeBasePaths(Path(root))
    if not paths.root.is_dir():
        return ProductResult(
            status="failed",
            classification="root_missing",
            summary="Knowledge base root is missing.",
            severity="blocking",
            details={"route_names": list(ALLOWED_ROUTE_NAMES)},
        )
    return ProductResult(
        status="pass",
        classification="gateway_ready",
        summary="Gateway is configured for localhost-only policy operation.",
        severity="info",
        details={
            "bind_hosts": sorted(LOCAL_BIND_HOSTS),
            "http_exposure": "localhost_only",
            "route_names": list(ALLOWED_ROUTE_NAMES),
        },
    )


class LocalGatewayServer:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


def _http_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: Mapping[str, object],
    *,
    origin: str | None = None,
    allowed_origins: set[str] | None = None,
    preflight: bool = False,
) -> None:
    payload = json.dumps(_json_safe(dict(body)), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    if origin and origin != "*" and allowed_origins and "*" not in allowed_origins and origin in allowed_origins:
        handler.send_header("Access-Control-Allow-Origin", origin)
        if preflight:
            handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            handler.send_header(
                "Access-Control-Allow-Headers",
                f"{TOKEN_HEADER}, Content-Type",
            )
    handler.end_headers()
    handler.wfile.write(payload)


def start_local_http_gateway(
    root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    capability_token: str | None = None,
    allowed_origins: set[str] | None = None,
) -> LocalGatewayServer:
    if host not in LOCAL_BIND_HOSTS:
        raise RuntimeError("gateway must bind localhost")

    gateway = PolicyGateway(root, capability_token=capability_token)
    token = gateway.capability_token
    origins = set(allowed_origins or ())
    if "*" in origins:
        raise RuntimeError("gateway must not enable open CORS")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _drain_request_body(self) -> None:
            try:
                remaining = max(0, int(self.headers.get("Content-Length", "0")))
            except (TypeError, ValueError):
                return
            while remaining:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    return
                remaining -= len(chunk)

        def _authorize(self) -> tuple[bool, int, dict[str, object], str | None]:
            origin = self.headers.get("Origin")
            if origin and origin not in origins:
                return (
                    False,
                    403,
                    _result(
                        "failed",
                        "origin_not_allowed",
                        "Gateway request origin is not allowed.",
                        http_status=403,
                    ),
                    origin,
                )

            supplied_token = self.headers.get(TOKEN_HEADER)
            if not supplied_token:
                return (
                    False,
                    401,
                    _result(
                        "failed",
                        "capability_token_required",
                        "Gateway capability token is required.",
                        http_status=401,
                    ),
                    origin,
                )
            if not secrets.compare_digest(supplied_token, token):
                return (
                    False,
                    401,
                    _result(
                        "failed",
                        "capability_token_invalid",
                        "Gateway capability token is invalid.",
                        http_status=401,
                    ),
                    origin,
                )
            return True, 200, {}, origin

        def _reject_non_post(self) -> None:
            authorized, status, body, origin = self._authorize()
            if authorized:
                status = 405
                body = _result(
                    "failed",
                    "method_not_allowed",
                    "Gateway HTTP wrapper accepts POST requests only.",
                    http_status=405,
                )
            _http_response(
                self,
                status,
                body,
                origin=origin,
                allowed_origins=origins,
            )

        def do_GET(self) -> None:
            self._reject_non_post()

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("Origin")
            if origin and origin in origins:
                _http_response(
                    self,
                    200,
                    _result(
                        "pass",
                        "preflight_allowed",
                        "Gateway CORS preflight is allowed for this origin.",
                    ),
                    origin=origin,
                    allowed_origins=origins,
                    preflight=True,
                )
                return
            _http_response(
                self,
                403,
                _result(
                    "failed",
                    "origin_not_allowed",
                    "Gateway request origin is not allowed.",
                    http_status=403,
                ),
                origin=origin,
                allowed_origins=origins,
            )

        def do_POST(self) -> None:
            authorized, status, body, origin = self._authorize()
            if not authorized:
                self._drain_request_body()
                _http_response(
                    self,
                    status,
                    body,
                    origin=origin,
                    allowed_origins=origins,
                )
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict):
                    raise RuntimeError("Gateway request must be a JSON object.")
                operation = str(request.get("operation", ""))
                payload = request.get("payload", {})
                if not isinstance(payload, dict):
                    raise RuntimeError("Gateway payload must be a JSON object.")
                response = gateway.handle(operation, payload)
                response_status = 200 if response.get("status") == "pass" else 400
            except Exception as exc:
                response = _result(
                    "failed",
                    "bad_request",
                    summarize_text(str(exc), limit=200),
                    http_status=400,
                )
                response_status = 400

            _http_response(
                self,
                response_status,
                response,
                origin=origin,
                allowed_origins=origins,
            )

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return LocalGatewayServer(server, thread)
