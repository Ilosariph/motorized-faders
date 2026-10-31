"""
home_assistant tests — lights and covers both ways, mute, echo suppression,
paging, entity diffs and the WebSocket / HA API framing over a socket pair.

Run with: python3 tests/test_home_assistant.py
"""

import base64
import hashlib
import json
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_host import FakeSerial, _wait_for, check, FAILURES, PASSES  # noqa: E402

from faders.core import FaderHost  # noqa: E402
from faders.extensions import home_assistant as ha  # noqa: E402

# Short write intervals and echo windows so the tests do not sleep long.
ha.Binding.WRITE = {
    "brightness": (0.05, False, 0.15),
    "position": (0.05, True, 0.15),
    "tilt": (0.05, True, 0.15),
}


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, payload, wait=False, timeout=None):
        self.sent.append(payload)

    def calls(self):
        return [(p["target"]["entity_id"], p["service"], p["service_data"])
                for p in self.sent if p.get("type") == "call_service"]


def light(on=True, brightness=255, modes=("brightness",), name=None):
    attrs = {"supported_color_modes": list(modes)}
    if on and brightness is not None:
        attrs["brightness"] = brightness
    if name:
        attrs["friendly_name"] = name
    return {"s": "on" if on else "off", "a": attrs}


def cover(position=None, tilt=None, state="open"):
    attrs = {}
    if position is not None:
        attrs["current_position"] = position
    if tilt is not None:
        attrs["current_tilt_position"] = tilt
    return {"s": state, "a": attrs}


def change(eid, state=None, **attrs):
    plus = {"a": attrs}
    if state is not None:
        plus["s"] = state
    return {"c": {eid: {"+": plus}}}


def make(entries, initial=None, num_layers=1):
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    for i in range(num_layers - 1):
        host.add_layer(f"other{i}")
    layer = host.add_layer("Home")
    slots = [None if e is None else ha.Binding(*e) if isinstance(e, tuple)
             else ha.Binding(e) for e in entries]
    ext = ha.HomeAssistantExtension(layer, "http://ha", "tok", slots, start=False)
    layer.register(ext)
    ext._client = FakeClient()
    ext._set_status("ok")
    if initial is not None:
        ext.handle_event({"a": initial})
    return host, layer, ext


def touch(host, idx, value):
    """A hand moving fader `idx` to `value`."""
    host._handle_line(f"STATE:{idx + 1},IDLE")
    pos = ["0"] * 4
    pos[idx] = f"{value:.1f}"
    host._handle_line("POS:" + ",".join(pos))


def press(host, idx, muted):
    host._handle_line(f"BTN:F{idx + 1}")
    host._handle_line(f"BTNUP:F{idx + 1}")
    host._handle_line(f"MUTE:{idx + 1},{1 if muted else 0}")


def settle(ext):
    """Let write timers and echo windows run out."""
    time.sleep(0.25)


# ---------------------------------------------------------------------------

def test_light_initial_state_and_screens():
    print("Light")
    host, layer, ext = make(["light.desk", "light.lamp", ("light.hall", None, "Hall")],
                            {"light.desk": light(brightness=204, name="Desk lamp"),
                             "light.lamp": light(on=False),
                             "light.hall": light(modes=("onoff",))})
    check(host._display[0] == ("Desk lamp", "on 80%"), f"friendly name, brightness (got {host._display[0]})")
    check(host._pending[0] == 80.0, f"204/255 -> 80% (got {host._pending[0]})")
    check("MUTE:2,1" in host._ser.lines(), f"off light parks its fader (got {host._ser.lines()})")
    check(host._display[1][1] == "off", "off shown")
    check(host._display[2] == ("Hall", "on"), f"label wins, on/off light (got {host._display[2]})")
    check(layer.creases[0] == [(25.0, 55.0, 10), (50.0, 55.0, 10), (75.0, 55.0, 10)],
          f"brightness creases (got {layer.creases[0]})")
    check(layer.creases[2] == [], "no creases on an on/off light")
    check(host._display[3] == ("-", ""), "empty fader blank")
    check(not ext.PAGED and not layer.paged, "one page: not paged")


