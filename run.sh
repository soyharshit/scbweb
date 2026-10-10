#!/usr/bin/env bash
# Start the web client. Creates a local venv on first run.
set -e
cd "$(dirname "$0")"

# --- create the virtualenv (python venv, falling back to uv) ---------------
if [ ! -x .venv/bin/python ] || [ ! -f .venv/pyvenv.cfg ]; then
  rm -rf .venv
  if python3 -m venv .venv 2>/dev/null; then
    echo "Created virtualenv with python3 -m venv"
  elif command -v uv >/dev/null 2>&1; then
    uv venv .venv
    echo "Created virtualenv with uv"
  else
    echo "Could not create a virtualenv. Install python3-venv or uv." >&2
    exit 1
  fi
fi

# --- install dependencies --------------------------------------------------
# python -m pip rather than the pip shim: the shim carries a hardcoded
# shebang, so it breaks if the virtualenv is ever moved.
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
elif command -v uv >/dev/null 2>&1; then
  # venv created by `uv venv` (ships without pip)
  uv pip install -q -p .venv/bin/python -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app.main:app \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-8000}" \
  --workers 1 \
  --log-level info
