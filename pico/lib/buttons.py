"""
Button ladder decode — 6 buttons on 2 spare MCP3208 channels.

Wiring per docs/pcb_design.md: a 10k top resistor to 3.3 V, each button
shorting the node to GND through its own series resistor. Idle reads full
scale; a press reads 3.3 V * Rs / (10k + Rs).

    CH6: SW_G1 (0R), SW_G2 (4k7)
    CH7: SW_F1 (0R), SW_F2 (2k2), SW_F3 (4k7), SW_F4 (10k)

Simultaneous presses on one channel read as the lowest-value button, which is
why the 2 general buttons sit on their own channel.
"""

R_TOP = 10000.0

# (name, series resistance to GND) per channel, any order.
GENERAL_LADDER = (("G1", 0.0), ("G2", 4700.0))
FADER_LADDER = (("F1", 0.0), ("F2", 2200.0), ("F3", 4700.0), ("F4", 10000.0))

# Fraction of full scale. Idle is 1.0; the widest real band is ~0.09 wide, so
# half the gap to the nearest neighbour is a generous window.
IDLE_THRESHOLD = 0.92

# A press must read the same band on this many consecutive polls to count.
# The 100 nF at each ladder node handles contact bounce; this covers the
# settling ramp through the band boundaries.
DEBOUNCE_SAMPLES = 2


def _expected(series_r):
    """Fraction of full-scale this button reads when pressed."""
    return series_r / (R_TOP + series_r)


class ButtonLadder:
    """
    One ADC channel carrying several buttons. `poll()` returns the name of a
    button that was just pressed, or None.

    Edge-triggered: a held button reports once. Release is not reported —
    nothing in this design needs it.
    """

    def __init__(self, read_fn, ladder, full_scale=4095):
        self._read = read_fn
        self._full_scale = float(full_scale)
        # Sort by expected level so neighbouring bands are adjacent, then take
        # each boundary as the midpoint between neighbours.
        entries = sorted(((_expected(r), name) for name, r in ladder))
        self._bands = []
        for i, (level, name) in enumerate(entries):
            upper = (
                (level + entries[i + 1][0]) / 2.0
                if i + 1 < len(entries)
                else (level + IDLE_THRESHOLD) / 2.0
            )
            self._bands.append((upper, name))

        self._held = None
        self._candidate = None
        self._candidate_count = 0

    def _classify(self, raw):
        frac = raw / self._full_scale
        if frac >= IDLE_THRESHOLD:
            return None
        for upper, name in self._bands:
            if frac < upper:
                return name
        return None

    def poll(self):
        reading = self._classify(self._read())

        if reading != self._candidate:
            self._candidate = reading
            self._candidate_count = 1
            return None

        self._candidate_count += 1
        if self._candidate_count < DEBOUNCE_SAMPLES:
            return None

        # Stable reading. Report only the transition into a press.
        if reading != self._held:
            self._held = reading
            if reading is not None:
                return reading
        return None
