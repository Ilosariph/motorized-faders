"""
Media player extension (Linux and Windows).

Puts whichever media player is in use (Spotify, Plezy, a browser ...) on the
faders. Default layout, one role per fader:
    fader 1   volume of the player's audio; the button mutes it
    fader 2   button: previous track
    fader 3   button: play / pause; the screen shows the title
    fader 4   button: next track

  - fader move (human)      -> that application's own stream volume
  - volume changed in app   -> fader follows
  - volume-fader button     -> mutes the stream; muted elsewhere parks the fader
  - haptic creases          -> every 25% on the volume fader

Faders with only a button role park at the bottom. G1 / G2 combinations keep
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
  Linux     MPRIS through `playerctl`, streams through `pactl`. A stream
            belongs to a player when its process id matches the owner of the
            player's D-Bus name (`busctl`), or else when the application name
            contains the player's name.
  Windows   Media sessions through WinRT (GlobalSystemMediaTransportControls),
            streams through WASAPI audio sessions on the default output.
            Needs: pip install "winrt-Windows.Media.Control[all]" pycaw

Config entry shape (faders/config.json), every key optional:
    "media": {
        "layout": ["volume", "previous", "play_pause", "next"],
        "players": ["spotify", "plezy"],
        "ignore": ["firefox"],
        "creases": true
    }

`layout` has one entry per fader: "volume", "previous", "play_pause", "next"
or null for a fader left to another extension on the same layer. At most one
"volume".
`players`: only these, matched as case-insensitive substrings of the player's
name; list order breaks ties when nothing is playing.
`ignore`: never these.
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

COMMAND_TIMEOUT_S = 2.0

log = logging.getLogger("faders.media")


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


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

def register(host, cfg):
    ext = MediaExtension(
        host=host,
        backend=make_backend(),
        layout=cfg.get("layout", DEFAULT_LAYOUT),
        players=cfg.get("players", ()),
        ignore=cfg.get("ignore", ()),
        creases=bool(cfg.get("creases", True)),
    )
    host.register(ext)


class MediaExtension(Extension):
    PAGED = True

    def __init__(self, host, backend, layout=DEFAULT_LAYOUT, players=(), ignore=(),
                 creases=True, start=True):
        layout = list(layout)
        unknown = [r for r in layout if r is not None and r != VOLUME and r not in ACTIONS]
        if unknown:
            raise ValueError(f"media: unknown layout role {unknown[0]!r}")
        if layout.count(VOLUME) > 1:
            raise ValueError("media: only one fader can be \"volume\"")
        if len(layout) > host.num_faders:
            raise ValueError(f"media: layout has {len(layout)} entries for "
                             f"{host.num_faders} faders")
        self.host = host
        self._backend = backend
        self._layout = layout + [None] * (host.num_faders - len(layout))
        self._vol_idx = self._layout.index(VOLUME) if VOLUME in self._layout else None
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
        # What the hand asked for, carried out on the worker thread. Volume
        # and mute keep only the latest value.
        self._commands = deque()
        self._pending_volume = None
        self._pending_mute = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

        if creases and self._vol_idx is not None:
            host.set_creases(self._vol_idx, [
                (float(v), *CREASE_NORMAL)
                for v in range(CREASE_STEP, 100, CREASE_STEP)])
        self._park_button_faders()
        self._push_screens()
        if start:
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
            if role in ACTIONS:
                self.host.set_fader(idx, 0.0)

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
        return player.title or player.name, player.status

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
        volume, muted = None, False
        if player is not None and self._vol_idx is not None:
            volume, muted = self._backend.audio(player.id)
        with self._lock:
            changed = (player.id if player else None) != (
                self._player.id if self._player else None)
            self._player = player
            if changed:
                log.info("player: %s", player.name if player else "none")
                self._volume, self._muted = volume, muted
                self._drive()
            elif volume is not None:
                self._follow(volume, muted)
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
        return bool(commands)

    # ----- host hooks and worker -------------------------------------------

    def on_connect(self):
        """Rebooted Pico: drive every fader again."""
        self._park_button_faders()
        with self._lock:
            if self._player is not None:
                self._drive()

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
        next_poll = 0.0
        failing = False
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.apply():
                    next_poll = min(next_poll, time.monotonic() + POLL_AFTER_COMMAND_S)
                if self._page_delta:
                    next_poll = 0.0
                if time.monotonic() >= next_poll:
                    self.poll()
                    next_poll = time.monotonic() + POLL_INTERVAL_S
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

