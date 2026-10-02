"""
Firmware tests — control loop, protocol parsing, mute behaviour, buttons,
rail check, display gating. Run with: python3 tests/test_firmware.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import stubs  # noqa: E402
from harness import load_firmware  # noqa: E402
from sim import SimFader  # noqa: E402

FAILURES = []
PASSES = [0]


def check(condition, label):
    if condition:
        PASSES[0] += 1
    else:
        FAILURES.append(label)
        print("  FAIL: " + label)


def close(a, b, tol=0.6):
    return abs(a - b) <= tol


# calibrate() trims 1% of the observed span off each end as margin, so a
# commanded 0% / 100% settles just inside the mechanical limit. Position
# assertions on a driven fader must allow for that plus the PID deadband.
SETTLE_TOL = 2.0


# ---------------------------------------------------------------------------


class Rig:
    """Firmware + 4 simulated faders wired through the stubbed MCP3208."""

    def __init__(self, inverted=(False, False, False, False), vm_volts=9.0):
        self.fw = load_firmware()
        fw = self.fw
        self.sims = [
            SimFader(inverted=inverted[i], position=50.0)
            for i in range(fw.NUM_FADERS)
        ]
        self.vm_volts = vm_volts
        self.button_level = {
            fw.ADC_CH_GENERAL_BTN: fw.MCP_FULL_SCALE,
            fw.ADC_CH_FADER_BTN: fw.MCP_FULL_SCALE,
        }

        self.spi = fw.SPI(0, sck=fw.Pin(18), mosi=fw.Pin(19), miso=fw.Pin(16))
        self.spi.responder = self._adc_responder
        self.adc = fw.MCP3208(self.spi, fw.Pin(fw.PIN_MCP_CS, fw.Pin.OUT))

        self.stby = fw.Pin(fw.PIN_STBY, fw.Pin.OUT)
        self.stby.value(1)

        self.motors = []
        self.faders = []
        for idx in range(fw.NUM_FADERS):
            in1, in2, pwm = fw.MOTOR_PINS[idx]
            motor = fw.MotorDriver(in1, in2, pwm, self.stby)
            self.motors.append(motor)
            channel = fw.ADC_CH_FADER[idx]
            self.faders.append(
                fw.FaderPID(read_raw=lambda ch=channel: self.adc.read_u16(ch),
                            motor=motor)
            )
        self.screens = [None] * fw.NUM_FADERS

    def _adc_responder(self, channel):
        fw = self.fw
        if channel in fw.ADC_CH_FADER:
            return self.sims[fw.ADC_CH_FADER.index(channel)].raw12()
        if channel == fw.ADC_CH_VM_RAIL:
            ratio = fw.VM_DIVIDER_BOTTOM / (fw.VM_DIVIDER_TOP + fw.VM_DIVIDER_BOTTOM)
            return int(self.vm_volts * ratio / fw.ADC_VREF * fw.MCP_FULL_SCALE)
        return self.button_level.get(channel, fw.MCP_FULL_SCALE)

    def tick(self, ms=5):
        """Advance the simulation and run one firmware loop iteration."""
        for motor, sim in zip(self.motors, self.sims):
            sim.set_drive(motor.in1.value(), motor.in2.value(),
                          motor.pwm.duty_u16())
            sim.step(ms / 1000.0, self.stby.value())
        stubs.advance(ms)
        return [f.update() for f in self.faders]

    def run_for(self, ms, step=5):
        for _ in range(ms // step):
            self.tick(step)

    def calibrate_all(self):
        # Patch the firmware's blocking delay so the simulator advances during
        # calibration sweeps; otherwise time never passes and nothing moves.
        def sim_delay(ms):
            for _ in range(max(1, ms // 5)):
                for motor, sim in zip(self.motors, self.sims):
                    sim.set_drive(motor.in1.value(), motor.in2.value(),
                                  motor.pwm.duty_u16())
                    sim.step(5 / 1000.0, self.stby.value())
                stubs.advance(5)

        original = self.fw._delay
        self.fw._delay = sim_delay
        try:
            for fader in self.faders:
                fader.calibrate()
        finally:
            self.fw._delay = original


# ---------------------------------------------------------------------------
# MCP3208 framing
# ---------------------------------------------------------------------------

def test_mcp3208_framing():
    print("MCP3208")
    rig = Rig()
    fw = rig.fw

    levels = {0: 0, 1: 2048, 2: 4095, 3: 1234}
    rig.spi.responder = lambda ch: levels.get(ch, 0)
    for ch, expected in levels.items():
        check(rig.adc.read(ch) == expected,
              f"channel {ch} reads back {expected}")

    # Command frame: start bit + single-ended mode + 3 address bits.
    rig.spi.writes.clear()
    rig.adc.read(5)
    tx = rig.spi.writes[-1]
    check(tx[0] == 0x06 | (5 >> 2), "command byte 0 encodes start/SGL/addr msb")
    check(tx[1] == (5 & 0x03) << 6, "command byte 1 encodes addr lsbs")
    check(len(tx) == 3, "frame is 3 bytes")

    check(rig.adc.read_u16(2) == 4095 << 4, "read_u16 scales 12-bit to 16-bit")

    try:
        rig.adc.read(8)
        check(False, "out-of-range channel rejected")
    except ValueError:
        check(True, "out-of-range channel rejected")

    # CS must idle high and frame each transaction.
    cs = fw.Pin._registry[fw.PIN_MCP_CS]
    check(cs.value() == 1, "CS idles high after a read")


# ---------------------------------------------------------------------------
# Calibration and position
# ---------------------------------------------------------------------------

def test_calibration_both_polarities():
    print("Calibration")
    for inverted in (False, True):
        rig = Rig(inverted=(inverted,) * 4)
        rig.calibrate_all()
        for idx, (fader, sim) in enumerate(zip(rig.faders, rig.sims)):
            check(fader.adc_max > fader.adc_min,
                  f"fader {idx+1} inverted={inverted}: calibrated range sane")
            check(fader.inverted == inverted,
                  f"fader {idx+1} inverted={inverted}: polarity detected")
            check(fader.state == "IDLE",
                  f"fader {idx+1} inverted={inverted}: boots IDLE")

            # Position must track the simulated mechanism at both ends.
            sim.position = 0.0
            check(close(fader.read_position(), 0.0, 1.5),
                  f"fader {idx+1} inverted={inverted}: bottom reads ~0%")
            sim.position = 100.0
            check(close(fader.read_position(), 100.0, 1.5),
                  f"fader {idx+1} inverted={inverted}: top reads ~100%")
            sim.position = 50.0
            check(close(fader.read_position(), 50.0, 1.5),
                  f"fader {idx+1} inverted={inverted}: mid reads ~50%")


def test_collapsed_calibration_does_not_divide_by_zero():
    rig = Rig()
    fader = rig.faders[0]
    fader.adc_min = fader.adc_max = 2000
    check(fader.read_position() == 0.0,
          "collapsed calibration range returns 0.0, no ZeroDivisionError")


# ---------------------------------------------------------------------------
# PID
# ---------------------------------------------------------------------------

def test_pid_reaches_setpoint_and_releases():
    print("PID")
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]

    fader.engage(80.0)
    check(fader.state == "MOVING", "engage() arms the motor")
    rig.run_for(3000)
    check(close(sim.position, 80.0, SETTLE_TOL),
          f"drives to setpoint (got {sim.position:.1f})")

    # Settle timer then releases the motor.
    rig.run_for(rig.fw.SETTLE_MS + 300)
    check(fader.state == "IDLE", "returns to IDLE after settle")
    check(rig.motors[0].pwm.duty_u16() == 0, "motor coasts once IDLE")


def test_min_move_pct_matches_9v_rail():
    fw = load_firmware()
    check(fw.MIN_MOVE_PCT == 35.0, "MIN_MOVE_PCT raised to 35 for the 9 V rail")
    check(fw.CAL_MOTOR_POWER == 70, "calibration power raised to 70 for 9 V")


def test_motor_direction_and_stall_floor():
    rig = Rig()
    motor = rig.motors[0]
    motor.drive(50.0)
    check((motor.in1.value(), motor.in2.value()) == (1, 0), "positive = forward")
    motor.drive(-50.0)
    check((motor.in1.value(), motor.in2.value()) == (0, 1), "negative = reverse")
    motor.drive(0)
    check((motor.in1.value(), motor.in2.value()) == (0, 0), "zero = coast")
    check(motor.pwm.duty_u16() == 0, "zero power = zero duty")

    motor.drive(1.0)  # would be ~655 duty, below stall
    check(motor.pwm.duty_u16() == rig.fw.PWM_MIN, "tiny output floors to PWM_MIN")
    motor.drive(100.0)
    check(motor.pwm.duty_u16() == rig.fw.PWM_MAX, "full output = full duty")
    motor.drive(150.0)
    check(motor.pwm.duty_u16() == rig.fw.PWM_MAX, "over-range duty is clamped")
    check(motor.pwm.freq() == rig.fw.PWM_FREQ, "PWM frequency set to 20 kHz")


def test_pwm_slices_are_distinct():
    fw = load_firmware()
    slices = [((pins[2] >> 1) & 7) for pins in fw.MOTOR_PINS]
    check(len(set(slices)) == len(slices),
          f"4 PWM outputs on 4 distinct slices (got {slices})")


def test_pin_map_matches_design_doc():
    print("Pin map")
    fw = load_firmware()
    expected_motor = ((2, 3, 6), (7, 8, 9), (10, 11, 12), (13, 14, 15))
    check(fw.MOTOR_PINS == expected_motor, "motor pins match the design doc")
    check(fw.PIN_SPI_SCK == 18 and fw.PIN_SPI_MOSI == 19 and fw.PIN_SPI_MISO == 16,
          "SPI0 on GP18/19/16")
    check(fw.PIN_MCP_CS == 17, "MCP3208 CS on GP17")
    check(fw.PIN_DISP_DC == 20, "display DC on GP20")
    check(fw.DISPLAY_CS_PINS == (21, 26, 27, 28), "display CS on GP21/26/27/28")
    check(fw.PIN_STBY == 22, "STBY on GP22")

    # No pin may serve two purposes.
    used = [fw.PIN_STBY, fw.PIN_SPI_SCK, fw.PIN_SPI_MOSI, fw.PIN_SPI_MISO,
            fw.PIN_MCP_CS, fw.PIN_DISP_DC]
    used += list(fw.DISPLAY_CS_PINS)
    for pins in fw.MOTOR_PINS:
        used += list(pins)
    check(len(set(used)) == len(used), "no GPIO assigned twice")
    check(len(used) == 22, f"22 GPIO assigned (got {len(used)})")
    check(4 not in used and 5 not in used, "GP4/GP5 unused (not broken out)")
    check(0 not in used and 1 not in used, "GP0/GP1 left free for I2C")


# ---------------------------------------------------------------------------
# Mute
# ---------------------------------------------------------------------------

def test_mute_parks_at_bottom_and_restores():
    print("Mute")
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]

    fader.engage(75.0)
    rig.run_for(3000 + rig.fw.SETTLE_MS + 300)
    check(close(sim.position, 75.0, SETTLE_TOL), "fader parked at 75%")

    fader.set_mute(True)
    check(fader.muted is True, "mute flag set")
    check(close(fader.unmute_position, 75.0, SETTLE_TOL),
          f"remembered 75% (got {fader.unmute_position:.1f})")
    rig.run_for(3000)
    check(close(sim.position, 0.0, SETTLE_TOL),
          f"muting drives to the bottom (got {sim.position:.1f})")

    rig.run_for(rig.fw.SETTLE_MS + 300)
    restore_target = fader.unmute_position
    fader.set_mute(False)
    rig.run_for(3000)
    check(close(sim.position, restore_target, SETTLE_TOL),
          f"unmuting returns to {restore_target:.1f}% (got {sim.position:.1f})")
    check(fader.muted is False, "mute flag cleared")


def test_manual_move_while_muted_becomes_new_restore_target():
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]

    fader.engage(60.0)
    rig.run_for(3000 + rig.fw.SETTLE_MS + 300)
    fader.set_mute(True)
    rig.run_for(3000 + rig.fw.SETTLE_MS + 300)
    check(fader.state == "IDLE", "muted fader settles to IDLE")

    # User drags the muted fader up by hand.
    sim.position = 40.0
    rig.run_for(200)
    check(close(fader.unmute_position, 40.0, SETTLE_TOL),
          f"manual move updates restore target (got {fader.unmute_position:.1f})")

    # Pressing mute again re-mutes from where they left it.
    remembered = fader.unmute_position
    fader.set_mute(True)
    rig.run_for(3000)
    check(close(sim.position, 0.0, SETTLE_TOL),
          f"re-mute drives back down (got {sim.position:.1f})")
    check(close(fader.unmute_position, remembered, 0.01),
          "re-mute keeps the manual position as the restore target")


def test_repeated_mute_does_not_lose_the_restore_target():
    """
    Regression: the IDLE-branch tracker used to overwrite unmute_position with
    the parked position after a re-mute, so muting twice lost the position the
    fader was supposed to return to.
    """
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]

    fader.engage(70.0)
    rig.run_for(3000 + rig.fw.SETTLE_MS + 300)

    for cycle in range(3):
        fader.set_mute(True)
        rig.run_for(3000 + rig.fw.SETTLE_MS + 400)
        check(close(sim.position, 0.0, SETTLE_TOL),
              f"cycle {cycle}: muted fader parks at the bottom")
        check(close(fader.unmute_position, 70.0, SETTLE_TOL),
              f"cycle {cycle}: restore target survives mute "
              f"(got {fader.unmute_position:.1f})")

        # Mute again while already muted and parked — must not relearn 0%.
        fader.set_mute(True)
        rig.run_for(500)
        check(close(fader.unmute_position, 70.0, SETTLE_TOL),
              f"cycle {cycle}: re-mute while parked keeps the target "
              f"(got {fader.unmute_position:.1f})")

        fader.set_mute(False)
        rig.run_for(3000 + rig.fw.SETTLE_MS + 400)
        check(close(sim.position, 70.0, SETTLE_TOL),
              f"cycle {cycle}: unmute returns to 70% (got {sim.position:.1f})")


def test_park_zone_does_not_block_a_genuine_low_target():
    """A fader the user deliberately leaves near the bottom still restores."""
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]
    fader.engage(12.0)
    rig.run_for(3000 + rig.fw.SETTLE_MS + 300)
    fader.set_mute(True)
    check(close(fader.unmute_position, 12.0, SETTLE_TOL),
          f"12% is above the park zone and is remembered "
          f"(got {fader.unmute_position:.1f})")
    rig.run_for(3000 + rig.fw.SETTLE_MS + 400)
    fader.set_mute(False)
    rig.run_for(3000)
    check(close(sim.position, 12.0, SETTLE_TOL),
          f"unmute returns to 12% (got {sim.position:.1f})")


def test_manual_move_while_unmuted_does_not_touch_restore_target():
    rig = Rig()
    rig.calibrate_all()
    fader, sim = rig.faders[0], rig.sims[0]
    fader.unmute_position = 88.0
    sim.position = 20.0
    rig.run_for(300)
    check(fader.unmute_position == 88.0,
          "unmuted manual movement leaves the restore target alone")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def collect_output(fw, fn):
    """Run fn() capturing the firmware's stdout writes as a list of lines."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return [ln for ln in buf.getvalue().split("\n") if ln]


