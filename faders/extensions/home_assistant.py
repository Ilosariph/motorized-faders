"""
Home Assistant extension.

Binds faders to Home Assistant entities:
  light   - fader move (human)       -> brightness; dragged to the bottom
                                        turns the light off
          - fader button (mute)      -> light off / back on at its brightness
          - changed in HA            -> fader follows; light off -> fader
                                        parks (muted), on -> it returns
  cover   - `"attribute": "position"` (default): fader = cover position,
            0 closed, 100 open
          - `"attribute": "tilt"`: fader = slat tilt position
          - fader button (mute)      -> position / tilt 0, unmute restores it
          - changed in HA            -> fader follows once the cover stops
Two faders on one cover, one per attribute, give position and angle.

Entries fill the faders in order; more than fit become pages of four, stepped
with G2 + F1 / F4 and shown as "1/2" on the last screen. A `null` entry
leaves its fader empty, e.g. to start a blind's pair on a new page.

Screens show the label on line 1 and the state on line 2. Creases: every 25%
for brightness and position, one at 50% tilt (slats level on most blinds).
Lights that only switch and covers that only open/close get none: their
fader is on/off, split at the bottom / at half travel.

Talks to HA's WebSocket API with a long-lived access token (profile ->
Security -> Long-lived access tokens). Pure stdlib, no Python deps.

Config entry shape (faders/config.json):
    "home_assistant": {
        "url": "http://homeassistant.local:8123",
        "token_file": "~/.config/motorized-faders/ha-token",
        "faders": [
            {"entity": "light.desk", "label": "Desk"},
            null,
            {"entity": "cover.office_blinds", "attribute": "position",
             "label": "Blinds"},
            {"entity": "cover.office_blinds", "attribute": "tilt",
             "label": "Slats"}
        ]
    }

`token` may be given inline instead of `token_file`; keep the config private
then (a Nix-generated config sits in the world-readable store, so prefer the
file there). `url` may be https; certificates are verified unless
`"verify_ssl": false`. `label` defaults to the entity's friendly name.
`creases` (default true, per fader) turns the detents off when false.
"""

import base64
import hashlib
import json
import logging
import math
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from pathlib import Path

from ..core import DISPLAY_LINE_MAX, Extension

# Layer name when the config does not group plugins into layers itself.
LAYER_NAME = "Home"

RECONNECT_INTERVAL_S = 5.0
REQUEST_TIMEOUT_S = 10.0
# Ping HA this often; a connection that misses the pong is dropped.
PING_INTERVAL_S = 30.0

# WebSocket opcodes (RFC 6455).
WS_CONT, WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# A fader below this is "at the bottom": a light dragged there turns off, and
# an entity off/closed in HA leaves a fader sitting there unmuted.
OFF_BELOW_PCT = 1.0
# Matches the firmware's MUTE_PARK_ZONE_PCT: a muted fader dragged above this
# picks a new restore level.
MUTE_PARK_ZONE_PCT = 3.0

# Fader <-> HA differences smaller than this are rounding, not a change.
FOLLOW_EPSILON_PCT = 2.0

log = logging.getLogger("faders.home_assistant")

# A fader not bound to anything known yet: the next sync drives it.
_UNKNOWN = object()


def register(host, cfg):
    token = cfg.get("token")
    if not token and cfg.get("token_file"):
        token = Path(cfg["token_file"]).expanduser().read_text().strip()
    slots = [None if f is None else
             Binding(str(f["entity"]), f.get("attribute"), f.get("label"),
                     bool(f.get("creases", True)))
             for f in cfg.get("faders", [])]
    ext = HomeAssistantExtension(
        host=host,
        url=str(cfg["url"]),
        token=token or "",
        slots=slots,
        verify_ssl=bool(cfg.get("verify_ssl", True)),
    )
    host.register(ext)


class HaError(Exception):
    pass


# ----- WebSocket ----------------------------------------------------------


