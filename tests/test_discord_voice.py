"""
discord_voice tests — user list to fader mapping, volume and mute both ways,
paging, the RPC framing over a socket pair and the OAuth token cache.

Run with: python3 tests/test_discord_voice.py
"""

import json
import os
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
from faders.extensions import discord_voice as dv  # noqa: E402

SELF_ID = "1"


class FakeRpc:
    def __init__(self, replies=None):
        self.sent = []
        self.replies = replies or {}

    def send(self, cmd, args=None, evt=None, wait=False, timeout=None):
        self.sent.append((cmd, args, evt))
        reply = self.replies.get(cmd)
        if isinstance(reply, Exception):
            raise reply
        return reply(args) if callable(reply) else reply

    def settings(self):
        return [args for cmd, args, _ in self.sent if cmd == "SET_USER_VOICE_SETTINGS"]


def rpc(ui):
    """What a slider percentage goes over the socket as."""
    return round(dv.ui_to_rpc(ui), 2)


def state(uid, name, volume=100, mute=False, nick=None):
    return {"user": {"id": uid, "username": name}, "nick": nick,
            "volume": dv.ui_to_rpc(volume), "mute": mute, "voice_state": {}}


def make(users=()):
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Discord")
    ext = dv.DiscordVoiceExtension(layer, "cid", "secret", start=False,
                                   token_file=os.path.join(tempfile.mkdtemp(), "t.json"))
    layer.register(ext)
    ext._self_id = SELF_ID
    ext._rpc = FakeRpc()
    if users is not None:
        ext._load_channel({"id": "c1", "name": "General",
                           "voice_states": [state(SELF_ID, "me")] + list(users)})
    return host, layer, ext


# ---------------------------------------------------------------------------

def test_offline_and_empty_screens():
    print("Screens")
    host, layer, ext = make(users=None)
    check(host._display[0] == ("Discord", "offline"), f"offline shown (got {host._display[0]})")
    check(host._display[3][1].endswith("1/1"), f"page always shown (got {host._display[3]})")
    ext._load_channel(None)
    check(host._display[0] == ("Discord", "not in VC"), "not in VC shown")
    ext._load_channel({"id": "c", "voice_states": [state(SELF_ID, "me")]})
    check(host._display[0] == ("Discord", "nobody here"), "alone in VC shown")


def test_users_bound_in_join_order_without_self():
    host, layer, ext = make([state("2", "alice", 150), state("3", "bob", nick="Bobby"),
                             state("4", "carol", 50, mute=True)])
    shown = list(host._display)
    check(shown[0] == ("alice", "vol 150%"), f"alice on fader 1 (got {shown[0]})")
    check(shown[1][0] == "Bobby", "server nickname preferred")
    check(shown[3][0] == "-" and shown[3][1].endswith("1/1"),
          f"empty fader 4 shows the page (got {shown[3]})")
    check(host._pending[0] == 75.0, f"150% volume -> 75% fader (got {host._pending[0]})")
    check(host._pending[1] == 50.0, "100% -> 50%")
    sent = host._ser.lines()
    check("MUTE:3,1,25.0" in sent, f"muted user parks with restore level (got {sent})")
    check(host._pending[3] == 0.0, "empty fader parks at 0")
    check(layer.creases[0] == [(25.0, 55.0, 10), (50.0, 70.0, 18), (75.0, 55.0, 10)],
          f"creases at 50/100/150% (got {layer.creases[0]})")
    check(layer.creases[3] in ([], None), "no creases on an empty fader")