def test_set_command():
    print("Protocol: SET")
    rig = Rig()
    rig.calibrate_all()
    fw = rig.fw

    fw._handle_command("SET:10,20,30,40", rig.faders, rig.screens)
    check([f.setpoint for f in rig.faders] == [10.0, 20.0, 30.0, 40.0],
          "all four slots engage")
    check(all(f.state == "MOVING" for f in rig.faders), "all four armed")

    # Empty slots must leave that fader entirely alone.
    for fader in rig.faders:
        fader.state = "IDLE"
    fw._handle_command("SET:,55,,", rig.faders, rig.screens)
    check(rig.faders[1].setpoint == 55.0, "filled slot engages")
    check(rig.faders[1].state == "MOVING", "filled slot armed")
    check([rig.faders[i].state for i in (0, 2, 3)] == ["IDLE"] * 3,
          "empty slots stay IDLE")
    check(rig.faders[0].setpoint == 10.0, "empty slot keeps its old setpoint")

    # Short line, out-of-range values, garbage.
    fw._handle_command("SET:99", rig.faders, rig.screens)
    check(rig.faders[0].setpoint == 99.0, "short SET: line works")
    fw._handle_command("SET:150,-20,,", rig.faders, rig.screens)
    check(rig.faders[0].setpoint == 100.0, "over-range setpoint clamps to 100")
    check(rig.faders[1].setpoint == 0.0, "under-range setpoint clamps to 0")

    out = collect_output(fw, lambda: fw._handle_command(
        "SET:abc,,,", rig.faders, rig.screens))
    check(any("DBG:parse err" in ln for ln in out),
          "unparseable SET: reports an error instead of raising")


