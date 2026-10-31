"""
Host tests — protocol parsing, button dispatch, mute mirroring, display
de-duplication, and the pulse_sink extension's pactl interaction.

Run with: python3 tests/test_host.py
"""

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES = []
PASSES = [0]


def check(condition, label):
    if condition:
        PASSES[0] += 1
    else:
        FAILURES.append(label)
        print("  FAIL: " + label)


# ---------------------------------------------------------------------------
# `serial` is not installed in every environment and the tests never touch a
# real port, so stub the module before importing faders.core.
# ---------------------------------------------------------------------------

class _SerialException(Exception):
    pass


class FakeSerial:
    def __init__(self, *a, **k):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data.decode("ascii"))

    def read(self, n):
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True

    def lines(self):
        return [ln for ln in "".join(self.written).split("\n") if ln]


def _install_serial_stub():
    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = FakeSerial
    serial_mod.SerialException = _SerialException
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    list_ports.comports = lambda: []
    tools.list_ports = list_ports
    serial_mod.tools = tools
    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools
    sys.modules["serial.tools.list_ports"] = list_ports


_install_serial_stub()

from faders.core import DISPLAY_LINE_MAX, Extension, FaderHost, _sanitize  # noqa: E402


class RecordingExtension(Extension):
    def __init__(self):
        self.positions = []
        self.mutes = []
        self.buttons = []
        self.rails = []
        self.calibrations = []
        self.connects = 0

    def on_connect(self):
        self.connects += 1

    def on_position(self, idx, value):
        self.positions.append((idx, value))

    def on_mute(self, idx, muted):
        self.mutes.append((idx, muted))

    def on_button(self, button):
        self.buttons.append(button)

    def on_rail(self, ok, volts):
        self.rails.append((ok, volts))

    def on_calibration(self, phase):
        self.calibrations.append(phase)


def make_host(num_faders=4):
    host = FaderHost(port="/dev/null", num_faders=num_faders)
    host._ser = FakeSerial()
    ext = RecordingExtension()
    host.register(ext)
    return host, ext


# ---------------------------------------------------------------------------

def test_position_parsing_four_faders():
    print("POS parsing")
    host, ext = make_host()
    host._handle_line("POS:10.0,20.0,30.0,40.0")
    check(ext.positions == [(0, 10.0), (1, 20.0), (2, 30.0), (3, 40.0)],
          f"all four positions dispatched (got {ext.positions})")

    # A short line must not raise or invent values.
    ext.positions.clear()
    host._handle_line("POS:50.0,60.0")
    check(ext.positions == [(0, 50.0), (1, 60.0)], "short POS: line handled")

    # Garbage slots are skipped individually, not fatally.
    ext.positions.clear()
    host._handle_line("POS:1.0,bad,3.0,4.0")
    check(ext.positions == [(0, 1.0), (2, 3.0), (3, 4.0)],
          f"unparseable slot skipped (got {ext.positions})")


def test_loopback_suppression():
    print("Loopback guard")
    host, ext = make_host()
    host.set_fader(1, 80.0)
    host._handle_line("POS:0,80.0,0,0")
    check((1, 80.0) not in ext.positions,
          "position echoed back during our own move is suppressed")

    host._handle_line("STATE:2,IDLE")
    host._handle_line("POS:0,80.0,0,0")
    check((1, 80.0) in ext.positions,
          "position forwarded again once the Pico reports IDLE")


def test_button_toggles_mute():
    print("Button dispatch")
    host, ext = make_host()
    host._handle_line("BTN:F3")
    sent = host._ser.lines()
    check("MUTE:3,1" in sent, f"F3 press sends MUTE:3,1 (got {sent})")
    check(ext.buttons == ["F3"], "raw button event reaches extensions")

    # The Pico confirms; only then is the state considered real.
    check(host.is_muted(2) is False, "state not assumed before confirmation")
    host._handle_line("MUTE:3,1")
    check(host.is_muted(2) is True, "confirmation updates host state")
    check(ext.mutes == [(2, True)], f"on_mute dispatched (got {ext.mutes})")

    # Second press unmutes.
    host._ser.written.clear()
    host._handle_line("BTN:F3")
    check("MUTE:3,0" in host._ser.lines(), "second press unmutes")
    host._handle_line("MUTE:3,0")
    check(host.is_muted(2) is False, "unmute confirmation applied")


