"""
A simulated motorized fader: position integrates motor drive over time.

Crude but sufficient — it gives the PID a plant that responds to direction and
duty, so calibration endpoints, deadband settling and mute travel can be tested
without hardware.
"""

# Percent of travel per second at full duty. The real fader crosses 100 mm in
# roughly a second, so this is the right order of magnitude.
TRAVEL_PCT_PER_S = 120.0

# Duty below which the motor does not overcome stall friction.
STALL_DUTY = 9000


class SimFader:
    def __init__(self, raw_min=200, raw_max=3900, inverted=False, position=50.0):
        self.position = position
        self.raw_min = raw_min
        self.raw_max = raw_max
        self.inverted = inverted
        self._in1 = 0
        self._in2 = 0
        self._duty = 0

    def set_drive(self, in1, in2, duty):
        self._in1, self._in2, self._duty = in1, in2, duty

    def step(self, dt_s, stby=1):
        if not stby or self._duty < STALL_DUTY:
            return
        direction = 0
        if self._in1 and not self._in2:
            direction = 1
        elif self._in2 and not self._in1:
            direction = -1
        if direction == 0:
            return
        speed = self._duty / 65535.0 * TRAVEL_PCT_PER_S
        self.position = max(0.0, min(100.0, self.position + direction * speed * dt_s))

    def raw12(self):
        """ADC counts this wiper presents, honouring wiper polarity."""
        fraction = self.position / 100.0
        if self.inverted:
            fraction = 1.0 - fraction
        span = self.raw_max - self.raw_min
        return int(self.raw_min + fraction * span)
