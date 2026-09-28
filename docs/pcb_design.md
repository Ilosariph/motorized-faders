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
| MCU | Keep the existing RP2040 module — bare RP2040 not needed |
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

---

## Buttons: resistor ladder on spare ADC channels

6 buttons (4 per-fader + 2 general) on 2 spare MCP3208 channels, 3 per channel.
Costs **zero GPIO**.

```
3.3V --[10k]--+--------------------> MCP3208 CH6
              +--[SW1]--[GND]            pressed: 0.00 V
              +--[SW2]--[3.3k]--[GND]    pressed: 0.82 V
              +--[SW3]--[10k]---[GND]    pressed: 1.65 V
                                         idle:    3.30 V
```

Four bands ~800 mV apart — trivially discriminated at 12 bits. Add 100 nF to
GND at the node for debounce. Buttons are slow, so ADC-rate polling is ample.

Simultaneous presses on one channel read as the lowest-value button. If true
multi-press is wanted on the 2 general buttons, put those two on their own
channel (2 buttons = 3 bands, more margin) and the 4 per-fader buttons on the
other.

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
| Display DC + RES (shared across all 4) | 2 |
| Display CS x 4 | 4 |
| **Total** | **23** |

One pin spare.

**The SPI bus is shared** between the MCP3208 and the four displays. This is
what makes the budget close — a separate ADC bus would need 2 more pins and put
the total at 25, over budget. It is safe because the displays are write-only
(no MISO) so only the MCP3208 ever drives MISO; devices are selected by CS.

Run the bus at ~1 MHz (safe for the MCP3208 at 3.3 V; the SSD1306 tolerates it
easily). A full 128x64 frame is 1 KB, ~8 ms per redraw — redraw on change only,
never every loop, and PID timing is unaffected.

**Touch sense is not in this budget.** The T terminal is unused; move
completion comes from the PID state machine (deadband + settle timer).

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
| 10 uF ceramic | 2 | 3.3 V and 5 V rail bulk |
| 100 nF X7R | ~12 | VM x2, VCC x2, MCP3208 VDD + VREF, MCU, per display |
| 10 kΩ | 2 | STBY pull-ups |
| 10 kΩ | 2 | button ladder top resistors |
| 3.3 kΩ | 2 | button ladder |
| 10 kΩ | 2 | button ladder |
| 10 kΩ | 4 | series into each ADC input (RC filter) |
| 10 nF X7R | 4 | RC filter on each wiper |
| 100 nF | 2 | button ladder debounce |

### Wiper RC filter — high value, near-free

**10 kΩ series + 10 nF to GND on each wiper**, at the ADC input. Corner is
~1.6 kHz: roughly 22 dB of attenuation at the 20 kHz `PWM_FREQ`, while staying
far above fader mechanical bandwidth.

The series resistor must stay well below the MCP3208's sample-and-hold input
requirement — 10 kΩ against the fader's own 5 kΩ wiper impedance settles fine
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
| 10 V / 5 A supply | 1 | 4 motors x 800 mA peak = 3.2 A + headroom |
| Buck 10 V -> 5 V (MP1584 / LM2596) | 1 | logic rail |
| Barrel jack | 1 | |
| Reverse-polarity protection | 1 | SS34 series diode or P-FET |
| Fuse, 5 A | 1 | on the 10 V input |

### Mechanical / connectors

| Part | Qty |
|------|-----|
| Alps RS60N11M9 fader | 4 |
| Tactile button (per-fader) | 4 |
| Tactile button (general) | 2 |
| Display headers, 7-pin | 4 |
| Motor + fader terminals (JST or screw) | as needed |

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

### Measure before laying out footprints

Two measurements are **required** and cannot be taken from any datasheet here,
because the boards in hand are not reference designs:

**RP2040 module (RP2040+RTL8720 clone).** Row spacing is **confirmed identical
to a stock Pico**: 17.78 mm (7 x 0.1in) between the two 20-pin rows, so a
standard KiCad Pico footprint fits. Still worth checking total board length and
whether the USB connector overhangs the end — a keepout is needed there so a
tall capacitor does not foul it.

**TB6612FNG breakout.** The SparkFun board is 27 x 19 x 3 mm with pins on two
0.1in headers, inputs one side and outputs the other. Pin count is **8 per side,
16 total** — measured from the board in hand, since SparkFun's hookup guide
states pin functions but not the physical row layout. Row spacing still needs
measuring before drawing the footprint.

Getting either wrong is unfixable after fab.

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

| Value | MPN | Qty | Purpose |
|-------|-----|-----|---------|
| 10 kΩ | `RC0805FR-0710KL` | 25 | STBY pull-ups, button ladder, ADC filter |
| 3.3 kΩ | `RC0805FR-073K3L` | 10 | button ladder |

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

### Not from DigiKey

Cheaper at Bastelgarage or similar: buck converter (MP1584/LM2596, ~CHF 5),
10 V/5 A supply, barrel jack, fuse holder, pin headers.

If breadboarding the TB6612FNG before committing to a PCB, add 2x SSOP-24 to
DIP adapter boards (~CHF 2 ea).

### Basket total

Semiconductors + passives + buttons + headers lands around CHF 28. With 4x Adafruit 326
it clears the CHF 70 free-shipping threshold; without displays it does not, so
either add the displays, stock up on passives, or accept the CHF 23 shipping.

---

## Open Items

- Check RP2040 module board length and USB overhang for the keepout. Row
  spacing is confirmed as stock Pico (17.78 mm).
- **Measure the TB6612FNG breakout row spacing.** Pin count is confirmed at
  8 per side (16 total); the distance between the two rows still needs calipers.
- Confirm which SSD1306 variant is on hand (4-pin I2C vs 7-pin SPI) — design
  assumes SPI.
- Assign concrete GPIO numbers to the 23 nets above before schematic capture.
- Decide button grouping: 3+3, or 2 general + 4 per-fader.
