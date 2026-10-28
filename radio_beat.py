#!/usr/bin/env python3
"""Rádio Beat player: GTK4 / libadwaita window, mpv plays the stream.

mpv runs as a child process controlled over its JSON IPC socket; its MPRIS
plugin (mpv-mpris) gives media keys and the bar's player widget for free.
The stream's ICY title ("ARTIST - SONG") is looked up on Deezer (then
iTunes) for the album, proper spelling and the cover.

It starts playing right away. Closing the window keeps it playing, with an
icon in the bar's tray (StatusNotifierItem) that shows the window again;
Stop quits. Started at login (--autostart) it plays with only the tray icon.
"""

import ctypes
import json
import os
import shutil
import signal
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Graphene, Gtk  # noqa: E402

APP_ID = "local.hanis.RadioBeat"
HERE = os.path.dirname(os.path.realpath(__file__))
CONFIG = os.path.join(GLib.get_user_config_dir(), "radio-beat")
SETTINGS = os.path.join(CONFIG, "settings.json")
AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart", f"{APP_ID}.desktop")
UA = "RadioBeat-Linux/1.0"

# (id, label, url). Radio Beat has no FLAC stream today (checked 2026-10-25);
# FLAC_CANDIDATES are probed at start and added on top if one ever answers.
STREAMS = [
    ("aac128", "AAC 128 kbps", "https://icecast2.play.cz/beat128aac"),
    ("mp3128", "MP3 128 kbps", "https://icecast2.play.cz/radiobeat128.mp3"),
    ("mp3backup", "MP3 128 kbps (backup server)", "https://icecast1.play.cz/beat64.mp3"),
    ("aac64", "AAC 64 kbps (radiobeat.cz)", "https://stream.rcs.revma.com/3d47nqvb938uv"),
]
FLAC_CANDIDATES = [
    "https://icecast2.play.cz/beatflac",
    "https://icecast2.play.cz/beat.flac",
    "https://icecast2.play.cz/radiobeat.flac",
    "https://icecast1.play.cz/beatflac",
]


def load_settings() -> dict:
    try:
        with open(SETTINGS) as f:
            return {"quality": "aac128", "volume": 80, **json.load(f)}
    except (OSError, ValueError):
        return {"quality": "aac128", "volume": 80}


def save_settings(s: dict) -> None:
    os.makedirs(CONFIG, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, SETTINGS)


def probe_flac() -> str | None:
    for url in FLAC_CANDIDATES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=4) as r:
                kind = r.headers.get("Content-Type", "")
                if "flac" in kind or "ogg" in kind:
                    return url
        except (OSError, ValueError):
            continue
    return None


# --- autostart ----------------------------------------------------------------

def launcher() -> str:
    exe = os.path.expanduser("~/.local/bin/radio-beat")
    return exe if os.path.exists(exe) else f"{sys.executable} {os.path.realpath(__file__)}"


def autostart_on() -> bool:
    return os.path.exists(AUTOSTART)


def set_autostart(on: bool) -> None:
    if not on:
        try:
            os.remove(AUTOSTART)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    with open(AUTOSTART, "w") as f:
        f.write(
            "[Desktop Entry]\nType=Application\nName=Rádio Beat\n"
            f"Exec={launcher()} --autostart\nIcon={APP_ID}\n"
            "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
        )


# --- KDE: open in the bottom-right corner ---------------------------------------
# A Wayland window can't place itself, and KWin's window rules have no "bottom
# right" placement. A small KWin script does it: each time the window appears,
# it goes flush against the right edge, its bottom on the taskbar (KWin's
# MaximizeArea is the screen without the panels). The app loads the script
# into KWin over D-Bus at start (Plasma 6, and 5's clientAdded).
# The earlier "under the mouse" rule (KWIN_RULE) is taken out again.

KWIN_SCRIPT_NAME = "radio-beat-corner"
KWIN_SCRIPT = """
function place(w) {
    if (!w || w.resourceClass !== "%s") return;
    var a = workspace.clientArea(KWin.MaximizeArea, w);
    var g = w.frameGeometry;
    w.frameGeometry = {x: a.x + a.width - g.width, y: a.y + a.height - g.height, width: g.width, height: g.height};
}
(workspace.windowAdded || workspace.clientAdded).connect(place);
""" % APP_ID


def _kwin_scripting(method: str, args=None, reply: str | None = None):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    return bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method, args,
                         GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE, 5000, None)


def kwin_corner(on: bool) -> None:
    """Loads (or unloads) the bottom-right placement script into the running KWin."""
    try:
        loaded = _kwin_scripting("isScriptLoaded", GLib.Variant("(s)", (KWIN_SCRIPT_NAME,)), "(b)").unpack()[0]
        if loaded:
            _kwin_scripting("unloadScript", GLib.Variant("(s)", (KWIN_SCRIPT_NAME,)), "(b)")
        if on:
            os.makedirs(CONFIG, exist_ok=True)
            path = os.path.join(CONFIG, "kwin-corner.js")
            with open(path, "w") as f:
                f.write(KWIN_SCRIPT)
            _kwin_scripting("loadScript", GLib.Variant("(ss)", (path, KWIN_SCRIPT_NAME)), "(i)")
            _kwin_scripting("start")
    except GLib.Error as e:
        print(f"radio-beat: KWin script: {e.message}", file=sys.stderr)


KWIN_RULE = "radio-beat-placement"
KWIN_UNDER_MOUSE, KWIN_FORCE = 7, 2  # Placement::UnderMouse, Rules::Force


def is_kde() -> bool:
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and bool(shutil.which("kwriteconfig6"))


def _kcfg(*args: str) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True)
    return r.stdout.strip()


