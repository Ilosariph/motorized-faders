# VM Power Switch — Plan

Goal: the Pico can physically cut the 9 V motor rail. When the PC shuts down
(or sleeps, or the host dies) VM goes to 0 V, so nothing can move a fader.

Status: **plan**, nothing built yet.

---

## Why STBY is not enough

STBY low already disables both TB6612FNGs, but it is one logic pin holding a
live 9 V / 3.2 A rail back:

| Situation | Today | With switch |
|---|---|---|
| PC off, USB keeps 5 V in S5 (common BIOS default) | Pico runs on, VM live, motors armed | Host gone → VM cut |
| PC off, USB 5 V drops | Pico dead, STBY pull-up has no 3.3 V → **STBY undefined with VM live** (see [pcb_design.md](pcb_design.md#trigger-board-husb238-jumper-set-to-9-v)) | Switch defaults off → VM 0 V |
| Firmware hang with STBY high | Motor may keep driving | Watchdog reset → switch off |
| Host crash / suspend | Faders armed, nobody listening | Keepalive timeout → VM cut |

The second row is an existing hole: the switch closes it as a side effect.

---

## Hardware

High-side P-channel load switch between the PD trigger board and the `+9V`
net, driven through an N-channel level shifter from a spare GPIO.

```
 +9V_PD ──┬────────┬──────── S
 (PD in)  │        │          Q1 AO4407A (P-ch)     D ──── +9V (VM, bulk, VM_SENSE)
       [R1 100k] [C1 1uF]     G
          │        │          │
          └────────┴──────────┤   (Q1 gate node)
                              │
                           [R2 10k]
                              │
                              D
                    Q2 BSS138 (N-ch)
 GP0 (VM_EN) ──[R3 100R]──┬── G
                          │   S
                      [R4 100k]
                          │   │
 GND ─────────────────────┴───┘
```

| Ref | Part | Footprint | Role |
|---|---|---|---|
| Q1 | AO4407A, P-ch, -30 V, -12 A, ~11 mΩ @ -10 V, Vgs ±25 V | SOIC-8 | Rail switch |
| Q2 | BSS138, N-ch, Vth ≤ 1.5 V | SOT-23 | 3.3 V → gate level shift |
| R1 | 100 kΩ | 0805 | Q1 gate pull-up to source → **off by default** |
| R2 | 10 kΩ | 0805 | Gate pull-down via Q2; sets Vgs ≈ -8.2 V, limits gate current |
| C1 | 1 µF, ≥ 16 V | 0805 | Q1 G–S, soft start (R2·C1 ≈ 10 ms ramp) |
| R3 | 100 Ω | 0805 | Q2 gate series, edge damping |
| R4 | 100 kΩ | 0805 | Q2 gate pull-down → **off while Pico is unpowered / in reset / booting** |
| TP | Test point on `+9V_PD` | THT pad | Measure PD output independent of the switch |

Notes:

- **Fail-safe by construction.** Every un-driven state (Pico unpowered,
  reset, pin still an input, firmware crashed into the watchdog) leaves Q2
  off → R1 holds Q1 off.
- **Soft start matters.** Switching 9 V straight into the 2× 100 µF bulk
  caps is an inrush spike a PD source may treat as a fault and renegotiate or
  drop. With C1 the ramp is ~10 ms → ~0.2 A average charge current. Tune C1
  on the bench (scope `+9V` at turn-on).
- **Dissipation:** 3.2 A peak × 11 mΩ ≈ 0.11 W, no heatsinking.
- **Turn-off** is R1·C1 ≈ 100 ms. Harmless: firmware drops STBY first, so
  the switch never opens under motor load.
- **After cut** the bulk caps bleed through the VM_SENSE divider (13.3 kΩ,
  τ ≈ 2.7 s) → < 1 V in ~6 s. No extra bleeder needed.
- **No reverse-polarity protection** from Q1 (its body diode conducts when
  reversed). The separate reverse-polarity FET in pcb_design.md is still the
  answer for bench-supply bring-up, if fitted.

### Schematic / PCB changes

1. Split the 9 V net: PD connector (1x04) pin → new net `+9V_PD`; everything
   else (VM1-3, bulk caps, VM_SENSE divider) stays on `+9V`.
2. Insert Q1/Q2/R1-R4/C1 between them, near the PD connector.
3. **VM_SENSE stays on the switched side** — the rail check then proves the
   switch actually turned on, not just that PD negotiated.
4. New label `VM_EN` on GP0 (GP0/GP1 were held for an HUSB238 I2C read that
   pcb_design.md already rejected; GP1 stays spare).
5. Q1 drain and source as copper pours, 3.2 A path ≥ 1.5 mm wide.
6. Update pcb_design.md: pin map (GP0), BOM, power table, power section.

---

## Firmware (`pico/main.py`)

### State

New pin `PIN_VM_EN = 0`, `Pin.OUT` driven 0 as the first thing in `main()`
(R4 already holds it low until then).

```
boot ───────> OFF  (VM_EN 0, STBY 0, screens dark)
               │  host sends PWR:on
               v
        VM_EN 1, wait VM_SETTLE_MS (~50 ms)
        read VM ──out of band──> RAIL:fail, VM_EN 0, stay OFF
               │ ok
               v
        STBY 1, calibrate() if never calibrated
               │
               v
              ON ──── PWR:off, or no host line for HOST_TIMEOUT_MS ────┐
               ^                                                       │
               └──────────── PWR:on ──── OFF <─ STBY 0, motors coast,  ┘
                                                VM_EN 0, screens dark
```

| Constant | Value | Why |
|---|---|---|
| `HOST_TIMEOUT_MS` | 10000 | 5 missed keepalives; long enough for a busy host |
| `VM_SETTLE_MS` | 50 | Soft-start ramp + margin before the rail check |

- **Calibration moves to first arm**, not boot. Endpoints stay in RAM across
  OFF→ON, so re-arming after sleep does not sweep the faders again.
- **On OFF:** set every fader IDLE, motors coast, STBY 0, then VM_EN 0 —
  never open the switch under load. Screens `poweroff()` (SSD1306 0xAE): no
  OLED burn-in overnight while USB standby keeps the Pico alive.
- **On ON after OFF:** emit `PWR:on`; host replays state (below). Faders
  moved by hand while off are simply re-driven to the host's setpoints.
- **Stuck-switch check:** while OFF and > 10 s after the cut, VM_SENSE
  should read < 1 V. If not → `PWR:fault,<volts>` (Q1 shorted). Warning only;
  STBY stays low anyway.
- **Hardware watchdog:** `machine.WDT(timeout=8000)` (RP2040 max ~8.3 s), fed
  in the main loop and inside `calibrate()`'s sweep loops. A hang → reset →
  GP0 back to input → R4 → VM off. Enable last, after everything else works:
  a WDT on the rp2 port cannot be stopped, which makes `mpremote` sessions
  awkward. Gate behind `USE_WDT = True`.
- Buttons still report while OFF (host may want them; e.g. a wake button
  later). Position telemetry continues — harmless, useful for debugging.

### Protocol (additive)

| Direction | Line | Meaning |
|---|---|---|
| Host → Pico | `PWR:on` | Arm (or keepalive if already on). Sent on connect, then every 2 s |
| Host → Pico | `PWR:off` | Cut VM now (clean host shutdown) |
| Pico → Host | `PWR:on` / `PWR:off` | State edge, with reason in a following `DBG:` (`timeout`, `host`, `rail`) |
| Pico → Host | `PWR:fault,<volts>` | VM present while switched off |

Keepalive = only `PWR:on` resets the timer, not any line. A stuck host loop
that still flushes old `DISP:` writes should not hold the motors armed.

Existing `RAIL:` is unchanged, but now emitted on each arm, not just at boot.

---

## Host

| File | Change |
|---|---|
| `faders/core.py` | Send `PWR:on` on connect and every 2 s from the run loop. `stop()` sends `PWR:off` and flushes before closing the port. Parse `PWR:on` edge → same replay path as reconnect (`on_connect`, displays, badges, creases) |
| `faders/run.py` | SIGTERM → `host.stop()` already exists; verify `PWR:off` actually goes out (systemd stop timeout, port still open) |
| `host/host.py`, `host/autotune.py`, `host/demo*.py` | Same keepalive, or they lose the motors after 10 s. Small shared helper |
| `docs/firmware.md` | Protocol tables, state machine, new constants |
| `README.md` | One paragraph: VM is off whenever the host is not running |

Shutdown paths covered:

| Event | What cuts VM |
|---|---|
| Linux shutdown / logout (NixOS user service) | SIGTERM → `PWR:off` (immediate) |
| Windows shutdown / process killed | Keepalive timeout (10 s) |
| Suspend (USB 5 V stays, host frozen) | Keepalive timeout; resume → `PWR:on` → re-arm |
| PC off, USB 5 V drops | Pico dies → R4/R1 → off |
| Host crash | Keepalive timeout |
| Firmware hang | Watchdog → reset → off |

---

## Tests

- `tests/test_firmware.py`: OFF at boot; `PWR:on` → rail check → STBY order
  (VM_EN before STBY on arm, STBY before VM_EN on disarm); timeout via stubbed
  `ticks_ms`; non-`PWR` lines do not reset the timer; rail fail keeps VM_EN 0;
  no recalibration on re-arm; stuck-switch fault.
- `tests/test_host.py`: keepalive cadence; `PWR:off` on `stop()`; `PWR:on`
  edge triggers replay.

---

## Bring-up order

1. Hand-wire Q1/Q2 on the existing board (bodge on the PD lead) or a
   breadboard; toggle GP0 from the REPL, scope `+9V` turn-on for inrush.
2. Firmware state machine + host keepalive, without WDT.
3. Pull the USB lead with PD connected → VM must stay 0 V.
4. `systemctl --user stop motorized-faders` → VM 0 V immediately.
   Kill -9 the host → VM 0 V within ~10 s.
5. Enable WDT.
6. Spin the PCB revision.

## Open questions

- Should a fader button wake the rig while the host is absent? Current plan:
  no — the host owns everything, and no host = no motors.
- `HOST_TIMEOUT_MS` 10 s vs. shorter: shorter cuts faster on Windows shutdown,
  but risks dropping during a long blocking call in a plugin (Discord auth
  popup, HA reconnect). Keepalive should come from a dedicated thread, not
  the plugin loop, which makes 5 s safe.