def test_light_fader_writes():
    host, layer, ext = make(["light.desk"], {"light.desk": light(brightness=255)})
    touch(host, 0, 50.0)
    check(ext._client.calls() == [("light.desk", "turn_on", {"brightness": 128})],
          f"drag -> brightness (got {ext._client.calls()})")
    check(host._display[0][1] == "on 50%", "screen follows the drag")
    touch(host, 0, 52.0)
    touch(host, 0, 60.0)
    _wait_for(lambda: len(ext._client.calls()) == 2)
    check(ext._client.calls()[-1] == ("light.desk", "turn_on", {"brightness": 153}),
          f"trailing value sent, intermediate coalesced (got {ext._client.calls()})")

    # Echo of an earlier value mid-drag: ignored, screen keeps the drag.
    ext.handle_event(change("light.desk", brightness=128))
    check(host._display[0][1] == "on 60%", "echo does not repaint the screen")
    host._pending = [None] * 4
    settle(ext)
    _wait_for(lambda: host._pending[0] is not None, 0.5)
    check(host._pending[0] == 50.2, f"after the echo window HA's last word wins (got {host._pending[0]})")

    # Dragged to the bottom: off, but no mute.
    host._pending = [None] * 4
    touch(host, 0, 0.0)
    _wait_for(lambda: ext._client.calls()[-1][1] == "turn_off")
    check(ext._client.calls()[-1] == ("light.desk", "turn_off", {}), "bottom turns off")
    settle(ext)
    ext.handle_event(change("light.desk", "off"))
    check(not any("MUTE:1,1" in l for l in host._ser.lines()), "fader at 0 not turned into a mute")
    check(host._display[0][1] == "off", "off shown")


def test_light_mute_button():
    host, layer, ext = make(["light.desk"], {"light.desk": light(brightness=178)})
    press(host, 0, True)
    check(ext._client.calls() == [("light.desk", "turn_off", {})], f"mute -> off (got {ext._client.calls()})")
    ext.handle_event(change("light.desk", "off"))
    check(len(ext._client.calls()) == 1, "HA's off is not written back")

    # Picking a new level while parked, then unmuting.
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:40.0,0,0,0")
    press(host, 0, False)
    check(ext._client.calls()[-1] == ("light.desk", "turn_on", {"brightness": 102}),
          f"unmute -> on at the picked level (got {ext._client.calls()})")
    host._pending = [None] * 4
    ext.handle_event(change("light.desk", "on", brightness=102))
    check(host._pending[0] is None and host._display[0][1] == "on 40%",
          "HA's on is not driven back")


def test_light_changed_in_ha():
    host, layer, ext = make(["light.desk"], {"light.desk": light(brightness=128)})
    host._ser.written.clear()
    ext.handle_event(change("light.desk", "off"))
    check("MUTE:1,1,50.2" in host._ser.lines(), f"off in HA parks with restore (got {host._ser.lines()})")
    host._handle_line("MUTE:1,1")
    check(ext._client.calls() == [], "confirmation writes nothing")
    host._ser.written.clear()
    ext.handle_event(change("light.desk", "on", brightness=255))
    check("MUTE:1,0,100.0" in host._ser.lines(), f"on in HA returns the fader (got {host._ser.lines()})")
    host._handle_line("MUTE:1,0")
    check(ext._client.calls() == [], "unmute confirmation writes nothing")
    host._pending = [None] * 4
    ext.handle_event(change("light.desk", brightness=26))
    check(host._pending[0] == 10.2, f"brightness in HA moves the fader (got {host._pending[0]})")
    host._pending = [None] * 4
    ext.handle_event(change("light.desk", brightness=28))
    check(host._pending[0] is None, "rounding-sized change ignored")
    ext.handle_event(change("light.desk", "unavailable"))
    check(host._display[0][1] == "unavailable", "unavailable shown")
    touch(host, 0, 70.0)
    check(len(ext._client.calls()) == 0, "unavailable entity takes no writes")
    host._ser.written.clear()
    press(host, 0, True)
    check("MUTE:1,0" in host._ser.lines(), "mute on unavailable entity is undone")


