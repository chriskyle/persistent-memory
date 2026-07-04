from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTOR_SCRIPT = ROOT / "skills" / "memory-vector" / "scripts" / "vector_memory.py"
SYNC_SCRIPT = ROOT / "skills" / "memory-sync" / "scripts" / "memory_sync.py"


def vector_cli(root: Path, namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VECTOR_SCRIPT), "--root", str(root), "--namespace", namespace, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def sync_cli(root: Path, namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--root", str(root), "--namespace", namespace, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_remember_recall_dedup_and_forget(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert vector_cli(root, "repo", "init", "--json").returncode == 0

    remembered = vector_cli(
        root,
        "repo",
        "remember",
        "Render deploys require /healthz to return 200 before traffic shifts.",
        "--tags",
        "deploy,render",
        "--importance",
        "4",
        "--json",
    )
    assert remembered.returncode == 0
    memory_id = json.loads(remembered.stdout)["id"]

    recall = vector_cli(root, "repo", "recall", "render deploys require healthz", "--min-score", "0.0", "--json")
    assert recall.returncode == 0
    results = json.loads(recall.stdout)
    assert results[0]["id"] == memory_id
    assert "healthz" in results[0]["text"]

    duplicate = vector_cli(
        root,
        "repo",
        "remember",
        "Render deploys require /healthz to return 200 before traffic shifts.",
        "--json",
    )
    assert duplicate.returncode == 0
    assert json.loads(duplicate.stdout)["deduped"] is True

    listed = vector_cli(root, "repo", "list", "--json")
    assert listed.returncode == 0
    assert json.loads(listed.stdout)[0]["occurrences"] == 2

    assert vector_cli(root, "repo", "forget", memory_id, "--reason", "obsolete").returncode == 0
    recall_after_forget = vector_cli(root, "repo", "recall", "render deploys require healthz", "--min-score", "0.0", "--json")
    assert recall_after_forget.returncode == 0
    assert json.loads(recall_after_forget.stdout) == []


def test_reindex_merges_multiple_device_shards(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert vector_cli(root, "repo", "init").returncode == 0
    first_device = (root / "repo" / "memory-vector" / ".local" / "device_id").read_text(encoding="utf-8").strip()
    assert vector_cli(root, "repo", "remember", "First device memory about billing retries.", "--tags", "billing").returncode == 0

    # Simulate a second device by changing only local, unsynced device state.
    local_dir = root / "repo" / "memory-vector" / ".local"
    (local_dir / "device_id").write_text("seconddevice\n", encoding="utf-8")
    (local_dir / "state.json").write_text('{"next_seq": 1}\n', encoding="utf-8")
    assert vector_cli(root, "repo", "remember", "Second device memory about deploy rollbacks.", "--tags", "deploy").returncode == 0

    assert first_device != "seconddevice"
    assert vector_cli(root, "repo", "reindex").returncode == 0
    stats = vector_cli(root, "repo", "stats")
    assert stats.returncode == 0
    payload = json.loads(stats.stdout)
    assert payload["live_memories"] == 2
    assert payload["shards"] == 2


def test_cross_skill_sync_then_recall_on_second_device(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    cloud.mkdir()

    assert sync_cli(root_a, "repo", "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0
    assert vector_cli(root_a, "repo", "init").returncode == 0
    assert vector_cli(
        root_a,
        "repo",
        "remember",
        "Agents should check vector memory before risky database migrations.",
        "--tags",
        "process,database",
    ).returncode == 0
    assert sync_cli(root_a, "repo", "sync", "--direction", "push").returncode == 0

    assert sync_cli(root_b, "repo", "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0
    assert vector_cli(root_b, "repo", "init").returncode == 0
    recall = vector_cli(root_b, "repo", "recall", "what should agents do before database migrations", "--json")
    assert recall.returncode == 0
    results = json.loads(recall.stdout)
    assert results
    assert "vector memory" in results[0]["text"]
