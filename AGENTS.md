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
- Package manager is **uv** (declared via `[tool.uv]` + `[dependency-groups]` in
  `pyproject.toml`). It is preinstalled in the VM snapshot at `~/.local/bin/uv`.
- The startup update script runs `uv sync` (guarded on `pyproject.toml` existing),
  which creates `.venv` with the dev tooling (`pytest`, `ruff`). No other install
  steps are needed.
- If `uv` is not on `PATH` in a shell, invoke it as `~/.local/bin/uv` or run
  `source ~/.local/bin/env`.

### Lint / test / run
- Lint: `uv run ruff check .`
- Tests: `uv run pytest`
- Run a skill (this repo is **not** an installable package — call scripts directly):
  `uv run python skills/<skill>/scripts/<script>.py <command> ...`
  e.g. `uv run python skills/memory-vector/scripts/vector_memory.py recall "..."`

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
