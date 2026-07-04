---
name: memory-checkpoint
description: Start/end persistent memory checkpoint for AI agent tasks. Use at the beginning of non-trivial work to pull cloud memory and recall relevant vector memories, and at the end to store durable findings and push them back to cloud storage. Coordinates memory-sync and memory-vector so agents reliably use memory instead of forgetting to call lower-level skills.
metadata:
  short-description: Run memory start/end checkpoints
---

# Memory Checkpoint

Use this skill as the default habit layer for persistent memory. It combines:

- `memory-sync` for cloud pull/push
- `memory-vector` for semantic recall/remember

The bundled script is `scripts/checkpoint.py`, relative to this `SKILL.md`.
Run it with Python 3. This skill assumes `memory-sync` and `memory-vector`
are installed as sibling directories in the same `skills/` bundle.

## Start of task

Run this before substantial implementation, debugging, planning, or research:

```bash
python <skill-dir>/scripts/checkpoint.py start "task description"
```

It performs:

1. `memory-sync sync --direction pull`
2. `memory-vector recall "task description"`

Read the recalled memories before proceeding. If a relevant memory looks stale
or wrong, use `memory-vector forget` or store a corrected memory at the end.

## End of task

Run this after completing work or learning durable facts:

```bash
python <skill-dir>/scripts/checkpoint.py end \
  --memory "The repo uses ruff with E501 ignored in pyproject.toml." \
  --tags repo,tooling \
  --importance 3
```

It performs:

1. `memory-vector remember` for each `--memory`
2. `memory-sync sync --direction push`

Repeat `--memory` for separate atomic memories. Do not store secrets, hidden
reasoning, credentials, or large transcripts.

## Options

```bash
python <skill-dir>/scripts/checkpoint.py start "<task>" --top-k 5 --min-score 0.15
python <skill-dir>/scripts/checkpoint.py end --memory "<fact>" --tags deploy,render --importance 4
python <skill-dir>/scripts/checkpoint.py --namespace my-project start "<task>"
python <skill-dir>/scripts/checkpoint.py --root /tmp/agent-memory start "<task>"
```

Use `--allow-missing-sync` or `--allow-missing-vector` only during initial
setup or when intentionally running one subsystem without the other. Normal
usage should fail loudly if memory is not initialized.

## When to use

Use a start checkpoint when:

- the task is non-trivial or multi-step
- prior project history, user preferences, incidents, or decisions might help
- you are resuming after context compaction or a new agent run
- you are about to make a risky change and want prior context

Use an end checkpoint when:

- you learned a durable fact future agents should know
- you completed a workflow whose outcome may matter later
- you corrected an assumption or found an important constraint
- you updated or invalidated earlier memory
