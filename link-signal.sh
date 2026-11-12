#!/usr/bin/env bash
# Link this machine as a Signal device of your account.
#
# The QR printed by `signal-cli link` straight into a terminal is drawn with
# half-block characters and is often unusable (line wrap / font). This writes a
# real PNG instead, which you can open or scp to your laptop:
#   scp root@<vm>:/tmp/signal-link.png .
set -euo pipefail

OUT="/tmp/signal-link.png"
LOG="/tmp/signal-link.txt"
: >"$LOG"

if ! command -v signal-cli >/dev/null 2>&1; then
    echo "signal-cli is not installed (expected /usr/local/bin/signal-cli)" >&2
    exit 1
fi

echo "starting signal-cli link ... (Ctrl-C to abort)"
signal-cli link -n "trackFlyPrice" >"$LOG" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' INT TERM

for _ in $(seq 1 60); do
    grep -q "sgnl://" "$LOG" 2>/dev/null && break
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "signal-cli exited early:" >&2
        cat "$LOG" >&2
        exit 1
    fi
    sleep 1
done

uri=$(grep -o 'sgnl://[^ ]*' "$LOG" | head -1 || true)
if [ -z "$uri" ]; then
    echo "no link URI found:" >&2
    cat "$LOG" >&2
    exit 1
fi

if command -v qrencode >/dev/null 2>&1; then
    printf '%s' "$uri" | qrencode -o "$OUT" -s 10 -m 3
    echo
    echo "QR image : $OUT"
    echo "Open it or copy it:  scp root@<vm>:$OUT ."
else
    echo
    echo "qrencode is not installed. Raw link:"
    echo "$uri"
    echo "(install with:  apt-get install -y qrencode)"
fi

echo
echo "Phone: Signal > Settings > Linked devices > Link new device -> scan"
echo "Waiting for the device to be linked ..."

wait "$pid"
echo
echo "Done. Check the number with:  signal-cli listAccounts"
echo "Then put it in .env as SIGNAL_ACCOUNT and run:  ./restart.sh"
