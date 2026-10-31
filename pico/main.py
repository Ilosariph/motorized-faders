"""
Motorized Fader Controller — MicroPython firmware for the 4-fader PCB.

Hardware per docs/pcb_design.md:
  Faders:   4x Alps RSA0N11M9 (100 mm, 10 kOhm), wipers -> MCP3208 CH0-CH3
  ADC:      MCP3208 12-bit 8-channel SPI, VREF ratiometric with fader supply
  Drivers:  2x TB6612FNG, faders 1-2 on driver A, faders 3-4 on driver B
  Displays: 4x SSD1306 128x64 SPI (Adafruit 326), shared SCK/MOSI/DC, own CS
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
    BTNUP:F1..F4|G1|G2       button release edge, for hold combos
    MUTE:idx,0|1             mute state confirmation, idx is 1-based
    RAIL:ok,<volts> | RAIL:fail,<volts>  VM check result at boot
    CAL:start | CAL:done
    DBG:<text>

  Host -> Pico
    SET:f1,f2,f3,f4          setpoints, 0-100%; an empty slot means
                             "leave this fader unchanged"
    MUTE:idx,0|1[,pos]       mute/unmute fader idx (1-based). Muting parks the
                             fader at the bottom and remembers where it was;
                             unmuting drives it back there. `pos` overrides
                             the remembered position (mute) or the target
                             (unmute), for a host switching what the fader
                             controls while it may still be moving.
    DISP:idx,line1,line2     display text for fader idx (1-based). Content is
                             whatever the active host plugin wants to show.
    SLOT:idx,A|B             A/B slot badge for fader idx; empty clears it.
    CREASES:idx,pos:power:ms,...
                             haptic detents for fader idx: a pulse of
                             `power` % for `ms` against the hand as it drags
                             across `pos`. No entries clears them.
    TUNE:kp,ki,kd,minmove,deadband,deadband_exit
                             live PID gains, empty slot keeps the value;
                             echoed as DBG:tune ..., lost on reset.

Buttons report press and release edges only — the firmware assigns them no
function. The host decides what a press means, which is what makes a future
layer-switch button a host-side config change rather than a firmware change.
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
# Hand-tuned on the rig with step tests (9 V rail). Earlier autotune runs
# were measured while display redraws stalled the loop, which forced timid
# gains; with redraws skipped mid-move the plant takes far more.
KP = 6.0          # Proportional gain — main driving force
KI = 0.0          # Integral gain — corrects steady-state error
KD = 0.05         # Derivative gain — damping, reduces overshoot

# Widened from 0.2/0.5 for the 100 mm fader: at the power needed to break
# stiction it covers more than 0.2% between loop iterations, so it never
# landed inside the band and hunted instead.
DEADBAND = 1.0    # % — motor brakes when within this distance of target
DEADBAND_EXIT = 2.0  # % — must exceed this to leave hold state (hysteresis)

# Anti-windup: integral is clamped to this range
INTEGRAL_MAX = 100.0

# Integral only accumulates within this distance of the target (%), and resets
# when the error changes sign. Integrating across a whole move winds up tens of
# percent of stored drive on the 100 mm fader, which arrives as overshoot.
INTEGRAL_ZONE = 5.0

# Stall guard: a fader that cannot reach its setpoint (jammed, stuck at an end
# stop, held by hand) would otherwise be driven indefinitely at stall current.
# A move is abandoned if it stays engaged longer than MOVE_TIMEOUT_MS, or if
# output stays at STALL_OUTPUT_PCT while the position moves less than
# STALL_PROGRESS_PCT within STALL_WINDOW_MS.
#
# STALL_OUTPUT_PCT is full power: a stuck carriage always gets there, through
# kp x error or the breakaway ramp below. Counting from a lower output (it was
# 50) started the window while the ramp was still climbing, so a fader parked
# at an end stop was abandoned ~50 ms after reaching full power — before it
# could break free.
MOVE_TIMEOUT_MS = 3000
STALL_WINDOW_MS = 300
STALL_PROGRESS_PCT = 0.5
STALL_OUTPUT_PCT = 100.0

# Breakaway: static friction at an end stop is higher than mid-travel, so the
# MIN_MOVE_PCT floor that keeps a moving carriage going may not start one
# parked against a stop. While the position is not progressing, the floor rises
# by this much per ms until the carriage breaks free (or the stall guard above
# gives up), then drops back to MIN_MOVE_PCT.
BREAKAWAY_RAMP_PCT_PER_MS = 0.2

# Minimum motor output (%) when actively moving — below this the motor
# crawls near stall. Floor non-zero PID output to this to keep short
# moves brisk. Set to 0 to disable.
# Raised from 30.0 for the 9 V motor rail: USB PD has no 10 V PDO, so the
# motors run 10% under their rated voltage (docs/pcb_design.md).
MIN_MOVE_PCT = 38.0

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

# Virtual creases: while IDLE the motor is off, so any movement is the user's
# hand. Crossing a crease position fires a short pulse against that motion —
# a bump to feel, never a pull toward the crease.
#
# The anchor only moves once the fader is CREASE_HYST_PCT away from it, so
# ADC flicker while resting on a crease never reads as a crossing. The pulse
# itself shoves the fader back over the crease; re-crossing that same crease
# within CREASE_REFRACTORY_MS of the pulse ending is ignored, or it would
# buzz. Other creases stay live, so a fast drag feels every one.
CREASE_HYST_PCT = 0.8
CREASE_REFRACTORY_MS = 120

# Pulses scale with drag speed: a slow, careful adjustment gets a light tap,
# a fast flick the full bump. Full strength at CREASE_FULL_SPEED %/s. Power
# scales down to CREASE_MIN_POWER, which still beats stiction; length only
# to CREASE_MIN_LENGTH_SCALE of full, since pulses must stay short enough
# not to run into the next crease on a fast drag.
CREASE_FULL_SPEED = 150.0
CREASE_MIN_POWER = 40.0
CREASE_MIN_LENGTH_SCALE = 0.9
CREASE_MIN_MS = 4
MAX_CREASES = 24

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

# Samples averaged per position read. Was 16, but with four faders that put the
# control loop at ~10 ms — long enough for the carriage to travel ~9% between
# reads at full power, which showed up as overshoot. The RC-filtered wiper reads
# within a count or two, so 4 samples lose nothing.
ADC_OVERSAMPLE = 4

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

# Serial input. A CREASES: line with a crease every 10% of a 0-150 sink runs
# ~170 chars, so the line limit has to clear that with room to spare.
INPUT_CHARS_PER_LOOP = 512
INPUT_LINE_MAX = 512


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

    def brake(self):
        """
        Short brake: IN1 = IN2 = high shorts the motor windings through the
        TB6612FNG, stopping the carriage dead instead of letting it coast past
        the target. Only for arrival — an idle fader must coast so the user can
        move it by hand.
        """
        self.in1.value(1)
        self.in2.value(1)
        self.pwm.duty_u16(PWM_MAX)


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

        # Virtual creases: (position, power %, ms) sorted by position.
        self.creases = ()
        self._crease_anchor = None  # None until the first IDLE read
        self._crease_anchor_time = 0  # when the anchor last moved, for speed
        self._nudge_start = ticks_ms()
        self._nudge_ms = -CREASE_REFRACTORY_MS  # no pulse pending at boot
        self._nudge_power = 0.0
        self._nudge_crease = None  # crease the last pulse fired for

        # Stall guard state. `stalled` is set when a move is abandoned and
        # cleared by the main loop once it has reported it.
        self.move_start = 0
        self.stall_ref_pos = 0.0
        self.stall_ref_time = 0
        self.stalled = False
        # Breakaway ramp: last position the carriage was seen moving from.
        self.progress_ref_pos = 0.0
        self.progress_ref_time = 0
        self.last_dt_ms = 0  # loop period, reported by TUNE: for diagnosis

    def _raw_adc(self):
        """Average ADC_OVERSAMPLE samples to reduce noise."""
        return sum(self._read() for _ in range(ADC_OVERSAMPLE)) // ADC_OVERSAMPLE

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
        self.last_dt_ms = ticks_diff(now, self.last_time)
        dt = min(self.last_dt_ms / 1000.0, MAX_DT_S)
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
            self._update_creases(position, now)
            return position

        error = self.setpoint - position

        # MOVING only: SETTLING is the motor-off settle timer, which a slow
        # but successful move would otherwise run into.
        if self.state == "MOVING" and \
                ticks_diff(now, self.move_start) >= MOVE_TIMEOUT_MS:
            return self._abandon(position)

        if self.state == "MOVING" and abs(error) < DEADBAND:
            self.state = "SETTLING"
            self.settle_start = now

        if self.state == "SETTLING":
            if abs(error) > DEADBAND_EXIT:
                self.state = "MOVING"
                self.integral = 0.0
                self.last_error = 0.0
                self.progress_ref_pos = position
                self.progress_ref_time = now
            elif ticks_diff(now, self.settle_start) >= SETTLE_MS:
                self.motor.stop()
                self.integral = 0.0
                self.last_error = 0.0
                self.state = "IDLE"
                self.last_reported_pos = position
                return position
            else:
                # In deadband, waiting out settle timer — brake, no PID.
                self.motor.brake()
                return position

        # PID (MOVING only)
        if error * self.last_error < 0 or abs(error) >= INTEGRAL_ZONE:
            # Crossed the setpoint, or still far from it: stored drive would
            # only push the fader further past.
            self.integral = 0.0
        elif dt > 0:
            self.integral += error * dt
            self.integral = max(-INTEGRAL_MAX, min(INTEGRAL_MAX, self.integral))
        derivative = (error - self.last_error) / dt if dt > 0 else 0.0

        self.last_error = error
        output = KP * error + KI * self.integral + KD * derivative
        output = max(-100.0, min(100.0, output))

        # Floor non-zero output so short moves don't crawl near stall. The
        # floor ramps up while the carriage is not moving, to break stiction.
        if abs(position - self.progress_ref_pos) >= STALL_PROGRESS_PCT:
            self.progress_ref_pos = position
            self.progress_ref_time = now
        stuck_ms = ticks_diff(now, self.progress_ref_time)
        floor = min(100.0, MIN_MOVE_PCT + BREAKAWAY_RAMP_PCT_PER_MS * stuck_ms)
        # Only output that pushes toward the target is floored. A small
        # opposite-sign output is the D term asking to brake; flooring it would
        # turn that into full reverse drive and shake the fader in place.
        if 0 < abs(output) < floor and output * error > 0:
            output = floor if output > 0 else -floor

        # No-progress check: hard drive with the position not changing means
        # the carriage is blocked.
        if abs(output) < STALL_OUTPUT_PCT or \
                abs(position - self.stall_ref_pos) >= STALL_PROGRESS_PCT:
            self.stall_ref_pos = position
            self.stall_ref_time = now
        elif ticks_diff(now, self.stall_ref_time) >= STALL_WINDOW_MS:
            return self._abandon(position)

        self.motor.drive(output)
        return position

    @property
    def nudging(self):
        """A crease pulse is running. Its length is the bump's strength."""
        return ticks_diff(ticks_ms(), self._nudge_start) < self._nudge_ms

    def set_creases(self, creases):
        self.creases = tuple(sorted(creases)[:MAX_CREASES])
        self._crease_anchor = None

    def _update_creases(self, position, now):
        """IDLE only: fire a pulse when the hand drags across a crease."""
        since = ticks_diff(now, self._nudge_start)
        if since < self._nudge_ms:
            return  # pulse running; it was started with motor.drive()
        if self._nudge_power:
            self.motor.stop()
            self._nudge_power = 0.0

        anchor = self._crease_anchor
        if anchor is None:
            self._crease_anchor = position
            self._crease_anchor_time = now
            return
        if abs(position - anchor) < CREASE_HYST_PCT:
            return
        elapsed = ticks_diff(now, self._crease_anchor_time)
        self._crease_anchor = position
        self._crease_anchor_time = now
        if not self.creases:
            return
        refractory = since < self._nudge_ms + CREASE_REFRACTORY_MS

        # Walk toward the hand's motion, so a step that spans two creases
        # fires the one met first.
        motion = 1 if position > anchor else -1
        order = self.creases if motion > 0 else reversed(self.creases)
        for crease, power, ms in order:
            # Side test, not a sign-change product: a reading exactly on the
            # crease would make the product 0 and hide the crossing.
            if (anchor < crease) == (position < crease):
                continue
            if refractory and crease == self._nudge_crease:
                continue  # the last pulse knocking the fader back over it
            speed = abs(position - anchor) * 1000.0 / max(1, elapsed)
            scale = min(1.0, speed / CREASE_FULL_SPEED)
            self._nudge_power = -motion * max(
                min(power, CREASE_MIN_POWER), power * scale)
            self._nudge_start = now
            self._nudge_ms = max(
                CREASE_MIN_MS, int(ms * max(CREASE_MIN_LENGTH_SCALE, scale)))
            self._nudge_crease = crease
            self.motor.drive(self._nudge_power)
            return

    def _abandon(self, position):
        """Give up on the current move: motor off, IDLE, flag for reporting."""
        self.motor.stop()
        self.integral = 0.0
        self.last_error = 0.0
        self.state = "IDLE"
        self.last_reported_pos = position
        self.stalled = True
        return position

    def state_changed(self):
        """Return new state if it changed since last call, else None."""
        if self.state != self._last_emitted_state:
            self._last_emitted_state = self.state
            return self.state
        return None

    def engage(self, setpoint):
        """Set new target and arm the motor (state -> MOVING)."""
        # A commanded move is not the hand: re-anchor once it is IDLE again,
        # and drop any pulse so the PID owns the motor from the first update.
        self._crease_anchor = None
        self._nudge_ms = -CREASE_REFRACTORY_MS
        self._nudge_power = 0.0
        self.setpoint = max(0.0, min(100.0, setpoint))
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = ticks_ms()
        self.in_deadband = False
        self.state = "MOVING"
        self.move_start = self.last_time
        self.stall_ref_pos = self.read_position()
        self.stall_ref_time = self.last_time
        self.progress_ref_pos = self.stall_ref_pos
        self.progress_ref_time = self.last_time
        self.stalled = False

    def set_mute(self, muted, restore=None):
        """
        Mute: park at the bottom, remembering the current position.
        Unmute: drive back to the remembered position.

        `restore` replaces the remembered position. The host passes it when
        it swaps what the fader controls: the fader may be mid-move then, and
        reading it would remember a point on the way.

        Idempotent — re-muting an already-muted fader re-parks it, which is
        what the user wants after dragging a muted fader by hand.
        """
        if restore is not None:
            self.unmute_position = max(0.0, min(100.0, restore))
        if muted and restore is not None:
            self.muted = True
            self.engage(0.0)
        elif muted:
            # Capture the position to come back to. Prefer the live reading
            # over self.setpoint: the user may have moved the fader by hand
            # since the last SET:. Re-muting an already-muted fader the user
            # has dragged up must keep the target update()'s IDLE branch
            # recorded, so only read the fader when it is outside the park
            # zone — inside it, the stored target is the better answer.
            live = self.read_position()
            if live > MUTE_PARK_ZONE_PCT:
                # Within the hold band of the last setpoint the fader has not
                # been moved, only stopped short by the deadband. Keep the
                # commanded value, or every mute cycle drifts by up to DEADBAND.
                if abs(live - self.setpoint) <= DEADBAND_EXIT:
                    self.unmute_position = self.setpoint
                else:
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
        elif line.startswith("SLOT:"):
            _handle_slot(line[5:], screens)
        elif line.startswith("CREASES:"):
            _handle_creases(line[8:], faders)
        elif line.startswith("TUNE:"):
            _handle_tune(line[5:], faders)
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
    restore = None
    if len(parts) > 2 and parts[2].strip():
        restore = float(parts[2])
    fader.set_mute(muted, restore)
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