def kwin_rule(on: bool) -> None:
    rules = [x for x in _kcfg("kreadconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "rules").split(",") if x]
    if not on and KWIN_RULE not in rules:
        return  # never added: leave KWin's rules alone
    w = lambda key, val, group=KWIN_RULE: subprocess.run(
        ["kwriteconfig6", "--file", "kwinrulesrc", "--group", group, "--key", key, str(val)], capture_output=True)
    if on:
        for key, val in (("Description", "Rádio Beat: open next to the tray"), ("wmclass", APP_ID),
                         ("wmclassmatch", 1), ("wmclasscomplete", "false"),
                         ("placement", KWIN_UNDER_MOUSE), ("placementrule", KWIN_FORCE)):
            w(key, val)
        if KWIN_RULE not in rules:
            rules.append(KWIN_RULE)
    else:
        subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", KWIN_RULE, "--key", "placement", "--delete"],
                       capture_output=True)
        rules = [x for x in rules if x != KWIN_RULE]
    w("rules", ",".join(rules), "General")
    w("count", len(rules), "General")
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION).call_sync("org.kde.KWin", "/KWin", "org.kde.KWin", "reconfigure",
                                                        None, None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        pass


# --- updates ----------------------------------------------------------------------

REPO = "kupecjan1980-pixel/radio-beat"
RAW = f"https://raw.githubusercontent.com/{REPO}/master"


LATEST: dict = {}  # the newest commit seen by update_status()


def update_status() -> tuple[bool | None, str]:
    """(newer version on GitHub?, text); None when it couldn't be checked.
    Compares the installed commit (VERSION, written by install.sh) with GitHub's newest."""
    try:
        c = _json(f"https://api.github.com/repos/{REPO}/commits/master")
        sha, info = c["sha"], c["commit"]
        LATEST["sha"] = sha
    except (OSError, ValueError, KeyError):
        return None, "Couldn't reach GitHub"
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            mine = f.read().strip()
    except OSError:
        mine = ""
    if mine == sha:
        return False, f"Up to date ({info['committer']['date'][:10]})"
    return True, f"New version from {info['committer']['date'][:10]}:\n{info['message'].splitlines()[0]}"


def run_update() -> str | None:
    """Installs the GitHub version (the same get.sh as the first install); error text or None."""
    # get.sh of that very commit: raw.githubusercontent.com serves master's files
    # from a cache that lags a few minutes behind.
    sha = LATEST.get("sha", "")
    src = f"https://raw.githubusercontent.com/{REPO}/{sha}/get.sh" if sha else f"{RAW}/get.sh"
    r = subprocess.run(["bash", "-c", f"curl -fsSL {src} | RADIO_BEAT_SHA={sha} bash"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or ["update failed"])[-1]
    return None


# --- song lookup ----------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def first_artist(artist: str) -> str:
    return re.split(r"\s+(?:&|feat\.?|ft\.?|x|a|and)\s+|,|/", artist, flags=re.I)[0].strip()


def split_title(icy: str) -> tuple[str, str] | None:
    if " - " not in icy:
        return None
    artist, song = icy.split(" - ", 1)
    return (artist.strip(), song.strip()) if artist.strip() and song.strip() else None


def _json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def _score(artist: str, song: str, cand_artist: str, cand_title: str) -> int:
    a, t = norm(first_artist(artist)), norm(song)
    ca, ct = norm(cand_artist), norm(cand_title)
    if not ca or (a not in ca and ca not in a):
        return -1
    score = 2 if ca == a else 1
    score += 3 if ct == t else (1 if t in ct or ct in t else -2)
    if "live" in cand_title.lower() and "live" not in song.lower():
        score -= 1
    return score


def lookup(artist: str, song: str) -> dict | None:
    """{artist, title, album, cover} from Deezer, else iTunes; None if unsure."""
    best, best_score = None, 0
    q = urllib.parse.quote(f"{first_artist(artist)} {song}")
    try:
        for x in _json(f"https://api.deezer.com/search?q={q}&limit=10").get("data", []):
            sc = _score(artist, song, x["artist"]["name"], x["title"])
            if sc > best_score:
                best_score = sc
                best = {"artist": x["artist"]["name"], "title": x["title"],
                        "album": x["album"]["title"], "cover": (x["album"].get("cover_xl") or "").replace("/1000x1000-", "/1200x1200-") or None}
    except (OSError, ValueError, KeyError):
        pass
    if best_score >= 4:
        return best
    try:
        for x in _json(f"https://itunes.apple.com/search?term={q}&entity=song&limit=10").get("results", []):
            sc = _score(artist, song, x["artistName"], x["trackName"])
            if sc > best_score:
                best_score = sc
                art = x.get("artworkUrl100", "")
                best = {"artist": x["artistName"], "title": x["trackName"],
                        "album": x.get("collectionName"), "cover": art.replace("100x100bb", "1920x1920bb") or None}
    except (OSError, ValueError, KeyError):
        pass
    return best if best_score >= 3 else None


def fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()
    except (OSError, ValueError):
        return None


# --- programme (radiobeat.cz) ----------------------------------------------------
# The station's site lists the week (Monday first) at /cs/program/ and the
# host on air on the homepage; there's no public API, so both are read from
# the HTML.

SITE = "https://www.radiobeat.cz"
DAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]


def _text(html: str) -> str:
    import html as h
    return " ".join(h.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def parse_schedule(page: str) -> dict[int, list[dict]]:
    """weekday (0 = Monday) -> shows [{start (minutes), title, desc, img}]."""
    out: dict[int, list[dict]] = {}
    days = page.split('class="schedule__day')[1:]
    for chunk in days:
        m = re.search(r'ca-control">\s*<a[^>]*>\s*([^<\s]+)', chunk)
        name = m.group(1).strip().lower() if m else ""
        if name not in DAYS:
            continue
        shows = []
        for item in chunk.split('schedule__list-item')[1:]:
            t = re.search(r'schedule__title">(.*?)</h3>', item, re.S)
            m = re.match(r"(\d{1,2}):(\d{2})\s*(.*)", _text(t.group(1))) if t else None
            if not m:
                continue
            d = re.search(r'schedule__description">(.*?)</p>', item, re.S)
            img = re.search(r'data-src="([^"]+)"', item)
            shows.append({"start": int(m.group(1)) * 60 + int(m.group(2)), "title": m.group(3),
                          "desc": _text(d.group(1)) if d else "",
                          "img": SITE + img.group(1).replace("/media/200/", "/media/400/") if img else None})
        out[DAYS.index(name)] = sorted(shows, key=lambda x: x["start"])
    return out


def parse_host(page: str) -> str | None:
    m = re.search(r'id="interpeteur-name">(.*?)</span>', page, re.S)
    return _text(m.group(1)) or None if m else None


def on_air(schedule: dict[int, list[dict]], now: time.struct_time) -> tuple[dict | None, dict | None]:
    """The show on now and the next one (with "day" for a later day)."""
    if not schedule:
        return None, None
    mins = now.tm_hour * 60 + now.tm_min
    cur = nxt = None
    for back in range(8):  # now: the last start at or before now, today or earlier
        shows = schedule.get((now.tm_wday - back) % 7) or []
        before = [s for s in shows if back or s["start"] <= mins]
        if before:
            cur = before[-1]
            break
    for ahead in range(8):
        day = (now.tm_wday + ahead) % 7
        later = [s for s in schedule.get(day) or [] if ahead or s["start"] > mins]
        if later:
            nxt = dict(later[0], day=None if ahead == 0 else ("zítra" if ahead == 1 else DAYS[day]))
            break
    return cur, nxt


# --- mpv ------------------------------------------------------------------------

def _die_with_parent() -> None:
    # PR_SET_PDEATHSIG: mpv stops even if the app is killed instead of quit.
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)


FLATPAK_MPV = "io.mpv.Mpv"


def mpv_command(sock_dir: str) -> list[str] | None:
    """mpv on the system, else the Flathub one (Bazzite and other atomic
    systems); its sandbox gets the folder with the control socket."""
    if shutil.which("mpv"):
        return ["mpv"]
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", FLATPAK_MPV], capture_output=True).returncode == 0:
        run = os.environ.get("XDG_RUNTIME_DIR", "")
        share = f"xdg-run/{os.path.relpath(sock_dir, run)}" if run and sock_dir.startswith(run + "/") else sock_dir
        return ["flatpak", "run", f"--filesystem={share}", FLATPAK_MPV]
    return None


class Mpv:
    """mpv child process over JSON IPC; property changes arrive on `on_prop`
    ("mpv-exit" when mpv goes away)."""

    PROPS = ("metadata", "audio-codec-name", "audio-bitrate", "idle-active", "playback-time", "paused-for-cache")

    def __init__(self, on_prop):
        self.on_prop = on_prop
        run = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        sock_dir = os.path.join(run, "radio-beat")
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
        self.sock_path = os.path.join(sock_dir, f"mpv-{os.getpid()}.sock")
        try:
            os.remove(self.sock_path)
        except FileNotFoundError:
            pass
        cmd = mpv_command(sock_dir)
        if not cmd:
            raise RuntimeError("mpv isn't installed (system package, or Flathub: flatpak install flathub io.mpv.Mpv)")
        self.proc = subprocess.Popen(
            # --load-scripts=no: no MPRIS plugin, the app shows itself as the player (Mpris)
            [*cmd, "--idle=yes", "--no-video", "--load-scripts=no", "--no-terminal", "--force-window=no",
             "--title=Rádio Beat", f"--input-ipc-server={self.sock_path}",
             "--cache=yes", "--demuxer-max-bytes=4MiB", "--network-timeout=15",
             # let ffmpeg pick a dropped HTTP stream up again by itself
             "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=5",
             *os.environ.get("RADIO_BEAT_MPV_ARGS", "").split()],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        for _ in range(300):  # a Flatpak mpv takes a few seconds the first time
            if os.path.exists(self.sock_path) or self.proc.poll() is not None:
                break
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        for i, prop in enumerate(self.PROPS):
            self.cmd("observe_property", i + 1, prop)

    def cmd(self, *args) -> None:
        data = json.dumps({"command": list(args)}).encode() + b"\n"
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def _reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                GLib.idle_add(self.on_prop, "mpv-exit", None)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "property-change":
                    GLib.idle_add(self.on_prop, msg["name"], msg.get("data"))

    def play(self, url: str) -> None:
        self.cmd("loadfile", url, "replace")

    def stop(self) -> None:
        self.cmd("stop")

    def pause(self, on: bool) -> None:
        self.cmd("set_property", "pause", on)

    def volume(self, v: float) -> None:
        self.cmd("set_property", "volume", v)

    def quit(self) -> None:
        self.cmd("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- tray icon (StatusNotifierItem) ----------------------------------------------

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewToolTip"/>
    <signal name="NewTitle"/>
  </interface>
</node>
"""


MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" type="x"/></method>
    <method name="SetPosition"><arg direction="in" type="o"/><arg direction="in" type="x"/></method>
    <method name="OpenUri"><arg direction="in" type="s"/></method>
    <signal name="Seeked"><arg type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class Mpris:
    """The app's own media controls (MPRIS): the desktop's player widget and
    media keys see "Rádio Beat" with the song and cover, instead of mpv
    (whose MPRIS plugin isn't loaded)."""

    PATH = "/org/mpris/MediaPlayer2"
    ROOT, PLAYER = "org.mpris.MediaPlayer2", "org.mpris.MediaPlayer2.Player"

    def __init__(self, app):
        self.app = app
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.cover_file = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                                       "radio-beat", "cover.jpg")
        self.last: dict = {}
        node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        for iface in node.interfaces:
            self.bus.register_object(self.PATH, iface, self._call, self._get, self._set)
        Gio.bus_own_name_on_connection(self.bus, "org.mpris.MediaPlayer2.radiobeat",
                                       Gio.BusNameOwnerFlags.NONE, None, None)

    def _metadata(self) -> dict:
        a = self.app
        info = a.info or {}
        md = {"mpris:trackid": GLib.Variant("o", "/local/hanis/RadioBeat/live"),
              "xesam:title": GLib.Variant("s", info.get("title") or a.song or "Rádio Beat"),
              "xesam:artist": GLib.Variant("as", [info.get("artist") or a.artist_raw or "Rádio Beat"])}
        if info.get("album"):
            md["xesam:album"] = GLib.Variant("s", info["album"])
        art = info.get("cover") if a.cover_tex else None
        if art:
            md["mpris:artUrl"] = GLib.Variant("s", art)
        return md

    def _props(self) -> dict:
        a = self.app
        return {
            self.ROOT: {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "Rádio Beat"),
                "DesktopEntry": GLib.Variant("s", APP_ID),
                "SupportedUriSchemes": GLib.Variant("as", []), "SupportedMimeTypes": GLib.Variant("as", []),
            },
            self.PLAYER: {
                "PlaybackStatus": GLib.Variant("s", "Paused" if a.paused else "Playing"),
                "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
                "MaximumRate": GLib.Variant("d", 1.0),
                "Metadata": GLib.Variant("a{sv}", self._metadata()),
                "Volume": GLib.Variant("d", a.settings["volume"] / 100),
                "Position": GLib.Variant("x", 0),
                "CanGoNext": GLib.Variant("b", False), "CanGoPrevious": GLib.Variant("b", False),
                "CanPlay": GLib.Variant("b", True), "CanPause": GLib.Variant("b", True),
                "CanSeek": GLib.Variant("b", False), "CanControl": GLib.Variant("b", True),
            },
        }

    def _get(self, _conn, _sender, _path, iface, prop):
        return self._props().get(iface, {}).get(prop)

    def _set(self, _conn, _sender, _path, iface, prop, value):
        if iface == self.PLAYER and prop == "Volume":
            self.app.set_volume(max(0.0, min(1.0, value.unpack())) * 100)
        return True

    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        a = self.app
        if method == "Raise":
            a.show_window()
        elif method == "Quit":
            a.quit_all()
        elif method == "PlayPause" or (method == "Play" and a.paused) or (method in ("Pause", "Stop") and not a.paused):
            a.toggle_pause()
        invocation.return_value(None)

    def changed(self) -> None:
        """PropertiesChanged for what's different since the last call."""
        now = self._props()[self.PLAYER]
        diff = {k: v for k, v in now.items() if k in ("PlaybackStatus", "Metadata", "Volume")
                and (k not in self.last or self.last[k] != v)}
        self.last = now
        if diff:
            self.bus.emit_signal(None, self.PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                 GLib.Variant("(sa{sv}as)", (self.PLAYER, diff, [])))


class Tray:
    """Tray icon: click shows / hides the window, middle click pauses,
    scrolling changes the volume. Registers again when the bar restarts."""

    PATH = "/StatusNotifierItem"

    def __init__(self, app):
        self.app = app
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.tooltip = "Rádio Beat"
        node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.bus.register_object(self.PATH, node.interfaces[0], self._call, self._get, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self.pixmap = self._pixmap()
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                         Gio.BusNameWatcherFlags.NONE, self._register, None)

    def _pixmap(self):
        from gi.repository import GdkPixbuf
        out = []
        for size in (32, 64):
            p = os.path.join(HERE, "icons", "hicolor", f"{size}x{size}" if size != 32 else "48x48", "apps", f"{APP_ID}.png")
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(p, size, size).add_alpha(False, 0, 0, 0)
            except GLib.Error:
                continue
            px, w, h, stride = pb.get_pixels(), pb.get_width(), pb.get_height(), pb.get_rowstride()
            argb = bytearray()
            for y in range(h):
                row = px[y * stride: y * stride + w * 4]
                for x in range(0, w * 4, 4):
                    r, g, b, a = row[x: x + 4]
                    argb += bytes((a, r, g, b))  # ARGB32, network byte order
            out.append((w, h, bytes(argb)))
        return out

    def _register(self, *_):
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                      "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None,
                      Gio.DBusCallFlags.NONE, -1, None, None)

    def _get(self, _conn, _sender, _path, _iface, prop):
        v = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "radio-beat"),
            "Title": GLib.Variant("s", "Rádio Beat"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", APP_ID),
            "IconThemePath": GLib.Variant("s", os.path.join(HERE, "icons")),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmap),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, [], "Rádio Beat", self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
        }
        return v.get(prop)

    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Activate":
            self.app.toggle_window()
        elif method in ("SecondaryActivate", "ContextMenu"):
            self.app.toggle_pause()
        elif method == "Scroll":
            delta, orient = params.unpack()
            if orient == "vertical":
                self.app.nudge_volume(-5 if delta > 0 else 5)
        invocation.return_value(None)

    def set_tooltip(self, text: str) -> None:
        if text != self.tooltip:
            self.tooltip = text
            self.bus.emit_signal(None, self.PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)


