#!/usr/bin/env python3
"""Generic, pluggable cloud file-sync engine for persistent agent memory.

This script tracks a local directory (the "tracked root") against a
pluggable remote backend (localdir / git / rclone / dropbox / gdrive / s3), with content-hash
based change detection, automatic conflict handling that never silently
discards data, and content-hash based deduplication.

It knows nothing about *what* it is syncing -- the `memory-vector` skill (and
any other agent tooling) simply writes files under the tracked root and this
script keeps them mirrored to the cloud. This is intentional: it keeps the
hard, general problem (reliable file sync) in one place instead of
re-implemented per memory subsystem.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import enum
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backends.base import Backend, BackendError  # noqa: E402
from backends.dropbox_backend import DropboxBackend  # noqa: E402
from backends.git_backend import GitBackend  # noqa: E402
from backends.gdrive_backend import GoogleDriveBackend  # noqa: E402
from backends.localdir import LocalDirBackend  # noqa: E402
from backends.rclone_backend import RcloneBackend  # noqa: E402
from backends.s3_backend import S3Backend  # noqa: E402

CONFIG_VERSION = 1
MANIFEST_VERSION = 1
DEFAULT_CONFLICT_RETENTION_DAYS = 30

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    "**/.local/**",
    "**/*.sqlite3",
    "**/*.sqlite3-journal",
    "**/*.sqlite3-wal",
    "**/*.sqlite3-shm",
    "**/*.tmp",
    "**/.DS_Store",
)


class SyncStateError(RuntimeError):
    """Raised when local sync state is missing or inconsistent."""


class Action(str, enum.Enum):
    NOOP = "noop"
    PULL = "pull"
    PUSH = "push"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"
    CONFLICT = "conflict"


@dataclasses.dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def classify(local_hash: str | None, remote_hash: str | None, manifest_hash: str | None) -> Decision:
    """Pure 3-way classification of what to do with a single path.

    `manifest_hash` is the hash that was true on both sides as of the last
    successful sync (the common ancestor). Comparing local/remote against it
    is what lets us tell "one side changed" apart from "both sides changed
    independently" (a real conflict) using only content hashes.
    """
    if local_hash is None and remote_hash is None:
        return Decision(Action.NOOP, "absent on both sides")

    if local_hash is None:
        if manifest_hash is None:
            return Decision(Action.PULL, "new remote file")
        if manifest_hash == remote_hash:
            return Decision(Action.DELETE_REMOTE, "deleted locally since last sync")
        return Decision(Action.PULL, "resurrected: remote changed after local delete")

    if remote_hash is None:
        if manifest_hash is None:
            return Decision(Action.PUSH, "new local file")
        if manifest_hash == local_hash:
            return Decision(Action.DELETE_LOCAL, "deleted remotely since last sync")
        return Decision(Action.PUSH, "resurrected: local changed after remote delete")

    if local_hash == remote_hash:
        return Decision(Action.NOOP, "identical on both sides")
    if manifest_hash is None:
        return Decision(Action.CONFLICT, "created independently on both sides")
    if manifest_hash == local_hash:
        return Decision(Action.PULL, "local unchanged, remote updated")
    if manifest_hash == remote_hash:
        return Decision(Action.PUSH, "remote unchanged, local updated")
    return Decision(Action.CONFLICT, "changed independently on both sides")


# --------------------------------------------------------------------------
# Namespace + root resolution
# --------------------------------------------------------------------------


def default_namespace(cwd: Path) -> str:
    """Derive a stable namespace from repo identity, durable across branches.

    Unlike note-taker's per-branch session scoping, persistent memory should
    survive branch switches, so scoping is by repo/remote identity instead.
    """
    remote = _git_output(["config", "--get", "remote.origin.url"], cwd)
    if remote:
        return _slugify_remote(remote)
    root = _git_output(["rev-parse", "--show-toplevel"], cwd)
    if root:
        return _slugify(Path(root).name)
    return _slugify(cwd.name) or "default"


def _slugify_remote(remote: str) -> str:
    text = remote.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    text = text.split("://", 1)[-1]
    text = text.split("@", 1)[-1]
    text = text.replace(":", "/")
    parts = [p for p in text.split("/") if p]
    slug = "-".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "default")
    return _slugify(slug)


def _slugify(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text.lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "default"


def resolve_base_dir(env: Mapping[str, str], base_dir: Path | str | None) -> Path:
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    env_root = env.get("AGENT_MEMORY_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.home() / ".agent-memory"


def resolve_tracked_root(
    *,
    cwd: Path,
    env: Mapping[str, str],
    base_dir: Path | str | None,
    namespace: str | None,
) -> tuple[Path, str]:
    ns = namespace or env.get("AGENT_MEMORY_NAMESPACE", "").strip() or default_namespace(cwd)
    ns = _slugify(ns)
    root = resolve_base_dir(env, base_dir) / ns
    return root, ns


def _git_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


# --------------------------------------------------------------------------
# Local state: config, manifest, conflicts, device id
# --------------------------------------------------------------------------


def sync_dir(tracked_root: Path) -> Path:
    return tracked_root / ".sync"


def config_path(tracked_root: Path) -> Path:
    return sync_dir(tracked_root) / "config.json"


def manifest_path(tracked_root: Path) -> Path:
    return sync_dir(tracked_root) / "manifest.json"


def conflicts_path(tracked_root: Path) -> Path:
    return sync_dir(tracked_root) / "conflicts.json"


def device_id_path(tracked_root: Path) -> Path:
    return sync_dir(tracked_root) / "device_id"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncStateError(f"could not read {path}: {exc}") from exc


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_config(tracked_root: Path) -> dict[str, Any]:
    config = load_json(config_path(tracked_root), None)
    if config is None:
        raise SyncStateError(
            f"no memory-sync config at {config_path(tracked_root)}; run `init` first"
        )
    return config


def load_manifest(tracked_root: Path) -> dict[str, Any]:
    return load_json(manifest_path(tracked_root), {"version": MANIFEST_VERSION, "entries": {}, "last_sync_at": None})


def load_conflicts(tracked_root: Path) -> list[dict[str, Any]]:
    return load_json(conflicts_path(tracked_root), [])


def get_or_create_device_id(tracked_root: Path) -> str:
    path = device_id_path(tracked_root)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    new_id = uuid.uuid4().hex[:12]
    write_atomic(path, new_id + "\n")
    return new_id


# --------------------------------------------------------------------------
# Local tree walking + ignore patterns
# --------------------------------------------------------------------------


def is_ignored(relpath: str, patterns: tuple[str, ...]) -> bool:
    if relpath.startswith(".sync/") or relpath == ".sync":
        return True
    return any(fnmatch.fnmatch(relpath, pattern) for pattern in patterns)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_local_tree(tracked_root: Path, patterns: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not tracked_root.exists():
        return entries
    for path in sorted(tracked_root.rglob("*")):
        if not path.is_file():
            continue
        relpath = path.relative_to(tracked_root).as_posix()
        if is_ignored(relpath, patterns):
            continue
        stat = path.stat()
        entries[relpath] = {"hash": hash_file(path), "size": stat.st_size, "mtime": stat.st_mtime}
    return entries


# --------------------------------------------------------------------------
# Backend factory
# --------------------------------------------------------------------------


def build_backend(tracked_root: Path, config: Mapping[str, Any]) -> Backend:
    kind = config["backend"]
    cfg = config.get("backend_config", {})
    if kind == "localdir":
        return LocalDirBackend(cfg["path"])
    if kind == "git":
        mirror_dir = sync_dir(tracked_root) / "git-mirror"
        return GitBackend(cfg["remote"], branch=cfg.get("branch", "main"), mirror_dir=mirror_dir)
    if kind == "rclone":
        return RcloneBackend(cfg["remote_path"])
    if kind == "dropbox":
        return DropboxBackend(cfg["remote_name"], path=cfg.get("path", ""))
    if kind == "gdrive":
        return GoogleDriveBackend(cfg["remote_name"], path=cfg.get("path", ""))
    if kind == "s3":
        return S3Backend(cfg["bucket"], prefix=cfg.get("prefix", ""), region=cfg.get("region"))
    raise SyncStateError(f"unknown backend kind: {kind}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    env = os.environ
    tracked_root, namespace = resolve_tracked_root(
        cwd=args.cwd or Path.cwd(), env=env, base_dir=args.root, namespace=args.namespace
    )
    tracked_root.mkdir(parents=True, exist_ok=True)
    sync_dir(tracked_root).mkdir(parents=True, exist_ok=True)

    backend_config = _backend_config_from_args(args)
    existing = load_json(config_path(tracked_root), None)
    if existing is not None and existing.get("backend") != args.backend:
        raise SyncStateError(
            f"{config_path(tracked_root)} is already configured for backend "
            f"'{existing.get('backend')}'; cannot reinitialize as '{args.backend}'"
        )

    config = {
        "version": CONFIG_VERSION,
        "namespace": namespace,
        "backend": args.backend,
        "backend_config": backend_config,
        "conflict_policy": "newest-mtime",
        "ignore_patterns": list(DEFAULT_IGNORE_PATTERNS),
        "created_at": existing.get("created_at") if existing else _now_iso(),
    }
    write_json_atomic(config_path(tracked_root), config)
    get_or_create_device_id(tracked_root)

    warning = None
    try:
        backend = build_backend(tracked_root, config)
        _run_sync(tracked_root, config, backend, direction="pull", dry_run=False)
    except BackendError as exc:
        warning = str(exc)

    payload = {
        "tracked_root": str(tracked_root),
        "namespace": namespace,
        "backend": args.backend,
        "warning": warning,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Initialized memory-sync for namespace '{namespace}' at {tracked_root}")
        print(f"Backend: {args.backend} ({backend_config})")
        if warning:
            print(f"warning: initial pull failed: {warning}", file=sys.stderr)
    return 0


def _backend_config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.backend == "localdir":
        if not args.path:
            raise SyncStateError("--path is required for the localdir backend")
        return {"path": str(Path(args.path).expanduser())}
    if args.backend == "git":
        if not args.remote:
            raise SyncStateError("--remote is required for the git backend")
        return {"remote": args.remote, "branch": args.branch or "main"}
    if args.backend == "rclone":
        if not args.remote_path:
            raise SyncStateError("--remote-path is required for the rclone backend")
        return {"remote_path": args.remote_path}
    if args.backend in {"dropbox", "gdrive"}:
        if not args.remote_name:
            raise SyncStateError(f"--remote-name is required for the {args.backend} backend")
        return {"remote_name": args.remote_name, "path": args.provider_path or ""}
    if args.backend == "s3":
        if not args.bucket:
            raise SyncStateError("--bucket is required for the s3 backend")
        return {"bucket": args.bucket, "prefix": args.prefix or "", "region": args.region}
    raise SyncStateError(f"unknown backend: {args.backend}")


def cmd_sync(args: argparse.Namespace) -> int:
    env = os.environ
    tracked_root, _namespace = resolve_tracked_root(
        cwd=args.cwd or Path.cwd(), env=env, base_dir=args.root, namespace=args.namespace
    )
    config = load_config(tracked_root)
    backend = build_backend(tracked_root, config)
    report = _run_sync(tracked_root, config, backend, direction=args.direction, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"memory-sync: {tracked_root} ({'dry-run' if args.dry_run else args.direction})")
    for action, paths in report["applied"].items():
        if paths:
            print(f"  {action}: {len(paths)}")
            for path in paths[:20]:
                print(f"    - {path}")
    if report["conflicts"]:
        print(f"  conflicts: {len(report['conflicts'])} (see `status` / `resolve`)")
        for c in report["conflicts"][:20]:
            print(f"    - {c['path']} (winner={c.get('winner', 'pending')})")
    if report["pending"]:
        print(f"  pending (direction-limited): {len(report['pending'])}")
    return 0


def _run_sync(
    tracked_root: Path,
    config: dict[str, Any],
    backend: Backend,
    *,
    direction: str,
    dry_run: bool,
) -> dict[str, Any]:
    patterns = tuple(config.get("ignore_patterns", DEFAULT_IGNORE_PATTERNS))
    manifest = load_manifest(tracked_root)
    manifest_entries: dict[str, Any] = dict(manifest.get("entries", {}))
    conflicts_log = load_conflicts(tracked_root)
    conflicts_by_path = {c["path"]: c for c in conflicts_log}

    local = walk_local_tree(tracked_root, patterns)
    try:
        remote_objects = backend.list_objects()
    except BackendError:
        if direction == "pull" or not dry_run:
            raise
        remote_objects = {}
    remote = {relpath: {"hash": obj.hash, "size": obj.size, "mtime": obj.mtime} for relpath, obj in remote_objects.items()}

    all_paths = sorted(set(local) | set(remote) | set(manifest_entries))
    applied: dict[str, list[str]] = {a.value: [] for a in Action}
    pending: list[str] = []
    new_conflicts: list[dict[str, Any]] = []

    device_id = get_or_create_device_id(tracked_root)

    for relpath in all_paths:
        l_hash = local.get(relpath, {}).get("hash")
        r_hash = remote.get(relpath, {}).get("hash")
        m_hash = manifest_entries.get(relpath, {}).get("hash")
        decision = classify(l_hash, r_hash, m_hash)

        if decision.action == Action.NOOP:
            if l_hash is not None and manifest_entries.get(relpath, {}).get("hash") != l_hash:
                if not dry_run:
                    manifest_entries[relpath] = local[relpath]
            continue

        if decision.action == Action.CONFLICT:
            if dry_run or direction != "both":
                pending.append(relpath)
                new_conflicts.append(
                    {
                        "path": relpath,
                        "at": _now_iso(),
                        "local_hash": l_hash,
                        "remote_hash": r_hash,
                        "resolved": False,
                    }
                )
                continue
            conflict_record = _resolve_conflict(
                tracked_root,
                backend,
                relpath,
                local.get(relpath),
                remote.get(relpath),
                device_id=device_id,
                policy=config.get("conflict_policy", "newest-mtime"),
            )
            manifest_entries[relpath] = conflict_record["canonical_entry"]
            if conflict_record.get("conflict_relpath"):
                manifest_entries[conflict_record["conflict_relpath"]] = conflict_record["conflict_entry"]
                applied[Action.PUSH.value].append(conflict_record["conflict_relpath"])
            applied[Action.CONFLICT.value].append(relpath)
            new_conflicts.append(
                {
                    "path": relpath,
                    "at": _now_iso(),
                    "local_hash": l_hash,
                    "remote_hash": r_hash,
                    "resolved": True,
                    "winner": conflict_record["winner"],
                    "conflict_path": conflict_record.get("conflict_relpath"),
                }
            )
            continue

        if decision.action == Action.PULL and direction in ("both", "pull"):
            if not dry_run:
                data = backend.get_object(relpath)
                _write_local_file(tracked_root / relpath, data)
                manifest_entries[relpath] = remote[relpath]
            applied[Action.PULL.value].append(relpath)
        elif decision.action == Action.PUSH and direction in ("both", "push"):
            if not dry_run:
                data = (tracked_root / relpath).read_bytes()
                backend.put_object(relpath, data, mtime=local[relpath]["mtime"])
                manifest_entries[relpath] = local[relpath]
            applied[Action.PUSH.value].append(relpath)
        elif decision.action == Action.DELETE_LOCAL and direction in ("both", "pull"):
            if not dry_run:
                (tracked_root / relpath).unlink(missing_ok=True)
                manifest_entries.pop(relpath, None)
            applied[Action.DELETE_LOCAL.value].append(relpath)
        elif decision.action == Action.DELETE_REMOTE and direction in ("both", "push"):
            if not dry_run:
                backend.delete_object(relpath)
                manifest_entries.pop(relpath, None)
            applied[Action.DELETE_REMOTE.value].append(relpath)
        else:
            pending.append(relpath)

    if not dry_run:
        backend.flush(f"memory-sync: {config.get('namespace', 'default')} @ {_now_iso()}")
        manifest["version"] = MANIFEST_VERSION
        manifest["entries"] = manifest_entries
        manifest["last_sync_at"] = _now_iso()
        write_json_atomic(manifest_path(tracked_root), manifest)

        for record in new_conflicts:
            conflicts_by_path[record["path"]] = record
        write_json_atomic(conflicts_path(tracked_root), list(conflicts_by_path.values()))

    return {
        "tracked_root": str(tracked_root),
        "direction": direction,
        "dry_run": dry_run,
        "applied": applied,
        "conflicts": new_conflicts,
        "pending": pending,
    }


def _write_local_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(path)


def _resolve_conflict(
    tracked_root: Path,
    backend: Backend,
    relpath: str,
    local_entry: dict[str, Any] | None,
    remote_entry: dict[str, Any] | None,
    *,
    device_id: str,
    policy: str,
) -> dict[str, Any]:
    """Deterministically pick a canonical side and preserve the other.

    Nothing is ever discarded: the losing side is written next to the
    canonical file as `<name>.conflict-<device>-<timestamp><suffix>` and
    queued to be pushed too, so every device eventually sees both.
    """
    local_mtime = (local_entry or {}).get("mtime", 0.0)
    remote_mtime = (remote_entry or {}).get("mtime", 0.0)
    winner = "remote" if remote_mtime > local_mtime else "local"

    local_path = tracked_root / relpath
    local_bytes = local_path.read_bytes() if local_path.exists() else b""
    remote_bytes = backend.get_object(relpath)

    if winner == "local":
        canonical_bytes, loser_bytes = local_bytes, remote_bytes
        canonical_entry = local_entry
        backend.put_object(relpath, canonical_bytes, mtime=local_mtime)
    else:
        canonical_bytes, loser_bytes = remote_bytes, local_bytes
        canonical_entry = remote_entry
        _write_local_file(local_path, canonical_bytes)

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = local_path.suffix
    stem_path = relpath[: -len(suffix)] if suffix else relpath
    conflict_relpath = f"{stem_path}.conflict-{device_id}-{timestamp}{suffix}"
    _write_local_file(tracked_root / conflict_relpath, loser_bytes)
    backend.put_object(conflict_relpath, loser_bytes)
    conflict_hash = hashlib.sha256(loser_bytes).hexdigest()

    return {
        "winner": winner,
        "canonical_entry": canonical_entry or {"hash": hashlib.sha256(canonical_bytes).hexdigest(), "size": len(canonical_bytes), "mtime": time.time()},
        "conflict_relpath": conflict_relpath,
        "conflict_entry": {"hash": conflict_hash, "size": len(loser_bytes), "mtime": time.time()},
    }


def cmd_status(args: argparse.Namespace) -> int:
    env = os.environ
    tracked_root, namespace = resolve_tracked_root(
        cwd=args.cwd or Path.cwd(), env=env, base_dir=args.root, namespace=args.namespace
    )
    config = load_config(tracked_root)
    backend = build_backend(tracked_root, config)
    report = _run_sync(tracked_root, config, backend, direction="both", dry_run=True)
    manifest = load_manifest(tracked_root)
    conflicts_log = [c for c in load_conflicts(tracked_root) if not c.get("resolved")]

    payload = {
        "tracked_root": str(tracked_root),
        "namespace": namespace,
        "backend": backend.describe(),
        "last_sync_at": manifest.get("last_sync_at"),
        "pending_pull": report["applied"][Action.PULL.value] + report["applied"][Action.DELETE_LOCAL.value],
        "pending_push": report["applied"][Action.PUSH.value] + report["applied"][Action.DELETE_REMOTE.value],
        "unresolved_conflicts": conflicts_log,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Namespace: {namespace}")
    print(f"Tracked root: {tracked_root}")
    print(f"Backend: {backend.describe()}")
    print(f"Last sync: {payload['last_sync_at'] or 'never'}")
    print(f"Pending pull: {len(payload['pending_pull'])}")
    print(f"Pending push: {len(payload['pending_push'])}")
    print(f"Unresolved conflicts: {len(conflicts_log)}")
    for c in conflicts_log:
        print(f"  - {c['path']}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    env = os.environ
    tracked_root, _namespace = resolve_tracked_root(
        cwd=args.cwd or Path.cwd(), env=env, base_dir=args.root, namespace=args.namespace
    )
    config = load_config(tracked_root)
    backend = build_backend(tracked_root, config)
    manifest = load_manifest(tracked_root)
    manifest_entries: dict[str, Any] = dict(manifest.get("entries", {}))
    conflicts_log = load_conflicts(tracked_root)
    device_id = get_or_create_device_id(tracked_root)

    local_path = tracked_root / args.path
    found = False
    for record in conflicts_log:
        if record["path"] != args.path or record.get("resolved"):
            continue
        found = True
        if args.keep == "local":
            data = local_path.read_bytes()
            backend.put_object(args.path, data)
            manifest_entries[args.path] = {"hash": hashlib.sha256(data).hexdigest(), "size": len(data), "mtime": local_path.stat().st_mtime}
            record["winner"] = "local"
        elif args.keep == "remote":
            data = backend.get_object(args.path)
            _write_local_file(local_path, data)
            manifest_entries[args.path] = {"hash": hashlib.sha256(data).hexdigest(), "size": len(data), "mtime": time.time()}
            record["winner"] = "remote"
        else:  # both
            local_entry = manifest_entries.get(args.path)
            resolved = _resolve_conflict(
                tracked_root,
                backend,
                args.path,
                {"mtime": local_path.stat().st_mtime} if local_path.exists() else None,
                local_entry,
                device_id=device_id,
                policy=config.get("conflict_policy", "newest-mtime"),
            )
            manifest_entries[args.path] = resolved["canonical_entry"]
            record["winner"] = resolved["winner"]
            record["conflict_path"] = resolved.get("conflict_relpath")
        record["resolved"] = True
        record["resolved_at"] = _now_iso()
        backend.flush(f"memory-sync: resolve {args.path}")

    if not found:
        print(f"No unresolved conflict recorded for '{args.path}'.", file=sys.stderr)
        return 1

    manifest["entries"] = manifest_entries
    write_json_atomic(manifest_path(tracked_root), manifest)
    write_json_atomic(conflicts_path(tracked_root), conflicts_log)
    print(f"Resolved '{args.path}' keeping {args.keep}.")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    env = os.environ
    tracked_root, _namespace = resolve_tracked_root(
        cwd=args.cwd or Path.cwd(), env=env, base_dir=args.root, namespace=args.namespace
    )
    config = load_config(tracked_root)
    patterns = tuple(config.get("ignore_patterns", DEFAULT_IGNORE_PATTERNS))

    by_hash: dict[str, list[Path]] = {}
    for path in sorted(tracked_root.rglob("*")):
        if not path.is_file():
            continue
        relpath = path.relative_to(tracked_root).as_posix()
        if is_ignored(relpath, patterns):
            continue
        by_hash.setdefault(hash_file(path), []).append(path)

    dedup_count = 0
    for _digest, paths in by_hash.items():
        if len(paths) < 2:
            continue
        canonical = paths[0]
        for duplicate in paths[1:]:
            try:
                duplicate.unlink()
                os.link(canonical, duplicate)
                dedup_count += 1
            except OSError:
                continue

    pruned_count = 0
    if not args.dedupe_only:
        cutoff = time.time() - (args.retention_days * 86400)
        for path in tracked_root.rglob("*.conflict-*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                pruned_count += 1

        if config.get("backend") == "git":
            mirror_dir = sync_dir(tracked_root) / "git-mirror"
            if (mirror_dir / ".git").exists():
                subprocess.run(["git", "gc", "--quiet"], cwd=mirror_dir, check=False)

    print(f"Deduplicated {dedup_count} file(s); pruned {pruned_count} old conflict file(s).")
    return 0


def _now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync agent files/memory between local disk and a cloud backend.")
    parser.add_argument("--root", type=Path, default=None, help="Override AGENT_MEMORY_ROOT (base dir for all namespaces).")
    parser.add_argument("--namespace", default=None, help="Override the derived namespace.")
    parser.add_argument("--cwd", type=Path, default=None, help="Resolve repo identity from this directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize tracking against a cloud backend.")
    init_parser.add_argument(
        "--backend",
        required=True,
        choices=["localdir", "git", "rclone", "dropbox", "gdrive", "s3"],
    )
    init_parser.add_argument("--path", help="localdir backend: directory to treat as the cloud.")
    init_parser.add_argument("--remote", help="git backend: remote URL.")
    init_parser.add_argument("--branch", help="git backend: branch name (default main).")
    init_parser.add_argument("--remote-path", help="rclone backend: e.g. myremote:bucket/prefix.")
    init_parser.add_argument("--remote-name", help="dropbox/gdrive backend: configured rclone remote name.")
    init_parser.add_argument("--provider-path", help="dropbox/gdrive backend: optional folder/path inside the remote.")
    init_parser.add_argument("--bucket", help="s3 backend: bucket name.")
    init_parser.add_argument("--prefix", help="s3 backend: key prefix.")
    init_parser.add_argument("--region", help="s3 backend: AWS region.")
    init_parser.add_argument("--json", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    sync_parser = subparsers.add_parser("sync", help="Sync local <-> remote.")
    sync_parser.add_argument("--direction", choices=["both", "push", "pull"], default="both")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--json", action="store_true")
    sync_parser.set_defaults(func=cmd_sync)

    status_parser = subparsers.add_parser("status", help="Show pending changes and unresolved conflicts.")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    resolve_parser = subparsers.add_parser("resolve", help="Manually resolve a flagged conflict.")
    resolve_parser.add_argument("path", help="Relative path (as reported by `status`).")
    resolve_parser.add_argument("--keep", required=True, choices=["local", "remote", "both"])
    resolve_parser.set_defaults(func=cmd_resolve)

    gc_parser = subparsers.add_parser("gc", help="Deduplicate local files and prune old conflict copies.")
    gc_parser.add_argument("--dedupe-only", action="store_true")
    gc_parser.add_argument("--retention-days", type=float, default=DEFAULT_CONFLICT_RETENTION_DAYS)
    gc_parser.set_defaults(func=cmd_gc)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SyncStateError, BackendError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
