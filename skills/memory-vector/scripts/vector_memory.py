#!/usr/bin/env python3
"""Append-only semantic memory store for AI agents.

The durable state is a set of per-device JSONL shards plus tombstone logs.
Those files are safe to sync with `memory-sync`: each device only appends to
its own shard, embeddings are embedded inside each memory record, and the
SQLite index is a rebuildable local cache that is intentionally not synced.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from embeddings.base import (  # noqa: E402
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResult,
)
from embeddings.hashing import HashingEmbeddingProvider, tokenize  # noqa: E402
from embeddings.openai_provider import OpenAIEmbeddingProvider  # noqa: E402

CONFIG_VERSION = 1
INDEX_VERSION = 1
DEFAULT_DIM = 256
DEFAULT_MAX_TEXT_CHARS = 12_000
DEFAULT_DUPLICATE_THRESHOLD = 0.95


class MemoryStateError(RuntimeError):
    """Raised when memory-vector state is missing or inconsistent."""


@dataclasses.dataclass(frozen=True)
class MemoryPaths:
    base_root: Path
    namespace: str

    @property
    def root(self) -> Path:
        return self.base_root / self.namespace / "memory-vector"

    @property
    def shards_dir(self) -> Path:
        return self.root / "shards"

    @property
    def tombstones_dir(self) -> Path:
        return self.root / "tombstones"

    @property
    def local_dir(self) -> Path:
        return self.root / ".local"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def index_path(self) -> Path:
        return self.root / "index.sqlite3"

    @property
    def device_id_path(self) -> Path:
        return self.local_dir / "device_id"

    @property
    def state_path(self) -> Path:
        return self.local_dir / "state.json"


# --------------------------------------------------------------------------
# Namespace/root resolution
# --------------------------------------------------------------------------


def resolve_base_dir(env: Mapping[str, str], base_dir: Path | str | None) -> Path:
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    env_root = env.get("AGENT_MEMORY_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.home() / ".agent-memory"


def default_namespace(cwd: Path) -> str:
    remote = _git_output(["config", "--get", "remote.origin.url"], cwd)
    if remote:
        return _slugify_remote(remote)
    root = _git_output(["rev-parse", "--show-toplevel"], cwd)
    if root:
        return _slugify(Path(root).name)
    return _slugify(cwd.name) or "default"


def resolve_paths(
    *, cwd: Path, env: Mapping[str, str], base_dir: Path | str | None, namespace: str | None
) -> MemoryPaths:
    ns = namespace or env.get("AGENT_MEMORY_NAMESPACE", "").strip() or default_namespace(cwd)
    return MemoryPaths(base_root=resolve_base_dir(env, base_dir), namespace=_slugify(ns))


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
# State helpers
# --------------------------------------------------------------------------


def init_memory(paths: MemoryPaths, *, provider_name: str, dim: int, max_text_chars: int) -> dict[str, Any]:
    paths.shards_dir.mkdir(parents=True, exist_ok=True)
    paths.tombstones_dir.mkdir(parents=True, exist_ok=True)
    paths.local_dir.mkdir(parents=True, exist_ok=True)
    device_id = get_or_create_device_id(paths)
    existing = load_json(paths.config_path, None)
    config = {
        "version": CONFIG_VERSION,
        "namespace": paths.namespace,
        "embedding_provider": provider_name,
        "embedding_dim": dim,
        "max_text_chars": max_text_chars,
        "duplicate_threshold": DEFAULT_DUPLICATE_THRESHOLD,
        "created_at": existing.get("created_at") if isinstance(existing, dict) else _now_iso(),
    }
    write_json_atomic(paths.config_path, config)
    if not paths.state_path.exists():
        write_json_atomic(paths.state_path, {"next_seq": 1})
    return {"root": str(paths.root), "namespace": paths.namespace, "device_id": device_id}


def ensure_initialized(paths: MemoryPaths) -> dict[str, Any]:
    config = load_json(paths.config_path, None)
    if not isinstance(config, dict):
        raise MemoryStateError(f"no memory-vector config at {paths.config_path}; run `init` first")
    for directory in (paths.shards_dir, paths.tombstones_dir, paths.local_dir):
        directory.mkdir(parents=True, exist_ok=True)
    get_or_create_device_id(paths)
    return config


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryStateError(f"could not read {path}: {exc}") from exc


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def get_or_create_device_id(paths: MemoryPaths) -> str:
    if paths.device_id_path.exists():
        existing = paths.device_id_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    device_id = uuid.uuid4().hex[:12]
    write_atomic(paths.device_id_path, device_id + "\n")
    return device_id


def next_seq(paths: MemoryPaths) -> int:
    state = load_json(paths.state_path, {"next_seq": 1})
    value = int(state.get("next_seq", 1))
    write_json_atomic(paths.state_path, {"next_seq": value + 1})
    return value


def shard_path(paths: MemoryPaths) -> Path:
    return paths.shards_dir / f"{get_or_create_device_id(paths)}.jsonl"


def tombstone_path(paths: MemoryPaths) -> Path:
    return paths.tombstones_dir / f"{get_or_create_device_id(paths)}.jsonl"


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------


def build_provider(config: Mapping[str, Any]) -> EmbeddingProvider:
    provider = str(config.get("embedding_provider", "hash"))
    if provider == "hash":
        return HashingEmbeddingProvider(dim=int(config.get("embedding_dim", DEFAULT_DIM)))
    if provider == "openai":
        model = os.environ.get("AGENT_MEMORY_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        return OpenAIEmbeddingProvider(model=model)
    raise MemoryStateError(f"unknown embedding provider: {provider}")


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(v * v for v in left))
    right_norm = math.sqrt(sum(v * v for v in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


# --------------------------------------------------------------------------
# Index rebuild and reads
# --------------------------------------------------------------------------


def source_fingerprint(paths: MemoryPaths) -> str:
    items: list[str] = []
    for root in (paths.shards_dir, paths.tombstones_dir):
        for path in sorted(root.glob("*.jsonl")):
            stat = path.stat()
            relpath = path.relative_to(paths.root).as_posix()
            items.append(f"{relpath}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def connect_index(paths: MemoryPaths) -> sqlite3.Connection:
    paths.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.index_path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            text TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            importance INTEGER NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            occurrences INTEGER NOT NULL,
            ttl_seconds INTEGER,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            embedding_json TEXT NOT NULL,
            supersedes_json TEXT NOT NULL,
            device_id TEXT NOT NULL,
            seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tombstones (
            target_id TEXT PRIMARY KEY,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
        """
    )


