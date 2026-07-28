import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from .commands import _create_self_statement_unlocked
from .locks import acquire_write_lock
from .paths import KnowledgeBasePaths


PRIVACY_VALUES = {"public", "personal", "sensitive", "restricted"}
CONFIDENCE_VALUES = {"confirmed", "likely", "uncertain", "unknown"}
REVIEW_STATUS_VALUES = {"approved", "rejected"}
RECORD_STATUS_VALUES = {"pending", "approved", "rejected", "published"}
PUBLISHABLE_SOURCE_TYPES = {"self_statement"}
CANDIDATE_ID_RE = re.compile(r"^mem-[0-9a-f]{16}$")
SOURCE_ID_RE = re.compile(r"^src-[0-9a-f]{12}$")
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


def _candidate_dir(paths: KnowledgeBasePaths) -> Path:
    return paths.meta / "memory-candidates"


def _require_safe_candidate_directory(paths: KnowledgeBasePaths, directory: Path) -> None:
    if directory.exists() and not directory.is_dir():
        raise RuntimeError("memory candidate path is unsafe")
    meta_real = paths.meta.resolve(strict=True)
    directory_real = directory.resolve(strict=False)
    try:
        directory_real.relative_to(meta_real)
    except ValueError:
        raise RuntimeError("memory candidate path is unsafe") from None


def _validate_candidate_id(candidate_id: str) -> str:
    candidate_id = str(candidate_id).strip()
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise RuntimeError("Invalid candidate id")
    return candidate_id


def _candidate_path(paths: KnowledgeBasePaths, candidate_id: str) -> Path:
    candidate_id = _validate_candidate_id(candidate_id)
    directory = _candidate_dir(paths)
    _require_safe_candidate_directory(paths, directory)
    path = directory / f"{candidate_id}.json"
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError:
        raise RuntimeError("Invalid candidate id") from None
    return path


def _validate_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise RuntimeError("Invalid event_date") from None


def _clean_required(value: str, field: str) -> str:
    clean = str(value).strip()
    if not clean:
        raise RuntimeError(f"{field} must not be blank")
    if SECRET_RE.search(clean):
        raise RuntimeError("memory candidate contains suspected secret")
    return clean


def _validate_capture_inputs(
    *,
    type: str,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    value_reason: str,
    suggested_source_type: str,
) -> dict[str, str]:
    candidate_type = _clean_required(type, "type")
    clean_text = _clean_required(text, "text")
    clean_reason = _clean_required(value_reason, "value_reason")
    event_date = _validate_date(event_date)
    privacy = str(privacy).strip().casefold()
    confidence = str(confidence).strip().casefold()
    suggested_source_type = str(suggested_source_type).strip().casefold()
    if privacy not in PRIVACY_VALUES:
        raise RuntimeError(f"Invalid privacy: {privacy}")
    if confidence not in CONFIDENCE_VALUES:
        raise RuntimeError(f"Invalid confidence: {confidence}")
    if suggested_source_type not in PUBLISHABLE_SOURCE_TYPES:
        raise RuntimeError(f"Invalid suggested_source_type: {suggested_source_type}")
    return {
        "type": candidate_type,
        "text": clean_text,
        "event_date": event_date,
        "privacy": privacy,
        "confidence": confidence,
        "value_reason": clean_reason,
        "suggested_source_type": suggested_source_type,
    }