def test_each_fader_button_maps_to_its_own_fader():
    host, ext = make_host()
    for n in (1, 2, 3, 4):
        host._ser.written.clear()
        host._handle_line(f"BTN:F{n}")
        check(f"MUTE:{n},1" in host._ser.lines(),
              f"F{n} mutes fader {n}, not another")


def test_general_buttons_have_no_function_yet():
    print("General buttons")
    host, ext = make_host()
    host._handle_line("BTN:G1")
    host._handle_line("BTN:G2")
    check(host._ser.lines() == [],
          f"G1/G2 send nothing to the Pico (got {host._ser.lines()})")
    check(not any(host.is_muted(i) for i in range(4)),
          "G1/G2 mute nothing")
    check(ext.buttons == ["G1", "G2"],
          "G1/G2 still reach extensions, for a future layer switch")


def test_g2_held_fader_button_swaps_ab():
    print("A/B swap")
    host, ext = make_host()
    host._handle_line("POS:30.0,50.0,50.0,50.0")
    ext.positions.clear()

    host._handle_line("BTN:G2")
    host._handle_line("BTN:F1")
    check(host._ser.lines() == ["SLOT:1,B"],
          f"first swap only marks A and shows B (got {host._ser.lines()})")
    check(host._pending[0] is None, "first swap moves nothing")
    check(not host.is_muted(0), "G2 + F1 does not mute")
    host._handle_line("BTNUP:F1")

    # User moves fader 1 to the B level by hand.
    host._handle_line("POS:70.0,50.0,50.0,50.0")
    ext.positions.clear()
    host._handle_line("BTN:F1")
    check(host._pending[0] == 30.0, f"second swap returns to A (got {host._pending[0]})")
    check(host._ser.lines()[-1] == "SLOT:1,A", "badge shows A")
    check(ext.positions == [(0, 30.0)], f"extensions told the new level (got {ext.positions})")
    check(not any(host._pending[1:]), "only fader 1 moves")

    # Third swap goes back to where B was left.
    host._handle_line("BTNUP:F1")
    host._handle_line("POS:30.0,50.0,50.0,50.0")
    host._handle_line("BTN:F1")
    check(host._pending[0] == 70.0, f"third swap returns to B (got {host._pending[0]})")
    check(host._ser.lines()[-1] == "SLOT:1,B", "badge shows B")

    # A rebooted Pico gets its badges back.
    host._ser.written.clear()
    host._handle_line("CAL:done")
    check(host._ser.lines() == ["SLOT:1,B"],
          f"slots resent after calibration (got {host._ser.lines()})")

    # Released G2: fader buttons mute again.
    host._handle_line("BTNUP:G2")
    host._handle_line("BTN:F2")
    check("MUTE:2,1" in host._ser.lines(), "F2 mutes again once G2 is released")


def test_ab_swap_ignored_while_muted():
    host, ext = make_host()
    host._handle_line("MUTE:1,1")
    host._handle_line("POS:0.0,50.0,50.0,50.0")
    host._handle_line("BTN:G2")
    host._handle_line("BTN:F1")
    host._handle_line("BTN:F1")
    check(host._pending[0] is None and host._ser.lines() == [],
          "swap on a muted fader does nothing")


def test_g1_held_is_not_the_swap_modifier():
    host, ext = make_host()
    host._handle_line("BTN:G1")
    for n in (1, 2, 3, 4):
        host._handle_line(f"BTN:F{n}")
        host._handle_line(f"BTNUP:F{n}")
    check(host._ser.lines() == [],
          f"G1 + Fn neither mutes nor swaps, single layer (got {host._ser.lines()})")
    check(host._target is None, "one layer: G1 + F1/F4 switches nothing")


def test_g1_g2_held_fader_button_does_nothing():
    host, ext = make_host()
    host._handle_line("BTN:G1")
    host._handle_line("BTN:G2")
    host._handle_line("BTN:F2")
    check(host._ser.lines() == [], "G1 + G2 + F2 is ambiguous: nothing sent")


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

class PagedExtension(RecordingExtension):
    PAGED = True

    def __init__(self):
        super().__init__()
        self.pages = []
        self.activations = []

    def on_page(self, delta):
        self.pages.append(delta)

    def on_activate(self):
        self.activations.append("on")

    def on_deactivate(self):
        self.activations.append("off")


def make_layered_host():
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    audio, chat = host.add_layer("Audio"), host.add_layer("Chat")
    a, b = RecordingExtension(), PagedExtension()
    audio.register(a)
    chat.register(b)
    return host, audio, chat, a, b


