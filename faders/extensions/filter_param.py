"""
PipeWire filter-chain parameter extension.

Binds faders to controls of LV2 / LADSPA plugins running inside PipeWire
`filter-chain` modules (compressor threshold, ratio, makeup, gate threshold
...):
  - fader move (human)          -> `pw-cli set-param` on the filter node
  - changed elsewhere           -> fader follows (pw-dump --monitor)
  - fader button (mute)         -> bypass the plugin (its `enabled` control);
                                   every fader on that plugin parks while it
                                   is bypassed, and unmuting turns it back on
  - display                     -> label, value in real units ("-18.0 dB")
  - crease                      -> one detent at the original value, so the
                                   configured setting can be found by feel

PipeWire exposes every plugin control as a node Prop named
`<filter node name>:<port symbol>`, e.g. `fx-comp-apps:al` for the `al`
(threshold) port of the filter named `fx-comp-apps`. `param` is that full
name; the node carrying it is found by itself.

Values set this way live in PipeWire only, and a PipeWire restart (or a
reboot) reloads the filter with the values from its config. So the extension
keeps a state file (`$XDG_STATE_HOME/motorized-faders/filter_param.json`)
holding, per control:
    original  the value the filter came up with: the config's own value,
              kept as the backup
    value     the last value seen, re-applied when the filter is loaded anew
Changing a value in the PipeWire config wins over a saved one: when a fresh
filter comes up with a new original, the saved value is dropped. Put the
originals back with
    motorized-faders-fx reset            (all saved controls)
    motorized-faders-fx reset KEY ...    (some)
and list them with `motorized-faders-fx status`. `"persist": false` turns
saving off; `"state_file"` moves the file.

Config (faders/config.json) — dict form, faders fill pages of four:
    "filter_param": {
        "faders": [
            {"param": "fx-comp-apps:al", "scale": "db", "min": -40,
             "max": 0, "label": "Apps thr"},
            {"param": "fx-comp-apps:cr", "scale": "log", "min": 1,
             "max": 10, "unit": ":1", "label": "Apps ratio"},
            ...
        ]
    }
Per entry:
    param     full control name (required)
    scale     "db": the control is a linear gain (LSP thresholds, makeup),
                    shown and mapped in dB; min / max in dB
              "log": mapped logarithmically (times, ratios); min > 0
              "linear" (default)
    min, max  the fader's range, in the units of `scale`
    unit      display suffix (default " dB" for db, else none)
    decimals  display decimals (default: 1 for dB and below 10, else 0)
    label     line 1 of the screen (default: the control name)
    creases   false turns the original-value detent off
More entries than faders page like pulse_sink (G2 + F1 / F4 held, `fader`
4-7 is page 2 ...). Give this plugin a layer of its own: a layer has one set
of pages, and two paged plugins on one layer would page over each other.

Needs `pw-dump` and `pw-cli` (pipewire) in PATH.
"""

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..core import Extension, register_slots

PW_DUMP = "pw-dump"
PW_CLI = "pw-cli"

LAYER_NAME = "FX"

# Batch writes to pw-cli no more often than this.
WRITE_INTERVAL_S = 0.04
# Ignore value reports for this long after a fader write: they are our own
# writes coming back, older than where the hand already is.
SELF_ECHO_WINDOW_S = 0.3
# Fader moves smaller than this (in fader %) are not written.
WRITE_EPSILON_PCT = 0.3
# A reported value this close to the fader's is not worth a motor move.
FOLLOW_EPSILON_PCT = 0.5
# Wait before restarting a pw-dump that exited (PipeWire restarting).
RESTART_DELAY_S = 2.0
# Save the state file this long after the last change.
SAVE_DELAY_S = 1.0
# The detent at the original value: (power %, pulse ms).
CREASE_ORIGINAL = (55.0, 12)

log = logging.getLogger("faders.filter_param")


def default_state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return Path(base) / "motorized-faders" / "filter_param.json"


def _close(a, b):
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def _props_of(info):
    """{control name: value} from a node's Props params, or None if absent."""
    props = (info.get("params") or {}).get("Props")
    if props is None:
        return None
    out = {}
    for p in props:
        flat = p.get("params") if isinstance(p, dict) else None
        if not flat:
            continue
        for k, v in zip(flat[::2], flat[1::2]):
            if isinstance(k, str) and isinstance(v, (int, float)) \
                    and not isinstance(v, bool):
                out[k] = float(v)
    return out


