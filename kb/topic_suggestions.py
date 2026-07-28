from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .locks import acquire_write_lock
from .paths import KnowledgeBasePaths
from .redaction import redact_text
from .sources import read_source_card
from .text import extract_text
from .wiki import safe_slug, target_path_for_title


SOURCE_ID_RE = re.compile(r"^src-[0-9a-f]{12}$")
CONFLICT_MARKERS = (
    "conflict",
    "conflicting",
    "contradict",
    "contradiction",
    "\u51b2\u7a81",
    "\u77db\u76fe",
    "\u4e0d\u4e00\u81f4",
)


def create_topic_suggestions(
    root: str | Path, *, source_ids: list[str] | None = None
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    with acquire_write_lock(paths.root, operation="suggest-topics"):
        return _create_topic_suggestions_unlocked(paths, source_ids=source_ids)


def _create_topic_suggestions_unlocked(
    paths: KnowledgeBasePaths, *, source_ids: list[str] | None = None
) -> dict[str, object]:
    for source_id in source_ids or []:
        _validate_source_id(source_id)
    cards = _load_source_cards(paths)
    if not cards:
        raise RuntimeError("No source cards found")

    if source_ids:
        by_id = {card["source_id"]: card for card in cards}
        unknown = [source_id for source_id in source_ids if source_id not in by_id]
        if unknown:
            raise RuntimeError(f"Unknown source id: {unknown[0]}")
        selected = [by_id[source_id] for source_id in source_ids]
    else:
        selected = cards

    suggestion = _build_suggestion(paths, selected)
    suggestion_dir = paths.meta / "topic-suggestions"
    suggestion_path = suggestion_dir / f"{suggestion['suggestion_id']}.json"
    _ensure_safe_suggestion_path(paths, suggestion_path)
    _write_json_atomic(suggestion_path, suggestion)
    return {**suggestion, "path": str(suggestion_path)}


def _require_initialized(paths: KnowledgeBasePaths) -> None:
    from .commands import _require_initialized_repository

    _require_initialized_repository(paths)


def redacted_cli_payload(result: dict[str, object]) -> dict[str, object]:
    payload = {
        "path": result["path"],
        "suggestion_id": result["suggestion_id"],
        "created_at": result["created_at"],
        "source_ids": result["source_ids"],
        "suggested_pages": result["suggested_pages"],
        "duplicate_candidates": result["duplicate_candidates"],
        "conflict_markers": result["conflict_markers"],
        "next_actions": result["next_actions"],
    }
    return _redact_json_value(payload)


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _load_source_cards(paths: KnowledgeBasePaths) -> list[dict[str, str]]:
    if not paths.sources.is_dir():
        return []
    cards: list[dict[str, str]] = []
    sources_root = paths.sources.resolve()
    for card_path in sorted(paths.sources.glob("src-*.md")):
        _ensure_safe_source_card_path(paths, card_path, sources_root)
        if not card_path.is_file():
            continue
        try:
            card = read_source_card(card_path)
        except RuntimeError as exc:
            if "source_id" in str(exc):
                raise RuntimeError(
                    f"Invalid source id in source card: {card_path.name}"
                ) from exc
            raise
        _validate_source_id(card["source_id"])
        _validate_source_card_against_raw(paths, card)
        cards.append(card)
    return cards


def _validate_source_id(source_id: str) -> None:
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise RuntimeError(f"Invalid source id: {source_id}")


def _ensure_safe_source_card_path(
    paths: KnowledgeBasePaths, card_path: Path, sources_root: Path
) -> None:
    if card_path.is_symlink():
        raise RuntimeError(f"source card path is unsafe: {card_path.name}")
    try:
        resolved = card_path.resolve(strict=False)
        resolved.relative_to(sources_root)
        resolved.relative_to(paths.root.resolve())
    except ValueError:
        raise RuntimeError(f"source card path is unsafe: {card_path.name}") from None


def _validate_source_card_against_raw(
    paths: KnowledgeBasePaths, card: dict[str, str]
) -> None:
    from .commands import _validate_source_for_index

    _validate_source_for_index(paths, card)


def _build_suggestion(
    paths: KnowledgeBasePaths, cards: list[dict[str, str]]
) -> dict[str, object]:
    source_ids = [card["source_id"] for card in cards]
    suggested_pages = [_suggested_page(paths, card) for card in cards]
    duplicate_candidates = _duplicate_candidates(paths, suggested_pages)
    conflict_markers = _conflict_markers(paths, cards)
    return {
        "suggestion_id": f"topic-{secrets.token_hex(8)}",
        "created_at": _timestamp(),
        "source_ids": source_ids,
        "suggested_pages": suggested_pages,
        "duplicate_candidates": duplicate_candidates,
        "conflict_markers": conflict_markers,
        "next_actions": [
            "Review suggested_pages against local sources.",
            "Create LLM drafts only with retrieved local evidence.",
            "Publish stable wiki updates through validate-draft and publish-draft.",
        ],
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _suggested_page(paths: KnowledgeBasePaths, card: dict[str, str]) -> dict[str, object]:
    title = _safe_title(card.get("title", ""), card["source_id"])
    target_title = _target_title(paths, title)
    target_path = target_path_for_title(paths, target_title)
    target_label = target_path.resolve().relative_to(paths.root.resolve()).as_posix()
    kind = "update_page" if target_path.exists() else "new_page"
    return {
        "kind": kind,
        "title": title,
        "target": target_label,
        "reason": "source card title provides a safe local organization target",
        "supporting_source_ids": [card["source_id"]],
    }


def _safe_title(title: str, source_id: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    return cleaned or source_id


def _target_title(paths: KnowledgeBasePaths, title: str) -> str:
    slug = safe_slug(title)
    candidates = [slug]
    if _looks_like_learning(title):
        candidates.insert(0, f"learning/{slug}")
    candidates.append(f"learning/{slug}")
    for candidate in candidates:
        try:
            target_path_for_title(paths, candidate)
        except ValueError:
            continue
        return candidate
    return "learning/source"


def _looks_like_learning(title: str) -> bool:
    lowered = title.casefold()
    if "\u5b66\u4e60" in lowered:
        return True
    return any(token in lowered for token in ("learn", "learning", "study", "学习"))


def _duplicate_candidates(
    paths: KnowledgeBasePaths, suggested_pages: list[dict[str, object]]
) -> list[dict[str, object]]:
    pages = _stable_wiki_pages(paths)
    duplicates: list[dict[str, object]] = []
    for suggested in suggested_pages:
        title = str(suggested["title"])
        target = str(suggested["target"])
        title_key = _normalize_title(title)
        target_key = _normalize_target(target)
        matches: list[str] = []
        for page in pages:
            label = page["path"]
            if _normalize_target(label) == target_key:
                matches.append(label)
                continue
            if _normalize_title(page["title"]) == title_key:
                matches.append(label)
        if matches:
            duplicates.append(
                {
                    "suggested_target": target,
                    "candidates": sorted(set(matches)),
                    "reason": "near match by local wiki title or target path only",
                }
            )
    return duplicates


def _stable_wiki_pages(paths: KnowledgeBasePaths) -> list[dict[str, str]]:
    if not paths.wiki.is_dir():
        return []
    pages: list[dict[str, str]] = []
    wiki_root = paths.wiki.resolve()
    root = paths.root.resolve()
    drafts_root = paths.drafts.resolve()
    for page in sorted(paths.wiki.rglob("*.md")):
        if page.is_symlink():
            continue
        try:
            resolved = page.resolve()
            resolved.relative_to(wiki_root)
            resolved.relative_to(root)
        except ValueError:
            continue
        try:
            resolved.relative_to(drafts_root)
            continue
        except ValueError:
            pass
        if not page.is_file():
            continue
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pages.append(
            {
                "path": resolved.relative_to(root).as_posix(),
                "title": _wiki_title(page, text),
            }
        )
    return pages


def _wiki_title(page: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            return stripped.lstrip("#").strip() or page.stem
    return page.stem.replace("-", " ").replace("_", " ")


def _normalize_title(value: str) -> str:
    return safe_slug(value)


def _normalize_target(value: str) -> str:
    return value.casefold().replace("\\", "/").removesuffix(".md")


def _conflict_markers(
    paths: KnowledgeBasePaths, cards: list[dict[str, str]]
) -> list[dict[str, object]]:
    found: dict[str, set[str]] = {}
    for card in cards:
        text = _read_source_text(paths, card)
        lowered = text.casefold()
        for marker in CONFLICT_MARKERS:
            if marker.casefold() in lowered:
                found.setdefault(marker, set()).add(card["source_id"])
    return [
        {
            "marker": marker,
            "source_ids": sorted(source_ids),
            "reason": "explicit marker appears in local source text",
        }
        for marker, source_ids in sorted(found.items())
    ]


def _read_source_text(paths: KnowledgeBasePaths, card: dict[str, str]) -> str:
    raw_path = _resolve_raw_path(paths, card)
    if not raw_path.is_file():
        return ""
    try:
        return extract_text(raw_path)
    except Exception:
        return ""


def _resolve_raw_path(paths: KnowledgeBasePaths, card: dict[str, str]) -> Path:
    raw_value = card.get("raw_path", "")
    raw_path = Path(raw_value)
    if raw_path.is_absolute():
        raise RuntimeError(f"Invalid raw_path for {card['source_id']}: {raw_value}")
    resolved = (paths.root / raw_path).resolve()
    try:
        resolved.relative_to(paths.raw.resolve())
    except ValueError:
        raise RuntimeError(f"Invalid raw_path for {card['source_id']}: {raw_value}") from None
    return resolved


def _ensure_safe_suggestion_path(paths: KnowledgeBasePaths, path: Path) -> None:
    suggestions = paths.meta / "topic-suggestions"
    if suggestions.is_symlink():
        raise RuntimeError("Topic suggestion directory is unsafe")
    try:
        path.resolve(strict=False).relative_to(suggestions.resolve(strict=False))
        path.resolve(strict=False).relative_to(paths.root.resolve(strict=False))
    except ValueError:
        raise RuntimeError(f"Unsafe topic suggestion path: {path}") from None


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
