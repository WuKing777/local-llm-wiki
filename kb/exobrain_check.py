"""Read-only proactive status check for a personal exobrain repository."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .commands import _path_label, _require_initialized_repository, _stable_wiki_files
from .governance import analyze_governance
from .memory_candidates import (
    _read_candidate,
    _validate_candidate_id,
    _validate_candidate_record,
)
from .paths import KnowledgeBasePaths
from .redaction import redact_text


SCHEMA_VERSION = 1
CREATED_AT = "deterministic-read-only"
NOTICES = [
    "AI is not a fact source; use local evidence for factual claims.",
    "Stable content requires source review before becoming trusted knowledge.",
    "Memory candidates require confirmation before publishing stable sources.",
]
PATH_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_.-]{6,}(?=$|[\\/\s,;)\]}'\"])")


def _safe_text(value: object) -> str:
    redacted = PATH_SECRET_RE.sub("[redacted-secret]", str(value))
    return redact_text(redacted).replace("[redacted-api-key]", "[redacted-secret]")


def _safe_relative(paths: KnowledgeBasePaths, path: Path) -> str:
    return _safe_text(_path_label(paths, path))


def _json_files(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    try:
        directory_root = directory.resolve(strict=True)
        directory_root.relative_to(directory.parent.resolve(strict=True))
    except OSError:
        return []
    except ValueError:
        return []
    files: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(directory_root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            files.append(path)
    return files


def _count_raw_files(paths: KnowledgeBasePaths) -> int:
    if paths.raw.is_symlink() or not paths.raw.is_dir():
        return 0
    try:
        raw_root = paths.raw.resolve(strict=True)
        raw_root.relative_to(paths.root.resolve(strict=True))
    except OSError:
        return 0
    except ValueError:
        return 0
    count = 0
    for path in paths.raw.rglob("*"):
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(raw_root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            count += 1
    return count


def _count_source_cards(paths: KnowledgeBasePaths) -> int:
    if paths.sources.is_symlink() or not paths.sources.is_dir():
        return 0
    try:
        sources_root = paths.sources.resolve(strict=True)
        root = paths.root.resolve(strict=True)
        sources_root.relative_to(root)
    except OSError:
        return 0
    except ValueError:
        raise RuntimeError(f"source card path is unsafe: {_safe_text(paths.sources.name)}") from None

    count = 0
    for card in sorted(paths.sources.glob("src-*.md")):
        if card.is_symlink():
            raise RuntimeError(f"source card path is unsafe: {_safe_text(card.name)}")
        try:
            resolved = card.resolve(strict=True)
            resolved.relative_to(sources_root)
            resolved.relative_to(root)
        except (OSError, ValueError):
            raise RuntimeError(f"source card path is unsafe: {_safe_text(card.name)}") from None
        if card.is_file():
            count += 1
    return count


def _count_drafts(paths: KnowledgeBasePaths) -> tuple[int, list[str]]:
    if not paths.drafts.is_dir():
        return 0, []
    drafts: list[str] = []
    drafts_root = paths.drafts.resolve()
    root = paths.root.resolve()
    for path in sorted(paths.drafts.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(drafts_root)
            resolved.relative_to(root)
        except ValueError:
            continue
        drafts.append(_safe_relative(paths, path))
    return len(drafts), drafts


def _candidate_counts(paths: KnowledgeBasePaths) -> tuple[dict[str, int], dict[str, list[str]]]:
    counts = {
        "pending_memory_candidates": 0,
        "approved_memory_candidates": 0,
        "damaged_memory_candidates": 0,
    }
    ids = {"pending": [], "approved": [], "damaged": []}
    candidate_dir = paths.meta / "memory-candidates"
    if candidate_dir.is_symlink():
        return counts, ids
    try:
        candidate_root = candidate_dir.resolve(strict=True) if candidate_dir.exists() else None
        if candidate_root is not None:
            candidate_root.relative_to(paths.meta.resolve(strict=True))
    except OSError:
        candidate_root = None
    except ValueError:
        candidate_root = None
    for path in sorted(candidate_dir.glob("*.json")) if candidate_dir.is_dir() else []:
        candidate_id = path.stem
        if path.is_symlink():
            counts["damaged_memory_candidates"] += 1
            ids["damaged"].append(_safe_text(candidate_id))
            continue
        if candidate_root is None:
            continue
        try:
            path.resolve(strict=True).relative_to(candidate_root)
        except (OSError, ValueError):
            counts["damaged_memory_candidates"] += 1
            ids["damaged"].append(_safe_text(candidate_id))
            continue
        try:
            clean_id = _validate_candidate_id(candidate_id)
            data = _validate_candidate_record(
                _read_candidate(path),
                candidate_id=clean_id,
                allow_published=True,
            )
        except RuntimeError:
            counts["damaged_memory_candidates"] += 1
            ids["damaged"].append(_safe_text(candidate_id))
            continue
        status = str(data.get("status", ""))
        if status == "pending":
            counts["pending_memory_candidates"] += 1
            ids["pending"].append(clean_id)
        elif status == "approved":
            counts["approved_memory_candidates"] += 1
            ids["approved"].append(clean_id)
    return counts, ids


def _count_topic_suggestions(paths: KnowledgeBasePaths) -> int:
    return len(_json_files(paths.meta / "topic-suggestions"))


def _count_review_queue_open_items(paths: KnowledgeBasePaths) -> int:
    review_queue = paths.meta / "review-queue.md"
    if not review_queue.is_file():
        return 0
    try:
        lines = review_queue.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in lines if line.strip().startswith("- [ ]"))


def _count_benchmark_records(paths: KnowledgeBasePaths) -> int:
    evals = paths.meta / "evals"
    if evals.is_symlink() or not evals.exists():
        return 0
    if not evals.is_dir():
        return 0
    benchmark = paths.meta / "evals" / "retrieval-benchmark.jsonl"
    if benchmark.is_symlink() or not benchmark.is_file():
        return 0
    try:
        evals_root = evals.resolve(strict=True)
        evals_root.relative_to(paths.meta.resolve(strict=True))
        benchmark.resolve(strict=True).relative_to(evals_root)
        return sum(1 for line in benchmark.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return 0


def _action(
    action_id: str,
    priority: int,
    reason: str,
    command: dict[str, object],
    *,
    requires_confirmation: bool = False,
) -> dict[str, object]:
    return {
        "id": action_id,
        "priority": priority,
        "reason": _safe_text(reason),
        "command": _redact_json(command),
        "requires_confirmation": requires_confirmation,
    }


def _redact_json(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _safe_text(value)


def _next_actions(
    counts: dict[str, int],
    ids: dict[str, list[str]],
    drafts: list[str],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if counts["governance_blocking"]:
        actions.append(
            _action(
                "resolve-governance-blocking",
                10,
                "Blocking governance issues must be resolved first.",
                {"argv": ["python", "-m", "kb", "govern", "--root", "<root>"]},
                requires_confirmation=True,
            )
        )
    if counts["damaged_memory_candidates"]:
        actions.append(
            _action(
                "repair-damaged-candidates",
                20,
                "Damaged memory candidate JSON needs manual inspection.",
                {"kind": "manual-inspect", "path": "meta/memory-candidates"},
            )
        )
    for candidate_id in ids["pending"]:
        actions.append(
            _action(
                "review-candidate",
                30,
                "Pending memory candidate needs review before it can become a source.",
                {
                    "argv": [
                        "python",
                        "-m",
                        "kb",
                        "review-candidate",
                        candidate_id,
                        "--root",
                        "<root>",
                        "--status",
                        "approved|rejected",
                    ]
                },
                requires_confirmation=True,
            )
        )
    for candidate_id in ids["approved"]:
        actions.append(
            _action(
                "publish-memory",
                40,
                "Approved memory candidate can be published only after confirmation.",
                {
                    "argv": [
                        "python",
                        "-m",
                        "kb",
                        "publish-memory",
                        candidate_id,
                        "--root",
                        "<root>",
                        "--confirm",
                    ]
                },
                requires_confirmation=True,
            )
        )
    for draft in drafts:
        actions.append(
            _action(
                "validate-draft",
                50,
                "Draft must pass evidence validation before publishing.",
                {
                    "argv": [
                        "python",
                        "-m",
                        "kb",
                        "validate-draft",
                        "--root",
                        "<root>",
                        draft,
                    ]
                },
            )
        )
        actions.append(
            _action(
                "publish-draft",
                60,
                "Validated draft can be published only through publish-draft.",
                {
                    "argv": [
                        "python",
                        "-m",
                        "kb",
                        "publish-draft",
                        "--root",
                        "<root>",
                        draft,
                        "--target",
                        "<target-title>",
                    ]
                },
                requires_confirmation=True,
            )
        )
    if counts["topic_suggestions"]:
        actions.append(
            _action(
                "inspect-topic-suggestions",
                70,
                "Topic suggestions are review inputs and should be inspected.",
                {"kind": "manual-inspect", "path": "meta/topic-suggestions"},
            )
        )
        actions.append(
            _action(
                "suggest-topics",
                80,
                "Refresh topic suggestions after new source imports.",
                {"argv": ["python", "-m", "kb", "suggest-topics", "--root", "<root>"]},
                requires_confirmation=True,
            )
        )
    if counts["retrieval_benchmark_records"] == 0:
        actions.append(
            _action(
                "benchmark-add",
                90,
                "No retrieval benchmark records were found.",
                {
                    "argv": [
                        "python",
                        "-m",
                        "kb",
                        "benchmark-add",
                        "--root",
                        "<root>",
                        "--query",
                        "<redacted-query>",
                        "--expected-source-id",
                        "<source-id>",
                    ]
                },
                requires_confirmation=True,
            )
        )
    return sorted(actions, key=lambda item: (int(item["priority"]), str(item["id"])))


def exobrain_check(root: str | Path) -> dict[str, Any]:
    """Return a deterministic, redacted, read-only repository status report."""

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    governance = analyze_governance(paths.root)
    candidate_counts, candidate_ids = _candidate_counts(paths)
    draft_count, drafts = _count_drafts(paths)

    counts = {
        "source_cards": _count_source_cards(paths),
        "raw_files": _count_raw_files(paths),
        "stable_wiki_pages": len(_stable_wiki_files(paths)),
        "drafts": draft_count,
        **candidate_counts,
        "topic_suggestions": _count_topic_suggestions(paths),
        "review_queue_open_items": _count_review_queue_open_items(paths),
        "retrieval_benchmark_records": _count_benchmark_records(paths),
        "governance_blocking": int(governance["blocking_count"]),
        "governance_advisory": int(governance["advisory_count"])
        + candidate_counts["damaged_memory_candidates"],
    }
    status = "blocked" if counts["governance_blocking"] else "pass"
    classification = "governance_blocking" if counts["governance_blocking"] else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "root": "<root>",
        "status": status,
        "classification": classification,
        "created_at": CREATED_AT,
        "counts": counts,
        "next_actions": _next_actions(counts, candidate_ids, drafts),
        "notices": list(NOTICES),
    }


__all__ = ["exobrain_check"]
