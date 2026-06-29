# Modular Fader System — Design Plan

This document describes how to evolve the single-Pico prototype into a modular, expandable system using RP2040-Zero modules and magnetic connectors.

---

## Goal

Each module contains:
- 2 motorized faders (Alps RS60N11M9)
- 2 small OLED screens (SSD1306, 128×64)
- Independent motor driver

Modules connect together magnetically and can be added or removed without tools. The Pico W acts as the master controller, bridging USB/WiFi on one side and the module UART chain on the other.

---

## Controller Choice: RP2040-Zero

The **RP2040-Zero** (Waveshare) is the same RP2040 chip as the Pico, in a compact 23×18mm form factor. It is the natural choice for modules because:

- **4 ADC channels** — covers 2 fader wipers with no external ADC IC
- **30 GPIO pins** — plenty for motor driver, touch, I2C, UART
- **5V tolerant VIN** — onboard 3.3V regulator, powered directly from the 5V bus
- **Same MicroPython** as the Pico — the `main.py` firmware ports with pin number changes only
- **No WiFi radio** — no ADC noise, no unnecessary complexity (WiFi not needed; Pico W is the network bridge)
- **~€3–4** — cheaper than ESP32 or D1 Mini

### GPIO Assignment (RP2040-Zero)

| GPIO | Signal |
|------|--------|
| GP26 / ADC0 | Fader 1 wiper |
| GP27 / ADC1 | Fader 2 wiper |
| GP2 | AIN1 (fader 1 direction) |
| GP3 | AIN2 (fader 1 direction) |
| GP4 | PWMA (fader 1 speed) |
| GP5 | BIN1 (fader 2 direction) |
| GP6 | BIN2 (fader 2 direction) |
| GP7 | PWMB (fader 2 speed) |
| GP9 | Touch 1 (external 1MΩ pull-down to GND; AC pickup when touched) |
| GP10 | Touch 2 (external 1MΩ pull-down to GND; AC pickup when touched) |
| GP20 | I2C0 SDA (2× SSD1306 OLED) |
| GP21 | I2C0 SCL |
| GP0 | UART0 TX (upstream toward master) |
| GP1 | UART0 RX (upstream toward master) |
| GP8 | UART1 TX (downstream toward next module) |
| GP9 | UART1 RX (downstream toward next module) |
| STBY | Tied HIGH via 10kΩ to 3.3V |

This is identical to the Pico firmware pin layout — porting is a matter of changing UART and I2C pin assignments only.

---

## Connector Choice: 5-pin magnetic, UART daisy-chain

```
Pin 1: 10V  — motor supply (VM on TB6612FNG)
Pin 2: 5V   — logic supply (RP2040-Zero VIN → onboard 3.3V LDO)
Pin 3: GND
Pin 4: TX
Pin 5: RX
```

Each module has two connectors: upstream (toward master) and downstream (toward next module). The TX/RX lines cross between connectors so that TX of one module connects to RX of the next.

**Why UART over I2C on the connector:**
- I2C has capacitance limits (~400pF) that restrict cable length between modules.
- UART works reliably at longer distances without bus repeaters.
- Full-duplex: modules can send position data without being polled.

---

## Communication Protocol

Line-based ASCII over UART (115200 baud). Each message ends with `\n`.

### Master → Module

```
MODULE:1:SET:50.0,75.0\n
```

- `1` is the module address (1-indexed).
- Each module acts if the address matches, then **forwards the packet downstream** unchanged.
- Module address set as a constant in firmware (or via a GPIO strap pin tied to GND/3.3V at boot).

### Module → Master

```
MODULE:1:POS:47.3,82.1\n
```

- Each module sends position upstream at ~20Hz.
- Intermediate modules forward packets they did not originate upstream.

### Additional commands (future)

```
MODULE:1:TUNE:0.8,0.05,0.02\n   -> live PID tuning
MODULE:*:SET:50.0,50.0\n         -> broadcast to all modules
MODULE:1:DISP:Vol\n              -> set OLED label text
```

