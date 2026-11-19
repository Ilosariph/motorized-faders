"""
Browser bridge: playback speed of web pages (YouTube, Spotify web, Jellyfin
web ...) for the media plugin.

Browsers publish their media sessions over MPRIS / Windows media sessions,
but refuse to have the rate set from outside (Firefox: "Rate setting is not
supported"). The extension in `browser/` sets it from inside the page and
connects here:

    extension -> host   {"type": "state", "rate": 1.5 | null,
                         "playing": true, "title": "..."}
    host -> extension   {"type": "set_rate", "rate": 1.5}

One WebSocket per browser profile, on 127.0.0.1 only. Pages can open
WebSockets to localhost too, so only extension origins (moz-extension://,
chrome-extension://) are let in.

The protocol is plain RFC 6455 text frames, done here with the standard
library rather than pulling in a dependency.
"""

import base64
import hashlib
import json
import logging
import socket
import struct
import threading
import time

DEFAULT_PORT = 47391

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_ORIGINS = ("moz-extension://", "chrome-extension://")
# Requests and messages are tiny; anything bigger is not our extension.
_MAX_HEADER = 8192
_MAX_PAYLOAD = 65536
_HANDSHAKE_TIMEOUT_S = 5.0

_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

log = logging.getLogger("faders.browser")


class _Closed(Exception):
    pass


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise _Closed()
        data += chunk
    return data


def accept_key(key):
    return base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()


def encode_frame(payload, opcode=_OP_TEXT):
    """Server frame: final, unmasked."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def read_frame(sock):
    """(opcode, payload) of one client frame. Fragments are not used by
    browsers for messages this small; a continuation is rejected."""
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    if not b0 & 0x80 or opcode == 0:
        raise _Closed()
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if n > _MAX_PAYLOAD or not b1 & 0x80:
        # Too big, or unmasked: clients must mask.
        raise _Closed()
    mask = _recv_exact(sock, 4)
    data = _recv_exact(sock, n)
    return opcode, bytes(c ^ mask[i % 4] for i, c in enumerate(data))


def _parse_request(raw):
    """(path, headers lower-cased) of an HTTP request head, None if bad."""
    try:
        lines = raw.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        return None
    if method != "GET":
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return path, headers


class _Client:
    def __init__(self, sock, origin):
        self.sock = sock
        self.origin = origin
        self.send_lock = threading.Lock()
        self.rate = None
        self.playing = False
        self.title = ""
        self.updated = 0.0

    def send(self, message):
        frame = encode_frame(json.dumps(message).encode())
        with self.send_lock:
            self.sock.sendall(frame)


class BrowserBridge:
    """
    WebSocket server for the browser extension. `rate()` / `set_rate()` act
    on the page the browser reports: the one playing, most recently changed
    first. `on_change` is called (no arguments, from a connection thread)
    when a browser connects, leaves or reports a new state.
    """

    def __init__(self, port=DEFAULT_PORT, host="127.0.0.1", on_change=None):
        self.port = port
        self.host = host
        self.on_change = on_change
        self._lock = threading.Lock()
        self._clients = []
        self._server = None
        self._thread = None

    # ----- lifecycle ---------------------------------------------------------

    def start(self):
        """Listen. False when the port is taken (another host running)."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.host, self.port))
        except OSError as e:
            log.error("browser bridge: cannot listen on %s:%d: %s", self.host, self.port, e)
            server.close()
            return False
        server.listen(4)
        self.port = server.getsockname()[1]
        self._server = server
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        log.info("browser bridge on ws://%s:%d", self.host, self.port)
        return True

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            server.close()
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.sock.close()
            except OSError:
                pass

    # ----- media state -------------------------------------------------------

    def connected(self):
        with self._lock:
            return bool(self._clients)

    def _current(self):
        """Client whose page the faders act on. Lock held."""
        with_media = [c for c in self._clients if c.rate is not None]
        if not with_media:
            return None
        return max(with_media, key=lambda c: (c.playing, c.updated))

    def rate(self):
        """Rate of the current page's media, None without one."""
        with self._lock:
            client = self._current()
            return None if client is None else client.rate

    def title(self):
        with self._lock:
            client = self._current()
            return "" if client is None else client.title

    def set_rate(self, rate):
        """Ask the current page for `rate`. False without one."""
        with self._lock:
            client = self._current()
        if client is None:
            return False
        try:
            client.send({"type": "set_rate", "rate": round(float(rate), 3)})
        except OSError:
            return False
        with self._lock:
            # Shown right away; the page confirms with its own report.
            client.rate = float(rate)
        return True

    # ----- connections -------------------------------------------------------

    def _accept(self):
        while self._server is not None:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _handshake(self, sock):
        """The connected client, or None after refusing it."""
        sock.settimeout(_HANDSHAKE_TIMEOUT_S)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(1024)
            if not chunk or len(raw) > _MAX_HEADER:
                return None
            raw += chunk
        request = _parse_request(raw.split(b"\r\n\r\n", 1)[0])
        if request is None:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return None
        _, headers = request
        origin = headers.get("origin", "")
        key = headers.get("sec-websocket-key")
        if not origin.startswith(_ORIGINS):
            log.warning("browser bridge: refused origin %r", origin)
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return None
        if headers.get("upgrade", "").lower() != "websocket" or not key:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return None
        sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key(key).encode() + b"\r\n\r\n")
        sock.settimeout(None)
        return _Client(sock, origin)

    def _serve(self, sock):
        client = None
        try:
            client = self._handshake(sock)
            if client is None:
                return
            with self._lock:
                self._clients.append(client)
            log.info("browser connected: %s", client.origin)
            self._changed()
            while True:
                opcode, payload = read_frame(sock)
                if opcode == _OP_CLOSE:
                    with client.send_lock:
                        sock.sendall(encode_frame(b"", _OP_CLOSE))
                    return
                if opcode == _OP_PING:
                    with client.send_lock:
                        sock.sendall(encode_frame(payload, _OP_PONG))
                elif opcode == _OP_TEXT:
                    self._message(client, payload)
        except (_Closed, OSError):
            pass
        finally:
            if client is not None:
                with self._lock:
                    if client in self._clients:
                        self._clients.remove(client)
                log.info("browser disconnected: %s", client.origin)
                self._changed()
            try:
                sock.close()
            except OSError:
                pass

    def _message(self, client, payload):
        try:
            message = json.loads(payload.decode())
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(message, dict) or message.get("type") != "state":
            return
        rate = message.get("rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
            rate = None
        with self._lock:
            client.rate = None if rate is None else float(rate)
            client.playing = bool(message.get("playing"))
            client.title = str(message.get("title") or "")[:200]
            client.updated = time.monotonic()
        self._changed()

    def _changed(self):
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("browser bridge: on_change failed")
