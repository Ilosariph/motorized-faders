"""
Voicemeeter extension (Windows).

Binds one fader to one Voicemeeter input strip or output bus:
  - fader move (human)          -> `Strip[n].Gain` / `Bus[n].Gain` in dB
  - gain changed in Voicemeeter -> `host.set_fader(idx, NN)` to move the motor
  - fader button (mute)         -> `Strip[n].Mute` / `Bus[n].Mute`
  - mute changed in Voicemeeter -> `host.set_mute(idx, muted)`
  - screens                     -> strip/bus label on line 1, gain on line 2
  - haptic creases              -> every 12 dB, stronger at 0 dB on a fader
                                   that goes past it (where boost begins)

Works with Voicemeeter, Banana and Potato. Talks to it through the Remote API
DLL that ships with Voicemeeter (`VoicemeeterRemote64.dll`), via ctypes — no
Python deps. Voicemeeter may start after the host, quit and come back; the
faders resync whenever it reappears.

Config entry shape (faders/config.json):
    "voicemeeter": [
        {"fader": 0, "strip": 0, "label": "Mic"},
        {"fader": 1, "strip": 5, "label": "Music"},
        {"fader": 3, "bus": 0, "min": -40, "max": 0}
    ]

`strip` / `bus` are Voicemeeter's 0-based indices, left to right as in its
window: on Banana strips 0-2 are the hardware inputs, 3 is VAIO, 4 is AUX;
buses 0-2 are A1-A3, 3-4 are B1-B2.

`min`/`max` are the gain bounds in dB (default -60 / +12, Voicemeeter's own
slider range). Fader 0% maps to `min`, fader 100% maps to `max`; set
`"max": 0` so a fader can't push a strip into boost.

`label` is optional display text; it defaults to the name set in Voicemeeter
(right-click a strip label), else a short default such as "In 1", "VAIO" or
"A1".

`creases` (default true) turns the detents off when false.

`dll` (optional, first instance wins) is the path to the Remote DLL when it
is not found via the registry or the default install folder.
"""

import ctypes
import logging
import os
import struct
import sys
import threading
import time

from ..core import Extension

# Layer name when the config does not group plugins into layers itself.
LAYER_NAME = "Voicemeeter"

GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 12.0

# Ignore fader moves that change gain by less than this.
WRITE_EPSILON_DB = 0.1

# How often the poll thread asks Voicemeeter whether anything changed.
POLL_INTERVAL_S = 0.05

# Voicemeeter applies a set asynchronously and then flags itself dirty, so a
# read shortly after our own write can still return the previous value.
# Gain reads within this window after a write are not taken as remote moves.
SELF_ECHO_WINDOW_S = 0.3

# Creases, in dB: (power %, pulse ms) at full strength.
CREASE_STEP_DB = 12
CREASE_NORMAL = (55.0, 10)   # every 12 dB
CREASE_BIG = (70.0, 18)      # 0 dB, only when the fader can go past it

log = logging.getLogger("faders.voicemeeter")

# VBVMR_GetVoicemeeterType -> short default names, left to right.
_KINDS = {
    1: ("Voicemeeter", ["In 1", "In 2", "VAIO"], ["A", "B"]),
    2: ("Banana", ["In 1", "In 2", "In 3", "VAIO", "AUX"],
        ["A1", "A2", "A3", "B1", "B2"]),
    3: ("Potato", ["In 1", "In 2", "In 3", "In 4", "In 5", "VAIO", "AUX",
                   "VAIO3"],
        ["A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3"]),
}

_UNINSTALL_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                  r"\VB:Voicemeeter {17359A74-1236-5467}")
_DEFAULT_DIR = r"C:\Program Files (x86)\VB\Voicemeeter"