def _read_candidate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("memory candidate not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("memory candidate is not readable JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("memory candidate is not a JSON object")
    return data


def _invalid_candidate(message: str) -> RuntimeError:
    return RuntimeError(f"Invalid memory candidate: {message}")


def _require_string_field(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid_candidate(f"{field} is required")
    if SECRET_RE.search(value):
        raise _invalid_candidate(f"{field} contains suspected secret")
    return value


def _validate_candidate_record(
    data: dict[str, Any],
    *,
    candidate_id: str,
    allow_published: bool = False,
) -> dict[str, Any]:
    required = (
        "id",
        "type",
        "text",
        "original_text",
        "event_date",
        "privacy",
        "confidence",
        "needs_confirmation",
        "value_reason",
        "suggested_source_type",
        "created_at",
        "status",
    )
    for field in required:
        if field not in data:
            raise _invalid_candidate(f"{field} is missing")
    if data.get("id") != candidate_id:
        raise _invalid_candidate("id does not match file name")
    _require_string_field(data, "type")
    _require_string_field(data, "text")
    _require_string_field(data, "original_text")
    _require_string_field(data, "value_reason")
    try:
        _validate_date(_require_string_field(data, "event_date"))
    except RuntimeError:
        raise _invalid_candidate("event_date is invalid") from None
    try:
        datetime.fromisoformat(_require_string_field(data, "created_at"))
    except ValueError:
        raise _invalid_candidate("created_at is invalid") from None

    privacy = _require_string_field(data, "privacy")
    confidence = _require_string_field(data, "confidence")
    suggested_source_type = _require_string_field(data, "suggested_source_type")
    status = _require_string_field(data, "status")
    if privacy not in PRIVACY_VALUES:
        raise _invalid_candidate("privacy is invalid")
    if confidence not in CONFIDENCE_VALUES:
        raise _invalid_candidate("confidence is invalid")
    if suggested_source_type not in PUBLISHABLE_SOURCE_TYPES:
        raise _invalid_candidate("suggested_source_type is invalid")
    if not isinstance(data.get("needs_confirmation"), bool):
        raise _invalid_candidate("needs_confirmation is invalid")
    if status not in RECORD_STATUS_VALUES:
        raise _invalid_candidate("status is invalid")
    if status == "published":
        if not allow_published:
            raise _invalid_candidate("status lifecycle is invalid")
        source_id = data.get("source_id")
        if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
            raise _invalid_candidate("source_id is invalid")
    elif "source_id" in data:
        raise _invalid_candidate("source_id is invalid before publish")
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    created_dirs = _missing_parent_dirs(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temp_path: Path | None = None
    succeeded = False
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        temp_path.replace(path)
        succeeded = True
    finally:
        if temp_path is not None and temp_path.exists():
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


def _snapshot_paths(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    paths = KnowledgeBasePaths(root)
    for relative in ("raw", "sources", "meta", "db"):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path != paths.write_lock:
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _restore_snapshot(root: Path, snapshot: dict[str, bytes]) -> None:
    paths = KnowledgeBasePaths(root)
    managed_roots = [root / name for name in ("raw", "sources", "meta", "db")]
    current_files: list[Path] = []
    for base in managed_roots:
        if base.exists():
            current_files.extend(
                path
                for path in base.rglob("*")
                if path.is_file() and path != paths.write_lock
            )
    for path in sorted(current_files, reverse=True):
        relative = path.relative_to(root).as_posix()
        if relative not in snapshot:
            path.unlink()
    for relative, data in snapshot.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for base in managed_roots:
        if not base.exists():
            continue
        for directory in sorted(
            (path for path in base.rglob("*") if path.is_dir()),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass


def capture(
    root: str | Path,
    *,
    type: str,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    value_reason: str,
    suggested_source_type: str,
) -> dict[str, Any]:
    """Capture a non-stable memory candidate under meta/memory-candidates."""
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    with acquire_write_lock(paths.root, operation="memory-candidate-capture"):
        return _capture_unlocked(
            paths,
            type=type,
            text=text,
            event_date=event_date,
            privacy=privacy,
            confidence=confidence,
            value_reason=value_reason,
            suggested_source_type=suggested_source_type,
        )


def _capture_unlocked(
    paths: KnowledgeBasePaths,
    *,
    type: str,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    value_reason: str,
    suggested_source_type: str,
) -> dict[str, Any]:
    clean = _validate_capture_inputs(
        type=type,
        text=text,
        event_date=event_date,
        privacy=privacy,
        confidence=confidence,
        value_reason=value_reason,
        suggested_source_type=suggested_source_type,
    )
    candidate_id = f"mem-{token_hex(8)}"
    path = _candidate_path(paths, candidate_id)
    while path.exists():
        candidate_id = f"mem-{token_hex(8)}"
        path = _candidate_path(paths, candidate_id)

    created_at = datetime.now().replace(microsecond=0).isoformat()
    candidate: dict[str, Any] = {
        "id": candidate_id,
        "type": clean["type"],
        "text": clean["text"],
        "original_text": clean["text"],
        "event_date": clean["event_date"],
        "privacy": clean["privacy"],
        "confidence": clean["confidence"],
        "needs_confirmation": True,
        "value_reason": clean["value_reason"],
        "suggested_source_type": clean["suggested_source_type"],
        "created_at": created_at,
        "status": "pending",
    }
    _write_json_atomic(path, candidate)
    return dict(candidate)


def review(root: str | Path, candidate_id: str, *, status: str) -> dict[str, Any]:
    """Approve or reject a memory candidate without creating stable facts."""
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    with acquire_write_lock(paths.root, operation="memory-candidate-review"):
        return _review_unlocked(paths, candidate_id, status=status)


def _review_unlocked(
    paths: KnowledgeBasePaths, candidate_id: str, *, status: str
) -> dict[str, Any]:
    status = str(status).strip().casefold()
    if status not in REVIEW_STATUS_VALUES:
        raise RuntimeError(f"Invalid review status: {status}")
    path = _candidate_path(paths, candidate_id)
    candidate = _validate_candidate_record(
        _read_candidate(path), candidate_id=_validate_candidate_id(candidate_id)
    )
    updated = dict(candidate)
    updated["status"] = status
    _write_json_atomic(path, updated)
    return updated


def publish(root: str | Path, candidate_id: str, *, confirm: bool) -> dict[str, Any]:
    """Publish an approved candidate through the existing self_statement source path."""
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized(paths)
    with acquire_write_lock(paths.root, operation="memory-candidate-publish"):
        return _publish_unlocked(paths, candidate_id, confirm=confirm)


def _publish_unlocked(
    paths: KnowledgeBasePaths, candidate_id: str, *, confirm: bool
) -> dict[str, Any]:
    path = _candidate_path(paths, candidate_id)
    candidate = _validate_candidate_record(
        _read_candidate(path),
        candidate_id=_validate_candidate_id(candidate_id),
        allow_published=True,
    )
    if candidate.get("status") != "approved":
        raise RuntimeError("memory candidate must be approved before publish")
    if confirm is not True:
        raise RuntimeError("publish requires explicit confirm")
    if candidate.get("suggested_source_type") != "self_statement":
        raise RuntimeError("memory candidate suggested_source_type is not publishable")

    snapshot = _snapshot_paths(paths.root)
    try:
        source = _create_self_statement_unlocked(
            paths.root,
            text=str(candidate["text"]),
            event_date=str(candidate["event_date"]),
            privacy=str(candidate["privacy"]),
            confidence=str(candidate["confidence"]),
            input_method="chat",
        )
        updated = dict(candidate)
        updated["status"] = "published"
        updated["source_id"] = source["source_id"]
        _write_json_atomic(path, updated)
        return updated
    except Exception:
        _restore_snapshot(paths.root, snapshot)
        raise


__all__ = ["capture", "review", "publish"]
