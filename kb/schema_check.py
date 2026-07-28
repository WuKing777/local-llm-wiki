from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .paths import KnowledgeBasePaths
from .product_result import ProductResult
from .sources import SOURCE_CARD_FIELDS, read_source_card
from .wiki import DRAFT_FIELDS


CURRENT_SCHEMA_VERSION = 1
MIN_SUPPORTED_SCHEMA_VERSION = 1
ENGINE_NAME = "local-llm-wiki"
ENGINE_VERSION = "0.1.0"
PROFILE_KIND = "personal_exobrain"
REVIEW_STATUS_VALUES = ["reviewed", "verified", "pass", "needs_reingest", "rejected"]
SCHEMA_CONTRACTS = {
    "kb_manifest": "kb/schemas/kb-manifest.schema.json",
    "source_card": "kb/schemas/source-card.schema.json",
    "wiki_front_matter": "kb/schemas/wiki-front-matter.schema.json",
    "backup_manifest": "kb/schemas/backup-manifest.schema.json",
    "profile_registry": "kb/schemas/profile-registry.schema.json",
    "doctor_report": "kb/schemas/doctor-report.schema.json",
    "retrieval_benchmark": "kb/schemas/retrieval-benchmark.schema.json",
}
WRITE_LOCK_INTEGRATION = {
    "required_task": "Productization Hardening Task 4",
    "status": "required_not_integrated",
    "schema_check_write_scope": "meta/kb-manifest.json",
    "enforced": False,
}

