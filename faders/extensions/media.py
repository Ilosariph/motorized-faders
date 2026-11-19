"""
Media player extension (Linux and Windows).

Puts whichever media player is in use (Spotify, Plezy, a browser ...) on the
faders. Default layout, one role per fader:
    fader 1   volume of the player's audio; the button mutes it
    fader 2   button: previous track
    fader 3   playback speed; the button plays / pauses, the screen shows
              the title, status and speed
    fader 4   button: next track

  - fader move (human)      -> that application's own stream volume
  - volume changed in app   -> fader follows
  - volume-fader button     -> mutes the stream; muted elsewhere parks the fader
  - haptic creases          -> every 25% on the volume fader; 1x (firm),
                               every 0.5x above on the speed fader
  - speed fader (human)     -> the player's playback rate, 0.25x - 3x by default, in
                               0.05x steps; the lower quarter of the travel
                               is 0.25x - 1x, the rest 1x - 3x
  - speed changed in app    -> fader follows

Faders with only a button role park at the bottom, and so does the speed
fader for a player that reports no rate. A player that reports one but
refuses to have it set (Firefox) shows "fixed": the fader follows the
player and springs back when moved.

Browsers: the extension in `browser/` connects to a WebSocket on
127.0.0.1:47391 (`faders/browser_bridge.py`) and sets the speed from inside
the page. While it is connected and the page has media, a browser player's
speed goes through it instead of MPRIS / the media session. G1 / G2 combinations keep
their usual meaning, so layer switching works as on any layer.

Which player: the one playing. It stays selected while it plays; when it
pauses it stays selected until another one starts.

Several playing at once: one page each, in the order they started, stepped
with G2 + F1 / F4 held and shown as "1/2" on the last screen (tapped: A/B
swap, as on any layer). The selected player keeps its page when paused, so
play/pause can resume it; a player neither playing nor selected has no page.

Volume is per application stream rather than the player's own volume setting,
which many players ignore or do not expose (Firefox, for one). That is also
the one model both systems share.

Backends, chosen by platform, same interface:
  Linux     MPRIS through `playerctl`, speed through the MPRIS `Rate`
            property (`busctl`), streams through `pactl`. A stream
            belongs to a player when its process id matches the owner of the
            player's D-Bus name (`busctl`), or else when the application name
            contains the player's name.
  Windows   Media sessions through WinRT (GlobalSystemMediaTransportControls),
            speed through the session's playback rate, streams through
            WASAPI audio sessions on the default output.
            Needs: pip install "winrt-Windows.Media.Control[all]" pycaw

Config entry shape (faders/config.json), every key optional:
    "media": {
        "layout": ["volume", "previous", "play_pause", "next"],
        "players": ["spotify", "plezy"],
        "ignore": ["firefox"],
        "creases": true,
        "speed": true,
        "speed_min": 0.25,
        "speed_max": 3.0,
        "browser": true,
        "browser_port": 47391
    }

`layout` has one entry per fader: "volume", "previous", "play_pause", "next"
or null for a fader left to another extension on the same layer. At most one
"volume".
`players`: only these, matched as case-insensitive substrings of the player's
name; list order breaks ties when nothing is playing.
`ignore`: never these.
`speed`: false keeps the play / pause fader a plain button.
`speed_min` / `speed_max`: the speed fader's range, 0.25 / 3 by default (no upper limit).
1x sits a quarter up when the range spans it. Firefox mutes above 4x.
`browser`: false runs no WebSocket for the browser extension.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..browser_bridge import DEFAULT_PORT as BROWSER_PORT, BrowserBridge
from ..core import Extension, with_page

# Layer name when the config does not group plugins into layers itself.
LAYER_NAME = "Media"

VOLUME = "volume"
ACTIONS = ("previous", "play_pause", "next")
DEFAULT_LAYOUT = (VOLUME, "previous", "play_pause", "next")

PLAYING = "Playing"
PAUSED = "Paused"
STOPPED = "Stopped"

# How often to look at the players, and how soon after a button press, when
# the player has had time to act on it.
POLL_INTERVAL_S = 1.0
POLL_AFTER_COMMAND_S = 0.2

# Ignore volume differences below this: pactl reports whole percents.
VOLUME_EPSILON = 1.5
# Fader moves smaller than this are noise, not a volume change.
WRITE_MIN_STEP = 0.5
# Bounds volume writes during a drag; the last value always goes out.
WRITE_MIN_INTERVAL_S = 0.03
# A poll can overlap a drag and read the volume from before it. Readings this
# soon after the hand are not taken as changes made in the application.
HAND_WINDOW_S = 1.0

# (power %, pulse ms), as in pulse_sink.
CREASE_STEP = 25
CREASE_NORMAL = (55.0, 10)
CREASE_FIRM = (80.0, 15)

# Speed fader: the minimum at the bottom, 1x at SPEED_UNITY_POS, the maximum
# at the top, linear in between. Most listening happens between 1x and 2x, so
# that stretch gets most of the travel. Defaults; `speed_min` / `speed_max`
# in the config override them. A range that does not straddle 1x is linear
# over the whole travel.
SPEED_MIN = 0.25
SPEED_MAX = 3.0
SPEED_UNITY_POS = 25.0
SPEED_STEP = 0.05
# Light detents every SPEED_CREASE_STEP above 1x (firm at 1x itself).
SPEED_CREASE_STEP = 0.5
# Rate differences below this are rounding, not a change.
SPEED_EPSILON = 0.01

COMMAND_TIMEOUT_S = 2.0

log = logging.getLogger("faders.media")


def _speed_points(lo, hi):
    """(position, rate) corners of the fader's speed curve."""
    if lo < 1.0 < hi:
        return ((0.0, lo), (SPEED_UNITY_POS, 1.0), (100.0, hi))
    return ((0.0, lo), (100.0, hi))


