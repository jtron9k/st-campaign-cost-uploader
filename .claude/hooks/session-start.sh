#!/bin/bash
# Installs the project's Python toolchain so tests and linters are runnable
# the moment a Claude Code on the web session starts.
#
# Local sessions are left alone: on a developer machine the venv is already
# there, and re-syncing on every /clear or /compact is noise.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# uv ships in the remote image at ~/.local/bin, which is not always on PATH
# for a non-login shell.
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "session-start: uv not found on PATH; cannot install dependencies" >&2
  exit 1
fi

# The project needs >=3.12 and the base image ships 3.11, so uv fetches its
# own interpreter. Idempotent: a no-op once .venv matches uv.lock.
uv python install 3.12
uv sync --extra dev

# The audit log is written here on every ServiceTitan write and is gitignored,
# so it does not exist in a fresh clone.
mkdir -p logs

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$CLAUDE_ENV_FILE"
fi

echo "session-start: uv sync complete. Run tests with 'uv run pytest', lint with 'uv run ruff check'."
