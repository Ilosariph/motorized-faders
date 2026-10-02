"""
Motorized Fader Controller — MicroPython firmware for the 4-fader PCB.

Hardware per docs/pcb_design.md:
  Faders:   4x Alps RSA0N11M9 (100 mm, 10 kOhm), wipers -> MCP3208 CH0-CH3
  ADC:      MCP3208 12-bit 8-channel SPI, VREF ratiometric with fader supply
  Drivers:  2x TB6612FNG, faders 1-2 on driver A, faders 3-4 on driver B
  Displays: 4x SSD1306 128x64 SPI (Adafruit 938), shared SCK/MOSI/DC, own CS
  Buttons:  resistor ladders on MCP3208 CH6 (2 general) and CH7 (4 per-fader)
  Rail:     VM divider into MCP3208 CH4, checked at boot before enabling STBY
  Motor:    9 V from a USB-C PD trigger (PD has no 10 V PDO)

SPI0 is shared between the MCP3208 and all four displays. Safe because the
displays are write-only, so only the MCP3208 ever drives MISO.

Serial protocol (115200 baud, USB). All lines newline-terminated:

  Pico -> Host
    POS:f1,f2,f3,f4          position, 0-100%, one slot per fader
    RAW:r1,r2,r3,r4          raw ADC 0-65535, diagnostics, sent while engaged
    STATE:idx,IDLE|MOVING|SETTLING       idx is 1-based
    BTN:F1..F4|G1|G2         button press edge; the host assigns meaning
    MUTE:idx,0|1             mute state confirmation, idx is 1-based
    RAIL:ok,<volts> | RAIL:fail,<volts>  VM check result at boot
    CAL:start | CAL:done
    DBG:<text>

  Host -> Pico
    SET:f1,f2,f3,f4          setpoints, 0-100%; an empty slot means
                             "leave this fader unchanged"
    MUTE:idx,0|1             mute/unmute fader idx (1-based). Muting parks the
                             fader at the bottom and remembers where it was;
                             unmuting drives it back there.
    DISP:idx,line1,line2     display text for fader idx (1-based). Content is
                             whatever the active host plugin wants to show.

Buttons report presses only — the firmware assigns them no function. The host
decides what a press means, which is what makes a future layer-switch button
a host-side config change rather than a firmware change.
"""

import sys
import select
from machine import Pin, SPI
from utime import ticks_ms, ticks_diff

from lib.buttons import ButtonLadder, FADER_LADDER, GENERAL_LADDER
from lib.fader_screen import FaderScreen
from lib.mcp3208 import MCP3208
from lib.ssd1306_spi import SSD1306_SPI

# ---------------------------------------------------------------------------
# PID tuning constants — adjust these to tune your faders
# ---------------------------------------------------------------------------
KP = 3.5          # Proportional gain — main driving force
KI = 1.5          # Integral gain — corrects steady-state error
KD = 0.05         # Derivative gain — damping, reduces overshoot

DEADBAND = 0.2    # % — motor stops when within this distance of target
DEADBAND_EXIT = 0.5  # % — must exceed this to leave hold state (hysteresis)

# Anti-windup: integral is clamped to this range
INTEGRAL_MAX = 100.0

# Minimum motor output (%) when actively moving — below this the motor
# crawls near stall. Floor non-zero PID output to this to keep short
# moves brisk. Set to 0 to disable.
# Raised from 30.0 for the 9 V motor rail: USB PD has no 10 V PDO, so the
# motors run 10% under their rated voltage (docs/pcb_design.md).
MIN_MOVE_PCT = 35.0

# Calibration sweep power, likewise raised for 9 V (was 60 at 10 V).
CAL_MOTOR_POWER = 70

