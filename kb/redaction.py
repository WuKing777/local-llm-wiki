"""Production-safe text redaction helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_ENV_SECRET_NAMES = ("KB_LLM_API_KEY", "KB_EMBEDDING_API_KEY")
_SECRET_NAME_RE = re.compile(
    r"(api[_-]?key|secret|token|password|credential|bearer)", re.IGNORECASE
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTH_BEARER_RE = re.compile(
    r"(?i)(\bAuthorization\s*:\s*Bearer\s+)([^\s,;]+)"
)
_QUOTED_SECRET_FIELD_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?\b
        (?:api[_-]?key|password|token|access[_-]?token|refresh[_-]?token|secret)
        \b["']?\s*[:=]\s*
    )
    (?P<quote>["'])
    (?P<value>[^"'\r\n]*)
    (?P=quote)
    """
)
_UNQUOTED_SECRET_FIELD_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        \b(?:api[_-]?key|password|token|access[_-]?token|refresh[_-]?token|secret)
        \b\s*[:=]\s*
    )
    (?P<value>[^\s,;}]+)
    """
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_.-]{5,}(?=$|[\s,;)\]}'\"])")
_TRUNCATION_MARKER = "[truncated]"


def _environment_secret_values(env: Mapping[str, str] | None) -> list[str]:
    source = os.environ if env is None else env
    values: list[str] = []

    for name in _ENV_SECRET_NAMES:
        value = source.get(name)
        if value:
            values.append(str(value))

    for name, value in source.items():
        if not value or name in _ENV_SECRET_NAMES:
            continue
        text_value = str(value)
        if len(text_value) >= 8 and _SECRET_NAME_RE.search(str(name)):
            values.append(text_value)

    return sorted(set(values), key=len, reverse=True)


def _redacted_secret_field_value(value: str) -> str:
    if value.startswith("[redacted-") and value.endswith("]"):
        return value
    return "[redacted-secret]"


def redact_text(text: object, env: dict[str, str] | None = None) -> str:
    """Return text with known secret shapes replaced by stable markers."""

    redacted = str(text)

    for secret in _environment_secret_values(env):
        redacted = redacted.replace(secret, "[redacted-env-secret]")

    redacted = _PRIVATE_KEY_RE.sub("[redacted-private-key]", redacted)
    redacted = _AUTH_BEARER_RE.sub(r"\1[redacted-bearer-token]", redacted)
    redacted = _QUOTED_SECRET_FIELD_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_redacted_secret_field_value(match.group('value'))}"
            f"{match.group('quote')}"
        ),
        redacted,
    )
    redacted = _UNQUOTED_SECRET_FIELD_RE.sub(
        lambda match: (
            f"{match.group('prefix')}"
            f"{_redacted_secret_field_value(match.group('value'))}"
        ),
        redacted,
    )
    return _API_KEY_RE.sub("[redacted-api-key]", redacted)


def summarize_text(
    text: object, *, limit: int = 500, env: dict[str, str] | None = None
) -> str:
    """Redact text, then return a bounded summary suitable for persistence."""

    redacted = redact_text(text, env=env)
    if len(redacted) <= limit:
        return redacted
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER
    return redacted[: limit - len(_TRUNCATION_MARKER)].rstrip() + _TRUNCATION_MARKER
