#!/bin/sh
# nncq bundled demo (Linux / macOS): indexes demo/orbitkit, starts the server on this folder, opens the dashboard.
# First run creates ../.venv (lite: no torch, no GPU). Ctrl+C stops the server.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/../.venv/bin/python"
if [ ! -x "$py" ]; then
  echo "== first run: installing nncq into $here/../.venv [lite]"
  python3 -m venv "$here/../.venv"
  "$py" -m pip install --quiet --upgrade pip
  "$py" -m pip install --quiet -e "$here/.."
fi
exec "$py" "$here/run.py" "$@"