def ws_url(url):
    """http(s)://host:port[/] -> (secure, host, port, path) of the WS API."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme in ("https", "wss")
    port = parts.port or (443 if secure else 80)
    path = parts.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path += "/api/websocket"
    return secure, parts.hostname, port, path


def ws_accept(key):
    """Sec-WebSocket-Accept the server must answer `key` with."""
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


class WebSocket:
    """Minimal RFC 6455 client: text frames out, text frames in, pings."""

    def __init__(self, sock):
        self._sock = sock
        self._send_lock = threading.Lock()

    @classmethod
    def connect(cls, url, verify_ssl=True, timeout=REQUEST_TIMEOUT_S):
        secure, host, port, path = ws_url(url)
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                ctx = ssl.create_default_context()
                if not verify_ssl:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=host)
            ws = cls(sock)
            ws.handshake(f"{host}:{port}", path)
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        return ws

    def handshake(self, host, path):
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(1)
            if not chunk:
                raise HaError("connection closed during WebSocket handshake")
            head += chunk
            if len(head) > 16384:
                raise HaError("oversized WebSocket handshake reply")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise HaError(f"WebSocket upgrade refused: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != ws_accept(key):
            raise HaError("bad Sec-WebSocket-Accept")

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(head + mask + body)

    def send_text(self, text):
        self._send_frame(WS_TEXT, text.encode())

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise HaError("Home Assistant closed the connection")
            buf += chunk
        return buf

    def _recv_frame(self):
        b0, b1 = self._recv_exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if b1 & 0x80 else None
        payload = self._recv_exact(n)
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def recv_text(self):
        """Next whole text message; answers pings on the way."""
        parts = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == WS_PING:
                self._send_frame(WS_PONG, payload)
                continue
            if opcode == WS_PONG:
                continue
            if opcode == WS_CLOSE:
                raise HaError("Home Assistant closed the connection")
            if opcode in (WS_TEXT, WS_BINARY, WS_CONT):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode()

    def close(self):
        try:
            self._send_frame(WS_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class HaClient:
    """
    HA's WebSocket API over a WebSocket: auth, then id-numbered commands.
    Replies are matched by id; subscription events land in `events`.
    """

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._replies = {}
        self.events = queue.Queue()
        self.closed = threading.Event()
        self._reader = None

    def _recv(self):
        return json.loads(self._ws.recv_text())

    def authenticate(self, token):
        msg = self._recv()
        if msg.get("type") != "auth_required":
            raise HaError(f"unexpected greeting: {msg}")
        self._ws.send_text(json.dumps({"type": "auth", "access_token": token}))
        msg = self._recv()
        if msg.get("type") == "auth_invalid":
            raise HaError(f"token rejected: {msg.get('message', '')}")
        if msg.get("type") != "auth_ok":
            raise HaError(f"unexpected auth reply: {msg}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return msg.get("ha_version")

    def _read_loop(self):
        try:
            while True:
                msgs = self._recv()
                # HA may coalesce several messages into one frame.
                for msg in msgs if isinstance(msgs, list) else [msgs]:
                    self._handle(msg)
        except (OSError, HaError, ValueError):
            pass
        finally:
            self.closed.set()
            self.events.put(None)
            with self._id_lock:
                for slot in self._replies.values():
                    slot[0].set()
                self._replies.clear()

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "event":
            self.events.put(msg.get("event") or {})
            return
        with self._id_lock:
            slot = self._replies.pop(msg.get("id"), None)
        if slot is not None:
            slot[1] = msg
            slot[0].set()
        elif kind == "result" and not msg.get("success", True):
            log.warning("Home Assistant: %s", (msg.get("error") or {}).get("message", msg))

    def send(self, payload, wait=False, timeout=REQUEST_TIMEOUT_S):
        """
        Send a command. With `wait`, block for its result and return it,
        raising HaError on failure; otherwise fire and forget (a failure is
        still logged by the reader).
        """
        if self.closed.is_set():
            raise HaError("not connected")
        slot = [threading.Event(), None]
        with self._id_lock:
            msg_id = self._next_id
            self._next_id += 1
            if wait:
                self._replies[msg_id] = slot
        try:
            self._ws.send_text(json.dumps(dict(payload, id=msg_id)))
        except OSError as e:
            raise HaError(f"write failed: {e}") from e
        if not wait:
            return None
        if not slot[0].wait(timeout):
            with self._id_lock:
                self._replies.pop(msg_id, None)
            raise HaError(f"{payload.get('type')}: no reply")
        reply = slot[1]
        if reply is None:
            raise HaError(f"{payload.get('type')}: connection lost")
        if reply.get("type") == "result" and not reply.get("success", True):
            raise HaError(f"{payload.get('type')}: "
                          f"{(reply.get('error') or {}).get('message')}")
        return reply.get("result")

    def close(self):
        self._ws.close()
        self.closed.set()


def apply_entity_diff(states, event):
    """
    Fold one `subscribe_entities` event into `states` ({entity_id: {"state",
    "attributes"}}). Returns the entity ids that changed.
    """
    changed = []
    for eid, full in (event.get("a") or {}).items():
        states[eid] = {"state": full.get("s"), "attributes": dict(full.get("a") or {})}
        changed.append(eid)
    for eid, diff in (event.get("c") or {}).items():
        entry = states.setdefault(eid, {"state": None, "attributes": {}})
        plus = diff.get("+") or {}
        if "s" in plus:
            entry["state"] = plus["s"]
        entry["attributes"].update(plus.get("a") or {})
        for name in (diff.get("-") or {}).get("a") or []:
            entry["attributes"].pop(name, None)
        changed.append(eid)
    for eid in event.get("r") or []:
        states[eid] = {"state": "unavailable", "attributes": {}}
        changed.append(eid)
    return changed


# ----- bindings -----------------------------------------------------------


class Binding:
    """One entity attribute on a fader slot, and what is known of it."""

    # Per kind: (min seconds between writes, debounce instead of throttle,
    # seconds to ignore HA's reports after our own write).
    WRITE = {
        "brightness": (0.15, False, 1.0),
        # A cover is a motor: one command when the fader comes to rest, not
        # a stream of them during the drag.
        "position": (0.5, True, 3.0),
        "tilt": (0.5, True, 3.0),
    }

    def __init__(self, entity, attribute=None, label=None, creases=True):
        self.entity = entity
        domain = entity.split(".", 1)[0]
        if domain == "light":
            self.kind = "brightness"
        elif domain == "cover":
            self.kind = "tilt" if attribute == "tilt" else "position"
        else:
            raise ValueError(f"{entity}: only light.* and cover.* entities are supported")
        self.label = label
        self.creases = creases
        self.name = label or entity
        self.available = False
        # Per HA: off / closed (value at the bottom), and the value 0-100.
        # Between our write and HA's report they hold what we wrote.
        self.off = None
        self.value = None
        # Fader level this binding last put the fader at; None if unknown.
        self.level = None
        # Fader level unmute returns to: the last value above the bottom.
        self.restore = None
        # Mute state this binding last gave its fader, so the Pico's
        # confirmation of it is not taken for a button press.
        self.muted = False
        # Light that dims / cover that takes a position (else only on/off,
        # open/close).
        self.settable = True
        self.moving = False
        self.last_write_t = -1e9
        self.sent_t = -1e9
        self.pending = None
        self.timer = None
        self.resync = None

    def crease_layout(self):
        if not self.creases or not self.settable:
            return []
        if self.kind == "tilt":
            return [(50.0, 70.0, 18)]
        return [(25.0, 55.0, 10), (50.0, 55.0, 10), (75.0, 55.0, 10)]

    def echo(self):
        """True while HA's reports may still be our own write echoing."""
        window = Binding.WRITE[self.kind][2]
        return self.pending is not None or time.monotonic() - self.last_write_t < window

    def update(self, state):
        """Take an HA state ({"state", "attributes"})."""
        attrs = state.get("attributes") or {}
        s = state.get("state")
        if not self.label and attrs.get("friendly_name"):
            self.name = attrs["friendly_name"]
        self.available = s not in (None, "unavailable", "unknown")
        if not self.available:
            return
        if self.kind == "brightness":
            modes = attrs.get("supported_color_modes")
            if modes is not None:
                self.settable = any(m != "onoff" for m in modes)
            self.off = s != "on"
            if self.off:
                self.value = 0.0
            elif not self.settable:
                self.value = 100.0
            elif attrs.get("brightness") is not None:
                self.value = round(float(attrs["brightness"]) / 255.0 * 100.0, 1)
        else:
            key = "current_tilt_position" if self.kind == "tilt" else "current_position"
            self.moving = s in ("opening", "closing")
            self.settable = key in attrs
            if attrs.get(key) is not None:
                self.value = float(attrs[key])
            elif self.kind == "position" and s in ("open", "closed"):
                # Open/close-only cover: no position, just the end stops.
                self.value = 100.0 if s == "open" else 0.0
            self.off = self.value is not None and self.value < OFF_BELOW_PCT
        # Mid-travel positions are not a level to come back to.
        if not self.off and self.value is not None and not self.moving:
            self.restore = self.value

    def status_text(self):
        if not self.available:
            return "unavailable"
        if self.value is None:
            return "--"
        if self.kind == "brightness":
            if self.off:
                return "off"
            return f"on {self.value:.0f}%" if self.settable else "on"
        if self.kind == "tilt":
            return f"tilt {self.value:.0f}%"
        if self.off:
            return "closed"
        return f"open {self.value:.0f}%" if self.settable else "open"

    def service_for(self, value):
        """(domain, service, data) that sets the entity to fader `value`."""
        if self.kind == "brightness":
            if value < OFF_BELOW_PCT:
                return "light", "turn_off", {}
            if not self.settable:
                return "light", "turn_on", {}
            return "light", "turn_on", {"brightness": max(1, round(value / 100.0 * 255))}
        if self.kind == "tilt":
            if not self.settable:
                return ("cover", "open_cover_tilt" if value >= 50 else "close_cover_tilt", {})
            return "cover", "set_cover_tilt_position", {"tilt_position": round(value)}
        if not self.settable:
            return "cover", "open_cover" if value >= 50 else "close_cover", {}
        return "cover", "set_cover_position", {"position": round(value)}


