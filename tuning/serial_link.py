"""
Serial link to the Pico for PID tuning.

Wraps pyserial so the tuner can:
  - find/open the Pico
  - push runtime PID gains (PID:kp,ki,kd) and wait for the PID:OK ack
  - issue a SET: step and record the resulting POS: trace until the
    fader reports STATE:1,IDLE (or a timeout fires)
"""

import threading
import time
import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
BAUD = 115200


def find_pico_port():
    for port in serial.tools.list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


class PicoLink:
    def __init__(self, port=None, debug=False):
        if port is None:
            port = find_pico_port()
        if port is None:
            raise RuntimeError(
                "Pico not found on USB. Pass --port /dev/ttyACM0 explicitly."
            )
        self.ser = serial.Serial(port, BAUD, timeout=0.05)
        self.ser.reset_input_buffer()
        self.debug = debug

        # Reader thread fans incoming lines into per-type queues.
        self._buf = ""
        self._lock = threading.Lock()
        self._pos_samples = []      # list of (t_host, pos1, pos2)
        self._states = {1: None, 2: None}
        self._state_events = []     # list of (t_host, fader_idx, state)
        self._pid_ack = None        # latest PID:OK tuple or None
        self._stop = threading.Event()
        self._recording = False
        self._t0 = 0.0
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass

    # ---- reader ----------------------------------------------------------

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                data = self.ser.read(128)
            except serial.SerialException:
                return
            if not data:
                continue
            self._buf += data.decode("ascii", errors="ignore")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._handle(line.strip())

    def _handle(self, line):
        if not line:
            return
        if self.debug:
            print(f"<< {line}")
        if line.startswith("POS:"):
            try:
                p1, p2 = line[4:].split(",")
                p1 = float(p1)
                p2 = float(p2)
            except ValueError:
                return
            with self._lock:
                if self._recording:
                    self._pos_samples.append((time.monotonic() - self._t0, p1, p2))
        elif line.startswith("STATE:"):
            try:
                idx_s, state = line[6:].split(",", 1)
                idx = int(idx_s)
            except ValueError:
                return
            with self._lock:
                self._states[idx] = state
                if self._recording:
                    self._state_events.append(
                        (time.monotonic() - self._t0, idx, state)
                    )
        elif line.startswith("PID:OK"):
            with self._lock:
                self._pid_ack = line

    # ---- commands --------------------------------------------------------

    def write_line(self, text):
        self.ser.write((text.rstrip("\n") + "\n").encode("ascii"))

    def set_pid(self, kp, ki, kd, timeout=1.0):
        with self._lock:
            self._pid_ack = None
        self.write_line(f"PID:{kp:.4f},{ki:.4f},{kd:.4f}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                ack = self._pid_ack
            if ack is not None:
                return ack
            time.sleep(0.01)
        raise TimeoutError("Pico did not ack PID:")

    def wait_for_calibration(self, timeout=15.0):
        """Block until we see a STATE: edge or any POS line — meaning the
        firmware finished its boot calibration and is in the main loop."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._states[1] is not None or self._pos_samples:
                    return
            time.sleep(0.05)

    def step(self, target_f1, fader_idx=1, settle_timeout=8.0,
             pre_settle=0.3):
        """
        Drive the fader to `target_f1` and record the response.

        Returns dict { 't': [...], 'pos': [...], 'target': float,
                       'start': float, 'aborted': bool }.
        Uses STATE: edges from the Pico to know when the move is done:
        the fader transitions MOVING -> SETTLING -> IDLE.
        """
        # Clear buffers
        with self._lock:
            self._pos_samples = []
            self._state_events = []
            self._recording = True
            self._t0 = time.monotonic()

        # Sample a moment before firing so we have a clear start point.
        time.sleep(pre_settle)

        with self._lock:
            start_pos = (
                self._pos_samples[-1][fader_idx]
                if self._pos_samples else None
            )

        self.write_line(f"SET:{target_f1:.2f},0.0")

        deadline = time.monotonic() + settle_timeout
        aborted = False
        while True:
            time.sleep(0.02)
            with self._lock:
                state = self._states.get(fader_idx)
            # The IDLE we want is the *post-move* one, not the pre-existing
            # IDLE we started from. Detect it via the state-event log.
            with self._lock:
                seen_moving = any(
                    e[1] == fader_idx and e[2] == "MOVING"
                    for e in self._state_events
                )
                seen_idle_after = any(
                    e[1] == fader_idx and e[2] == "IDLE"
                    for e in self._state_events
                ) and seen_moving
            if seen_idle_after:
                break
            if time.monotonic() > deadline:
                aborted = True
                break

        # Brief tail so settled-position averaging has samples
        time.sleep(0.15)

        with self._lock:
            samples = list(self._pos_samples)
            self._recording = False

        t = [s[0] for s in samples]
        pos = [s[fader_idx] for s in samples]
        if start_pos is None and pos:
            start_pos = pos[0]
        return {
            "t": t,
            "pos": pos,
            "target": target_f1,
            "start": start_pos if start_pos is not None else 0.0,
            "aborted": aborted,
        }
