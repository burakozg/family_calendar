#!/bin/sh
# Run the backend test suite.
#
#   ./run-tests.sh            the whole suite
#   ./run-tests.sh -k mail    anything pytest takes
#
# Prefers the repo's own .venv, because the previous version called a bare
# `python3 -m pytest` and so failed with "No module named pytest" unless the
# venv happened to be activated first — the documented command not working from
# a clean shell is how a test suite quietly stops being run.
#
# Falls back to whatever python3 is on PATH, which is right when the deps are
# installed globally or an env is already active. Set PYTHON to override.
#
# First-time setup:
#   cd backend && uv sync
# (creates backend/.venv; PYTHON=backend/.venv/bin/python or just cd backend
# and run `uv run pytest` directly)
set -eu

cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x backend/.venv/bin/python ]; then
  PY=backend/.venv/bin/python
else
  PY=python3
fi

if ! "$PY" -c 'import pytest' 2>/dev/null; then
  echo "pytest is not available to $PY." >&2
  echo "Create the venv and install the dev requirements — see the header." >&2
  exit 1
fi

# From the repo root, not backend/: pytest.ini lives here and sets both the
# testpath and the timeout that turns a hang into a named failure.
exec "$PY" -m pytest -q "$@"
