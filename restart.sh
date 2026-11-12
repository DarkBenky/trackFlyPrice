#!/usr/bin/env bash
# Restart the watcher in a detached tmux session (run after editing .env).
set -euo pipefail
cd "$(dirname "$0")"

tmux kill-session -t flights 2>/dev/null || true
tmux new-session -d -s flights -c "$PWD" "./run.sh; exec bash"
sleep 2
echo "started tmux session 'flights'"
echo "  attach:  tmux attach -t flights"
echo "  stop:    tmux kill-session -t flights"
echo "  log:     tail -f $PWD/data/trackfly.log"