# Calibration sweeps run until the wiper stops moving, not for a fixed time:
# the RSA0N11M9 has 100 mm of travel (the prototype's RS60N11M9 had 60 mm), so
# any duration long enough to be safe is mostly wasted, and one that is too
# short silently calibrates against a mid-travel point instead of a hard stop.
# A sweep ends when consecutive reads differ by less than CAL_STALL_COUNTS for
# CAL_STALL_READS in a row, meaning the carriage is against its end stop.
CAL_STALL_COUNTS = 120      # of 65535 — ~0.2% of travel
CAL_STALL_READS = 4
CAL_SWEEP_STEP_MS = 30
CAL_SWEEP_TIMEOUT_MS = 4000  # backstop: a disconnected motor must not hang boot

# State machine: motor only runs on demand. After SET:, drive to target,
# hold for SETTLE_MS within deadband, then release. While idle, report
# position only when it changes by REPORT_DELTA (user moved the fader).
SETTLE_MS = 1000
REPORT_DELTA = 0.2

# Muting parks the fader at the bottom. Movement below this point is not read
# as the user choosing a new unmute target — it is the fader sitting where mute
# put it. calibrate() trims 1% of span as margin, so a commanded 0% settles a
# little above true zero and the zone has to clear that.
MUTE_PARK_ZONE_PCT = 3.0

# ---------------------------------------------------------------------------
# Motor / hardware constants
# ---------------------------------------------------------------------------
PWM_FREQ = 20000  # Hz — 20kHz is above hearing range, no motor whine
# Minimum PWM duty below which motor stalls rather than moving slowly.
# Below this threshold the output is treated as zero (coast).
PWM_MIN = 10000
PWM_MAX = 65535

# ---------------------------------------------------------------------------
# Pin map — validated in docs/pcb_design.md against two hardware constraints:
# SPI0 on legal pins, and the four PWM outputs on four distinct slices.
# ---------------------------------------------------------------------------
PIN_STBY = 22          # shared by both TB6612FNG

# (in1, in2, pwm) per fader. PWM pins are GP6/9/12/15 -> slices 3/4/6/7.
MOTOR_PINS = (
    (2, 3, 6),      # fader 1 — driver A channel A
    (7, 8, 9),      # fader 2 — driver A channel B
    (10, 11, 12),   # fader 3 — driver B channel A
    (13, 14, 15),   # fader 4 — driver B channel B
)

PIN_SPI_SCK = 18
PIN_SPI_MOSI = 19
PIN_SPI_MISO = 16
PIN_MCP_CS = 17
PIN_DISP_DC = 20
DISPLAY_CS_PINS = (21, 26, 27, 28)

# ~1 MHz: safe for the MCP3208 at 3.3 V, trivial for the SSD1306. A full
# 128x64 frame is 1 KB, ~8 ms — which is why screens redraw on change only.
SPI_BAUDRATE = 1_000_000

# MCP3208 channel assignment
ADC_CH_FADER = (0, 1, 2, 3)
ADC_CH_VM_RAIL = 4
ADC_CH_GENERAL_BTN = 6
ADC_CH_FADER_BTN = 7

MCP_FULL_SCALE = 4095
ADC_VREF = 3.3

# ---------------------------------------------------------------------------
# VM rail check — a divider into CH4 catches the HUSB238's silent 5 V
# fallback, which otherwise presents as a mechanical fault (docs/pcb_design.md)
# ---------------------------------------------------------------------------
VM_DIVIDER_TOP = 10000.0     # ohms, VM -> CH4
VM_DIVIDER_BOTTOM = 3300.0   # ohms, CH4 -> GND
VM_EXPECTED_V = 9.0
VM_TOLERANCE_V = 1.2        # accepts a sagging 9 V, rejects 5 V and 12 V

# ---------------------------------------------------------------------------
# Hardware configuration — set False for any fader not connected
# ---------------------------------------------------------------------------
FADER_ENABLED = (True, True, True, True)
NUM_FADERS = len(FADER_ENABLED)

# ---------------------------------------------------------------------------
# Serial / timing
# ---------------------------------------------------------------------------
POS_INTERVAL_MS = 50   # send position every 50ms (~20Hz)
MAX_DT_S = 0.1         # cap dt to prevent derivative spike on first PID call
BUTTON_POLL_MS = 15    # buttons are slow; no need to read them every loop
DISPLAY_POLL_MS = 100  # rate limit for redraws, which still only run on change