def needs_reindex(paths: MemoryPaths) -> bool:
    if not paths.index_path.exists():
        return True
    with connect_index(paths) as conn:
        initialize_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='source_fingerprint'").fetchone()
        return row is None or row["value"] != source_fingerprint(paths)


def ensure_index(paths: MemoryPaths) -> None:
    if needs_reindex(paths):
        reindex(paths)


def reindex(paths: MemoryPaths) -> int:
    fingerprint = source_fingerprint(paths)
    with connect_index(paths) as conn:
        initialize_schema(conn)
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM tombstones")
        conn.execute("DELETE FROM meta")

        events = sorted(read_events(paths), key=lambda e: (e.get("created_at", ""), e.get("device_id", ""), e.get("seq", 0)))
        inserted = 0
        for event in events:
            kind = event.get("event", "memory")
            if kind == "memory":
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memories (
                        id, content_hash, text, tags_json, importance, source, created_at,
                        last_seen_at, occurrences, ttl_seconds, embedding_model, embedding_dim,
                        embedding_json, supersedes_json, device_id, seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["id"],
                        event["content_hash"],
                        event["text"],
                        json.dumps(event.get("tags", []), sort_keys=True),
                        int(event.get("importance", 3)),
                        event.get("source"),
                        event["created_at"],
                        event.get("last_seen_at", event["created_at"]),
                        int(event.get("occurrences", 1)),
                        event.get("ttl_seconds"),
                        event["embedding_model"],
                        int(event["embedding_dim"]),
                        json.dumps(event["embedding"], separators=(",", ":")),
                        json.dumps(event.get("supersedes", []), sort_keys=True),
                        event.get("device_id", ""),
                        int(event.get("seq", 0)),
                    ),
                )
                inserted += 1
                for target_id in event.get("supersedes", []):
                    conn.execute(
                        "INSERT OR REPLACE INTO tombstones (target_id, reason, created_at) VALUES (?, ?, ?)",
                        (target_id, f"superseded by {event['id']}", event["created_at"]),
                    )
            elif kind == "touch":
                conn.execute(
                    """
                    UPDATE memories
                    SET occurrences = occurrences + 1, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (event["created_at"], event["target_id"]),
                )

        for tombstone in read_tombstones(paths):
            conn.execute(
                "INSERT OR REPLACE INTO tombstones (target_id, reason, created_at) VALUES (?, ?, ?)",
                (tombstone["target_id"], tombstone.get("reason"), tombstone.get("created_at", _now_iso())),
            )

        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('index_version', ?)", (str(INDEX_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('source_fingerprint', ?)", (fingerprint,))
        conn.commit()
    return inserted


def read_events(paths: MemoryPaths) -> Iterable[dict[str, Any]]:
    for path in sorted(paths.shards_dir.glob("*.jsonl")):
        yield from read_jsonl(path)


def read_tombstones(paths: MemoryPaths) -> Iterable[dict[str, Any]]:
    for path in sorted(paths.tombstones_dir.glob("*.jsonl")):
        yield from read_jsonl(path)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryStateError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if isinstance(parsed, dict):
                yield parsed


def live_memory_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT m.*
        FROM memories m
        LEFT JOIN tombstones t ON t.target_id = m.id
        WHERE t.target_id IS NULL
        ORDER BY m.created_at DESC
        """
    ).fetchall()
    now = _now()
    return [row for row in rows if not _is_expired(row, now)]


