"""Provider-agnostic LLM preflight checks.

The preflight path intentionally never writes drafts or stable wiki content. The
only optional persistence is one redacted audit JSONL file when explicitly asked.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .context import (
    EmptyContextError,
    build_context_pack,
    build_prompt_messages,
    prompt_hash,
)
from .factuality import llm_draft_contract_status, parse_llm_draft_response
from .llm import OpenAICompatibleClient, load_llm_config
from .locks import WriteLockError, acquire_write_lock, lock_check
from .paths import KnowledgeBasePaths
from .product_result import ProductResult
from .redaction import redact_text, summarize_text
from .sources import read_source_card
from .wiki import draft_timestamp, validate_draft_content


AUDIT_METADATA_KEYS = (
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
)
REDACTION_VERSION = "redaction-v1"
PASS_CLASSIFICATIONS = {"pass", "configured_but_unverified"}
PUBLIC_PRIVACY_LEVELS = {"public", "personal"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _provider_class(config: object | None) -> str:
    if config is None:
        return "unknown"
    base_url = str(getattr(config, "base_url", "")).casefold()
    model = str(getattr(config, "model", "")).casefold()
    if "deepseek" in base_url or "deepseek" in model:
        return "deepseek"
    return "openai_compatible"


def _chunk_ids(context_chunks: list[dict[str, object]]) -> list[str]:
    return [
        f"{chunk['source_id']}#{chunk['chunk_index']}" for chunk in context_chunks
    ]


def _source_ids(context_sources: list[dict[str, str]]) -> list[str]:
    return [str(source["source_id"]) for source in context_sources]


def _result(
    classification: str,
    summary: str,
    *,
    provider_class: str = "unknown",
    model_label: str = "unset",
    message_hash: str = "",
    context_sources: list[str] | None = None,
    context_chunks: list[str] | None = None,
    privacy_levels: list[str] | None = None,
    policy_decision: str = "not_evaluated",
    confirmation_summary: str = "",
    latency_ms: int = 0,
    attempt_count: int = 0,
    details: dict[str, object] | None = None,
) -> ProductResult:
    audit_metadata = {
        "timestamp": _timestamp(),
        "provider_class": redact_text(provider_class),
        "model_label": redact_text(model_label),
        "prompt_hash": message_hash,
        "context_sources": list(context_sources or []),
        "context_chunks": list(context_chunks or []),
        "privacy_levels": list(privacy_levels or []),
        "policy_decision": policy_decision,
        "confirmation_summary": redact_text(confirmation_summary),
        "classification": classification,
        "redaction_version": REDACTION_VERSION,
        "latency_ms": max(0, int(latency_ms)),
        "attempt_count": max(0, int(attempt_count)),
    }
    assert tuple(audit_metadata) == AUDIT_METADATA_KEYS
    status = "pass" if classification in PASS_CLASSIFICATIONS else "failed"
    severity = "info" if status == "pass" else "blocking"
    result_details = {"audit_metadata": audit_metadata}
    if details:
        result_details.update(details)
    return ProductResult(
        status=status,
        classification=classification,
        summary=redact_text(summary),
        severity=severity,
        details=result_details,
    )


def _require_initialized_repository(paths: KnowledgeBasePaths) -> None:
    if not paths.root.is_dir():
        raise RuntimeError("Knowledge base is not initialized")
    for directory in (
        paths.raw,
        paths.inbox,
        paths.wiki,
        paths.sources,
        paths.meta,
        paths.db,
    ):
        if not directory.is_dir():
            raise RuntimeError("Knowledge base is not initialized")
    for path in (paths.meta / "log.md", paths.meta / "review-queue.md"):
        if not path.is_file():
            raise RuntimeError("Knowledge base is not initialized")


def _lock_blocking_classification(root: Path) -> str | None:
    result = lock_check(root)
    if result.status == "pass":
        return None
    if result.classification == "active_lock":
        return "write_lock_active"
    if result.classification == "stale_lock_candidate":
        return "stale_lock_candidate"
    return "write_lock_active"


def _source_privacy(paths: KnowledgeBasePaths, source_ids: list[str]) -> dict[str, str]:
    levels: dict[str, str] = {}
    for source_id in source_ids:
        card = read_source_card(paths.sources / f"{source_id}.md")
        privacy = card.get("privacy", "public").strip().casefold() or "public"
        levels[source_id] = privacy
    return levels


def _confirmation_payload(
    confirmation: object,
    *,
    provider_class: str,
    selected_source_ids: list[str],
) -> dict[str, object] | None:
    if not isinstance(confirmation, dict):
        return None
    return dict(confirmation)


def _confirmation_source_ids(payload: dict[str, object]) -> set[str]:
    value = payload.get("source_ids", payload.get("source_scope", []))
    if value == "all":
        return {"all"}
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    return set()


def _restricted_source_ids(payload: dict[str, object]) -> set[str]:
    value = payload.get("restricted_source_ids", [])
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    return set()


def _confirmation_summary(payload: dict[str, object] | None) -> str:
    if not payload:
        return ""
    summary = payload.get("summary", payload.get("confirmation_summary", ""))
    return summarize_text(summary, limit=200)


def _confirmation_base_valid(
    payload: dict[str, object] | None,
    *,
    provider_class: str,
    required_source_ids: set[str],
) -> bool:
    if payload is None:
        return False
    provider = str(payload.get("provider", "")).strip()
    timestamp = str(payload.get("timestamp", "")).strip()
    summary = str(payload.get("summary", payload.get("confirmation_summary", ""))).strip()
    source_ids = _confirmation_source_ids(payload)
    covers_sources = "all" in source_ids or required_source_ids.issubset(source_ids)
    return bool(
        provider == provider_class
        and timestamp
        and summary
        and required_source_ids
        and covers_sources
    )


def _policy_decision(
    privacy_by_source: dict[str, str],
    confirmation: object,
    *,
    provider_class: str,
) -> tuple[str, str, str]:
    selected = set(privacy_by_source)
    sensitive = {
        source_id
        for source_id, level in privacy_by_source.items()
        if level == "sensitive"
    }
    restricted = {
        source_id
        for source_id, level in privacy_by_source.items()
        if level == "restricted"
    }
    unknown = {
        source_id
        for source_id, level in privacy_by_source.items()
        if level not in PUBLIC_PRIVACY_LEVELS | {"sensitive", "restricted"}
    }
    payload = _confirmation_payload(
        confirmation, provider_class=provider_class, selected_source_ids=sorted(selected)
    )
    summary = _confirmation_summary(payload)

    if unknown:
        return "policy_blocked", "blocked", summary
    if restricted:
        restricted_confirmed = (
            _confirmation_base_valid(
                payload, provider_class=provider_class, required_source_ids=selected
            )
            and restricted.issubset(_restricted_source_ids(payload or {}))
        )
        if not restricted_confirmed:
            return "policy_blocked", "blocked", summary
        return "allowed", "confirmed", summary
    if sensitive:
        if not _confirmation_base_valid(
            payload, provider_class=provider_class, required_source_ids=sensitive
        ):
            return "policy_confirmation_required", "confirmation_required", summary
        return "allowed", "confirmed", summary
    return "allowed", "allowed", summary


def _contains_configured_secret(
    env: Mapping[str, str],
    config: object | None,
    *values: object,
) -> bool:
    secrets: list[str] = []
    api_key = getattr(config, "api_key", None) if config is not None else None
    if api_key:
        secrets.append(str(api_key))
    for name, value in env.items():
        lowered = str(name).casefold()
        if value and any(word in lowered for word in ("api_key", "token", "secret")):
            secrets.append(str(value))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret and any(secret in str(value) for value in values):
            return True
    return False


def _provider_failure_classification(error: RuntimeError) -> str:
    message = str(error).casefold()
    if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden")):
        return "auth_failure"
    if "api key" in message or "credential" in message or "authentication" in message:
        return "auth_failure"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if any(
        marker in message
        for marker in (
            "network",
            "connection",
            "dns",
            "name resolution",
            "refused",
            "urlerror",
        )
    ):
        return "network_failure"
    return "provider_error"


def _draft_validation_metadata(
    *,
    title: str,
    query: str,
    model_label: str,
    message_hash: str,
    context_sources: list[str],
    context_chunks: list[str],
    claims: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "draft_id": "preflight-preview",
        "title": title,
        "query": query,
        "created_at": draft_timestamp(),
        "model": model_label,
        "prompt_hash": message_hash,
        "context_sources": context_sources,
        "context_chunks": context_chunks,
        "claims": claims,
    }


def _validation_classification(issues: list[dict[str, str]]) -> str:
    issue_types = {str(issue.get("type", "")) for issue in issues}
    if "secret-leak" in issue_types:
        return "secret_leak_blocked"
    if "unsupported-draft-heading" in issue_types:
        return "unsupported_heading"
    if issue_types:
        return "invalid_claim_evidence"
    return "pass"


def _write_audit(paths: KnowledgeBasePaths, metadata: dict[str, object]) -> None:
    record = {key: metadata[key] for key in AUDIT_METADATA_KEYS}
    content = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with acquire_write_lock(paths.root, operation="llm-preflight-audit"):
        with (paths.meta / "llm-audit.jsonl").open("a", encoding="utf-8", newline="\n") as audit:
            audit.write(content)


def _maybe_write_audit(
    paths: KnowledgeBasePaths,
    result: ProductResult,
    *,
    write_audit: bool,
) -> ProductResult:
    if not write_audit or result.classification == "secret_leak_blocked":
        return result
    try:
        _write_audit(paths, result.details["audit_metadata"])
    except WriteLockError as exc:
        classification = (
            "write_lock_active"
            if exc.classification == "write_lock_active"
            else exc.classification
        )
        if classification not in {"write_lock_active", "stale_lock_candidate"}:
            classification = "write_lock_active"
        metadata = dict(result.details["audit_metadata"])
        return _result(
            classification,
            exc.summary,
            provider_class=str(metadata["provider_class"]),
            model_label=str(metadata["model_label"]),
            message_hash=str(metadata["prompt_hash"]),
            context_sources=list(metadata["context_sources"]),
            context_chunks=list(metadata["context_chunks"]),
            privacy_levels=list(metadata["privacy_levels"]),
            policy_decision=str(metadata["policy_decision"]),
            confirmation_summary=str(metadata["confirmation_summary"]),
            latency_ms=int(metadata["latency_ms"]),
            attempt_count=int(metadata["attempt_count"]),
        )
    return result


def llm_preflight(
    root: str | Path,
    query: str,
    title: str,
    target: str | None = None,
    context_limit: int = 5,
    write_audit: bool = False,
    privacy_confirmation: object | None = None,
    client: object | None = None,
    env: Mapping[str, str] | None = None,
    offline: bool = False,
) -> ProductResult:
    started = time.monotonic()
    env_source = os.environ if env is None else env
    paths = KnowledgeBasePaths(Path(root))
    config = None
    provider_class = "unknown"
    model_label = "unset"
    message_hash = ""
    context_source_ids: list[str] = []
    context_chunk_ids: list[str] = []
    privacy_levels: list[str] = []
    policy_decision = "not_evaluated"
    confirmation_summary = ""
    attempt_count = 0

    try:
        config = load_llm_config(env_source)
        provider_class = _provider_class(config)
        model_label = str(config.model)
    except RuntimeError as exc:
        return _result("missing_config", str(exc), latency_ms=0)

    if _contains_configured_secret(env_source, config, query, title, target):
        return _result(
            "secret_leak_blocked",
            "Preflight input contained a configured secret.",
            provider_class=provider_class,
            model_label=model_label,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        _require_initialized_repository(paths)
    except RuntimeError as exc:
        return _result(
            "unknown_failure",
            str(exc),
            provider_class=provider_class,
            model_label=model_label,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    lock_classification = _lock_blocking_classification(paths.root)
    if lock_classification is not None:
        return _result(
            lock_classification,
            "Root write lock blocks LLM preflight.",
            provider_class=provider_class,
            model_label=model_label,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        context_pack = build_context_pack(paths.root, query, limit=context_limit)
    except EmptyContextError as exc:
        return _result(
            "empty_context",
            str(exc),
            provider_class=provider_class,
            model_label=model_label,
            policy_decision="not_evaluated",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except RuntimeError as exc:
        return _result(
            "unknown_failure",
            str(exc),
            provider_class=provider_class,
            model_label=model_label,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    context_source_ids = _source_ids(context_pack.context_sources)
    context_chunk_ids = _chunk_ids(context_pack.context_chunks)
    try:
        privacy_by_source = _source_privacy(paths, context_source_ids)
    except RuntimeError as exc:
        return _result(
            "unknown_failure",
            str(exc),
            provider_class=provider_class,
            model_label=model_label,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    privacy_levels = sorted(set(privacy_by_source.values()))
    policy_classification, policy_decision, confirmation_summary = _policy_decision(
        privacy_by_source,
        privacy_confirmation,
        provider_class=provider_class,
    )
    if policy_classification != "allowed":
        return _result(
            policy_classification,
            "Privacy policy blocks provider preflight.",
            provider_class=provider_class,
            model_label=model_label,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        messages = build_prompt_messages(title, query, context_pack, provider=provider_class)
        message_hash = prompt_hash(messages)
    except EmptyContextError:
        return _result(
            "empty_context",
            "Empty context: no-matching-chunks",
            provider_class=provider_class,
            model_label=model_label,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    if _contains_configured_secret(env_source, config, messages):
        return _result(
            "secret_leak_blocked",
            "Preflight prompt contained a configured secret.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    if offline:
        result = _result(
            "configured_but_unverified",
            "LLM configuration and local contract plumbing are present but no provider call was made.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=0,
        )
        return _maybe_write_audit(paths, result, write_audit=write_audit)

    completion_client = client if client is not None else OpenAICompatibleClient(config)
    try:
        attempt_count = 1
        content = completion_client.complete(messages)
    except RuntimeError as exc:
        classification = _provider_failure_classification(exc)
        return _result(
            classification,
            "Provider preflight call failed.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    if _contains_configured_secret(env_source, config, content):
        return _result(
            "secret_leak_blocked",
            "Provider response contained a configured secret.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    if not isinstance(content, str) or not content.strip():
        return _result(
            "empty_response",
            "Provider response content was empty.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    contract_status = llm_draft_contract_status(content, title, target=target)
    if contract_status == "invalid_json":
        return _result(
            "invalid_json",
            "Provider response was not valid draft JSON.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )
    if contract_status == "unsupported_heading":
        return _result(
            "unsupported_heading",
            "Provider response used unsupported draft headings.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )
    if contract_status != "pass":
        return _result(
            "invalid_contract",
            "Provider response did not match the draft contract.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    try:
        parsed = parse_llm_draft_response(content)
    except RuntimeError:
        return _result(
            "invalid_contract",
            "Provider response did not match the draft contract.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    if _contains_configured_secret(env_source, config, parsed.body, parsed.claims):
        return _result(
            "secret_leak_blocked",
            "Provider response contained a configured secret.",
            provider_class=provider_class,
            model_label=model_label,
            message_hash=message_hash,
            context_sources=context_source_ids,
            context_chunks=context_chunk_ids,
            privacy_levels=privacy_levels,
            policy_decision=policy_decision,
            confirmation_summary=confirmation_summary,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt_count=attempt_count,
        )

    validation_metadata = _draft_validation_metadata(
        title=title,
        query=query,
        model_label=model_label,
        message_hash=message_hash,
        context_sources=context_source_ids,
        context_chunks=context_chunk_ids,
        claims=parsed.claims,
    )
    issues = validate_draft_content(
        paths,
        validation_metadata,
        parsed.body,
        "wiki/_drafts/preflight-preview.md",
        target=target,
        draft_text=parsed.body,
    )
    classification = _validation_classification(issues)
    result = _result(
        classification,
        "LLM preflight passed." if classification == "pass" else "Local draft validation failed.",
        provider_class=provider_class,
        model_label=model_label,
        message_hash=message_hash,
        context_sources=context_source_ids,
        context_chunks=context_chunk_ids,
        privacy_levels=privacy_levels,
        policy_decision=policy_decision,
        confirmation_summary=confirmation_summary,
        latency_ms=int((time.monotonic() - started) * 1000),
        attempt_count=attempt_count,
        details={"issues": issues} if issues else None,
    )
    return _maybe_write_audit(paths, result, write_audit=write_audit)
