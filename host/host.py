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
    Type two numbers separated by space or comma, e.g.:
        50 75       -> fader 1 to 50%, fader 2 to 75%
        0 100       -> fader 1 to bottom, fader 2 to top
        50          -> fader 1 only (fader 2 unchanged)
"""

import sys
import threading
import time
import serial
import serial.tools.list_ports

# Raspberry Pi Pico W USB VID (MicroPython USB serial)
PICO_VID = 0x2E8A
BAUD = 115200

state = {
    "f1": 0.0,
    "f2": 0.0,
    "status": "connecting",  # "connecting", "calibrating", "running", "error"
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
        try:
            parts = line[4:].split(",")
            f1 = float(parts[0])
            f2 = float(parts[1])
            with state_lock:
                state["f1"] = f1
                state["f2"] = f2
                state["status"] = "running"
        except (ValueError, IndexError):
            pass
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


def send_setpoint(ser, f1, f2):
    cmd = f"SET:{f1:.1f},{f2:.1f}\n"
    try:
        ser.write(cmd.encode("ascii"))
    except serial.SerialException:
        print("\n[error] Failed to send — serial connection lost.")


def parse_input(text, current_f1, current_f2):
    """
    Parse user input into (f1, f2) setpoints.
    Accepts: '50 75', '50,75', '50' (only f1).
    Returns (f1, f2) or None if invalid.
    """
    text = text.strip().replace(",", " ")
    parts = text.split()
    if not parts:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None

    f1 = values[0] if len(values) >= 1 else current_f1
    f2 = values[1] if len(values) >= 2 else current_f2

    # Validate range
    errors = []
    if not 0 <= f1 <= 100:
        errors.append(f"F1={f1} out of range (0–100)")
    if not 0 <= f2 <= 100:
        errors.append(f"F2={f2} out of range (0–100)")
    if errors:
        print("\n[warn] " + ", ".join(errors))
        return None
    return f1, f2


def display_loop(stop_event):
    """
    Runs in a daemon thread, overwrites the current line with fader status.
    The main thread's blocking input() call will interrupt this naturally.
    """
    while not stop_event.is_set():
        with state_lock:
            f1 = state["f1"]
            f2 = state["f2"]
            status = state["status"]

        if status == "calibrating":
            status_str = "[calibrating...]"
        elif status == "error":
            status_str = "[serial error]"
        elif status == "connecting":
            status_str = "[waiting for data...]"
        else:
            status_str = ""

        bar1 = make_bar(f1)
        bar2 = make_bar(f2)
        line = (
            f"\r  F1: {f1:5.1f}% [{bar1}]  "
            f"F2: {f2:5.1f}% [{bar2}]  {status_str}   "
        )
        sys.stdout.write(line)
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
    print("  Enter setpoints as: F1 F2  (e.g. '50 75')")
    print("  Or just F1 to leave F2 unchanged.")
    print("  Ctrl+C to exit.\n")

    try:
        while True:
            # Blocking input — display thread keeps updating above this line
            text = input()
            with state_lock:
                current_f1 = state["f1"]
                current_f2 = state["f2"]

            result = parse_input(text, current_f1, current_f2)
            if result is not None:
                f1, f2 = result
                send_setpoint(ser, f1, f2)
                # Print confirmation on a fresh line
                print(f"\r  -> Set F1={f1:.1f}%  F2={f2:.1f}%")

    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
