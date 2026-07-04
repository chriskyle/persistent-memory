---
name: memory-vector
description: Persistent semantic vector memory for AI agents. Use when an agent should store durable memories, recall prior actions or context, look up relevant project/user facts, deduplicate repeated observations, forget obsolete memories, consolidate related memories, rebuild a local index after sync, or keep embeddings synced as JSONL records.
metadata:
  short-description: Store and recall semantic memory
---

# Memory Vector

Use this skill for durable, cross-run semantic memory: facts, decisions,
prior actions, recurring project context, stable user preferences, and
operational notes that should be available to future agents.

The bundled script is `scripts/vector_memory.py`, relative to this `SKILL.md`.
Run it with Python 3.

## Storage model

Memory data lives under:

```text
<AGENT_MEMORY_ROOT>/<namespace>/memory-vector/
  config.json
  shards/<device_id>.jsonl
  tombstones/<device_id>.jsonl
  index.sqlite3
  .local/device_id
  .local/state.json
```

Only the JSONL shards, tombstones, and config are durable cloud state.
`index.sqlite3` and `.local/*` are rebuildable/local-only and should not be
synced. `memory-sync` ignores them by default.

Each memory record stores its embedding directly in JSONL, so embeddings are
synced together with the text and metadata. The SQLite database is only a
local query cache rebuilt from synced files.

## Core workflow

1. Initialize once:

   ```bash
   python <skill-dir>/scripts/vector_memory.py init
   ```

2. Before starting a non-trivial task, recall relevant memories:

   ```bash
   python <skill-dir>/scripts/vector_memory.py recall "task description or question"
   ```

3. After learning something durable, store one atomic memory:

   ```bash
   python <skill-dir>/scripts/vector_memory.py remember \
     "Render deploys require the /healthz endpoint to return 200 before traffic is shifted." \
     --tags deploy,render \
     --importance 4 \
     --source "repo investigation"
   ```

4. After pulling synced memory shards from cloud storage, rebuild or rely on
   auto-reindex:

   ```bash
   python <skill-dir>/scripts/vector_memory.py reindex
   ```

## Commands

```bash
python <skill-dir>/scripts/vector_memory.py init
python <skill-dir>/scripts/vector_memory.py remember "<memory>" --tags tag1,tag2
python <skill-dir>/scripts/vector_memory.py recall "<query>" --top-k 8
python <skill-dir>/scripts/vector_memory.py forget <memory-id> --reason "<why>"
python <skill-dir>/scripts/vector_memory.py list
python <skill-dir>/scripts/vector_memory.py stats
python <skill-dir>/scripts/vector_memory.py consolidate
python <skill-dir>/scripts/vector_memory.py reindex
```

Use `AGENT_MEMORY_ROOT` or `--root` to override the base directory. Use
`AGENT_MEMORY_NAMESPACE` or `--namespace` to override the derived repo
namespace.

## Embedding providers

Default provider:

```bash
python <skill-dir>/scripts/vector_memory.py init --embedding-provider hash
```

The hash provider is deterministic, stdlib-only, and works offline. It is good
for reliable keyword/entity recall and lightweight semantic-ish similarity.

Optional OpenAI provider:

```bash
OPENAI_API_KEY=... \
python <skill-dir>/scripts/vector_memory.py init --embedding-provider openai
```

Set `AGENT_MEMORY_OPENAI_EMBEDDING_MODEL` to choose a model. Each record stores
`embedding_model` and `embedding_dim`, so mixed-provider stores remain
auditable and do not silently corrupt scoring.

## Reliability rules

- Keep memories atomic. Store one durable fact, decision, or action per
  `remember` call instead of dumping large transcripts.
- Recall at task start and before risky decisions.
- Remember only durable, externally useful information. Do not store hidden
  reasoning transcripts, secrets, credentials, or irrelevant chat history.
- Use tags for stable domains (`deploy`, `auth`, `billing`, `user-preference`,
  `incident`) so future recall can filter when needed.
- Use `forget` for obsolete or unsafe memories. This appends a tombstone
  instead of mutating old shards.
- Use `consolidate` to find near-duplicate clusters. The script prints
  clusters; the agent should write the actual summary with `remember
  --supersedes id1,id2,...` so summarization quality stays under model
  judgment.

## Retrieval behavior

`recall` uses hybrid scoring:

- vector cosine similarity
- lexical overlap for exact keywords/entities
- importance boost
- recency/occurrence boost

This is intentionally more reliable for agent memory than pure vector search,
which can miss exact filenames, identifiers, customer names, or command-line
flags.

## Sync integration

Use `memory-sync` to sync the parent namespace directory:

```bash
python <memory-sync-dir>/scripts/memory_sync.py sync --direction both
python <skill-dir>/scripts/vector_memory.py reindex
```

The append-only per-device shard design avoids ordinary write conflicts:
each device writes only `shards/<its-device-id>.jsonl` and
`tombstones/<its-device-id>.jsonl`; all agents rebuild a merged view locally.
