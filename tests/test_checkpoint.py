from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_SCRIPT = ROOT / "skills" / "memory-checkpoint" / "scripts" / "checkpoint.py"
SYNC_SCRIPT = ROOT / "skills" / "memory-sync" / "scripts" / "memory_sync.py"
VECTOR_SCRIPT = ROOT / "skills" / "memory-vector" / "scripts" / "vector_memory.py"


def run_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_checkpoint_end_remembers_and_start_recalls(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud"
    root = tmp_path / "root"
    cloud.mkdir()
    common = ["--root", str(root), "--namespace", "repo"]

    assert run_script(SYNC_SCRIPT, *common, "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0
    assert run_script(VECTOR_SCRIPT, *common, "init").returncode == 0

    end = run_script(
        CHECKPOINT_SCRIPT,
        *common,
        "end",
        "--memory",
        "Checkpoint skill stores durable summary memories before syncing.",
        "--tags",
        "checkpoint,process",
        "--importance",
        "4",
        "--json",
    )
    assert end.returncode == 0, end.stderr
    payload = json.loads(end.stdout)
    assert payload["phase"] == "end"
    assert payload["sync_returncode"] == 0
    assert payload["remembered"][0]["returncode"] == 0

    start = run_script(
        CHECKPOINT_SCRIPT,
        *common,
        "start",
        "how are checkpoint summary memories synced",
        "--json",
    )
    assert start.returncode == 0, start.stderr
    payload = json.loads(start.stdout)
    assert payload["phase"] == "start"
    assert "Checkpoint skill stores durable summary memories" in payload["recall_output"]


def test_checkpoint_allows_missing_subsystems_when_requested(tmp_path: Path) -> None:
    result = run_script(
        CHECKPOINT_SCRIPT,
        "--root",
        str(tmp_path / "root"),
        "--namespace",
        "missing",
        "--allow-missing-sync",
        "--allow-missing-vector",
        "start",
        "task",
        "--json",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["sync_returncode"] != 0
    assert payload["recall_returncode"] != 0