def register(host, cfg):
    if ("strip" in cfg) == ("bus" in cfg):
        raise ValueError("voicemeeter: give exactly one of 'strip' or 'bus'")
    kind = "Strip" if "strip" in cfg else "Bus"
    ext = VoicemeeterExtension(
        host=host,
        fader_idx=int(cfg["fader"]),
        kind=kind,
        index=int(cfg["strip" if kind == "Strip" else "bus"]),
        gain_min=float(cfg.get("min", GAIN_MIN_DB)),
        gain_max=float(cfg.get("max", GAIN_MAX_DB)),
        label=cfg.get("label"),
        creases=bool(cfg.get("creases", True)),
        client=_shared_client(cfg.get("dll")),
    )
    host.register(ext)


# ---------------------------------------------------------------------------
# Remote API
# ---------------------------------------------------------------------------

def _find_dll():
    name = ("VoicemeeterRemote64.dll" if struct.calcsize("P") == 8
            else "VoicemeeterRemote.dll")
    dirs = []
    try:
        import winreg
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_KEY,
                                    0, winreg.KEY_READ | view) as key:
                    uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
                dirs.append(os.path.dirname(uninstall.strip('"')))
            except OSError:
                pass
    except ImportError:
        pass
    dirs.append(_DEFAULT_DIR)
    for d in dirs:
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return None


class Remote:
    """
    The Remote API DLL, one call at a time. Every VBVMR_* function returns 0
    on success and a negative code on failure; -2 is "no server", i.e.
    Voicemeeter is not running.
    """

    def __init__(self, dll_path=None):
        path = dll_path or _find_dll()
        if path is None:
            raise OSError("VoicemeeterRemote DLL not found; is Voicemeeter "
                          "installed? Set 'dll' in the config otherwise")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("the Voicemeeter Remote API is Windows-only")
        self._dll = loader(path)
        d = self._dll
        d.VBVMR_Login.restype = ctypes.c_long
        d.VBVMR_Logout.restype = ctypes.c_long
        d.VBVMR_IsParametersDirty.restype = ctypes.c_long
        d.VBVMR_GetVoicemeeterType.argtypes = [ctypes.POINTER(ctypes.c_long)]
        d.VBVMR_GetVoicemeeterType.restype = ctypes.c_long
        d.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p,
                                              ctypes.POINTER(ctypes.c_float)]
        d.VBVMR_GetParameterFloat.restype = ctypes.c_long
        d.VBVMR_GetParameterStringW.argtypes = [ctypes.c_char_p,
                                                ctypes.c_wchar_p]
        d.VBVMR_GetParameterStringW.restype = ctypes.c_long
        d.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
        d.VBVMR_SetParameterFloat.restype = ctypes.c_long
        log.info("using %s", path)

    def login(self):
        # 0: logged in, 1: logged in but Voicemeeter is not running yet.
        return self._dll.VBVMR_Login()

    def logout(self):
        self._dll.VBVMR_Logout()

    def dirty(self):
        """1 if parameters changed since the last call, 0 if not, <0 if
        Voicemeeter is not reachable."""
        return self._dll.VBVMR_IsParametersDirty()

    def kind(self):
        value = ctypes.c_long()
        if self._dll.VBVMR_GetVoicemeeterType(ctypes.byref(value)) != 0:
            return None
        return value.value

    def get_float(self, name):
        value = ctypes.c_float()
        if self._dll.VBVMR_GetParameterFloat(name.encode(),
                                             ctypes.byref(value)) != 0:
            return None
        return value.value

    def get_string(self, name):
        buf = ctypes.create_unicode_buffer(512)
        if self._dll.VBVMR_GetParameterStringW(name.encode(), buf) != 0:
            return None
        return buf.value

    def set_float(self, name, value):
        return self._dll.VBVMR_SetParameterFloat(name.encode(), float(value))