def _is_expired(row: sqlite3.Row, now: dt.datetime) -> bool:
    ttl = row["ttl_seconds"]
    if ttl is None:
        return False
    try:
        created = parse_timestamp(row["created_at"])
    except ValueError:
        return False
    return created + dt.timedelta(seconds=int(ttl)) < now


# --------------------------------------------------------------------------
# Memory operations
# --------------------------------------------------------------------------


def remember(
    paths: MemoryPaths,
    config: Mapping[str, Any],
    *,
    text: str,
    tags: list[str],
    importance: int,
    source: str | None,
    ttl_seconds: int | None,
    supersedes: list[str],
    threshold: float | None,
) -> dict[str, Any]:
    text = text.strip()
    max_chars = int(config.get("max_text_chars", DEFAULT_MAX_TEXT_CHARS))
    if not text:
        raise MemoryStateError("memory text cannot be empty")
    if len(text) > max_chars:
        raise MemoryStateError(f"memory text is too long ({len(text)} chars > max {max_chars})")
    if not 1 <= importance <= 5:
        raise MemoryStateError("--importance must be between 1 and 5")

    ensure_index(paths)
    provider = build_provider(config)
    embedding = provider.embed(text)
    content_hash = "sha256:" + hashlib.sha256(_canonical_text(text).encode("utf-8")).hexdigest()
    duplicate_threshold = threshold if threshold is not None else float(config.get("duplicate_threshold", DEFAULT_DUPLICATE_THRESHOLD))

    with connect_index(paths) as conn:
        initialize_schema(conn)
        exact = conn.execute(
            """
            SELECT m.*
            FROM memories m
            LEFT JOIN tombstones t ON t.target_id = m.id
            WHERE m.content_hash = ? AND t.target_id IS NULL
            ORDER BY m.created_at DESC
            LIMIT 1
            """,
            (content_hash,),
        ).fetchone()
        if exact is not None:
            append_touch(paths, exact["id"], reason="exact-duplicate")
            reindex(paths)
            return {"id": exact["id"], "deduped": True, "reason": "exact-duplicate"}

        near = find_near_duplicate(conn, embedding, tags, duplicate_threshold)
        if near is not None:
            append_touch(paths, near["id"], reason=f"near-duplicate:{near['similarity']:.3f}")
            reindex(paths)
            return {
                "id": near["id"],
                "deduped": True,
                "reason": "near-duplicate",
                "similarity": round(float(near["similarity"]), 4),
            }

    device_id = get_or_create_device_id(paths)
    seq = next_seq(paths)
    memory_id = "mem_" + hashlib.sha256(f"{device_id}:{seq}:{time.time_ns()}:{content_hash}".encode()).hexdigest()[:20]
    timestamp = _now_iso()
    record = {
        "event": "memory",
        "id": memory_id,
        "seq": seq,
        "created_at": timestamp,
        "last_seen_at": timestamp,
        "device_id": device_id,
        "namespace": paths.namespace,
        "text": text,
        "tags": sorted(set(tags)),
        "importance": importance,
        "source": source,
        "ttl_seconds": ttl_seconds,
        "content_hash": content_hash,
        "embedding_model": embedding.model,
        "embedding_dim": embedding.dim,
        "embedding": embedding.vector,
        "supersedes": supersedes,
        "occurrences": 1,
    }
    append_jsonl(shard_path(paths), record)
    for target_id in supersedes:
        append_tombstone(paths, target_id, reason=f"superseded by {memory_id}")
    reindex(paths)
    return {"id": memory_id, "deduped": False, "path": str(shard_path(paths))}


