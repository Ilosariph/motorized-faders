"""
FaderHost — serial transport + extension dispatch for the motorized fader rig.

Talks the Pico protocol from `pico/main.py`:
    Pico -> host:  POS:f1,f2          (position, 0-100%)
                   STATE:idx,IDLE|MOVING|SETTLING
                   CAL:start | CAL:done
                   RAW:..., DBG:...   (ignored)
    host -> Pico:  SET:f1,f2          (setpoints, 0-100%)

Extensions subclass `Extension`. The host calls `on_position(idx, value)` when a
fader physically moves (touched by a human). Extensions call
`host.set_fader(idx, value)` to drive the motor.

Loopback guard: while a fader is moving in response to a `SET:` we just sent,
`on_position` is *not* called for that fader. Pico reports a STATE:idx,IDLE
edge when the move + settle is complete; only then do we resume forwarding
positions. This avoids extensions echoing their own setpoints back into the
source (e.g. PulseAudio) and oscillating.
"""

import sys
import threading
import time

import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
BAUD = 115200
SET_FLUSH_HZ = 50
SET_FLUSH_INTERVAL = 1.0 / SET_FLUSH_HZ


def find_pico_port():
    for port in serial.tools.list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


class Extension:
    """Base class. Override the hooks you need; defaults are no-ops."""

    def on_position(self, fader_idx, value):
        """Called when a fader moves under human control (loopback-filtered)."""

    def on_calibration(self, phase):
        """phase is 'start' or 'done'."""

    def stop(self):
        """Called on shutdown. Release resources."""


class FaderHost:
    def __init__(self, port=None, num_faders=2):
        self.num_faders = num_faders
        self._port = port
        self._ser = None
        self._extensions = []

        # Last position reported by Pico, indexed 0..num_faders-1.
        self._positions = [0.0] * num_faders
        # Last setpoint we sent — used to fill SET slots that nothing updated.
        self._setpoints = [50.0] * num_faders
        # Pending writes from extensions (None = unchanged this tick).
        self._pending = [None] * num_faders
        # Per-fader Pico state. Updated from STATE: lines.
        self._states = ["IDLE"] * num_faders
        # Loopback guard: True while we drove a SET and Pico hasn't returned
        # to IDLE yet. Suppresses on_position for that fader.
        self._self_driven = [False] * num_faders

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = None
        self._writer = None

    # ----- public API ------------------------------------------------------

    def register(self, ext):
        self._extensions.append(ext)

    def set_fader(self, idx, value):
        """Extension entrypoint. Queues a SET:; flushed at ~50 Hz."""
        if not 0 <= idx < self.num_faders:
            return
        value = max(0.0, min(100.0, float(value)))
        with self._lock:
            self._pending[idx] = value
            self._self_driven[idx] = True

    def run(self):
        self._ser = self._connect()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._reader.start()
        self._writer.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        for ext in self._extensions:
            try:
                ext.stop()
            except Exception as e:
                sys.stderr.write(f"[ext stop err] {e}\n")
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass

    # ----- internals -------------------------------------------------------

    def _connect(self):
        port = self._port or find_pico_port()
        if port is None:
            raise RuntimeError("Pico not found (VID 0x2E8A). Pass port= explicitly.")
        sys.stderr.write(f"[faders] connecting to {port} at {BAUD} baud\n")
        ser = serial.Serial(port, BAUD, timeout=0.1)
        ser.reset_input_buffer()
        return ser

    def _reader_loop(self):
        buf = ""
        while not self._stop.is_set():
            try:
                data = self._ser.read(128)
            except serial.SerialException:
                sys.stderr.write("[faders] serial read failed\n")
                self._stop.set()
                return
            if not data:
                continue
            buf += data.decode("ascii", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                self._handle_line(line.strip())

    def _handle_line(self, line):
        if line.startswith("POS:"):
            self._handle_pos(line[4:])
        elif line.startswith("STATE:"):
            self._handle_state(line[6:])
        elif line.startswith("CAL:"):
            phase = "start" if "start" in line else "done"
            for ext in self._extensions:
                try:
                    ext.on_calibration(phase)
                except Exception as e:
                    sys.stderr.write(f"[ext cal err] {e}\n")

    def _handle_pos(self, payload):
        parts = payload.split(",")
        for idx, raw in enumerate(parts[: self.num_faders]):
            try:
                value = float(raw)
            except ValueError:
                continue
            with self._lock:
                self._positions[idx] = value
                suppressed = self._self_driven[idx]
            if suppressed:
                continue
            for ext in self._extensions:
                try:
                    ext.on_position(idx, value)
                except Exception as e:
                    sys.stderr.write(f"[ext pos err] {e}\n")

    def _handle_state(self, payload):
        parts = payload.split(",", 1)
        if len(parts) != 2:
            return
        try:
            pico_idx = int(parts[0])
        except ValueError:
            return
        idx = pico_idx - 1  # Pico uses 1-based, host uses 0-based
        state = parts[1].strip()
        if not 0 <= idx < self.num_faders:
            return
        with self._lock:
            self._states[idx] = state
            # Move complete: release the loopback gate.
            if state == "IDLE":
                self._self_driven[idx] = False

    def _writer_loop(self):
        while not self._stop.is_set():
            time.sleep(SET_FLUSH_INTERVAL)
            with self._lock:
                if all(p is None for p in self._pending):
                    continue
                for idx, val in enumerate(self._pending):
                    if val is not None:
                        self._setpoints[idx] = val
                        self._pending[idx] = None
                line = "SET:" + ",".join(f"{v:.1f}" for v in self._setpoints) + "\n"
            try:
                self._ser.write(line.encode("ascii"))
            except serial.SerialException:
                sys.stderr.write("[faders] serial write failed\n")
                self._stop.set()
                return
