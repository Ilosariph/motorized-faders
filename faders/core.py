"""
FaderHost — serial transport + extension dispatch for the motorized fader rig.

Talks the Pico protocol from `pico/main.py`:
    Pico -> host:  POS:f1,f2,f3,f4    (position, 0-100%)
                   STATE:idx,IDLE|MOVING|SETTLING
                   BTN:F1..F4|G1|G2   (button press edge)
                   BTNUP:F1..F4|G1|G2 (button release edge)
                   MUTE:idx,0|1       (mute state confirmation)
                   RAIL:ok|fail,volts (VM rail check at boot)
                   CAL:start | CAL:done
                   RAW:..., DBG:...   (ignored)
    host -> Pico:  SET:f1,f2,f3,f4    (setpoints, 0-100%)
                   MUTE:idx,0|1[,pos] (mute/unmute a fader, optional restore)
                   DISP:idx,l1,l2     (display text for a fader)
                   SLOT:idx,A|B       (A/B slot badge, empty clears)
                   CREASES:idx,pos:power:ms,...  (haptic detents)

Extensions subclass `Extension`. The host calls `on_position(idx, value)` when a
fader physically moves (touched by a human). Extensions call
`host.set_fader(idx, value)` to drive the motor, `host.set_mute(idx, muted)` to
mute it, `host.set_display(idx, line1, line2)` to label its screen, and
`host.set_creases(idx, creases)` to give it haptic detents.

Layers: the four faders are shared by several plugin sets. Each `Layer` holds
its own extensions and its own idea of every fader's level, mute, screen text,
A/B badge and creases. Extensions get their layer as `host`, with the same API;
only the active layer's writes reach the Pico, the rest are stored and applied
when that layer becomes active. Input (positions, buttons, mutes) goes to the
active layer only.

Button meaning lives here, not in firmware. The Pico reports which button was
pressed and released and nothing more; `_handle_button` maps that to an
action. Current mapping:
    F1..F4            toggle mute
    G1 held + F1/F4   previous / next layer
    G2 held + Fn      swap fader n between two remembered positions (A/B),
                      or, on a layer with pages (Extension.PAGED), G2 + F1/F4
                      is previous / next page
    G1 + G2 + Fn      nothing (ambiguous); G1 or G2 alone: nothing

While G1 is held (and there is more than one layer), screens 1 and 4 name
the previous and next layer, so a switch can be aimed before it is made.

A layer switch is applied when G1 is released: while it is held, steps only
retarget the screens, so stepping through several layers does not drive the
motors at every step.

Loopback guard: while a fader is moving in response to a `SET:` we just sent,
`on_position` is *not* called for that fader. Pico reports a STATE:idx,IDLE
edge when the move + settle is complete; only then do we resume forwarding
positions. This avoids extensions echoing their own setpoints back into the
source (e.g. PulseAudio) and oscillating.
"""

import logging
import sys
import threading
import time

import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
BAUD = 115200
SET_FLUSH_HZ = 50
SET_FLUSH_INTERVAL = 1.0 / SET_FLUSH_HZ

# Loopback guard watchdog. A commanded move normally ends in STATE:idx,IDLE
# within MOVE_TIMEOUT + SETTLE (~4 s). If the Pico still reports IDLE this
# long after we armed the guard, it never saw the SET: (a lost or garbled
# line) and no STATE: edge will ever release it — release it ourselves, or
# that fader is deaf to the hand for good.
GUARD_TIMEOUT_S = 5.0

# How often to look for the Pico while it is unplugged.
RECONNECT_INTERVAL_S = 1.0

# A layer's stored level and the fader's must differ by more than this for a
# layer switch to move the fader.
APPLY_EPSILON_PCT = 0.5

# Matches the firmware's MUTE_PARK_ZONE_PCT: a muted fader reading below this
# is sitting parked, not placed by the hand at a new restore level.
MUTE_PARK_ZONE_PCT = 3.0

log = logging.getLogger("faders")


# Display text budget. The Pico truncates to fit its own font, but trimming
# here keeps the serial line short and the two ends in agreement.
DISPLAY_LINE_MAX = 21