def speed_to_pos(rate, lo=SPEED_MIN, hi=SPEED_MAX):
    """Fader position (0-100) for a playback rate."""
    rate = min(hi, max(lo, rate))
    points = _speed_points(lo, hi)
    for (p0, r0), (p1, r1) in zip(points, points[1:]):
        if rate <= r1:
            return p0 + (rate - r0) / (r1 - r0) * (p1 - p0)
    return 100.0


def pos_to_speed(pos, lo=SPEED_MIN, hi=SPEED_MAX):
    """Playback rate for a fader position, in SPEED_STEP steps; the ends are
    the range's own ends, on the grid or not."""
    if pos <= 0.0:
        return lo
    if pos >= 100.0:
        return hi
    points = _speed_points(lo, hi)
    rate = hi
    for (p0, r0), (p1, r1) in zip(points, points[1:]):
        if pos <= p1:
            rate = r0 + (pos - p0) / (p1 - p0) * (r1 - r0)
            break
    rate = round(round(rate / SPEED_STEP) * SPEED_STEP, 2)
    return min(hi, max(lo, rate))


def speed_creases(lo=SPEED_MIN, hi=SPEED_MAX):
    """(rate, firm) detents: 1x firm, then every SPEED_CREASE_STEP above it,
    both ends left out."""
    found = [(1.0, True)] if lo < 1.0 < hi else []
    rate = 1.0 + SPEED_CREASE_STEP
    while rate < hi - 1e-9:
        if rate > lo + 1e-9:
            found.append((rate, False))
        rate += SPEED_CREASE_STEP
    return found


def speed_text(rate):
    """1.0 -> "1x", 1.25 -> "1.25x"."""
    return f"{rate:.2f}".rstrip("0").rstrip(".") + "x"


class BackendError(Exception):
    """The backend cannot run; the message is short enough for a screen."""


@dataclass
class Player:
    id: str
    name: str
    status: str = STOPPED
    title: str = ""
    artist: str = ""


# ---------------------------------------------------------------------------
# Linux: MPRIS + PipeWire / PulseAudio streams
# ---------------------------------------------------------------------------

PLAYERCTL = "playerctl"
PACTL = "pactl"
BUSCTL = "busctl"

