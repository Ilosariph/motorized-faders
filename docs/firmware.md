# Firmware — 4-Fader Build

Implements the deferred firmware items from [pcb_design.md](pcb_design.md):
MCP3208 ADC, four SSD1306 SPI displays, the button ladders, the VM rail check,
and mute.

---

## Module Layout

| File | Role |
|------|------|
| `pico/main.py` | Pin map, PID, state machine, protocol, main loop |
| `pico/lib/mcp3208.py` | 12-bit 8-channel SPI ADC |
| `pico/lib/buttons.py` | Resistor-ladder decode, debounce, press edges |
| `pico/lib/ssd1306_spi.py` | SSD1306 SPI driver + 5x7 font |
| `pico/lib/fader_screen.py` | Per-fader screen layout |

Upload `pico/main.py` **and** the `pico/lib/` directory — see
[upload.md](upload.md).

`FaderPID` takes a `read_raw` callable rather than a pin number, so all
hardware access sits behind one injection point. The PID body, `calibrate()`
and `read_position()` are unchanged from the 2-fader prototype.

---

## Serial Protocol

115200 baud over USB, line-based ASCII. Additive — a host that does not know a
prefix ignores it.

### Pico to host

| Line | Meaning |
|------|---------|
| `POS:f1,f2,f3,f4` | Position, 0-100%. 20 Hz while engaged, on-change while idle |
| `RAW:r1,r2,r3,r4` | Raw ADC 0-65535, sent only while engaged, diagnostics |
| `STATE:idx,IDLE\|MOVING\|SETTLING` | State edge, `idx` 1-based |
| `BTN:F1..F4\|G1\|G2` | Button press edge |
| `BTNUP:F1..F4\|G1\|G2` | Button release edge |
| `MUTE:idx,0\|1` | Mute state confirmation |
| `RAIL:ok,<volts>` / `RAIL:fail,<volts>` | VM check at boot |
| `CAL:start` / `CAL:done` | Calibration phase |
| `DBG:<text>` | Diagnostics |

### Host to Pico

| Line | Meaning |
|------|---------|
| `SET:f1,f2,f3,f4` | Setpoints. An **empty slot leaves that fader alone** |
| `MUTE:idx,0\|1[,pos]` | Mute/unmute fader `idx` (1-based). Optional `pos` sets the position unmute returns to (mute) or drives to (unmute) instead of reading the fader |
| `DISP:idx,line1,line2` | Display text for fader `idx` (1-based) |
| `SLOT:idx,A\|B` | A/B slot badge for fader `idx`; empty clears it |
| `MENU:idx,label,name` | Menu card on fader `idx`'s screen (no bar), until the next `DISP:` |
| `CREASES:idx,pos:power:ms,...` | Haptic detents for fader `idx`, see below. No entries clears |
| `TUNE:kp,ki,kd,minmove,deadband,deadband_exit` | Live PID gains, empty slot keeps value, echoed as `DBG:tune ... loop=Nms`, lost on reset. See [pid_tuning.md](pid_tuning.md) |

Only these prefixes are parsed as input. Everything else on stdin is
discarded, which matters because the Pico's own output can arrive back as a USB
CDC loopback.

`DISP:` splits on the first two commas only, so `line2` may contain commas.
The host strips commas and newlines anyway (`faders/core.py::_sanitize`).

---

## Virtual creases

Haptic detents with no touch sensor. While a fader is `IDLE` the motor is off,
so any movement is the user's hand; crossing a crease fires one short pulse
of `power` % for `ms` against that motion. A bump to feel, never a pull
toward the crease, and nothing happens during a commanded move.

- The crossing test runs on an anchor that only moves once the fader is
  `CREASE_HYST_PCT` (0.8%) away, so ADC flicker while resting on a crease
  never fires.
- Pulses scale with drag speed: full strength at `CREASE_FULL_SPEED`
  (150 %/s) and above. Power scales down to `CREASE_MIN_POWER` (40%) for a
  slow drag; length only to `CREASE_MIN_LENGTH_SCALE` (90%), since pulses
  must stay short enough not to run into the next crease on a fast drag.
- The pulse shoves the fader back over the crease; re-crossing that same
  crease within `CREASE_REFRACTORY_MS` (120 ms) of the pulse ending is
  ignored, or it buzzes. Other creases stay live during that window.
- Display redraws are skipped while a pulse runs: a stalled loop would
  stretch the pulse, and its length is the bump's strength.