def test_fader_writes_volume_and_mute_only():
    print("Volume / mute")
    host, layer, ext = make([state("2", "alice"), state("3", "bob")])
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:80.0,50,0,0")
    check({"user_id": "2", "volume": rpc(160)} in ext._rpc.settings(),
          f"80% fader -> 160% volume (got {ext._rpc.settings()})")
    check(host._display[0] == ("alice", "vol 160%"), "screen follows the drag")

    # Within the write interval: coalesced, then the last value still lands.
    host._handle_line("POS:81.0,50,0,0")
    host._handle_line("POS:90.0,50,0,0")
    _wait_for(lambda: {"user_id": "2", "volume": rpc(180)} in ext._rpc.settings())
    check({"user_id": "2", "volume": rpc(180)} in ext._rpc.settings(), "trailing value sent")
    check({"user_id": "2", "volume": rpc(162)} not in ext._rpc.settings(),
          "intermediate value coalesced away")

    ext._rpc.sent.clear()
    host._handle_line("BTN:F2")
    host._handle_line("MUTE:2,1")
    check(ext._rpc.settings() == [{"user_id": "3", "mute": True}],
          f"fader mute sends Discord's mute flag only, no volume (got {ext._rpc.settings()})")

    ext._rpc.sent.clear()
    host._handle_line("STATE:2,IDLE")
    host._handle_line("POS:80.0,40.0,0,0")
    check(ext._rpc.settings() == [], "dragging a muted fader writes nothing")

    host._handle_line("BTNUP:F2")
    host._handle_line("BTN:F2")
    host._handle_line("MUTE:2,0")
    check(ext._rpc.settings() == [{"user_id": "3", "mute": False}], "unmute flag only")

    # Empty fader: nothing to write.
    ext._rpc.sent.clear()
    host._handle_line("STATE:4,IDLE")
    host._handle_line("POS:80.0,40.0,0,70.0")
    check(not any(s.get("user_id") is None for s in ext._rpc.settings()),
          "empty fader ignored")


def test_discord_side_changes_move_the_fader():
    host, layer, ext = make([state("2", "alice")])
    host._pending = [None] * 4
    ext.handle_event("VOICE_STATE_UPDATE", state("2", "alice", 40))
    check(host._pending[0] == 20.0, f"volume set in Discord moves the fader (got {host._pending[0]})")
    check(host._display[0] == ("alice", "vol 40%"), "screen updated")

    host._ser.written.clear()
    ext.handle_event("VOICE_STATE_UPDATE", state("2", "alice", 40, mute=True))
    check("MUTE:1,1,20.0" in host._ser.lines(), "mute in Discord parks the fader")
    host._handle_line("MUTE:1,1")
    check(ext._rpc.settings() == [], "the confirmation is not written back")

    # Our own write echoed back is not a remote change.
    host._handle_line("MUTE:1,0")
    ext.handle_event("VOICE_STATE_UPDATE", state("2", "alice", 40, mute=False))
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:60.0,0,0,0")
    host._pending = [None] * 4
    ext.handle_event("VOICE_STATE_UPDATE", state("2", "alice", 118))
    check(host._pending[0] is None, "echo of our own write ignored")


def test_paging():
    print("Paging")
    users = [state(str(i), f"user{i}", 100 + i) for i in range(2, 8)]  # 6 users
    host, layer, ext = make(users)
    check(host._display[3] == ("user5", "vol 105%          1/2"),
          f"page 1/2 on the last screen (got {host._display[3]})")
    host._pending = [None] * 4
    host._handle_line("BTN:G2")
    host._handle_line("BTN:F4")
    check(ext._page == 1, "G2 + F4 -> next page")
    check(host._display[0][0] == "user6" and host._display[1][0] == "user7",
          f"second page users shown (got {host._display[:2]})")
    check(host._display[2][0] == "-" and host._display[3][1].endswith("2/2"),
          f"empty slots and 2/2 (got {host._display[2:]})")
    check(host._pending[0] == 53.0 and host._pending[2] == 0.0,
          f"faders driven to the new users (got {host._pending})")
    check(not any(host.is_muted(i) for i in range(4)), "paging mutes nothing")

    host._handle_line("BTNUP:F4")
    host._handle_line("BTN:F4")
    check(ext._page == 0, "wraps back to page 1")
    host._handle_line("BTNUP:F4")
    host._handle_line("BTN:F1")
    check(ext._page == 1, "G2 + F1 -> previous page, wrapping")

    # A user on the last page leaves: the page goes away, we land on page 1.
    ext.handle_event("VOICE_STATE_DELETE", state("6", "user6"))
    ext.handle_event("VOICE_STATE_DELETE", state("7", "user7"))
    check(ext._page == 0 and host._display[3][1].endswith("1/1"),
          f"page clamped when users leave (got {host._display[3]})")

    # A join on another page does not move faders on this one.
    host._pending = [None] * 4
    ext.handle_event("VOICE_STATE_CREATE", state("9", "late"))
    check(host._pending == [None] * 4, "join onto page 2 moves nothing here")
    check(host._display[3][1].endswith("1/2"), "page count updated")


