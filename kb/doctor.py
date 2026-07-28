"""Read-only knowledge-base health analyzer."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .commands import (
    lint_repository,
    lock_check_repository,
    schema_check_repository,
    status_repository,
)
from .embeddings import load_embedding_config
from .governance import analyze_governance
from .llm import load_llm_config
from .paths import KnowledgeBasePaths
from .redaction import redact_text, summarize_text
from .schema_check import CURRENT_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION


Check = dict[str, object]


def _redact(value: object) -> str:
    return summarize_text(redact_text(value), limit=500)


def _safe_detail(value: object) -> object:
    if isinstance(value, dict):
        return {_redact(key): _safe_detail(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _redact(value)


def _check(
    check_id: str,
    status: str,
    *,
    severity: str,
    classification: str,
    summary: str,
    details: dict[str, object] | None = None,
) -> Check:
    if status not in {"pass", "warning", "failed"}:
        status = "warning"
        classification = "invalid_check_status"
        summary = "Doctor check returned an invalid status."
    return {
        "id": _redact(check_id),
        "status": _redact(status),
        "severity": _redact(severity),
        "classification": _redact(classification),
        "summary": _redact(summary),
        "details": _safe_detail(details or {}),
    }


def _pass(check_id: str, classification: str, summary: str, **details: object) -> Check:
    return _check(
        check_id,
        "pass",
        severity="info",
        classification=classification,
        summary=summary,
        details=details,
    )


def _warning(
    check_id: str, classification: str, summary: str, **details: object
) -> Check:
    return _check(
        check_id,
        "warning",
        severity="advisory",
        classification=classification,
        summary=summary,
        details=details,
    )


def _failed(
    check_id: str, classification: str, summary: str, **details: object
) -> Check:
    return _check(
        check_id,
        "failed",
        severity="blocking",
        classification=classification,
        summary=summary,
        details=details,
    )


def _product_result_check(check_id: str, result: object) -> Check:
    data = result.to_dict()  # ProductResult-compatible.
    return _check(
        check_id,
        str(data.get("status", "failed")),
        severity=str(data.get("severity", "blocking")),
        classification=str(data.get("classification", "check_failed")),
        summary=str(data.get("summary", "Doctor check failed.")),
        details=data.get("details", {}) if isinstance(data.get("details"), dict) else {},
    )


def _relative_label(paths: KnowledgeBasePaths, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()


def _root_exists_check(paths: KnowledgeBasePaths) -> Check:
    if not paths.root.exists():
        return _failed(
            "root-exists",
            "root_missing",
            "Knowledge base root does not exist.",
        )
    if not paths.root.is_dir():
        return _failed(
            "root-exists",
            "root_not_directory",
            "Knowledge base root is not a directory.",
        )
    return _pass("root-exists", "root_ok", "Knowledge base root exists.")


def _initialized_check(paths: KnowledgeBasePaths) -> Check:
    if not paths.root.is_dir():
        return _failed(
            "initialized",
            "repository_uninitialized",
            "Knowledge base is not initialized.",
        )
    required_directories = (
        paths.raw,
        paths.inbox,
        paths.wiki,
        paths.sources,
        paths.meta,
        paths.db,
    )
    required_files = (
        paths.meta / "index.md",
        paths.meta / "log.md",
        paths.meta / "source-map.jsonl",
        paths.meta / "review-queue.md",
    )
    missing = [
        _relative_label(paths, path)
        for path in (*required_directories, *required_files)
        if not path.exists()
    ]
    wrong_type = [
        _relative_label(paths, path)
        for path in required_directories
        if path.exists() and not path.is_dir()
    ]
    wrong_type.extend(
        _relative_label(paths, path)
        for path in required_files
        if path.exists() and not path.is_file()
    )
    if missing or wrong_type:
        return _failed(
            "initialized",
            "repository_uninitialized",
            "Required knowledge-base directories or metadata files are missing.",
            missing=missing,
            wrong_type=wrong_type,
        )
    return _pass("initialized", "repository_initialized", "Knowledge base is initialized.")


def _manifest_check(paths: KnowledgeBasePaths) -> Check:
    manifest = paths.meta / "kb-manifest.json"
    if not paths.root.is_dir():
        return _failed("manifest", "root_missing", "Manifest cannot be read.")
    if not manifest.exists():
        return _failed("manifest", "manifest_missing", "Root manifest is missing.")
    if not manifest.is_file():
        return _failed("manifest", "manifest_invalid", "Root manifest is not a file.")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed(
            "manifest",
            "manifest_invalid",
            "Root manifest cannot be read as valid UTF-8 JSON.",
            error=type(exc).__name__,
        )
    if not isinstance(data, dict):
        return _failed("manifest", "manifest_invalid", "Root manifest is not an object.")
    return _pass("manifest", "manifest_present", "Root manifest is present.")


def _schema_check(paths: KnowledgeBasePaths) -> Check:
    try:
        return _product_result_check("schema", schema_check_repository(paths.root))
    except Exception as exc:
        return _failed(
            "schema",
            "schema_check_error",
            "Schema check failed before producing a result.",
            error=type(exc).__name__,
        )


def _write_lock_check(paths: KnowledgeBasePaths) -> Check:
    try:
        return _product_result_check("write-lock", lock_check_repository(paths.root))
    except Exception as exc:
        return _failed(
            "write-lock",
            "lock_check_error",
            "Write lock check failed before producing a result.",
            error=type(exc).__name__,
        )


def _git_installed_check() -> Check:
    if shutil.which("git"):
        return _pass("git-installed", "git_available", "Git executable is available.")
    return _failed("git-installed", "git_missing", "Git executable is not available.")


def _run_git(paths: KnowledgeBasePaths, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=paths.root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )


def _git_repository_check(paths: KnowledgeBasePaths) -> Check:
    if not shutil.which("git"):
        return _warning(
            "git-repository",
            "git_missing",
            "Git repository check was skipped because Git is unavailable.",
        )
    if not paths.root.is_dir():
        return _failed("git-repository", "root_missing", "Git repository cannot be read.")
    try:
        completed = _run_git(paths, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _warning(
            "git-repository",
            "git_check_unavailable",
            "Git repository check could not run.",
            error=type(exc).__name__,
        )
    if completed.returncode == 0 and completed.stdout.strip().lower() == "true":
        return _pass("git-repository", "git_repository", "Root is inside a Git repository.")
    return _warning(
        "git-repository",
        "not_git_repository",
        "Knowledge base root is not inside a Git repository.",
    )


def _git_worktree_clean_check(paths: KnowledgeBasePaths) -> Check:
    if not shutil.which("git"):
        return _warning(
            "git-worktree-clean",
            "git_missing",
            "Git worktree check was skipped because Git is unavailable.",
        )
    if not paths.root.is_dir():
        return _failed(
            "git-worktree-clean",
            "root_missing",
            "Git worktree cannot be read.",
        )
    try:
        repo = _run_git(paths, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _warning(
            "git-worktree-clean",
            "git_check_unavailable",
            "Git worktree check could not run.",
            error=type(exc).__name__,
        )
    if repo.returncode != 0 or repo.stdout.strip().lower() != "true":
        return _warning(
            "git-worktree-clean",
            "not_git_repository",
            "Git worktree check was skipped because root is not a Git repository.",
        )
    try:
        completed = _run_git(paths, ["status", "--porcelain", "--untracked-files=all"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed(
            "git-worktree-clean",
            "git_status_error",
            "Git worktree status could not be read.",
            error=type(exc).__name__,
        )
    if completed.returncode != 0:
        return _failed(
            "git-worktree-clean",
            "git_status_error",
            "Git worktree status failed.",
        )
    dirty_count = len([line for line in completed.stdout.splitlines() if line.strip()])
    if dirty_count:
        return _failed(
            "git-worktree-clean",
            "dirty_worktree",
            "Git worktree has uncommitted changes.",
            dirty_count=dirty_count,
        )
    return _pass("git-worktree-clean", "worktree_clean", "Git worktree is clean.")


def _python_version_check() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        return _pass("python-version", "python_supported", "Python version is supported.", version=version)
    return _failed(
        "python-version",
        "python_unsupported",
        "Python version is below the supported minimum.",
        version=version,
        minimum="3.10",
    )


def _package_import_check() -> Check:
    try:
        package = importlib.import_module("kb")
    except Exception as exc:
        return _failed(
            "package-import",
            "package_import_failed",
            "The kb package could not be imported.",
            error=type(exc).__name__,
        )
    version = getattr(package, "__version__", "unknown")
    return _pass("package-import", "package_imported", "The kb package imports.", version=version)


def _sqlite_fts_check() -> Check:
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE doctor_fts USING fts5(content)")
    except sqlite3.Error as exc:
        return _failed(
            "sqlite-fts",
            "sqlite_fts_unavailable",
            "SQLite FTS5 is unavailable.",
            error=type(exc).__name__,
        )
    return _pass("sqlite-fts", "sqlite_fts_available", "SQLite FTS5 is available.")


def _index_status_check(paths: KnowledgeBasePaths) -> Check:
    if not paths.database.is_file():
        return _failed("index-status", "database_missing", "Search index database is missing.")
    try:
        with closing(sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
            ).fetchall()
            tables = {str(row[0]) for row in table_rows}
            required = {"documents", "chunks", "chunk_fts"}
            missing = sorted(required - tables)
            if missing:
                return _failed(
                    "index-status",
                    "index_schema_missing",
                    "Search index database is missing required tables.",
                    missing=missing,
                )
            documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            fts_rows = int(connection.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0])
    except sqlite3.Error as exc:
        return _failed(
            "index-status",
            "index_unreadable",
            "Search index database could not be read.",
            error=type(exc).__name__,
        )
    if chunks != fts_rows:
        return _failed(
            "index-status",
            "index_fts_mismatch",
            "Search index FTS rows do not match chunk rows.",
            documents=documents,
            chunks=chunks,
            fts_rows=fts_rows,
        )
    return _pass(
        "index-status",
        "index_readable",
        "Search index is readable.",
        documents=documents,
        chunks=chunks,
        fts_rows=fts_rows,
    )


def _vector_index_status_check(paths: KnowledgeBasePaths) -> Check:
    if not paths.database.is_file():
        return _warning(
            "vector-index-status",
            "database_missing",
            "Vector index database is missing.",
        )
    try:
        with closing(sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'chunk_vectors'"
            ).fetchone()
            if table is None:
                return _warning(
                    "vector-index-status",
                    "vector_index_missing",
                    "Vector index has not been built.",
                )
            rows = int(connection.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0])
    except sqlite3.Error as exc:
        return _failed(
            "vector-index-status",
            "vector_index_unreadable",
            "Vector index could not be read.",
            error=type(exc).__name__,
        )
    if rows == 0:
        return _warning(
            "vector-index-status",
            "vector_index_empty",
            "Vector index table exists but contains no vectors.",
        )
    return _pass("vector-index-status", "vector_index_readable", "Vector index is readable.", rows=rows)


def _lint_check(paths: KnowledgeBasePaths) -> Check:
    try:
        issues = lint_repository(paths.root)
    except Exception as exc:
        return _failed(
            "lint",
            "lint_error",
            "Lint check failed before producing issues.",
            error=type(exc).__name__,
        )
    if issues:
        issue_types = sorted({str(issue.get("type", "unknown")) for issue in issues})
        return _failed(
            "lint",
            "lint_issues",
            "Lint found blocking local knowledge-base issues.",
            issue_count=len(issues),
            issue_types=issue_types,
        )
    return _pass("lint", "lint_clean", "Lint found no issues.")


def _status_check(paths: KnowledgeBasePaths) -> Check:
    try:
        status = status_repository(paths.root)
    except Exception as exc:
        return _failed(
            "status",
            "status_error",
            "Repository status could not be read.",
            error=type(exc).__name__,
        )
    return _pass("status", "status_readable", "Repository status is readable.", **status)


def _governance_check(paths: KnowledgeBasePaths) -> Check:
    try:
        analysis = analyze_governance(paths.root)
    except Exception as exc:
        return _failed(
            "governance",
            "governance_error",
            "Governance analysis failed before producing issues.",
            error=type(exc).__name__,
        )
    blocking_count = int(analysis.get("blocking_count", 0))
    advisory_count = int(analysis.get("advisory_count", 0))
    if blocking_count:
        return _failed(
            "governance",
            "governance_blocking_issues",
            "Governance analysis found blocking issues.",
            blocking_count=blocking_count,
            advisory_count=advisory_count,
        )
    if advisory_count:
        return _warning(
            "governance",
            "governance_advisory_issues",
            "Governance analysis found advisory issues.",
            blocking_count=blocking_count,
            advisory_count=advisory_count,
        )
    return _pass(
        "governance",
        "governance_clean",
        "Governance analysis found no issues.",
        blocking_count=blocking_count,
        advisory_count=advisory_count,
    )


def _tesseract_check() -> Check:
    configured = os.environ.get("KB_TESSERACT_CMD", "").strip()
    if configured or shutil.which("tesseract"):
        return _pass("tesseract", "tesseract_available", "Tesseract OCR is configured.")
    return _warning(
        "tesseract",
        "tesseract_missing",
        "Tesseract OCR is not configured or discoverable.",
    )


def _obsidian_check(paths: KnowledgeBasePaths) -> Check:
    if (paths.root / ".obsidian").is_dir():
        return _pass("obsidian", "obsidian_vault_configured", "Obsidian vault files are present.")
    if shutil.which("obsidian"):
        return _pass("obsidian", "obsidian_available", "Obsidian executable is discoverable.")
    return _warning(
        "obsidian",
        "obsidian_missing",
        "Obsidian is not configured for this root or discoverable on PATH.",
    )


def _llm_config_check() -> Check:
    try:
        config = load_llm_config(os.environ)
    except RuntimeError as exc:
        return _warning(
            "llm-config",
            "llm_config_missing_or_invalid",
            "LLM provider config is missing or invalid.",
            error=str(exc),
        )
    return _pass(
        "llm-config",
        "llm_configured",
        "LLM provider config is present.",
        provider=config.base_url,
        model=config.model,
    )


def _embedding_config_check() -> Check:
    try:
        config = load_embedding_config(os.environ)
    except RuntimeError as exc:
        return _warning(
            "embedding-config",
            "embedding_config_missing_or_invalid",
            "Embedding provider config is missing or invalid.",
            error=str(exc),
        )
    return _pass(
        "embedding-config",
        "embedding_configured",
        "Embedding provider config is present.",
        provider=config.base_url,
        model=config.model,
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backup_freshness_check(paths: KnowledgeBasePaths) -> Check:
    candidates = [paths.root / "backup-manifest.json", paths.meta / "backup-manifest.json"]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return _warning(
            "backup-freshness",
            "backup_manifest_missing",
            "No backup manifest is present.",
        )
    newest: datetime | None = None
    for manifest in existing:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _failed(
                "backup-freshness",
                "backup_manifest_unreadable",
                "Backup manifest could not be read.",
                error=type(exc).__name__,
            )
        if not isinstance(data, dict):
            return _failed(
                "backup-freshness",
                "backup_manifest_invalid",
                "Backup manifest is not an object.",
            )
        created_at = _parse_datetime(data.get("created_at"))
        if created_at is None:
            return _failed(
                "backup-freshness",
                "backup_manifest_invalid",
                "Backup manifest created_at is missing or invalid.",
            )
        if newest is None or created_at > newest:
            newest = created_at
    assert newest is not None
    age_days = max(0, int((datetime.now(timezone.utc) - newest).total_seconds() // 86400))
    if age_days > 30:
        return _warning(
            "backup-freshness",
            "backup_stale",
            "Newest backup manifest is older than the freshness window.",
            age_days=age_days,
        )
    return _pass("backup-freshness", "backup_fresh", "Backup manifest is fresh.", age_days=age_days)


def _docs_encoding_check(paths: KnowledgeBasePaths) -> Check:
    docs = paths.root / "docs"
    if not docs.exists():
        return _pass("docs-encoding", "docs_absent", "No docs directory is present.")
    if not docs.is_dir():
        return _failed("docs-encoding", "docs_path_invalid", "Docs path is not a directory.")
    checked = 0
    for path in sorted(docs.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".json", ".yml", ".yaml"}:
            continue
        checked += 1
        try:
            path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _failed(
                "docs-encoding",
                "docs_encoding_invalid",
                "Docs file is not readable as UTF-8.",
                path=_relative_label(paths, path),
                error=type(exc).__name__,
            )
    return _pass("docs-encoding", "docs_encoding_ok", "Docs files are UTF-8 readable.", checked=checked)


def _migration_status_check(paths: KnowledgeBasePaths) -> Check:
    manifest = paths.meta / "kb-manifest.json"
    if not manifest.is_file():
        return _failed(
            "migration-status",
            "migration_status_unavailable",
            "Migration status cannot be read without a manifest.",
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed(
            "migration-status",
            "migration_status_unavailable",
            "Migration status could not be read.",
            error=type(exc).__name__,
        )
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, bool) or not isinstance(version, int):
        return _failed(
            "migration-status",
            "schema_version_invalid",
            "Manifest schema version is invalid.",
        )
    if version > CURRENT_SCHEMA_VERSION:
        return _failed(
            "migration-status",
            "schema_migration_required",
            "Manifest schema version is newer than this product supports.",
            found=version,
            supported=CURRENT_SCHEMA_VERSION,
        )
    if version < MIN_SUPPORTED_SCHEMA_VERSION:
        return _failed(
            "migration-status",
            "schema_unsupported",
            "Manifest schema version is no longer supported.",
            found=version,
            supported_min=MIN_SUPPORTED_SCHEMA_VERSION,
        )
    if version < CURRENT_SCHEMA_VERSION:
        return _warning(
            "migration-status",
            "schema_migration_available",
            "Manifest schema version is older than the current product version.",
            found=version,
            current=CURRENT_SCHEMA_VERSION,
        )
    return _pass(
        "migration-status",
        "schema_current",
        "Manifest schema version is current.",
        version=version,
    )


def _probe_llm_online() -> dict[str, object]:
    try:
        config = load_llm_config(os.environ)
    except RuntimeError as exc:
        return {
            "status": "warning",
            "classification": "llm_config_missing_or_invalid",
            "summary": "LLM online probe skipped because provider config is missing or invalid.",
            "details": {"error": str(exc)},
        }
    return {
        "status": "warning",
        "classification": "llm_online_probe_not_run",
        "summary": "LLM provider config is present; no provider roundtrip was performed.",
        "provider": config.base_url,
    }


def _probe_embedding_online() -> dict[str, object]:
    try:
        config = load_embedding_config(os.environ)
    except RuntimeError as exc:
        return {
            "status": "warning",
            "classification": "embedding_config_missing_or_invalid",
            "summary": "Embedding online probe skipped because provider config is missing or invalid.",
            "details": {"error": str(exc)},
        }
    return {
        "status": "warning",
        "classification": "embedding_online_probe_not_run",
        "summary": "Embedding provider config is present; no provider roundtrip was performed.",
        "provider": config.base_url,
    }


def _online_probe_check(check_id: str, payload: dict[str, object]) -> Check:
    details = payload.get("details", {})
    if not isinstance(details, dict):
        details = {}
    if "provider" in payload:
        details = dict(details)
        details["provider"] = payload["provider"]
    status = str(payload.get("status", "warning"))
    severity = str(
        payload.get(
            "severity",
            "info" if status == "pass" else "blocking" if status == "failed" else "advisory",
        )
    )
    return _check(
        check_id,
        status,
        severity=severity,
        classification=str(payload.get("classification", "online_probe_result")),
        summary=str(payload.get("summary", "Online probe produced a result.")),
        details=details,
    )


def _online_checks() -> list[Check]:
    checks: list[Check] = []
    for check_id, probe in (
        ("llm-online", _probe_llm_online),
        ("embedding-online", _probe_embedding_online),
    ):
        try:
            payload = probe()
        except Exception as exc:
            checks.append(
                _warning(
                    check_id,
                    "online_probe_error",
                    "Online probe failed before producing a result.",
                    error=type(exc).__name__,
                )
            )
            continue
        checks.append(_online_probe_check(check_id, payload))
    return checks


def _overall_status(checks: list[Check]) -> str:
    if any(
        check.get("status") == "failed" and check.get("severity") == "blocking"
        for check in checks
    ):
        return "failed"
    if any(check.get("status") in {"warning", "failed"} for check in checks):
        return "warning"
    return "pass"


def doctor(root: str | Path, *, online: bool = False) -> dict[str, object]:
    """Return a read-only health report for a knowledge-base root."""

    paths = KnowledgeBasePaths(Path(root))
    checks = [
        _root_exists_check(paths),
        _initialized_check(paths),
        _manifest_check(paths),
        _schema_check(paths),
        _write_lock_check(paths),
        _git_installed_check(),
        _git_repository_check(paths),
        _git_worktree_clean_check(paths),
        _python_version_check(),
        _package_import_check(),
        _sqlite_fts_check(),
        _index_status_check(paths),
        _vector_index_status_check(paths),
        _lint_check(paths),
        _status_check(paths),
        _governance_check(paths),
        _tesseract_check(),
        _obsidian_check(paths),
        _llm_config_check(),
        _embedding_config_check(),
        _backup_freshness_check(paths),
        _docs_encoding_check(paths),
        _migration_status_check(paths),
    ]
    if online:
        checks.extend(_online_checks())
    return {
        "root": _redact(paths.root),
        "status": _overall_status(checks),
        "checks": checks,
    }


def format_doctor_summary(result: dict[str, object]) -> str:
    checks = result.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    counts = {"pass": 0, "warning": 0, "failed": 0}
    failed_ids: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "failed"))
        if status in counts:
            counts[status] += 1
        if status == "failed":
            failed_ids.append(str(check.get("id", "unknown")))
    failed_text = ",".join(failed_ids) if failed_ids else "none"
    line = (
        f"doctor status={result.get('status', 'failed')} "
        f"pass={counts['pass']} warning={counts['warning']} failed={counts['failed']} "
        f"failed_ids={failed_text}"
    )
    return _redact(line)


__all__ = [
    "doctor",
    "format_doctor_summary",
    "_probe_llm_online",
    "_probe_embedding_online",
]