class Client:
    """
    One Remote API session shared by every fader bound to Voicemeeter. A poll
    thread watches the dirty flag and tells the extensions when Voicemeeter
    appears (`vm_ready`), changes something (`vm_changed`) or goes away
    (`vm_lost`). All DLL calls go through `call`, under one lock.
    """

    def __init__(self, remote, start=True):
        self._remote = remote
        self._lock = threading.Lock()
        self._listeners = []
        self.connected = False
        self.kind = None
        self._stop = threading.Event()
        self._thread = None
        if start:
            code = remote.login()
            if code == 1:
                log.info("Voicemeeter is not running; waiting for it")
            elif code != 0:
                log.error("VBVMR_Login failed (%d)", code)
            self._thread = threading.Thread(target=self._poll_loop,
                                            daemon=True)
            self._thread.start()

    def add(self, listener):
        self._listeners.append(listener)

    def remove(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)
        if not self._listeners:
            self.stop()

    def call(self, method, *args):
        with self._lock:
            return getattr(self._remote, method)(*args)

    def poll_once(self):
        state = self.call("dirty")
        if state < 0:
            if self.connected:
                self.connected = False
                log.warning("Voicemeeter went away")
                for listener in list(self._listeners):
                    listener.vm_lost()
            return
        if not self.connected:
            self.connected = True
            self.kind = self.call("kind")
            log.info("connected to %s",
                     _KINDS.get(self.kind, ("Voicemeeter (type %s)" % self.kind,))[0])
            for listener in list(self._listeners):
                listener.vm_ready()
            return
        if state == 1:
            for listener in list(self._listeners):
                listener.vm_changed()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Voicemeeter poll failed")
            self._stop.wait(POLL_INTERVAL_S)

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            try:
                self.call("logout")
            except Exception:
                pass


_client = None


def _shared_client(dll_path=None):
    global _client
    if _client is None or _client._stop.is_set():
        try:
            remote = Remote(dll_path)
        except OSError as e:
            log.error("%s", e)
            remote = None
        _client = Client(remote) if remote is not None else _NoClient()
    return _client


class _NoClient:
    """Stand-in when the DLL is unavailable: faders show "no VM" and the
    rest of the host keeps working."""
    connected = False
    kind = None

    def add(self, listener):
        pass

    def remove(self, listener):
        pass

    def call(self, method, *args):
        return None


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

