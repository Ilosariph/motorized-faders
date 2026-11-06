"""
filter_param tests — dB / log mapping, pw-dump parsing, fader writes through
pw-cli, echo suppression, bypass on the button, paging, and the state file
that carries values across PipeWire restarts.

Run with: python3 tests/test_filter_param.py
"""

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_host import ExpiredTimer, FakeSerial, check, FAILURES, PASSES  # noqa: E402

from faders.core import DISPLAY_LINE_MAX, FaderHost  # noqa: E402
from faders.extensions import filter_param as fp  # noqa: E402

fp.SELF_ECHO_WINDOW_S = 0.0


def node(node_id, values, serial=None, name="sink-fx-apps"):
    flat = []
    for k, v in values.items():
        flat += [k, v]
    return {"id": node_id, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name,
                               "object.serial": serial or node_id},
                     "params": {"Props": [{"volume": 1.0},
                                          {"params": flat}]}}}


def core(cookie):
    return {"id": 0, "type": "PipeWire:Interface:Core",
            "info": {"cookie": cookie, "name": "pipewire-0"}}


APPS = {"fx-comp-apps:enabled": 1.0, "fx-comp-apps:al": 0.1259,
        "fx-comp-apps:cr": 2.9, "fx-comp-apps:mk": 2.3}
COMMS = {"fx-comp-comms:enabled": 1.0, "fx-comp-comms:al": 0.1259,
         "fx-comp-comms:cr": 2.9}

ENTRIES = [
    {"param": "fx-comp-apps:al", "scale": "db", "min": -40, "max": 0,
     "label": "Apps thr"},
    {"param": "fx-comp-apps:cr", "scale": "log", "min": 1, "max": 10,
     "unit": ":1", "label": "Apps ratio"},
    {"param": "fx-comp-apps:mk", "scale": "db", "min": 0, "max": 12,
     "label": "Apps gain"},
    None,
    {"param": "fx-comp-comms:al", "scale": "db", "min": -40, "max": 0,
     "label": "Comms thr"},
    {"param": "fx-comp-comms:cr", "scale": "log", "min": 1, "max": 10,
     "unit": ":1", "label": "Comms ratio"},
]


class Rig:
    """A host with one FX layer of filter_param faders; no threads, no pw-*."""

    def __init__(self, entries=ENTRIES, state=None, persist=True):
        self.host = FaderHost(port="/dev/null", num_faders=4)
        self.host._ser = FakeSerial()
        self.layer = self.host.add_layer("FX")
        self.calls = []
        self._orig_run = fp.subprocess.run
        fp.subprocess.run = self._run
        fp.PwParams.start = lambda self: None
        cfg = {"faders": entries}
        if persist:
            cfg["state_file"] = str(state)
        else:
            cfg["persist"] = False
        self.group = fp.register_all(self.layer, cfg)
        self.params = next(e for e in self.layer.extensions
                           if isinstance(e, fp.PwParams))
        self.host._on_connected()

    def _run(self, cmd, **kwargs):
        self.calls.append(cmd)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    def close(self):
        fp.subprocess.run = self._orig_run
        timer = self.params._save_timer
        if timer is not None:
            timer.cancel()

    def writes(self):
        """Every (node, {param: value}) written through pw-cli so far."""
        self.params.flush()
        out = []
        for cmd in self.calls:
            body = cmd[4].strip("{} ").split("[", 1)[1].rstrip("] ").split()
            out.append((int(cmd[2]), {body[i].strip('"'): float(body[i + 1])
                                      for i in range(0, len(body), 2)}))
        self.calls.clear()
        return out

    def confirm(self, sent):
        """The Pico's MUTE: confirmations for the MUTE: lines in `sent`."""
        for ln in sent:
            if ln.startswith("MUTE:"):
                self.host._handle_line(",".join(ln.split(",")[:2]))
        self.lines()

    def lines(self):
        out = self.host._ser.lines()
        self.host._ser.written.clear()
        return out


# ---------------------------------------------------------------------------

