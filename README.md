# Motorized Fader Controller

RP2040 board (Pico-clone with RTL8720DN WiFi — GP4/GP5 not exposed) + TB6612FNG
motor drivers + Alps motorized faders.

**Two builds exist:**

| | 2-fader breadboard | 4-fader PCB |
|---|---|---|
| Faders | 2x RS60N11M9 (60 mm, 5 kΩ) | 4x RSA0N11M9 (100 mm, 10 kΩ) |
| ADC | RP2040 internal (GP26/27) | MCP3208, 12-bit SPI |
| Drivers | 1x TB6612FNG breakout | 2x TB6612FNG |
| Displays | none | 4x SSD1306 128x64 SPI |
| Buttons | none | 4 per-fader + 2 general, ADC ladder |
| Motor rail | 10 V bench supply | 9 V USB-C PD trigger |
| Docs | this file | [pcb_design.md](docs/pcb_design.md), [firmware.md](docs/firmware.md) |

The wiring below is the **2-fader breadboard** build. For the 4-fader PCB see
[docs/pcb_design.md](docs/pcb_design.md) (hardware) and
[docs/firmware.md](docs/firmware.md) (protocol, mute, displays, buttons).

Firmware in `pico/` is the **4-fader** version. It needs the MCP3208 and will
report `RAIL:fail` without the PD rail; the 2-fader build is kept in git
history.

---

## Wiring

### Power

| From | To | Notes |
|------|----|-------|
| 10V supply + | TB6612FNG VM | Motor power |
| 10V supply − | Common GND | |
| Pico 3.3V (pin 36) | TB6612FNG VCC | Logic power |
| Pico GND (any) | TB6612FNG GND | |
| Pico GND (any) | 10V supply − | Common ground — important! |

The Pico gets power from USB. The 10V supply powers the motors only. All grounds must be connected together.

---

### Fader → TB6612FNG (Motor)

| Fader terminal | Connect to |
|----------------|-----------|
| Terminal 1 (GND) | GND |
| Terminal 3 (3.3V) | Pico 3.3V |
| Terminal A | TB6612FNG AO1 (fader 1) or BO1 (fader 2) |
| Terminal B | TB6612FNG AO2 (fader 1) or BO2 (fader 2) |

If the fader moves in the wrong direction, swap A and B.

---

### Fader → Pico (Sensing)

| Fader terminal | Pico pin | Notes |
|----------------|----------|-------|
| Terminal 2 (wiper) | GP26 (fader 1) | ADC position feedback |
| Terminal 2 (wiper) | GP27 (fader 2) | ADC position feedback |

Terminal T (touch) is unused — the firmware detects move completion from the
PID state machine (deadband + settle timer), not from touch.

---

### TB6612FNG → Pico (Control)

| TB6612FNG pin | Pico pin | Notes |
|---------------|----------|-------|
| AIN1 | GP2 | Fader 1 direction |
| AIN2 | GP3 | Fader 1 direction |
| PWMA | GP6 | Fader 1 speed |
| BIN1 | GP7 | Fader 2 direction |
| BIN2 | GP8 | Fader 2 direction |
| PWMB | GP9 | Fader 2 speed |
| STBY | GP10 | Driver enable |

> **Note:** GP4 and GP5 are skipped — this board does not expose them on the header. If you have a real Raspberry Pi Pico W, you can use the standard GP2–GP8 range instead and update `pico/main.py` accordingly.

---

## One Fader Only

If you only have one fader connected, the firmware still works — fader 2 will sit at its default setpoint (50%) but won't move since nothing is connected. No code changes needed.

Just leave BIN1, BIN2, PWMB, BO1, BO2, and GP27 unconnected.

---

## PC Software

Two interfaces. The **terminal tool** is for bring-up and PID tuning; the
**extension host** is what you actually run day to day.

### Terminal tool

```bash
pip install pyserial
python host/host.py
```

Type `50 75` to set fader 1 to 50% and fader 2 to 75%. Type `50` to set fader 1 only.

If the Pico isn't detected automatically, pass the port explicitly:

```bash
python host/host.py /dev/ttyACM0      # Linux
python host/host.py COM3              # Windows
```

### Extension host

Binds faders to things on the PC. `faders/config.json` declares which
extension owns which fader:

```bash
python -m faders.run
```

With `pulse_sink`, each fader controls a PulseAudio/PipeWire sink: move the
fader to change volume, change it in pavucontrol and the fader follows, and
press the fader's button to mute. The fader's display shows that sink's label
and volume — display content comes from whichever extension owns the fader, so
it describes whatever that fader actually controls.

```json
{"fader": 0, "sink": "sink-music", "min": 0, "max": 100, "label": "Music"}
```

`label` is optional and defaults to the sink name — worth setting, since real
sink names (`alsa_output.pci-0000_00_1f.3.analog-stereo`) do not fit a 128 px
screen.

### Tests

No hardware needed — the fader is simulated and `machine`/`utime` are stubbed:

```bash
python3 tests/run_all.py
```

---

## Files

| File | Description |
|------|-------------|
| `pico/main.py` | MicroPython firmware — copy to Pico as `main.py` |
| `pico/lib/` | Device drivers: MCP3208, buttons, SSD1306, screen layout — **upload with `main.py`** |
| `host/host.py` | PC terminal interface (calibrated 0–100% display) |
| `host/host_raw.py` | Raw 16-bit ADC viewer for wiring diagnostics |
| `faders/` | Extension host — serial transport, button/mute dispatch, plugins |
| `faders/extensions/pulse_sink.py` | Binds a fader to a PulseAudio/PipeWire sink |
| `tests/` | Test suites — run `python3 tests/run_all.py`, no hardware required |
| `docs/upload.md` | How to flash firmware to the Pico (Thonny + mpremote) |
| `docs/pid_tuning.md` | How to tune the PID controller |
| `docs/firmware.md` | 4-fader firmware — protocol, buttons, mute, displays |
| `docs/pcb_design.md` | 4-fader PCB design — ADC choice, TB6612FNG, BOM |
| `docs/modular_design.md` | Future modular expansion plan (RP2040-Zero modules) |