def test_inactive_layer_writes_are_stored_not_sent():
    print("Layers")
    host, audio, chat, a, b = make_layered_host()
    chat.set_fader(0, 80.0)
    chat.set_display(0, "Alice", "vol 160%")
    chat.set_mute(1, True, 40.0)
    chat.set_creases(0, [(50, 70, 18)])
    check(host._ser.lines() == [] and host._pending == [None] * 4,
          f"inactive layer reaches nothing (got {host._ser.lines()})")
    check(chat.is_muted(1) and not audio.is_muted(1),
          "mute stored on the inactive layer only")
    check(not chat.is_active() and audio.is_active(), "first layer active")

    audio.set_display(0, "Music", "vol 50%")
    check(host._ser.lines() == ["DISP:1,Music,vol 50%"], "active layer writes through")


def test_layer_switch_applies_on_g1_release():
    host, audio, chat, a, b = make_layered_host()
    audio.set_fader(0, 30.0)
    host._pending = [None] * 4
    host._setpoints[0] = 30.0
    chat.set_fader(0, 80.0)
    chat.set_display(0, "Alice", "vol 160%")
    host._ser.written.clear()

    host._handle_line("BTN:G1")
    host._handle_line("BTN:F4")
    sent = host._ser.lines()
    check("DISP:2,Chat,layer 2/2" in sent, f"target layer named at once (got {sent})")
    check(not any(ln.startswith(("MUTE:", "CREASES:")) for ln in sent)
          and host._pending == [None] * 4, "no motion while G1 is held")
    check(not audio.is_active() and not chat.is_active(), "no layer live mid-switch")

    # Input mid-switch goes nowhere.
    host._handle_line("POS:55.0,0,0,0")
    host._handle_line("BTN:F2")
    check(a.positions == [] and b.positions == [], "positions dropped mid-switch")
    check(not any(ln.startswith("MUTE:") for ln in host._ser.lines()),
          "fader button does not mute mid-switch")

    check(host._target is not None, "still pending while G1 is held")
    host._handle_line("BTNUP:G1")
    check(chat.is_active(), "target layer active on G1 release")
    check(host._pending[0] == 80.0, f"fader moves to the layer level (got {host._pending})")
    check("DISP:1,Alice,vol 160%" in host._ser.lines(), "layer screen restored")
    check(b.activations == ["on"], "on_activate dispatched")


def test_repeated_steps_apply_only_the_last():
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layers = [host.add_layer(n) for n in ("A", "B", "C")]
    exts = [RecordingExtension() for _ in layers]
    for layer, ext in zip(layers, exts):
        layer.register(ext)
    layers[1].set_fader(0, 90.0)
    layers[2].set_fader(0, 10.0)
    host._setpoints[0] = 50.0
    host._handle_line("BTN:G1")
    host._handle_line("BTN:F4")
    host._handle_line("BTN:F4")
    check("DISP:2,C,layer 3/3" in host._ser.lines(), "second step names C")
    check(host._pending[0] is None, "intermediate layer B never moves the faders")
    host._handle_line("BTNUP:G1")
    check(layers[2].is_active() and host._pending[0] == 10.0,
          "only the final layer is applied")

    # Wrap backwards: F1 from A goes to the last layer.
    host2, audio, chat, a, b = make_layered_host()
    host2._handle_line("BTN:G1")
    host2._handle_line("BTN:F1")
    host2._handle_line("BTNUP:G1")
    check(chat.is_active(), "G1 + F1 steps back and wraps")


def test_stepping_back_to_the_active_layer_does_not_move():
    host, audio, chat, a, b = make_layered_host()
    audio.set_display(0, "Music", "vol 50%")
    audio.set_fader(0, 30.0)
    chat.set_fader(0, 80.0)
    host._handle_line("BTN:G1")
    host._handle_line("BTN:F4")
    host._handle_line("BTN:F1")
    host._ser.written.clear()
    host._handle_line("BTNUP:G1")
    check(audio.is_active(), "back on the first layer")
    check(host._pending[0] == 30.0, "own queued level untouched, nothing new queued")
    check(host._ser.lines() == ["DISP:1,Music,vol 50%"] + [f"DISP:{i},," for i in (2, 3, 4)],
          f"only screens restored (got {host._ser.lines()})")
    check(a.mutes == [] and b.activations == [], "no activation for a no-op switch")