# The SSD1306's RC reset needs ~5 ms to release before the first command.
DISPLAY_RESET_MS = 10


class MotorDriver:
    def __init__(self, in1, in2, pwm_pin, stby):
        from machine import PWM
        self.in1 = Pin(in1, Pin.OUT)
        self.in2 = Pin(in2, Pin.OUT)
        self.pwm = PWM(Pin(pwm_pin))
        self.pwm.freq(PWM_FREQ)
        # STBY is shared by all four channels, so it is owned by main() and
        # passed in already configured rather than re-created per motor.
        self.stby = stby

    def drive(self, power):
        """
        Drive motor at given power (-100.0 to +100.0).
        Positive = forward (fader up), negative = reverse (fader down).
        """
        duty = int(abs(power) / 100.0 * PWM_MAX)
        if 0 < duty < PWM_MIN:
            # Below stall threshold — boost to PWM_MIN so motor actually moves
            duty = PWM_MIN
        duty = min(duty, PWM_MAX)

        if power > 0:
            self.in1.value(1)
            self.in2.value(0)
        elif power < 0:
            self.in1.value(0)
            self.in2.value(1)
        else:
            self.in1.value(0)
            self.in2.value(0)  # coast

        self.pwm.duty_u16(duty)

    def stop(self):
        self.drive(0)


