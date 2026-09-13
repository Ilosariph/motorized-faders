#!/usr/bin/env bash
# Upload pico/main.py to the Pico as main.py and reset it.
#
# Usage:
#   ./pico/upload.sh            # upload + reset
#   ./pico/upload.sh -r         # upload + reset, then drop into the REPL
#   PORT=/dev/ttyACM1 ./pico/upload.sh
#
# Nothing else may hold the serial port — stop host.py / host_raw.py /
# Thonny first, or mpremote fails to connect.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/main.py"
PORT="${PORT:-$(ls /dev/ttyACM* 2>/dev/null | head -1)}"

if [[ -z "$PORT" ]]; then
  echo "error: no /dev/ttyACM* found — is the Pico plugged in?" >&2
  exit 1
fi

if ! command -v mpremote >/dev/null; then
  echo "error: mpremote not found — enter the nix shell first." >&2
  exit 1
fi

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
