"""Minimal local product console state presenter."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .doctor import doctor
from .gateway import PolicyGateway
from .product_paths import registry_path
from .profile_registry import list_profiles
from .redaction import redact_text, summarize_text


REDACTION_VERSION = "redaction-v1"
SCHEMA_VERSION = 1
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|"
    r"passwd|secret|credential|authorization|bearer|capability)",
    re.IGNORECASE,
)


def _is_secret_key(key: object) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key)))


def _safe_value(value: object, *, under_secret_key: bool = False) -> object:
    if isinstance(value, dict):
        safe: dict[str, object] = {}
        for key, item in value.items():
            safe_key = str(redact_text(key))
            safe[safe_key] = _safe_value(
                item,
                under_secret_key=under_secret_key or _is_secret_key(key),
            )
        return safe
    if isinstance(value, list):
        return [_safe_value(item, under_secret_key=under_secret_key) for item in value]
    if isinstance(value, tuple):
        return [_safe_value(item, under_secret_key=under_secret_key) for item in value]
    if under_secret_key:
        return "[redacted-secret]"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, os.PathLike):
        return summarize_text(os.fspath(value), limit=500)
    if isinstance(value, str):
        return summarize_text(value, limit=500)
    return f"[non-json:{type(value).__name__}]"


def _check_map(report: dict[str, object]) -> dict[str, dict[str, object]]:
    checks = report.get("checks", [])
    if not isinstance(checks, list):
        return {}
    mapped: dict[str, dict[str, object]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id", "unknown"))
        mapped[check_id] = check
    return mapped


def _check_summary(check: dict[str, object] | None, *, default_id: str) -> dict[str, object]:
    if check is None:
        return {
            "id": default_id,
            "status": "warning",
            "classification": "check_unavailable",
            "severity": "advisory",
            "summary": "Check is not available in the doctor report.",
        }
    return _safe_value(
        {
            "id": check.get("id", default_id),
            "status": check.get("status", "warning"),
            "classification": check.get("classification", "unknown"),
            "severity": check.get("severity", "advisory"),
            "summary": check.get("summary", ""),
        }
    )  # type: ignore[return-value]


def _health_summary(report: dict[str, object]) -> dict[str, object]:
    checks = _check_map(report)
    failed = sorted(
        check_id
        for check_id, check in checks.items()
        if check.get("status") == "failed"
    )
    warnings = sorted(
        check_id
        for check_id, check in checks.items()
        if check.get("status") == "warning"
    )
    return _safe_value(
        {
            "status": report.get("status", "failed"),
            "check_count": len(checks),
            "failed_check_ids": failed,
            "warning_check_ids": warnings,
        }
    )  # type: ignore[return-value]


def _profile_summary(root: Path) -> dict[str, object]:
    path = registry_path()
    try:
        profiles = list_profiles(path)
    except RuntimeError:
        return _safe_value(
            {
                "status": "failed",
                "classification": "profile_registry_unreadable",
                "path": str(path),
                "profile_count": 0,
                "selected_profile_id": None,
                "profiles": [],
            }
        )  # type: ignore[return-value]

    root_key = os.path.normcase(str(root.resolve()))
    selected_id: str | None = None
    selected_kind: str | None = None
    selected_health: str | None = None
    for profile in profiles:
        profile_root = str(profile.get("root", ""))
        selected = os.path.normcase(profile_root) == root_key
        if selected:
            selected_id = str(profile.get("id", ""))
            selected_kind = str(profile.get("kind", ""))
            selected_health = str(profile.get("last_health_status", "unknown"))

    return _safe_value(
        {
            "status": "pass",
            "classification": "profile_registry_read",
            "path": str(path),
            "profile_count": len(profiles),
            "selected_profile_id": selected_id,
            "selected_profile_kind": selected_kind,
            "selected_profile_health_status": selected_health,
        }
    )  # type: ignore[return-value]


def _root_summary(root: Path, checks: dict[str, dict[str, object]]) -> dict[str, object]:
    initialized = checks.get("initialized", {}).get("classification") == "repository_initialized"
    return _safe_value(
        {
            "path": str(root.resolve()),
            "exists": root.exists(),
            "initialized": bool(initialized),
        }
    )  # type: ignore[return-value]


def _dependency_summaries(checks: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "llm": _check_summary(checks.get("llm-config"), default_id="llm-config"),
        "ocr": _check_summary(checks.get("tesseract"), default_id="tesseract"),
        "embedding": _check_summary(
            checks.get("embedding-config"),
            default_id="embedding-config",
        ),
    }


def _governance_summary(checks: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "lint": _check_summary(checks.get("lint"), default_id="lint"),
        "status": _check_summary(checks.get("status"), default_id="status"),
        "governance": _check_summary(
            checks.get("governance"),
            default_id="governance",
        ),
    }


def _obsidian_summary(root: Path, checks: dict[str, dict[str, object]]) -> dict[str, object]:
    return _safe_value(
        {
            **_check_summary(checks.get("obsidian"), default_id="obsidian"),
            "open_command": {
                "kind": "obsidian_uri",
                "uri": f"obsidian://open?vault={quote(root.name)}",
                "executes": False,
            },
        }
    )  # type: ignore[return-value]


def _action(
    action_id: str,
    label: str,
    *,
    transport: str,
    route: str | None = None,
    command: str | None = None,
    available: bool = True,
    requires_confirmation: bool = False,
    confirmation_reason: str = "",
    executes: bool = False,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "id": action_id,
        "label": label,
        "transport": transport,
        "available": available,
        "requires_confirmation": requires_confirmation,
        "executes": executes,
    }
    if route:
        descriptor["gateway_operation"] = route
    if command:
        descriptor["command"] = command
    if confirmation_reason:
        descriptor["confirmation_reason"] = confirmation_reason
    return _safe_value(descriptor)  # type: ignore[return-value]


def _action_descriptors(root: Path, route_names: set[str]) -> list[dict[str, object]]:
    mutating = "Writes or changes knowledge-base state and must be confirmed."
    cloud_send = "May send minimized context to a configured provider after privacy gates."
    return [
        _action(
            "record-memory",
            "Record memory",
            transport="kb_command",
            command="self-statement",
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "ingest-inbox",
            "Ingest inbox",
            transport="kb_command",
            command="ingest-inbox",
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "rebuild-index",
            "Rebuild index",
            transport="kb_command",
            command="rebuild-index",
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "generate-draft",
            "Generate draft",
            transport="policy_gateway",
            route="draft-create",
            available="draft-create" in route_names,
            requires_confirmation=True,
            confirmation_reason=cloud_send,
        ),
        _action(
            "validate-draft",
            "Validate draft",
            transport="policy_gateway",
            route="draft-validate",
            available="draft-validate" in route_names,
        ),
        _action(
            "publish-draft",
            "Publish draft",
            transport="policy_gateway",
            route="draft-publish",
            available="draft-publish" in route_names,
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "run-governance",
            "Run governance",
            transport="kb_command",
            command="govern",
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "create-backup",
            "Create backup",
            transport="policy_gateway",
            route="backup",
            available="backup" in route_names,
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "restore-to-new-directory",
            "Restore to new directory",
            transport="policy_gateway",
            route="restore-to-new-root",
            available="restore-to-new-root" in route_names,
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
        _action(
            "run-eval-search",
            "Run eval-search",
            transport="policy_gateway",
            route="eval-search",
            available="eval-search" in route_names,
        ),
        _action(
            "inspect-trust-report",
            "Inspect trust report",
            transport="kb_command",
            command="trust-report",
        ),
        _action(
            "open-obsidian",
            "Open Obsidian",
            transport="local_open_descriptor",
            command=f"obsidian://open?vault={quote(root.name)}",
        ),
        _action(
            "create-import-knowledge-base",
            "Create or import knowledge base",
            transport="kb_command",
            command="init",
            requires_confirmation=True,
            confirmation_reason=mutating,
        ),
    ]


def _gateway_routes(root: Path) -> set[str]:
    try:
        return set(PolicyGateway(root).route_names())
    except RuntimeError:
        return set()


def product_console_state(root: str | Path) -> dict[str, object]:
    """Return deterministic, redacted state for a local product console."""

    root_path = Path(root)
    report = doctor(root_path, online=False)
    checks = _check_map(report)
    route_names = _gateway_routes(root_path)
    state = {
        "schema_version": SCHEMA_VERSION,
        "redaction_version": REDACTION_VERSION,
        "root": _root_summary(root_path, checks),
        "profile_registry": _profile_summary(root_path),
        "health": _health_summary(report),
        "dependencies": _dependency_summaries(checks),
        "backup": _check_summary(
            checks.get("backup-freshness"),
            default_id="backup-freshness",
        ),
        "governance": _governance_summary(checks),
        "obsidian": _obsidian_summary(root_path, checks),
        "actions": _action_descriptors(root_path, route_names),
        "notices": [
            "AI is not a fact source; stable wiki claims require local source evidence.",
            "The product console is a descriptor surface and does not execute actions from state output.",
        ],
    }
    return _safe_value(state)  # type: ignore[return-value]


__all__ = ["product_console_state"]
