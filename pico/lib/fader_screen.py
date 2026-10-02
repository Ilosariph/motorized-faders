"""
Per-fader screen layout on a 128x64 SSD1306.

Content is plugin-supplied: the host pushes two text lines with
`DISP:idx,line1,line2` and the Pico draws them above a position bar it renders
from data it already has. This keeps the firmware plugin-agnostic — pulse_sink
sends a sink name and volume, a future plugin sends whatever it wants.

    +------------------------------+
    | sink-music                   |  line1 (scale 2)
    | vol 78%                      |  line2
    |                              |
    | [##################------]   |  position bar, from read_position()
    |                        78.2  |  live position, always firmware-owned
    | ############ MUTE ########## |  mute bar — solid, full width
    +------------------------------+

The display is a monochrome SSD1306: white-on-black, no colour. Mute therefore
reads as a solid full-width bar along the bottom with inverted text knocked out
of it — a large filled block is recognisable from across a desk at a glance,
where a small outlined badge is not.

Redraws happen on change only — never every loop. A full frame is ~8 ms at
1 MHz, which would otherwise disturb PID timing.
"""

from .ssd1306_spi import CHAR_W, HEIGHT, WIDTH

BAR_X = 2
BAR_Y = 32
BAR_W = WIDTH - 4
BAR_H = 14

# Mute bar: full width along the bottom, tall enough to read as a block.
MUTE_BAR_H = 11
MUTE_BAR_Y = HEIGHT - MUTE_BAR_H

# Redraw the bar when the position moves at least this far (%). Matches the
# protocol's REPORT_DELTA so the screen and the host see the same granularity.
POSITION_EPSILON = 0.5

_MAX_CHARS_SCALE2 = WIDTH // (CHAR_W * 2)
_MAX_CHARS_SCALE1 = WIDTH // CHAR_W

_MUTE_LABEL = "MUTE"


class FaderScreen:
    def __init__(self, display):
        self.display = display
        self.line1 = ""
        self.line2 = ""
        self.muted = False
        self.position = 0.0
        self._drawn = None  # last rendered tuple; None forces a first draw

    # ----- state ----------------------------------------------------------

    def set_lines(self, line1, line2):
        self.line1 = line1
        self.line2 = line2

    def set_muted(self, muted):
        self.muted = bool(muted)

    def set_position(self, position):
        self.position = position

    def needs_redraw(self):
        return self._fingerprint() != self._drawn

    def _fingerprint(self):
        # Quantise position so sub-epsilon jitter doesn't trigger redraws.
        return (
            self.line1,
            self.line2,
            self.muted,
            int(self.position / POSITION_EPSILON),
        )

    # ----- rendering ------------------------------------------------------

    def draw(self, force=False):
        """Redraw if anything changed. Returns True if the panel was written."""
        if not force and not self.needs_redraw():
            return False

        d = self.display
        d.fill(0)

        # line1 at scale 2 — the identity of what this fader controls, so it
        # must be readable at a glance from across a desk. Falls back to
        # scale 1 when the text is too long to fit doubled, since a truncated
        # sink name is worse than a smaller legible one.
        if len(self.line1) <= _MAX_CHARS_SCALE2:
            d.text(self.line1, 2, 0, 1, scale=2)
        else:
            d.text(self.line1[:_MAX_CHARS_SCALE1], 2, 4, 1)

        d.text(self.line2[:_MAX_CHARS_SCALE1], 2, 20, 1)

        self._draw_bar()
        if self.muted:
            self._draw_mute_bar()
        else:
            self._draw_readout()

        d.show()
        self._drawn = self._fingerprint()
        return True

    def _draw_bar(self):
        d = self.display
        d.rect(BAR_X, BAR_Y, BAR_W, BAR_H, 1)
        fraction = max(0.0, min(100.0, self.position)) / 100.0
        inner = BAR_W - 4
        filled = int(inner * fraction)
        if filled > 0:
            d.fill_rect(BAR_X + 2, BAR_Y + 2, filled, BAR_H - 4, 1)

    def _draw_readout(self):
        text = "{:.1f}%".format(self.position)
        x = WIDTH - len(text) * CHAR_W - 2
        self.display.text(text, x, HEIGHT - 9, 1)

    def _draw_mute_bar(self):
        """Solid bar along the bottom, label knocked out of it."""
        d = self.display
        d.fill_rect(0, MUTE_BAR_Y, WIDTH, MUTE_BAR_H, 1)
        label = "{}  {:.1f}%".format(_MUTE_LABEL, self.position)
        x = (WIDTH - len(label) * CHAR_W) // 2
        d.text(label, x, MUTE_BAR_Y + 2, 0)
