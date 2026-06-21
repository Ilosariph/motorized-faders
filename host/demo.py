#!/usr/bin/env python3
"""
Motorized Fader Controller — demo script

Cycles fader 1 through a sequence of setpoints, waits, then repeats.
Useful for stress-testing PID tune across move sizes.

Usage:
    python demo.py [/dev/ttyACM0]

Dependencies:
    pip install pyserial
"""

import sys
import time
import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
BAUD = 115200

# Setpoints to cycle through (fader 1). Mix of long + short moves.
SEQUENCE = [0, 80, 20, 100, 50]

# Position tolerance (%) — fader considered "arrived" when within this.
ARRIVE_TOL = 1.0
# Max time to wait for fader to reach setpoint (seconds).
ARRIVE_TIMEOUT_S = 4.0
# Extra hold once arrived, before next move (seconds).
HOLD_AFTER_ARRIVE_S = 0.8

# Pause between full cycles (seconds).
CYCLE_PAUSE_S = 8.0


def find_pico_port():
    for port in serial.tools.list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


def connect(port=None):
    if port is None:
        port = find_pico_port()
    if port is None:
        port = input("Pico not found. Enter port (e.g. /dev/ttyACM0): ").strip()
    print(f"Connecting to {port} at {BAUD} baud...")
    ser = serial.Serial(port, BAUD, timeout=0.1)
    ser.reset_input_buffer()
    return ser


def send_setpoint(ser, f1, f2=0.0):
    cmd = f"SET:{f1:.1f},{f2:.1f}\n"
    ser.write(cmd.encode("ascii"))


def read_lines(ser, buf):
    """Drain serial, return (new_buf, list_of_lines)."""
    data = ser.read(128)
    if not data:
        return buf, []
    buf += data.decode("ascii", errors="ignore")
    lines = []
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        lines.append(line.strip())
    return buf, lines


def latest_pos(lines, last):
    """Extract latest POS:f1,f2 from line list. Return last if none found."""
    for line in lines:
        if line.startswith("POS:"):
            try:
                f1, f2 = line[4:].split(",")
                last = (float(f1), float(f2))
            except (ValueError, IndexError):
                pass
    return last


def wait_arrived(ser, target, buf, fader_idx=0):
    """Block until fader[fader_idx] within ARRIVE_TOL of target, or timeout."""
    deadline = time.time() + ARRIVE_TIMEOUT_S
    pos = (0.0, 0.0)
    while time.time() < deadline:
        buf, lines = read_lines(ser, buf)
        pos = latest_pos(lines, pos)
        if abs(pos[fader_idx] - target) <= ARRIVE_TOL:
            return buf, pos[fader_idx], True
        time.sleep(0.02)
    return buf, pos[fader_idx], False


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        ser = connect(port)
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)

    print(f"Demo: {SEQUENCE}  tol={ARRIVE_TOL}%  timeout={ARRIVE_TIMEOUT_S}s"
          f"  pause={CYCLE_PAUSE_S}s")
    print("Ctrl+C to exit.\n")

    buf = ""
    cycle = 0
    try:
        while True:
            cycle += 1
            print(f"--- cycle {cycle} ---")
            for sp in SEQUENCE:
                t0 = time.time()
                send_setpoint(ser, sp)
                buf, final_pos, arrived = wait_arrived(ser, sp, buf)
                dt = time.time() - t0
                mark = "OK" if arrived else "TIMEOUT"
                print(f"  SET={sp:5.1f}  pos={final_pos:5.1f}  "
                      f"t={dt:.2f}s  [{mark}]")
                time.sleep(HOLD_AFTER_ARRIVE_S)
            print(f"  pause {CYCLE_PAUSE_S}s")
            time.sleep(CYCLE_PAUSE_S)
    except KeyboardInterrupt:
        print("\nStopping. Holding last setpoint.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
