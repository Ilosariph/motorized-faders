"""
media tests — player selection, buttons, volume and mute both ways against a
fake backend; the Linux backend's parsing against canned command output; the
Windows backend against stand-ins for WinRT and pycaw.

Run with: python3 tests/test_media.py
"""

import json
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_host import ExpiredTimer, FakeSerial, check, FAILURES, PASSES  # noqa: E402

from faders.core import FaderHost  # noqa: E402
from faders.extensions import media  # noqa: E402
from faders.extensions.media import Player  # noqa: E402


class FakeBackend:
    def __init__(self, players=()):
        self.list = list(players)
        self.levels = {}
        self.commands = []
        self.volumes = []
        self.mutes = []
        # Player id -> playback rate; ids in `fixed` refuse a new one.
        self.rates = {}
        self.fixed = set()
        self.rate_writes = []

    def open(self):
        pass

    def close(self):
        pass

    def players(self):
        return list(self.list)

    def command(self, player_id, action):
        self.commands.append((player_id, action))

    def audio(self, player_id):
        return self.levels.get(player_id, (None, False))

    def set_volume(self, player_id, percent):
        self.volumes.append((player_id, round(percent, 1)))
        self.levels[player_id] = (percent, self.levels[player_id][1])

    def set_mute(self, player_id, muted):
        self.mutes.append((player_id, muted))
        self.levels[player_id] = (self.levels[player_id][0], muted)

    def rate(self, player_id):
        return self.rates.get(player_id)

    def set_rate(self, player_id, rate):
        self.rate_writes.append((player_id, rate))
        if player_id in self.fixed:
            return False
        self.rates[player_id] = rate
        return True


SPOTIFY = Player("spotify", "Spotify", media.PLAYING, "Night Witches", "Sabaton")
PLEZY = Player("plezy", "Plezy", media.PAUSED, "Episode", "Show")


def make(listed=(SPOTIFY,), levels=None, **kwargs):
    backend = FakeBackend(listed)
    backend.levels = ({"spotify": (60.0, False), "plezy": (30.0, False)}
                      if levels is None else dict(levels))
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Media")
    ext = media.MediaExtension(layer, backend, start=False, **kwargs)
    layer.register(ext)
    return host, layer, ext, backend


def idle(host):
    """The commanded moves finished; the hand has the faders."""
    for i in range(1, 5):
        host._handle_line(f"STATE:{i},IDLE")
    host._commanded = [None] * 4


def expire_hand(ext):
    ext._hand_t = -1e9


# ---------------------------------------------------------------------------

def test_layout_and_screens():
    print("Layout")
    host, layer, ext, backend = make()
    check(host._display[0] == ("Media", "no player"),
          f"no player before the first poll (got {host._display[0]})")
    check([host._pending[i] for i in (1, 2, 3)] == [0.0, 0.0, 0.0],
          f"button faders park (got {host._pending})")
    ext.poll()
    check(host._display[0] == ("Spotify", "vol 60%"), f"volume screen (got {host._display[0]})")
    check(host._display[1] == ("Previous", "|<"), "previous screen")
    check(host._display[2] == ("Night Witches", "Playing"), f"title (got {host._display[2]})")
    check(host._display[3] == ("Next", ">|"), "next screen")
    check(host._pending[0] == 60.0, f"volume fader follows the player (got {host._pending[0]})")
    check([c[0] for c in layer.bumps[0]] == [25.0, 50.0, 75.0], "bumps on the volume fader")
    check(layer.bumps[1] is None, "none on button faders")

    for bad in (["volume", "volume"], ["nope"], [None] * 5):
        try:
            make(layout=bad)
            check(False, f"layout {bad} rejected")
        except ValueError:
            check(True, "")


def test_non_ascii_title_reaches_the_screen():
    print("Non-ASCII")
    title = Player("spotify", "Spotify", media.PLAYING, "Größtes Klärwerk • Teil 2")
    host, layer, ext, backend = make([title])
    ext.poll()
    check(host._display[2][0] == "Grosstes Klarwerk  Te",
          f"title reduced to ASCII (got {host._display[2]})")
    check(all(line.isascii() for line in host._ser.lines()), "only ASCII on the wire")