def append_touch(paths: MemoryPaths, target_id: str, *, reason: str) -> None:
    append_jsonl(
        shard_path(paths),
        {
            "event": "touch",
            "target_id": target_id,
            "reason": reason,
            "seq": next_seq(paths),
            "device_id": get_or_create_device_id(paths),
            "created_at": _now_iso(),
        },
    )


def append_tombstone(paths: MemoryPaths, target_id: str, *, reason: str | None) -> str:
    tombstone_id = "del_" + uuid.uuid4().hex[:20]
    append_jsonl(
        tombstone_path(paths),
        {
            "id": tombstone_id,
            "target_id": target_id,
            "reason": reason,
            "created_at": _now_iso(),
            "device_id": get_or_create_device_id(paths),
            "seq": next_seq(paths),
        },
    )
    return tombstone_id


def find_near_duplicate(
    conn: sqlite3.Connection, embedding: EmbeddingResult, tags: list[str], threshold: float
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    tag_set = set(tags)
    for row in live_memory_rows(conn):
        if row["embedding_dim"] != embedding.dim:
            continue
        if tag_set:
            row_tags = set(json.loads(row["tags_json"]))
            if row_tags and tag_set.isdisjoint(row_tags):
                continue
        similarity = cosine(json.loads(row["embedding_json"]), embedding.vector)
        if similarity >= threshold and (best is None or similarity > best["similarity"]):
            best = {"id": row["id"], "similarity": similarity}
    return best


def recall(
    paths: MemoryPaths,
    config: Mapping[str, Any],
    *,
    query: str,
    top_k: int,
    tags: list[str],
    min_score: float,
    since: str | None,
) -> list[dict[str, Any]]:
    ensure_index(paths)
    provider = build_provider(config)
    query_embedding = provider.embed(query)
    query_tokens = set(tokenize(query))
    tag_filter = set(tags)
    since_dt = parse_since(since) if since else None

    results: list[dict[str, Any]] = []
    with connect_index(paths) as conn:
        initialize_schema(conn)
        for row in live_memory_rows(conn):
            row_tags = set(json.loads(row["tags_json"]))
            if tag_filter and tag_filter.isdisjoint(row_tags):
                continue
            created_at = parse_timestamp(row["created_at"])
            if since_dt and created_at < since_dt:
                continue
            vector_score = cosine(json.loads(row["embedding_json"]), query_embedding.vector)
            lexical_score = lexical_overlap(query_tokens, set(tokenize(row["text"])))
            importance_score = (int(row["importance"]) - 1) / 4
            recency_score = recency_boost(row["last_seen_at"])
            score = (0.60 * vector_score) + (0.25 * lexical_score) + (0.10 * importance_score) + (0.05 * recency_score)
            if score < min_score:
                continue
            results.append(
                {
                    "id": row["id"],
                    "score": round(score, 4),
                    "vector_score": round(vector_score, 4),
                    "lexical_score": round(lexical_score, 4),
                    "importance": int(row["importance"]),
                    "occurrences": int(row["occurrences"]),
                    "created_at": row["created_at"],
                    "last_seen_at": row["last_seen_at"],
                    "tags": sorted(row_tags),
                    "source": row["source"],
                    "text": row["text"],
                }
            )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:top_k]


def lexical_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def recency_boost(timestamp: str) -> float:
    try:
        age_seconds = max((_now() - parse_timestamp(timestamp)).total_seconds(), 0.0)
    except ValueError:
        return 0.0
    # Half-life-ish: recent memories get a small lift, old ones still score via content.
    return 1.0 / (1.0 + (age_seconds / (30 * 86400)))


def list_memories(paths: MemoryPaths, *, limit: int) -> list[dict[str, Any]]:
    ensure_index(paths)
    with connect_index(paths) as conn:
        initialize_schema(conn)
        rows = live_memory_rows(conn)[:limit]
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "tags": json.loads(row["tags_json"]),
                "importance": int(row["importance"]),
                "occurrences": int(row["occurrences"]),
                "text": row["text"],
            }
            for row in rows
        ]