class FaderPID:
    def __init__(self, read_raw, motor):
        # Hardware access is isolated to this callable, so the ADC can be
        # swapped (internal ADC, MCP3208, a test stub) without touching the
        # control loop. Must return 0-65535.
        self._read = read_raw
        self.motor = motor

        # Calibrated ADC range (set by calibrate())
        self.adc_min = 0
        self.adc_max = 65535
        # True when the wiper reads high at the bottom of travel; set by
        # calibrate() from the observed sweep endpoints.
        self.inverted = False

        # PID state
        self.setpoint = 50.0  # will be overwritten after calibration
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = ticks_ms()
        # Hysteresis: once inside deadband, stay holding until error grows
        # past DEADBAND_EXIT. Prevents buzz from ADC flicker across edge.
        self.in_deadband = False

        # State machine: IDLE (motor off), MOVING (PID driving), SETTLING
        # (PID holding, counting down to release).
        self.state = "IDLE"
        self.settle_start = 0
        self.last_reported_pos = 0.0
        # Tracks last state we emitted on serial so main loop can dedupe.
        self._last_emitted_state = "IDLE"

        # Mute: the fader parks at the bottom and remembers where it was.
        # Unmuting drives it back. If the user moves a muted fader by hand,
        # that new position becomes the restore target, so pressing mute
        # again re-mutes from wherever they left it.
        self.muted = False
        self.unmute_position = 50.0

    def _raw_adc(self):
        """Average 16 samples to reduce noise (kills sub-% jitter)."""
        return sum(self._read() for _ in range(16)) // 16

    def read_raw(self):
        """Return raw ADC value (0-65535), uncalibrated."""
        return self._raw_adc()

    def read_position(self):
        """Return fader position as 0.0-100.0%, 0 = bottom of travel."""
        raw = self._raw_adc()
        raw = max(self.adc_min, min(self.adc_max, raw))
        span = self.adc_max - self.adc_min
        if span <= 0:
            # Calibration failed (collapsed range) — report 0 rather than
            # dividing by zero and killing the control loop.
            return 0.0
        pct = (raw - self.adc_min) / span * 100.0
        return 100.0 - pct if self.inverted else pct

    def calibrate(self, motor_power=CAL_MOTOR_POWER, settle_ms=400):
        """
        Auto-calibrate ADC range by driving fader to both mechanical limits.

        Wiper orientation is detected rather than assumed: whichever limit
        reads lower becomes adc_min, and self.inverted records whether the
        ADC counts down as the fader travels up.
        """
        # Drive to one mechanical limit, then the other. Which limit reads
        # higher on the ADC depends on how the wiper is wired, so don't
        # assume: record what each end actually reads and sort afterwards.
        #
        # The sweep ends when the wiper stops moving, which is the actual
        # condition we care about (carriage against the end stop). Driving for
        # a fixed number of steps instead would calibrate against wherever the
        # fader happened to reach, and that error propagates into every
        # position report afterwards.
        def _sweep(power):
            self.motor.drive(power)
            _delay(settle_ms)
            end = self._raw_adc()
            still = 0
            elapsed = settle_ms
            while still < CAL_STALL_READS and elapsed < CAL_SWEEP_TIMEOUT_MS:
                self.motor.drive(power)
                _delay(CAL_SWEEP_STEP_MS)
                elapsed += CAL_SWEEP_STEP_MS
                reading = self._raw_adc()
                if abs(reading - end) < CAL_STALL_COUNTS:
                    still += 1
                else:
                    still = 0
                end = reading
            self.motor.stop()
            _delay(100)
            if elapsed >= CAL_SWEEP_TIMEOUT_MS:
                # Motor unplugged, jammed, or the rail sagged — the endpoint is
                # not trustworthy. Say so rather than calibrating against it
                # silently.
                _emit("DBG:calibration sweep timed out, endpoint may be wrong")
            return end

        end_fwd = _sweep(motor_power)
        end_rev = _sweep(-motor_power)

        lower = min(end_fwd, end_rev)
        upper = max(end_fwd, end_rev)

        # Wiper polarity: if driving forward lands on the LOW ADC end, the
        # ADC counts down as the fader travels up, so position must be
        # flipped to keep 0% = bottom.
        self.inverted = end_fwd < end_rev

        # Add small margin to avoid clipping at extremes
        margin = int((upper - lower) * 0.01)
        self.adc_min = lower + margin
        self.adc_max = upper - margin

        # Boot in IDLE — fader free, no torque until host sends SET:
        self.setpoint = self.read_position()
        self.last_time = ticks_ms()
        self.state = "IDLE"
        self.motor.stop()
        self.last_reported_pos = self.setpoint
        self.unmute_position = self.setpoint

    def update(self):
        """
        Run one state-machine iteration. Returns current position (0-100%).
        IDLE: motor off, no PID. MOVING: PID drives to setpoint. SETTLING:
        PID still holds, releases motor after SETTLE_MS of stillness.
        """
        now = ticks_ms()
        dt = min(ticks_diff(now, self.last_time) / 1000.0, MAX_DT_S)
        self.last_time = now

        position = self.read_position()

        if self.state == "IDLE":
            # A muted fader the user has dragged somewhere: treat that as the
            # new restore target, so pressing mute again re-mutes from where
            # they left it. Nothing drives the motor in IDLE, so any movement
            # here is the user's hand.
            #
            # Exclude the park zone at the bottom. Muting leaves the fader
            # sitting there, and without this guard that parked position would
            # immediately overwrite the position we are supposed to restore.
            if (
                self.muted
                and position > MUTE_PARK_ZONE_PCT
                and abs(position - self.unmute_position) >= DEADBAND_EXIT
            ):
                self.unmute_position = position
            return position

        error = self.setpoint - position

        if self.state == "MOVING" and abs(error) < DEADBAND:
            self.state = "SETTLING"
            self.settle_start = now

        if self.state == "SETTLING":
            if abs(error) > DEADBAND_EXIT:
                self.state = "MOVING"
                self.integral = 0.0
                self.last_error = 0.0
            elif ticks_diff(now, self.settle_start) >= SETTLE_MS:
                self.motor.stop()
                self.integral = 0.0
                self.last_error = 0.0
                self.state = "IDLE"
                self.last_reported_pos = position
                return position
            else:
                # In deadband, waiting out settle timer — motor off, no PID.
                self.motor.stop()
                return position

        # PID (MOVING only)
        if dt > 0:
            self.integral += error * dt
            self.integral = max(-INTEGRAL_MAX, min(INTEGRAL_MAX, self.integral))
            derivative = (error - self.last_error) / dt
        else:
            derivative = 0.0

        self.last_error = error
        output = KP * error + KI * self.integral + KD * derivative
        output = max(-100.0, min(100.0, output))

        # Floor non-zero output so short moves don't crawl near stall
        if 0 < abs(output) < MIN_MOVE_PCT:
            output = MIN_MOVE_PCT if output > 0 else -MIN_MOVE_PCT

        self.motor.drive(output)
        return position

    def state_changed(self):
        """Return new state if it changed since last call, else None."""
        if self.state != self._last_emitted_state:
            self._last_emitted_state = self.state
            return self.state
        return None

    def engage(self, setpoint):
        """Set new target and arm the motor (state -> MOVING)."""
        self.setpoint = max(0.0, min(100.0, setpoint))
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = ticks_ms()
        self.in_deadband = False
        self.state = "MOVING"

    def set_mute(self, muted):
        """
        Mute: park at the bottom, remembering the current position.
        Unmute: drive back to the remembered position.

        Idempotent — re-muting an already-muted fader re-parks it, which is
        what the user wants after dragging a muted fader by hand.
        """
        if muted:
            # Capture the position to come back to. Prefer the live reading
            # over self.setpoint: the user may have moved the fader by hand
            # since the last SET:. Re-muting an already-muted fader the user
            # has dragged up must keep the target update()'s IDLE branch
            # recorded, so only read the fader when it is outside the park
            # zone — inside it, the stored target is the better answer.
            live = self.read_position()
            if live > MUTE_PARK_ZONE_PCT:
                self.unmute_position = live
            self.muted = True
            self.engage(0.0)
        else:
            self.muted = False
            self.engage(self.unmute_position)


