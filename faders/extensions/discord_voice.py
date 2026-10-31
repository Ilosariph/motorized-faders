"""
Discord voice extension.

Puts the people in your current Discord voice channel on the faders:
  - fader move (human)          -> that user's local volume, 0-200%
  - volume changed in Discord   -> fader follows
  - fader button (mute)         -> Discord's per-user local mute (right-click
                                   a user -> Mute); their volume is untouched
  - mute changed in Discord     -> fader parks / returns
  - screens                     -> user name on line 1, volume on line 2; the
                                   last screen also shows the page, "1/3"
  - G2 + F1 / F4                -> previous / next page of four users
  - haptic creases              -> every 50% of volume, stronger at 100%

Yourself are left out: Discord has no local volume for your own voice. Users
keep the order they joined in, so a fader does not change hands when someone
else joins.

Talks to the desktop client over its local RPC socket — the official client
only; Vesktop/arRPC implement no voice commands. Pure stdlib, no Python deps.

Setup (once):
  1. https://discord.com/developers/applications -> New Application.
  2. OAuth2: copy Client ID and Client Secret; add redirect
     `http://localhost` (any registered URL works, it is never visited).
  3. Put both in the config. On first start Discord pops an "Authorize"
     dialog; after that the token is cached and refreshed on its own.
RPC voice scopes are restricted to an application's own developers, which is
you here, so no approval from Discord is needed.

Config entry shape (faders/config.json):
    "discord_voice": {
        "client_id": "1234567890",
        "client_secret_file": "/run/secrets/discord-faders",
        "max_volume": 200
    }

`client_secret` may be given inline instead of `client_secret_file`; keep the
config private then (a Nix-generated config sits in the world-readable store,
so prefer the file there). `token_file` overrides where the OAuth token is
cached (default `$XDG_STATE_HOME/motorized-faders/discord_token.json`). Both
file paths may start with `~`.
`max_volume` (default 200) is the volume at the top of the fader travel.

Volumes here are the percentages Discord's slider shows. RPC speaks
amplitude instead (UI 60% is RPC 24); `ui_to_rpc` / `rpc_to_ui` convert at
the socket boundary.
`creases` (default true) turns the detents off when false.
"""

import json
import logging
import math
import os
import queue
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ..core import Extension

# Layer name when the config does not group plugins into layers itself.
LAYER_NAME = "Discord"

SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
TOKEN_URL = "https://discord.com/api/oauth2/token"
# Discord's edge rejects urllib's default agent.
USER_AGENT = "DiscordBot (https://github.com/Ilosariph/motorized-faders, 0.1)"

# IPC frame opcodes.
OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = range(5)

VOICE_EVENTS = ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE",
                "VOICE_STATE_DELETE")

RECONNECT_INTERVAL_S = 5.0
REQUEST_TIMEOUT_S = 10.0
# The user has to click "Authorize" in Discord; give them time.
AUTHORIZE_TIMEOUT_S = 300.0
# Refresh the token when it has less than this left.
TOKEN_REFRESH_MARGIN_S = 24 * 3600

# Coalesce volume writes: ignore changes smaller than this many percent of
# Discord volume, and send at most one per user this often. The last value of
# a drag is always sent, after the interval.
WRITE_EPSILON = 1.0
WRITE_MIN_INTERVAL_S = 0.05
# Discord echoes our own writes back as VOICE_STATE_UPDATE; ignore volume in
# those for this long after a write, or the fader chases its own history.
SELF_ECHO_WINDOW_S = 0.5

# Creases, in Discord volume %: (power %, pulse ms), as in pulse_sink.
CREASE_STEP = 50
CREASE_NORMAL = (55.0, 10)
CREASE_BIG = (70.0, 18)      # 100%: the user's own level, unboosted

DISPLAY_LINE2_MAX = 21

# Slider % <-> RPC amplitude %, fitted to the desktop client: below 100% the
# slider is a power curve (UI 30/60 -> RPC 4/24), above it every further 100%
# doubles amplitude (UI 150/200 -> RPC 141/200).
UI_EXPONENT = 2.77
RPC_MAX = 200.0


def ui_to_rpc(ui):
    if ui <= 0:
        return 0.0
    if ui <= 100:
        return 100.0 * (ui / 100.0) ** UI_EXPONENT
    return min(RPC_MAX, 100.0 * 2.0 ** ((ui - 100.0) / 100.0))


def rpc_to_ui(volume):
    if volume <= 0:
        return 0.0
    if volume <= 100:
        return 100.0 * (volume / 100.0) ** (1.0 / UI_EXPONENT)
    return min(200.0, 100.0 + 100.0 * math.log2(volume / 100.0))

