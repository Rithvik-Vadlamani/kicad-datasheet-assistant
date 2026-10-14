from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import numpy as np

from .config import Settings


def request_json(url: str, payload: dict, *, api_key: str | None = None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        # No automatic retries: a timed-out generation may already have incurred cost.
        with httpx.Client(timeout=httpx.Timeout(180, connect=10), follow_redirects=False) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as error:
        raise ValueError(f"Model service returned HTTP {error.response.status_code}; check model, key, quota and service logs.") from None
    except httpx.RequestError:
        raise ValueError("Cannot reach model service. Start Ollama or check network/API configuration.") from None


class Provider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ollama(self, endpoint: str) -> str:
        url = urlparse(self.settings.ollama_url)
        if url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Local Ollama backend requires a loopback URL")
        return self.settings.ollama_url.rstrip("/") + endpoint

    @staticmethod
    def key() -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("Set OPENAI_API_KEY in your environment; do not put secrets in project.json")
        return key

    def embed(self, texts: list[str]) -> np.ndarray:
        s = self.settings
        if s.embedding_provider == "none":
            raise ValueError("Semantic search is disabled. Configure an embedding provider then index again.")
        if s.embedding_provider == "local":
            from .retrieval_adapters import embed_local
            result = embed_local(s.embedding_model, texts)
            norms = np.linalg.norm(result, axis=1, keepdims=True)
            if not np.isfinite(result).all() or (norms == 0).any():
                raise ValueError("Local embedding model returned invalid vectors")
            return result / norms
        vectors = []
        for start in range(0, len(texts), 24):
            batch = texts[start:start + 24]
            if s.embedding_provider == "ollama":
                data = request_json(self.ollama("/api/embed"), {
                    "model": s.embedding_model, "input": batch, "truncate": False,
                })
                rows = data["embeddings"]
            else:
                data = request_json("https://api.openai.com/v1/embeddings", {
                    "model": s.embedding_model, "input": batch, "encoding_format": "float",
                }, api_key=self.key())
                rows = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
            if len(rows) != len(batch):
                raise ValueError("Embedding service returned an incorrect vector count")
            vectors.extend(rows)
        result = np.asarray(vectors, dtype=np.float32)
        if result.ndim != 2 or not np.isfinite(result).all():
            raise ValueError("Embedding service returned malformed vectors")
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("Embedding service returned a zero vector")
        return result / norms

    def generate(self, system: str, context: str) -> str:
        s = self.settings
        if s.chat_provider == "none":
            raise ValueError("Configure a chat provider or use ask --evidence-only")
        if s.chat_provider == "ollama":
            data = request_json(self.ollama("/api/chat"), {
                "model": s.chat_model, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": context}],
                "options": {"temperature": 0.1, "num_predict": s.max_output_tokens,
                            "num_ctx": 32768},
            })
            if data.get("done_reason") == "length":
                raise ValueError("Model answer was truncated; increase max_output_tokens or narrow the question")
            result = data["message"]["content"]
        else:
            data = request_json("https://api.openai.com/v1/responses", {
                "model": s.chat_model, "instructions": system, "input": context,
                "max_output_tokens": s.max_output_tokens, "store": False,
            }, api_key=self.key())
            if data.get("status") != "completed":
                raise ValueError("Cloud response did not complete. Check model output budget and service status.")
            result = "\n".join(c["text"] for item in data.get("output", [])
                               if item.get("type") == "message" for c in item.get("content", [])
                               if c.get("type") == "output_text")
        if not result.strip():
            raise ValueError("Model returned no answer")
        return result
