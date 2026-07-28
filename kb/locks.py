from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import KnowledgeBasePaths
from .product_result import ProductResult
from .redaction import redact_text
from .schema_check import ENGINE_VERSION


class WriteLockError(RuntimeError):
    def __init__(
        self,
        classification: str,
        summary: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(summary)
        self.classification = classification
        self.summary = summary
        self.details = dict(details or {})


@dataclass
class WriteLock:
    root: Path
    path: Path
    nonce: str
    payload: dict[str, object]
    file_identity: tuple[int, int, int, int]
    released: bool = False

    def __enter__(self) -> "WriteLock":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.released:
            self.release()

    def release(self, *, nonce: str | None = None) -> None:
        expected_nonce = self.nonce if nonce is None else nonce
        payload, read_error = _read_lock_payload(self.path)
        if read_error is not None:
            raise WriteLockError(
                read_error.classification,
                read_error.summary,
                details=read_error.details,
            )
        if payload is None:
            self.released = True
            return

        actual_nonce = payload.get("nonce")
        if actual_nonce != expected_nonce or expected_nonce != self.nonce:
            raise WriteLockError(
                "write_lock_nonce_mismatch",
                "Write lock nonce mismatch; lock was not released.",
                details={"lock_path": _relative_lock_label(self.root, self.path)},
            )
        if payload != self.payload or _file_identity(self.path) != self.file_identity:
            raise WriteLockError(
                "write_lock_identity_mismatch",
                "Write lock changed after acquisition; lock was not released.",
                details={"lock_path": _relative_lock_label(self.root, self.path)},
            )

        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.released = True


def _pass(classification: str, summary: str, **details: object) -> ProductResult:
    return ProductResult(
        status="pass",
        classification=classification,
        summary=summary,
        severity="info",
        details=dict(details),
    )


def _fail(classification: str, summary: str, **details: object) -> ProductResult:
    return ProductResult(
        status="failed",
        classification=classification,
        summary=summary,
        severity="blocking",
        details=dict(details),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp() -> str:
    return _now().isoformat()


def _relative_lock_label(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_within_root(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", 0)),
    )


def _ensure_lock_directory(paths: KnowledgeBasePaths) -> None:
    if paths.root.exists() and not paths.root.is_dir():
        raise WriteLockError(
            "path_invalid",
            "Knowledge base root is not a directory.",
            details={"root": redact_text(paths.root)},
        )
    if paths.meta.is_symlink() or (paths.meta.exists() and not paths.meta.is_dir()):
        raise WriteLockError(
            "path_invalid",
            "Lock metadata directory is not a safe directory.",
            details={"path": _relative_lock_label(paths.root, paths.meta)},
        )
    if not paths.meta.exists():
        paths.meta.mkdir(parents=True, exist_ok=True)
    if paths.write_lock.is_symlink():
        raise WriteLockError(
            "path_escape",
            "Write lock path is a symlink.",
            details={"path": _relative_lock_label(paths.root, paths.write_lock)},
        )
    if not _is_within_root(paths.root, paths.meta):
        raise WriteLockError(
            "path_escape",
            "Lock metadata directory escapes the knowledge base root.",
            details={"path": _relative_lock_label(paths.root, paths.meta)},
        )
    if not _is_within_root(paths.root, paths.write_lock):
        raise WriteLockError(
            "path_escape",
            "Write lock path escapes the knowledge base root.",
            details={"path": _relative_lock_label(paths.root, paths.write_lock)},
        )


def _lock_path_safety_result(paths: KnowledgeBasePaths) -> ProductResult | None:
    if paths.meta.is_symlink():
        return _fail(
            "lock_path_escape",
            "Lock metadata directory is a symlink.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            reason="symlink_not_allowed",
        )
    if paths.meta.exists():
        if not paths.meta.is_dir():
            return _fail(
                "lock_path_escape",
                "Lock metadata directory is not a safe directory.",
                lock_path=_relative_lock_label(paths.root, paths.write_lock),
            )
        if not _is_within_root(paths.root, paths.meta):
            return _fail(
                "lock_path_escape",
                "Lock metadata directory escapes the knowledge base root.",
                lock_path=_relative_lock_label(paths.root, paths.write_lock),
            )
    if paths.write_lock.is_symlink():
        return _fail(
            "lock_path_escape",
            "Write lock path is a symlink.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            reason="symlink_not_allowed",
        )
    if paths.write_lock.exists() and not _is_within_root(paths.root, paths.write_lock):
        return _fail(
            "lock_path_escape",
            "Write lock path escapes the knowledge base root.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
        )
    return None


def _safe_text(value: object) -> str:
    return redact_text(value)


def _process_name() -> str:
    argv0 = Path(sys.argv[0]).name if sys.argv else ""
    return _safe_text(argv0 or "python")


def _lock_payload(
    paths: KnowledgeBasePaths,
    *,
    operation: str,
    lease_seconds: int,
    nonce: str,
) -> dict[str, object]:
    timestamp = _timestamp()
    return {
        "pid": os.getpid(),
        "process_name": _process_name(),
        "started_at": timestamp,
        "operation": _safe_text(operation),
        "engine_version": _safe_text(ENGINE_VERSION),
        "nonce": nonce,
        "host": _safe_text(socket.gethostname()),
        "cwd": _safe_text(Path.cwd()),
        "heartbeat_at": timestamp,
        "lease_seconds": lease_seconds,
    }


def _read_lock_payload(
    lock_path: Path,
) -> tuple[dict[str, Any] | None, ProductResult | None]:
    data, read_error = _read_lock_bytes(lock_path)
    if read_error is not None:
        return None, read_error
    if data is None:
        return None, None
    return _parse_lock_payload_bytes(lock_path, data)


def _read_lock_bytes(lock_path: Path) -> tuple[bytes | None, ProductResult | None]:
    if lock_path.is_symlink():
        return None, _fail(
            "lock_path_escape",
            "Write lock path is a symlink.",
            lock_path=lock_path.name,
            reason="symlink_not_allowed",
        )
    if not lock_path.exists():
        return None, None
    try:
        return lock_path.read_bytes(), None
    except OSError as exc:
        return None, _fail(
            "uncertain_lock",
            "Cannot read write lock.",
            lock_path=lock_path.name,
            error=type(exc).__name__,
        )


def _parse_lock_payload_bytes(
    lock_path: Path, data: bytes
) -> tuple[dict[str, Any] | None, ProductResult | None]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, _fail(
            "uncertain_lock",
            "Cannot decode write lock.",
            lock_path=lock_path.name,
            error=type(exc).__name__,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _fail(
            "uncertain_lock",
            "Write lock is not valid JSON.",
            lock_path=lock_path.name,
            line=exc.lineno,
            column=exc.colno,
        )
    if not isinstance(payload, dict):
        return None, _fail(
            "uncertain_lock",
            "Write lock must contain a JSON object.",
            lock_path=lock_path.name,
        )
    return payload, None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lease_seconds(payload: dict[str, Any]) -> int:
    value = payload.get("lease_seconds")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    return 0


def _lock_payload_validation_failure(
    payload: dict[str, Any],
) -> tuple[str, dict[str, object]] | None:
    required = (
        "pid",
        "process_name",
        "started_at",
        "operation",
        "engine_version",
        "nonce",
        "host",
        "cwd",
        "heartbeat_at",
        "lease_seconds",
    )
    for field in required:
        if field not in payload:
            return "uncertain_lock", {"reason": "missing_field", "field": field}

    if isinstance(payload.get("pid"), bool) or not isinstance(payload.get("pid"), int):
        return "uncertain_lock", {"reason": "invalid_field", "field": "pid"}
    for field in ("process_name", "operation", "engine_version", "nonce", "host", "cwd"):
        if not isinstance(payload.get(field), str) or not payload.get(field):
            return "uncertain_lock", {"reason": "invalid_field", "field": field}
    if _parse_timestamp(payload.get("started_at")) is None:
        return "uncertain_lock", {"reason": "invalid_field", "field": "started_at"}
    if _parse_timestamp(payload.get("heartbeat_at")) is None:
        return "uncertain_lock", {"reason": "invalid_field", "field": "heartbeat_at"}
    if _lease_seconds(payload) <= 0:
        return "uncertain_lock", {"reason": "invalid_field", "field": "lease_seconds"}
    return None


def _classify_payload(payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
    validation_failure = _lock_payload_validation_failure(payload)
    if validation_failure is not None:
        return validation_failure

    heartbeat = _parse_timestamp(payload.get("heartbeat_at"))
    lease_seconds = _lease_seconds(payload)
    details: dict[str, object] = {
        "pid": payload.get("pid"),
        "operation": payload.get("operation", ""),
        "lease_seconds": lease_seconds,
    }
    age_seconds = max(0, int((_now() - heartbeat).total_seconds()))
    details["age_seconds"] = age_seconds
    if age_seconds > lease_seconds:
        return "stale_lock_candidate", details
    return "active_lock", details


def acquire_write_lock(
    root: str | Path, *, operation: str, lease_seconds: int = 900
) -> WriteLock:
    paths = KnowledgeBasePaths(Path(root))
    _ensure_lock_directory(paths)
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise WriteLockError(
            "invalid_lock_lease",
            "lease_seconds must be positive.",
            details={"lease_seconds": lease_seconds},
        )

    nonce = secrets.token_hex(16)
    payload = _lock_payload(
        paths,
        operation=operation,
        lease_seconds=lease_seconds,
        nonce=nonce,
    )
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with paths.write_lock.open("x", encoding="utf-8", newline="\n") as lock_file:
            lock_file.write(content)
    except FileExistsError:
        existing, read_error = _read_lock_payload(paths.write_lock)
        if read_error is not None:
            raise WriteLockError(
                read_error.classification,
                read_error.summary,
                details=read_error.details,
            ) from None
        assert existing is not None
        classification, details = _classify_payload(existing)
        if classification == "active_lock":
            raise WriteLockError(
                "write_lock_active",
                "A write lock is already active.",
                details=details,
            ) from None
        raise WriteLockError(
            classification,
            "A stale write lock candidate exists and was not removed.",
            details=details,
        ) from None

    return WriteLock(
        root=paths.root,
        path=paths.write_lock,
        nonce=nonce,
        payload=payload,
        file_identity=_file_identity(paths.write_lock),
    )


def lock_check(root: str | Path) -> ProductResult:
    paths = KnowledgeBasePaths(Path(root))
    safety_result = _lock_path_safety_result(paths)
    if safety_result is not None:
        return safety_result
    if not paths.write_lock.exists() and not paths.write_lock.is_symlink():
        return _pass("no_lock", "No root write lock is present.")

    payload, read_error = _read_lock_payload(paths.write_lock)
    if read_error is not None:
        return read_error
    assert payload is not None
    classification, details = _classify_payload(payload)
    if classification == "stale_lock_candidate":
        return _fail(
            "stale_lock_candidate",
            "Root write lock is stale candidate; recover-lock must handle it explicitly.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            **details,
        )
    if classification == "uncertain_lock":
        return _fail(
            "uncertain_lock",
            "Root write lock has invalid or incomplete lease metadata.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            **details,
        )
    return _fail(
        "active_lock",
        "Root write lock is active.",
        lock_path=_relative_lock_label(paths.root, paths.write_lock),
        **details,
    )


def recover_lock(root: str | Path, *, manual_confirm: bool = False) -> ProductResult:
    paths = KnowledgeBasePaths(Path(root))
    safety_result = _lock_path_safety_result(paths)
    if safety_result is not None:
        return safety_result
    if not paths.write_lock.exists() and not paths.write_lock.is_symlink():
        return _pass("no_lock", "No root write lock is present.")

    try:
        classified_identity = _file_identity(paths.write_lock)
    except OSError:
        return _pass("no_lock", "No root write lock is present.")
    classified_bytes, byte_error = _read_lock_bytes(paths.write_lock)
    try:
        identity_after_read = _file_identity(paths.write_lock)
    except OSError:
        return _pass("no_lock", "No root write lock is present.")

    if byte_error is None and classified_identity != identity_after_read:
        return _fail(
            "write_lock_identity_mismatch",
            "Write lock changed while recovery was classifying it; lock was not removed.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
        )
    if byte_error is not None:
        payload = None
        read_error = byte_error
    elif classified_bytes is None:
        return _pass("no_lock", "No root write lock is present.")
    else:
        payload, read_error = _parse_lock_payload_bytes(paths.write_lock, classified_bytes)

    if read_error is not None:
        previous_classification = read_error.classification
        details = dict(read_error.details)
    else:
        assert payload is not None
        previous_classification, details = _classify_payload(payload)

    if previous_classification == "active_lock":
        return _fail(
            "active_lock",
            "Active write lock was not removed.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            **details,
        )
    if previous_classification not in {"stale_lock_candidate", "uncertain_lock"}:
        return _fail(
            previous_classification,
            "Write lock was not removed because it is not recoverable.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            **details,
        )

    details["lock_path"] = _relative_lock_label(paths.root, paths.write_lock)
    details["previous_classification"] = previous_classification

    if not manual_confirm:
        return _fail(
            "manual_confirmation_required",
            "Write lock was not removed because manual confirmation was not provided.",
            **details,
        )

    try:
        current_identity = _file_identity(paths.write_lock)
    except OSError:
        return _pass("no_lock", "No root write lock is present.")
    current_bytes, current_error = _read_lock_bytes(paths.write_lock)
    if (
        current_error is not None
        or current_bytes != classified_bytes
        or current_identity != identity_after_read
    ):
        return _fail(
            "write_lock_identity_mismatch",
            "Write lock changed after classification; lock was not removed.",
            lock_path=_relative_lock_label(paths.root, paths.write_lock),
            previous_classification=previous_classification,
        )

    try:
        paths.write_lock.unlink()
    except FileNotFoundError:
        return _pass("no_lock", "No root write lock is present.")
    return _pass(
        "lock_recovered",
        "Write lock was removed after explicit manual confirmation.",
        **details,
    )


__all__ = [
    "WriteLockError",
    "acquire_write_lock",
    "lock_check",
    "recover_lock",
]
