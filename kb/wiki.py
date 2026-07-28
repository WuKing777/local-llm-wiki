import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .factuality import validate_claims
from .paths import KnowledgeBasePaths


DRAFT_FIELDS = (
    "draft_id",
    "title",
    "query",
    "created_at",
    "model",
    "prompt_hash",
    "context_sources",
    "context_chunks",
    "claims",
)
SOURCE_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])src-[0-9a-f]{12}(?![A-Za-z0-9_-])")
SOURCE_ID_FULL_RE = re.compile(r"^src-[0-9a-f]{12}$")
CONTEXT_CHUNK_RE = re.compile(r"^(src-[0-9a-f]{12})#([0-9]+)$")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
UNSAFE_TARGET_ROOTS = {"_drafts", "raw", "sources", "meta", "db"}


def safe_slug(title: str) -> str:
    normalized = title.replace("\\", " ").replace("/", " ")
    slug = re.sub(r"[^\w]+", "-", normalized.casefold()).strip("-_")
    return slug or "draft"


def _ensure_under(path: Path, parent: Path) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        raise RuntimeError(f"Draft path outside _drafts: {path}") from None


def _drafts_dir(paths: KnowledgeBasePaths) -> Path:
    drafts = paths.wiki / "_drafts"
    _ensure_under(drafts, paths.wiki)
    _ensure_under(drafts, paths.root)
    return drafts


def draft_path_for_title(paths: KnowledgeBasePaths, title: str) -> Path:
    drafts = _drafts_dir(paths)
    draft_path = drafts / f"{safe_slug(title)}.md"
    _ensure_under(draft_path, drafts)
    return draft_path


def target_path_for_title(paths: KnowledgeBasePaths, target: str) -> Path:
    parts = _safe_target_parts(target)
    target_path = paths.wiki.joinpath(*parts[:-1], f"{parts[-1]}.md")
    try:
        target_path.resolve().relative_to(paths.wiki.resolve())
    except ValueError:
        raise ValueError("Unsafe target") from None
    return target_path


def _safe_target_parts(target: str) -> list[str]:
    cleaned = target.strip()
    if not cleaned:
        raise ValueError("Unsafe target")

    windows_target = PureWindowsPath(cleaned)
    posix_target = PurePosixPath(cleaned)
    if (
        cleaned.startswith(("/", "\\"))
        or Path(cleaned).is_absolute()
        or windows_target.is_absolute()
        or posix_target.is_absolute()
        or windows_target.drive
    ):
        raise ValueError("Unsafe target")
    if re.search(r"[\\/]{2,}", cleaned):
        raise ValueError("Unsafe target")

    parts = [part for part in re.split(r"[\\/]+", cleaned) if part]
    if not parts or any(part in {".", ".."} or ".." in part for part in parts):
        raise ValueError("Unsafe target")
    if any(part.casefold() in UNSAFE_TARGET_ROOTS for part in parts):
        raise ValueError("Unsafe target")
    slugged = [safe_slug(part) for part in parts]
    if not all(slugged):
        raise ValueError("Unsafe target")
    return slugged


def draft_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def draft_id() -> str:
    return f"draft-{uuid.uuid4().hex[:12]}"


def _front_matter_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_draft(
    paths: KnowledgeBasePaths, metadata: dict[str, object], body: str
) -> Path:
    return write_draft_at_path(
        paths, draft_path_for_title(paths, str(metadata.get("title", ""))), metadata, body
    )


def write_draft_at_path(
    paths: KnowledgeBasePaths,
    draft_path: str | Path,
    metadata: dict[str, object],
    body: str,
) -> Path:
    for field in DRAFT_FIELDS:
        if field not in metadata or metadata[field] in ("", None, [], {}):
            raise RuntimeError(f"Missing draft metadata field: {field}")

    drafts_dir = _drafts_dir(paths)
    draft_path = Path(draft_path)
    if not draft_path.is_absolute():
        draft_path = paths.root / draft_path
    _ensure_under(draft_path, drafts_dir)

    drafts_existed = drafts_dir.exists()
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    if draft_path.exists():
        raise RuntimeError(f"Draft already exists: {draft_path}")

    lines = ["---"]
    lines.extend(f"{field}: {_front_matter_value(metadata[field])}" for field in DRAFT_FIELDS)
    lines.extend(["---", "", body.rstrip(), ""])
    content = "\n".join(lines)
    created = False
    try:
        draft_file = draft_path.open("x", encoding="utf-8")
        created = True
        with draft_file as draft:
            draft.write(content)
    except FileExistsError:
        raise RuntimeError(f"Draft already exists: {draft_path}") from None
    except Exception:
        if created and draft_path.exists():
            draft_path.unlink()
        if not drafts_existed and drafts_dir.is_dir():
            try:
                drafts_dir.rmdir()
            except OSError:
                pass
        raise
    return draft_path