def test_selection():
    print("Selection")
    a = Player("a", "A", media.PAUSED)
    b = Player("b", "B", media.PLAYING)
    host, layer, ext, backend = make([a, b], levels={"a": (10.0, False), "b": (20.0, False)})
    ext.poll()
    check(ext._player.id == "b", "the one playing is picked")
    b.status = media.PAUSED
    ext.poll()
    check(ext._player.id == "b", "stays on it when it pauses")
    a.status = media.PLAYING
    host._pending = [None] * 4
    ext.poll()
    check(ext._player.id == "a", "moves when another starts")
    check(host._pending[0] == 10.0, f"fader moves to the new player (got {host._pending[0]})")
    b.status = media.PLAYING
    ext.poll()
    check(ext._player.id == "a", "two playing: the current one stays")
    backend.list = [b]
    ext.poll()
    check(ext._player.id == "b", "current one gone: the other takes over")
    backend.list = []
    ext.poll()
    check(ext._player is None and host._display[0] == ("Media", "no player"), "none left")

    host, layer, ext, backend = make([SPOTIFY, PLEZY], ignore=["spot"])
    ext.poll()
    check(ext._player.id == "plezy", "ignore")
    paused = [Player("a", "A", media.PAUSED), Player("b", "B", media.PAUSED)]
    host, layer, ext, backend = make(paused, players=["b", "a"],
                                     levels={"a": (1.0, False), "b": (2.0, False)})
    ext.poll()
    check(ext._player.id == "b", "players order breaks ties")
    host, layer, ext, backend = make([SPOTIFY], players=["plezy"])
    ext.poll()
    check(ext._player is None, "players excludes the rest")


def page_step(host, button):
    """G2 + button held long enough to page."""
    original = host._hold_timer
    host._hold_timer = ExpiredTimer
    try:
        for line in ("BTN:G2", f"BTN:{button}", f"BTNUP:{button}", "BTNUP:G2"):
            host._handle_line(line)
    finally:
        host._hold_timer = original


def test_pages():
    print("Pages")
    a = Player("a", "A", media.PLAYING, "Song A")
    b = Player("b", "B", media.PLAYING, "Song B")
    host, layer, ext, backend = make(
        [a, b], levels={"a": (10.0, False), "b": (20.0, False)})
    check(layer.paged, "G2 + F1/F4 held pages on this layer")
    ext.poll()
    check(ext._player.id == "a" and ext._pages == ["a", "b"], f"one page each (got {ext._pages})")
    check(host._display[3] == ("Next", ">|\t1/2"), f"page shown (got {host._display[3]})")

    host._pending = [None] * 4
    page_step(host, "F4")
    ext.poll()
    check(ext._player.id == "b", "G2 + F4 held: next page")
    check(host._display[0] == ("B", "vol 20%") and host._display[2][0] == "Song B",
          f"screens follow (got {host._display[:3]})")
    check(host._display[3][1].endswith("2/2"), "page 2/2 shown")
    check(host._pending[0] == 20.0, f"fader follows the page (got {host._pending[0]})")
    host._handle_line("BTN:F3")
    ext.apply()
    check(backend.commands == [("b", "play_pause")], "transport acts on the page shown")

    b.status = media.PAUSED
    ext.poll()
    check(ext._player.id == "b" and ext._pages == ["a", "b"],
          "paused keeps its page: the next press resumes it")
    b.status = media.PLAYING
    page_step(host, "F1")
    ext.poll()
    check(ext._player.id == "a", "G2 + F1 held: previous page")

    page_step(host, "F1")
    ext.poll()
    check(ext._player.id == "b", "pages wrap")
    a.status = media.PAUSED
    ext.poll()
    check(ext._pages == ["b"] and ext._player.id == "b",
          f"not playing, not selected: page goes (got {ext._pages})")
    check(host._display[3] == ("Next", ">|"), f"no page shown for one (got {host._display[3]})")

    c = Player("c", "C", media.PLAYING)
    backend.list = [a, b, c]
    backend.levels["c"] = (5.0, False)
    ext.poll()
    check(ext._player.id == "b" and ext._pages == ["b", "c"],
          "a new one adds a page without taking the faders")
    page_step(host, "F4")
    ext.poll()
    backend.list = [a, b]
    ext.poll()
    check(ext._player.id == "b" and ext._pages == ["b"], "closed player: its page goes")

    host, layer, ext, backend = make([SPOTIFY])
    ext.poll()
    host._pending = [None] * 4
    page_step(host, "F4")
    ext.poll()
    check(ext._player.id == "spotify" and host._pending[0] is None,
          "one page: stepping changes nothing")

    host, layer, ext, backend = make(
        [Player("a", "A", media.PLAYING), Player("b", "B", media.PLAYING)],
        levels={"a": (10.0, False), "b": (20.0, False)},
        layout=["volume", "next"])
    ext.poll()
    check(host._display[1][1].endswith("1/2"), "page on the last screen this plugin draws")


