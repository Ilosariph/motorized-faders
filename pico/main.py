"""
Motorized Fader Controller — MicroPython firmware for Raspberry Pi Pico W
Fader: Alps RS60N11M9 series (Motor N Fader, 10V DC rated motor)
Driver: SparkFun TB6612FNG dual motor driver

Wiring (RP2040 + RTL8720 board — GP4/GP5 not exposed):
  Fader 1: wiper→GP26(ADC0), 3.3V→pin3, GND→pin1
           motor→AO1/AO2
  Fader 2: wiper→GP27(ADC1), 3.3V→pin3, GND→pin1
           motor→BO1/BO2
  Driver:  AIN1→GP2, AIN2→GP3, PWMA→GP6
           BIN1→GP7, BIN2→GP8, PWMB→GP9
           STBY→GP10, VCC→3.3V, VM→10V motor supply, GND→common

Serial protocol (115200 baud, USB):
  Pico→Host (~20Hz): POS:47.3,82.1\n
                     RAW:31245,47102\n   (raw ADC, 0–65535, for diagnostics)
  Host→Pico:         SET:50.0,75.0\n
  An empty SET: slot means "leave this fader unchanged":
                     SET:50.0,\n     (moves fader 1 only)
                     SET:,75.0\n     (moves fader 2 only)

"""

import sys
import select
from machine import ADC, Pin, PWM
from utime import ticks_ms, ticks_diff

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
MIN_MOVE_PCT = 30.0

# State machine: motor only runs on demand. After SET:, drive to target,
# hold for SETTLE_MS within deadband, then release. While idle, report
# position only when it changes by REPORT_DELTA (user moved the fader).
SETTLE_MS = 1000
REPORT_DELTA = 0.2

# ---------------------------------------------------------------------------
# Motor / hardware constants
# ---------------------------------------------------------------------------
PWM_FREQ = 20000  # Hz — 20kHz is above hearing range, no motor whine
# Minimum PWM duty below which motor stalls rather than moving slowly.
# Below this threshold the output is treated as zero (coast).
PWM_MIN = 10000
PWM_MAX = 65535

# ---------------------------------------------------------------------------
# Hardware configuration
# ---------------------------------------------------------------------------
FADER1_ENABLED = True   # set to False if fader 1 is not connected
FADER2_ENABLED = True   # set to False if fader 2 is not connected

# ---------------------------------------------------------------------------
# Serial / timing
# ---------------------------------------------------------------------------
POS_INTERVAL_MS = 50  # send position every 50ms (~20Hz)
MAX_DT_S = 0.1        # cap dt to prevent derivative spike on first PID call


class MotorDriver:
    def __init__(self, in1, in2, pwm_pin, stby_pin):
        self.in1 = Pin(in1, Pin.OUT)
        self.in2 = Pin(in2, Pin.OUT)
        self.pwm = PWM(Pin(pwm_pin))
        self.pwm.freq(PWM_FREQ)
        # STBY is shared between both motors; set HIGH to enable driver
        self.stby = Pin(stby_pin, Pin.OUT)
        self.stby.value(1)

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
    def __init__(self, adc_pin, motor):
        self.adc = ADC(adc_pin)
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

    def _raw_adc(self):
        """Average 16 ADC samples to reduce noise (kills sub-% jitter)."""
        return sum(self.adc.read_u16() for _ in range(16)) // 16

    def read_raw(self):
        """Return raw ADC value (0–65535), uncalibrated."""
        return self._raw_adc()

    def read_position(self):
        """Return fader position as 0.0–100.0%, 0 = bottom of travel."""
        raw = self._raw_adc()
        raw = max(self.adc_min, min(self.adc_max, raw))
        span = self.adc_max - self.adc_min
        if span <= 0:
            # Calibration failed (collapsed range) — report 0 rather than
            # dividing by zero and killing the control loop.
            return 0.0
        pct = (raw - self.adc_min) / span * 100.0
        return 100.0 - pct if self.inverted else pct

    def calibrate(self, motor_power=60, settle_ms=400, sweep_steps=15):
        """
        Auto-calibrate ADC range by driving fader to both mechanical limits.
        Mirrors the initFaders() approach from the original Arduino sketch.

        Wiper orientation is detected rather than assumed: whichever limit
        reads lower becomes adc_min, and self.inverted records whether the
        ADC counts down as the fader travels up.
        """
        # Drive to one mechanical limit, then the other. Which limit reads
        # higher on the ADC depends on how the wiper is wired, so don't
        # assume: record what each end actually reads and sort afterwards.
        def _sweep(power):
            self.motor.drive(power)
            _delay(settle_ms)
            end = self._raw_adc()
            for _ in range(sweep_steps):
                self.motor.drive(power)
                _delay(30)
                end = self._raw_adc()
            self.motor.stop()
            _delay(100)
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

    def update(self):
        """
        Run one state-machine iteration. Returns current position (0–100%).
        IDLE: motor off, no PID. MOVING: PID drives to setpoint. SETTLING:
        PID still holds, releases motor after SETTLE_MS of stillness.
        """
        now = ticks_ms()
        dt = min(ticks_diff(now, self.last_time) / 1000.0, MAX_DT_S)
        self.last_time = now

        position = self.read_position()

        if self.state == "IDLE":
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
        """Set new target and arm the motor (state → MOVING)."""
        self.setpoint = max(0.0, min(100.0, setpoint))
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = ticks_ms()
        self.in_deadband = False
        self.state = "MOVING"