def test_cover_position_and_tilt():
    print("Cover")
    host, layer, ext = make([("cover.blinds", "position", "Blinds"),
                             ("cover.blinds", "tilt", "Slats")],
                            {"cover.blinds": cover(60, 50)})
    check(host._pending[:2] == [60.0, 50.0], f"both faders from one entity (got {host._pending})")
    check(host._display[0] == ("Blinds", "open 60%") and host._display[1] == ("Slats", "tilt 50%"),
          f"screens (got {host._display[:2]})")
    check(layer.creases[1] == [(50.0, 70.0, 18)], "tilt crease at 50%")

    touch(host, 0, 30.0)
    touch(host, 0, 20.0)
    check(ext._client.calls() == [], "cover not commanded mid-drag")
    _wait_for(lambda: ext._client.calls())
    check(ext._client.calls() == [("cover.blinds", "set_cover_position", {"position": 20})],
          f"one command once the fader rests (got {ext._client.calls()})")
    touch(host, 1, 75.0)
    _wait_for(lambda: len(ext._client.calls()) == 2)
    check(ext._client.calls()[-1] == ("cover.blinds", "set_cover_tilt_position", {"tilt_position": 75}),
          "tilt fader -> tilt position")

    # Moved in HA: the fader waits for the travel to end.
    settle(ext)
    host._pending = [None] * 4
    ext.handle_event(change("cover.blinds", "opening", current_position=40))
    check(host._pending[0] is None and host._display[0][1] == "open 40%",
          "travel shown, fader stays")
    ext.handle_event(change("cover.blinds", "open", current_position=90))
    check(host._pending[0] == 90.0, f"fader follows when stopped (got {host._pending[0]})")

    # Closed in HA from elsewhere: mute; button reopens to the restore level.
    host._ser.written.clear()
    ext.handle_event(change("cover.blinds", "closed", current_position=0))
    check("MUTE:1,1,90.0" in host._ser.lines(), f"closed -> parked (got {host._ser.lines()})")
    host._handle_line("MUTE:1,1")
    press(host, 0, False)
    check(ext._client.calls()[-1] == ("cover.blinds", "set_cover_position", {"position": 90}),
          "unmute reopens")


def test_cover_mute_while_travelling():
    host, layer, ext = make([("cover.blinds", "position")], {"cover.blinds": cover(80)})
    press(host, 0, True)
    check(ext._client.calls() == [("cover.blinds", "set_cover_position", {"position": 0})],
          f"mute closes (got {ext._client.calls()})")
    ext.handle_event(change("cover.blinds", "closing", current_position=50))
    press(host, 0, False)
    check(ext._client.calls()[-1] == ("cover.blinds", "set_cover_position", {"position": 80}),
          f"unmute mid-travel still reopens (got {ext._client.calls()})")


def test_open_close_only_cover():
    host, layer, ext = make([("cover.garage", "position")], {"cover.garage": cover(state="closed")})
    check(host._display[0][1] == "closed", "closed shown")
    press(host, 0, False)  # nothing to unmute from a fresh Pico, but harmless
    host._handle_line("MUTE:1,1")
    host._handle_line("MUTE:1,0")
    ext._client.sent.clear()
    touch(host, 0, 70.0)
    _wait_for(lambda: ext._client.calls())
    check(ext._client.calls() == [("cover.garage", "open_cover", {})],
          f"upper half opens (got {ext._client.calls()})")


def test_no_follow():
    host, layer, ext = make([("cover.b", "position", "Blinds", True, False)],
                            {"cover.b": cover(1, 0)})
    check(host._display[0] == ("Blinds", "--") and host._pending[0] is None,
          f"reported value ignored at start (got {host._display[0]}, {host._pending[0]})")
    touch(host, 0, 60.0)
    _wait_for(lambda: ext._client.calls())
    check(ext._client.calls() == [("cover.b", "set_cover_position", {"position": 60})], "fader still writes")
    settle(ext)
    host._pending = [None] * 4
    ext.handle_event(change("cover.b", current_position=1))
    settle(ext)
    check(host._pending[0] is None and host._display[0][1] == "open 60%",
          f"bogus report does not move the fader (got {host._pending[0]}, {host._display[0]})")
    press(host, 0, True)
    press(host, 0, False)
    check(ext._client.calls()[-2:] == [("cover.b", "set_cover_position", {"position": 0}),
                                       ("cover.b", "set_cover_position", {"position": 60})],
          f"mute / unmute still work (got {ext._client.calls()})")
    host._ser.written.clear()
    ext.handle_event(change("cover.b", "unavailable"))
    check(host._display[0][1] == "unavailable", "availability still followed")


def test_switch():
    host, layer, ext = make([("switch.printer", None, "3D")], {"switch.printer": {"s": "on", "a": {}}})
    check(host._display[0] == ("3D", "on") and host._pending[0] == 100.0,
          f"switch on -> fader top (got {host._display[0]}, {host._pending[0]})")
    check(layer.creases[0] == [], "no creases on a switch")
    press(host, 0, True)
    check(ext._client.calls() == [("switch.printer", "turn_off", {})], f"mute -> switch.turn_off (got {ext._client.calls()})")
    press(host, 0, False)
    check(ext._client.calls()[-1] == ("switch.printer", "turn_on", {}), "unmute -> switch.turn_on")
    settle(ext)
    touch(host, 0, 0.0)
    _wait_for(lambda: len(ext._client.calls()) == 3)
    check(ext._client.calls()[-1] == ("switch.printer", "turn_off", {}), "bottom -> off")


