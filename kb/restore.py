"""Safe restore for durable knowledge-base backup archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .backup import validate_backup_manifest
from .commands import _rebuild_index_unlocked, lint_repository, status_repository
from .governance import analyze_governance
from .locks import WriteLockError, acquire_write_lock
from .product_result import ProductResult
from .schema_check import RESERVED_WINDOWS_NAMES, SHA256_RE


MANIFEST_NAME = "backup-manifest.json"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SECRET_BYTES_RE = re.compile(
    rb"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    rb"Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
SECRET_FIELD_BYTES_RE = re.compile(
    rb"\b(api[_-]?key|password|passwd|secret|credential|authorization|"
    rb"access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*['\"]?"
    rb"[^'\"\s,;}]{4,}",
    re.IGNORECASE,
)


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


def _contains_secret_shape(data: bytes) -> bool:
    return bool(SECRET_BYTES_RE.search(data) or SECRET_FIELD_BYTES_RE.search(data))


def _unsafe_relative_path_reason(value: str) -> str | None:
    if not value or value in {".", ".."}:
        return "path_normalization_failure"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return "control_character"
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    if (
        value.startswith(("/", "\\"))
        or windows_path.is_absolute()
        or posix_path.is_absolute()
        or windows_path.drive
    ):
        return "absolute_or_drive_qualified_path"
    if ":" in value:
        return "alternate_data_stream"
    parts = posix_path.parts
    if any(part in {"", ".", ".."} for part in parts):
        return "path_traversal_candidate"
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if stem in RESERVED_WINDOWS_NAMES:
            return "reserved_windows_name"
    return None


def _zip_metadata_failure(info: zipfile.ZipInfo, archive: Path) -> ProductResult | None:
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == 0o120000:
        return _fail(
            "symlink_escape",
            "Backup archive entry is a symlink.",
            path=str(archive),
            candidate=info.filename,
        )
    if info.external_attr & FILE_ATTRIBUTE_REPARSE_POINT:
        return _fail(
            "junction_or_reparse_point",
            "Backup archive entry is a junction or reparse point.",
            path=str(archive),
            candidate=info.filename,
        )
    return None


def _read_manifest(archive: zipfile.ZipFile, archive_path: Path) -> tuple[dict[str, object] | None, ProductResult | None]:
    try:
        data = archive.read(MANIFEST_NAME)
    except KeyError:
        return None, _fail(
            "manifest_missing",
            "Backup archive does not contain backup-manifest.json.",
            path=str(archive_path),
        )
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _fail(
            "manifest_invalid",
            "Backup manifest is not valid UTF-8 JSON.",
            path=str(archive_path),
            error=type(exc).__name__,
        )
    if not isinstance(manifest, dict):
        return None, _fail(
            "manifest_invalid",
            "Backup manifest must contain a JSON object.",
            path=str(archive_path),
        )
    return manifest, None


def _validate_restore_archive(path: Path) -> tuple[list[tuple[str, bytes]], ProductResult | None]:
    manifest_result = validate_backup_manifest(path)
    if manifest_result.status != "pass":
        if manifest_result.classification == "manifest_missing":
            return [], _fail(
                "manifest_invalid",
                "Backup manifest is missing.",
                **manifest_result.details,
            )
        return [], manifest_result

    try:
        with zipfile.ZipFile(path) as archive:
            manifest, failure = _read_manifest(archive, path)
            if failure is not None:
                return [], failure
            assert manifest is not None
            files = manifest.get("files")
            if not isinstance(files, list):
                return [], _fail("manifest_invalid", "Backup manifest files must be a list.", path=str(path))

            infos = archive.infolist()
            seen: set[str] = set()
            info_by_name: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                normalized = info.filename.replace("\\", "/").casefold()
                if normalized in seen:
                    return [], _fail(
                        "duplicate_normalized_path",
                        "Backup archive contains duplicate normalized paths.",
                        path=str(path),
                        candidate=info.filename,
                    )
                seen.add(normalized)
                info_by_name[info.filename] = info
                metadata_failure = _zip_metadata_failure(info, path)
                if metadata_failure is not None:
                    return [], metadata_failure

            expected_names = {MANIFEST_NAME}
            payloads: list[tuple[str, bytes]] = []
            for index, item in enumerate(files):
                if not isinstance(item, dict):
                    return [], _fail("manifest_invalid", "Backup manifest file entry must be an object.", index=index)
                name = item.get("path")
                digest = item.get("sha256")
                if not isinstance(name, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    return [], _fail("manifest_invalid", "Backup manifest file entry is invalid.", index=index)
                unsafe = _unsafe_relative_path_reason(name)
                if unsafe:
                    return [], _fail(unsafe, "Backup manifest contains an unsafe file path.", candidate=name)
                if name not in info_by_name:
                    return [], _fail(
                        "manifest_mismatch",
                        "Backup manifest lists a file missing from the archive.",
                        candidate=name,
                    )
                expected_names.add(name)
                data = archive.read(name)
                if _contains_secret_shape(data) or _contains_secret_shape(name.encode("utf-8", errors="ignore")):
                    return [], _fail(
                        "secret_in_backup_candidate",
                        "Backup archive file contains a secret-shaped value.",
                        candidate=name,
                    )
                if hashlib.sha256(data).hexdigest() != digest:
                    return [], _fail(
                        "hash_mismatch",
                        "Backup archive file hash does not match the manifest.",
                        candidate=name,
                    )
                payloads.append((name, data))

            extras = sorted(set(info_by_name) - expected_names)
            if extras:
                return [], _fail(
                    "manifest_mismatch",
                    "Backup archive contains files not listed by the manifest.",
                    extra_files=extras,
                )
            return payloads, None
    except (OSError, zipfile.BadZipFile) as exc:
        return [], _fail(
            "archive_invalid",
            "Backup archive cannot be opened.",
            path=str(path),
            error=type(exc).__name__,
        )


def _unique_child(parent: Path, prefix: str, root: Path) -> Path:
    return parent / f"{prefix}{root.name}.{uuid.uuid4().hex}"


def _same_parent(left: Path, right: Path) -> bool:
    try:
        left_parent = left.parent.resolve(strict=False)
        right_parent = right.parent.resolve(strict=False)
    except OSError:
        return False
    return left_parent == right_parent and left_parent.drive.casefold() == right_parent.drive.casefold()


def _under_parent(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _remove_created_tree(path: Path, parent: Path) -> None:
    if not path.exists():
        return
    if not _under_parent(path, parent):
        raise RuntimeError(f"Refusing to remove path outside restore parent: {path}")
    shutil.rmtree(path)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore_pre_lock_target_shape(
    root: Path,
    parent: Path,
    *,
    root_existed: bool,
    meta_existed: bool,
) -> None:
    meta = root / "meta"
    if root_existed:
        if not meta_existed and meta.exists():
            _remove_created_tree(meta, root)
        return
    if root.exists():
        _remove_created_tree(root, parent)


def _target_entries_excluding_lock(root: Path) -> list[str]:
    if not root.exists():
        return []
    entries: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == "meta/.kb-write.lock":
            continue
        if path.is_dir() and relative == "meta" and not any(
            child.relative_to(root).as_posix() != "meta/.kb-write.lock"
            for child in path.rglob("*")
        ):
            continue
        entries.append(relative)
    return sorted(entries)


def _remove_root_content_except_lock(root: Path, lock_path: Path) -> None:
    if not root.exists():
        return
    for child in list(root.iterdir()):
        if child.name == "meta" and child.is_dir() and not child.is_symlink():
            for meta_child in list(child.iterdir()):
                if meta_child == lock_path:
                    continue
                _remove_path(meta_child)
            continue
        _remove_path(child)


def _move_existing_content_to_rollback(
    root: Path, rollback: Path, lock_path: Path
) -> bool:
    moved = False
    for child in list(root.iterdir()):
        if child.name == "meta" and child.is_dir() and not child.is_symlink():
            for meta_child in list(child.iterdir()):
                if meta_child == lock_path:
                    continue
                target = rollback / "meta" / meta_child.name
                target.parent.mkdir(parents=True, exist_ok=True)
                meta_child.replace(target)
                moved = True
            continue
        target = rollback / child.name
        target.parent.mkdir(parents=True, exist_ok=True)
        child.replace(target)
        moved = True
    return moved


def _move_staging_content_to_root(staging: Path, root: Path, lock_path: Path) -> None:
    for child in list(staging.iterdir()):
        if child.name == "meta" and child.is_dir() and not child.is_symlink():
            target_meta = root / "meta"
            target_meta.mkdir(exist_ok=True)
            for meta_child in list(child.iterdir()):
                target = target_meta / meta_child.name
                if target == lock_path:
                    raise OSError("restore staging attempted to replace active lock")
                meta_child.replace(target)
            child.rmdir()
            continue
        child.replace(root / child.name)
    staging.rmdir()


def _restore_rollback_content(root: Path, rollback: Path, lock_path: Path) -> None:
    _remove_root_content_except_lock(root, lock_path)
    if rollback.exists():
        _move_staging_content_to_root(rollback, root, lock_path)


def _write_restored_file(staging: Path, archive_name: str, data: bytes) -> None:
    target = staging / PurePosixPath(archive_name)
    try:
        target.resolve(strict=False).relative_to(staging.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError("restore path escaped staging directory") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _extract_to_staging(staging: Path, payloads: list[tuple[str, bytes]]) -> ProductResult | None:
    try:
        staging.mkdir()
        for archive_name, data in payloads:
            _write_restored_file(staging, archive_name, data)
    except OSError as exc:
        return _fail(
            "staging_write_failure",
            "Restore staging directory could not be written.",
            staging=str(staging),
            error=type(exc).__name__,
        )
    return None


def _post_restore_checks(root: Path) -> ProductResult | None:
    try:
        _rebuild_index_unlocked(root)
    except WriteLockError as exc:
        return _fail(exc.classification, exc.summary, **exc.details)
    except RuntimeError as exc:
        return _fail("rebuild_index_failed", "Restore index rebuild failed.", error=str(exc))

    try:
        lint_issues = lint_repository(root)
    except RuntimeError as exc:
        return _fail("lint_failed", "Restore lint check failed.", error=str(exc))
    if lint_issues:
        return _fail(
            "lint_failed",
            "Restore lint check reported issues.",
            issue_count=len(lint_issues),
            issues=lint_issues,
        )

    try:
        status = status_repository(root)
    except RuntimeError as exc:
        return _fail("status_failed", "Restore status check failed.", error=str(exc))

    try:
        governance = analyze_governance(root)
    except RuntimeError as exc:
        return _fail("governance_failed", "Restore governance analysis failed.", error=str(exc))
    blocking = governance.get("blocking", [])
    if blocking:
        return _fail(
            "blocking_governance_issue",
            "Blocking governance issues remain after restore.",
            blocking=blocking if isinstance(blocking, list) else [],
        )
    return None


def restore_backup(
    backup: str | Path, root: str | Path, *, replace: bool = False
) -> ProductResult:
    backup_path = Path(backup).expanduser().resolve(strict=False)
    root_path = Path(root).expanduser().resolve(strict=False)
    parent = root_path.parent

    payloads, validation_failure = _validate_restore_archive(backup_path)
    if validation_failure is not None:
        return validation_failure

    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail(
            "target_parent_unavailable",
            "Restore target parent directory cannot be prepared.",
            root=str(root_path),
            error=type(exc).__name__,
        )

    staging = _unique_child(parent, ".restore.", root_path)
    rollback = _unique_child(parent, ".rollback.", root_path)
    if not _same_parent(staging, root_path) or not _same_parent(rollback, root_path):
        return _fail(
            "cross_volume_atomicity_unsupported",
            "Restore staging and rollback paths must share the target parent.",
            root=str(root_path),
            staging=str(staging),
            rollback=str(rollback),
        )

    lock_path = root_path / "meta" / ".kb-write.lock"
    if root_path.exists() and not replace and not lock_path.exists() and not lock_path.is_symlink():
        durable_entries = _target_entries_excluding_lock(root_path)
        if durable_entries:
            return _fail(
                "target_not_empty",
                "Restore target is not empty; pass replace=True to replace it.",
                root=str(root_path),
                entries=durable_entries,
            )

    root_existed_before_lock = root_path.exists()
    meta_existed_before_lock = (root_path / "meta").exists()
    try:
        lock = acquire_write_lock(root_path, operation="restore")
    except WriteLockError as exc:
        classification = (
            "write_lock_active"
            if exc.classification in {"write_lock_active", "active_lock"}
            else exc.classification
        )
        return _fail(classification, exc.summary, **exc.details)

    result_after_lock: ProductResult | None = None
    restore_shape_after_lock = False
    with lock:
        durable_entries = _target_entries_excluding_lock(root_path)
        if durable_entries and not replace:
            return _fail(
                "target_not_empty",
                "Restore target is not empty; pass replace=True to replace it.",
                root=str(root_path),
                entries=durable_entries,
            )
        staging_failure = _extract_to_staging(staging, payloads)
        if staging_failure is not None:
            if staging.exists():
                _remove_created_tree(staging, parent)
            result_after_lock = staging_failure
            restore_shape_after_lock = True
        else:
            rollback_created = False
            rollback_prepared = False
            try:
                rollback_created = _move_existing_content_to_rollback(
                    root_path, rollback, lock_path
                )
                rollback_prepared = True
                _move_staging_content_to_root(staging, root_path, lock_path)
            except OSError as exc:
                if staging.exists():
                    _remove_created_tree(staging, parent)
                if not rollback_prepared and not rollback.exists():
                    pass
                elif rollback_created or rollback.exists():
                    _restore_rollback_content(root_path, rollback, lock_path)
                else:
                    _remove_root_content_except_lock(root_path, lock_path)
                result_after_lock = _fail(
                    "atomic_swap_failure",
                    "Restore staging directory could not be moved into place.",
                    root=str(root_path),
                    error=type(exc).__name__,
                )
                restore_shape_after_lock = True
            else:
                check_failure = _post_restore_checks(root_path)
                if check_failure is not None:
                    try:
                        if rollback_created or rollback.exists():
                            _restore_rollback_content(root_path, rollback, lock_path)
                        else:
                            _remove_root_content_except_lock(root_path, lock_path)
                    finally:
                        if staging.exists():
                            _remove_created_tree(staging, parent)
                    result_after_lock = check_failure
                    restore_shape_after_lock = True
                else:
                    if rollback_created and rollback.exists():
                        _remove_created_tree(rollback, parent)
                    if staging.exists():
                        _remove_created_tree(staging, parent)

                    result_after_lock = _pass(
                        "backup_restored",
                        "Backup archive restored.",
                        root=str(root_path),
                        backup=str(backup_path),
                        file_count=len(payloads),
                        replaced=bool(replace and root_existed_before_lock),
                    )

    if restore_shape_after_lock:
        _restore_pre_lock_target_shape(
            root_path,
            parent,
            root_existed=root_existed_before_lock,
            meta_existed=meta_existed_before_lock,
        )
    assert result_after_lock is not None
    return result_after_lock


__all__ = ["restore_backup"]