def test_loopback_lines_are_ignored():
    print("Protocol: loopback")
    rig = Rig()
    rig.calibrate_all()
    fw = rig.fw
    before = [f.setpoint for f in rig.faders]
    for line in ("POS:1,2,3,4", "STATE:1,MOVING", "CAL:done", "RAW:1,2,3,4",
                 "DBG:whatever", "BTN:F1", "RAIL:ok,9.01", "", "   ",
                 "garbage"):
        fw._handle_command(line, rig.faders, rig.screens)
    check([f.setpoint for f in rig.faders] == before,
          "our own output lines echoed back on stdin change nothing")


def test_mute_command_and_confirmation():
    print("Protocol: MUTE")
    rig = Rig()
    rig.calibrate_all()
    fw = rig.fw

    out = collect_output(fw, lambda: fw._handle_command(
        "MUTE:3,1", rig.faders, rig.screens))
    check(rig.faders[2].muted is True, "MUTE:3,1 mutes fader 3 (1-based)")
    check(rig.faders[2].setpoint == 0.0, "muted fader targets the bottom")
    check("MUTE:3,1" in out, f"confirmation emitted (got {out})")
    check(not rig.faders[0].muted and not rig.faders[3].muted,
          "other faders unaffected")

    out = collect_output(fw, lambda: fw._handle_command(
        "MUTE:3,0", rig.faders, rig.screens))
    check(rig.faders[2].muted is False, "MUTE:3,0 unmutes")
    check("MUTE:3,0" in out, "unmute confirmation emitted")

    # Index bounds — 0 and 5 are both invalid for a 1-based 4-fader rig.
    for bad in ("MUTE:0,1", "MUTE:5,1", "MUTE:9,1", "MUTE:x,1", "MUTE:1"):
        out = collect_output(fw, lambda: fw._handle_command(
            bad, rig.faders, rig.screens))
        check(any("DBG:parse err" in ln for ln in out),
              f"'{bad}' rejected with an error line")
    check(not any(f.muted for f in rig.faders),
          "no fader muted by the invalid commands")


