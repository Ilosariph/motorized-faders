"""
Host-side stubs for the MicroPython modules `pico/main.py` imports.

Lets the firmware's control loop, protocol parsing and mute logic be exercised
under CPython. The stubs model behaviour the tests depend on — PWM duty, pin
levels, a simulated fader that actually moves when driven — not the full
MicroPython API.
"""

import sys
import types

_clock_ms = [0]


def advance(ms):
    _clock_ms[0] += ms


def now_ms():
    return _clock_ms[0]


def reset_clock():
    _clock_ms[0] = 0


class Pin:
    OUT = "OUT"
    IN = "IN"
    _registry = {}

    def __init__(self, pin, mode=None, *args, **kwargs):
        self.pin = pin
        self.mode = mode
        self._value = 0
        Pin._registry[pin] = self

    def value(self, val=None):
        if val is None:
            return self._value
        self._value = int(val)
        return None


class PWM:
    def __init__(self, pin):
        self.pin = pin
        self._freq = 0
        self._duty = 0

    def freq(self, f=None):
        if f is None:
            return self._freq
        self._freq = f

    def duty_u16(self, d=None):
        if d is None:
            return self._duty
        self._duty = d


class SPI:
    """Records traffic; MCP3208 reads are served by an installed responder."""

    def __init__(self, bus, baudrate=0, polarity=0, phase=0, sck=None,
                 mosi=None, miso=None):
        self.bus = bus
        self.baudrate = baudrate
        self.writes = []
        self.responder = None

    def write(self, data):
        self.writes.append(bytes(data))

    def write_readinto(self, tx, rx):
        self.writes.append(bytes(tx))
        if self.responder is None:
            for i in range(len(rx)):
                rx[i] = 0
            return
        # Decode the MCP3208 command frame to learn which channel was asked
        # for, then answer with that channel's simulated level.
        channel = ((tx[0] & 0x01) << 2) | (tx[1] >> 6)
        value = self.responder(channel) & 0x0FFF
        rx[0] = 0
        rx[1] = (value >> 8) & 0x0F
        rx[2] = value & 0xFF


def install():
    """Install the fake `machine` / `utime` modules and the `lib` package."""
    machine = types.ModuleType("machine")
    machine.Pin = Pin
    machine.PWM = PWM
    machine.SPI = SPI
    machine.ADC = object
    sys.modules["machine"] = machine

    utime = types.ModuleType("utime")
    utime.ticks_ms = now_ms
    utime.ticks_diff = lambda a, b: a - b
    utime.sleep_ms = advance
    sys.modules["utime"] = utime