def test_paging():
    print("Paging")
    entries = [f"light.l{i}" for i in range(5)] + [None, ("cover.b", "position")]
    host, layer, ext = make(entries, {f"light.l{i}": light(brightness=i * 50) for i in range(5)})
    check(ext.PAGED and layer.paged, "more entries than faders: paged")
    check(host._display[3][1].endswith("1/2"), f"page on the last screen (got {host._display[3]})")
    host._pending = [None] * 4
    host._handle_line("BTN:G2")
    host._handle_line("BTN:F4")
    check(ext._page == 1, "G2 + F4 -> next page")
    check(host._display[0][0] == "light.l4" and host._display[1] == ("-", ""),
          f"page 2: l4, null gap (got {host._display[:3]})")
    check(host._display[2] == ("cover.b", "unavailable"), "no state yet: unavailable")
    check(host._pending[0] == 78.4 and host._pending[1] == 0.0,
          f"faders driven to the page (got {host._pending})")
    check(host._display[3] == ("-", "2/2"), f"page 2/2 (got {host._display[3]})")

    # Off-page change: no fader moves, but the state is kept for later.
    host._pending = [None] * 4
    ext.handle_event(change("light.l1", brightness=255))
    check(host._pending == [None] * 4, "off-page entity moves nothing")
    host._handle_line("BTNUP:F4")
    host._handle_line("BTN:F1")
    check(ext._page == 0 and host._pending[1] == 100.0,
          f"back on page 1 the fader shows the new level (got {host._pending})")
    touch(host, 1, 30.0)
    check(ext._client.calls()[-1] == ("light.l1", "turn_on", {"brightness": 76}),
          "fader 2 on page 1 writes l1")


def test_inactive_layer_and_status():
    print("Layered / status")
    host, layer, ext = make(["light.desk"], num_layers=2)
    ext._set_status("offline")
    check(host._ser.lines() == [], "nothing reaches the Pico while another layer is active")
    check(layer.display[0] == ("light.desk", "offline"), f"status on the screen (got {layer.display[0]})")
    ext._set_status("ok")
    ext.handle_event({"a": {"light.desk": light(on=False)}})
    host.step_layer(1)
    sent = host._ser.lines()
    check(any(l.startswith("MUTE:1,1") for l in sent) and "DISP:1,light.desk,off" in sent,
          f"applied on switch (got {sent})")


def test_entity_diff():
    print("Diffs")
    states = {}
    changed = ha.apply_entity_diff(states, {"a": {"light.a": {"s": "on", "a": {"brightness": 5, "x": 1}}}})
    check(changed == ["light.a"] and states["light.a"]["attributes"]["brightness"] == 5, "add")
    ha.apply_entity_diff(states, {"c": {"light.a": {"+": {"s": "off", "a": {"y": 2}},
                                                    "-": {"a": ["brightness"]}}}})
    check(states["light.a"] == {"state": "off", "attributes": {"x": 1, "y": 2}},
          f"change: + and - applied (got {states['light.a']})")
    ha.apply_entity_diff(states, {"r": ["light.a"]})
    check(states["light.a"]["state"] == "unavailable", "removed -> unavailable")
    check(ha.ws_url("https://h.local:8443/") == (True, "h.local", 8443, "/api/websocket"), "wss url")
    check(ha.ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          "accept key matches the RFC 6455 example")
    check(ha.ws_url("http://h") == (False, "h", 80, "/api/websocket"), "ws url, default port")


# ----- WebSocket over a socket pair ----------------------------------------

def server_read(sock):
    def exact(n):
        buf = b""
        while len(buf) < n:
            buf += sock.recv(n - len(buf))
        return buf
    b0, b1 = exact(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", exact(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", exact(8))[0]
    assert b1 & 0x80, "client frames must be masked"
    mask = exact(4)
    return b0 & 0x0F, bytes(c ^ mask[i & 3] for i, c in enumerate(exact(n)))


