import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from .context import (
    EmptyContextError,
    build_context_pack,
    build_prompt_messages,
    prompt_hash,
)
from .draft_repair import DraftRepairError, next_repaired_draft_path, repair_draft_content
from .embeddings import (
    OpenAICompatibleEmbeddingClient,
    load_embedding_config,
)
from .factuality import (
    llm_draft_contract_status,
    parse_llm_draft_response,
)
from .llm import OpenAICompatibleClient, load_llm_config
from .locks import WriteLockError, acquire_write_lock
from .paths import KnowledgeBasePaths, generated_gitignore_content
from .schema import initialize_database
from .schema_check import write_manifest_if_missing
from .sources import (
    OPTIONAL_SOURCE_CARD_FIELDS,
    read_source_card,
    remove_source_map_entry,
    source_id_and_sha256,
    source_metadata,
    upsert_source_map,
    write_source_card,
)
from .text import SUPPORTED_EXTENSIONS, chunk_text, extract_text, kind_for_path
from .wiki import (
    draft_id,
    draft_path_for_title,
    draft_timestamp,
    read_draft,
    target_path_for_title,
    validate_draft,
    validate_draft_content,
    wiki_link_exists,
    write_draft,
    write_draft_at_path,
    _drafts_dir,
)


METADATA_FILES = {
    "index.md": "# Knowledge Base Index\n",
    "log.md": "# Audit Log\n",
    "source-map.jsonl": "",
    "review-queue.md": "# Review Queue\n",
    "quality-report.md": "# Quality Report\n",
}
OBSIDIAN_APP_CONFIG = {
    "alwaysUpdateLinks": True,
    "attachmentFolderPath": "meta/assets",
    "newFileLocation": "folder",
    "newFileFolderPath": "inbox",
}
OBSIDIAN_CORE_PLUGINS = [
    "file-explorer",
    "global-search",
    "switcher",
    "graph",
    "backlink",
    "outgoing-link",
    "tag-pane",
    "page-preview",
    "templates",
    "command-palette",
]
OBSIDIAN_TEMPLATES_CONFIG = {
    "folder": "meta/templates",
    "dateFormat": "YYYY-MM-DD",
    "timeFormat": "HH:mm",
}
OBSIDIAN_HOME = """# Knowledge Base Home

## Daily Loop

1. Add raw source material to inbox or raw.
2. Run ingest-inbox from the repository root.
3. Run llm-check before drafting.
4. Use llm-draft only when local evidence is found.
5. Validate and publish drafts before editing stable wiki pages.
6. Ask questions with answer and review the cited evidence.

## Working Areas

- [[review-queue]]
- sources
- wiki/_drafts
- wiki
- inbox

## Guardrails

- LLM output belongs in wiki/_drafts first.
- Stable wiki claims need source ids and exact local evidence.
- No retrieved evidence means no draft.
- Real API keys must never be pasted into notes.
"""
OBSIDIAN_SOURCE_REVIEW_TEMPLATE = """# Source Review

source_id:
title:
raw_path:
reviewed_at:

## What This Source Supports

-

## Follow-Up Questions

-
"""
OBSIDIAN_WIKI_PAGE_TEMPLATE = """# Title

Each factual paragraph must cite a real source id such as src-xxxxxxxxxxxx.
Publish LLM-generated content only through validate-draft and publish-draft.
"""
SOURCE_ID_RE = re.compile(r"\bsrc-[0-9a-f]{12}\b")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
FRONT_MATTER_FIELD_RE = re.compile(r"^[A-Za-z0-9_-]+:\s*.*$")
CONFLICT_MARKERS = (
    "conflict",
    "conflicting",
    "contradict",
    "contradiction",
    "\u51b2\u7a81",
    "\u77db\u76fe",
    "\u4e0d\u4e00\u81f4",
)


def _acquire_command_write_lock(root: str | Path, *, operation: str):
    try:
        return acquire_write_lock(root, operation=operation)
    except WriteLockError:
        raise


REVIEWED_SOURCE_STATUSES = {"reviewed", "verified", "pass"}
BLOCKING_SOURCE_STATUSES = {"needs_reingest", "rejected"}
SOURCE_REVIEW_STATUSES = REVIEWED_SOURCE_STATUSES | BLOCKING_SOURCE_STATUSES
DERIVED_WORKFLOWS = {"ocr", "pdf-text", "pandoc"}
OCR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pdf"}
ANSWER_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "can",
    "did",
    "do",
    "does",
    "for",
    "how",
    "in",
    "is",
    "me",
    "of",
    "or",
    "should",
    "tell",
    "the",
    "to",
    "what",
    "why",
    "with",
}


def _validate_canonical_directories(root: Path, directories: tuple[Path, ...]) -> None:
    resolved_directories: list[tuple[Path, Path]] = []
    seen_targets: dict[Path, Path] = {}

    for directory in directories:
        target = directory.resolve()
        previous = seen_targets.get(target)
        if previous is not None:
            raise RuntimeError(
                f"Canonical path collision: {previous} and {directory} resolve to {target}"
            )
        seen_targets[target] = directory
        resolved_directories.append((directory, target))

    for directory, target in resolved_directories:
        try:
            target.relative_to(root)
        except ValueError:
            raise RuntimeError(
                f"Expected canonical directory inside root: {directory} resolves to {target}"
            ) from None


def _bootstrap_init_lock_directory(paths: KnowledgeBasePaths) -> None:
    if paths.root.exists() and not paths.root.is_dir():
        raise RuntimeError(f"Expected root directory: {paths.root}")
    if paths.meta.exists() or paths.meta.is_symlink():
        return
    paths.meta.mkdir(parents=True, exist_ok=True)


