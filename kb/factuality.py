import json
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import KnowledgeBasePaths
from .sources import read_source_card, source_id_and_sha256
from .text import chunk_text, extract_text, normalize_whitespace


SOURCE_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])src-[0-9a-f]{12}(?![A-Za-z0-9_-])")
CONTEXT_CHUNK_RE = re.compile(r"^(src-[0-9a-f]{12})#([0-9]+)$")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SENTENCE_END = ".!?;\u3002\uff01\uff1f\uff1b"
TRIM_CHARS = " \t\r\n,.;:!?\u3002\uff01\uff1f\uff1b"
PUNCTUATION = ",.;:!?\u3002\uff01\uff1f\uff1b"


@dataclass(frozen=True)
class ParsedDraftResponse:
    body: str
    claims: list[dict[str, object]]


def _valid_claim_header_shape(claims: object) -> bool:
    if not isinstance(claims, list) or not claims:
        return False
    for claim in claims:
        if not isinstance(claim, dict):
            return False
        if not isinstance(claim.get("claim_id"), str) or not claim["claim_id"].strip():
            return False
        paragraph = claim.get("paragraph")
        if not isinstance(paragraph, int) or isinstance(paragraph, bool):
            return False
        if not isinstance(claim.get("text"), str) or not claim["text"].strip():
            return False
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
    return True


def _valid_claim_shape(claims: object) -> bool:
    if not _valid_claim_header_shape(claims):
        return False
    for claim in claims:
        for item in claim["evidence"]:
            if not isinstance(item, dict):
                return False
            chunk_id = item.get("chunk")
            quote = item.get("quote")
            if (
                not isinstance(chunk_id, str)
                or not chunk_id.strip()
                or not isinstance(quote, str)
                or not quote.strip()
            ):
                return False
    return True


def parse_llm_draft_response(content: str) -> ParsedDraftResponse:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError("Invalid LLM draft response") from None

    if not isinstance(payload, dict):
        raise RuntimeError("Invalid LLM draft response")

    body = payload.get("body")
    claims = payload.get("claims")
    if not isinstance(body, str) or not body.strip():
        raise RuntimeError("Invalid LLM draft response")
    if not _valid_claim_shape(claims):
        raise RuntimeError("Invalid LLM draft response")

    return ParsedDraftResponse(body=body, claims=claims)


def llm_draft_contract_status(
    content: str, title: str, target: str | None = None
) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return "invalid_json"

    if not isinstance(payload, dict):
        return "invalid_claim_shape"

    body = payload.get("body")
    claims = payload.get("claims")
    if not isinstance(body, str) or not body.strip():
        return "invalid_claim_shape"
    if not _valid_claim_shape(claims):
        return "invalid_claim_shape"

    allowed_heading_titles = {target} if target else None
    heading_issues = _validate_heading_contract(
        {"title": title}, body, allowed_heading_titles
    )
    if heading_issues:
        return "unsupported_heading"
    return "pass"


