"""Read-only trust evidence report for a local knowledge base."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .commands import (
    _path_label,
    _read_wiki_pages,
    _require_initialized_repository,
    _resolve_raw_path,
    _source_cards,
    lint_repository,
)
from .factuality import SOURCE_ID_RE, factual_statements, non_heading_paragraphs
from .governance import analyze_governance
from .paths import KnowledgeBasePaths
from .redaction import redact_text
from .sources import source_id_and_sha256
from .text import chunk_text, extract_text, normalize_whitespace
from .wiki import read_draft, validate_draft


SCHEMA_VERSION = "trust-report-v1"
TRUST_PRINCIPLES = (
    "AI/LLM output is never a fact source",
    "Stable wiki claims must be backed by local source evidence.",
    "Drafts must pass exact claim evidence validation before publish.",
    "Private source text must not be exposed by trust surfaces.",
)
BLOCKING_LINT_TYPES = {
    "missing-citation",
    "invalid-source-reference",
    "broken-wiki-link",
    "quote-support-unconfirmed",
}
REVIEWED_SOURCE_STATUSES = {"reviewed", "verified", "pass"}
BLOCKING_SOURCE_STATUSES = {"needs_reingest", "rejected"}
DISPLAY_PRIVACY_LEVELS = {"public", "personal", "sensitive", "restricted", "private"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redacted_issue(issue: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): redact_text(value)
        for key, value in sorted(issue.items())
    }


def _is_public_privacy(privacy: str | None) -> bool:
    return privacy is not None and privacy.strip().casefold() == "public"


def _privacy_label(card: dict[str, str]) -> str:
    privacy = card.get("privacy")
    if privacy is None or not privacy.strip():
        return "unspecified"
    normalized = privacy.strip().casefold()
    if normalized in DISPLAY_PRIVACY_LEVELS:
        return normalized
    return "unknown-non-public"


def _source_review(card: dict[str, str]) -> dict[str, str]:
    status = card.get("review_status", "").strip().casefold()
    if status in BLOCKING_SOURCE_STATUSES:
        return {
            "status": "failed",
            "review_status": status,
            "reviewed_at": redact_text(card.get("reviewed_at", "")),
        }
    if status in REVIEWED_SOURCE_STATUSES or card.get("reviewed_at"):
        return {
            "status": "pass",
            "review_status": status or "reviewed_at-present",
            "reviewed_at": redact_text(card.get("reviewed_at", "")),
        }
    return {
        "status": "warning",
        "review_status": status or "missing",
        "reviewed_at": redact_text(card.get("reviewed_at", "")),
    }


def _raw_integrity(paths: KnowledgeBasePaths, card: dict[str, str]) -> dict[str, str]:
    is_public = _is_public_privacy(card.get("privacy"))
    try:
        raw_path = _resolve_raw_path(paths, card)
        data = raw_path.read_bytes()
        source_id, sha256 = source_id_and_sha256(data)
    except Exception as exc:
        reason = redact_text(str(exc)) if is_public else "private-raw-integrity-unavailable"
        return {"status": "failed", "reason": reason}

    issues: list[str] = []
    if source_id != card.get("source_id"):
        issues.append("source_id_mismatch")
    if sha256 != card.get("sha256"):
        issues.append("sha256_mismatch")
    if issues:
        return {"status": "failed", "reason": ",".join(sorted(issues))}
    return {
        "status": "pass",
        "raw_path": (
            redact_text(card.get("raw_path", ""))
            if is_public
            else "[redacted-private-path]"
        ),
        "sha256": card.get("sha256", ""),
    }


def _source_reports(
    paths: KnowledgeBasePaths, cards: list[dict[str, str]]
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for card in sorted(cards, key=lambda item: item["source_id"]):
        privacy = _privacy_label(card)
        is_public = _is_public_privacy(card.get("privacy"))
        reports.append(
            {
                "source_id": card["source_id"],
                "title": (
                    redact_text(card.get("title", ""))
                    if is_public
                    else "[redacted-private-title]"
                ),
                "kind": card.get("kind", ""),
                "privacy": privacy,
                "review": _source_review(card),
                "raw_integrity": _raw_integrity(paths, card),
            }
        )
    return reports


def _chunks_for_source(
    paths: KnowledgeBasePaths, card: dict[str, str]
) -> list[str]:
    raw_path = _resolve_raw_path(paths, card)
    data = raw_path.read_bytes()
    expected_source_id, expected_sha256 = source_id_and_sha256(data)
    if expected_source_id != card.get("source_id") or expected_sha256 != card.get("sha256"):
        return []
    return chunk_text(extract_text(raw_path))


def _source_chunks(
    paths: KnowledgeBasePaths, cards_by_id: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    chunks: dict[str, list[str]] = {}
    for source_id in sorted(cards_by_id):
        try:
            chunks[source_id] = _chunks_for_source(paths, cards_by_id[source_id])
        except Exception:
            chunks[source_id] = []
    return chunks


def _issues_by_path(issues: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    by_path: dict[str, list[dict[str, str]]] = {}
    for issue in issues:
        path = issue.get("path")
        if not path:
            continue
        by_path.setdefault(path, []).append(_redacted_issue(issue))
    for path in by_path:
        by_path[path] = sorted(by_path[path], key=lambda item: tuple(sorted(item.items())))
    return by_path


def _quote_support_for_page(
    body: str,
    source_ids: list[str],
    cards_by_id: dict[str, dict[str, str]],
    chunks_by_source: dict[str, list[str]],
) -> tuple[list[dict[str, str]], int]:
    support: list[dict[str, str]] = []
    unsupported_statements = 0
    seen: set[tuple[str, str, str]] = set()
    for paragraph in non_heading_paragraphs(body):
        paragraph_source_ids = sorted(set(SOURCE_ID_RE.findall(paragraph)))
        if not paragraph_source_ids:
            continue
        statements = factual_statements(paragraph)
        for statement in statements:
            quote = normalize_whitespace(statement)
            if not quote:
                continue
            statement_supported = False
            for source_id in paragraph_source_ids:
                if source_id not in source_ids:
                    continue
                for chunk_index, chunk in enumerate(chunks_by_source.get(source_id, [])):
                    if quote not in chunk:
                        continue
                    chunk_id = f"{source_id}#{chunk_index}"
                    card = cards_by_id.get(source_id, {})
                    public_quote = (
                        "[redacted-private-quote]"
                        if not _is_public_privacy(card.get("privacy"))
                        else redact_text(quote)
                    )
                    key = (source_id, chunk_id, public_quote)
                    statement_supported = True
                    if key in seen:
                        continue
                    seen.add(key)
                    support.append(
                        {
                            "source_id": source_id,
                            "chunk": chunk_id,
                            "quote": public_quote,
                            "status": "supported",
                        }
                    )
                    break
            if not statement_supported:
                unsupported_statements += 1
    return (
        sorted(support, key=lambda item: (item["source_id"], item["chunk"], item["quote"])),
        unsupported_statements,
    )


def _stable_wiki_reports(
    paths: KnowledgeBasePaths,
    lint_issues: list[dict[str, str]],
    cards_by_id: dict[str, dict[str, str]],
    chunks_by_source: dict[str, list[str]],
) -> list[dict[str, object]]:
    by_path = _issues_by_path(lint_issues)
    pages: list[dict[str, object]] = []
    for _page, label, body in _read_wiki_pages(paths):
        source_ids = sorted(set(SOURCE_ID_RE.findall(body)))
        issues = by_path.get(label, [])
        citation_issue_types = {
            issue.get("type", "")
            for issue in issues
            if issue.get("type") in {"missing-citation", "invalid-source-reference"}
        }
        if citation_issue_types:
            citation_status = "failed"
        elif source_ids:
            citation_status = "pass"
        else:
            citation_status = "warning"
        quote_support, unsupported_statement_count = _quote_support_for_page(
            body, source_ids, cards_by_id, chunks_by_source
        )
        if source_ids and unsupported_statement_count:
            issues = list(issues) + [
                {
                    "type": "quote-support-unconfirmed",
                    "path": label,
                    "count": str(unsupported_statement_count),
                    "reason": "stable prose citation exists but exact quote support was not deterministically confirmed",
                }
            ]
        pages.append(
            {
                "path": label,
                "kind": "stable",
                "citation_status": citation_status,
                "source_ids": source_ids,
                "quote_support": quote_support,
                "issues": sorted(issues, key=lambda item: tuple(sorted(item.items()))),
            }
        )
    return sorted(pages, key=lambda item: str(item["path"]))


def _draft_reports(paths: KnowledgeBasePaths) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    draft_reports: list[dict[str, object]] = []
    readiness: list[dict[str, object]] = []
    drafts = sorted(paths.drafts.rglob("*.md"), key=lambda path: _path_label(paths, path))
    for draft in drafts:
        label = _path_label(paths, draft)
        issues = [_redacted_issue(issue) for issue in validate_draft(paths, draft)]
        validation_status = "pass" if not issues else "failed"
        try:
            metadata, _body = read_draft(draft)
        except RuntimeError:
            metadata = {}
        raw_context_sources = metadata.get("context_sources", [])
        if isinstance(raw_context_sources, list):
            context_sources = sorted(str(item) for item in raw_context_sources)
        elif isinstance(raw_context_sources, str):
            context_sources = sorted(
                part.strip() for part in raw_context_sources.split(",") if part.strip()
            )
        else:
            context_sources = []
        draft_reports.append(
            {
                "path": label,
                "kind": "draft",
                "validation_status": validation_status,
                "context_sources": context_sources,
                "issues": sorted(issues, key=lambda item: tuple(sorted(item.items()))),
            }
        )
        readiness.append(
            {
                "path": label,
                "classification": "ready" if not issues else "blocked",
                "validation_status": validation_status,
                "blocking_issues": sorted(
                    issues, key=lambda item: tuple(sorted(item.items()))
                ),
            }
        )
    return draft_reports, readiness


def _audit_report(paths: KnowledgeBasePaths) -> dict[str, object]:
    log_path = paths.meta / "log.md"
    recent_entries: list[str] = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        if any(marker in line for marker in ("[publish-draft]", "[repair-draft]", "rollback")):
            recent_entries.append(redact_text(line))
    return {
        "log_path": "meta/log.md",
        "recent_entries": recent_entries[-20:],
        "rollback_pointers": [
            "publish-draft keeps previous target bytes and restores audit log on failure",
            "meta/log.md contains publish audit events",
        ],
    }


def _residual_risks(
    stable_pages: list[dict[str, object]],
    draft_reports: list[dict[str, object]],
    governance: dict[str, object],
    source_reports: list[dict[str, object]],
) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    for page in stable_pages:
        issues = page.get("issues", [])
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict) and issue.get("type") in BLOCKING_LINT_TYPES:
                    risks.append(
                        {
                            "classification": "blocking_evidence_gap",
                            "path": str(page.get("path", "")),
                            "issue_type": str(issue.get("type", "")),
                        }
                    )
    for draft in draft_reports:
        if draft.get("validation_status") != "pass":
            risks.append(
                {
                    "classification": "draft_validation_gap",
                    "path": str(draft.get("path", "")),
                    "issue_type": "validate_draft_failed",
                }
            )
    for source in source_reports:
        review = source.get("review", {})
        raw_integrity = source.get("raw_integrity", {})
        if isinstance(review, dict) and review.get("status") == "failed":
            risks.append(
                {
                    "classification": "source_review_blocker",
                    "path": f"sources/{source.get('source_id', '')}.md",
                    "issue_type": "source_review_failed",
                }
            )
        if isinstance(raw_integrity, dict) and raw_integrity.get("status") == "failed":
            risks.append(
                {
                    "classification": "source_integrity_blocker",
                    "path": f"sources/{source.get('source_id', '')}.md",
                    "issue_type": "raw_integrity_failed",
                }
            )
    blocking_count = governance.get("blocking_count", 0)
    if isinstance(blocking_count, int) and blocking_count:
        risks.append(
            {
                "classification": "governance_blocker",
                "path": "",
                "issue_type": "governance_blocking",
            }
        )
    return sorted(risks, key=lambda item: (item["classification"], item["path"], item["issue_type"]))


def _status(
    risks: list[dict[str, str]],
    governance: dict[str, object],
    source_reports: list[dict[str, object]],
    stable_pages: list[dict[str, object]],
) -> str:
    if risks:
        return "failed"
    advisory_count = governance.get("advisory_count", 0)
    if isinstance(advisory_count, int) and advisory_count:
        return "warning"
    for source in source_reports:
        review = source.get("review", {})
        if isinstance(review, dict) and review.get("status") == "warning":
            return "warning"
    for page in stable_pages:
        if page.get("citation_status") == "warning":
            return "warning"
        if page.get("issues"):
            return "warning"
    return "pass"


def _classification(status: str) -> str:
    if status == "failed":
        return "trust_report_failed"
    if status == "warning":
        return "trust_report_warning"
    return "trust_report_pass"


def _summary(
    source_reports: list[dict[str, object]],
    stable_pages: list[dict[str, object]],
    draft_reports: list[dict[str, object]],
    governance: dict[str, object],
    risks: list[dict[str, str]],
) -> dict[str, int]:
    advisory_count = governance.get("advisory_count", 0)
    return {
        "sources": len(source_reports),
        "stable_pages": len(stable_pages),
        "drafts": len(draft_reports),
        "blocking_residual_risks": len(risks),
        "advisory_governance_issues": advisory_count
        if isinstance(advisory_count, int)
        else 0,
    }


def trust_report(root: str | Path) -> dict[str, object]:
    """Return a deterministic, read-only trust report for ``root``."""

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    cards = _source_cards(paths)
    cards_by_id = {card["source_id"]: card for card in cards}
    lint_issues = lint_repository(paths.root)
    governance = _json_safe(analyze_governance(paths.root))
    source_reports = _source_reports(paths, cards)
    chunks_by_source = _source_chunks(paths, cards_by_id)
    stable_pages = _stable_wiki_reports(paths, lint_issues, cards_by_id, chunks_by_source)
    draft_reports, readiness = _draft_reports(paths)
    risks = _residual_risks(stable_pages, draft_reports, governance, source_reports)
    report_status = _status(risks, governance, source_reports, stable_pages)
    summary = _summary(source_reports, stable_pages, draft_reports, governance, risks)

    return _json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "status": report_status,
            "classification": _classification(report_status),
            "summary": summary,
            "root": {
                "path": str(paths.root),
                "trust_principles": list(TRUST_PRINCIPLES),
            },
            "sources": source_reports,
            "stable_wiki": stable_pages,
            "drafts": draft_reports,
            "publish_readiness": readiness,
            "governance": governance,
            "audit": _audit_report(paths),
            "residual_risks": risks,
        }
    )


__all__ = ["trust_report"]
