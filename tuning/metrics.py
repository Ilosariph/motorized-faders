"""
Score a fader step-response trace.

Inputs are timestamped position samples emitted by the firmware
(`POS:` lines, ~20 Hz). The cost is dominated by integral absolute
error (IAE) with extra penalties for overshoot and steady-state error,
plus a hefty penalty for traces that never settle (so the optimizer
can't chase fast-but-unstable gains).
"""

from dataclasses import dataclass


@dataclass
class StepMetrics:
    iae: float
    rise_time: float       # seconds, 10% -> 90% of the commanded step
    overshoot_pct: float   # percent past target, of step magnitude
    settling_time: float   # seconds until |err| < settle_band and stays
    ss_error: float        # mean |err| over the last tail_s seconds
    cost: float
    aborted: bool


def score_step(trace, settle_band=1.0, tail_s=0.4,
               w_overshoot=2.0, w_ss=5.0, w_settle=0.5,
               abort_penalty=500.0):
    target = trace["target"]
    start = trace["start"]
    t = trace["t"]
    pos = trace["pos"]
    aborted = trace.get("aborted", False)

    if len(t) < 4:
        return StepMetrics(0, 0, 0, 0, 0, abort_penalty * 2, True)

    span = max(abs(target - start), 1e-3)
    direction = 1.0 if target >= start else -1.0

    # IAE — trapezoidal integration of |err| over time
    iae = 0.0
    for i in range(1, len(t)):
        dt = t[i] - t[i - 1]
        e0 = abs(target - pos[i - 1])
        e1 = abs(target - pos[i])
        iae += 0.5 * (e0 + e1) * dt

    # Rise time: first 10% -> 90% crossing
    p10 = start + 0.10 * (target - start)
    p90 = start + 0.90 * (target - start)
    t10 = t90 = None
    for i, p in enumerate(pos):
        crossed = (p >= p10) if direction > 0 else (p <= p10)
        if t10 is None and crossed:
            t10 = t[i]
        crossed90 = (p >= p90) if direction > 0 else (p <= p90)
        if t90 is None and crossed90:
            t90 = t[i]
            break
    rise = (t90 - t10) if (t10 is not None and t90 is not None) else (t[-1] - t[0])

    # Overshoot — max excursion past target, in % of step size
    if direction > 0:
        peak = max(pos)
        os_raw = max(0.0, peak - target)
    else:
        peak = min(pos)
        os_raw = max(0.0, target - peak)
    overshoot = os_raw / span * 100.0

    # Settling time — last index where |err| > settle_band, then the
    # *next* sample's timestamp is when we settled.
    settle_t = t[-1] - t[0]
    for i in range(len(pos) - 1, -1, -1):
        if abs(target - pos[i]) > settle_band:
            settle_t = t[i] - t[0]
            break
        if i == 0:
            settle_t = 0.0

    # Steady-state error — mean |err| in the last tail_s of the trace
    cutoff = t[-1] - tail_s
    tail = [abs(target - pos[i]) for i in range(len(t)) if t[i] >= cutoff]
    ss_err = sum(tail) / len(tail) if tail else abs(target - pos[-1])

    cost = (
        iae
        + w_overshoot * overshoot
        + w_ss * ss_err * span / 50.0   # scale ss_err penalty with step size
        + w_settle * settle_t
    )
    if aborted:
        cost += abort_penalty

    return StepMetrics(
        iae=iae,
        rise_time=rise,
        overshoot_pct=overshoot,
        settling_time=settle_t,
        ss_error=ss_err,
        cost=cost,
        aborted=aborted,
    )


def aggregate(metrics_list, weights=None):
    """Weighted mean cost across a step suite. Returns (mean_cost, list)."""
    if not metrics_list:
        return float("inf"), []
    if weights is None:
        weights = [1.0] * len(metrics_list)
    total_w = sum(weights)
    cost = sum(m.cost * w for m, w in zip(metrics_list, weights)) / total_w
    return cost, metrics_list