def stats(paths: MemoryPaths) -> dict[str, Any]:
    ensure_index(paths)
    with connect_index(paths) as conn:
        initialize_schema(conn)
        live_count = len(live_memory_rows(conn))
        total_count = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        tombstone_count = conn.execute("SELECT COUNT(*) AS c FROM tombstones").fetchone()["c"]
        models = conn.execute(
            "SELECT embedding_model, embedding_dim, COUNT(*) AS c FROM memories GROUP BY embedding_model, embedding_dim"
        ).fetchall()
    return {
        "root": str(paths.root),
        "namespace": paths.namespace,
        "live_memories": live_count,
        "total_memories": total_count,
        "tombstones": tombstone_count,
        "shards": len(list(paths.shards_dir.glob("*.jsonl"))),
        "embedding_models": [
            {"model": row["embedding_model"], "dim": row["embedding_dim"], "count": row["c"]} for row in models
        ],
        "index_path": str(paths.index_path),
    }


def consolidate(paths: MemoryPaths, *, similarity: float) -> list[list[dict[str, Any]]]:
    ensure_index(paths)
    clusters: list[list[dict[str, Any]]] = []
    visited: set[str] = set()
    with connect_index(paths) as conn:
        initialize_schema(conn)
        rows = live_memory_rows(conn)
        vectors = {row["id"]: json.loads(row["embedding_json"]) for row in rows}
        for row in rows:
            if row["id"] in visited:
                continue
            cluster = [row]
            visited.add(row["id"])
            for other in rows:
                if other["id"] in visited or other["id"] == row["id"]:
                    continue
                if row["embedding_dim"] != other["embedding_dim"]:
                    continue
                if cosine(vectors[row["id"]], vectors[other["id"]]) >= similarity:
                    cluster.append(other)
                    visited.add(other["id"])
            if len(cluster) > 1:
                clusters.append(
                    [
                        {
                            "id": item["id"],
                            "tags": json.loads(item["tags_json"]),
                            "text": item["text"],
                        }
                        for item in cluster
                    ]
                )
    return clusters


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return sorted({_slugify(tag.strip()) for tag in raw.split(",") if tag.strip()})