def init_repository(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _bootstrap_init_lock_directory(paths)
    with _acquire_command_write_lock(paths.root, operation="init"):
        return _init_repository_unlocked(paths.root)


def _init_repository_unlocked(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    created_dirs: list[str] = []
    created_files: list[str] = []

    if paths.root.exists() and not paths.root.is_dir():
        raise RuntimeError(f"Expected root directory: {paths.root}")

    required_directories = (
        paths.raw,
        paths.inbox,
        paths.wiki,
        paths.sources,
        paths.meta,
        paths.db,
    )

    for directory in required_directories:
        if directory.exists() and not directory.is_dir():
            raise RuntimeError(f"Expected directory: {directory}")
        if not directory.exists():
            directory.mkdir(parents=True)
            created_dirs.append(str(directory))

    _validate_canonical_directories(paths.root, required_directories)

    if paths.database.exists() and not paths.database.is_file():
        raise RuntimeError(f"Expected database file: {paths.database}")

    for filename, content in METADATA_FILES.items():
        path = paths.meta / filename
        if path.exists() and not path.is_file():
            raise RuntimeError(f"Expected metadata file: {path}")
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created_files.append(str(path))

    manifest_path = paths.meta / "kb-manifest.json"
    if manifest_path.exists() and not manifest_path.is_file():
        raise RuntimeError(f"Expected manifest file: {manifest_path}")
    if not manifest_path.exists():
        write_manifest_if_missing(paths.root)
        created_files.append(str(manifest_path))

    _ensure_generated_gitignore(paths)

    database_exists = paths.database.exists()
    initialize_database(paths.database)
    if not database_exists:
        created_files.append(str(paths.database))

    return {
        "root": str(paths.root),
        "created_dirs": created_dirs,
        "created_files": created_files,
    }


def _ensure_generated_gitignore(paths: KnowledgeBasePaths) -> None:
    gitignore = paths.root / ".gitignore"
    generated = generated_gitignore_content()
    required_entries = {
        "meta/.kb-write.lock",
        "*.tmp",
        "meta/audit/",
        "meta/cache/",
        "meta/runtime/",
    }
    if gitignore.is_symlink() or (gitignore.exists() and not gitignore.is_file()):
        raise RuntimeError(f"Expected .gitignore file: {gitignore}")
    if not gitignore.exists():
        gitignore.write_text(generated, encoding="utf-8")
        return

    try:
        content = gitignore.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Expected UTF-8 .gitignore file: {gitignore}") from exc
    active_entries = {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [entry for entry in sorted(required_entries) if entry not in active_entries]
    if not missing:
        return
    separator = "" if content.endswith("\n") else "\n"
    addition = (
        f"{separator}\n# Product runtime exclusions\n"
        + "\n".join(missing)
        + "\n"
    )
    gitignore.write_text(content + addition, encoding="utf-8")


def _ensure_directory(path: Path, created_dirs: list[str]) -> None:
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"Expected directory: {path}")
    if not path.exists():
        path.mkdir(parents=True)
        created_dirs.append(str(path))


def _write_text_if_missing(path: Path, content: str, created_files: list[str]) -> None:
    if path.exists() and not path.is_file():
        raise RuntimeError(f"Expected file: {path}")
    if not path.exists():
        path.write_text(content, encoding="utf-8")
        created_files.append(str(path))


def init_obsidian_vault(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _bootstrap_init_lock_directory(paths)
    with _acquire_command_write_lock(paths.root, operation="obsidian-init"):
        return _init_obsidian_vault_unlocked(paths.root)


def _init_obsidian_vault_unlocked(root: str | Path) -> dict[str, object]:
    result = _init_repository_unlocked(root)
    paths = KnowledgeBasePaths(Path(root))
    created_dirs = list(result["created_dirs"])
    created_files = list(result["created_files"])

    _ensure_directory(paths.root / ".obsidian", created_dirs)
    _ensure_directory(paths.meta / "templates", created_dirs)
    _ensure_directory(paths.meta / "assets", created_dirs)

    _write_text_if_missing(
        paths.root / ".obsidian" / "app.json",
        json.dumps(OBSIDIAN_APP_CONFIG, indent=2) + "\n",
        created_files,
    )
    _write_text_if_missing(
        paths.root / ".obsidian" / "core-plugins.json",
        json.dumps(OBSIDIAN_CORE_PLUGINS, indent=2) + "\n",
        created_files,
    )
    _write_text_if_missing(
        paths.root / ".obsidian" / "templates.json",
        json.dumps(OBSIDIAN_TEMPLATES_CONFIG, indent=2) + "\n",
        created_files,
    )
    _write_text_if_missing(paths.meta / "obsidian-home.md", OBSIDIAN_HOME, created_files)
    _write_text_if_missing(
        paths.meta / "templates" / "source-review.md",
        OBSIDIAN_SOURCE_REVIEW_TEMPLATE,
        created_files,
    )
    _write_text_if_missing(
        paths.meta / "templates" / "wiki-page.md",
        OBSIDIAN_WIKI_PAGE_TEMPLATE,
        created_files,
    )

    return {
        "root": str(paths.root),
        "created_dirs": created_dirs,
        "created_files": created_files,
    }


def init_personal_exobrain(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _bootstrap_init_lock_directory(paths)
    with _acquire_command_write_lock(paths.root, operation="exobrain-init"):
        return _init_personal_exobrain_unlocked(paths.root)


def _init_personal_exobrain_unlocked(root: str | Path) -> dict[str, object]:
    from . import exobrain

    original_init_obsidian = exobrain.init_obsidian_vault
    exobrain.init_obsidian_vault = _init_obsidian_vault_unlocked
    try:
        return exobrain.init_personal_exobrain(root)
    finally:
        exobrain.init_obsidian_vault = original_init_obsidian


def create_self_statement(
    root: str | Path,
    *,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    input_method: str,
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="self-statement"):
        return _create_self_statement_unlocked(
            paths.root,
            text=text,
            event_date=event_date,
            privacy=privacy,
            confidence=confidence,
            input_method=input_method,
        )


def _create_self_statement_unlocked(
    root: str | Path,
    *,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    input_method: str,
) -> dict[str, str]:
    from .self_statement import create_self_statement as _create_self_statement

    return _create_self_statement(
        root,
        text=text,
        event_date=event_date,
        privacy=privacy,
        confidence=confidence,
        input_method=input_method,
    )


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _resolve_raw_path(paths: KnowledgeBasePaths, metadata: dict[str, str]) -> Path:
    raw_path_value = metadata["raw_path"]
    raw_path = Path(raw_path_value)
    if raw_path.is_absolute():
        raise RuntimeError(
            f"Invalid raw_path for {metadata['source_id']}: {raw_path_value}"
        )

    resolved = (paths.root / raw_path).resolve()
    try:
        resolved.relative_to(paths.raw.resolve())
    except ValueError:
        raise RuntimeError(
            f"Invalid raw_path for {metadata['source_id']}: {raw_path_value}"
        ) from None
    return resolved


def _validate_source_for_index(
    paths: KnowledgeBasePaths, metadata: dict[str, str]
) -> Path:
    raw_path = _resolve_raw_path(paths, metadata)
    if not raw_path.is_file():
        raise RuntimeError(
            f"Missing raw file for {metadata['source_id']}: {metadata['raw_path']}"
        )
    if raw_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"Unsupported extension: {raw_path.suffix.lower()}")
    try:
        data = raw_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"Unreadable raw_path for {metadata['source_id']}: {metadata['raw_path']}"
        ) from exc
    expected_source_id, expected_sha256 = source_id_and_sha256(data)
    if metadata["sha256"] != expected_sha256:
        raise RuntimeError(
            f"Source card sha256 mismatch for {metadata['source_id']}: "
            f"expected {expected_sha256}"
        )
    if metadata["source_id"] != expected_source_id:
        raise RuntimeError(
            f"Source card source_id mismatch for {metadata['source_id']}: "
            f"expected {expected_source_id}"
        )
    expected_kind = kind_for_path(raw_path)
    if metadata["kind"] != expected_kind:
        raise RuntimeError(
            f"Source card kind mismatch for {metadata['source_id']}: "
            f"expected {expected_kind}"
        )
    return raw_path


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name).strip(" .")
    return cleaned or "source"


def _validate_import_target(paths: KnowledgeBasePaths, target: Path) -> None:
    if target.is_symlink():
        raise RuntimeError(f"Import target is a symlink: {target}")
    if target.exists() and not target.is_file():
        raise RuntimeError(f"Import target is not a file: {target}")
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise RuntimeError(f"Import target cannot be resolved: {target}") from exc
    try:
        resolved.relative_to(paths.raw.resolve())
    except ValueError:
        raise RuntimeError(f"Import target outside raw: {target}") from None


def _copy_target(
    paths: KnowledgeBasePaths, source: Path, data: bytes, source_id: str
) -> Path:
    imports_dir = paths.raw / "imports" / date.today().isoformat()
    filename = _safe_filename(source.name)
    target = imports_dir / filename
    _validate_import_target(paths, target)
    try:
        target_matches = target.exists() and target.read_bytes() == data
    except OSError as exc:
        raise RuntimeError(f"Import target cannot be read: {target}") from exc
    if not target.exists() or target_matches:
        return target

    source_id_stem = source_id.removeprefix("src-")
    stem = Path(filename).stem or "source"
    suffix = Path(filename).suffix
    target = imports_dir / f"{stem}-{source_id_stem}{suffix}"
    _validate_import_target(paths, target)
    return target


def _copy_to_raw_subdir(
    paths: KnowledgeBasePaths,
    source: Path,
    data: bytes,
    subdir: str,
    source_id: str,
) -> Path:
    target_dir = paths.raw / subdir / date.today().isoformat()
    filename = _safe_filename(source.name)
    target = target_dir / filename
    _validate_import_target(paths, target)
    try:
        target_matches = target.exists() and target.read_bytes() == data
    except OSError as exc:
        raise RuntimeError(f"Import target cannot be read: {target}") from exc
    if target.exists() and not target_matches:
        source_id_stem = source_id.removeprefix("src-")
        stem = Path(filename).stem or "source"
        suffix = Path(filename).suffix
        target = target_dir / f"{stem}-{source_id_stem}{suffix}"
        _validate_import_target(paths, target)

    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    elif target.read_bytes() != data:
        raise RuntimeError(f"Import target collision: {target}")
    return target


def _raw_file_for_ingest(
    paths: KnowledgeBasePaths, source: Path, data: bytes, source_id: str
) -> Path:
    if _is_under(source, paths.raw):
        return source.resolve()

    target = _copy_target(paths, source, data, source_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    elif target.read_bytes() != data:
        raise RuntimeError(f"Import target collision: {target}")
    return target


def _existing_source_metadata(
    paths: KnowledgeBasePaths, source_id: str, sha256: str
) -> dict[str, str] | None:
    card = paths.sources / f"{source_id}.md"
    if not card.exists():
        return None
    metadata = read_source_card(card)
    _validate_source_for_index(paths, metadata)
    if metadata["sha256"] != sha256:
        raise RuntimeError(f"source_id collision: {source_id}")
    return metadata


def _apply_workflow_metadata(
    paths: KnowledgeBasePaths, metadata: dict[str, str], workflow: str | None
) -> dict[str, str]:
    if not workflow:
        return metadata
    updated = dict(metadata)
    updated["workflow"] = workflow
    write_source_card(paths, updated)
    return updated


def _append_event(paths: KnowledgeBasePaths, event_type: str, message: str) -> None:
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    log_path = paths.meta / "log.md"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"- {timestamp} [{event_type}] {message}\n")

    initialize_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute(
            "INSERT INTO events (event_type, message) VALUES (?, ?)",
            (event_type, message),
        )
        connection.commit()


def _write_index_rows(
    connection: sqlite3.Connection, metadata: dict[str, str], chunks: list[str]
) -> None:
    row = connection.execute(
        "SELECT id FROM documents WHERE source_id = ?",
        (metadata["source_id"],),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO documents (source_id, raw_path, title, sha256)
            VALUES (?, ?, ?, ?)
            """,
            (
                metadata["source_id"],
                metadata["raw_path"],
                metadata["title"],
                metadata["sha256"],
            ),
        )
        document_id = int(cursor.lastrowid)
    else:
        document_id = int(row[0])
        connection.execute(
            """
            UPDATE documents
            SET raw_path = ?, title = ?, sha256 = ?
            WHERE id = ?
            """,
            (
                metadata["raw_path"],
                metadata["title"],
                metadata["sha256"],
                document_id,
            ),
        )
        connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    for chunk_index, content in enumerate(chunks):
        cursor = connection.execute(
            """
            INSERT INTO chunks (document_id, source_id, chunk_index, content)
            VALUES (?, ?, ?, ?)
            """,
            (document_id, metadata["source_id"], chunk_index, content),
        )
        chunk_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO chunk_fts (content, source_id, document_id, chunk_id)
            VALUES (?, ?, ?, ?)
            """,
            (content, metadata["source_id"], document_id, chunk_id),
        )


def _index_source(paths: KnowledgeBasePaths, metadata: dict[str, str]) -> int:
    raw_path = _validate_source_for_index(paths, metadata)
    chunks = chunk_text(extract_text(raw_path))
    initialize_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute(
                "DELETE FROM chunk_fts WHERE source_id = ?", (metadata["source_id"],)
            )
            _write_index_rows(connection, metadata, chunks)
    return len(chunks)


def ingest_file(
    root: str | Path, path: str | Path, workflow: str | None = None
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    with _acquire_command_write_lock(paths.root, operation="ingest"):
        return _ingest_file_unlocked(paths.root, path, workflow=workflow)


def _ingest_file_unlocked(
    root: str | Path, path: str | Path, workflow: str | None = None
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    _init_repository_unlocked(paths.root)

    source = Path(path).expanduser()
    if not source.is_file():
        raise RuntimeError(f"Expected source file: {source}")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"Unsupported extension: {source.suffix.lower()}")

    data = source.read_bytes()
    source_id, sha256 = source_id_and_sha256(data)
    metadata = _existing_source_metadata(paths, source_id, sha256)
    if metadata is None:
        raw_path = _raw_file_for_ingest(paths, source, data, source_id)
        metadata = source_metadata(paths, raw_path, data)
    metadata = _apply_workflow_metadata(paths, metadata, workflow)
    if not (paths.sources / f"{metadata['source_id']}.md").exists():
        write_source_card(paths, metadata)
    upsert_source_map(paths, metadata)
    chunk_count = _index_source(paths, metadata)
    _append_event(
        paths,
        "ingest",
        f"{metadata['source_id']} {metadata['raw_path']} ({chunk_count} chunks)",
    )
    return metadata


def ingest_derived(
    root: str | Path,
    *,
    original: str | Path,
    text: str | Path,
    workflow: str,
) -> dict[str, str]:
    if workflow not in DERIVED_WORKFLOWS:
        raise RuntimeError(
            "workflow must be one of: " + ", ".join(sorted(DERIVED_WORKFLOWS))
        )
    paths = KnowledgeBasePaths(Path(root))
    with _acquire_command_write_lock(paths.root, operation="ingest-derived"):
        return _ingest_derived_unlocked(
            paths.root,
            original=original,
            text=text,
            workflow=workflow,
        )


def _ingest_derived_unlocked(
    root: str | Path,
    *,
    original: str | Path,
    text: str | Path,
    workflow: str,
) -> dict[str, str]:
    if workflow not in DERIVED_WORKFLOWS:
        raise RuntimeError(
            "workflow must be one of: " + ", ".join(sorted(DERIVED_WORKFLOWS))
        )

    paths = KnowledgeBasePaths(Path(root))
    _init_repository_unlocked(paths.root)

    original_path = Path(original).expanduser()
    text_path = Path(text).expanduser()
    if not original_path.is_file():
        raise RuntimeError(f"Expected original file: {original_path}")
    if not text_path.is_file():
        raise RuntimeError(f"Expected derived text file: {text_path}")
    if text_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"Unsupported derived text extension: {text_path.suffix.lower()}")

    text_data = text_path.read_bytes()
    source_id, sha256 = source_id_and_sha256(text_data)
    metadata = _existing_source_metadata(paths, source_id, sha256)
    original_data = original_path.read_bytes()
    imported_text = _copy_to_raw_subdir(paths, text_path, text_data, "derived", source_id)
    imported_original = _copy_to_raw_subdir(
        paths, original_path, original_data, "originals", source_id
    )

    if metadata is None:
        metadata = source_metadata(paths, imported_text, text_data)
    metadata = dict(metadata)
    metadata["workflow"] = workflow
    metadata["original_path"] = _path_label(paths, imported_original)
    write_source_card(paths, metadata)
    upsert_source_map(paths, metadata)
    chunk_count = _index_source(paths, metadata)
    _append_event(
        paths,
        "ingest-derived",
        f"{metadata['source_id']} {metadata['raw_path']} "
        f"original={metadata['original_path']} workflow={workflow} "
        f"({chunk_count} chunks)",
    )
    return metadata


def _extract_pdf_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for PDF text extraction") from exc

    try:
        reader = PdfReader(str(pdf_path))
        parts = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise RuntimeError(f"Cannot extract PDF text: {pdf_path}") from exc
    text = "\n\n".join(part.strip() for part in parts if part.strip()).strip()
    if not text:
        raise RuntimeError("PDF has no extractable text; use OCR and ingest-derived")
    return text


def ingest_pdf(root: str | Path, path: str | Path) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    with _acquire_command_write_lock(paths.root, operation="ingest-pdf"):
        return _ingest_pdf_unlocked(paths.root, path)


def _ingest_pdf_unlocked(root: str | Path, path: str | Path) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    pdf_path = Path(path).expanduser()
    if not pdf_path.is_file():
        raise RuntimeError(f"Expected PDF file: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise RuntimeError("PDF ingest expects .pdf")

    text = _extract_pdf_text(pdf_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        extracted = Path(tmpdir) / f"{pdf_path.stem}.pdf.txt"
        extracted.write_text(f"# {pdf_path.stem}\n\n{text}\n", encoding="utf-8")
        return _ingest_derived_unlocked(
            paths.root,
            original=pdf_path,
            text=extracted,
            workflow="pdf-text",
        )


def _tesseract_command(env: dict[str, str] | None = None) -> str | None:
    environment = env if env is not None else os.environ
    configured = environment.get("KB_TESSERACT_CMD", "").strip()
    if configured:
        return configured
    search_path = environment.get("PATH") if env is not None else None
    return shutil.which("tesseract", path=search_path)


def ocr_check(env: dict[str, str] | None = None) -> dict[str, object]:
    command = _tesseract_command(env)
    if not command:
        raise RuntimeError("KB_TESSERACT_CMD or tesseract command is required")
    return {"command": "set"}


def _run_tesseract(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Tesseract OCR failed") from exc
    if completed.returncode != 0:
        raise RuntimeError("Tesseract OCR failed")
    return completed.stdout


def ingest_ocr(
    root: str | Path,
    path: str | Path,
    *,
    lang: str = "eng",
    runner: object | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    with _acquire_command_write_lock(paths.root, operation="ingest-ocr"):
        return _ingest_ocr_unlocked(
            paths.root,
            path,
            lang=lang,
            runner=runner,
            env=env,
        )


def _ingest_ocr_unlocked(
    root: str | Path,
    path: str | Path,
    *,
    lang: str = "eng",
    runner: object | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    image_path = Path(path).expanduser()
    if not image_path.is_file():
        raise RuntimeError(f"Expected OCR input file: {image_path}")
    if image_path.suffix.lower() not in OCR_EXTENSIONS:
        raise RuntimeError(f"Unsupported OCR extension: {image_path.suffix.lower()}")
    language = re.sub(r"[^A-Za-z0-9_+.-]+", "", lang).strip()
    if not language:
        raise RuntimeError("OCR language must not be empty")

    command = _tesseract_command(env)
    if not command:
        raise RuntimeError("KB_TESSERACT_CMD or tesseract command is required")
    args = [command, str(image_path), "stdout", "-l", language]
    text = runner(args) if runner is not None else _run_tesseract(args)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Tesseract OCR produced no text")

    paths = KnowledgeBasePaths(Path(root))
    with tempfile.TemporaryDirectory() as tmpdir:
        extracted = Path(tmpdir) / f"{image_path.stem}.ocr.txt"
        extracted.write_text(f"# {image_path.stem}\n\n{text.strip()}\n", encoding="utf-8")
        return _ingest_derived_unlocked(
            paths.root,
            original=image_path,
            text=extracted,
            workflow="ocr",
        )


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def review_source(
    root: str | Path,
    source_id: str,
    *,
    status: str,
    reviewer: str = "",
    note: str = "",
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="review-source"):
        return _review_source_unlocked(
            paths.root,
            source_id,
            status=status,
            reviewer=reviewer,
            note=note,
        )


def _review_source_unlocked(
    root: str | Path,
    source_id: str,
    *,
    status: str,
    reviewer: str = "",
    note: str = "",
) -> dict[str, str]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    normalized_status = status.strip().casefold().replace("-", "_")
    if normalized_status not in SOURCE_REVIEW_STATUSES:
        raise RuntimeError(
            "status must be one of: " + ", ".join(sorted(SOURCE_REVIEW_STATUSES))
        )
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise RuntimeError(f"Invalid source_id: {source_id}")

    card_path = paths.sources / f"{source_id}.md"
    if not card_path.is_file():
        raise RuntimeError(f"Missing source card: {source_id}")
    metadata = read_source_card(card_path)
    _validate_source_for_index(paths, metadata)
    metadata = dict(metadata)
    metadata["review_status"] = normalized_status
    metadata["reviewed_at"] = datetime.now().replace(microsecond=0).isoformat()
    if reviewer:
        metadata["reviewer"] = _single_line(_redact_secret(reviewer))
    if note:
        metadata["review_note"] = _single_line(_redact_secret(note))

    write_source_card(paths, metadata)
    upsert_source_map(paths, metadata)
    _append_event(
        paths,
        "review-source",
        f"{source_id} status={normalized_status} reviewer={metadata.get('reviewer', '')}",
    )
    return metadata


def _delete_indexed_source(paths: KnowledgeBasePaths, source_id: str) -> None:
    initialize_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE source_id = ?", (source_id,)
            ).fetchone()
            connection.execute("DELETE FROM chunk_fts WHERE source_id = ?", (source_id,))
            vector_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'chunk_vectors'"
            ).fetchone()
            if vector_table is not None:
                try:
                    connection.execute(
                        "DELETE FROM chunk_vectors WHERE source_id = ?", (source_id,)
                    )
                except sqlite3.Error as exc:
                    raise RuntimeError(
                        "Invalid vector index; run vector-rebuild"
                    ) from exc
            if row is not None:
                connection.execute("DELETE FROM documents WHERE id = ?", (int(row[0]),))


def _stable_wiki_references_source(
    paths: KnowledgeBasePaths, source_id: str
) -> list[str]:
    labels: list[str] = []
    for page in _stable_wiki_files(paths):
        try:
            body = _wiki_page_body(page.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Cannot read wiki page: {page}") from exc
        if source_id in set(SOURCE_ID_RE.findall(body)):
            labels.append(_path_label(paths, page))
    return labels


def _validate_vector_index_schema(paths: KnowledgeBasePaths) -> None:
    if not paths.database.is_file():
        return
    required_columns = {
        "model",
        "source_id",
        "raw_path",
        "title",
        "chunk_index",
        "content",
        "dimensions",
        "vector_json",
    }
    try:
        with closing(sqlite3.connect(paths.database)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'chunk_vectors'"
            ).fetchone()
            if table is None:
                return
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(chunk_vectors)")
            }
    except sqlite3.Error as exc:
        raise RuntimeError("Invalid vector index; run vector-rebuild") from exc
    if not required_columns.issubset(columns):
        raise RuntimeError("Invalid vector index; run vector-rebuild")


def refresh_source(root: str | Path, source_id: str) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="refresh-source"):
        return _refresh_source_unlocked(paths.root, source_id)


def _refresh_source_unlocked(root: str | Path, source_id: str) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    _validate_vector_index_schema(paths)
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise RuntimeError(f"Invalid source_id: {source_id}")

    old_card_path = paths.sources / f"{source_id}.md"
    if not old_card_path.is_file():
        raise RuntimeError(f"Missing source card: {source_id}")
    old_metadata = read_source_card(old_card_path)
    raw_path = _resolve_raw_path(paths, old_metadata)
    if not raw_path.is_file():
        raise RuntimeError(f"Missing raw file for {source_id}: {old_metadata['raw_path']}")
    if raw_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"Unsupported extension: {raw_path.suffix.lower()}")
    data = raw_path.read_bytes()
    new_source_id, new_sha256 = source_id_and_sha256(data)

    if new_source_id != source_id:
        references = _stable_wiki_references_source(paths, source_id)
        if references:
            raise RuntimeError(
                f"Cannot refresh {source_id}; stable wiki pages still cite it: "
                + ", ".join(references)
            )

    metadata = source_metadata(paths, raw_path, data)
    for field in OPTIONAL_SOURCE_CARD_FIELDS:
        if old_metadata.get(field):
            metadata[field] = old_metadata[field]
    if new_source_id == source_id:
        metadata["imported_at"] = old_metadata["imported_at"]
    if metadata["sha256"] != new_sha256:
        raise RuntimeError("Source refresh hash mismatch")

    write_source_card(paths, metadata)
    upsert_source_map(paths, metadata)
    chunk_count = _index_source(paths, metadata)
    if new_source_id != source_id:
        remove_source_map_entry(paths, source_id)
        _delete_indexed_source(paths, source_id)
        if old_card_path.exists():
            old_card_path.unlink()
    _append_event(
        paths,
        "refresh-source",
        f"{source_id} -> {new_source_id} {metadata['raw_path']} ({chunk_count} chunks)",
    )
    return {
        "old_source_id": source_id,
        "source_id": new_source_id,
        "raw_path": metadata["raw_path"],
        "chunks": chunk_count,
        "changed": new_source_id != source_id,
    }


def _inbox_label(paths: KnowledgeBasePaths, path: Path) -> str:
    return f"inbox/{path.relative_to(paths.inbox).as_posix()}"


def _sorted_inbox_files(paths: KnowledgeBasePaths) -> list[Path]:
    inbox_root = paths.inbox.resolve()
    files: list[Path] = []
    entries = sorted(
        paths.inbox.rglob("*"),
        key=lambda path: path.relative_to(paths.inbox).as_posix(),
    )
    for entry in entries:
        label = _inbox_label(paths, entry)
        try:
            resolved = entry.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"Inbox path cannot be resolved: {label}") from exc
        try:
            resolved.relative_to(inbox_root)
        except ValueError:
            raise RuntimeError(f"Inbox path escapes inbox: {label}") from None

        if entry.is_dir():
            continue
        if not entry.is_file():
            raise RuntimeError(f"Expected inbox file: {label}")
        suffix = entry.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise RuntimeError(f"Unsupported inbox file: {label} ({suffix})")
        files.append(entry)
    return files


def ingest_inbox(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    inbox_files = _sorted_inbox_files(paths) if paths.inbox.exists() else []
    with _acquire_command_write_lock(paths.root, operation="ingest-inbox"):
        return _ingest_inbox_unlocked(paths.root, inbox_files=inbox_files)


def _ingest_inbox_unlocked(
    root: str | Path, inbox_files: list[Path] | None = None
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    if inbox_files is None:
        inbox_files = _sorted_inbox_files(paths) if paths.inbox.exists() else []
    _init_repository_unlocked(paths.root)
    ingested: list[dict[str, str]] = []
    for source in inbox_files:
        metadata = dict(_ingest_file_unlocked(paths.root, source))
        metadata["inbox_path"] = _inbox_label(paths, source)
        ingested.append(metadata)
    return {"root": str(paths.root), "count": len(ingested), "ingested": ingested}


def rebuild_index(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    with _acquire_command_write_lock(paths.root, operation="rebuild-index"):
        return _rebuild_index_unlocked(paths.root)


def _rebuild_index_unlocked(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _init_repository_unlocked(paths.root)
    initialize_database(paths.database)

    source_cards: list[tuple[dict[str, str], list[str]]] = []
    for card in sorted(paths.sources.glob("src-*.md")):
        metadata = read_source_card(card)
        raw_path = _validate_source_for_index(paths, metadata)
        chunks = chunk_text(extract_text(raw_path))
        source_cards.append((metadata, chunks))

    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("DELETE FROM chunk_fts")
            connection.execute("DELETE FROM chunks")
            connection.execute("DELETE FROM documents")
            for metadata, chunks in source_cards:
                _write_index_rows(connection, metadata, chunks)

    count = len(source_cards)
    _append_event(paths, "index", f"rebuilt index from {count} sources")
    return {"root": str(paths.root), "sources": count}


def _fts_query(query: str) -> str:
    return " ".join(re.findall(r"[\w]+", query, flags=re.UNICODE))


def search(root: str | Path, query: str, limit: int = 10) -> list[dict[str, str]]:
    if limit < 1:
        return []

    fts_query = _fts_query(query)
    if not fts_query:
        return []

    paths = KnowledgeBasePaths(Path(root))
    initialize_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        try:
            rows = connection.execute(
                """
                SELECT f.source_id,
                       d.raw_path,
                       d.title,
                       snippet(chunk_fts, 0, '', '', '...', 18) AS snippet
                FROM chunk_fts AS f
                JOIN documents AS d ON d.id = f.document_id
                WHERE chunk_fts MATCH ?
                ORDER BY bm25(chunk_fts)
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Invalid search query") from exc

    return [
        {"source_id": row[0], "raw_path": row[1], "title": row[2], "snippet": row[3]}
        for row in rows
    ]


def _source_chunk_records(paths: KnowledgeBasePaths) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for card in sorted(paths.sources.glob("src-*.md")):
        metadata = read_source_card(card)
        raw_path = _validate_source_for_index(paths, metadata)
        for chunk_index, content in enumerate(chunk_text(extract_text(raw_path))):
            records.append(
                {
                    "source_id": metadata["source_id"],
                    "raw_path": metadata["raw_path"],
                    "title": metadata["title"],
                    "chunk_index": chunk_index,
                    "content": content,
                }
            )
    return records


def _ensure_vector_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunk_vectors (
            id INTEGER PRIMARY KEY,
            model TEXT NOT NULL,
            source_id TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            title TEXT,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(model, source_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_vectors_model
            ON chunk_vectors(model);
        """
    )


def _validate_vectors(
    records: list[dict[str, object]], vectors: list[list[float]]
) -> int:
    if len(records) != len(vectors):
        raise RuntimeError("Embedding response count mismatch")
    dimensions = len(vectors[0]) if vectors else 0
    if dimensions < 1:
        raise RuntimeError("Embedding vectors must not be empty")
    for vector in vectors:
        if len(vector) != dimensions:
            raise RuntimeError("Embedding vectors must have consistent dimensions")
        if not all(isinstance(value, (int, float)) for value in vector):
            raise RuntimeError("Embedding vectors must be numeric")
    return dimensions


def vector_rebuild(
    root: str | Path,
    *,
    client: object | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    config = load_embedding_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="vector-rebuild"):
        return _vector_rebuild_unlocked(
            paths.root, client=client, env=env, config=config
        )


def _vector_rebuild_unlocked(
    root: str | Path,
    *,
    client: object | None = None,
    env: dict[str, str] | None = None,
    config: object | None = None,
) -> dict[str, object]:
    if config is None:
        config = load_embedding_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    records = _source_chunk_records(paths)
    if not records:
        raise RuntimeError("No source chunks to embed")
    embedding_client = client if client is not None else OpenAICompatibleEmbeddingClient(config)
    vectors = embedding_client.embed([str(record["content"]) for record in records])
    dimensions = _validate_vectors(records, vectors)

    initialize_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        with connection:
            _ensure_vector_tables(connection)
            connection.execute("DELETE FROM chunk_vectors WHERE model = ?", (config.model,))
            for record, vector in zip(records, vectors):
                connection.execute(
                    """
                    INSERT INTO chunk_vectors
                        (model, source_id, raw_path, title, chunk_index, content,
                         dimensions, vector_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config.model,
                        record["source_id"],
                        record["raw_path"],
                        record["title"],
                        record["chunk_index"],
                        record["content"],
                        dimensions,
                        json.dumps([float(value) for value in vector]),
                    ),
                )
    _append_event(
        paths,
        "vector-rebuild",
        f"model={config.model} chunks={len(records)} dimensions={dimensions}",
    )
    return {
        "root": str(paths.root),
        "model": config.model,
        "chunks": len(records),
        "dimensions": dimensions,
    }


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def semantic_search(
    root: str | Path,
    query: str,
    limit: int = 10,
    *,
    client: object | None = None,
    env: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    config = load_embedding_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    if not paths.database.is_file():
        return []
    with closing(sqlite3.connect(paths.database)) as connection:
        try:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'chunk_vectors'"
            ).fetchone()
            if table is None:
                return []
            rows = connection.execute(
                """
                SELECT source_id, raw_path, title, chunk_index, content,
                       dimensions, vector_json
                FROM chunk_vectors
                WHERE model = ?
                """,
                (config.model,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("Invalid vector index; run vector-rebuild") from exc
    if not rows:
        return []

    current_chunks = {
        (str(record["source_id"]), int(record["chunk_index"])): record
        for record in _source_chunk_records(paths)
    }
    filtered_rows = []
    for row in rows:
        key = (str(row[0]), int(row[3]))
        record = current_chunks.get(key)
        if record is None or str(row[4]) != str(record["content"]):
            continue
        filtered_rows.append((row, record))
    if not filtered_rows:
        return []

    embedding_client = client if client is not None else OpenAICompatibleEmbeddingClient(config)
    query_vectors = embedding_client.embed([query])
    dimensions = _validate_vectors([{"content": query}], query_vectors)
    query_vector = query_vectors[0]

    results: list[dict[str, object]] = []
    try:
        for row, record in filtered_rows:
            loaded = json.loads(row[6])
            if not isinstance(loaded, list):
                raise ValueError("vector_json is not a list")
            vector = [float(value) for value in loaded]
            if int(row[5]) != dimensions:
                continue
            score = _cosine_similarity(query_vector, vector)
            results.append(
                {
                    "source_id": record["source_id"],
                    "raw_path": record["raw_path"],
                    "title": record["title"],
                    "chunk_index": int(row[3]),
                    "snippet": record["content"],
                    "score": score,
                }
            )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid vector index; run vector-rebuild") from exc
    results.sort(key=lambda item: (-float(item["score"]), str(item["source_id"])))
    return results[:limit]


def hybrid_search(
    root: str | Path,
    query: str,
    limit: int = 10,
    *,
    client: object | None = None,
    env: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    current_source_ids = {
        str(record["source_id"]) for record in _source_chunk_records(paths)
    }
    fts_results = [
        result
        for result in search(root, query, limit)
        if str(result["source_id"]) in current_source_ids
    ]
    semantic_results = semantic_search(root, query, limit, client=client, env=env)
    combined: dict[tuple[str, str], dict[str, object]] = {}
    for rank, result in enumerate(fts_results, start=1):
        key = (str(result["source_id"]), str(result["snippet"]))
        combined[key] = dict(result, score=1.0 / rank, retrieval="fts")
    for rank, result in enumerate(semantic_results, start=1):
        key = (str(result["source_id"]), str(result["snippet"]))
        current = combined.get(key)
        score = (1.0 / rank) + float(result["score"])
        if current is None:
            combined[key] = dict(result, score=score, retrieval="semantic")
        else:
            current["score"] = float(current["score"]) + score
            current["retrieval"] = "hybrid"
    results = list(combined.values())
    results.sort(key=lambda item: (-float(item["score"]), str(item["source_id"])))
    return results[:limit]


def eval_search_repository(
    root: str | Path,
    benchmark: str | Path,
    *,
    limit: int = 10,
    client: object | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    from .eval_search import eval_search

    return eval_search(root, benchmark, limit=limit, client=client, env=env)


def capture_candidate(
    root: str | Path,
    *,
    type: str,
    text: str,
    event_date: str,
    privacy: str,
    confidence: str,
    value_reason: str,
    suggested_source_type: str,
) -> dict[str, object]:
    from .memory_candidates import capture

    return capture(
        root,
        type=type,
        text=text,
        event_date=event_date,
        privacy=privacy,
        confidence=confidence,
        value_reason=value_reason,
        suggested_source_type=suggested_source_type,
    )


def review_candidate(
    root: str | Path, candidate_id: str, *, status: str
) -> dict[str, object]:
    from .memory_candidates import review

    return review(root, candidate_id, status=status)


def publish_memory(
    root: str | Path, candidate_id: str, *, confirm: bool
) -> dict[str, object]:
    from .memory_candidates import publish

    return publish(root, candidate_id, confirm=confirm)


def suggest_topics(
    root: str | Path, *, source_ids: list[str] | None = None
) -> dict[str, object]:
    from .topic_suggestions import _create_topic_suggestions_unlocked

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(root, operation="suggest-topics"):
        return _create_topic_suggestions_unlocked(paths, source_ids=source_ids)


def daily_workflow(
    root: str | Path, *, workflow_date: str | None = None
) -> dict[str, object]:
    from .daily_workflows import create_daily_workflow_plan

    return create_daily_workflow_plan(root, workflow_date=workflow_date)


def benchmark_add(
    root: str | Path,
    *,
    query: str,
    expected_source_ids: list[str],
    expected_wiki_paths: list[str] | None = None,
    expected_quotes: list[str] | None = None,
    privacy: str = "public",
    confirmed: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    from .retrieval_benchmark import _add_benchmark_case_unlocked

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(root, operation="benchmark-add"):
        return _add_benchmark_case_unlocked(
            paths,
            query,
            expected_source_ids,
            expected_wiki_paths=expected_wiki_paths,
            expected_quotes=expected_quotes,
            privacy=privacy,
            confirmed=confirmed,
            env_source=os.environ if env is None else env,
        )


def exobrain_check_repository(root: str | Path) -> dict[str, object]:
    from .exobrain_check import exobrain_check

    return exobrain_check(root)


def embedding_check(env: dict[str, str] | None = None) -> dict[str, object]:
    config = load_embedding_config(env if env is not None else os.environ)
    return {
        "base_url": "set",
        "model": "set",
        "api_key": "set" if config.api_key else "unset",
        "timeout_seconds": config.timeout_seconds,
    }


def _question_terms(question: str) -> list[str]:
    terms = [
        token.casefold()
        for token in re.findall(r"[\w]+", question, flags=re.UNICODE)
        if token.casefold() not in ANSWER_STOPWORDS
    ]
    return terms


def _paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line.strip())
            continue
        if current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _matches_question(text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    haystack = text.casefold()
    return all(term in haystack for term in terms)


def _unique_source_ids(evidence: list[dict[str, object]]) -> list[str]:
    source_ids: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        source_id = str(item["source_id"])
        if source_id not in seen:
            source_ids.append(source_id)
            seen.add(source_id)
    return source_ids


def _unsupported_answer(question: str, reason: str) -> dict[str, object]:
    return {
        "question": question,
        "status": "unsupported",
        "uncertainty": "high",
        "answer": "No local evidence found.",
        "source_ids": [],
        "evidence": [],
        "reason": reason,
    }


def _stable_wiki_evidence(
    paths: KnowledgeBasePaths, question: str, limit: int
) -> list[dict[str, object]]:
    terms = _question_terms(question)
    existing_source_ids = {
        card.stem for card in paths.sources.glob("src-*.md") if card.is_file()
    }
    evidence: list[dict[str, object]] = []
    for page in sorted(paths.wiki.rglob("*.md"), key=lambda path: _path_label(paths, path)):
        if not _is_under(page, paths.wiki):
            raise RuntimeError(f"Wiki page outside wiki: {_path_label(paths, page)}")
        if _is_under(page, paths.wiki / "_drafts") or not page.is_file():
            continue
        try:
            text = page.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Cannot read wiki page: {page}") from exc
        for paragraph in _paragraphs(text):
            if paragraph.lstrip().startswith("#") or not _matches_question(paragraph, terms):
                continue
            source_ids = [
                source_id
                for source_id in SOURCE_ID_RE.findall(paragraph)
                if source_id in existing_source_ids
            ]
            for source_id in source_ids:
                evidence.append(
                    {
                        "kind": "wiki",
                        "source_id": source_id,
                        "path": _path_label(paths, page),
                        "quote": paragraph,
                    }
                )
                if len(evidence) >= limit:
                    return evidence
    return evidence


def _source_chunk_evidence(
    paths: KnowledgeBasePaths, question: str, limit: int
) -> tuple[list[dict[str, object]], str | None]:
    terms = _question_terms(question)
    query = " ".join(terms) if terms else question
    try:
        context_pack = build_context_pack(paths.root, query, limit=limit)
    except EmptyContextError as exc:
        return [], exc.reason
    return [
        {
            "kind": "source",
            "source_id": chunk.source_id,
            "raw_path": chunk.raw_path,
            "title": chunk.title,
            "chunk_index": chunk.chunk_index,
            "quote": chunk.content,
        }
        for chunk in context_pack.chunks
    ], None


def answer(root: str | Path, question: str, limit: int = 5) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    if limit < 1:
        return _unsupported_answer(question, "limit")

    wiki_evidence = _stable_wiki_evidence(paths, question, limit)
    if wiki_evidence:
        return {
            "question": question,
            "status": "answered",
            "uncertainty": "low",
            "answer": str(wiki_evidence[0]["quote"]),
            "source_ids": _unique_source_ids(wiki_evidence),
            "evidence": wiki_evidence,
            "reason": "wiki",
        }

    source_evidence, reason = _source_chunk_evidence(paths, question, limit)
    if not source_evidence:
        return _unsupported_answer(question, reason or "no-matching-chunks")
    return {
        "question": question,
        "status": "answered",
        "uncertainty": "medium",
        "answer": str(source_evidence[0]["quote"]),
        "source_ids": _unique_source_ids(source_evidence),
        "evidence": source_evidence,
        "reason": "source",
    }


def _path_label(paths: KnowledgeBasePaths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()


def _require_initialized_repository(paths: KnowledgeBasePaths) -> None:
    if not paths.root.is_dir():
        raise RuntimeError("Knowledge base is not initialized")

    required_directories = (
        paths.raw,
        paths.inbox,
        paths.wiki,
        paths.sources,
        paths.meta,
        paths.db,
    )
    for directory in required_directories:
        if not directory.is_dir():
            raise RuntimeError("Knowledge base is not initialized")

    _validate_canonical_directories(paths.root, required_directories)

    for path in (paths.meta / "log.md", paths.meta / "review-queue.md"):
        if not path.is_file():
            raise RuntimeError("Knowledge base is not initialized")


def _source_ids_for_context(context_sources: list[dict[str, str]]) -> list[str]:
    return [source["source_id"] for source in context_sources]


def _chunk_ids_for_context(context_chunks: list[dict[str, object]]) -> list[str]:
    return [
        f"{chunk['source_id']}#{chunk['chunk_index']}" for chunk in context_chunks
    ]


def _contains_secret(api_key: str | None, *values: object) -> bool:
    if not api_key:
        return False
    return any(api_key in str(value) for value in values)


def _llm_provider(config: object) -> str | None:
    base_url = str(getattr(config, "base_url", "")).casefold()
    model = str(getattr(config, "model", "")).casefold()
    if "deepseek" in base_url or "deepseek" in model:
        return "deepseek"
    return None


def _llm_draft_attempts(config: object) -> int:
    return 2 if _llm_provider(config) == "deepseek" else 1


def _claim_issue_types_for_response(
    paths: KnowledgeBasePaths,
    title: str,
    context_pack: object,
    parsed: object,
    target: str | None = None,
) -> list[str]:
    metadata: dict[str, object] = {
        "draft_id": "draft-preview",
        "title": title,
        "query": context_pack.query,
        "created_at": draft_timestamp(),
        "model": "preview",
        "prompt_hash": "0" * 64,
        "context_sources": _source_ids_for_context(context_pack.context_sources),
        "context_chunks": _chunk_ids_for_context(context_pack.context_chunks),
        "claims": parsed.claims,
    }
    issues = validate_draft_content(
        paths,
        metadata,
        parsed.body,
        "wiki/_drafts/preview.md",
        target=target,
        draft_text=parsed.body,
    )
    return sorted({str(issue.get("type", "invalid-claims")) for issue in issues})


def _read_audit_snapshots(paths: KnowledgeBasePaths) -> tuple[Path, Path, str, str]:
    log_path = paths.meta / "log.md"
    review_path = paths.meta / "review-queue.md"
    try:
        log_before = log_path.read_text(encoding="utf-8")
        review_before = review_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Cannot read audit metadata") from exc
    return log_path, review_path, log_before, review_before


def _restore_audit_file_if_changed(path: Path, content: str) -> None:
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        path.write_text(content, encoding="utf-8")
        return
    if current != content:
        path.write_text(content, encoding="utf-8")


def _append_draft_audit(
    paths: KnowledgeBasePaths, draft_path: Path, metadata: dict[str, object]
) -> None:
    draft_label = _path_label(paths, draft_path)
    source_ids = ", ".join(str(source) for source in metadata["context_sources"])
    timestamp = datetime.now().replace(microsecond=0).isoformat()

    with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
        log.write(
            f"- {timestamp} [llm-draft] {draft_label} "
            f"query={metadata['query']} model={metadata['model']} "
            f"prompt_hash={metadata['prompt_hash']}\n"
        )

    with (paths.meta / "review-queue.md").open("a", encoding="utf-8") as review:
        review.write(
            f"- [ ] Review {draft_label} title={metadata['title']} "
            f"query={metadata['query']} sources={source_ids}; "
            f"validate: python -m kb validate-draft --root {paths.root} {draft_label}\n"
        )


def _append_draft_archive_audit(
    paths: KnowledgeBasePaths, original_path: Path, archive_path: Path
) -> None:
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    original_label = _redact_secret(_path_label(paths, original_path))
    archive_label = _redact_secret(_path_label(paths, archive_path))
    with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
        log.write(
            f"- {timestamp} [archive-draft] {original_label} -> {archive_label}\n"
        )


def _append_failed_compile_audit(
    paths: KnowledgeBasePaths,
    draft_path: Path,
    archive_path: Path,
    issues: list[dict[str, str]],
) -> None:
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    draft_label = _redact_secret(_path_label(paths, draft_path))
    archive_label = _redact_secret(_path_label(paths, archive_path))
    issue_types = ", ".join(
        sorted({str(issue.get("type", "unknown")) for issue in issues})
    )
    with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
        log.write(
            f"- {timestamp} [compile-page-failed] {draft_label} -> "
            f"{archive_label} issues={_redact_secret(issue_types)}\n"
        )

    with (paths.meta / "review-queue.md").open("a", encoding="utf-8") as review:
        review.write(
            f"- [ ] Review failed compile draft {archive_label} "
            f"issues={_redact_secret(issue_types)}; "
            f"validate: python -m kb validate-draft --root {paths.root} "
            f"{archive_label}\n"
        )


def _archive_draft_path(paths: KnowledgeBasePaths, draft_path: Path) -> Path | None:
    if not draft_path.exists():
        return None
    if not draft_path.is_file():
        raise RuntimeError(f"Existing draft path is not a file: {draft_path}")

    drafts_dir = _drafts_dir(paths)
    try:
        draft_path.resolve().relative_to(drafts_dir.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Draft path is outside drafts: {draft_path}") from exc

    timestamp = datetime.now().replace(microsecond=0).strftime("%Y%m%dT%H%M%S")
    archive_dir = drafts_dir / "_archive" / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    for index in range(1000):
        suffix = "" if index == 0 else f".{index}"
        archive_path = archive_dir / f"{draft_path.stem}{suffix}{draft_path.suffix}"
        if archive_path.exists():
            continue
        log_path, review_path, log_before, review_before = _read_audit_snapshots(paths)
        try:
            os.replace(draft_path, archive_path)
            _append_draft_archive_audit(paths, draft_path, archive_path)
        except Exception:
            if archive_path.exists() and not draft_path.exists():
                os.replace(archive_path, draft_path)
            _remove_empty_parents_until(archive_dir, drafts_dir)
            _restore_audit_file_if_changed(log_path, log_before)
            _restore_audit_file_if_changed(review_path, review_before)
            raise
        return archive_path

    raise RuntimeError("No available draft archive path")


def _archive_existing_draft_for_title(
    paths: KnowledgeBasePaths, title: str
) -> Path | None:
    draft_path = draft_path_for_title(paths, title)
    return _archive_draft_path(paths, draft_path)


def _restore_archived_draft(
    paths: KnowledgeBasePaths, title: str, archived_draft: Path
) -> None:
    draft_path = draft_path_for_title(paths, title)
    if archived_draft.exists() and not draft_path.exists():
        os.replace(archived_draft, draft_path)
        _remove_empty_parents_until(archived_draft.parent, _drafts_dir(paths))


def _redact_secret(value: object) -> str:
    text = str(value)
    for name in ("KB_LLM_API_KEY", "KB_EMBEDDING_API_KEY"):
        api_key = os.environ.get(name)
        if api_key:
            text = text.replace(api_key, "[redacted]")
    return text


def _metadata_source_ids(metadata: dict[str, object]) -> list[str]:
    raw_sources = metadata.get("context_sources", [])
    if isinstance(raw_sources, list):
        values = raw_sources
    elif isinstance(raw_sources, str):
        values = [part.strip() for part in raw_sources.split(",")]
    else:
        values = []
    return [str(source) for source in values if str(source)]


def _append_publish_audit(
    paths: KnowledgeBasePaths,
    draft_path: Path,
    target_path: Path,
    metadata: dict[str, object],
) -> None:
    draft_label = _redact_secret(_path_label(paths, draft_path))
    target_label = _redact_secret(_path_label(paths, target_path))
    source_ids = ", ".join(_metadata_source_ids(metadata))
    timestamp = datetime.now().replace(microsecond=0).isoformat()

    with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
        log.write(
            f"- {timestamp} [publish-draft] {draft_label} -> {target_label} "
            f"sources={source_ids}\n"
        )


def _append_repair_audit(
    paths: KnowledgeBasePaths,
    draft_path: Path,
    repaired_path: Path,
    metadata: dict[str, object],
) -> None:
    draft_label = _redact_secret(_path_label(paths, draft_path))
    repaired_label = _redact_secret(_path_label(paths, repaired_path))
    source_ids = ", ".join(_metadata_source_ids(metadata))
    timestamp = datetime.now().replace(microsecond=0).isoformat()

    with (paths.meta / "log.md").open("a", encoding="utf-8") as log:
        log.write(
            f"- {timestamp} [repair-draft] {draft_label} -> {repaired_label} "
            f"sources={source_ids}\n"
        )

    with (paths.meta / "review-queue.md").open("a", encoding="utf-8") as review:
        review.write(
            f"- [ ] Review repaired draft {repaired_label}; "
            f"validate: python -m kb validate-draft --root {paths.root} "
            f"{repaired_label}\n"
        )


def llm_draft(
    root: str | Path,
    query: str,
    title: str,
    client: object | None = None,
    env: dict[str, str] | None = None,
    context_limit: int = 5,
    archive_existing: bool = False,
) -> dict[str, object]:
    config = load_llm_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="llm-draft"):
        return _llm_draft_unlocked(
            paths.root,
            query,
            title,
            client=client,
            env=env,
            context_limit=context_limit,
            archive_existing=archive_existing,
            config=config,
        )


def _llm_draft_unlocked(
    root: str | Path,
    query: str,
    title: str,
    client: object | None = None,
    env: dict[str, str] | None = None,
    context_limit: int = 5,
    archive_existing: bool = False,
    config: object | None = None,
) -> dict[str, object]:
    if config is None:
        config = load_llm_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    draft_path = draft_path_for_title(paths, title)
    draft_label = _path_label(paths, draft_path)
    validation_command = (
        f"python -m kb validate-draft --root {paths.root} {draft_label}"
    )
    if _contains_secret(config.api_key, paths.root, draft_label, validation_command):
        raise RuntimeError("LLM draft contains configured secret")
    if draft_path.exists():
        if not draft_path.is_file():
            raise RuntimeError(f"Existing draft path is not a file: {draft_path}")
        if not archive_existing:
            raise RuntimeError("Draft already exists")

    try:
        context_pack = build_context_pack(paths.root, query, limit=context_limit)
    except EmptyContextError as exc:
        raise RuntimeError(str(exc)) from None

    log_path, review_path, log_before, review_before = _read_audit_snapshots(paths)

    completion_client = client if client is not None else OpenAICompatibleClient(config)
    provider = _llm_provider(config)
    retry_feedback: list[str] = []
    messages = build_prompt_messages(
        title, query, context_pack, provider=provider, retry_feedback=retry_feedback
    )
    content = ""
    parsed = None
    attempts = _llm_draft_attempts(config)
    for attempt_index in range(attempts):
        content = completion_client.complete(messages)
        if _contains_secret(config.api_key, content):
            raise RuntimeError("LLM draft contains configured secret")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM response content was empty")
        try:
            parsed = parse_llm_draft_response(content)
        except RuntimeError:
            raise
        if _contains_secret(config.api_key, parsed.body, parsed.claims):
            raise RuntimeError("LLM draft contains configured secret")
        issue_types = _claim_issue_types_for_response(
            paths, title, context_pack, parsed
        )
        if provider == "deepseek" and issue_types:
            if attempt_index < attempts - 1:
                retry_feedback = issue_types
                messages = build_prompt_messages(
                    title,
                    query,
                    context_pack,
                    provider=provider,
                    retry_feedback=retry_feedback,
                )
                continue
            raise RuntimeError("LLM draft failed local contract")
        break
    if parsed is None:
        raise RuntimeError("Invalid LLM draft response")

    context_sources = _source_ids_for_context(context_pack.context_sources)
    context_chunks = _chunk_ids_for_context(context_pack.context_chunks)
    message_hash = prompt_hash(messages)
    metadata: dict[str, object] = {
        "draft_id": draft_id(),
        "title": title,
        "query": query,
        "created_at": draft_timestamp(),
        "model": config.model,
        "prompt_hash": message_hash,
        "context_sources": context_sources,
        "context_chunks": context_chunks,
        "claims": parsed.claims,
    }
    if _contains_secret(config.api_key, metadata, parsed.body, parsed.claims):
        raise RuntimeError("LLM draft contains configured secret")

    drafts_dir = draft_path.parent
    drafts_existed = drafts_dir.exists()
    audit_started = False
    draft_created = False
    archived_draft: Path | None = None

    try:
        if archive_existing:
            archived_draft = _archive_existing_draft_for_title(paths, title)
        draft_path = write_draft(paths, metadata, parsed.body)
        draft_created = True
        audit_started = True
        _append_draft_audit(paths, draft_path, metadata)
    except Exception:
        if draft_created and draft_path.exists():
            draft_path.unlink()
        if archived_draft is not None:
            _restore_archived_draft(paths, title, archived_draft)
        if audit_started or archived_draft is not None:
            _restore_audit_file_if_changed(log_path, log_before)
            _restore_audit_file_if_changed(review_path, review_before)
        if not drafts_existed and drafts_dir.is_dir():
            try:
                drafts_dir.rmdir()
            except OSError:
                pass
        raise
    return {
        "path": str(draft_path),
        "metadata": metadata,
        "archived_draft": str(archived_draft) if archived_draft is not None else "",
    }


def llm_contract_check(
    response_path: str | Path | None = None,
    *,
    title: str,
    target: str | None = None,
    root: str | Path | None = None,
    query: str | None = None,
    client: object | None = None,
    env: dict[str, str] | None = None,
    context_limit: int = 5,
) -> dict[str, str]:
    if response_path is not None:
        try:
            content = Path(response_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"status": "read_failure"}
        return {"status": llm_draft_contract_status(content, title, target=target)}

    if root is None or not query:
        raise RuntimeError("--response or both --root and --query are required")

    config = load_llm_config(env if env is not None else os.environ)
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    try:
        context_pack = build_context_pack(paths.root, query, limit=context_limit)
        messages = build_prompt_messages(
            title, query, context_pack, provider=_llm_provider(config)
        )
    except EmptyContextError as exc:
        return {"status": "read_failure"}

    completion_client = client if client is not None else OpenAICompatibleClient(config)
    try:
        content = completion_client.complete(messages)
    except RuntimeError:
        return {"status": "read_failure"}
    if not isinstance(content, str):
        return {"status": "invalid_claim_shape"}
    status = llm_draft_contract_status(content, title, target=target)
    if status != "pass":
        return {"status": status}

    parsed = parse_llm_draft_response(content)
    if _claim_issue_types_for_response(paths, title, context_pack, parsed, target):
        return {"status": "invalid_claim_shape"}
    return {"status": "pass"}


def validate_draft_file(
    root: str | Path, draft_path: str | Path, target: str | None = None
) -> list[dict[str, str]]:
    paths = KnowledgeBasePaths(Path(root))
    draft = Path(draft_path)
    if not draft.is_absolute():
        draft = paths.root / draft
    return validate_draft(paths, draft, target=target)


def repair_draft_file(
    root: str | Path, draft_path: str | Path, target: str | None = None
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="repair-draft"):
        return _repair_draft_file_unlocked(paths.root, draft_path, target=target)


def _repair_draft_file_unlocked(
    root: str | Path, draft_path: str | Path, target: str | None = None
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    draft = Path(draft_path)
    if not draft.is_absolute():
        draft = paths.root / draft
    draft_label = _path_label(paths, draft)
    safe_draft_label = _redact_secret(draft_label)
    api_key = os.environ.get("KB_LLM_API_KEY")
    if _contains_secret(api_key, paths.root, draft, draft_label, target):
        return {
            "path": "",
            "issues": [{"type": "secret-leak", "path": safe_draft_label}],
        }

    try:
        drafts_dir = _drafts_dir(paths)
    except RuntimeError:
        return {
            "path": "",
            "issues": [
                {"type": "draft-path-outside-drafts", "path": safe_draft_label}
            ],
        }
    if not _is_under(draft, drafts_dir):
        return {
            "path": "",
            "issues": [
                {"type": "draft-path-outside-drafts", "path": safe_draft_label}
            ],
        }

    try:
        metadata, body = read_draft(draft)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Cannot read draft: {draft}") from exc
    if _contains_secret(api_key, metadata, body):
        return {
            "path": "",
            "issues": [{"type": "secret-leak", "path": safe_draft_label}],
        }

    try:
        repaired_metadata, repaired_body = repair_draft_content(paths, metadata, target)
    except DraftRepairError as exc:
        return {"path": "", "issues": [{"type": str(exc), "path": safe_draft_label}]}

    if _contains_secret(api_key, repaired_metadata, repaired_body):
        return {
            "path": "",
            "issues": [{"type": "secret-leak", "path": safe_draft_label}],
        }

    repaired_path = next_repaired_draft_path(paths, draft)
    drafts_existed = repaired_path.parent.exists()
    repaired_created = False
    try:
        write_draft_at_path(paths, repaired_path, repaired_metadata, repaired_body)
        repaired_created = True
        validation_issues = validate_draft(paths, repaired_path, target=target)
        if validation_issues:
            repaired_path.unlink()
            if not drafts_existed and repaired_path.parent.is_dir():
                try:
                    repaired_path.parent.rmdir()
                except OSError:
                    pass
            return {"path": "", "issues": validation_issues}

        log_path, review_path, log_before, review_before = _read_audit_snapshots(paths)
        try:
            _append_repair_audit(paths, draft, repaired_path, repaired_metadata)
        except Exception:
            if repaired_path.exists():
                repaired_path.unlink()
            _restore_audit_file_if_changed(log_path, log_before)
            _restore_audit_file_if_changed(review_path, review_before)
            raise
    except Exception:
        if repaired_created and repaired_path.exists():
            repaired_path.unlink()
        if not drafts_existed and repaired_path.parent.is_dir():
            try:
                repaired_path.parent.rmdir()
            except OSError:
                pass
        raise

    return {"path": str(repaired_path), "issues": []}


def compile_page(
    root: str | Path,
    query: str,
    title: str,
    target: str | None = None,
    client: object | None = None,
    env: dict[str, str] | None = None,
    context_limit: int = 5,
    archive_existing: bool = False,
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="compile-page"):
        return _compile_page_unlocked(
            paths.root,
            query,
            title,
            target=target,
            client=client,
            env=env,
            context_limit=context_limit,
            archive_existing=archive_existing,
        )


def _compile_page_unlocked(
    root: str | Path,
    query: str,
    title: str,
    target: str | None = None,
    client: object | None = None,
    env: dict[str, str] | None = None,
    context_limit: int = 5,
    archive_existing: bool = False,
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    log_path, review_path, log_before, review_before = _read_audit_snapshots(paths)
    draft_result = _llm_draft_unlocked(
        root,
        query,
        title,
        client=client,
        env=env,
        context_limit=context_limit,
        archive_existing=archive_existing,
    )
    draft_path = str(draft_result["path"])
    archived_draft = str(draft_result.get("archived_draft", ""))
    publish_target = target or title
    failed_draft = ""
    generated_draft_paths = [draft_path]

    validation_issues = validate_draft_file(root, draft_path, target=publish_target)
    publish_path = draft_path
    repaired_path = ""
    if validation_issues:
        try:
            if repair_draft_file is _ORIGINAL_REPAIR_DRAFT_FILE:
                repair_result = _repair_draft_file_unlocked(
                    root, draft_path, target=publish_target
                )
            else:
                repair_result = repair_draft_file(
                    root, draft_path, target=publish_target
                )
        except Exception as exc:
            _finalize_failed_compile(
                paths,
                title,
                generated_draft_paths,
                archived_draft,
                log_path,
                review_path,
                log_before,
                review_before,
                [{"type": "compile-exception", "error": str(exc)}],
                primary_draft_path=draft_path,
            )
            raise
        if repair_result["issues"]:
            failed_draft = _finalize_failed_compile(
                paths,
                title,
                generated_draft_paths,
                archived_draft,
                log_path,
                review_path,
                log_before,
                review_before,
                repair_result["issues"],
                primary_draft_path=draft_path,
            )
            return {
                "target": "",
                "draft": draft_path,
                "repaired_draft": "",
                "archived_draft": "",
                "failed_draft": failed_draft,
                "issues": repair_result["issues"],
            }
        repaired_path = str(repair_result["path"])
        publish_path = repaired_path
        generated_draft_paths.append(repaired_path)

    try:
        if publish_draft is _ORIGINAL_PUBLISH_DRAFT:
            publish_result = _publish_draft_unlocked(root, publish_path, publish_target)
        else:
            publish_result = publish_draft(root, publish_path, publish_target)
    except Exception as exc:
        _finalize_failed_compile(
            paths,
            title,
            generated_draft_paths,
            archived_draft,
            log_path,
            review_path,
            log_before,
            review_before,
            [{"type": "compile-exception", "error": str(exc)}],
            primary_draft_path=publish_path,
        )
        raise
    if publish_result["issues"]:
        failed_draft = _finalize_failed_compile(
            paths,
            title,
            generated_draft_paths,
            archived_draft,
            log_path,
            review_path,
            log_before,
            review_before,
            publish_result["issues"],
            primary_draft_path=publish_path,
        )
        return {
            "target": "",
            "draft": draft_path,
            "repaired_draft": repaired_path,
            "archived_draft": "",
            "failed_draft": failed_draft,
            "issues": publish_result["issues"],
        }

    return {
        "target": str(publish_result["target"]),
        "draft": draft_path,
        "repaired_draft": repaired_path,
        "archived_draft": archived_draft,
        "failed_draft": failed_draft,
        "issues": [],
    }


def _lint_issue_key(issue: dict[str, str]) -> str:
    return json.dumps(issue, sort_keys=True, separators=(",", ":"))


def _publish_lint_issue(issue: dict[str, str]) -> dict[str, str]:
    result = {
        "type": "publish-lint-issue",
        "lint_type": str(issue.get("type", "")),
    }
    for key, value in issue.items():
        if key != "type":
            result[key] = str(value)
    return result


def _remove_empty_dirs(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _remove_empty_parents_until(start: Path, stop: Path) -> None:
    current = start
    stop_resolved = stop.resolve()
    while True:
        try:
            current_resolved = current.resolve()
            current_resolved.relative_to(stop_resolved)
        except ValueError:
            return
        if current_resolved == stop_resolved:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _finalize_failed_compile(
    paths: KnowledgeBasePaths,
    title: str,
    draft_paths: list[str],
    archived_draft: str,
    log_path: Path,
    review_path: Path,
    log_before: str,
    review_before: str,
    issues: list[dict[str, str]],
    primary_draft_path: str | None = None,
) -> str:
    archives: dict[Path, Path] = {}
    for draft_path in draft_paths:
        if not draft_path:
            continue
        draft = Path(draft_path)
        if draft in archives:
            continue
        archived = _archive_draft_path(paths, draft)
        if archived is not None:
            archives[draft] = archived
    if archived_draft:
        _restore_archived_draft(paths, title, Path(archived_draft))
    _restore_audit_file_if_changed(log_path, log_before)
    _restore_audit_file_if_changed(review_path, review_before)
    primary = Path(primary_draft_path) if primary_draft_path else None
    failed_archive = archives.get(primary) if primary is not None else None
    if failed_archive is None and archives:
        failed_archive = next(iter(archives.values()))
    if failed_archive is None:
        return ""
    failed_draft = primary if primary is not None else next(iter(archives))
    _append_failed_compile_audit(paths, failed_draft, failed_archive, issues)
    return str(failed_archive)


def _new_parent_dirs_for_target(target_path: Path, stop: Path) -> list[Path]:
    directories: list[Path] = []
    current = target_path.parent
    stop_resolved = stop.resolve()
    while not current.exists():
        try:
            current.resolve().relative_to(stop_resolved)
        except ValueError:
            break
        directories.append(current)
        current = current.parent
    return directories


def _rollback_target(
    target_path: Path,
    existed: bool,
    previous: bytes | None,
    created_dirs: list[Path] | None = None,
) -> None:
    try:
        if existed:
            if previous is None:
                raise RuntimeError("Missing previous target bytes")
            target_path.write_bytes(previous)
        elif target_path.exists():
            target_path.unlink()
        if created_dirs:
            _remove_empty_dirs(created_dirs)
    except Exception as exc:
        raise RuntimeError("Publish rollback failed") from exc


def publish_draft(
    root: str | Path, draft_path: str | Path, target: str
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="publish-draft"):
        return _publish_draft_unlocked(paths.root, draft_path, target)


def _publish_draft_unlocked(
    root: str | Path, draft_path: str | Path, target: str
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    draft = Path(draft_path)
    if not draft.is_absolute():
        draft = paths.root / draft
    draft_label = _path_label(paths, draft)

    try:
        target_path = target_path_for_title(paths, target)
    except ValueError:
        return {
            "target": "",
            "issues": [
                {"type": "unsafe-target", "path": draft_label, "target": target}
            ],
        }

    pre_lint_issues = lint_repository(paths.root)
    validation_issues = validate_draft(paths, draft, target=target)
    if validation_issues:
        return {"target": str(target_path), "issues": validation_issues}

    metadata, body = read_draft(draft)
    target_existed = target_path.exists()
    previous_target = target_path.read_bytes() if target_existed else None
    created_target_dirs = _new_parent_dirs_for_target(target_path, paths.wiki)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=target_path.parent,
    )
    temp_path: Path | None = Path(temp_name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(body.encode("utf-8"))
        os.replace(temp_path, target_path)
        replaced = True
        temp_path = None

        post_lint_issues = lint_repository(paths.root)
        status_repository(paths.root)
        pre_issue_set = {_lint_issue_key(issue) for issue in pre_lint_issues}
        new_issues = [
            issue
            for issue in post_lint_issues
            if _lint_issue_key(issue) not in pre_issue_set
        ]
        if new_issues:
            _rollback_target(
                target_path, target_existed, previous_target, created_target_dirs
            )
            replaced = False
            return {
                "target": str(target_path),
                "issues": [_publish_lint_issue(issue) for issue in new_issues],
            }

        log_path = paths.meta / "log.md"
        try:
            log_before = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("Cannot read audit metadata") from exc
        try:
            _append_publish_audit(paths, draft, target_path, metadata)
        except Exception:
            _restore_audit_file_if_changed(log_path, log_before)
            raise
    except Exception:
        if replaced:
            _rollback_target(
                target_path, target_existed, previous_target, created_target_dirs
            )
        raise
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return {"target": str(target_path), "issues": []}


_ORIGINAL_REPAIR_DRAFT_FILE = repair_draft_file
_ORIGINAL_PUBLISH_DRAFT = publish_draft


def _wiki_page_body(text: str) -> str:
    lines = text.splitlines()
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            index += 1
        if index >= len(lines):
            index = 1
            while index < len(lines) and (
                not lines[index].strip()
                or FRONT_MATTER_FIELD_RE.match(lines[index].strip())
            ):
                index += 1
            return "\n".join(lines[index:])
        index += 1

    return "\n".join(lines[index:])


def _wiki_page_has_body(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _source_cards(paths: KnowledgeBasePaths) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for card in sorted(paths.sources.glob("src-*.md")):
        try:
            cards.append(read_source_card(card))
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Cannot read source card: {card}") from exc
    return cards


def lint_repository(root: str | Path) -> list[dict[str, str]]:
    paths = KnowledgeBasePaths(Path(root))
    issues: list[dict[str, str]] = []
    source_cards = _source_cards(paths)
    source_ids = {card["source_id"] for card in source_cards}

    for card in source_cards:
        raw_path = _resolve_raw_path(paths, card)
        if not raw_path.is_file():
            issues.append(
                {
                    "type": "missing-raw-file",
                    "source_id": card["source_id"],
                    "raw_path": card["raw_path"],
                }
            )

    for page in _stable_wiki_files(paths):
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Cannot read wiki page: {page}") from exc
        page_label = _path_label(paths, page)
        body = _wiki_page_body(text)
        referenced_source_ids = set(SOURCE_ID_RE.findall(body))

        if _wiki_page_has_body(body) and not referenced_source_ids:
            issues.append({"type": "missing-citation", "path": page_label})

        for source_id in sorted(referenced_source_ids - source_ids):
            issues.append(
                {
                    "type": "invalid-source-reference",
                    "path": page_label,
                    "source_id": source_id,
                }
            )

        for target in WIKI_LINK_RE.findall(body):
            target = target.strip()
            if target and not wiki_link_exists(paths, target):
                issues.append(
                    {
                        "type": "broken-wiki-link",
                        "path": page_label,
                        "target": target,
                    }
                )

    return issues


def _database_status_counts(paths: KnowledgeBasePaths) -> tuple[int, int]:
    if not paths.database.is_file():
        return 0, 0

    try:
        with closing(sqlite3.connect(paths.database)) as connection:
            indexed_documents = int(
                connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )
            chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot read database status: {paths.database}") from exc
    return indexed_documents, chunks


def status_repository(root: str | Path) -> dict[str, int]:
    paths = KnowledgeBasePaths(Path(root))
    indexed_documents, chunks = _database_status_counts(paths)
    issues = lint_repository(paths.root)
    return {
        "raw_files": sum(1 for path in paths.raw.rglob("*") if path.is_file()),
        "source_cards": len(list(paths.sources.glob("src-*.md"))),
        "wiki_pages": len(_stable_wiki_files(paths)),
        "indexed_documents": indexed_documents,
        "chunks": chunks,
        "lint_issues": len(issues),
    }


def schema_check_repository(root: str | Path, *, write_manifest: bool = False):
    if write_manifest:
        paths = KnowledgeBasePaths(Path(root))
        if not paths.root.exists() or not paths.root.is_dir():
            return _schema_check_repository_unlocked(
                paths.root, write_manifest=write_manifest
            )
        with _acquire_command_write_lock(paths.root, operation="schema-check"):
            return _schema_check_repository_unlocked(
                paths.root, write_manifest=write_manifest
            )
    return _schema_check_repository_unlocked(root, write_manifest=write_manifest)


def _schema_check_repository_unlocked(
    root: str | Path, *, write_manifest: bool = False
):
    from .schema_check import schema_check

    return schema_check(root, write_manifest=write_manifest)


def lock_check_repository(root: str | Path):
    from .locks import lock_check

    return lock_check(root)


def recover_lock_repository(root: str | Path, *, manual_confirm: bool = False):
    from .locks import recover_lock

    return recover_lock(root, manual_confirm=manual_confirm)


def backup_repository(
    root: str | Path, output: str | Path, *, allow_dirty: bool = False
):
    from .backup import create_backup

    return create_backup(root, output, allow_dirty=allow_dirty)


def restore_repository(
    backup: str | Path, root: str | Path, *, replace: bool = False
):
    from .restore import restore_backup

    return restore_backup(backup, root, replace=replace)


def migrate_check_repository(source: str | Path, restored: str | Path):
    from .migrate import migrate_check

    return migrate_check(source, restored)


def gateway_check_repository(root: str | Path):
    from .gateway import gateway_check

    return gateway_check(root)


def product_console_repository(root: str | Path) -> dict[str, object]:
    from .product_console import product_console_state

    return product_console_state(root)


def trust_report_repository(root: str | Path) -> dict[str, object]:
    from .trust_report import trust_report

    return trust_report(root)


def llm_preflight_repository(
    root: str | Path,
    query: str,
    title: str,
    *,
    target: str | None = None,
    context_limit: int = 5,
    write_audit: bool = False,
    privacy_confirmation: object | None = None,
    client: object | None = None,
    env: dict[str, str] | None = None,
    offline: bool = False,
):
    from .llm_preflight import llm_preflight

    return llm_preflight(
        root,
        query,
        title,
        target=target,
        context_limit=context_limit,
        write_audit=write_audit,
        privacy_confirmation=privacy_confirmation,
        client=client,
        env=env,
        offline=offline,
    )


def _governance_issue(
    severity: str, issue_type: str, path: str, reason: str, **fields: str
) -> dict[str, str]:
    issue = {
        "severity": severity,
        "type": issue_type,
        "path": _redact_secret(path),
        "reason": _redact_secret(reason),
    }
    for key, value in sorted(fields.items()):
        issue[key] = _redact_secret(str(value))
    return issue


def _governance_issue_key(issue: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        issue.get("type", ""),
        issue.get("path", ""),
        issue.get("source_id", ""),
        issue.get("target", ""),
        issue.get("reason", ""),
    )


def _lint_issue_path(issue: dict[str, str]) -> str:
    if issue.get("path"):
        return issue["path"]
    if issue.get("source_id"):
        return f"sources/{issue['source_id']}.md"
    if issue.get("raw_path"):
        return issue["raw_path"]
    return "."


def _governance_lint_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    blocking: list[dict[str, str]] = []
    for issue in issues:
        fields = {
            key: value
            for key, value in issue.items()
            if key not in {"type", "path"}
        }
        blocking.append(
            _governance_issue(
                "blocking",
                issue["type"],
                _lint_issue_path(issue),
                f"lint issue {issue['type']}",
                **fields,
            )
        )
    return blocking


def _stable_wiki_files(paths: KnowledgeBasePaths) -> list[Path]:
    pages: list[Path] = []
    wiki_root = paths.wiki.resolve()
    drafts_root = (paths.wiki / "_drafts").resolve()
    for page in sorted(paths.wiki.rglob("*.md"), key=lambda path: _path_label(paths, path)):
        try:
            resolved = page.resolve()
            resolved.relative_to(wiki_root)
        except ValueError:
            raise RuntimeError(f"Wiki page outside wiki: {page}") from None
        try:
            resolved.relative_to(drafts_root)
            continue
        except ValueError:
            pass
        if page.is_file():
            pages.append(page)
    return pages


def _read_wiki_pages(paths: KnowledgeBasePaths) -> list[tuple[Path, str, str]]:
    pages: list[tuple[Path, str, str]] = []
    for page in _stable_wiki_files(paths):
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Cannot read wiki page: {page}") from exc
        pages.append((page, _path_label(paths, page), _wiki_page_body(text)))
    return pages


def _wiki_page_title(page: Path, body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            return stripped.lstrip("#").strip() or page.stem
    return page.stem.replace("-", " ").replace("_", " ").strip() or page.name


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).casefold()


def _duplicate_wiki_title_issues(
    pages: list[tuple[Path, str, str]]
) -> list[dict[str, str]]:
    by_title: dict[str, list[tuple[str, str]]] = {}
    for page, label, body in pages:
        title = _wiki_page_title(page, body)
        by_title.setdefault(_normalized_title(title), []).append((label, title))

    issues: list[dict[str, str]] = []
    for duplicates in by_title.values():
        if len(duplicates) < 2:
            continue
        labels = [label for label, _title in duplicates]
        title = duplicates[0][1]
        for label in labels:
            others = ", ".join(other for other in labels if other != label)
            issues.append(
                _governance_issue(
                    "advisory",
                    "duplicate-wiki-title",
                    label,
                    f"title {title!r} also appears in {others}",
                )
            )
    return issues


def _possible_conflict_issues(
    pages: list[tuple[Path, str, str]]
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for _page, label, body in pages:
        lowered = body.casefold()
        for marker in CONFLICT_MARKERS:
            if marker.casefold() in lowered:
                issues.append(
                    _governance_issue(
                        "advisory",
                        "possible-conflict-marker",
                        label,
                        f"explicit conflict marker {marker!r} appears in page body",
                    )
                )
                break
    return issues


def _orphan_wiki_page_issues(
    paths: KnowledgeBasePaths, pages: list[tuple[Path, str, str]]
) -> list[dict[str, str]]:
    page_labels = {_path_label(paths, page) for page, _label, _body in pages}
    inbound: set[str] = set()
    for _page, _label, body in pages:
        for target in WIKI_LINK_RE.findall(body):
            cleaned = target.strip()
            if not cleaned:
                continue
            try:
                target_path = target_path_for_title(paths, cleaned)
            except ValueError:
                continue
            target_label = _path_label(paths, target_path)
            if target_label in page_labels:
                inbound.add(target_label)

    issues: list[dict[str, str]] = []
    for _page, label, body in pages:
        if not _wiki_page_has_body(body):
            continue
        if label not in inbound:
            issues.append(
                _governance_issue(
                    "advisory",
                    "orphan-wiki-page",
                    label,
                    "stable wiki page has no inbound wiki links",
                )
            )
    return issues


def _source_review_status_issues(
    source_cards: list[dict[str, str]]
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for card in source_cards:
        status = card.get("review_status", "").casefold()
        reviewed_at = card.get("reviewed_at", "")
        if status in BLOCKING_SOURCE_STATUSES:
            source_id = card["source_id"]
            issues.append(
                _governance_issue(
                    "blocking",
                    "source-review-blocking",
                    f"sources/{source_id}.md",
                    f"source review status is {status}",
                    source_id=source_id,
                )
            )
            continue
        if reviewed_at or status in REVIEWED_SOURCE_STATUSES:
            continue
        source_id = card["source_id"]
        issues.append(
            _governance_issue(
                "advisory",
                "source-review-missing",
                f"sources/{source_id}.md",
                "source card has no reviewed_at or accepted review_status",
                source_id=source_id,
            )
        )
    return issues


def _stale_source_issues(
    paths: KnowledgeBasePaths,
    source_cards: list[dict[str, str]],
    pages: list[tuple[Path, str, str]],
) -> tuple[list[dict[str, str]], set[str]]:
    issues: list[dict[str, str]] = []
    stale_source_ids: set[str] = set()
    for card in source_cards:
        source_id = card["source_id"]
        raw_path = _resolve_raw_path(paths, card)
        if not raw_path.is_file():
            continue
        try:
            current_source_id, current_sha256 = source_id_and_sha256(raw_path.read_bytes())
        except OSError as exc:
            raise RuntimeError(f"Cannot read raw file: {raw_path}") from exc
        if current_source_id == source_id and current_sha256 == card["sha256"]:
            continue
        stale_source_ids.add(source_id)
        issues.append(
            _governance_issue(
                "blocking",
                "stale-source-card",
                f"sources/{source_id}.md",
                "raw file content hash changed since source card was written",
                raw_path=card["raw_path"],
                source_id=source_id,
            )
        )

    for _page, label, body in pages:
        for source_id in sorted(set(SOURCE_ID_RE.findall(body)) & stale_source_ids):
            issues.append(
                _governance_issue(
                    "blocking",
                    "stale-wiki-page",
                    label,
                    f"page references stale source {source_id}",
                    source_id=source_id,
                )
            )
    return issues, stale_source_ids


def _open_review_item_issues(paths: KnowledgeBasePaths) -> list[dict[str, str]]:
    review_path = paths.meta / "review-queue.md"
    try:
        lines = review_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Cannot read review queue: {review_path}") from exc

    issues: list[dict[str, str]] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("- [ ]"):
            continue
        reason = _redact_secret(stripped)
        if len(reason) > 160:
            reason = reason[:157] + "..."
        issues.append(
            _governance_issue(
                "advisory",
                "open-review-item",
                "meta/review-queue.md",
                reason,
                line=str(index),
            )
        )
    return issues


def _format_governance_section(title: str, issues: list[dict[str, str]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not issues:
        lines.extend(["(none)", ""])
        return lines
    for issue in sorted(issues, key=_governance_issue_key):
        fields = [
            f"{key}={issue[key]}"
            for key in sorted(issue)
            if key not in {"severity", "type"}
        ]
        lines.append(f"- [{issue['type']}] " + " ".join(fields))
    lines.append("")
    return lines


def _render_quality_report(
    status: dict[str, int],
    blocking: list[dict[str, str]],
    advisory: list[dict[str, str]],
) -> str:
    lines = [
        "# Quality Report",
        "",
        "## Summary",
        "",
        f"- blocking: {len(blocking)}",
        f"- advisory: {len(advisory)}",
    ]
    for key in sorted(status):
        lines.append(f"- {key}: {status[key]}")
    lines.append("")
    lines.extend(_format_governance_section("Blocking Issues", blocking))
    lines.extend(_format_governance_section("Advisory Issues", advisory))
    lines.extend(
        [
            "## Scope",
            "",
            "- Blocking issues must be fixed before claiming the knowledge base is clean.",
            "- Advisory issues are visible quality drift signals for review.",
            "- Semantic contradictions are surfaced only when pages contain explicit conflict markers.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            temp_file.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def govern(root: str | Path) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)
    with _acquire_command_write_lock(paths.root, operation="govern"):
        return _govern_unlocked(paths.root)


def _govern_unlocked(root: str | Path) -> dict[str, object]:
    from .governance import analyze_governance, render_quality_report

    paths = KnowledgeBasePaths(Path(root))
    _require_initialized_repository(paths)

    analysis = analyze_governance(paths.root)
    report = render_quality_report(analysis)
    report_path = paths.meta / "quality-report.md"
    _atomic_write_text(report_path, report)
    return {
        "path": str(report_path),
        "blocking": analysis["blocking"],
        "advisory": analysis["advisory"],
        "blocking_count": analysis["blocking_count"],
        "advisory_count": analysis["advisory_count"],
        "status": analysis["status"],
    }


def _profile_registry_path(config_dir: str | Path | None) -> Path:
    from .product_paths import registry_path

    return registry_path(config_dir=config_dir)


def profile_add(
    config_dir: str | Path | None,
    *,
    name: str,
    root: str | Path,
    kind: str,
) -> dict[str, object]:
    from .profile_registry import add_or_update_profile

    return add_or_update_profile(
        _profile_registry_path(config_dir),
        name=name,
        root=root,
        kind=kind,
    )


def profile_list(config_dir: str | Path | None) -> list[dict[str, object]]:
    from .profile_registry import list_profiles

    return list_profiles(_profile_registry_path(config_dir))


def llm_check(env: dict[str, str] | None = None) -> dict[str, object]:
    config = load_llm_config(env if env is not None else os.environ)
    return {
        "base_url": "set",
        "model": "set",
        "api_key": "set" if config.api_key else "unset",
        "timeout_seconds": config.timeout_seconds,
        "response_format": config.response_format or "unset",
        "max_tokens": config.max_tokens or "unset",
        "thinking": config.thinking or "unset",
        "reasoning_effort": config.reasoning_effort or "unset",
    }
