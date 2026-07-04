"""Generic multi-cloud backend, implemented via the `rclone` CLI over subprocess.

`rclone` supports 40+ providers (S3, GCS, Azure Blob, Dropbox, ...) behind one
interface, so this backend only needs a remote path such as
`myremote:bucket/prefix` that the caller has already configured with
`rclone config`. No provider-specific code lives in this skill.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import Backend, BackendError, RemoteObject


class RcloneBackend(Backend):
    def __init__(self, remote_path: str) -> None:
        if shutil.which("rclone") is None:
            raise BackendError(
                "the 'rclone' backend requires the 'rclone' CLI to be installed and on PATH"
            )
        self.remote_path = remote_path.rstrip("/")

    def list_objects(self) -> dict[str, RemoteObject]:
        result = self._rclone(["lsjson", "--recursive", "--hash", self.remote_path])
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise BackendError(f"could not parse rclone lsjson output: {exc}") from exc

        objects: dict[str, RemoteObject] = {}
        for entry in entries:
            if entry.get("IsDir"):
                continue
            relpath = entry["Path"].replace("\\", "/")
            hashes = entry.get("Hashes") or {}
            digest = hashes.get("sha256") or hashes.get("md5") or ""
            objects[relpath] = RemoteObject(
                relpath=relpath,
                hash=digest,
                size=int(entry.get("Size", 0)),
                mtime=_parse_rclone_timestamp(entry.get("ModTime")),
            )
        return objects

    def get_object(self, relpath: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "object"
            self._rclone(["copyto", f"{self.remote_path}/{relpath}", str(out_path)])
            return out_path.read_bytes()

    def put_object(self, relpath: str, data: bytes, *, mtime: float | None = None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "object"
            in_path.write_bytes(data)
            self._rclone(["copyto", str(in_path), f"{self.remote_path}/{relpath}"])

    def delete_object(self, relpath: str) -> None:
        self._rclone(["deletefile", f"{self.remote_path}/{relpath}"])

    def describe(self) -> dict[str, str]:
        return {"backend": "rclone", "remote_path": self.remote_path}

    def _rclone(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["rclone", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise BackendError(f"rclone {' '.join(args)} failed: {result.stderr.strip()}")
        return result


def _parse_rclone_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