def read_draft(path: str | Path) -> tuple[dict[str, object], str]:
    draft_path = Path(path)
    lines = draft_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("Missing opening front matter")

    metadata: dict[str, object] = {}
    body_start = 0
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if not separator:
            raise RuntimeError(f"Invalid draft field: {draft_path}")
        raw_value = value.strip()
        try:
            metadata[key.strip()] = json.loads(raw_value)
        except json.JSONDecodeError:
            metadata[key.strip()] = raw_value
    else:
        raise RuntimeError("Missing closing front matter")

    return metadata, "\n".join(lines[body_start:]).strip()


def _path_label(paths: KnowledgeBasePaths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _source_card_exists(paths: KnowledgeBasePaths, source_id: str) -> bool:
    return (paths.sources / f"{source_id}.md").is_file()


def _list_value(value: object) -> list[object] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return None


def _safe_missing_target(paths: KnowledgeBasePaths, target: str) -> bool:
    try:
        target_path_for_title(paths, target)
    except ValueError:
        return False
    return True


def _literal_wiki_link_path(paths: KnowledgeBasePaths, target: str) -> Path | None:
    cleaned = target.strip()
    if not cleaned:
        return None
    try:
        _safe_target_parts(cleaned)
    except ValueError:
        return None

    windows_target = PureWindowsPath(cleaned)
    posix_target = PurePosixPath(cleaned)
    if (
        cleaned.startswith(("/", "\\"))
        or Path(cleaned).is_absolute()
        or windows_target.is_absolute()
        or posix_target.is_absolute()
        or windows_target.drive
    ):
        return None

    candidate = paths.wiki / f"{cleaned}.md"
    try:
        resolved = candidate.resolve()
        resolved.relative_to(paths.wiki.resolve())
    except (OSError, ValueError):
        return None
    if _is_under(resolved, paths.wiki / "_drafts"):
        return None
    return resolved


def wiki_link_exists(paths: KnowledgeBasePaths, target: str) -> bool:
    literal_path = _literal_wiki_link_path(paths, target)
    if literal_path is not None and literal_path.is_file():
        return True
    try:
        return target_path_for_title(paths, target).is_file()
    except ValueError:
        return False


def _paragraphs(body: str) -> list[str]:
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


def _has_real_citation(
    paragraph: str, existing_source_ids: set[str], context_source_ids: set[str]
) -> bool:
    for source_id in SOURCE_ID_RE.findall(paragraph):
        if source_id in existing_source_ids and source_id in context_source_ids:
            return True
    return False


def validate_draft_content(
    paths: KnowledgeBasePaths,
    metadata: dict[str, object],
    body: str,
    path_label: str,
    target: str | None = None,
    draft_text: str | None = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    existing_source_ids = {
        card.stem for card in paths.sources.glob("src-*.md") if card.is_file()
    }

    for field in DRAFT_FIELDS:
        if field not in metadata or metadata[field] in ("", None, [], {}):
            issues.append(
                {"type": "missing-draft-field", "path": path_label, "field": field}
            )

    raw_context_sources = _list_value(metadata.get("context_sources"))
    context_source_ids: set[str] = set()
    if raw_context_sources is None:
        if "context_sources" in metadata:
            issues.append(
                {
                    "type": "invalid-context-source",
                    "path": path_label,
                    "source_id": str(metadata["context_sources"]),
                }
            )
    else:
        for raw_source_id in raw_context_sources:
            source_id = str(raw_source_id)
            if SOURCE_ID_FULL_RE.fullmatch(source_id) and _source_card_exists(
                paths, source_id
            ):
                context_source_ids.add(source_id)
                continue
            issues.append(
                {
                    "type": "invalid-context-source",
                    "path": path_label,
                    "source_id": source_id,
                }
            )

    raw_context_chunks = _list_value(metadata.get("context_chunks"))
    if raw_context_chunks is None:
        if "context_chunks" in metadata:
            issues.append(
                {
                    "type": "invalid-context-chunk",
                    "path": path_label,
                    "chunk": str(metadata["context_chunks"]),
                }
            )
    else:
        for raw_chunk_id in raw_context_chunks:
            chunk_id = str(raw_chunk_id)
            match = CONTEXT_CHUNK_RE.fullmatch(chunk_id)
            if match and match.group(1) in context_source_ids:
                continue
            issues.append(
                {
                    "type": "invalid-context-chunk",
                    "path": path_label,
                    "chunk": chunk_id,
                }
            )

    allowed_missing_target = None
    if target is not None:
        if _safe_missing_target(paths, target):
            allowed_missing_target = target.strip()
        else:
            issues.append(
                {"type": "unsafe-target", "path": path_label, "target": target}
            )

    ignored_wiki_targets = (
        {allowed_missing_target} if allowed_missing_target is not None else set()
    )
    allowed_heading_titles = (
        {allowed_missing_target} if allowed_missing_target is not None else set()
    )
    issues.extend(
        validate_claims(
            paths,
            metadata,
            body,
            ignored_wiki_targets=ignored_wiki_targets,
            allowed_heading_titles=allowed_heading_titles,
        )
    )

    for source_id in sorted(set(SOURCE_ID_RE.findall(body))):
        if source_id not in existing_source_ids or source_id not in context_source_ids:
            issues.append(
                {
                    "type": "citation-outside-context",
                    "path": path_label,
                    "source_id": source_id,
                }
            )

    for index, paragraph in enumerate(_paragraphs(body), start=1):
        non_heading_lines = [
            line for line in paragraph.splitlines() if not line.lstrip().startswith("#")
        ]
        if not non_heading_lines:
            continue
        paragraph_text = "\n".join(non_heading_lines)
        if not _has_real_citation(
            paragraph_text, existing_source_ids, context_source_ids
        ):
            issues.append(
                {
                    "type": "missing-paragraph-citation",
                    "path": path_label,
                    "paragraph": str(index),
                }
            )

    for wiki_target in WIKI_LINK_RE.findall(body):
        wiki_target = wiki_target.strip()
        if not wiki_target:
            continue
        if wiki_link_exists(paths, wiki_target):
            continue
        if allowed_missing_target is not None and wiki_target == allowed_missing_target:
            continue
        issues.append(
            {
                "type": "broken-wiki-link",
                "path": path_label,
                "target": wiki_target,
            }
        )

    api_key = os.environ.get("KB_LLM_API_KEY")
    if api_key and draft_text is not None and api_key in draft_text:
        issues.append({"type": "secret-leak", "path": path_label})

    return issues


def validate_draft(
    paths: KnowledgeBasePaths, draft_path: str | Path, target: str | None = None
) -> list[dict[str, str]]:
    path = Path(draft_path)
    path_label = _path_label(paths, path)
    issues: list[dict[str, str]] = []
    try:
        drafts_dir = _drafts_dir(paths)
    except RuntimeError:
        return [{"type": "draft-path-outside-drafts", "path": path_label}]
    if not _is_under(path, drafts_dir):
        return [{"type": "draft-path-outside-drafts", "path": path_label}]

    try:
        draft_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Cannot read draft: {path}") from exc

    try:
        metadata, body = read_draft(path)
    except RuntimeError as exc:
        message = str(exc)
        if message == "Missing opening front matter":
            metadata = {}
            body = draft_text
        elif message == "Missing closing front matter" or message.startswith(
            "Invalid draft field:"
        ):
            metadata = {}
            body = draft_text
            issues.append({"type": "invalid-draft-front-matter", "path": path_label})
        else:
            raise
    issues.extend(
        validate_draft_content(
            paths, metadata, body, path_label, target=target, draft_text=draft_text
        )
    )
    return issues