# --- picture enhancing (AI upscaling, sharpening, denoising) -----------------------
# A cover (or the logo) that full screen would stretch by more than
# UPSCALE_FROM is enhanced in steps, each shown as soon as it's done:
# 1. upscaled: Real-ESRGAN (ncnn) on the GPU through Vulkan, so any GPU or iGPU
#    with a Vulkan driver (AMD, NVIDIA, Intel Arc); without one, Lanczos on the CPU;
# 2. sharpened: CAS (contrast adaptive sharpening) on the upscaled picture;
# 3. denoised: NL-means (on the GPU when ffmpeg can, else the CPU), then CAS
#    again: the final picture (denoising before sharpening, as it should be).
# All local. Real-ESRGAN and its model (47 MB download) go into
# ~/.local/share/radio-beat by themselves, ffmpeg too (42 MB) when the system
# has none; downloads are checked against their checksums. Everything keeps
# itself working: files checked at every start (downloaded again if missing or
# damaged, retried while offline), the GPU tested with a tiny picture (smaller
# tiles if it times out, remembered), damaged cache files cleared.

ESR_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
ESR_SHA256 = "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96"
ESR_MODEL = "realesrgan-x4plus"
FFMPEG_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "radio-beat")
UPSCALED_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "radio-beat", "upscaled")
UPSCALE_FROM = 1.05
UPSCALED_KEEP = 300  # cached pictures
SHARPEN = "cas=strength=0.5"
DENOISE = "nlmeans=s=1.0:p=7:r=15"
DENOISE_VK = "format=rgba,hwupload,nlmeans_vulkan=s=1.0:p=7:r=15,hwdownload,format=rgba"
SOFTWARE_GPU = re.compile(r"llvmpipe|lavapipe|swiftshader", re.I)
DISCRETE_GPU = re.compile(r"nvidia|geforce|rtx|gtx|quadro|radeon (rx|pro)|\bDG2\b|\bBMG\b|arc\(tm\) [ab]\d", re.I)


def _secs(t: float) -> str:
    return f"{t:.1f} s" if t < 10 else f"{t:.0f} s"


def _download(url: str, timeout: int = 120) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (OSError, ValueError) as e:
        print(f"enhancer: download of {url} failed: {e}", file=sys.stderr)
        return None