---

## Power Bus Design

```
[10V PSU]──────────────────────────────────────── 10V rail ──► TB6612FNG VM
[10V PSU]──[buck converter 10V→5V]─────────────── 5V rail  ──► RP2040-Zero VIN

[Pico W]──5-pin connector──[Module 1]──5-pin connector──[Module 2]──► ...
```

One 10V supply powers the whole chain. A single **LM2596 or MP1584 buck module** steps it down to 5V for the logic rail — one converter for the entire chain, not per module.

**Per-module power budget (peak):**

| Component | Current (10V rail) | Current (5V rail) |
|-----------|-------------------|-------------------|
| 2× fader motor (800mA max each) | up to 1600 mA | — |
| RP2040-Zero | — | ~50 mA |
| 2× SSD1306 OLED | — | 20 mA |
| **Total peak** | **1600 mA** | **70 mA** |

Normal PID operation: motors rarely draw more than 200–300mA each. A **10V/3A supply** handles 2 modules; **10V/5A** covers 4+ modules.

The TB6612FNG is rated 1A continuous / 3A peak per channel at up to 13.5V — handles 10V and 800mA comfortably.

---

## Components Needed Per Module

| Component | Qty | Notes |
|-----------|-----|-------|
| RP2040-Zero | 1 | Main controller |
| TB6612FNG | 1 | Dual motor driver |
| SSD1306 OLED (128×64, I2C) | 2 | One per fader |
| 100µF electrolytic cap | 1 | 10V motor rail decoupling |
| 100nF ceramic cap | 4 | Per-IC supply decoupling |
| 10kΩ resistor | 1 | STBY pull-up to 3.3V |
| 1MΩ resistor | 2 | Touch pull-down to GND (one per fader T pin) |
| 5-pin magnetic connector | 2 | Upstream + downstream |

No external ADC, no touch IC, no LDO — the RP2040-Zero handles everything natively. The 1MΩ pull-downs on the T pins are the only passives required for the touch sense to work (see the README touch-wiring note).

---

## Master Role (Pico W)

```
PC ──USB serial──► Pico W ──UART──► Module 1 ──UART──► Module 2 ──► ...
     (SET/POS protocol)            (MODULE:N: protocol)
```

The Pico W translates the existing `SET:f1,f2\n` / `POS:f1,f2\n` PC protocol into the `MODULE:N:SET/POS` chain protocol. `host.py` needs only minor changes to address multiple modules by index.

The Pico W's second UART (GP4/GP5 on a genuine Pico W; remap to e.g. GP8/GP9 on the current RP2040+RTL8720 board, which doesn't expose GP4/GP5) drives the module chain; its USB serial continues talking to the PC.

Optionally, the Pico W can expose a WiFi TCP server instead of USB, removing the cable entirely.

---

## Physical Design Notes

- **PCB size**: RP2040-Zero (23×18mm) is compact enough for a module PCB alongside two faders (~60mm travel each) and two small OLEDs.
- **Connector placement**: one 5-pin magnetic connector at each end of the PCB. TX/RX cross on the PCB traces so the connector pinout is symmetric (same physical connector on both ends).
- **OLED placement**: one SSD1306 above each fader, showing current position and target.
- **I2C addresses**: SSD1306 #1 = 0x3C (ADDR→GND), SSD1306 #2 = 0x3D (ADDR→VCC). Both on the same I2C bus.

---

## Development Sequence

1. **Port `pico/main.py` to RP2040-Zero** — change UART/I2C pin numbers, remove WiFi disable call. The PID logic is identical.
2. **Test single module** with one fader, verify ADC and PID work.
3. **Add OLED output** — display position and setpoint on each SSD1306.
4. **Add UART passthrough** — module relays unknown packets downstream/upstream.
5. **Test 2-module chain** with Pico W as master.
6. **Design PCB** in KiCad — RP2040-Zero footprint, TB6612FNG, 2× SSD1306 headers, 5-pin connector footprints at each end.
7. **Order and test** before scaling to more modules.
