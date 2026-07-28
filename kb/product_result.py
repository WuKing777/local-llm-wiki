"""Serializable productization result helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from kb.redaction import redact_text


_REDACTED_SECRET = "[redacted-secret]"
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_SECRET_KEY_WORDS = {
    "bearer",
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
}
_SECRET_TOKEN_CONTEXT_WORDS = {
    "access",
    "api",
    "auth",
    "authorization",
    "bearer",
    "client",
    "csrf",
    "id",
    "jwt",
    "oauth",
    "refresh",
    "session",
}
_NON_SECRET_TOKEN_CONTEXT_WORDS = {
    "budget",
    "budgets",
    "count",
    "counts",
    "limit",
    "limits",
    "max",
    "maximum",
    "remaining",
    "total",
    "totals",
    "usage",
    "used",
}


def _key_words(value: object) -> list[str]:
    if isinstance(value, str):
        text = value
    elif isinstance(value, os.PathLike):
        path_value = os.fspath(value)
        text = path_value if isinstance(path_value, str) else str(path_value)
    else:
        return []
    return [
        word.casefold()
        for word in _KEY_WORD_RE.findall(_CAMEL_CASE_BOUNDARY_RE.sub("_", text))
    ]


def _has_adjacent_words(
    words: list[str], first: str, second_options: set[str]
) -> bool:
    return any(
        word == first and words[index + 1] in second_options
        for index, word in enumerate(words[:-1])
    )


def _is_secret_like_key(value: object) -> bool:
    words = _key_words(value)
    if not words:
        return False

    word_set = set(words)
    if word_set & _SECRET_KEY_WORDS:
        return True
    if "apikey" in word_set or _has_adjacent_words(words, "api", {"key", "keys"}):
        return True
    if "token" in word_set:
        if len(words) == 1:
            return True
        if (
            word_set & _NON_SECRET_TOKEN_CONTEXT_WORDS
            and not word_set & _SECRET_TOKEN_CONTEXT_WORDS
        ):
            return False
        return True
    if "tokens" in word_set and word_set & _SECRET_TOKEN_CONTEXT_WORDS:
        return True
    return False


def _safe_dict_key(value: object) -> str:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, os.PathLike):
        return redact_text(os.fspath(value))
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    return f"[non-json-key:{type(value).__name__}]"


def _json_safe(value: object, *, under_secret_key: bool = False) -> object:
    if isinstance(value, list):
        return [_json_safe(item, under_secret_key=under_secret_key) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item, under_secret_key=under_secret_key) for item in value]
    if isinstance(value, dict):
        return {
            _safe_dict_key(key): _json_safe(
                item,
                under_secret_key=under_secret_key or _is_secret_like_key(key),
            )
            for key, item in value.items()
        }
    if under_secret_key:
        return _REDACTED_SECRET
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, os.PathLike):
        return redact_text(os.fspath(value))
    return f"[non-json:{type(value).__name__}]"


@dataclass(frozen=True)
class ProductResult:
    status: str
    classification: str
    summary: str
    severity: str = "info"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": redact_text(self.status),
            "classification": redact_text(self.classification),
            "summary": redact_text(self.summary),
            "severity": redact_text(self.severity),
            "details": _json_safe(self.details),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