def test_buttons():
    print("Buttons")
    host, layer, ext, backend = make()
    ext.poll()
    for button in ("F2", "F3", "F4"):
        host._handle_line(f"BTN:{button}")
        host._handle_line(f"BTNUP:{button}")
    check(ext.apply() is True, "commands report that a quick poll is due")
    check(backend.commands == [("spotify", "previous"), ("spotify", "play_pause"),
                               ("spotify", "next")],
          f"buttons are transport (got {backend.commands})")
    check(not any(ln.startswith("MUTE:") for ln in host._ser.lines()),
          f"transport buttons do not mute (got {host._ser.lines()})")

    host._handle_line("BTN:F1")
    check("MUTE:1,1" in host._ser.lines(), "volume fader's button still mutes")
    host._handle_line("BTNUP:F1")

    backend.commands.clear()
    host._handle_line("BTN:G2")
    host._handle_line("BTN:F3")
    host._handle_line("BTNUP:F3")
    host._handle_line("BTNUP:G2")
    ext.apply()
    check(backend.commands == [], "G2 + button is not a transport press")
    check(layer.ab_slot[2] == "B", "it stays the A/B swap")

    other = host.add_layer("Other")
    backend.commands.clear()
    host._handle_line("BTN:G1")
    host._handle_line("BTN:F4")
    host._handle_line("BTNUP:F4")
    host._handle_line("BTNUP:G1")
    ext.apply()
    check(backend.commands == [] and host.active_layer is other,
          "G1 + F4 switches layer without skipping")
    host._handle_line("BTN:F4")
    ext.apply()
    check(backend.commands == [], "another layer's F4 is not ours")


def test_button_without_player():
    host, layer, ext, backend = make([])
    ext.poll()
    host._handle_line("BTN:F3")
    check(ext.apply() is False and backend.commands == [], "no player: nothing sent")


def test_fader_writes_volume():
    print("Fader -> player")
    host, layer, ext, backend = make()
    ext.poll()
    idle(host)
    host._handle_line("POS:40.0,0,0,0")
    ext.apply()
    check(backend.volumes == [("spotify", 40.0)], f"volume written (got {backend.volumes})")
    check(host._display[0][1] == "vol 40%", "screen follows the drag")
    host._handle_line("POS:40.2,0,0,0")
    ext.apply()
    check(len(backend.volumes) == 1, "jitter not written")

    # A poll overlapping the drag still reads the old volume.
    backend.levels["spotify"] = (60.0, False)
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[0] is None and ext._volume == 40.0,
          "stale reading right after the hand is ignored")

    host._handle_line("POS:40.0,55.0,0,0")
    ext.apply()
    check(len(backend.volumes) == 1, "moving a button fader writes nothing")


def test_app_changes_move_the_fader():
    print("Player -> fader")
    host, layer, ext, backend = make()
    ext.poll()
    host._pending = [None] * 4
    backend.levels["spotify"] = (61.0, False)
    ext.poll()
    check(host._pending[0] is None, "reading noise moves nothing")
    backend.levels["spotify"] = (80.0, False)
    ext.poll()
    check(host._pending[0] == 80.0, f"app volume moves the fader (got {host._pending[0]})")

    backend.levels["spotify"] = (80.0, True)
    ext.poll()
    check("MUTE:1,1,80.0" in host._ser.lines(), f"app mute parks (got {host._ser.lines()})")
    host._handle_line("MUTE:1,1")
    ext.apply()
    check(backend.mutes == [], "the confirmation is not written back")

    backend.levels["spotify"] = (50.0, True)
    host._ser.written.clear()
    ext.poll()
    check("MUTE:1,1,50.0" in host._ser.lines(), "muted app volume: only the restore level")

    backend.levels["spotify"] = (50.0, False)
    host._ser.written.clear()
    ext.poll()
    check("MUTE:1,0,50.0" in host._ser.lines(), f"unmute returns (got {host._ser.lines()})")


def test_mute_from_button():
    print("Mute")
    host, layer, ext, backend = make()
    ext.poll()
    idle(host)
    host._handle_line("BTN:F1")
    host._handle_line("MUTE:1,1")
    ext.apply()
    check(backend.mutes == [("spotify", True)], f"mute written (got {backend.mutes})")
    host._handle_line("STATE:1,IDLE")
    host._handle_line("POS:70,0,0,0")
    ext.apply()
    check(backend.volumes == [], "dragging a muted fader writes nothing")
    expire_hand(ext)
    host._handle_line("MUTE:1,0")
    ext.apply()
    check(backend.mutes[-1] == ("spotify", False), "unmute written")


def test_no_stream():
    print("No stream")
    host, layer, ext, backend = make(levels={})
    ext.poll()
    check(host._display[0] == ("Spotify", "vol --"), f"shown (got {host._display[0]})")
    idle(host)
    host._handle_line("POS:40,0,0,0")
    ext.apply()
    check(backend.volumes == [], "nothing to write to")
    backend.levels["spotify"] = (70.0, False)
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[0] == 70.0, "stream appears: fader goes to it")


