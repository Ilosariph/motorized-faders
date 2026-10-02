#!/usr/bin/env python3
"""
Motorized Fader Controller — PC host interface
Connects to Pico W over USB serial, displays fader positions,
and lets you send setpoints.

Usage:
    python host.py [/dev/ttyACM0]

Dependencies:
    pip install pyserial

Setpoint input:
    Type up to NUM_FADERS numbers separated by spaces or commas, e.g.:
        50 75 20 90 -> all four faders
        50 75       -> faders 1 and 2 only; 3 and 4 unchanged
        50          -> fader 1 only
        , , 30      -> fader 3 only (empty slots are skipped)
"""

import sys
import threading
import time
import serial
import serial.tools.list_ports

# Raspberry Pi Pico W USB VID (MicroPython USB serial)
PICO_VID = 0x2E8A
BAUD = 115200

# The 4-fader PCB reports four slots; the 2-fader breadboard reports two and
# the extra slots simply stay at 0.
NUM_FADERS = 4

state = {
    "positions": [0.0] * NUM_FADERS,
    "muted": [False] * NUM_FADERS,
    "status": "connecting",  # "connecting", "calibrating", "running", "error"
    "rail": None,            # None until the Pico reports RAIL:
}
state_lock = threading.Lock()


def find_pico_port():
    for port in serial.tools.list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


def connect(port=None):
    if port is None:
        port = find_pico_port()
    if port is None:
        port = input(
            "Pico not found automatically. Enter serial port (e.g. /dev/ttyACM0): "
        ).strip()
    print(f"Connecting to {port} at {BAUD} baud...")
    ser = serial.Serial(port, BAUD, timeout=0.1)
    ser.reset_input_buffer()
    return ser


def parse_line(line):
    if line.startswith("POS:"):
        parts = line[4:].split(",")
        with state_lock:
            for idx, raw in enumerate(parts[:NUM_FADERS]):
                try:
                    state["positions"][idx] = float(raw)
                except ValueError:
                    continue
            state["status"] = "running"
    elif line.startswith("MUTE:"):
        parts = line[5:].split(",", 1)
        if len(parts) == 2:
            try:
                idx = int(parts[0]) - 1
            except ValueError:
                return
            if 0 <= idx < NUM_FADERS:
                with state_lock:
                    state["muted"][idx] = parts[1].strip() == "1"
    elif line.startswith("RAIL:"):
        parts = line[5:].split(",", 1)
        volts = parts[1].strip() if len(parts) > 1 else "?"
        with state_lock:
            state["rail"] = (parts[0].strip() == "ok", volts)
    elif line.startswith("BTN:"):
        # Buttons are handled by `faders.run`, not this bring-up tool. Print
        # them so the ladder can be verified by pressing each one.
        print(f"\r  [button] {line[4:].strip()}" + " " * 40)
    elif line.startswith("CAL:"):
        with state_lock:
            state["status"] = "calibrating" if "start" in line else "running"


def reader_thread(ser, stop_event):
    buf = ""
    while not stop_event.is_set():
        try:
            data = ser.read(64)
            if not data:
                continue
            buf += data.decode("ascii", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                parse_line(line.strip())
        except serial.SerialException:
            with state_lock:
                state["status"] = "error"
            break
        except Exception:
            time.sleep(0.05)


def make_bar(pct, width=20):
    filled = max(0, min(width, int(pct / 100.0 * width)))
    return "#" * filled + "-" * (width - filled)


def send_setpoint(ser, values):
    """`values` is a list of NUM_FADERS floats or None (leave unchanged)."""
    slots = ["" if v is None else f"{v:.1f}" for v in values]
    cmd = "SET:" + ",".join(slots) + "\n"
    try:
        ser.write(cmd.encode("ascii"))
    except serial.SerialException:
        print("\n[error] Failed to send — serial connection lost.")


def parse_input(text):
    """
    Parse user input into a list of NUM_FADERS setpoints, where None means
    "leave this fader unchanged".

    Accepts: '50 75 20 90', '50,75', '50', ', , 30' (fader 3 only).
    Returns the list, or None if nothing valid was given.
    """
    # Split on commas first so empty slots survive, then on whitespace.
    if "," in text:
        fields = [f.strip() for f in text.split(",")]
    else:
        fields = text.split()
    if not fields:
        return None

    values = [None] * NUM_FADERS
    errors = []
    got_one = False
    for idx, field in enumerate(fields[:NUM_FADERS]):
        if not field:
            continue
        try:
            value = float(field)
        except ValueError:
            errors.append(f"F{idx + 1}='{field}' is not a number")
            continue
        if not 0 <= value <= 100:
            errors.append(f"F{idx + 1}={value} out of range (0-100)")
            continue
        values[idx] = value
        got_one = True

    if errors:
        print("\n[warn] " + ", ".join(errors))
    return values if got_one else None


def display_loop(stop_event):
    """
    Runs in a daemon thread, overwrites the current line with fader status.
    The main thread's blocking input() call will interrupt this naturally.
    """
    while not stop_event.is_set():
        with state_lock:
            positions = list(state["positions"])
            muted = list(state["muted"])
            status = state["status"]
            rail = state["rail"]

        if status == "calibrating":
            status_str = "[calibrating...]"
        elif status == "error":
            status_str = "[serial error]"
        elif status == "connecting":
            status_str = "[waiting for data...]"
        elif rail is not None and not rail[0]:
            status_str = f"[RAIL FAIL {rail[1]}V — motors disabled]"
        else:
            status_str = ""

        cells = []
        for idx, pos in enumerate(positions):
            flag = "M" if muted[idx] else " "
            cells.append(f"F{idx + 1}{flag}{pos:5.1f}% [{make_bar(pos, 10)}]")
        sys.stdout.write("\r  " + "  ".join(cells) + f"  {status_str}   ")
        sys.stdout.flush()
        time.sleep(0.1)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        ser = connect(port)
    except Exception as e:
        print(f"[error] Could not open serial port: {e}")
        print("Tip: make sure you are in the 'dialout' group: sudo usermod -aG dialout $USER")
        sys.exit(1)

    stop_event = threading.Event()

    reader = threading.Thread(target=reader_thread, args=(ser, stop_event), daemon=True)
    reader.start()

    display = threading.Thread(target=display_loop, args=(stop_event,), daemon=True)
    display.start()

    print("Motorized Fader Controller")
    print(f"  Enter up to {NUM_FADERS} setpoints, e.g. '50 75 20 90'")
    print("  Fewer values leaves the rest unchanged; ', , 30' sets fader 3 only.")
    print("  'M' beside a fader means muted. Press a fader button to toggle.")
    print("  Ctrl+C to exit.\n")

    try:
        while True:
            # Blocking input — display thread keeps updating above this line
            text = input()
            values = parse_input(text)
            if values is not None:
                send_setpoint(ser, values)
                shown = "  ".join(
                    f"F{i + 1}={v:.1f}%" for i, v in enumerate(values)
                    if v is not None
                )
                print(f"\r  -> Set {shown}" + " " * 20)

    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