# Unit separator: cannot appear in a title, unlike every printable delimiter.
_SEP = "\x1f"
_PLAYER_FORMAT = _SEP.join(
    ("{{playerInstance}}", "{{status}}", "{{artist}}", "{{title}}"))
_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
_PLAYERCTL_COMMANDS = {"play_pause": "play-pause", "next": "next",
                       "previous": "previous"}


def linux_name(instance):
    """"com_edde746_plezy" -> "Plezy", "firefox.instance_1_60" -> "Firefox"."""
    base = instance.split(".instance")[0].rsplit("_", 1)[-1]
    return base[:1].upper() + base[1:]


class LinuxBackend:
    def __init__(self):
        # MPRIS instance -> pid of its D-Bus owner, None when unknown.
        self._pids = {}
        # Player id -> sink-input indices, from the last audio() read.
        self._streams = {}

    def open(self):
        for tool in (PLAYERCTL, PACTL):
            if shutil.which(tool) is None:
                raise BackendError(f"no {tool}")

    @staticmethod
    def _output(*argv):
        """A command's stdout, None if it failed. Never raises."""
        try:
            done = subprocess.run(argv, capture_output=True, text=True,
                                  errors="replace", timeout=COMMAND_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("%s: %s", argv[0], e)
            return None
        return done.stdout if done.returncode == 0 else None

    def players(self):
        out = self._output(PLAYERCTL, "-a", "metadata", "--format", _PLAYER_FORMAT)
        found = []
        for line in (out or "").splitlines():
            parts = line.split(_SEP)
            # playerctld only mirrors the real players.
            if len(parts) == 4 and parts[0] != "playerctld":
                found.append(Player(parts[0], linux_name(parts[0]), parts[1],
                                    parts[3], parts[2]))
        self._pids = {p.id: self._pids.get(p.id) for p in found}
        return found

    def command(self, player_id, action):
        self._output(PLAYERCTL, "-p", player_id, _PLAYERCTL_COMMANDS[action])

    def _rate_argv(self, player_id, verb):
        return (BUSCTL, "--user", verb, _MPRIS_PREFIX + player_id, _MPRIS_PATH,
                _MPRIS_PLAYER, "Rate")

    def rate(self, player_id):
        """Playback rate, None when the player has none."""
        out = self._output(*self._rate_argv(player_id, "get-property"))
        # Reply looks like: d 1.5
        parts = (out or "").split()
        if len(parts) != 2 or parts[0] != "d":
            return None
        try:
            return float(parts[1])
        except ValueError:
            return None

    def set_rate(self, player_id, rate):
        """False when the player refuses (Firefox: "Rate setting is not
        supported")."""
        return self._output(*self._rate_argv(player_id, "set-property"),
                            "d", f"{rate:.2f}") is not None

    def _pid(self, player_id):
        if self._pids.get(player_id) is None:
            out = self._output(
                BUSCTL, "--user", "call", "org.freedesktop.DBus", "/",
                "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s",
                _MPRIS_PREFIX + player_id)
            # Reply looks like: u 55601
            if out and out.split()[-1].isdigit():
                self._pids[player_id] = out.split()[-1]
        return self._pids.get(player_id)

    def _pactl_list(self, kind):
        out = self._output(PACTL, "-f", "json", "list", kind)
        try:
            return json.loads(out) if out else []
        except ValueError:
            return []

    @staticmethod
    def _belongs(props, pid, name):
        """Does a stream with these properties belong to the player?"""
        if pid is not None and props.get("application.process.id") == pid:
            return True
        names = (props.get("application.name"),
                 props.get("application.process.binary"))
        return any(name.lower() in n.lower() for n in names if n)

    def audio(self, player_id):
        """(volume %, muted) of the player's stream; (None, False) if none."""
        clients = {str(c.get("index")): c.get("properties") or {}
                   for c in self._pactl_list("clients")}
        pid = self._pid(player_id)
        name = linux_name(player_id)
        mine = []
        for stream in self._pactl_list("sink-inputs"):
            # Some applications only identify themselves on the client.
            props = dict(clients.get(str(stream.get("client")), {}),
                         **(stream.get("properties") or {}))
            if self._belongs(props, pid, name):
                mine.append(stream)
        self._streams[player_id] = [s["index"] for s in mine]
        if not mine:
            return None, False
        # A corked stream can hold a stale level; the running one is the truth.
        mine.sort(key=lambda s: bool(s.get("corked")))
        channels = mine[0].get("volume") or {}
        percent = next(iter(channels.values()), {}).get("value_percent", "0%")
        return float(percent.rstrip("%")), all(s.get("mute") for s in mine)

    def set_volume(self, player_id, percent):
        for index in self._streams.get(player_id, ()):
            self._output(PACTL, "set-sink-input-volume", str(index), f"{percent:.1f}%")

    def set_mute(self, player_id, muted):
        for index in self._streams.get(player_id, ()):
            self._output(PACTL, "set-sink-input-mute", str(index), "1" if muted else "0")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Windows: WinRT media sessions + WASAPI audio sessions
# ---------------------------------------------------------------------------

# Values of GlobalSystemMediaTransportControlsSessionPlaybackStatus.
_WIN_STATUS = {4: PLAYING, 5: PAUSED}
# Firefox registers under a hash of its install path; this is the default one.
_WIN_ALIASES = {"308046b0af4a39cb": "Firefox"}


def windows_name(app_id):
    """Readable name from an AppUserModelID: Spotify.exe, Vendor.App!Spotify."""
    alias = _WIN_ALIASES.get(app_id.lower())
    if alias:
        return alias
    tail = app_id.split("!")[-1].replace("/", "\\").rsplit("\\", 1)[-1]
    if tail.lower().endswith(".exe"):
        tail = tail[:-4]
    return tail[:1].upper() + tail[1:]


async def _result(operation):
    return await operation


class WindowsBackend:
    """
    Every call must come from one thread, the one that called `open`: COM is
    initialized per thread, which is why the imports are here as well.
    """

    def __init__(self):
        self._manager = None
        self._audio = None
        self._sessions = {}

    def open(self):
        # comtypes initializes the importing thread, as STA unless told; WinRT
        # needs it to be MTA.
        sys.coinit_flags = 0
        try:
            from pycaw.pycaw import AudioUtilities
            # Namespaces of the values the calls below return.
            import winrt.windows.foundation  # noqa: F401
            import winrt.windows.foundation.collections  # noqa: F401
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager)
        except ImportError as e:
            log.error('%s; pip install "winrt-Windows.Media.Control[all]" pycaw', e)
            raise BackendError("deps missing") from e
        self._audio = AudioUtilities
        self._manager = asyncio.run(_result(Manager.request_async()))

    def players(self):
        found, sessions = [], {}
        for session in self._manager.get_sessions():
            app_id = session.source_app_user_model_id
            sessions[app_id] = session
            info = session.get_playback_info()
            status = _WIN_STATUS.get(int(info.playback_status), STOPPED)
            try:
                props = asyncio.run(_result(session.try_get_media_properties_async()))
            except OSError:
                # An application still starting up has no properties to give.
                props = None
            found.append(Player(app_id, windows_name(app_id), status,
                                props.title if props else "",
                                props.artist if props else ""))
        self._sessions = sessions
        return found

    def command(self, player_id, action):
        session = self._sessions.get(player_id)
        if session is None:
            return
        operation = {"play_pause": session.try_toggle_play_pause_async,
                     "next": session.try_skip_next_async,
                     "previous": session.try_skip_previous_async}[action]()
        asyncio.run(_result(operation))

    def rate(self, player_id):
        session = self._sessions.get(player_id)
        if session is None:
            return None
        rate = session.get_playback_info().playback_rate
        return None if rate is None else float(rate)

    def set_rate(self, player_id, rate):
        session = self._sessions.get(player_id)
        if session is None:
            return False
        controls = session.get_playback_info().controls
        if not controls.is_playback_rate_enabled:
            return False
        return bool(asyncio.run(_result(session.try_change_playback_rate_async(rate))))

    def _audio_sessions(self, player_id):
        """The player's WASAPI sessions, matched on the process name."""
        name = windows_name(player_id).lower()
        app_id = player_id.lower()
        mine = []
        for session in self._audio.GetAllSessions():
            try:
                exe = session.Process.name().rsplit(".", 1)[0].lower()
            except Exception:
                # System sounds have no process; others can exit at any moment.
                continue
            if exe == name or exe in app_id:
                mine.append(session.SimpleAudioVolume)
        return mine

    def audio(self, player_id):
        mine = self._audio_sessions(player_id)
        if not mine:
            return None, False
        return (mine[0].GetMasterVolume() * 100.0,
                all(s.GetMute() for s in mine))

    def set_volume(self, player_id, percent):
        for session in self._audio_sessions(player_id):
            session.SetMasterVolume(percent / 100.0, None)

    def set_mute(self, player_id, muted):
        for session in self._audio_sessions(player_id):
            session.SetMute(int(muted), None)

    def close(self):
        pass