def test_disp_command():
    print("Protocol: DISP")
    rig = Rig()
    fw = rig.fw
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "pico"))
    from lib.fader_screen import FaderScreen

    class FakeDisplay:
        def __init__(self):
            self.shown = 0
        def fill(self, c): pass
        def text(self, *a, **k): pass
        def rect(self, *a): pass
        def fill_rect(self, *a): pass
        def hline(self, *a): pass
        def vline(self, *a): pass
        def pixel(self, *a): pass
        def show(self): self.shown += 1

    screens = [FaderScreen(FakeDisplay()) for _ in range(fw.NUM_FADERS)]

    fw._handle_command("DISP:1,sink-music,vol 78%", rig.faders, screens)
    check(screens[0].line1 == "sink-music", "DISP line1 set")
    check(screens[0].line2 == "vol 78%", "DISP line2 set")

    # Text containing commas must survive — only the first two commas delimit.
    fw._handle_command("DISP:2,a,b,c", rig.faders, screens)
    check(screens[1].line1 == "a" and screens[1].line2 == "b,c",
          f"commas in text preserved (got {screens[1].line2!r})")

    fw._handle_command("DISP:3,only-line1", rig.faders, screens)
    check(screens[2].line1 == "only-line1" and screens[2].line2 == "",
          "missing line2 defaults to empty")

    out = collect_output(fw, lambda: fw._handle_command(
        "DISP:7,x,y", rig.faders, screens))
    check(any("DBG:parse err" in ln for ln in out), "out-of-range DISP rejected")

    # A None screen (display absent / fader disabled) must not raise.
    screens[3] = None
    fw._handle_command("DISP:4,x,y", rig.faders, screens)
    check(True, "DISP to an absent display is a no-op")