class HomeAssistantExtension(Extension):
    def __init__(self, host, url, token, slots, verify_ssl=True, start=True):
        """`slots`: Binding or None per fader, page after page."""
        self.host = host
        self.url = url
        self.token = token
        self.verify_ssl = verify_ssl
        n = host.num_faders
        self._slots = list(slots)
        # G2 + F1/F4 pages only when there is more than one page; otherwise
        # the layer keeps its A/B swap.
        self.PAGED = len(self._slots) > n
        self._page = 0
        # Binding each fader shows, None for an empty slot.
        self._bound = [_UNKNOWN] * n
        self._lock = threading.RLock()
        self._states = {}
        self._status = "offline"
        self._client = None
        self._stop = threading.Event()

        self._sync()
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def bindings(self):
        return [b for b in self._slots if b is not None]

    def page_count(self):
        return max(1, math.ceil(len(self._slots) / self.host.num_faders))

    def _binding(self, fader_idx):
        b = self._bound[fader_idx] if 0 <= fader_idx < len(self._bound) else None
        return None if b is _UNKNOWN else b

    def _fader_of(self, b):
        for idx, bound in enumerate(self._bound):
            if bound is b:
                return idx
        return None

    # ----- faders and screens ---------------------------------------------

    def _sync(self):
        """
        Bind each fader to its slot on the current page and push its screen.
        A fader whose binding changed is driven; one that kept it is not.
        """
        n = self.host.num_faders
        with self._lock:
            self._page = min(self._page, self.page_count() - 1)
            start = self._page * n
            for idx in range(n):
                pos = start + idx
                b = self._slots[pos] if pos < len(self._slots) else None
                if b is not self._bound[idx]:
                    self._bound[idx] = b
                    self._drive(idx, b)
                self._push_display(idx)

    def _drive(self, idx, b):
        """Put fader `idx` where `b`'s entity is. Caller holds the lock."""
        if b is None:
            self.host.set_creases(idx, [])
            if self.host.is_muted(idx):
                self.host.set_mute(idx, False, 0.0)
            else:
                self.host.set_fader(idx, 0.0)
            return
        self.host.set_creases(idx, b.crease_layout())
        if not b.available or b.value is None:
            b.muted = self.host.is_muted(idx)
            return
        b.muted = b.off
        if b.off:
            b.level = None
            self.host.set_mute(idx, True, b.restore)
        elif self.host.is_muted(idx):
            b.level = b.value
            self.host.set_mute(idx, False, b.value)
        else:
            b.level = b.value
            self.host.set_fader(idx, b.value)

    def _push_display(self, idx):
        """Label and state; the last screen carries the page. Lock held."""
        n = self.host.num_faders
        b = self._binding(idx)
        if b is not None:
            line1 = b.name
            line2 = b.status_text() if self._status == "ok" else self._status
        elif idx == 0 and self._status != "ok":
            line1, line2 = "Home", self._status
        else:
            line1, line2 = "-", ""
        if self.PAGED and idx == n - 1:
            page = f"{self._page + 1}/{self.page_count()}"
            width = DISPLAY_LINE_MAX - len(page)
            line2 = f"{line2[:width - 1]:<{width}}{page}"
        self.host.set_display(idx, line1, line2)

    def _follow(self, b):
        """
        HA reported a change for `b`: move its fader to match, unless the
        cover is still travelling. Caller holds the lock.
        """
        idx = self._fader_of(b)
        if idx is None or not b.available or b.value is None or b.moving:
            return
        muted = self.host.is_muted(idx)
        if b.off:
            # Off with the fader already at the bottom: dragged there, so it
            # stays a fader at 0 rather than becoming a mute.
            at_bottom = b.level is not None and b.level < OFF_BELOW_PCT
            if not muted and not at_bottom:
                log.info("%s: off in Home Assistant", b.entity)
                b.level = None
                b.muted = True
                self.host.set_mute(idx, True, b.restore)
            return
        if muted:
            log.info("%s: on in Home Assistant", b.entity)
            b.level = b.value
            b.muted = False
            self.host.set_mute(idx, False, b.value)
        elif b.level is None or abs(b.value - b.level) >= FOLLOW_EPSILON_PCT:
            log.info("%s: %s set in Home Assistant", b.entity, b.status_text())
            b.level = b.value
            self.host.set_fader(idx, b.value)

    def _set_status(self, status):
        with self._lock:
            if status == self._status:
                return
            self._status = status
            for idx in range(self.host.num_faders):
                self._push_display(idx)

    # ----- host hooks ------------------------------------------------------

    def on_connect(self):
        """Rebooted Pico: drive every fader again."""
        with self._lock:
            self._bound = [_UNKNOWN] * self.host.num_faders
            self._sync()

    def on_page(self, delta):
        with self._lock:
            self._page = (self._page + delta) % self.page_count()
            log.info("page %d/%d", self._page + 1, self.page_count())
            self._sync()

    def on_position(self, fader_idx, value):
        with self._lock:
            b = self._binding(fader_idx)
            if b is None or not b.available:
                return
            if self.host.is_muted(fader_idx):
                # Parked: a drag only picks where unmute returns to.
                if value > MUTE_PARK_ZONE_PCT:
                    b.restore = value
                return
            b.level = value
            if b.settable:
                pass
            elif b.kind == "brightness":
                value = 0.0 if value < OFF_BELOW_PCT else 100.0
            else:
                value = 100.0 if value >= 50.0 else 0.0
            b.value = value
            b.off = value < OFF_BELOW_PCT
            if not b.off:
                b.restore = value
            self._push_display(fader_idx)
            self._queue_write(b, value)

    def on_mute(self, fader_idx, muted):
        with self._lock:
            b = self._binding(fader_idx)
            if b is None:
                return
            if not b.available:
                # Nothing to switch: put the fader back the way it was.
                if muted != b.muted:
                    self.host.set_mute(fader_idx, b.muted)
                return
            if b.muted == muted:
                return  # our own set_mute confirmed
            self._cancel_write(b)
            b.muted = b.off = muted
            if muted:
                b.value = 0.0
                call = b.service_for(0.0)
            elif b.restore is None:
                # Unknown level: let HA bring back the entity's own.
                b.value = 100.0
                call = (("light", "turn_on", {}) if b.kind == "brightness"
                        else b.service_for(100.0))
            else:
                b.value = b.level = b.restore
                call = b.service_for(b.restore)
            self._push_display(fader_idx)
        log.info("%s: %s", b.entity, "off" if muted else "on")
        self._call_service(b, *call)

    def stop(self):
        self._stop.set()
        with self._lock:
            for b in self.bindings:
                self._cancel_write(b)
                if b.resync is not None:
                    b.resync.cancel()
            client = self._client
        if client is not None:
            client.close()

    # ----- writes to HA ----------------------------------------------------

    def _queue_write(self, b, value):
        """Throttle (lights) or debounce (covers) a drag. Lock held."""
        interval, debounce, _ = Binding.WRITE[b.kind]
        b.pending = value
        b.last_write_t = time.monotonic()
        if debounce:
            self._cancel_timer(b)
            wait = interval
        elif b.timer is not None:
            return
        else:
            wait = interval - (time.monotonic() - b.sent_t)
        if wait > 0:
            b.timer = threading.Timer(wait, self._flush, (b,))
            b.timer.daemon = True
            b.timer.start()
            return
        self._flush(b)

    def _flush(self, b):
        with self._lock:
            b.timer = None
            value = b.pending
            b.pending = None
            if value is None:
                return
            b.sent_t = b.last_write_t = time.monotonic()
        log.debug("%s: fader -> %.0f%%", b.entity, value)
        self._call_service(b, *b.service_for(value))

    def _cancel_timer(self, b):
        if b.timer is not None:
            b.timer.cancel()
            b.timer = None

    def _cancel_write(self, b):
        self._cancel_timer(b)
        b.pending = None

    def _call_service(self, b, domain, service, data):
        client = self._client
        if client is None:
            return
        try:
            client.send({"type": "call_service", "domain": domain, "service": service,
                         "service_data": data, "target": {"entity_id": b.entity}})
        except HaError as e:
            log.debug("%s.%s dropped: %s", domain, service, e)

    # ----- events from HA --------------------------------------------------

    def handle_event(self, event):
        """One `subscribe_entities` event."""
        with self._lock:
            initial = set((event.get("a") or {}))
            for eid in apply_entity_diff(self._states, event):
                for b in self.bindings:
                    if b.entity == eid:
                        self._apply_state(b, eid in initial)

    def _apply_state(self, b, initial=False):
        """Fold HA's latest state for `b` in and follow it. Lock held."""
        if not initial and b.echo():
            # Mid-drag, HA reports values we have already moved past. Keep
            # what we wrote; look again once the echo window has passed.
            self._schedule_resync(b)
            return
        b.update(self._states.get(b.entity) or {})
        idx = self._fader_of(b)
        if idx is not None:
            self._push_display(idx)
            if initial:
                self._drive(idx, b)
            else:
                self._follow(b)

    def _schedule_resync(self, b):
        if b.resync is not None:
            b.resync.cancel()
        wait = max(0.05, Binding.WRITE[b.kind][2] - (time.monotonic() - b.last_write_t))
        b.resync = threading.Timer(wait, self._resync, (b,))
        b.resync.daemon = True
        b.resync.start()

    def _resync(self, b):
        with self._lock:
            b.resync = None
            if not self._stop.is_set():
                self._apply_state(b)

    # ----- connection ------------------------------------------------------

    def _run(self):
        if not self.token:
            log.error("no token configured; set `token` or `token_file`")
            self._set_status("no token")
            return
        while not self._stop.is_set():
            client = None
            status = "offline"
            try:
                client = HaClient(WebSocket.connect(self.url, self.verify_ssl))
                version = client.authenticate(self.token)
                log.info("connected to Home Assistant %s at %s", version, self.url)
                with self._lock:
                    self._client = client
                self._set_status("ok")
                client.send({"type": "subscribe_entities",
                             "entity_ids": sorted({b.entity for b in self.bindings})},
                            wait=True)
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    try:
                        event = client.events.get(timeout=1.0)
                    except queue.Empty:
                        event = {}
                    if event is None:
                        break
                    if event:
                        self.handle_event(event)
                    if time.monotonic() - last_ping > PING_INTERVAL_S:
                        client.send({"type": "ping"}, wait=True)
                        last_ping = time.monotonic()
                if not self._stop.is_set():
                    log.warning("Home Assistant connection lost")
            except (HaError, OSError, ValueError) as e:
                if not self._stop.is_set():
                    log.warning("Home Assistant: %s; retrying in %.0f s", e,
                                RECONNECT_INTERVAL_S)
                if "token rejected" in str(e):
                    status = "bad token"
            finally:
                with self._lock:
                    if self._client is client:
                        self._client = None
                if client is not None:
                    client.close()
            self._set_status(status)
            self._stop.wait(RECONNECT_INTERVAL_S)