def test_reconnect_and_custom_layout():
    print("Reconnect, layout")
    host, layer, ext, backend = make()
    ext.poll()
    host._pending = [None] * 4
    ext.on_connect()
    check(host._pending == [60.0, 0.0, 0.0, 0.0], f"all re-driven (got {host._pending})")

    host, layer, ext, backend = make(layout=[None, "next", None, "volume"])
    ext.poll()
    check(host._pending[3] == 60.0 and host._pending[0] is None, "volume on fader 4")
    host._handle_line("BTN:F1")
    check("MUTE:1,1" in host._ser.lines(), "unclaimed fader keeps its mute")
    check(ext.apply() is False, "and sends no command")
    check(host._display[0] is None, "unclaimed screen left alone")


def test_speed_mapping():
    print("Speed mapping")
    check(media.speed_to_pos(0.25, 0.25, 3.5) == 0.0 and media.speed_to_pos(3.5, 0.25, 3.5) == 100.0, "ends")
    check(media.speed_to_pos(1.0, 0.25, 3.5) == 25.0, "1x a quarter up")
    check(media.speed_to_pos(2.0, 0.25, 3.5) == 55.0, "2x")
    check(media.speed_to_pos(16.0, 0.25, 3.5) == 100.0 and media.speed_to_pos(0.1, 0.25, 3.5) == 0.0, "clamped")
    for rate in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.65, 3.0, 3.5):
        back = media.pos_to_speed(media.speed_to_pos(rate, 0.25, 3.5), 0.25, 3.5)
        check(back == rate, f"{rate} round trip (got {back})")
    check(media.pos_to_speed(26.5, 0.25, 3.5) == 1.05, "0.05x steps")
    check([media.speed_text(r) for r in (1.0, 1.5, 1.25)] == ["1x", "1.5x", "1.25x"],
          "speed text")


def test_speed():
    print("Speed")
    host, layer, ext, backend = make(speed_min=0.25, speed_max=3.5)
    check(host._pending[2] == 0.0, "no rate yet: speed fader parked")
    check([c[:2] for c in layer.bumps[2]] == [(25.0, 80.0), (40.0, 55.0), (55.0, 55.0),
                                                 (70.0, 55.0), (85.0, 55.0)],
          f"bumps: 1x firm, 1.5x 2x 2.5x 3x (got {layer.bumps[2]})")
    backend.rates["spotify"] = 1.5
    ext.poll()
    check(host._pending[2] == 40.0, f"speed fader follows the rate (got {host._pending[2]})")
    check(host._display[2] == ("Night Witches", "> 1.5x"), f"screen (got {host._display[2]})")

    idle(host)
    host._handle_line("POS:60,0,55,0")
    ext.apply()
    check(backend.rate_writes == [("spotify", 2.0)], f"rate written (got {backend.rate_writes})")
    check(host._display[2][1] == "> 2x", "screen follows the drag")
    check(len(backend.volumes) <= 1, "speed fader writes no volume")

    # A poll overlapping the drag still reads the old rate.
    backend.rates["spotify"] = 1.5
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[2] is None and ext._rate == 2.0, "stale rate after the hand ignored")

    ext._speed_hand_t = -1e9
    backend.rates["spotify"] = 1.25
    ext.poll()
    check(host._pending[2] == media.speed_to_pos(1.25, 0.25, 3.5), "rate changed in the app moves the fader")

    host._handle_line("BTN:F3")
    ext.apply()
    check(backend.commands == [("spotify", "play_pause")], "button still plays / pauses")
    check(not any(ln.startswith("MUTE:3") for ln in host._ser.lines()), "and does not mute")

    SPOTIFY.status = media.PAUSED
    try:
        ext.poll()
        check(host._display[2][1] == "|| 1.25x", f"paused mark (got {host._display[2]})")
    finally:
        SPOTIFY.status = media.PLAYING

    backend.list = [PLEZY]
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[2] == 0.0 and host._display[2] == ("Episode", "Paused"),
          f"player without a rate: parked, plain status (got {host._display[2]})")
    idle(host)
    backend.rate_writes.clear()
    host._handle_line("POS:30,0,70,0")
    ext.apply()
    check(backend.rate_writes == [], "nothing to write without a rate")


