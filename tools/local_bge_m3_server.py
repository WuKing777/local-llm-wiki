"""Local OpenAI-compatible embedding server for BAAI/bge-m3.

This is an operational adapter for the knowledge-base CLI. It performs local
inference only and exposes the minimal `/v1/embeddings` shape used by kb.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.environ.get("BGE_M3_MODEL", "BAAI/bge-m3")
MODEL_ALIAS = os.environ.get("BGE_M3_MODEL_ALIAS", "bge-m3")
CACHE_FOLDER = os.environ.get("BGE_M3_CACHE_DIR") or None

app = FastAPI(title="Local bge-m3 embeddings")
_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status_code=status_code,
    )


def _load_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(MODEL_NAME, cache_folder=CACHE_FOLDER)
    return _model


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_ALIAS}


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return _error("Request body must be valid JSON")

    requested_model = str(payload.get("model", "")).strip()
    if requested_model not in {MODEL_ALIAS, MODEL_NAME}:
        return _error("Unsupported embedding model")

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        texts = [raw_input]
    elif isinstance(raw_input, list) and all(isinstance(item, str) for item in raw_input):
        texts = raw_input
    else:
        return _error("input must be a string or a list of strings")
    if not texts:
        return _error("input must not be empty")

    try:
        model = _load_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    except Exception:
        return _error("Local embedding inference failed", status_code=500)

    return JSONResponse(
        {
            "object": "list",
            "model": requested_model,
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": vector.astype(float).tolist(),
                }
                for index, vector in enumerate(vectors)
            ],
        }
    )
