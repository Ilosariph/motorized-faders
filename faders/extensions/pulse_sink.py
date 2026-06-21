"""
PulseAudio / PipeWire sink extension.

Binds one fader to one sink:
  - fader move (human)         -> `pactl set-sink-volume <sink> NN%`
  - sink volume change (pavu)  -> `host.set_fader(idx, NN)` to move the motor

Config entry shape (faders/config.json):
    "pulse_sink": [
        {"fader": 0, "sink": "sink-music", "min": 0, "max": 100}
    ]

`min`/`max` are the sink-volume bounds (percent). Fader 0% maps to `min`,
fader 100% maps to `max`. Useful for taming a fader to e.g. 0-80% so you
can't accidentally pin a sink to clipping.

Pure subprocess — no Python deps. Works on PipeWire's pactl shim too.
"""

import re
import subprocess
import sys
import threading
import time

from ..core import Extension

PACTL = "pactl"

# Coalesce writes to pactl: ignore changes smaller than this many percent,
# and never write more often than this many seconds apart.
WRITE_EPSILON_PCT = 0.5
WRITE_MIN_INTERVAL_S = 0.03

# When we write to pactl, the subscribe loop will see our own change echoed
# back. Ignore subscribe events for this many seconds after each write.
SELF_ECHO_WINDOW_S = 0.25

# pactl subscribe event line. Example:
#   Event 'change' on sink #42
_EVENT_RE = re.compile(r"Event '(?P<ev>\w+)' on sink #(?P<idx>\d+)")
# `pactl get-sink-volume <sink>` returns lines containing e.g. "/  80% /".
_VOLUME_RE = re.compile(r"/\s*(\d+)%\s*/")


def register(host, cfg):
    ext = PulseSinkExtension(
        host=host,
        fader_idx=int(cfg["fader"]),
        sink=str(cfg["sink"]),
        vol_min=float(cfg.get("min", 0)),
        vol_max=float(cfg.get("max", 100)),
    )
    host.register(ext)


class PulseSinkExtension(Extension):
    def __init__(self, host, fader_idx, sink, vol_min, vol_max):
        self.host = host
        self.fader_idx = fader_idx
        self.sink = sink
        self.vol_min = vol_min
        self.vol_max = vol_max

        self._last_written_vol = None
        self._last_write_t = 0.0
        self._last_self_write_t = 0.0
        self._lock = threading.Lock()

        self._sink_index = self._resolve_sink_index()
        if self._sink_index is None:
            sys.stderr.write(
                f"[pulse_sink] sink '{sink}' not found at startup; "
                "will resolve lazily.\n"
            )

        # Seed fader to current sink volume so the motor starts in sync.
        current = self._read_sink_volume()
        if current is not None:
            fader_value = self._sink_to_fader(current)
            host.set_fader(fader_idx, fader_value)

        self._stop = threading.Event()
        self._sub_proc = None
        self._sub_thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        self._sub_thread.start()

    # ----- mapping ---------------------------------------------------------

    def _fader_to_sink(self, fader_pct):
        span = self.vol_max - self.vol_min
        return self.vol_min + (fader_pct / 100.0) * span

    def _sink_to_fader(self, sink_pct):
        span = self.vol_max - self.vol_min
        if span <= 0:
            return 0.0
        raw = (sink_pct - self.vol_min) / span * 100.0
        return max(0.0, min(100.0, raw))

    # ----- fader -> sink ---------------------------------------------------

    def on_position(self, fader_idx, value):
        if fader_idx != self.fader_idx:
            return
        target_vol = self._fader_to_sink(value)
        now = time.monotonic()
        with self._lock:
            if (
                self._last_written_vol is not None
                and abs(target_vol - self._last_written_vol) < WRITE_EPSILON_PCT
            ):
                return
            if now - self._last_write_t < WRITE_MIN_INTERVAL_S:
                return
            self._last_written_vol = target_vol
            self._last_write_t = now
            self._last_self_write_t = now
        # Fire-and-forget; we don't want to block the reader thread.
        try:
            subprocess.Popen(
                [PACTL, "set-sink-volume", self.sink, f"{target_vol:.1f}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            sys.stderr.write("[pulse_sink] `pactl` not found in PATH\n")

    # ----- sink -> fader (subscribe loop) ----------------------------------

    def _resolve_sink_index(self):
        try:
            out = subprocess.check_output(
                [PACTL, "list", "short", "sinks"], text=True, timeout=2.0
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == self.sink:
                try:
                    return int(parts[0])
                except ValueError:
                    return None
        return None

    def _read_sink_volume(self):
        try:
            out = subprocess.check_output(
                [PACTL, "get-sink-volume", self.sink], text=True, timeout=2.0
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        m = _VOLUME_RE.search(out)
        if m is None:
            return None
        return float(m.group(1))

    def _subscribe_loop(self):
        try:
            self._sub_proc = subprocess.Popen(
                [PACTL, "subscribe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            sys.stderr.write("[pulse_sink] `pactl` not found; subscribe disabled\n")
            return

        for line in self._sub_proc.stdout:
            if self._stop.is_set():
                break
            m = _EVENT_RE.search(line)
            if m is None:
                continue
            ev = m.group("ev")
            idx = int(m.group("idx"))
            if ev == "new" and self._sink_index is None:
                # A sink appeared — re-resolve in case it's ours.
                self._sink_index = self._resolve_sink_index()
                continue
            if ev != "change":
                continue
            if self._sink_index is None or idx != self._sink_index:
                continue
            # Ignore the echo of our own write.
            with self._lock:
                if time.monotonic() - self._last_self_write_t < SELF_ECHO_WINDOW_S:
                    continue
            sink_vol = self._read_sink_volume()
            if sink_vol is None:
                continue
            fader_value = self._sink_to_fader(sink_vol)
            self.host.set_fader(self.fader_idx, fader_value)

    # ----- shutdown --------------------------------------------------------

    def stop(self):
        self._stop.set()
        if self._sub_proc is not None:
            try:
                self._sub_proc.terminate()
            except Exception:
                pass
