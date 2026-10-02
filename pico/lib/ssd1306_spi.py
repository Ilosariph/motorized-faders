"""
Minimal SSD1306 128x64 SPI driver with a 5x7 bitmap font.

Written rather than using the stock `ssd1306` module + `framebuf` because four
displays share one SPI bus here and each needs its own buffer; this keeps the
per-display cost to one 1 KB bytearray and avoids pulling in framebuf.

RES is not a GPIO — it is an RC power-on reset shared by all four displays
(docs/pcb_design.md). Wait ~5 ms after power-up before the first command; in
practice calibration runs first and takes far longer.

DC and SCK/MOSI are shared across all four displays; only CS differs.
"""

WIDTH = 128
HEIGHT = 64
PAGES = HEIGHT // 8

# 5x7 font, column-major, one byte per column, bit 0 = top row.
_FONT = {
    " ": b"\x00\x00\x00\x00\x00", "!": b"\x00\x00\x5f\x00\x00",
    '"': b"\x00\x07\x00\x07\x00", "#": b"\x14\x7f\x14\x7f\x14",
    "$": b"\x24\x2a\x7f\x2a\x12", "%": b"\x23\x13\x08\x64\x62",
    "&": b"\x36\x49\x55\x22\x50", "'": b"\x00\x05\x03\x00\x00",
    "(": b"\x00\x1c\x22\x41\x00", ")": b"\x00\x41\x22\x1c\x00",
    "*": b"\x14\x08\x3e\x08\x14", "+": b"\x08\x08\x3e\x08\x08",
    ",": b"\x00\x50\x30\x00\x00", "-": b"\x08\x08\x08\x08\x08",
    ".": b"\x00\x60\x60\x00\x00", "/": b"\x20\x10\x08\x04\x02",
    "0": b"\x3e\x51\x49\x45\x3e", "1": b"\x00\x42\x7f\x40\x00",
    "2": b"\x42\x61\x51\x49\x46", "3": b"\x21\x41\x45\x4b\x31",
    "4": b"\x18\x14\x12\x7f\x10", "5": b"\x27\x45\x45\x45\x39",
    "6": b"\x3c\x4a\x49\x49\x30", "7": b"\x01\x71\x09\x05\x03",
    "8": b"\x36\x49\x49\x49\x36", "9": b"\x06\x49\x49\x29\x1e",
    ":": b"\x00\x36\x36\x00\x00", ";": b"\x00\x56\x36\x00\x00",
    "<": b"\x00\x08\x14\x22\x41", "=": b"\x14\x14\x14\x14\x14",
    ">": b"\x41\x22\x14\x08\x00", "?": b"\x02\x01\x51\x09\x06",
    "@": b"\x32\x49\x79\x41\x3e", "A": b"\x7e\x11\x11\x11\x7e",
    "B": b"\x7f\x49\x49\x49\x36", "C": b"\x3e\x41\x41\x41\x22",
    "D": b"\x7f\x41\x41\x22\x1c", "E": b"\x7f\x49\x49\x49\x41",
    "F": b"\x7f\x09\x09\x09\x01", "G": b"\x3e\x41\x49\x49\x7a",
    "H": b"\x7f\x08\x08\x08\x7f", "I": b"\x00\x41\x7f\x41\x00",
    "J": b"\x20\x40\x41\x3f\x01", "K": b"\x7f\x08\x14\x22\x41",
    "L": b"\x7f\x40\x40\x40\x40", "M": b"\x7f\x02\x0c\x02\x7f",
    "N": b"\x7f\x04\x08\x10\x7f", "O": b"\x3e\x41\x41\x41\x3e",
    "P": b"\x7f\x09\x09\x09\x06", "Q": b"\x3e\x41\x51\x21\x5e",
    "R": b"\x7f\x09\x19\x29\x46", "S": b"\x46\x49\x49\x49\x31",
    "T": b"\x01\x01\x7f\x01\x01", "U": b"\x3f\x40\x40\x40\x3f",
    "V": b"\x1f\x20\x40\x20\x1f", "W": b"\x7f\x20\x18\x20\x7f",
    "X": b"\x63\x14\x08\x14\x63", "Y": b"\x03\x04\x78\x04\x03",
    "Z": b"\x61\x51\x49\x45\x43", "[": b"\x00\x7f\x41\x41\x00",
    "\\": b"\x02\x04\x08\x10\x20", "]": b"\x00\x41\x41\x7f\x00",
    "^": b"\x04\x02\x01\x02\x04", "_": b"\x40\x40\x40\x40\x40",
    "`": b"\x00\x01\x02\x04\x00", "a": b"\x20\x54\x54\x54\x78",
    "b": b"\x7f\x48\x44\x44\x38", "c": b"\x38\x44\x44\x44\x20",
    "d": b"\x38\x44\x44\x48\x7f", "e": b"\x38\x54\x54\x54\x18",
    "f": b"\x08\x7e\x09\x01\x02", "g": b"\x0c\x52\x52\x52\x3e",
    "h": b"\x7f\x08\x04\x04\x78", "i": b"\x00\x44\x7d\x40\x00",
    "j": b"\x20\x40\x44\x3d\x00", "k": b"\x7f\x10\x28\x44\x00",
    "l": b"\x00\x41\x7f\x40\x00", "m": b"\x7c\x04\x18\x04\x78",
    "n": b"\x7c\x08\x04\x04\x78", "o": b"\x38\x44\x44\x44\x38",
    "p": b"\x7c\x14\x14\x14\x08", "q": b"\x08\x14\x14\x18\x7c",
    "r": b"\x7c\x08\x04\x04\x08", "s": b"\x48\x54\x54\x54\x20",
    "t": b"\x04\x3f\x44\x40\x20", "u": b"\x3c\x40\x40\x20\x1c",
    "v": b"\x1c\x20\x40\x20\x1c", "w": b"\x3c\x40\x30\x40\x3c",
    "x": b"\x44\x28\x10\x28\x44", "y": b"\x0c\x50\x50\x50\x3c",
    "z": b"\x44\x64\x54\x4c\x44", "{": b"\x00\x08\x36\x41\x00",
    "|": b"\x00\x00\x7f\x00\x00", "}": b"\x00\x41\x36\x08\x00",
    "~": b"\x08\x04\x08\x10\x08",
}
_UNKNOWN = b"\x7f\x41\x41\x41\x7f"  # hollow box for anything unmapped
CHAR_W = 6  # 5 columns + 1 spacing


