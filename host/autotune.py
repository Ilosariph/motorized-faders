#!/usr/bin/env python3
"""
Motorized Fader Controller — automatic PID tuning

Drives one fader through a fixed sequence of test moves for each candidate gain
set, scores how each move went, and searches for the gains with the lowest
score. Gains are applied live with TUNE:, so nothing is reflashed until the end.

A move is scored from the Pico's own telemetry (POS:, STATE:, DBG:stall):

  arrival     ms from SET: until the fader last entered SETTLING
  overshoot   % travelled past the target
  reversals   direction changes on the way — the hunting you see by eye
  final error % left between target and where the fader settled
  stalled     the firmware abandoned the move (or it never finished)

The search is coordinate descent: nudge one gain up and down, keep whichever
scores best, move on to the next gain, and halve the step size once a full pass
finds nothing better. Slow but robust against a noisy, nonlinear plant.

Usage:
    python host/autotune.py                 # tune fader 1, print the result
    python host/autotune.py --fader 3
    python host/autotune.py --apply         # also write gains into pico/main.py
    python host/autotune.py --start 1.0,0,0,40

Safety: only the chosen fader moves. Keep a hand near the power — the firmware
stall guard abandons a jammed move after ~300 ms of hard drive, and a gain set
that stalls is scored out of contention.

Dependencies:
    pip install pyserial
"""

import argparse
import pathlib
import re
import sys
import time

BAUD = 115200
PICO_VID = 0x2E8A

# Matches the firmware's SETTLE_MS: a fader reports IDLE this long after it
# last arrived, so the wait for IDLE has to allow for it.
SETTLE_MS = 1000
MOVE_TIMEOUT_MS = 6000

# Unscored positioning move, so every candidate starts from the same place.
HOME = 50.0

# (target %) in order, starting from HOME and ending back there so consecutive
# evaluations are comparable. Covers: big moves both ways, short moves where
# the minimum-power floor dominates, and a breakaway from the bottom end stop.
SEQUENCE = (80.0, 20.0, 25.0, 22.0, 0.0, 15.0, 50.0)

# Cost weights. Units: one point ~ one millisecond of arrival time.
W_OVERSHOOT = 40.0      # per % past the target
W_REVERSAL = 150.0      # per direction change
W_FINAL_ERROR = 100.0   # per % left at rest
STALL_COST = 3000.0     # abandoned or unfinished move

# Reversals are counted with hysteresis so ADC flicker is not hunting.
REVERSAL_HYSTERESIS = 0.4

# Search space: name -> (TUNE: slot, min, max, initial step)
PARAMS = {
    "kp": (0, 0.3, 8.0, 0.5),
    "kd": (2, 0.0, 0.3, 0.02),
    "minmove": (3, 20.0, 70.0, 5.0),
    "ki": (1, 0.0, 3.0, 0.3),
}
SEARCH_ORDER = ("minmove", "kp", "kd", "ki")
DEFAULT_START = {"kp": 1.0, "ki": 0.0, "kd": 0.0, "minmove": 40.0}

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "pico" / "main.py"
CONSTANT_NAMES = {"kp": "KP", "ki": "KI", "kd": "KD", "minmove": "MIN_MOVE_PCT"}


# ---------------------------------------------------------------------------
# Link: the serial line, abstracted so the tests can swap in a simulator
# ---------------------------------------------------------------------------

class SerialLink:
    def __init__(self, port=None):
        import serial
        import serial.tools.list_ports

        if port is None:
            for info in serial.tools.list_ports.comports():
                if info.vid == PICO_VID:
                    port = info.device
                    break
        if port is None:
            raise SystemExit("Pico not found; pass --port /dev/ttyACM0")
        self.ser = serial.Serial(port, BAUD, timeout=0.02)
        # The Pico's USB CDC drops what arrives right after the port opens,
        # so the first TUNE: went nowhere. Give it a moment first.
        time.sleep(0.5)
        self.ser.reset_input_buffer()
        self._buf = ""

    def now_ms(self):
        return time.monotonic() * 1000.0

    def send(self, line):
        self.ser.write((line + "\n").encode("ascii"))

    def poll(self):
        """Return the complete lines received so far (may be empty)."""
        data = self.ser.read(256)
        if data:
            self._buf += data.decode("ascii", errors="ignore")
        lines = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# Measuring one move
# ---------------------------------------------------------------------------

def set_line(fader_idx, target):
    """SET: with only this fader's slot filled — the others stay untouched."""
    slots = [""] * (fader_idx + 1)
    slots[fader_idx] = "{:.1f}".format(target)
    return "SET:" + ",".join(slots)


