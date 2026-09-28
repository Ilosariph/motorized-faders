# 4-Fader PCB — Design Notes

Hardware design for a single-board 4-fader controller: 4 motorized faders, one
button per fader, 2 general buttons, one display per fader.

This supersedes the 2-fader breadboard build in the [README](../README.md).
The modular multi-board concept lives in [modular_design.md](modular_design.md);
this document is the single-board version.

---

## Summary of Decisions

| Question | Decision |
|----------|----------|
| ADC | MCP3208 (12-bit, 8ch, SPI) for **all 4** faders — not internal+external mix |
| Why not ADS1115 | ~215 Hz effective per channel — too slow for the PID loop |
| Motor driver | 2× TB6612FNG — **dual footprint**: SSOP-24 land + breakout header, same nets |
| Display | SSD1306 128×64 **SPI** (I2C can't address 4 — only 0x3C/0x3D exist) |
| Buttons | Resistor ladder on 2 spare MCP3208 channels — costs 0 GPIO |
| Faders | RSA0N11M9 100 mm 10 kΩ, case-mounted, soldered wire — not on PCB |
| Pin map | Fixed: SPI0 on GP16/18/19, PWM on 4 distinct slices, GP0/1 spare |
| MCU | Keep the existing RP2040 module — bare RP2040 not needed |
| Motor power | USB-C PD trigger, **jumper-set 9 V** (PD has no 10 V PDO) |
| Rail check | Divider into spare ADC CH4 — catches silent 5 V fallback, 0 GPIO |
| Assembly | Fully hand-solderable, no hot air / reflow required |
| Build order | Socket breakouts first, prove the board, then solder SSOP — **no respin** |

---

## ADC: MCP3208

**Do not use the ADS1115**, despite it being the usual recommendation. At its
maximum 860 SPS one conversion takes ~1.3 ms, and its input mux is sequential,
giving **~215 Hz per channel across 4 channels**. The firmware averages 16
samples per read (`FaderPID._raw_adc`), so a single position read would cost
~75 ms — the ADC would become the dominant time constant of the control loop
and wreck the derivative term.

| | MCP3208 | ADS1115 |
|---|---|---|
| Interface | SPI | I2C |
| Channels | 8 | 4 |
| Rate | 100 kSPS | 860 SPS |
| 4ch x 16 averaged samples | ~0.6 ms | ~75 ms |
| Resolution | 12 bit | 16 bit nominal |
| Package | **DIP-16** available | MSOP-10 only |

12 bits over 60 mm of travel is 0.015 mm/LSB. `DEADBAND` is 0.2% = 0.12 mm, so
resolution is not the binding constraint — loop rate is. The 8 channels also
leave 4 spare inputs, 2 of which the buttons use (below).

### Ratiometric reference — do not skip this

Tie MCP3208 `VREF` to the **same 3.3 V net** that feeds fader terminal 3.

The fader is a resistive divider, so if VREF and the fader supply are the same
node, supply drift cancels out of the ratio entirely. This is the single
largest accuracy win in the design and costs nothing. `VDD` and `VREF` both to
3.3 V, 100 nF on each.

### Why all 4 faders, not internal + external

Mixing the RP2040's internal ADC (faders 1-2) with the MCP3208 (faders 3-4)
would work, but:

- **No code saving.** Hardware access is isolated to `FaderPID._raw_adc()`.
  Swapping it out is the same edit whether 2 or 4 faders use it — mixed mode
  just means maintaining two paths.
- **The paths don't match physically.** The RP2040 ADC is ~8.5-9 ENOB with
  known DNL glitches from its SAR DAC capacitors; the MCP3208 with ratiometric
  VREF has a genuinely different transfer function. Faders would calibrate and
  jitter differently while `DEADBAND` / `MIN_MOVE_PCT` are global constants.
- **GP26/27/28 are needed elsewhere** — see the GPIO budget.

### Firmware change

Inject a reader callable instead of a pin number:

```python
class FaderPID:
    def __init__(self, read_raw, motor):
        self._read = read_raw          # callable -> 0..65535
        ...
    def _raw_adc(self):
        return sum(self._read() for _ in range(16)) // 16
```

Construct with `FaderPID(read_raw=lambda: mcp.read(0) << 4, motor=...)`. The
`<< 4` scales the 12-bit result into the existing 16-bit `adc_min`/`adc_max`
convention, so `calibrate()`, `read_position()` and the entire PID body are
untouched. The `POS:` / `STATE:` protocol, `host/`, and `faders/core.py` are
all unaffected.

---

## Motor Driver: TB6612FNG, direct mount

Two chips: faders 1-2 on the first, faders 3-4 on the second.

### Pinout (SSOP-24)

| Pin | Name | | Pin | Name |
|-----|------|---|-----|------|
| 1, 2 | AO1 | | 13 | VM2 |
| 3, 4 | PGND1 | | 14 | VM3 |
| 5, 6 | AO2 | | 15 | PWMB |
| 7, 8 | BO2 | | 16 | BIN2 |
| 9, 10 | PGND2 | | 17 | BIN1 |
| 11, 12 | BO1 | | 18 | GND (signal) |
| | | | 19 | STBY |
| | | | 20 | VCC |
| | | | 21 | AIN1 |
| | | | 22 | AIN2 |
| | | | 23 | PWMA |
| | | | 24 | VM1 |

**Doubled pins are not optional.** Pairs 1/2, 3/4, 5/6, 7/8, 9/10, 11/12 are
the same net doubled for current capacity — tie each pair together with wide
copper. Likewise VM1/VM2/VM3 (24, 13, 14) are one net.

### External components, per chip

| Part | Value | Placement |
|------|-------|-----------|
| Bulk cap | 100 uF electrolytic, >=25 V | across VM/PGND, near pins 24 + 13/14 |
| VM ceramic | 100 nF X7R | same nets, right at the pins |
| VCC ceramic | 100 nF X7R | pin 20 -> pin 18, as close as possible |
| STBY pull-up | 10 kΩ | pin 19 -> 3.3 V |

**The STBY pull-up matters even though firmware drives the pin.**
`MotorDriver.__init__` sets STBY as an output, but the resistor defines the pin
state during MCU reset and before `main.py` runs. Without it STBY floats at
boot while the 10 V rail is already up, leaving the H-bridge in an undefined
state. Both chips' STBY can share one GPIO.

### Why AIN1/AIN2 and BIN1/BIN2 must come from the MCU

These are **inputs to** the TB6612FNG, not outputs from it. The chip has no
logic of its own; the motor terminals are AO1/AO2 and BO1/BO2. Per channel:

| AIN1 | AIN2 | PWM | Result |
|------|------|-----|--------|
| H | L | PWM | forward |
| L | H | PWM | reverse |
| L | L | H | coast (free-spin) |
| H | H | X | brake (motor shorted) |

AIN1/AIN2 select direction, PWMA sets speed — exactly what `MotorDriver.drive()`
does from the sign and magnitude of `power`.

**Rejected optimisation:** since the firmware never uses brake, AIN2 is always
the inverse of AIN1, so a 74HC04 hex inverter could derive it and save 4 GPIO.
This does not work — with a hard inverter AIN1/AIN2 can never both be LOW, so
**coast is lost**, and `MotorDriver.stop()` coasts on every settle. Keep both
pins as GPIO.

### Layout

- VM/PGND carries the motor current: tight loop, traces >=1 mm for 1.6 A,
  bulk cap inside the loop.
- Keep PGND1/PGND2 separate from signal GND across the board; join at a single
  point at the bulk cap ground terminal.
- SSOP-24 has no thermal pad — heat leaves through the PGND pins. Pour copper.

---

## Displays: SSD1306 SPI

`modular_design.md` specified I2C SSD1306 at 0x3C / 0x3D. **That does not scale
to 4 displays** — those are the only two addresses the SSD1306 supports.

**Use the SPI variant.** Share SCK / MOSI / DC / RES across all four, give each
its own CS. Costs 4 GPIO, adds no IC, and is much faster than I2C — which
matters because redraws happen while motors are running.

Alternative, only if I2C modules are already on hand: a TCA9548A I2C mux
(addr 0x70) — 1 extra IC, 2 GPIO total, but every draw needs a channel-select
write first on the slower bus.

**Identifying which variant a module is:** count the pins.
4 pins (GND/VCC/SCL/SDA) = I2C. 7 pins (GND/VCC/D0/D1/RES/DC/CS) = SPI.

### PCB footprint: fit the 7-pin SPI header regardless

Displays are panel-mounted in the case and wired back with jumpers, so the
module never sits on the PCB. Put a **7-pin SPI header footprint** on the board
anyway — it is the superset:

- A 7-pin SPI module wires straight in.
- A 4-pin I2C module also works from the same header: SDA -> D1 (MOSI),
  SCL -> D0 (SCK), leave CS/DC/RES unconnected.
- The reverse is not true — a 4-pin I2C header cannot drive an SPI module.

This defers the display decision to assembly time, which is when the variant in
hand is actually known. Label the pads on silkscreen
(`GND VCC D0 D1 RES DC CS`) since that is what gets read with jumpers in hand.

**Flying-lead caution:** SPI over long leads is more fragile than I2C — faster
clock, no acknowledgement. Keep display leads under ~15 cm. If glitches appear,
drop the bus to ~500 kHz before suspecting anything else.

---

## Buttons: resistor ladder on spare ADC channels

6 buttons on 2 spare MCP3208 channels, split **2 general + 4 per-fader**.
Costs **zero GPIO**.

Grouping by function rather than 3+3 keeps the mapping obvious when reading the
firmware or probing the board, and gives the 2-button channel wider margins.

```
Channel 6 -- 2 general buttons
3.3V --[10k]--+--------------------> CH6
              +--[SW_G1]--[GND]          pressed: 0.00 V
              +--[SW_G2]--[4.7k]--[GND]  pressed: 1.06 V
                                         idle:    3.30 V

Channel 7 -- 4 per-fader buttons
3.3V --[10k]--+--------------------> CH7
              +--[SW_F1]--[GND]          pressed: 0.00 V
              +--[SW_F2]--[2.2k]--[GND]  pressed: 0.60 V
              +--[SW_F3]--[4.7k]--[GND]  pressed: 1.06 V
              +--[SW_F4]--[10k]---[GND]  pressed: 1.65 V
                                         idle:    3.30 V
```

Bands are 600 mV or wider — trivially discriminated at 12 bits (1 LSB =
0.8 mV). Add 100 nF to GND at each node for debounce. Buttons are slow, so
ADC-rate polling is ample.

Simultaneous presses on one channel read as the lowest-value button. The 2+4
split means the two general buttons can never mask each other's neighbours,
which is the case most likely to be pressed together.

---

## GPIO Budget

Board exposes GP0-3, GP6-22, GP26-28 = **24 usable** (GP4/GP5 not broken out).
GP26-28 are free now that faders read via the MCP3208.

| Function | Pins |
|----------|------|
| Motor control, 2x TB6612FNG (AIN1/2, PWMA, BIN1/2, PWMB) | 12 |
| STBY (shared between both chips) | 1 |
| SPI bus, shared (SCK, MOSI, MISO) | 3 |
| MCP3208 CS | 1 |
| Display DC (shared across all 4) | 1 |
| Display CS x 4 | 4 |
| **Total** | **22** |

Two pins spare. Display **RES needs no GPIO** — it is an RC power-on reset; see
[Free a GPIO](#free-a-gpio-rc-reset-on-display-res). The VM rail check also
costs no GPIO: it is a divider into spare MCP3208 channel CH4.

**The SPI bus is shared** between the MCP3208 and the four displays. This is
what makes the budget close — a separate ADC bus would need 2 more pins and put
the total at 25, over budget. It is safe because the displays are write-only
(no MISO) so only the MCP3208 ever drives MISO; devices are selected by CS.

Run the bus at ~1 MHz (safe for the MCP3208 at 3.3 V; the SSD1306 tolerates it
easily). A full 128x64 frame is 1 KB, ~8 ms per redraw — redraw on change only,
never every loop, and PID timing is unaffected.

**Touch sense is not in this budget.** The T terminal is unused; move
completion comes from the PID state machine (deadband + settle timer).

### Pin assignment is constrained, not free

The count fits, but *which* physical pin carries which signal is not arbitrary.

**SPI must land on hardware-SPI pins.** The RP2040 mapping is fixed silicon,
not a routing matrix (GP4/GP5 omitted — not broken out on this board):

| | SPI0 | SPI1 |
|---|------|------|
| SCK | GP2, GP6, GP18 | GP10, GP14 |
| MOSI (TX) | GP3, GP7, GP19 | GP11, GP15 |
| MISO (RX) | GP0, GP16 | GP8, GP12 |

The 2-fader prototype puts motor control on **GP2/GP3/GP6**, which collides
with SPI0's only usable SCK/MOSI options. Motor control must move off those
pins. Cleanest fix: run the bus on **SPI0 at GP18 (SCK) / GP19 (MOSI) /
GP16 (MISO)**, inside the otherwise lightly used GP16-22 block.

CS lines are ordinary GPIO — not fixed — so they go wherever is left.

**PWM outputs want separate slices.** GPn maps to PWM slice `(n >> 1) & 7`, and
two outputs on the same slice share a frequency. The 4 PWM pins (PWMA/PWMB on
each driver) should land on 4 different slices. Eight slices exist, so this is
easy — but it constrains the map rather than being discovered after layout.

---

## Pin Map

Validated against both hardware constraints: SPI0 on legal pins, and the four
PWM outputs on four distinct slices.

| GPIO | Signal | Notes |
|------|--------|-------|
| GP2 | A_AIN1 | driver A, fader 1 direction |
| GP3 | A_AIN2 | driver A, fader 1 direction |
| GP6 | A_PWMA | fader 1 speed — PWM slice 3 |
| GP7 | A_BIN1 | driver A, fader 2 direction |
| GP8 | A_BIN2 | driver A, fader 2 direction |
| GP9 | A_PWMB | fader 2 speed — PWM slice 4 |
| GP10 | B_AIN1 | driver B, fader 3 direction |
| GP11 | B_AIN2 | driver B, fader 3 direction |
| GP12 | B_PWMA | fader 3 speed — PWM slice 6 |
| GP13 | B_BIN1 | driver B, fader 4 direction |
| GP14 | B_BIN2 | driver B, fader 4 direction |
| GP15 | B_PWMB | fader 4 speed — PWM slice 7 |
| GP16 | SPI0 MISO | from MCP3208 only |
| GP17 | MCP3208 CS | |
| GP18 | SPI0 SCK | shared: ADC + 4 displays |
| GP19 | SPI0 MOSI | shared: ADC + 4 displays |
| GP20 | DISP_DC | shared across all 4 displays |
| GP21 | DISP_CS1 | |
| GP22 | STBY | both drivers |
| GP26 | DISP_CS2 | |
| GP27 | DISP_CS3 | |
| GP28 | DISP_CS4 | |

**22 assigned. GP0 and GP1 free** — reserved for I2C (SDA/SCL) if a mux or the
HUSB238 status read is ever added. GP4/GP5 are not broken out on this board.

### Why these pins

**SPI0 is forced.** The RP2040's SPI mapping is fixed silicon. Of SPI0's
options — SCK on GP2/6/18, MOSI on GP3/7/19, MISO on GP0/16 — only the GP18/19
pair leaves the low pins free for motor control. MISO takes GP16 so GP0/GP1
stay open for I2C.

**PWM slices are distinct.** Slice = `(n >> 1) & 7`; two outputs on one slice
share a frequency. GP6/9/12/15 land on slices 3/4/6/7.

**GP26-28 carry display CS, not analog.** They are the only ADC-capable pins,
but all four faders read through the MCP3208, so their analog function is
unused. CS is plain GPIO and fits there fine.

### Not on the MCU

| Signal | Where |
|--------|-------|
| Fader wipers x4 | MCP3208 CH0-CH3 |
| General buttons (2) | MCP3208 CH6 ladder |
| Per-fader buttons (4) | MCP3208 CH7 ladder |
| VM rail check | MCP3208 CH4 divider |
| MCP3208 CH5 | spare |
| Display RES | RC power-on reset, no GPIO |

---

## Mechanical

### Faders — RSA0N11M9, 100 mm travel

**Note the part change:** the prototype used RS60N11M9 (60 mm, 5 kΩ). The
current faders are **RSA0N11M9: 100 mm travel, 10 kΩ**, roughly 130 mm overall.
Motor spec is unchanged — 10 V DC rated, 800 mA max — so nothing in the driver
or power design moves.

The 10 kΩ element halves wiper current versus 5 kΩ, which slightly *improves*
the ratiometric ADC path. Source impedance rises, but 10 kΩ against the
MCP3208's sample-and-hold is still fine at the rates used here.

Alps lists RSA0N11M9 as **"Not Recommended for New Designs"** — it remains in
production for existing designs, but buy spares rather than assuming future
availability.

**Mounting:** M3 screws, 4 mm, into the 3D-printed case — already proven in the
prototype. Faders are **not board-mounted**: they mount to the case and connect
by soldered wire, so the PCB carries no fader mechanical load and its outline is
not driven by fader pitch.

### Wiring — soldered, not connectored

Faders connect by soldered wire directly to PCB pads. **Five conductors per
fader, 20 total:**

| Net | To | Notes |
|-----|----|-------|
| Wiper | MCP3208 CHn via 10 kΩ RC filter | keep away from motor leads |
| 3.3 V | fader terminal 3 | same net as MCP3208 VREF — ratiometric |
| GND | fader terminal 1 | |
| Motor + | TB6612FNG AO1 / BO1 | |
| Motor - | TB6612FNG AO2 / BO2 | |

Use **through-hole pads in a row, labelled on silkscreen**, per fader. Add a
strain-relief hole beside each group so the wire bundle can be zip-tied to the
board — soldered wires fail at the joint when flexed, and a fader that gets
moved during assembly will flex them.

**Route wiper wires away from motor wires.** The motor pair carries switched
current at 20 kHz; the wiper is a high-impedance analog line. Running them in
one bundle couples PWM straight into the ADC. Separate bundles if possible, and
twist each motor pair.

Buttons and displays likewise: through-hole pads, silkscreen-labelled.

### Test points

Through-hole pads or 1 mm loops, labelled:

| Test point | Why |
|------------|-----|
| VM | confirm 9 V (or 10 V on the bench) before enabling motors |
| 3.3 V | regulator sanity |
| GND | x2, spread apart — scope ground for probing |
| Wiper 1 | scope the analog path, verify RC filter effect |
| SPI SCK | confirm the bus is clocking |

Free on a PCB and the difference between diagnosing by measurement and by
guesswork.

### Board outline

Not yet fixed. Since faders are case-mounted, the PCB outline is free — driven
by component area and wherever the case has room, not by fader pitch. Include
**M3 mounting holes** (3.2 mm drill) at the corners, and a keepout past the
RP2040 module's header end for its USB connector.

---

## Bill of Materials

### ICs

| Part | Qty | Package | Note |
|------|-----|---------|------|
| TB6612FNG | 2 | SSOP-24 | 1 per 2 faders |
| MCP3208-CI/P | 1 | **DIP-16** | order the `/P`, not the `/SL` SOIC |
| RP2040 module (existing) | 1 | headers | GP4/GP5 not exposed |
| Female header, 20-pos, 2.54 mm | 2 | THT | RP2040 module |
| Female header, 8-pos, 2.54 mm | 4 | THT | driver breakouts, 2 per TB6612FNG |
| SSD1306 128x64 SPI | 4 | 7-pin module | one per fader |

### Passives — 0805 throughout

0805 rather than 0603: easier to place and drag-solder by hand, no real
downside at this density.

| Part | Qty | Purpose |
|------|-----|---------|
| 100 uF >=25 V electrolytic | 2 | VM bulk, one per TB6612FNG |
| 470 uF >=25 V electrolytic | 1 | 10 V rail input bulk |
| 100 nF X7R | ~13 | VM x2, VCC x2, MCP3208 VDD + VREF, MCU, per display, RES |

No separate 3.3 V or 5 V bulk capacitor: the Pico regulates 3.3 V on-module
with its own bulk, and there is no 5 V rail on this PCB — logic power comes
from the Pico's USB. 100 nF decoupling is sufficient at these currents.
| 10 kΩ | 9 | 2x STBY pull-up, 4x ADC filter, 2x ladder top, 1x ladder |
| 4.7 kΩ | 2 | button ladder (SW_G2, SW_F3) |
| 2.2 kΩ | 1 | button ladder (SW_F2) |
| 10 kΩ | 1 | VM divider, top leg (counted in the 9 above) |
| 3.3 kΩ | 1 | VM divider, bottom leg |
| 10 kΩ + 100 nF | 1 ea | display RES, RC power-on reset (frees a GPIO) |
| 10 nF X7R | 4 | RC filter on each wiper |
| 100 nF | 2 | button ladder debounce |

### Wiper RC filter — high value, near-free

**10 kΩ series + 10 nF to GND on each wiper**, at the ADC input. Corner is
~1.6 kHz: roughly 22 dB of attenuation at the 20 kHz `PWM_FREQ`, while staying
far above fader mechanical bandwidth.

The series resistor must stay well below the MCP3208's sample-and-hold input
requirement — 10 kΩ against the fader's own 10 kΩ wiper impedance settles fine
at the rates used here. Do not scale R up further to shrink C.

X7R rather than C0G: **100 nF C0G does not exist in 0805** (C0G tops out near
10 nF in that size), so the filter uses 10 nF C0G-grade X7R with a
correspondingly larger resistor to hit the same corner frequency.

This filters motor PWM coupling before it reaches the ADC and should visibly
reduce the jitter that the 16x averaging currently masks — possibly allowing
that averaging to be reduced, buying loop rate back.

### Power

| Part | Qty | Note |
|------|-----|------|
| USB-C PD trigger (HUSB238) | 1 | jumper-set to 9 V, feeds VM only |
| USB-C PD charger, >=45 W | 1 | must offer a 9 V PDO; 4 motors x 800 mA = 3.2 A peak |
| 10 kΩ + 3.3 kΩ | 1 ea | VM divider into MCP3208 CH4 for rail verification |

Logic power comes from the Pico's own USB-C to the PC — no buck converter,
barrel jack or fuse needed. PD boards current-limit at the source.

### Mechanical / connectors

| Part | Qty |
|------|-----|
| Alps RSA0N11M9 fader (100 mm, 10 kΩ) | 4 |
| Tactile button (per-fader) | 4 |
| Tactile button (general) | 2 |
| Display headers, 7-pin | 4 |
| Fader wire pads, 5 per fader | 20 nets | soldered wire, no connectors |

---

## Power

| Rail | Source | Feeds |
|------|--------|-------|
| 5 V logic | Pico USB-C from the PC | RP2040 module |
| 3.3 V | Pico onboard regulator | fader wipers, TB6612FNG VCC, MCP3208, displays |
| 9 V motor | USB-C PD trigger board | TB6612FNG VM only |

3.3 V draw is negligible: TB6612FNG VCC is ~1 mA each and four faders at 3.3 V
into 10 kΩ is ~1.3 mA total, against the Pico regulator's 300 mA.

**Common ground is mandatory.** With two independent USB sources, the PD
ground and the Pico's USB ground must be tied together on the PCB. Without it
the TB6612FNG's logic inputs have no valid reference to its motor rail.

### 9 V, not 10 V — USB PD has no 10 V PDO

The Alps RSA0N11M9 motor is rated **10 V DC, 800 mA max**. USB PD fixed PDOs
are 5 / 9 / 12 / 15 / 20 V — **10 V does not exist** in the spec. 12 V would be
20% over rated on a continuously driven part; 9 V is 10% under, which is the
safe direction to miss.

Exactly 10.0 V is reachable only through **PPS** (USB PD 3.0, 20 mV steps), and
only over I2C — PPS is a different request type, not a jumper-selectable
voltage. That requires a PPS-capable charger and 2 GPIO, for a 10% torque
difference that firmware can absorb. Not worth it.

**Firmware compensation for 9 V** — both are existing tuning constants:

| Constant | 10 V | 9 V |
|----------|------|-----|
| `MIN_MOVE_PCT` | 30.0 | ~35 |
| `calibrate(motor_power=)` | 60 | ~70 |

PID output clamps at +/-100% and currently floors at 30%, so there is ample
unused band. Long travels are marginally slower; short moves dominate real use.

### Trigger board: HUSB238, jumper-set to 9 V

Adafruit HUSB238 breakout (product 5807). Few pins, mountable at the case's
USB-C cutout, wired back with 2 wires. Solder the **9 V jumper** closed.

**Set the voltage by jumper even if I2C is added later.** Adafruit's docs:
*"when configuring over I2C, the jumper settings are used on startup until the
I2C commands come over."* I2C overrides the jumper, it does not replace it. So
with a jumper left at the 5 V default, VM sits at 5 V from PD plug-in until
firmware speaks — and if the board is powered before the Pico, VM steps
5 V -> 9 V while the TB6612FNG is already live. Worse, the STBY pull-up goes to
3.3 V *from the Pico*, which is absent during that window, so STBY is genuinely
undefined with a motor rail up.

Jumper-setting the target eliminates the window entirely.

### Silent fallback — and how to catch it

If the charger cannot supply the requested PDO, the HUSB238 **falls back
silently**: it walks the source PDO list high-to-low and takes the first match,
landing on 5 V. No error, no indication.

At the bench this looks like a mechanical fault — faders crawl or stall, PID
integral winds to `INTEGRAL_MAX`, output saturates, `MIN_MOVE_PCT` sits below
stall. Identical symptoms to a stiff fader or a bad motor solder joint, so the
wrong layer gets debugged.

**Catch it with a divider into a spare ADC channel** — MCP3208 CH4 is free
after the two button ladders, so this costs **zero GPIO**:

```
VM (9 V) --[10k]--+--------> MCP3208 CH4
                  |
                [3.3k]
                  |
                 GND
```

| VM | CH4 reads |
|----|-----------|
| 5 V (fallback) | 1.24 V |
| 9 V (expected) | 2.23 V |
| 12 V (wrong jumper) | 2.98 V |

Widely separated and all inside the 3.3 V range. Firmware reads the rail at
boot and gates on it rather than assuming:

```
boot -> read CH4
     -> VM in expected band? -> STBY high, calibrate()
     -> else                 -> STBY stays low, emit error, skip calibration
```

Same principle as the `STATE:` protocol line — gate on a reported state, never
on an assumption. This also catches a sagging rail or a marginal cable, which
an I2C status read would not: the divider measures the actual rail, whereas
I2C only reports what was negotiated.

### Why not I2C to the HUSB238

It fits, but only just, and it buys less than the divider:

| | I2C route | Divider route |
|---|---|---|
| GPIO cost | 2 | **0** |
| Spare pins after | 0 | 1 |
| Detects 5 V fallback | yes | yes |
| Measures actual VM | no | **yes** |
| Catches sagging rail / bad cable | no | **yes** |
| Extra parts | none | 2 resistors |

The 2 GPIO are only available by first freeing the display RES pin (below).
Since the voltage is jumper-set anyway, I2C's remaining role is status
readback — which the divider does more directly.

### Reverse-polarity protection — one part, worth fitting

Not needed for the PD path (USB-C cannot be reversed), but the **bench supply
used during bring-up can be**, and that is the higher-risk phase. One part:

**P-channel MOSFET in the VM high side.** Source to supply +, drain to VM, gate
to GND through a 100 kΩ resistor. Correct polarity turns it on with a few tens
of milliohms drop; reversed, it stays off and nothing downstream sees voltage.
Better than a series diode, which would burn ~0.5 V and a watt at 3.2 A.

Sizing: needs Vds >= 20 V, Id >= 5 A, low Rds(on). Plenty of choices in SOT-223
or DPAK around CHF 0.50. An alternative is a **Schottky across VM reverse-biased
plus a fuse** — cheaper still, but it protects by blowing the fuse rather than
by staying off.

At 1.5 A bench limit the risk is modest; at the 10 A the supply can deliver, a
reversed lead into 470 uF and two H-bridges is destructive. Fit the FET.

### Free a GPIO: RC reset on display RES

SSD1306 reset is a power-on pulse: low briefly at startup, high forever after.
Firmware never asserts it again, so it does not need a GPIO. Use an **RC
reset** on the shared RES net — 10 kΩ to 3.3 V, 100 nF to GND — which generates
the pulse passively.

This drops the GPIO total from 23 to **22**, leaving 2 spare. One resistor and
one capacitor, both already in the BOM, shared across all four displays.

---

## Assembly

Everything is hand-solderable. **No hot air station or reflow needed.**

| Part | Difficulty |
|------|-----------|
| MCP3208 DIP-16 | trivial |
| Passives 0805 | easy |
| RP2040 module, displays, buttons, connectors | easy (THT / headers) |
| TB6612FNG SSOP-24, 0.65 mm pitch | the only fine-pitch part |

**SSOP-24 technique:** drag-soldering, no hot air. Tack one corner pin, check
alignment, tack the opposite corner, then drag a well-loaded iron tip along
each side. Bridges are expected — clear with solder wick. Needs flux (a flux
pen suffices) and ideally a fine chisel tip.

Fine-pitch soldering is **deferred, not required**: the board carries both a
SSOP-24 land pattern and breakout headers at each driver position, so the first
build can use socketed breakout boards. See
[Dual Footprints](#dual-footprints--socketed-test-build).

---

## Dual Footprints — Socketed Test Build

The board carries **two footprints for every driver position, wired to the same
nets**: the SSOP-24 land pattern for the bare TB6612FNG, and a 0.1in header
pair for a SparkFun-style breakout board.

**Populate one or the other, never both.** This is standard practice and costs
only board area. It allows the whole system — firmware, PID tuning, 4 faders,
displays, buttons — to be proven with zero fine-pitch soldering, then converted
to bare ICs later without a board respin.

Do the same for the MCP3208: it is DIP-16, so fit a **socket** rather than
soldering it down. This protects a CHF 2.23 part from rework heat and allows
swapping it if an input is damaged during bring-up.

### Build order

1. **Test build** — socket the RP2040 module, plug in TB6612FNG breakouts, seat
   the MCP3208 in its DIP socket. Solder passives, buttons, connectors.
2. Bring up and tune against all 4 faders. Everything mechanical and all
   firmware work happens here.
3. **Final build** (optional) — remove the breakouts, drag-solder bare
   TB6612FNG into the SSOP-24 lands on the same board.

### Layout rules

- Every signal net reaches **both** footprints. The SSOP land and the
  corresponding header pin are the same net, so the netlist is unchanged; only
  the physical pads differ.
- Place the SSOP-24 land **inside or beside** the header outline so the
  breakout's body does not sit over the land pattern's solder mask. A breakout
  in the socket must not physically block later access to the SSOP pads.
- Keep the VM bulk capacitor on the **main PCB**, not relying on the breakout's
  own caps. Those sit on the far side of the socket contacts — the wrong side
  of the connection.
- Keep VM/PGND copper pours continuous under both footprints.

### Electrical caveat while socketed

Motor current flows through the header contacts. The Sullins PPTC parts are
rated **3 A**, comfortably above the 800 mA per motor — but socket contacts add
resistance and are a weaker path than solder.

Treat socketed operation as the **test configuration, not the final one**. It
works, but soldered SSOP with proper copper pours is the better long-term motor
path.

### Footprint measurements — taken

Both boards were measured directly rather than trusted to datasheets, since
neither is a reference design. Both came back Pico-standard:

**RP2040 module (RP2040+RTL8720 clone).** Row spacing is **identical to a stock
Pico**: 17.78 mm (7 x 0.1in) between the two 20-pin rows, so a standard KiCad
Pico footprint fits. The board runs slightly longer than the 20-pin rows with
only minor USB overhang — a small keepout past the header end is enough.

**TB6612FNG breakout.** The SparkFun board is 27 x 19 x 3 mm with pins on two
0.1in headers, inputs one side and outputs the other. Measured from the board
in hand: **8 pins per side (16 total)**, row spacing **identical to the Pico's
17.78 mm**. SparkFun's hookup guide states pin functions but not the physical
row layout, hence measuring.

Both footprints can therefore use standard 2.54 mm / 17.78 mm geometry.

---

## Future: I2C Expansion (TCA9548A / PCA9548A)

Not needed for this design — recorded because the 2 spare GPIO make it
available, and because it is the natural escape hatch if the display route
changes.

An 8-channel I2C switch lets multiple devices with **identical fixed addresses**
share one bus. Mux address is 0x70 (0x70-0x77 via A0/A1/A2), and a channel is
selected by writing a one-byte mask before each transaction.

**Where it would be used here:**

- **I2C displays instead of SPI.** SSD1306 has only two selectable addresses
  (0x3C / 0x3D), so four on one bus is impossible without a mux. Costs 2 GPIO
  total instead of 4 CS lines — a net gain of 2 pins over the SPI route.
- **Any further I2C devices** once SDA/SCL exist — an I/O expander for more
  buttons, sensors, a second fader bank.

**Sourcing note:** the usual `TCA9548APWR` is **out of stock at DigiKey until
December 2026**. The `PCA9548APWR` is the pin-compatible NXP-origin equivalent,
in stock, and DigiKey lists the two as direct substitutes. Use the PCA unless
the TCA is back.

Both are **TSSOP-24 at 0.65 mm pitch** — the same pitch as the TB6612FNG, so no
new soldering capability is needed. A breakout module is the alternative
(Bastelgarage stocks one at CHF 5.90, though it was out of stock when checked).

If the board is ever laid out with a mux in mind: it needs the two I2C nets,
100 nF decoupling, and pull-ups (4.7 kΩ to 3.3 V on SDA and SCL) — values
already in the BOM.

---

## Sourcing — DigiKey Switzerland

All part numbers verified in stock on digikey.ch. Free shipping over CHF 70;
below that, CHF 23. Ships from Germany, typically ~48 h.

No Swiss hobby shop (Bastelgarage, Pi-Shop, Play-Zone) carries the bare
MCP3208, the SSOP TB6612FNG, or an SPI SSD1306 — they stock modules, not ICs.
Distrelec (Zurich) may carry the ICs but blocks automated lookup; worth a
manual check for the three semiconductors if buying Swiss matters.

### Semiconductors

| Part | DigiKey / MPN | Qty | CHF ea |
|------|---------------|-----|--------|
| ADC, 12-bit 8ch SPI, DIP-16 | `MCP3208-CI/P` | 2 | 2.23 |
| Motor driver, SSOP-24 | `TB6612FNG,C,8,EL` | 3 | 1.64 |

Buy one spare of each — both are the parts a wiring mistake kills.

### Capacitors

| Purpose | MPN | Qty | CHF ea | Notes |
|---------|-----|-----|--------|-------|
| VM bulk (per driver) | `EEU-FR1E101B` | 2 | 0.38 | 100 uF 25 V, 130 mOhm ESR, 6.3x12.7 mm, 5 mm pitch |
| 10 V rail input bulk | `EEU-FR1E471` | 1 | 0.85 | 470 uF 25 V, 43 mOhm ESR, 10x14 mm, 5 mm pitch |
| Decoupling, everywhere | `CL21B104KBCNNNC` | 25 | 0.08 | 100 nF 50 V X7R 0805 |
| Wiper RC filter | `CL21B103KBANNNC` | 10 | ~0.08 | 10 nF 50 V X7R 0805 |

**Why these electrolytics:** Panasonic FR series is low-ESR and 105 C rated.
ESR is the spec that matters for a motor rail — it sets how well the cap
absorbs the current spikes when the H-bridge switches. A general-purpose 105 C
cap at ~1 ohm ESR would not do the job. Both are 5 mm lead pitch, 25 V for
headroom over the 10 V rail.

Order 100 nF in quantity — at 0.08 CHF they are consumable, and the design
uses ~12 with more wanted for rework.

### Resistors — 0805, 1%

Yageo RC0805 series, pattern `RC0805FR-07<value>L`:

| Value | MPN | Per board | Purpose |
|-------|-----|-----------|---------|
| 10 kΩ | `RC0805FR-0710KL` | 9 | 2x STBY pull-up, 4x ADC filter, 2x ladder top, 1x ladder |
| 4.7 kΩ | `RC0805FR-074K7L` | 2 | button ladder (general SW_G2, per-fader SW_F3) |
| 2.2 kΩ | `RC0805FR-072K2L` | 1 | button ladder (per-fader SW_F2) |

The 4.7 kΩ and 2.2 kΩ come from the **2 general + 4 per-fader** ladder split;
the earlier 3.3 kΩ belonged to the abandoned 3+3 grouping and is not needed.

Ladder values are not critical — the bands are 600 mV or wider against a 0.8 mV
LSB, so anything close works. Substitute from stock rather than reordering for
these two lines alone.

A 0805 resistor assortment book is often better value than reels if you do not
have one — it covers rework and future revisions.

### Tactile buttons — 3D-printed cover

**`B3F-4050`** (Omron/Aratas), 0.42 CHF, 22,666 in stock. Qty 6.

| Spec | Value |
|------|-------|
| Body | 12.0 x 12.0 mm |
| Plunger height above PCB | 7.30 mm |
| Operating force | 130 gf |
| Travel | 0.30 mm |
| Life | 3,000,000 cycles |
| Mount | through-hole, 4 pin |

This is the right part for a printed cover, specifically because of the
**7.30 mm plunger**: the tall stem gives clearance for a front panel of real
thickness, so the printed cap sits on the plunger rather than needing the panel
to be paper-thin. The 12x12 mm body is also large enough to print a stable cap
for without it rocking.

**Force choice:** the near-identical `B3F-4055` is the same body and stem at
**260 gf** — twice the force. For faders you tap while mixing, 130 gf is the
better feel; 260 gf is for gloved or anti-accidental use. Do not order 4055 by
mistake, the part numbers differ by one digit.

Omron B32 series key tops fit these if a printed cap does not work out.

### Displays — still open

No cheap generic SPI SSD1306 at DigiKey. Either:

- **`1528-1447-ND`** (Adafruit 326), ~CHF 16 ea — 0.96in 128x64 SSD1306,
  I2C **or** SPI selectable by cutting a jumper on the back. 4x = ~CHF 64.
- **Generic 7-pin SPI SSD1306** from AliExpress, ~CHF 3-4 ea, 2-4 week ship.
  These are exactly the 7-pin modules this design assumes, no jumper cutting.

### Sockets and headers

| Part | MPN | Qty | CHF ea |
|------|-----|-----|--------|
| Female header, 20-pos, 1 row (RP2040) | `PPTC201LFBN-RC` (S7018-ND) | 2 | 0.92 |
| Female header, 8-pos, 1 row (drivers) | `PPTC081LFBN-RC` (S7006-ND) | 4 | ~0.45 |

Sullins PPTC series: 2.54 mm pitch, through-hole, 8.51 mm insulator height,
tin-plated, 3 A rated. 16,583 in stock.

Two 20-position strips socket the RP2040 module. The SparkFun TB6612FNG
breakout has **8 pins per side (16 total)** — measured, not from the datasheet —
so each driver position takes two 8-position strips, four per board.

Buy 8-position rather than cutting down 20s: a 20 yields only two clean 8s
(one position is lost at each cut), so cutting costs more strip and wastes a
third of each. The same family covers other lengths if a revision needs them:
`PPTC061LFBN-RC` (6), `PPTC101LFBN-RC` (10) — identical pitch and height, so
everything seats at a consistent level.

**The MCP3208 does not need a socket.** DIP-16 is the easiest part on the board
to desolder if one is ever damaged, and spares are cheap. Solder it directly.
If a socket is wanted for the bring-up board anyway, DigiKey files these under
*IC Sockets*, not headers: `A 16-LC-TT` (AE9992-ND), CHF 1.01, 16-pos 2x8,
0.3in row spacing.

**Lead time:** these carry an 11-week manufacturer lead time. Irrelevant while
stock lasts, but do not leave them off an order assuming they can be added
later.

### Power

| Part | MPN / DK# | Qty | CHF ea |
|------|-----------|-----|--------|
| USB-C PD trigger, jumper or I2C | Adafruit **5807** (1528-5807-ND) | 1 | 4.77 |
| VM divider, top leg | `RC0805FR-0710KL` | 1 | 0.014 |
| VM divider, bottom leg | `RC0805FR-073K3L` | 1 | 0.018 |

The divider uses 10 kΩ and 3.3 kΩ — **both already in the basket**, so the rail
check adds no new line item. (The 3.3 kΩ was left over from the abandoned 3+3
button ladder; this gives it a purpose.)

**In stock at DigiKey** (2,256 units), so this stays a single order.

Order **5807** specifically, not 5991 — both use the HUSB238, but 5807 is the
solder-jumper version this design needs: *"cut the 5V jumper and solder close
the 9V, 12V, 15V, 18V or 20V jumper"*. I2C remains available on the same board
if it is ever wanted.

### Panel-mounting the USB ports

Both USB-C ports (Pico and HUSB238) sit on modules inside the case, so each
needs to reach a panel cutout.

**Recommended: short USB-C extension cables**, male-to-female, 0.3-0.5 m, run
from the module to a panel cutout and secured with a printed bracket or
P-clip. Cheap, no special parts, and the printed case can hold the female end
directly.

**Feedthrough connectors exist but are poor value here.** Cliff's DUALSLIM
range (e.g. `CP30701MB3`, 4654-CP30701MB3-ND) is a proper panel-mount USB-C
receptacle-to-receptacle feedthrough with a screw flange — but it is
**CHF 16.53 each and out of stock** at DigiKey, with a 6-week lead time. Two of
them would cost more than every semiconductor in this design.

Since the case is 3D printed, a cutout sized to a cable's moulded shell plus a
printed retaining clip does the same job for nothing. If PD current matters,
check the extension is rated for it — many cheap USB-C cables are 3 A, which
covers 9 V x 3 A = 27 W comfortably.

### Future expansion — not needed now

| Part | MPN / DK# | CHF ea | Note |
|------|-----------|--------|------|
| I2C 8-ch switch | `PCA9548APWR` (296-21775-1-ND) | 1.46 | **in stock**, 13,910 |
| I2C 8-ch switch (alt) | `TCA9548APWR` (296-34905-1-ND) | 1.19 | **out of stock** until Dec 2026 |

Pin-compatible; DigiKey lists them as direct substitutes. TSSOP-24, 0.65 mm —
same pitch as the TB6612FNG. Only needed if the design moves to I2C displays or
gains further I2C devices.

### Not from DigiKey

Cheaper at Bastelgarage or similar: pin headers, and a TCA9548A breakout module
(CHF 5.90) if a mux is ever wanted without TSSOP soldering.

**No longer needed:** the bench PSU, barrel jack, fuse holder and 10 V/5 A
supply are all replaced by the USB-C PD trigger board. A buck converter is only
required if the 12 V-plus-buck route is taken instead of jumper-set 9 V.

If breadboarding the TB6612FNG before committing to a PCB, add 2x SSOP-24 to
DIP adapter boards (~CHF 2 ea).

### Basket total

Semiconductors + passives + buttons + headers lands around CHF 28. With 4x Adafruit 326
it clears the CHF 70 free-shipping threshold; without displays it does not, so
either add the displays, stock up on passives, or accept the CHF 23 shipping.

---

## Open Items

- Fix the board outline and M3 hole positions once the case layout is decided.
  Faders are case-mounted, so the PCB outline is otherwise unconstrained.
- Pick a reverse-polarity P-FET (Vds >= 20 V, Id >= 5 A, SOT-223/DPAK) if
  fitting one — not yet in the basket.
- Firmware, deferred: MCP3208 driver, `FaderPID` ADC injection, button-ladder
  decode, VM rail check at boot, display output.
