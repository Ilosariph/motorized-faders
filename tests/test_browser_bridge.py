"""
browser_bridge tests — WebSocket handshake, origin check and messages over a
real socket on 127.0.0.1; the media plugin's browser routing against a fake
bridge.

Run with: python3 tests/test_browser_bridge.py
"""

import base64
import json
import os
import pathlib
import socket
import struct
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_host import check, FAILURES, PASSES  # noqa: E402

from faders import browser_bridge  # noqa: E402
from faders.browser_bridge import BrowserBridge  # noqa: E402
from faders.extensions import media  # noqa: E402


class Client:
    """Just enough of a browser's WebSocket."""

    def __init__(self, port, origin="moz-extension://1234"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        key = base64.b64encode(os.urandom(16)).decode()
        head = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n")
        if origin is not None:
            head += f"Origin: {origin}\r\n"
        self.sock.sendall((head + "\r\n").encode())
        self.response = b""
        while b"\r\n\r\n" not in self.response:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            self.response += chunk
        self.status = self.response.split(b" ", 2)[1] if self.response else b""
        self.accept_ok = browser_bridge.accept_key(key).encode() in self.response

    def send_frame(self, payload, opcode=0x1):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        else:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send(self, message):
        self.send_frame(json.dumps(message).encode())

    def recv(self):
        b0, b1 = self._exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        return b0 & 0x0F, self._exact(n)

    def _exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def close(self):
        self.sock.close()


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def make_bridge():
    changes = []
    bridge = BrowserBridge(port=0, on_change=lambda: changes.append(1))
    check(bridge.start(), "listens")
    return bridge, changes


def test_handshake():
    print("Handshake")
    # RFC 6455 section 1.3 example, independent of our own computation.
    check(browser_bridge.accept_key("dGhlIHNhbXBsZSBub25jZQ==")
          == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "accept key matches the RFC")
    bridge, changes = make_bridge()
    try:
        page = Client(bridge.port, origin="https://evil.example")
        check(page.status == b"403", f"web page origin refused (got {page.status})")
        page.close()
        bare = Client(bridge.port, origin=None)
        check(bare.status == b"403", "no origin refused")
        bare.close()
        check(not bridge.connected(), "nobody connected")

        ext = Client(bridge.port)
        check(ext.status == b"101" and ext.accept_ok, f"extension accepted ({ext.response!r})")
        check(wait_for(bridge.connected), "connected")
        check(wait_for(lambda: changes), "on_change on connect")
        chrome = Client(bridge.port, origin="chrome-extension://abcd")
        check(chrome.status == b"101", "chromium extension accepted")
        chrome.close()
        ext.close()
    finally:
        bridge.stop()


def test_messages():
    print("Messages")
    bridge, changes = make_bridge()
    try:
        ext = Client(bridge.port)
        wait_for(bridge.connected)
        check(bridge.rate() is None and bridge.set_rate(1.5) is False,
              "no page with media: nothing to set")
        ext.send({"type": "state", "rate": 1.25, "playing": True, "title": "A video"})
        check(wait_for(lambda: bridge.rate() == 1.25), f"rate reported (got {bridge.rate()})")
        check(bridge.title() == "A video", "title")

        check(bridge.set_rate(2.0) is True, "set")
        opcode, payload = ext.recv()
        check(opcode == 1 and json.loads(payload) == {"type": "set_rate", "rate": 2.0},
              f"sent to the extension (got {payload!r})")
        check(bridge.rate() == 2.0, "shown before the page confirms")

        ext.send_frame(b"hi", opcode=0x9)
        opcode, payload = ext.recv()
        check((opcode, payload) == (0xA, b"hi"), "ping answered")

        for bad in ({"type": "state", "rate": "fast"}, {"type": "state", "rate": True},
                    {"type": "state", "rate": -1}):
            ext.send(bad)
        ext.send_frame(b"not json")
        check(wait_for(lambda: bridge.rate() is None), "bad rate: no media")

        other = Client(bridge.port)
        wait_for(lambda: len(bridge._clients) == 2)
        ext.send({"type": "state", "rate": 1.5, "playing": False})
        other.send({"type": "state", "rate": 0.75, "playing": True})
        check(wait_for(lambda: bridge.rate() == 0.75), "the browser playing wins")
        other.send({"type": "state", "rate": 0.75, "playing": False})
        wait_for(lambda: not any(c.playing for c in bridge._clients))
        time.sleep(0.01)
        ext.send({"type": "state", "rate": 1.5, "playing": False})
        check(wait_for(lambda: bridge.rate() == 1.5), "neither playing: the latest")

        n = len(changes)
        other.send_frame(b"", opcode=0x8)
        check(wait_for(lambda: len(bridge._clients) == 1), "close frame ends it")
        check(len(changes) > n, "on_change on leaving")
        ext.close()
        check(wait_for(lambda: not bridge.connected()), "dropped socket ends it")
    finally:
        bridge.stop()


def test_port_taken():
    bridge, _ = make_bridge()
    try:
        second = BrowserBridge(port=bridge.port)
        check(second.start() is False, "port taken: no crash, just no bridge")
    finally:
        bridge.stop()


# ----- media plugin routing --------------------------------------------------

class FakeBridge:
    def __init__(self):
        self.value = None
        self.sets = []
        self.on_change = None

    def rate(self):
        return self.value

    def set_rate(self, rate):
        if self.value is None:
            return False
        self.sets.append(rate)
        self.value = rate
        return True


class MprisRates:
    def __init__(self):
        self.sets = []

    def rate(self, player_id):
        return 2.5

    def set_rate(self, player_id, rate):
        self.sets.append((player_id, rate))
        return False


def test_routing():
    print("Routing")
    for player_id in ("firefox.instance_1_2841", "chromium.instance42", "brave"):
        check(media.is_browser(player_id), f"{player_id} is a browser")
    for player_id in ("spotify", "com_edde746_plezy", "mpv", "vlc"):
        check(not media.is_browser(player_id), f"{player_id} is not")

    bridge, mpris = FakeBridge(), MprisRates()
    backend = media.BridgedBackend(mpris, bridge)
    check(backend.rate("firefox.instance_1_1") == 2.5, "no page media: MPRIS rate")
    check(backend.set_rate("firefox.instance_1_1", 1.5) is False
          and mpris.sets == [("firefox.instance_1_1", 1.5)], "no page media: MPRIS refuses")
    bridge.value = 1.25
    check(backend.rate("firefox.instance_1_1") == 1.25, "page media: its rate")
    check(backend.set_rate("firefox.instance_1_1", 1.5) is True and bridge.sets == [1.5],
          "page media: set through the extension")
    check(backend.rate("mpv") == 2.5 and backend.set_rate("mpv", 1.0) is False
          and bridge.sets == [1.5], "other players never go to the browser")


def test_fixed_cleared_on_change():
    from faders.core import FaderHost
    from test_host import FakeSerial
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Media")
    bridge = FakeBridge()

    class Backend(MprisRates):
        def players(self):
            return [media.Player("firefox.instance_1_1", "Firefox", media.PLAYING, "Video")]

        def audio(self, player_id):
            return None, False

    ext = media.MediaExtension(layer, media.BridgedBackend(Backend(), bridge),
                               bridge=bridge, start=False)
    layer.register(ext)
    check(bridge.on_change == ext.rates_changed, "plugin listens to the bridge")
    ext._fixed_rate.add("firefox.instance_1_1")
    bridge.on_change()
    check(not ext._fixed_rate and ext._poll_soon, "extension connects: try setting again")


def main():
    for test in (test_handshake, test_messages, test_port_taken, test_routing,
                 test_fixed_cleared_on_change):
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
