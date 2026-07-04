"""Embedding provider protocol for memory-vector."""

from __future__ import annotations

import abc
import dataclasses


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider cannot embed text."""


@dataclasses.dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str
    dim: int


class EmbeddingProvider(abc.ABC):
    @abc.abstractmethod
    def embed(self, text: str) -> EmbeddingResult:
        """Return an embedding vector for `text`."""