- The Pico forgets creases on reboot. `FaderHost.set_creases()` caches them
  and replays them on connect and after every `CAL:done`.

`pulse_sink` places them in sink-volume terms: every 25%, stronger at
100% on a sink whose `max` goes past it (on 0-150,
that is 66.7% of travel). `"creases": false` in its config entry turns them
off. Strengths are `CREASE_NORMAL` / `CREASE_BIG` in
`faders/extensions/pulse_sink.py`.

---

## Buttons: firmware reports, host decides

The firmware assigns buttons **no meaning**. It decodes which button was
pressed or released and emits `BTN:` / `BTNUP:`; `FaderHost._handle_button`
maps that to an action.

This is deliberate: layers, paging and A/B are all host-side, with no
firmware, protocol, or PCB change. Currently:

| Button | Action |
|--------|--------|
| `F1`-`F4` | Toggle mute on the matching fader |
| `G1` held + `F1` / `F4` | Previous / next layer. Screens name the layer while `G1` is held; faders move on its release |
| `G2` held + `F1`-`F4` | A/B swap: store the fader's current position, move to the one stored by the last swap. The first swap only stores. Ignored while muted |
| `G2` held + `F1` / `F4`, paged layer | Previous / next page (e.g. `discord_voice`), replaces A/B there |
| `G1` / `G2` alone, `G1`/`G2` + `F2`/`F3` where unassigned | Nothing |

Held modifiers never also mute. While a layer switch is settling, only further
`G1` steps are taken.

All presses are also reported to extensions via `on_button`.

Edges only: a held button reports `BTN:` once and `BTNUP:` once on release.
The host tracks what is held from those, which is what makes `G2` usable as
a modifier. `G2` and the fader buttons sit on different ADC channels, so
holding both reads correctly; `G1` + `G2` together read as `G1`.

### Ladder decode

Thresholds are derived from the resistor values, not hardcoded ADC counts, so
the BOM and the firmware cannot drift apart:

```
CH6: G1 = 0R, G2 = 4k7                        -> 0.00 V, 1.06 V
CH7: F1 = 0R, F2 = 2k2, F3 = 4k7, F4 = 10k    -> 0.00, 0.60, 1.06, 1.65 V
```

Each band's boundary is the midpoint to its neighbour, leaving ≥±3% of
full-scale margin — far more than resistor tolerance needs. A press must read
the same band twice running (`DEBOUNCE_SAMPLES`) to count, on top of the 100 nF
at each node.

---

## Mute

Pressing a fader button parks that fader at the bottom and remembers where it
was. Pressing again drives it back.

The split follows the same principle as the buttons:

- **Firmware owns the motion** — park, remember, restore. No host round trip
  for the mechanical behaviour.
- **Host owns the meaning** — which button mutes what, and applying mute to
  whatever the fader controls (a PulseAudio sink).

Sequence:

```
button press -> BTN:F3
             -> host: toggle_mute(2) -> MUTE:3,1
             -> firmware: save position, engage 0.0
             -> MUTE:3,1 (confirmation)
             -> host: pactl set-sink-mute sink-chat 1
```

The host tracks mute from the **confirmation**, never from the command it
sent — the same gate-on-reported-state rule the `STATE:` line exists for.

### Moving a muted fader by hand

Specified behaviour: drag a muted fader somewhere, press mute again, and it
re-mutes from the new position.

`FaderPID.update()` therefore tracks manual movement while muted and IDLE, and
treats the new position as the restore target.

**The park zone matters.** Muting leaves the fader sitting at the bottom, which
is itself "movement while muted". Without a guard, the parked position
immediately overwrites the position being restored — so pressing mute twice
loses it. Positions below `MUTE_PARK_ZONE_PCT` (3%) are therefore not read as
a new target. 3% clears the 1% calibration margin at each end with room to
spare, and a fader a user genuinely parks that low is already at the bottom.

`tests/test_firmware.py` covers three full mute cycles and a deliberate 12%
target against this.

---

## Displays

Content is **host-supplied**, so the screen describes whatever plugin owns the
fader. `pulse_sink` pushes the sink label and volume; another extension on the
same fader would label it differently with no firmware change.

```
+------------------------------+
| Music                        |  line1, scale 2 (scale 1 if too long)
| vol 78%                      |  line2, scale 2 (scale 1 if too long)
| [####################----]   |  position bar, firmware-owned
| A                            |  A/B badge (host, SLOT:)
+------------------------------+
```

