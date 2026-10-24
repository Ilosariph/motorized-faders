#!/usr/bin/env python3
"""
Motorized Fader Controller — 4-fader demo

Drives all four faders at once through a choreographed sequence (mirrored
sweeps, crossovers, small nudges, full-travel swings) and ends with every
fader at 80%. Each step waits for all faders to report IDLE, so moves never
overlap and every step starts from rest.

Usage:
    python host/demo_all.py [/dev/ttyACM0]

Close host.py / Thonny first (one program per serial port).

Dependencies:
    pip install pyserial
"""

import sys
import time
import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
BAUD = 115200
NUM_FADERS = 4

# One setpoint per fader, per step.
SEQUENCE = [
    (50, 50, 50, 50),
    (80, 20, 80, 20),
    (20, 80, 20, 80),
    (90, 10, 60, 40),
    (10, 90, 40, 60),
    (25, 25, 75, 75),
    (22, 27, 73, 78),
    (0, 100, 0, 100),
    (50, 50, 50, 50),
    (100, 0, 100, 0),
    (80, 80, 80, 80),
]

# Per step. Firmware settles 1 s after arrival, so a normal step takes ~1.2 s.
STEP_TIMEOUT_S = 6.0


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
    ser = serial.Serial(port, BAUD, timeout=0.02)
    ser.dtr = True
    ser.reset_input_buffer()
    return ser


def run_step(ser, targets):
    """Send one SET: and wait until every fader is IDLE again."""
    ser.write(("SET:" + ",".join(f"{t:.1f}" for t in targets) + "\n").encode())
    t0 = time.time()
    arrived = [None] * NUM_FADERS
    idle = set()
    stalled = []
    pos = None
    while time.time() - t0 < STEP_TIMEOUT_S and len(idle) < NUM_FADERS:
        line = ser.readline().decode("ascii", errors="ignore").strip()
        if line.startswith("POS:"):
            try:
                pos = [float(x) for x in line[4:].split(",")]
            except ValueError:
                pass
        elif line.startswith("STATE:"):
            idx, state = line[6:].split(",")
            idx = int(idx) - 1
            if state == "SETTLING" and arrived[idx] is None:
                arrived[idx] = time.time() - t0
            elif state == "IDLE":
                idle.add(idx)
        elif line.startswith("DBG:stall"):
            stalled.append(line)
    return arrived, pos, stalled, len(idle) == NUM_FADERS


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        ser = connect(port)
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)

    try:
        for targets in SEQUENCE:
            arrived, pos, stalled, done = run_step(ser, targets)
            times = " ".join(
                f"{a * 1000:4.0f}" if a is not None else "   -" for a in arrived
            )
            final = " ".join(f"{p:5.1f}" for p in pos) if pos else "?"
            mark = "OK" if done and not stalled else "STALL/TIMEOUT"
            print(f"  SET={targets}  arrive_ms=[{times}]  pos=[{final}]  [{mark}]")
            for line in stalled:
                print(f"    {line}")
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
