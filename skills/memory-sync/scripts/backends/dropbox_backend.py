"""Dropbox backend, implemented as a thin provider-specific rclone wrapper.

Dropbox access requires OAuth. Rather than storing OAuth refresh tokens or
provider-specific secrets inside this skill, configure a Dropbox remote with
`rclone config` and pass the remote name/path to `memory-sync init`.
"""

from __future__ import annotations

from .rclone_backend import RcloneBackend


class DropboxBackend(RcloneBackend):
    def __init__(self, remote_name: str, *, path: str = "") -> None:
        remote_path = _remote_path(remote_name, path)
        super().__init__(remote_path)
        self.remote_name = remote_name
        self.path = path.strip("/")

    def describe(self) -> dict[str, str]:
        return {"backend": "dropbox", "remote_name": self.remote_name, "path": self.path}


def _remote_path(remote_name: str, path: str) -> str:
    base = remote_name.rstrip(":")
    relpath = path.strip("/")
    return f"{base}:{relpath}" if relpath else f"{base}:"