class VoicemeeterExtension(Extension):
    def __init__(self, host, fader_idx, kind, index, gain_min=GAIN_MIN_DB,
                 gain_max=GAIN_MAX_DB, label=None, creases=True, client=None):
        self.host = host
        self.fader_idx = fader_idx
        self.kind = kind
        self.index = index
        self.param = f"{kind}[{index}]"
        self.gain_min = max(GAIN_MIN_DB, gain_min)
        self.gain_max = min(GAIN_MAX_DB, gain_max)
        self._label_cfg = label
        self.label = label or self.param
        self._client = client

        self._lock = threading.Lock()
        self._last_gain = None
        self._last_mute = None
        self._last_self_write_t = 0.0
        self._tag = f"fader {fader_idx + 1} [{self.param}]"

        if creases:
            host.set_creases(fader_idx, self.crease_layout())
        self._push_display(None)
        client.add(self)
        if client.connected:
            self.vm_ready()

    # ----- mapping ---------------------------------------------------------

    def _fader_to_gain(self, fader_pct):
        return self.gain_min + (fader_pct / 100.0) * (self.gain_max - self.gain_min)

    def _gain_to_fader(self, gain):
        span = self.gain_max - self.gain_min
        if span <= 0:
            return 0.0
        return max(0.0, min(100.0, (gain - self.gain_min) / span * 100.0))

    def crease_layout(self):
        """(fader %, power, ms) at every 12 dB strictly inside the range."""
        layout = []
        db = (int(self.gain_min) // CREASE_STEP_DB + 1) * CREASE_STEP_DB
        while db < self.gain_max:
            power, ms = CREASE_BIG if db == 0 else CREASE_NORMAL
            layout.append((self._gain_to_fader(db), power, ms))
            db += CREASE_STEP_DB
        return layout

    # ----- display ---------------------------------------------------------

    def _default_label(self):
        names = _KINDS.get(self._client.kind)
        if names is not None:
            table = names[1] if self.kind == "Strip" else names[2]
            if self.index < len(table):
                return table[self.index]
        return self.param

    def _refresh_label(self):
        if self._label_cfg:
            return
        name = self._client.call("get_string", f"{self.param}.Label")
        self.label = name.strip() if name and name.strip() else self._default_label()

    def _push_display(self, gain):
        if not self._client.connected:
            line2 = "no VM"
        elif gain is None:
            line2 = "--"
        elif abs(gain) < 0.05:
            line2 = "0.0 dB"
        else:
            line2 = f"{gain:+.1f} dB"
        self.host.set_display(self.fader_idx, self.label, line2)

    # ----- Voicemeeter -> fader --------------------------------------------

    def _read(self):
        gain = self._client.call("get_float", f"{self.param}.Gain")
        mute = self._client.call("get_float", f"{self.param}.Mute")
        return gain, (None if mute is None else mute >= 0.5)

    def vm_ready(self):
        """Voicemeeter appeared (or the Pico reconnected): match it fully."""
        self._refresh_label()
        gain, muted = self._read()
        log.info("%s: %s, gain %s, %s", self._tag, self.label,
                 "unknown" if gain is None else f"{gain:.1f} dB",
                 "muted" if muted else "unmuted")
        with self._lock:
            self._last_gain = gain
            self._last_mute = muted
        level = None if gain is None else self._gain_to_fader(gain)
        if muted is not None and muted != self.host.is_muted(self.fader_idx):
            # Park or return with the strip's gain as the restore level.
            self.host.set_mute(self.fader_idx, muted, level)
        elif level is not None and not muted:
            self.host.set_fader(self.fader_idx, level)
        self._push_display(gain)

    def vm_lost(self):
        self._push_display(None)

    def vm_changed(self):
        self._refresh_label()
        gain, muted = self._read()
        with self._lock:
            echo = time.monotonic() - self._last_self_write_t < SELF_ECHO_WINDOW_S
            mute_changed = muted is not None and muted != self._last_mute
            if mute_changed:
                self._last_mute = muted
            gain_changed = (gain is not None and not echo and (
                self._last_gain is None
                or abs(gain - self._last_gain) >= WRITE_EPSILON_DB))
            if gain_changed:
                self._last_gain = gain
            shown = gain if not echo else self._last_gain
        if mute_changed:
            log.info("%s: %s in Voicemeeter", self._tag,
                     "muted" if muted else "unmuted")
            self.host.set_mute(self.fader_idx, muted)
        self._push_display(shown)
        # A muted fader is parked at the bottom by the Pico, which remembers
        # where to return to; driving it now would lose that.
        if gain_changed and not self._last_mute:
            log.info("%s: gain %.1f dB set in Voicemeeter", self._tag, gain)
            self.host.set_fader(self.fader_idx, self._gain_to_fader(gain))

    # ----- fader -> Voicemeeter --------------------------------------------

    def on_connect(self):
        if self._client.connected:
            self.vm_ready()

    def on_position(self, fader_idx, value):
        if fader_idx != self.fader_idx or not self._client.connected:
            return
        target = self._fader_to_gain(value)
        with self._lock:
            # Dragging a muted fader only picks where unmute returns to.
            if self._last_mute:
                return
            if (self._last_gain is not None
                    and abs(target - self._last_gain) < WRITE_EPSILON_DB):
                return
            self._last_gain = target
            self._last_self_write_t = time.monotonic()
        log.debug("%s: fader %.1f%% -> %.1f dB", self._tag, value, target)
        if self._client.call("set_float", f"{self.param}.Gain", target):
            log.warning("%s: setting gain failed", self._tag)
        self._push_display(target)

    def on_mute(self, fader_idx, muted):
        if fader_idx != self.fader_idx or not self._client.connected:
            return
        with self._lock:
            if self._last_mute == muted:
                return
            self._last_mute = muted
            self._last_self_write_t = time.monotonic()
        log.info("%s: %s", self._tag, "muted" if muted else "unmuted")
        self._client.call("set_float", f"{self.param}.Mute",
                          1.0 if muted else 0.0)

    def stop(self):
        self._client.remove(self)