def _delay(ms):
    """Blocking delay in milliseconds."""
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < ms:
        pass


def _handle_command(line, fader1, fader2):
    line = line.strip()
    if not line:
        return
    # Only SET: is an input command. Ignore everything else — notably our own
    # output lines (CAL:, POS:, ...), which can arrive back on stdin as a USB
    # CDC loopback and would otherwise be parsed as commands.
    if not line.startswith("SET:"):
        return
    sys.stdout.write("DBG:got '{}'\n".format(line))
    if line.startswith("SET:"):
        try:
            parts = line[4:].split(",")
            # An empty slot means "leave this fader alone" — the host only
            # fills slots something actually asked to move. Engaging every
            # slot on every SET: would re-command a fader the user is
            # currently holding, or drive it back to a stale setpoint.
            if FADER1_ENABLED and len(parts) > 0 and parts[0].strip():
                fader1.engage(float(parts[0]))
                sys.stdout.write("DBG:sp1={}\n".format(fader1.setpoint))
            if FADER2_ENABLED and len(parts) > 1 and parts[1].strip():
                fader2.engage(float(parts[1]))
        except (ValueError, IndexError) as e:
            sys.stdout.write("DBG:parse err {}\n".format(e))


def main():
    # WiFi-disable code removed — this board uses RTL8720 (not CYW43) and
    # importing `network` prints '[CYW43] Failed to ...' messages to stdout,
    # corrupting the SET: command stream. If you port back to a real Pico W,
    # re-add: wlan = network.WLAN(network.STA_IF); wlan.active(False)

    # Motor drivers (both share STBY on GP10)
    motor_a = MotorDriver(in1=2, in2=3, pwm_pin=6, stby_pin=10) if FADER1_ENABLED else None
    motor_b = MotorDriver(in1=7, in2=8, pwm_pin=9, stby_pin=10) if FADER2_ENABLED else None

    fader1 = FaderPID(adc_pin=26, motor=motor_a) if FADER1_ENABLED else None
    fader2 = FaderPID(adc_pin=27, motor=motor_b) if FADER2_ENABLED else None

    # Calibrate enabled faders — faders sweep to limits then hold position
    sys.stdout.write("CAL:start\n")
    if fader1:
        fader1.calibrate()
    if fader2:
        fader2.calibrate()
    sys.stdout.write("CAL:done\n")

    last_pos_send = ticks_ms()
    input_buf = ""

    while True:
        pos1 = fader1.update() if fader1 else 0.0
        pos2 = fader2.update() if fader2 else 0.0

        # Emit STATE: edges so the host can detect "move complete" without
        # heuristics. Additive — old hosts ignore unknown line prefixes.
        if fader1:
            s = fader1.state_changed()
            if s is not None:
                sys.stdout.write("STATE:1,{}\n".format(s))
        if fader2:
            s = fader2.state_changed()
            if s is not None:
                sys.stdout.write("STATE:2,{}\n".format(s))

        # Telemetry: 20Hz heartbeat while engaged (MOVING/SETTLING),
        # on-change-only while idle (user moved fader by hand).
        engaged = (
            (fader1 and fader1.state != "IDLE")
            or (fader2 and fader2.state != "IDLE")
        )
        now = ticks_ms()
        if ticks_diff(now, last_pos_send) >= POS_INTERVAL_MS:
            send = False
            if engaged:
                send = True
            else:
                delta1 = fader1 and abs(pos1 - fader1.last_reported_pos) >= REPORT_DELTA
                delta2 = fader2 and abs(pos2 - fader2.last_reported_pos) >= REPORT_DELTA
                if delta1 or delta2:
                    send = True
            if send:
                sys.stdout.write("POS:{:.1f},{:.1f}\n".format(pos1, pos2))
                if engaged:
                    raw1 = fader1.read_raw() if fader1 else 0
                    raw2 = fader2.read_raw() if fader2 else 0
                    sys.stdout.write("RAW:{},{}\n".format(raw1, raw2))
                if fader1:
                    fader1.last_reported_pos = pos1
                if fader2:
                    fader2.last_reported_pos = pos2
                last_pos_send = now

        # Non-blocking serial read
        if select.select([sys.stdin], [], [], 0)[0]:
            char = sys.stdin.read(1)
            if char == '\n':
                _handle_command(input_buf, fader1, fader2)
                input_buf = ""
            else:
                input_buf += char
                if len(input_buf) > 64:
                    input_buf = ""  # guard against buffer overflow


main()
