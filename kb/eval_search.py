"""Read-only retrieval benchmark evaluation."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .commands import hybrid_search, search, semantic_search
from .embeddings import load_embedding_config
from .paths import KnowledgeBasePaths
from .redaction import redact_text
from .sources import read_source_card, source_id_and_sha256
from .text import chunk_text, extract_text


PUBLIC_PRIVACY_LEVELS = {"public", "personal"}
PRIVATE_PRIVACY_LEVELS = {"sensitive", "restricted"}
VALID_PRIVACY_LEVELS = PUBLIC_PRIVACY_LEVELS | PRIVATE_PRIVACY_LEVELS
SKIPPED_EXTERNAL_DEPENDENCY = "skipped_external_dependency"
SOURCE_ID_RE = re.compile(r"^src-[0-9a-f]{12}$")
REQUIRED_SEARCH_COLUMNS = {
    "documents": {"id", "source_id", "raw_path", "title", "sha256", "created_at"},
    "chunks": {
        "id",
        "document_id",
        "source_id",
        "chunk_index",
        "content",
        "created_at",
    },
    "events": {"id", "event_type", "message", "created_at"},
    "chunk_fts": {"content", "source_id", "document_id", "chunk_id"},
}
PRIVATE_KEY_MARKER_RE = re.compile(
    r"-----\s*(?:BEGIN|END)\s+[A-Z0-9 ]*PRIVATE KEY\s*-----",
    re.IGNORECASE,
)
BARE_BEARER_TOKEN_RE = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9][A-Za-z0-9._~+/=-]{5,}"
)
SK_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[^\s,;)\]}'\"]*")


def _redact_eval_text(value: object, env: Mapping[str, str] | None = None) -> str:
    redacted = redact_text(value, env=dict(env or {}))
    redacted = PRIVATE_KEY_MARKER_RE.sub("[redacted-private-key-marker]", redacted)
    return BARE_BEARER_TOKEN_RE.sub("Bearer [redacted-bearer-token]", redacted)


def _redact_value(value: object, env: Mapping[str, str] | None = None) -> object:
    env_dict = dict(env or {})
    if isinstance(value, dict):
        return {
            str(redact_text(key, env=env_dict)): _redact_value(item, env)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, env) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, env) for item in value]
    if isinstance(value, str):
        return _redact_eval_text(value, env=env_dict)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, os.PathLike):
        return _redact_eval_text(os.fspath(value), env=env_dict)
    return f"[non-json:{type(value).__name__}]"


def eval_search_json(result: dict[str, object], env: Mapping[str, str] | None = None) -> str:
    return json.dumps(_redact_value(result, env), ensure_ascii=False, sort_keys=True)


def _result(
    status: str,
    classification: str,
    summary: str,
    *,
    env: Mapping[str, str] | None = None,
    query_count: int = 0,
    fts_hit_rate: float = 0.0,
    semantic_hit_rate: float | None = None,
    hybrid_hit_rate: float | None = None,
    missing_sources: list[str] | None = None,
    missing_wiki_paths: list[str] | None = None,
    stale_warnings: list[dict[str, object]] | None = None,
    privacy_summary: dict[str, int] | None = None,
    modes: dict[str, dict[str, object]] | None = None,
    quote_support: dict[str, object] | None = None,
    duplicate_warnings: list[dict[str, object]] | None = None,
    low_quality_source_markers: list[dict[str, object]] | None = None,
    residual_risks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    resolved_privacy_summary = privacy_summary or {
        level: 0 for level in ("public", "personal", "sensitive", "restricted")
    }
    resolved_modes = modes or {
        "fts": {"classification": classification, "hit_rate": fts_hit_rate},
        "semantic": {
            "classification": SKIPPED_EXTERNAL_DEPENDENCY,
            "hit_rate": semantic_hit_rate,
        },
        "hybrid": {
            "classification": SKIPPED_EXTERNAL_DEPENDENCY,
            "hit_rate": hybrid_hit_rate,
        },
    }
    resolved_stale_warnings = stale_warnings or []
    resolved_quote_support = quote_support or {
        "authority": "metric_only",
        "expected_quote_count": 0,
        "supported_quote_count": 0,
        "missing_quote_count": 0,
        "hit_rate": 0.0,
    }
    result = {
        "status": status,
        "classification": classification,
        "summary": summary,
        "query_count": query_count,
        "fts_top_k_hit_rate": fts_hit_rate,
        "semantic_top_k_hit_rate": semantic_hit_rate,
        "hybrid_top_k_hit_rate": hybrid_hit_rate,
        "missing_expected_sources": sorted(set(missing_sources or [])),
        "missing_expected_wiki_paths": sorted(set(missing_wiki_paths or [])),
        "stale_index_warnings": resolved_stale_warnings,
        "privacy_summary": resolved_privacy_summary,
        "modes": resolved_modes,
        "benchmark_report": {
            "query_count": query_count,
            "modes": resolved_modes,
            "hit_rates": {
                "fts": fts_hit_rate,
                "semantic": semantic_hit_rate,
                "hybrid": hybrid_hit_rate,
            },
            "quote_support": resolved_quote_support,
            "duplicate_warnings": duplicate_warnings or [],
            "stale_index_warnings": resolved_stale_warnings,
            "low_quality_source_markers": low_quality_source_markers or [],
            "privacy_summary": resolved_privacy_summary,
            "residual_risks": residual_risks or [],
        },
    }
    return _redact_value(result, env)


def _resolve_benchmark_path(root: Path, benchmark: str | Path | None) -> Path:
    root_resolved = root.resolve()
    if benchmark is None:
        return root_resolved / "meta" / "evals" / "retrieval-benchmark.jsonl"
    candidate = Path(benchmark)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Benchmark path escapes root") from exc
    return resolved


def _as_string_list(record: dict[str, object], *keys: str) -> list[str]:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        raise ValueError(f"Invalid benchmark field: {key}")
    return []


def _safe_wiki_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or ":" in normalized:
        raise ValueError("Invalid expected wiki path")
    if not normalized.startswith("wiki/"):
        raise ValueError("Invalid expected wiki path")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid expected wiki path")
    return "/".join(parts)


def _contains_secret_shape(text: str, env: Mapping[str, str]) -> bool:
    env_dict = dict(env)
    if SK_PREFIX_RE.search(text):
        return True
    if _redact_eval_text(text, env=env_dict) != text:
        return True
    with_decoded_newlines = text.replace("\\n", "\n").replace("\\r", "\r")
    if SK_PREFIX_RE.search(with_decoded_newlines):
        return True
    return (
        _redact_eval_text(with_decoded_newlines, env=env_dict)
        != with_decoded_newlines
    )


def _confirmed_private_sample(record: dict[str, object]) -> bool:
    if (
        record.get("privacy_confirmed") is True
        or record.get("confirmed") is True
        or record.get("user_confirmed") is True
    ):
        return True
    confirmation = record.get("privacy_confirmation")
    if isinstance(confirmation, dict):
        return bool(confirmation.get("provider") or confirmation.get("summary"))
    return False


def _load_benchmark(
    benchmark_path: Path, env: Mapping[str, str]
) -> tuple[str | None, list[dict[str, object]], dict[str, int]]:
    if not benchmark_path.is_file():
        return "missing_benchmark_file", [], {
            "public": 0,
            "personal": 0,
            "sensitive": 0,
            "restricted": 0,
        }

    records: list[dict[str, object]] = []
    privacy_summary = {
        "public": 0,
        "personal": 0,
        "sensitive": 0,
        "restricted": 0,
    }
    try:
        lines = benchmark_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "invalid_benchmark", [], privacy_summary

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if _contains_secret_shape(line, env):
            return "secret_in_benchmark", [], privacy_summary
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            return "invalid_benchmark", [], privacy_summary
        if not isinstance(loaded, dict):
            return "invalid_benchmark", [], privacy_summary
        if _contains_secret_shape(json.dumps(loaded, ensure_ascii=False), env):
            return "secret_in_benchmark", [], privacy_summary

        query = loaded.get("query")
        if not isinstance(query, str) or not query.strip():
            return "invalid_benchmark", [], privacy_summary
        privacy = str(loaded.get("privacy", "public")).strip().casefold() or "public"
        if privacy not in VALID_PRIVACY_LEVELS:
            return "invalid_benchmark", [], privacy_summary
        if privacy in PRIVATE_PRIVACY_LEVELS and not _confirmed_private_sample(loaded):
            privacy_summary[privacy] += 1
            return "policy_confirmation_required", [], privacy_summary
        try:
            expected_sources = _as_string_list(
                loaded, "expected_source_ids", "expected_sources"
            )
            if not expected_sources:
                raise ValueError("Benchmark record must include expected source ids")
            if any(
                not SOURCE_ID_RE.fullmatch(source_id)
                for source_id in expected_sources
            ):
                raise ValueError("Invalid expected source id")
            expected_wiki_paths = [
                _safe_wiki_path(path)
                for path in _as_string_list(loaded, "expected_wiki_paths")
            ]
            expected_quotes = _as_string_list(loaded, "expected_quotes")
            if "expected_quotes" in loaded:
                raw_expected_quotes = loaded["expected_quotes"]
                if (
                    not isinstance(raw_expected_quotes, list)
                    or not raw_expected_quotes
                    or any(
                        not isinstance(quote, str) or not quote.strip()
                        for quote in raw_expected_quotes
                    )
                ):
                    raise ValueError("Invalid expected quote")
        except ValueError:
            return "invalid_benchmark", [], privacy_summary

        record = {
            "line_number": line_number,
            "query": query,
            "expected_sources": expected_sources,
            "expected_wiki_paths": expected_wiki_paths,
            "expected_quotes": expected_quotes,
            "privacy": privacy,
            "privacy_confirmation_supported": privacy
            not in PRIVATE_PRIVACY_LEVELS
            or (
                isinstance(loaded.get("privacy_confirmation"), dict)
                and bool(
                    loaded["privacy_confirmation"].get("provider")
                    or loaded["privacy_confirmation"].get("summary")
                )
            ),
        }
        records.append(record)
        privacy_summary[privacy] += 1

    if not records:
        return "invalid_benchmark", [], privacy_summary
    return None, records, privacy_summary


def _existing_source_ids(paths: KnowledgeBasePaths) -> set[str]:
    return {
        path.stem
        for path in paths.sources.glob("src-*.md")
        if path.is_file() and SOURCE_ID_RE.fullmatch(path.stem)
    }


def _readonly_database_connection(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)


def _search_index_available(paths: KnowledgeBasePaths) -> bool:
    if not paths.database.is_file():
        return False
    try:
        with closing(_readonly_database_connection(paths.database)) as connection:
            tables = {
                str(row[0]): str(row[1] or "")
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            columns = {
                table: {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                for table in REQUIRED_SEARCH_COLUMNS
            }
    except sqlite3.Error:
        return False
    return set(REQUIRED_SEARCH_COLUMNS).issubset(
        tables
    ) and "using fts5" in tables["chunk_fts"].casefold() and all(
        required.issubset(columns.get(table, set()))
        for table, required in REQUIRED_SEARCH_COLUMNS.items()
    )


def _indexed_source_ids(paths: KnowledgeBasePaths) -> set[str]:
    try:
        with closing(_readonly_database_connection(paths.database)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT source_id FROM chunk_fts"
            ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows if str(row[0])}


def _dedupe_dicts(items: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _duplicate_warnings(records: list[dict[str, object]]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    first_query_line: dict[str, int] = {}
    first_sources_line: dict[tuple[str, ...], int] = {}
    first_wiki_paths_line: dict[tuple[str, ...], int] = {}

    for record in records:
        line_number = int(record["line_number"])
        normalized_query = str(record["query"]).strip().casefold()
        first_line = first_query_line.setdefault(normalized_query, line_number)
        if first_line != line_number:
            warnings.append(
                {
                    "type": "duplicate_query",
                    "first_line": first_line,
                    "line_number": line_number,
                }
            )

        sources_key = tuple(sorted(str(source) for source in record["expected_sources"]))
        first_line = first_sources_line.setdefault(sources_key, line_number)
        if first_line != line_number:
            warnings.append(
                {
                    "type": "duplicate_expected_sources",
                    "first_line": first_line,
                    "line_number": line_number,
                }
            )

        wiki_paths_key = tuple(
            sorted(str(path) for path in record["expected_wiki_paths"])
        )
        if wiki_paths_key:
            first_line = first_wiki_paths_line.setdefault(wiki_paths_key, line_number)
            if first_line != line_number:
                warnings.append(
                    {
                        "type": "duplicate_expected_wiki_paths",
                        "first_line": first_line,
                        "line_number": line_number,
                    }
                )

    return warnings


def _duplicate_source_card_warnings(
    cards: dict[str, dict[str, str]]
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for field in ("sha256", "raw_path"):
        groups: dict[str, list[str]] = {}
        for source_id, card in cards.items():
            value = card.get(field, "").strip()
            if value:
                groups.setdefault(value, []).append(source_id)
        for source_ids in groups.values():
            if len(source_ids) < 2:
                continue
            warnings.append(
                {
                    "type": "duplicate_source_card",
                    "field": field,
                    "source_ids": sorted(source_ids),
                }
            )
    return warnings


def _read_source_cards(
    paths: KnowledgeBasePaths, source_ids: set[str]
) -> dict[str, dict[str, str]]:
    cards: dict[str, dict[str, str]] = {}
    for source_id in sorted(source_ids):
        card_path = paths.sources / f"{source_id}.md"
        if not card_path.is_file() or card_path.is_symlink():
            continue
        try:
            cards[source_id] = read_source_card(card_path)
        except RuntimeError:
            continue
    return cards


def _safe_raw_path(paths: KnowledgeBasePaths, card: dict[str, str]) -> Path | None:
    raw_value = card.get("raw_path", "")
    raw_path = Path(raw_value)
    if raw_path.is_absolute():
        return None
    try:
        resolved = (paths.root / raw_path).resolve()
        resolved.relative_to(paths.root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def _source_texts(
    paths: KnowledgeBasePaths, cards: dict[str, dict[str, str]]
) -> dict[str, str]:
    texts: dict[str, str] = {}
    for source_id, card in cards.items():
        raw_path = _safe_raw_path(paths, card)
        if raw_path is None:
            continue
        try:
            texts[source_id] = extract_text(raw_path)
        except RuntimeError:
            continue
    return texts


def _source_chunks(
    paths: KnowledgeBasePaths, cards: dict[str, dict[str, str]]
) -> dict[tuple[str, int], str]:
    chunks: dict[tuple[str, int], str] = {}
    for source_id, text in _source_texts(paths, cards).items():
        for chunk_index, content in enumerate(chunk_text(text)):
            chunks[(source_id, chunk_index)] = content
    return chunks


def _quote_support_and_markers(
    records: list[dict[str, object]],
    retrieved_texts_by_line: dict[int, list[str]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    expected_count = 0
    supported_count = 0
    markers: list[dict[str, object]] = []

    for record in records:
        line_number = int(record["line_number"])
        quotes = [str(quote) for quote in record.get("expected_quotes", [])]
        if not quotes:
            markers.append(
                {
                    "type": "missing_expected_quote",
                    "line_number": line_number,
                    "reason": "no_expected_quotes",
                }
            )
            continue
        for quote_index, quote in enumerate(quotes, start=1):
            expected_count += 1
            supported = any(
                quote in text for text in retrieved_texts_by_line.get(line_number, [])
            )
            if supported:
                supported_count += 1
                continue
            markers.append(
                {
                    "type": "missing_expected_quote",
                    "line_number": line_number,
                    "expected_quote_index": quote_index,
                }
            )

    hit_rate = supported_count / expected_count if expected_count else 0.0
    return (
        {
            "authority": "metric_only",
            "expected_quote_count": expected_count,
            "supported_quote_count": supported_count,
            "missing_quote_count": expected_count - supported_count,
            "hit_rate": hit_rate,
        },
        markers,
    )


def _source_quality_markers(
    records: list[dict[str, object]],
    cards: dict[str, dict[str, str]],
    source_texts: dict[str, str],
) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    blocking_statuses = {"needs_reingest", "rejected"}
    accepted_statuses = {"reviewed", "verified", "pass"}
    for record in records:
        line_number = int(record["line_number"])
        benchmark_privacy = str(record["privacy"])
        if (
            benchmark_privacy in PRIVATE_PRIVACY_LEVELS
            and not record.get("privacy_confirmation_supported")
        ):
            markers.append(
                {
                    "type": "unsupported_privacy_confirmation",
                    "line_number": line_number,
                    "privacy": benchmark_privacy,
                }
            )
        for source_id in record["expected_sources"]:
            card = cards.get(str(source_id))
            if card is None:
                continue
            review_status = card.get("review_status", "").strip().casefold()
            if review_status in blocking_statuses:
                markers.append(
                    {
                        "type": "source_review_blocker",
                        "source_id": str(source_id),
                        "line_number": line_number,
                    }
                )
            elif review_status not in accepted_statuses:
                markers.append(
                    {
                        "type": "unreviewed_source",
                        "source_id": str(source_id),
                        "line_number": line_number,
                    }
                )
            if not card.get("title", "").strip():
                markers.append(
                    {
                        "type": "missing_title",
                        "source_id": str(source_id),
                        "line_number": line_number,
                    }
                )
            if len(source_texts.get(str(source_id), "").strip()) < 40:
                markers.append(
                    {
                        "type": "very_short_content",
                        "source_id": str(source_id),
                        "line_number": line_number,
                    }
                )
            source_privacy = card.get("privacy", "").strip().casefold()
            if (
                benchmark_privacy == "public"
                and source_privacy in PRIVATE_PRIVACY_LEVELS
            ):
                markers.append(
                    {
                        "type": "private_source_in_public_benchmark",
                        "source_id": str(source_id),
                        "line_number": line_number,
                    }
                )
    return markers


def _raw_hash_warnings(
    paths: KnowledgeBasePaths,
    cards: dict[str, dict[str, str]],
    source_lines: dict[str, int],
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for source_id, card in cards.items():
        raw_path = _safe_raw_path(paths, card)
        if raw_path is None:
            continue
        try:
            current_source_id, current_sha256 = source_id_and_sha256(
                raw_path.read_bytes()
            )
        except OSError:
            continue
        if current_source_id == source_id and current_sha256 == card.get("sha256"):
            continue
        warnings.append(
            {
                "type": "raw_source_hash_mismatch",
                "source_id": source_id,
                "line_number": source_lines.get(source_id, 0),
            }
        )
    return warnings


def _vector_stale_warnings(
    paths: KnowledgeBasePaths,
    expected_source_ids: set[str],
    current_chunks: dict[tuple[str, int], str],
    source_lines: dict[str, int],
) -> list[dict[str, object]]:
    if not paths.database.is_file():
        return []
    try:
        with closing(_readonly_database_connection(paths.database)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'chunk_vectors'"
            ).fetchone()
            if table is None:
                return []
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(chunk_vectors)")
            }
            if not {"source_id", "chunk_index", "content"}.issubset(columns):
                return [{"type": "malformed_vector_index", "table": "chunk_vectors"}]
            rows = connection.execute(
                """
                SELECT source_id, chunk_index, content
                FROM chunk_vectors
                WHERE source_id IN ({})
                """.format(",".join("?" for _ in expected_source_ids)),
                tuple(sorted(expected_source_ids)),
            ).fetchall()
    except sqlite3.Error:
        return [{"type": "malformed_vector_index", "table": "chunk_vectors"}]

    warnings: list[dict[str, object]] = []
    for row in rows:
        source_id = str(row[0])
        chunk_index = int(row[1])
        current = current_chunks.get((source_id, chunk_index))
        if current is not None and str(row[2]) == current:
            continue
        warnings.append(
            {
                "type": "stale_vector_row",
                "source_id": source_id,
                "chunk_index": chunk_index,
                "line_number": source_lines.get(source_id, 0),
            }
        )
    return warnings


def _retrieved_texts(results: list[dict[str, object]]) -> list[str]:
    texts: list[str] = []
    for result in results:
        snippet = result.get("snippet")
        if isinstance(snippet, str) and snippet:
            texts.append(snippet)
    return texts


def _merge_retrieved_texts(
    target: dict[int, list[str]], source: dict[int, list[str]]
) -> None:
    for line_number, texts in source.items():
        target.setdefault(line_number, []).extend(texts)


def _hit_rate(hits: list[bool]) -> float:
    if not hits:
        return 0.0
    return sum(1 for hit in hits if hit) / len(hits)


def _mode_classification(mode: str, hit_rate: float, query_count: int) -> str:
    if query_count == 0:
        return "invalid_benchmark"
    if hit_rate >= 1.0:
        return "pass"
    if mode == "fts":
        return "fts_expectation_failed"
    return f"{mode}_expectation_failed"


def _local_endpoint_allowed(base_url: str) -> bool:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").casefold()
    return host in {"localhost", "127.0.0.1", "::1"}


def _embedding_mode_ready(
    *,
    client: object | None,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    try:
        config = load_embedding_config(env)
    except RuntimeError as exc:
        return False, str(exc)
    if client is not None:
        return True, ""
    if _local_endpoint_allowed(config.base_url):
        return True, ""
    return False, "No explicit local embedding client or localhost endpoint configured"


def _evaluate_mode(
    root: Path,
    records: list[dict[str, object]],
    *,
    mode: str,
    limit: int,
    client: object | None,
    env: Mapping[str, str],
) -> tuple[float | None, str, dict[int, list[str]]]:
    ready, reason = _embedding_mode_ready(client=client, env=env)
    if not ready:
        return None, SKIPPED_EXTERNAL_DEPENDENCY, {}

    hits: list[bool] = []
    retrieved_by_line: dict[int, list[str]] = {}
    try:
        for record in records:
            expected = set(record["expected_sources"])
            if mode == "semantic":
                results = semantic_search(
                    root,
                    str(record["query"]),
                    limit=limit,
                    client=client,
                    env=dict(env),
                )
            else:
                results = hybrid_search(
                    root,
                    str(record["query"]),
                    limit=limit,
                    client=client,
                    env=dict(env),
                )
            retrieved_by_line[int(record["line_number"])] = _retrieved_texts(results)
            found = {str(result.get("source_id", "")) for result in results}
            hits.append(not expected or expected.issubset(found))
    except RuntimeError:
        return None, SKIPPED_EXTERNAL_DEPENDENCY, {}

    hit_rate = _hit_rate(hits)
    return hit_rate, _mode_classification(mode, hit_rate, len(records)), retrieved_by_line


def eval_search(
    root: str | Path,
    benchmark: str | Path | None,
    *,
    limit: int = 10,
    client: object | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    env_source: Mapping[str, str] = os.environ if env is None else env
    root_path = Path(root)
    paths = KnowledgeBasePaths(root_path)
    try:
        benchmark_path = _resolve_benchmark_path(paths.root, benchmark)
    except ValueError as exc:
        return _result(
            "failed",
            "invalid_benchmark",
            str(exc),
            env=env_source,
        )

    load_error, records, privacy_summary = _load_benchmark(benchmark_path, env_source)
    if load_error:
        summary = {
            "missing_benchmark_file": "Benchmark file was not found.",
            "invalid_benchmark": "Benchmark JSONL is invalid.",
            "secret_in_benchmark": "Benchmark contains a secret-shaped value.",
            "policy_confirmation_required": "Benchmark contains unconfirmed private samples.",
        }[load_error]
        return _result(
            "failed",
            load_error,
            summary,
            env=env_source,
            privacy_summary=privacy_summary,
        )

    if not _search_index_available(paths):
        return _result(
            "failed",
            "search_index_missing",
            "Search index is missing; rebuild the index before evaluation.",
            env=env_source,
            query_count=len(records),
            privacy_summary=privacy_summary,
            stale_warnings=[
                {"type": "search_index_missing", "path": "db/kb.sqlite3"}
            ],
        )

    effective_limit = max(1, int(limit))
    fts_hits: list[bool] = []
    missing_sources: list[str] = []
    missing_wiki_paths: list[str] = []
    stale_warnings: list[dict[str, object]] = []
    expected_source_ids = {
        str(source_id)
        for record in records
        for source_id in record["expected_sources"]
    }
    source_lines: dict[str, int] = {}
    for record in records:
        for source_id in record["expected_sources"]:
            source_lines.setdefault(str(source_id), int(record["line_number"]))
    source_cards = _read_source_cards(paths, expected_source_ids)
    current_source_texts = _source_texts(paths, source_cards)
    current_source_chunks = _source_chunks(paths, source_cards)
    retrieved_texts_by_line: dict[int, list[str]] = {}
    duplicate_warnings = _dedupe_dicts(
        _duplicate_warnings(records) + _duplicate_source_card_warnings(source_cards)
    )
    source_quality_markers = _source_quality_markers(
        records, source_cards, current_source_texts
    )
    existing_source_ids = _existing_source_ids(paths)
    indexed_source_ids = _indexed_source_ids(paths)
    stale_warnings.extend(_raw_hash_warnings(paths, source_cards, source_lines))
    stale_warnings.extend(
        _vector_stale_warnings(
            paths, expected_source_ids, current_source_chunks, source_lines
        )
    )

    for record in records:
        expected_sources = set(record["expected_sources"])
        fts_results = search(paths.root, str(record["query"]), effective_limit)
        retrieved_texts_by_line.setdefault(int(record["line_number"]), []).extend(
            _retrieved_texts(fts_results)
        )
        fts_source_ids = {str(result.get("source_id", "")) for result in fts_results}
        fts_missed = sorted(expected_sources - fts_source_ids)
        missing_source_assets = sorted(expected_sources - existing_source_ids)
        missing_indexed_sources = sorted(expected_sources - indexed_source_ids)
        missing_sources.extend(missing_source_assets)
        for source_id in missing_source_assets:
            stale_warnings.append(
                {"type": "missing_expected_source", "source_id": source_id}
            )
        missing_sources.extend(missing_indexed_sources)
        for source_id in missing_indexed_sources:
            stale_warnings.append(
                {"type": "expected_source_not_indexed", "source_id": source_id}
            )
        fts_hits.append(not expected_sources or not fts_missed)

        for wiki_path in record["expected_wiki_paths"]:
            if not (paths.root / wiki_path).is_file():
                missing_wiki_paths.append(wiki_path)
                stale_warnings.append(
                    {"type": "missing_expected_wiki_path", "path": wiki_path}
                )

    fts_hit_rate = _hit_rate(fts_hits)
    fts_classification = _mode_classification("fts", fts_hit_rate, len(records))
    semantic_hit_rate, semantic_classification, semantic_retrieved = _evaluate_mode(
        paths.root,
        records,
        mode="semantic",
        limit=effective_limit,
        client=client,
        env=env_source,
    )
    _merge_retrieved_texts(retrieved_texts_by_line, semantic_retrieved)
    hybrid_hit_rate, hybrid_classification, hybrid_retrieved = _evaluate_mode(
        paths.root,
        records,
        mode="hybrid",
        limit=effective_limit,
        client=client,
        env=env_source,
    )
    _merge_retrieved_texts(retrieved_texts_by_line, hybrid_retrieved)
    quote_support, quote_markers = _quote_support_and_markers(
        records, retrieved_texts_by_line
    )
    low_quality_source_markers = _dedupe_dicts(source_quality_markers + quote_markers)

    modes = {
        "fts": {"classification": fts_classification, "hit_rate": fts_hit_rate},
        "semantic": {
            "classification": semantic_classification,
            "hit_rate": semantic_hit_rate,
        },
        "hybrid": {
            "classification": hybrid_classification,
            "hit_rate": hybrid_hit_rate,
        },
    }
    pass_like = {"pass", SKIPPED_EXTERNAL_DEPENDENCY}
    status = (
        "pass"
        if fts_classification == "pass"
        and not missing_sources
        and not missing_wiki_paths
        and semantic_classification in pass_like
        and hybrid_classification in pass_like
        else "failed"
    )
    if status == "pass":
        classification = "pass"
        summary = "Retrieval benchmark passed."
    elif missing_sources:
        classification = (
            "fts_expectation_failed"
            if fts_classification != "pass"
            else "missing_expected_sources"
        )
        summary = "Required expected sources were missing from the index."
    elif missing_wiki_paths:
        classification = "missing_expected_wiki_paths"
        summary = "Required expected wiki paths were missing."
    elif fts_classification != "pass":
        classification = fts_classification
        summary = "Required FTS expectations did not pass."
    elif semantic_classification not in pass_like:
        classification = semantic_classification
        summary = "Semantic retrieval expectations did not pass."
    else:
        classification = hybrid_classification
        summary = "Hybrid retrieval expectations did not pass."

    return _result(
        status,
        classification,
        summary,
        env=env_source,
        query_count=len(records),
        fts_hit_rate=fts_hit_rate,
        semantic_hit_rate=semantic_hit_rate,
        hybrid_hit_rate=hybrid_hit_rate,
        missing_sources=missing_sources,
        missing_wiki_paths=missing_wiki_paths,
        stale_warnings=_dedupe_dicts(stale_warnings),
        privacy_summary=privacy_summary,
        modes=modes,
        quote_support=quote_support,
        duplicate_warnings=duplicate_warnings,
        low_quality_source_markers=low_quality_source_markers,
        residual_risks=[],
    )
