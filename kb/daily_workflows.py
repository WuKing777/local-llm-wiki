from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .locks import acquire_write_lock
from .paths import KnowledgeBasePaths
from .redaction import redact_text


CANDIDATE_STATUSES = ("pending", "approved", "rejected", "published")


def create_daily_workflow_plan(
    root: str | Path, *, workflow_date: str | None = None
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    clean_date = _workflow_date(workflow_date)
    with acquire_write_lock(paths.root, operation="daily-workflow"):
        plan = _build_plan(paths, clean_date)
        plan_path = paths.meta / "workflows" / "daily" / f"{clean_date}.json"
        _ensure_safe_workflow_path(paths, plan_path)
        _write_json_atomic(plan_path, plan)
        return {**plan, "path": str(plan_path)}


def redacted_cli_payload(result: dict[str, object]) -> dict[str, object]:
    keys = (
        "path",
        "workflow_id",
        "date",
        "created_at",
        "status",
        "commands",
        "open_items",
        "source_counts",
        "candidate_counts",
        "review_targets",
    )
    return _redact_json_value({key: result[key] for key in keys if key in result})


def _require_initialized(paths: KnowledgeBasePaths) -> None:
    from .commands import _require_initialized_repository

    _require_initialized_repository(paths)


def _workflow_date(value: str | None) -> str:
    if value is None or not str(value).strip():
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise RuntimeError("Invalid workflow date") from None


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _build_plan(paths: KnowledgeBasePaths, workflow_date: str) -> dict[str, object]:
    source_counts = _source_counts(paths)
    candidate_counts, candidate_open_items = _candidate_counts(paths)
    topic_suggestion_count = _topic_suggestion_count(paths)
    review_queue_open_count = _review_queue_open_count(paths)
    open_items = list(candidate_open_items)
    if review_queue_open_count:
        open_items.append(
            {
                "type": "review-queue-open-items",
                "count": review_queue_open_count,
                "path": "meta/review-queue.md",
            }
        )
    if candidate_counts["pending"]:
        open_items.append(
            {
                "type": "pending-memory-candidates",
                "count": candidate_counts["pending"],
                "path": "meta/memory-candidates",
            }
        )

    return {
        "workflow_id": f"daily-{workflow_date}",
        "date": workflow_date,
        "created_at": _timestamp(),
        "status": "planned",
        "commands": _commands(paths, workflow_date),
        "open_items": open_items,
        "source_counts": {
            **source_counts,
            "topic_suggestions": topic_suggestion_count,
            "review_queue_open_items": review_queue_open_count,
        },
        "candidate_counts": candidate_counts,
        "review_targets": _review_targets(workflow_date),
    }


def _commands(paths: KnowledgeBasePaths, workflow_date: str) -> list[str]:
    root = "<root>"
    period_week = _iso_week(workflow_date)
    period_month = workflow_date[:7]
    return [
        (
            "capture-candidate --root "
            f"{root} --type preference --text <memory-candidate> "
            f"--event-date {workflow_date} --privacy personal --confidence confirmed "
            "--value-reason <why-this-matters> --suggested-source-type self_statement"
        ),
        f"suggest-topics --root {root}",
        (
            f"compile-page --root {root} --kind daily --date {workflow_date} "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind weekly-review --period {period_week} "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind monthly-review --period {period_month} "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind goal --title <goal-title> "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind project --title <project-title> "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind decision --title <decision-title> "
            "--archive-existing"
        ),
        (
            f"compile-page --root {root} --kind preference-summary "
            "--archive-existing"
        ),
    ]


def _iso_week(workflow_date: str) -> str:
    parsed = date.fromisoformat(workflow_date)
    year, week, _weekday = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _review_targets(workflow_date: str) -> list[dict[str, str]]:
    return [
        {"kind": "daily", "date": workflow_date},
        {"kind": "weekly-review", "period": _iso_week(workflow_date)},
        {"kind": "monthly-review", "period": workflow_date[:7]},
        {"kind": "goal", "template": "<goal-title>"},
        {"kind": "project", "template": "<project-title>"},
        {"kind": "decision", "template": "<decision-title>"},
        {"kind": "preference-summary", "template": "preference-summary"},
    ]


def _source_counts(paths: KnowledgeBasePaths) -> dict[str, int]:
    return {
        "source_cards": _count_files(paths.sources, "src-*.md"),
        "raw_files": sum(1 for path in paths.raw.rglob("*") if path.is_file())
        if paths.raw.is_dir()
        else 0,
    }


def _count_files(directory: Path, pattern: str) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob(pattern) if path.is_file())


def _topic_suggestion_count(paths: KnowledgeBasePaths) -> int:
    return _count_files(paths.meta / "topic-suggestions", "*.json")


def _review_queue_open_count(paths: KnowledgeBasePaths) -> int:
    review_queue = paths.meta / "review-queue.md"
    try:
        lines = review_queue.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in lines if line.strip().startswith("- [ ]"))


def _candidate_counts(
    paths: KnowledgeBasePaths,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    counts["damaged"] = 0
    open_items: list[dict[str, object]] = []
    directory = paths.meta / "memory-candidates"
    if not directory.is_dir():
        return counts, open_items
    for candidate_path in sorted(directory.glob("*.json")):
        if candidate_path.is_symlink() or not candidate_path.is_file():
            counts["damaged"] += 1
            open_items.append(_damaged_candidate_item(candidate_path.name))
            continue
        try:
            data = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            counts["damaged"] += 1
            open_items.append(_damaged_candidate_item(candidate_path.name))
            continue
        if not isinstance(data, dict):
            counts["damaged"] += 1
            open_items.append(_damaged_candidate_item(candidate_path.name))
            continue
        status = data.get("status")
        if status in CANDIDATE_STATUSES:
            counts[str(status)] += 1
        else:
            counts["damaged"] += 1
            open_items.append(_damaged_candidate_item(candidate_path.name))
    return counts, open_items


def _damaged_candidate_item(filename: str) -> dict[str, object]:
    return {
        "type": "damaged-memory-candidate",
        "path": f"meta/memory-candidates/{redact_text(filename)}",
        "advisory": "candidate JSON could not be counted by status",
    }


def _ensure_safe_workflow_path(paths: KnowledgeBasePaths, path: Path) -> None:
    workflows = paths.meta / "workflows"
    daily = workflows / "daily"
    if workflows.is_symlink() or daily.is_symlink():
        raise RuntimeError("workflow path is unsafe")
    if workflows.exists() and not workflows.is_dir():
        raise RuntimeError("workflow path is unsafe")
    if daily.exists() and not daily.is_dir():
        raise RuntimeError("workflow path is unsafe")
    try:
        path.resolve(strict=False).relative_to(daily.resolve(strict=False))
        path.resolve(strict=False).relative_to(paths.root.resolve(strict=False))
        daily.resolve(strict=False).relative_to(paths.meta.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("workflow path is unsafe") from None


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    created_dirs = _missing_parent_dirs(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    succeeded = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            json.dump(data, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
        os.replace(temp_path, path)
        succeeded = True
    finally:
        if temp_path.exists():
            temp_path.unlink()
        if not succeeded:
            _remove_empty_dirs(created_dirs)


def _missing_parent_dirs(path: Path) -> list[Path]:
    directories: list[Path] = []
    current = path.parent
    while not current.exists():
        directories.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return directories


def _remove_empty_dirs(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


__all__ = ["create_daily_workflow_plan", "redacted_cli_payload"]
