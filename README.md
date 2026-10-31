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

#### Layers and buttons

Plugins are grouped into **layers** that take turns owning the four faders.
Each layer remembers its own fader levels, mutes, screens and creases.
Without a `layers` key every plugin is its own layer (`pulse_sink` → "Audio",
`discord_voice` → "Discord", `home_assistant` → "Home"); to choose, list them:

```json
"layers": [
  {"name": "Audio",   "extensions": {"pulse_sink": [...]}},
  {"name": "Discord", "extensions": {"discord_voice": {...}}}
],
```

| Buttons | Action |
|---|---|
| `F1`-`F4` | Mute that fader |
| `G1` + `F1` / `F4` | Previous / next layer — screens show the name while `G1` is held, faders move on its release |
| `G2` + `Fn` | A/B position swap (layers without pages, e.g. Audio) |
| `G2` + `F1` / `F4` | Previous / next page (paged layers, e.g. Discord, Home with >4 entities) |

#### Discord

`discord_voice` puts the other people in your current voice channel on the
faders: name on the screen, fader = their local volume 0-200%, fader button =
Discord's per-user mute (right-click → Mute; volume untouched). More than four
people → pages, shown as `x/y` on the last screen. Needs the official desktop
client (Vesktop/arRPC lack voice RPC) and your own Discord application:

1. <https://discord.com/developers/applications> → New Application.
2. OAuth2 → copy Client ID and Client Secret, add redirect `http://localhost`.
3. Config: `"discord_voice": {"client_id": "...", "client_secret_file": "/path/to/secret"}`
   (`client_secret` inline also works; keep the file private).
4. Start the host, click **Authorize** in the Discord popup once. The token is
   cached in `~/.local/state/motorized-faders/discord_token.json`.

#### Home Assistant

`home_assistant` binds faders to lights and covers over HA's WebSocket API:

- **Light**: fader = brightness, dragged to the bottom = off; button = off /
  back on at its brightness. Turned off in HA → fader parks (muted).
- **Cover**: `"attribute": "position"` (default) or `"tilt"`. Two faders on
  one blind give position and slat angle. Button = close (0) / restore.
  Covers get one command when the fader comes to rest; the fader follows HA
  once the cover stops moving.

Entries fill the faders in order; more than four → pages (`G2` + `F1`/`F4`,
`x/y` on the last screen). `null` leaves a fader empty, e.g. to keep a
blind's pair on one page. Needs a long-lived access token (HA profile →
Security):

```json
"home_assistant": {
  "url": "http://homeassistant.local:8123",
  "token_file": "~/.config/motorized-faders/ha-token",
  "faders": [
    {"entity": "light.desk", "label": "Desk"},
    null,
    {"entity": "cover.office", "attribute": "position", "label": "Blinds"},
    {"entity": "cover.office", "attribute": "tilt", "label": "Slats"}
  ]
}
```

`token` inline also works (keep the config private); `"verify_ssl": false`
for a self-signed https URL.

The host survives the Pico being unplugged: it waits for the port to come
back (it may return as a different `ttyACM` node), then replays display text,
A/B badges and creases, and extensions re-seed fader positions and mute via
their `on_connect` hook. Starting without the Pico attached waits the same way.

### NixOS

`flake.nix` exports the package (`nix run .`), a dev shell and
`nixosModules.default`, which runs the host as a systemd **user** service
(plugins like `pulse_sink` need the user's PipeWire session) and grants the
logged-in user the Pico's tty via udev `uaccess`:

```nix
{
  inputs.motorized-faders.url = "github:Ilosariph/motorized-faders";
  # in nixosSystem modules: inputs.motorized-faders.nixosModules.default
  services.motorized-faders = {
    enable = true;
    plugins.pulse_sink = [
      { fader = 0; sink = "sink-master"; max = 100; label = "Master"; }
      { fader = 3; sink = "sink-music"; max = 150; label = "Music"; }
    ];
    plugins.discord_voice = {
      client_id = "1234567890";
      client_secret_file = "/run/secrets/discord-faders";  # not in the store
    };
  };
}
```

`plugins` keys are module names in `faders/extensions/` (checked at eval);
each value is one instance config or a list. `layers` (list of
`{ name; extensions; }`) sets explicit layers. Logs:
`journalctl --user -u motorized-faders -f`.

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
| `faders/extensions/discord_voice.py` | Voice-channel users on the faders: volume, mute, pages |
| `faders/extensions/home_assistant.py` | HA lights (brightness, on/off) and covers (position, tilt), paged |
| `tests/` | Test suites — run `python3 tests/run_all.py`, no hardware required |
| `docs/upload.md` | How to flash firmware to the Pico (Thonny + mpremote) |
| `docs/pid_tuning.md` | How to tune the PID controller |
| `docs/firmware.md` | 4-fader firmware — protocol, buttons, mute, displays |
| `docs/pcb_design.md` | 4-fader PCB design — ADC choice, TB6612FNG, BOM |
| `docs/modular_design.md` | Future modular expansion plan (RP2040-Zero modules) |
