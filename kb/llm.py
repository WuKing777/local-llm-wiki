"""OpenAI-compatible LLM adapter."""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TEMPERATURE = 0.2
READ_RETRY_ATTEMPTS = 3
ALLOWED_RESPONSE_FORMATS = {"json_object"}
ALLOWED_THINKING = {"enabled", "disabled"}
ALLOWED_REASONING_EFFORTS = {"high", "max"}


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    response_format: str | None = None
    max_tokens: int | None = None
    thinking: str | None = None
    reasoning_effort: str | None = None


def _optional_choice(env: Mapping[str, str], name: str, allowed: set[str]) -> str | None:
    value = env.get(name, "").strip()
    if not value:
        return None
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _optional_positive_int(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def load_llm_config(env: Mapping[str, str]) -> LLMConfig:
    base_url = env.get("KB_LLM_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("KB_LLM_BASE_URL is required")

    model = env.get("KB_LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("KB_LLM_MODEL is required")

    timeout_text = env.get("KB_LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = float(timeout_text)
    except ValueError as exc:
        raise RuntimeError("KB_LLM_TIMEOUT_SECONDS must be a positive number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeError("KB_LLM_TIMEOUT_SECONDS must be a positive number")

    api_key = env.get("KB_LLM_API_KEY") or None
    if "api.deepseek.com" in base_url.lower() and not api_key:
        raise RuntimeError("KB_LLM_API_KEY is required for DeepSeek API")
    return LLMConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        response_format=_optional_choice(
            env, "KB_LLM_RESPONSE_FORMAT", ALLOWED_RESPONSE_FORMATS
        ),
        max_tokens=_optional_positive_int(env, "KB_LLM_MAX_TOKENS"),
        thinking=_optional_choice(env, "KB_LLM_THINKING", ALLOWED_THINKING),
        reasoning_effort=_optional_choice(
            env, "KB_LLM_REASONING_EFFORT", ALLOWED_REASONING_EFFORTS
        ),
    )


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(self, messages: list[dict[str, str]]) -> str:
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        request_body: dict[str, object] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
        }
        if self.config.response_format:
            request_body["response_format"] = {"type": self.config.response_format}
        if self.config.max_tokens:
            request_body["max_tokens"] = self.config.max_tokens
        if self.config.thinking:
            request_body["thinking"] = {"type": self.config.thinking}
        if self.config.reasoning_effort:
            request_body["reasoning_effort"] = self.config.reasoning_effort

        payload = json.dumps(request_body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        response_body = self._read_response_with_retries(request)

        try:
            data = json.loads(response_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError("LLM response was not valid JSON") from None

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM response missing choices[0].message.content") from exc
        if not isinstance(content, str) or not content:
            raise RuntimeError("LLM response content was empty")
        return content

    def _read_response_with_retries(self, request: urllib.request.Request) -> bytes:
        last_error = "LLM request failed while reading response"
        for attempt in range(READ_RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError(
                            f"LLM request failed with HTTP {response.status}"
                        )
                    return response.read()
            except urllib.error.HTTPError as exc:
                exc.close()
                raise RuntimeError(f"LLM request failed with HTTP {exc.code}") from None
            except TimeoutError:
                last_error = "LLM request timed out"
            except urllib.error.URLError:
                last_error = "LLM request failed"
            except (http.client.HTTPException, OSError):
                last_error = "LLM request failed while reading response"

            if attempt == READ_RETRY_ATTEMPTS - 1:
                break

        raise RuntimeError(last_error) from None
