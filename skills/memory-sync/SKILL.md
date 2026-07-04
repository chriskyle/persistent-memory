---
name: memory-sync
description: Durable cloud file synchronization for AI agent memory and working files. Use when an agent needs to initialize persistent storage, sync local memory/files with cloud storage, pull prior state at task start, push durable artifacts at task end, inspect sync status, resolve conflicts, deduplicate files, or use localdir, git, s3, Dropbox, Google Drive, or generic rclone as a backend.
metadata:
  short-description: Sync persistent agent files
---

# Memory Sync

Use this skill when agent files or memory need to survive across runs,
machines, branches, or context resets. It tracks a local memory directory
against a cloud backend with content-hash change detection, conflict
preservation, and local deduplication.

The bundled script is `scripts/memory_sync.py`, relative to this `SKILL.md`.
Run it with Python 3.

## Core workflow

1. Initialize a namespace once:

   ```bash
   python <skill-dir>/scripts/memory_sync.py init --backend localdir --path /path/to/cloud-copy
   ```

2. At the start of a task, pull remote changes:

   ```bash
   python <skill-dir>/scripts/memory_sync.py sync --direction pull
   ```

3. During work, write durable memory/files under the tracked root reported by
   `init` or `status`.

4. At the end of a task, push changes:

   ```bash
   python <skill-dir>/scripts/memory_sync.py sync --direction push
   ```

5. If unsure whether local and cloud differ, use:

   ```bash
   python <skill-dir>/scripts/memory_sync.py status
   python <skill-dir>/scripts/memory_sync.py sync --dry-run
   ```

## Commands

```bash
python <skill-dir>/scripts/memory_sync.py init --backend localdir --path /path/to/cloud-copy
python <skill-dir>/scripts/memory_sync.py sync --direction both
python <skill-dir>/scripts/memory_sync.py status
python <skill-dir>/scripts/memory_sync.py resolve <path> --keep local
python <skill-dir>/scripts/memory_sync.py gc
```

Use `AGENT_MEMORY_ROOT` or `--root` to override the base directory. Use
`AGENT_MEMORY_NAMESPACE` or `--namespace` to override the derived namespace.
By default, the namespace is derived from the current git remote/repo identity
and is stable across branches.

## Backends

### Local directory

Use for tests, mounted cloud drives, shared volumes, or simple setups:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend localdir \
  --path /mnt/cloud/agent-memory
```

### Dropbox

Dropbox uses rclone for OAuth and file operations. Configure a Dropbox remote
outside the skill with `rclone config`, then initialize:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend dropbox \
  --remote-name mydropbox \
  --provider-path agent-memory
```

This keeps Dropbox OAuth tokens in rclone's normal config instead of inside
the memory-sync state directory or a git history.

### Google Drive

Google Drive also uses rclone for OAuth and file operations:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend gdrive \
  --remote-name mydrive \
  --provider-path agent-memory
```

### Generic rclone

Use when the provider is supported by rclone but not exposed as a first-class
backend:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend rclone \
  --remote-path myremote:agent-memory
```

### S3-compatible storage

S3 uses the `aws` CLI and ambient AWS credentials:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend s3 \
  --bucket my-agent-memory-bucket \
  --prefix agent-memory \
  --region us-east-1
```

### Git

Git uses a remote branch as a versioned object store:

```bash
python <skill-dir>/scripts/memory_sync.py init \
  --backend git \
  --remote https://github.com/example/agent-memory-store.git \
  --branch main
```

Use Git only when the remote is private and the contents are appropriate for
git history. Git is convenient and auditable, but it is easy to leak durable
memory through clones, forks, logs, or undeleted history. Prefer Dropbox,
Google Drive, S3, or another rclone-backed encrypted/private store for
sensitive or user-specific memory.

## Conflict behavior

Sync uses a three-way content-hash comparison:

- unchanged local + changed remote: pull
- changed local + unchanged remote: push
- same content on both sides: no-op
- changed differently on both sides: conflict

On conflict, memory-sync never silently discards a side. It keeps one version
as canonical (default: newest modification time) and writes the other as:

```text
<name>.conflict-<device>-<timestamp><suffix>
```

Use `status` to inspect conflicts and `resolve` to mark the intended winner.

## Deduplication

Run:

```bash
python <skill-dir>/scripts/memory_sync.py gc
```

This hardlinks identical local files by sha256 and prunes old conflict copies.
For the git backend, it also runs `git gc` in the local mirror.

## What to sync

This skill is intentionally generic. It should sync the durable files produced
by other memory skills, including `memory-vector` JSONL shards and embedding
records. It ignores derived local caches such as `*.sqlite3` by default because
they are rebuildable and should not be conflict-prone cloud state.