def make_backend():
    return WindowsBackend() if sys.platform == "win32" else LinuxBackend()


# Substrings of browser player names; their speed goes through the extension.
BROWSERS = ("firefox", "librewolf", "floorp", "waterfox", "zen", "chrom",
            "brave", "vivaldi", "edge", "opera")


def is_browser(player_id):
    name = (windows_name if sys.platform == "win32" else linux_name)(player_id)
    return any(b in name.lower() or b in player_id.lower() for b in BROWSERS)


class BridgedBackend:
    """A backend whose browser players get their speed from the extension,
    when it is connected and the page has media. Everything else passes
    through."""

    def __init__(self, backend, bridge):
        self._backend = backend
        self._bridge = bridge

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def rate(self, player_id):
        if is_browser(player_id):
            rate = self._bridge.rate()
            if rate is not None:
                return rate
        return self._backend.rate(player_id)

    def set_rate(self, player_id, rate):
        if is_browser(player_id) and self._bridge.set_rate(rate):
            return True
        return self._backend.set_rate(player_id, rate)


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

def register(host, cfg):
    backend = make_backend()
    bridge = None
    if cfg.get("browser", True):
        bridge = BrowserBridge(port=int(cfg.get("browser_port", BROWSER_PORT)))
        backend = BridgedBackend(backend, bridge)
    ext = MediaExtension(
        host=host,
        backend=backend,
        bridge=bridge,
        layout=cfg.get("layout", DEFAULT_LAYOUT),
        players=cfg.get("players", ()),
        ignore=cfg.get("ignore", ()),
        creases=bool(cfg.get("creases", True)),
        speed=bool(cfg.get("speed", True)),
        speed_min=float(cfg.get("speed_min", SPEED_MIN)),
        speed_max=float(cfg.get("speed_max", SPEED_MAX)),
    )
    host.register(ext)