def _delay(ms):
    """Blocking delay in milliseconds."""
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < ms:
        pass


def _emit(line):
    sys.stdout.write(line + "\n")


def read_vm_rail(adc):
    """Return the VM rail voltage measured through the CH4 divider."""
    counts = sum(adc.read(ADC_CH_VM_RAIL) for _ in range(8)) // 8
    at_adc = counts / MCP_FULL_SCALE * ADC_VREF
    ratio = VM_DIVIDER_BOTTOM / (VM_DIVIDER_TOP + VM_DIVIDER_BOTTOM)
    return at_adc / ratio


def _parse_index(text):
    """Parse a 1-based fader index from the protocol into a 0-based one."""
    idx = int(text) - 1
    if not 0 <= idx < NUM_FADERS:
        raise ValueError("fader index out of range")
    return idx


def _handle_command(line, faders, screens):
    line = line.strip()
    if not line:
        return
    # Ignore everything that is not an input command — notably our own output
    # lines (CAL:, POS:, STATE:, ...), which can arrive back on stdin as a USB
    # CDC loopback and would otherwise be parsed as commands.
    try:
        if line.startswith("SET:"):
            _handle_set(line[4:], faders)
        elif line.startswith("MUTE:"):
            _handle_mute(line[5:], faders, screens)
        elif line.startswith("DISP:"):
            _handle_disp(line[5:], screens)
    except (ValueError, IndexError) as e:
        _emit("DBG:parse err {}".format(e))


def _handle_set(payload, faders):
    parts = payload.split(",")
    # An empty slot means "leave this fader alone" — the host only fills
    # slots something actually asked to move. Engaging every slot on every
    # SET: would re-command a fader the user is currently holding, or drive
    # it back to a stale setpoint.
    for idx, raw in enumerate(parts[:NUM_FADERS]):
        fader = faders[idx]
        if fader is None or not raw.strip():
            continue
        fader.engage(float(raw))


