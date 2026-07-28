import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath, PureWindowsPath

from .wiki import UNSAFE_TARGET_ROOTS


TITLE_KINDS = {
    "goal": ("{title} goal", "{title}", r"goals\{title}"),
    "person": ("{title} person relationship", "{title}", r"people\{title}"),
    "project": ("{title} project memory", "{title}", r"projects\{title}"),
    "decision": ("{title} decision", "{title}", r"decisions\{title}"),
    "preference-summary": (
        "{title} preference decision pattern",
        "{title}",
        "{title}",
    ),
}


@dataclass(frozen=True)
class PersonalCompileRequest:
    kind: str
    query: str
    title: str
    target: str
    context_limit: int = 5


def _safe_title_component(title: str) -> str:
    value = title.strip()
    if not value:
        raise RuntimeError("title is required")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        value.startswith(("/", "\\"))
        or windows.is_absolute()
        or posix.is_absolute()
        or windows.drive
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or ".." in value
        or value.casefold() in UNSAFE_TARGET_ROOTS
    ):
        raise RuntimeError("Unsafe target")
    return value


def _valid_daily(value: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise RuntimeError("date must be YYYY-MM-DD") from None


def _valid_period(kind: str, value: str) -> str:
    period = value.strip()
    patterns = {
        "weekly-review": r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$",
        "monthly-review": r"^\d{4}-(0[1-9]|1[0-2])$",
        "yearly-review": r"^\d{4}$",
    }
    if not re.fullmatch(patterns[kind], period):
        raise RuntimeError("period has invalid format")
    return period


def personal_compile_request(
    *, kind: str, title: str = "", date: str = "", period: str = ""
) -> PersonalCompileRequest:
    normalized = kind.strip().casefold()
    if normalized == "daily":
        value = _valid_daily(date)
        return PersonalCompileRequest(
            kind=normalized,
            query=f"{value} daily",
            title=value,
            target=rf"daily\{value}",
        )
    if normalized in {"weekly-review", "monthly-review", "yearly-review"}:
        value = _valid_period(normalized, period)
        label = normalized.removesuffix("-review").replace("-", " ")
        subdir = label.split()[0]
        return PersonalCompileRequest(
            kind=normalized,
            query=f"{value} {label} review",
            title=value,
            target=rf"reviews\{subdir}\{value}",
        )
    if normalized == "agent-context":
        value = _safe_title_component(title)
        return PersonalCompileRequest(
            kind=normalized,
            query="self_statement_raw confirmed",
            title=value,
            target=rf"agent-context\{value}",
            context_limit=25,
        )
    if normalized in TITLE_KINDS:
        value = _safe_title_component(title)
        query_template, title_template, target_template = TITLE_KINDS[normalized]
        return PersonalCompileRequest(
            kind=normalized,
            query=query_template.format(title=value),
            title=title_template.format(title=value),
            target=target_template.format(title=value),
        )
    raise RuntimeError(f"Invalid compile kind: {kind}")