`MENU:idx,label,name` replaces all of that with a menu card — `label` small
on top, `name` centred below as large as fits — until the next `DISP:`. The
host uses it while G1 is held: `< prev`, `selected`, `layer n/N`, `next >`.

Muted adds a solid full-width bar along the bottom,
`MUTE  NN%` knocked out of it, where `NN` is the position unmute returns to.
The otherwise empty position bar shows that same restore position as a thin
strip, so dragging a muted fader to pick a new restore level is visible. The SSD1306 is **monochrome** — there is no colour to use,
so mute reads as a large filled block, which carries across a desk where four
small letters do not.

**Redraw discipline.** A frame is drawn in pure Python and blocks the loop for
tens of ms. Screens redraw only when their content actually changes
(`FaderScreen.draw()` returns `False` otherwise), position changes are
quantised to 0.5%, the main loop draws **at most one display per pass**, and
**nothing redraws while any fader is MOVING** — a stalled loop leaves the motor
running blind at its last output, which overshoots into the end stops.

A display that fails to initialise is logged and set to `None`; the faders keep
working without it.

---

## VM Rail Check

The HUSB238 falls back to 5 V silently if the charger cannot supply 9 V. At the
bench that looks exactly like a stiff fader or a bad motor joint, so the
firmware measures the rail instead of assuming it:

```
boot -> STBY low (motors disabled)
     -> read CH4 divider
     -> 9 V +/- 1.2 V ? -> STBY high, calibrate()
     -> else            -> STBY stays low, RAIL:fail, skip calibration
```

On failure the displays show `RAIL FAIL` and the measured voltage, and the host
prints a warning naming the 9 V jumper. Buttons and screens still work, so the
fault is visible rather than silent.

| VM | CH4 | Verdict |
|----|-----|---------|
| 5 V | 1.24 V | fail — PD fell back |
| 9 V | 2.23 V | ok |
| 12 V | 2.98 V | fail — wrong jumper |

---

## Calibration

Sweeps run **until the wiper stops moving**, not for a fixed duration.

The prototype swept 15 steps of 30 ms, sized for 60 mm of travel. The
RSA0N11M9 has 100 mm; that sweep does not reach the end stop, and calibrating
against a mid-travel point corrupts every subsequent position report. The
sweep now ends when consecutive reads differ by less than `CAL_STALL_COUNTS`
for `CAL_STALL_READS` in a row — the carriage is against the stop.

`CAL_SWEEP_TIMEOUT_MS` (4 s) is a backstop only: an unplugged or jammed motor
emits `DBG:calibration sweep timed out` instead of hanging boot.

Wiper polarity is still detected rather than assumed.

---

## Stall guard

A fader that cannot reach its setpoint (jammed, stuck at an end stop, held by
hand) would otherwise be driven at stall current until the next command. A move
is abandoned — motor off, `IDLE`, `DBG:stall fN, move abandoned` — when either:

- it stays `MOVING` longer than `MOVE_TIMEOUT_MS` (3 s), or
- output stays at `STALL_OUTPUT_PCT` (100%) while the position changes less
  than `STALL_PROGRESS_PCT` (0.5%) within `STALL_WINDOW_MS` (300 ms).

A stuck carriage always reaches full power, through `kp x error` or the
breakaway ramp (40% -> 100% in ~300 ms). Arming the window at full power rather
than 50% matters: at 50% it started while the ramp was still climbing, and a
fader parked at an end stop was abandoned ~50 ms after reaching full power,
before it could break free. A jam costs at most ~0.6 s of drive per fader.

---

## Tests

No hardware required — `machine` and `utime` are stubbed and the fader is
simulated as a plant that integrates motor drive over time.

```bash
python3 tests/run_all.py
```

| Suite | Covers |
|-------|--------|
| `tests/test_firmware.py` | MCP3208 framing, calibration at both polarities, PID, pin map, mute cycling, protocol parsing, ladder decode, rail check, redraw gating |
| `tests/test_host.py` | POS/BTN/MUTE/RAIL parsing, button dispatch, layers, loopback guard, display de-dup, `pulse_sink` pactl calls |
| `tests/test_discord_voice.py` | `discord_voice`: user/fader mapping, paging, volume and mute both ways, RPC framing, OAuth token cache |
| `tests/test_integration.py` | Firmware + host over a loopback pipe: real button press to real fader travel |

The integration suite is the one that matters for the mute behaviour — it
presses a simulated button through the resistor ladder and asserts the
simulated fader reaches the bottom and comes back.