log = logging.getLogger("faders.discord_voice")


def register(host, cfg):
    secret = cfg.get("client_secret")
    if not secret and cfg.get("client_secret_file"):
        secret = Path(cfg["client_secret_file"]).expanduser().read_text().strip()
    ext = DiscordVoiceExtension(
        host=host,
        client_id=str(cfg["client_id"]),
        client_secret=secret or "",
        redirect_uri=str(cfg.get("redirect_uri", "http://localhost")),
        token_file=cfg.get("token_file"),
        max_volume=float(cfg.get("max_volume", 200)),
        creases=bool(cfg.get("creases", True)),
    )
    host.register(ext)


class RpcError(Exception):
    pass


def ipc_paths():
    """Candidate sockets: native, flatpak and snap clients, pipes 0-9."""
    bases = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        if os.environ.get(var):
            bases.append(os.environ[var])
    bases.append("/tmp")
    subdirs = ("", "app/com.discordapp.Discord", "snap.discord")
    for base in dict.fromkeys(bases):
        for sub in subdirs:
            for i in range(10):
                yield os.path.join(base, sub, f"discord-ipc-{i}")


class RpcClient:
    """
    Discord's local RPC: length-prefixed JSON frames over a Unix socket.
    Replies are matched to requests by nonce; events land in `events`.
    """

    def __init__(self, sock=None):
        self._sock = sock
        self._send_lock = threading.Lock()
        self._replies = {}
        self._replies_lock = threading.Lock()
        self.events = queue.Queue()
        self.closed = threading.Event()
        self._reader = None

    def connect(self, client_id):
        """Open the socket and handshake. Returns the READY payload."""
        if self._sock is None:
            for path in ipc_paths():
                if not os.path.exists(path):
                    continue
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(path)
                except OSError:
                    sock.close()
                    continue
                self._sock = sock
                break
            else:
                raise RpcError("Discord is not running (no IPC socket)")
        self._write(OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        op, data = self._read_frame()
        if op == OP_CLOSE:
            raise RpcError(f"handshake refused: {data.get('message', data)}")
        if data.get("evt") != "READY":
            raise RpcError(f"unexpected handshake reply: {data}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return data.get("data", {})

    def _write(self, op, payload):
        body = json.dumps(payload).encode()
        with self._send_lock:
            self._sock.sendall(struct.pack("<II", op, len(body)) + body)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RpcError("Discord closed the connection")
            buf += chunk
        return buf

    def _read_frame(self):
        op, length = struct.unpack("<II", self._recv_exact(8))
        return op, json.loads(self._recv_exact(length) or b"{}")

    def _read_loop(self):
        try:
            while True:
                op, data = self._read_frame()
                if op == OP_PING:
                    self._write(OP_PONG, data)
                    continue
                if op == OP_CLOSE:
                    log.warning("Discord closed RPC: %s", data.get("message", data))
                    return
                nonce = data.get("nonce")
                with self._replies_lock:
                    slot = self._replies.pop(nonce, None) if nonce else None
                if slot is not None:
                    slot[1] = data
                    slot[0].set()
                elif data.get("cmd") == "DISPATCH":
                    self.events.put((data.get("evt"), data.get("data") or {}))
                elif data.get("evt") == "ERROR":
                    log.warning("Discord RPC error: %s", data.get("data"))
        except (OSError, RpcError, ValueError):
            pass
        finally:
            self.closed.set()
            self.events.put((None, None))
            with self._replies_lock:
                for slot in self._replies.values():
                    slot[0].set()
                self._replies.clear()

    def send(self, cmd, args=None, evt=None, wait=False, timeout=REQUEST_TIMEOUT_S):
        """
        Send a command. With `wait`, block for its reply and return its data,
        raising RpcError on an error reply; otherwise fire and forget (an
        error reply is still logged by the reader).
        """
        if self.closed.is_set():
            raise RpcError("not connected")
        nonce = str(uuid.uuid4())
        payload = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            payload["evt"] = evt
        slot = [threading.Event(), None]
        if wait:
            with self._replies_lock:
                self._replies[nonce] = slot
        try:
            self._write(OP_FRAME, payload)
        except OSError as e:
            raise RpcError(f"write failed: {e}") from e
        if not wait:
            return None
        if not slot[0].wait(timeout):
            with self._replies_lock:
                self._replies.pop(nonce, None)
            raise RpcError(f"{cmd}: no reply")
        reply = slot[1]
        if reply is None:
            raise RpcError(f"{cmd}: connection lost")
        if reply.get("evt") == "ERROR":
            raise RpcError(f"{cmd}: {(reply.get('data') or {}).get('message')}")
        return reply.get("data")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self.closed.set()


class User:
    __slots__ = ("id", "name", "volume", "mute")

    def __init__(self, uid, name, volume, mute):
        self.id = uid
        self.name = name
        self.volume = volume
        self.mute = mute

    @classmethod
    def from_voice_state(cls, state):
        user = state.get("user") or {}
        name = (state.get("nick") or user.get("global_name")
                or user.get("username") or "?")
        volume = rpc_to_ui(float(state.get("volume", 100.0)))
        return cls(str(user.get("id")), name, round(volume, 1),
                   bool(state.get("mute")))


# A fader not bound to anything known yet: the next sync drives it.
_UNKNOWN = object()


class DiscordVoiceExtension(Extension):
    PAGED = True

    def __init__(self, host, client_id, client_secret, redirect_uri="http://localhost",
                 token_file=None, max_volume=200.0, creases=True, start=True):
        self.host = host
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.max_volume = max_volume
        self._creases = creases
        if token_file is None:
            state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            token_file = os.path.join(state, "motorized-faders", "discord_token.json")
        self.token_file = Path(token_file).expanduser()

        n = host.num_faders
        self._lock = threading.RLock()
        self._users = []
        self._page = 0
        self._self_id = None
        self._channel_id = None
        self._status = "offline"
        # User id each fader shows, None for an empty slot.
        self._bound = [_UNKNOWN] * n
        # Volume writes: last sent value and time per user, the value waiting
        # for the interval to pass, and when Discord last saw a write from us.
        self._last_sent = {}
        self._last_sent_t = {}
        self._pending_vol = {}
        self._flush_timers = {}
        self._rpc = None

        self._stop = threading.Event()
        self._sync()
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # ----- mapping ---------------------------------------------------------

    def _to_fader(self, volume):
        if self.max_volume <= 0:
            return 0.0
        return max(0.0, min(100.0, volume / self.max_volume * 100.0))

    def _to_volume(self, fader_pct):
        return fader_pct / 100.0 * self.max_volume

    def crease_layout(self):
        layout = []
        for vol in range(CREASE_STEP, int(self.max_volume), CREASE_STEP):
            power, ms = CREASE_BIG if vol == 100 else CREASE_NORMAL
            layout.append((self._to_fader(vol), power, ms))
        return layout

    def page_count(self):
        with self._lock:
            return max(1, math.ceil(len(self._users) / self.host.num_faders))

    def _user_at(self, idx):
        """User shown on fader `idx`, or None. Caller holds the lock."""
        uid = self._bound[idx]
        if uid is None or uid is _UNKNOWN:
            return None
        for user in self._users:
            if user.id == uid:
                return user
        return None

    # ----- faders and screens ---------------------------------------------

    def _sync(self):
        """
        Bind each fader to the user it should show now and push its screen.
        A fader whose user changed is driven to that user's level; one that
        kept its user is not touched, so a join elsewhere moves nothing.
        """
        n = self.host.num_faders
        with self._lock:
            pages = self.page_count()
            self._page = min(self._page, pages - 1)
            start = self._page * n
            for idx in range(n):
                pos = start + idx
                user = self._users[pos] if pos < len(self._users) else None
                uid = user.id if user else None
                if uid != self._bound[idx]:
                    self._bound[idx] = uid
                    self._drive(idx, user)
                self._push_display(idx)

    def _drive(self, idx, user):
        """Move fader `idx` to `user`'s level and mute. Caller holds the lock."""
        if user is None:
            self.host.set_creases(idx, [])
            if self.host.is_muted(idx):
                self.host.set_mute(idx, False, 0.0)
            else:
                self.host.set_fader(idx, 0.0)
            return
        level = self._to_fader(user.volume)
        self.host.set_creases(idx, self.crease_layout() if self._creases else [])
        if user.mute:
            self.host.set_mute(idx, True, level)
        elif self.host.is_muted(idx):
            self.host.set_mute(idx, False, level)
        else:
            self.host.set_fader(idx, level)

    def _push_display(self, idx):
        """Name and volume; the last screen carries the page. Lock held."""
        n = self.host.num_faders
        user = self._user_at(idx)
        if user is not None:
            line1, line2 = user.name, f"vol {user.volume:.0f}%"
        elif idx == 0 and self._status != "ok":
            line1, line2 = "Discord", self._status
        elif idx == 0 and not self._users:
            line1, line2 = "Discord", "nobody here"
        else:
            line1, line2 = "-", ""
        if idx == n - 1:
            # Page right-aligned after the volume, always shown.
            page = f"{self._page + 1}/{self.page_count()}"
            width = DISPLAY_LINE2_MAX - len(page)
            line2 = f"{line2[:width - 1]:<{width}}{page}"
        self.host.set_display(idx, line1, line2)

    def _set_status(self, status):
        with self._lock:
            if status == self._status:
                return
            self._status = status
            if status != "ok":
                self._users = []
                self._channel_id = None
            self._sync()

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
            user = self._user_at(fader_idx)
            if user is None or user.mute:
                # Muted, the fader sits parked and a drag only picks where
                # unmute returns to; Discord's volume stays as it is.
                return
            volume = round(self._to_volume(value))
            user.volume = volume
            self._push_display(fader_idx)
            self._queue_volume(user.id, volume)

    def on_mute(self, fader_idx, muted):
        with self._lock:
            user = self._user_at(fader_idx)
            if user is None or user.mute == muted:
                return
            user.mute = muted
            uid = user.id
        log.info("%s: %s", user.name, "muted" if muted else "unmuted")
        self._send("SET_USER_VOICE_SETTINGS", {"user_id": uid, "mute": muted})

    def stop(self):
        self._stop.set()
        with self._lock:
            for timer in self._flush_timers.values():
                timer.cancel()
            rpc = self._rpc
        if rpc is not None:
            rpc.close()

    # ----- writes to Discord -----------------------------------------------

    def _send(self, cmd, args):
        rpc = self._rpc
        if rpc is None:
            return
        try:
            rpc.send(cmd, args)
        except RpcError as e:
            log.debug("%s dropped: %s", cmd, e)

    def _queue_volume(self, uid, volume):
        """Coalesce a drag into writes at most WRITE_MIN_INTERVAL_S apart."""
        with self._lock:
            last = self._last_sent.get(uid)
            if last is not None and abs(volume - last) < WRITE_EPSILON:
                self._pending_vol.pop(uid, None)
                return
            self._pending_vol[uid] = volume
            wait = WRITE_MIN_INTERVAL_S - (time.monotonic() - self._last_sent_t.get(uid, 0.0))
            if uid in self._flush_timers:
                return
            if wait > 0:
                timer = threading.Timer(wait, self._flush_volume, (uid,))
                timer.daemon = True
                self._flush_timers[uid] = timer
                timer.start()
                return
        self._flush_volume(uid)

    def _flush_volume(self, uid):
        with self._lock:
            self._flush_timers.pop(uid, None)
            volume = self._pending_vol.pop(uid, None)
            if volume is None:
                return
            self._last_sent[uid] = volume
            self._last_sent_t[uid] = time.monotonic()
        log.debug("user %s: volume %.0f%%", uid, volume)
        self._send("SET_USER_VOICE_SETTINGS",
                   {"user_id": uid, "volume": round(ui_to_rpc(volume), 2)})

    # ----- events from Discord ---------------------------------------------

    def _load_channel(self, channel):
        """Replace the user list with the channel's voice states."""
        with self._lock:
            self._channel_id = (channel or {}).get("id")
            self._users = []
            for state in (channel or {}).get("voice_states") or []:
                user = User.from_voice_state(state)
                if user.id != self._self_id:
                    self._users.append(user)
            self._status = "ok" if self._channel_id else "not in VC"
            self._page = 0
            log.info("voice channel %s: %d other user(s)",
                     (channel or {}).get("name", "none"), len(self._users))
            self._sync()

    def handle_event(self, evt, data):
        if evt in VOICE_EVENTS:
            self._voice_state(evt, data)

    def _voice_state(self, evt, data):
        new = User.from_voice_state(data)
        if new.id == self._self_id:
            return
        with self._lock:
            existing = next((u for u in self._users if u.id == new.id), None)
            if evt == "VOICE_STATE_DELETE":
                if existing is not None:
                    self._users.remove(existing)
                    log.info("%s left", existing.name)
                    self._sync()
                return
            if existing is None:
                self._users.append(new)
                log.info("%s joined", new.name)
                self._sync()
                return
            existing.name = new.name
            idx = next((i for i, uid in enumerate(self._bound) if uid == new.id), None)
            echo = time.monotonic() - self._last_sent_t.get(new.id, -1e9) < SELF_ECHO_WINDOW_S
            echo = echo or new.id in self._pending_vol
            volume_changed = not echo and abs(new.volume - existing.volume) >= WRITE_EPSILON
            if volume_changed:
                existing.volume = new.volume
            mute_changed = new.mute != existing.mute
            existing.mute = new.mute
            if idx is None:
                return
            level = self._to_fader(existing.volume)
            if mute_changed:
                log.info("%s %s in Discord", existing.name,
                         "muted" if new.mute else "unmuted")
                self.host.set_mute(idx, new.mute, level)
            elif volume_changed:
                log.info("%s: volume %.0f%% set in Discord", existing.name,
                         new.volume)
                if existing.mute:
                    # Parked: only where unmute returns to changes.
                    self.host.set_mute(idx, True, level)
                else:
                    self.host.set_fader(idx, level)
            self._push_display(idx)

    # ----- connection ------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            rpc = RpcClient()
            try:
                rpc.connect(self.client_id)
                self._authenticate(rpc)
                with self._lock:
                    self._rpc = rpc
                rpc.send("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT", wait=True)
                self._select_channel(rpc, rpc.send("GET_SELECTED_VOICE_CHANNEL", wait=True))
                while not self._stop.is_set():
                    try:
                        evt, data = rpc.events.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if evt is None:
                        break
                    if evt == "VOICE_CHANNEL_SELECT":
                        channel = None
                        if data.get("channel_id"):
                            channel = rpc.send("GET_CHANNEL",
                                               {"channel_id": data["channel_id"]},
                                               wait=True)
                        self._select_channel(rpc, channel)
                    else:
                        self.handle_event(evt, data)
                if not self._stop.is_set():
                    log.warning("Discord connection lost")
            except (RpcError, OSError, ValueError, urllib.error.URLError) as e:
                if not self._stop.is_set():
                    log.warning("Discord: %s; retrying in %.0f s", e,
                                RECONNECT_INTERVAL_S)
            finally:
                with self._lock:
                    if self._rpc is rpc:
                        self._rpc = None
                rpc.close()
            self._set_status("offline")
            self._stop.wait(RECONNECT_INTERVAL_S)

    def _select_channel(self, rpc, channel):
        """Move the voice-state subscriptions to `channel` (None: left)."""
        old = self._channel_id
        new = (channel or {}).get("id")
        if old and old != new:
            for evt in VOICE_EVENTS:
                rpc.send("UNSUBSCRIBE", {"channel_id": old}, evt=evt)
        if new and new != old:
            for evt in VOICE_EVENTS:
                rpc.send("SUBSCRIBE", {"channel_id": new}, evt=evt, wait=True)
        self._load_channel(channel)

    # ----- OAuth -----------------------------------------------------------

    def _authenticate(self, rpc):
        token = self._load_token()
        if token and token.get("expires_at", 0) - time.time() < TOKEN_REFRESH_MARGIN_S:
            token = self._refresh(token)
        if token:
            try:
                data = rpc.send("AUTHENTICATE", {"access_token": token["access_token"]},
                                wait=True)
                self._authenticated(data)
                return
            except RpcError as e:
                log.warning("cached Discord token rejected (%s); authorizing again", e)
        if not self.client_secret:
            raise RpcError("no client_secret configured; cannot authorize")
        self._set_status("authorize?")
        log.info("asking Discord to authorize; confirm the dialog in the client")
        code = rpc.send("AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES},
                        wait=True, timeout=AUTHORIZE_TIMEOUT_S)["code"]
        token = self._token_request({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": self.redirect_uri})
        data = rpc.send("AUTHENTICATE", {"access_token": token["access_token"]}, wait=True)
        self._authenticated(data)

    def _authenticated(self, data):
        user = (data or {}).get("user") or {}
        self._self_id = str(user.get("id")) if user.get("id") else None
        log.info("Discord authenticated as %s", user.get("username", "?"))

    def _refresh(self, token):
        if not token.get("refresh_token") or not self.client_secret:
            return token
        try:
            return self._token_request({"grant_type": "refresh_token",
                                        "refresh_token": token["refresh_token"]})
        except (urllib.error.URLError, ValueError, KeyError) as e:
            log.warning("Discord token refresh failed: %s", e)
            return token

    def _token_request(self, fields):
        fields = dict(fields, client_id=self.client_id, client_secret=self.client_secret)
        req = urllib.request.Request(
            TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as res:
            token = json.loads(res.read())
        token["expires_at"] = time.time() + float(token.get("expires_in", 0))
        self._save_token(token)
        return token

    def _load_token(self):
        try:
            return json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return None

    def _save_token(self, token):
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(token, f)
        except OSError as e:
            log.warning("cannot cache Discord token at %s: %s", self.token_file, e)
