"""
Autotune tests — move analysis on real bench logs, the search, and a full
tuning run against the firmware driving simulated faders.
Run with: python3 tests/test_autotune.py
"""

import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "host"))

import stubs  # noqa: E402
import test_firmware  # noqa: E402  (Rig: firmware + simulated faders)
import autotune  # noqa: E402

FAILURES = []
PASSES = [0]


def check(condition, label):
    if condition:
        PASSES[0] += 1
    else:
        FAILURES.append(label)
        print("  FAIL: " + label)


def events_from_log(lines, interval_ms=50):
    """Bench log lines -> run_move-style events, one line per interval."""
    events = []
    for i, line in enumerate(lines):
        t = i * interval_ms
        if line.startswith("POS:"):
            events.append((t, "pos", float(line[4:].split(",")[0])))
        elif line.startswith("STATE:1,"):
            events.append((t, "state", line[len("STATE:1,"):]))
        elif line.startswith("DBG:stall f1,"):
            events.append((t, "stall", None))
    return events


# 20.9 -> 80 on the bench with kp=1, minmove=40, coasting stop: swung through
# 99 / 57 / 82 / 48 / 73 before settling.
SWINGING_UP = [
    "STATE:1,MOVING", "POS:67.1", "POS:99.2", "POS:56.7", "POS:82.3",
    "POS:47.9", "POS:73.4", "STATE:1,SETTLING", "POS:80.7", "POS:80.7",
    "STATE:1,IDLE",
]

# 95.8 -> 20 with the short brake: one clean move, settled 0.9% short.
CLEAN_DOWN = [
    "STATE:1,MOVING", "POS:18.2", "STATE:1,SETTLING", "POS:20.9",
    "POS:20.9", "STATE:1,IDLE",
]

# Parked at the bottom stop, 40% could not break stiction: no movement, then
# the firmware gave up.
STUCK = ["STATE:1,MOVING"] + ["POS:0.0"] * 10 + [
    "DBG:stall f1, move abandoned", "STATE:1,IDLE"]


def test_analysis_on_bench_logs():
    print("Analysis")
    swing = autotune.analyse_move(events_from_log(SWINGING_UP), 20.9, 80.0)
    check(abs(swing["overshoot"] - 19.2) < 0.01,
          f"overshoot measured past the target (got {swing['overshoot']:.1f})")
    check(swing["reversals"] >= 4,
          f"hunting counted as reversals (got {swing['reversals']})")
    check(not swing["stalled"], "swinging move still arrived")

    clean = autotune.analyse_move(events_from_log(CLEAN_DOWN), 95.8, 20.0)
    check(clean["reversals"] <= 1, "clean move has at most the arrival bounce")
    check(abs(clean["final_error"] - 0.9) < 0.01, "final error from last POS")
    check(abs(clean["overshoot"] - 1.8) < 0.01,
          "overshoot is direction-aware on a downward move")

    stuck = autotune.analyse_move(events_from_log(STUCK), 0.0, 20.0)
    check(stuck["stalled"], "abandoned move reads as stalled")
    check(autotune.move_cost(stuck) == autotune.STALL_COST,
          "stalled move costs STALL_COST")

    check(autotune.move_cost(clean) < autotune.move_cost(swing),
          "clean move scores better than the swinging one")


def test_set_line_touches_only_one_fader():
    check(autotune.set_line(0, 80) == "SET:80.0", "fader 1 slot")
    check(autotune.set_line(2, 15) == "SET:,,15.0", "fader 3 leaves 1-2 empty")


