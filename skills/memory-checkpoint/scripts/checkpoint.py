#!/usr/bin/env python3
"""Start/end checkpoint orchestration for the persistent memory skill bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


class CheckpointError(RuntimeError):
    """Raised when a checkpoint step cannot be completed."""


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path:
    return skill_root().parent


def memory_sync_script() -> Path:
    return bundle_root() / "memory-sync" / "scripts" / "memory_sync.py"


def memory_vector_script() -> Path:
    return bundle_root() / "memory-vector" / "scripts" / "vector_memory.py"


def common_args(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.root:
        out += ["--root", str(args.root)]
    if args.namespace:
        out += ["--namespace", args.namespace]
    if args.cwd:
        out += ["--cwd", str(args.cwd)]
    return out


def run_step(label: str, cmd: list[str], *, optional: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and not optional:
        raise CheckpointError(
            f"{label} failed with exit code {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def cmd_start(args: argparse.Namespace) -> int:
    common = common_args(args)
    sync = run_step(
        "memory-sync pull",
        [sys.executable, str(memory_sync_script()), *common, "sync", "--direction", "pull"],
        optional=args.allow_missing_sync,
    )
    recall = run_step(
        "memory-vector recall",
        [
            sys.executable,
            str(memory_vector_script()),
            *common,
            "recall",
            args.task,
            "--top-k",
            str(args.top_k),
            "--min-score",
            str(args.min_score),
        ],
        optional=args.allow_missing_vector,
    )
    payload = {
        "phase": "start",
        "sync_returncode": sync.returncode,
        "recall_returncode": recall.returncode,
        "sync_output": sync.stdout.strip(),
        "recall_output": recall.stdout.strip(),
        "sync_error": sync.stderr.strip(),
        "recall_error": recall.stderr.strip(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("# Memory checkpoint: start")
        if sync.stdout.strip():
            print("\n## Sync\n" + sync.stdout.strip())
        if sync.stderr.strip():
            print("\n## Sync warnings\n" + sync.stderr.strip())
        if recall.stdout.strip():
            print("\n## Relevant memories\n" + recall.stdout.strip())
        if recall.stderr.strip():
            print("\n## Recall warnings\n" + recall.stderr.strip())
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    common = common_args(args)
    remembered: list[dict[str, str | int]] = []
    for text in args.memory:
        result = run_step(
            "memory-vector remember",
            [
                sys.executable,
                str(memory_vector_script()),
                *common,
                "remember",
                text,
                "--importance",
                str(args.importance),
                "--source",
                args.source,
                "--json",
            ]
            + (["--tags", args.tags] if args.tags else []),
            optional=args.allow_missing_vector,
        )
        remembered.append(
            {
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )

    sync = run_step(
        "memory-sync push",
        [sys.executable, str(memory_sync_script()), *common, "sync", "--direction", "push"],
        optional=args.allow_missing_sync,
    )
    payload = {
        "phase": "end",
        "remembered": remembered,
        "sync_returncode": sync.returncode,
        "sync_output": sync.stdout.strip(),
        "sync_error": sync.stderr.strip(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("# Memory checkpoint: end")
        for item in remembered:
            if item["stdout"]:
                print("\n## Remembered\n" + str(item["stdout"]))
            if item["stderr"]:
                print("\n## Remember warnings\n" + str(item["stderr"]))
        if sync.stdout.strip():
            print("\n## Sync\n" + sync.stdout.strip())
        if sync.stderr.strip():
            print("\n## Sync warnings\n" + sync.stderr.strip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run start/end persistent memory checkpoints for agent tasks.")
    parser.add_argument("--root", type=Path, default=None, help="Override AGENT_MEMORY_ROOT.")
    parser.add_argument("--namespace", default=None, help="Override AGENT_MEMORY_NAMESPACE / derived namespace.")
    parser.add_argument("--cwd", type=Path, default=None, help="Resolve repo identity from this directory.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-missing-sync", action="store_true", help="Do not fail if memory-sync is uninitialized/unavailable.")
    parser.add_argument("--allow-missing-vector", action="store_true", help="Do not fail if memory-vector is uninitialized/unavailable.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Pull cloud memory and recall relevant prior context.")
    start_parser.add_argument("task", help="Current task description or question.")
    start_parser.add_argument("--top-k", type=int, default=8)
    start_parser.add_argument("--min-score", type=float, default=0.2)
    start_parser.set_defaults(func=cmd_start)

    end_parser = subparsers.add_parser("end", help="Remember durable findings and push them to cloud storage.")
    end_parser.add_argument("--memory", action="append", default=[], help="Durable memory to store. Repeat for multiple memories.")
    end_parser.add_argument("--tags", help="Comma-separated tags for stored memories.")
    end_parser.add_argument("--importance", type=int, default=3)
    end_parser.add_argument("--source", default="memory-checkpoint")
    end_parser.set_defaults(func=cmd_end)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CheckpointError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
