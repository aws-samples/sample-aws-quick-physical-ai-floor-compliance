#!/usr/bin/env bash
# Shared by the wrapper scripts: create a private virtual environment with a
# current boto3 and the MCP client, then run scripts/floor.py with it.
# The MCP client needs Python 3.10 or newer. AWS CloudShell's default python3
# can be older, so this picks the newest interpreter present and, when none is
# new enough and dnf is available (CloudShell), installs python3.12.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then command -v "$c"; return 0; fi
  done
  return 1
}

# A venv from an earlier CloudShell session can point at an interpreter that is gone.
if [ -d "$VENV" ] && ! py_ok "$VENV/bin/python"; then
  echo "Existing virtual environment is unusable; recreating it ..."
  rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
  if ! PY="$(find_python)"; then
    if command -v dnf >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
      echo "Python 3.10 or newer not found; installing python3.12 (about a minute) ..."
      sudo dnf install -y -q python3.12 >/dev/null
      PY="$(find_python)" || { echo "Could not install Python 3.12. Install Python 3.10+ and re-run." >&2; exit 1; }
    else
      echo "Python 3.10 or newer is required. Install it and re-run." >&2
      exit 1
    fi
  fi
  echo "Creating virtual environment in $VENV with $("$PY" --version) ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
fi
if [ ! -f "$VENV/.deps-ok" ] || [ "$REPO_DIR/scripts/requirements.txt" -nt "$VENV/.deps-ok" ]; then
  "$VENV/bin/python" -m pip install --quiet -r "$REPO_DIR/scripts/requirements.txt"
  touch "$VENV/.deps-ok"
fi
run_floor() {
  cd "$REPO_DIR"
  exec "$VENV/bin/python" scripts/floor.py "$@"
}