class Enhancer:
    MODEL_SIZES = {"param": 116029, "bin": 33424520}  # realesrgan-x4plus as unpacked
    RETRY = (60, 300, 1800)  # s between download tries, the last one repeats
    TILES = (128, 64, 32)

    def __init__(self, on_status=None, on_picture=None):
        self.dir = os.path.join(DATA_DIR, "realesrgan")
        self.bin = os.path.join(self.dir, "realesrgan-ncnn-vulkan")
        self.models = os.path.join(self.dir, "models")
        self.tile_file = os.path.join(self.dir, "tile")
        self.own_ffmpeg = os.path.join(DATA_DIR, "ffmpeg", "ffmpeg")
        self.ffmpeg = shutil.which("ffmpeg")
        self.ai = None  # True: GPU found and tested; False: no usable GPU; None: not known yet
        self.gpu: str | None = None
        self.gpu_index: int | None = None
        self.vk_denoise = False
        self.tile = self._load_tile()
        self.status = "Starting…"
        self.on_status, self.on_picture = on_status, on_picture
        self.jobs: dict[str, tuple] = {}  # kind ("logo" / "cover") -> job
        self.running: tuple | None = None
        self.proc: subprocess.Popen | None = None
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- status ------------------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.status = text
        print(f"enhancer: {text}", file=sys.stderr)
        if self.on_status:
            GLib.idle_add(lambda: (self.on_status(text), False)[1])

    def _say_ready(self) -> None:
        up = (f"AI upscaling on {self.gpu}" if self.ai
              else "No GPU for AI upscaling here (no Vulkan driver), so smooth upscaling on the CPU")
        if self.ffmpeg:
            where = "the GPU" if self.vk_denoise else "the CPU"
            self._say(f"On. {up}; sharpening on the CPU; denoising on {where}.")
        else:
            self._say(f"On. {up}; sharpening and denoising wait for ffmpeg (downloading).")

    def _picture(self, text: str) -> None:
        kind = self.running[0] if self.running else "cover"
        if self.on_picture:
            GLib.idle_add(lambda: (self.on_picture(text, kind), False)[1])

    # --- files ---------------------------------------------------------------------------

    def _load_tile(self) -> int:
        try:
            with open(self.tile_file) as f:
                t = int(f.read().strip())
            return t if t in self.TILES else self.TILES[0]
        except (OSError, ValueError):
            return self.TILES[0]

    def _save_tile(self) -> None:
        try:
            with open(self.tile_file, "w") as f:
                f.write(str(self.tile))
        except OSError:
            pass

    def _esr_ok(self) -> bool:
        try:
            return (os.access(self.bin, os.X_OK)
                    and all(os.path.getsize(os.path.join(self.models, f"{ESR_MODEL}.{ext}")) == n
                            for ext, n in self.MODEL_SIZES.items()))
        except OSError:
            return False

    def _ffmpeg_ok(self) -> bool:
        self.ffmpeg = shutil.which("ffmpeg") or (self.own_ffmpeg if os.access(self.own_ffmpeg, os.X_OK) else None)
        return self.ffmpeg is not None

    def maintain(self, then=None) -> None:
        """Clean up, install what's missing (retrying while offline), test the
        GPU. then() runs on the main thread whenever enhancing can do more."""
        self._clean()
        tries = 0
        while True:
            need = [n for n, ok in (("Real-ESRGAN (47 MB)", self._esr_ok()), ("ffmpeg (42 MB)", self._ffmpeg_ok())) if not ok]
            if self.ai is None and self._esr_ok():
                self._self_test()
                self._say_ready()
                if then:
                    GLib.idle_add(lambda: (then(), False)[1])
            if not need:
                break
            self._say(f"Downloading {' and '.join(need)}…")
            got = (self._esr_ok() or self._install_esr()) and (self._ffmpeg_ok() or self._install_ffmpeg())
            if got:
                continue
            wait = self.RETRY[min(tries, len(self.RETRY) - 1)]
            tries += 1
            self._say(f"Download failed; trying again in {wait // 60} min (pictures shown as they are until then)")
            time.sleep(wait)
        self._test_vk_denoise()
        self._say_ready()
        if then:
            GLib.idle_add(lambda: (then(), False)[1])

    def _clean(self) -> None:
        """Damaged or half-written cache files, and work folders left by a crash."""
        try:
            for f in os.listdir(UPSCALED_DIR):
                p = os.path.join(UPSCALED_DIR, f)
                if f.endswith(".part") or (f.endswith(".jpg") and not self._jpeg_ok(p)):
                    os.remove(p)
        except OSError:
            pass
        tmp = tempfile.gettempdir()
        try:
            for f in os.listdir(tmp):
                if f.startswith("radio-beat-esr-"):
                    shutil.rmtree(os.path.join(tmp, f), ignore_errors=True)
        except OSError:
            pass
        for d in (self.dir + ".part", os.path.dirname(self.own_ffmpeg) + ".part"):
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _jpeg_ok(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                head = f.read(2)
                f.seek(-2, os.SEEK_END)
                return head == b"\xff\xd8" and f.read(2) == b"\xff\xd9"
        except OSError:
            return False

    def _install_esr(self) -> bool:
        import hashlib
        import io
        import zipfile
        blob = _download(ESR_URL)
        if blob is None:
            return False
        if hashlib.sha256(blob).hexdigest() != ESR_SHA256:
            print("enhancer: Real-ESRGAN download doesn't match its checksum, not used", file=sys.stderr)
            return False
        want = {"realesrgan-ncnn-vulkan", f"models/{ESR_MODEL}.param", f"models/{ESR_MODEL}.bin"}
        tmp = self.dir + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    if name in want:
                        dest = os.path.join(tmp, name)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with z.open(name) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
            os.chmod(os.path.join(tmp, "realesrgan-ncnn-vulkan"), 0o755)
            shutil.rmtree(self.dir, ignore_errors=True)
            os.replace(tmp, self.dir)
        except (OSError, zipfile.BadZipFile) as e:
            print(f"enhancer: unpacking Real-ESRGAN failed: {e}", file=sys.stderr)
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        self.tile, self.ai = self.TILES[0], None
        return True

    def _install_ffmpeg(self) -> bool:
        """A static ffmpeg build when the system has none (checked against the
        site's published MD5)."""
        import hashlib
        import io
        import tarfile
        md5 = _download(FFMPEG_URL + ".md5", 30)
        blob = _download(FFMPEG_URL, 300)
        if not md5 or not blob:
            return False
        if hashlib.md5(blob).hexdigest() != md5.split()[0].decode():
            print("enhancer: ffmpeg download doesn't match its checksum, not used", file=sys.stderr)
            return False
        d = os.path.dirname(self.own_ffmpeg)
        tmp = d + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.makedirs(tmp)
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as t:
                m = next(m for m in t.getmembers() if m.isfile() and m.name.endswith("/ffmpeg"))
                with t.extractfile(m) as src, open(os.path.join(tmp, "ffmpeg"), "wb") as out:
                    shutil.copyfileobj(src, out)
            os.chmod(os.path.join(tmp, "ffmpeg"), 0o755)
            shutil.rmtree(d, ignore_errors=True)
            os.replace(tmp, d)
        except (OSError, tarfile.TarError, StopIteration) as e:
            print(f"enhancer: unpacking ffmpeg failed: {e}", file=sys.stderr)
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        return self._ffmpeg_ok()

    # --- GPU ----------------------------------------------------------------------------

    def _self_test(self) -> None:
        """Which GPUs Vulkan offers (never a software one), and a 64 px picture
        through the best of them; smaller tiles until it comes back right."""
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
        pb.fill(0x80B0E0FF)
        _, png = pb.save_to_bufferv("png", [], [])
        work = tempfile.mkdtemp(prefix="radio-beat-esr-")
        try:
            src = os.path.join(work, "t.png")
            with open(src, "wb") as f:
                f.write(png)
            start = self.TILES.index(self.tile)
            for tile in self.TILES[start:]:
                self.tile = tile
                out, err = self._esr(src, os.path.join(work, "o.png"))
                gpus = {int(i): n for i, n in re.findall(r"^\[(\d+) ([^\]]+)\]", err, re.M)}
                real = {i: n for i, n in gpus.items() if not SOFTWARE_GPU.search(n)}
                if not real:
                    self.ai = False
                    return
                if self.gpu_index not in real:
                    self.gpu_index = max(real, key=lambda i: (bool(DISCRETE_GPU.search(real[i])), -i))
                    self.gpu = real[self.gpu_index]
                    out, err = self._esr(src, os.path.join(work, "o.png"))  # again, on that one
                if out:
                    self._save_tile()
                    self.ai = True
                    return
            self.ai = False
            print(f"enhancer: the GPU fails Real-ESRGAN even with small tiles: {err[-200:]}", file=sys.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _test_vk_denoise(self) -> None:
        if not self.ffmpeg or not self.ai:
            self.vk_denoise = False
            return
        work = tempfile.mkdtemp(prefix="radio-beat-esr-")
        try:
            out = os.path.join(work, "o.png")
            self.vk_denoise = self._ff(["-f", "lavfi", "-i", "color=c=gray:s=64x64", "-frames:v", "1"],
                                       DENOISE_VK, out, vulkan=True) and os.path.exists(out)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # --- jobs ---------------------------------------------------------------------------

    def request(self, kind: str, key: str, data: bytes, src_size: tuple[int, int], size: tuple[int, int], done) -> None:
        """Main thread. done(picture bytes) runs on the main thread after each
        step. A newer request of the same kind replaces (and stops) the older one."""
        self.jobs[kind] = (key, data, src_size, size, done)
        r = self.running
        if r and r[0] == kind and r[1] != key and self.proc:
            self.proc.kill()
        self.wake.set()

    def drop(self, kind: str) -> None:
        self.jobs.pop(kind, None)
        r = self.running
        if r and r[0] == kind and self.proc:
            self.proc.kill()

    def _cache_path(self, key: str, size: tuple[int, int]) -> str:
        import hashlib
        h = hashlib.sha1(f"{key}|{size[0]}x{size[1]}|{'ai' if self.ai else 'plain'}|v2".encode()).hexdigest()
        return os.path.join(UPSCALED_DIR, f"{h}.jpg")

    def _worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            while self.jobs:
                kind = "logo" if "logo" in self.jobs else next(iter(self.jobs))
                job = self.jobs[kind]
                self.running = (kind, job[0])
                try:
                    self._enhance(kind, job)
                except Exception as e:  # never take the app down over a picture
                    print(f"enhancer: {e}", file=sys.stderr)
                self.running = None
                if self.jobs.get(kind) is job:
                    del self.jobs[kind]

    def _current(self, kind: str, job: tuple) -> bool:
        return self.jobs.get(kind) is job

    def _enhance(self, kind: str, job: tuple) -> None:
        key, data, (w0, h0), size, done = job
        what = "Cover" if kind == "cover" else "Logo"
        head = f"{what}: {w0}×{h0} px → {size[0]}×{size[1]} px"
        emit = lambda pic: GLib.idle_add(lambda: (done(pic), False)[1])
        cached = self._cache_path(key, size)
        if os.path.exists(cached):
            if self._jpeg_ok(cached):
                os.utime(cached)
                with open(cached, "rb") as f:
                    emit(f.read())
                note = cached[:-4] + ".txt"
                try:
                    with open(note) as f:
                        steps = f.read().strip()
                except OSError:
                    steps = "enhanced"
                self._picture(f"{head}, {steps} (from the cache)")
                return
            os.remove(cached)
        work = tempfile.mkdtemp(prefix="radio-beat-esr-")
        try:
            src = os.path.join(work, "in.img")
            with open(src, "wb") as f:
                f.write(data)
            steps: list[str] = []
            show = lambda more: self._picture(f"{head}, {', '.join(steps)}{more}")

            # 1. upscale
            self._picture(f"{head}: upscaling{' on ' + self.gpu if self.ai else ''}…")
            t = time.monotonic()
            up = os.path.join(work, "up.png")
            if self.ai:
                big = os.path.join(work, "x4.png")
                out, err = self._esr(src, big)
                if out is None and self.tile != self.TILES[-1]:
                    # The GPU timed out (black or failed): smaller tiles from now on, once more.
                    self.tile = self.TILES[self.TILES.index(self.tile) + 1]
                    self._save_tile()
                    out, err = self._esr(src, big)
                if out == "killed" or not self._current(kind, job):
                    return
                ok = out is not None and self._resize(big, size, up)
                label = "AI-upscaled"
                if not ok:
                    print(f"enhancer: Real-ESRGAN failed: {err.strip()[-200:]}", file=sys.stderr)
            else:
                ok, label = False, "upscaled"
            if not ok:
                ok = self._resize(src, size, up)
                label = "upscaled (smooth, no AI)" if label == "upscaled" else "upscaled (smooth: AI failed)"
            if not ok or not self._current(kind, job):
                if self._current(kind, job):
                    self._picture(f"{head}: couldn't be upscaled, shown as it is")
                return
            steps.append(f"{label} in {_secs(time.monotonic() - t)}")
            emit(open(up, "rb").read())
            if not self.ffmpeg:
                show(" (sharpening and denoising: no ffmpeg yet)")
                return

            # 2. sharpen
            show(", sharpening…")
            t = time.monotonic()
            sharp = os.path.join(work, "sharp.png")
            if not self._ff(["-i", up], SHARPEN, sharp) or not self._current(kind, job):
                if self._current(kind, job):
                    show(" (sharpening failed)")
                return
            steps.append(f"sharpened in {_secs(time.monotonic() - t)}")
            emit(open(sharp, "rb").read())

            # 3. denoise (on the upscaled picture), then sharpen: the final one
            show(", denoising…")
            t = time.monotonic()
            final = os.path.join(work, "final.jpg")
            ok = self.vk_denoise and self._ff(["-i", up], f"{DENOISE_VK},{SHARPEN}", final, vulkan=True, jpeg=True)
            where = "on the GPU"
            if not ok and self._current(kind, job):
                ok, where = self._ff(["-i", up], f"{DENOISE},{SHARPEN}", final, jpeg=True), "on the CPU"
            if not ok or not self._current(kind, job):
                if self._current(kind, job):
                    show(" (denoising failed)")
                return
            steps.append(f"denoised {where} in {_secs(time.monotonic() - t)}")
            with open(final, "rb") as f:
                pic = f.read()
            emit(pic)
            show("")
            os.makedirs(UPSCALED_DIR, exist_ok=True)
            with open(cached + ".part", "wb") as f:
                f.write(pic)
            os.replace(cached + ".part", cached)
            with open(cached[:-4] + ".txt", "w") as f:
                f.write(", ".join(steps))
            self._trim()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # --- tools --------------------------------------------------------------------------

    def _esr(self, src: str, out: str) -> tuple[str | None, str]:
        """Real-ESRGAN 4x: (out, stderr); out None on failure, "killed" when stopped."""
        cmd = ["nice", "-n", "19", self.bin, "-i", src, "-o", out, "-n", ESR_MODEL, "-m", self.models,
               "-t", str(self.tile), "-f", "png"]
        if self.gpu_index is not None:
            cmd += ["-g", str(self.gpu_index)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, err = self.proc.communicate()
        rc, self.proc = self.proc.returncode, None
        if rc < 0:
            return "killed", err
        if rc or "failed" in err or not os.path.exists(out) or not self._not_black(out):
            return None, err
        return out, err

    def _ff(self, inputs: list[str], vf: str, out: str, vulkan: bool = False, jpeg: bool = False) -> bool:
        if not self.ffmpeg:
            return False
        cmd = ["nice", "-n", "19", self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if vulkan:
            dev = f"vulkan=vk:{self.gpu_index}" if self.gpu_index is not None else "vulkan=vk"
            cmd += ["-init_hw_device", dev, "-filter_hw_device", "vk"]
        cmd += inputs + ["-vf", vf, "-frames:v", "1"] + (["-q:v", "2"] if jpeg else []) + [out]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, err = self.proc.communicate()
        rc, self.proc = self.proc.returncode, None
        if rc:
            if rc > 0:
                print(f"enhancer: ffmpeg {vf}: {err.strip()[-200:]}", file=sys.stderr)
            return False
        return os.path.exists(out)

    def _resize(self, src: str, size: tuple[int, int], out: str) -> bool:
        """To the screen's size: Lanczos through ffmpeg, else GdkPixbuf."""
        if self._ff(["-i", src], f"scale={size[0]}:{size[1]}:flags=lanczos", out):
            return True
        from gi.repository import GdkPixbuf
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(src).scale_simple(size[0], size[1], GdkPixbuf.InterpType.BILINEAR)
            return pb.savev(out, "png", [], [])
        except GLib.Error:
            return False

    @staticmethod
    def _not_black(path: str) -> bool:
        from gi.repository import GdkPixbuf
        try:
            px = GdkPixbuf.Pixbuf.new_from_file(path).get_pixels()
        except GLib.Error:
            return False
        return any(px[i] for i in range(0, len(px), 4099))

    def _trim(self) -> None:
        try:
            files = sorted((os.path.join(UPSCALED_DIR, f) for f in os.listdir(UPSCALED_DIR) if f.endswith(".jpg")),
                           key=os.path.getmtime)
        except OSError:
            return
        for f in files[:-UPSCALED_KEEP]:
            for p in (f, f[:-4] + ".txt"):
                try:
                    os.remove(p)
                except OSError:
                    pass


# --- window ---------------------------------------------------------------------

CSS = b"""
.cover-bg { background: #111; }
.backdrop { filter: blur(36px) brightness(0.55); transform: scale(1.15); }
.scrim { background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.75) 55%, rgba(0,0,0,0) 100%); }
.scrim label { color: white; text-shadow: 0 1px 3px rgba(0,0,0,0.8); }
.song { font-weight: 800; }
.artist { font-weight: 600; }
.album { opacity: 0.8; }
.status { opacity: 0.65; }
.round { min-width: 56px; min-height: 56px; border-radius: 999px; padding: 0; }
.play-button { background: #E3051A; color: white; min-width: 68px; min-height: 68px; }
.play-button:hover { background: #F21C30; }
.stop-button { background: rgba(255,255,255,0.16); color: white; }
.stop-button:hover { background: rgba(255,255,255,0.28); }
.scrim scale trough highlight { background: #E3051A; }
.overlay-header { background: none; box-shadow: none; }
.show-card { background: rgba(0,0,0,0.45); border-radius: 14px; padding: 10px; }
.show-pic { border-radius: 10px; }
.on-air { background: #E3051A; color: white; border-radius: 999px; padding: 1px 8px; font-weight: 800; }
.show-title { font-weight: 700; }
.show-sub { opacity: 0.8; }
.show-next { opacity: 0.7; }
.overlay-header button { background: rgba(0,0,0,0.45); color: white; }
.overlay-header windowcontrols image.icon { -gtk-icon-size: 34px; min-width: 34px; min-height: 34px; border-radius: 8px; }
"""


# Sizes at the window's first size (BASE_SIZE); a larger window or full screen
# scales them all by how much bigger it is (the smaller of width and height).
BASE_SIZE = (440, 600)
TEXT_PT = {"song": 24, "artist": 16.5, "album": 13, "status": 10, "on-air": 9,
           "show-title": 13.5, "show-sub": 10.5, "show-next": 10.5, "pic": 96}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rádio Beat", default_width=420, default_height=620)
        self.app = app
        self.set_icon_name(APP_ID)
        self._scale = 1.0
        self.scale_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.scale_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        # The cover, whole and as big as it fits (at the top in a tall window,
        # centred in a wide one), over a blurred copy of itself that fills the
        # rest of the window, whatever its shape; everything else lies on top.
        self.backdrop = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
        self.backdrop.add_css_class("backdrop")
        self.cover = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, hexpand=True,
                                 valign=Gtk.Align.START)
        bg = Gtk.Overlay(child=self.backdrop, hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        bg.add_css_class("cover-bg")
        bg.add_overlay(self.cover)
        overlay = Gtk.Overlay(child=bg)

        header = Adw.HeaderBar(show_title=False, valign=Gtk.Align.START)
        header.add_css_class("overlay-header")
        header.pack_end(self._settings_button())
        self.full_btn = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Full screen (F11)")
        self.full_btn.connect("clicked", lambda *_: self.toggle_full())
        header.pack_end(self.full_btn)
        overlay.add_overlay(header)
        self.connect("notify::fullscreened", self.on_fullscreened)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        # Double click anywhere (buttons and the volume slider keep their clicks): full screen and back.
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", self.on_double)
        self.add_controller(dbl)

        scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.END,
                        margin_top=0)
        scrim.add_css_class("scrim")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=90,
                        margin_bottom=22, margin_start=22, margin_end=22)
        scrim.append(inner)
        # What's on: the show now (picture, name, host) and the next one.
        self.show_card = Gtk.Box(spacing=12, margin_bottom=10)
        self.show_card.add_css_class("show-card")
        self.show_pic = Gtk.Image(pixel_size=TEXT_PT["pic"], valign=Gtk.Align.CENTER)
        self.show_pic.add_css_class("show-pic")
        self.show_pic.set_overflow(Gtk.Overflow.HIDDEN)
        self.show_card.append(self.show_pic)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        badge = Gtk.Label(label="ON AIR", valign=Gtk.Align.CENTER)
        badge.add_css_class("on-air")
        self.show_title = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)  # 3 = Pango.EllipsizeMode.END
        self.show_title.add_css_class("show-title")
        top.append(badge)
        top.append(self.show_title)
        self.show_sub = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=3)
        self.show_sub.add_css_class("show-sub")
        self.show_next = Gtk.Label(xalign=0, ellipsize=3)
        self.show_next.add_css_class("show-next")
        for w in (top, self.show_sub, self.show_next):
            text.append(w)
        self.show_card.append(text)
        self.show_card.set_visible(False)
        inner.append(self.show_card)

        self.song = Gtk.Label(wrap=True, xalign=0)
        self.song.add_css_class("song")
        self.artist = Gtk.Label(wrap=True, xalign=0)
        self.artist.add_css_class("artist")
        self.album = Gtk.Label(wrap=True, xalign=0)
        self.album.add_css_class("album")
        for w in (self.song, self.artist, self.album):
            inner.append(w)

        controls = Gtk.Box(spacing=12, margin_top=12)
        self.play_btn = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER)
        self.play_btn.add_css_class("round")
        self.play_btn.add_css_class("play-button")
        self.play_btn.connect("clicked", lambda *_: app.toggle_pause())
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop and quit",
                          valign=Gtk.Align.CENTER)
        stop.add_css_class("round")
        stop.add_css_class("stop-button")
        stop.connect("clicked", lambda *_: app.quit_all())
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_hexpand(True)
        self.vol.set_valign(Gtk.Align.CENTER)
        self.vol.set_value(app.settings["volume"])
        self.vol.connect("value-changed", lambda s: app.set_volume(s.get_value()))
        controls.append(self.play_btn)
        controls.append(stop)
        vol_icon = Gtk.Image(icon_name="audio-volume-high-symbolic", margin_start=6)
        vol_icon.add_css_class("vol-icon")
        controls.append(vol_icon)
        controls.append(self.vol)
        inner.append(controls)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("status")
        inner.append(self.status)
        overlay.add_overlay(scrim)

        self.set_content(overlay)
        self._apply_scale()
        self.connect("close-request", self.on_close)
        self.refresh()

    def _settings_button(self) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8)
        group = Adw.PreferencesGroup()
        self.quality = Adw.ComboRow(title="Quality")
        self.quality_model = Gtk.StringList()
        self.quality.set_model(self.quality_model)
        self.fill_quality()
        self.quality.connect("notify::selected", self.on_quality)
        group.add(self.quality)
        auto = Adw.SwitchRow(title="Start at login", subtitle="Plays with just the tray icon")
        auto.set_active(autostart_on())
        auto.connect("notify::active", lambda r, _: set_autostart(r.get_active()))
        group.add(auto)
        if is_kde():
            near = Adw.SwitchRow(title="Open in the corner", subtitle="Bottom right, on the taskbar")
            near.set_active(self.app.settings.get("tray_popup", True))
            near.connect("notify::active", lambda r, _: self.app.set_tray_popup(r.get_active()))
            group.add(near)
        self.enhance_row = Adw.SwitchRow(title="Enhance pictures", subtitle=self.app.enhance_status(),
                                         subtitle_lines=0)
        self.enhance_row.set_active(self.app.settings.get("enhance", True))
        self.enhance_row.connect("notify::active", lambda r, _: self.app.set_enhance(r.get_active()))
        group.add(self.enhance_row)
        self.picture_row = Adw.ActionRow(title="Picture now", subtitle=self.app.picture_text, subtitle_lines=0)
        group.add(self.picture_row)
        self.update_row = Adw.ActionRow(title="Updates")
        self.update_btn = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        self.update_btn.connect("clicked", lambda *_: self.app.update_clicked())
        self.update_row.add_suffix(self.update_btn)
        group.add(self.update_row)
        box.append(group)
        # Under the row, wrapped: a commit message would widen the menu past the window.
        self.update_note = Gtk.Label(label=self.app.update_text, xalign=0, wrap=True,
                                     max_width_chars=1, hexpand=False, margin_start=12, margin_end=12)
        self.update_note.add_css_class("dim-label")
        self.update_note.add_css_class("caption")
        box.append(self.update_note)
        # Opens leftwards from the gear (at the right edge), so it stays inside the window.
        # A popup takes its content's full natural width; this caps it near the app's width.
        fit = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                 vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                 width_request=320)
        pop = Gtk.Popover(child=fit, has_arrow=False)
        btn = Gtk.MenuButton(icon_name="emblem-system-symbolic", tooltip_text="Settings", popover=pop)
        pop.connect("show", lambda p: self._place_menu(btn, p))
        return btn

    MENU_W = 320 + 2 * 6  # the box above plus the popover's own padding
    MENU_GAP = 10  # px kept from the window's edges

    def _place_menu(self, btn, pop) -> None:
        """Drop the menu below the gear, inside the window with a gap from
        its edges, whatever the window's width and title buttons."""
        ok, pt = btn.compute_point(self, Graphene.Point())
        if not ok:
            return
        mid = pt.x + btn.get_width() / 2  # a popover is centred on its button
        right = min(self.get_width() - self.MENU_GAP, mid + self.MENU_W / 2)
        left = max(self.MENU_GAP, right - self.MENU_W)
        pop.set_offset(int(left + self.MENU_W / 2 - mid), 8)

    def fill_quality(self) -> None:
        self._filling = True
        while self.quality_model.get_n_items():
            self.quality_model.remove(0)
        for _, label, _ in self.app.streams:
            self.quality_model.append(label)
        ids = [s[0] for s in self.app.streams]
        q = self.app.settings["quality"]
        self.quality.set_selected(ids.index(q) if q in ids else 0)
        self._filling = False

    def on_quality(self, row, _pspec) -> None:
        if getattr(self, "_filling", False):
            return
        i = row.get_selected()
        if 0 <= i < len(self.app.streams):
            self.app.set_quality(self.app.streams[i][0])

    def toggle_full(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_fullscreened(self, *_):
        full = self.is_fullscreen()
        self.full_btn.set_icon_name("view-restore-symbolic" if full else "view-fullscreen-symbolic")
        self.full_btn.set_tooltip_text("Leave full screen (F11 / Esc)" if full else "Full screen (F11)")
        (self.add_css_class if full else self.remove_css_class)("full")

    def do_size_allocate(self, width, height, baseline):
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        f = round(max(1.0, min(width / BASE_SIZE[0], height / BASE_SIZE[1])), 2)
        if f != self._scale:
            self._scale = f
            GLib.idle_add(self._apply_scale)  # not while laying out

    def _apply_scale(self) -> bool:
        """Text and the show picture grow with the window: in full screen
        they fill the screen instead of staying window-sized."""
        f = self._scale
        css = "".join(f".{cls} {{ font-size: {pt * f:.1f}pt; }}\n" for cls, pt in TEXT_PT.items() if cls != "pic")
        # the buttons grow too, so they don't look lost next to the text
        css += (f".round {{ min-width: {56 * f:.0f}px; min-height: {56 * f:.0f}px; }}\n"
                f".play-button {{ min-width: {68 * f:.0f}px; min-height: {68 * f:.0f}px; }}\n"
                f".round image, .vol-icon {{ -gtk-icon-size: {16 * f:.0f}px; }}\n"
                f".overlay-header button {{ min-width: {34 * f:.0f}px; min-height: {34 * f:.0f}px; }}\n"
                f".overlay-header button image {{ -gtk-icon-size: {16 * f:.0f}px; }}\n"
                f".overlay-header windowcontrols image.icon {{ -gtk-icon-size: {34 * f:.0f}px; "
                f"min-width: {34 * f:.0f}px; min-height: {34 * f:.0f}px; }}\n"
                f".show-card {{ padding: {10 * f:.0f}px; border-radius: {14 * f:.0f}px; }}\n")
        self.scale_css.load_from_data(css.encode())
        self.show_pic.set_pixel_size(int(TEXT_PT["pic"] * f))
        return False

    def on_double(self, _gesture, n, x, y) -> None:
        if n != 2:
            return
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not self:
            if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Adw.HeaderBar)):
                return  # their own clicks (the title bar maximises on double click)
            w = w.get_parent()
        self.toggle_full()

    def on_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_F11 or (keyval == Gdk.KEY_Escape and self.is_fullscreen()):
            self.toggle_full()
            return True
        return False

    def on_close(self, *_):
        self.set_visible(False)  # keeps playing; the tray icon brings it back
        return True

    def refresh(self) -> None:
        a = self.app
        info = a.info or {}
        self.song.set_text(info.get("title") or a.song or "Rádio Beat")
        artist = info.get("artist") or a.artist_raw or "První bigbeat u nás"
        self.artist.set_text(artist)
        self.album.set_text(info.get("album") or "")
        self.album.set_visible(bool(info.get("album")))
        pic = a.cover_tex or a.logo_tex
        if pic is not self.cover.get_paintable():
            self.cover.set_paintable(pic)
            self.backdrop.set_paintable(pic)
        cur, nxt = on_air(a.schedule, time.localtime())
        self.show_card.set_visible(cur is not None)
        if cur:
            self.show_title.set_text(cur["title"])
            host = a.host if a.host and norm(a.host) != norm(cur["title"]) else None
            self.show_sub.set_text(" · ".join(x for x in (host, cur["desc"]) if x))
            self.show_sub.set_visible(bool(host or cur["desc"]))
            if nxt:
                when = f'{nxt["start"] // 60}:{nxt["start"] % 60:02d}'
                self.show_next.set_text(f'Next {nxt["day"] + " " if nxt["day"] else ""}{when} · {nxt["title"]}')
            self.show_next.set_visible(nxt is not None)
            tex = a.show_pics.get(cur["img"]) if cur["img"] else None
            self.show_pic.set_from_paintable(tex or a.logo_tex)
        self.play_btn.set_icon_name("media-playback-start-symbolic" if a.paused else "media-playback-pause-symbolic")
        self.play_btn.set_tooltip_text("Play" if a.paused else "Pause")
        if a.error:
            self.status.set_text(a.error)
        elif a.paused:
            self.status.set_text("Paused")
        elif a.tries:
            self.status.set_text(f"Reconnecting… ({a.stream()[1]})")
        else:
            parts = [p for p in (a.codec, f"{a.bitrate} kbps" if a.bitrate else None) if p]
            if a.alt:
                parts.append(f"backup: {a.stream()[1]}")
            self.status.set_text(" · ".join(parts) if parts else "Connecting…")
        self.update_note.set_text(a.update_text)
        self.update_btn.set_label("Update" if a.update_ready else "Check")
        self.update_btn.set_sensitive(not a.update_busy)
        (self.update_btn.add_css_class if a.update_ready else self.update_btn.remove_css_class)("suggested-action")
        if abs(self.vol.get_value() - a.settings["volume"]) >= 1:
            self.vol.set_value(a.settings["volume"])


