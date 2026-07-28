"""OpenAI-compatible embedding adapter."""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping


DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS


def load_embedding_config(env: Mapping[str, str]) -> EmbeddingConfig:
    base_url = env.get("KB_EMBEDDING_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("KB_EMBEDDING_BASE_URL is required")

    model = env.get("KB_EMBEDDING_MODEL", "").strip()
    if not model:
        raise RuntimeError("KB_EMBEDDING_MODEL is required")

    timeout_text = env.get(
        "KB_EMBEDDING_TIMEOUT_SECONDS", str(DEFAULT_EMBEDDING_TIMEOUT_SECONDS)
    )
    try:
        timeout_seconds = float(timeout_text)
    except ValueError as exc:
        raise RuntimeError("KB_EMBEDDING_TIMEOUT_SECONDS must be a positive number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeError("KB_EMBEDDING_TIMEOUT_SECONDS must be a positive number")

    return EmbeddingConfig(
        base_url=base_url,
        model=model,
        api_key=env.get("KB_EMBEDDING_API_KEY") or None,
        timeout_seconds=timeout_seconds,
    )


class OpenAICompatibleEmbeddingClient:
    def __init__(self, config: EmbeddingConfig):
        self.config = config

    def embed(self, texts: list[str]) -> list[list[float]]:
        endpoint = f"{self.config.base_url.rstrip('/')}/embeddings"
        payload = json.dumps(
            {"model": self.config.model, "input": texts},
            ensure_ascii=False,
        ).encode("utf-8")
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
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(
                        f"Embedding request failed with HTTP {response.status}"
                    )
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RuntimeError(f"Embedding request failed with HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("Embedding request failed") from None
        except TimeoutError:
            raise RuntimeError("Embedding request timed out") from None
        except (http.client.HTTPException, OSError):
            raise RuntimeError(
                "Embedding request failed while reading response"
            ) from None

        try:
            data = json.loads(response_body.decode("utf-8"))
            rows = data["data"]
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("Embedding response was not valid JSON") from exc

        if not isinstance(rows, list):
            raise RuntimeError("Embedding response data was not a list")
        rows = sorted(rows, key=lambda row: row.get("index", 0) if isinstance(row, dict) else 0)
        vectors: list[list[float]] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
                raise RuntimeError("Embedding response missing data[].embedding")
            vector = row["embedding"]
            if not all(isinstance(value, (int, float)) for value in vector):
                raise RuntimeError("Embedding vector contains non-numeric values")
            vectors.append([float(value) for value in vector])
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding response count mismatch")
        return vectors