def _handle_mute(payload, faders, screens):
    parts = payload.split(",")
    if len(parts) < 2:
        raise ValueError("MUTE needs idx,state")
    idx = _parse_index(parts[0])
    fader = faders[idx]
    if fader is None:
        return
    muted = parts[1].strip() not in ("0", "", "false", "False")
    fader.set_mute(muted)
    if screens[idx] is not None:
        screens[idx].set_muted(muted)
    # Confirm, so the host tracks the state the firmware actually holds
    # rather than assuming its command landed.
    _emit("MUTE:{},{}".format(idx + 1, 1 if muted else 0))


def _handle_disp(payload, screens):
    # Only split off the index: the text itself may contain commas, and the
    # second comma separates the two lines.
    parts = payload.split(",", 2)
    idx = _parse_index(parts[0])
    if screens[idx] is None:
        return
    line1 = parts[1] if len(parts) > 1 else ""
    line2 = parts[2] if len(parts) > 2 else ""
    screens[idx].set_lines(line1, line2)


def _build_screens(spi):
    """Bring up the four displays. A missing display must not kill the rig."""
    dc = Pin(PIN_DISP_DC, Pin.OUT)
    # The shared RC reset on DISP_RES needs to release before the first
    # command reaches any controller.
    _delay(DISPLAY_RESET_MS)

    screens = []
    for idx in range(NUM_FADERS):
        if not FADER_ENABLED[idx]:
            screens.append(None)
            continue
        try:
            cs = Pin(DISPLAY_CS_PINS[idx], Pin.OUT)
            screens.append(FaderScreen(SSD1306_SPI(spi, dc, cs)))
        except Exception as e:
            # Displays are cosmetic; faders must still work without them.
            _emit("DBG:display {} init failed: {}".format(idx + 1, e))
            screens.append(None)
    return screens


