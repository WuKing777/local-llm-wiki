"""Migration verification for restored knowledge-base roots."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .commands import vector_rebuild
from .product_result import ProductResult


CHECKED_PREFIXES = ("raw", "sources", "wiki", "meta")
GENERATED_REPORTS = {
    "meta/quality-report.md",
    "meta/doctor-report.json",
    "meta/log.md",
}


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


def _durable_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for prefix in CHECKED_PREFIXES:
        directory = root / prefix
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in GENERATED_REPORTS:
                continue
            hashes[relative.casefold()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _hash_check(source: Path, restored: Path) -> ProductResult:
    source_hashes = _durable_hashes(source)
    restored_hashes = _durable_hashes(restored)
    source_keys = set(source_hashes)
    restored_keys = set(restored_hashes)
    missing = sorted(source_keys - restored_keys)
    extra = sorted(restored_keys - source_keys)
    mismatched = sorted(
        key
        for key in source_keys & restored_keys
        if source_hashes[key] != restored_hashes[key]
    )
    if missing or extra or mismatched:
        return _fail(
            "hash_mismatch",
            "Restored durable file hashes do not match the source root.",
            missing=missing,
            extra=extra,
            mismatched=mismatched,
        )
    return _pass(
        "hashes_match",
        "Restored durable file hashes match the source root.",
        file_count=len(source_hashes),
    )


def _embedding_validation_requested() -> bool:
    return os.environ.get("KB_MIGRATE_CHECK_VECTOR", "").casefold() in {
        "1",
        "true",
        "yes",
    }


def _vector_check(restored: Path) -> ProductResult | None:
    if not _embedding_validation_requested():
        return None
    try:
        result = vector_rebuild(restored)
    except RuntimeError as exc:
        message = str(exc)
        classification = (
            "external_dependency_missing"
            if "KB_EMBEDDING_" in message or "Embedding request failed" in message
            else "vector_rebuild_failed"
        )
        return _fail(classification, "Vector rebuild check failed.", error=message)
    return _pass(
        "vector_rebuild_passed",
        "Vector rebuild check passed.",
        chunks=result.get("chunks", 0),
        model=result.get("model", ""),
    )


def migrate_check(source: str | Path, restored: str | Path) -> ProductResult:
    source_path = Path(source).expanduser().resolve()
    restored_path = Path(restored).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        return _fail("source_missing", "Source knowledge-base root is missing.", source=str(source_path))
    if not restored_path.exists() or not restored_path.is_dir():
        return _fail("restored_missing", "Restored knowledge-base root is missing.", restored=str(restored_path))

    hash_result = _hash_check(source_path, restored_path)
    if hash_result.status != "pass":
        return hash_result

    vector_result = _vector_check(restored_path)
    if vector_result is not None and vector_result.status != "pass":
        details = dict(vector_result.details)
        details["hash_check"] = hash_result.to_dict()
        return _fail(
            vector_result.classification,
            vector_result.summary,
            **details,
        )

    return _pass(
        "migrate_check_passed",
        "Restored knowledge-base root matches durable source assets.",
        hash_check=hash_result.to_dict(),
        vector_check=vector_result.to_dict() if vector_result is not None else {"status": "not_applicable"},
    )


__all__ = ["migrate_check"]
