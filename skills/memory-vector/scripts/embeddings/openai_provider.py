"""Optional OpenAI embeddings provider using only the Python standard library."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import EmbeddingError, EmbeddingProvider, EmbeddingResult


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, *, model: str = "text-embedding-3-small", api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise EmbeddingError("OPENAI_API_KEY is required for the openai embedding provider")

    def embed(self, text: str) -> EmbeddingResult:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc

        try:
            vector = [float(v) for v in parsed["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError("OpenAI embedding response did not contain a valid embedding") from exc
        return EmbeddingResult(vector=vector, model=self.model, dim=len(vector))