def test_inactive_layer_stores_only():
    print("Layered")
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    host.add_layer("Audio")
    layer = host.add_layer("Discord")
    ext = dv.DiscordVoiceExtension(layer, "cid", "s", start=False,
                                   token_file=os.path.join(tempfile.mkdtemp(), "t.json"))
    layer.register(ext)
    ext._self_id = SELF_ID
    ext._rpc = FakeRpc()
    ext._load_channel({"id": "c", "voice_states": [state("2", "alice", 150, mute=True)]})
    check(host._ser.lines() == [] and host._pending == [None] * 4,
          "nothing reaches the Pico while Audio is active")
    host.step_layer(1)
    _wait_for(lambda: host._target is None)
    sent = host._ser.lines()
    check("MUTE:1,1,75.0" in sent and "DISP:1,alice,vol 150%" in sent,
          f"applied on switch (got {sent})")


def test_rpc_framing_over_socket():
    print("RPC")
    a, b = socket.socketpair()

    def read(sock):
        op, n = struct.unpack("<II", sock.recv(8))
        body = b""
        while len(body) < n:
            body += sock.recv(n - len(body))
        return op, json.loads(body)

    def write(sock, op, payload):
        body = json.dumps(payload).encode()
        sock.sendall(struct.pack("<II", op, len(body)) + body)

    seen = {}

    def server():
        op, hello = read(b)
        seen["hello"] = (op, hello)
        write(b, dv.OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}})
        op, req = read(b)
        seen["req"] = req
        write(b, dv.OP_PING, {"x": 1})
        seen["pong"] = read(b)
        write(b, dv.OP_FRAME, {"cmd": "DISPATCH", "evt": "VOICE_STATE_CREATE",
                               "data": state("5", "eve")})
        write(b, dv.OP_FRAME, {"cmd": req["cmd"], "nonce": req["nonce"],
                               "data": {"id": "c9"}, "evt": None})
        op, req2 = read(b)
        write(b, dv.OP_FRAME, {"cmd": req2["cmd"], "nonce": req2["nonce"], "evt": "ERROR",
                               "data": {"code": 4006, "message": "Not authenticated"}})
        b.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    rpc = dv.RpcClient(sock=a)
    ready = rpc.connect("cid")
    check(seen["hello"] == (dv.OP_HANDSHAKE, {"v": 1, "client_id": "cid"}), "handshake frame")
    check(ready == {"v": 1}, "READY payload returned")
    data = rpc.send("GET_SELECTED_VOICE_CHANNEL", wait=True, timeout=2)
    check(data == {"id": "c9"}, f"reply matched by nonce (got {data})")
    check(seen["pong"] == (dv.OP_PONG, {"x": 1}), "PING answered with PONG")
    evt, payload = rpc.events.get(timeout=2)
    check(evt == "VOICE_STATE_CREATE" and payload["user"]["id"] == "5", "event queued")
    try:
        rpc.send("SET_USER_VOICE_SETTINGS", {"user_id": "5"}, wait=True, timeout=2)
        check(False, "error reply raises")
    except dv.RpcError as e:
        check("Not authenticated" in str(e), f"error message surfaced (got {e})")
    check(rpc.closed.wait(2), "closed once the server hangs up")
    check(rpc.events.get(timeout=2) == (None, None), "close wakes the event loop")
    th.join(2)