def test_speed_range():
    print("Speed range")
    check(media.speed_to_pos(0.5, 0.5, 2.0) == 0.0 and media.speed_to_pos(1.0, 0.5, 2.0) == 25.0
          and media.speed_to_pos(2.0, 0.5, 2.0) == 100.0, "custom range: ends, 1x a quarter up")
    check(media.speed_to_pos(1.5, 1.0, 2.0) == 50.0 and media.pos_to_speed(50.0, 1.0, 2.0) == 1.5,
          "range not spanning 1x: linear")
    check(media.pos_to_speed(100.0, 0.3, 2.33) == 2.33 and media.pos_to_speed(0.0, 0.33, 2.0) == 0.33,
          "ends off the 0.05x grid stay in range")
    check(media.speed_bumps(0.25, 3.5) == [(1.0, True), (1.5, False), (2.0, False),
                                             (2.5, False), (3.0, False)], "default detents")
    check(media.speed_bumps(1.0, 2.0) == [(1.5, False)], "1x at the end: no firm detent")
    check(media.speed_bumps(1.6, 3.0) == [(2.0, False), (2.5, False)], "only inside the range")

    check((media.SPEED_MIN, media.SPEED_MAX) == (0.25, 3.0), "default range 0.25x - 3x")
    check(media.speed_to_pos(3.0) == 100.0 and media.speed_to_pos(1.0) == 25.0, "default curve")
    check(media.pos_to_speed(100.0, 0.25, 5.0) == 5.0 and media.speed_to_pos(5.0, 0.25, 5.0) == 100.0,
          "5x reachable when configured")
    host, layer, ext, backend = make(speed_max=5.0)
    check(ext._speed_hi == 5.0, "speed_max 5 accepted")

    host, layer, ext, backend = make(speed_min=0.5, speed_max=2.0)
    check([c[:2] for c in layer.bumps[2]] == [(25.0, 80.0), (62.5, 55.0)],
          f"bumps follow the range (got {layer.bumps[2]})")
    backend.rates["spotify"] = 1.5
    ext.poll()
    check(host._pending[2] == 62.5, f"fader placed on the range (got {host._pending[2]})")
    idle(host)
    host._handle_line("POS:60,0,100,0")
    ext.apply()
    check(backend.rate_writes == [("spotify", 2.0)], f"top is the max (got {backend.rate_writes})")
    for lo, hi in ((2.0, 1.0), (0.0, 2.0), (1.0, 1.0)):
        try:
            make(speed_min=lo, speed_max=hi)
            check(False, f"range {lo}-{hi} rejected")
        except ValueError:
            check(True, "")


def test_speed_creases():
    print("Speed creases")
    check(media.speed_crease_rates(0.25, 0.25, 3.0) ==
          [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75], "multiples inside the range")
    check(media.speed_crease_rates(0.3, 0.25, 1.0) == [0.3, 0.6, 0.9], "odd step, ends left out")
    layout = media.speed_creases(0.25, 0.25, 3.0)
    gap = 25.0 / 3
    check(all(abs(w - gap * media.SPEED_CREASE_WIDTH_FRAC) < 1e-9 for _, _, w, _ in layout),
          "default width: a share of the narrowest gap")
    check([pw for r, _, _, pw in layout if r == 1.0] == [70.0] and
          all(pw == 50.0 for r, _, _, pw in layout if r != 1.0), "1x pulls harder")
    wide = media.speed_creases(0.25, 0.25, 3.0, width=20)
    check(all(abs(w - gap / 2) < 1e-9 for _, _, w, _ in wide), "width capped at half the gap")

    host, layer, ext, backend = make(speed_min=0.25, speed_max=3.0, speed_increment=0.25,
                                     speed_crease_width=3, speed_crease_power=60)
    check(layer.bumps[2] is None, "no speed bumps with creases")
    check([round(p, 3) for p, _, _ in layer.creases[2]] ==
          [round(media.speed_to_pos(r), 3) for r in media.speed_crease_rates(0.25)],
          f"a crease at every 0.25x (got {layer.creases[2]})")
    check(layer.creases[2][2] == (25.0, 3.0, 84.0) and layer.creases[2][3][1:] == (3.0, 60.0),
          f"configured width and power (got {layer.creases[2][2:4]})")
    check("CREASES:3,8.3:3.00:60," in "\n".join(host._ser.lines()), "sent to the Pico")

    backend.rates["spotify"] = 1.0
    ext.poll()
    idle(host)
    # Resting a little off a crease (where the pull stopped): its multiple.
    pos = media.speed_to_pos(1.25) + 1.1
    host._handle_line(f"POS:60,0,{pos:.1f},0")
    ext.apply()
    check(backend.rate_writes == [("spotify", 1.25)], f"inside a crease: its speed (got {backend.rate_writes})")
    # Between creases the 0.05x grid still applies.
    host._handle_line(f"POS:60,0,{media.speed_to_pos(1.4):.1f},0")
    ext.apply()
    check(backend.rate_writes[-1] == ("spotify", 1.4), f"between creases: fine steps (got {backend.rate_writes})")

    for bad in ({"speed_increment": 0}, {"speed_crease_power": 0},
                {"speed_crease_width": -1}):
        try:
            make(**bad)
            check(False, f"{bad} rejected")
        except ValueError:
            check(True, "")