def test_g1_hint_names_neighbour_layers():
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layers = [host.add_layer(n) for n in ("A", "B", "C")]
    for layer in layers:
        layer.register(RecordingExtension())
    layers[0].set_display(0, "Music", "vol 50%")
    host._ser.written.clear()

    host._handle_line("BTN:G1")
    check(host._ser.lines() == ["DISP:1,C,< prev 3/3", "DISP:4,B,next > 2/3"],
          f"G1 names previous / next layer on screens 1 and 4 (got {host._ser.lines()})")
    layers[0].set_display(0, "Music", "vol 60%")
    check(host._display[0] == ("C", "< prev 3/3"), "layer writes do not cover the hint")

    host._ser.written.clear()
    host._handle_line("BTN:F4")
    sent = host._ser.lines()
    check("DISP:1,A,< prev 1/3" in sent and "DISP:4,C,next > 3/3" in sent,
          f"hint follows the step target (got {sent})")
    check("DISP:2,B,layer 2/3" in sent, "middle screens name the target")

    host._ser.written.clear()
    host._handle_line("BTNUP:G1")
    check(layers[1].is_active()
          and host._ser.lines() == [f"DISP:{i},," for i in (1, 2, 3, 4)],
          f"release applies the target's screens (got {host._ser.lines()})")

    host2, audio, chat, a, b = make_layered_host()
    audio.set_display(0, "Music", "vol 60%")
    host2._handle_line("BTN:G1")
    host2._ser.written.clear()
    host2._handle_line("BTNUP:G1")
    check(host2._ser.lines() == ["DISP:1,Music,vol 60%", "DISP:4,,"],
          f"release restores the layer screens (got {host2._ser.lines()})")

    host3, _ = make_host()
    host3._handle_line("BTN:G1")
    check(host3._ser.lines() == [], "one layer: no hint")


def test_switch_replays_mute_with_level():
    host, audio, chat, a, b = make_layered_host()
    # Fader 1 muted on Audio (confirmed by the Pico), level 60 to return to.
    audio.set_fader(0, 60.0)
    host._handle_line("MUTE:1,1")
    check(a.mutes == [(0, True)], "confirmation reaches the live layer")
    chat.set_fader(0, 80.0)
    chat.set_mute(1, True, 40.0)
    chat.set_creases(0, [(50, 70, 18)])
    host._ser.written.clear()

    host.step_layer(1)
    sent = host._ser.lines()
    check("MUTE:1,0,80.0" in sent, f"unmuted on Chat: unmute straight to its level (got {sent})")
    check("MUTE:2,1,40.0" in sent, "muted on Chat: park, restore its level")
    check("CREASES:1,50.0:70:18" in sent, "Chat creases applied")

    # The Pico's confirmations of the replay are not user actions.
    host._handle_line("MUTE:1,0")
    host._handle_line("MUTE:2,1")
    check(b.mutes == [] and a.mutes == [(0, True)],
          f"replay confirmations not dispatched (got {b.mutes}, {a.mutes})")

    host._ser.written.clear()
    host.step_layer(1)
    sent = host._ser.lines()
    check("MUTE:1,1,60.0" in sent and "MUTE:2,0" in sent,
          f"back on Audio: mute and level restored (got {sent})")
    check("CREASES:1" in sent, "creases cleared for a layer without them")
    check(b.activations == ["on", "off"], "on_deactivate on the way out")


def test_hand_level_kept_per_layer():
    host, audio, chat, a, b = make_layered_host()
    host._handle_line("POS:42.0,0,0,0")
    check((0, 42.0) in a.positions, "hand move reaches the live layer")
    check(audio.setpoint[0] == 42.0, "and is remembered as its level")
    host.step_layer(1)
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:70.0,0,0,0")
    check((0, 70.0) in b.positions and (0, 70.0) not in a.positions,
          "moves now go to the second layer only")
    host._pending = [None] * 4
    host.step_layer(1)
    check(host._pending[0] == 42.0, "first layer's hand level restored")


def test_g2_pages_on_a_paged_layer():
    host, audio, chat, a, b = make_layered_host()
    host.step_layer(1)
    host._ser.written.clear()
    host._handle_line("BTN:G2")
    for n in (1, 2, 3, 4):
        host._handle_line(f"BTN:F{n}")
        host._handle_line(f"BTNUP:F{n}")
    check(b.pages == [-1, 1], f"G2 + F1/F4 page back/forward (got {b.pages})")
    check(host._ser.lines() == [], "no A/B swap, no mute on a paged layer")


