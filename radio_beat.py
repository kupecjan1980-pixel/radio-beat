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
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402

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


# --- KDE: open next to the tray ----------------------------------------------------
# A Wayland window can't place itself. KWin can: a window rule with "under the
# mouse" placement opens it where the tray icon was clicked, kept inside the
# screen above the panel, so it lines up with the taskbar's right end.

KWIN_RULE = "radio-beat-placement"
KWIN_UNDER_MOUSE, KWIN_FORCE = 7, 2  # Placement::UnderMouse, Rules::Force


def is_kde() -> bool:
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and bool(shutil.which("kwriteconfig6"))


def _kcfg(*args: str) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True)
    return r.stdout.strip()


def kwin_rule(on: bool) -> None:
    rules = [x for x in _kcfg("kreadconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "rules").split(",") if x]
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


def update_status() -> tuple[bool | None, str]:
    """(newer version on GitHub?, text); None when it couldn't be checked.
    Compares the installed commit (VERSION, written by install.sh) with GitHub's newest."""
    try:
        c = _json(f"https://api.github.com/repos/{REPO}/commits/master")
        sha, info = c["sha"], c["commit"]
    except (OSError, ValueError, KeyError):
        return None, "Couldn't reach GitHub"
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            mine = f.read().strip()
    except OSError:
        mine = ""
    if mine == sha:
        return False, f"Up to date ({info['committer']['date'][:10]})"
    return True, f"New version: {info['message'].splitlines()[0]} ({info['committer']['date'][:10]})"


def run_update() -> str | None:
    """Installs the GitHub version (the same get.sh as the first install); error text or None."""
    r = subprocess.run(["bash", "-c", f"curl -fsSL {RAW}/get.sh | bash"], capture_output=True, text=True, timeout=600)
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
                        "album": x["album"]["title"], "cover": x["album"].get("cover_xl")}
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
                        "album": x.get("collectionName"), "cover": art.replace("100x100bb", "600x600bb") or None}
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
            [*cmd, "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
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


# --- window ---------------------------------------------------------------------

CSS = b"""
.cover-bg { background: #111; }
.scrim { background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.75) 55%, rgba(0,0,0,0) 100%); }
.scrim label { color: white; text-shadow: 0 1px 3px rgba(0,0,0,0.8); }
.song { font-size: 22pt; font-weight: 800; }
.artist { font-size: 15pt; font-weight: 600; }
.album { font-size: 12pt; opacity: 0.8; }
.status { font-size: 9pt; opacity: 0.65; }
.round { min-width: 56px; min-height: 56px; border-radius: 999px; padding: 0; }
.play-button { background: #E3051A; color: white; min-width: 68px; min-height: 68px; }
.play-button:hover { background: #F21C30; }
.stop-button { background: rgba(255,255,255,0.16); color: white; }
.stop-button:hover { background: rgba(255,255,255,0.28); }
.scrim scale trough highlight { background: #E3051A; }
.overlay-header { background: none; box-shadow: none; }
.overlay-header button { background: rgba(0,0,0,0.45); color: white; }
"""


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rádio Beat", default_width=420, default_height=620)
        self.app = app
        self.set_icon_name(APP_ID)

        # The cover fills the window; everything else lies on top of it.
        self.cover = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
        bg = Gtk.Box(hexpand=True, vexpand=True)
        bg.add_css_class("cover-bg")
        bg.append(self.cover)
        overlay = Gtk.Overlay(child=bg)

        header = Adw.HeaderBar(show_title=False, valign=Gtk.Align.START)
        header.add_css_class("overlay-header")
        header.pack_end(self._settings_button())
        overlay.add_overlay(header)

        scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.END,
                        margin_top=0)
        scrim.add_css_class("scrim")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=90,
                        margin_bottom=22, margin_start=22, margin_end=22)
        scrim.append(inner)
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
        controls.append(Gtk.Image(icon_name="audio-volume-high-symbolic", margin_start=6))
        controls.append(self.vol)
        inner.append(controls)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("status")
        inner.append(self.status)
        overlay.add_overlay(scrim)

        self.set_content(overlay)
        self.connect("close-request", self.on_close)
        self.refresh()

    def _settings_button(self) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8, width_request=320)
        group = Adw.PreferencesGroup()
        self.quality = Adw.ComboRow(title="Audio quality")
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
            near = Adw.SwitchRow(title="Open next to the tray", subtitle="Where the tray icon was clicked")
            near.set_active(self.app.settings.get("tray_popup", True))
            near.connect("notify::active", lambda r, _: self.app.set_tray_popup(r.get_active()))
            group.add(near)
        self.update_row = Adw.ActionRow(title="Updates", subtitle=self.app.update_text)
        self.update_btn = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        self.update_btn.connect("clicked", lambda *_: self.app.update_clicked())
        self.update_row.add_suffix(self.update_btn)
        group.add(self.update_row)
        box.append(group)
        return Gtk.MenuButton(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                              popover=Gtk.Popover(child=box))

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
        self.cover.set_paintable(a.cover_tex or a.logo_tex)
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
        self.update_row.set_subtitle(a.update_text)
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
        self.paused = False
        self.paused_at = 0.0
        self.error: str | None = None
        self.icy = self.song = self.artist_raw = None
        self.info: dict | None = None
        self.codec = self.bitrate = None
        self.cover_tex = None
        self.logo_tex = None
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
                break
        self.start_mpv()
        GLib.timeout_add_seconds(5, self._watchdog)
        try:
            self.tray = Tray(self)
        except GLib.Error as e:
            print(f"no tray icon: {e}", file=sys.stderr)
        self.hold()  # runs on with the window closed
        threading.Thread(target=self._probe_flac, daemon=True).start()
        if is_kde():
            threading.Thread(target=kwin_rule, args=(self.settings.get("tray_popup", True),), daemon=True).start()
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
        threading.Thread(target=kwin_rule, args=(on,), daemon=True).start()

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

    def new_title(self, icy: str):
        self.icy = icy
        parts = split_title(icy)
        self.artist_raw, self.song = (parts if parts else (None, icy))
        self.info, self.cover_tex = None, None
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
            except GLib.Error:
                self.cover_tex = None
        self.update()
        return False


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
