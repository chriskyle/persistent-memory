# Persistent Memory Agent Skills

This repository contains a bundle of Agent Skills that give AI agents durable,
cloud-synced memory across runs, machines, branches, and context resets.

It complements
[`chriskyle/note-taker`](https://github.com/chriskyle/note-taker):

- `note-taker` is temporary working memory for a single session/thread.
- `persistent-memory` is durable memory that can be synced to cloud storage and
  recalled by future agents.

## Skills

```text
skills/
  memory-sync/        Cloud-backed file sync for persistent agent files.
  memory-vector/      Semantic memory store with synced embedding records.
  memory-checkpoint/  Start/end workflow that combines sync + vector memory.
```

### memory-sync

`memory-sync` tracks a namespace directory under `~/.agent-memory` (or
`AGENT_MEMORY_ROOT`) against a cloud backend. It supports:

- `localdir` for tests, mounted drives, and simple shared folders
- `dropbox` via rclone OAuth
- `gdrive` via rclone OAuth
- generic `rclone` for other providers
- `s3` via the AWS CLI
- `git` for private, versioned memory stores

Git is convenient but should be used carefully: durable memory in git may live
forever in clone/fork/history surfaces. Prefer Dropbox, Google Drive, S3, or
another private rclone-backed store for sensitive user- or project-specific
memory.

Example:

```bash
python skills/memory-sync/scripts/memory_sync.py init \
  --backend dropbox \
  --remote-name mydropbox \
  --provider-path agent-memory

python skills/memory-sync/scripts/memory_sync.py sync --direction both
```

### memory-vector

`memory-vector` stores durable memories as append-only JSONL shards:

```text
memory-vector/
  config.json
  shards/<device_id>.jsonl
  tombstones/<device_id>.jsonl
  index.sqlite3       # local rebuildable cache, not synced
  .local/             # local device state, not synced
```

Each memory record includes its embedding, so syncing the JSONL files syncs
the text, metadata, and vectors together. The default embedding provider is a
deterministic stdlib feature-hashing model; an optional OpenAI provider is
available when `OPENAI_API_KEY` is set.

Example:

```bash
python skills/memory-vector/scripts/vector_memory.py init
python skills/memory-vector/scripts/vector_memory.py remember \
  "Render deploys require /healthz to return 200 before traffic shifts." \
  --tags deploy,render \
  --importance 4
python skills/memory-vector/scripts/vector_memory.py recall "how do Render deploy health checks work?"
```

### memory-checkpoint

`memory-checkpoint` makes memory usage habitual:

- `start`: pull cloud memory, then recall relevant semantic memories
- `end`: remember durable findings, then push cloud memory

Example:

```bash
python skills/memory-checkpoint/scripts/checkpoint.py start "implement deploy health checks"

python skills/memory-checkpoint/scripts/checkpoint.py end \
  --memory "Render deploys require /healthz to return 200 before traffic shifts." \
  --tags deploy,render \
  --importance 4
```

## Development

This repo is not a Python package. `pyproject.toml` exists for development
tooling only.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest ruff
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```