def test_token_cache_and_refresh():
    print("OAuth")
    host, layer, ext = make(users=None)
    requests = []

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        fields = dict(p.split("=", 1) for p in req.data.decode().split("&"))
        requests.append(fields)
        return Response({"access_token": "A" + fields["grant_type"][0],
                         "refresh_token": "R", "expires_in": 604800})

    original = dv.urllib.request.urlopen
    dv.urllib.request.urlopen = fake_urlopen
    try:
        rpc = FakeRpc({"AUTHORIZE": {"code": "xyz"},
                       "AUTHENTICATE": {"user": {"id": "1", "username": "me"}}})
        ext._authenticate(rpc)
        check([c for c, _, _ in rpc.sent] == ["AUTHORIZE", "AUTHENTICATE"],
              f"no cache: authorize then authenticate (got {rpc.sent})")
        check(requests[0]["grant_type"] == "authorization_code" and requests[0]["code"] == "xyz",
              "code exchanged")
        check(oct(ext.token_file.stat().st_mode & 0o777) == "0o600", "token file private")

        rpc = FakeRpc({"AUTHENTICATE": {"user": {"id": "1"}}})
        ext._authenticate(rpc)
        check([c for c, _, _ in rpc.sent] == ["AUTHENTICATE"], "cached token reused")
        check(len(requests) == 1, "no token request for a fresh cached token")

        token = json.loads(ext.token_file.read_text())
        token["expires_at"] = time.time() + 60
        ext.token_file.write_text(json.dumps(token))
        rpc = FakeRpc({"AUTHENTICATE": {"user": {"id": "1"}}})
        ext._authenticate(rpc)
        check(requests[-1]["grant_type"] == "refresh_token", "expiring token refreshed")
        check(rpc.sent[0][1] == {"access_token": "Ar"}, "refreshed token used")

        rpc = FakeRpc({"AUTHENTICATE": dv.RpcError("invalid token")})
        rpc.replies["AUTHORIZE"] = {"code": "new"}
        calls = []

        def authenticate(args):
            calls.append(args)
            if len(calls) == 1:
                raise dv.RpcError("invalid token")
            return {"user": {"id": "1"}}
        rpc.replies["AUTHENTICATE"] = authenticate
        ext._authenticate(rpc)
        check([c for c, _, _ in rpc.sent] == ["AUTHENTICATE", "AUTHORIZE", "AUTHENTICATE"],
              "rejected token -> authorize again")
    finally:
        dv.urllib.request.urlopen = original


def test_config_secret_file():
    print("Config")
    d = tempfile.mkdtemp()
    secret = pathlib.Path(d, "secret")
    secret.write_text("s3cret\n")
    host = FaderHost(port="/dev/null", num_faders=4)
    layer = host.add_layer("Discord")
    captured = {}

    class Probe(dv.DiscordVoiceExtension):
        def __init__(self, host, **kw):
            captured.update(kw)

    real = dv.DiscordVoiceExtension
    dv.DiscordVoiceExtension = Probe
    try:
        dv.register(layer, {"client_id": 42, "client_secret_file": str(secret)})
    finally:
        dv.DiscordVoiceExtension = real
    check(captured["client_secret"] == "s3cret" and captured["client_id"] == "42",
          f"secret read from file (got {captured})")


def test_auto_and_explicit_layers():
    from faders import config as config_mod
    names = {"pulse_sink": "Audio", "discord_voice": "Discord"}
    cfg = dict(config_mod.DEFAULT_CONFIG,
               extensions={"pulse_sink": [{"fader": 0}], "discord_voice": {"client_id": 1}})
    specs = config_mod.layer_specs(cfg, names.get)
    check([n for n, _ in specs] == ["Audio", "Discord"], f"one layer per plugin (got {specs})")
    cfg["layers"] = [{"name": "Mix", "extensions": {"pulse_sink": [], "discord_voice": {}}}]
    specs = config_mod.layer_specs(cfg, names.get)
    check(len(specs) == 1 and specs[0][0] == "Mix"
          and set(specs[0][1]) == {"pulse_sink", "discord_voice"},
          "explicit layers win over auto")


def main():
    tests = [
        test_offline_and_empty_screens,
        test_users_bound_in_join_order_without_self,
        test_fader_writes_volume_and_mute_only,
        test_discord_side_changes_move_the_fader,
        test_paging,
        test_inactive_layer_stores_only,
        test_rpc_framing_over_socket,
        test_token_cache_and_refresh,
        test_config_secret_file,
        test_auto_and_explicit_layers,
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
