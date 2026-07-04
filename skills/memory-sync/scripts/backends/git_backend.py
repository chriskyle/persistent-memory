"""Uses a git remote/branch as a content-addressed, versioned object store.

A local working clone ("mirror") is kept under the sync control directory and
used as the source of truth for reads. Writes are staged into that working
tree and only committed + pushed once, when `flush()` is called at the end of
a `sync` pass -- so an entire sync becomes a single, atomic-looking commit.

This backend intentionally reuses whatever git/credential-helper setup is
already present in the environment (e.g. an authenticated `origin` remote in
a cloud agent sandbox), so it needs no new secrets beyond a reachable git
remote URL.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .base import Backend, BackendError, RemoteObject


class GitBackend(Backend):
    def __init__(self, remote: str, *, branch: str = "main", mirror_dir: Path | str) -> None:
        self.remote = remote
        self.branch = branch
        self.mirror_dir = Path(mirror_dir)
        self._dirty = False
        self._ensure_mirror()

    def list_objects(self) -> dict[str, RemoteObject]:
        objects: dict[str, RemoteObject] = {}
        for path in sorted(self.mirror_dir.rglob("*")):
            if not path.is_file():
                continue
            if ".git" in path.relative_to(self.mirror_dir).parts:
                continue
            relpath = path.relative_to(self.mirror_dir).as_posix()
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
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        self._dirty = True

    def delete_object(self, relpath: str) -> None:
        path = self._resolve(relpath)
        if path.exists():
            path.unlink()
            self._dirty = True

    def flush(self, message: str) -> None:
        if not self._dirty:
            return
        self._run("add", "-A")
        status = self._run("status", "--porcelain")
        if not status.stdout.strip():
            self._dirty = False
            return
        self._run("commit", "-m", message)
        self._run("push", "origin", f"HEAD:{self.branch}")
        self._dirty = False

    def describe(self) -> dict[str, str]:
        return {"backend": "git", "remote": self.remote, "branch": self.branch}

    def _ensure_mirror(self) -> None:
        if (self.mirror_dir / ".git").exists():
            self._run("fetch", "origin", self.branch)
            self._run("checkout", "-B", self.branch, f"origin/{self.branch}")
            return

        self.mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        cloned = subprocess.run(
            ["git", "clone", "--branch", self.branch, "--single-branch", self.remote, str(self.mirror_dir)],
            capture_output=True,
            text=True,
        )
        if cloned.returncode == 0:
            self._configure_identity()
            return

        # The remote branch may not exist yet (brand new remote store).
        # Initialize a fresh local repo and push the first commit ourselves.
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self._run("init")
        self._run("checkout", "-B", self.branch)
        self._configure_identity()
        self._run("remote", "add", "origin", self.remote)
        (self.mirror_dir / ".memory-sync-keep").write_text(
            "This file keeps the memory-sync remote branch non-empty.\n", encoding="utf-8"
        )
        self._run("add", "-A")
        self._run("commit", "-m", "memory-sync: initialize remote store")
        self._run("push", "-u", "origin", self.branch)

    def _configure_identity(self) -> None:
        self._run("config", "user.email", "agent-memory@local")
        self._run("config", "user.name", "agent-memory")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=self.mirror_dir, capture_output=True, text=True)
        if result.returncode != 0:
            raise BackendError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def _resolve(self, relpath: str) -> Path:
        candidate = (self.mirror_dir / relpath).resolve()
        if candidate != self.mirror_dir and self.mirror_dir not in candidate.parents:
            raise BackendError(f"refusing to access path outside backend root: {relpath}")
        return candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
