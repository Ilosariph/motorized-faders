"""
Per-fader screen layout on a 128x64 SSD1306.

Content is plugin-supplied: the host pushes two text lines with
`DISP:idx,line1,line2` and the Pico draws them above a position bar it renders
from data it already has. This keeps the firmware plugin-agnostic — pulse_sink
sends a sink name and volume, a future plugin sends whatever it wants.

    +------------------------------+
    | sink-music                   |  line1 (scale 2)
    | vol 78%                      |  line2 (scale 2)
    | [##################------]   |  position bar, from read_position()
    | A                            |  A/B slot badge (host)
    +------------------------------+

Each line drops to scale 1 when it is too long to fit doubled.

Muted, the bar is empty but for a thin strip showing where unmute will
return the fader, and a solid bar replaces the bottom row:

    | [=============             ]   |  thin strip: restore position
    | A######## MUTE 78% ##########  |  mute bar, restore % knocked out

`MENU:idx,label,name` swaps the whole layout for a menu card — no bar,
badge or mute — until the next `DISP:`. The host shows it on every screen
while G1 is held, to pick a layer:

    |           < prev             |  label (scale 1)
    |                              |
    |        Discord               |  name, as large as fits (scale 3..1)

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

_MAX_CHARS_SCALE3 = WIDTH // (CHAR_W * 3)
_MAX_CHARS_SCALE2 = WIDTH // (CHAR_W * 2)
_MAX_CHARS_SCALE1 = WIDTH // CHAR_W

# Menu card: label row, then the name centred in the space below it.
MENU_LABEL_Y = 4
MENU_NAME_TOP = 16
GLYPH_H = 7

_MUTE_LABEL = "MUTE"

# Restore strip inside the empty bar while muted: thin, so it reads as a
# marker rather than as the fader's live position.
RESTORE_H = 4

# A/B slot badge, bottom-left: one inverted character.
SLOT_X = 1
SLOT_W = CHAR_W + 3
SLOT_H = MUTE_BAR_H  # same row as the readout and the mute label


class FaderScreen:
    def __init__(self, display):
        self.display = display
        self.line1 = ""
        self.line2 = ""
        self.muted = False
        self.position = 0.0
        self.restore = 0.0
        self.slot = ""
        # (label, name) while a menu card replaces the layout, else None.
        self.menu = None
        self._drawn = None  # last rendered tuple; None forces a first draw

    # ----- state ----------------------------------------------------------

    def set_lines(self, line1, line2):
        """Fader text; also ends a menu card."""
        self.line1 = line1
        self.line2 = line2
        self.menu = None

    def set_menu(self, label, name):
        self.menu = (label, name)

    def set_muted(self, muted):
        self.muted = bool(muted)

    def set_position(self, position):
        self.position = position

    def set_restore(self, position):
        """Where unmute will return the fader. Drawn only while muted."""
        self.restore = position

    def set_slot(self, slot):
        """A/B slot badge text from the host; empty hides it."""
        self.slot = slot[:1]

    def needs_redraw(self):
        return self._fingerprint() != self._drawn

    def content_changed(self):
        """
        Text, mute, badge or menu differ from the panel. Bar and restore marker
        are left out: they follow a hand on the fader and can wait for the
        rate limit; content changes come in bursts (a layer switch) and
        should land on every screen at once.
        """
        if self._drawn is None:
            return True
        fp = self._fingerprint()
        return fp[:3] != self._drawn[:3] or fp[5:] != self._drawn[5:]

    def _fingerprint(self):
        # Quantise position so sub-epsilon jitter doesn't trigger redraws.
        return (
            self.line1,
            self.line2,
            self.muted,
            int(self.position / POSITION_EPSILON),
            int(self.restore / POSITION_EPSILON) if self.muted else None,
            self.slot,
            self.menu,
        )

    # ----- rendering ------------------------------------------------------

    def draw(self, force=False):
        """Redraw if anything changed. Returns True if the panel was written."""
        if not force and not self.needs_redraw():
            return False

        d = self.display
        d.fill(0)

        if self.menu is not None:
            self._draw_menu()
            d.show()
            self._drawn = self._fingerprint()
            return True

        # line1 at scale 2 — the identity of what this fader controls, so it
        # must be readable at a glance from across a desk. Falls back to
        # scale 1 when the text is too long to fit doubled, since a truncated
        # sink name is worse than a smaller legible one.
        if len(self.line1) <= _MAX_CHARS_SCALE2:
            d.text(self.line1, 2, 0, 1, scale=2)
        else:
            d.text(self.line1[:_MAX_CHARS_SCALE1], 2, 4, 1)

        # line2 likewise: the plugin's value (volume), the other thing to read
        # at a glance. The bar already shows the fader's own position.
        if len(self.line2) <= _MAX_CHARS_SCALE2:
            d.text(self.line2, 2, 16, 1, scale=2)
        else:
            d.text(self.line2[:_MAX_CHARS_SCALE1], 2, 20, 1)

        self._draw_bar()
        if self.muted:
            self._draw_restore()
            self._draw_mute_bar()
        self._draw_slot()

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

    def _draw_restore(self):
        d = self.display
        fraction = max(0.0, min(100.0, self.restore)) / 100.0
        filled = int((BAR_W - 4) * fraction)
        if filled > 0:
            y = BAR_Y + (BAR_H - RESTORE_H) // 2
            d.fill_rect(BAR_X + 2, y, filled, RESTORE_H, 1)

    def _draw_slot(self):
        if not self.slot:
            return
        d = self.display
        y = HEIGHT - SLOT_H
        if not self.muted:
            # Inside the mute bar the background is already solid.
            d.fill_rect(SLOT_X, y, SLOT_W, SLOT_H, 1)
        d.text(self.slot, SLOT_X + 2, y + 2, 0)

    def _draw_menu(self):
        d = self.display
        label, name = self.menu
        label = label[:_MAX_CHARS_SCALE1]
        d.text(label, (WIDTH - len(label) * CHAR_W) // 2, MENU_LABEL_Y, 1)
        if len(name) <= _MAX_CHARS_SCALE3:
            scale = 3
        elif len(name) <= _MAX_CHARS_SCALE2:
            scale = 2
        else:
            scale, name = 1, name[:_MAX_CHARS_SCALE1]
        # Glyphs are 5 of CHAR_W columns wide: centre on the ink, not the cell.
        width = (len(name) * CHAR_W - 1) * scale
        y = MENU_NAME_TOP + (HEIGHT - MENU_NAME_TOP - GLYPH_H * scale) // 2
        d.text(name, max(0, (WIDTH - width) // 2), y, 1, scale=scale)

    def _draw_mute_bar(self):
        """Solid bar along the bottom, label knocked out of it."""
        d = self.display
        d.fill_rect(0, MUTE_BAR_Y, WIDTH, MUTE_BAR_H, 1)
        label = "{}  {:.0f}%".format(_MUTE_LABEL, self.restore)
        x = (WIDTH - len(label) * CHAR_W) // 2
        d.text(label, x, MUTE_BAR_Y + 2, 0)
