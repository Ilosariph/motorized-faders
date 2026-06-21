#!/usr/bin/env python3
"""
Motorized Fader Controller — raw/diagnostic host

Two jobs in one tool:
  1. Print every line the Pico sends (POS, RAW, CAL, DBG, anything else)
     in append-only form — no overwriting, so you can see history.
  2. Let you type setpoints or arbitrary serial commands and forward them
     to the Pico verbatim.

Type a bare number or `f1 f2` -> sent as 'SET:f1,f2\\n' (like host.py).
Type anything starting with a non-digit -> sent verbatim with '\\n'
appended, e.g. 'SET:25,0', 'PING', etc.

Usage:
    python host_raw.py [/dev/ttyACM0]

Dependencies:
    pip install pyserial
"""

import sys
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


def reader_thread(ser, stop_event):
    buf = ""
    while not stop_event.is_set():
        try:
            data = ser.read(128)
            if not data:
                continue
            buf += data.decode("ascii", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    print(f"<< {line}")
        except serial.SerialException:
            print("<< [serial error]")
            break
        except Exception as e:
            print(f"<< [reader err: {e}]")
            time.sleep(0.05)


def looks_numeric(text):
    text = text.strip().replace(",", " ")
    parts = text.split()
    if not parts:
        return False
    try:
        [float(p) for p in parts]
        return True
    except ValueError:
        return False


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        ser = connect(port)
    except Exception as e:
        print(f"[error] Could not open serial port: {e}")
        sys.exit(1)

    print("Raw diagnostic host. Every Pico line printed verbatim.")
    print("Input modes:")
    print("  '50'        -> SET:50.0,0.0")
    print("  '50 75'     -> SET:50.0,75.0")
    print("  'SET:25,0'  -> sent as-is + newline")
    print("  any text    -> sent as-is + newline")
    print("Ctrl+C to exit.\n")

    stop_event = threading.Event()
    reader = threading.Thread(
        target=reader_thread, args=(ser, stop_event), daemon=True
    )
    reader.start()

    try:
        while True:
            text = input()
            if not text.strip():
                continue

            if looks_numeric(text):
                parts = text.replace(",", " ").split()
                f1 = float(parts[0])
                f2 = float(parts[1]) if len(parts) > 1 else 0.0
                cmd = f"SET:{f1:.1f},{f2:.1f}\n"
            else:
                cmd = text.rstrip("\n") + "\n"

            try:
                ser.write(cmd.encode("ascii"))
                print(f">> {cmd.rstrip()}")
            except serial.SerialException:
                print("[error] write failed — serial lost.")
                break
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
