# Motorized Fader Controller

RP2040 board (Pico-clone with RTL8720DN WiFi — GP4/GP5 not exposed) + SparkFun TB6612FNG dual motor driver + Alps RS60N11M9 motorized faders.

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
| Terminal T (touch) | GP11 (fader 1) | External 1MΩ pull-down to GND required |
| Terminal T (touch) | GP12 (fader 2) | External 1MΩ pull-down to GND required |

**Touch wiring:** The Alps RS60N11M9 touch pin does *not* short to GND when touched. The cap is connected to a conductive surface, and touching it couples 50/60 Hz mains hum through your body onto the pin. For reliable detection you need a high-value pull-down resistor (≈ 1 MΩ) between each T terminal and GND — this holds the input at 0 V when idle. Without it the input floats and reads noise. The firmware does **not** enable an internal pull-up; doing so would override the pull-down and the pin would read HIGH permanently.

```
Fader T pin ──┬── Pico GP11 (or GP12)
              │
             [1 MΩ]
              │
             GND
```

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

Just leave BIN1, BIN2, PWMB, BO1, BO2, GP27, and GP12 unconnected. (The fader-2 touch pull-down resistor isn't needed if GP12 is left floating.)

---

## PC Software

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

---

## Files

| File | Description |
|------|-------------|
| `pico/main.py` | MicroPython firmware — copy to Pico as `main.py` |
| `host/host.py` | PC terminal interface (calibrated 0–100% display) |
| `host/host_raw.py` | Raw 16-bit ADC viewer for wiring diagnostics |
| `docs/upload.md` | How to flash firmware to the Pico (Thonny + mpremote) |
| `docs/pid_tuning.md` | How to tune the PID controller |
| `docs/modular_design.md` | Future modular expansion plan (RP2040-Zero modules) |