class MediaExtension(Extension):
    PAGED = True

    def __init__(self, host, backend, layout=DEFAULT_LAYOUT, players=(), ignore=(),
                 creases=True, speed=True, speed_min=SPEED_MIN, speed_max=SPEED_MAX,
                 bridge=None, start=True):
        layout = list(layout)
        unknown = [r for r in layout if r is not None and r != VOLUME and r not in ACTIONS]
        if unknown:
            raise ValueError(f"media: unknown layout role {unknown[0]!r}")
        if layout.count(VOLUME) > 1:
            raise ValueError("media: only one fader can be \"volume\"")
        if not 0 < speed_min < speed_max:
            raise ValueError(f"media: need 0 < speed_min < speed_max "
                             f"(got {speed_min}, {speed_max})")
        if len(layout) > host.num_faders:
            raise ValueError(f"media: layout has {len(layout)} entries for "
                             f"{host.num_faders} faders")
        self.host = host
        self._backend = backend
        self._layout = layout + [None] * (host.num_faders - len(layout))
        self._vol_idx = self._layout.index(VOLUME) if VOLUME in self._layout else None
        # The play / pause fader sets the speed; its button stays play / pause.
        self._speed_lo, self._speed_hi = speed_min, speed_max
        self._speed_idx = (self._layout.index("play_pause")
                           if speed and "play_pause" in self._layout else None)
        # The page indicator goes on the last screen this extension draws.
        self._page_idx = max((i for i, r in enumerate(self._layout) if r), default=None)
        self._wanted = [p.lower() for p in players]
        self._ignored = [p.lower() for p in ignore]

        self._lock = threading.RLock()
        self._player = None
        # Ids of the players with a page, in the order they got one, and the
        # page steps asked for since the last poll.
        self._pages = []
        self._page_delta = 0
        self._volume = None
        self._muted = False
        self._error = None
        self._hand_t = -1e9
        # Playback rate of the shown player, None when it has none; ids of
        # players that refused a rate; the speed fader's last hand move, and
        # whether it was moved on a player whose rate cannot be set.
        self._rate = None
        self._fixed_rate = set()
        self._speed_hand_t = -1e9
        self._speed_moved = False
        # What the hand asked for, carried out on the worker thread. Volume
        # and mute keep only the latest value.
        self._commands = deque()
        self._pending_volume = None
        self._pending_mute = None
        self._pending_rate = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        # Set when the browser extension reports: poll soon.
        self._poll_soon = False
        self._bridge = bridge
        if bridge is not None:
            bridge.on_change = self.rates_changed

        if creases and self._vol_idx is not None:
            host.set_creases(self._vol_idx, [
                (float(v), *CREASE_NORMAL)
                for v in range(CREASE_STEP, 100, CREASE_STEP)])
        if creases and self._speed_idx is not None:
            host.set_creases(self._speed_idx, [
                (self._to_pos(r), *(CREASE_FIRM if firm else CREASE_NORMAL))
                for r, firm in speed_creases(speed_min, speed_max)])
        self._park_button_faders()
        self._drive_speed()
        self._push_screens()
        if start:
            if bridge is not None:
                bridge.start()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # ----- which player ----------------------------------------------------

    def _rank(self, player):
        """Position in `players`, None when the config rules this one out."""
        name = player.name.lower()
        key = player.id.lower()
        if any(p in name or p in key for p in self._ignored):
            return None
        if not self._wanted:
            return 0
        for rank, wanted in enumerate(self._wanted):
            if wanted in name or wanted in key:
                return rank
        return None

    def _select(self, players, delta):
        """Update the pages, step them by `delta` and return the player to
        show, or None. Lock held."""
        ranked = [(self._rank(p), p) for p in players]
        allowed = [p for _, p in sorted(
            (r for r in ranked if r[0] is not None), key=lambda r: r[0])]
        by_id = {p.id: p for p in allowed}
        current = self._player.id if self._player else None
        if current not in by_id:
            current = None
        playing = [p.id for p in allowed if p.status == PLAYING]
        new = [i for i in playing if i not in self._pages]
        # Only a player that just started takes over from a paused one: one
        # already on a page must not, or pausing would jump away from the
        # player the next press is meant to resume.
        if new and current not in playing:
            current = new[0]
        pages = [i for i in self._pages if i in playing or i == current]
        pages += [i for i in new if i not in pages]
        if current is None:
            current = pages[0] if pages else (allowed[0].id if allowed else None)
        if current is not None and current not in pages:
            pages.append(current)
        if delta and len(pages) > 1:
            current = pages[(pages.index(current) + delta) % len(pages)]
        self._pages = pages
        return by_id.get(current)

    # ----- faders and screens ----------------------------------------------

    def _park_button_faders(self):
        for idx, role in enumerate(self._layout):
            if role in ACTIONS and idx != self._speed_idx:
                self.host.set_fader(idx, 0.0)

    def _to_pos(self, rate):
        return speed_to_pos(rate, self._speed_lo, self._speed_hi)

    def _drive_speed(self):
        """Speed fader to the player's rate, the bottom without one. Lock held."""
        if self._speed_idx is None:
            return
        self._speed_moved = False
        self.host.set_fader(self._speed_idx,
                            0.0 if self._rate is None else self._to_pos(self._rate))

    def _drive(self):
        """Volume fader to the player's level and mute. Lock held."""
        if self._vol_idx is None:
            return
        level = self._volume if self._volume is not None else 0.0
        if self._muted:
            self.host.set_mute(self._vol_idx, True, level)
        elif self.host.is_muted(self._vol_idx):
            self.host.set_mute(self._vol_idx, False, level)
        else:
            self.host.set_fader(self._vol_idx, level)

    def _screen(self, role):
        player = self._player
        if role == VOLUME:
            if self._error:
                return "Media", self._error
            if player is None:
                return "Media", "no player"
            shown = "--" if self._volume is None else f"{self._volume:.0f}%"
            return player.name, f"vol {shown}"
        if role == "previous":
            return "Previous", "|<"
        if role == "next":
            return "Next", ">|"
        if player is None:
            return "-", ""
        if self._speed_idx is None or self._rate is None:
            return player.title or player.name, player.status
        mark = {PLAYING: ">", PAUSED: "||"}.get(player.status, "[]")
        fixed = " fixed" if player.id in self._fixed_rate else ""
        return player.title or player.name, f"{mark} {speed_text(self._rate)}{fixed}"

    def _push_screens(self):
        with self._lock:
            for idx, role in enumerate(self._layout):
                if role is None:
                    continue
                line1, line2 = self._screen(role)
                # A hand move can land between a poll's two locked sections,
                # when the old player may have lost its page already.
                shown = self._player.id if self._player else None
                if idx == self._page_idx and len(self._pages) > 1 and shown in self._pages:
                    page = self._pages.index(shown) + 1
                    line2 = with_page(line2, f"{page}/{len(self._pages)}")
                self.host.set_display(idx, line1, line2)

    # ----- player -> faders ------------------------------------------------

    def poll(self):
        """Look at the players once. Worker thread."""
        players = self._backend.players()
        with self._lock:
            delta, self._page_delta = self._page_delta, 0
            player = self._select(players, delta)
        volume, muted, rate = None, False, None
        if player is not None and self._vol_idx is not None:
            volume, muted = self._backend.audio(player.id)
        if player is not None and self._speed_idx is not None:
            rate = self._backend.rate(player.id)
        with self._lock:
            changed = (player.id if player else None) != (
                self._player.id if self._player else None)
            self._player = player
            if changed:
                log.info("player: %s", player.name if player else "none")
                self._volume, self._muted = volume, muted
                self._drive()
                self._rate = rate
                self._drive_speed()
            else:
                if volume is not None:
                    self._follow(volume, muted)
                self._follow_rate(rate)
            self._push_screens()

    def _follow(self, volume, muted):
        """Apply changes made in the application. Lock held."""
        if time.monotonic() - self._hand_t < HAND_WINDOW_S:
            return
        moved = (self._volume is None
                 or abs(volume - self._volume) >= VOLUME_EPSILON)
        toggled = muted != self._muted
        if not moved and not toggled:
            return
        if moved:
            self._volume = volume
        self._muted = muted
        if toggled or not muted:
            self._drive()
        else:
            # Parked: only where unmute returns to changes.
            self.host.set_mute(self._vol_idx, True, self._volume)

    def _follow_rate(self, rate):
        """Apply a speed change made in the application. Lock held."""
        if self._speed_idx is None:
            return
        if time.monotonic() - self._speed_hand_t < HAND_WINDOW_S:
            return
        same = (rate is None) == (self._rate is None) and (
            rate is None or abs(rate - self._rate) < SPEED_EPSILON)
        # A drag on a player whose rate is fixed springs back once the hand
        # has let go.
        if same and not self._speed_moved:
            return
        self._rate = rate
        self._drive_speed()

    # ----- hand -> player --------------------------------------------------

    def owns_button(self, fader_idx):
        return (0 <= fader_idx < len(self._layout)
                and self._layout[fader_idx] in ACTIONS)

    def on_fader_button(self, fader_idx):
        action = self._layout[fader_idx]
        log.info("%s", action)
        with self._lock:
            self._commands.append(action)
        self._wake.set()

    def on_page(self, delta):
        # Carried out by the next poll, on the worker thread: showing another
        # player means asking the backend for its volume.
        with self._lock:
            self._page_delta += delta
        self._wake.set()

    def on_position(self, fader_idx, value):
        if fader_idx == self._speed_idx and fader_idx is not None:
            self._on_speed(value)
            return
        if fader_idx != self._vol_idx:
            return
        with self._lock:
            # Muted, the fader sits parked and a drag only picks where unmute
            # returns to. With no stream there is nothing to set.
            if self._muted or self._volume is None:
                return
            if abs(value - self._volume) < WRITE_MIN_STEP:
                return
            self._volume = value
            self._pending_volume = value
            self._hand_t = time.monotonic()
            self._push_screens()
        self._wake.set()

    def _on_speed(self, value):
        with self._lock:
            if self._player is None or self._rate is None:
                return
            self._speed_hand_t = time.monotonic()
            if self._player.id in self._fixed_rate:
                self._speed_moved = True
                return
            rate = pos_to_speed(value, self._speed_lo, self._speed_hi)
            if abs(rate - self._rate) < SPEED_EPSILON:
                return
            self._rate = rate
            self._pending_rate = rate
            self._push_screens()
        self._wake.set()

    def on_mute(self, fader_idx, muted):
        if fader_idx != self._vol_idx:
            return
        with self._lock:
            if self._player is None or muted == self._muted:
                return
            self._muted = muted
            self._pending_mute = muted
            self._hand_t = time.monotonic()
        self._wake.set()

    def apply(self):
        """Carry out what the hand asked for. Worker thread. True after a
        transport command, whose effect is worth looking at soon."""
        with self._lock:
            player = self._player
            commands, self._commands = self._commands, deque()
            volume, self._pending_volume = self._pending_volume, None
            mute, self._pending_mute = self._pending_mute, None
            rate, self._pending_rate = self._pending_rate, None
        if player is None:
            return False
        for action in commands:
            self._backend.command(player.id, action)
        if volume is not None:
            self._backend.set_volume(player.id, volume)
            self._stop.wait(WRITE_MIN_INTERVAL_S)
        if mute is not None:
            log.info("%s %s", player.name, "muted" if mute else "unmuted")
            self._backend.set_mute(player.id, mute)
        if rate is not None and not self._backend.set_rate(player.id, rate):
            log.info("%s: speed cannot be set", player.name)
            with self._lock:
                self._fixed_rate.add(player.id)
                # Springs back to the player's rate on a poll after the hand.
                self._speed_moved = True
                self._push_screens()
        return bool(commands)

    # ----- host hooks and worker -------------------------------------------

    def on_connect(self):
        """Rebooted Pico: drive every fader again."""
        self._park_button_faders()
        with self._lock:
            if self._player is not None:
                self._drive()
            self._drive_speed()

    def rates_changed(self):
        """The browser extension connected, left or reported: a browser's
        speed may be settable now, and may have changed."""
        with self._lock:
            self._fixed_rate.clear()
            self._poll_soon = True
        self._wake.set()

    def _run(self):
        try:
            self._backend.open()
        except BackendError as e:
            log.error("media backend unavailable: %s", e)
            with self._lock:
                self._error = str(e)
            self._push_screens()
            return
        except Exception:
            log.exception("media backend failed to start")
            with self._lock:
                self._error = "backend error"
            self._push_screens()
            return
        next_poll = last_poll = 0.0
        failing = False
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.apply():
                    next_poll = min(next_poll, time.monotonic() + POLL_AFTER_COMMAND_S)
                if self._page_delta:
                    next_poll = 0.0
                with self._lock:
                    soon, self._poll_soon = self._poll_soon, False
                if soon:
                    next_poll = min(next_poll, last_poll + POLL_AFTER_COMMAND_S)
                if time.monotonic() >= next_poll:
                    self.poll()
                    last_poll = time.monotonic()
                    next_poll = last_poll + POLL_INTERVAL_S
                failing = False
            except Exception:
                next_poll = time.monotonic() + POLL_INTERVAL_S
                if not failing:
                    log.exception("media backend call failed")
                failing = True
            self._wake.wait(max(0.0, next_poll - time.monotonic()))
        self._backend.close()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._bridge is not None:
            self._bridge.stop()