def test_search_finds_minimum():
    print("Search")
    optimum = {"kp": 2.6, "ki": 0.4, "kd": 0.07, "minmove": 33.0}
    scale = {"kp": 1.0, "ki": 1.0, "kd": 0.05, "minmove": 10.0}

    def score(params):
        return sum(((params[n] - optimum[n]) / scale[n]) ** 2 for n in optimum)

    best, cost, evals = autotune.search(
        score, autotune.DEFAULT_START, max_evals=200, report=lambda *_: None)
    check(cost < 0.1, f"coordinate descent reaches the optimum (cost {cost:.3f})")
    for name, (_, low, high, _) in autotune.PARAMS.items():
        check(low <= best[name] <= high, f"{name} stays within bounds")

    fine, _, _ = autotune.search(
        score, optimum, max_evals=3, report=lambda *_: None, step_scale=0.25)
    check(abs(fine["kp"] - optimum["kp"]) <= autotune.PARAMS["kp"][3] * 0.25,
          "step_scale shrinks the first steps")

    runs = iter([1000.0, 3000.0])
    check(autotune.mean_score(lambda: next(runs), 2) == 2000.0,
          "repeats are averaged")
    runs = iter([1000.0, float("inf")])
    check(autotune.mean_score(lambda: next(runs), 2) == float("inf"),
          "one stalled run rules the candidate out")

    evals_seen = []
    autotune.search(lambda p: evals_seen.append(1) or score(p),
                    autotune.DEFAULT_START, max_evals=7,
                    report=lambda *_: None)
    check(len(evals_seen) <= 7, "evaluation budget is respected")


def test_apply_to_source():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "main.py"
    shutil.copy(autotune.MAIN_PY, tmp)
    autotune.apply_to_source(
        {"kp": 2.5, "ki": 0.25, "kd": 0.04, "minmove": 38.0}, tmp)
    text = tmp.read_text()
    for line in ("KP = 2.5", "KI = 0.25", "KD = 0.04", "MIN_MOVE_PCT = 38.0"):
        check(("\n" + line + " ") in text or ("\n" + line + "\n") in text,
              f"main.py rewritten: {line}")
    check("# Proportional gain" in text, "constant comments preserved")


class SimLink:
    """The firmware's main loop in miniature, behind autotune's link API."""

    def __init__(self, fader_idx=0):
        self.rig = test_firmware.Rig()
        self.rig.calibrate_all()
        self.fw = self.rig.fw
        self.idx = fader_idx
        self.out = []
        self.fw._emit = self.out.append
        self.last_pos = 0
        self.sent = []

    def now_ms(self):
        return stubs.now_ms()

    def send(self, line):
        self.sent.append(line)
        self.fw._handle_command(line, self.rig.faders, self.rig.screens)

    def poll(self):
        for _ in range(2):
            positions = self.rig.tick(5)
            for idx, fader in enumerate(self.rig.faders):
                if fader.stalled:
                    fader.stalled = False
                    self.out.append(f"DBG:stall f{idx + 1}, move abandoned")
                state = fader.state_changed()
                if state is not None:
                    self.out.append(f"STATE:{idx + 1},{state}")
            if stubs.now_ms() - self.last_pos >= self.fw.POS_INTERVAL_MS:
                self.last_pos = stubs.now_ms()
                self.out.append(
                    "POS:" + ",".join(f"{p:.1f}" for p in positions))
        lines, self.out[:] = list(self.out), []
        return lines


def test_end_to_end_against_firmware():
    print("End to end")
    link = SimLink()
    cost, moves = autotune.evaluate(link, 0, autotune.DEFAULT_START,
                                    report=lambda *_: None)
    check(cost != float("inf"), "a full sequence completes")
    check(len(moves) == len(autotune.SEQUENCE), "every move scored")
    check(not any(m["stalled"] for _, _, m, _ in moves), "no move stalls")
    for start, target, m, _ in moves:
        check(m["final_error"] <= link.fw.DEADBAND_EXIT,
              f"{start:.0f} -> {target:.0f} settles near target "
              f"(error {m['final_error']:.1f})")
    check(all(s.startswith(("TUNE:", "SET:")) for s in link.sent),
          "only TUNE: and SET: are sent")
    check(all(s == "SET:" + s[4:].split(",")[0] for s in link.sent
              if s.startswith("SET:")), "SET: lines fill fader 1's slot only")
    check(link.fw.KP == autotune.DEFAULT_START["kp"],
          "TUNE: reached the firmware")

    def score(params):
        return autotune.evaluate(link, 0, params, report=lambda *_: None)[0]

    best, best_cost, _ = autotune.search(
        score, autotune.DEFAULT_START, max_evals=6, report=lambda *_: None)
    check(best_cost <= cost, "search never returns worse than its start")


def main():
    tests = [
        test_analysis_on_bench_logs,
        test_set_line_touches_only_one_fader,
        test_search_finds_minimum,
        test_apply_to_source,
        test_end_to_end_against_firmware,
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
