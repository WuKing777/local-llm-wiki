"""Allowlisted durable backup creation and manifest validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .governance import analyze_governance
from .locks import WriteLockError, acquire_write_lock
from .paths import KnowledgeBasePaths
from .product_result import ProductResult
from .schema_check import ENGINE_VERSION, RESERVED_WINDOWS_NAMES, SHA256_RE


BACKUP_FORMAT_VERSION = 1
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
SECRET_ENV_NAME_RE = re.compile(
    r"(api[_-]?key|password|passwd|secret|credential|authorization|bearer|token)",
    re.IGNORECASE,
)
ROOT_FILES = {".gitignore", "README.md"}
META_DURABLE_FILES = {
    "meta/kb-manifest.json",
    "meta/index.md",
    "meta/source-map.jsonl",
    "meta/review-queue.md",
    "meta/quality-report.md",
    "meta/obsidian-home.md",
    "meta/profile-registry.json",
    "meta/doctor-report.json",
}
OBSIDIAN_DURABLE_FILES = {
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".obsidian/community-plugins.json",
    ".obsidian/core-plugins.json",
    ".obsidian/graph.json",
    ".obsidian/hotkeys.json",
    ".obsidian/templates.json",
}
TOOL_TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".txt",
}
RUNTIME_DIR_PREFIXES = (
    ".git/",
    ".pytest_cache/",
    "__pycache__/",
    "db/",
    "meta/audit/",
    "meta/cache/",
    "meta/logs/",
    "meta/ocr/",
    "meta/runtime/",
    "meta/tmp/",
    "meta/vector/",
    "meta/vectors/",
    "meta/model/",
    "meta/models/",
    ".obsidian/plugins/",
    ".obsidian/cache/",
)
RUNTIME_EXACT_PATHS = {
    "meta/.kb-write.lock",
    "meta/log.md",
    "meta/llm-audit.jsonl",
}
RUNTIME_SUFFIXES = (
    ".pyc",
    ".tmp",
    ".ocr.txt",
    ".sqlite",
    ".sqlite3",
    ".sqlite-journal",
    ".sqlite3-journal",
    ".sqlite-wal",
    ".sqlite3-wal",
    ".sqlite-shm",
    ".sqlite3-shm",
    ".db",
    ".db-journal",
    ".db-wal",
    ".db-shm",
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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _archive_name(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve()).as_posix()


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


def _path_safety_failure(root: Path, path: Path, archive_name: str) -> ProductResult | None:
    unsafe_reason = _unsafe_relative_path_reason(archive_name)
    if unsafe_reason:
        return _fail(
            unsafe_reason,
            "Backup candidate has an unsafe archive path.",
            path=archive_name,
        )
    try:
        resolved_root = root.resolve()
        resolved = path.resolve(strict=False)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return _fail(
            "canonical_escape",
            "Backup candidate canonical path escapes the knowledge base root.",
            path=archive_name,
        )
    if path.is_symlink():
        return _fail(
            "symlink_escape",
            "Backup candidate is a symlink.",
            path=archive_name,
        )
    try:
        stat = path.lstat()
    except OSError as exc:
        return _fail(
            "path_unreadable",
            "Backup candidate cannot be inspected.",
            path=archive_name,
            error=type(exc).__name__,
        )
    if int(getattr(stat, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT:
        return _fail(
            "junction_or_reparse_point",
            "Backup candidate is a junction or reparse point.",
            path=archive_name,
        )
    if path.is_file() and int(getattr(stat, "st_nlink", 1)) > 1:
        return _fail(
            "hardlink_unsafe",
            "Backup candidate has multiple hard links.",
            path=archive_name,
        )
    return None


def _is_runtime_only_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    basename = PurePosixPath(normalized).name
    if normalized in RUNTIME_EXACT_PATHS:
        return True
    if any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in RUNTIME_DIR_PREFIXES
    ):
        return True
    if basename.startswith(".backup"):
        return True
    if normalized.startswith(".obsidian/"):
        if basename.startswith("workspace") and basename.endswith(".json"):
            return True
    lowered = normalized.casefold()
    return any(lowered.endswith(suffix) for suffix in RUNTIME_SUFFIXES)


def _is_allowlisted_archive_name(name: str) -> bool:
    if _is_runtime_only_archive_name(name):
        return False
    if name in ROOT_FILES or name in META_DURABLE_FILES or name in OBSIDIAN_DURABLE_FILES:
        return True
    if name.startswith(("raw/", "sources/", "wiki/", "docs/reviews/")):
        return True
    if name.startswith("meta/templates/"):
        return True
    if name == "meta/evals/retrieval-benchmark.jsonl":
        return True
    if name.startswith("tools/"):
        return PurePosixPath(name).suffix.casefold() in TOOL_TEXT_SUFFIXES
    return False


def _collect_candidates(
    paths: KnowledgeBasePaths,
) -> tuple[list[Path], list[str], ProductResult | None]:
    candidates: list[Path] = []
    excluded: list[str] = []
    seen_normalized: dict[str, str] = {}

    def add_candidate(path: Path) -> ProductResult | None:
        try:
            archive_name = _archive_name(paths.root, path)
        except (OSError, RuntimeError, ValueError):
            return _fail(
                "canonical_escape",
                "Backup candidate canonical path escapes the knowledge base root.",
                path=str(path),
            )
        safety = _path_safety_failure(paths.root, path, archive_name)
        if safety is not None:
            return safety
        normalized = archive_name.replace("\\", "/").casefold()
        previous = seen_normalized.get(normalized)
        if previous is not None:
            if previous == archive_name:
                return None
            return _fail(
                "duplicate_normalized_path",
                "Backup candidates collide after normalized path comparison.",
                path=archive_name,
                previous=previous,
            )
        seen_normalized[normalized] = archive_name
        candidates.append(path)
        return None

    pending = [paths.root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.as_posix())
        except OSError as exc:
            return [], excluded, _fail(
                "candidate_unreadable",
                "Backup discovery cannot read a repository directory.",
                path=_archive_name(paths.root, directory)
                if directory != paths.root
                else ".",
                error=type(exc).__name__,
            )
        for path in children:
            try:
                archive_name = _archive_name(paths.root, path)
            except (OSError, RuntimeError, ValueError):
                return [], excluded, _fail(
                    "canonical_escape",
                    "Backup candidate canonical path escapes the knowledge base root.",
                    path=str(path),
                )
            failure = _path_safety_failure(paths.root, path, archive_name)
            if failure is not None:
                return [], excluded, failure
            if _contains_secret_text(archive_name):
                return [], excluded, _fail(
                    "secret_in_backup_candidate",
                    "Backup candidate path contains a secret-shaped value.",
                    path=archive_name,
                )
            if path.is_dir():
                if _is_runtime_only_archive_name(archive_name):
                    excluded.append(archive_name)
                    continue
                pending.append(path)
                continue
            if _is_runtime_only_archive_name(archive_name):
                excluded.append(archive_name)
                continue
            if not path.is_file():
                continue
            if not _is_allowlisted_archive_name(archive_name):
                return [], excluded, _fail(
                    "outside_allowlist",
                    "Backup candidate is outside the durable allowlist.",
                    path=archive_name,
                )
            failure = add_candidate(path)
            if failure is not None:
                return [], excluded, failure

    return (
        sorted(candidates, key=lambda item: _archive_name(paths.root, item)),
        sorted(set(excluded)),
        None,
    )


def _env_secret_bytes() -> list[bytes]:
    values: list[bytes] = []
    for name, value in os.environ.items():
        if not value:
            continue
        explicit_api_key = name in {"KB_LLM_API_KEY", "KB_EMBEDDING_API_KEY"}
        if explicit_api_key or (
            len(value) >= 8 and SECRET_ENV_NAME_RE.search(name)
        ):
            values.append(value.encode("utf-8", errors="ignore"))
    return sorted(set(values), key=len, reverse=True)


def _contains_secret_shape(data: bytes) -> bool:
    if SECRET_BYTES_RE.search(data) or SECRET_FIELD_BYTES_RE.search(data):
        return True
    return any(secret and secret in data for secret in _env_secret_bytes())


def _contains_secret_text(value: str) -> bool:
    return _contains_secret_shape(value.encode("utf-8", errors="ignore"))


def _safe_root_label(paths: KnowledgeBasePaths) -> str:
    label = paths.root.name
    if _contains_secret_text(label):
        return "local-root"
    return label


def _secret_manifest_metadata_path(value: object, path: str = "$") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            found = _secret_manifest_metadata_path(item, f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _secret_manifest_metadata_path(item, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str) and _contains_secret_text(value):
        return path
    return None


def _archive_name_policy_failure(archive_name: str, path: str | Path) -> ProductResult | None:
    unsafe_reason = _unsafe_relative_path_reason(archive_name)
    if unsafe_reason:
        return _fail(
            unsafe_reason,
            "Backup archive path is unsafe.",
            path=str(path),
            candidate=archive_name,
        )
    if _contains_secret_text(archive_name):
        return _fail(
            "secret_in_backup_candidate",
            "Backup archive path contains a secret-shaped value.",
            path=str(path),
            candidate=archive_name,
        )
    if _is_runtime_only_archive_name(archive_name) or not _is_allowlisted_archive_name(
        archive_name
    ):
        return _fail(
            "outside_allowlist",
            "Backup archive path is outside the durable allowlist.",
            path=str(path),
            candidate=archive_name,
        )
    return None


def _file_record(root: Path, path: Path) -> tuple[dict[str, object], bytes, ProductResult | None]:
    archive_name = _archive_name(root, path)
    policy_failure = _archive_name_policy_failure(archive_name, archive_name)
    if policy_failure is not None:
        return {}, b"", policy_failure
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {}, b"", _fail(
            "candidate_unreadable",
            "Backup candidate cannot be read.",
            path=archive_name,
            error=type(exc).__name__,
        )
    if _contains_secret_shape(data):
        return {}, b"", _fail(
            "secret_in_backup_candidate",
            "Backup candidate contains a secret-shaped value.",
            path=archive_name,
        )
    return {
        "path": archive_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }, data, None


def _git_dirty_entries(paths: KnowledgeBasePaths) -> tuple[dict[str, object], ProductResult | None]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=paths.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, RuntimeError) as exc:
        return {
            "available": False,
            "dirty": False,
            "dirty_entries": [],
            "error": type(exc).__name__,
        }, None
    if completed.returncode != 0:
        return {
            "available": False,
            "dirty": False,
            "dirty_entries": [],
            "runtime_filtered_entries": [],
            "error": completed.stderr.strip()[:200],
        }, None

    dirty_entries: list[str] = []
    runtime_filtered_entries: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        candidate = line[3:]
        if " -> " in candidate:
            candidate = candidate.rsplit(" -> ", 1)[1]
        candidate = candidate.strip().strip('"').replace("\\", "/")
        if not candidate:
            continue
        if _is_runtime_only_archive_name(candidate):
            runtime_filtered_entries.append(candidate)
            continue
        dirty_entries.append(candidate)
    dirty_entries = sorted(set(dirty_entries))
    return {
        "available": True,
        "dirty": bool(dirty_entries),
        "dirty_entries": dirty_entries,
        "runtime_filtered_entries": sorted(set(runtime_filtered_entries)),
    }, None


def _manifest(
    paths: KnowledgeBasePaths,
    records: list[dict[str, object]],
    git: dict[str, object],
    excluded: list[str],
) -> dict[str, object]:
    return {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": _now(),
        "engine_version": ENGINE_VERSION,
        "root_label": _safe_root_label(paths),
        "files": records,
        "counts": {"files": len(records)},
        "excluded": excluded,
        "git": git,
        "redaction_rule_version": "1",
        "path_safety_rule_version": "1",
        "secret_scan_rule_version": "1",
    }


def _write_zip_atomic(
    output: Path,
    manifest: dict[str, object],
    file_payloads: list[tuple[str, bytes]],
) -> ProductResult | None:
    output = output.expanduser().resolve()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return _fail(
            "permission_denied",
            "Backup output directory cannot be created.",
            output=str(output),
            error=type(exc).__name__,
        )
    except OSError as exc:
        return _fail(
            "temporary_directory_failure",
            "Backup output directory cannot be prepared.",
            output=str(output),
            error=type(exc).__name__,
        )
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".backup.{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(fd)
    except PermissionError as exc:
        return _fail(
            "permission_denied",
            "Backup temporary archive cannot be created.",
            output=str(output),
            error=type(exc).__name__,
        )
    except OSError as exc:
        return _fail(
            "temporary_directory_failure",
            "Backup temporary archive cannot be created.",
            output=str(output),
            error=type(exc).__name__,
        )
    temp_path = Path(temp_name)
    stage = "write"
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for archive_name, data in file_payloads:
                archive.writestr(archive_name, data)
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            archive.writestr(MANIFEST_NAME, manifest_bytes)
        stage = "replace"
        os.replace(temp_path, output)
    except PermissionError as exc:
        return _fail(
            "permission_denied",
            "Backup archive could not be written.",
            output=str(output),
            error=type(exc).__name__,
        )
    except OSError as exc:
        return _fail(
            "atomic_rename_failure" if stage == "replace" else "manifest_write_failure",
            "Backup archive could not be written.",
            output=str(output),
            error=type(exc).__name__,
        )
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return None


def _backup_output_policy_failure(root: Path, output: Path) -> ProductResult | None:
    try:
        resolved_root = root.resolve()
        resolved_output = output.expanduser().resolve(strict=False)
        relative_output = resolved_output.relative_to(resolved_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None

    return _fail(
        "unsafe_backup_output_path",
        "Backup output path must be outside the knowledge-base root.",
        output=str(resolved_output),
        root=str(resolved_root),
        path=relative_output,
    )


def create_backup(
    root: str | Path, output: str | Path, allow_dirty: bool = False
) -> ProductResult:
    paths = KnowledgeBasePaths(Path(root))
    output_path = Path(output)
    if not paths.root.exists():
        return _fail("root_missing", "Knowledge base root is missing.", root=str(paths.root))
    if not paths.root.is_dir():
        return _fail("root_invalid", "Knowledge base root is not a directory.", root=str(paths.root))
    failure = _backup_output_policy_failure(paths.root, output_path)
    if failure is not None:
        return failure

    try:
        lock = acquire_write_lock(paths.root, operation="backup")
    except WriteLockError as exc:
        classification = (
            "write_lock_active"
            if exc.classification in {"write_lock_active", "active_lock"}
            else exc.classification
        )
        return _fail(classification, exc.summary, **exc.details)

    with lock:
        try:
            governance = analyze_governance(paths.root)
        except RuntimeError as exc:
            return _fail(
                "blocking_governance_issue",
                "Read-only governance analysis failed before backup packaging.",
                error=str(exc),
            )
        blocking = governance.get("blocking", [])
        if blocking:
            return _fail(
                "blocking_governance_issue",
                "Blocking governance issues prevent backup packaging.",
                blocking_count=len(blocking) if isinstance(blocking, list) else 0,
                blocking=blocking if isinstance(blocking, list) else [],
            )

        candidates, excluded, failure = _collect_candidates(paths)
        if failure is not None:
            return failure

        records: list[dict[str, object]] = []
        payloads: list[tuple[str, bytes]] = []
        for candidate in candidates:
            record, data, failure = _file_record(paths.root, candidate)
            if failure is not None:
                return failure
            records.append(record)
            payloads.append((record["path"], data))

        git, failure = _git_dirty_entries(paths)
        if failure is not None:
            return failure
        if git.get("dirty") and not allow_dirty:
            return _fail(
                "dirty_worktree_unconfirmed",
                "Dirty durable worktree entries require explicit allow_dirty confirmation.",
                dirty_entries=git.get("dirty_entries", []),
            )

        manifest = _manifest(paths, records, git, excluded)
        failure = _write_zip_atomic(output_path, manifest, payloads)
        if failure is not None:
            return failure

    return _pass(
        "backup_created",
        "Backup archive created.",
        output=str(output_path.expanduser().resolve()),
        file_count=len(records),
        dirty=bool(manifest["git"]["dirty"]),
    )


def _read_manifest_from_zip(path: Path) -> tuple[dict[str, object] | None, ProductResult | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            failure = _duplicate_archive_member_failure(archive.namelist(), path)
            if failure is not None:
                return None, failure
            try:
                data = archive.read(MANIFEST_NAME)
            except KeyError:
                return None, _fail(
                    "manifest_missing",
                    "Backup archive does not contain backup-manifest.json.",
                    path=str(path),
                )
    except (OSError, zipfile.BadZipFile) as exc:
        return None, _fail(
            "archive_invalid",
            "Backup archive cannot be opened.",
            path=str(path),
            error=type(exc).__name__,
        )
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _fail(
            "manifest_invalid",
            "Backup manifest is not valid UTF-8 JSON.",
            path=str(path),
            error=type(exc).__name__,
        )
    if not isinstance(manifest, dict):
        return None, _fail(
            "manifest_invalid",
            "Backup manifest must contain a JSON object.",
            path=str(path),
        )
    return manifest, None


def _duplicate_archive_member_failure(names: list[str], path: Path) -> ProductResult | None:
    seen: set[str] = set()
    for name in names:
        normalized = name.replace("\\", "/").casefold()
        if normalized in seen:
            return _fail(
                "duplicate_normalized_path",
                "Backup archive contains duplicate normalized paths.",
                path=str(path),
                candidate=name,
            )
        seen.add(normalized)
    return None


def _validate_manifest_shape(manifest: dict[str, object], path: Path) -> ProductResult | None:
    secret_path = _secret_manifest_metadata_path(manifest)
    if secret_path:
        return _fail(
            "secret_in_backup_candidate",
            "Backup manifest contains secret-shaped metadata.",
            path=str(path),
            field=secret_path,
        )
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        return _fail(
            "manifest_invalid",
            "Backup manifest format_version is invalid.",
            path=str(path),
        )
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        return _fail(
            "manifest_invalid",
            "Backup manifest created_at is required.",
            path=str(path),
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        return _fail(
            "manifest_invalid",
            "Backup manifest files must be a list.",
            path=str(path),
        )
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return _fail(
                "manifest_invalid",
                "Backup manifest file entry must be an object.",
                path=str(path),
                index=index,
            )
        archive_name = item.get("path")
        digest = item.get("sha256")
        if not isinstance(archive_name, str):
            return _fail(
                "manifest_invalid",
                "Backup manifest file path is required.",
                path=str(path),
                index=index,
            )
        unsafe_reason = _unsafe_relative_path_reason(archive_name)
        if unsafe_reason:
            return _fail(
                unsafe_reason,
                "Backup manifest contains an unsafe file path.",
                path=str(path),
                candidate=archive_name,
            )
        if _contains_secret_text(archive_name):
            return _fail(
                "secret_in_backup_candidate",
                "Backup manifest contains a secret-shaped file path.",
                path=str(path),
                candidate=archive_name,
            )
        if _is_runtime_only_archive_name(archive_name) or not _is_allowlisted_archive_name(
            archive_name
        ):
            return _fail(
                "outside_allowlist",
                "Backup manifest contains a file outside the durable allowlist.",
                path=str(path),
                candidate=archive_name,
            )
        normalized = archive_name.replace("\\", "/").casefold()
        if normalized in seen:
            return _fail(
                "duplicate_normalized_path",
                "Backup manifest contains duplicate normalized paths.",
                path=str(path),
                candidate=archive_name,
            )
        seen.add(normalized)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            return _fail(
                "manifest_invalid",
                "Backup manifest file sha256 is invalid.",
                path=str(path),
                index=index,
            )
    return None


def validate_backup_manifest(path: str | Path) -> ProductResult:
    archive_path = Path(path)
    manifest, failure = _read_manifest_from_zip(archive_path)
    if failure is not None:
        return failure
    assert manifest is not None
    failure = _validate_manifest_shape(manifest, archive_path)
    if failure is not None:
        return failure

    with zipfile.ZipFile(archive_path) as archive:
        archive_name_list = archive.namelist()
        failure = _duplicate_archive_member_failure(archive_name_list, archive_path)
        if failure is not None:
            return failure
        archive_names = set(archive_name_list)
        expected_names = {MANIFEST_NAME}
        for archive_name in sorted(archive_names - {MANIFEST_NAME}):
            failure = _archive_name_policy_failure(archive_name, archive_path)
            if failure is not None:
                return failure
        files = manifest["files"]
        assert isinstance(files, list)
        for item in files:
            assert isinstance(item, dict)
            archive_name = item["path"]
            digest = item["sha256"]
            assert isinstance(archive_name, str)
            assert isinstance(digest, str)
            expected_names.add(archive_name)
            if archive_name not in archive_names:
                return _fail(
                    "manifest_mismatch",
                    "Backup manifest lists a file missing from the archive.",
                    path=str(archive_path),
                    candidate=archive_name,
                )
            data = archive.read(archive_name)
            if _contains_secret_shape(data):
                return _fail(
                    "secret_in_backup_candidate",
                    "Backup archive file contains a secret-shaped value.",
                    path=str(archive_path),
                    candidate=archive_name,
                )
            actual = hashlib.sha256(data).hexdigest()
            if actual != digest:
                return _fail(
                    "hash_mismatch",
                    "Backup archive file hash does not match the manifest.",
                    path=str(archive_path),
                    candidate=archive_name,
                )
        extras = sorted(archive_names - expected_names)
        if extras:
            return _fail(
                "manifest_mismatch",
                "Backup archive contains files not listed by the manifest.",
                path=str(archive_path),
                extra_files=extras,
            )

    return _pass(
        "backup_manifest_valid",
        "Backup manifest matches archive contents.",
        path=str(archive_path),
        file_count=len(manifest["files"]),
    )


__all__ = ["create_backup", "validate_backup_manifest"]