class App(Adw.Application):
    RELIVE = 30  # s paused: pick the live stream up again instead of resuming behind it
    STALL = 20  # s without new audio: reconnect
    PER_SERVER = 3  # failed tries before moving to the next stream

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = load_settings()
        self.streams = list(STREAMS)
        self.win: Window | None = None
        self.mpv: Mpv | None = None
        self.tray: Tray | None = None
        self.mpris: Mpris | None = None
        self.paused = False
        self.paused_at = 0.0
        self.error: str | None = None
        self.icy = self.song = self.artist_raw = None
        self.info: dict | None = None
        self.schedule: dict[int, list[dict]] = {}  # radiobeat.cz programme, by weekday
        self.host: str | None = None  # who's on air (the site's homepage)
        self.show_pics: dict[str, object] = {}  # show picture url -> texture
        self.codec = self.bitrate = None
        self.cover_tex = None
        self.logo_tex = None
        self.enhancer = Enhancer(on_status=self._enhance_status_changed, on_picture=self._picture_changed)
        self.upscale_src: dict[str, tuple[str, bytes]] = {}  # kind -> (key, picture bytes), for after the install
        self.orig_tex: dict[str, object] = {}  # kind -> the picture as it came
        self.picture_text = "No picture yet"
        self._lookup_id = 0
        self._started = False
        self.last_audio = time.monotonic()  # when playback last moved on
        self.last_pos = None
        self.tries = 0  # reconnects since audio last flowed
        self.alt = 0  # streams moved on from the chosen one (backup servers)
        self.last_reconnect = 0.0
        self.quitting = False
        self.update_text = "Not checked yet"
        self.update_ready = False
        self.update_busy = False
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.quit_all())
        self.add_action(q)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    # startup / command line
    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        theme.add_search_path(os.path.join(HERE, "icons"))
        for p in (os.path.join(HERE, "logo.png"), os.path.join(HERE, "icons", "hicolor", "256x256", "apps", f"{APP_ID}.png")):
            if os.path.exists(p):
                self.logo_tex = Gdk.Texture.new_from_filename(p)
                self.orig_tex["logo"] = self.logo_tex
                with open(p, "rb") as f:
                    self.upscale_src["logo"] = (p, f.read())
                break
        threading.Thread(target=self._install_upscaler, daemon=True).start()
        self.start_mpv()
        GLib.timeout_add_seconds(5, self._watchdog)
        self._programme()
        GLib.timeout_add_seconds(60, self._programme_tick)
        try:
            self.mpris = Mpris(self)
        except GLib.Error as e:
            print(f"no media controls: {e}", file=sys.stderr)
        try:
            self.tray = Tray(self)
        except GLib.Error as e:
            print(f"no tray icon: {e}", file=sys.stderr)
        self.hold()  # runs on with the window closed
        threading.Thread(target=self._probe_flac, daemon=True).start()
        if is_kde():
            threading.Thread(target=self._kde_place, args=(self.settings.get("tray_popup", True),), daemon=True).start()
        GLib.timeout_add_seconds(20, lambda: (self.check_update(), False)[1])  # once, after it settled

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        first = not self._started
        self._started = True
        if first:
            self.play()
        if "--autostart" in args and first:
            return 0
        self.show_window()
        return 0

    def show_window(self):
        if not self.win:
            self.win = Window(self)
        self.win.set_visible(True)
        self.win.present()

    def toggle_window(self):
        if self.win and self.win.get_visible():
            self.win.set_visible(False)
        else:
            self.show_window()

    # updates / tray placement
    def set_tray_popup(self, on: bool):
        self.settings["tray_popup"] = on
        save_settings(self.settings)
        threading.Thread(target=self._kde_place, args=(on,), daemon=True).start()

    @staticmethod
    def _kde_place(on: bool):
        kwin_rule(False)  # the earlier "under the mouse" rule
        kwin_corner(on)

    def check_update(self):
        if self.update_busy:
            return
        self.update_busy, self.update_text = True, "Checking…"
        self.update()

        def work():
            ready, text = update_status()
            GLib.idle_add(self._checked, bool(ready), text)
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, ready, text):
        self.update_busy, self.update_ready, self.update_text = False, ready, text
        self.update()
        return False

    def update_clicked(self):
        if not self.update_ready:
            self.check_update()
            return
        self.update_busy, self.update_text = True, "Updating…"
        self.update()

        def work():
            err = run_update()
            GLib.idle_add(self._updated, err)
        threading.Thread(target=work, daemon=True).start()

    def _updated(self, err):
        if err:
            self.update_busy, self.update_text = False, f"Update failed: {err}"
            self.update()
            return False
        # Start the new version once this one is gone (same app id), then quit.
        subprocess.Popen(["sh", "-c", f"sleep 2; exec {launcher()}"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.quit_all()
        return False

    def _probe_flac(self):
        url = probe_flac()
        if url:
            GLib.idle_add(self._add_flac, url)

    def _add_flac(self, url):
        self.streams.insert(0, ("flac", "FLAC (lossless)", url))
        if self.win:
            self.win.fill_quality()

    def start_mpv(self):
        try:
            self.mpv = Mpv(self.on_prop)
            self.mpv.volume(self.settings["volume"])
            self.error = None
        except (OSError, RuntimeError) as e:
            self.mpv, self.error = None, str(e)

    # playback
    def stream(self) -> tuple[str, str, str]:
        """The chosen quality, or the stream after it when it keeps failing."""
        ids = [i for i, _, _ in self.streams]
        q = self.settings["quality"]
        start = ids.index(q) if q in ids else 0
        return self.streams[(start + self.alt) % len(self.streams)]

    def url(self) -> str:
        return self.stream()[2]

    def play(self):
        """Start (again) on the chosen quality, live."""
        self.paused = False
        self.alt, self.tries = 0, 0
        self._load()
        self.update()

    def _load(self):
        if not self.mpv:
            return
        self.last_audio, self.last_pos = time.monotonic(), None
        self.last_reconnect = time.monotonic()
        self.mpv.pause(False)
        self.mpv.play(self.url())

    def reconnect(self, why: str):
        """The stream stopped: load it again, moving to the next server after a few tries."""
        if self.paused or self.quitting or time.monotonic() - self.last_reconnect < 5:
            return
        self.tries += 1
        if self.tries % self.PER_SERVER == 0:
            self.alt += 1
        print(f"radio-beat: {why}, reconnecting (try {self.tries}, {self.stream()[1]})", file=sys.stderr)
        self._load()
        self.update()

    def _watchdog(self):
        if self.mpv and not self.paused and time.monotonic() - self.last_audio > self.STALL:
            self.reconnect("no audio for a while")
        return True

    def toggle_pause(self):
        if not self.mpv:
            return
        if not self.paused:
            self.paused, self.paused_at = True, time.monotonic()
            self.mpv.pause(True)
        elif time.monotonic() - self.paused_at > self.RELIVE:
            self.play()  # back to live, not minutes behind
        else:
            self.paused = False
            self.last_audio = time.monotonic()
            self.mpv.pause(False)
        self.update()

    def set_quality(self, qid: str):
        self.settings["quality"] = qid
        save_settings(self.settings)
        self.play()

    def set_volume(self, v: float):
        v = max(0, min(100, int(v)))
        if v != self.settings["volume"]:
            self.settings["volume"] = v
            if self.mpv:
                self.mpv.volume(v)
            save_settings(self.settings)

    def nudge_volume(self, d: int):
        self.set_volume(self.settings["volume"] + d)
        self.update()

    def quit_all(self):
        self.quitting = True
        if self.mpv:
            self.mpv.quit()
        self.release()
        self.quit()

    def update(self):
        if self.win:
            self.win.refresh()
        if self.mpris:
            self.mpris.changed()
        if self.tray:
            info = self.info or {}
            title, artist = info.get("title") or self.song, info.get("artist") or self.artist_raw
            text = " – ".join(x for x in (artist, title) if x) or "Rádio Beat"
            self.tray.set_tooltip(("⏸ " if self.paused else "") + text)

    # mpv events (main loop)
    def on_prop(self, name, data):
        if name == "metadata":
            md = {k.lower(): v for k, v in (data or {}).items()}
            icy = md.get("icy-title")
            if icy and icy != self.icy:
                self.new_title(icy)
        elif name == "audio-codec-name":
            self.codec = {"aac": "AAC", "mp3": "MP3", "flac": "FLAC"}.get(data or "", (data or "").upper() or None)
        elif name == "audio-bitrate":
            self.bitrate = round(data / 1000) if data else None
        elif name == "playback-time":
            if data is not None and (self.last_pos is None or data > self.last_pos):
                if self.last_pos is not None and self.tries:
                    self.tries = 0  # audio flows again
                self.last_pos, self.last_audio = data, time.monotonic()
            return False  # many of these: no redraw
        elif name == "idle-active" and data and not self.paused and not self.quitting:
            # stream ended or didn't connect: try again shortly
            GLib.timeout_add_seconds(3, lambda: (self.reconnect("stream ended"), False)[1])
        elif name == "mpv-exit" and not self.quitting:
            print("radio-beat: mpv went away, starting it again", file=sys.stderr)
            GLib.timeout_add_seconds(2, self._restart_mpv)
        self.update()
        return False

    def _restart_mpv(self):
        self.start_mpv()
        if self.mpv and not self.paused:
            self._load()
        self.update()
        return False

    # programme: the week every 30 min, the host every 5, the card every minute
    def _programme_tick(self):
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 30 == 0 or not self.schedule:
            self._programme()
        elif self._ticks % 5 == 0:
            self._programme(host_only=True)
        else:
            self._show_pic()
            self.update()
        return True

    def _programme(self, host_only: bool = False):
        def work():
            sched = None
            if not host_only:
                page = fetch(SITE + "/cs/program/")
                sched = parse_schedule(page.decode("utf-8", "replace")) if page else None
            page = fetch(SITE + "/")
            host = parse_host(page.decode("utf-8", "replace")) if page else None
            GLib.idle_add(self._programme_got, sched, host)
        threading.Thread(target=work, daemon=True).start()

    def _programme_got(self, sched, host):
        if sched:
            self.schedule = sched
        if host:
            self.host = host
        self._show_pic()
        self.update()
        return False

    def _show_pic(self):
        cur, nxt = on_air(self.schedule, time.localtime())
        for show in (cur, nxt):
            url = show and show["img"]
            if url and url not in self.show_pics:
                self.show_pics[url] = None  # fetching

                def work(url=url):
                    data = fetch(url)
                    GLib.idle_add(self._show_pic_got, url, data)
                threading.Thread(target=work, daemon=True).start()

    def _show_pic_got(self, url, data):
        try:
            self.show_pics[url] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)) if data else None
        except GLib.Error:
            self.show_pics[url] = None
        self.update()
        return False

    def new_title(self, icy: str):
        self.icy = icy
        parts = split_title(icy)
        self.artist_raw, self.song = (parts if parts else (None, icy))
        self.info, self.cover_tex = None, None
        self.enhancer.drop("cover")
        self.upscale_src.pop("cover", None)
        self.orig_tex.pop("cover", None)
        self._lookup_id += 1
        if parts:
            threading.Thread(target=self._lookup, args=(self._lookup_id, *parts), daemon=True).start()

    def _lookup(self, lid, artist, song):
        info = lookup(artist, song)
        data = fetch(info["cover"]) if info and info.get("cover") else None
        GLib.idle_add(self._looked_up, lid, info, data)

    def _looked_up(self, lid, info, data):
        if lid != self._lookup_id:
            return False  # song changed meanwhile
        self.info = info
        if data:
            try:
                self.cover_tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self.upscale_src["cover"] = (info["cover"], data)
                self.orig_tex["cover"] = self.cover_tex
                self._enhance("cover")
            except GLib.Error:
                self.cover_tex = None
        self.update()
        return False


    # --- picture enhancing -----------------------------------------------------------

    def _install_upscaler(self):
        self.enhancer.maintain(then=lambda: (self._enhance("logo"), self._enhance("cover")))

    def enhance_status(self) -> str:
        return self.enhancer.status if self.settings.get("enhance", True) else \
            "Off: pictures are shown as they come."

    def _enhance_status_changed(self, _text: str) -> None:
        if self.win:
            self.win.enhance_row.set_subtitle(self.enhance_status())

    def _picture_changed(self, text: str, kind: str = "cover") -> None:
        if kind == "logo" and self.orig_tex.get("cover"):
            return  # the cover is what's shown
        self.picture_text = text
        if self.win:
            self.win.picture_row.set_subtitle(text)

    def set_enhance(self, on: bool) -> None:
        self.settings["enhance"] = on
        save_settings(self.settings)
        self._enhance_status_changed("")
        if on:
            self._enhance("logo")
            self._enhance("cover")
            return
        for kind in ("logo", "cover"):
            self.enhancer.drop(kind)
        self.logo_tex = self.orig_tex.get("logo", self.logo_tex)
        self.cover_tex = self.orig_tex.get("cover", self.cover_tex)
        self._picture_changed("Shown as it comes (enhancing is off)")
        self.update()

    def screen_px(self) -> tuple[int, int] | None:
        """Pixels of the screen the window is on (or the first one)."""
        disp = Gdk.Display.get_default()
        surf = self.win.get_surface() if self.win else None
        mon = disp.get_monitor_at_surface(surf) if surf else None
        if mon is None:
            mons = disp.get_monitors()
            mon = mons.get_item(0) if mons.get_n_items() else None
        if mon is None:
            return None
        g = mon.get_geometry()
        sc = mon.get_scale() if hasattr(mon, "get_scale") else mon.get_scale_factor()
        return round(g.width * sc), round(g.height * sc)

    def _enhance(self, kind: str) -> None:
        """Ask for an enhanced picture if full screen would stretch it."""
        tex = self.orig_tex.get(kind)
        src = self.upscale_src.get(kind)
        size = self.screen_px()
        if not tex or not src or not size:
            return
        w, h = tex.get_width(), tex.get_height()
        what = "Cover" if kind == "cover" else "Logo"
        f = min(size[0] / w, size[1] / h)
        if not self.settings.get("enhance", True):
            self._picture_changed(f"{what}: {w}×{h} px, shown as it comes (enhancing is off)", kind)
            return
        if f < UPSCALE_FROM:
            self._picture_changed(f"{what}: {w}×{h} px, shown as it comes (the screen, "
                                  f"{size[0]}×{size[1]} px, doesn't need it bigger)", kind)
            return
        if self.enhancer.ai is None and not self.enhancer.ffmpeg:
            self._picture_changed(f"{what}: {w}×{h} px, shown as it comes until the enhancing tools "
                                  "are downloaded", kind)
            return
        lid = self._lookup_id

        def done(pic: bytes):
            try:
                t = Gdk.Texture.new_from_bytes(GLib.Bytes.new(pic))
            except GLib.Error:
                return
            if not self.settings.get("enhance", True):
                return
            if kind == "cover":
                if lid != self._lookup_id:
                    return
                self.cover_tex = t
            else:
                self.logo_tex = t
            self.update()

        self.enhancer.request(kind, src[0], src[1], (w, h), (round(w * f), round(h * f)), done)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
