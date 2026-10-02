"""
End-to-end test: real firmware objects and a real FaderHost, connected by a
loopback serial pipe. Exercises the whole mute path the user actually touches —
press a fader button, watch the fader travel to the bottom, press again, watch
it return — with nothing stubbed between the two halves except the wire itself.

Run with: python3 tests/integration_test.py
"""

import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import test_host  # noqa: E402  (installs the `serial` stub)
import stubs  # noqa: E402
from harness import load_firmware  # noqa: E402
from sim import SimFader  # noqa: E402

from faders.core import FaderHost  # noqa: E402

FAILURES = []
PASSES = [0]


def check(condition, label):
    if condition:
        PASSES[0] += 1
    else:
        FAILURES.append(label)
        print("  FAIL: " + label)


class LoopbackSerial:
    """Host-side serial endpoint whose writes land in the firmware's stdin."""

    def __init__(self):
        self.to_pico = []
        self.from_pico = b""

    def write(self, data):
        self.to_pico.append(data.decode("ascii"))

    def read(self, n):
        chunk, self.from_pico = self.from_pico[:n], self.from_pico[n:]
        return chunk

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


class Rig:
    """Firmware + simulated faders + host, talking over LoopbackSerial."""

    def __init__(self, num_faders=4):
        self.fw = load_firmware()
        fw = self.fw
        self.num_faders = num_faders
        self.sims = [SimFader(position=50.0) for _ in range(num_faders)]
        self.button_level = {
            fw.ADC_CH_GENERAL_BTN: fw.MCP_FULL_SCALE,
            fw.ADC_CH_FADER_BTN: fw.MCP_FULL_SCALE,
        }
        self.vm_volts = 9.0

        self.spi = fw.SPI(0)
        self.spi.responder = self._adc
        self.adc = fw.MCP3208(self.spi, fw.Pin(fw.PIN_MCP_CS, fw.Pin.OUT))
        self.stby = fw.Pin(fw.PIN_STBY, fw.Pin.OUT)
        self.stby.value(1)

        self.motors, self.faders = [], []
        for idx in range(num_faders):
            in1, in2, pwm = fw.MOTOR_PINS[idx]
            motor = fw.MotorDriver(in1, in2, pwm, self.stby)
            self.motors.append(motor)
            ch = fw.ADC_CH_FADER[idx]
            self.faders.append(
                fw.FaderPID(read_raw=lambda c=ch: self.adc.read_u16(c), motor=motor)
            )
        self.screens = [FakeScreen() for _ in range(num_faders)]

        self.fader_buttons = fw.ButtonLadder(
            lambda: self.adc.read(fw.ADC_CH_FADER_BTN), fw.FADER_LADDER,
            fw.MCP_FULL_SCALE)
        self.general_buttons = fw.ButtonLadder(
            lambda: self.adc.read(fw.ADC_CH_GENERAL_BTN), fw.GENERAL_LADDER,
            fw.MCP_FULL_SCALE)

        self.wire = LoopbackSerial()
        self.host = FaderHost(port="/dev/null", num_faders=num_faders)
        self.host._ser = self.wire
        self.ext = test_host.RecordingExtension()
        self.host.register(self.ext)
        self._host_buf = ""
        self._pico_in = ""

        self.calibrate()

    def _adc(self, channel):
        fw = self.fw
        if channel in fw.ADC_CH_FADER:
            return self.sims[fw.ADC_CH_FADER.index(channel)].raw12()
        if channel == fw.ADC_CH_VM_RAIL:
            ratio = fw.VM_DIVIDER_BOTTOM / (fw.VM_DIVIDER_TOP + fw.VM_DIVIDER_BOTTOM)
            return int(self.vm_volts * ratio / fw.ADC_VREF * fw.MCP_FULL_SCALE)
        return self.button_level.get(channel, fw.MCP_FULL_SCALE)

    # ----- plumbing -------------------------------------------------------

    def _emit_to_host(self, line):
        self.wire.from_pico += (line + "\n").encode("ascii")

    def _pump_host_reads(self):
        data = self.wire.read(4096)
        if data:
            self._host_buf += data.decode("ascii")
            while "\n" in self._host_buf:
                line, self._host_buf = self._host_buf.split("\n", 1)
                self.host._handle_line(line.strip())

    def _pump_pico_reads(self):
        for chunk in self.wire.to_pico:
            self._pico_in += chunk
        self.wire.to_pico.clear()
        while "\n" in self._pico_in:
            line, self._pico_in = self._pico_in.split("\n", 1)
            out = []
            original = self.fw._emit
            self.fw._emit = out.append
            try:
                self.fw._handle_command(line, self.faders, self.screens)
            finally:
                self.fw._emit = original
            for emitted in out:
                self._emit_to_host(emitted)

    def press(self, button):
        """Hold a button long enough for the ladder to debounce it."""
        fw = self.fw
        ladder = dict(fw.FADER_LADDER + fw.GENERAL_LADDER)
        series_r = ladder[button]
        channel = (fw.ADC_CH_FADER_BTN if button.startswith("F")
                   else fw.ADC_CH_GENERAL_BTN)
        from lib.buttons import R_TOP
        level = int(series_r / (R_TOP + series_r) * fw.MCP_FULL_SCALE)
        self.button_level[channel] = level
        self.tick(ms=20, steps=4)
        self.button_level[channel] = fw.MCP_FULL_SCALE
        self.tick(ms=20, steps=4)

    def tick(self, ms=5, steps=1):
        """Advance one full system step: plant, firmware, wire, host."""
        for _ in range(steps):
            for motor, sim in zip(self.motors, self.sims):
                sim.set_drive(motor.in1.value(), motor.in2.value(),
                              motor.pwm.duty_u16())
                sim.step(ms / 1000.0, self.stby.value())
            stubs.advance(ms)

            positions = [f.update() for f in self.faders]

            for idx, fader in enumerate(self.faders):
                state = fader.state_changed()
                if state is not None:
                    self._emit_to_host(f"STATE:{idx + 1},{state}")

            for ladder in (self.fader_buttons, self.general_buttons):
                pressed = ladder.poll()
                if pressed is not None:
                    self._emit_to_host(f"BTN:{pressed}")

            self._emit_to_host(
                "POS:" + ",".join(f"{p:.1f}" for p in positions))

            self._pump_host_reads()
            self._pump_pico_reads()
            self._flush_host_writer()
            self._pump_pico_reads()

    def _flush_host_writer(self):
        """One iteration of the host's SET: coalescing writer."""
        host = self.host
        with host._lock:
            if all(p is None for p in host._pending):
                return
            slots = []
            for idx, val in enumerate(host._pending):
                if val is None:
                    slots.append("")
                else:
                    host._setpoints[idx] = val
                    host._pending[idx] = None
                    slots.append(f"{val:.1f}")
            line = "SET:" + ",".join(slots)
        host._send(line)

    def run_for(self, ms, step=5):
        self.tick(ms=step, steps=ms // step)

    def calibrate(self):
        def sim_delay(ms):
            for _ in range(max(1, ms // 5)):
                for motor, sim in zip(self.motors, self.sims):
                    sim.set_drive(motor.in1.value(), motor.in2.value(),
                                  motor.pwm.duty_u16())
                    sim.step(0.005, self.stby.value())
                stubs.advance(5)
        original = self.fw._delay
        self.fw._delay = sim_delay
        try:
            for fader in self.faders:
                fader.calibrate()
        finally:
            self.fw._delay = original


class FakeScreen:
    def __init__(self):
        self.line1 = ""
        self.line2 = ""
        self.muted = False
        self.position = 0.0
    def set_lines(self, l1, l2):
        self.line1, self.line2 = l1, l2
    def set_muted(self, m):
        self.muted = m
    def set_position(self, p):
        self.position = p
    def draw(self, force=False):
        return False


TOL = 2.5


# ---------------------------------------------------------------------------

def test_button_press_mutes_end_to_end():
    print("Button -> mute -> motion")
    rig = Rig()
    fader, sim = rig.faders[2], rig.sims[2]

    # Park fader 3 at a known position first.
    rig.host.set_fader(2, 72.0)
    rig.run_for(4000)
    check(abs(sim.position - 72.0) <= TOL,
          f"fader 3 parked at 72% (got {sim.position:.1f})")

    # The user presses the fader-3 button.
    rig.press("F3")
    rig.run_for(400)
    check(rig.host.is_muted(2) is True,
          "host records fader 3 muted after the button press")
    check(fader.muted is True, "firmware records fader 3 muted")
    check(rig.ext.mutes == [(2, True)],
          f"extension notified of the mute (got {rig.ext.mutes})")

    rig.run_for(4000)
    check(abs(sim.position - 0.0) <= TOL,
          f"muted fader travelled to the bottom (got {sim.position:.1f})")
    check(abs(fader.unmute_position - 72.0) <= TOL,
          f"firmware remembers 72% (got {fader.unmute_position:.1f})")
    check(rig.screens[2].muted is True, "display told it is muted")

    # Press again to unmute.
    rig.press("F3")
    rig.run_for(400)
    check(rig.host.is_muted(2) is False, "second press unmutes on the host")
    rig.run_for(4000)
    check(abs(sim.position - 72.0) <= TOL,
          f"unmuted fader returned to 72% (got {sim.position:.1f})")
    check(rig.screens[2].muted is False, "display told it is unmuted")


def test_only_the_pressed_fader_moves():
    print("Button isolation")
    rig = Rig()
    for idx in range(4):
        rig.host.set_fader(idx, 60.0)
    rig.run_for(4000)
    before = [s.position for s in rig.sims]

    rig.press("F2")
    rig.run_for(4000)
    check(abs(rig.sims[1].position - 0.0) <= TOL,
          f"fader 2 muted to the bottom (got {rig.sims[1].position:.1f})")
    for idx in (0, 2, 3):
        check(abs(rig.sims[idx].position - before[idx]) <= TOL,
              f"fader {idx + 1} unmoved by fader 2's button")
        check(rig.host.is_muted(idx) is False,
              f"fader {idx + 1} not muted by fader 2's button")


def test_general_button_does_nothing_but_is_reported():
    print("General button")
    rig = Rig()
    for idx in range(4):
        rig.host.set_fader(idx, 55.0)
    rig.run_for(4000)
    before = [s.position for s in rig.sims]

    rig.press("G1")
    rig.run_for(1500)
    check(rig.ext.buttons == ["G1"],
          f"G1 reported to extensions (got {rig.ext.buttons})")
    check(not any(rig.host.is_muted(i) for i in range(4)),
          "G1 mutes nothing")
    for idx in range(4):
        check(abs(rig.sims[idx].position - before[idx]) <= TOL,
              f"G1 leaves fader {idx + 1} alone")


def test_manual_drag_while_muted_then_remute():
    print("Drag while muted")
    rig = Rig()
    sim, fader = rig.sims[0], rig.faders[0]
    rig.host.set_fader(0, 80.0)
    rig.run_for(4000)

    rig.press("F1")
    rig.run_for(5000)
    check(abs(sim.position) <= TOL, "muted to the bottom")

    # User drags the muted fader up to 45%.
    sim.position = 45.0
    rig.run_for(500)
    check(abs(fader.unmute_position - 45.0) <= TOL,
          f"drag sets the new restore target (got {fader.unmute_position:.1f})")

    # Pressing mute again re-mutes from there, as specified.
    rig.press("F1")
    rig.run_for(400)
    check(rig.host.is_muted(0) is False,
          "host toggles out of mute on the second press")
    # Host unmuted it, so it drives back to the dragged position.
    rig.run_for(4000)
    check(abs(sim.position - 45.0) <= TOL,
          f"returns to the dragged position (got {sim.position:.1f})")


def test_position_reports_survive_the_round_trip():
    print("Telemetry")
    rig = Rig()
    rig.sims[3].position = 33.0
    rig.run_for(500)
    reported = [v for i, v in rig.ext.positions if i == 3]
    check(reported and abs(reported[-1] - 33.0) <= TOL,
          f"manual fader movement reaches the extension "
          f"(got {reported[-1] if reported else None})")


# ---------------------------------------------------------------------------

def main():
    for test in (
        test_button_press_mutes_end_to_end,
        test_only_the_pressed_fader_moves,
        test_general_button_does_nothing_but_is_reported,
        test_manual_drag_while_muted_then_remute,
        test_position_reports_survive_the_round_trip,
    ):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED, {PASSES[0]} passed")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print(f"all {PASSES[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