def run_move(link, fader_idx, target, log=None):
    """
    Command one move and collect telemetry until the fader is IDLE again.

    Returns a list of (t_ms since SET, kind, value) events, kind being "pos",
    "state" or "stall".
    """
    events = []
    link.send(set_line(fader_idx, target))
    start = link.now_ms()
    want_state = "STATE:{},".format(fader_idx + 1)
    want_stall = "DBG:stall f{},".format(fader_idx + 1)
    while True:
        elapsed = link.now_ms() - start
        if elapsed > MOVE_TIMEOUT_MS:
            events.append((elapsed, "timeout", None))
            return events
        for line in link.poll():
            t = link.now_ms() - start
            if log is not None:
                log(line)
            if line.startswith("POS:"):
                parts = line[4:].split(",")
                if fader_idx < len(parts):
                    try:
                        events.append((t, "pos", float(parts[fader_idx])))
                    except ValueError:
                        pass
            elif line.startswith(want_stall):
                events.append((t, "stall", None))
            elif line.startswith(want_state):
                state = line[len(want_state):]
                events.append((t, "state", state))
                if state == "IDLE":
                    return events


def analyse_move(events, start_pos, target):
    """Turn one move's events into metrics. Pure — tested on recorded logs."""
    positions = [v for _, kind, v in events if kind == "pos"]
    stalled = any(kind in ("stall", "timeout") for _, kind, _ in events)

    arrival = None
    for t, kind, value in events:
        if kind == "state" and value == "SETTLING":
            arrival = t  # last entry wins: re-entries mean it left again

    direction = 1.0 if target >= start_pos else -1.0
    overshoot = 0.0
    for pos in positions:
        overshoot = max(overshoot, (pos - target) * direction)

    reversals = 0
    trend = 0
    anchor = start_pos
    for pos in positions:
        delta = pos - anchor
        if abs(delta) < REVERSAL_HYSTERESIS:
            continue
        step = 1 if delta > 0 else -1
        if trend and step != trend:
            reversals += 1
        trend = step
        anchor = pos

    final = positions[-1] if positions else start_pos
    return {
        "arrival_ms": arrival,
        "overshoot": overshoot,
        "reversals": reversals,
        "final_error": abs(final - target),
        "final": final,
        "stalled": stalled or arrival is None,
    }


def move_cost(metrics):
    if metrics["stalled"]:
        return STALL_COST
    return (
        metrics["arrival_ms"]
        + W_OVERSHOOT * metrics["overshoot"]
        + W_REVERSAL * metrics["reversals"]
        + W_FINAL_ERROR * metrics["final_error"]
    )


# ---------------------------------------------------------------------------
# Evaluating one gain set
# ---------------------------------------------------------------------------

def tune_line(params):
    return "TUNE:{kp:.3f},{ki:.3f},{kd:.4f},{minmove:.1f}".format(**params)


def apply_params(link, params, timeout_ms=1000, attempts=3):
    """Send TUNE: and wait for the firmware to confirm it, retrying."""
    for _ in range(attempts):
        link.send(tune_line(params))
        start = link.now_ms()
        while link.now_ms() - start < timeout_ms:
            for line in link.poll():
                if line.startswith("DBG:tune"):
                    return line
                if line.startswith("DBG:parse err"):
                    raise RuntimeError("firmware rejected " + tune_line(params))
    raise RuntimeError("no DBG:tune reply after {} tries — is the firmware "
                       "current and nothing else holding the port?"
                       .format(attempts))


def evaluate(link, fader_idx, params, report=print):
    """Run the whole sequence with `params`; return (total cost, per-move)."""
    apply_params(link, params)
    # Positioning move, unscored, so each candidate starts at HOME. Its final
    # position is the real starting point of the first scored move.
    home = analyse_move(run_move(link, fader_idx, HOME), HOME, HOME)
    pos = home["final"]

    total = 0.0
    moves = []
    stalls = 0
    for target in SEQUENCE:
        metrics = analyse_move(run_move(link, fader_idx, target), pos, target)
        cost = move_cost(metrics)
        moves.append((pos, target, metrics, cost))
        total += cost
        pos = metrics["final"]
        if metrics["stalled"]:
            stalls += 1
            if stalls >= 2:
                # Hopeless and hard on the motor — stop early, worst score.
                report("    two stalls, abandoning this candidate")
                return float("inf"), moves
    return total, moves


def mean_score(evaluate_once, repeats):
    """
    Average `repeats` runs of one candidate. A single run is noisy enough on
    the real fader that the search keeps a lucky outlier — one bench run scored
    1247, then 3724 on its confirmation run with identical gains. A stalled run
    (inf) poisons the mean on purpose: a gain set that sometimes stalls is out.
    """
    return sum(evaluate_once() for _ in range(repeats)) / repeats