def _paragraph_blocks(body: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.strip():
            current.append(line)
            continue
        if current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def non_heading_paragraphs(body: str) -> list[str]:
    paragraphs: list[str] = []
    for paragraph in _paragraph_blocks(body):
        lines = [
            line for line in paragraph.splitlines() if not line.lstrip().startswith("#")
        ]
        text = "\n".join(lines).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _replace_wiki_link_label(match: re.Match[str]) -> str:
    target = match.group(1).strip()
    if "|" in target:
        return target.rsplit("|", 1)[1].strip()
    return target


def _strip_markup(text: str) -> str:
    text = WIKI_LINK_RE.sub(_replace_wiki_link_label, text)
    text = SOURCE_ID_RE.sub(" ", text)
    text = re.sub(r"(`+|\*\*?|\_\_?|~~)", "", text)
    return re.sub(rf"\s+([{re.escape(PUNCTUATION)}])", r"\1", text)


def _wiki_link_target(match: re.Match[str]) -> str:
    return match.group(1).split("|", 1)[0].strip()


def _strip_ignored_wiki_links(text: str, ignored_targets: set[str]) -> str:
    if not ignored_targets:
        return text
    ignored = {target.casefold() for target in ignored_targets}

    def replace_link(match: re.Match[str]) -> str:
        if _wiki_link_target(match).casefold() in ignored:
            return "__KB_IGNORED_WIKI_LINK__"
        return match.group(0)

    text = WIKI_LINK_RE.sub(replace_link, text)
    return re.sub(
        r"(?:\s*,?\s*(?:and\s+)?links?\s+(?:to\s+)?)?__KB_IGNORED_WIKI_LINK__",
        " ",
        text,
        flags=re.IGNORECASE,
    )


def _strip_statement_markup(text: str, ignored_wiki_targets: set[str] | None) -> str:
    text = _strip_ignored_wiki_links(text, ignored_wiki_targets or set())
    return _strip_markup(text)


def _clean_statement_text(text: str) -> str:
    return normalize_whitespace(_strip_markup(text)).strip(TRIM_CHARS)


def factual_statements(
    paragraph: str, ignored_wiki_targets: set[str] | None = None
) -> list[str]:
    cleaned = _strip_statement_markup(paragraph, ignored_wiki_targets)
    statements: list[str] = []
    for part in re.split(rf"(?:[{re.escape(SENTENCE_END)}]+|\n+)", cleaned):
        statement = normalize_whitespace(part).strip(TRIM_CHARS)
        if not statement:
            continue
        if re.search(r"[\w\u4e00-\u9fff]", statement, flags=re.UNICODE):
            statements.append(statement)
    return statements


def _issue(issue_type: str, **fields: object) -> dict[str, str]:
    issue = {"type": issue_type}
    issue.update({key: str(value) for key, value in fields.items()})
    return issue


def _string_list(value: object, key: str) -> list[str] | None:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and key in item:
            result.append(str(item[key]))
        else:
            return None
    return result


def _paths(value: object) -> KnowledgeBasePaths:
    if isinstance(value, KnowledgeBasePaths):
        return value
    return KnowledgeBasePaths(Path(value))


def _safe_raw_path(paths: KnowledgeBasePaths, card: dict[str, str]) -> Path | None:
    raw_path_value = card.get("raw_path")
    if not raw_path_value:
        return None
    raw_path = Path(raw_path_value)
    if raw_path.is_absolute():
        return None
    resolved = (paths.root / raw_path).resolve()
    try:
        resolved.relative_to(paths.raw.resolve())
    except ValueError:
        return None
    return resolved


def _reconstructed_chunk(
    paths: KnowledgeBasePaths, source_id: str, chunk_index: int
) -> str | None:
    card_path = paths.sources / f"{source_id}.md"
    try:
        card = read_source_card(card_path)
    except (OSError, UnicodeDecodeError, RuntimeError):
        return None
    if card.get("source_id") != source_id:
        return None

    raw_path = _safe_raw_path(paths, card)
    if raw_path is None or not raw_path.is_file():
        return None

    try:
        data = raw_path.read_bytes()
        expected_source_id, expected_sha256 = source_id_and_sha256(data)
        if expected_source_id != source_id or card.get("sha256") != expected_sha256:
            return None
        chunks = chunk_text(extract_text(raw_path))
    except (OSError, UnicodeDecodeError, RuntimeError):
        return None
    if chunk_index < 0 or chunk_index >= len(chunks):
        return None
    return chunks[chunk_index]


def _normalized_text(text: str) -> str:
    return _clean_statement_text(text)


def _heading_label(line: str) -> tuple[int, str] | None:
    match = HEADING_RE.match(line.strip())
    if match is None:
        return None
    label = _normalized_text(match.group(2))
    if not label:
        return None
    return len(match.group(1)), label


def _allowed_heading_labels(
    metadata: dict[str, object], allowed_heading_titles: set[str] | None
) -> set[str]:
    labels: set[str] = set()
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        labels.add(_normalized_text(title))
    for value in allowed_heading_titles or set():
        if isinstance(value, str) and value.strip():
            labels.add(_normalized_text(value))
    return {label for label in labels if label}


def _validate_heading_contract(
    metadata: dict[str, object],
    body: str,
    allowed_heading_titles: set[str] | None,
) -> list[dict[str, str]]:
    headings: list[tuple[int, str]] = []
    for line in body.splitlines():
        heading = _heading_label(line)
        if heading is not None:
            headings.append(heading)

    if not headings:
        return []

    allowed_labels = _allowed_heading_labels(metadata, allowed_heading_titles)
    issues: list[dict[str, str]] = []
    for index, (level, label) in enumerate(headings, start=1):
        if index == 1 and level == 1 and label in allowed_labels:
            continue
        issues.append(_issue("unsupported-draft-heading", heading=index))
    return issues


def validate_claims(
    paths: object,
    metadata: dict[str, object],
    body: str,
    ignored_wiki_targets: set[str] | None = None,
    allowed_heading_titles: set[str] | None = None,
) -> list[dict[str, str]]:
    kb_paths = _paths(paths)
    claims = metadata.get("claims")
    if not _valid_claim_header_shape(claims):
        return [_issue("invalid-claims")]

    context_sources = set(_string_list(metadata.get("context_sources"), "source_id") or [])
    context_chunks = set(_string_list(metadata.get("context_chunks"), "chunk") or [])
    paragraphs = non_heading_paragraphs(body)
    normalized_paragraphs = [normalize_whitespace(paragraph) for paragraph in paragraphs]
    issues: list[dict[str, str]] = []
    seen_claim_ids: set[str] = set()
    valid_claims_by_paragraph: dict[int, list[str]] = {}
    issues.extend(_validate_heading_contract(metadata, body, allowed_heading_titles))

    for claim in claims:
        claim_id = str(claim["claim_id"]).strip()
        paragraph_index = int(claim["paragraph"])
        claim_text = normalize_whitespace(str(claim["text"]))
        evidence = claim["evidence"]

        if claim_id in seen_claim_ids:
            issues.append(_issue("duplicate-claim-id"))
        seen_claim_ids.add(claim_id)

        if paragraph_index < 1 or paragraph_index > len(paragraphs):
            issues.append(
                _issue(
                    "claim-paragraph-out-of-range",
                    paragraph=paragraph_index,
                )
            )
            continue

        paragraph = paragraphs[paragraph_index - 1]
        normalized_paragraph = normalized_paragraphs[paragraph_index - 1]
        if claim_text not in normalized_paragraph:
            issues.append(_issue("claim-text-not-in-paragraph"))
        else:
            valid_claims_by_paragraph.setdefault(paragraph_index, []).append(
                _normalized_text(claim_text)
            )

        evidence_sources: set[str] = set()
        supported_by_quote = False
        for item in evidence:
            if not isinstance(item, dict):
                issues.append(_issue("invalid-claim-evidence"))
                continue
            chunk_id = item.get("chunk")
            quote = item.get("quote")
            if (
                not isinstance(chunk_id, str)
                or not chunk_id.strip()
                or not isinstance(quote, str)
                or not quote.strip()
            ):
                issues.append(_issue("invalid-claim-evidence"))
                continue

            match = CONTEXT_CHUNK_RE.fullmatch(chunk_id)
            if not match:
                issues.append(_issue("invalid-claim-evidence"))
                continue

            source_id = match.group(1)
            evidence_sources.add(source_id)
            if chunk_id not in context_chunks or source_id not in context_sources:
                issues.append(_issue("claim-evidence-outside-context"))
                continue

            chunk = _reconstructed_chunk(kb_paths, source_id, int(match.group(2)))
            if chunk is None:
                issues.append(_issue("claim-evidence-missing-chunk"))
                continue

            if quote not in chunk:
                issues.append(_issue("claim-quote-not-in-chunk"))
                continue

            if claim_text in quote:
                supported_by_quote = True

        if evidence_sources:
            paragraph_sources = set(SOURCE_ID_RE.findall(paragraph))
            for source_id in sorted(evidence_sources - paragraph_sources):
                issues.append(
                    _issue(
                        "claim-source-not-cited",
                        source_id=source_id,
                    )
                )

        if not supported_by_quote:
            issues.append(_issue("claim-text-not-supported-by-quote"))

    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        statements = factual_statements(paragraph, ignored_wiki_targets)
        if not statements:
            continue
        claim_texts = valid_claims_by_paragraph.get(paragraph_index, [])
        if not claim_texts:
            issues.append(
                _issue("paragraph-without-claim", paragraph=paragraph_index)
            )
            continue
        for statement in statements:
            normalized_statement = _normalized_text(statement)
            if not any(normalized_statement in claim_text for claim_text in claim_texts):
                issues.append(
                    _issue(
                        "unclaimed-statement",
                        paragraph=paragraph_index,
                    )
                )

    return issues
