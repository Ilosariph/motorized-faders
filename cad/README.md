# CAD

| File | Part | Source |
|------|------|--------|
| `case.scad` | Case: top panel + base tray, parametric | this repo |
| `adafruit_5807_husb238.step` (gitignored, download locally) | Adafruit 5807 HUSB238 USB-C PD breakout | [Adafruit_CAD_Parts](https://github.com/adafruit/Adafruit_CAD_Parts/tree/main/5807%20HUSB238%20Breakout) |

Display geometry (Adafruit 326, STEMMA QT revision) comes from Adafruit's
Eagle board — no STEP exists:
[Adafruit-128x64-Monochrome-OLED-PCB](https://github.com/adafruit/Adafruit-128x64-Monochrome-OLED-PCB).


## Case (`case.scad`)

```bash
openscad case.scad                                       # assembly w/ ghost parts
openscad -D 'part="panel"' -o panel.stl case.scad        # prints face down
openscad -D 'part="base"'  -o base.stl  case.scad
```

Outer 220 x 227 x 70 mm (65 mm inside) — needs a >=230 mm bed. Layout per column, front to
back: fader, fader button, display. General buttons (G2, G1) sit in a strip
right of fader 4, G2 level with the fader buttons.

| Part | Mounting | Hardware |
|------|----------|----------|
| Fader RSA0N11M9 | 2x M3 from top into frame, countersunk; 3 x 110 mm slot | M3x5 countersunk |
| Display Adafruit 326 0.96in (STEMMA QT rev) | glass up against panel underside on 1.6 mm posts; holes 24.13 x 26.67 c-c; window 23.7 x 12.9 | M2.5x8 pan head from top + nut |
| Buttons Omron B3F-4050 | snap cradle under 1.2 mm skin, 5.8 mm hole, plunger 1.8 mm proud | none |
| HUSB238 PD board | 2 standoffs (8 mm) at back wall right of PCB, USB-C facing out | M2.5x5 thread-forming |
| Panel -> base | 6 countersunk screws into heat-set inserts in wall bosses | M3x6, M3 insert 4.0 mm |
| PCB (`motorized-faders.kicad_pcb`, 155 x 93) | 4 standoffs (5 mm), KiCad top edge (Pico USB end) 0.5 mm off back wall | M3 self-tap |

`btn_type` = `"mx"` (Cherry MX plate cutout) or `"round"` (`round_hole_d`
panel pushbutton) are alternatives to the default `"tact"`. Back wall has two USB-C openings (15 x 9 mm, fits bulky overmolds). The
Pico one is a tall slot covering both the Pico on female headers and soldered
flat to the PCB. If the PCB outline or Pico position changes, update `pcb_*`
and `pico_usb_x`.

Not verified against hardware: fader slot length/width (lever stem 8 x 1.5 mm
read off the drawing) and M3 screw length — check one fader before printing
the full panel.
