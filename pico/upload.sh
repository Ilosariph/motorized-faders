#!/usr/bin/env bash
# Upload pico/main.py and pico/lib/ to the Pico and reset it.
#
# Usage:
#   ./pico/upload.sh            # upload + reset
#   ./pico/upload.sh -r         # upload + reset, then drop into the REPL
#   PORT=/dev/ttyACM1 ./pico/upload.sh
#
# Nothing else may hold the serial port — stop host.py / host_raw.py /
# Thonny first, or mpremote fails to connect.

set -euo pipefail

PICO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$PICO_DIR/main.py"
LIB="$PICO_DIR/lib"
PORT="${PORT:-$(ls /dev/ttyACM* 2>/dev/null | head -1)}"

if [[ -z "$PORT" ]]; then
  echo "error: no /dev/ttyACM* found — is the Pico plugged in?" >&2
  exit 1
fi

if ! command -v mpremote >/dev/null; then
  echo "error: mpremote not found — enter the nix shell first." >&2
  exit 1
fi

# main.py imports from lib/, so the directory has to go up too — a stale or
# missing lib/ shows up as an ImportError at boot, not as a missing feature.
echo "uploading $LIB -> $PORT:/lib/"
mpremote connect "$PORT" fs mkdir :lib || true
for f in "$LIB"/*.py; do
  echo "  $(basename "$f")"
  mpremote connect "$PORT" fs cp "$f" ":lib/$(basename "$f")"
done

echo "uploading $SRC -> $PORT:main.py"
mpremote connect "$PORT" fs cp "$SRC" :main.py
mpremote connect "$PORT" reset
echo "done — firmware restarted"

if [[ "${1:-}" == "-r" ]]; then
  echo "attaching REPL (Ctrl-] to exit)"
  # Give the board a moment to come back up after reset before attaching.
  sleep 1
  mpremote connect "$PORT" repl
fi