def test_creases_sent_and_replayed():
    print("Creases")
    host, ext = make_host()
    host.set_creases(1, [(66.666, 80, 40), (10, 45, 12)])
    check(host._ser.lines() == ["CREASES:2,66.7:80:40,10.0:45:12"],
          f"set_creases sends one CREASES: line (got {host._ser.lines()})")

    host.set_display(0, "Music", "vol 50%")
    host._ser.written.clear()
    host._handle_line("CAL:done")
    sent = host._ser.lines()
    check("CREASES:2,66.7:80:40,10.0:45:12" in sent, "creases replayed after reboot")
    check("DISP:1,Music,vol 50%" in sent, "display text replayed after reboot")

    host.set_creases(1, [])
    check(host._ser.lines()[-1] == "CREASES:2", "empty list clears")


def test_sends_before_connect_are_replayed():
    # Extensions register before run() opens the port.
    host = FaderHost(port="/dev/null", num_faders=4)
    host.set_display(2, "Comms", "")
    host.set_creases(2, [(50, 45, 12)])
    host._ser = FakeSerial()
    host._resend_after_boot()
    check(host._ser.written[0] == "\n",
          "a bare newline goes first, flushing the Pico's partial line")
    sent = host._ser.lines()
    check("DISP:3,Comms," in sent and "CREASES:3,50.0:45:12" in sent,
          f"pre-connect display and creases sent on connect (got {sent})")


def test_stuck_loopback_guard_is_released():
    print("Guard watchdog")
    import faders.core as core
    host, ext = make_host()
    clock = [1000.0]
    original = core.time.monotonic
    core.time.monotonic = lambda: clock[0]
    try:
        host.set_fader(0, 60.0)          # SET: lost on the wire: no STATE:
        host._handle_line("POS:40.0,0,0,0")
        check((0, 40.0) not in ext.positions, "guarded while the move is young")

        # A real move that is still running must stay guarded.
        host.set_fader(1, 60.0)
        host._handle_line("STATE:2,MOVING")

        clock[0] += core.GUARD_TIMEOUT_S + 0.5
        ext.positions.clear()
        host._handle_line("POS:42.0,45.0,0,0")
        check((0, 42.0) in ext.positions,
              f"guard on an IDLE fader released after timeout (got {ext.positions})")
        check((1, 45.0) not in ext.positions,
              "guard on a MOVING fader kept, however long")
    finally:
        core.time.monotonic = original


def test_concurrent_sends_stay_whole_lines():
    import threading, time as _time

    class SlowSerial(FakeSerial):
        def write(self, data):
            # Split each write so an unlocked interleave would show.
            text = data.decode("ascii")
            half = len(text) // 2
            self.written.append(text[:half])
            _time.sleep(0.0005)
            self.written.append(text[half:])

    host, ext = make_host()
    host._ser = SlowSerial()
    lines = [f"DISP:{i % 4 + 1},thread {t},line {i}" for t in range(4) for i in range(25)]
    threads = [threading.Thread(target=lambda t=t: [host._send(l) for l in lines if f"thread {t}," in l])
               for t in range(4)]
    for th in threads: th.start()
    for th in threads: th.join()
    check(sorted(host._ser.lines()) == sorted(lines),
          "lines from concurrent threads never interleave")


def test_replug_reconnects_and_restores():
    print("Reconnect")
    import threading
    import faders.core as core

    class DyingSerial(FakeSerial):
        """Reads nothing until `unplug` is set, then fails like a yanked port."""
        def __init__(self):
            super().__init__()
            self.unplug = threading.Event()

        def read(self, n):
            if self.unplug.wait(0.01):
                raise _SerialException("device disconnected")
            return b""

    first, second = DyingSerial(), DyingSerial()
    # Absent at start, then present, then gone for a while, then back.
    plan = [None, None, first, None, None, second]

    def fake_connect():
        return plan.pop(0) if plan else None

    original_interval = core.RECONNECT_INTERVAL_S
    core.RECONNECT_INTERVAL_S = 0.01
    host = FaderHost(port="/dev/null", num_faders=4)
    ext = RecordingExtension()
    host.register(ext)
    host._connect = fake_connect
    host.set_display(1, "Apps", "vol 40%")
    host.set_creases(1, [(50, 45, 12)])
    runner = threading.Thread(target=host.run, daemon=True)
    try:
        runner.start()
        _wait_for(lambda: host._ser is first)
        check(ext.connects == 1, "on_connect after waiting for an absent Pico")
        check("DISP:2,Apps,vol 40%" in first.lines(),
              "display replayed on first connect")

        host._handle_line("MUTE:2,1")
        host.set_fader(2, 70.0)
        first.unplug.set()
        _wait_for(lambda: host._ser is second)
        check(not host._stop.is_set(), "unplug does not stop the host")
        check(first.closed, "dead port closed")
        check(ext.connects == 2, "on_connect again after the replug")
        sent = second.lines()
        check("DISP:2,Apps,vol 40%" in sent and "CREASES:2,50.0:45:12" in sent,
              f"display and creases replayed to the rebooted Pico (got {sent})")
        check(not host.is_muted(1), "mute mirror reset: a rebooted Pico is unmuted")
        check(host._self_driven == [False] * 4, "loopback guards released")
    finally:
        host.stop()
        runner.join(2.0)
        core.RECONNECT_INTERVAL_S = original_interval
    check(not runner.is_alive(), "run() returns after stop()")