REQUIRED_SOURCE_FIELDS = list(SOURCE_CARD_FIELDS)
PRIVACY_VALUES = {"public", "personal", "sensitive", "restricted"}
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
SOURCE_ID_RE = re.compile(r"^src-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_FIELD_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|credential|authorization|bearer|private[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|password|passwd|secret|credential|token)\s*[:=]\s*\S{4,})",
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


def _relative_label(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_within_root(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _path_boundary_failure(
    paths: KnowledgeBasePaths, path: Path, artifact: str
) -> ProductResult | None:
    if not _is_within_root(paths.root, path):
        return _fail(
            "path_escape",
            f"{artifact} path escapes the knowledge base root.",
            artifact=artifact,
            path=_relative_label(paths.root, path),
        )
    if path.is_symlink():
        return _fail(
            "path_escape",
            f"{artifact} path is a symlink.",
            artifact=artifact,
            path=_relative_label(paths.root, path),
            reason="symlink_not_allowed",
        )
    return None


def _require_path_within_root(root: Path, path: Path, artifact: str) -> None:
    if not _is_within_root(root, path) or path.is_symlink():
        raise RuntimeError(f"{artifact} path escapes knowledge base root: {path}")


def _manifest_path(root: str | Path) -> Path:
    return KnowledgeBasePaths(Path(root)).meta / "kb-manifest.json"


def default_manifest(root: str | Path) -> dict[str, object]:
    KnowledgeBasePaths(Path(root))
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "created_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "engine_name": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "profile_kind": PROFILE_KIND,
        "required_source_fields": list(REQUIRED_SOURCE_FIELDS),
        "review_status_values": list(REVIEW_STATUS_VALUES),
        "contracts": dict(SCHEMA_CONTRACTS),
        "write_lock_integration": dict(WRITE_LOCK_INTEGRATION),
    }


def write_manifest_if_missing(root: str | Path) -> Path:
    paths = KnowledgeBasePaths(Path(root))
    manifest = paths.meta / "kb-manifest.json"
    _require_path_within_root(paths.root, paths.meta, "metadata directory")
    _require_path_within_root(paths.root, manifest, "manifest")
    if manifest.exists():
        if not manifest.is_file():
            raise RuntimeError(f"Expected manifest file: {manifest}")
        return manifest
    if not paths.meta.exists() or not paths.meta.is_dir():
        raise RuntimeError(f"Expected metadata directory: {paths.meta}")

    content = (
        json.dumps(default_manifest(paths.root), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    try:
        with manifest.open("x", encoding="utf-8", newline="\n") as manifest_file:
            manifest_file.write(content)
    except FileExistsError:
        pass
    return manifest


def _read_json_object(path: Path, invalid_classification: str) -> tuple[dict[str, Any] | None, ProductResult | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, _fail(
            invalid_classification,
            f"Cannot read JSON file: {path.name}",
            path=str(path),
            error=type(exc).__name__,
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _fail(
            invalid_classification,
            f"Invalid JSON in {path.name}.",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        )
    if not isinstance(data, dict):
        return None, _fail(
            invalid_classification,
            f"{path.name} must contain a JSON object.",
            path=str(path),
        )
    return data, None


def _secret_shape(value: object, path: str = "$") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if SECRET_FIELD_RE.search(key_text):
                return child_path
            found = _secret_shape(item, child_path)
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _secret_shape(item, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        return path
    return None


def _version_failure(
    data: dict[str, Any],
    *,
    field: str,
    invalid_classification: str,
) -> ProductResult | None:
    version = data.get(field)
    if not isinstance(version, int) or isinstance(version, bool):
        return _fail(
            invalid_classification,
            f"{field} must be an integer.",
            field=field,
            value_type=type(version).__name__,
        )
    if version > CURRENT_SCHEMA_VERSION:
        return _fail(
            "schema_upgrade_required",
            f"{field} {version} is newer than this product supports.",
            field=field,
            found=version,
            supported=CURRENT_SCHEMA_VERSION,
        )
    if version < MIN_SUPPORTED_SCHEMA_VERSION:
        return _fail(
            "schema_unsupported",
            f"{field} {version} is no longer supported.",
            field=field,
            found=version,
            supported_min=MIN_SUPPORTED_SCHEMA_VERSION,
        )
    return None


def _unsafe_relative_path_reason(value: str) -> str | None:
    if not value or value in {".", ".."}:
        return "path_normalization_failure"
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
        stem = part.split(".", 1)[0].upper()
        if stem in RESERVED_WINDOWS_NAMES:
            return "reserved_windows_name"
    return None


def _validate_manifest(data: dict[str, Any], manifest: Path) -> ProductResult | None:
    secret_path = _secret_shape(data)
    if secret_path:
        return _fail(
            "secret_in_manifest",
            "Root manifest contains secret-shaped metadata.",
            path=str(manifest),
            field=secret_path,
        )

    version_result = _version_failure(
        data, field="schema_version", invalid_classification="manifest_invalid"
    )
    if version_result:
        return version_result

    if not isinstance(data.get("created_at"), str) or not data["created_at"]:
        return _fail(
            "manifest_invalid",
            "Root manifest created_at is required.",
            path=str(manifest),
            field="created_at",
        )

    required_fields = data.get("required_source_fields")
    if required_fields != REQUIRED_SOURCE_FIELDS:
        return _fail(
            "manifest_invalid",
            "Root manifest required_source_fields does not match the current source-card contract.",
            path=str(manifest),
            field="required_source_fields",
            expected=REQUIRED_SOURCE_FIELDS,
            actual=required_fields,
        )
    expected_values = {
        "engine_name": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "profile_kind": PROFILE_KIND,
        "review_status_values": REVIEW_STATUS_VALUES,
        "contracts": SCHEMA_CONTRACTS,
        "write_lock_integration": WRITE_LOCK_INTEGRATION,
    }
    for field, expected in expected_values.items():
        if data.get(field) != expected:
            return _fail(
                "manifest_invalid",
                f"Root manifest {field} does not match the current schema contract.",
                path=str(manifest),
                field=field,
                expected=expected,
                actual=data.get(field),
            )
    return None


def _source_card_failure(
    paths: KnowledgeBasePaths, card: Path, message: str
) -> ProductResult:
    details: dict[str, object] = {
        "path": _relative_label(paths.root, card),
        "error": message,
    }
    missing_prefix = "Missing source card field: "
    if message.startswith(missing_prefix):
        details["missing_field"] = message.removeprefix(missing_prefix)
    return _fail("source_card_invalid", "Source card contract validation failed.", **details)


def _validate_source_cards(paths: KnowledgeBasePaths) -> ProductResult | None:
    if not paths.sources.exists() or not paths.sources.is_dir():
        return _fail(
            "repository_uninitialized",
            "Knowledge base sources directory is missing.",
            path=_relative_label(paths.root, paths.sources),
        )
    boundary_failure = _path_boundary_failure(paths, paths.sources, "sources")
    if boundary_failure:
        return boundary_failure

    for card in sorted(paths.sources.glob("src-*.md")):
        boundary_failure = _path_boundary_failure(paths, card, "source_card")
        if boundary_failure:
            return boundary_failure
        try:
            metadata = read_source_card(card)
        except RuntimeError as exc:
            return _source_card_failure(paths, card, str(exc))
        secret_path = _secret_shape(metadata)
        if secret_path:
            return _fail(
                "secret_in_source_card",
                "Source card contains secret-shaped metadata.",
                path=_relative_label(paths.root, card),
                field=secret_path,
            )
        for field in REQUIRED_SOURCE_FIELDS:
            if not metadata.get(field):
                return _source_card_failure(
                    paths, card, f"Missing source card field: {field}"
                )
        if not SOURCE_ID_RE.fullmatch(metadata["source_id"]):
            return _fail(
                "source_card_invalid",
                "Source card source_id is invalid.",
                path=_relative_label(paths.root, card),
                field="source_id",
            )
        if not SHA256_RE.fullmatch(metadata["sha256"]):
            return _fail(
                "source_card_invalid",
                "Source card sha256 is invalid.",
                path=_relative_label(paths.root, card),
                field="sha256",
            )
        raw_path_reason = _unsafe_relative_path_reason(metadata["raw_path"])
        if raw_path_reason:
            return _fail(
                "source_card_invalid",
                "Source card raw_path is unsafe.",
                path=_relative_label(paths.root, card),
                field="raw_path",
                reason=raw_path_reason,
            )
    return None


def _parse_front_matter(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"Cannot read wiki file: {type(exc).__name__}"
    if not lines or lines[0] != "---":
        return None, None

    metadata: dict[str, object] = {}
    for line in lines[1:]:
        if line == "---":
            return metadata, None
        key, separator, value = line.partition(":")
        if not separator:
            return None, "Invalid front matter field"
        raw_value = value.strip()
        try:
            metadata[key.strip()] = json.loads(raw_value)
        except json.JSONDecodeError:
            metadata[key.strip()] = raw_value
    return None, "Missing closing front matter"


def _validate_wiki_front_matter(paths: KnowledgeBasePaths) -> ProductResult | None:
    if not paths.wiki.exists() or not paths.wiki.is_dir():
        return _fail(
            "repository_uninitialized",
            "Knowledge base wiki directory is missing.",
            path=_relative_label(paths.root, paths.wiki),
        )
    boundary_failure = _path_boundary_failure(paths, paths.wiki, "wiki")
    if boundary_failure:
        return boundary_failure
    if paths.drafts.exists():
        boundary_failure = _path_boundary_failure(paths, paths.drafts, "drafts")
        if boundary_failure:
            return boundary_failure

    drafts_root = (paths.wiki / "_drafts").resolve()
    for page in sorted(paths.wiki.rglob("*.md")):
        boundary_failure = _path_boundary_failure(paths, page, "wiki_page")
        if boundary_failure:
            return boundary_failure
        metadata, error = _parse_front_matter(page)
        if error:
            return _fail(
                "wiki_front_matter_invalid",
                "Wiki front matter contract validation failed.",
                path=_relative_label(paths.root, page),
                error=error,
            )
        if metadata is None:
            continue
        secret_path = _secret_shape(metadata)
        if secret_path:
            return _fail(
                "wiki_front_matter_invalid",
                "Wiki front matter contains secret-shaped metadata.",
                path=_relative_label(paths.root, page),
                field=secret_path,
            )
        try:
            page.resolve().relative_to(drafts_root)
        except ValueError:
            continue
        for field in DRAFT_FIELDS:
            if metadata.get(field) in ("", None, [], {}):
                return _fail(
                    "wiki_front_matter_invalid",
                    "Draft front matter is missing a required field.",
                    path=_relative_label(paths.root, page),
                    missing_field=field,
                )
    return None


def _validate_doctor_report(path: Path) -> ProductResult:
    if not path.exists():
        return _fail("doctor_report_missing", "Doctor report is missing.", path=str(path))
    if not path.is_file():
        return _fail("doctor_report_invalid", "Doctor report path is not a file.", path=str(path))
    data, error = _read_json_object(path, "doctor_report_invalid")
    if error:
        return error
    assert data is not None
    secret_path = _secret_shape(data)
    if secret_path:
        return _fail(
            "doctor_report_invalid",
            "Doctor report contains secret-shaped metadata.",
            path=str(path),
            field=secret_path,
        )
    if data.get("status") not in {"pass", "warning", "failed"}:
        return _fail(
            "doctor_report_invalid",
            "Doctor report status is invalid.",
            path=str(path),
            field="status",
        )
    checks = data.get("checks")
    if not isinstance(checks, list):
        return _fail(
            "doctor_report_invalid",
            "Doctor report checks must be a list.",
            path=str(path),
            field="checks",
        )
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            return _fail(
                "doctor_report_invalid",
                "Doctor report check must be an object.",
                path=str(path),
                index=index,
            )
        for field in ("id", "status", "severity", "summary"):
            if not isinstance(check.get(field), str) or not check[field]:
                return _fail(
                    "doctor_report_invalid",
                    "Doctor report check is missing a required field.",
                    path=str(path),
                    index=index,
                    field=field,
                )
    return _pass("doctor_report_ok", "Doctor report contract is valid.", path=str(path))


def _validate_optional_artifacts(paths: KnowledgeBasePaths) -> ProductResult | None:
    profile_registry = paths.meta / "profile-registry.json"
    if profile_registry.exists():
        boundary_failure = _path_boundary_failure(
            paths, profile_registry, "profile_registry"
        )
        if boundary_failure:
            return boundary_failure
        result = check_profile_registry(profile_registry)
        if result.status != "pass":
            return result

    benchmark = paths.meta / "evals" / "retrieval-benchmark.jsonl"
    if benchmark.exists():
        boundary_failure = _path_boundary_failure(paths, benchmark, "retrieval_benchmark")
        if boundary_failure:
            return boundary_failure
        result = check_retrieval_benchmark(benchmark)
        if result.status != "pass":
            return result

    for backup_manifest in (
        paths.root / "backup-manifest.json",
        paths.meta / "backup-manifest.json",
    ):
        if backup_manifest.exists():
            boundary_failure = _path_boundary_failure(
                paths, backup_manifest, "backup_manifest"
            )
            if boundary_failure:
                return boundary_failure
            result = check_backup_manifest(backup_manifest)
            if result.status != "pass":
                return result

    doctor_report = paths.meta / "doctor-report.json"
    if doctor_report.exists():
        boundary_failure = _path_boundary_failure(paths, doctor_report, "doctor_report")
        if boundary_failure:
            return boundary_failure
        result = _validate_doctor_report(doctor_report)
        if result.status != "pass":
            return result
    return None


def schema_check(root: str | Path, *, write_manifest: bool = False) -> ProductResult:
    paths = KnowledgeBasePaths(Path(root))
    if not paths.root.exists():
        return _fail("root_missing", "Knowledge base root is missing.", root=str(paths.root))
    if not paths.root.is_dir():
        return _fail("root_invalid", "Knowledge base root is not a directory.", root=str(paths.root))

    manifest = _manifest_path(paths.root)
    manifest_written = False
    for artifact, path in (("meta", paths.meta), ("manifest", manifest)):
        boundary_failure = _path_boundary_failure(paths, path, artifact)
        if boundary_failure:
            return boundary_failure
    if not manifest.exists():
        if not write_manifest:
            return _fail(
                "manifest_missing",
                "Root manifest is missing and schema-check is no-write by default.",
                path=_relative_label(paths.root, manifest),
                write_manifest_flag="--write-manifest",
            )
        try:
            write_manifest_if_missing(paths.root)
        except RuntimeError as exc:
            return _fail(
                "manifest_write_failed",
                "Root manifest could not be written.",
                path=_relative_label(paths.root, manifest),
                error=str(exc),
            )
        manifest_written = True

    if not manifest.is_file():
        return _fail(
            "manifest_invalid",
            "Root manifest path is not a file.",
            path=_relative_label(paths.root, manifest),
        )

    data, json_error = _read_json_object(manifest, "manifest_invalid")
    if json_error:
        return json_error
    assert data is not None

    for validation in (
        _validate_manifest(data, manifest),
        _validate_source_cards(paths),
        _validate_wiki_front_matter(paths),
        _validate_optional_artifacts(paths),
    ):
        if validation:
            return validation

    return _pass(
        "schema_ok",
        "Schema contracts are valid.",
        root=str(paths.root),
        manifest=_relative_label(paths.root, manifest),
        manifest_written=manifest_written,
        write_lock_integration_required=manifest_written,
        write_lock_required_task="Productization Hardening Task 4"
        if manifest_written
        else "",
    )


def check_backup_manifest(path: str | Path) -> ProductResult:
    manifest = Path(path)
    if not manifest.exists():
        return _fail("backup_manifest_missing", "Backup manifest is missing.", path=str(manifest))
    if not manifest.is_file():
        return _fail("backup_manifest_invalid", "Backup manifest path is not a file.", path=str(manifest))
    data, error = _read_json_object(manifest, "backup_manifest_invalid")
    if error:
        return error
    assert data is not None

    secret_path = _secret_shape(data)
    if secret_path:
        return _fail(
            "secret_in_manifest",
            "Backup manifest contains secret-shaped metadata.",
            path=str(manifest),
            field=secret_path,
        )
    version_result = _version_failure(
        data, field="format_version", invalid_classification="backup_manifest_invalid"
    )
    if version_result:
        return version_result
    if not isinstance(data.get("created_at"), str) or not data["created_at"]:
        return _fail(
            "backup_manifest_invalid",
            "Backup manifest created_at is required.",
            path=str(manifest),
            field="created_at",
        )
    files = data.get("files")
    if not isinstance(files, list):
        return _fail(
            "backup_manifest_invalid",
            "Backup manifest files must be a list.",
            path=str(manifest),
            field="files",
        )

    normalized_paths: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return _fail(
                "backup_manifest_invalid",
                "Backup manifest file entry must be an object.",
                path=str(manifest),
                index=index,
            )
        file_path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(file_path, str):
            return _fail(
                "backup_manifest_invalid",
                "Backup manifest file path is required.",
                path=str(manifest),
                index=index,
                field="path",
            )
        unsafe_reason = _unsafe_relative_path_reason(file_path)
        if unsafe_reason:
            return _fail(
                unsafe_reason,
                "Backup manifest contains an unsafe file path.",
                path=str(manifest),
                index=index,
                candidate=file_path,
            )
        normalized = file_path.replace("\\", "/").casefold()
        if normalized in normalized_paths:
            return _fail(
                "duplicate_normalized_path",
                "Backup manifest contains duplicate normalized paths.",
                path=str(manifest),
                candidate=file_path,
            )
        normalized_paths.add(normalized)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            return _fail(
                "backup_manifest_invalid",
                "Backup manifest file sha256 is invalid.",
                path=str(manifest),
                index=index,
                field="sha256",
            )

    counts = data.get("counts", {})
    if counts is not None:
        if not isinstance(counts, dict) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()
        ):
            return _fail(
                "backup_manifest_invalid",
                "Backup manifest counts must be non-negative integers.",
                path=str(manifest),
                field="counts",
            )

    excluded = data.get("excluded", [])
    if excluded is not None:
        if not isinstance(excluded, list) or any(
            not isinstance(item, str) or _unsafe_relative_path_reason(item)
            for item in excluded
        ):
            return _fail(
                "backup_manifest_invalid",
                "Backup manifest excluded paths are invalid.",
                path=str(manifest),
                field="excluded",
            )

    return _pass(
        "backup_manifest_ok",
        "Backup manifest contract is valid.",
        path=str(manifest),
        file_count=len(files),
    )


def check_profile_registry(path: str | Path) -> ProductResult:
    from .profile_registry import PROFILE_FIELDS, load_profiles

    registry_path = Path(path)
    if not registry_path.exists():
        return _fail(
            "profile_registry_missing",
            "Profile registry is missing.",
            path=str(registry_path),
        )
    if not registry_path.is_file():
        return _fail(
            "profile_registry_invalid",
            "Profile registry path is not a file.",
            path=str(registry_path),
        )
    data, error = _read_json_object(registry_path, "profile_registry_invalid")
    if error:
        return error
    assert data is not None

    try:
        normalized = load_profiles(registry_path)
    except RuntimeError as exc:
        classification = (
            "secret_in_profile_registry"
            if "secret field is not allowed" in str(exc)
            else "profile_registry_invalid"
        )
        return _fail(classification, "Profile registry contract validation failed.", error=str(exc))

    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        return _fail(
            "profile_registry_invalid",
            "Profile registry profiles must be a list.",
            path=str(registry_path),
            field="profiles",
        )
    required = set(PROFILE_FIELDS)
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            return _fail(
                "profile_registry_invalid",
                "Profile entry must be an object.",
                path=str(registry_path),
                index=index,
            )
        if set(profile) != required:
            return _fail(
                "profile_registry_invalid",
                "Profile entry fields do not match the minimal registry contract.",
                path=str(registry_path),
                index=index,
                expected=sorted(required),
                actual=sorted(profile),
            )
        for field in PROFILE_FIELDS:
            if field == "last_health_at":
                continue
            if not isinstance(profile.get(field), str) or not profile[field]:
                return _fail(
                    "profile_registry_invalid",
                    "Profile entry field is invalid.",
                    path=str(registry_path),
                    index=index,
                    field=field,
                )
    return _pass(
        "profile_registry_ok",
        "Profile registry contract is valid.",
        path=str(registry_path),
        profile_count=len(normalized["profiles"]),
    )


def check_retrieval_benchmark(path: str | Path) -> ProductResult:
    benchmark_path = Path(path)
    if not benchmark_path.exists():
        return _fail(
            "retrieval_benchmark_missing",
            "Retrieval benchmark is missing.",
            path=str(benchmark_path),
        )
    if not benchmark_path.is_file():
        return _fail(
            "retrieval_benchmark_invalid",
            "Retrieval benchmark path is not a file.",
            path=str(benchmark_path),
        )
    try:
        lines = benchmark_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(
            "retrieval_benchmark_invalid",
            "Cannot read retrieval benchmark.",
            path=str(benchmark_path),
            error=type(exc).__name__,
        )

    record_count = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark line is not valid JSON.",
                path=str(benchmark_path),
                line=line_number,
                column=exc.colno,
            )
        if not isinstance(record, dict):
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark line must be an object.",
                path=str(benchmark_path),
                line=line_number,
            )
        secret_path = _secret_shape(record)
        if secret_path:
            return _fail(
                "secret_in_retrieval_benchmark",
                "Retrieval benchmark contains secret-shaped metadata.",
                path=str(benchmark_path),
                line=line_number,
                field=secret_path,
            )
        query = record.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark query is required.",
                path=str(benchmark_path),
                line=line_number,
                field="query",
            )
        expected_source_ids = record.get("expected_source_ids", [])
        expected_wiki_paths = record.get("expected_wiki_paths", [])
        if not isinstance(expected_source_ids, list) or any(
            not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id)
            for source_id in expected_source_ids
        ):
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark expected_source_ids are invalid.",
                path=str(benchmark_path),
                line=line_number,
                field="expected_source_ids",
            )
        if not isinstance(expected_wiki_paths, list) or any(
            not isinstance(wiki_path, str)
            or _unsafe_relative_path_reason(wiki_path)
            or not wiki_path.startswith("wiki/")
            for wiki_path in expected_wiki_paths
        ):
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark expected_wiki_paths are invalid.",
                path=str(benchmark_path),
                line=line_number,
                field="expected_wiki_paths",
            )
        if "expected_quotes" in record:
            expected_quotes = record["expected_quotes"]
            if not isinstance(expected_quotes, list) or not expected_quotes or any(
                not isinstance(quote, str) or not quote.strip()
                for quote in expected_quotes
            ):
                return _fail(
                    "retrieval_benchmark_invalid",
                    "Retrieval benchmark expected_quotes are invalid.",
                    path=str(benchmark_path),
                    line=line_number,
                    field="expected_quotes",
                )
        if not expected_source_ids and not expected_wiki_paths:
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark requires expected source ids or wiki paths.",
                path=str(benchmark_path),
                line=line_number,
            )
        privacy = record.get("privacy")
        if privacy not in PRIVACY_VALUES:
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark privacy is invalid.",
                path=str(benchmark_path),
                line=line_number,
                field="privacy",
            )
        if privacy in {"sensitive", "restricted"} and not (
            record.get("confirmed") is True or record.get("user_confirmed") is True
        ):
            return _fail(
                "retrieval_benchmark_invalid",
                "Sensitive retrieval benchmark samples require explicit confirmation metadata.",
                path=str(benchmark_path),
                line=line_number,
                field="privacy",
                reason="unconfirmed_sensitive_sample",
            )
        if "notes" in record and not isinstance(record["notes"], str):
            return _fail(
                "retrieval_benchmark_invalid",
                "Retrieval benchmark notes must be a string.",
                path=str(benchmark_path),
                line=line_number,
                field="notes",
            )
        record_count += 1

    if record_count == 0:
        return _fail(
            "retrieval_benchmark_invalid",
            "Retrieval benchmark must contain at least one record.",
            path=str(benchmark_path),
        )
    return _pass(
        "retrieval_benchmark_ok",
        "Retrieval benchmark contract is valid.",
        path=str(benchmark_path),
        record_count=record_count,
    )
