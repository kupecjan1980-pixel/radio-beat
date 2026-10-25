#!/usr/bin/env python3
"""Rádio Beat player: GTK4 / libadwaita window, mpv plays the stream.

mpv runs as a child process controlled over its JSON IPC socket; its MPRIS
plugin (mpv-mpris) gives media keys and the bar's player widget for free.
The stream's ICY title ("ARTIST - SONG") is looked up on Deezer (then
iTunes) for the album, proper spelling and the cover.

Closing the window while it plays keeps the music going; starting the app
again shows the window. Started at login (--autostart) it just plays.
"""

import json
import os
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

class Mpv:
    """mpv child process over JSON IPC; property changes arrive on `on_prop`."""

    def __init__(self, on_prop):
        self.on_prop = on_prop
        self.sock_path = os.path.join(tempfile.mkdtemp(prefix="radio-beat-"), "mpv.sock")
        self.proc = subprocess.Popen(
            ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
             "--title=Rádio Beat", f"--input-ipc-server={self.sock_path}",
             "--cache=yes", "--demuxer-max-bytes=4MiB", "--network-timeout=15",
             *os.environ.get("RADIO_BEAT_MPV_ARGS", "").split()],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        for i, prop in enumerate(("metadata", "audio-codec-name", "audio-bitrate", "core-idle", "idle-active")):
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
                return
            if not chunk:
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

    def volume(self, v: float) -> None:
        self.cmd("set_property", "volume", v)

    def quit(self) -> None:
        self.cmd("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- window ---------------------------------------------------------------------

CSS = b"""
.cover { border-radius: 14px; }
.song { font-size: 20pt; font-weight: 800; }
.artist { font-size: 14pt; font-weight: 600; }
.play-button { min-width: 64px; min-height: 64px; border-radius: 999px; }
.play-button { background: #E3051A; color: white; }
.play-button:hover { background: #F21C30; }
.play-button.playing { background: alpha(#E3051A, 0.75); }
"""


COVER = 320  # px, largest cover


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rádio Beat", default_width=400, default_height=720)
        self.app = app
        self.set_icon_name(APP_ID)

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Quit", "app.quit")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(mb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=8,
                      margin_bottom=18, margin_start=18, margin_end=18)

        # A fixed square (Gtk.Image keeps its pixel size), corners rounded by the clip box.
        self.cover = Gtk.Image(pixel_size=COVER)
        clip = Gtk.Box(halign=Gtk.Align.CENTER, overflow=Gtk.Overflow.HIDDEN)
        clip.add_css_class("cover")
        clip.append(self.cover)
        box.append(clip)

        self.song = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.song.add_css_class("song")
        self.artist = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.artist.add_css_class("artist")
        self.album = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.album.add_css_class("dim-label")
        for w in (self.song, self.artist, self.album):
            box.append(w)

        controls = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER, margin_top=4)
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play")
        self.play_btn.add_css_class("play-button")
        self.play_btn.connect("clicked", lambda *_: app.toggle())
        controls.append(self.play_btn)
        vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        vol.set_size_request(180, -1)
        vol.set_valign(Gtk.Align.CENTER)
        vol.set_value(app.settings["volume"])
        vol.connect("value-changed", lambda s: app.set_volume(s.get_value()))
        controls.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        controls.append(vol)
        box.append(controls)

        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        box.append(self.status)

        prefs = Adw.PreferencesGroup(margin_top=6)
        self.quality = Adw.ComboRow(title="Audio quality")
        self.quality_model = Gtk.StringList()
        self.quality.set_model(self.quality_model)
        self.fill_quality()
        self.quality.connect("notify::selected", self.on_quality)
        prefs.add(self.quality)
        auto = Adw.SwitchRow(title="Start at login", subtitle="Plays in the background; open the app to see it")
        auto.set_active(autostart_on())
        auto.connect("notify::active", lambda r, _: set_autostart(r.get_active()))
        prefs.add(auto)
        box.append(prefs)

        clamp = Adw.Clamp(maximum_size=420, child=box, valign=Gtk.Align.CENTER)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True)
        tv = Adw.ToolbarView(content=scroll)
        tv.add_top_bar(header)
        self.set_content(tv)
        self.connect("close-request", self.on_close)
        self.connect("notify::default-height", lambda *_: self.fit_cover())
        # Tiled windows get their size from the compositor: shrink the cover
        # so the controls stay in view.
        self._last_h = 0
        self.add_tick_callback(self._watch_size)
        self.play_btn.grab_focus()
        self.refresh()

    def _watch_size(self, *_):
        h = self.get_height()
        if h and h != self._last_h:
            self._last_h = h
            self.fit_cover()
        return True

    def fit_cover(self) -> None:
        h = self.get_height() or 720
        size = max(140, min(COVER, h - 420))
        self.cover.set_pixel_size(size)

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
        if self.app.playing:
            self.set_visible(False)  # keep playing; starting the app again shows it
            return True
        self.app.quit_all()
        return False

    def refresh(self) -> None:
        a = self.app
        info = a.info or {}
        self.song.set_text(info.get("title") or a.song or "Rádio Beat")
        self.artist.set_text(info.get("artist") or a.artist_raw or ("První bigbeat u nás" if not a.playing else ""))
        self.album.set_text(info.get("album") or "")
        self.album.set_visible(bool(info.get("album")))
        self.cover.set_from_paintable(a.cover_tex or a.logo_tex)
        self.play_btn.set_icon_name("media-playback-stop-symbolic" if a.playing else "media-playback-start-symbolic")
        self.play_btn.set_tooltip_text("Stop" if a.playing else "Play")
        (self.play_btn.add_css_class if a.playing else self.play_btn.remove_css_class)("playing")
        if a.playing:
            parts = [p for p in (a.codec, f"{a.bitrate} kbps" if a.bitrate else None) if p]
            self.status.set_text(" · ".join(parts) if parts else ("Connecting…" if a.loading else ""))
        else:
            self.status.set_text("")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = load_settings()
        self.streams = list(STREAMS)
        self.win: Window | None = None
        self.mpv: Mpv | None = None
        self.playing = self.loading = False
        self.icy = self.song = self.artist_raw = None
        self.info: dict | None = None
        self.codec = self.bitrate = None
        self.cover_tex = None
        self.logo_tex = None
        self._lookup_id = 0
        self._started = False
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
        self.mpv = Mpv(self.on_prop)
        self.mpv.volume(self.settings["volume"])
        self.hold()  # alive while playing with the window closed
        threading.Thread(target=self._probe_flac, daemon=True).start()

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        first = not self._started
        self._started = True
        if "--autostart" in args and first:
            self.play()
            return 0
        self.show_window()
        return 0

    def show_window(self):
        if not self.win:
            self.win = Window(self)
        self.win.set_visible(True)
        self.win.present()

    def _probe_flac(self):
        url = probe_flac()
        if url:
            GLib.idle_add(self._add_flac, url)

    def _add_flac(self, url):
        self.streams.insert(0, ("flac", "FLAC (lossless)", url))
        if self.win:
            self.win.fill_quality()

    # playback
    def url(self) -> str:
        return next((u for i, _, u in self.streams if i == self.settings["quality"]), self.streams[0][2])

    def play(self):
        self.playing = self.loading = True
        self.mpv.play(self.url())
        self.update()

    def stop(self):
        self.playing = self.loading = False
        self.mpv.stop()
        self.icy = self.song = self.artist_raw = None
        self.info, self.cover_tex, self.codec, self.bitrate = None, None, None, None
        self.update()

    def toggle(self):
        self.stop() if self.playing else self.play()

    def set_quality(self, qid: str):
        self.settings["quality"] = qid
        save_settings(self.settings)
        if self.playing:
            self.play()

    def set_volume(self, v: float):
        self.settings["volume"] = int(v)
        self.mpv.volume(v)
        save_settings(self.settings)

    def quit_all(self):
        if self.mpv:
            self.mpv.quit()
        self.release()
        self.quit()

    def update(self):
        if self.win:
            self.win.refresh()

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
        elif name == "core-idle":
            self.loading = bool(data) and self.playing
        elif name == "idle-active" and data and self.playing:
            # stream dropped or didn't connect: try again shortly
            GLib.timeout_add_seconds(3, self._retry)
        self.update()
        return False

    def _retry(self):
        if self.playing:
            self.mpv.play(self.url())
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