def test_write_failure_disconnects_without_stopping():
    class BrokenSerial(FakeSerial):
        def write(self, data):
            raise _SerialException("write failed")

    host, ext = make_host()
    broken = BrokenSerial()
    host._ser = broken
    host.set_display(0, "Music", "")
    check(host._ser is None and broken.closed, "write failure drops the port")
    check(not host._stop.is_set(), "write failure does not stop the host")
    host.set_display(0, "Music", "x")  # no port: dropped quietly, no raise
    check(host._display[0] == ("Music", "x"), "display still tracked for replay")


def _wait_for(cond, timeout=2.0):
    import time as _time
    end = _time.monotonic() + timeout
    while not cond() and _time.monotonic() < end:
        _time.sleep(0.005)


def test_unknown_button_is_ignored():
    host, ext = make_host()
    for bad in ("BTN:", "BTN:F9", "BTN:F0", "BTN:X1", "BTN:FX"):
        host._handle_line(bad)
    check(host._ser.lines() == [], f"out-of-range buttons send nothing")
    check(not any(host.is_muted(i) for i in range(4)), "nothing muted")


def test_mute_confirmation_is_idempotent():
    print("MUTE mirroring")
    host, ext = make_host()
    host._handle_line("MUTE:1,1")
    host._handle_line("MUTE:1,1")
    host._handle_line("MUTE:1,1")
    check(ext.mutes == [(0, True)],
          f"repeated identical confirmations dispatch once (got {ext.mutes})")

    for bad in ("MUTE:9,1", "MUTE:0,1", "MUTE:x,1", "MUTE:1"):
        host._handle_line(bad)
    check(ext.mutes == [(0, True)], "malformed MUTE: lines ignored")


def test_set_mute_guards_loopback():
    host, ext = make_host()
    host.set_mute(0, True)
    host._handle_line("POS:0.0,0,0,0")
    check((0, 0.0) not in ext.positions,
          "the fader motion a mute causes does not echo into extensions")


def test_display_dedupe_and_sanitize():
    print("Display")
    host, ext = make_host()
    host.set_display(0, "sink-music", "vol 78%")
    check(host._ser.lines() == ["DISP:1,sink-music,vol 78%"],
          f"display text sent (got {host._ser.lines()})")

    host._ser.written.clear()
    host.set_display(0, "sink-music", "vol 78%")
    check(host._ser.lines() == [], "identical text is not re-sent")

    host._ser.written.clear()
    host.set_display(0, "sink-music", "vol 79%")
    check(host._ser.lines() == ["DISP:1,sink-music,vol 79%"],
          "changed text is sent")

    # Commas would corrupt a comma-delimited frame.
    host._ser.written.clear()
    host.set_display(1, "a,b", "c,d")
    check(host._ser.lines() == ["DISP:2,a b,c d"],
          f"commas replaced (got {host._ser.lines()})")

    # Newlines would split the frame into two lines.
    host._ser.written.clear()
    host.set_display(2, "x\ny", "p\rq")
    sent = host._ser.lines()
    check(len(sent) == 1 and sent[0] == "DISP:3,x y,p q",
          f"newlines neutralised (got {sent})")

    check(_sanitize("x" * 80) == "x" * DISPLAY_LINE_MAX,
          "over-long text trimmed to the display width")
    check(_sanitize(None) == "", "None becomes empty text")

    host._ser.written.clear()
    host.set_display(9, "nope", "nope")
    check(host._ser.lines() == [], "out-of-range display index ignored")


def test_rail_handling():
    print("RAIL")
    host, ext = make_host()
    host._handle_line("RAIL:ok,9.01")
    check(host.rail_ok is True and abs(host.rail_volts - 9.01) < 1e-6,
          "rail ok recorded with voltage")
    check(ext.rails == [(True, 9.01)], "on_rail dispatched")

    host, ext = make_host()
    host._handle_line("RAIL:fail,5.04")
    check(host.rail_ok is False, "rail failure recorded")
    check(ext.rails == [(False, 5.04)], "failure dispatched to extensions")


