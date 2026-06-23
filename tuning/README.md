# PID Tuning Automation

Closed-loop step-response tuner for the motorized faders. Runs on the
host, talks to the Pico over USB-serial, and uses Twiddle (coordinate
ascent) to find PID gains that minimize a cost function over a suite of
step responses.

## How it works

1. Reads the current `KP/KI/KD` out of `pico/main.py` as the seed.
2. Sends each candidate to the firmware via `PID:kp,ki,kd` (a new
   command added to the existing protocol — the firmware acks with
   `PID:OK,...`).
3. For each candidate, runs a default suite of steps covering tiny
   (5%), small (20%), medium (60%), and full-travel (~90%) moves in
   both directions, plus end-stop anchored moves.
4. Scores each trace: IAE + overshoot penalty + steady-state error +
   settling time. Aborted/unsettled trials get a large penalty so the
   optimizer doesn't chase unstable gains.
5. Twiddle adjusts each gain independently, expanding the step on
   improvements and shrinking on regressions, until convergence or the
   eval budget is hit.

## Prerequisites

- Firmware must be the version in this repo (accepts `PID:` command).
- `pip install pyserial`.

## Usage

```bash
# Score current gains without changing anything
python tuning/tune.py --dry-run

# Tune (default 50 evaluations, ~3–5 minutes)
python tuning/tune.py

# Tune and patch pico/main.py with the result
python tuning/tune.py --write

# Pick a port explicitly / start from custom seed
python tuning/tune.py --port /dev/ttyACM0 --seed 3.0 1.0 0.05
```

Each step takes ~1–2 s of motion plus settle/idle margin, and each
evaluation runs the full 10-step suite, so a 50-eval run is a couple of
minutes of fader motion. Don't sit on the fader during tuning.

## Files

- `serial_link.py` — open the Pico, push `PID:` gains, run `SET:`
  steps, record `POS:` traces until `STATE:n,IDLE`.
- `metrics.py` — score a single step trace (IAE / overshoot / settling
  / ss-error) into one scalar cost.
- `tune.py` — CLI entry point. Reads seed from `pico/main.py`, runs the
  step suite, and drives Twiddle.

## Tuning the tuner

If results feel off, the levers are in `tune.py` (`DEFAULT_STEPS`) and
`metrics.py` (`w_overshoot`, `w_ss`, `w_settle`). For example:
- Audible motor whine on aggressive Kp → bump `w_overshoot`.
- Tuner converges to a slow-but-clean response → lower `w_overshoot`.
- Steady-state error matters more than rise time → bump `w_ss`.