def _sanitize(text):
    """Strip protocol delimiters from display text and trim it to width."""
    if text is None:
        return ""
    cleaned = str(text).replace(",", " ").replace("\n", " ").replace("\r", " ")
    return cleaned.strip()[:DISPLAY_LINE_MAX]


def find_pico_port():
    for port in serial.tools.list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


class Extension:
    """Base class. Override the hooks you need; defaults are no-ops."""

    # True for a plugin whose faders page through more items than fit: G2 +
    # F1/F4 then pages (`on_page`) instead of swapping A/B on its layer.
    PAGED = False

    def on_position(self, fader_idx, value):
        """Called when a fader moves under human control (loopback-filtered)."""

    def on_calibration(self, phase):
        """phase is 'start' or 'done'."""

    def on_connect(self):
        """
        Called each time the Pico's port opens: at startup and after a
        replug. A replugged Pico has rebooted, so it holds no setpoints and
        no mute state; re-assert both here. Display text and creases set
        through the host are replayed by the host itself.
        """

    def on_mute(self, fader_idx, muted):
        """
        Called when a fader's mute state changes, after the Pico confirms it.
        Extensions apply it to whatever they control (e.g. a PulseAudio sink).
        """

    def on_button(self, button):
        """
        Called for every button press the Pico reports: 'F1'-'F4' (per-fader)
        or 'G1'/'G2' (general). Mute is already handled by the host for F*
        buttons; this hook exists for extensions that want the raw event.
        """

    def on_page(self, delta):
        """G2 + F1/F4 on a PAGED layer: step the page by `delta` (-1 / +1)."""

    def on_activate(self):
        """This extension's layer just became active and owns the faders."""

    def on_deactivate(self):
        """This extension's layer just handed the faders to another one."""

    def on_rail(self, ok, volts):
        """VM rail check result at boot. `ok` False means motors are disabled."""

    def stop(self):
        """Called on shutdown. Release resources."""


class Layer:
    """
    One plugin set and its view of the faders. Extensions receive the layer
    as their `host`: the methods mirror FaderHost's, and write through to the
    Pico only while this layer is active.
    """

    def __init__(self, host, name):
        self._host = host
        self.name = name
        self.extensions = []
        n = host.num_faders
        # Level per fader; for a muted fader, the level unmute returns to.
        # None until something sets it — a switch then leaves that fader be.
        self.setpoint = [None] * n
        self.muted = [False] * n
        self.display = [None] * n
        self.creases = [None] * n
        # A/B position swap (G2 + fader button): the position stored for the
        # slot that is *not* active. None until the first swap marks one.
        self.ab_other = [None] * n
        # Slot the fader is in now, shown as a badge on its screen. None until
        # the first swap; that swap leaves A stored and the fader in B.
        self.ab_slot = [None] * n

    @property
    def num_faders(self):
        return self._host.num_faders

    @property
    def paged(self):
        return any(getattr(e, "PAGED", False) for e in self.extensions)

    def register(self, ext):
        self._host._register(self, ext)

    def is_active(self):
        return self._host._is_live(self)

    def set_fader(self, idx, value):
        self._host._layer_set_fader(self, idx, value)

    def set_mute(self, idx, muted, level=None):
        self._host._layer_set_mute(self, idx, muted, level)

    def is_muted(self, idx):
        if not 0 <= idx < self.num_faders:
            return False
        with self._host._lock:
            return self.muted[idx]

    def toggle_mute(self, idx):
        if not 0 <= idx < self.num_faders:
            return False
        with self._host._lock:
            target = not self.muted[idx]
        self.set_mute(idx, target)
        return target

    def set_display(self, idx, line1, line2=""):
        self._host._layer_set_display(self, idx, line1, line2)

    def set_creases(self, idx, creases):
        self._host._layer_set_creases(self, idx, creases)

    def swap_ab(self, idx):
        self._host._layer_swap_ab(self, idx)


