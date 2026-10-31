"""
voicemeeter tests — gain/mute both ways, labels, creases and Voicemeeter
coming and going, against a fake Remote API.

Run with: python3 tests/test_voicemeeter.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_host import FakeSerial, check, FAILURES, PASSES  # noqa: E402

from faders.core import FaderHost  # noqa: E402
from faders.extensions import voicemeeter as vm  # noqa: E402


class FakeRemote:
    """Banana with every strip/bus at 0 dB, unmuted, unlabelled."""

    def __init__(self):
        self.running = True
        self.params = {}
        self.strings = {}
        self.sets = []
        self._dirty = False

    def login(self):
        return 0

    def logout(self):
        pass

    def dirty(self):
        if not self.running:
            return -2
        d, self._dirty = self._dirty, False
        return 1 if d else 0

    def kind(self):
        return 2

    def get_float(self, name):
        return self.params.get(name, 0.0) if self.running else None

    def get_string(self, name):
        return self.strings.get(name, "") if self.running else None

    def set_float(self, name, value):
        self.sets.append((name, round(value, 2)))
        self.params[name] = value
        return 0

    def change(self, name, value):
        """A change made in Voicemeeter's window."""
        self.params[name] = value
        self._dirty = True


def make(entries, remote=None):
    remote = remote or FakeRemote()
    client = vm.Client(remote, start=False)
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Voicemeeter")
    exts = []
    for e in entries:
        kind = "Strip" if "strip" in e else "Bus"
        ext = vm.VoicemeeterExtension(
            layer, e["fader"], kind, e.get("strip", e.get("bus")),
            gain_min=e.get("min", vm.GAIN_MIN_DB),
            gain_max=e.get("max", vm.GAIN_MAX_DB),
            label=e.get("label"), client=client)
        layer.register(ext)
        exts.append(ext)
    return host, layer, client, remote, exts


# ---------------------------------------------------------------------------

def test_waits_for_voicemeeter_then_syncs():
    print("Connect")
    remote = FakeRemote()
    remote.running = False
    remote.params["Strip[0].Gain"] = -24.0
    remote.params["Bus[0].Mute"] = 1.0
    remote.strings["Strip[0].Label"] = "Mic"
    host, layer, client, remote, exts = make(
        [{"fader": 0, "strip": 0}, {"fader": 1, "strip": 3},
         {"fader": 3, "bus": 0, "max": 0}], remote)
    client.poll_once()
    check(host._display[0] == ("Strip[0]", "no VM"),
          f"no VM shown while it is not running (got {host._display[0]})")
    check(host._pending == [None] * 4, "nothing driven without Voicemeeter")

    remote.running = True
    client.poll_once()
    check(host._display[0] == ("Mic", "-24.0 dB"),
          f"label from Voicemeeter, gain shown (got {host._display[0]})")
    check(host._display[1] == ("VAIO", "0.0 dB"),
          f"default Banana strip name (got {host._display[1]})")
    check(host._display[3] == ("A1", "0.0 dB"),
          f"default Banana bus name (got {host._display[3]})")
    check(abs(host._pending[0] - 50.0) < 0.01,
          f"-24 dB of -60..+12 -> 50% (got {host._pending[0]})")
    check("MUTE:4,1,100.0" in host._ser.lines(),
          f"muted bus parks with its restore level (got {host._ser.lines()})")

    remote.running = False
    client.poll_once()
    check(host._display[0] == ("Mic", "no VM"), "Voicemeeter quitting shown")


def test_creases():
    host, layer, client, remote, exts = make(
        [{"fader": 0, "strip": 0}, {"fader": 1, "strip": 1, "max": 0}])
    positions = [round(p, 1) for p, _, _ in layer.creases[0]]
    check(positions == [16.7, 33.3, 50.0, 66.7, 83.3],
          f"every 12 dB on -60..+12 (got {positions})")
    check(layer.creases[0][-1][1:] == vm.CREASE_BIG, "0 dB crease is strong")
    check(all(c[1:] == vm.CREASE_NORMAL for c in layer.creases[1]),
          "no strong crease when 0 dB is the end stop")


def test_fader_writes_gain_and_mute():
    print("Fader -> Voicemeeter")
    host, layer, client, remote, exts = make([{"fader": 0, "strip": 2}])
    client.poll_once()
    remote.sets.clear()
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:25.0,0,0,0")
    check(remote.sets == [("Strip[2].Gain", -42.0)],
          f"25% -> -42 dB (got {remote.sets})")
    check(host._display[0] == ("In 3", "-42.0 dB"), "screen follows the drag")
    host._handle_line("POS:25.05,0,0,0")
    check(len(remote.sets) == 1, "sub-0.1 dB jitter not written")

    # Our own write flagged dirty: not taken as a remote move.
    remote._dirty = True
    host._pending = [None] * 4
    client.poll_once()
    check(host._pending[0] is None, "echo of our own write ignored")

    remote.sets.clear()
    host._handle_line("BTN:F1")
    host._handle_line("MUTE:1,1")
    check(remote.sets == [("Strip[2].Mute", 1.0)], f"mute written (got {remote.sets})")
    remote.sets.clear()
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:80.0,0,0,0")
    check(remote.sets == [], "dragging a muted fader writes nothing")
    host._handle_line("MUTE:1,0")
    check(remote.sets == [("Strip[2].Mute", 0.0)], "unmute written")


def test_voicemeeter_changes_move_the_fader():
    print("Voicemeeter -> fader")
    host, layer, client, remote, exts = make([{"fader": 0, "bus": 4, "label": "Stream"}])
    client.poll_once()
    host._pending = [None] * 4
    remote.change("Bus[4].Gain", 6.0)
    client.poll_once()
    check(abs(host._pending[0] - 91.67) < 0.01,
          f"+6 dB moves the fader (got {host._pending[0]})")
    check(host._display[0] == ("Stream", "+6.0 dB"), "config label kept, gain shown")

    host._ser.written.clear()
    remote.change("Bus[4].Mute", 1.0)
    client.poll_once()
    check("MUTE:1,1" in host._ser.lines(), f"mute in Voicemeeter parks the fader "
          f"(got {host._ser.lines()})")
    remote.sets.clear()
    host._handle_line("MUTE:1,1")
    check(remote.sets == [], "the confirmation is not written back")

    host._pending = [None] * 4
    remote.change("Bus[4].Gain", -12.0)
    client.poll_once()
    check(host._pending[0] is None, "gain change while muted leaves the parked fader")
    check(host._display[0][1] == "-12.0 dB", "but the screen shows it")

    remote.strings["Bus[4].Label"] = "Renamed"
    remote._dirty = True
    client.poll_once()
    check(host._display[0][0] == "Stream", "config label beats Voicemeeter's")


def test_no_dll_keeps_host_running():
    print("No DLL")
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Voicemeeter")
    ext = vm.VoicemeeterExtension(layer, 0, "Strip", 0, client=vm._NoClient())
    layer.register(ext)
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:50.0,0,0,0")
    host._handle_line("MUTE:1,1")
    check(host._display[0] == ("Strip[0]", "no VM"), "no VM shown")


def test_register_validates_target():
    try:
        vm.register(None, {"fader": 0, "strip": 0, "bus": 0})
        check(False, "strip and bus together rejected")
    except ValueError:
        check(True, "")


def main():
    tests = [
        test_waits_for_voicemeeter_then_syncs,
        test_creases,
        test_fader_writes_gain_and_mute,
        test_voicemeeter_changes_move_the_fader,
        test_no_dll_keeps_host_running,
        test_register_validates_target,
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
