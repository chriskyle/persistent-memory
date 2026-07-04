"""S3-compatible backend, implemented via the `aws` CLI over subprocess.

Uses the CLI (rather than boto3) so this skill has no required third-party
dependency: if the `aws` binary and credentials are present, the backend
works; if not, `S3Backend.__init__` raises a clear `BackendError` telling the
agent what is missing.

Change detection uses the S3 ETag as a best-effort content fingerprint. For
the modest, single-part uploads this skill produces, ETag is the MD5 of the
object body, which is sufficient to answer "did this object's content
change since we last saw it". It is not treated as a cryptographic hash.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import Backend, BackendError, RemoteObject


class S3Backend(Backend):
    def __init__(self, bucket: str, *, prefix: str = "", region: str | None = None) -> None:
        if shutil.which("aws") is None:
            raise BackendError(
                "the 's3' backend requires the 'aws' CLI to be installed and on PATH"
            )
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region

    def list_objects(self) -> dict[str, RemoteObject]:
        args = ["s3api", "list-objects-v2", "--bucket", self.bucket]
        if self.prefix:
            args += ["--prefix", f"{self.prefix}/"]
        result = self._aws(args)
        payload = json.loads(result.stdout or "{}")
        objects: dict[str, RemoteObject] = {}
        for item in payload.get("Contents", []):
            key = item["Key"]
            relpath = key[len(self.prefix) + 1 :] if self.prefix else key
            if not relpath or relpath.endswith("/"):
                continue
            objects[relpath] = RemoteObject(
                relpath=relpath,
                hash=item.get("ETag", "").strip('"'),
                size=int(item.get("Size", 0)),
                mtime=_parse_s3_timestamp(item.get("LastModified")),
            )
        return objects

    def get_object(self, relpath: str) -> bytes:
        key = self._key(relpath)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "object"
            self._aws(["s3api", "get-object", "--bucket", self.bucket, "--key", key, str(out_path)])
            return out_path.read_bytes()

    def put_object(self, relpath: str, data: bytes, *, mtime: float | None = None) -> None:
        key = self._key(relpath)
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "object"
            in_path.write_bytes(data)
            self._aws(["s3api", "put-object", "--bucket", self.bucket, "--key", key, "--body", str(in_path)])

    def delete_object(self, relpath: str) -> None:
        key = self._key(relpath)
        self._aws(["s3api", "delete-object", "--bucket", self.bucket, "--key", key])

    def describe(self) -> dict[str, str]:
        return {"backend": "s3", "bucket": self.bucket, "prefix": self.prefix, "region": self.region or ""}

    def _key(self, relpath: str) -> str:
        return f"{self.prefix}/{relpath}" if self.prefix else relpath

    def _aws(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = ["aws", *args, "--output", "json"]
        if self.region:
            cmd += ["--region", self.region]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise BackendError(f"aws {' '.join(args)} failed: {result.stderr.strip()}")
        return result


def _parse_s3_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