def server_frame(opcode, payload, fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


def test_websocket_and_api():
    print("WebSocket")
    a, b = socket.socketpair()
    seen = {}

    def server():
        req = b""
        while b"\r\n\r\n" not in req:
            req += b.recv(1)
        key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n")
               if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + ha.WS_GUID).encode()).digest()).decode()
        seen["path"] = req.split(b" ")[1]
        b.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        b.sendall(server_frame(ha.WS_TEXT, b'{"type": "auth_required"}'))
        seen["auth"] = json.loads(server_read(b)[1])
        # Fragmented auth_ok.
        b.sendall(server_frame(ha.WS_TEXT, b'{"type": "auth', fin=False))
        b.sendall(server_frame(ha.WS_CONT, b'_ok", "ha_version": "2026.10"}'))
        op, body = server_read(b)
        req = json.loads(body)
        seen["sub"] = req
        b.sendall(server_frame(ha.WS_PING, b"hb"))
        seen["pong"] = server_read(b)
        big = {"id": req["id"], "type": "event",
               "event": {"a": {"light.x": {"s": "on", "a": {"pad": "x" * 70000}}}}}
        reply = {"id": req["id"], "type": "result", "success": True, "result": None}
        b.sendall(server_frame(ha.WS_TEXT, json.dumps([reply, big]).encode()))
        op, body = server_read(b)
        req = json.loads(body)
        b.sendall(server_frame(ha.WS_TEXT, json.dumps(
            {"id": req["id"], "type": "result", "success": False,
             "error": {"code": "not_found", "message": "Service not found"}}).encode()))
        b.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    ws = ha.WebSocket(a)
    ws.handshake("ha:8123", "/api/websocket")
    client = ha.HaClient(ws)
    version = client.authenticate("tok")
    check(seen["path"] == b"/api/websocket", "upgrade path")
    check(seen["auth"] == {"type": "auth", "access_token": "tok"}, "auth message")
    check(version == "2026.10", "fragmented auth_ok reassembled")
    client.send({"type": "subscribe_entities", "entity_ids": ["light.x"]}, wait=True, timeout=2)
    check(seen["sub"]["type"] == "subscribe_entities" and seen["sub"]["id"] == 1, "ids numbered")
    check(seen["pong"] == (ha.WS_PONG, b"hb"), "ping answered")
    event = client.events.get(timeout=2)
    check(len(event["a"]["light.x"]["a"]["pad"]) == 70000, "64-bit length frame, coalesced list")
    try:
        client.send({"type": "call_service"}, wait=True, timeout=2)
        check(False, "failed result raises")
    except ha.HaError as e:
        check("Service not found" in str(e), f"error surfaced (got {e})")
    check(client.closed.wait(2), "closed once the server hangs up")
    check(client.events.get(timeout=2) is None, "close wakes the event loop")
    th.join(2)


def test_config():
    print("Config")
    d = tempfile.mkdtemp()
    token = pathlib.Path(d, "token")
    token.write_text("abc\n")
    host = FaderHost(port="/dev/null", num_faders=4)
    layer = host.add_layer("Home")
    captured = {}

    class Probe(ha.HomeAssistantExtension):
        def __init__(self, host, **kw):
            captured.update(kw)

    real = ha.HomeAssistantExtension
    ha.HomeAssistantExtension = Probe
    try:
        ha.register(layer, {"url": "http://ha", "token_file": str(token),
                            "faders": [{"entity": "light.a"}, None,
                                       {"entity": "cover.b", "attribute": "tilt"}]})
    finally:
        ha.HomeAssistantExtension = real
    slots = captured["slots"]
    check(captured["token"] == "abc", "token read from file")
    check(slots[1] is None and slots[0].kind == "brightness" and slots[2].kind == "tilt",
          f"slots parsed (got {slots})")
    try:
        ha.Binding("sensor.x")
        check(False, "unsupported domain rejected")
    except ValueError:
        check(True, "")
    captured.clear()
    ha.HomeAssistantExtension = Probe
    try:
        ha.register(layer, {"url": "http://ha", "token": "t",
                            "faders": [{"entity": "sensor.x"}, {"entity": "light.a"}]})
    finally:
        ha.HomeAssistantExtension = real
    check(captured["slots"][0] is None and captured["slots"][1].entity == "light.a",
          "bad entry leaves its fader empty instead of crashing")


def main():
    tests = [
        test_light_initial_state_and_screens,
        test_light_fader_writes,
        test_light_mute_button,
        test_light_changed_in_ha,
        test_cover_position_and_tilt,
        test_cover_mute_while_travelling,
        test_open_close_only_cover,
        test_no_follow,
        test_switch,
        test_paging,
        test_inactive_layer_and_status,
        test_entity_diff,
        test_websocket_and_api,
        test_config,
    ]
    for test in tests:
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED, {PASSES[0]} passed")
        return 1
    print(f"all {PASSES[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
