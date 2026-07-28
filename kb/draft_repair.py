from pathlib import Path

from .factuality import CONTEXT_CHUNK_RE, _reconstructed_chunk
from .paths import KnowledgeBasePaths
from .text import normalize_whitespace
from .wiki import draft_id, draft_timestamp


class DraftRepairError(RuntimeError):
    pass


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _claim_items(metadata: dict[str, object]) -> list[dict[str, object]]:
    claims = metadata.get("claims")
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def _repairable_quote(
    paths: KnowledgeBasePaths,
    context_sources: set[str],
    context_chunks: set[str],
    chunk_id: object,
    quote: object,
) -> tuple[str, str, str] | None:
    if not isinstance(chunk_id, str) or not isinstance(quote, str) or not quote.strip():
        return None

    match = CONTEXT_CHUNK_RE.fullmatch(chunk_id)
    if match is None:
        return None

    source_id = match.group(1)
    if source_id not in context_sources or chunk_id not in context_chunks:
        return None

    chunk = _reconstructed_chunk(paths, source_id, int(match.group(2)))
    if chunk is None or quote not in chunk:
        return None

    claim_text = normalize_whitespace(quote).strip()
    if not claim_text or claim_text not in quote:
        return None
    return source_id, chunk_id, claim_text


def repair_draft_content(
    paths: KnowledgeBasePaths,
    metadata: dict[str, object],
    target: str | None = None,
) -> tuple[dict[str, object], str]:
    context_sources = set(_string_list(metadata.get("context_sources")))
    context_chunks = set(_string_list(metadata.get("context_chunks")))
    if not context_sources or not context_chunks:
        raise DraftRepairError("draft-not-repairable")

    repaired_claims: list[dict[str, object]] = []
    paragraphs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for claim in _claim_items(metadata):
        evidence = claim.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            repaired = _repairable_quote(
                paths,
                context_sources,
                context_chunks,
                item.get("chunk"),
                item.get("quote"),
            )
            if repaired is None:
                continue
            source_id, chunk_id, claim_text = repaired
            key = (chunk_id, claim_text)
            if key in seen:
                continue
            seen.add(key)
            paragraph_index = len(paragraphs) + 1
            repaired_claims.append(
                {
                    "claim_id": f"repair-{paragraph_index}",
                    "paragraph": paragraph_index,
                    "text": claim_text,
                    "evidence": [{"chunk": chunk_id, "quote": claim_text}],
                }
            )
            paragraphs.append(f"{claim_text} {source_id}")

    if not repaired_claims:
        raise DraftRepairError("draft-not-repairable")

    title = target or str(metadata.get("title", "")).strip()
    if not title:
        raise DraftRepairError("draft-not-repairable")

    repaired_metadata = dict(metadata)
    repaired_metadata.update(
        {
            "draft_id": draft_id(),
            "title": title,
            "created_at": draft_timestamp(),
            "model": f"{metadata.get('model', 'unknown')}+deterministic-repair",
            "claims": repaired_claims,
            "repair_of": metadata.get("draft_id", ""),
            "repair_strategy": "extractive-evidence-quotes",
        }
    )
    body = f"# {title}\n\n" + "\n\n".join(paragraphs)
    return repaired_metadata, body


def next_repaired_draft_path(paths: KnowledgeBasePaths, draft_path: Path) -> Path:
    drafts_dir = paths.wiki / "_drafts"
    stem = draft_path.stem
    for index in range(1, 1000):
        suffix = "repaired" if index == 1 else f"repaired-{index}"
        candidate = drafts_dir / f"{stem}.{suffix}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError("No available repaired draft path")
