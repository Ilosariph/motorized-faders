"""
MCP3208 — 12-bit 8-channel SPI ADC.

Shares SPI0 with the four SSD1306 displays (see docs/pcb_design.md). Only the
MCP3208 drives MISO, so bus sharing is safe; devices are selected by CS.

VREF is tied to the same 3.3 V net that feeds fader terminal 3, so readings are
ratiometric and supply drift cancels out of the divider ratio.
"""


class MCP3208:
    def __init__(self, spi, cs):
        self.spi = spi
        self.cs = cs
        self.cs.value(1)
        self._tx = bytearray(3)
        self._rx = bytearray(3)

    def read(self, channel):
        """Single-ended read of `channel` (0-7). Returns 0-4095."""
        if not 0 <= channel <= 7:
            raise ValueError("channel out of range")
        # Command frame, MSB first: 5 leading zeros, start bit, SGL/DIFF=1
        # (single-ended), then 3 address bits. The 12 result bits follow one
        # null bit, landing as rx[1][3:0] + rx[2].
        self._tx[0] = 0x06 | (channel >> 2)
        self._tx[1] = (channel & 0x03) << 6
        self._tx[2] = 0x00

        self.cs.value(0)
        self.spi.write_readinto(self._tx, self._rx)
        self.cs.value(1)

        return ((self._rx[1] & 0x0F) << 8) | self._rx[2]

    def read_u16(self, channel):
        """Read scaled to the 0-65535 convention the PID code already uses."""
        return self.read(channel) << 4