def test_speed_fixed():
    print("Speed fixed")
    host, layer, ext, backend = make(speed_min=0.25, speed_max=3.5)
    backend.rates["spotify"] = 2.5
    backend.fixed.add("spotify")
    ext.poll()
    idle(host)
    host._handle_line("POS:60,0,30,0")
    ext.apply()
    check(backend.rate_writes == [("spotify", 1.15)], f"tried once (got {backend.rate_writes})")
    check(host._display[2][1] == "> 1.15x fixed", f"shown as fixed (got {host._display[2]})")
    host._handle_line("POS:60,0,40,0")
    ext.apply()
    check(len(backend.rate_writes) == 1, "not tried again")
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[2] is None, "no spring back while the hand is on it")
    ext._speed_hand_t = -1e9
    ext.poll()
    check(host._pending[2] == media.speed_to_pos(2.5, 0.25, 3.5) and host._display[2][1] == "> 2.5x fixed",
          f"springs back to the player's rate (got {host._pending[2]}, {host._display[2]})")
    host._pending = [None] * 4
    ext.poll()
    check(host._pending[2] is None, "only once")


def test_speed_off():
    host, layer, ext, backend = make(speed=False)
    backend.rates["spotify"] = 1.5
    ext.poll()
    check(host._pending[2] == 0.0 and host._display[2] == ("Night Witches", "Playing"),
          f"speed false: plain play / pause (got {host._pending[2]}, {host._display[2]})")
    check(layer.bumps[2] is None, "no speed bumps")
    idle(host)
    host._handle_line("POS:60,0,62.5,0")
    ext.apply()
    check(backend.rate_writes == [], "nothing written")


def test_backend_unavailable():
    print("Backend unavailable")
    backend = FakeBackend()

    def refuse():
        raise media.BackendError("no playerctl")

    backend.open = refuse
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Media")
    ext = media.MediaExtension(layer, backend, start=False)
    ext._run()
    check(host._display[0] == ("Media", "no playerctl"), f"shown (got {host._display[0]})")


def test_worker_thread():
    print("Worker")
    backend = FakeBackend([Player("spotify", "Spotify", media.PLAYING),
                           Player("plezy", "Plezy", media.PLAYING)])
    backend.levels = {"spotify": (60.0, False), "plezy": (30.0, False)}
    host = FaderHost(port="/dev/null", num_faders=4)
    host._ser = FakeSerial()
    layer = host.add_layer("Media")
    ext = media.MediaExtension(layer, backend)
    layer.register(ext)

    def wait_for(condition):
        deadline = time.monotonic() + 3.0
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        return condition()

    check(wait_for(lambda: host._display[0] == ("Spotify", "vol 60%")), "worker polls")
    host._handle_line("BTN:F4")
    check(wait_for(lambda: backend.commands == [("spotify", "next")]),
          f"button reaches the player (got {backend.commands})")
    ext.on_page(1)
    check(wait_for(lambda: host._display[0] == ("Plezy", "vol 30%")), "page step shown")
    ext.stop()
    ext._thread.join(2.0)
    check(not ext._thread.is_alive(), "stop ends the worker")


