from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "memory-sync" / "scripts" / "memory_sync.py"


def load_memory_sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location("memory_sync_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


memory_sync = load_memory_sync()


def cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--cwd", str(ROOT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_classify_three_way_sync_decisions() -> None:
    assert memory_sync.classify("a", "a", None).action == memory_sync.Action.NOOP
    assert memory_sync.classify("a", None, None).action == memory_sync.Action.PUSH
    assert memory_sync.classify(None, "a", None).action == memory_sync.Action.PULL
    assert memory_sync.classify("a", "b", "a").action == memory_sync.Action.PULL
    assert memory_sync.classify("b", "a", "a").action == memory_sync.Action.PUSH
    assert memory_sync.classify("b", "c", "a").action == memory_sync.Action.CONFLICT
    assert memory_sync.classify(None, "a", "a").action == memory_sync.Action.DELETE_REMOTE
    assert memory_sync.classify("a", None, "a").action == memory_sync.Action.DELETE_LOCAL


def test_provider_specific_rclone_backend_config() -> None:
    args = Namespace(backend="dropbox", remote_name="mydropbox", provider_path="agent-memory")
    assert memory_sync._backend_config_from_args(args) == {
        "remote_name": "mydropbox",
        "path": "agent-memory",
    }

    args = Namespace(backend="gdrive", remote_name="mydrive", provider_path="")
    assert memory_sync._backend_config_from_args(args) == {"remote_name": "mydrive", "path": ""}


def test_localdir_sync_conflict_preserves_both_sides(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    cloud.mkdir()

    assert cli(tmp_path, "--root", str(root_a), "--namespace", "ns", "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0
    assert cli(tmp_path, "--root", str(root_b), "--namespace", "ns", "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0

    (root_a / "ns" / "note.txt").write_text("base\n", encoding="utf-8")
    assert cli(tmp_path, "--root", str(root_a), "--namespace", "ns", "sync").returncode == 0
    assert cli(tmp_path, "--root", str(root_b), "--namespace", "ns", "sync").returncode == 0

    (root_a / "ns" / "note.txt").write_text("device a\n", encoding="utf-8")
    (root_b / "ns" / "note.txt").write_text("device b\n", encoding="utf-8")
    assert cli(tmp_path, "--root", str(root_a), "--namespace", "ns", "sync").returncode == 0
    result = cli(tmp_path, "--root", str(root_b), "--namespace", "ns", "sync")

    assert result.returncode == 0
    assert "conflict" in result.stdout
    assert (root_b / "ns" / "note.txt").read_text(encoding="utf-8") == "device b\n"
    conflict_files = list((root_b / "ns").glob("note.conflict-*.txt"))
    assert len(conflict_files) == 1
    assert conflict_files[0].read_text(encoding="utf-8") == "device a\n"


def test_git_backend_syncs_through_local_bare_repo(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote), "-b", "main"], check=True, capture_output=True, text=True)
    root_a = tmp_path / "git-a"
    root_b = tmp_path / "git-b"

    assert cli(tmp_path, "--root", str(root_a), "--namespace", "repo", "init", "--backend", "git", "--remote", str(remote), "--branch", "main").returncode == 0
    (root_a / "repo" / "data.txt").write_text("git backend\n", encoding="utf-8")
    assert cli(tmp_path, "--root", str(root_a), "--namespace", "repo", "sync").returncode == 0

    assert cli(tmp_path, "--root", str(root_b), "--namespace", "repo", "init", "--backend", "git", "--remote", str(remote), "--branch", "main").returncode == 0
    assert (root_b / "repo" / "data.txt").read_text(encoding="utf-8") == "git backend\n"


def test_json_status_reports_pending_changes(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud"
    root = tmp_path / "root"
    cloud.mkdir()
    assert cli(tmp_path, "--root", str(root), "--namespace", "ns", "init", "--backend", "localdir", "--path", str(cloud)).returncode == 0
    (root / "ns" / "new.txt").write_text("new\n", encoding="utf-8")
    result = cli(tmp_path, "--root", str(root), "--namespace", "ns", "status", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["pending_push"] == ["new.txt"]