def test_mute_to_absent_fader_is_safe():
    rig = Rig()
    fw = rig.fw
    faders = [None] * fw.NUM_FADERS
    screens = [None] * fw.NUM_FADERS
    fw._handle_command("MUTE:1,1", faders, screens)
    fw._handle_command("SET:50,,,", faders, screens)
    check(True, "commands addressed to a disabled fader are no-ops")


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def test_button_ladder_decode():
    print("Buttons")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "pico"))
    from lib.buttons import (DEBOUNCE_SAMPLES, FADER_LADDER, GENERAL_LADDER,
                             ButtonLadder, R_TOP)

    level = [4095]
    ladder = ButtonLadder(lambda: level[0], FADER_LADDER, 4095)

    def press(series_r):
        """Hold a button long enough to clear debounce; return events seen."""
        level[0] = int(series_r / (R_TOP + series_r) * 4095)
        return [ladder.poll() for _ in range(DEBOUNCE_SAMPLES + 1)]

    def release():
        level[0] = 4095
        return [ladder.poll() for _ in range(DEBOUNCE_SAMPLES + 1)]

    for name, series_r in FADER_LADDER:
        events = [e for e in press(series_r) if e]
        check(events == [name], f"{name} decodes from {series_r:.0f}R (got {events})")
        check(not any(release()), f"{name} release emits nothing")

    # Idle must decode as nothing at all.
    level[0] = 4095
    check(not any(ladder.poll() for _ in range(5)), "idle emits no presses")

    # A held button reports exactly once, not once per poll.
    press(2200.0)
    extra = [e for e in (ladder.poll() for _ in range(20)) if e]
    check(extra == [], f"held button reports once, not repeatedly (got {extra})")
    release()

    # Single-sample glitches must not register.
    level[0] = 4095
    ladder.poll()
    level[0] = 0
    glitch = ladder.poll()
    level[0] = 4095
    check(glitch is None, "a one-sample glitch is debounced away")

    # Both ladders must use disjoint, correctly ordered bands.
    gen = ButtonLadder(lambda: 4095, GENERAL_LADDER, 4095)
    check([n for _, n in gen._bands] == ["G1", "G2"], "general ladder ordered")
    check([n for _, n in ladder._bands] == ["F1", "F2", "F3", "F4"],
          "fader ladder ordered")

    # Mid-band readings, not just exact values — real resistors have tolerance.
    for name, series_r in FADER_LADDER:
        nominal = series_r / (R_TOP + series_r)
        for error in (-0.03, 0.03):
            level[0] = 4095
            for _ in range(DEBOUNCE_SAMPLES + 1):
                ladder.poll()
            level[0] = max(0, int((nominal + error) * 4095))
            events = [e for e in (ladder.poll()
                                  for _ in range(DEBOUNCE_SAMPLES + 1)) if e]
            check(events == [name],
                  f"{name} still decodes with {error:+.0%} level error")


