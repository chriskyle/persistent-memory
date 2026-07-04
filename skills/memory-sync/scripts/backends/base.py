"""Backend protocol shared by every memory-sync remote storage provider.

A backend is a flat, path-addressed object store. Paths are POSIX-style
relative paths from the tracked root, e.g. ``memory-vector/shards/abc.jsonl``.
Backends do not need to understand sync semantics (conflicts, manifests,
dedup) -- that logic lives once in ``memory_sync.py`` and is reused across
every backend.
"""

from __future__ import annotations

import abc
import dataclasses


class BackendError(RuntimeError):
    """Raised when a backend cannot complete a requested operation."""


@dataclasses.dataclass(frozen=True)
class RemoteObject:
    """Metadata for a single object as seen in the remote store."""

    relpath: str
    hash: str
    size: int
    mtime: float


class Backend(abc.ABC):
    """Minimal object-storage interface every memory-sync backend must implement."""

    @abc.abstractmethod
    def list_objects(self) -> dict[str, RemoteObject]:
        """Return metadata for every object currently in the remote."""

    @abc.abstractmethod
    def get_object(self, relpath: str) -> bytes:
        """Fetch the raw bytes of a single remote object."""

    @abc.abstractmethod
    def put_object(self, relpath: str, data: bytes, *, mtime: float | None = None) -> None:
        """Upload/overwrite a single remote object."""

    @abc.abstractmethod
    def delete_object(self, relpath: str) -> None:
        """Delete a single remote object. Must not raise if already absent."""

    def flush(self, message: str) -> None:
        """Finalize a batch of put/delete calls.

        Backends with transactional semantics (e.g. git: commit + push)
        override this. Backends that write through immediately (localdir,
        s3, rclone) can leave this as a no-op.
        """
        return None

    def describe(self) -> dict[str, str]:
        """Human-readable backend identity, used by the `status` command."""
        return {"backend": type(self).__name__}
