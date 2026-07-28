"""Safe write entrypoint for retrieval benchmark cases."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from .eval_search import (
    BARE_BEARER_TOKEN_RE,
    PRIVATE_KEY_MARKER_RE,
    PRIVATE_PRIVACY_LEVELS,
    SOURCE_ID_RE,
    VALID_PRIVACY_LEVELS,
)
from .locks import acquire_write_lock
from .paths import KnowledgeBasePaths
from .redaction import redact_text
from .sources import read_source_card


BENCHMARK_RELATIVE_PATH = "meta/evals/retrieval-benchmark.jsonl"
SK_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[^\s,;)\]}'\"]*")


def _benchmark_path(paths: KnowledgeBasePaths) -> Path:
    return paths.root / "meta" / "evals" / "retrieval-benchmark.jsonl"


def _validate_benchmark_storage_for_write(paths: KnowledgeBasePaths) -> Path:
    evals_dir = paths.root / "meta" / "evals"
    benchmark = _benchmark_path(paths)
    root_resolved = paths.root.resolve()

    if paths.meta.is_symlink() or not paths.meta.is_dir():
        raise RuntimeError("Benchmark metadata parent must be a directory")
    if evals_dir.is_symlink():
        raise RuntimeError("Benchmark directory must not be a symlink")
    if evals_dir.exists() and not evals_dir.is_dir():
        raise RuntimeError("Benchmark parent must be a directory")
    if not evals_dir.exists():
        evals_dir.mkdir(parents=False, exist_ok=False)
    if evals_dir.is_symlink() or not evals_dir.is_dir():
        raise RuntimeError("Benchmark parent must be a directory")
    if benchmark.is_symlink():
        raise RuntimeError("Benchmark path must not be a symlink")
    if benchmark.exists() and not benchmark.is_file():
        raise RuntimeError("Benchmark path must be a file")

    try:
        evals_dir.resolve(strict=False).relative_to(root_resolved)
        benchmark.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError("Benchmark path escapes root") from exc

    return benchmark


def _require_initialized_repository(paths: KnowledgeBasePaths) -> None:
    required_directories = (
        paths.raw,
        paths.inbox,
        paths.wiki,
        paths.sources,
        paths.meta,
        paths.db,
    )
    if not paths.root.is_dir() or any(not path.is_dir() for path in required_directories):
        raise RuntimeError("Knowledge base is not initialized")
    for path in (paths.meta / "log.md", paths.meta / "review-queue.md"):
        if not path.is_file():
            raise RuntimeError("Knowledge base is not initialized")


def _contains_secret_shape(text: str, env: Mapping[str, str]) -> bool:
    if SK_PREFIX_RE.search(text):
        return True
    if PRIVATE_KEY_MARKER_RE.search(text):
        return True
    if BARE_BEARER_TOKEN_RE.search(text):
        return True
    if redact_text(text, env=dict(env)) != text:
        return True
    decoded = text.replace("\\n", "\n").replace("\\r", "\r")
    if SK_PREFIX_RE.search(decoded):
        return True
    if PRIVATE_KEY_MARKER_RE.search(decoded):
        return True
    if BARE_BEARER_TOKEN_RE.search(decoded):
        return True
    return redact_text(decoded, env=dict(env)) != decoded


def _validate_query(query: object, env: Mapping[str, str]) -> str:
    if not isinstance(query, str) or not query.strip():
        raise RuntimeError("Benchmark query must not be empty")
    cleaned = query.strip()
    if _contains_secret_shape(cleaned, env):
        raise RuntimeError("Benchmark query contains a secret-shaped value")
    return cleaned


def _validate_expected_sources(paths: KnowledgeBasePaths, values: Sequence[str]) -> list[str]:
    if not values:
        raise RuntimeError("Benchmark case must include at least one expected source id")
    source_ids: list[str] = []
    for value in values:
        source_id = str(value).strip()
        if not SOURCE_ID_RE.fullmatch(source_id):
            raise RuntimeError("Invalid expected source id")
        card = paths.sources / f"{source_id}.md"
        if card.is_symlink() or not card.is_file():
            raise RuntimeError(f"Expected source card does not exist: {source_id}")
        metadata = read_source_card(card)
        if metadata["source_id"] != source_id:
            raise RuntimeError(f"Expected source card is invalid: {source_id}")
        if source_id not in source_ids:
            source_ids.append(source_id)
    return source_ids


def _validate_wiki_path(paths: KnowledgeBasePaths, value: str) -> str:
    raw = str(value).strip()
    if "\\" in raw:
        raise RuntimeError("Invalid expected wiki path")
    if not raw or raw.startswith("/") or ":" in raw:
        raise RuntimeError("Invalid expected wiki path")
    if not raw.startswith("wiki/"):
        raise RuntimeError("Invalid expected wiki path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError("Invalid expected wiki path")
    if len(parts) >= 2 and parts[1] == "_drafts":
        raise RuntimeError("Invalid expected wiki path")

    target = paths.root / raw
    try:
        target_resolved = target.resolve(strict=False)
        target_resolved.relative_to(paths.wiki.resolve())
        target_resolved.relative_to((paths.wiki / "_drafts").resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("Invalid expected wiki path")
    try:
        target.resolve(strict=False).relative_to(paths.root.resolve())
    except ValueError as exc:
        raise RuntimeError("Invalid expected wiki path") from exc
    if target.is_symlink() or not target.is_file():
        raise RuntimeError("Expected wiki path does not exist")
    return raw


def _validate_wiki_paths(
    paths: KnowledgeBasePaths, values: Sequence[str] | None
) -> list[str]:
    wiki_paths: list[str] = []
    for value in values or []:
        wiki_path = _validate_wiki_path(paths, value)
        if wiki_path not in wiki_paths:
            wiki_paths.append(wiki_path)
    return wiki_paths


def _validate_expected_quotes(
    values: Sequence[str] | None, env: Mapping[str, str]
) -> list[str]:
    quotes: list[str] = []
    for value in values or []:
        quote = str(value).strip()
        if not quote:
            raise RuntimeError("Expected quote must not be empty")
        if _contains_secret_shape(quote, env):
            raise RuntimeError("Expected quote contains a secret-shaped value")
        if quote not in quotes:
            quotes.append(quote)
    return quotes


def _validate_privacy(privacy: str, confirmed: bool) -> str:
    normalized = str(privacy).strip().casefold() or "public"
    if normalized not in VALID_PRIVACY_LEVELS:
        raise RuntimeError("Invalid benchmark privacy level")
    if normalized in PRIVATE_PRIVACY_LEVELS and not confirmed:
        raise RuntimeError("Private benchmark cases require explicit confirmation")
    return normalized


def _write_jsonl_atomic(paths: KnowledgeBasePaths, record: dict[str, object]) -> None:
    evals_dir = paths.root / "meta" / "evals"
    created_dirs = _missing_parent_dirs(evals_dir / "retrieval-benchmark.jsonl")
    path = _validate_benchmark_storage_for_write(paths)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"

    path = _validate_benchmark_storage_for_write(paths)
    content = existing + json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    succeeded = False
    try:
        try:
            temp_path.resolve(strict=True).relative_to(paths.root.resolve())
        except ValueError as exc:
            os.close(fd)
            fd = -1
            raise RuntimeError("Benchmark temp path escapes root") from exc
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            fd = -1
            temp_file.write(content)
        path = _validate_benchmark_storage_for_write(paths)
        os.replace(temp_path, path)
        succeeded = True
    finally:
        if fd != -1:
            os.close(fd)
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


def add_benchmark_case(
    root: str | Path,
    query: str,
    expected_source_ids: Sequence[str],
    expected_wiki_paths: Sequence[str] | None = None,
    expected_quotes: Sequence[str] | None = None,
    privacy: str = "public",
    confirmed: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    paths = KnowledgeBasePaths(Path(root))
    env_source = os.environ if env is None else env
    _require_initialized_repository(paths)
    with acquire_write_lock(paths.root, operation="benchmark-add"):
        return _add_benchmark_case_unlocked(
            paths,
            query,
            expected_source_ids,
            expected_wiki_paths=expected_wiki_paths,
            expected_quotes=expected_quotes,
            privacy=privacy,
            confirmed=confirmed,
            env_source=env_source,
        )


def _add_benchmark_case_unlocked(
    paths: KnowledgeBasePaths,
    query: str,
    expected_source_ids: Sequence[str],
    expected_wiki_paths: Sequence[str] | None = None,
    expected_quotes: Sequence[str] | None = None,
    privacy: str = "public",
    confirmed: bool = False,
    env_source: Mapping[str, str] | None = None,
) -> dict[str, object]:
    env_source = os.environ if env_source is None else env_source
    cleaned_query = _validate_query(query, env_source)
    source_ids = _validate_expected_sources(paths, expected_source_ids)
    wiki_paths = _validate_wiki_paths(paths, expected_wiki_paths)
    quotes = _validate_expected_quotes(expected_quotes, env_source)
    privacy_level = _validate_privacy(privacy, confirmed)

    record: dict[str, object] = {
        "query": cleaned_query,
        "expected_source_ids": source_ids,
        "privacy": privacy_level,
    }
    if wiki_paths:
        record["expected_wiki_paths"] = wiki_paths
    if quotes:
        record["expected_quotes"] = quotes
    if privacy_level in PRIVATE_PRIVACY_LEVELS:
        record["confirmed"] = True

    _write_jsonl_atomic(paths, record)
    benchmark = _benchmark_path(paths)

    return {
        "id": "retrieval-benchmark",
        "path": str(benchmark),
        "benchmark": BENCHMARK_RELATIVE_PATH,
        "expected_source_count": len(source_ids),
        "expected_wiki_path_count": len(wiki_paths),
        "expected_quote_count": len(quotes),
        "privacy": privacy_level,
    }


def redacted_cli_payload(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "benchmark": BENCHMARK_RELATIVE_PATH,
        "expected_source_count": int(result.get("expected_source_count", 0)),
        "expected_quote_count": int(result.get("expected_quote_count", 0)),
        "expected_wiki_path_count": int(result.get("expected_wiki_path_count", 0)),
        "id": str(result.get("id", "retrieval-benchmark")),
        "privacy": str(result.get("privacy", "public")),
    }