def test_simultaneous_press_reads_lowest():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "pico"))
    from lib.buttons import DEBOUNCE_SAMPLES, FADER_LADDER, ButtonLadder
    # Two buttons down pull the node to the lower of the two levels, which the
    # design doc accepts: the press decodes as the lower-value button.
    level = [4095]
    ladder = ButtonLadder(lambda: level[0], FADER_LADDER, 4095)
    level[0] = 0  # F1 (0R) wins over anything else
    events = [e for e in (ladder.poll() for _ in range(DEBOUNCE_SAMPLES + 1)) if e]
    check(events == ["F1"], f"simultaneous press decodes as lowest (got {events})")


# ---------------------------------------------------------------------------
# VM rail check
# ---------------------------------------------------------------------------

def test_vm_rail_check():
    print("VM rail")
    for volts, should_pass, label in (
        (9.0, True, "9 V nominal"),
        (8.6, True, "9 V sagging slightly"),
        (5.0, False, "5 V silent PD fallback"),
        (12.0, False, "12 V wrong jumper"),
        (0.0, False, "rail absent"),
    ):
        rig = Rig(vm_volts=volts)
        fw = rig.fw
        measured = fw.read_vm_rail(rig.adc)
        ok = abs(measured - fw.VM_EXPECTED_V) <= fw.VM_TOLERANCE_V
        check(close(measured, volts, 0.15),
              f"{label}: divider measures {volts} V (got {measured:.2f})")
        check(ok is should_pass,
              f"{label}: gate {'accepts' if should_pass else 'rejects'}")