def test_extension_exception_does_not_kill_dispatch():
    print("Isolation")

    class Exploding(Extension):
        def on_position(self, idx, value):
            raise RuntimeError("boom")

    host, ext = make_host()
    host.register(Exploding())
    host._handle_line("POS:5.0,0,0,0")
    check((0, 5.0) in ext.positions,
          "one failing extension does not stop the others")


def test_set_flush_builds_sparse_lines():
    print("SET writer")
    host, ext = make_host()
    host.set_fader(2, 42.0)
    # Drive one writer iteration by hand rather than starting the thread.
    with host._lock:
        slots = []
        for idx, val in enumerate(host._pending):
            if val is None:
                slots.append("")
            else:
                host._pending[idx] = None
                slots.append(f"{val:.1f}")
        line = "SET:" + ",".join(slots)
    check(line == "SET:,,42.0,", f"only the requested slot is filled (got {line})")


# ---------------------------------------------------------------------------
# pulse_sink
# ---------------------------------------------------------------------------

def test_pulse_sink_display_and_mute():
    print("pulse_sink")
    import faders.extensions.pulse_sink as ps

    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(cmd)
        def terminate(self):
            pass

    outputs = {
        "get-sink-volume": "Volume: front-left: 45000 /  69% / -9.00 dB\n",
        "get-sink-mute": "Mute: no\n",
        "list": "0\tsink-music\tmodule\ts16le\tRUNNING\n",
    }

    def fake_check_output(cmd, **kwargs):
        for key, value in outputs.items():
            if key in cmd:
                return value
        raise AssertionError(f"unexpected command {cmd}")

    original_popen = ps.subprocess.Popen
    original_check = ps.subprocess.check_output
    original_thread = ps.threading.Thread
    ps.subprocess.Popen = FakePopen
    ps.subprocess.check_output = fake_check_output
    # Don't start the real subscribe thread in a test.
    ps.threading.Thread = lambda *a, **k: types.SimpleNamespace(
        start=lambda: None, join=lambda *a, **k: None)
    try:
        host, _ = make_host()
        ps.register(host, {"fader": 0, "sink": "sink-music", "label": "Music"})
        ext = host._extensions[-1]

        layout = host._creases[0]
        check([round(p) for p, _, _ in layout] == [25, 50, 75],
              f"0-100 sink: creases every 25% (got {[round(p, 1) for p, _, _ in layout]})")
        check(all(c[1:] == ps.CREASE_NORMAL for c in layout),
              "no boost crease on a sink that stops at 100%")

        boost = ps.PulseSinkExtension.__new__(ps.PulseSinkExtension)
        boost.vol_min, boost.vol_max = 0.0, 150.0
        layout = boost.crease_layout()
        check([round(p, 1) for p, _, _ in layout] == [16.7, 33.3, 50.0, 66.7, 83.3],
              f"0-150 sink: volume 25..125 in steps of 25 (got {[round(p, 1) for p, _, _ in layout]})")
        big = [p for p, w, ms in layout if (w, ms) == ps.CREASE_BIG]
        check(len(big) == 1 and abs(big[0] - 66.67) < 0.1,
              f"0-150 sink: big crease where 100% volume sits (got {big})")

        sent = host._ser.lines()
        check(any(ln.startswith("DISP:1,Music,vol 69%") for ln in sent),
              f"sink label and volume pushed to the display (got {sent})")
        check(ext.label == "Music", "config label used over the sink name")

        # Mute from a button press.
        calls.clear()
        ext.on_mute(0, True)
        check(["pactl", "set-sink-mute", "sink-music", "1"] in calls,
              f"mute calls pactl set-sink-mute 1 (got {calls})")

        calls.clear()
        ext.on_mute(0, False)
        check(["pactl", "set-sink-mute", "sink-music", "0"] in calls,
              "unmute calls pactl set-sink-mute 0")

        # A mute for a different fader must be ignored.
        calls.clear()
        ext.on_mute(2, True)
        check(calls == [], "mute for another fader ignored")

        # While muted, fader movement must not write volume.
        ext._last_mute = True
        calls.clear()
        ext.on_position(0, 55.0)
        check(calls == [],
              f"moving a muted fader does not change sink volume (got {calls})")

        # Once unmuted it writes again.
        ext._last_mute = False
        ext._last_written_vol = None
        ext._last_write_t = 0.0
        calls.clear()
        ext.on_position(0, 55.0)
        check(any("set-sink-volume" in c for c in calls),
              f"unmuted movement writes volume (got {calls})")

        # Default label falls back to the sink name.
        host2, _ = make_host()
        ps.register(host2, {"fader": 1, "sink": "sink-apps"})
        check(host2._extensions[-1].label == "sink-apps",
              "label defaults to the sink name")
    finally:
        ps.subprocess.Popen = original_popen
        ps.subprocess.check_output = original_check
        ps.threading.Thread = original_thread


