import json
from datetime import datetime
from pathlib import Path

from .commands import init_obsidian_vault, lint_repository, status_repository


EXOBRAIN_DIRECTORIES = (
    "inbox",
    "raw/imports",
    "raw/derived",
    "raw/originals",
    "raw/self-statements",
    "sources",
    "wiki/_drafts",
    "wiki/daily",
    "wiki/reviews/weekly",
    "wiki/reviews/monthly",
    "wiki/reviews/yearly",
    "wiki/goals",
    "wiki/people",
    "wiki/projects",
    "wiki/decisions",
    "wiki/agent-context",
    "meta/templates",
    "meta/assets",
    "db",
    "tools",
    "docs/reviews",
    ".obsidian",
)

TEMPLATE_TYPES = {
    "daily-log.md": "daily",
    "memory.md": "memory",
    "person.md": "person",
    "project.md": "project",
    "decision.md": "decision",
    "review.md": "review",
    "health-note.md": "health",
    "finance-note.md": "finance",
    "learning-note.md": "learning",
    "relationship-note.md": "relationship",
    "goal.md": "goal",
    "agent-context.md": "agent_context",
    "self-statement.md": "self_statement",
    "source-review.md": "source_review",
}

INITIAL_WIKI_PAGES = {
    "我.md": "我",
    "人生时间线.md": "人生时间线",
    "价值观与原则.md": "价值观与原则",
    "目标.md": "目标",
    "项目.md": "项目",
    "人际关系.md": "人际关系",
    "偏好.md": "偏好",
    "决策记录.md": "决策记录",
    "复盘.md": "复盘",
    "健康.md": "健康",
    "财务.md": "财务",
    "学习.md": "学习",
    "工作.md": "工作",
    "情绪与状态.md": "情绪与状态",
    "外脑使用手册.md": "外脑使用手册",
    "agent-context/我是谁.md": "我是谁",
}

GITIGNORE = """# Rebuildable local state
db/
*.sqlite
*.sqlite3
*.db

# Generated vector/model/OCR caches
meta/tmp/
meta/ocr/
meta/cache/
meta/vector/
*.ocr.txt
*.tmp

# Local application/runtime noise
.obsidian/workspace*.json
.trash/
__pycache__/
*.pyc
"""


def _timestamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _ensure_dir(path: Path, created_dirs: list[str]) -> None:
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"Expected directory: {path}")
    if not path.exists():
        path.mkdir(parents=True)
        created_dirs.append(str(path.resolve()))


def _was_created_this_run(path: Path, created_files: list[str]) -> bool:
    resolved = path.resolve()
    return any(Path(created).resolve() == resolved for created in created_files)


def _write_if_missing(path: Path, content: str, created_files: list[str]) -> None:
    if path.exists() and not path.is_file():
        raise RuntimeError(f"Expected file: {path}")
    if path.exists() and _was_created_this_run(path, created_files):
        path.write_text(content, encoding="utf-8")
        return
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created_files.append(str(path.resolve()))


def _front_matter(page_type: str, *, review_required: bool = False) -> str:
    fields: dict[str, object] = {
        "type": page_type,
        "created_at": _timestamp(),
        "event_date": "",
        "privacy": "personal",
        "confidence": "unknown",
        "source_ids": [],
        "status": "structure",
        "tags": [],
        "review_required": review_required,
    }
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    )
    lines.extend(["---", ""])
    return "\n".join(lines)


def _template_content(template_type: str) -> str:
    return _front_matter(template_type, review_required=True) + "# {{title}}\n"


def _wiki_content(page_type: str, title: str) -> str:
    return _front_matter(page_type) + f"# {title}\n"


def init_personal_exobrain(root: str | Path) -> dict[str, object]:
    """Create the first-stage personal exobrain profile without overwriting files."""
    root_path = Path(root).expanduser()
    root_existed = root_path.exists()
    result = init_obsidian_vault(root_path)
    created_dirs = list(result["created_dirs"])
    created_files = list(result["created_files"])

    for relative in EXOBRAIN_DIRECTORIES:
        _ensure_dir(root_path / relative, created_dirs)

    for filename, template_type in TEMPLATE_TYPES.items():
        _write_if_missing(
            root_path / "meta" / "templates" / filename,
            _template_content(template_type),
            created_files,
        )

    for relative, title in INITIAL_WIKI_PAGES.items():
        page_type = "agent_context" if relative.startswith("agent-context/") else "index"
        _write_if_missing(
            root_path / "wiki" / relative,
            _wiki_content(page_type, title),
            created_files,
        )

    _write_if_missing(root_path / ".gitignore", GITIGNORE, created_files)

    if not root_existed:
        lint_issues = lint_repository(root_path)
        status = status_repository(root_path)
        if lint_issues or status["lint_issues"]:
            raise RuntimeError("Personal exobrain initializer produced lint issues")

    return {
        "root": root_path.resolve(),
        "created_dirs": created_dirs,
        "created_files": created_files,
        "overwritten_files": [],
    }