class FaderHost:
    def __init__(self, port=None, num_faders=4):
        self.num_faders = num_faders
        self._port = port
        self._ser = None
        self._extensions = []
        self._layers = []
        # Layer whose state the Pico holds now.
        self._active = 0
        # Layer a switch is heading to; differs from _active while G1 is
        # still held. None when no switch is pending.
        self._target = None

        # Last position reported by Pico, indexed 0..num_faders-1.
        self._positions = [0.0] * num_faders
        # Last setpoint we sent — used to fill SET slots that nothing updated.
        self._setpoints = [50.0] * num_faders
        # Pending writes from extensions (None = unchanged this tick).
        self._pending = [None] * num_faders
        # Per-fader Pico state. Updated from STATE: lines.
        self._states = ["IDLE"] * num_faders
        # Loopback guard: True while we drove a SET and Pico hasn't returned
        # to IDLE yet. Suppresses on_position for that fader.
        self._self_driven = [False] * num_faders
        # When each guard was last armed, for the watchdog.
        self._self_driven_since = [0.0] * num_faders
        # Mute state the Pico holds, mirrored from its MUTE: confirmations
        # rather than assumed from the commands we send.
        self._muted = [False] * num_faders
        # Display text, badge and creases the Pico holds, so unchanged values
        # are not re-sent and a rebooted Pico can be given them back.
        self._display = [None] * num_faders
        self._slots = [None] * num_faders
        self._creases = [None] * num_faders
        # Buttons currently held down, from BTN:/BTNUP: edges.
        self._held = set()
        # True while G1 is held: the outer screens show the layer hint.
        self._hint = False
        # VM rail check result, None until the Pico reports it.
        self.rail_ok = None
        self.rail_volts = None

        self._lock = threading.RLock()
        # Writes come from the reader, the writer and extension threads; one
        # line must go out whole or the Pico parses garbage.
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._waiting_logged = False
        self._reader = None
        self._writer = None

    # ----- layers ----------------------------------------------------------

    def add_layer(self, name):
        """Create a layer; the first one created starts active."""
        layer = Layer(self, name)
        with self._lock:
            self._layers.append(layer)
        return layer

    def _default_layer(self):
        """The layer FaderHost's own API acts on: the active one, made on demand."""
        with self._lock:
            if not self._layers:
                self._layers.append(Layer(self, "main"))
            return self._layers[self._active]

    @property
    def layers(self):
        return list(self._layers)

    @property
    def active_layer(self):
        with self._lock:
            return self._layers[self._active] if self._layers else None

    def _is_live(self, layer):
        """True when `layer`'s writes reach the Pico: active, no switch pending."""
        with self._lock:
            return (self._target is None and bool(self._layers)
                    and self._layers[self._active] is layer)

    def _live_layer(self):
        with self._lock:
            if self._target is not None or not self._layers:
                return None
            return self._layers[self._active]

    def _register(self, layer, ext):
        with self._lock:
            layer.extensions.append(ext)
            self._extensions.append(ext)

    def step_layer(self, delta):
        """
        Switch `delta` layers on. While G1 is held, only the screens name
        the target; the faders follow on its release. Otherwise at once.
        """
        with self._lock:
            if len(self._layers) < 2:
                return
            start = self._active if self._target is None else self._target
            self._target = (start + delta) % len(self._layers)
            target = self._layers[self._target]
            position = self._target + 1
            total = len(self._layers)
            held = "G1" in self._held
        if not held:
            self._finish_switch()
            return
        log.info("layer -> %s (%d/%d), applying on G1 release", target.name,
                 position, total)
        hinted = self._hinted()
        for idx in range(self.num_faders):
            if idx not in hinted:
                self._show(idx, target.name, f"layer {position}/{total}")
        if hinted:
            self._show_hint()

    def _hinted(self):
        """Faders whose screens the G1 layer hint holds: none, or 1 and N."""
        with self._lock:
            if not self._hint or len(self._layers) < 2:
                return ()
            return (0, self.num_faders - 1)

    def _show_hint(self):
        """Screens 1 and N: the layers G1 + F1 / F4 would step to."""
        with self._lock:
            if len(self._layers) < 2:
                return
            base = self._active if self._target is None else self._target
            total = len(self._layers)
            prev = (base - 1) % total
            nxt = (base + 1) % total
            prev_name = self._layers[prev].name
            next_name = self._layers[nxt].name
        self._show(0, prev_name, f"< prev {prev + 1}/{total}")
        self._show(self.num_faders - 1, next_name, f"next > {nxt + 1}/{total}")

    def _release_g1(self):
        """
        G1 released (or lost to a replug): apply the pending switch, which
        redraws every screen, or else put back the screens the hint covered.
        """
        hinted = self._hinted()
        with self._lock:
            self._hint = False
            pending = self._target is not None
            layer = self._layers[self._active] if self._layers else None
            texts = {idx: layer.display[idx] or ("", "") for idx in hinted}
        if pending:
            self._finish_switch()
            return
        for idx, text in texts.items():
            self._show(idx, *text)

    def _finish_switch(self):
        """G1 released: hand the faders to the target layer."""
        with self._lock:
            if self._target is None:
                return
            old = self._layers[self._active]
            new = self._layers[self._target]
            self._active = self._target
            self._target = None
        self._apply_layer(new, moved=new is not old)
        if new is not old:
            log.info("layer %s active", new.name)
            for ext in old.extensions:
                self._call(ext, "on_deactivate")
            for ext in new.extensions:
                self._call(ext, "on_activate")

    def _apply_layer(self, layer, moved):
        """
        Bring the Pico in line with `layer`. `moved` is False when a switch
        came back to the layer that already held the faders: then levels and
        mutes are left where they are and only the screens are restored.
        """
        hinted = self._hinted()
        for idx in range(self.num_faders):
            with self._lock:
                text = layer.display[idx]
                slot = layer.ab_slot[idx]
                creases = layer.creases[idx]
                muted = layer.muted[idx]
                level = layer.setpoint[idx]
                pico_muted = self._muted[idx]
                current = self._pending[idx]
                if current is None:
                    current = self._setpoints[idx]
            if idx not in hinted:
                self._show(idx, *(text or ("", "")))
            self._show_slot(idx, slot or "")
            if moved:
                self._push_creases(idx, creases or [])
            if muted:
                if moved or not pico_muted:
                    self._send_mute(idx, True, level)
            elif pico_muted:
                self._send_mute(idx, False, level)
            elif (moved and level is not None
                    and abs(level - current) > APPLY_EPSILON_PCT):
                self._queue_set(idx, level)

    # ----- public API ------------------------------------------------------
    # FaderHost's own setters act on the active layer, so a single-layer
    # setup can ignore layers entirely.

    def register(self, ext):
        self._register(self._default_layer(), ext)

    def set_fader(self, idx, value):
        """Extension entrypoint. Queues a SET:; flushed at ~50 Hz."""
        self._layer_set_fader(self._default_layer(), idx, value)

    def set_mute(self, idx, muted, level=None):
        """
        Mute or unmute a fader. The Pico parks it at the bottom and remembers
        where it was, so the host does not track the restore position. With
        `level`, that is the position to remember (mute) or return to
        (unmute) instead — for a plugin that just changed what the fader
        stands for.
        """
        self._layer_set_mute(self._default_layer(), idx, muted, level)

    def is_muted(self, idx):
        return self._default_layer().is_muted(idx)

    def toggle_mute(self, idx):
        """Flip a fader's mute state. Returns the state requested."""
        return self._default_layer().toggle_mute(idx)

    def set_display(self, idx, line1, line2=""):
        """
        Set a fader's display text. Content is the extension's choice — this is
        what makes the screens plugin-configured rather than firmware-defined.

        Commas and newlines are stripped: the protocol is line-based with
        comma-delimited fields, so they would corrupt the frame.
        """
        self._layer_set_display(self._default_layer(), idx, line1, line2)

    def set_creases(self, idx, creases):
        """
        Haptic detents: `creases` is a list of (position %, power %, ms).
        Dragging the fader by hand across a position fires a pulse of that
        power and length against the hand. An empty list clears them.
        """
        self._layer_set_creases(self._default_layer(), idx, creases)

    def swap_ab(self, idx):
        self._layer_swap_ab(self._default_layer(), idx)

    # ----- per-layer writes ------------------------------------------------

    def _layer_set_fader(self, layer, idx, value):
        if not 0 <= idx < self.num_faders:
            return
        value = max(0.0, min(100.0, float(value)))
        with self._lock:
            layer.setpoint[idx] = value
            live = self._is_live(layer)
        if live:
            self._queue_set(idx, value)

    def _queue_set(self, idx, value):
        with self._lock:
            self._pending[idx] = value
            self._self_driven[idx] = True
            self._self_driven_since[idx] = time.monotonic()

    def _layer_set_mute(self, layer, idx, muted, level=None):
        if not 0 <= idx < self.num_faders:
            return
        if level is not None:
            level = max(0.0, min(100.0, float(level)))
        with self._lock:
            if level is not None:
                layer.setpoint[idx] = level
            live = self._is_live(layer)
            if not live:
                # Applied when the layer next takes the faders. The live
                # layer instead learns its state from the Pico's confirmation.
                layer.muted[idx] = bool(muted)
        if live:
            self._send_mute(idx, muted, level)

    def _send_mute(self, idx, muted, level=None):
        """
        Sent immediately rather than queued: a mute is a discrete user action,
        not a continuous value that benefits from coalescing.
        """
        with self._lock:
            # Guard the loopback for the motion this mute causes, the same way
            # set_fader does — the fader is about to move under our command.
            self._self_driven[idx] = True
            self._self_driven_since[idx] = time.monotonic()
            if level is not None and not muted:
                self._setpoints[idx] = level
        line = f"MUTE:{idx + 1},{1 if muted else 0}"
        if level is not None:
            line += f",{level:.1f}"
        self._send(line)

    def _layer_set_display(self, layer, idx, line1, line2):
        if not 0 <= idx < self.num_faders:
            return
        text = (_sanitize(line1), _sanitize(line2))
        with self._lock:
            layer.display[idx] = text
            live = self._is_live(layer)
        if live and idx not in self._hinted():
            self._show(idx, *text)

    def _show(self, idx, line1, line2):
        line1, line2 = _sanitize(line1), _sanitize(line2)
        with self._lock:
            if self._display[idx] == (line1, line2):
                return  # unchanged — the Pico redraws on change only
            self._display[idx] = (line1, line2)
        self._send(f"DISP:{idx + 1},{line1},{line2}")

    def _show_slot(self, idx, slot):
        with self._lock:
            if (self._slots[idx] or "") == slot:
                return
            self._slots[idx] = slot
        self._send(f"SLOT:{idx + 1},{slot}")

    def _layer_set_creases(self, layer, idx, creases):
        if not 0 <= idx < self.num_faders:
            return
        creases = [(float(p), float(w), int(ms)) for p, w, ms in creases]
        with self._lock:
            layer.creases[idx] = creases
            live = self._is_live(layer)
        if live:
            self._push_creases(idx, creases)

    def _push_creases(self, idx, creases):
        with self._lock:
            if self._creases[idx] == creases or (
                    self._creases[idx] is None and not creases):
                return
            self._creases[idx] = creases
        self._send_creases(idx, creases)

    def _send_creases(self, idx, creases):
        entries = "".join(f",{p:.1f}:{w:.0f}:{ms}" for p, w, ms in creases)
        self._send(f"CREASES:{idx + 1}{entries}")

    def _layer_swap_ab(self, layer, idx):
        """
        Swap a fader between two positions. The current position is stored
        and the fader moves to the one stored by the previous swap, so each
        press returns to wherever the other slot was last left. The first
        swap has nothing stored yet, so it only marks the current position.

        Extensions are told directly: the loopback guard hides this move's
        POS: from them, but unlike a SET: from an extension, nothing else
        knows about it.
        """
        if not 0 <= idx < self.num_faders:
            return
        with self._lock:
            if layer.muted[idx]:
                return  # parked at the bottom; its position is not a level
            current = self._positions[idx]
            target = layer.ab_other[idx]
            layer.ab_other[idx] = current
            slot = "A" if layer.ab_slot[idx] == "B" else "B"
            layer.ab_slot[idx] = slot
            live = self._is_live(layer)
        if live:
            self._show_slot(idx, slot)
        log.info("fader %d: A/B swap to %s (%s)", idx + 1, slot,
                 "marked" if target is None else f"{target:.1f}%")
        if target is None:
            return
        self._layer_set_fader(layer, idx, target)
        if live:
            self._dispatch_to(layer, "on_position", idx, target)

    def run(self):
        """
        Serve until stop(). Survives the Pico being absent or unplugged: it
        waits for the port to appear, and when a read or write fails it
        closes the port and waits again.
        """
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()
        try:
            while not self._stop.is_set():
                ser = self._connect()
                if ser is None:
                    self._stop.wait(RECONNECT_INTERVAL_S)
                    continue
                self._ser = ser
                self._on_connected()
                self._reader = threading.Thread(target=self._reader_loop,
                                                args=(ser,), daemon=True)
                self._reader.start()
                while self._reader.is_alive() and not self._stop.is_set():
                    self._reader.join(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        for ext in self._extensions:
            try:
                ext.stop()
            except Exception as e:
                sys.stderr.write(f"[ext stop err] {e}\n")
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass

    # ----- internals -------------------------------------------------------

    def _connect(self):
        """Open the Pico's port, or return None if it is absent or busy."""
        # Re-resolve every attempt: a replugged Pico may come back as a
        # different ttyACM node.
        port = self._port or find_pico_port()
        if port is None:
            self._log_waiting("Pico not found (VID 0x2E8A)")
            return None
        try:
            ser = serial.Serial(port, BAUD, timeout=0.1)
            ser.reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            self._log_waiting(f"cannot open {port}: {e}")
            return None
        log.info("connected to %s at %d baud", port, BAUD)
        self._waiting_logged = False
        return ser

    def _log_waiting(self, reason):
        # Once per outage, not once per retry.
        if not self._waiting_logged:
            log.warning("%s; waiting for it", reason)
            self._waiting_logged = True

    def _on_connected(self):
        """
        The Pico just (re)appeared. After a replug it has rebooted: every
        fader is IDLE and unmuted, and nothing sent before still applies.
        """
        with self._lock:
            self._states = ["IDLE"] * self.num_faders
            self._self_driven = [False] * self.num_faders
            self._muted = [False] * self.num_faders
            self._pending = [None] * self.num_faders
            self._held.clear()
            if self._layers:
                # The active layer mirrors the Pico, which is unmuted now;
                # its extensions re-assert mutes from on_connect.
                self._layers[self._active].muted = [False] * self.num_faders
        # G1 is no longer held: apply a pending switch, drop the hint.
        self._release_g1()
        self._resend_after_boot()
        self._dispatch_all("on_connect")

    def _disconnect(self, ser, reason):
        """Drop `ser` after an I/O failure; run() then waits for a replug."""
        with self._write_lock:
            if self._ser is not ser:
                return  # already dropped by the other thread
            self._ser = None
        try:
            ser.close()
        except Exception:
            pass
        if not self._stop.is_set():
            log.warning("Pico disconnected (%s)", reason)

    def _reader_loop(self, ser):
        buf = ""
        while not self._stop.is_set() and self._ser is ser:
            try:
                data = ser.read(128)
            except (serial.SerialException, OSError, TypeError) as e:
                # pyserial raises TypeError from read() when the port was
                # closed under it by a failed write on another thread.
                self._disconnect(ser, f"read failed: {e}")
                return
            if not data:
                continue
            buf += data.decode("ascii", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                self._handle_line(line.strip())

    def _send(self, line):
        """Write one protocol line immediately. Safe to call from any thread."""
        with self._write_lock:
            ser = self._ser
            if ser is None:
                return
            try:
                ser.write((line + "\n").encode("ascii"))
                return
            except (serial.SerialException, OSError) as e:
                reason = f"write failed: {e}"
        self._disconnect(ser, reason)

    @staticmethod
    def _call(ext, hook, *args):
        """Call one extension hook, isolating its failure from the rest."""
        try:
            getattr(ext, hook)(*args)
        except Exception as e:
            sys.stderr.write(f"[ext {hook} err] {e}\n")

    def _dispatch_all(self, hook, *args):
        """Call `hook` on every extension of every layer."""
        for ext in list(self._extensions):
            self._call(ext, hook, *args)

    def _dispatch_to(self, layer, hook, *args):
        for ext in list(layer.extensions):
            self._call(ext, hook, *args)

    def _dispatch(self, hook, *args):
        """Call `hook` on the live layer's extensions; none mid-switch."""
        layer = self._live_layer()
        if layer is not None:
            self._dispatch_to(layer, hook, *args)

    def _handle_line(self, line):
        if line.startswith("POS:"):
            self._handle_pos(line[4:])
        elif line.startswith("STATE:"):
            self._handle_state(line[6:])
        elif line.startswith("BTN:"):
            self._handle_button(line[4:].strip())
        elif line.startswith("BTNUP:"):
            button = line[6:].strip()
            with self._lock:
                self._held.discard(button)
            if button == "G1":
                self._release_g1()
        elif line.startswith("MUTE:"):
            self._handle_mute(line[5:])
        elif line.startswith("RAIL:"):
            self._handle_rail(line[5:])
        elif line.startswith("CAL:"):
            phase = "start" if "start" in line else "done"
            log.info("Pico calibration %s", phase)
            if phase == "done":
                self._resend_after_boot()
            self._dispatch_all("on_calibration", phase)

    def _resend_after_boot(self):
        """
        A rebooted Pico has default screens, blank badges and no creases;
        the host still knows all three.
        """
        # Terminate whatever partial line the Pico is holding: opening the
        # port or a reboot leaves a fragment in its input buffer, which would
        # otherwise swallow the first command (measured: 1 line in 8 lost).
        self._send("")
        with self._lock:
            display = list(self._display)
            slots = list(self._slots)
            creases = list(self._creases)
        for idx, text in enumerate(display):
            if text is not None:
                self._send(f"DISP:{idx + 1},{text[0]},{text[1]}")
        for idx, slot in enumerate(slots):
            if slot:
                self._send(f"SLOT:{idx + 1},{slot}")
        for idx, layout in enumerate(creases):
            if layout is not None:
                self._send_creases(idx, layout)

    def _handle_pos(self, payload):
        parts = payload.split(",")
        for idx, raw in enumerate(parts[: self.num_faders]):
            try:
                value = float(raw)
            except ValueError:
                continue
            with self._lock:
                self._positions[idx] = value
                suppressed = self._self_driven[idx]
                age = time.monotonic() - self._self_driven_since[idx]
                if (suppressed and self._states[idx] == "IDLE"
                        and age > GUARD_TIMEOUT_S):
                    self._self_driven[idx] = False
                    suppressed = False
                    log.warning(
                        "fader %d: loopback guard stuck for %.1f s with the "
                        "Pico IDLE (lost SET:?); releasing", idx + 1, age)
                layer = self._live_layer()
                if not suppressed:
                    # Human moved this fader. Track it as the current setpoint
                    # so the next SET: (which broadcasts every slot) doesn't
                    # command this fader back to a stale value.
                    self._setpoints[idx] = value
                    # And as the layer's level, so switching away and back
                    # returns the fader to where the hand left it. A muted
                    # fader parked at the bottom is not a level.
                    if layer is not None and not (
                            layer.muted[idx] and value <= MUTE_PARK_ZONE_PCT):
                        layer.setpoint[idx] = value
            if suppressed or layer is None:
                continue
            self._dispatch_to(layer, "on_position", idx, value)

    def _handle_state(self, payload):
        parts = payload.split(",", 1)
        if len(parts) != 2:
            return
        try:
            pico_idx = int(parts[0])
        except ValueError:
            return
        idx = pico_idx - 1  # Pico uses 1-based, host uses 0-based
        state = parts[1].strip()
        if not 0 <= idx < self.num_faders:
            return
        with self._lock:
            self._states[idx] = state
            # Move complete: release the loopback gate.
            if state == "IDLE":
                self._self_driven[idx] = False
        log.debug("fader %d: %s", idx + 1, state)

    def _handle_button(self, button):
        """
        Map a button press to an action. Firmware assigns buttons no meaning,
        so this is the single place that decides what one does (see the
        module docstring for the map). A held general button turns a fader
        button into a command and never also mutes; while a layer switch is
        settling, only further layer steps are taken.
        """
        if not button:
            return
        with self._lock:
            self._held.add(button)
            g1 = "G1" in self._held
            g2 = "G2" in self._held
            pending = self._target is not None
            layer = self._live_layer()
        mods = "+".join(m for m, on in (("G1", g1), ("G2", g2)) if on)
        log.info("button %s%s", button, f" ({mods} held)" if mods else "")
        idx = None
        if button.startswith("F") and button[1:].isdigit():
            idx = int(button[1:]) - 1
            if not 0 <= idx < self.num_faders:
                idx = None
        with self._lock:
            start_hint = button == "G1" and not self._hint
            self._hint = self._hint or start_hint
        if start_hint:
            self._show_hint()
        if idx is not None:
            # F1 steps back, the last fader button steps forward.
            step = {0: -1, self.num_faders - 1: 1}.get(idx)
            if g1 and g2:
                pass
            elif g1:
                if step is not None:
                    self.step_layer(step)
            elif layer is None:
                pass  # mid-switch: the faders belong to no layer yet
            elif g2:
                if not layer.paged:
                    self._layer_swap_ab(layer, idx)
                elif step is not None:
                    self._dispatch_to(layer, "on_page", step)
            else:
                layer.toggle_mute(idx)
        if not pending:
            self._dispatch("on_button", button)

    def _handle_mute(self, payload):
        parts = payload.split(",", 1)
        if len(parts) != 2:
            return
        try:
            idx = int(parts[0]) - 1
        except ValueError:
            return
        if not 0 <= idx < self.num_faders:
            return
        muted = parts[1].strip().split(",")[0] == "1"
        with self._lock:
            self._muted[idx] = muted
            # Confirmations belong to the layer holding the faders, even one
            # that arrives just after a switch was started.
            layer = self._layers[self._active] if self._layers else None
            if layer is None or layer.muted[idx] == muted:
                return  # already known: a re-confirmation or a switch replay
            layer.muted[idx] = muted
        log.info("fader %d: %s", idx + 1, "muted" if muted else "unmuted")
        self._dispatch_to(layer, "on_mute", idx, muted)

    def _handle_rail(self, payload):
        parts = payload.split(",", 1)
        ok = parts[0].strip() == "ok"
        try:
            volts = float(parts[1]) if len(parts) > 1 else None
        except ValueError:
            volts = None
        with self._lock:
            self.rail_ok = ok
            self.rail_volts = volts
        if not ok:
            sys.stderr.write(
                f"[faders] VM rail check FAILED at {volts} V — motors "
                "disabled by firmware. Check the HUSB238 9 V jumper.\n"
            )
        self._dispatch_all("on_rail", ok, volts)

    def _writer_loop(self):
        while not self._stop.is_set():
            time.sleep(SET_FLUSH_INTERVAL)
            if self._ser is None:
                continue  # keep pending setpoints until the Pico is back
            with self._lock:
                if all(p is None for p in self._pending):
                    continue
                # Only fill slots an extension actually requested this tick.
                # Empty slots tell the Pico to leave that fader alone, so we
                # never re-command a fader the user is holding.
                slots = []
                for idx, val in enumerate(self._pending):
                    if val is None:
                        slots.append("")
                    else:
                        self._setpoints[idx] = val
                        self._pending[idx] = None
                        slots.append(f"{val:.1f}")
                line = "SET:" + ",".join(slots)
            log.debug("send %s", line)
            self._send(line)