def describe(params):
    return "kp={kp:.2f} ki={ki:.2f} kd={kd:.3f} minmove={minmove:.0f}".format(
        **params)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(score, start, max_evals=40, report=print, step_scale=1.0):
    """
    Coordinate descent over PARAMS. `score(params)` returns a cost; lower is
    better. `step_scale` shrinks the initial steps to refine around a known
    good start. Returns (best params, best cost, evaluations used).
    """
    best = dict(start)
    best_cost = score(best)
    evals = 1
    report("start  {}  cost {:.0f}".format(describe(best), best_cost))
    initial = {name: PARAMS[name][3] * step_scale for name in PARAMS}
    steps = dict(initial)

    while evals < max_evals:
        improved = False
        for name in SEARCH_ORDER:
            _, low, high, _ = PARAMS[name]
            for sign in (1, -1):
                if evals >= max_evals:
                    break
                candidate = dict(best)
                candidate[name] = round(
                    min(high, max(low, best[name] + sign * steps[name])), 4)
                if candidate[name] == best[name]:
                    continue
                cost = score(candidate)
                evals += 1
                mark = ""
                if cost < best_cost:
                    best, best_cost, improved = candidate, cost, True
                    mark = "  <- best"
                report("try    {}  cost {:.0f}{}".format(
                    describe(candidate), cost, mark))
                if mark:
                    break  # don't also try the other direction
        if not improved:
            if all(steps[n] <= initial[n] / 8 for n in PARAMS):
                break  # converged
            for name in steps:
                steps[name] /= 2.0
            report("       no improvement, halving steps")
    return best, best_cost, evals


# ---------------------------------------------------------------------------
# Writing the result back
# ---------------------------------------------------------------------------

def apply_to_source(params, path=MAIN_PY):
    """Rewrite the KP/KI/KD/MIN_MOVE_PCT constants in pico/main.py."""
    source = path.read_text()
    for name, const in CONSTANT_NAMES.items():
        pattern = re.compile(r"^({} = )[0-9.]+".format(const), re.MULTILINE)
        value = repr(float(round(params[name], 4)))
        source, count = pattern.subn(lambda m: m.group(1) + value, source)
        if count != 1:
            raise RuntimeError("could not find {} in {}".format(const, path))
    path.write_text(source)


def parse_start(text):
    values = [float(v) for v in text.split(",")]
    if len(values) != 4:
        raise argparse.ArgumentTypeError("expected kp,ki,kd,minmove")
    return dict(zip(("kp", "ki", "kd", "minmove"), values))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="serial port, default: autodetect")
    parser.add_argument("--fader", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--start", type=parse_start, default=DEFAULT_START,
                        help="kp,ki,kd,minmove to start from")
    parser.add_argument("--evals", type=int, default=40,
                        help="max gain sets to try (~15 s per run)")
    parser.add_argument("--repeats", type=int, default=1,
                        help="runs averaged per candidate; 2-3 beats noise")
    parser.add_argument("--step", type=float, default=1.0,
                        help="initial step scale; 0.5 or less to refine")
    parser.add_argument("--apply", action="store_true",
                        help="write the result into pico/main.py")
    args = parser.parse_args(argv)

    fader_idx = args.fader - 1
    link = SerialLink(args.port)
    try:
        print("tuning fader {} — {} moves x {} run(s) per candidate, up to {} "
              "candidates".format(args.fader, len(SEQUENCE), args.repeats,
                                  args.evals))

        def score(params):
            return mean_score(
                lambda: evaluate(link, fader_idx, params)[0], args.repeats)

        best, best_cost, evals = search(score, args.start, args.evals,
                                        step_scale=args.step)

        # Confirmation run: the best score may have been a lucky one.
        confirm, moves = evaluate(link, fader_idx, best)
        print("\nbest after {} candidates: {}".format(evals, describe(best)))
        print("cost {:.0f} (confirmation run {:.0f})".format(best_cost, confirm))
        for start, target, m, cost in moves:
            arrival = ("stall" if m["stalled"]
                       else "{:.0f} ms".format(m["arrival_ms"]))
            print("  {:5.1f} -> {:5.1f}  {:>8}  overshoot {:4.1f}%  "
                  "reversals {}  error {:.1f}%".format(
                      start, target, arrival, m["overshoot"], m["reversals"],
                      m["final_error"]))
        print("\nlive:     " + tune_line(best))
        if args.apply:
            apply_to_source(best)
            print("written to {} — upload to make it stick".format(MAIN_PY))
        else:
            print("rerun with --apply to write these into pico/main.py")
    except KeyboardInterrupt:
        print("\ninterrupted — gains on the Pico are whatever was tried last;"
              " reset it to restore the defaults")
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
