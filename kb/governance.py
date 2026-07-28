"""Read-only governance analysis and quality report rendering."""

from __future__ import annotations

from pathlib import Path

from .commands import (
    _duplicate_wiki_title_issues,
    _governance_issue_key,
    _governance_lint_issues,
    _open_review_item_issues,
    _orphan_wiki_page_issues,
    _possible_conflict_issues,
    _read_wiki_pages,
    _require_initialized_repository,
    _source_cards,
    _source_review_status_issues,
    _stale_source_issues,
    lint_repository,
    status_repository,
)
from .paths import KnowledgeBasePaths


def analyze_governance(root: str | Path) -> dict[str, object]:
    """Return governance issues without writing the quality report."""

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    source_cards = _source_cards(paths)
    pages = _read_wiki_pages(paths)
    lint_issues = lint_repository(paths.root)
    status = status_repository(paths.root)

    blocking = _governance_lint_issues(lint_issues)
    stale_issues, _stale_source_ids = _stale_source_issues(paths, source_cards, pages)
    blocking.extend(stale_issues)

    advisory: list[dict[str, str]] = []
    advisory.extend(_duplicate_wiki_title_issues(pages))
    advisory.extend(_possible_conflict_issues(pages))
    advisory.extend(_orphan_wiki_page_issues(paths, pages))

    source_review_issues = _source_review_status_issues(source_cards)
    blocking.extend(
        issue for issue in source_review_issues if issue.get("severity") == "blocking"
    )
    advisory.extend(
        issue for issue in source_review_issues if issue.get("severity") != "blocking"
    )
    advisory.extend(_open_review_item_issues(paths))

    blocking = sorted(blocking, key=_governance_issue_key)
    advisory = sorted(advisory, key=_governance_issue_key)
    return {
        "root": str(paths.root),
        "blocking": blocking,
        "advisory": advisory,
        "blocking_count": len(blocking),
        "advisory_count": len(advisory),
        "status": status,
    }


def _format_governance_section(title: str, issues: list[dict[str, str]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not issues:
        lines.extend(["(none)", ""])
        return lines
    for issue in sorted(issues, key=_governance_issue_key):
        fields = [
            f"{key}={issue[key]}"
            for key in sorted(issue)
            if key not in {"severity", "type"}
        ]
        lines.append(f"- [{issue['type']}] " + " ".join(fields))
    lines.append("")
    return lines


def render_quality_report(analysis: dict[str, object]) -> str:
    """Render a deterministic quality report without writing it."""

    status = analysis.get("status", {})
    if not isinstance(status, dict):
        status = {}
    blocking = analysis.get("blocking", [])
    advisory = analysis.get("advisory", [])
    if not isinstance(blocking, list):
        blocking = []
    if not isinstance(advisory, list):
        advisory = []

    lines = [
        "# Quality Report",
        "",
        "## Summary",
        "",
        f"- blocking: {len(blocking)}",
        f"- advisory: {len(advisory)}",
    ]
    for key in sorted(status):
        lines.append(f"- {key}: {status[key]}")
    lines.append("")
    lines.extend(_format_governance_section("Blocking Issues", blocking))
    lines.extend(_format_governance_section("Advisory Issues", advisory))
    lines.extend(
        [
            "## Scope",
            "",
            "- Blocking issues must be fixed before claiming the knowledge base is clean.",
            "- Advisory issues are visible quality drift signals for review.",
            "- Semantic contradictions are surfaced only when pages contain explicit conflict markers.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["analyze_governance", "render_quality_report"]
