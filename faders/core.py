"""
FaderHost — serial transport + extension dispatch for the motorized fader rig.

Talks the Pico protocol from `pico/main.py`:
    Pico -> host:  POS:f1,f2,f3,f4    (position, 0-100%)
                   STATE:idx,IDLE|MOVING|SETTLING
                   BTN:F1..F4|G1|G2   (button press edge)
                   MUTE:idx,0|1       (mute state confirmation)
                   RAIL:ok|fail,volts (VM rail check at boot)
                   CAL:start | CAL:done
                   RAW:..., DBG:...   (ignored)
    host -> Pico:  SET:f1,f2,f3,f4    (setpoints, 0-100%)
                   MUTE:idx,0|1       (mute/unmute a fader)
                   DISP:idx,l1,l2     (display text for a fader)

Extensions subclass `Extension`. The host calls `on_position(idx, value)` when a
fader physically moves (touched by a human). Extensions call
`host.set_fader(idx, value)` to drive the motor, `host.set_mute(idx, muted)` to
mute it, and `host.set_display(idx, line1, line2)` to label its screen.

Button meaning lives here, not in firmware. The Pico reports which button was
pressed and nothing more; `_handle_button` maps that to an action. A future
layer-switch button (one general button re-purposing the four fader buttons)
is therefore a change to this mapping, not a firmware change.

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


# Display text budget. The Pico truncates to fit its own font, but trimming
# here keeps the serial line short and the two ends in agreement.
DISPLAY_LINE_MAX = 21


def _sanitize(text):
    """Strip protocol delimiters from display text and trim it to width."""
    if text is None:
        return ""
    cleaned = str(text).replace(",", " ").replace("\n", " ").replace("\r", " ")
    return cleaned.strip()[:DISPLAY_LINE_MAX]


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

    def on_mute(self, fader_idx, muted):
        """
        Called when a fader's mute state changes, after the Pico confirms it.
        Extensions apply it to whatever they control (e.g. a PulseAudio sink).
        """

    def on_button(self, button):
        """
        Called for every button press the Pico reports: 'F1'-'F4' (per-fader)
        or 'G1'/'G2' (general). Mute is already handled by the host for F*
        buttons; this hook exists for extensions that want the raw event.
        """

    def on_rail(self, ok, volts):
        """VM rail check result at boot. `ok` False means motors are disabled."""

    def stop(self):
        """Called on shutdown. Release resources."""


class FaderHost:
    def __init__(self, port=None, num_faders=4):
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
        # Mute state, mirrored from the Pico's MUTE: confirmations rather than
        # assumed from the commands we send.
        self._muted = [False] * num_faders
        # Last display text sent per fader, so unchanged text is not re-sent.
        self._display = [None] * num_faders
        # VM rail check result, None until the Pico reports it.
        self.rail_ok = None
        self.rail_volts = None

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

    def set_mute(self, idx, muted):
        """
        Mute or unmute a fader. The Pico parks it at the bottom and remembers
        where it was, so the host does not track the restore position.

        Sent immediately rather than queued: a mute is a discrete user action,
        not a continuous value that benefits from coalescing.
        """
        if not 0 <= idx < self.num_faders:
            return
        with self._lock:
            # Guard the loopback for the motion this mute causes, the same way
            # set_fader does — the fader is about to move under our command.
            self._self_driven[idx] = True
        self._send(f"MUTE:{idx + 1},{1 if muted else 0}")

    def is_muted(self, idx):
        if not 0 <= idx < self.num_faders:
            return False
        with self._lock:
            return self._muted[idx]

    def toggle_mute(self, idx):
        """Flip a fader's mute state. Returns the state requested."""
        if not 0 <= idx < self.num_faders:
            return False
        with self._lock:
            target = not self._muted[idx]
        self.set_mute(idx, target)
        return target

    def set_display(self, idx, line1, line2=""):
        """
        Set a fader's display text. Content is the extension's choice — this is
        what makes the screens plugin-configured rather than firmware-defined.

        Commas and newlines are stripped: the protocol is line-based with
        comma-delimited fields, so they would corrupt the frame.
        """
        if not 0 <= idx < self.num_faders:
            return
        line1 = _sanitize(line1)
        line2 = _sanitize(line2)
        with self._lock:
            if self._display[idx] == (line1, line2):
                return  # unchanged — the Pico redraws on change only
            self._display[idx] = (line1, line2)
        self._send(f"DISP:{idx + 1},{line1},{line2}")

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

    def _send(self, line):
        """Write one protocol line immediately. Safe to call from any thread."""
        if self._ser is None:
            return
        try:
            self._ser.write((line + "\n").encode("ascii"))
        except serial.SerialException:
            sys.stderr.write("[faders] serial write failed\n")
            self._stop.set()

    def _dispatch(self, hook, *args):
        """Call `hook` on every extension, isolating failures to one of them."""
        for ext in self._extensions:
            try:
                getattr(ext, hook)(*args)
            except Exception as e:
                sys.stderr.write(f"[ext {hook} err] {e}\n")

    def _handle_line(self, line):
        if line.startswith("POS:"):
            self._handle_pos(line[4:])
        elif line.startswith("STATE:"):
            self._handle_state(line[6:])
        elif line.startswith("BTN:"):
            self._handle_button(line[4:].strip())
        elif line.startswith("MUTE:"):
            self._handle_mute(line[5:])
        elif line.startswith("RAIL:"):
            self._handle_rail(line[5:])
        elif line.startswith("CAL:"):
            phase = "start" if "start" in line else "done"
            self._dispatch("on_calibration", phase)

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
                if not suppressed:
                    # Human moved this fader. Track it as the current setpoint
                    # so the next SET: (which broadcasts every slot) doesn't
                    # command this fader back to a stale value.
                    self._setpoints[idx] = value
            if suppressed:
                continue
            self._dispatch("on_position", idx, value)

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

    def _handle_button(self, button):
        """
        Map a button press to an action. Firmware assigns buttons no meaning,
        so this is the single place that decides what one does.

        Currently one layer: the four fader buttons toggle mute, and the two
        general buttons have no function yet. Adding a layer-switch button
        later means branching here on an active-layer field — no firmware or
        protocol change.
        """
        if not button:
            return
        if button.startswith("F") and button[1:].isdigit():
            idx = int(button[1:]) - 1
            if 0 <= idx < self.num_faders:
                self.toggle_mute(idx)
        # G1 / G2: reserved. A future layer switch lands here.
        self._dispatch("on_button", button)

    def _handle_mute(self, payload):
        parts = payload.split(",", 1)
        if len(parts) != 2:
            return
        try:
            idx = int(parts[0]) - 1
        except ValueError:
            return
        if not 0 <= idx < self.num_faders:
            return
        muted = parts[1].strip() == "1"
        with self._lock:
            if self._muted[idx] == muted:
                return  # already known — the Pico re-confirmed, nothing to do
            self._muted[idx] = muted
        self._dispatch("on_mute", idx, muted)

    def _handle_rail(self, payload):
        parts = payload.split(",", 1)
        ok = parts[0].strip() == "ok"
        try:
            volts = float(parts[1]) if len(parts) > 1 else None
        except ValueError:
            volts = None
        with self._lock:
            self.rail_ok = ok
            self.rail_volts = volts
        if not ok:
            sys.stderr.write(
                f"[faders] VM rail check FAILED at {volts} V — motors "
                "disabled by firmware. Check the HUSB238 9 V jumper.\n"
            )
        self._dispatch("on_rail", ok, volts)

    def _writer_loop(self):
        while not self._stop.is_set():
            time.sleep(SET_FLUSH_INTERVAL)
            with self._lock:
                if all(p is None for p in self._pending):
                    continue
                # Only fill slots an extension actually requested this tick.
                # Empty slots tell the Pico to leave that fader alone, so we
                # never re-command a fader the user is holding.
                slots = []
                for idx, val in enumerate(self._pending):
                    if val is None:
                        slots.append("")
                    else:
                        self._setpoints[idx] = val
                        self._pending[idx] = None
                        slots.append(f"{val:.1f}")
                line = "SET:" + ",".join(slots)
            self._send(line)
