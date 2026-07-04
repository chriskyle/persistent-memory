"""Google Drive backend, implemented as a thin provider-specific rclone wrapper.

Google Drive access requires OAuth. This backend intentionally delegates that
setup to rclone so credentials stay in the user's rclone config/keychain rather
than inside the memory-sync state directory or git history.
"""

from __future__ import annotations

from .rclone_backend import RcloneBackend


class GoogleDriveBackend(RcloneBackend):
    def __init__(self, remote_name: str, *, path: str = "") -> None:
        remote_path = _remote_path(remote_name, path)
        super().__init__(remote_path)
        self.remote_name = remote_name
        self.path = path.strip("/")

    def describe(self) -> dict[str, str]:
        return {"backend": "gdrive", "remote_name": self.remote_name, "path": self.path}


def _remote_path(remote_name: str, path: str) -> str:
    base = remote_name.rstrip(":")
    relpath = path.strip("/")
    return f"{base}:{relpath}" if relpath else f"{base}:"
