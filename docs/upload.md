# Uploading firmware to the Pico

The Pico runs MicroPython. `pico/main.py` must be copied to the device as
`main.py` so it runs on boot.

## Thonny (GUI, recommended for quick edits)

1. Plug the Pico into USB.
2. Open Thonny.
3. Bottom-right status bar → click the interpreter name → choose
   **MicroPython (Raspberry Pi Pico)**. Pick the serial port if asked
   (e.g. `/dev/ttyACM0`).
4. **View → Files** to show two panes: *This computer* (top) and
   *Raspberry Pi Pico* (bottom).
5. In the top pane, navigate to this repo's `pico/` directory.
6. Right-click `main.py` → **Upload to /**. The file appears in the
   bottom pane as `main.py`.
7. Hit the red **Stop** button (or `Ctrl+F2`) to soft-reset — firmware
   starts. Output appears in the Shell pane.

Editing in place: double-click `main.py` in the bottom pane, edit, save
(`Ctrl+S`) — Thonny writes back to the Pico. Stop/restart to apply.

Close Thonny (or disconnect) before running `host/host.py` or
`host/host_raw.py` — only one program can hold the serial port.

## mpremote (CLI, fastest for repeated uploads)

```
pip install mpremote
mpremote cp pico/main.py :main.py
mpremote reset
```

Watch serial output:

```
mpremote
```

Exit with `Ctrl+]`.

## First-time MicroPython install

If the Pico is brand new or has been used for C/Arduino:

1. Hold the **BOOTSEL** button while plugging in USB. Pico mounts as a
   USB drive named `RPI-RP2`.
2. Download the latest Pico W MicroPython UF2 from
   https://micropython.org/download/RPI_PICO_W/
3. Drag the `.uf2` onto the `RPI-RP2` drive. The Pico reboots into
   MicroPython.
4. Then upload `main.py` using either method above.
