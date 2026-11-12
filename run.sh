#!/usr/bin/env bash
# Create the venv on first run, then start the watcher + dashboard.
#   ./run.sh                 # normal loop (run inside tmux)
#   ./run.sh --once          # one polling cycle
#   ./run.sh --test-notify   # send a test Signal message
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"
STAMP="$VENV/.requirements.stamp"

if [ ! -x "$VENV/bin/python" ]; then
    echo "creating virtualenv in $VENV ..."
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip >/dev/null
fi

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
    echo "installing dependencies ..."
    "$VENV/bin/pip" install -r requirements.txt
    touch "$STAMP"
fi

exec "$VENV/bin/python" -m trackfly.main "$@"