def test_scales():
    print("scales")
    db = fp.Scale("db", -40, 0)
    check(abs(db.to_fader(0.1259) - 55.0) < 0.1, f"-18 dB on -40..0 is 55% (got {db.to_fader(0.1259)})")
    check(abs(db.from_fader(55.0) - 0.1259) < 1e-3, "and back")
    log = fp.Scale("log", 1, 10)
    check(abs(log.to_fader(10 ** 0.5) - 50.0) < 1e-6, "log midpoint")
    check(log.to_fader(100) == 100.0 and log.to_fader(0.5) == 0.0, "clamped to the travel")
    lin = fp.Scale("linear", 0, 200)
    check(lin.from_fader(25) == 50.0, "linear")
    for bad in (("db", 0, 0), ("log", 0, 10), ("cubic", 0, 1)):
        try:
            fp.Scale(*bad)
            check(False, f"{bad} rejected")
        except ValueError:
            pass
    ext = type("E", (), {})()
    fmt = fp.FilterParamExtension.format
    ext.scale, ext.unit, ext.decimals = db, " dB", None
    check(fmt(ext, 0.1259) == "-18.0 dB", f"dB format (got {fmt(ext, 0.1259)})")
    ext.scale = fp.Scale("db", 0, 12)
    check(fmt(ext, 2.3) == "+7.2 dB", f"positive dB signed (got {fmt(ext, 2.3)})")
    ext.scale, ext.unit = log, ":1"
    check(fmt(ext, 2.9) == "2.9:1", "ratio")
    ext.scale, ext.unit = fp.Scale("log", 1, 1000), " ms"
    check(fmt(ext, 250.0) == "250 ms", "whole numbers past 10")


def test_follows_pipewire_and_writes():
    print("follow and write")
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(state=pathlib.Path(tmp) / "s.json")
        try:
            check(rig.layer.paged and rig.group.pages == 2, "six slots -> two pages")
            check(rig.host._display[0] == ("Apps thr", "--"), "unknown before pw-dump")
            rig.lines()
            rig.params.feed([core(7), node(70, APPS), node(76, COMMS)])
            sent = rig.lines()
            check("DISP:1,Apps thr,-18.0 dB" in sent, f"value shown (got {sent})")
            check(abs(rig.host._pending[0] - 55.0) < 0.1,
                  f"fader moves to -18 dB (got {rig.host._pending})")
            check("CREASES:1,55.0:55:12" in sent, f"crease at the original (got {sent})")
            check(rig.host._display[3][1].endswith("1/2"), "page shown on the last screen")
            check(rig.writes() == [], "nothing written for values read")

            rig.host._self_driven = [False] * 4
            rig.host._handle_line("POS:50.0,46.2,60.3,0")
            check(rig.writes() == [(70, {"fx-comp-apps:al": 0.1})],
                  "fader 50% writes -20 dB to node 70, still faders nothing")
            check(rig.host._display[0] == ("Apps thr", "-20.0 dB"), "display follows the hand")

            rig.host._pending = [None] * 4
            rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.1}))])
            check(rig.host._pending == [None] * 4,
                  "own write coming back does not move the motor")

            rig.lines()
            rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.01}))])
            sent = rig.lines()
            check(rig.host._pending[0] == 0.0,
                  f"-40 dB set elsewhere: fader follows (got {rig.host._pending})")
            check("DISP:1,Apps thr,-40.0 dB" in sent, f"and the display (got {sent})")

            rig.params.feed([{"id": 70, "info": None}])
            check(rig.host._display[0] == ("Apps thr", "--"), "node removed: --")
            rig.host._handle_line("POS:30.0,46.2,60.3,0")
            check(rig.writes() == [], "no node: nothing written")
        finally:
            rig.close()


