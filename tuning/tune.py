#!/usr/bin/env python3
"""
Automated PID tuner for the motorized fader controller.

Runs on the host. Talks to the Pico over USB serial:
  - reads the current KP/KI/KD out of pico/main.py as a seed,
  - runs a suite of step responses for each candidate gain,
  - scores them with metrics.score_step (IAE + overshoot + settling),
  - searches with Twiddle (coordinate-ascent).

Usage:
    python tuning/tune.py                       # tune, print results
    python tuning/tune.py --dry-run             # just score current gains
    python tuning/tune.py --max-evals 60 --write   # patch pico/main.py
    python tuning/tune.py --port /dev/ttyACM0

Dependencies:
    pip install pyserial
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from serial_link import PicoLink  # noqa: E402
from metrics import score_step, aggregate  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(REPO_ROOT, "pico", "main.py")

# (start, target, weight) — weight emphasises large/full-travel steps
# because that's where instability shows up worst.
DEFAULT_STEPS = [
    (50.0, 45.0, 0.7),   # tiny down  (5%)  — deadband / stall
    (50.0, 55.0, 0.7),   # tiny up    (5%)
    (50.0, 30.0, 1.0),   # small down (20%)
    (50.0, 70.0, 1.0),   # small up   (20%)
    (20.0, 80.0, 1.2),   # medium up  (60%)
    (80.0, 20.0, 1.2),   # medium down(60%)
    (5.0,  95.0, 1.5),   # large up   (90%) — overshoot territory
    (95.0,  5.0, 1.5),   # large down (90%)
    (0.0,  90.0, 1.2),   # from bottom — asymmetry near end-stop
    (100.0, 10.0, 1.2),  # from top
]


def read_seed_gains(path=MAIN_PY):
    src = open(path).read()
    def grab(name, default):
        m = re.search(rf"^{name}\s*=\s*([0-9.+\-eE]+)", src, re.M)
        return float(m.group(1)) if m else default
    return grab("KP", 1.0), grab("KI", 0.0), grab("KD", 0.0)


def write_gains(kp, ki, kd, path=MAIN_PY):
    src = open(path).read()
    for name, val in (("KP", kp), ("KI", ki), ("KD", kd)):
        src = re.sub(
            rf"^({name}\s*=\s*)[0-9.+\-eE]+",
            lambda m, v=val: f"{m.group(1)}{v:.4f}",
            src,
            count=1,
            flags=re.M,
        )
    with open(path, "w") as f:
        f.write(src)


def run_step_suite(link, steps, settle_timeout=8.0, between_pause=0.25):
    results = []
    weights = []
    for start, target, w in steps:
        # Park at the start position first
        link.step(start, settle_timeout=settle_timeout)
        time.sleep(between_pause)
        trace = link.step(target, settle_timeout=settle_timeout)
        results.append(score_step(trace))
        weights.append(w)
        if trace.get("aborted"):
            # Bail out early — this candidate is hopeless and we don't
            # want to keep cooking the motor against a wall.
            break
        time.sleep(between_pause)
    cost, _ = aggregate(results, weights)
    return cost, results


def evaluate(link, gains, steps, verbose=False):
    kp, ki, kd = gains
    if min(gains) < 0:
        return float("inf"), []
    link.set_pid(kp, ki, kd)
    cost, results = run_step_suite(link, steps)
    if verbose:
        print(f"  Kp={kp:.4f} Ki={ki:.4f} Kd={kd:.4f}  cost={cost:.2f}")
        for (s, t, _), m in zip(steps, results):
            print(
                f"    {s:5.1f}->{t:5.1f}  IAE={m.iae:6.2f} "
                f"OS={m.overshoot_pct:5.1f}%  ts={m.settling_time:.2f}s  "
                f"ss={m.ss_error:.2f}  cost={m.cost:.2f}"
                + ("  [ABORT]" if m.aborted else "")
            )
    return cost, results


def twiddle(link, seed, steps, max_evals=50, tol=0.01, verbose=True):
    """
    Sebastian Thrun's twiddle / coordinate ascent.
    Returns best (gains, cost).
    """
    p = list(seed)
    # Initial step sizes — relative to the seed so we explore a similar
    # neighborhood regardless of magnitude. Tiny lower bound so a
    # zero-seed (e.g. Ki=0) still moves.
    dp = [max(0.05, abs(v) * 0.3) for v in p]

    evals = 0
    best_cost, _ = evaluate(link, p, steps, verbose=verbose)
    evals += 1
    if verbose:
        print(f"[seed] cost = {best_cost:.2f}")

    while sum(dp) > tol and evals < max_evals:
        for i in range(len(p)):
            if evals >= max_evals:
                break
            p[i] += dp[i]
            cost, _ = evaluate(link, p, steps, verbose=verbose)
            evals += 1
            if cost < best_cost:
                best_cost = cost
                dp[i] *= 1.1
                if verbose:
                    print(f"  + accept (Kp,Ki,Kd)={tuple(round(x,4) for x in p)}  best={best_cost:.2f}")
            else:
                p[i] -= 2 * dp[i]
                if p[i] < 0:
                    p[i] = 0.0
                if evals >= max_evals:
                    break
                cost, _ = evaluate(link, p, steps, verbose=verbose)
                evals += 1
                if cost < best_cost:
                    best_cost = cost
                    dp[i] *= 1.1
                    if verbose:
                        print(f"  - accept (Kp,Ki,Kd)={tuple(round(x,4) for x in p)}  best={best_cost:.2f}")
                else:
                    p[i] += dp[i]
                    dp[i] *= 0.6
        if verbose:
            print(f"[iter] dp={tuple(round(x,4) for x in dp)}  evals={evals}/{max_evals}")
    return tuple(p), best_cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (auto-detect by default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate current/seed gains and exit")
    ap.add_argument("--max-evals", type=int, default=50)
    ap.add_argument("--tol", type=float, default=0.02)
    ap.add_argument("--seed", nargs=3, type=float, metavar=("KP", "KI", "KD"),
                    help="override seed gains (default: read pico/main.py)")
    ap.add_argument("--write", action="store_true",
                    help="patch pico/main.py with the best gains found")
    ap.add_argument("--debug-serial", action="store_true")
    args = ap.parse_args()

    seed = tuple(args.seed) if args.seed else read_seed_gains()
    print(f"Seed gains: Kp={seed[0]:.4f}  Ki={seed[1]:.4f}  Kd={seed[2]:.4f}")

    link = PicoLink(port=args.port, debug=args.debug_serial)
    try:
        print("Waiting for Pico boot calibration...")
        link.wait_for_calibration()
        time.sleep(0.5)

        if args.dry_run:
            evaluate(link, seed, DEFAULT_STEPS, verbose=True)
            return

        best, best_cost = twiddle(
            link, seed, DEFAULT_STEPS,
            max_evals=args.max_evals, tol=args.tol,
        )
        print()
        print("=" * 60)
        print(f"Best:  Kp={best[0]:.4f}  Ki={best[1]:.4f}  Kd={best[2]:.4f}")
        print(f"Cost:  {best_cost:.2f}  (seed was first row above)")
        print("=" * 60)

        if args.write:
            write_gains(*best)
            print(f"Patched {MAIN_PY}")
        else:
            print("Re-run with --write to update pico/main.py in place,")
            print("or copy the gains above manually.")
    finally:
        link.close()


if __name__ == "__main__":
    main()
