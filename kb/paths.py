from dataclasses import dataclass
from pathlib import Path


WRITE_LOCK_RELATIVE_PATH = "meta/.kb-write.lock"
RUNTIME_ONLY_RELATIVE_PATHS = (
    WRITE_LOCK_RELATIVE_PATH,
)
GENERATED_GITIGNORE_PATTERNS = (
    "# Rebuildable local state",
    "db/",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "",
    "# Runtime locks and product temp files",
    WRITE_LOCK_RELATIVE_PATH,
    "*.tmp",
    "meta/tmp/",
    "meta/runtime/",
    "",
    "# Product runtime audit/cache files",
    "meta/audit/",
    "meta/cache/",
    "meta/logs/",
    "meta/llm-audit.jsonl",
    "",
    "# Generated vector/model/OCR caches",
    "meta/ocr/",
    "meta/vector/",
    "*.ocr.txt",
    "",
    "# Local application/runtime noise",
    ".obsidian/workspace*.json",
    ".trash/",
    "__pycache__/",
    "*.pyc",
)


def generated_gitignore_content() -> str:
    return "\n".join(GENERATED_GITIGNORE_PATTERNS).rstrip() + "\n"


def is_runtime_only_path(root: str | Path, path: str | Path) -> bool:
    root_path = Path(root).expanduser().resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root_path / target
    try:
        relative = target.resolve(strict=False).relative_to(root_path)
    except ValueError:
        return False
    normalized = relative.as_posix()
    return normalized in RUNTIME_ONLY_RELATIVE_PATHS


@dataclass(frozen=True)
class KnowledgeBasePaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def meta(self) -> Path:
        return self.root / "meta"

    @property
    def write_lock(self) -> Path:
        return self.root / WRITE_LOCK_RELATIVE_PATH

    @property
    def db(self) -> Path:
        return self.root / "db"

    @property
    def database(self) -> Path:
        return self.db / "kb.sqlite3"

    @property
    def drafts(self) -> Path:
        return self.wiki / "_drafts"

    @property
    def raw_self_statements(self) -> Path:
        return self.raw / "self-statements"

    @property
    def wiki_daily(self) -> Path:
        return self.wiki / "daily"

    @property
    def wiki_reviews(self) -> Path:
        return self.wiki / "reviews"

    @property
    def wiki_goals(self) -> Path:
        return self.wiki / "goals"

    @property
    def wiki_people(self) -> Path:
        return self.wiki / "people"

    @property
    def wiki_projects(self) -> Path:
        return self.wiki / "projects"

    @property
    def wiki_decisions(self) -> Path:
        return self.wiki / "decisions"

    @property
    def wiki_agent_context(self) -> Path:
        return self.wiki / "agent-context"