def test_pulse_sink_reads_mute_at_startup():
    import faders.extensions.pulse_sink as ps

    outputs = {
        "get-sink-volume": "Volume: front-left: 45000 /  50% / -9.00 dB\n",
        "get-sink-mute": "Mute: yes\n",
        "list": "0\tsink-music\tmodule\ts16le\tRUNNING\n",
    }

    def fake_check_output(cmd, **kwargs):
        for key, value in outputs.items():
            if key in cmd:
                return value
        raise AssertionError(cmd)

    original_check = ps.subprocess.check_output
    original_popen = ps.subprocess.Popen
    original_thread = ps.threading.Thread
    ps.subprocess.check_output = fake_check_output
    ps.subprocess.Popen = lambda *a, **k: types.SimpleNamespace(terminate=lambda: None)
    ps.threading.Thread = lambda *a, **k: types.SimpleNamespace(
        start=lambda: None, join=lambda *a, **k: None)
    try:
        host, _ = make_host()
        ps.register(host, {"fader": 0, "sink": "sink-music"})
        check(not any(ln.startswith("MUTE:") for ln in host._ser.lines()),
              "nothing seeded before the Pico is connected")
        host._extensions[-1].on_connect()
        check(any("MUTE:1,1" in ln for ln in host._ser.lines()),
              f"a sink already muted at startup mutes the fader on connect "
              f"(got {host._ser.lines()})")
        check(host._pending[0] == 50.0,
              "fader driven to the sink volume on connect")
    finally:
        ps.subprocess.check_output = original_check
        ps.subprocess.Popen = original_popen
        ps.threading.Thread = original_thread


def test_config_defaults_to_four_faders():
    print("Config")
    import json
    from faders import config as config_mod
    check(config_mod.DEFAULT_CONFIG["num_faders"] == 4,
          "config default is 4 faders")
    cfg = json.load(open(ROOT / "faders" / "config.json"))
    check(cfg["num_faders"] == 4, "shipped config declares 4 faders")
    entries = cfg["extensions"]["pulse_sink"]
    check(sorted(e["fader"] for e in entries) == [0, 1, 2, 3],
          "shipped config binds all four faders")
    check(all("label" in e for e in entries),
          "every shipped entry has a display label")


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_position_parsing_four_faders,
        test_loopback_suppression,
        test_button_toggles_mute,
        test_each_fader_button_maps_to_its_own_fader,
        test_general_buttons_have_no_function_yet,
        test_g2_held_fader_button_swaps_ab,
        test_ab_swap_ignored_while_muted,
        test_g1_held_is_not_the_swap_modifier,
        test_g1_g2_held_fader_button_does_nothing,
        test_inactive_layer_writes_are_stored_not_sent,
        test_layer_switch_applies_on_g1_release,
        test_repeated_steps_apply_only_the_last,
        test_stepping_back_to_the_active_layer_does_not_move,
        test_g1_hint_names_neighbour_layers,
        test_switch_replays_mute_with_level,
        test_hand_level_kept_per_layer,
        test_g2_pages_on_a_paged_layer,
        test_creases_sent_and_replayed,
        test_sends_before_connect_are_replayed,
        test_stuck_loopback_guard_is_released,
        test_concurrent_sends_stay_whole_lines,
        test_replug_reconnects_and_restores,
        test_write_failure_disconnects_without_stopping,
        test_unknown_button_is_ignored,
        test_mute_confirmation_is_idempotent,
        test_set_mute_guards_loopback,
        test_display_dedupe_and_sanitize,
        test_rail_handling,
        test_extension_exception_does_not_kill_dispatch,
        test_set_flush_builds_sparse_lines,
        test_pulse_sink_display_and_mute,
        test_pulse_sink_reads_mute_at_startup,
        test_config_defaults_to_four_faders,
    ]
    for test in tests:
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED, {PASSES[0]} passed")
        for failure in FAILURES:
            print("  - " + failure)
        return 1
    print(f"all {PASSES[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