def set_props_cmd(node_id, values):
    """pw-cli command setting `values` ({name: value}) on node `node_id`."""
    body = " ".join(f'"{k}" {v:.6g}' for k, v in values.items())
    return [PW_CLI, "s", str(node_id), "Props", f"{{ params = [ {body} ] }}"]


# ----- shared PipeWire watcher -----------------------------------------------

class PwParams(Extension):
    """
    One `pw-dump --monitor` and one writer for every fader of the plugin.
    Tracks which node carries each watched control and its current value,
    notifies watchers on change, and keeps the state file.

    Registered on the layer as an extension of its own so it is stopped with
    the host; it has no fader.
    """

    def __init__(self, state_path=None):
        self._lock = threading.RLock()
        self._watchers = {}       # key -> [callback(value or None)]
        self._values = {}         # key -> current value
        self._where = {}          # key -> node id
        self._serials = {}        # node id -> object.serial
        self._cookie = None       # PipeWire core cookie: new on each start
        self._pending = {}        # key -> value to write
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._proc = None
        self._state_path = Path(state_path) if state_path else None
        self._state = self._load_state()
        self._save_timer = None

    def start(self):
        """Begin watching; after every fader has called watch()."""
        for target in (self._monitor_loop, self._writer_loop):
            threading.Thread(target=target, daemon=True).start()

    # ----- watchers ------------------------------------------------------

    def watch(self, key, callback):
        with self._lock:
            self._watchers.setdefault(key, []).append(callback)

    def keys(self):
        with self._lock:
            return list(self._watchers)

    def value(self, key):
        with self._lock:
            return self._values.get(key)

    def original(self, key):
        with self._lock:
            entry = self._state.get(key)
            return entry.get("original") if entry else None

    def _notify(self, key, value):
        with self._lock:
            callbacks = list(self._watchers.get(key, ()))
        for cb in callbacks:
            try:
                cb(value)
            except Exception:
                log.exception("watcher for %s failed", key)

    # ----- writes --------------------------------------------------------

    def set(self, key, value):
        with self._lock:
            if key not in self._where:
                log.warning("%s: no PipeWire node carries it; not set", key)
                return
            self._pending[key] = float(value)
        self._wake.set()

    def flush(self):
        """Write everything pending now: one pw-cli call per node."""
        with self._lock:
            pending, self._pending = self._pending, {}
            by_node = {}
            for key, value in pending.items():
                node = self._where.get(key)
                if node is not None:
                    by_node.setdefault(node, {})[key] = value
        for node, values in by_node.items():
            cmd = set_props_cmd(node, values)
            log.debug("%s", " ".join(cmd))
            try:
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True,
                                     timeout=2.0)
                if res.returncode:
                    log.warning("pw-cli exited %d: %s", res.returncode,
                                res.stderr.strip())
            except FileNotFoundError:
                log.error("`%s` not found in PATH", PW_CLI)
            except subprocess.TimeoutExpired:
                log.warning("pw-cli timed out setting %s", list(values))

    def _writer_loop(self):
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            self.flush()
            time.sleep(WRITE_INTERVAL_S)

    # ----- pw-dump -------------------------------------------------------

    def feed(self, objects):
        """Apply one pw-dump array (the full dump or a monitor update)."""
        for obj in objects:
            if obj.get("type") == "PipeWire:Interface:Core" and obj.get("info"):
                cookie = obj["info"].get("cookie")
                if cookie is not None:
                    self._cookie = cookie
        for obj in objects:
            info = obj.get("info")
            node_id = obj.get("id")
            if info is None:
                self._removed(node_id)
                continue
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = _props_of(info)
            if props is None:
                continue
            serial = (info.get("props") or {}).get("object.serial")
            if serial is not None:
                self._serials[node_id] = serial
            for key in self.keys():
                if key in props:
                    self._seen(key, node_id, props[key])

    def _removed(self, node_id):
        with self._lock:
            gone = [k for k, n in self._where.items() if n == node_id]
            for key in gone:
                del self._where[key]
                self._values.pop(key, None)
            self._serials.pop(node_id, None)
        for key in gone:
            log.info("%s: node #%s removed", key, node_id)
            self._notify(key, None)

    def _seen(self, key, node_id, value):
        with self._lock:
            appeared = self._where.get(key) != node_id
            self._where[key] = node_id
            changed = not _close(self._values.get(key), value)
            self._values[key] = value
        if appeared:
            log.info("%s on node #%s = %.6g", key, node_id, value)
            self._appeared(key, node_id, value)
        if changed:
            self._remember(key, value)
            self._notify(key, value)

    def _appeared(self, key, node_id, value):
        """
        `key` showed up on a node. A node PipeWire has loaded since the state
        was written carries the config's value: that is the original, and a
        saved value is put back on top of it. A node the state already knows
        (the host restarted, PipeWire did not) keeps whatever it has.
        """
        if self._state_path is None:
            return
        serial = self._serials.get(node_id)
        with self._lock:
            entry = self._state.get(key)
            if entry and entry.get("cookie") == self._cookie \
                    and entry.get("serial") == serial:
                return
            saved = entry.get("value") if entry else None
            original = entry.get("original") if entry else None
            if original is not None and not _close(original, value):
                log.info("%s: config value changed %.6g -> %.6g; saved %s "
                         "dropped", key, original, value,
                         "value" if saved is not None else "state")
                saved = None
            self._state[key] = {"original": value,
                                "value": value if saved is None else saved,
                                "cookie": self._cookie, "serial": serial}
        self._schedule_save()
        if saved is not None and not _close(saved, value):
            log.info("%s: restoring saved %.6g (config %.6g)", key, saved,
                     value)
            self.set(key, saved)

    def _monitor_loop(self):
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    [PW_DUMP, "--monitor", "--no-colors"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1, start_new_session=True)
            except FileNotFoundError:
                log.error("`%s` not found; filter params disabled", PW_DUMP)
                return
            self.read_stream(self._proc.stdout)
            if self._stop.is_set():
                break
            log.warning("pw-dump exited; restarting in %.0f s", RESTART_DELAY_S)
            for node_id in list(set(self._where.values())):
                self._removed(node_id)
            self._stop.wait(RESTART_DELAY_S)

    def read_stream(self, lines):
        """pw-dump prints one JSON array per update, `[` and `]` at column 0."""
        buf = []
        for line in lines:
            if self._stop.is_set():
                return
            buf.append(line)
            if line.rstrip("\n") == "]":
                try:
                    objects = json.loads("".join(buf))
                except ValueError as e:
                    log.warning("unreadable pw-dump output: %s", e)
                    objects = []
                buf = []
                self.feed(objects)

    # ----- state file ----------------------------------------------------

    def _load_state(self):
        if self._state_path is None:
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            return {k: v for k, v in data.get("params", {}).items()
                    if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("state file %s unreadable (%s); starting empty",
                        self._state_path, e)
            return {}

    def _remember(self, key, value):
        if self._state_path is None:
            return
        with self._lock:
            entry = self._state.get(key)
            if entry is None or _close(entry.get("value"), value):
                return
            entry["value"] = value
        self._schedule_save()

    def _schedule_save(self):
        if self._state_path is None:
            return
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(SAVE_DELAY_S, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def save(self):
        if self._state_path is None:
            return
        with self._lock:
            self._save_timer = None
            data = {"params": dict(sorted(self._state.items()))}
        write_state(self._state_path, data)

    # ----- shutdown --------------------------------------------------------

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            timer = self._save_timer
        if timer is not None:
            timer.cancel()
            self.save()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass


def write_state(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


# ----- value <-> fader mapping -----------------------------------------------

class Scale:
    def __init__(self, kind, lo, hi):
        if kind not in ("db", "log", "linear"):
            raise ValueError(f"scale must be db, log or linear, not {kind!r}")
        if hi <= lo:
            raise ValueError(f"max ({hi}) must be above min ({lo})")
        if kind == "log" and lo <= 0:
            raise ValueError("a log scale needs min > 0")
        self.kind = kind
        self.lo = float(lo)
        self.hi = float(hi)

    def shown(self, value):
        """Control value in the scale's display units (dB for db)."""
        if self.kind == "db":
            return 20.0 * math.log10(max(value, 1e-9))
        return value

    def to_fader(self, value):
        x = self.shown(value)
        if self.kind == "log":
            x = math.log(max(x, 1e-9))
            lo, hi = math.log(self.lo), math.log(self.hi)
        else:
            lo, hi = self.lo, self.hi
        return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))

    def from_fader(self, pct):
        t = max(0.0, min(100.0, pct)) / 100.0
        if self.kind == "log":
            return math.exp(math.log(self.lo) + t * (math.log(self.hi) - math.log(self.lo)))
        x = self.lo + t * (self.hi - self.lo)
        if self.kind == "db":
            return 10.0 ** (x / 20.0)
        return x


# ----- one fader ---------------------------------------------------------------

def register_all(host, cfg):
    """
    The whole `filter_param` config: an entry list or a dict (see
    register_slots). One PwParams serves every entry.
    """
    opts = cfg if isinstance(cfg, dict) and "faders" in cfg else {}
    state_path = None
    if opts.get("persist", True):
        state_path = Path(os.path.expanduser(opts["state_file"])) \
            if opts.get("state_file") else default_state_path()
    params = PwParams(state_path=state_path)
    if isinstance(cfg, dict) and "faders" in cfg:
        cfg = {k: v for k, v in cfg.items() if k not in ("persist", "state_file")}
    host.register(params)
    group = register_slots(host, cfg,
                           lambda h, entry: register(h, entry, params),
                           "filter_param")
    params.start()
    return group


def register(host, cfg, params):
    scale = cfg.get("scale", "linear")
    ext = FilterParamExtension(
        host=host,
        params=params,
        fader_idx=int(cfg["fader"]),
        key=str(cfg["param"]),
        scale=Scale(scale, float(cfg["min"]), float(cfg["max"])),
        unit=cfg.get("unit", " dB" if scale == "db" else ""),
        decimals=cfg.get("decimals"),
        label=cfg.get("label"),
        creases=bool(cfg.get("creases", True)),
    )
    host.register(ext)
    return ext


class FilterParamExtension(Extension):
    def __init__(self, host, params, fader_idx, key, scale, unit="",
                 decimals=None, label=None, creases=True):
        if ":" not in key:
            raise ValueError(f"param {key!r} is not <filter>:<control>")
        self.host = host
        self.params = params
        self.fader_idx = fader_idx
        self.key = key
        self.enabled_key = key.rsplit(":", 1)[0] + ":enabled"
        self.scale = scale
        self.unit = unit
        self.decimals = decimals
        self.label = label or key
        self.creases = creases
        self._tag = f"fader {fader_idx + 1} [{self.label}]"

        self._lock = threading.Lock()
        self._muted = False        # plugin bypassed
        self._fader = None         # where the fader is, as far as we know
        self._last_written = None  # last value this fader wrote
        self._last_write_t = 0.0
        self._crease_at = None

        params.watch(key, self._on_value)
        if self.enabled_key != key:
            params.watch(self.enabled_key, self._on_enabled)
        self._push_display()

    # ----- display / creases ---------------------------------------------

    def format(self, value):
        x = self.scale.shown(value)
        decimals = self.decimals
        if decimals is None:
            decimals = 1 if self.scale.kind == "db" or abs(x) < 10 else 0
        sign = "+" if self.scale.kind == "db" and x > 0.05 else ""
        return f"{sign}{x:.{int(decimals)}f}{self.unit}"

    def _push_display(self, value=None):
        if self._muted:
            line2 = "bypassed"
        else:
            if value is None:
                value = self.params.value(self.key)
            line2 = "--" if value is None else self.format(value)
        self.host.set_display(self.fader_idx, self.label, line2)

    def _update_creases(self):
        if not self.creases:
            return
        original = self.params.original(self.key)
        if original is None:
            return
        at = round(self.scale.to_fader(original), 1)
        if at == self._crease_at:
            return
        self._crease_at = at
        # The travel ends are end stops already.
        layout = [] if at <= 1.0 or at >= 99.0 else [(at, *CREASE_ORIGINAL)]
        self.host.set_creases(self.fader_idx, layout)

    # ----- (re)connect -----------------------------------------------------

    def on_connect(self):
        value = self.params.value(self.key)
        enabled = self.params.value(self.enabled_key)
        with self._lock:
            self._muted = enabled is not None and enabled < 0.5
            self._last_written = None
            muted = self._muted
            if value is not None:
                self._fader = self.scale.to_fader(value)
            level = self._fader
        if muted:
            self.host.set_mute(self.fader_idx, True, level)
        elif level is not None:
            self.host.set_fader(self.fader_idx, level)
        self._update_creases()
        self._push_display(value)

    # ----- fader -> PipeWire ---------------------------------------------

    def on_position(self, fader_idx, value):
        if fader_idx != self.fader_idx:
            return
        with self._lock:
            if self._muted:
                # Parked or placed while bypassed: not a value change.
                return
            if self._fader is not None \
                    and abs(value - self._fader) < WRITE_EPSILON_PCT:
                return
            self._fader = value
            target = self.scale.from_fader(value)
            self._last_written = target
            self._last_write_t = time.monotonic()
        log.debug("%s: fader %.1f%% -> %.6g", self._tag, value, target)
        self.params.set(self.key, target)
        self._push_display(target)

    def on_mute(self, fader_idx, muted):
        if fader_idx != self.fader_idx:
            return
        with self._lock:
            if self._muted == muted:
                return
            self._muted = muted
            level = self._fader
        log.info("%s: plugin %s", self._tag, "bypassed" if muted else "on")
        self.params.set(self.enabled_key, 0.0 if muted else 1.0)
        if not muted and level is not None:
            # The Pico returns it to where the hand left it while parked,
            # which was never written: back to the value it stands for.
            self.host.set_fader(self.fader_idx, level)
        self._push_display()

    # ----- PipeWire -> fader ---------------------------------------------

    def _on_value(self, value):
        self._update_creases()
        if value is None:
            self._push_display()
            return
        target = self.scale.to_fader(value)
        with self._lock:
            echo = (time.monotonic() - self._last_write_t < SELF_ECHO_WINDOW_S
                    or _close(value, self._last_written))
            if echo:
                return
            self._last_written = None
            muted = self._muted
            first = self._fader is None
            move = first or abs(target - self._fader) >= FOLLOW_EPSILON_PCT
            self._fader = target
        self._push_display(value)
        if not move:
            return
        log.log(logging.DEBUG if first else logging.INFO,
                "%s: %s%s, moving fader to %.1f%%", self._tag,
                self.format(value), "" if first else " set elsewhere", target)
        if muted:
            # Parked: change where unbypassing brings it back to.
            self.host.set_mute(self.fader_idx, True, target)
        else:
            self.host.set_fader(self.fader_idx, target)

    def _on_enabled(self, enabled):
        if enabled is None:
            return
        muted = enabled < 0.5
        with self._lock:
            if muted == self._muted:
                return
            self._muted = muted
            level = self._fader
        log.info("%s: plugin %s elsewhere", self._tag,
                 "bypassed" if muted else "on")
        self.host.set_mute(self.fader_idx, muted, level)
        self._push_display()


# ----- CLI: status / reset ---------------------------------------------------

def _dump_once():
    out = subprocess.check_output([PW_DUMP, "--no-colors"], text=True,
                                  timeout=5.0)
    return json.loads(out)


def _live_values():
    """{control name: (node id, value)} for every node's Props."""
    live = {}
    for obj in _dump_once():
        info = obj.get("info")
        if obj.get("type") != "PipeWire:Interface:Node" or not info:
            continue
        for key, value in (_props_of(info) or {}).items():
            live[key] = (obj["id"], value)
    return live


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="motorized-faders-fx",
        description="Saved filter_param values: list them, or put the "
                    "PipeWire config's originals back.")
    parser.add_argument("--state", default=str(default_state_path()),
                        help="state file (default: %(default)s)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="saved, original and live values")
    reset = sub.add_parser("reset", help="set controls back to the original")
    reset.add_argument("keys", nargs="*",
                       help="controls to reset (default: all saved)")
    args = parser.parse_args(argv)

    path = Path(args.state)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"no state file at {path}")
        return 0
    state = data.get("params", {})
    try:
        live = _live_values()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"pw-dump failed: {e}", file=sys.stderr)
        live = {}

    if args.cmd == "status":
        print(f"{'control':<28}{'original':>12}{'saved':>12}{'live':>12}")
        for key, entry in sorted(state.items()):
            now = live.get(key, (None, None))[1]
            cells = [entry.get("original"), entry.get("value"), now]
            print(f"{key:<28}" + "".join(
                f"{'-' if c is None else format(c, '.6g'):>12}" for c in cells))
        return 0

    keys = args.keys or sorted(state)
    unknown = [k for k in keys if k not in state]
    if unknown:
        print("not in the state file: " + ", ".join(unknown), file=sys.stderr)
        return 1
    by_node = {}
    for key in keys:
        entry = state[key]
        original = entry.get("original")
        if original is None:
            continue
        entry["value"] = original
        if key in live:
            by_node.setdefault(live[key][0], {})[key] = original
        print(f"{key}: {original:.6g}")
    write_state(path, data)
    failed = False
    for node, values in by_node.items():
        if subprocess.run(set_props_cmd(node, values)).returncode:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
