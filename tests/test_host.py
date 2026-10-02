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
        check(any("MUTE:1,1" in ln for ln in host._ser.lines()),
              f"a sink already muted at startup mutes the fader "
              f"(got {host._ser.lines()})")
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