def test_names():
    print("Names")
    for instance, want in (("spotify", "Spotify"), ("com_edde746_plezy", "Plezy"),
                           ("firefox.instance_1_60", "Firefox")):
        check(media.linux_name(instance) == want, f"{instance} -> {media.linux_name(instance)}")
    for app_id, want in (("Spotify.exe", "Spotify"), ("Chrome", "Chrome"),
                         ("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "Spotify"),
                         ("308046B0AF4A39CB", "Firefox"),
                         (r"C:\Apps\plezy.exe", "Plezy")):
        check(media.windows_name(app_id) == want, f"{app_id} -> {media.windows_name(app_id)}")


# ----- Linux backend --------------------------------------------------------

def sink_input(index, client, props, percent="65%", mute=False, corked=False):
    return {"index": index, "client": str(client), "properties": props, "mute": mute,
            "corked": corked, "volume": {"front-left": {"value_percent": percent}}}


def linux_backend():
    sep = media._SEP
    metadata = "\n".join(sep.join(fields) for fields in (
        ("spotify", "Paused", "Sabaton", "Night Witches"),
        ("com_edde746_plezy", "Playing", "Show", "Episode"),
        ("playerctld", "Playing", "Show", "Episode"),
    )) + "\n"
    sinks = [
        sink_input(60, "", {"media.name": "loopback"}),
        sink_input(812, 811, {"application.name": "spotify",
                              "application.process.id": "55601"}, "65%", corked=True),
        sink_input(834, 833, {"application.name": "mpv"}, "40%"),
        sink_input(900, 899, {"application.name": "other"}, "10%"),
    ]
    clients = [{"index": 833, "properties": {"application.name": "plezy",
                                             "application.process.id": "65517"}},
               {"index": 899, "properties": {}}]
    pids = {"spotify": "55601", "com_edde746_plezy": "65517"}
    rates = {"com_edde746_plezy": "d 1.5\n", "firefox": "d 2.5\n", "spotify": "garbage\n"}
    calls = []

    def output(*argv):
        calls.append(argv)
        if argv[0] == media.PLAYERCTL and "metadata" in argv:
            return metadata
        if argv[0] == media.BUSCTL and argv[2] == "get-property":
            return rates.get(argv[3].removeprefix(media._MPRIS_PREFIX))
        if argv[0] == media.BUSCTL and argv[2] == "set-property":
            return None if "firefox" in argv[3] else ""
        if argv[0] == media.BUSCTL:
            return f"u {pids[argv[-1].removeprefix(media._MPRIS_PREFIX)]}\n"
        if argv[:4] == (media.PACTL, "-f", "json", "list"):
            return json.dumps(sinks if argv[4] == "sink-inputs" else clients)
        return ""

    backend = media.LinuxBackend()
    backend._output = output
    return backend, calls, sinks


def test_linux_backend():
    print("Linux backend")
    backend, calls, sinks = linux_backend()
    players = backend.players()
    check([p.id for p in players] == ["spotify", "com_edde746_plezy"], "playerctld skipped")
    check(players[0].title == "Night Witches" and players[0].artist == "Sabaton"
          and players[0].status == "Paused", f"fields (got {players[0]})")

    check(backend.audio("spotify") == (65.0, False), "stream found by pid")
    check(backend.audio("com_edde746_plezy") == (40.0, False),
          "stream found by its client's pid")
    backend.set_volume("com_edde746_plezy", 33.3)
    check((media.PACTL, "set-sink-input-volume", "834", "33.3%") in calls, "only its stream set")
    backend.set_mute("spotify", True)
    check((media.PACTL, "set-sink-input-mute", "812", "1") in calls, "mute")
    backend.command("spotify", "play_pause")
    check((media.PLAYERCTL, "-p", "spotify", "play-pause") in calls, "play-pause")

    backend._pids["spotify"] = "1"
    sinks[1]["properties"]["application.process.id"] = "2"
    check(backend.audio("spotify") == (65.0, False), "name match when pids differ")

    del sinks[1]
    check(backend.audio("spotify") == (None, False), "no stream")

    check(backend.rate("com_edde746_plezy") == 1.5, "rate read")
    check(backend.rate("spotify") is None and backend.rate("gone") is None, "no rate")
    check(backend.set_rate("com_edde746_plezy", 1.25) is True, "rate set")
    check((media.BUSCTL, "--user", "set-property", "org.mpris.MediaPlayer2.com_edde746_plezy",
           "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "Rate", "d", "1.25")
          in calls, "set through busctl")
    check(backend.set_rate("firefox", 1.25) is False, "refused")


def test_linux_backend_failures():
    backend = media.LinuxBackend()
    backend._output = lambda *argv: None
    check(backend.players() == [], "playerctl failing or no players")
    check(backend.audio("spotify") == (None, False), "pactl failing")
    backend._output = lambda *argv: "not json"
    check(backend._pactl_list("clients") == [], "bad JSON")


# ----- Windows backend, stand-ins for WinRT / pycaw --------------------------

class Op:
    """Awaitable like an IAsyncOperation."""

    def __init__(self, value=None):
        self.value = value

    def __await__(self):
        return self.value
        yield


class FakeSession:
    def __init__(self, app_id, status, title):
        self.source_app_user_model_id = app_id
        self.status = status
        self.title = title
        self.calls = []
        self.rate = None
        self.rate_enabled = False

    def get_playback_info(self):
        return types.SimpleNamespace(
            playback_status=self.status, playback_rate=self.rate,
            controls=types.SimpleNamespace(is_playback_rate_enabled=self.rate_enabled))

    def try_change_playback_rate_async(self, rate):
        self.calls.append(("rate", rate))
        self.rate = rate
        return Op(True)

    def try_get_media_properties_async(self):
        return Op(types.SimpleNamespace(title=self.title, artist="Artist"))

    def try_toggle_play_pause_async(self):
        self.calls.append("toggle")
        return Op(True)

    def try_skip_next_async(self):
        self.calls.append("next")
        return Op(True)

    def try_skip_previous_async(self):
        self.calls.append("previous")
        return Op(True)


class FakeSimpleVolume:
    def __init__(self, level):
        self.level = level
        self.muted = 0

    def GetMasterVolume(self):
        return self.level

    def SetMasterVolume(self, level, context):
        self.level = level

    def GetMute(self):
        return self.muted

    def SetMute(self, muted, context):
        self.muted = muted


def install_windows_stubs(sessions, audio):
    manager = types.SimpleNamespace(get_sessions=lambda: sessions)
    control = types.ModuleType("winrt.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = types.SimpleNamespace(
        request_async=lambda: Op(manager))
    pycaw_inner = types.ModuleType("pycaw.pycaw")
    pycaw_inner.AudioUtilities = types.SimpleNamespace(GetAllSessions=lambda: audio)
    modules = {
        "winrt": types.ModuleType("winrt"),
        "winrt.windows": types.ModuleType("winrt.windows"),
        "winrt.windows.foundation": types.ModuleType("winrt.windows.foundation"),
        "winrt.windows.foundation.collections": types.ModuleType(
            "winrt.windows.foundation.collections"),
        "winrt.windows.media": types.ModuleType("winrt.windows.media"),
        "winrt.windows.media.control": control,
        "pycaw": types.ModuleType("pycaw"),
        "pycaw.pycaw": pycaw_inner,
    }
    sys.modules.update(modules)
    return list(modules)


def test_windows_backend():
    print("Windows backend")
    spotify = FakeSession("Spotify.exe", 4, "Night Witches")
    chrome = FakeSession("Chrome", 5, "Video")
    volume = FakeSimpleVolume(0.5)
    process = types.SimpleNamespace(name=lambda: "Spotify.exe")
    audio = [types.SimpleNamespace(Process=None, SimpleAudioVolume=None),
             types.SimpleNamespace(Process=process, SimpleAudioVolume=volume)]
    installed = install_windows_stubs([spotify, chrome], audio)
    try:
        backend = media.WindowsBackend()
        backend.open()
        players = backend.players()
        check([(p.name, p.status, p.title) for p in players]
              == [("Spotify", "Playing", "Night Witches"), ("Chrome", "Paused", "Video")],
              f"sessions listed (got {players})")
        check(backend.audio("Spotify.exe") == (50.0, False), "audio session matched by exe")
        check(backend.audio("Chrome") == (None, False), "no audio session")
        backend.set_volume("Spotify.exe", 25.0)
        check(volume.level == 0.25, "volume written as a scalar")
        backend.set_mute("Spotify.exe", True)
        check(volume.muted == 1, "mute written")
        for action in ("play_pause", "next", "previous"):
            backend.command("Spotify.exe", action)
        check(spotify.calls == ["toggle", "next", "previous"], f"commands (got {spotify.calls})")
        backend.command("Gone.exe", "next")

        spotify.calls.clear()
        check(backend.rate("Spotify.exe") is None, "no rate")
        check(backend.set_rate("Spotify.exe", 1.5) is False and spotify.calls == [],
              "rate change not enabled: refused")
        spotify.rate, spotify.rate_enabled = 1.0, True
        check(backend.rate("Spotify.exe") == 1.0, "rate read")
        check(backend.set_rate("Spotify.exe", 1.5) is True and spotify.rate == 1.5, "rate set")
        check(backend.set_rate("Gone.exe", 1.5) is False, "unknown session")
    finally:
        for name in installed:
            sys.modules.pop(name, None)


def test_windows_missing_deps():
    backend = media.WindowsBackend()
    # sys.modules[name] = None makes the import raise ImportError.
    sys.modules["pycaw"] = None
    try:
        backend.open()
        check(False, "missing packages reported")
    except media.BackendError as e:
        check(str(e) == "deps missing", f"short message (got {e})")
    finally:
        sys.modules.pop("pycaw", None)


def main():
    tests = [
        test_layout_and_screens,
        test_non_ascii_title_reaches_the_screen,
        test_selection,
        test_pages,
        test_buttons,
        test_button_without_player,
        test_fader_writes_volume,
        test_app_changes_move_the_fader,
        test_mute_from_button,
        test_no_stream,
        test_reconnect_and_custom_layout,
        test_speed_mapping,
        test_speed,
        test_speed_range,
        test_speed_creases,
        test_speed_fixed,
        test_speed_off,
        test_backend_unavailable,
        test_worker_thread,
        test_names,
        test_linux_backend,
        test_linux_backend_failures,
        test_windows_backend,
        test_windows_missing_deps,
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