def test_bypass_and_paging():
    print("bypass and pages")
    rig = Rig(persist=False)
    try:
        rig.params.feed([core(7), node(70, APPS), node(76, COMMS)])
        rig.lines()
        rig.host._handle_line("BTN:F2")
        check(rig.lines() == ["MUTE:2,1"], "button mutes the fader")
        rig.host._handle_line("MUTE:2,1")
        check(rig.writes() == [(70, {"fx-comp-apps:enabled": 0.0})],
              "confirmed mute bypasses the plugin")
        check(rig.host._display[1] == ("Apps ratio", "bypassed"), "screen says bypassed")
        rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:enabled": 0.0}))])
        sent = rig.lines()
        check("MUTE:1,1,55.0" in sent and "MUTE:3,1,60.3" in sent,
              f"other faders of the plugin park too (got {sent})")
        check(not any(ln.startswith("MUTE:2") for ln in sent), "the pressed one is not resent")
        rig.host._handle_line("MUTE:1,1")
        rig.host._handle_line("MUTE:3,1")
        check(rig.writes() == [], "their confirmations write nothing")
        rig.host._self_driven = [False] * 4
        rig.host._handle_line("POS:40.0,10.0,5.0,0")
        check(rig.writes() == [], "moves while bypassed write nothing")

        rig.host._hold_timer = ExpiredTimer
        rig.host._handle_button("G2")
        rig.host._handle_button("F4")
        rig.host._held.clear()
        check(rig.group.page == 1, "G2 + F4 held: page 2")
        check(rig.host._display[0] == ("Comms thr", "-18.0 dB"),
              f"page 2 shows comms (got {rig.host._display[0]})")
        check(rig.host._display[3][0] == "-" and rig.host._display[3][1].endswith("2/2"),
              f"empty slot on page 2 (got {rig.host._display[3]})")
        sent = rig.lines()
        check("MUTE:1,0,55.0" in sent, f"apps fader unparks for comms (got {sent})")
        rig.confirm(sent)
        rig.host._self_driven = [False] * 4
        rig.host._handle_line("POS:55.0,50.0,0,0")
        w = rig.writes()
        check(len(w) == 1 and w[0][0] == 76 and abs(w[0][1]["fx-comp-comms:cr"] - 10 ** 0.5) < 1e-4,
              f"page 2 fader writes the comms ratio (got {w})")

        rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.01,
                                                "fx-comp-apps:enabled": 0.0}))])
        check(not any(ln.startswith(("SET:", "MUTE:")) for ln in rig.lines()),
              "hidden page: apps change sends nothing")
        rig.host._handle_button("G2")
        rig.host._handle_button("F1")
        rig.host._held.clear()
        sent = rig.lines()
        check("MUTE:1,1,0.0" in sent and "DISP:1,Apps thr,bypassed" in sent,
              f"back on page 1: bypassed faders park, hidden change kept (got {sent})")
        rig.confirm(sent)

        rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.01}))])
        sent = rig.lines()
        check("MUTE:1,0,0.0" in sent and "MUTE:2,0,46.2" in sent,
              f"enabled elsewhere: faders return (got {sent})")
        check(rig.host._display[1] == ("Apps ratio", "2.9:1"), "screen shows the value again")

        rig.host._handle_line("BTN:F2")
        rig.host._handle_line("MUTE:2,1")
        rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.01,
                                                "fx-comp-apps:enabled": 0.0}))])
        rig.confirm(rig.lines())
        rig.host._self_driven = [False] * 4
        rig.host._handle_line("POS:0,20.0,0,0")
        rig.host._pending = [None] * 4
        rig.host._handle_line("BTN:F2")
        rig.host._handle_line("MUTE:2,0")
        check(abs(rig.host._pending[1] - 46.2) < 0.1,
              f"button unbypass: fader back on the value, not where the hand parked it (got {rig.host._pending})")
        check(rig.writes()[-1] == (70, {"fx-comp-apps:enabled": 1.0}),
              "and the plugin is back on")
    finally:
        rig.close()


