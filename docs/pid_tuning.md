# PID Tuning Guide — Motorized Faders

This guide explains how to tune the PID controller in `pico/main.py` for the Alps RS60N11M9 motorized fader.

---

## What the Constants Do

All tuning constants live at the top of `main.py`. You do not need to reflash — edit the values and save to the Pico.

### Kp — Proportional Gain

The motor power is directly proportional to the distance from the target:

```
motor_power = Kp × (target - current_position)
```

- **Too low**: Fader moves sluggishly, barely reaches the target.
- **Too high**: Fader oscillates back and forth around the target.
- **Just right**: Fader moves briskly and settles with little or no overshoot.

Start here. Kp is the most important constant.

### Ki — Integral Gain

Accumulates error over time, correcting a fader that consistently stops short of the target:

```
integral_term = Ki × (sum of error × time)
```

- **Too low**: Fader stops a percent or two short of the target and stays there.
- **Too high**: Fader oscillates slowly, with a growing wobble that doesn't damp out.
- **Just right**: Fader reaches exactly the target position after settling.

Only add Ki *after* Kp and Kd are set. Most faders don't need much — start at 0.01.

### Kd — Derivative Gain

Reacts to how fast the error is changing, damping the motor as the fader approaches target:

```
derivative_term = Kd × (error_change / time_elapsed)
```

- **Too low**: Fader overshoots and takes several oscillations to settle.
- **Too high**: Fader vibrates rapidly (reacting to ADC noise), feels jittery.
- **Just right**: Fader glides smoothly into position with minimal overshoot.

Note: Kd is sensitive to ADC noise. If you increase it and see vibration at rest, reduce it.

---

## DEADBAND

```python
DEADBAND = 1.5  # percent
```

The motor is stopped and the integral resets whenever the fader is within `DEADBAND`% of the target. This prevents the motor from hunting back and forth due to friction and mechanical backlash.

- **Too small**: Fader buzzes at rest trying to correct tiny errors.
- **Too large**: Fader stops noticeably before the target position.

For most motorized faders, 1–2% works well.

---

## INTEGRAL_MAX (Anti-Windup)

```python
INTEGRAL_MAX = 50.0
```

Clamps the integral term to prevent runaway accumulation when the fader is held back (e.g., user gripping it). Without this limit, the integral builds up while blocked, then dumps into the motor violently when released.

If you see a sudden surge when releasing the fader after holding it, reduce this value.

---

## Step-by-Step Tuning Procedure

This manual procedure is more reliable than Ziegler-Nichols for motorized faders, because faders respond fast and mechanically — Z-N tends to produce overly aggressive integral values.

### Step 1: Disable Ki and Kd

```python
KP = 0.3
KI = 0.0
KD = 0.0
```

Start conservative.

### Step 2: Tune Kp

Run `host.py` and send a setpoint: `0 0` then `100 100`. Watch how the fader moves.

- Increase Kp in steps of 0.1 until the fader moves briskly to the target.
- If it starts oscillating (bouncing back and forth), reduce Kp by 20%.
- The Kp where oscillation just starts is your "critical gain". Use 60–70% of that value.

Typical working range: **0.4 – 1.2**

### Step 3: Add Kd

With Kp set, add a small Kd to reduce overshoot:

```python
KD = 0.01
```

Increase in steps of 0.01 until overshoot is gone. Stop before the fader starts vibrating.

Typical working range: **0.01 – 0.05**

### Step 4: Add Ki (only if needed)

If the fader consistently stops short of the target (steady-state error), add a small Ki:

```python
KI = 0.01
```

Increase slowly. If the fader starts oscillating again (slowly), reduce Ki.

Typical working range: **0.0 – 0.1**

### Step 5: Check DEADBAND

With PID tuned, verify the deadband feels right:
- Fader should hold position without buzzing.
- Fader should reach the exact target (within 1–2%) before stopping.

Adjust `DEADBAND` in 0.5% increments.

---

## Symptom / Fix Table

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Fader moves sluggishly | Kp too low | Increase Kp by 0.1 |
| Fader oscillates around target | Kp too high | Reduce Kp by 20% |
| Fader overshoots then oscillates | Kd too low | Increase Kd by 0.01 |
| Fader vibrates rapidly at rest | Kd too high or ADC noise | Reduce Kd; ensure WiFi is disabled on Pico |
| Fader stops just short of target | Ki too low or DEADBAND too large | Increase Ki by 0.01 or reduce DEADBAND |
| Fader oscillates slowly (long period) | Ki too high | Reduce Ki by 0.01 |
| Fader surges after touch release | Integral windup | Reduce INTEGRAL_MAX |
| Fader doesn't move at all | PWM_MIN too high or STBY low | Check wiring; reduce PWM_MIN |
| Fader moves in wrong direction | Motor wires swapped | Swap AO1/AO2 connections |

---

## Notes on the Alps RS60N11M9

- The fader has some mechanical friction and a small amount of backlash. This means a deadband of at least 1% is always needed.
- The motor responds quickly — Kd matters more than Ki for this fader.
- ADC noise: on a genuine Pico W the ADC is affected by the CYW43 WiFi radio, so the firmware disables WiFi at startup. On the RP2040+RTL8720 clone board this `network` call may silently no-op — if you see random position jitter, suspect supply noise or wiring before software.
- The `initFaders()` calibration sweep runs automatically on boot. If the fader hits a mechanical stop and stalls, the calibration still completes — the final ADC values will be accurate.