def main():
    # WiFi-disable code removed — this board uses RTL8720 (not CYW43) and
    # importing `network` prints '[CYW43] Failed to ...' messages to stdout,
    # corrupting the SET: command stream. If you port back to a real Pico W,
    # re-add: wlan = network.WLAN(network.STA_IF); wlan.active(False)

    # STBY starts LOW: the H-bridges stay disabled until the VM rail is
    # verified. The 10k pull-up defines the pin during MCU reset, before this
    # line runs; from here on firmware owns it.
    stby = Pin(PIN_STBY, Pin.OUT)
    stby.value(0)

    spi = SPI(
        0,
        baudrate=SPI_BAUDRATE,
        polarity=0,
        phase=0,
        sck=Pin(PIN_SPI_SCK),
        mosi=Pin(PIN_SPI_MOSI),
        miso=Pin(PIN_SPI_MISO),
    )
    adc = MCP3208(spi, Pin(PIN_MCP_CS, Pin.OUT))

    screens = _build_screens(spi)

    # Gate on a measured rail, never on the assumption that PD negotiated the
    # voltage we asked for. The HUSB238 falls back to 5 V silently, which at
    # the bench looks like a stiff fader or a bad motor joint.
    vm = read_vm_rail(adc)
    rail_ok = abs(vm - VM_EXPECTED_V) <= VM_TOLERANCE_V
    _emit("RAIL:{},{:.2f}".format("ok" if rail_ok else "fail", vm))

    faders = []
    for idx in range(NUM_FADERS):
        if not FADER_ENABLED[idx]:
            faders.append(None)
            continue
        in1, in2, pwm_pin = MOTOR_PINS[idx]
        motor = MotorDriver(in1, in2, pwm_pin, stby)
        channel = ADC_CH_FADER[idx]
        faders.append(
            FaderPID(
                # << 4 scales the 12-bit result into the 16-bit adc_min/max
                # convention the PID body already uses.
                read_raw=lambda ch=channel: adc.read_u16(ch),
                motor=motor,
            )
        )

    general_buttons = ButtonLadder(
        lambda: adc.read(ADC_CH_GENERAL_BTN), GENERAL_LADDER, MCP_FULL_SCALE
    )
    fader_buttons = ButtonLadder(
        lambda: adc.read(ADC_CH_FADER_BTN), FADER_LADDER, MCP_FULL_SCALE
    )

    if not rail_ok:
        # Leave STBY low, skip calibration. Buttons and displays still work,
        # so the failure is visible and diagnosable rather than silent.
        _emit("DBG:VM rail out of band, motors disabled")
        for idx, screen in enumerate(screens):
            if screen is not None:
                screen.set_lines("RAIL FAIL", "{:.1f}V expected {:.0f}V".format(
                    vm, VM_EXPECTED_V))
                screen.draw(force=True)
    else:
        stby.value(1)
        _emit("CAL:start")
        for idx, screen in enumerate(screens):
            if screen is not None:
                screen.set_lines("Calibrating", "fader {}".format(idx + 1))
                screen.draw(force=True)
        for fader in faders:
            if fader is not None:
                fader.calibrate()
        _emit("CAL:done")
        for idx, screen in enumerate(screens):
            if screen is not None:
                screen.set_lines("Fader {}".format(idx + 1), "")
                screen.draw(force=True)

    last_pos_send = ticks_ms()
    last_button_poll = ticks_ms()
    last_display_poll = ticks_ms()
    input_buf = ""

    while True:
        positions = [0.0] * NUM_FADERS
        for idx, fader in enumerate(faders):
            if fader is not None:
                positions[idx] = fader.update()

        # Emit STATE: edges so the host can detect "move complete" without
        # heuristics. Additive — old hosts ignore unknown line prefixes.
        for idx, fader in enumerate(faders):
            if fader is None:
                continue
            state = fader.state_changed()
            if state is not None:
                _emit("STATE:{},{}".format(idx + 1, state))

        now = ticks_ms()

        # Buttons: report the press edge and nothing else. The host owns what
        # a press means, so adding a layer-switch button later touches no
        # firmware.
        if ticks_diff(now, last_button_poll) >= BUTTON_POLL_MS:
            last_button_poll = now
            for ladder in (fader_buttons, general_buttons):
                pressed = ladder.poll()
                if pressed is not None:
                    _emit("BTN:{}".format(pressed))

        # Telemetry: 20Hz heartbeat while engaged (MOVING/SETTLING),
        # on-change-only while idle (user moved fader by hand).
        engaged = any(
            fader is not None and fader.state != "IDLE" for fader in faders
        )
        if ticks_diff(now, last_pos_send) >= POS_INTERVAL_MS:
            send = engaged
            if not send:
                for idx, fader in enumerate(faders):
                    if fader is None:
                        continue
                    if abs(positions[idx] - fader.last_reported_pos) >= REPORT_DELTA:
                        send = True
                        break
            if send:
                _emit("POS:" + ",".join("{:.1f}".format(p) for p in positions))
                if engaged:
                    raws = [
                        fader.read_raw() if fader is not None else 0
                        for fader in faders
                    ]
                    _emit("RAW:" + ",".join(str(r) for r in raws))
                for idx, fader in enumerate(faders):
                    if fader is not None:
                        fader.last_reported_pos = positions[idx]
                last_pos_send = now

        # Displays: redraw on change only. draw() is a no-op when nothing
        # changed, and the rate limit bounds how often that check can cost a
        # full 1 KB frame write mid-move.
        if ticks_diff(now, last_display_poll) >= DISPLAY_POLL_MS:
            last_display_poll = now
            for idx, screen in enumerate(screens):
                if screen is None:
                    continue
                screen.set_position(positions[idx])
                # One frame per pass at most, so a four-display redraw can
                # never stall the PID loop for ~32 ms at once.
                if screen.draw():
                    break

        # Non-blocking serial read
        if select.select([sys.stdin], [], [], 0)[0]:
            char = sys.stdin.read(1)
            if char == '\n':
                _handle_command(input_buf, faders, screens)
                input_buf = ""
            else:
                input_buf += char
                if len(input_buf) > 128:
                    input_buf = ""  # guard against buffer overflow


main()