def test_state_survives_restart():
    print("state file")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "fx" / "s.json"
        rig = Rig(state=path)
        try:
            rig.params.feed([core(1), node(70, APPS)])
            rig.params.feed([node(70, dict(APPS, **{"fx-comp-apps:al": 0.2}))])
            rig.params.save()
            saved = json.loads(path.read_text())["params"]["fx-comp-apps:al"]
            check(saved["original"] == 0.1259 and saved["value"] == 0.2,
                  f"original kept as backup, value saved (got {saved})")
            check("fx-comp-apps:enabled" in json.loads(path.read_text())["params"],
                  "bypass state saved too")
        finally:
            rig.close()

        rig = Rig(state=path)
        try:
            rig.params.feed([core(1), node(70, dict(APPS, **{"fx-comp-apps:al": 0.2}))])
            check(rig.writes() == [], "host restart, same PipeWire: nothing re-applied")
        finally:
            rig.close()

        rig = Rig(state=path)
        try:
            rig.params.feed([core(2), node(30, APPS)])
            check(rig.writes() == [(30, {"fx-comp-apps:al": 0.2})],
                  "PipeWire restarted: saved value re-applied")
            rig.params.feed([node(30, dict(APPS, **{"fx-comp-apps:al": 0.2}))])
            rig.params.save()
            check(json.loads(path.read_text())["params"]["fx-comp-apps:al"]["original"] == 0.1259,
                  "original stays the config's value")
        finally:
            rig.close()

        rig = Rig(state=path)
        try:
            rig.params.feed([core(3), node(31, dict(APPS, **{"fx-comp-apps:al": 0.0631}))])
            check(rig.writes() == [], "config changed: its new value wins")
            rig.params.save()
            entry = json.loads(path.read_text())["params"]["fx-comp-apps:al"]
            check(entry["original"] == 0.0631 and entry["value"] == 0.0631,
                  f"new original recorded (got {entry})")
        finally:
            rig.close()

        live = {"fx-comp-apps:al": (31, 0.3), "fx-comp-apps:cr": (31, 4.0)}
        state = json.loads(path.read_text())
        state["params"]["fx-comp-apps:al"]["value"] = 0.3
        state["params"]["fx-comp-apps:cr"]["value"] = 4.0
        path.write_text(json.dumps(state))
        ran = []
        orig = (fp._live_values, fp.subprocess.run)
        fp._live_values = lambda: live
        fp.subprocess.run = lambda cmd, **k: (ran.append(cmd), type("R", (), {"returncode": 0})())[1]
        try:
            fp.main(["--state", str(path), "reset", "fx-comp-apps:al"])
            check(ran == [fp.set_props_cmd(31, {"fx-comp-apps:al": 0.0631})],
                  f"reset sets one control back (got {ran})")
            after = json.loads(path.read_text())["params"]
            check(after["fx-comp-apps:al"]["value"] == 0.0631
                  and after["fx-comp-apps:cr"]["value"] == 4.0,
                  "reset only touches the named control")
            ran.clear()
            fp.main(["--state", str(path), "reset"])
            check(len(ran) == 1 and ran[0][2] == "31"
                  and '"fx-comp-apps:cr" 2.9' in ran[0][4],
                  f"reset with no names: every control, one call per node (got {ran})")
        finally:
            fp._live_values, fp.subprocess.run = orig


def test_stream_framing():
    print("pw-dump framing")
    params = fp.PwParams()
    got = []
    params.feed = lambda objs: got.append(objs)
    text = json.dumps([core(1)], indent=2) + "\n" + json.dumps([node(5, APPS)], indent=2) + "\n"
    params.read_stream(text.splitlines(keepends=True))
    check(len(got) == 2 and got[1][0]["id"] == 5, f"one feed per array (got {len(got)})")


def test_bad_entry_left_empty():
    print("bad entries")
    rig = Rig(entries=[{"param": "nocolon", "min": 0, "max": 1},
                       {"param": "x:y", "scale": "log", "min": 0, "max": 1},
                       {"param": "x:z"}], persist=False)
    try:
        exts = [e for e in rig.layer.extensions if isinstance(e, fp.FilterParamExtension)]
        check(exts == [], "invalid entries skipped, host keeps running")
    finally:
        rig.close()


def main():
    for test in (test_scales, test_follows_pipewire_and_writes,
                 test_bypass_and_paging, test_state_survives_restart,
                 test_stream_framing, test_bad_entry_left_empty):
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
