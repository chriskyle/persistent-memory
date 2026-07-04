# AGENTS.md

## Cursor Cloud specific instructions

### Repository layout
`persistent-memory` is a bundle of Python-based **Agent Skills** (CLI scripts), not
a web app or importable package. The full project (`pyproject.toml`, `skills/`,
`tests/`) currently lives on the feature branch behind PR #1
(`cursor/agent-memory-skills-bundle-65e6`); the `main` branch only contains a
placeholder `README.md` until that PR merges. See the project `README.md` on the
feature branch for skill descriptions and usage examples.

### Toolchain / setup
- Dev tools (`pytest`, `ruff`) are installed on startup into the **system
  `python3` user site** (`~/.local/lib/python3.*/site-packages`), so they are
  importable by `/usr/bin/python3` — no virtualenv activation and no per-agent
  `pip install` needed. They persist in the VM snapshot.
- The startup update script installs them from the repo's
  `[dependency-groups].dev` group in `pyproject.toml` (resolved with
  `uv pip compile`, `uv` is preinstalled at `~/.local/bin/uv`), falling back to a
  direct `pip install --user pytest "ruff>=0.14.10"` when `pyproject.toml` is
  absent (e.g. the placeholder `main`).
- Use `python3 -m ...` to invoke the tools. The `pytest`/`ruff` console scripts
  land in `~/.local/bin`, which may not be on `PATH`; `python3 -m` avoids that.

### Lint / test / run
- Lint: `python3 -m ruff check .`
- Tests: `python3 -m pytest`
- Run a skill (this repo is **not** an installable package — the skill scripts are
  stdlib-only, so call them directly):
  `python3 skills/<skill>/scripts/<script>.py <command> ...`
  e.g. `python3 skills/memory-vector/scripts/vector_memory.py recall "..."`

### Non-obvious gotchas
- Skills read/write durable data under `~/.agent-memory` by default. Set
  `AGENT_MEMORY_ROOT` to an isolated temp dir for tests/manual runs so you don't
  pollute the real store (`.agent-memory/` is gitignored).
- `memory-vector recall` defaults to `--min-score 0.2`. The default embedding is a
  deterministic stdlib feature-hashing model, so lexically-different paraphrases can
  score below the threshold and return "No relevant memories found." Use shared
  terms or lower `--min-score` when validating recall. A real OpenAI embedding
  provider is used only when `OPENAI_API_KEY` is set.
- Only the `localdir` and `git` sync backends work out of the box. The
  `dropbox`/`gdrive`/generic `rclone` backends require the `rclone` binary, and the
  `s3` backend requires the AWS CLI — none of these are installed by default.