class SSD1306_SPI:
    def __init__(self, spi, dc, cs):
        self.spi = spi
        self.dc = dc
        self.cs = cs
        self.cs.value(1)
        self.buf = bytearray(WIDTH * PAGES)
        self.init_display()

    # ----- transport -------------------------------------------------------

    def _cmd(self, *bytes_):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes(bytes_))
        self.cs.value(1)

    def init_display(self):
        for c in (
            0xAE,              # display off
            0x20, 0x00,        # horizontal addressing mode
            0x40,              # start line 0
            0xA1,              # segment remap (A1 = flipped horizontally)
            0xA8, HEIGHT - 1,  # multiplex ratio
            0xC8,              # COM scan direction, reversed
            0xD3, 0x00,        # display offset
            0xDA, 0x12,        # COM pin config for 128x64
            0xD5, 0x80,        # clock divide
            0xD9, 0xF1,        # pre-charge
            0xDB, 0x30,        # VCOM deselect
            0x81, 0xCF,        # contrast
            0xA4,              # resume from RAM
            0xA6,              # non-inverted
            0x8D, 0x14,        # charge pump on
            0xAF,              # display on
        ):
            self._cmd(c)
        self.fill(0)
        self.show()

    def show(self):
        self._cmd(0x21, 0, WIDTH - 1)      # column range
        self._cmd(0x22, 0, PAGES - 1)      # page range
        self.dc.value(1)
        self.cs.value(0)
        self.spi.write(self.buf)
        self.cs.value(1)

    def poweroff(self):
        self._cmd(0xAE)

    # ----- drawing ---------------------------------------------------------

    def fill(self, colour):
        value = 0xFF if colour else 0x00
        for i in range(len(self.buf)):
            self.buf[i] = value

    def pixel(self, x, y, colour):
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            return
        index = (y >> 3) * WIDTH + x
        bit = 1 << (y & 7)
        if colour:
            self.buf[index] |= bit
        else:
            self.buf[index] &= ~bit & 0xFF

    def hline(self, x, y, width, colour):
        for i in range(x, x + width):
            self.pixel(i, y, colour)

    def vline(self, x, y, height, colour):
        for i in range(y, y + height):
            self.pixel(x, i, colour)

    def rect(self, x, y, width, height, colour):
        self.hline(x, y, width, colour)
        self.hline(x, y + height - 1, width, colour)
        self.vline(x, y, height, colour)
        self.vline(x + width - 1, y, height, colour)

    def fill_rect(self, x, y, width, height, colour):
        for row in range(y, y + height):
            self.hline(x, row, width, colour)

    def text(self, string, x, y, colour=1, scale=1):
        """Draw `string` with its top-left at (x, y). scale=2 doubles size."""
        cursor = x
        for char in string:
            glyph = _FONT.get(char, _UNKNOWN)
            for col, bits in enumerate(glyph):
                for row in range(7):
                    if bits & (1 << row):
                        if scale == 1:
                            self.pixel(cursor + col, y + row, colour)
                        else:
                            self.fill_rect(
                                cursor + col * scale, y + row * scale,
                                scale, scale, colour,
                            )
            cursor += CHAR_W * scale
            if cursor >= WIDTH:
                break
