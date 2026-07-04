"""Zero-dependency backend that treats a local/mounted directory as "the cloud".

This works unmodified with network drives, rclone/FUSE mounts, or any other
path that behaves like a filesystem. It is also what the test suite uses to
simulate a real cloud backend without network access or credentials.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .base import Backend, BackendError, RemoteObject


class LocalDirBackend(Backend):
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_objects(self) -> dict[str, RemoteObject]:
        objects: dict[str, RemoteObject] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relpath = path.relative_to(self.root).as_posix()
            stat = path.stat()
            objects[relpath] = RemoteObject(
                relpath=relpath,
                hash=_hash_file(path),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        return objects

    def get_object(self, relpath: str) -> bytes:
        path = self._resolve(relpath)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise BackendError(f"remote object missing: {relpath}") from exc

    def put_object(self, relpath: str, data: bytes, *, mtime: float | None = None) -> None:
        path = self._resolve(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def delete_object(self, relpath: str) -> None:
        path = self._resolve(relpath)
        path.unlink(missing_ok=True)

    def describe(self) -> dict[str, str]:
        return {"backend": "localdir", "path": str(self.root)}

    def _resolve(self, relpath: str) -> Path:
        candidate = (self.root / relpath).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise BackendError(f"refusing to access path outside backend root: {relpath}")
        return candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
