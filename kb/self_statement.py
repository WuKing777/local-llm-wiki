import re
import tempfile
from datetime import date, datetime
from pathlib import Path

from .paths import KnowledgeBasePaths
from .sources import (
    imported_timestamp,
    relative_path,
    source_id_and_sha256,
    title_for,
    upsert_source_map,
    write_source_card,
)
from .text import kind_for_path


PRIVACY_VALUES = {"public", "personal", "sensitive", "restricted"}
CONFIDENCE_VALUES = {"confirmed", "likely", "uncertain", "unknown"}
INPUT_METHOD_VALUES = {"chat", "obsidian", "manual_file", "voice_transcript", "imported_note"}
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|api[_-]?key|password|token)",
    re.IGNORECASE,
)


def _require_initialized(paths: KnowledgeBasePaths) -> None:
    from .commands import _require_initialized_repository

    try:
        _require_initialized_repository(paths)
    except RuntimeError:
        raise RuntimeError("root is not initialized") from None
    if not (paths.meta / "source-map.jsonl").is_file():
        raise RuntimeError("root is not initialized")


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _safe_stem(text: str) -> str:
    stem = re.sub(r"[^\w.-]+", "-", _single_line(text), flags=re.UNICODE).strip(".-")
    return stem[:48].strip(".-") or "self-statement"


def _validate_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise RuntimeError(f"Invalid event_date: {value}") from None


def _validate_inputs(
    *,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    input_method: str,
) -> tuple[str, str, str, str, str]:
    clean_text = text.strip()
    if not clean_text:
        raise RuntimeError("self_statement text must not be blank")
    event_date = _validate_date(event_date)
    privacy = privacy.strip().casefold()
    confidence = confidence.strip().casefold()
    input_method = input_method.strip().casefold()
    if privacy not in PRIVACY_VALUES:
        raise RuntimeError(f"Invalid privacy: {privacy}")
    if confidence not in CONFIDENCE_VALUES:
        raise RuntimeError(f"Invalid confidence: {confidence}")
    if input_method not in INPUT_METHOD_VALUES:
        raise RuntimeError(f"Invalid input_method: {input_method}")
    if SECRET_RE.search(clean_text):
        raise RuntimeError("self_statement contains suspected secret")
    return clean_text, event_date, privacy, confidence, input_method


def _raw_markdown(
    *,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    input_method: str,
    created_at: str,
) -> str:
    return "\n".join(
        [
            "---",
            "type: self_statement_raw",
            f"created_at: {created_at}",
            f"event_date: {event_date}",
            f"input_method: {input_method}",
            f"privacy: {privacy}",
            f"confidence: {confidence}",
            "---",
            "",
            "# 本人直接陈述",
            "",
            text,
            "",
        ]
    )


def _replace_with_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    ) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    try:
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _missing_parent_dirs(path: Path, stop: Path) -> list[Path]:
    directories: list[Path] = []
    current = path.parent
    stop_resolved = stop.resolve()
    while not current.exists():
        try:
            current.resolve().relative_to(stop_resolved)
        except ValueError:
            break
        directories.append(current)
        current = current.parent
    return directories


def _remove_empty_dirs(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _append_review_queue(paths: KnowledgeBasePaths, metadata: dict[str, str]) -> None:
    with (paths.meta / "review-queue.md").open("a", encoding="utf-8") as review:
        review.write(
            f"- [ ] Review self_statement {metadata['source_id']} "
            f"privacy={metadata['privacy']} confidence={metadata['confidence']}\n"
        )


def create_self_statement(
    root: str | Path,
    *,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    input_method: str,
) -> dict[str, str]:
    """Create an auditable self_statement source without writing stable wiki pages."""
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    clean_text, event_date, privacy, confidence, input_method = _validate_inputs(
        text=text,
        event_date=event_date,
        privacy=privacy,
        confidence=confidence,
        input_method=input_method,
    )

    created_at = datetime.now().replace(microsecond=0).isoformat()
    raw_text = _raw_markdown(
        text=clean_text,
        event_date=event_date,
        privacy=privacy,
        confidence=confidence,
        input_method=input_method,
        created_at=created_at,
    )
    raw_data = raw_text.encode("utf-8")
    source_id, sha256 = source_id_and_sha256(raw_data)
    raw_path = paths.raw / "self-statements" / event_date / f"{_safe_stem(clean_text)}-{source_id[4:]}.md"
    created_raw_dirs = _missing_parent_dirs(raw_path, paths.raw)
    pending = confidence in {"uncertain", "unknown"} or privacy in {"sensitive", "restricted"}
    metadata = {
        "source_id": source_id,
        "title": _single_line(title_for(raw_path, raw_text))[:120],
        "raw_path": relative_path(paths.root, raw_path),
        "sha256": sha256,
        "imported_at": imported_timestamp(),
        "kind": kind_for_path(raw_path),
        "source_type": "self_statement",
        "privacy": privacy,
        "confidence": confidence,
        "event_date": event_date,
        "input_method": input_method,
        "pending_confirmation": "true" if pending else "false",
        "review_status": "pending" if pending else "reviewed",
    }

    source_card = paths.sources / f"{source_id}.md"
    source_map = paths.meta / "source-map.jsonl"
    review_queue = paths.meta / "review-queue.md"
    log_path = paths.meta / "log.md"
    database_path = paths.database
    source_map_before = source_map.read_text(encoding="utf-8")
    review_before = review_queue.read_text(encoding="utf-8")
    log_before = log_path.read_text(encoding="utf-8") if log_path.exists() else None
    database_before = database_path.read_bytes() if database_path.exists() else None
    raw_existed = raw_path.exists()
    card_existed = source_card.exists()
    raw_before = raw_path.read_bytes() if raw_existed else None
    card_before = source_card.read_bytes() if card_existed else None

    try:
        _replace_with_bytes(raw_path, raw_data)
        write_source_card(paths, metadata)
        upsert_source_map(paths, metadata)
        if pending:
            _append_review_queue(paths, metadata)

        from . import commands as _commands

        chunks = _commands._index_source(paths, metadata)
        _commands._append_event(
            paths,
            "self-statement",
            f"{source_id} {metadata['raw_path']} ({chunks} chunks)",
        )
    except Exception:
        if raw_existed and raw_before is not None:
            raw_path.write_bytes(raw_before)
        elif raw_path.exists():
            raw_path.unlink()
        _remove_empty_dirs(created_raw_dirs)
        if card_existed and card_before is not None:
            source_card.write_bytes(card_before)
        elif source_card.exists():
            source_card.unlink()
        source_map.write_text(source_map_before, encoding="utf-8")
        review_queue.write_text(review_before, encoding="utf-8")
        if log_before is not None:
            log_path.write_text(log_before, encoding="utf-8")
        elif log_path.exists():
            log_path.unlink()
        if database_before is not None:
            database_path.write_bytes(database_before)
        elif database_path.exists():
            database_path.unlink()
        raise

    return metadata