def _handle_slot(payload, screens):
    parts = payload.split(",", 1)
    idx = _parse_index(parts[0])
    if screens[idx] is not None:
        screens[idx].set_slot(parts[1].strip() if len(parts) > 1 else "")


def _handle_creases(payload, faders):
    parts = payload.split(",")
    idx = _parse_index(parts[0])
    creases = []
    for entry in parts[1:]:
        if not entry.strip():
            continue
        pos, power, ms = entry.split(":")
        pos, power, ms = float(pos), float(power), int(ms)
        if not (0 <= pos <= 100 and 0 < power <= 100 and 0 < ms <= 200):
            raise ValueError("crease out of range")
        creases.append((pos, power, ms))
    if faders[idx] is not None:
        faders[idx].set_creases(creases)


def _handle_tune(payload, faders):
    """
    Live PID tuning: `TUNE:kp,ki,kd,minmove,deadband,deadband_exit`. An empty
    slot keeps that value, so `TUNE:` alone just reports. Changes last until
    reset — copy the final values into the constants at the top of this file.
    """
    global KP, KI, KD, MIN_MOVE_PCT, DEADBAND, DEADBAND_EXIT
    values = [KP, KI, KD, MIN_MOVE_PCT, DEADBAND, DEADBAND_EXIT]
    for idx, raw in enumerate(payload.split(",")[:len(values)]):
        if raw.strip():
            values[idx] = float(raw)
    if values[3] < 0 or values[3] > 100:
        raise ValueError("minmove out of range")
    if not 0 < values[4] <= values[5]:
        raise ValueError("need 0 < deadband <= deadband_exit")
    KP, KI, KD, MIN_MOVE_PCT, DEADBAND, DEADBAND_EXIT = values
    # Loop period matters as much as the gains: the carriage travels
    # speed x dt between reads, which bounds how tight the deadband can be.
    dts = [f.last_dt_ms for f in faders if f is not None]
    dt = max(dts) if dts else 0
    # DBG:, not TUNE:, so a loopback of this line can't re-apply anything.
    _emit("DBG:tune kp={} ki={} kd={} minmove={} deadband={} exit={} "
          "loop={}ms".format(*(values + [dt])))


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
            if fader.stalled:
                fader.stalled = False
                _emit("DBG:stall f{}, move abandoned".format(idx + 1))
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
                if ladder.released is not None:
                    _emit("BTNUP:{}".format(ladder.released))
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
        # full 1 KB frame write.
        #
        # Never while a fader is MOVING: the pure-Python framebuffer blocks
        # the loop for tens of ms per frame, and the motor runs blind at its
        # last output meanwhile — that alone made moves overshoot into the
        # end stops and hunt. The bar catches up once the move settles.
        # Crease pulses too: a stall mid-pulse stretches it, and the bump's
        # strength is its length.
        moving = any(
            fader is not None and (fader.state == "MOVING" or fader.nudging)
            for fader in faders
        )
        if not moving and ticks_diff(now, last_display_poll) >= DISPLAY_POLL_MS:
            last_display_poll = now
            for idx, screen in enumerate(screens):
                if screen is None:
                    continue
                screen.set_position(positions[idx])
                if faders[idx] is not None:
                    screen.set_restore(faders[idx].unmute_position)
                # One frame per pass at most, so a four-display redraw can
                # never stall the PID loop for ~32 ms at once.
                if screen.draw():
                    break

        # Non-blocking serial read. Drain everything waiting, not one char
        # per loop: a slow loop (display redraw) at one char each turned a
        # burst of DISP: lines into seconds of lag for every later command.
        # The cap only bounds a pathological flood.
        for _ in range(INPUT_CHARS_PER_LOOP):
            if not select.select([sys.stdin], [], [], 0)[0]:
                break
            char = sys.stdin.read(1)
            if char == '\n':
                _handle_command(input_buf, faders, screens)
                input_buf = ""
            else:
                input_buf += char
                if len(input_buf) > INPUT_LINE_MAX:
                    input_buf = ""  # guard against buffer overflow


main()
