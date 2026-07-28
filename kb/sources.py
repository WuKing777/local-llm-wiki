import hashlib
import json
from datetime import datetime
from pathlib import Path

from .paths import KnowledgeBasePaths
from .text import kind_for_path

SOURCE_CARD_FIELDS = (
    "source_id",
    "title",
    "raw_path",
    "sha256",
    "imported_at",
    "kind",
)
OPTIONAL_SOURCE_CARD_FIELDS = (
    "workflow",
    "original_path",
    "review_status",
    "reviewed_at",
    "reviewer",
    "review_note",
    "source_type",
    "privacy",
    "confidence",
    "event_date",
    "input_method",
    "pending_confirmation",
)


def source_id_and_sha256(data: bytes) -> tuple[str, str]:
    sha256 = hashlib.sha256(data).hexdigest()
    return f"src-{sha256[:12]}", sha256


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def imported_timestamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def title_for(path: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.stem
        if stripped:
            break
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def write_source_card(paths: KnowledgeBasePaths, metadata: dict[str, str]) -> Path:
    card = paths.sources / f"{metadata['source_id']}.md"
    lines = ["---"]
    lines.extend(f"{field}: {metadata[field]}" for field in SOURCE_CARD_FIELDS)
    lines.extend(
        f"{field}: {metadata[field]}"
        for field in OPTIONAL_SOURCE_CARD_FIELDS
        if metadata.get(field)
    )
    lines.extend(["---", ""])
    card.write_text("\n".join(lines), encoding="utf-8")
    return card


def read_source_card(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("Missing opening front matter")

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        key, separator, value = line.partition(":")
        if not separator:
            raise RuntimeError(f"Invalid source card field: {path}")
        metadata[key.strip()] = value.strip()
    else:
        raise RuntimeError("Missing closing front matter")

    for field in SOURCE_CARD_FIELDS:
        if not metadata.get(field):
            raise RuntimeError(f"Missing source card field: {field}")

    if path.parent.name == "sources" and path.name.startswith("src-"):
        expected_source_id = path.stem
        if metadata["source_id"] != expected_source_id:
            raise RuntimeError(
                f"Source card source_id does not match filename: {metadata['source_id']}"
            )
    return metadata


def upsert_source_map(paths: KnowledgeBasePaths, metadata: dict[str, str]) -> None:
    map_path = paths.meta / "source-map.jsonl"
    entries: list[dict[str, str]] = []
    if map_path.exists():
        for line in map_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("source_id") != metadata["source_id"]:
                    entries.append(entry)
    entry = {
        "source_id": metadata["source_id"],
        "title": metadata["title"],
        "raw_path": metadata["raw_path"],
        "sha256": metadata["sha256"],
        "imported_at": metadata["imported_at"],
        "kind": metadata["kind"],
    }
    for field in OPTIONAL_SOURCE_CARD_FIELDS:
        if metadata.get(field):
            entry[field] = metadata[field]
    entries.append(entry)
    content = "".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )
    map_path.write_text(content, encoding="utf-8")


def remove_source_map_entry(paths: KnowledgeBasePaths, source_id: str) -> None:
    map_path = paths.meta / "source-map.jsonl"
    if not map_path.exists():
        return
    entries: list[dict[str, str]] = []
    for line in map_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("source_id") != source_id:
            entries.append(entry)
    content = "".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )
    map_path.write_text(content, encoding="utf-8")


def source_metadata(paths: KnowledgeBasePaths, raw_path: Path, data: bytes) -> dict[str, str]:
    source_id, sha256 = source_id_and_sha256(data)
    text = data.decode("utf-8", errors="replace")
    return {
        "source_id": source_id,
        "title": title_for(raw_path, text),
        "raw_path": relative_path(paths.root, raw_path),
        "sha256": sha256,
        "imported_at": imported_timestamp(),
        "kind": kind_for_path(raw_path),
    }
