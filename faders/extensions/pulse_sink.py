"""
PulseAudio / PipeWire sink extension.

Binds one fader to one sink:
  - fader move (human)         -> `pactl set-sink-volume <sink> NN%`
  - sink volume change (pavu)  -> `host.set_fader(idx, NN)` to move the motor
  - fader button (mute)        -> `pactl set-sink-mute <sink> 0|1`
  - sink mute change (pavu)    -> `host.set_mute(idx, muted)`

It also owns what the fader's display shows: the sink name on line 1 and the
live volume on line 2. That is the point of pushing display text from the host
— the screen describes whatever the active plugin controls, so a different
extension on the same fader labels it differently with no firmware change.

Config entry shape (faders/config.json):
    "pulse_sink": [
        {"fader": 0, "sink": "sink-music", "min": 0, "max": 100,
         "label": "Music"}
    ]

`min`/`max` are the sink-volume bounds (percent). Fader 0% maps to `min`,
fader 100% maps to `max`. Useful for taming a fader to e.g. 0-80% so you
can't accidentally pin a sink to clipping.

`label` is optional display text; it defaults to the sink name. Sink names are
often long (`alsa_output.pci-0000_00_1f.3.analog-stereo`), which does not fit a
128 px screen, so a short label is usually worth setting.

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
# `pactl get-sink-mute <sink>` returns "Mute: yes" or "Mute: no".
_MUTE_RE = re.compile(r"Mute:\s*(yes|no)")


def register(host, cfg):
    ext = PulseSinkExtension(
        host=host,
        fader_idx=int(cfg["fader"]),
        sink=str(cfg["sink"]),
        vol_min=float(cfg.get("min", 0)),
        vol_max=float(cfg.get("max", 100)),
        label=cfg.get("label"),
    )
    host.register(ext)


class PulseSinkExtension(Extension):
    def __init__(self, host, fader_idx, sink, vol_min, vol_max, label=None):
        self.host = host
        self.fader_idx = fader_idx
        self.sink = sink
        self.vol_min = vol_min
        self.vol_max = vol_max
        # Shown on line 1 of this fader's display. Defaults to the sink name,
        # which is often too long for the screen — hence the config override.
        self.label = label or sink

        self._last_written_vol = None
        self._last_write_t = 0.0
        self._last_self_write_t = 0.0
        self._last_mute = None
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
            self._push_display(current)
        else:
            self._push_display(None)

        # Seed mute state the same way, so a sink muted before startup shows
        # as muted rather than being discovered on the first button press.
        muted = self._read_sink_mute()
        if muted is not None:
            self._last_mute = muted
            host.set_mute(fader_idx, muted)

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
        with self._lock:
            muted = self._last_mute
        if muted:
            # A muted fader sits at the bottom, and the user may drag it
            # around while muted to pick where it returns to. Neither is a
            # volume change — writing them through would zero the sink and
            # lose the volume that unmuting is supposed to restore.
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

        self._push_display(target_vol)

    # ----- display ---------------------------------------------------------

    def _push_display(self, sink_vol):
        """
        Label this fader's screen. Line 1 identifies the sink, line 2 shows
        its volume. The Pico redraws only when the text actually changes, and
        host.set_display() drops duplicate sends, so this is cheap to call on
        every update.
        """
        if sink_vol is None:
            line2 = "--"
        else:
            line2 = "vol {:.0f}%".format(sink_vol)
        self.host.set_display(self.fader_idx, self.label, line2)

    # ----- mute ------------------------------------------------------------

    def on_mute(self, fader_idx, muted):
        """A fader button was pressed (or the Pico confirmed a mute) — apply
        it to the sink."""
        if fader_idx != self.fader_idx:
            return
        with self._lock:
            if self._last_mute == muted:
                return  # already in this state; nothing to write
            self._last_mute = muted
            self._last_self_write_t = time.monotonic()
        try:
            subprocess.Popen(
                [PACTL, "set-sink-mute", self.sink, "1" if muted else "0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            sys.stderr.write("[pulse_sink] `pactl` not found in PATH\n")

    def _read_sink_mute(self):
        try:
            out = subprocess.check_output(
                [PACTL, "get-sink-mute", self.sink], text=True, timeout=2.0
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        m = _MUTE_RE.search(out)
        if m is None:
            return None
        return m.group(1) == "yes"

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

            # Mute changed elsewhere (pavucontrol, a media key): move the
            # fader to match, so the hardware never disagrees with the mixer.
            muted = self._read_sink_mute()
            if muted is not None:
                with self._lock:
                    changed = muted != self._last_mute
                    if changed:
                        self._last_mute = muted
                if changed:
                    self.host.set_mute(self.fader_idx, muted)

            sink_vol = self._read_sink_volume()
            if sink_vol is None:
                continue
            self._push_display(sink_vol)
            # While muted the fader belongs at the bottom; the Pico is holding
            # it there and remembering where to return to. Driving it to the
            # sink volume now would fight that and lose the restore position.
            if muted:
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