def parse_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_duration(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    match = re.fullmatch(r"(\d+)([smhdw]?)", raw.strip())
    if not match:
        raise MemoryStateError("duration must look like 30s, 10m, 12h, 7d, or 4w")
    value = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return value * multiplier


def parse_since(raw: str) -> dt.datetime:
    if re.fullmatch(r"\d+[smhdw]?", raw.strip()):
        duration = parse_duration(raw)
        if duration is not None:
            return _now() - dt.timedelta(seconds=duration)
    return parse_timestamp(raw)


def parse_timestamp(value: str) -> dt.datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _canonical_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds").replace("+00:00", "Z")


def format_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No relevant memories found.\n"
    lines: list[str] = []
    for item in results:
        lines.append(f"- {item['id']} score={item['score']} tags={','.join(item['tags']) or '-'}")
        lines.append(f"  {item['text']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def paths_from_args(args: argparse.Namespace) -> MemoryPaths:
    return resolve_paths(
        cwd=args.cwd or Path.cwd(),
        env=os.environ,
        base_dir=args.root,
        namespace=args.namespace,
    )


def cmd_init(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    payload = init_memory(
        paths,
        provider_name=args.embedding_provider,
        dim=args.dim,
        max_text_chars=args.max_text_chars,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Initialized memory-vector namespace '{paths.namespace}' at {paths.root}")
        print(f"Device: {payload['device_id']}")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    config = ensure_initialized(paths)
    result = remember(
        paths,
        config,
        text=args.text,
        tags=parse_tags(args.tags),
        importance=args.importance,
        source=args.source,
        ttl_seconds=parse_duration(args.ttl),
        supersedes=parse_ids(args.supersedes),
        threshold=args.duplicate_threshold,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["deduped"]:
        print(f"Deduplicated into existing memory {result['id']} ({result['reason']}).")
    else:
        print(f"Remembered {result['id']}.")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    config = ensure_initialized(paths)
    results = recall(
        paths,
        config,
        query=args.query,
        top_k=args.top_k,
        tags=parse_tags(args.tags),
        min_score=args.min_score,
        since=args.since,
    )
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(format_results(results), end="")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    ensure_initialized(paths)
    tombstone_id = append_tombstone(paths, args.id, reason=args.reason)
    reindex(paths)
    print(f"Forgot {args.id} ({tombstone_id}).")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    ensure_initialized(paths)
    memories = list_memories(paths, limit=args.limit)
    if args.json:
        print(json.dumps(memories, indent=2, sort_keys=True))
    else:
        for item in memories:
            print(f"- {item['id']} tags={','.join(item['tags']) or '-'} occurrences={item['occurrences']}")
            print(f"  {item['text']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    ensure_initialized(paths)
    payload = stats(paths)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    ensure_initialized(paths)
    clusters = consolidate(paths, similarity=args.similarity)
    if args.json:
        print(json.dumps(clusters, indent=2, sort_keys=True))
    elif not clusters:
        print("No consolidation clusters found.")
    else:
        for index, cluster in enumerate(clusters, start=1):
            print(f"Cluster {index}:")
            print("  Supersede with: python <skill-dir>/scripts/vector_memory.py remember \"<summary>\" --supersedes " + ",".join(item["id"] for item in cluster))
            for item in cluster:
                print(f"  - {item['id']}: {item['text']}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    paths = paths_from_args(args)
    ensure_initialized(paths)
    count = reindex(paths)
    print(f"Reindexed {count} memory record(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store and recall persistent semantic memory for AI agents.")
    parser.add_argument("--root", type=Path, default=None, help="Override AGENT_MEMORY_ROOT.")
    parser.add_argument("--namespace", default=None, help="Override AGENT_MEMORY_NAMESPACE / derived repo namespace.")
    parser.add_argument("--cwd", type=Path, default=None, help="Resolve repo identity from this directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize the vector memory store.")
    init_parser.add_argument("--embedding-provider", choices=["hash", "openai"], default=os.environ.get("AGENT_MEMORY_EMBEDDING_PROVIDER", "hash"))
    init_parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="Hash embedding dimension.")
    init_parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS)
    init_parser.add_argument("--json", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    remember_parser = subparsers.add_parser("remember", help="Store a durable memory.")
    remember_parser.add_argument("text")
    remember_parser.add_argument("--tags", help="Comma-separated tags.")
    remember_parser.add_argument("--importance", type=int, default=3, help="1 (low) through 5 (critical).")
    remember_parser.add_argument("--source")
    remember_parser.add_argument("--ttl", help="Optional TTL such as 30d or 4w.")
    remember_parser.add_argument("--supersedes", help="Comma-separated memory ids superseded by this one.")
    remember_parser.add_argument("--duplicate-threshold", type=float, default=None)
    remember_parser.add_argument("--json", action="store_true")
    remember_parser.set_defaults(func=cmd_remember)

    recall_parser = subparsers.add_parser("recall", help="Search memories by semantic + lexical relevance.")
    recall_parser.add_argument("query")
    recall_parser.add_argument("--top-k", type=int, default=8)
    recall_parser.add_argument("--tags", help="Comma-separated required tags.")
    recall_parser.add_argument("--since", help="ISO timestamp or duration such as 30d.")
    recall_parser.add_argument("--min-score", type=float, default=0.2)
    recall_parser.add_argument("--json", action="store_true")
    recall_parser.set_defaults(func=cmd_recall)

    forget_parser = subparsers.add_parser("forget", help="Append a tombstone for a memory id.")
    forget_parser.add_argument("id")
    forget_parser.add_argument("--reason")
    forget_parser.set_defaults(func=cmd_forget)

    list_parser = subparsers.add_parser("list", help="List recent live memories.")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    stats_parser = subparsers.add_parser("stats", help="Show store health and counts.")
    stats_parser.set_defaults(func=cmd_stats)

    consolidate_parser = subparsers.add_parser("consolidate", help="Print near-duplicate clusters for agent-authored summaries.")
    consolidate_parser.add_argument("--similarity", type=float, default=0.92)
    consolidate_parser.add_argument("--json", action="store_true")
    consolidate_parser.set_defaults(func=cmd_consolidate)

    reindex_parser = subparsers.add_parser("reindex", help="Rebuild the local sqlite cache from shards/tombstones.")
    reindex_parser.set_defaults(func=cmd_reindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (MemoryStateError, EmbeddingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