def test_rail_divider_values_match_doc():
    fw = load_firmware()
    ratio = fw.VM_DIVIDER_BOTTOM / (fw.VM_DIVIDER_TOP + fw.VM_DIVIDER_BOTTOM)
    # The doc tabulates these ADC-side voltages for the 10k/3.3k divider.
    for vm, expected in ((5.0, 1.24), (9.0, 2.23), (12.0, 2.98)):
        check(close(vm * ratio, expected, 0.02),
              f"VM {vm} V reads {expected} V at the ADC")
    check(fw.VM_DIVIDER_TOP == 10000.0 and fw.VM_DIVIDER_BOTTOM == 3300.0,
          "divider is 10k/3.3k per the BOM")


# ---------------------------------------------------------------------------
# Display gating
# ---------------------------------------------------------------------------

def test_display_redraws_only_on_change():
    print("Display gating")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "pico"))
    from lib.fader_screen import POSITION_EPSILON, FaderScreen

    class CountingDisplay:
        def __init__(self): self.shown = 0
        def fill(self, c): pass
        def text(self, *a, **k): pass
        def rect(self, *a): pass
        def fill_rect(self, *a): pass
        def hline(self, *a): pass
        def vline(self, *a): pass
        def pixel(self, *a): pass
        def show(self): self.shown += 1

    disp = CountingDisplay()
    screen = FaderScreen(disp)
    screen.set_lines("sink", "vol")
    screen.set_position(50.0)
    check(screen.draw() is True, "first draw always renders")
    check(disp.shown == 1, "one frame written")

    for _ in range(50):
        screen.draw()
    check(disp.shown == 1, "no redraw while nothing changes")

    screen.set_position(50.0 + POSITION_EPSILON / 4)
    screen.draw()
    check(disp.shown == 1, "sub-epsilon position change does not redraw")

    screen.set_position(50.0 + POSITION_EPSILON * 2)
    screen.draw()
    check(disp.shown == 2, "position change past epsilon redraws")

    screen.set_muted(True)
    screen.draw()
    check(disp.shown == 3, "mute state change redraws")

    screen.set_lines("other", "vol")
    screen.draw()
    check(disp.shown == 4, "text change redraws")

    screen.draw(force=True)
    check(disp.shown == 5, "force=True redraws regardless")


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_mcp3208_framing,
        test_calibration_both_polarities,
        test_collapsed_calibration_does_not_divide_by_zero,
        test_pid_reaches_setpoint_and_releases,
        test_min_move_pct_matches_9v_rail,
        test_motor_direction_and_stall_floor,
        test_pwm_slices_are_distinct,
        test_pin_map_matches_design_doc,
        test_mute_parks_at_bottom_and_restores,
        test_manual_move_while_muted_becomes_new_restore_target,
        test_repeated_mute_does_not_lose_the_restore_target,
        test_park_zone_does_not_block_a_genuine_low_target,
        test_manual_move_while_unmuted_does_not_touch_restore_target,
        test_set_command,
        test_loopback_lines_are_ignored,
        test_mute_command_and_confirmation,
        test_disp_command,
        test_mute_to_absent_fader_is_safe,
        test_button_ladder_decode,
        test_simultaneous_press_reads_lowest,
        test_vm_rail_check,
        test_rail_divider_values_match_doc,
        test_display_redraws_only_on_change,
    ]
    for test in tests:
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED, {PASSES[0]} passed")
        for failure in FAILURES:
            print("  - " + failure)
        return 1
    print(f"all {PASSES[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
