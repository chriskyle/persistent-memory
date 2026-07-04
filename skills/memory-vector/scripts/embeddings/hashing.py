"""Deterministic, zero-dependency feature-hashing embeddings.

This is not a replacement for a learned embedding model, but it gives the
memory skill useful lexical/semantic-ish recall without network access,
credentials, package installation, or non-determinism. Because the vectors are
stored in the synced JSONL records, every agent sees identical embeddings for
the same text.
"""

from __future__ import annotations

import hashlib
import math
import re

from .base import EmbeddingProvider, EmbeddingResult

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_/-]*", re.IGNORECASE)


class HashingEmbeddingProvider(EmbeddingProvider):
    def __init__(self, *, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dim = dim

    def embed(self, text: str) -> EmbeddingResult:
        vector = [0.0] * self.dim
        tokens = tokenize(text)
        features = list(tokens)
        features.extend(f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False))
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dim
            sign = -1.0 if (value >> 63) else 1.0
            vector[index] += sign
        normalize_in_place(vector)
        return EmbeddingResult(vector=vector, model="hash-v1", dim=self.dim)


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text)]


def normalize_in_place(vector: list[float]) -> None:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return
    for i, value in enumerate(vector):
        vector[i] = value / norm
