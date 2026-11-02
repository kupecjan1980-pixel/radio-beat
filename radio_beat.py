#!/usr/bin/env python3
"""Rádio Beat player: GTK4 / libadwaita window, mpv plays the stream.

mpv runs as a child process controlled over its JSON IPC socket; its MPRIS
plugin (mpv-mpris) gives media keys and the bar's player widget for free.
The stream's ICY title ("ARTIST - SONG") is looked up on Deezer (then
iTunes) for the album, proper spelling and the cover.

It starts playing right away. Closing the window keeps it playing, with an
icon in the bar's tray (StatusNotifierItem) that shows the window again;
Stop quits. Started at login (--autostart) it plays with only the tray icon, or opens in full screen when set
so (autostart_full); --hidden (a restart after an update while the window was closed) plays with only the tray
icon.
"""

# The version, as in Handheld Control: 0.MINOR.PATCH, the patch one up with every change pushed, the minor one up
# (patch back to 0) with a bigger new feature; each a git tag vX.Y.Z (and a GitHub release). Every earlier commit
# numbered the same way afterwards (CHANGELOG.md).
APP_VERSION = "0.25.13"

import collections
import ctypes
import glob
import html
import itertools
import json
import math
import os
import shutil
import signal
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gio, Graphene, Gsk, Gtk, Pango  # noqa: E402

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
    ("mp3backup", "MP3 128 kbps (záložní server)", "https://icecast1.play.cz/beat64.mp3"),
    ("aac64", "AAC 64 kbps (radiobeat.cz)", "https://stream.rcs.revma.com/3d47nqvb938uv"),
]
# What each stream sounds like (the list shows them best first).
STREAM_NOTES = {
    "flac": "Nejlepší: bezeztrátově, přesně jak se vysílá",
    "aac128": "Velmi dobrá: AAC uchová víc detailů než MP3 při stejném datovém toku",
    "mp3128": "Dobrá: MP3 potřebuje na stejný zvuk víc dat než AAC",
    "mp3backup": "Dobrá: stejné MP3 z jiného serveru",
    "aac64": "Nejnižší: poloviční data, stačí na malé reproduktory",
}

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
# leaves full screen or comes back from being minimised, it goes flush against
# the right edge, its bottom on the taskbar (by the clock) (KWin's
# MaximizeArea is the screen without the panels). The app loads the script
# into KWin over D-Bus at start (Plasma 6, and 5's clientAdded).
# The earlier "under the mouse" rule (KWIN_RULE) is taken out again.

KWIN_SCRIPT_NAME = "radio-beat-corner"
KWIN_SCRIPT = """
function place(w) {  // true when it was put there (not while full screen or still screen-sized)
    if (!w || w.resourceClass !== "%s" || w.fullScreen) return false;
    var a = workspace.clientArea(KWin.MaximizeArea, w);
    var g = w.frameGeometry;
    if (g.width >= a.width) return false;
    w.frameGeometry = {x: a.x + a.width - g.width, y: a.y + a.height - g.height, width: g.width, height: g.height};
    return true;
}
function corner(w) {  // the bottom-right rectangle for the window's size as a window (not full screen)
    var a = workspace.clientArea(KWin.MaximizeArea, w);
    var r = w.fullscreenGeometryRestore;
    var width = r && r.width > 0 && r.width < a.width ? r.width : Math.min(560, a.width);
    var height = r && r.height > 0 && r.height < a.height ? r.height : Math.min(760, a.height);
    return {x: a.x + a.width - width, y: a.y + a.height - height, width: width, height: height};
}
function landing(w) {  // full screen: leaving it lands in the corner (no stop at the top left on the way)
    if (!w.fullScreen) return;
    try { w.fullscreenGeometryRestore = corner(w); } catch (e) {}
}
function watch(w) {
    if (!w || w.resourceClass !== "%s") return;
    // KWin gives the window its size back after these signals: it's placed at the next fitting geometry.
    // Once placed, moving it by hand stays.
    var due = !place(w);
    landing(w);
    function again() { due = !place(w); }
    if (w.frameGeometryChanged) w.frameGeometryChanged.connect(function () { if (due && place(w)) due = false; });
    if (w.fullScreenChanged) w.fullScreenChanged.connect(function () {
        if (w.fullScreen) { landing(w); return; }
        w.frameGeometry = corner(w);  // straight to the corner, before it's drawn anywhere else
        again();
    });
    // brought back from the taskbar or the tray: always a window in the corner, even if it was full screen
    if (w.minimizedChanged) w.minimizedChanged.connect(function () {
        if (w.minimized) return;
        if (w.fullScreen) { landing(w); w.fullScreen = false; } else again();
    });
}
(workspace.windowAdded || workspace.clientAdded).connect(watch);
""" % (APP_ID, APP_ID)


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
        for key, val in (("Description", "Rádio Beat: otevřít u oznamovací oblasti"), ("wmclass", APP_ID),
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


AUTO_UPDATE_S = 5 * 60  # how often it looks for a new version
LATEST: dict = {}  # the newest commit seen by update_status()


def update_status() -> tuple[bool | None, str]:
    """(newer version on GitHub?, text); None when it couldn't be checked.
    Compares the installed commit (VERSION, written by install.sh) with GitHub's newest."""
    try:
        c = _json(f"https://api.github.com/repos/{REPO}/commits/master")
        sha, info = c["sha"], c["commit"]
        LATEST["sha"] = sha
    except (OSError, ValueError, KeyError):
        return None, "GitHub nedostupný"
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            mine = f.read().strip()
    except OSError:
        mine = ""
    if not re.fullmatch(r"[0-9a-f]{7,40}", mine):
        # not an installed copy (no VERSION: run from the source folder): never replaced by an update
        return False, "Běží ze složky se zdrojáky: sám se neaktualizuje"
    if mine == sha:
        return False, f"Verze {APP_VERSION}: aktuální ({info['committer']['date'][:10]})"
    # Only forwards: GitHub's commit must come after the installed one (a newer local build stays).
    if re.fullmatch(r"[0-9a-f]{7,40}", mine):
        try:
            status = _json(f"https://api.github.com/repos/{REPO}/compare/{mine}...{sha}")["status"]
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not on GitHub: built here, newer than what's published
                return False, "Tahle verze je novější než na GitHubu (ještě nevydaná)"
            return None, "GitHub nedostupný"
        except (OSError, ValueError, KeyError):
            return None, "GitHub nedostupný"
        if status in ("behind", "identical"):
            return False, (f"Verze {APP_VERSION}: aktuální (novější než na GitHubu)" if status == "behind"
                           else f"Verze {APP_VERSION}: aktuální")
    theirs = remote_version(sha)
    return True, (f"Nová verze {theirs or ''} z {info['committer']['date'][:10]} (teď {APP_VERSION}):\n"
                  f"{info['message'].splitlines()[0]}").replace("verze  z", "verze z")


def remote_version(sha: str) -> str:
    """APP_VERSION of GitHub's commit `sha` (only the file's start fetched), or ''."""
    try:
        req = urllib.request.Request(f"https://raw.githubusercontent.com/{REPO}/{sha}/radio_beat.py",
                                     headers={"User-Agent": UA, "Range": "bytes=0-4095"})
        with urllib.request.urlopen(req, timeout=8) as r:
            m = re.search(r'^APP_VERSION = "([0-9.]+)"', r.read().decode(errors="replace"), re.M)
        return m.group(1) if m else ""
    except (OSError, ValueError):
        return ""


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

VERSION_NOTE = re.compile(r"\s*[(\[][^)\]]*\b(remaster(ed)?|mono|stereo|single|version|edit|mix|deluxe|"
                          r"expanded|anniversary|bonus)\b[^)\]]*[)\]]|\s+-\s+(\d{4}\s+)?remaster(ed)?.*$", re.I)


def plain_title(text: str) -> str:
    """'Paperback Writer (Remastered 2015)' → 'Paperback Writer': the song, not the reissue's note."""
    return VERSION_NOTE.sub("", text).strip() or text


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"['´`’‘]", "", s)  # Pavlov's = PAVLOVS ´DOG (not "pavlov s")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def bare_song(song: str) -> str:
    """'Green Onions 1962', 'Green Onions (1962)' → 'Green Onions': the stream's year after the name isn't in
    the song's name ('1999' stays: nothing would be left)."""
    return re.sub(r"\s*[(\[]?\b(19[3-9]\d|20[0-3]\d)[)\]]?\s*$", "", song).strip() or song


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


def _key(s: str) -> str:
    """A name as compared: no brackets, no "- Remastered 2002" or such, no leading "The", no spaces or hyphens
    ("LOCOMOTION" = "The Loco-Motion (Remastered 2002)", "ACDC" = "AC/DC")."""
    s = re.sub(r"\s+-\s+.*\b(remaster\w*|live|mono|stereo|single|edit|version|mix)\b.*$", "", s, flags=re.I)
    return re.sub(r"^the ", "", norm(s)).replace(" ", "")


def _score(artist: str, song: str, cand_artist: str, cand_title: str) -> int:
    import difflib
    a, t = _key(first_artist(artist)), _key(song)
    ca, ct = _key(cand_artist), _key(cand_title)
    if not ca or (a not in ca and ca not in a):
        return -1
    score = 2 if ca == a else 1
    if ct == t:
        score += 3
    elif len(t) >= 4 and (t in ct or ct in t):
        score += 1
    elif difflib.SequenceMatcher(None, t, ct).ratio() >= 0.85:  # a letter or two off (the stream's spelling)
        score += 2
    else:
        score -= 2
    if "live" in cand_title.lower() and "live" not in song.lower():
        score -= 1
    return score


YTDLP = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "radio-beat", "yt-dlp")
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
NOT_STUDIO = re.compile(r"cover|karaoke|instrumental|reaction|slowed|sped up|\b8d\b|remix|tutorial|lesson|drum cam",
                        re.I)  # not the song at all
LIVE = re.compile(r"\blive\b|unplugged|concert|koncert|en vivo|acoustic", re.I)  # the song, but not the record


def ytdlp() -> list[str] | None:
    """yt-dlp: the system's, else the official release fetched into ~/.local/share/radio-beat (checked against
    its checksums, fetched again weekly: YouTube changes and old ones stop working)."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        fresh = time.time() - os.path.getmtime(YTDLP) < 7 * 86400
    except OSError:
        fresh = False
    if not fresh:
        import hashlib
        blob, sums = fetch(YTDLP_URL + "yt-dlp"), fetch(YTDLP_URL + "SHA2-256SUMS")
        want = re.search(r"^([0-9a-f]{64})\s+yt-dlp$", sums.decode(errors="ignore"), re.M) if sums else None
        if blob and want and hashlib.sha256(blob).hexdigest() == want.group(1):
            os.makedirs(os.path.dirname(YTDLP), exist_ok=True)
            with open(YTDLP + ".part", "wb") as f:
                f.write(blob)
            os.replace(YTDLP + ".part", YTDLP)
    return ["python3", YTDLP] if os.path.exists(YTDLP) else None


def find_track(artist: str, song: str) -> tuple[str, str] | None:
    """The song to play from YouTube: (its best audio stream, what it is: "opus 128 kbps"). The studio version
    (YouTube Music's album audio, 'Artist - Topic', or official audio; never a cover, remix…), a live or
    unplugged one only when there's nothing else; FLAC when there is one, else the best audio there is.
    None when nothing fits."""
    cmd = ytdlp()
    if not cmd:
        return None
    song = bare_song(song)
    a, t = _key(first_artist(artist)), _key(re.sub(r"\s*[(\[].*?[)\]]", "", song) or song)
    best, best_score = None, float("-inf")
    for query in (f"{first_artist(artist)} {song}", f"{first_artist(artist)} {song} official audio"):
        try:
            out = subprocess.run([*cmd, "-J", "--flat-playlist", "--no-warnings", f"ytsearch10:{query}"],
                                 capture_output=True, text=True, timeout=40).stdout
            entries = json.loads(out).get("entries") or []
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        for rank, e in enumerate(entries):
            title, channel = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
            dur = e.get("duration") or 0
            if t not in _key(title) or NOT_STUDIO.search(title) or not 60 <= dur <= 1500 \
                    or a not in _key(title + " " + channel):  # (another artist's song of that name: not it)
                continue
            score = 10 - rank * 0.3
            if channel.endswith(" - Topic") and a in _key(channel):
                score += 6  # YouTube Music's album audio: the record itself
            elif a in _key(channel):
                score += 3  # the artist's own channel
            if re.search(r"official audio|\baudio\b|visuali[sz]er", title, re.I):
                score += 2
            if LIVE.search(title):
                score -= 30  # live: only when no studio version is found at all
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= 13:
            break
    if best is None:
        return None
    live = " · živě (studiová verze nenalezena)" if LIVE.search(best.get("title") or "") else ""
    try:
        out = subprocess.run([*cmd, "-f", "bestaudio[acodec=flac]/bestaudio", "--no-warnings", "--print",
                              "%(acodec)s %(abr)s", "--print", "urls",
                              f"https://www.youtube.com/watch?v={best['id']}"],
                             capture_output=True, text=True, timeout=40).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < 2 or not out[1].startswith("http"):
        return None
    codec, _, abr = out[0].partition(" ")
    try:
        what = f"{codec} {round(float(abr))} kbps"
    except ValueError:
        what = codec
    return out[1], f"{what}{live} · YouTube: {best.get('title')}"


def small_cover(url: str) -> str:
    """A cover's small version (Deezer 250 px, iTunes 300 px): enough for a list's picture."""
    return re.sub(r"/\d+x\d+-", "/250x250-", url) if "dzcdn" in url else re.sub(r"\d+x\d+bb", "300x300bb", url)


def lookup(artist: str, song: str) -> dict | None:
    """{artist, title, album, cover} from Deezer, else iTunes; None if unsure."""
    song = bare_song(song)
    best, best_score = None, 0
    q = urllib.parse.quote(f"{first_artist(artist)} {song}")
    try:
        for x in _json(f"https://api.deezer.com/search?q={q}&limit=10").get("data", []):
            sc = _score(artist, song, x["artist"]["name"], x["title"])
            if sc > best_score:
                best_score = sc
                best = {"artist": x["artist"]["name"], "title": x["title"],
                        "album": x["album"]["title"], "cover": (x["album"].get("cover_xl") or "").replace("/1000x1000-", "/1200x1200-") or None,
                        "deezer_album": x["album"].get("id")}
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
                        "album": x.get("collectionName"), "cover": art.replace("100x100bb", "1920x1920bb") or None,
                        "released": (x.get("releaseDate") or "")[:10]}
    except (OSError, ValueError, KeyError):
        pass
    if best_score >= 3:
        return best
    # not on Deezer or iTunes: a YouTube video of the song itself, else at least the artist's photo
    video = youtube_song(artist, song)
    if video:
        return video
    try:
        for x in _json(f"https://api.deezer.com/search/artist?q={urllib.parse.quote(first_artist(artist))}&limit=5"
                       ).get("data", []):
            if _key(x.get("name") or "") == _key(first_artist(artist)) and x.get("picture_xl") \
                    and not re.search(r"/artist/(d41d8cd98f00b204e9800998ecf8427e)?/", x["picture_xl"]):
                # (no photo: Deezer's grey placeholder, an empty name's hash)
                return {"artist": x["name"], "title": song, "album": None, "cover": x["picture_xl"],
                        "artist_photo": True}
    except (OSError, ValueError, KeyError):
        pass
    return youtube_artist(artist, song)


def _youtube(query: str) -> list[dict]:
    """YouTube's search results (its page's data): [{kind: video / channel, id, title, owner, thumb}]."""
    try:
        req = urllib.request.Request("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query),
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "cs,en"})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode("utf-8", "ignore")
        data = json.loads(re.search(r"var ytInitialData = (\{.*?\});</script>", page).group(1))
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    text = lambda t: "".join(x.get("text", "") for x in (t or {}).get("runs", [])) or (t or {}).get("simpleText", "")

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                out.append({"kind": "video", "id": v.get("videoId"), "title": text(v.get("title")),
                            "owner": text(v.get("ownerText"))})
            elif "channelRenderer" in o:
                c = o["channelRenderer"]
                thumbs = (c.get("thumbnail") or {}).get("thumbnails") or [{}]
                out.append({"kind": "channel", "title": text(c.get("title")), "owner": text(c.get("title")),
                            "thumb": thumbs[-1].get("url", "")})
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return out


def _video_thumb(vid: str) -> str:
    """A video's biggest still: maxres (no black bars) when it has one, else the 480 px one."""
    url = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=8):
            return url
    except OSError:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def proper_names(artist: str, song: str, info: dict | None) -> tuple[str | None, str | None]:
    """The song's names as the lookup spells them, when they're the stream's own but for marks and case
    ("ZAPOMENUTY HRICHY" → "Zapomenutý hříchy", "SKWOR" → "Škwor"); None for a name it didn't improve."""
    if not info:
        return None, None
    a = info.get("artist")
    t = plain_title(info.get("title") or "")
    sa = a if a and _key(a) == _key(first_artist(artist)) and a != artist else None
    st = t if t and _key(t) == _key(bare_song(song)) and t != song else None
    return sa, st


def youtube_song(artist: str, song: str) -> dict | None:
    """A song neither Deezer nor iTunes has (Czech bands as Špinaví lůzři): its video on YouTube, the still."""
    import difflib
    a, t = _key(first_artist(artist)), _key(song)
    if not a:
        return None
    for v in _youtube(f"{first_artist(artist)} {song}"):
        if v["kind"] != "video" or not v["id"]:
            continue
        both = _key(v["title"] + " " + v["owner"])
        name = _key(re.split(r"\s[-–|]\s", v["title"])[-1]) if re.search(r"\s[-–|]\s", v["title"]) else _key(v["title"])
        if a in both and t and (t in _key(v["title"]) or difflib.SequenceMatcher(None, t, name).ratio() >= 0.85):
            # the video's own spelling when it's the same names (with the Czech marks the stream drops)
            parts = [x.strip() for x in re.split(r"\s[-–|]\s", re.sub(r"\s*[(\[].*?[)\]]", "", v["title"]))]
            shown_t = next((x for x in parts if _key(x) == t), song)
            shown_a = next((x for x in parts + [v["owner"].removesuffix(" - Topic")] if _key(x) == a),
                           first_artist(artist))
            return {"artist": shown_a, "title": shown_t, "album": None, "cover": _video_thumb(v["id"]),
                    "youtube": True}
    return None


def youtube_artist(artist: str, song: str) -> dict | None:
    """The last resort: the artist's YouTube channel picture, else a still of one of their videos."""
    a = _key(first_artist(artist))
    if not a:
        return None
    found = _youtube(first_artist(artist))
    for c in found:
        if c["kind"] == "channel" and _key(c["title"]) == a and c.get("thumb"):
            thumb = re.sub(r"=s\d+-", "=s800-", c["thumb"])
            return {"artist": c["title"], "title": song, "album": None,
                    "cover": ("https:" + thumb) if thumb.startswith("//") else thumb, "artist_photo": True}
    for v in found:
        if v["kind"] == "video" and v["id"] and (_key(v["owner"]) == a or _key(v["title"]).startswith(a)):
            return {"artist": first_artist(artist), "title": song, "album": None, "cover": _video_thumb(v["id"]),
                    "artist_photo": True}
    return None


# --- song facts: first release and where it was recorded (MusicBrainz) ---------------

MB = "https://musicbrainz.org/ws/2"
MB_UA = "RadioBeat/1.0 ( https://github.com/kupecjan1980-pixel/radio-beat )"  # MusicBrainz wants a real one
FACTS_CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "radio-beat",
                           "facts-cs3.json")
_mb_lock = threading.Lock()
_mb_last = [0.0]
MONTHS = ("ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září", "října",
          "listopadu", "prosince")  # 8. listopadu 1971
MONTHS_1 = ("leden", "únor", "březen", "duben", "květen", "červen", "červenec", "srpen", "září", "říjen",
            "listopad", "prosinec")  # listopad 1971


def _mb(path: str):
    """One MusicBrainz request (at most one a second, as it asks; again after a 503)."""
    for attempt in range(3):
        with _mb_lock:
            time.sleep(max(0.0, _mb_last[0] + 1.1 - time.monotonic()))
            _mb_last[0] = time.monotonic()
        try:
            req = urllib.request.Request(MB + path, headers={"User-Agent": MB_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 503:
                return None
            time.sleep(2 * (attempt + 1))
        except (OSError, ValueError):
            return None
    return None


def _date(d: str) -> str:
    parts = d.split("-")
    try:
        if len(parts) == 3:
            return f"{int(parts[2])}. {MONTHS[int(parts[1]) - 1]} {parts[0]}"
        if len(parts) == 2:
            return f"{MONTHS_1[int(parts[1]) - 1]} {parts[0]}"
    except (ValueError, IndexError):
        pass
    return parts[0]


# Where a release came out: "vydáno ve Velké Británii" (MusicBrainz's country codes).
COUNTRY_IN = {"GB": "ve Velké Británii", "US": "v USA", "XC": "v Československu", "CS": "v Československu",
              "CZ": "v Česku", "SK": "na Slovensku", "DE": "v Německu", "XG": "v Německu", "FR": "ve Francii",
              "IT": "v Itálii", "NL": "v Nizozemsku", "SE": "ve Švédsku", "AU": "v Austrálii", "CA": "v Kanadě",
              "JP": "v Japonsku", "IE": "v Irsku", "PL": "v Polsku", "HU": "v Maďarsku", "AT": "v Rakousku",
              "XE": "v Evropě", "XW": "po celém světě", "DK": "v Dánsku", "NO": "v Norsku", "FI": "ve Finsku",
              "ES": "ve Španělsku", "BE": "v Belgii", "CH": "ve Švýcarsku", "SU": "v Sovětském svazu"}


# A country by name, after the studio and the city: "Nahráno v Sound City Studios, Van Nuys, California, USA".
COUNTRY_NAME = {"GB": "Velká Británie", "US": "USA", "CZ": "Česko", "SK": "Slovensko", "DE": "Německo",
                "FR": "Francie", "IT": "Itálie", "NL": "Nizozemsko", "SE": "Švédsko", "AU": "Austrálie",
                "CA": "Kanada", "JP": "Japonsko", "IE": "Irsko", "PL": "Polsko", "HU": "Maďarsko", "AT": "Rakousko",
                "DK": "Dánsko", "NO": "Norsko", "FI": "Finsko", "ES": "Španělsko", "BE": "Belgie",
                "CH": "Švýcarsko", "JM": "Jamajka", "BS": "Bahamy", "MX": "Mexiko", "BR": "Brazílie",
                "RU": "Rusko", "PT": "Portugalsko", "GR": "Řecko", "IS": "Island", "NZ": "Nový Zéland"}
_AREAS: dict = {}  # MusicBrainz area id -> (name, type, iso code, parent id)


def _area(aid: str):
    if aid not in _AREAS:
        got = _mb(f"/area/{aid}?inc=area-rels&fmt=json") or {}
        parent = next((x["area"]["id"] for x in got.get("relations", [])
                       if x.get("type") == "part of" and x.get("direction") == "backward" and x.get("area")), None)
        _AREAS[aid] = (got.get("name"), got.get("type"), (got.get("iso-3166-1-codes") or [None])[0], parent)
    return _AREAS[aid]


def _where(area: dict | None) -> list[str]:
    """An area and what it's in: the city (a district's too), the state (the top one below the country:
    California, England, Bavaria) and the country — ["Van Nuys", "California", "USA"]."""
    if not area or not area.get("id"):
        return [area["name"]] if area and area.get("name") else []
    out, state, aid = [], None, area["id"]
    for _ in range(8):
        name, kind, iso, parent = _area(aid)
        if not name:
            break
        if kind == "Country":
            out += ([state] if state else []) + [COUNTRY_NAME.get(iso, name)]
            break
        if not out or kind == "City" and name not in out:
            out.append(name)  # the place's own area (Praha 9), and the city a district is in (Praha)
        elif kind == "Subdivision":
            state = name  # the last one before the country wins
        if not parent:
            out += [state] if state else []
            break
        aid = parent
    return list(dict.fromkeys(out))


def years_ago(text: str) -> str:
    """'{ago:1971}' → 'před 55 lety' (worked out when shown, so it stays right)."""
    def ago(m):
        n = time.localtime().tm_year - int(m.group(1))
        return "letos" if n <= 0 else "před rokem" if n == 1 else f"před {n} lety"
    return re.sub(r"\{ago:(\d{4})\}", ago, text)


def _places(rels: list) -> list[str]:
    out = []
    for r in rels or []:
        if r.get("type") == "recorded at" and r.get("place"):
            pl = r["place"]
            out.append([pl["name"]] + [x for x in _where(pl.get("area")) if x not in pl["name"]])
        elif r.get("type") == "recorded in" and r.get("area"):
            out.append(_where(r["area"]))
    return [list(x) for x in dict.fromkeys(tuple(x) for x in out if x)]


def _join_places(places: list) -> str:
    """Two places at most, what they share said once: "Apple Studios a Abbey Road Studios, London, England,
    Velká Británie"."""
    places = places[:2]
    if len(places) < 2:
        return ", ".join(places[0]) if places else ""
    a, b = places
    n = 0
    while n < min(len(a), len(b)) - 1 and a[-1 - n] == b[-1 - n]:
        n += 1
    shared = a[len(a) - n:] if n else []
    return ", ".join([", ".join(a[:len(a) - n]) + " a " + ", ".join(b[:len(b) - n])] + shared)


def song_facts(artist: str, song: str) -> str:
    """'First released 8 November 1971 · Recorded at Olympic Studios, London', what MusicBrainz knows (may be
    ''), remembered per song."""
    key = f"3|{norm(first_artist(artist))}|{norm(song)}"  # (3: no release country; city, state, country)
    try:
        with open(FACTS_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    song = re.sub(r"\s*[(\[].*?[)\]]", "", song).strip() or song  # "Stairway to Heaven (Remaster)": the song
    a, t = norm(first_artist(artist)), norm(song)
    base = f'recording:"{song}" AND artist:"{first_artist(artist)}" AND status:official'

    def search(extra: str = ""):
        found = _mb(f"/recording?query={urllib.parse.quote(base + extra)}&fmt=json&limit=100")
        if found is None:
            return None
        return [r for r in found.get("recordings", [])
                if norm(r.get("title", "")) == t
                and any(a in norm(c.get("name", "")) or norm(c.get("name", "")) in a
                        for c in r.get("artist-credit", []) if isinstance(c, dict))]

    recs = search()
    if recs is None:
        return ""  # not reached: asked again next time
    # Live and later versions crowd the search: asked again for anything older than the oldest found, until
    # there's nothing older.
    for _ in range(4):
        dates = [r["first-release-date"] for r in recs if r.get("first-release-date")]
        if not dates:
            break
        older = search(f" AND firstreleasedate:{{* TO {min(dates)}}}")
        if not older:
            break
        recs += older
    # The earliest date with something behind it: a lone recording on a single release dated before all the
    # rest is a slip in the data (Paint It Black "1964"), not the first release.
    support: dict = {}
    for r in recs:
        if r.get("first-release-date"):
            support[r["first-release-date"]] = support.get(r["first-release-date"], 0) + len(r.get("releases", []))
    dates = sorted(support)
    first = next((d for d in dates if support[d] >= 2), dates[0] if dates else "")
    if first and len(first) < 10:  # only the year or month: the day from Wikidata, iTunes or Deezer (that year)
        other = earliest_release(artist, song)
        if other[:4] == first[:4] and len(other) > len(first) and other.startswith(first):
            first = other
    # (where the first release came out misleads: the Beatles' "in Canada", Škwor's "worldwide"; where it
    # was recorded is what counts, below)
    # Where: the oldest plain recordings first (not a demo or a live one).
    places = []
    for r in sorted(recs, key=lambda r: (bool(r.get("disambiguation")), -len(r.get("releases", [])),
                                         r.get("first-release-date") or "9999"))[:6]:
        got = _mb(f"/recording/{r['id']}?inc=place-rels+area-rels&fmt=json")
        places = _places((got or {}).get("relations"))
        if places:
            break
    if not places:  # the studio is often on the release (the album) rather than on the recording
        firsts = sorted((rel for r in recs for rel in r.get("releases", []) if rel.get("date")),
                        key=lambda rel: rel["date"])[:3]
        for rel in firsts:
            got = _mb(f"/release/{rel['id']}?inc=place-rels+area-rels&fmt=json")
            places = _places((got or {}).get("relations"))
            if places:
                break
    where = f"v {_join_places(places)}" if places else ""
    if not where:  # no studio known: the artist's country (a band records at home, as a rule)
        ids = [c["artist"]["id"] for r in recs for c in r.get("artist-credit", [])
               if isinstance(c, dict) and c.get("artist", {}).get("id")
               and (a in norm(c.get("name", "")) or norm(c.get("name", "")) in a)]
        if ids:
            home = (_mb(f"/artist/{max(set(ids), key=ids.count)}?fmt=json") or {}).get("country")
            where = COUNTRY_IN.get(home, "") if home not in ("XW", "XE") else ""
    bits = ([f"Poprvé vydáno {_date(first)} ({{ago:{first[:4]}}})"] if first else []) + \
           ([f"Nahráno {where}"] if where else [])
    text = " · ".join(bits)
    cache[key] = text
    try:
        os.makedirs(os.path.dirname(FACTS_CACHE), exist_ok=True)
        if len(cache) > 3000:
            cache = dict(list(cache.items())[-2000:])
        with open(FACTS_CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(FACTS_CACHE + ".tmp", FACTS_CACHE)
    except OSError:
        pass
    return text


# --- about the song (Wikipedia), for the show card's song turn ---------------------------

ABOUT_CACHE = os.path.join(os.path.dirname(FACTS_CACHE), "about-cs.json")


def to_czech(text: str) -> str:
    """English → Czech through MyMemory (a free translation service: the text is sent there); '' if it can't."""
    out, chunk = [], ""
    for sentence in sentences(text):
        if len(chunk) + len(sentence) > 450 and chunk:
            out.append(chunk)
            chunk = ""
        chunk = f"{chunk} {sentence}".strip()
    if chunk:
        out.append(chunk)
    done = []
    for part in out:
        try:
            r = _json(f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(part)}&langpair=en|cs")
            t = (r.get("responseData") or {}).get("translatedText") or ""
        except (OSError, ValueError):
            return ""
        if not t or "MYMEMORY WARNING" in t.upper():
            return ""
        done.append(t)
    return " ".join(done)


# Ostravština (default; a switch for standard Czech): the long vowels short, a few of its words.
OSTRAVA_WORDS = {"velmi": "fest", "hodně": "fest", "moc": "fest", "dobrý": "fajny", "dobrá": "fajna",
                 "dobré": "fajne", "dobře": "fajně", "pěkný": "fajny", "pěkná": "fajna", "pěkné": "fajne",
                 "teď": "ted", "kamarád": "kamoš", "kamarádi": "kamoši", "taky": "tež", "také": "tež"}
_SHORT = str.maketrans("áéíýúůÁÉÍÝÚŮ", "aeiyuuAEIYUU")


def ostrava(text: str) -> str:
    def word(m):
        w = m.group(0)
        o = OSTRAVA_WORDS.get(w.lower())
        return w if o is None else (o.capitalize() if w[0].isupper() else o)
    return re.sub(r"\w+", word, text).translate(_SHORT)
ABOUT_LEN = 700  # characters at most, cut at a sentence's end (more than fits: it scrolls while typed)


def _wiki(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})  # Wikipedia wants one with a contact too
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


# Czech and English abbreviations a sentence doesn't end at (tzv. jazz, St. Louis); a number before the dot is
# a date or an order (1. května 1997, 60. let) and doesn't end one either.
ABBR = {"tzv", "např", "resp", "atd", "apod", "tj", "mj", "str", "č", "čs", "sv", "st", "ing", "mgr", "dr", "prof",
        "nar", "zem", "ml", "jr", "sr", "mr", "mrs", "ms", "vs", "feat", "no", "vol", "op", "ca", "inc", "ltd",
        "co", "mt", "jan", "feb", "aug", "sept", "oct", "nov", "dec", "angl", "něm", "fr", "lat", "pozn", "srov"}


def sentences(text: str) -> list[str]:
    out, start = [], 0
    for m in re.finditer(r"[.!?…][\"“”»)]?\s+(?=[„\"“«(]?[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9])", text):
        if text[m.start()] == ".":
            last = re.search(r"(\S+)$", text[start:m.start()])
            w = last.group(1).lower().strip("(„\"“") if last else ""
            if w.isdigit() or w in ABBR or (len(w) == 1 and w.isalpha()) or re.fullmatch(r"[ivxlc]+", w):
                continue
        out.append(text[start:m.start() + 1].strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def _trim_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for sentence in sentences(text):
        if len(out) + len(sentence) + 1 > ABOUT_LEN:
            break
        out = f"{out} {sentence}".strip()
    return out or text[:ABOUT_LEN].rsplit(" ", 1)[0] + "…"


ARTICLE_END = ("Reference", "Odkazy", "Externí odkazy", "Související články", "Literatura", "Poznámky",
               "See also", "References", "External links", "Further reading", "Notes", "Bibliography")


def _prose(x: str) -> bool:
    """A sentence, not a list's line (a discography: years, brackets, catalogue numbers)."""
    digits = sum(c.isdigit() for c in x)
    return digits / len(x) < 0.12 and x.count("(") <= 2 and len(x.split()) >= 7


def wiki_sentences(name: str, words: tuple, langs=("cs", "en")) -> dict:
    """Everything the Wikipedia article named after `name` says, as sentences (for the show card's mix), and its
    picture: {"lang": "cs" | "en", "s": [...], "img": url}, Czech first; {} when there's no such article.
    Remembered."""
    key = f"sent3|{norm(name)}"
    try:
        with open(ABOUT_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    n, found, reached = norm(name), {}, False
    for lang in langs:
        try:
            hits = _wiki(f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
                         f"&srsearch={urllib.parse.quote(name)}&format=json&srlimit=10")["query"]["search"]
            reached = True
            for hit in hits:
                if norm(re.sub(r"\(.*?\)", "", hit["title"])) != n:
                    continue
                q = urllib.parse.quote(hit["title"])
                pages = _wiki(f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=extracts|pageprops|pageimages"
                              f"&explaintext=1&exsectionformat=plain&piprop=thumbnail&pithumbsize=320&redirects=1"
                              f"&titles={q}&format=json")
                page = next(iter(pages["query"]["pages"].values()))
                if "disambiguation" in (page.get("pageprops") or {}):
                    continue
                text = page.get("extract", "")
                if not any(w in text[:1500].lower() for w in words):
                    continue
                kept = []
                for line in text.split("\n"):
                    line = line.strip()
                    if line in ARTICLE_END:
                        break
                    if len(line) > 60 and "." in line:  # a paragraph (not a heading or a list item)
                        kept += [x for x in sentences(line) if 40 <= len(x) <= 260 and _prose(x)]
                if kept:  # with the article's picture (the band, the artist), for beside the text
                    found = {"lang": lang, "s": kept[:200], "img": (page.get("thumbnail") or {}).get("source", "")}
                    break
        except (OSError, ValueError, KeyError, StopIteration):
            continue
        if found:
            break
    if not reached:
        return {}
    cache[key] = found
    try:
        os.makedirs(os.path.dirname(ABOUT_CACHE), exist_ok=True)
        with open(ABOUT_CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(ABOUT_CACHE + ".tmp", ABOUT_CACHE)
    except OSError:
        pass
    return found


TRANSLATED = os.path.join(os.path.dirname(FACTS_CACHE), "translated-cs.json")
_translate_off = [0.0]  # MyMemory's free daily limit reached: until when


def czech_sentence(text: str) -> str:
    """One English sentence in Czech (remembered); '' when MyMemory's free daily limit is used up."""
    try:
        with open(TRANSLATED) as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    if text in done:
        return done[text]
    if time.time() < _translate_off[0]:
        return ""
    cs = to_czech(text)
    if not cs:
        _translate_off[0] = time.time() + 3600  # try again in an hour
        return ""
    done[text] = cs
    try:
        with open(TRANSLATED + ".tmp", "w") as f:
            json.dump(done, f)
        os.replace(TRANSLATED + ".tmp", TRANSLATED)
    except OSError:
        pass
    return cs


def song_about(artist: str, song: str) -> str:
    """A few sentences about the song from Wikipedia (English, else Czech): its article's summary, only when it
    names the artist (so it's this song); '' when there's none. Remembered per song."""
    key = f"{norm(first_artist(artist))}|{norm(song)}"
    try:
        with open(ABOUT_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    a, t = norm(first_artist(artist)), norm(song)
    text, reached = "", False
    for lang, word in (("cs", "píseň"), ("en", "song")):
        try:
            q = urllib.parse.quote(f"{song} {first_artist(artist)} {word}")
            hits = _wiki(f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}"
                         "&format=json&srlimit=5")["query"]["search"]
            reached = True
            for hit in hits:
                if t not in norm(hit["title"]):
                    continue
                summary = _wiki(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                                f"{urllib.parse.quote(hit['title'].replace(' ', '_'))}")
                extract = summary.get("extract", "")
                if a and a in norm(extract):
                    text = _trim_text(extract) if lang == "cs" else _trim_text(to_czech(extract) or "")
                    break
        except (OSError, ValueError, KeyError):
            continue
        if text:
            break
    if not reached:
        return ""  # offline: asked again next time
    cache[key] = text
    try:
        os.makedirs(os.path.dirname(ABOUT_CACHE), exist_ok=True)
        if len(cache) > 3000:
            cache = dict(list(cache.items())[-2000:])
        with open(ABOUT_CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(ABOUT_CACHE + ".tmp", ABOUT_CACHE)
    except OSError:
        pass
    return text


MUSIC_WORDS = ("band", "singer", "musician", "group", "rock", "songwriter", "guitarist", "drummer", "rapper",
               "skupina", "zpěvák", "zpěvačka", "hudeb", "kapela", "písničkář", "kytarist")
# Artists Rádio Beat plays (classic rock and Czech bigbeat): the mix draws on them until the history has enough
# of its own (a new install, or the stream sending no song names, as it does at night).
SEED_ARTISTS = ("Olympic", "Pražský výběr", "Katapult", "Žlutý pes", "Lucie", "Kabát", "Visací zámek",
                "Blue Effect", "Flamengo", "Synkopy 61", "Michal Prokop", "Vladimír Mišík", "Petr Kalandra",
                "Led Zeppelin", "Deep Purple", "Pink Floyd", "The Rolling Stones", "The Beatles", "Queen", "AC/DC",
                "Uriah Heep", "Jethro Tull", "Black Sabbath", "Status Quo", "Smokie", "Nazareth", "Eagles",
                "Creedence Clearwater Revival", "Dire Straits", "Whitesnake", "Rainbow", "Scorpions", "The Doors",
                "Jimi Hendrix", "Santana", "ZZ Top", "Alice Cooper", "Cream", "The Who", "Yes", "Genesis",
                "Free", "Thin Lizzy", "Sweet", "Slade", "T. Rex", "Kiss", "Aerosmith", "Lynyrd Skynyrd",
                "Golden Earring", "Bob Dylan", "Eric Clapton", "Rory Gallagher", "Manfred Mann's Earth Band")
SEED_UNTIL = 15  # artists of its own in the history

HOST_WORDS = ("moderátor", "moderátorka", "rozhlas", "radio", "rádio", "presenter", "broadcaster", "dj ", "kritik",
              "critic", "publicist", "novinář", "hudebník", "zpěvák", "zpěvačka", "musician", "singer", "journalist")


def name_about(name: str, words: tuple, langs=("en", "cs")) -> str:
    """A few sentences (Czech) from the Wikipedia article named after `name` (a band, a host), only when it
    reads like one of `words`; '' when there's none. Remembered."""
    key = f"name|{norm(name)}"
    try:
        with open(ABOUT_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    n, text, reached = norm(name), "", False
    for lang in langs:
        try:
            hits = _wiki(f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
                         f"&srsearch={urllib.parse.quote(name)}&format=json&srlimit=10")["query"]["search"]
            reached = True
            for hit in hits:
                if norm(re.sub(r"\(.*?\)", "", hit["title"])) != n:
                    continue
                summary = _wiki(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                                f"{urllib.parse.quote(hit['title'].replace(' ', '_'))}")
                if summary.get("type") == "disambiguation":  # "may refer to": the right one is further down
                    continue
                extract = summary.get("extract", "")
                if any(w in extract.lower() for w in words):
                    text = _trim_text(extract) if lang == "cs" else _trim_text(to_czech(extract) or "")
                    break
        except (OSError, ValueError, KeyError):
            continue
        if text:
            break
    if not reached:
        return ""
    cache[key] = text
    try:
        os.makedirs(os.path.dirname(ABOUT_CACHE), exist_ok=True)
        with open(ABOUT_CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(ABOUT_CACHE + ".tmp", ABOUT_CACHE)
    except OSError:
        pass
    return text


# --- what was played, and by whom (a database on this computer, newest first) ----------------

class History:
    """Every song played (when, the artist, the title, the album, the show, who was on air) and every host
    seen (their shows, when last on), in SQLite under ~/.local/share/radio-beat. Every play is kept (the 'i'
    panel's HRÁLO lists them all) and shared with the other computers (CacheShare). Feeds the show card's mix
    too: artists played earlier, all the station's hosts."""

    SAME = 1800  # s: the same song this close in time is the same play (each computer logs the stream itself;
    # a long song may be named again after 10 minutes)

    def __init__(self, path: str):
        import sqlite3
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS plays (at REAL, artist TEXT, title TEXT, album TEXT, "
                            "show TEXT, host TEXT)")
            self.db.execute("CREATE INDEX IF NOT EXISTS plays_at ON plays (at DESC)")
            self.db.execute("CREATE TABLE IF NOT EXISTS hosts (name TEXT PRIMARY KEY, shows TEXT, first REAL, "
                            "last REAL)")
            # when this computer looked a song's picture or track info up (the other computers get it then)
            self.db.execute("CREATE TABLE IF NOT EXISTS found (artist TEXT, title TEXT, at REAL, "
                            "PRIMARY KEY (artist, title))")
            # a song's names as spelled properly (the stream drops the Czech marks: "ZAPOMENUTY HRICHY" is
            # "Zapomenutý hříchy"), from the lookup; NULL: looked up, nothing better found
            self.db.execute("CREATE TABLE IF NOT EXISTS names (artist TEXT, title TEXT, shown_artist TEXT, "
                            "shown_title TEXT, PRIMARY KEY (artist, title))")
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(plays)")]
            if "cover" not in cols:
                self.db.execute("ALTER TABLE plays ADD COLUMN cover TEXT")  # its picture ("": none found)
            if "facts" not in cols:
                self.db.execute("ALTER TABLE plays ADD COLUMN facts TEXT")  # first release, where ("": none)
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:  # once: a song logged twice (the app
                kept: dict = {}                                             # restarted during it) kept once
                gone = []
                for rowid, at, artist, title in self.db.execute("SELECT rowid, at, artist, title FROM plays "
                                                                "ORDER BY at").fetchall():
                    song = ((artist or "").lower(), (title or "").lower())
                    if song in kept and at - kept[song] < self.SAME:
                        gone.append((rowid,))
                    else:
                        kept[song] = at
                self.db.executemany("DELETE FROM plays WHERE rowid = ?", gone)
                self.db.execute("PRAGMA user_version = 1")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:  # once more: the wider SAME, and a song
                kept4: dict = {}                                            # right after itself is one play
                prev, gone4 = None, []
                for rowid, at, artist, title in self.db.execute("SELECT rowid, at, artist, title FROM plays "
                                                                "ORDER BY at").fetchall():
                    song = ((artist or "").lower(), (title or "").lower())
                    if (song == prev and at - kept4[song] < 3 * 3600) or (song in kept4 and at - kept4[song] < self.SAME):
                        gone4.append((rowid,))
                    else:
                        kept4[song] = at
                    prev = song
                self.db.executemany("DELETE FROM plays WHERE rowid = ?", gone4)
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:  # once: songs not found before looked up
                self.db.execute("UPDATE plays SET cover = NULL WHERE cover = ''")  # again (a better match, YouTube)
                self.db.execute("PRAGMA user_version = 3")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
                self.db.execute("PRAGMA user_version = 4")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 5:  # once: track info looked up again where
                stale = re.compile(r"Poprvé vydáno [^(·]*? (%s) \(" % "|".join(map(re.escape, COUNTRY_IN.values())))
                redo = [(rowid,) for rowid, facts in self.db.execute(  # it said where the release came out
                    "SELECT rowid, facts FROM plays WHERE facts IS NOT NULL") if stale.search(facts)]
                self.db.executemany("UPDATE plays SET facts = NULL WHERE rowid = ?", redo)
                # and songs with a year after the name ("Green Onions 1962"): not found then, looked up again
                self.db.execute("UPDATE plays SET cover = NULL, album = NULL, facts = NULL WHERE title GLOB "
                                "'*[12][90][0-9][0-9]' OR title GLOB '*([12][90][0-9][0-9])'")
                self.db.execute("PRAGMA user_version = 5")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 6:  # once: all track info looked up again
                self.db.execute("UPDATE plays SET facts = NULL")  # (the full date; studio, city, state, country)
                self.db.execute("PRAGMA user_version = 6")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 7:  # once: what was looked up before sharing
                self.db.execute("INSERT OR IGNORE INTO found SELECT lower(artist), lower(title), ? FROM plays "  # it
                                "WHERE cover IS NOT NULL OR facts IS NOT NULL GROUP BY lower(artist), lower(title)",
                                (time.time(),))
                self.db.execute("PRAGMA user_version = 7")

    def play(self, artist: str, title: str, show: str | None, host: str | None) -> None:
        import sqlite3
        now = time.time()
        try:
            with self.lock, self.db:
                last = self.db.execute("SELECT lower(artist), lower(title), at FROM plays ORDER BY at DESC LIMIT 1").fetchone()
                if last and (last[0], last[1]) == (artist.lower(), title.lower()) and now - last[2] < 3 * 3600:
                    return  # the same song as the last one: named again (a long song) or the app restarted
                if self.db.execute("SELECT 1 FROM plays WHERE at > ? AND lower(artist) = lower(?) AND lower(title) = "
                                   "lower(?)", (now - self.SAME, artist, title)).fetchone():
                    return  # logged already
                self.db.execute("INSERT INTO plays VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL)",
                                (now, artist, title, show, host))
        except sqlite3.Error as e:  # a full disk: nothing is deleted, this play just isn't kept here (the
            print(f"radio-beat: play not logged: {e}", file=sys.stderr)  # other computer has it)
            return
        if self.on_play:
            self.on_play()

    on_play = None  # called after a play is logged (the HRÁLO view shows it)

    def page(self, before: float, n: int = 60) -> list[tuple]:
        """(at, artist, title, album, show, cover, facts, shown artist, shown title) of the plays before `before`,
        newest first (the shown names: properly spelled, else the stream's)."""
        with self.lock:
            return self.db.execute(
                "SELECT p.at, p.artist, p.title, p.album, p.show, p.cover, p.facts, "
                "coalesce(n.shown_artist, p.artist), coalesce(n.shown_title, p.title) FROM plays p LEFT JOIN names n "
                "ON n.artist = lower(p.artist) AND n.title = lower(p.title) WHERE p.at < ? ORDER BY p.at DESC LIMIT ?",
                (before, n)).fetchall()

    def set_names(self, artist: str, title: str, shown_artist: str | None, shown_title: str | None,
                  mine: bool = True) -> None:
        """The song's properly spelled names (None: none better), noted as found here to share them."""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO names VALUES (lower(?), lower(?), ?, ?)",
                            (artist, title, shown_artist, shown_title))
            if mine:
                self._found(artist, title)

    def missing(self) -> list[tuple]:
        """(artist, title) of the songs some play of which still lacks its picture or track info, newest first."""
        with self.lock:
            return self.db.execute(
                "SELECT p.artist, p.title, max(p.cover IS NULL), max(p.facts IS NULL) FROM plays p LEFT JOIN names n "
                "ON n.artist = lower(p.artist) AND n.title = lower(p.title) "
                "WHERE p.cover IS NULL OR p.facts IS NULL OR n.artist IS NULL "
                "GROUP BY lower(p.artist), lower(p.title) ORDER BY max(p.at) DESC").fetchall()

    def set_facts(self, artist: str, title: str, facts: str) -> None:
        """A song's first release and such ("" when nothing's known): every play of it lacking them."""
        with self.lock, self.db:
            self.db.execute("UPDATE plays SET facts = ? WHERE lower(artist) = lower(?) AND lower(title) = lower(?) "
                            "AND (facts IS NULL OR (facts = '' AND ? != ''))", (facts, artist, title, facts))
            self._found(artist, title)

    def song_art(self, artist: str, title: str, album: str | None, cover: str) -> None:
        """A song's album and picture, looked up later ("" when none was found): every play of it lacking them."""
        with self.lock, self.db:
            self.db.execute("UPDATE plays SET cover = ? WHERE lower(artist) = lower(?) AND lower(title) = lower(?) "
                            "AND cover IS NULL", (cover, artist, title))
            if album:
                self.db.execute("UPDATE plays SET album = ? WHERE lower(artist) = lower(?) AND lower(title) = "
                                "lower(?) AND album IS NULL", (album, artist, title))
            self._found(artist, title)

    def _found(self, artist: str, title: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO found VALUES (lower(?), lower(?), ?)", (artist, title, time.time()))

    def lacks(self, artist: str, title: str) -> tuple[bool, bool, bool]:
        """Whether some play of the song still lacks its picture, its track info, and whether its names were
        never looked up (another computer may have sent them meanwhile)."""
        with self.lock:
            got = self.db.execute("SELECT max(cover IS NULL), max(facts IS NULL) FROM plays WHERE lower(artist) = "
                                  "lower(?) AND lower(title) = lower(?)", (artist, title)).fetchone()
            named = self.db.execute("SELECT 1 FROM names WHERE artist = lower(?) AND title = lower(?)",
                                    (artist, title)).fetchone()
        return bool(got and got[0]), bool(got and got[1]), not named

    def info_export(self, since: float, n: int = 2000) -> list[list]:
        """Songs this computer looked up after `since`, oldest first: [artist, title, album, cover, facts, at]."""
        with self.lock:
            rows = self.db.execute("SELECT artist, title, at FROM found WHERE at > ? ORDER BY at LIMIT ?",
                                   (since, n)).fetchall()
            out = []
            for artist, title, at in rows:
                got = self.db.execute("SELECT album, cover, facts FROM plays WHERE lower(artist) = ? AND "
                                      "lower(title) = ? ORDER BY (cover IS NULL) + (facts IS NULL), at DESC LIMIT 1",
                                      (artist, title)).fetchone()
                named = self.db.execute("SELECT shown_artist, shown_title FROM names WHERE artist = ? AND title = ?",
                                        (artist, title)).fetchone()
                if got:
                    out.append([artist, title, *got, at, *(named or (None, None))])
            return out

    def info_merge(self, rows) -> int:
        """Pictures and track info another computer looked up: every play here lacking them takes them (not
        noted as found here: not sent back). Returns how many songs got something."""
        import sqlite3
        n = 0
        try:
            with self.lock, self.db:
                for r in rows:
                    if not (isinstance(r, list) and len(r) in (6, 8) and all(isinstance(x, str) and len(x) <= 300
                                                                             for x in r[:2])
                            and all(x is None or (isinstance(x, str) and len(x) <= 500) for x in r[2:5] + r[6:])):
                        continue
                    artist, title, album, cover, facts, _ = r[:6]
                    if len(r) == 8 and (r[6] or r[7]):  # its proper names
                        self.db.execute("INSERT OR IGNORE INTO names VALUES (lower(?), lower(?), ?, ?)",
                                        (artist, title, r[6], r[7]))
                    if cover and not cover.startswith("https://"):
                        cover = None
                    song = "lower(artist) = ? AND lower(title) = ?"
                    c = self.db.total_changes
                    if cover is not None:
                        self.db.execute(f"UPDATE plays SET cover = ? WHERE {song} AND cover IS NULL",
                                        (cover, artist, title))
                    if album:
                        self.db.execute(f"UPDATE plays SET album = ? WHERE {song} AND album IS NULL",
                                        (album, artist, title))
                    if facts is not None:
                        self.db.execute(f"UPDATE plays SET facts = ? WHERE {song} AND (facts IS NULL OR "
                                        "(facts = '' AND ? != ''))", (facts, artist, title, facts))
                    n += self.db.total_changes != c
        except sqlite3.Error as e:
            print(f"radio-beat: track info not merged: {e}", file=sys.stderr)
        return n

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]

    def export(self, since: float, n: int = 2000) -> list[list]:
        """Plays after `since`, oldest first: [at, artist, title, album, show, host, cover] (for the other
        computers)."""
        with self.lock:
            return [list(r) for r in self.db.execute("SELECT at, artist, title, album, show, host, cover, facts FROM "
                                                     "plays WHERE at > ? ORDER BY at LIMIT ?", (since, n)).fetchall()]

    def merge(self, rows) -> int:
        """Plays from another computer: each kept unless this one logged it too (the same song within SAME s;
        its album taken if this one hasn't one). Returns how many were new."""
        import sqlite3
        new = 0
        try:
            return self._merge(rows)
        except sqlite3.Error as e:  # a full disk: nothing deleted, the rest is fetched another time
            print(f"radio-beat: plays not merged: {e}", file=sys.stderr)
            return 0

    def _merge(self, rows) -> int:
        new = 0
        with self.lock, self.db:
            for r in rows:
                if isinstance(r, list) and 6 <= len(r) < 8:
                    r = r + [None] * (8 - len(r))  # an older Rádio Beat: no picture, no facts
                if not (isinstance(r, list) and len(r) == 8 and isinstance(r[0], (int, float))
                        and all(isinstance(x, str) and len(x) <= 300 for x in r[1:3])
                        and all(x is None or (isinstance(x, str) and len(x) <= 500) for x in r[3:])
                        and 1e9 < r[0] < time.time() + 86400):
                    continue
                at, artist, title, album, show, host, cover, facts = r
                if cover and not cover.startswith("https://"):
                    cover = None
                same = self.db.execute("SELECT rowid, album, cover, facts FROM plays WHERE at BETWEEN ? AND ? AND "
                                       "lower(artist) = lower(?) AND lower(title) = lower(?) LIMIT 1",
                                       (at - self.SAME, at + self.SAME, artist, title)).fetchone()
                if same:
                    if album and not same[1]:
                        self.db.execute("UPDATE plays SET album = ? WHERE rowid = ?", (album, same[0]))
                    if cover and not same[2]:
                        self.db.execute("UPDATE plays SET cover = ? WHERE rowid = ?", (cover, same[0]))
                    if facts and not same[3]:
                        self.db.execute("UPDATE plays SET facts = ? WHERE rowid = ?", (facts, same[0]))
                    continue
                self.db.execute("INSERT INTO plays VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (at, artist, title, album, show, host, cover, facts))
                new += 1
        return new

    def album(self, artist: str, title: str, album: str, cover: str | None = None) -> None:
        """The album and its picture, once the lookup found them (the newest play of that song)."""
        with self.lock, self.db:
            self.db.execute("UPDATE plays SET album = ?, cover = coalesce(?, cover) WHERE rowid = (SELECT rowid FROM "
                            "plays WHERE artist = ? AND title = ? ORDER BY at DESC LIMIT 1)", (album, cover, artist, title))

    def host(self, name: str, show: str | None) -> None:
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute("SELECT shows FROM hosts WHERE name = ?", (name,)).fetchone()
            shows = [x for x in (row[0].split("|") if row and row[0] else []) if x]
            if show and show not in shows:
                shows.append(show)
            self.db.execute("INSERT INTO hosts VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET shows = "
                            "excluded.shows, last = excluded.last", (name, "|".join(shows[-8:]), now, now))

    def artists(self, n: int = 40) -> list[str]:
        """The artists played lately, newest first (each once)."""
        with self.lock:
            rows = self.db.execute("SELECT artist, MAX(at) m FROM plays GROUP BY lower(artist) ORDER BY m DESC "
                                   "LIMIT ?", (n,)).fetchall()
        return [r[0] for r in rows]

    def since(self, seconds: float) -> tuple[list[str], list[tuple[str, str]]]:
        """The artists and the songs played within the last `seconds`, newest first (each once)."""
        t = time.time() - seconds
        with self.lock:
            rows = self.db.execute("SELECT artist, title, MAX(at) m FROM plays WHERE at >= ? GROUP BY lower(artist), "
                                   "lower(title) ORDER BY m DESC", (t,)).fetchall()
        artists = list(dict.fromkeys(r[0] for r in rows))
        return artists, [(r[0], r[1]) for r in rows]

    def songs(self, n: int = 30) -> list[tuple[str, str]]:
        """(artist, title) of the songs played lately, newest first (each once)."""
        with self.lock:
            rows = self.db.execute("SELECT artist, title, MAX(at) m FROM plays GROUP BY lower(artist), lower(title) "
                                   "ORDER BY m DESC LIMIT ?", (n,)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def hosts(self) -> list[tuple[str, list[str]]]:
        """Every host seen, the latest first, with their shows."""
        with self.lock:
            rows = self.db.execute("SELECT name, shows FROM hosts ORDER BY last DESC").fetchall()
        return [(r[0], [x for x in (r[1] or "").split("|") if x]) for r in rows]


_EARLIEST: dict = {}  # song -> its earliest release date found (this run)
COMPILATION = re.compile(r"greatest|best of|hits|collection|anthology|essential|gold\b|ultimate|cross road|"
                         r"the very|definitive|platinum|story|years|antologie|výběr|to nej", re.I)


_WD_PLACES: dict = {}  # (artist, song) keys -> where Wikidata says the song was recorded


def wikidata_release(artist: str, song: str) -> str:
    """The song's original release from Wikidata (the database behind Wikipedia): "1992-05-12", "1992-05" or
    "1992" as precise as it's known, the earliest of the song's items by that performer (the song, its single);
    "" when it isn't there."""
    a, t = _key(first_artist(artist)), _key(song)
    api = "https://www.wikidata.org/w/api.php?format=json&"

    def get(q):
        req = urllib.request.Request(api + q, headers={"User-Agent": MB_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    try:
        found = get("action=query&list=search&srlimit=8&srsearch=" + urllib.parse.quote(f"{song} {first_artist(artist)}"))
        ids = [x["title"] for x in found["query"]["search"] if x["title"].startswith("Q")]
        if not ids:
            return ""
        items = get("action=wbgetentities&props=labels|claims&languages=en|cs&ids=" + "|".join(ids))["entities"]
        val = lambda c, p: [v["mainsnak"].get("datavalue", {}).get("value") for v in c.get(p, [])]
        named = {}
        for qid, it in items.items():
            labels = [l["value"] for l in it.get("labels", {}).values()]
            if any(_key(part) == t for l in labels for part in re.split(r"\s+/\s+", l)):  # "Bed of Roses / ..."
                named[qid] = it
        perf_ids = {v["id"] for it in named.values() for v in val(it.get("claims", {}), "P175") if v}
        if not perf_ids:
            return ""
        perfs = get("action=wbgetentities&props=labels&languages=en|cs&ids=" + "|".join(sorted(perf_ids)[:40]))["entities"]
        by = {pid for pid, p in perfs.items()
              if any(a in _key(l["value"]) or _key(l["value"]) in a for l in p.get("labels", {}).values())}
        dates, places = [], []
        for it in named.values():
            c = it.get("claims", {})
            if any(v and v["id"] in by for v in val(c, "P175")):
                for v in val(c, "P577"):
                    m = re.match(r"\+(\d{4})-(\d\d)-(\d\d)", (v or {}).get("time", ""))
                    if m:
                        dates.append("-".join(x for x in m.groups() if x != "00"))
                places += [v["id"] for v in val(c, "P483") if v and v["id"] not in places]  # recorded at
        if places:  # the studios' names
            got = get("action=wbgetentities&props=labels&languages=cs|en&ids=" + "|".join(places[:4]))["entities"]
            names = [(p.get("labels", {}).get("cs") or p.get("labels", {}).get("en") or {}).get("value")
                     for p in (got[q] for q in places[:4] if q in got)]
            _WD_PLACES[(a, t)] = [n for n in names if n][:2]
        return min(dates, key=lambda d: (d + "-00-00")[:10]) if dates else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def earliest_release(artist: str, song: str) -> str:
    """The song's first release (YYYY-MM-DD) when MusicBrainz can't be asked: the earliest date among iTunes's
    and Deezer's releases of it (the original album, singles, reissues, compilations: the oldest counts; live
    versions don't). The album the lookup matched is often a later reissue (Nirvana "13 years ago").
    Wikidata first: it has the original date of well-known songs."""
    song = re.sub(r"\s*[(\[].*?[)\]]", "", song).strip() or song
    a, t = _key(first_artist(artist)), _key(song)
    if (a, t) in _EARLIEST:
        return _EARLIEST[(a, t)]
    known = wikidata_release(artist, song)  # what Wikipedia's data says (its song, or only its single)
    q = urllib.parse.quote(f"{first_artist(artist)} {song}")
    same = lambda ca, ct: a and (a in _key(ca) or _key(ca) in a) and _key(ct) == t and "live" not in ct.lower()
    dates = []
    try:
        for x in _json(f"https://itunes.apple.com/search?term={q}&entity=song&limit=50").get("results", []):
            album = x.get("collectionName") or ""
            if same(x.get("artistName", ""), x.get("trackName", "")) and "live" not in album.lower() \
                    and not COMPILATION.search(album):  # (iTunes dates compilations wrong: Bon Jovi's "1983")
                dates.append((x.get("releaseDate") or "")[:10])
    except (OSError, ValueError):
        pass
    try:
        albums = []
        for x in _json(f"https://api.deezer.com/search?q={q}&limit=25").get("data", []):
            if same(x["artist"]["name"], x["title"]) and "live" not in x["album"]["title"].lower() \
                    and not COMPILATION.search(x["album"]["title"]) and x["album"]["id"] not in albums:
                albums.append(x["album"]["id"])
        for aid in albums[:8]:
            dates.append((_json(f"https://api.deezer.com/album/{aid}").get("release_date") or "")[:10])
    except (OSError, ValueError, KeyError):
        pass
    dates = [d for d in dates if re.match(r"(19|20)\d\d-\d\d-\d\d$", d)] + ([known] if known else [])
    if not dates:
        _EARLIEST[(a, t)] = ""
        return ""
    # the earliest year of all (Wikidata may know only the single: Lithium's 1992, the album 1991); in it, the
    # exact day when one source has it (Wikidata may know only the year)
    year = min(d[:4] for d in dates)
    _EARLIEST[(a, t)] = min((d for d in dates if d[:4] == year), key=lambda d: (-len(d), d))
    return _EARLIEST[(a, t)]


def track_facts(artist: str, song: str, info: dict | None) -> str:
    """The song's line under the album: its first release and where it was recorded ('Poprvé vydáno 24. září
    1991 …'), else when its album came out; "" when nothing's known."""
    song = bare_song(song)
    # asked with the names the lookup found (the stream's may be misspelt: "PAVLOVS ´DOG")
    facts = song_facts(info["artist"], info["title"]) if info and not info.get("artist_photo") else ""
    if not facts:
        facts = song_facts(artist, song)
    if not facts:  # MusicBrainz not reached / no answer: the earliest release Wikidata, iTunes and Deezer know
        day = earliest_release(info["artist"], info["title"]) if info and not info.get("artist_photo") else ""
        day = day or earliest_release(artist, song)
        if day:
            facts = f"Poprvé vydáno {_date(day)} ({{ago:{day[:4]}}})"
        elif info and (day := album_released(info)):
            facts = f"Album vydáno {_date(day)} ({{ago:{day[:4]}}})"
        places = [p for n in ([(info["artist"], info["title"])] if info and not info.get("artist_photo") else [])
                  + [(artist, song)]
                  for p in _WD_PLACES.get((_key(first_artist(n[0])), _key(re.sub(r"\s*[(\[].*?[)\]]", "", n[1])
                                                                          .strip() or n[1])), [])]
        if places:
            facts = " · ".join(x for x in (facts, f"Nahráno v {' a '.join(dict.fromkeys(places).keys())}") if x)
    return facts


def album_released(info: dict) -> str:
    """The album's release date (YYYY-MM-DD) from the lookup: iTunes has it, Deezer's album page does."""
    if info.get("released"):
        return info["released"]
    if info.get("deezer_album"):
        try:
            return (_json(f"https://api.deezer.com/album/{info['deezer_album']}").get("release_date") or "")[:10]
        except (OSError, ValueError):
            pass
    return ""


COVER_PX = 4096


def square_cover(data: bytes | None) -> bytes | None:
    """A cover that isn't square (a YouTube video's still: 16:9, black bars, a timestamp or captions at its
    edges) made square, at its own resolution (COVER_PX at most): the black bars found and cut off, then a wide picture's middle square (its
    edges' text cut off with them), a tall one whole with its sides the picture itself blurred and darkened.
    Square ones as they are."""
    if not data:
        return data
    from gi.repository import GdkPixbuf
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        w, h = pb.get_width(), pb.get_height()
    except (GLib.Error, AttributeError):
        return data
    ff = shutil.which("ffmpeg") or os.path.join(DATA_DIR, "ffmpeg", "ffmpeg")
    if abs(w - h) <= max(w, h) * 0.03 or not os.access(ff, os.X_OK):
        return data
    bars, side = "", min(w, h)
    try:  # the picture without its black bars (a 4:3 video in a 16:9 still)
        out = subprocess.run([ff, "-i", "pipe:0", "-vf", "cropdetect=limit=24:round=2:skip=0", "-f", "null", "-"],
                             input=data, capture_output=True, timeout=30).stderr.decode("utf-8", "ignore")
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out)
        if m and int(m[-1][0]) > w * 0.3 and int(m[-1][1]) > h * 0.3:
            bars = "crop={}:{}:{}:{},".format(*m[-1])
            side = min(int(m[-1][0]), int(m[-1][1]))
    except (OSError, subprocess.SubprocessError):
        pass
    # Its own resolution (at most COVER_PX): a small still stays small, so the enhancing sees it needs the AI
    # upscaling (blown up to 4096 here by plain resizing it looked big enough, and stayed blurry).
    n = max(64, min(COVER_PX, side))
    graph = (f"[0]{bars}split[b][f];[b]scale=256:256:force_original_aspect_ratio=increase,crop=256:256,"
             f"gblur=sigma=8,eq=brightness=-0.12,scale={n}:{n}:flags=bicubic[bg];"
             f"[f]crop='min(iw,ih)':ih,scale={n}:{n}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    try:
        r = subprocess.run([ff, "-v", "error", "-i", "pipe:0", "-filter_complex", graph, "-frames:v", "1",
                            "-q:v", "2", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                           input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return data
    return r.stdout if r.returncode == 0 and r.stdout else data


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


def parse_news(page: str) -> list[dict]:
    """radiobeat.cz's news (/cs/clanky/1): [{title, date, text, img, url}], newest first."""
    out = []
    for chunk in page.split('class="article-list__item"')[1:]:
        title = re.search(r'class="article-list__title"[^>]*>(.*?)</h2>', chunk, re.S)
        date = re.search(r'class="article-list__meta">\s*([^<]+?)\s*<', chunk)
        img = re.search(r'data-src="([^"]+)"', chunk)
        desc = re.search(r'class="article-list__desc">(.*?)</div>', chunk, re.S)
        url = re.search(r'href="(/cs/1/[^"]+)"', chunk)
        clean = lambda h: html.unescape(re.sub(r"<[^>]+>", " ", h)).strip()
        if title:
            out.append({"title": re.sub(r"\s+", " ", clean(title.group(1))),
                        "date": date.group(1).strip() if date else "",
                        "text": re.sub(r"\s+", " ", clean(desc.group(1))) if desc else "",
                        "img": SITE + img.group(1).replace("/media/250/", "/media/500/") if img else "",
                        "url": SITE + url.group(1) if url else ""})
    return out


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

def _killed_with_parent() -> None:
    # SIGKILL, not SIGTERM: an enhancing step may be stopped (SIGSTOP, while something animates), and a stopped
    # process never acts on SIGTERM: it was left behind holding GPU memory
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)


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

    def __init__(self, on_prop, name: str = ""):
        self.on_prop = on_prop
        run = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        sock_dir = os.path.join(run, "radio-beat")
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
        self.sock_path = os.path.join(sock_dir, f"mpv-{os.getpid()}{'-' + name if name else ''}.sock")
        try:
            os.remove(self.sock_path)
        except FileNotFoundError:
            pass
        cmd = mpv_command(sock_dir)
        if not cmd:
            raise RuntimeError("Chybí mpv (balíček systému, nebo z Flathubu: flatpak install flathub io.mpv.Mpv)")
        self.proc = subprocess.Popen(
            # --load-scripts=no: no MPRIS plugin, the app shows itself as the player (Mpris)
            [*cmd, "--idle=yes", "--no-video", "--load-scripts=no", "--no-terminal", "--force-window=no",
             "--title=Rádio Beat", f"--audio-client-name={CLIENT_NAME}", f"--input-ipc-server={self.sock_path}",
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

    def mute(self, on: bool) -> None:
        self.cmd("set_property", "mute", on)

    def quit(self) -> None:
        self.cmd("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- sound card volume ---------------------------------------------------------------

CLIENT_NAME = "radio-beat"  # mpv's audio stream name (--audio-client-name), to find it among the streams


class CardVolume:
    """The volume of the sound card Rádio Beat plays on (its PipeWire/PulseAudio sink), both ways: set() changes
    it, and its own changes (a volume knob on the card, the desktop's volume) come to on_change(percent, card name)
    on the main loop. Through pactl: 'pactl subscribe' says when, 'pactl -f json list' reads it."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.ok = shutil.which("pactl") is not None
        self.sink = None  # our stream's sink (index), None: the default one
        self.running = False
        self.proc = None
        self.wake = threading.Event()  # something changed: read the volume again
        self.want = None  # percent waiting to be set
        self.want_ev = threading.Event()

    def start(self) -> None:
        if self.ok and not self.running:
            self.running = True
            for f in (self._listen, self._reader, self._setter):
                threading.Thread(target=f, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self.wake.set()
        self.want_ev.set()
        if self.proc:
            self.proc.kill()

    def set(self, v: int) -> None:
        self.want = v
        self.want_ev.set()

    def _listen(self) -> None:
        while self.running:
            try:
                self.proc = proc = subprocess.Popen(["pactl", "subscribe"], stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                self.ok = False
                return
            self.wake.set()
            for line in proc.stdout:
                if " on sink" in line or " on server" in line:  # sinks, streams (sink-input), default sink
                    self.wake.set()
            proc.wait()
            time.sleep(2)  # the sound server restarted: subscribe again

    def _pactl(self, *args) -> str:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout

    def _read(self):
        try:
            mine = [i for i in json.loads(self._pactl("-f", "json", "list", "sink-inputs"))
                    if i["properties"].get("application.name") == CLIENT_NAME]
            sinks = json.loads(self._pactl("-f", "json", "list", "sinks"))
            default = self._pactl("get-default-sink").strip()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return None
        sink = next((s for s in sinks if mine and s["index"] == mine[0]["sink"]), None) \
            or next((s for s in sinks if s["name"] == default), None)
        if not sink:
            return None
        self.sink = str(sink["index"]) if mine else None
        vols = [int(c["value_percent"].rstrip("%")) for c in sink["volume"].values()] or [0]
        return round(sum(vols) / len(vols)), sink.get("description", "")  # muted or not: the volume it's set to

    def _reader(self) -> None:
        last = None
        while self.running:
            self.wake.wait()
            self.wake.clear()
            got = self._read() if self.running else None
            if got and got != last:
                last = got
                GLib.idle_add(self.on_change, *got)
            time.sleep(0.05)  # a knob turn comes as a burst of changes: one read per 50 ms

    def _setter(self) -> None:
        while self.running:
            self.want_ev.wait()
            self.want_ev.clear()
            v, self.want = self.want, None
            if v is None or not self.running:
                continue
            target = self.sink or "@DEFAULT_SINK@"
            try:
                self._pactl("set-sink-volume", target, f"{v}%")
                self._pactl("set-sink-mute", target, "0")
            except (OSError, subprocess.SubprocessError):
                pass


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
                "Volume": GLib.Variant("d", a.volume_now() / 100),
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
# UPSCALE_FROM is enhanced in steps, each shown as soon as it's done (and
# morphed into the next one on screen):
# 1. a quick AI pass (realesr-animevideov3, small and fast), so an AI picture
#    is up within seconds;
# 2. the full AI pass: Real-ESRGAN x4plus for photos, x4plus-anime for the
#    logo (flat colours, letters: sharp edges instead of smears), at 4x and
#    then scaled down to the screen (supersampled), through Vulkan on any GPU
#    or iGPU (AMD, NVIDIA, Intel Arc); without one, on the CPU through software
#    Vulkan (lavapipe) when the system has it: slow, but still AI;
# 3. sharpened: unsharp masks and CAS on the upscaled picture;
# 4. denoised: NL-means (on the GPU when ffmpeg can, else the CPU), then
#    sharpened again: the final picture (denoising before sharpening).
# Never a plain resize while AI can still happen: pictures wait for the GPU
# test at start, a failed AI pass is tried again with smaller tiles, and a
# picture that only got a plain resize isn't cached (tried again next time).
# Only a system without any Vulkan at all gets Lanczos.
# A picture big enough for the screen already isn't upscaled: sharpened at
# once, then redrawn by Real-ESRGAN for detail (from half size, back to its own
# size), then denoised and sharpened. Pictures of REDRAW_FROM px and more go
# through Real-ESRGAN from half size either way: sharper, and 4x less work.
# All local. Real-ESRGAN and its model (47 MB download) go into
# ~/.local/share/radio-beat by themselves, ffmpeg too (42 MB) when the system
# has none; downloads are checked against their checksums. Everything keeps
# itself working: files checked at every start (downloaded again if missing or
# damaged, retried while offline), the GPU tested with a tiny picture (smaller
# tiles if it times out, remembered), damaged cache files cleared.

ESR_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
ESR_SHA256 = "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96"
ESR_MODEL = "realesrgan-x4plus"  # photos
ESR_MODEL_ART = "realesrgan-x4plus-anime"  # the logo: flat colours, letters
ESR_QUICK = "realesr-animevideov3"  # the quick first pass (its -x4 file)
FFMPEG_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "radio-beat")
UPSCALED_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "radio-beat", "upscaled")
UPSCALE_FROM = 1.05
def _size(n: float) -> str:
    """Bytes in the biggest unit they make at least one of: KB, MB or GB."""
    if n >= 2**30:
        return f"{n / 2**30:.2f} GB".replace(".", ",")
    if n >= 2**20:
        return f"{n / 2**20:.1f} MB".replace(".", ",")
    return f"{n / 2**10:.0f} KB"


def obrazku(n: int) -> str:
    """'1 obrázek', '3 obrázky', '5 obrázků'."""
    return f"{n} " + ("obrázek" if n == 1 else "obrázky" if 2 <= n <= 4 else "obrázků")


# The cache (this computer's own disk) may take 80 % of the space it could have (the free space plus what it
# already takes), never leaving under 2 GB free; the oldest pictures go first.
UPSCALED_SHARE = 0.8
UPSCALED_MIN_FREE = 2 << 30
# Fine and wide unsharp masks, then contrast adaptive sharpening: as sharp as
# it goes without halos or blown-up JPEG blocks (CAS alone at full strength does that).
SHARPEN = "unsharp=3:3:1.2:3:3:0,unsharp=9:9:0.5:9:9:0,cas=strength=0.6"
REDRAW_FROM = 1000  # px: bigger pictures go through Real-ESRGAN from half size
DENOISE = "nlmeans=s=1.0:p=7:r=15"
DENOISE_VK = "format=rgba,hwupload,nlmeans_vulkan=s=1.0:p=7:r=15,hwdownload,format=rgba"
VK_STRIP_PX = 2_000_000  # some GPUs give a bigger picture back black (Renoir: from about 3 MP): done in strips
VK_OVERLAP = 32  # px each strip reaches into its neighbours (more than the denoiser's 22 px reach): no seams


def denoise_vk(w: int, h: int) -> str:
    """DENOISE_VK for a w x h picture: whole when small, else in overlapping horizontal strips, put back."""
    n = 1
    while w * (math.ceil(h / n) + 2 * VK_OVERLAP * (n > 1)) > VK_STRIP_PX and n < h // 64:
        n += 1
    if n == 1:
        return DENOISE_VK
    base = math.ceil(h / n)
    labels = "".join(f"[s{i}]" for i in range(n))
    parts = [f"split={n}{labels}"]
    for i in range(n):
        top, bottom = i * base, min(h, (i + 1) * base)
        y0, y1 = max(0, top - VK_OVERLAP), min(h, bottom + VK_OVERLAP)
        parts.append(f"[s{i}]crop={w}:{y1 - y0}:0:{y0},{DENOISE_VK},crop={w}:{bottom - top}:0:{top - y0}[d{i}]")
    parts.append("".join(f"[d{i}]" for i in range(n)) + f"vstack=inputs={n}")
    return ";".join(parts)
SOFTWARE_GPU = re.compile(r"llvmpipe|lavapipe|swiftshader", re.I)
DISCRETE_GPU = re.compile(r"nvidia|geforce|rtx|gtx|quadro|radeon (rx|pro)|\bDG2\b|\bBMG\b|arc\(tm\) [ab]\d", re.I)


def _cpu_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    # "AMD Ryzen 7 8700G w/ Radeon 780M Graphics" -> "AMD Ryzen 7 8700G"
                    return re.sub(r"\s+(w/|with) .*$|\s+\d+-Core Processor$|\(R\)|\(TM\)", "", name).strip()
    except OSError:
        pass
    return "(unknown)"


def _simd() -> str:
    """The widest vector instructions this CPU has: ffmpeg picks its own code by them."""
    try:
        with open("/proc/cpuinfo") as f:
            flags = next((line.split(":", 1)[1].split() for line in f if line.startswith("flags")), [])
    except OSError:
        return ""
    return next((name for flag, name in (("avx512f", "AVX-512"), ("avx2", "AVX2"), ("avx", "AVX"),
                                         ("sse4_2", "SSE4.2"), ("sse2", "SSE2")) if flag in flags), "")


SIMD = _simd()
# NL-means on the CPU: more threads aren't faster (it waits on the memory: 16 threads took 9 s like 1, at 7x
# the CPU time), they only take the cores from the rest
DENOISE_THREADS = 2


class GpuWatch:
    """A step watched: how busy the GPU was and at what clock (amdgpu's sysfs), the watts the chip drew (the
    amdgpu sensor: the whole chip on an APU), and how many CPU threads the step's programs kept busy on
    average (their CPU time over the step's time)."""

    def __init__(self, enh=None):
        import resource
        self.enh = enh
        self.paused0 = dict(enh.paused) if enh else {}
        self.busy: list[int] = []
        self.mhz: list[int] = []
        self.watts: list[float] = []
        self.top = 0
        self.ram = self.vram = self.gtt = 0  # the step's programs at their peak (summed when several run)
        self.dev = next((os.path.dirname(p) for p in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"))),
                        None)
        self.power = next((p for d in sorted(glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"))
                           for p in (os.path.join(d, "power1_average"), os.path.join(d, "power1_input"))
                           if os.path.exists(p)), None)
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.cpu0, self.t0 = ru.ru_utime + ru.ru_stime, time.monotonic()
        self.run = True  # (memory is watched even without GPU sensors)
        if self.run:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        end = time.monotonic() + 3600
        while self.run and time.monotonic() < end:
            self._memory()
            if self.power:
                try:
                    with open(self.power) as f:
                        self.watts.append(int(f.read()) / 1e6)
                except (OSError, ValueError):
                    pass
            if not self.dev:
                time.sleep(0.2)
                continue
            try:
                with open(os.path.join(self.dev, "gpu_busy_percent")) as f:
                    busy = int(f.read())
                with open(os.path.join(self.dev, "pp_dpm_sclk")) as f:
                    levels = [(int(m.group(1)), "*" in line) for line in f
                              if (m := re.search(r"(\d+)\s*mhz", line, re.I))]
            except (OSError, ValueError):  # "busy" now and then: the next look
                time.sleep(0.2)
                continue
            self.busy.append(busy)
            if levels:
                self.top = max(self.top, *(c for c, _ in levels))
                cur = next((c for c, on in levels if on), None)
                if cur and busy:
                    self.mhz.append(cur)
            time.sleep(0.2)

    def _memory(self) -> None:
        """RAM and GPU memory of the step's programs now; the peaks kept."""
        if not self.enh:
            return
        with self.enh.plock:
            pids = [p.pid for p in self.enh.procs if p.poll() is None]
        ram = vram = gtt = 0
        for pid in pids:
            r, v, g, _ = proc_memory(pid)
            ram, vram, gtt = ram + r, vram + v, gtt + g
        self.ram, self.vram, self.gtt = max(self.ram, ram), max(self.vram, vram), max(self.gtt, gtt)

    def stop(self, gpu: bool = True) -> str:
        """ "GPU vytížená 84 % při 1980 z 2000 MHz, 23 W, 3,2 z 16 vláken CPU" (averages over the step; the GPU
        part only for a GPU step, and only what it could tell)."""
        import resource
        self.run = False
        parts = []
        if gpu and len(self.busy) >= 2:
            text = f"vytížení GPU {round(sum(self.busy) / len(self.busy))} %"
            if self.mhz and self.top:
                text += f" při {round(sum(self.mhz) / len(self.mhz))} z {self.top} MHz"
            parts.append(text)
        if self.watts:
            parts.append(f"{sum(self.watts) / len(self.watts):.0f} W")
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        took = time.monotonic() - self.t0
        if took > 0.2:
            used = (ru.ru_utime + ru.ru_stime - self.cpu0) / took
            parts.append(f"{used:.1f}".replace(".", ",") + f" z {os.cpu_count() or 1} vláken CPU")
            # what it waited for meanwhile: stopped (and why), the GPU (a GPU step's CPU just feeds it), or
            # reading and writing the picture (one thread, whatever the CPU has)
            waits = []
            if gpu and len(self.busy) >= 2:
                waits.append("GPU (CPU ji jen zásobuje daty)")
            elif used < 1.5:
                waits.append("načtení a uložení obrázku (to jde jen jedním vláknem)")
            if self.enh:
                for why, t in self.enh.paused.items():
                    if t - self.paused0.get(why, 0.0) >= 0.1:
                        waits.append(f"{why} ({_secs(t - self.paused0.get(why, 0.0))})")
            if waits:
                parts.append("CPU čekal na: " + ", ".join(waits))
        if self.ram:
            mem = f"paměť až RAM {mem_text(self.ram)}"
            if self.vram or self.gtt:
                mem += f" + GPU VRAM {mem_text(self.vram)} a GTT {mem_text(self.gtt)}"
            parts.append(mem)
        return ", ".join(parts)


# The steps' notes kept with the cached pictures were English before (and come so from older versions): shown
# in Czech.
CZ_STEPS = (("quick AI redraw", "rychlé AI překreslení"), ("AI redraw for detail", "AI překreslení pro detail"),
            ("AI redrawn and upscaled", "AI překreslení a zvětšení"), ("AI redrawn", "AI překreslení"),
            ("AI upscaled", "AI zvětšení"), ("upscaled (smooth", "zvětšeno (hladce"), ("sharpened", "zaostřeno"),
            ("denoised", "odšuměno a zaostřeno"), ("final ", "výsledek "), (" on CPU", " na CPU"),
            (" on GPU", " na GPU"), (" on NPU", " na NPU"), (" busy at ", " při "), (" of ", " z "),
            (" threads", " vláken"), ("enhanced", "vylepšeno"), ("quick AI pass", "rychlý AI průchod"),
            ("AI-upscaled", "AI zvětšeno"), ("AI-redrawn and upscaled", "AI překresleno a zvětšeno"),
            ("AI-redrawn", "AI překresleno"), ("on the CPU", "na CPU"), ("on the GPU", "na GPU"), (" on ", " na "),
            (" in ", " za "), (" busy", " vytížení"), ("upscaled", "zvětšeno"))


def cz_steps(text: str) -> str:
    for en, cz in CZ_STEPS:
        text = text.replace(en, cz)
    text = re.sub(r"(\d)\.(\d+) (s|MB|GB)\b", r"\1,\2 \3", text)  # Czech decimal commas
    # the first Czech wording of what a step waited for, as it reads now
    text = re.sub(r"GPU vytížená (\d+) %", r"vytížení GPU \1 %", text)
    # (a number glued back together first: the rule below once matched the last digit of "GPU 15 %" alone,
    # "GPU 1vytížení GPU 5 %", again on every showing)
    while re.search(r"\d+vytížení GPU \d", text):
        text = re.sub(r"(\d+)vytížení GPU (\d)", r"\1\2", text)
    text = re.sub(r"(?<!GPU )(?<![\d,])(\d+) % (?:% )?při ", r"vytížení GPU \1 % při ", text)  # ("45 % % při")
    text = text.replace(" for detail", " pro detail").replace("zostřen", "zaostřen").replace("zostřov", "zaostřov")
    text = re.sub(r"(?<!CPU )čekal na: ", "CPU čekal na: ", text)
    text = text.replace("na GPU (procesor ji jen zásobuje daty)", "GPU (CPU ji jen zásobuje daty)")
    text = text.replace("GPU (procesor ji jen zásobuje daty)", "GPU (CPU ji jen zásobuje daty)")
    text = text.replace("načítání a ukládání obrázku, to jde jen jedním vláknem",
                        "načtení a uložení obrázku (to jde jen jedním vláknem)")
    for old, new in (("rezervu pro plynulý obraz", "rezervu pro plynulý obraz"), ("animace", "dokončení animace"),
                     ("otevřený panel", "zavření panelu")):
        text = re.sub(rf"(?<!dokončení ){old} (\d+(?:,\d+)? s)", rf"{new} (\1)", text)
    if re.search(r"\d s|za \d", text) and "vláken CPU" not in text and "starší záznam" not in text:
        text += " · starší záznam: vlákna CPU, příkon a na co se čekalo se tehdy neměřily"
    return text


def recaption_cache() -> None:
    """Once: the notes kept with the cached pictures (and the last picture's, in the settings) in today's words."""
    mark = os.path.join(UPSCALED_DIR, ".notes-cz3")
    if os.path.exists(mark) or not os.path.isdir(UPSCALED_DIR):
        return
    for name in os.listdir(UPSCALED_DIR):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(UPSCALED_DIR, name)
        try:
            with open(path) as f:
                old = f.read()
            new = cz_steps(old)
            if new != old:
                with open(path + ".part", "w") as f:
                    f.write(new)
                os.replace(path + ".part", path)
        except OSError:
            continue
    try:
        open(mark, "w").close()
    except OSError:
        pass


def _secs(t: float) -> str:
    return f"{t:.1f} s".replace(".", ",") if t < 10 else f"{t:.0f} s"


def _download(url: str, timeout: int = 120) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (OSError, ValueError) as e:
        print(f"enhancer: download of {url} failed: {e}", file=sys.stderr)
        return None


class Enhancer:
    MODEL_FILES = {  # as unpacked
        f"{ESR_MODEL}.param": 116029, f"{ESR_MODEL}.bin": 33424520,
        f"{ESR_MODEL_ART}.param": 30290, f"{ESR_MODEL_ART}.bin": 8943500,
        f"{ESR_QUICK}-x4.param": 3077, f"{ESR_QUICK}-x4.bin": 1247368,
    }
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
        self.soft = False  # AI on the CPU through software Vulkan (no GPU)
        self.vk_denoise = None  # None: not tested yet (the start-up test, after the downloads)
        self.vk_fail_px = 1 << 62  # pictures this big (pixels) came out black from the GPU denoise
        self.tile = self._load_tile()
        self.status = "Spouští se…"
        self.on_status, self.on_picture = on_status, on_picture
        self.jobs: dict[str, tuple] = {}  # kind ("logo" / "cover") -> job
        self.running: tuple | None = None
        self.proc: subprocess.Popen | None = None
        self.wake = threading.Event()
        # Animations come first: the AI and ffmpeg get at most SHARE of the time (stopped for the rest of each
        # tenth of a second), and none at all while something animates (hold).
        self.procs: set = set()
        self.held = False
        self.held_at = 0.0
        self.quiet = False  # a panel is open: the small pictures' quick steps only
        self.light = False  # the job now is a small picture's quick one
        self.light_done: set[str] = set()  # small pictures shown quick, waiting for the full enhancing
        self.cache_used = self.cache_allowed = -1  # bytes (counted at start, then kept up to date)
        self.on_gone = None  # called with each picture the cache lets go (CacheShare: not fetched back)
        self.fetch = None  # asks the other computers for a picture before making it (CacheShare): who gave it
        self.verify = None  # (name, source bytes, path) -> a picture from the internet hub looks like the original
        self.watches: list[GpuWatch] = []  # the GPU watched during the job's steps
        self.sizes: dict[str, list] = {}  # kind -> [original bytes, enhanced bytes or None while working]
        threading.Thread(target=self._trim, daemon=True).start()
        # NPU (testing, off unless switched on): AMD XDNA through ONNX Runtime's Vitis AI provider.
        self.npu_wanted = False
        self.npu_ready = False  # driver, runtime and a model all found
        self.npu_text = "Vypnuto"
        self.npu_model = None
        self.plock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._duty, daemon=True).start()

    NPU_DIR = os.path.join(DATA_DIR, "npu")  # an ONNX super-resolution model built for the NPU goes here

    def npu_probe(self) -> None:
        """What the NPU path has and lacks (for the gear menu): the amdxdna driver, ONNX Runtime with the Vitis
        AI provider (AMD's XRT and Ryzen AI Software), and a model for it."""
        if not self.npu_wanted:
            self.npu_ready, self.npu_text = False, "Vypnuto: AI běží na GPU"
            return
        drivers = {os.path.basename(os.path.realpath(d))
                   for d in glob.glob("/sys/class/accel/accel*/device/driver")}
        have_npu = "amdxdna" in drivers
        try:
            r = subprocess.run(["python3", "-c", "import onnxruntime as o; print(','.join(o.get_available_providers()))"],
                               capture_output=True, text=True, timeout=30)
            providers = r.stdout.strip().split(",") if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            providers = []
        has_ort, has_vai = bool(providers), "VitisAIExecutionProvider" in providers
        models = sorted(glob.glob(os.path.join(self.NPU_DIR, "*.onnx")))
        self.npu_model = models[0] if models else None
        parts = ["NPU: " + ("nalezena (ovladač amdxdna)" if have_npu else "nenalezena (žádné zařízení amdxdna)"),
                 "běhové prostředí: " + ("ONNX Runtime s Vitis AI" if has_vai else
                                "ONNX Runtime bez Vitis AI (AMD XRT + Ryzen AI Software)" if has_ort
                                else "chybí (ONNX Runtime s Vitis AI: AMD XRT + Ryzen AI Software)"),
                 "model: " + (os.path.basename(self.npu_model) if self.npu_model else
                              f"žádný (ONNX model pro zvětšování sestavený pro NPU do {self.NPU_DIR})")]
        self.npu_ready = have_npu and has_vai and bool(self.npu_model)
        self.npu_text = " · ".join(parts) + (" — AI zvětšování zkusí nejdřív NPU" if self.npu_ready
                                              else " — AI zůstává na GPU")
        print(f"enhancer: {self.npu_text}", file=sys.stderr)

    def _npu(self, src: str, out: str, work: str) -> bool:
        """The AI step on the NPU (npu_upscale.py): True when it ran there and gave a picture."""
        from gi.repository import GdkPixbuf
        try:
            _, w, h = GdkPixbuf.Pixbuf.get_file_info(src)
        except (GLib.Error, TypeError):
            return False
        raw_in, raw_out = os.path.join(work, "npu-in.rgb"), os.path.join(work, "npu-out.rgb")
        if not self._ff(["-i", src], "format=rgb24", raw_in.replace(".rgb", ".png")):
            return False
        r = subprocess.run([self.ffmpeg, "-v", "error", "-y", "-i", raw_in.replace(".rgb", ".png"), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", raw_in], capture_output=True)
        if r.returncode:
            return False
        proc = self.proc = self._spawn(["nice", "-n", "19", "python3", os.path.join(HERE, "npu_upscale.py"),
                                        self.npu_model, raw_in, str(w), str(h), raw_out])
        _, err = proc.communicate()
        self._reap(proc)
        if self.proc is proc:
            self.proc = None
        if proc.returncode != 0:
            print(f"enhancer: NPU: {err.strip()[-200:] or proc.returncode} (the GPU does it)", file=sys.stderr)
            return False
        n = round(math.sqrt(os.path.getsize(raw_out) / (w * h * 3))) if os.path.exists(raw_out) else 0
        if n < 1:
            return False
        r = subprocess.run([self.ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                            f"{w * n}x{h * n}", "-i", raw_out, out], capture_output=True)
        return r.returncode == 0 and self._not_black(out)

    SHARE = 0.92  # of the CPU / GPU time the enhancing may take; the rest stays free for the animations

    def _stopped(self) -> bool:
        """Waiting: something animates, or a panel is open and the job is a heavy one (the cover, the logo)."""
        light = bool(self.running and self.running[0].startswith("pic:"))
        return self.held or (self.quiet and not light)

    def hold(self, on: bool) -> None:
        """Stop (or let go on) the enhancing while something animates."""
        with self.plock:
            if on and not self.held:
                self.held_at = time.monotonic()
            self.held = on
            self._signal(signal.SIGSTOP if self._stopped() else signal.SIGCONT)

    def set_quiet(self, on: bool) -> None:
        """A panel ('i', gear) open: only the small pictures' quick steps run, the full ones wait for it to
        close (fluid scrolling and animations)."""
        with self.plock:
            self.quiet = on
            self._signal(signal.SIGSTOP if self._stopped() else signal.SIGCONT)
        self.wake.set()

    # How long the steps' programs were stopped, and why (shown with each step: a step that waited isn't one
    # that left the CPU or GPU idle for nothing).
    paused: dict = {}
    _stopped_at: tuple | None = None  # (when, why)

    def _signal(self, sig) -> None:
        now = time.monotonic()
        if sig == signal.SIGSTOP and self.procs and self._stopped_at is None:
            why = "dokončení animace" if self.held else "zavření panelu" if self.quiet else "rezervu pro plynulý obraz"
            self._stopped_at = (now, why)
        elif sig in (signal.SIGCONT, signal.SIGKILL) and self._stopped_at is not None:
            at, why = self._stopped_at
            self.paused[why] = self.paused.get(why, 0.0) + now - at
            self._stopped_at = None
        for p in list(self.procs):
            try:
                os.kill(p.pid, sig)
            except OSError:
                pass

    def _duty(self) -> None:
        while True:
            time.sleep(0.1 * self.SHARE)
            with self.plock:
                if self.held and time.monotonic() - self.held_at > 30:
                    self.held = False  # a lost "mouse left": never wait for good
                    if not self._stopped():
                        self._signal(signal.SIGCONT)
                if not self.procs or self._stopped():
                    continue
                self._signal(signal.SIGSTOP)
            time.sleep(0.1 * (1 - self.SHARE))
            with self.plock:
                if not self._stopped():
                    self._signal(signal.SIGCONT)

    def _spawn(self, cmd: list) -> subprocess.Popen:
        # ends with the app (it may be stopped by the duty cycle: it would wait for good)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                preexec_fn=_killed_with_parent)
        with self.plock:
            self.procs.add(proc)
            if self._stopped():
                os.kill(proc.pid, signal.SIGSTOP)
        return proc

    def stop_all(self) -> None:
        """Quitting: whatever runs ends (let go first if it was stopped)."""
        with self.plock:
            self._signal(signal.SIGCONT)
            self._signal(signal.SIGKILL)

    def _reap(self, proc) -> None:
        with self.plock:
            self.procs.discard(proc)

    # --- status ------------------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.status = text
        print(f"enhancer: {text}", file=sys.stderr)
        if self.on_status:
            GLib.idle_add(lambda: (self.on_status(text), False)[1])

    def _say_ready(self) -> None:
        """What runs where: the GPU (Vulkan) and the CPU, by name, and for what."""
        cpu = f"{_cpu_name()} ({SIMD})" if SIMD else _cpu_name()
        gpu = f"GPU {self.gpu} (Vulkan)"
        if self.ai and self.soft:
            parts = [f"Bez GPU pro Vulkan, takže AI zvětšování (Real-ESRGAN) na {self.gpu}: pomalé, ale AI"]
        elif self.ai:
            parts = [f"AI zvětšování a AI překreslování pro detail (Real-ESRGAN: x4plus pro obaly, "
                     f"x4plus-anime pro logo): {gpu}"]
        else:
            parts = [f"Vůbec žádný Vulkan (ani softwarový), takže hladké zvětšování (Lanczos): CPU {cpu}"]
        if self.ffmpeg:
            parts.append(f"zaostřování: CPU {cpu}")
            parts.append(f"odšumování: {gpu}" if self.vk_denoise else
                         "odšumování: zkouší se GPU…" if self.vk_denoise is None else f"odšumování: CPU {cpu}")
        else:
            parts.append("zaostřování a odšumování čekají na ffmpeg (stahuje se)")
        self._say("Zapnuto. " + "; ".join(parts) + ".")

    def notes(self) -> str:
        """For the gear menu's notes: every picture step, its tool / AI model, and where it runs."""
        cpu = f"CPU {_cpu_name()}" + (f" ({SIMD}: nejširší instrukce, které na něm ffmpeg použije)" if SIMD else "")
        on = (self.gpu if self.soft else f"GPU {self.gpu} (Vulkan)") if self.ai else None
        lines = ["Jak se vylepšují obrázky:"]
        if self.ai is None:
            lines.append("• AI: čeká se na test GPU (do té doby se bez AI nic nezvětšuje)")
        elif self.ai:
            lines += [f"• Rychlý AI průchod: Real-ESRGAN {ESR_QUICK} (ncnn) na {on}",
                      f"• Obaly a obrázky pořadů: Real-ESRGAN {ESR_MODEL} (RealESRGAN_x4plus, 4× a pak "
                      f"zmenšeno na obrazovku, Lanczos) na {on}",
                      f"• Logo a ikona aplikace: Real-ESRGAN {ESR_MODEL_ART} (RealESRGAN_x4plus_anime_6B, "
                      f"pro ploché barvy a písmena) na {on}",
                      f"• Velké obrázky: AI překreslení pro detail z poloviční velikosti, stejné modely, na {on}"]
        else:
            lines.append(f"• Zvětšování: žádný Vulkan, takže Lanczos (ffmpeg, ne AI) na {cpu}")
        if self.ffmpeg:
            lines.append(f"• Zaostřování: ffmpeg unsharp (jemné + široké) a CAS na {cpu}")
            if self.vk_denoise:
                big = (f"; obrázky od {self.vk_fail_px / 1e6:.1f} MP na {cpu} (tahle GPU je vrací "
                       f"černé)" if self.vk_fail_px < 1 << 62 else "")
                lines.append(f"• Odšumování: ffmpeg nlmeans_vulkan na GPU {self.gpu}{big}")
            elif self.vk_denoise is None:
                lines.append("• Odšumování: zkouší se GPU (ffmpeg nlmeans_vulkan); mezitím CPU")
            else:
                lines.append(f"• Odšumování: ffmpeg NL-means na {cpu}, {DENOISE_THREADS} vláken (víc nezrychlí: "
                             f"čeká na paměť; Vulkan odšumovač ffmpegu tu neběží)")
            lines.append(f"• Velikosti ikony aplikace: Lanczos zmenšený z AI předlohy (ffmpeg) na {cpu}")
        else:
            lines.append("• Zaostřování a odšumování: čeká se na ffmpeg")
        lines.append("• Plynulý přechod mezi kroky: GTK (GSK) na GPU")
        lines.append("• U každého kroku: jak dlouho trval, kde běžel, vytížení a takt GPU, příkon čipu (čidlo amdgpu: na APU "
                     "celý čip), kolik vláken CPU v průměru vytížil, na co čekal a kolik paměti jeho programy nejvíc zabraly "
                     "(RAM a na GPU VRAM + GTT, ze /proc)")
        lines.append(f"• NPU (testování): {self.npu_text}")
        return "\n".join(lines)

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
                    and all(os.path.getsize(os.path.join(self.models, f)) == n for f, n in self.MODEL_FILES.items()))
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
                self.wake.set()  # pictures waiting for the test
                if then:
                    GLib.idle_add(lambda: (then(), False)[1])
            if not need:
                break
            self._say(f"Stahuje se {' a '.join(need)}…")
            got = (self._esr_ok() or self._install_esr()) and (self._ffmpeg_ok() or self._install_ffmpeg())
            if got:
                continue
            wait = self.RETRY[min(tries, len(self.RETRY) - 1)]
            tries += 1
            self._say(f"Stahování selhalo; znovu za {wait // 60} min (do té doby obrázky, jak přijdou)")
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
        want = {"realesrgan-ncnn-vulkan"} | {f"models/{f}" for f in self.MODEL_FILES}
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
        """Which GPUs Vulkan offers, and a 64 px picture through the best of
        them; smaller tiles until it comes back right. A software one (the CPU)
        only when there's no GPU at all."""
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
                real = {i: n for i, n in gpus.items() if not SOFTWARE_GPU.search(n)} or gpus
                if not real:
                    self.ai = False
                    return
                if self.gpu_index not in real:
                    self.gpu_index = max(real, key=lambda i: (bool(DISCRETE_GPU.search(real[i])), -i))
                    self.soft = bool(SOFTWARE_GPU.search(real[self.gpu_index]))
                    self.gpu = (f"CPU {_cpu_name()} (softwarový Vulkan)" if self.soft
                                else real[self.gpu_index])
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
        if not self.ffmpeg or not self.ai or self.soft:
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

    def request(self, kind: str, key: str, data: bytes, src_size: tuple[int, int], size: tuple[int, int], done,
                screen: tuple[int, int] | None = None) -> None:
        """Main thread. done(picture bytes) runs on the main thread after each
        step. A newer request of the same kind replaces (and stops) the older one."""
        # Enhanced before (the AI's final picture is kept): shown at once, even before the GPU test is done, and
        # not made again.
        hit = self._cache_path(key, size, ai=True)
        if kind != "icon" and os.path.exists(hit) and self._jpeg_ok(hit) and self._not_black(hit) \
                and (not self.verify or self.verify(os.path.basename(hit), data, hit)):
            self._used(hit)
            with open(hit, "rb") as f:
                pic = f.read()
            self.sizes[kind] = [len(data), None, len(pic), None, None]  # (no time: from the cache)
            done(pic)
            try:
                with open(hit[:-4] + ".txt") as f:
                    steps = f.read().strip()
            except OSError:
                steps = "vylepšeno"
            what = {"cover": "Obal", "track": "Obal", "logo": "Logo"}.get(kind, "Obrázek pořadu")
            if self.on_picture:
                self.on_picture(f"{what}: {src_size[0]}×{src_size[1]} px (původně {_size(len(data))}) → "
                                f"{size[0]}×{size[1]} px, {cz_steps(steps)} (z mezipaměti)", kind)
            self.drop(kind)
            return
        self.jobs[kind] = (key, data, src_size, size, done, screen)
        r = self.running
        if r and r[0] == kind and r[1] != key and self.proc:
            self.proc.kill()
        self.wake.set()

    def drop(self, kind: str) -> None:
        self.jobs.pop(kind, None)
        r = self.running
        if r and r[0] == kind and self.proc:
            self.proc.kill()

    def _cache_path(self, key: str, size: tuple[int, int], ai: bool | None = None) -> str:
        import hashlib
        ai = self.ai if ai is None else ai
        h = hashlib.sha1(f"{key}|{size[0]}x{size[1]}|{'ai' if ai else 'plain'}|v5".encode()).hexdigest()
        return os.path.join(UPSCALED_DIR, f"{h}.jpg")

    def _worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            # Nothing before the GPU test is done: no plain resizes while AI may still come.
            while self.jobs and self.ai is not None:
                if self.quiet:  # a panel open: only the small pictures' quick steps, the rest after it closes
                    kind = next((k for k in self.jobs if k.startswith("pic:")), None)
                    if kind is None:
                        break
                else:
                    kind = next((k for k in ("logo", "cover", "show") if k in self.jobs), next(iter(self.jobs)))
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
        key, data, (w0, h0), size, done, screen = job
        # a small picture while a panel is open: the quick AI pass and sharpening only, not cached (the full
        # enhancing follows in the background once the panels are closed)
        self.light = kind.startswith("pic:") and self.quiet
        what = {"cover": "Obal", "track": "Obal", "logo": "Logo"}.get(kind, "Obrázek pořadu")
        self.model = ESR_MODEL_ART if kind in ("logo", "icon") else ESR_MODEL
        same = size == (w0, h0)  # big enough already: no upscaling
        orig = _size(len(data))
        # source, fast enhanced (the first step shown), final; when it began, how long it took in all
        self.sizes[kind] = [len(data), None, None, time.monotonic(), None]
        if same:
            scr = f"obrazovku {screen[0]}×{screen[1]} px" if screen else "obrazovku"
            head = f"{what}: původně {w0}×{h0} px ({orig}), zvětšovat netřeba (dost velký na {scr})"
        else:
            head = f"{what}: původně {w0}×{h0} px ({orig}) → {size[0]}×{size[1]} px"
        # The icon master only wants the final picture; everything else shows each step.
        sizes = self.sizes
        steps: list[str] = []

        def emit(pic, final=False):
            # each step: what, how long, where, and the size it left: "sharpened 0.4 s on CPU (3.9 MB)"
            if steps and not steps[-1].endswith(")"):
                steps[-1] += f" ({_size(len(pic))})"
            got = sizes.setdefault(kind, [len(data), None, None, None, None])
            if final:
                got[2] = len(pic)
                if got[3] is not None:
                    got[4] = time.monotonic() - got[3]
            elif got[1] is None:
                got[1] = len(pic)
            if kind != "icon" or final:
                GLib.idle_add(lambda: (done(pic), False)[1])
        cached = self._cache_path(key, size)
        if self.light:
            self.light_done.add(kind)
        else:
            self.light_done.discard(kind)
        fetched = None
        if not self.light and self.fetch and not os.path.exists(cached):
            fetched = self.fetch(os.path.basename(cached))  # another computer may have made it already
        if os.path.exists(cached) and self.verify and not self.verify(os.path.basename(cached), data, cached):
            pass  # from the internet hub and not like the original: deleted, made here
        if os.path.exists(cached):
            if self._jpeg_ok(cached) and self._not_black(cached):  # black ones came from the GPU giving up
                self._used(cached)
                with open(cached, "rb") as f:
                    pic = f.read()
                self.sizes[kind] = [len(data), None, len(pic), None, None]  # (no time: from the cache)
                emit(pic, True)
                try:
                    with open(cached[:-4] + ".txt") as f:
                        was = f.read().strip()
                except OSError:
                    was = "vylepšeno"
                was = cz_steps(was)
                if "výsledek " not in was:
                    was += f", výsledek {_size(len(pic))}"
                self._picture(f"{head}, {was} ({f'staženo z {fetched}' if fetched else 'z mezipaměti'})")
                return
            os.remove(cached)
        if self.light:
            cached = None  # the quick version isn't kept: the full one comes later
        work = tempfile.mkdtemp(prefix="radio-beat-esr-")
        try:
            src = os.path.join(work, "in.img")
            with open(src, "wb") as f:
                f.write(data)
            show = lambda more: self._picture(head + "".join(f", {x}" for x in steps) + more)
            live = lambda: self._current(kind, job)
            if same:
                self._same_size(src, (w0, h0), work, steps, show, live, emit, cached)
            else:
                self._bigger(src, (w0, h0), size, work, steps, show, live, emit, cached)
        finally:
            for watch in self.watches:
                watch.run = False
            self.watches.clear()
            shutil.rmtree(work, ignore_errors=True)

    def _bigger(self, src, src_size, size, work, steps, show, live, emit, cached) -> None:
        """1. a quick AI pass, shown; 2. the full AI pass, shown; 3. sharpened,
        shown; 4. denoised and sharpened: the final one."""
        up = os.path.join(work, "up.png")
        label = None
        t = time.monotonic()
        if self.ai:
            show(f": rychlý AI průchod na {self.gpu}…")
            t, watch = time.monotonic(), self._watch()
            quick = os.path.join(work, "quick.png")
            out = self._ai(src, src_size, size, quick, work, ESR_QUICK)
            if out == "killed" or not live():
                return
            if out:
                steps.append(f"rychlé AI překreslení {_secs(time.monotonic() - t)} na {self._where(watch)}")
                emit(open(quick, "rb").read())
            if self.light and out:  # a small picture: the quick pass is enough
                shutil.copyfile(quick, up)
                if self._sharpen(up, work, steps, show, live, emit):
                    sharp = os.path.join(work, "sharp.png")
                    self._keep(sharp, steps, emit, cached)
                return
            show(f", AI zvětšování ({self.model}) na {self.gpu}…")
            t = time.monotonic()
            watch = self._watch()
            full = self._ai(src, src_size, size, up, work, self.model)
            if full == "killed" or not live():
                return
            if full:
                label = f"AI {full} ({self.model}) {_secs(time.monotonic() - t)} na {self._where(watch)}"
            elif out:
                shutil.copyfile(quick, up)
                label = "(plný AI průchod selhal, zůstal ten rychlý)"
                cached = None  # the full pass again next time
        if label is None:
            watch = self._watch()
            if not self._resize(src, size, up) or not live():
                if live():
                    show(": nešlo zvětšit, ukázáno, jak je")
                return
            label = "zvětšeno (hladce: bez Vulkanu pro AI)" if not self.ai else "zvětšeno (hladce: AI selhala)"
            if self.ai:
                cached = None  # AI again next time
        steps.append(f"{label} {_secs(time.monotonic() - t)} na CPU, {watch.stop(False)}" if label.startswith("zvětš")
                     else label)
        emit(open(up, "rb").read())
        if not self.ffmpeg:
            show(" (zaostření a odšumění: zatím chybí ffmpeg)")
            return
        if self._sharpen(up, work, steps, show, live, emit):
            self._final(up, steps, show, live, emit, cached, work)

    def _same_size(self, src, size, work, steps, show, live, emit, cached) -> None:
        """1. sharpened as it is, shown at once; 2. AI-redrawn for detail
        (when there's a GPU); 3. denoised and sharpened: the final one."""
        if not self.ffmpeg:
            show(" (zaostření a odšumění: zatím chybí ffmpeg)")
            return
        orig = os.path.join(work, "orig.png")
        if not self._ff(["-i", src], "format=rgb24", orig):
            return
        if not self._sharpen(orig, work, steps, show, live, emit):
            return
        if self.light:  # a small picture big enough already: sharpened is enough
            self._keep(os.path.join(work, "sharp.png"), steps, emit, cached)
            return
        base = orig
        if self.ai:
            show(f", AI překreslování pro detail na {self.gpu}…")
            t, watch = time.monotonic(), self._watch()
            redrawn = os.path.join(work, "redrawn.png")
            out = self._ai(orig, size, size, redrawn, work, self.model)
            if out == "killed" or not live():
                return
            if out:
                steps.append(f"AI překreslení pro detail ({self.model}) {_secs(time.monotonic() - t)} "
                             f"na {self._where(watch)}")
                base = redrawn
            else:
                cached = None  # the redraw again next time
        self._final(base, steps, show, live, emit, cached, work)

    def _ai(self, src, src_size, size, out, work, model) -> str | None:
        """Real-ESRGAN (`model`) at 4x, then down to `size`: from half size when
        the picture is big (sharper from a soft source, and 4x less work).
        Returns what it did ("upscaled" / "redrawn"), "killed", or None when it
        failed even with the smallest tiles."""
        w0, h0 = src_size
        # Real-ESRGAN reads plain 8-bit RGB only (a CMYK JPEG or a 16-bit PNG cover: "decode image failed"):
        # every picture goes in as one.
        feed, how = os.path.join(work, "feed.png"), "zvětšení"
        if not self._ff(["-i", src], "format=rgb24", feed):
            feed = src
        if min(w0, h0) >= REDRAW_FROM:
            half = os.path.join(work, "half.png")
            if self._ff(["-i", src], f"scale={w0 // 2}:{h0 // 2}:flags=lanczos,format=rgb24", half):
                feed, how = half, "překreslení" if size == src_size else "překreslení a zvětšení"
        big = os.path.join(work, "x4.png")
        if self.npu_ready and self._npu(feed, big, work):
            return how if self._resize(big, size, out) else None
        res, err = self._esr(feed, big, model)
        while res is None and self.tile != self.TILES[-1]:
            # The GPU timed out (black or failed): smaller tiles from now on, and again.
            self.tile = self.TILES[self.TILES.index(self.tile) + 1]
            self.tile_ok = 0
            self._save_tile()
            res, err = self._esr(feed, big, model)
        if res and res != "killed" and self.tile != self.TILES[0]:
            # Small tiles leave the GPU idle between them (it clocks down: a quarter of it busy): after a few
            # pictures done right, the next bigger tile is tried again (a failure steps back down, as above).
            self.tile_ok = getattr(self, "tile_ok", 0) + 1
            if self.tile_ok >= 3:
                self.tile, self.tile_ok = self.TILES[self.TILES.index(self.tile) - 1], 0
                self._save_tile()
        if res == "killed":
            return "killed"
        if res is None or not self._resize(big, size, out):
            print(f"enhancer: Real-ESRGAN failed: {err.strip()[-200:]}", file=sys.stderr)
            return None
        return how

    def _sharpen(self, pic, work, steps, show, live, emit) -> bool:
        show(", zaostřování…")
        t, watch = time.monotonic(), self._watch()
        sharp = os.path.join(work, "sharp.png")
        if not self._ff(["-i", pic], SHARPEN, sharp, threads=os.cpu_count() or 1) or not live():
            if live():
                show(" (zaostření selhalo)")
            return False
        steps.append(f"zaostřeno {_secs(time.monotonic() - t)} na CPU{', ' + SIMD if SIMD else ''}, {watch.stop(False)}")
        emit(open(sharp, "rb").read())
        return True

    def _where(self, watch: GpuWatch | None = None) -> str:
        """Where the AI runs, for the steps' notes: on the GPU with how busy it was and its clock."""
        where = "NPU" if self.npu_ready else "CPU (bez GPU pro Vulkan)" if self.soft else "GPU"
        load = watch.stop(where == "GPU") if watch else ""
        return f"{where}, {load}" if load else where

    def _watch(self) -> GpuWatch:
        watch = GpuWatch(self)
        self.watches.append(watch)
        return watch

    def _keep(self, path: str, steps, emit, cached) -> None:
        """A small picture's last step: shown and cached as it is (a JPEG, like the full ones)."""
        jpg = path[:-4] + ".jpg"
        if not self._ff(["-i", path], "format=rgb24", jpg, jpeg=True):
            return
        with open(jpg, "rb") as f:
            pic = f.read()
        emit(pic, True)
        steps.append(f"výsledek {_size(len(pic))}")
        if cached:
            os.makedirs(UPSCALED_DIR, exist_ok=True)
            with open(cached + ".part", "wb") as f:
                f.write(pic)
            os.replace(cached + ".part", cached)
            self.cache_used += len(pic)
            with open(cached[:-4] + ".txt", "w") as f:
                f.write(", ".join(steps))

    def _final(self, base, steps, show, live, emit, cached, work) -> None:
        """Denoised (GPU when ffmpeg can, else CPU), then sharpened: shown and cached."""
        show(", odšumování…")
        t, watch = time.monotonic(), self._watch()
        final = os.path.join(work, "final.jpg")
        from gi.repository import GdkPixbuf
        _, w, h = GdkPixbuf.Pixbuf.get_file_info(base)
        ok = self.vk_denoise and w * h < self.vk_fail_px \
            and self._ff(["-i", base], f"{denoise_vk(w, h)},{SHARPEN}", final, vulkan=True, jpeg=True)
        if ok and not self._not_black(final):
            # The GPU gives up on big pictures without an error (all black: Renoir from about 3 MP, hence the
            # strips); if it still does: the CPU does this one, and ones as big from now on.
            ok, self.vk_fail_px = False, min(self.vk_fail_px, w * h)
        load = watch.stop()
        where = f"GPU, {load}" if load else "GPU"  # the NPU never denoises: not _where()
        if not ok and live():
            t, watch = time.monotonic(), self._watch()  # the CPU's own time
            ok = self._ff(["-i", base], f"{DENOISE},{SHARPEN}", final, jpeg=True, threads=DENOISE_THREADS)
            where = f"CPU, {SIMD + ', ' if SIMD else ''}{watch.stop(False)}"
        if not ok or not live():
            if live():
                show(" (odšumění selhalo)")
            return
        steps.append(f"odšuměno a zaostřeno {_secs(time.monotonic() - t)} na {where}")
        with open(final, "rb") as f:
            pic = f.read()
        emit(pic, True)
        steps.append(f"výsledek {_size(len(pic))}")
        show("" if cached else " (neuloženo: příště znovu)")
        if not cached:
            return
        os.makedirs(UPSCALED_DIR, exist_ok=True)
        with open(cached + ".part", "wb") as f:
            f.write(pic)
        os.replace(cached + ".part", cached)
        self.cache_used += len(pic)
        with open(cached[:-4] + ".txt", "w") as f:
            f.write(", ".join(steps))
        self._trim()

    # --- tools --------------------------------------------------------------------------

    def _esr(self, src: str, out: str, model: str = ESR_MODEL) -> tuple[str | None, str]:
        """Real-ESRGAN 4x: (out, stderr); out None on failure, "killed" when stopped."""
        cmd = ["nice", "-n", "19", self.bin, "-i", src, "-o", out, "-n", model, "-s", "4", "-m", self.models,
               "-t", str(self.tile), "-f", "png"]
        if self.gpu_index is not None:
            cmd += ["-g", str(self.gpu_index)]
        # Its own handle: the start-up tests run alongside the worker; self.proc is for stopping it.
        proc = self.proc = self._spawn(cmd)
        _, err = proc.communicate()
        self._reap(proc)
        rc = proc.returncode
        if self.proc is proc:
            self.proc = None
        if rc < 0:
            return "killed", err
        if rc or "failed" in err or not os.path.exists(out) or not self._not_black(out):
            return None, err
        return out, err

    def _ff(self, inputs: list[str], vf: str, out: str, vulkan: bool = False, jpeg: bool = False,
            threads: int = 0) -> bool:
        if not self.ffmpeg:
            return False
        cmd = ["nice", "-n", "19", self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if threads:
            cmd += ["-filter_threads", str(threads)]
        if vulkan:
            dev = f"vulkan=vk:{self.gpu_index}" if self.gpu_index is not None else "vulkan=vk"
            cmd += ["-init_hw_device", dev, "-filter_hw_device", "vk"]
        # PNGs between the steps (in /tmp) lightly compressed: the same pixels in half the time (the default
        # level's single-threaded compressing was most of a step's time; the sharpening itself is ~0.1 s)
        png = ["-compression_level", "1"] if out.endswith(".png") else []
        cmd += inputs + ["-vf", vf, "-frames:v", "1"] + (["-q:v", "2"] if jpeg else []) + png + [out]
        # Its own handle: the start-up tests run alongside the worker; self.proc is for stopping it.
        proc = self.proc = self._spawn(cmd)
        _, err = proc.communicate()
        self._reap(proc)
        rc = proc.returncode
        if self.proc is proc:
            self.proc = None
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
        """Any colour in it (the GPU timing out gives an all-black picture). Colour
        bytes only: an alpha channel's 255s made a black picture pass before."""
        from gi.repository import GdkPixbuf
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
        except GLib.Error:
            return False
        px, n, stride = pb.get_pixels(), pb.get_n_channels(), pb.get_rowstride()
        for y in range(0, pb.get_height(), max(1, pb.get_height() // 64)):
            row = y * stride
            for x in range(0, pb.get_width(), max(1, pb.get_width() // 64)):
                i = row + x * n
                if px[i] > 8 or px[i + 1] > 8 or px[i + 2] > 8:
                    return True
        return False

    USES = os.path.join(UPSCALED_DIR, "uses.json")  # picture -> times shown from the cache

    def _uses(self) -> dict:
        try:
            with open(self.USES) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _used(self, path: str) -> None:
        """A cached picture shown again: counted, and its time set to now."""
        os.utime(path)
        uses = self._uses()
        name = os.path.basename(path)
        uses[name] = uses.get(name, 0) + 1
        try:
            with open(self.USES + ".tmp", "w") as f:
                json.dump(uses, f)
            os.replace(self.USES + ".tmp", self.USES)
        except OSError:
            pass

    def size_text(self, kind: str) -> str:
        src, fast, final, began, took = ((self.sizes.get(kind) or []) + [None] * 5)[:5]
        if not src:
            return ""
        parts = [f"původní {_size(src)}"]
        if fast:  # no first pass (from the cache, or a picture big enough already): left out
            parts.append(f"rychle vylepšený {_size(fast)}")
        parts.append(f"výsledný {_size(final) if final is not None else '…'}")
        text = "Velikost: " + " / ".join(parts)
        if took is not None:  # how long it took in all, from the picture's arrival to the final one (1:20)
            text += f" / {int(took) // 60}:{int(took) % 60:02d}"
        elif final is not None and began is None:
            text += " (z mezipaměti)"
        return text

    shared_counts: dict = {}  # the other computers' names -> (pictures there, pictures both have together)

    def cache_text(self) -> str:
        if self.cache_used < 0:
            return "Mezipaměť: počítá se…"
        try:
            here = sum(1 for f in os.listdir(UPSCALED_DIR) if f.endswith(".jpg"))
        except OSError:
            here = 0
        pics = f"{obrazku(here)} na tomto počítači"
        for who, (there, total) in self.shared_counts.items():
            pics += f", {obrazku(there)} na počítači {who.rsplit('-', 1)[0]}, dohromady {obrazku(total)}"
        return (f"Mezipaměť: {pics}; zabírají {_size(self.cache_used)} z vyhrazených "
                f"{self.cache_allowed / 2**30:.1f} GB (80 % volného místa na disku)".replace(".", ","))

    def _trim(self) -> None:
        """When the cache is full: the least used pictures go first (shown the fewest times), and of those the
        one not shown for the longest."""
        uses = self._uses()
        try:
            files = sorted((os.path.join(UPSCALED_DIR, f) for f in os.listdir(UPSCALED_DIR) if f.endswith(".jpg")),
                           key=lambda f: (uses.get(os.path.basename(f), 0), os.path.getmtime(f)))
        except OSError:
            return
        try:
            free = shutil.disk_usage(UPSCALED_DIR).free
        except OSError:
            return
        sizes = {f: os.path.getsize(f) for f in files if os.path.exists(f)}
        used = sum(sizes.values())
        allowed = min(UPSCALED_SHARE * (free + used), free + used - UPSCALED_MIN_FREE)
        gone = False
        self.cache_used, self.cache_allowed = used, int(max(0, allowed))
        for f in files:  # least used first
            if used <= allowed:
                break
            used -= sizes.get(f, 0)
            self.cache_used = used
            uses.pop(os.path.basename(f), None)
            gone = True
            if self.on_gone:
                self.on_gone(os.path.basename(f))
            for p in (f, f[:-4] + ".txt"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        if gone:
            try:
                with open(self.USES + ".tmp", "w") as f:
                    json.dump(uses, f)
                os.replace(self.USES + ".tmp", self.USES)
            except OSError:
                pass


# --- the picture cache shared on the local network ------------------------------------------------
# Every computer running Rádio Beat on the same network keeps its own full copy of the enhanced pictures:
# each one serves its cache (read, and new pictures given to it) on SHARE_PORT, finds the others by
# trying that port on its own /24 network every SHARE_EVERY s, then fetches what it lacks and hands over
# what they lack. A picture is the same file (the same name) on every computer, so nothing's there twice.
# Only computers on the local network (private addresses) are answered; only cache files are accepted
# (40-hex names, a whole JPEG or a short text), and none the cache let go before (they'd come right back).
# Either way round is enough: a computer whose firewall turns the others away still fetches and hands over.

SHARE_PORT = 48765
SHARE_SCREEN = (3840, 2160)  # pictures enhanced for at least this (App.enhance_px): the same files everywhere
SHARE_EVERY = 600
SHARE_NAME = re.compile(r"^[0-9a-f]{40}\.(jpg|txt)$")
SHARE_MAX = 40 << 20  # bytes a picture may have
# The shared database over the internet (radio_beat_hub.py, run on one computer behind a tunnel): every Rádio Beat
# swaps pictures, plays and track info with it, wherever it is. On the computer running it: straight to it.
HUB_URL = "https://ryzen7030pro.tail326f7d.ts.net"  # public address (Tailscale Funnel); settings "hub_url" overrides
HUB_LOCAL = "http://127.0.0.1:48766"
HUB_LIKE = 40  # a picture from the hub is used only if it looks like the original (mean difference of 16x16, 0-255)


class CacheShare:
    def __init__(self, enhancer, on_status, history=None):
        self.enh = enhancer
        self.history = history  # the plays (History): shared too
        self.plays_in = self.plays_out = 0
        self.info_in = 0  # songs whose picture or track info came from another computer
        self.marks_path = os.path.join(DATA_DIR, "share-plays.json")  # per computer: plays fetched / sent up to
        try:
            with open(self.marks_path) as f:
                self.marks = json.load(f)
        except (OSError, ValueError):
            self.marks = {}
        self.on_status = on_status
        self.on = False
        self.peers: dict[str, str] = {}  # address -> its name
        self.text = "Vypnuto"
        self.port_note = ""
        self.got = self.gave = 0  # pictures fetched and handed over since the start
        self.server = None
        self.wake = threading.Event()
        self.gone_path = os.path.join(UPSCALED_DIR, "gone.json")
        try:
            with open(self.gone_path) as f:
                self.gone = set(json.load(f))
        except (OSError, ValueError):
            self.gone = set()
        enhancer.on_gone = self._let_go
        enhancer.fetch = self.fetch
        enhancer.verify = self.verify
        self.hub_url = ""  # (App sets it from the settings)
        self.hub_text = ""
        self.unverified_path = os.path.join(DATA_DIR, "hub-unverified.json")  # pictures from the hub not yet checked
        try:
            with open(self.unverified_path) as f:
                self.unverified = set(json.load(f))
        except (OSError, ValueError):
            self.unverified = set()
        try:
            with open(os.path.join(DATA_DIR, "share-id")) as f:
                self.id = f.read().strip()
        except OSError:
            self.id = ""
        if not self.id:
            self.id = f"{socket.gethostname()}-{random.getrandbits(32):08x}"
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(os.path.join(DATA_DIR, "share-id"), "w") as f:
                    f.write(self.id)
            except OSError:
                pass
        threading.Thread(target=self._loop, daemon=True).start()

    def set_on(self, on: bool, asked: bool = False) -> None:
        if asked:  # turned on by hand: the firewall question may be asked again
            try:
                os.remove(self.REFUSED)
            except OSError:
                pass
        self.on = on
        if on and self.server is None:
            self._serve()
        elif not on and self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self._say("Hledají se další počítače v této síti…" if on else "Vypnuto")
        if on:
            self.wake.set()

    def _note(self, text: str) -> None:
        """What the firewall question came to: shown after the sharing's own status."""
        self.port_note = text
        GLib.idle_add(lambda: (self.on_status(), False)[1])

    def _say(self, text: str) -> None:
        self.text = text
        GLib.idle_add(lambda: (self.on_status(), False)[1])

    def _let_go(self, name: str) -> None:
        self.gone.add(name)
        if len(self.gone) > 20000:
            self.gone = set(list(self.gone)[-10000:])
        try:
            with open(self.gone_path + ".tmp", "w") as f:
                json.dump(sorted(self.gone), f)
            os.replace(self.gone_path + ".tmp", self.gone_path)
        except OSError:
            pass

    @staticmethod
    def _names() -> set[str]:
        try:
            return {f for f in os.listdir(UPSCALED_DIR) if SHARE_NAME.match(f)}
        except OSError:
            return set()

    def _room(self, n: int) -> bool:
        e = self.enh
        return e.cache_allowed < 0 or e.cache_used + n <= e.cache_allowed

    def _store(self, name: str, data: bytes) -> bool:
        """A picture (or its notes) from another computer, kept when it's whole and new here."""
        path = os.path.join(UPSCALED_DIR, name)
        if not SHARE_NAME.match(name) or name in self.gone or os.path.exists(path):
            return False
        if name.endswith(".txt"):
            if len(data) > 4096 or not os.path.exists(path[:-4] + ".jpg"):  # only with its picture
                return False
            try:
                data.decode()
            except UnicodeDecodeError:
                return False
        elif not (data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9") or not self._room(len(data)):
            return False
        os.makedirs(UPSCALED_DIR, exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        if name.endswith(".jpg"):
            self.enh.cache_used += len(data)
        return True

    # serving
    def _serve(self) -> None:
        import http.server
        import ipaddress
        share = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _local(self) -> bool:
                try:
                    ip = ipaddress.ip_address(self.client_address[0])
                except ValueError:
                    return False
                if ip.is_private or ip.is_link_local or ip.is_loopback:
                    return True
                self.send_error(403)
                return False

            def _reply(self, body: bytes, kind: str = "application/octet-stream") -> None:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._local():
                    return
                if self.path.startswith("/radio-beat/v1/plays?since=") and share.history:
                    try:
                        since = float(self.path.split("=", 1)[1])
                    except ValueError:
                        return self.send_error(400)
                    rows = share.history.export(since)
                    return self._reply(json.dumps({"plays": rows, "more": len(rows) == 2000}).encode(),
                                       "application/json")
                if self.path.startswith("/radio-beat/v1/songinfo?since=") and share.history:
                    try:
                        since = float(self.path.split("=", 1)[1])
                    except ValueError:
                        return self.send_error(400)
                    rows = share.history.info_export(since)
                    return self._reply(json.dumps({"songs": rows, "more": len(rows) == 2000}).encode(),
                                       "application/json")
                if self.path == "/radio-beat/v1/list":
                    return self._reply(json.dumps({"id": share.id, "files": sorted(share._names())}).encode(),
                                       "application/json")
                name = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/radio-beat/v1/file/") and SHARE_NAME.match(name):
                    try:
                        with open(os.path.join(UPSCALED_DIR, name), "rb") as f:
                            return self._reply(f.read())
                    except OSError:
                        pass
                self.send_error(404)

            def do_PUT(self):
                if not self._local():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                if self.path == "/radio-beat/v1/plays" and share.history:
                    if not 0 < n <= 4 << 20:
                        return self.send_error(400)
                    try:
                        rows = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self.send_error(400)
                    got = share.history.merge(rows if isinstance(rows, list) else [])
                    share.plays_in += got
                    return self._reply(json.dumps({"new": got}).encode(), "application/json")
                if self.path == "/radio-beat/v1/songinfo" and share.history:
                    if not 0 < n <= 4 << 20:
                        return self.send_error(400)
                    try:
                        rows = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self.send_error(400)
                    got = share.history.info_merge(rows if isinstance(rows, list) else [])
                    share.info_in += got
                    return self._reply(json.dumps({"new": got}).encode(), "application/json")
                name = self.path.rsplit("/", 1)[-1]
                if not self.path.startswith("/radio-beat/v1/file/") or not SHARE_NAME.match(name) \
                        or not 0 < n <= SHARE_MAX:
                    return self.send_error(400)
                ok = share._store(name, self.rfile.read(n))
                if ok and name.endswith(".jpg"):
                    share.got += 1
                self.send_response(201 if ok else 409)
                self.send_header("Content-Length", "0")
                self.end_headers()

        try:
            self.server = http.server.ThreadingHTTPServer(("0.0.0.0", SHARE_PORT), Handler)
        except OSError as e:
            self.server = None
            self._say(f"Obrázky nejde nabízet (port {SHARE_PORT}: {e.strerror}); od ostatních se dál stahuje")
            return
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._open_port, daemon=True).start()

    # the firewall: the port opened for the local network (once, with the administrator's password)
    PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    REFUSED = os.path.join(DATA_DIR, "share-port-refused")

    @staticmethod
    def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            return None

    def _blocked(self) -> tuple[str, str] | None:
        """(firewall, the root command opening the port) when a firewall turns the others away, else None."""
        if shutil.which("ufw") and (r := self._run(["systemctl", "is-active", "ufw"])) and r.stdout.strip() == "active":
            try:
                with open("/etc/ufw/user.rules") as f:
                    if f"--dport {SHARE_PORT} " in f.read():
                        return None
            except OSError:
                pass
            return "ufw", " && ".join(f"ufw allow proto tcp from {net} to any port {SHARE_PORT} comment radio-beat"
                                      for net in self.PRIVATE)
        if shutil.which("firewall-cmd") and (r := self._run(["firewall-cmd", "--state"])) \
                and r.stdout.strip() == "running":
            zone = (self._run(["firewall-cmd", "--get-default-zone"]) or subprocess.CompletedProcess([], 0, "")).stdout.strip()
            if not zone:
                return None
            ports = (self._run(["firewall-cmd", f"--zone={zone}", "--list-ports"])
                     or subprocess.CompletedProcess([], 0, "")).stdout.split()
            for p in ports:  # "48765/tcp", "1025-65535/tcp" (Fedora's desktop zone)
                rng, _, proto = p.partition("/")
                lo, _, hi = rng.partition("-")
                if proto == "tcp" and lo.isdigit() and int(lo) <= SHARE_PORT <= int(hi or lo):
                    return None
            return "firewalld", (f"firewall-cmd --permanent --zone={zone} --add-port={SHARE_PORT}/tcp && "
                                 f"firewall-cmd --reload")
        return None

    def _open_port(self) -> None:
        got = self._blocked()
        if not got:
            self._note("")
            return
        wall, cmd = got
        how = f"jednou spusť v terminálu: sudo sh -c '{cmd}'"
        if os.path.exists(self.REFUSED):
            self._note(f"Firewall ({wall}) ostatní odmítá; tento počítač dál stahuje a předává. "
                      f"Aby je pustil, vypni a zapni sdílení (zeptá se znovu), nebo {how}")
            return
        self._note(f"Žádá se heslo správce pro otevření portu {SHARE_PORT} ({wall}, jen tato síť)…")
        r = self._run(["pkexec", "sh", "-c", cmd]) if shutil.which("pkexec") else None
        if r is not None and r.returncode == 0:
            self._note(f"Port {SHARE_PORT} ve firewallu otevřen ({wall}, jen místní síť).")
            self.wake.set()
            return
        if r is not None and r.returncode in (126, 127) and "agent" not in (r.stderr or "").lower():
            try:  # turned down: not asked at every start
                with open(self.REFUSED, "w") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M"))
            except OSError:
                pass
        self._note(f"Port {SHARE_PORT} ve firewallu otevřít nešlo ({wall}); tento počítač dál stahuje a předává. "
                  f"Aby šli dovnitř i ostatní: {how}")

    # finding the others, and swapping pictures with them
    def _loop(self) -> None:
        time.sleep(30)  # the cache counted first (its room)
        while True:
            if self.on:
                try:
                    self._round()
                except Exception as e:  # never the app's end
                    self._say(f"Sdílení selhalo: {e}")
            self.wake.wait(SHARE_EVERY)
            self.wake.clear()

    @staticmethod
    def _own_ip() -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet goes out: just the address it'd use
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()

    def _ask(self, ip: str, path: str, data: bytes | None = None, timeout: float = 20):
        req = urllib.request.Request(f"http://{ip}:{SHARE_PORT}{path}", data=data,
                                     method="PUT" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _find(self) -> dict[str, str]:
        import concurrent.futures
        me = self._own_ip()
        if not me or not __import__("ipaddress").ip_address(me).is_private:
            return {}
        nets = dict.fromkeys([me.rsplit(".", 1)[0]] + self._upstream())

        def knock(ip):
            try:
                with socket.create_connection((ip, SHARE_PORT), timeout=0.4):
                    return ip
            except OSError:
                return None

        cands = [f"{net}.{i}" for net in nets for i in range(1, 255) if f"{net}.{i}" != me] + list(self.peers)
        with concurrent.futures.ThreadPoolExecutor(64) as pool:
            open_ = {ip for ip in pool.map(knock, dict.fromkeys(cands)) if ip}
        found = {}
        for ip in open_:
            try:
                got = json.loads(self._ask(ip, "/radio-beat/v1/list", timeout=5))
            except (OSError, ValueError):
                continue
            if got.get("id") and got["id"] != self.id:
                found[ip] = got["id"]
        return found

    def _plays(self, ip: str, who: str) -> str:
        """The played songs both ways: theirs since the last fetch, ours since the last send (an overlap is
        harmless: the same play twice is kept once). Returns a note for the status, "" when not shared."""
        if not self.history:
            return ""
        mark = self.marks.setdefault(who, {"in": 0.0, "out": 0.0})
        got = sent = 0
        try:
            while True:  # theirs
                reply = json.loads(self._ask(ip, f"/radio-beat/v1/plays?since={mark['in']}", timeout=30))
                rows = reply.get("plays") or []
                got += self.history.merge(rows)
                if rows:
                    mark["in"] = max(mark["in"], max(r[0] for r in rows if isinstance(r, list) and r))
                if not reply.get("more") or not rows:
                    break
            while rows := self.history.export(mark["out"]):  # ours
                self._ask(ip, "/radio-beat/v1/plays", json.dumps(rows).encode(), timeout=60)
                sent += len(rows)
                mark["out"] = rows[-1][0]
        except (OSError, ValueError, TypeError, KeyError):
            pass  # an older Rádio Beat there (no plays yet), or gone: next round
        # the pictures and track info each looked up (each song once, on either computer: the work shared)
        info_got = info_sent = 0
        mark.setdefault("info_in", 0.0)
        mark.setdefault("info_out", 0.0)
        try:
            while True:
                reply = json.loads(self._ask(ip, f"/radio-beat/v1/songinfo?since={mark['info_in']}", timeout=30))
                rows = reply.get("songs") or []
                info_got += self.history.info_merge(rows)
                if rows:
                    mark["info_in"] = max(mark["info_in"], max(r[5] for r in rows if isinstance(r, list) and len(r) in (6, 8)))
                if not reply.get("more") or not rows:
                    break
            while rows := self.history.info_export(mark["info_out"]):
                self._ask(ip, "/radio-beat/v1/songinfo", json.dumps(rows).encode(), timeout=60)
                info_sent += len(rows)
                mark["info_out"] = rows[-1][5]
        except (OSError, ValueError, TypeError, KeyError):
            pass  # an older Rádio Beat there, or gone: next round
        self.info_in += info_got
        try:
            with open(self.marks_path + ".part", "w") as f:
                json.dump(self.marks, f)
            os.replace(self.marks_path + ".part", self.marks_path)
        except OSError:
            pass
        self.plays_in += got
        self.plays_out += sent
        return (f"; hrané skladby: {got} nových odtamtud, {sent} odesláno ({self.history.count()} celkem tady); "
                f"informace o skladbách: {info_got} doplněno odtamtud, {info_sent} odesláno")

    def fetch(self, name: str) -> str | None:
        """A picture about to be made: fetched from a computer that has it already (with its notes).
        Returns that computer's name, None when none has it."""
        if not self.on:
            return None
        self.gone.discard(name)  # trimmed here for room once: wanted again now, the other computer still has it
        for ip, who in list(self.peers.items()):
            try:
                if not self._store(name, self._ask(ip, f"/radio-beat/v1/file/{name}", timeout=15)):
                    continue
            except OSError:  # not there (404), or gone off the network
                continue
            try:
                self._store(name[:-4] + ".txt", self._ask(ip, f"/radio-beat/v1/file/{name[:-4]}.txt", timeout=5))
            except OSError:
                pass
            self.got += 1
            return who.rsplit("-", 1)[0]
        return None

    @staticmethod
    def _upstream() -> list[str]:
        """The networks of the routers on the way out (a router behind another one: the computer behind the
        first router can reach the one on the outer network, not the other way round; the one that can does
        the fetching and the handing over). Their first three numbers, private addresses only, the first three
        hops (the house's routers: further out lies the internet provider's network, not to be searched)."""
        try:
            out = subprocess.run(["tracepath", "-n", "-m", "3", "1.1.1.1"], capture_output=True, text=True,
                                 timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        ipaddress = __import__("ipaddress")
        nets = []
        for hop in re.findall(r"^\s*\d+:\s+(\d+\.\d+\.\d+\.\d+)", out, re.M):
            if ipaddress.ip_address(hop).is_private and hop.rsplit(".", 1)[0] not in nets:
                nets.append(hop.rsplit(".", 1)[0])
        return nets

    def _round(self) -> None:
        try:
            self._hub_round()
        except Exception as e:  # never the LAN's end
            self.hub_text = f"Internet: selhalo ({e})"
            GLib.idle_add(lambda: (self.on_status(), False)[1])
        self.peers = self._find()
        if not self.peers:
            self._say(f"V této síti zatím žádný další počítač s Rádiem Beat (hledá každých {SHARE_EVERY // 60} min)")
            return
        for ip, who in self.peers.items():
            theirs = set(json.loads(self._ask(ip, "/radio-beat/v1/list")).get("files", []))
            mine = self._names()
            got = gave = 0
            # a picture's notes travel with it
            for name in sorted(theirs - mine - self.gone, key=lambda n: n.endswith(".txt")):
                if not self.on:
                    return
                if name.endswith(".txt") and name[:-4] + ".jpg" not in self._names():
                    continue
                if name.endswith(".jpg") and not self._room(0):
                    break  # full here
                try:
                    if self._store(name, self._ask(ip, f"/radio-beat/v1/file/{name}", timeout=60)) \
                            and name.endswith(".jpg"):
                        got += 1
                except OSError:
                    continue
            for name in sorted(mine - theirs, key=lambda n: n.endswith(".txt")):
                if not self.on:
                    return
                try:
                    with open(os.path.join(UPSCALED_DIR, name), "rb") as f:
                        self._ask(ip, f"/radio-beat/v1/file/{name}", f.read(), timeout=60)
                    gave += name.endswith(".jpg")
                except urllib.error.HTTPError:
                    continue  # it doesn't want that one (it let it go, or it's full)
                except OSError:
                    break  # it turns us away (its firewall): it fetches from us itself
            self.got += got
            self.gave += gave
            jpg = lambda names: {n for n in names if n.endswith(".jpg")}
            try:  # what each has now, after the exchange
                theirs = set(json.loads(self._ask(ip, "/radio-beat/v1/list")).get("files", []))
            except (OSError, ValueError):
                pass
            mine = self._names()
            self.enh.shared_counts[who] = (len(jpg(theirs)), len(jpg(theirs | mine)))
            plays = self._plays(ip, who)
            self._say(f"Sdíleno s {who.rsplit('-', 1)[0]} ({ip}) v {time.strftime('%H:%M')}: "
                      f"{got} obrázků staženo, {gave} předáno (od spuštění: {self.got} sem, {self.gave} tam)"
                      f"{plays}")
        if self.got:
            self.enh._trim()

    # --- the shared database over the internet (radio_beat_hub.py) ----------------------------------------------
    def _hub_base(self) -> str | None:
        """Where the hub is: on this computer straight to it, else its public address; None: none."""
        try:
            with socket.create_connection(("127.0.0.1", 48766), timeout=0.3):
                return HUB_LOCAL
        except OSError:
            pass
        return (self.hub_url or HUB_URL).rstrip("/") or None

    def _hub(self, base: str, path: str, data: bytes | None = None, timeout: float = 30):
        req = urllib.request.Request(base + path, data=data, method="PUT" if data is not None else "GET",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _save_unverified(self) -> None:
        try:
            with open(self.unverified_path + ".part", "w") as f:
                json.dump(sorted(self.unverified), f)
            os.replace(self.unverified_path + ".part", self.unverified_path)
        except OSError:
            pass

    def _hub_round(self) -> None:
        """Pictures, plays and track info both ways with the hub; each kind from where it got to last time (the
        hub numbers what it takes: nothing another computer sent late is missed)."""
        base = self._hub_base()
        if not base:
            self.hub_text = ""
            return
        m = self.marks.setdefault("hub", {})
        for k in ("files", "plays", "info", "files_out", "plays_out", "info_out"):
            m.setdefault(k, 0)
        got = gave = plays_in = plays_out = info_in = info_out = 0
        stats = json.loads(self._hub(base, "/radio-beat/v1/hello", timeout=10))
        # pictures from the hub (checked against the original before first use: anyone can send it pictures)
        while self.on:
            page = json.loads(self._hub(base, f"/radio-beat/v1/list?since={m['files']}"))
            mine = self._names()
            for name in sorted(page.get("files", []), key=lambda n: n.endswith(".txt")):
                if name in mine or name in self.gone or (name.endswith(".jpg") and not self._room(0)):
                    continue
                if name.endswith(".txt") and name[:-4] + ".jpg" not in self._names():
                    continue
                try:
                    if self._store(name, self._hub(base, f"/radio-beat/v1/file/{name}", timeout=60)) \
                            and name.endswith(".jpg"):
                        got += 1
                        self.unverified.add(name)
                except OSError:
                    continue
            m["files"] = page.get("next", m["files"])
            self._save_marks()
            if not page.get("more"):
                break
        self._save_unverified()
        # pictures made here, the hub doesn't have yet (newest last; from the hub or unchecked: not sent back)
        def mtime(n):
            try:
                return os.path.getmtime(os.path.join(UPSCALED_DIR, n))
            except OSError:
                return 0.0
        todo = sorted(((mtime(n), n) for n in self._names() if n not in self.unverified),
                      key=lambda x: (x[0], x[1].endswith(".txt")))
        for t, name in todo:
            if not self.on or t <= m["files_out"]:
                continue
            try:
                with open(os.path.join(UPSCALED_DIR, name), "rb") as f:
                    self._hub(base, f"/radio-beat/v1/file/{name}", f.read(), timeout=120)
                gave += name.endswith(".jpg")
            except urllib.error.HTTPError as e:
                if e.code == 507:  # the hub is full
                    break
            except OSError:
                break  # gone for now: from here next round
            m["files_out"] = t
        self._save_marks()
        if self.history:
            # plays: ours up (by when they were played), the hub's down (by its numbering)
            while self.on and (rows := self.history.export(m["plays_out"])):
                self._hub(base, "/radio-beat/v1/plays", json.dumps(rows).encode(), timeout=60)
                plays_out += len(rows)
                m["plays_out"] = rows[-1][0]
            while self.on:
                page = json.loads(self._hub(base, f"/radio-beat/v1/plays?since={m['plays']}", timeout=60))
                plays_in += self.history.merge(page.get("plays") or [])
                m["plays"] = page.get("next", m["plays"])
                if not page.get("more"):
                    break
            # track info the same way
            while self.on and (rows := self.history.info_export(m["info_out"])):
                self._hub(base, "/radio-beat/v1/songinfo", json.dumps(rows).encode(), timeout=60)
                info_out += len(rows)
                m["info_out"] = rows[-1][5]
            while self.on:
                page = json.loads(self._hub(base, f"/radio-beat/v1/songinfo?since={m['info']}", timeout=60))
                info_in += self.history.info_merge(page.get("songs") or [])
                m["info"] = page.get("next", m["info"])
                if not page.get("more"):
                    break
            self._save_marks()
        self.got += got
        self.info_in += info_in
        where = "na tomto počítači" if base == HUB_LOCAL else base.split("//", 1)[-1]
        self.hub_text = (f"Internet ({where}) v {time.strftime('%H:%M')}: obrázky {got} staženo, {gave} odesláno; "
                         f"hrané skladby {plays_in} nových, {plays_out} odesláno; údaje o skladbách {info_in} nových, "
                         f"{info_out} odesláno · na serveru {obrazku(stats.get('pictures', 0))} "
                         f"({_size(stats.get('bytes', 0))}), {stats.get('plays', 0)} hraných skladeb, "
                         f"{stats.get('songs', 0)} skladeb s údaji")
        GLib.idle_add(lambda: (self.on_status(), False)[1])
        if got:
            self.enh._trim()

    def _save_marks(self) -> None:
        try:
            with open(self.marks_path + ".part", "w") as f:
                json.dump(self.marks, f)
            os.replace(self.marks_path + ".part", self.marks_path)
        except OSError:
            pass

    def verify(self, name: str, source: bytes, path: str) -> bool:
        """A picture from the hub, before its first use: does it look like the original (a small, rough
        comparison)? If not, it's deleted and not taken again; the picture is enhanced here instead."""
        if name not in self.unverified:
            return True
        ok = False
        try:
            from gi.repository import GdkPixbuf

            def tiny(data: bytes):
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pb = loader.get_pixbuf().scale_simple(16, 16, GdkPixbuf.InterpType.BILINEAR)
                px, n, st = pb.get_pixels(), pb.get_n_channels(), pb.get_rowstride()
                return [px[y * st + x * n + c] for y in range(16) for x in range(16) for c in range(3)]
            with open(path, "rb") as f:
                a, b = tiny(source), tiny(f.read())
            ok = sum(abs(x - y) for x, y in zip(a, b)) / len(a) < HUB_LIKE
        except Exception:
            ok = False
        self.unverified.discard(name)
        self._save_unverified()
        if not ok:
            for p in (path, path[:-4] + ".txt"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self._let_go(name)
            print(f"Rádio Beat: obrázek ze serveru {name} neodpovídá originálu: zahozen", file=sys.stderr)
        return ok


# --- window ---------------------------------------------------------------------

CSS = b"""
.cover-bg { background: #111; }
.backdrop { transform: scale(1.15); }  /* blurred and darkened by CoverView itself; GlassCard draws it the same: BACKDROP_* */
.scrim-shade { background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.75) 55%, rgba(0,0,0,0) 100%); }
.scrim label { color: white; text-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65), 0 0 2px rgba(0,0,0,0.5); }  /* left and bottom: readable on pale covers */
.song { font-weight: 800; }
@keyframes flash { 0% { text-shadow: 0 0 14px rgba(255,255,255,1), 0 0 34px rgba(255,255,255,0.8); }
                   100% { text-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65), 0 0 2px rgba(0,0,0,0.5); } }
.scrim label.flash { animation: flash 900ms ease-out; }
.artist { font-weight: 600; }
.album { opacity: 0.8; }
.status { opacity: 0.65; }
.round { min-width: 56px; min-height: 56px; border-radius: 999px; padding: 0; }
.glass-disc > button.round { background: none; box-shadow: none; }  /* the glass is the button */
.glass-disc > button.round:hover { background: rgba(255,255,255,0.10); }
.glass-disc > button.round:active { background: rgba(0,0,0,0.15); }
.play-button { background: #E3051A; color: white; min-width: 68px; min-height: 68px; }
.play-button:hover { background: #F21C30; }
.stop-button { background: rgba(255,255,255,0.16); color: white; }
.stop-button:hover { background: rgba(255,255,255,0.28); }
.scrim scale trough highlight { background: #E3051A; }
/* Buttons, their icons and the volume slider: the letters' shadow (down and left), standing out in 3D */
scale.vol-slider trough { box-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
scale.vol-slider slider { box-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
.glass-disc image, .glass-head image, .vol-icon, button.quality-tab image
    { -gtk-icon-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
.overlay-header { background: none; box-shadow: none; }
.show-card { padding: 0; }
.show-pic { border-radius: 10px; }
.on-air { background: #E3051A; color: white; border-radius: 999px; padding: 1px 8px; font-weight: 800; }
.day-head { background: #E3051A; color: white; border-radius: 999px; padding: 1px 10px; font-weight: 800; }
.show-title { font-weight: 700; }
.show-sub { opacity: 0.8; }
.show-next { opacity: 0.7; }
.overlay-header button { background: rgba(0,0,0,0.45); color: white; }
.overlay-header .glass-disc > button, .overlay-header .glass-head > button { background: none; box-shadow: none; }
.overlay-header .glass-disc > button:hover, .overlay-header .glass-head > button:hover { background: rgba(255,255,255,0.10); }
.overlay-header .top-logo { border-radius: 8px; margin-left: 4px; }
.overlay-header button.close-button { color: white; }
.overlay-header button.info-button { color: white; font-weight: 900; font-size: 1.25em; font-family: serif;
                                     padding-left: 0; padding-right: 0; }  /* square, like the gear's */
/* the panels' and menus' text: a dark halo round the letters themselves (readable on a bright cover), the glass
   as clear as ever */
.puddle label { color: white; text-shadow: -1px 2px 1px rgba(0,0,0,0.9), 0 0 1px rgba(0,0,0,1), 0 0 3px rgba(0,0,0,1), 0 0 7px rgba(0,0,0,1), 0 0 10px rgba(0,0,0,0.9), 0 0 14px rgba(0,0,0,0.85), 0 0 22px rgba(0,0,0,0.75); }
.puddle button.puddle-tab label { text-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
.puddle switch, popover.glass-menu switch { box-shadow: 0 0 6px rgba(0,0,0,0.75); }
.puddle .puddle-now label { font-weight: 700; }
/* the panels' and menus' lists: no pale fill, no lines between the rows, no pale switch tracks (the glass alone,
   clear) */
.puddle .boxed-list, .puddle .boxed-list > row, popover.glass-menu .boxed-list, popover.glass-menu .boxed-list > row {
    background: none; box-shadow: none; border: none; }
.puddle .boxed-list separator, popover.glass-menu .boxed-list separator { background: none; min-height: 0; }
.puddle switch:not(:checked), popover.glass-menu switch:not(:checked) { background: none; }
.puddle button.puddle-tab { padding: 4px 14px; min-height: 0; border-radius: 999px; color: white;
                            background: none; box-shadow: none; font-weight: 700; }  /* the glass is the tab */
.puddle button.puddle-tab:hover { background: rgba(255,255,255,0.10); }
button.quality-tab { padding: 4px 14px; min-height: 0; border-radius: 999px; color: white; background: none;
                     box-shadow: none; font-weight: 700; }  /* like the 'i' panel's tabs: the glass is the tab */
button.quality-tab:hover { background: rgba(255,255,255,0.10); }
button.quality-tab label { color: white; text-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
.puddle button.puddle-tab.on-air { background: none; }
/* the tabs' cells light nothing of their own: only the tab, in its pill, like the round buttons */
.puddle flowboxchild, .puddle flowboxchild:hover, .puddle flowboxchild:focus, .puddle flowboxchild:selected
    { background: none; outline: none; box-shadow: none; padding: 0; }
.puddle .puddle-now { background: rgba(227,5,26,0.25); border-radius: 10px; padding: 6px; }
.puddle .puddle-past { opacity: 0.55; }
.glass-disc > button.disc-btn { background: none; box-shadow: none; min-width: 0; min-height: 0; padding: 0;
                                border-radius: 999px; color: white; }  /* the glass is the button */
.glass-disc > button.disc-btn:hover { background: rgba(255,255,255,0.10); }
.glass-disc > button.disc-btn:active { background: rgba(0,0,0,0.15); }

popover.glass-menu > contents { background: none; box-shadow: 0 6px 22px rgba(0,0,0,0.45); padding: 0; border-radius: 16px; }
popover.glass-menu label { text-shadow: -1px 1px 2px rgba(0,0,0,0.8), 0 0 1px rgba(0,0,0,1), 0 0 3px rgba(0,0,0,1), 0 0 7px rgba(0,0,0,1), 0 0 10px rgba(0,0,0,0.9), 0 0 14px rgba(0,0,0,0.85), 0 0 22px rgba(0,0,0,0.75); }
.overlay-header:backdrop, .overlay-header:backdrop * { opacity: 1; filter: none; }  /* not faded when unfocused */

/* Under the mouse: pause, stop, the speaker and close pump once a second; the gear ticks round like a watch's
   hand, twice a second (12 steps a turn). */
@keyframes pump { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.12); } }
@keyframes tick { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.glass-disc:hover, .glass-head.pumping { animation: pump 1s ease-in-out infinite; }
/* The volume slider pumps mostly up and down (a little sideways: more would reach the speaker), its knob
   like the buttons. */
@keyframes pump-tall { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.015, 1.8); } }
scale.vol-slider:hover trough { animation: pump-tall 1s ease-in-out infinite; }
scale.vol-slider:hover slider { animation: pump 1s ease-in-out infinite; }
/* The quality list's arrow bobs up and down (never turns). */
@keyframes bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(4px); } }
row:hover .bob-arrow { animation: bob 900ms ease-in-out infinite; }
.overlay-header button.gear-button:hover image { animation: tick 500ms steps(12) infinite; }  /* its own icon only, not the menu's */
"""


def mem_text(b: float) -> str:
    """Bytes for people: kB under a megabyte, else MB with one decimal."""
    return f"{b / 1024:.0f} kB" if b < 1 << 20 else f"{b / (1 << 20):.1f} MB"


def tex_bytes(tex) -> int:
    """What a texture holds: 4 bytes a pixel (on the GPU: VRAM, or GTT on an APU)."""
    return tex.get_width() * tex.get_height() * 4 if tex is not None else 0


def proc_memory(pid="self") -> tuple:
    """(RAM resident, VRAM, GTT, has a GPU client) of a process: /proc/<pid>/status and its DRM fdinfo (each GPU
    client once). Bytes."""
    rss = 0
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
    except OSError:
        pass
    seen, vram, gtt = set(), 0, 0
    try:
        for fd in os.listdir(f"/proc/{pid}/fdinfo"):
            try:
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    info = dict(l.split(":", 1) for l in f if ":" in l)
            except OSError:
                continue
            cid = info.get("drm-client-id", "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)

            def kib(key):
                v = info.get(key, "").split()
                return int(v[0]) * (1024 if v[1:] == ["KiB"] else 1 << 20 if v[1:] == ["MiB"] else 1) if v else 0
            vram += kib("drm-memory-vram")
            gtt += kib("drm-memory-gtt")
    except OSError:
        pass
    return rss, vram, gtt, bool(seen)


def app_memory() -> str:
    """The whole app's memory: RAM (resident) and what the GPU driver counts for it (VRAM, GTT)."""
    rss, vram, gtt, gpu = proc_memory()
    gpu = f", GPU: VRAM {mem_text(vram)} + GTT {mem_text(gtt)} (sdílená RAM pro GPU)" if gpu else ""
    return f"RAM {mem_text(rss)}{gpu}"


# The look's switchable effects (gear menu), set from the settings by Window.apply_look.
EFFECTS = {"mirror": True, "shadows": True, "anim": True, "season": True, "gpu": True}
NO_SHADOW_CSS = """
scale.vol-slider trough, scale.vol-slider slider { box-shadow: none; }
.glass-disc image, .glass-head image, .vol-icon, button.quality-tab image { -gtk-icon-shadow: none; }
"""
NO_ANIM_CSS = """
* { animation: none; }
"""

# While anything animates, the picture enhancing waits (App.anim_hold): smooth animations even on a slow chip.
_ANIM_HOLD: list = [None]


def anim_hold(on: bool) -> None:
    if _ANIM_HOLD[0]:
        _ANIM_HOLD[0](on)


def hold_while_hovered(widget) -> None:
    """The mouse over `widget` (it pumps, ticks or bobs there): the enhancing waits meanwhile."""
    m = Gtk.EventControllerMotion()
    m.connect("enter", lambda *_: anim_hold(True))
    m.connect("leave", lambda *_: anim_hold(False))
    widget.add_controller(m)


class CoverView(Gtk.Widget):
    """A picture filling the widget, "contain" (whole, at the top in a tall
    space, centred in a wide one; small pictures are stretched too) or "cover"
    (filling it, cropped). A new picture morphs in: the old one dissolves into
    it through a soft blur that peaks halfway. A better version of the same
    picture (the next enhancing step) is redrawn slowly instead, no blur: the
    new detail grows in over the old picture."""

    MORPH_MS = 4000  # a new picture: the old one morphs into it
    MORPH_BLUR = 8.0  # a little soft halfway through the morph
    REDRAW_MS = 4000

    SOFT_RES = 0.5  # a blurred picture's texture, of the widget's size: a blur this wide hides the lower resolution

    def __init__(self, fit: str, soft: float = 0.0, dim: float = 1.0, **kw):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN, **kw)
        self.fit = fit
        # Blurred by `soft` px and darkened to `dim` (the backdrop): each picture blurred once into a texture,
        # a morph then cross-fades two textures (a CSS blur redid the blur every frame: ~10 fps while morphing)
        self.soft, self.dim = soft, dim
        self._soft: list = []  # [(picture, w, h, texture)], the pictures up now only
        self.pic = None
        self.old = None
        self.t = 1.0
        self.blur = self.MORPH_BLUR
        self.anim = None
        self.watchers: list = []  # redrawn along (the glass card behind which this shows)
        self._holding = False  # the enhancing waits while this morphs

    def set_paintable(self, pic, redraw: bool = False) -> None:
        if pic is self.pic:
            return
        self.old, self.pic = self.pic, pic
        if self.anim:
            self.anim.pause()
            self.anim = None
        if self.old is None or pic is None:
            self.t = 1.0
            self._hold(False)
            self._changed()
            return
        self._hold(True)
        target = Adw.CallbackAnimationTarget.new(self._tick)
        self.blur = 0.0 if redraw else self.MORPH_BLUR
        self.anim = Adw.TimedAnimation.new(self, 0.0, 1.0, self.REDRAW_MS if redraw else self.MORPH_MS, target)
        self.anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        self.anim.play()

    def morph(self, steps: int):
        """(old picture, how far into the new one 0..1 in `steps` steps) while morphing, else (None, 1.0): the
        glass in front shows the change at the same moment as the picture itself."""
        if self.old is None or self.t >= 1.0 or self.pic is None:
            return None, 1.0
        return self.old, round(self.t * steps) / steps

    def _hold(self, on: bool) -> None:
        if on != self._holding:
            self._holding = on
            anim_hold(on)

    def _tick(self, value: float) -> None:
        self.t = value
        if value >= 1.0:
            self.old = None
            self._hold(False)
        self._changed()

    def _changed(self) -> None:
        self.queue_draw()
        for w in self.watchers:
            w.queue_draw()

    def rect(self, pic, w: float, h: float) -> tuple[float, float, float, float]:
        pw = pic.get_intrinsic_width() or 1
        ph = pic.get_intrinsic_height() or 1
        s = max(w / pw, h / ph) if self.fit == "cover" else min(w / pw, h / ph)
        dw, dh = pw * s, ph * s
        return (w - dw) / 2, ((h - dh) / 2 if self.fit == "cover" else 0.0), dw, dh

    def draw(self, snap, pic, w: float, h: float) -> None:
        x, y, dw, dh = self.rect(pic, w, h)
        snap.save()
        snap.translate(Graphene.Point().init(x, y))
        pic.snapshot(snap, dw, dh)
        snap.restore()

    def _soft_tex(self, pic, w: int, h: int):
        for p, pw, ph, tex in self._soft:
            if p is pic and (pw, ph) == (w, h):
                return tex
        native = self.get_native()
        if not native:
            return None
        q = self.SOFT_RES
        s = Gtk.Snapshot()
        s.scale(q, q)
        s.push_blur(self.soft)  # in the scaled space: the blur as wide on the picture as at full size
        s.push_clip(Graphene.Rect().init(0, 0, w, h))
        self.draw(s, pic, w, h)
        s.pop()
        s.pop()
        node = s.to_node()
        if node is None:
            return None
        tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w * q, h * q))
        self._soft = [e for e in self._soft if e[0] is self.pic or e[0] is self.old] + [(pic, w, h, tex)]
        return tex

    def _draw_soft(self, snap, w: int, h: int) -> None:
        box = Graphene.Rect().init(0, 0, w, h)
        snap.push_color_matrix(_dim_matrix(self.dim), Graphene.Vec4().init(0, 0, 0, 0))
        new = self._soft_tex(self.pic, w, h)
        old = self._soft_tex(self.old, w, h) if self.old is not None and self.t < 1.0 else None
        if old and new:
            snap.push_cross_fade(self.t)
            snap.append_texture(old, box)
            snap.pop()
            snap.append_texture(new, box)
            snap.pop()
        elif new:
            snap.append_texture(new, box)
        snap.pop()

    def do_snapshot(self, snap) -> None:
        w, h = self.get_width(), self.get_height()
        if not self.pic or w <= 0 or h <= 0:
            return
        if self.soft:
            self._draw_soft(snap, w, h)
            return
        if self.old is None or self.t >= 1.0:
            self.draw(snap, self.pic, w, h)
            return
        # The old picture morphs into the new one: a cross-fade, softened a little halfway.
        blur = self.blur * math.sin(math.pi * self.t)
        # a wider blur at half resolution (it hides it; a quarter of the pixels: at 4K the full one dropped
        # frames to 20-30 ms halfway)
        half = blur > 2.0
        if half:
            snap.save()
            snap.scale(0.5, 0.5)
            snap.push_blur(blur / 2)
            snap.scale(2.0, 2.0)
        elif blur > 0.5:
            snap.push_blur(blur)
        snap.push_cross_fade(self.t)
        self.draw(snap, self.old, w, h)
        snap.pop()
        self.draw(snap, self.pic, w, h)
        snap.pop()
        if half:
            snap.pop()
            snap.restore()
        elif blur > 0.5:
            snap.pop()


BACKDROP_BLUR, BACKDROP_SCALE = 36.0, 1.15  # the backdrop's blur (CoverView soft) and its CSS zoom
BACKDROP_DIM = 0.55  # its brightness


# Where the pictures behind the glass have lettering (a cover's title): the glass edges mirror only that.
# Window coordinates, cells of TEXT_CELL analysis pixels at TEXT_Q of the window's size; found in a thread.
TEXT_Q, TEXT_CELL = 0.33, 16
TEXT_MAP = {"key": None, "cells": frozenset(), "version": 0, "busy": False, "waiting": []}


def _text_cells(data: bytes, stride: int, W: int, H: int) -> frozenset:
    """Cells that look like lettering: many thin strokes (a strong edge and the opposite one within a few
    pixels) in a row of cells. Faces, skies, foam score near nothing; titles high."""
    C = TEXT_CELL
    cols, rows = (W + C - 1) // C, (H + C - 1) // C
    score = [[0] * cols for _ in range(rows)]
    maxw = max(3, int(24 * TEXT_Q))
    for y in range(H):
        o = y * stride + 1  # the green byte: brightness enough
        row = data[o:o + W * 3:3]
        srow = score[y // C]
        last_x, last_s = -99, 0
        for x in range(W - 2):
            d = row[x + 2] - row[x]
            if d >= 55 or d <= -55:
                sg = 1 if d > 0 else -1
                if sg != last_s and 2 <= x - last_x <= maxw:
                    srow[x // C] += 1
                    last_s = 0
                else:
                    last_x, last_s = x, sg
    cells = set()
    for r in range(rows):
        for c in range(cols):
            v = score[r][c] / C
            if v >= 0.25 and ((c and score[r][c - 1] / C >= 0.15) or (c + 1 < cols and score[r][c + 1] / C >= 0.15)):
                cells.add((r, c))
    return frozenset(cells)


def _opaque(tex):
    """A rendered texture as a plain opaque one: GTK then skips whatever it covers (an alpha texture is
    blended over everything under it). For a finished lens, drawn once a frame; not for stacked rings."""
    d = Gdk.TextureDownloader.new(tex)
    d.set_format(Gdk.MemoryFormat.R8G8B8)
    data, stride = d.download_bytes()
    return Gdk.MemoryTexture.new(tex.get_width(), tex.get_height(), Gdk.MemoryFormat.R8G8B8, data, stride)


def _dim_matrix(k: float) -> Graphene.Matrix:
    """Colours times k, alpha as it is (CSS brightness(k))."""
    return Graphene.Matrix().init_from_float([k, 0, 0, 0, 0, k, 0, 0, 0, 0, k, 0, 0, 0, 0, 1])


class FlagLogo(Gtk.Widget):
    """The station logo at the top left. Under the mouse it waves like a flag in the wind, held at its left
    edge as on a pole: drawn in thin upright strips, each lifted along a wave running to the free edge and
    shaded where the cloth folds; it settles flat again when the mouse leaves."""

    STRIPS = 28
    WAVES = 1.3  # waves across the flag
    SPEED = 1.6  # waves a second
    LIFT = 0.09  # how high the free edge lifts, in the logo's height

    def __init__(self, size: int = 34, **kw):
        super().__init__(**kw)
        self.pic = None
        self.size = size
        self.amp = 0.0  # 0 flat .. 1 waving
        self.phase = 0.0
        self.hover = False
        self.tick = None
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._wave(True))
        motion.connect("leave", lambda *_: self._wave(False))
        self.add_controller(motion)

    def get_paintable(self):
        return self.pic

    def set_from_paintable(self, pic) -> None:
        self.pic = pic
        self.queue_draw()

    def set_pixel_size(self, size: int) -> None:
        self.size = size
        self.queue_resize()

    def do_measure(self, orientation, for_size):
        return self.size, self.size, -1, -1

    def _wave(self, on: bool) -> None:
        if on and not EFFECTS["anim"]:
            return  # animations off: the flag stays still
        self.hover = on
        if self.tick is None:
            anim_hold(True)
            self.tick = self.add_tick_callback(self._step)

    def _step(self, _widget, clock) -> bool:
        self.phase = clock.get_frame_time() / 1e6 * 2 * math.pi * self.SPEED
        self.amp += ((1.0 if self.hover else 0.0) - self.amp) * 0.12
        self.queue_draw()
        if not self.hover and self.amp < 0.01:
            self.amp, self.tick = 0.0, None
            anim_hold(False)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def do_snapshot(self, snap) -> None:
        if not self.pic:
            return
        w, h = self.get_width(), self.get_height()
        if self.amp <= 0:
            self.pic.snapshot(snap, w, h)
            return
        sw = w / self.STRIPS
        for i in range(self.STRIPS):
            x = i * sw
            f = (x + sw / 2) / w  # 0 at the pole, 1 at the free edge
            ang = self.phase - f * 2 * math.pi * self.WAVES
            lift = math.sin(ang) * h * self.LIFT * f * self.amp
            fold = math.cos(ang) * 0.22 * f * self.amp  # light on one side of a fold, shade on the other
            snap.save()
            snap.translate(Graphene.Point().init(0, lift))
            snap.push_clip(Graphene.Rect().init(x, 0, sw + 0.6, h))  # a hair wider: no seams
            self.pic.snapshot(snap, w, h)
            snap.append_color(_rgba(255, 255, 255, fold) if fold > 0 else _rgba(0, 0, 0, -fold),
                              Graphene.Rect().init(x, 0, sw + 0.6, h))
            snap.pop()
            snap.restore()


# The glass drawn by the graphics card in one pass: a small OpenGL shader works out, for every pixel, which point
# behind the glass it shows (fish-eye, magnifier, the rim's bend), from one picture of what's behind uploaded
# when that changes. GTK draws with thousands of ring layers otherwise. OpenGL through libepoxy (which GTK
# itself uses: nothing to install); OpenGL ES 3 or desktop OpenGL 3.3, any GPU of the last ten years. Anything
# failing: the ring layers, as before.
def gpu_text() -> str:
    if not EFFECTS["gpu"]:
        return ("Vypnuto: lupa, rybí oko a zrcadlení na okrajích skel se skládají postaru z tisíců soustředných "
                "vrstev (GTK): pomaleji, velké panely ⓘ a ⚙ se dokreslují postupně, dýchání tlačítek hrubší")
    if GPU.ok is False:
        return f"Nedostupné: {GPU.why} · skla se skládají z vrstev postaru"
    st = GPU.stats
    what = ("Zapnuto: lupu, rybí oko, ohyb okraje a zrcadlení u okrajů všech skel (panely ⓘ a ⚙, menu Kvalita, "
            "karta pořadu, kulatá tlačítka, pilulky, záložky) počítá grafická karta jedním průchodem (OpenGL "
            "shader) pro každý pixel, místo tisíců vrstev: hned hotové, hladké bez prstenců, plynulé i na 4K a "
            "165 Hz; obraz za sklem se do karty nahraje jen když se změní")
    if not GPU.ok:
        return what + " · spustí se s prvním sklem"
    per = st["ms"] / st["frames"] if st["frames"] else 0.0
    mem = st["live_bytes"] + sum(GPU.sources.values())
    return (f"{what} · {GPU.why} · teď: {st['frames']} výpočtů čočky ({per:.2f} ms), obraz za sklem nahrán "
            f"{st['uploads']}× (za celou dobu přeneseno {mem_text(st['upload_bytes'])}: každé nahrání přepíše "
            f"předchozí, nic se nehromadí) · drží teď v paměti GPU: {mem_text(mem)}")


class GPU:
    ok = None  # None: not tried yet; False: not available (the layers instead)
    why = ""  # what failed, for the effect's note
    ctx = None
    stats = {"frames": 0, "uploads": 0, "upload_bytes": 0, "ms": 0.0, "live": 0, "live_bytes": 0}
    _gl = None
    _prog = None
    _loc = {}
    _vao = 0
    _fbo = 0
    _free = []  # output textures GTK has let go of: (id, w, h), drawn into again
    sources = {}  # uploaded pictures: id -> bytes
    _SIGS = {
        "glGenTextures": (None, "i", "p"), "glDeleteTextures": (None, "i", "p"), "glBindTexture": (None, "u", "u"),
        "glTexImage2D": (None, "u", "i", "i", "i", "i", "i", "u", "u", "p"), "glTexParameteri": (None, "u", "u", "i"),
        "glGenerateMipmap": (None, "u"), "glPixelStorei": (None, "u", "i"),
        "glGenFramebuffers": (None, "i", "p"), "glBindFramebuffer": (None, "u", "u"),
        "glFramebufferTexture2D": (None, "u", "u", "u", "u", "i"), "glCheckFramebufferStatus": ("u", "u"),
        "glViewport": (None, "i", "i", "i", "i"), "glCreateShader": ("u", "u"),
        "glShaderSource": (None, "u", "i", "p", "p"), "glCompileShader": (None, "u"),
        "glGetShaderiv": (None, "u", "u", "p"), "glGetShaderInfoLog": (None, "u", "i", "p", "s"),
        "glCreateProgram": ("u",), "glAttachShader": (None, "u", "u"), "glLinkProgram": (None, "u"),
        "glGetProgramiv": (None, "u", "u", "p"), "glUseProgram": (None, "u"),
        "glGetUniformLocation": ("i", "u", "s"), "glUniform1i": (None, "i", "i"), "glUniform1f": (None, "i", "f"),
        "glUniform2f": (None, "i", "f", "f"), "glUniform4f": (None, "i", "f", "f", "f", "f"),
        "glActiveTexture": (None, "u"), "glGenVertexArrays": (None, "i", "p"), "glBindVertexArray": (None, "u"),
        "glDrawArrays": (None, "u", "i", "i"), "glFinish": (None,), "glGetError": ("u",),
        "glDisable": (None, "u"), "glGetString": ("s", "u"),
    }
    VS = """
out vec2 uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}"""
    # (rows: the uploaded picture's first row is its top, and so is the output's: uv.y 0 at the top for both)
    FS = """
in vec2 uv;
out vec4 color;
uniform sampler2D src;
uniform vec2 size;     // the glass (logical px)
uniform vec4 box;      // where the picture of what's behind lies, in the glass's coordinates
uniform float radius, mag, rx, ry, depth, curve;
float ring_r(float d) {  // a ring's corners, d in from the edge (as GlassCard._ring_r)
    return max(0.0, min(max(radius - d, radius * 0.6 + d * 0.9), min(size.x - 2.0 * d, size.y - 2.0 * d) * 0.5));
}
float outside(vec2 p, float d) {  // > 0: p outside the ring d in from the edge
    float r = ring_r(d);
    vec2 q = abs(p - size * 0.5) - (size * 0.5 - d) + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
void main() {
    vec2 p = uv * size;
    float lo = 0.0, hi = min(size.x, size.y) * 0.5;  // how deep inside the glass p is: the deepest ring holding it
    for (int i = 0; i < 18; i++) {
        float m = (lo + hi) * 0.5;
        if (outside(p, m) <= 0.0) lo = m; else hi = m;
    }
    float b = lo < depth ? pow(1.0 - lo / depth, curve) : 0.0;  // 1 at the edge, 0 from depth inwards
    vec2 s = vec2(mag + (rx - mag) * b, mag + (ry - mag) * b);
    vec2 c = size * 0.5;
    color = texture(src, (c + (p - c) / s - box.xy) / box.zw);
}"""

    @classmethod
    def start(cls) -> bool:
        if cls.ok is not None:
            return cls.ok
        cls.ok = False
        if os.environ.get("RADIO_BEAT_GPU") == "0":
            cls.why = "vypnuto proměnnou RADIO_BEAT_GPU=0"
            return False
        try:
            lib = ctypes.CDLL("libepoxy.so.0")
            kinds = {"i": ctypes.c_int, "u": ctypes.c_uint, "f": ctypes.c_float, "p": ctypes.c_void_p,
                     "s": ctypes.c_char_p, None: None}
            gl = types_ns()
            for name, sig in cls._SIGS.items():
                ptr = ctypes.c_void_p.in_dll(lib, "epoxy_" + name).value
                setattr(gl, name, ctypes.CFUNCTYPE(kinds[sig[0]], *(kinds[k] for k in sig[1:]))(ptr))
            cls._gl = gl
            ctx = Gdk.Display.get_default().create_gl_context()
            ctx.realize()
            ctx.make_current()
            es = ctx.get_use_es()
            major, minor = ctx.get_version()
            if (major, minor) < ((3, 0) if es else (3, 3)):
                raise RuntimeError(f"OpenGL {'ES ' if es else ''}{major}.{minor} je moc staré")
            head = "#version 300 es\nprecision highp float;\n" if es else "#version 330 core\n"
            prog = gl.glCreateProgram()
            for kind, src in ((0x8B31, cls.VS), (0x8B30, cls.FS)):
                sh = gl.glCreateShader(kind)
                text = ctypes.c_char_p((head + src).encode())
                gl.glShaderSource(sh, 1, ctypes.byref(text), None)
                gl.glCompileShader(sh)
                done = ctypes.c_int()
                gl.glGetShaderiv(sh, 0x8B81, ctypes.byref(done))
                if not done.value:
                    log = ctypes.create_string_buffer(2048)
                    gl.glGetShaderInfoLog(sh, 2048, None, log)
                    raise RuntimeError(log.value.decode(errors="replace").strip())
                gl.glAttachShader(prog, sh)
            gl.glLinkProgram(prog)
            done = ctypes.c_int()
            gl.glGetProgramiv(prog, 0x8B82, ctypes.byref(done))
            if not done.value:
                raise RuntimeError("shader se nepodařilo slinkovat")
            cls._prog = prog
            cls._loc = {n: gl.glGetUniformLocation(prog, n.encode())
                        for n in ("src", "size", "box", "radius", "mag", "rx", "ry", "depth", "curve")}
            vao, fbo = ctypes.c_uint(), ctypes.c_uint()
            gl.glGenVertexArrays(1, ctypes.byref(vao))
            gl.glGenFramebuffers(1, ctypes.byref(fbo))
            cls._vao, cls._fbo = vao.value, fbo.value
            cls.ctx = ctx
            cls.ok = True
            name = (gl.glGetString(0x1F01) or b"").decode(errors="replace")  # GL_RENDERER: which GPU
            name = re.sub(r"\s*\(.*$", "", name).strip()  # (without the driver's details)
            cls.why = f"{name or 'GPU'}, OpenGL {'ES ' if es else ''}{major}.{minor}"
            cls.name = cls.why  # (kept for a retry after a failure)
        except Exception as e:  # no libepoxy, no GL, an old GPU: the layers as before
            cls.why = f"nedostupné ({e})"
            cls.ok = False
        print("Rádio Beat: GPU sklo:", "zapnuto," if cls.ok else "", cls.why, file=sys.stderr)
        return cls.ok

    @classmethod
    def upload(cls, tex, old: int = 0) -> int:
        """A GdkTexture (what's behind) into a GL texture with mipmaps (the rim squeezes it): its id."""
        gl = cls._gl
        cls.ctx.make_current()
        d = Gdk.TextureDownloader.new(tex)
        d.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
        data, stride = d.download_bytes()
        w, h = tex.get_width(), tex.get_height()
        buf = data.get_data()
        tid = ctypes.c_uint(old)
        if not old:
            gl.glGenTextures(1, ctypes.byref(tid))
        gl.glBindTexture(0x0DE1, tid.value)
        gl.glPixelStorei(0x0CF5, 4)  # UNPACK_ALIGNMENT
        gl.glPixelStorei(0x0CF2, stride // 4)  # UNPACK_ROW_LENGTH
        gl.glTexImage2D(0x0DE1, 0, 0x8058, w, h, 0, 0x1908, 0x1401, ctypes.c_char_p(buf))
        gl.glPixelStorei(0x0CF2, 0)
        gl.glGenerateMipmap(0x0DE1)
        for pname, val in ((0x2801, 0x2703), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
            gl.glTexParameteri(0x0DE1, pname, val)  # trilinear, clamped at the edges
        cls.stats["uploads"] += 1
        cls.stats["upload_bytes"] += w * h * 4
        cls.sources[tid.value] = w * h * 4 * 4 // 3  # (with its mipmaps)
        return tid.value

    @classmethod
    def forget(cls, tid: int) -> None:
        if tid and cls.ok:
            cls.ctx.make_current()
            cls._gl.glDeleteTextures(1, ctypes.byref(ctypes.c_uint(tid)))
            cls.sources.pop(tid, None)

    @classmethod
    def lens(cls, src: int, pw: int, ph: int, u: dict):
        """The lens (uniforms `u`) drawn from texture `src` into a pw x ph texture, as a GdkTexture."""
        gl = cls._gl
        t = time.perf_counter()
        cls.ctx.make_current()
        out = next((f for f in cls._free if f[1:] == (pw, ph)), None)
        if out:
            cls._free.remove(out)
        while len(cls._free) > 6:  # sizes no longer drawn (a resized window): their memory back
            old, ow, oh = cls._free.pop(0)
            gl.glDeleteTextures(1, ctypes.byref(ctypes.c_uint(old)))
            cls.stats["live"] -= 1
            cls.stats["live_bytes"] -= ow * oh * 4
        if out:  # one GTK let go of, the same size: drawn into again
            tid = out[0]
            gl.glBindTexture(0x0DE1, tid)
        else:
            c = ctypes.c_uint()
            gl.glGenTextures(1, ctypes.byref(c))
            tid = c.value
            gl.glBindTexture(0x0DE1, tid)
            gl.glTexImage2D(0x0DE1, 0, 0x8058, pw, ph, 0, 0x1908, 0x1401, None)
            gl.glTexParameteri(0x0DE1, 0x2801, 0x2601)
            gl.glTexParameteri(0x0DE1, 0x2800, 0x2601)
            cls.stats["live"] += 1
            cls.stats["live_bytes"] += pw * ph * 4
        gl.glBindFramebuffer(0x8D40, cls._fbo)
        gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tid, 0)
        gl.glViewport(0, 0, pw, ph)
        gl.glDisable(0x0BE2)  # (no blending: the output is the picture)
        gl.glUseProgram(cls._prog)
        gl.glActiveTexture(0x84C0)
        gl.glBindTexture(0x0DE1, src)
        L = cls._loc
        gl.glUniform1i(L["src"], 0)
        gl.glUniform2f(L["size"], u["w"], u["h"])
        gl.glUniform4f(L["box"], *u["box"])
        for n in ("radius", "mag", "rx", "ry", "depth", "curve"):
            gl.glUniform1f(L[n], u[n])
        gl.glBindVertexArray(cls._vao)
        gl.glDrawArrays(0x0004, 0, 3)
        gl.glBindFramebuffer(0x8D40, 0)
        gl.glFinish()  # done before GTK reads it (it may in another context)
        b = Gdk.GLTextureBuilder()
        b.set_context(cls.ctx)
        b.set_id(tid)
        b.set_width(pw)
        b.set_height(ph)
        b.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
        tex = b.build(lambda *_: cls._free.append((tid, pw, ph)), tid)  # (let go by GTK: drawn into again)
        if tex is None:
            raise RuntimeError("GTK nepřijal texturu")
        cls.stats["frames"] += 1
        cls.stats["ms"] += (time.perf_counter() - t) * 1000
        return tex


def types_ns():
    import types
    return types.SimpleNamespace()


def draw_layers(s, layers, ref, skip=()) -> None:
    """Widgets [(widget, its paintable or None)] drawn into `s` where they are in `ref`, leaving out the ones
    in `skip` (glass buttons a glass shows mirrored: not also as they are), what's around them piece by piece."""
    skip = set(skip)

    def draw(w, paint=None):
        if w in skip or not w.get_mapped():
            return
        if any(t.is_ancestor(w) for t in skip):
            c = w.get_first_child()
            while c is not None:
                draw(c)
                c = c.get_next_sibling()
            return
        ok, p = w.compute_point(ref, Graphene.Point().init(0, 0))
        if ok:
            s.save()
            s.translate(p)
            (paint or Gtk.WidgetPaintable.new(w)).snapshot(s, w.get_width(), w.get_height())
            s.restore()
    for w, paint in layers:
        draw(w, paint)


def widget_sig(w, out: list) -> None:
    """What `w` and what's in it show, into `out`: compared before the glass pictures the window anew (a
    picture of it is ~12 ms; asked for whenever anything seems changed, even when nothing visible did). Its
    place, size, state, text and value; this app's own widgets' public state too (the clock's colon, blinking,
    left out: SIG_SKIP)."""
    if not w.get_visible():
        out.append(None)
        return
    par = w.get_parent()
    ok, b = w.compute_bounds(par) if par is not None else (False, None)
    out += [type(w), w.get_mapped(), w.get_opacity(), int(w.get_state_flags()), tuple(w.get_css_classes()),
            (round(b.get_x(), 1), round(b.get_y(), 1), round(b.get_width(), 1), round(b.get_height(), 1)) if ok else None]
    if isinstance(w, Gtk.Label):
        out.append(w.get_label())
    elif isinstance(w, Gtk.Range):
        out.append(w.get_value())
    elif isinstance(w, Gtk.Image):
        out += [w.get_icon_name(), w.get_paintable()]
    elif isinstance(w, Gtk.Picture):
        out.append(w.get_paintable())
    if type(w).__module__ == __name__:
        skip = getattr(w, "SIG_SKIP", ())
        for k, v in vars(w).items():
            if k[0] != "_" and k not in skip and not callable(v) and not isinstance(v, (dict, set)):
                out.append((k, tuple(v) if isinstance(v, list) else v))
    c = w.get_first_child()
    while c is not None:
        if not isinstance(c, (Gtk.Popover, Gtk.Native)):  # (menus hanging from it: their own surface)
            widget_sig(c, out)
        c = c.get_next_sibling()


class GlassCard(Gtk.Box):
    """A liquid-glass panel: what's behind it (the backdrop and the cover) is
    not blurred but refracted, like through a lens: sharp and true in the
    middle, bent hard in a narrow band at the rim (it shows what lies further out, squeezed
    in). Drawn as layers, each from a little further in and a little more
    bent, blended through soft masks so there are no steps; then a dark tint
    (Background shade), a soft top highlight and a thin bright edge. GTK has
    no displacement filter or backdrop-filter, so it's drawn here."""

    RIM = 1.6  # the curved side wall's width (its shading), in corner radii
    MAG = 1.25  # the middle, magnified like through a thick magnifying glass
    # Fish-eye: the bend runs across the whole glass, not just its rim: the middle magnified most, less and less
    # towards the edges, which show a little of what lies beyond them.
    FISH_REACH = 0.45  # how far beyond its edge the glass shows, in half its smaller side
    FISH_CURVE = 1.7  # how the magnification falls off from the middle (higher: flatter middle)

    def __init__(self, behind: list, **kw):
        super().__init__(**kw)
        self.behind = behind  # [(CoverView, darken 0..1)], back to front
        self.radius = 28.0
        self._blurred = []  # [(key, texture)]: the blurred backdrop, made once per picture and size (two morphing)
        self.shade = 1.0  # scales the darkening (gear menu: Background shade)
        self.tint = 0.0  # a dark tint over the glass (times shade): none, the glass clear (no gray veil on pale covers)
        self.mag = self.MAG  # < 1: a concave lens (things look smaller)
        self.frame = True  # its lit edges and border (not for the puddle's neck)
        self.warp_content = False  # what's inside bent by the glass too (the big panels)
        self.cache_lens = True  # the lens drawn from a texture while nothing behind it changes
        self.band = 0  # px: the lens bends only in this band along the edge (thick glass slab); 0: fish-eye
        self.reach = self.FISH_REACH  # how far beyond its edge it shows
        self.lens_max = 1e9  # px: the bend's depth from the edge at most (big glass: as thick as the card's)
        self.curve = self.FISH_CURVE  # higher: the bend gathers at the rim (a thick glass edge), the middle flatter
        self.shine = False  # a big lens: a curved reflection over it and its rim darker (it reads as a lens)
        self.wall = self.RIM  # the curved side wall's shaded width, in corner radii
        self.pulse_tick = None  # the mouse on it: the magnification breathes, like a magnifier moved up and down
        self.lift = 0.0  # the card drawn this much bigger meanwhile (it pumps, outline and all)
        self._lens_cache = (None, None)
        self._flat_cache = (None, None)  # (key, (texture, where)): what's behind, flattened for the lens
        self._build = None  # a lens texture being built over several frames (_lens_texture)
        # each ring drawn from every layer behind, not one flattened picture: slower (the 'i' panel ~2 s of work
        # at 4K, spread over the frames), but its edges show the faint echoes of the picture the user likes
        self.echoes = False
        self._build_tick = None
        self.concave = False  # lit like a dish: shade along the top inside edge, light along the bottom
        self.color = None  # (r, g, b, alpha): colored glass
        self.scrim = None  # the shaded panel it lies on: its shading copied into the glass (see-through)
        self.sharp_bars = False  # the blurred bars beside the cover seen sharp (the cover filling them) instead
        self.depth = True  # a soft shadow below the card: it floats over the cover
        self.drop = False  # the letters' shadow under it (buttons, pills): they stand out like the text
        self.flash = 0.0  # 1 → 0 when the song changes
        self.flash_anim = None
        self.mirrors = []  # glass buttons beside it: seen through it like the rest, but upside down (a mirror)
        # What lies just beyond its top and bottom edges (what the rim bends in) seen mirrored about the edge:
        # upside down, the closest part right at the edge (the buttons in `mirrors` drawn over it as before).
        self.edge_mirror = False
        self._edge_reach = 0.0  # how far beyond its edges the rim reaches (_lens_plan)
        self._edge_reach_x = 0.0
        self._mirror_tex = None  # the mirrored strips, rendered once per lens build (drawn by every ring)
        self._mirror_paints = {}
        self._gpu = {}  # the GPU lens: its uploaded picture of what's behind, its last output and what made it
        self._flat_mirror = False  # the mirrored strips drawn into the flat picture (the GPU lens bends it all)

    PULSE = 0.2  # how much more it magnifies at the top of a breath
    LIFT = 0.04  # and how much bigger the whole card gets then (outline, text and all), like the buttons' pump
    PULSE_HZ = 1.0

    def breathe(self) -> None:
        """Under the mouse the magnification rises and falls once a second, as if the glass were lifted
        towards the eye and lowered again; it eases back when the mouse leaves."""
        self._base_mag = self.mag
        self._breath_on, self._breath_amp = False, 0.0
        m = Gtk.EventControllerMotion()
        m.connect("enter", lambda *_: self._breath(True))
        m.connect("leave", lambda *_: self._breath(False))
        self.add_controller(m)

    def _breath(self, on: bool) -> None:
        self._breath_on = on
        if on and self.pulse_tick is None:
            anim_hold(True)
            self.pulse_tick = self.add_tick_callback(self._breath_step)

    def _breath_step(self, _w, clock) -> bool:
        t = clock.get_frame_time() / 1e6
        self._breath_amp += ((1.0 if self._breath_on else 0.0) - self._breath_amp) * 0.1
        wave = self._breath_amp * (1 - math.cos(2 * math.pi * self.PULSE_HZ * t)) / 2
        self.mag = self._base_mag + self.PULSE * wave
        self.lift = self.LIFT * wave
        self.queue_draw()
        if not self._breath_on and self._breath_amp < 0.01:
            self.mag, self.pulse_tick, self.lift = self._base_mag, None, 0.0
            anim_hold(False)
            self.queue_draw()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def blink(self) -> None:
        """A brief bright flash (a new song)."""
        if self.flash_anim:
            self.flash_anim.pause()
        target = Adw.CallbackAnimationTarget.new(lambda v: (setattr(self, "flash", v), self.queue_draw()))
        self.flash_anim = Adw.TimedAnimation.new(self, 1.0, 0.0, 900, target)
        self.flash_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim_hold(True)
        GLib.timeout_add(950, lambda: (anim_hold(False), False)[1])
        self.flash_anim.play()

    def _behind(self, snap, w: float, h: float, sx: float, sy: float) -> None:
        """What's behind the card, zoomed around its middle: > 1 magnified, < 1 squeezed (shows what's beyond)."""
        cx, cy = w / 2, h / 2
        snap.save()
        if abs(sx - 1) > 1e-4 or abs(sy - 1) > 1e-4:
            snap.translate(Graphene.Point().init(cx, cy))
            snap.scale(sx, sy)
            snap.translate(Graphene.Point().init(-cx, -cy))
        for view, darken in self.behind:
            pic = view.pic
            ok, p = self._where(view)
            if not pic or not ok:
                continue
            old, t = self._morph(view)  # (the picture morphing into a new one: the glass shows it as it is)
            vw, vh = view.get_width(), view.get_height()
            if view.has_css_class("backdrop") and self.sharp_bars:
                # Sharp: the cover filling the window (what the bars are a blur of), zoomed like them.
                ok, p = self._where(view.get_parent())
                if ok:
                    self._mixed(snap, old, t, pic, lambda q: self._sharp(snap, view, q, p, vw, vh))
                    if darken:
                        snap.append_color(_rgba(0, 0, 0, darken), Graphene.Rect().init(p.x, p.y, vw, vh))
                continue
            if view.has_css_class("backdrop"):
                # The blurred bars beside the cover: blurred here too, like around the card, and bent like
                # the cover (thick glass all over, just blurry there).
                # Where it sits before its CSS zoom (the parent's origin): the texture has the zoom in it.
                ok, p = self._where(view.get_parent())
                if ok:
                    self._mixed(snap, old, t, pic, lambda q: self._blurry(snap, view, q, p, vw, vh, darken))
                continue
            snap.save()
            snap.translate(p)
            self._mixed(snap, old, t, pic, lambda q: view.draw(snap, q, vw, vh))
            if darken:
                snap.append_color(_rgba(0, 0, 0, darken), Graphene.Rect().init(0, 0, vw, vh))
            snap.restore()
        if self.edge_mirror and (sy < 1 or self._flat_mirror) and not getattr(self, "_mirroring", False):
            self._mirror_edges(snap, w, h)  # (only rings squeezing it in see beyond the edges)
        for tile in self.mirrors:
            ok, p = self._where(tile)
            if ok and tile.get_mapped():
                snap.save()
                snap.translate(p)
                self.draw_mirrored(snap, tile)
                snap.restore()
        snap.restore()

    def _morph(self, view):
        """(old picture, mix 0..1) of a view morphing into a new picture, else (None, 1.0); in steps the lens can
        keep up with: smooth on the GPU, else just halfway (the layers take frames to build)."""
        if not hasattr(view, "morph"):
            return None, 1.0
        return view.morph(24 if EFFECTS["gpu"] and GPU.ok else 2)

    @staticmethod
    def _mixed(snap, old, t: float, pic, draw) -> None:
        """draw(picture): the old picture cross-faded into the new one at t, or just the new one."""
        if old is None or t >= 1.0:
            draw(pic)
            return
        if t <= 0.0:
            draw(old)
            return
        snap.push_cross_fade(t)
        draw(old)
        snap.pop()
        draw(pic)
        snap.pop()

    @staticmethod
    def _sharp(snap, view, pic, p, vw: float, vh: float) -> None:
        """The cover filling the window (sharp), zoomed like the bars, and past the window's edges too."""
        for zoom in (BACKDROP_SCALE * 1.3, BACKDROP_SCALE):
            snap.save()
            snap.translate(p)
            snap.translate(Graphene.Point().init(vw / 2, vh / 2))
            snap.scale(zoom, zoom)
            snap.translate(Graphene.Point().init(-vw / 2, -vh / 2))
            view.draw(snap, pic, vw, vh)
            snap.restore()

    def _blurry(self, snap, view, pic, p, vw: float, vh: float, darken: float) -> None:
        """The blurred bars, and past the window's edges (glass near them bends what lies beyond) the blur
        mirrored at each edge, seamless (a stretched copy left a seam the lens repeated in rings)."""
        tex = self._backdrop(view, pic, vw, vh, darken)
        if not tex:
            return
        for mx in (-1, 0, 1):
            for my in (-1, 0, 1):
                snap.save()  # a mirror copy: flipped about the window's edge it touches
                snap.translate(Graphene.Point().init(p.x + (2 * vw if mx == 1 else 0), p.y + (2 * vh if my == 1 else 0)))
                snap.scale(-1 if mx else 1, -1 if my else 1)
                snap.append_texture(tex, Graphene.Rect().init(0, 0, vw, vh))
                snap.restore()

    def _mirror_edges(self, snap, w: float, h: float) -> None:
        """Above the top edge and below the bottom one, as far as the rim reaches: what lies there flipped
        within that strip, so the rim (which shows the strip's far side right at the edge) shows the part
        closest to the edge there, upside down, further parts further in: a mirror at the edge."""
        b = self._edge_reach + 2
        if b <= 2:
            return
        # Rendered once into textures (thousands of rings at 4K each drawing the pictures again was 3x the
        # work: menus stuttered), then each ring draws just those.
        key = (round(w), round(h), round(b), self._build["key"] if self._build else None)
        if self._mirror_tex is None or self._mirror_tex[0] != key:
            self._mirror_tex = (key, self._render_strips(w, h, b))
        for tex, rect in self._mirror_tex[1]:
            snap.append_texture(tex, rect)

    def _text_spans(self, y0: float, b: float) -> list:
        """(x from, x to) in the glass's coordinates where the pictures between y0 and y0 + b have lettering;
        the map found anew (in a thread) when the pictures or the window change, meanwhile nothing."""
        win = self.get_root()
        ok, p = win.compute_point(self, Graphene.Point().init(0, 0)) if win else (False, None)
        if not ok:
            return []
        key = tuple((id(v.pic), v.get_width(), v.get_height()) for v, _ in self.behind) + \
            (win.get_width(), win.get_height())
        if TEXT_MAP["key"] != key:
            self._find_text(win, p, key)
        if TEXT_MAP["key"] != key:
            return []
        C = TEXT_CELL / TEXT_Q  # a cell, in window px
        r0, r1 = int((y0 - p.y) // C), int((y0 + b - p.y) // C)
        cols = sorted({c for r, c in TEXT_MAP["cells"] if r0 <= r <= r1})
        spans = []
        for c in cols:
            xa, xb = p.x + (c - 1) * C, p.x + (c + 2) * C  # a cell either side: whole letters, soft ends
            if spans and xa <= spans[-1][1]:
                spans[-1][1] = xb
            else:
                spans.append([xa, xb])
        return spans

    def _find_text(self, win, p, key) -> None:
        if TEXT_MAP["busy"] == key:
            if self not in TEXT_MAP["waiting"]:
                TEXT_MAP["waiting"].append(self)
            return
        native = self.get_native()
        W, H = int(win.get_width() * TEXT_Q), int(win.get_height() * TEXT_Q)
        if not native or W < 8 or H < 8:
            return
        st = Gtk.Snapshot()
        st.scale(TEXT_Q, TEXT_Q)
        st.translate(Graphene.Point().init(-p.x, -p.y))  # the window's corner at the texture's
        self._mirroring = True
        try:
            self._behind_pictures(st, self.get_width(), self.get_height())
        finally:
            self._mirroring = False
        node = st.to_node()
        if node is None:
            return
        d = Gdk.TextureDownloader.new(native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, W, H)))
        d.set_format(Gdk.MemoryFormat.R8G8B8)
        data, stride = d.download_bytes()
        data = data.get_data()
        TEXT_MAP["busy"], TEXT_MAP["waiting"] = key, [self]

        def work():
            cells = _text_cells(data, stride, W, H)

            def done():
                if TEXT_MAP["busy"] == key:
                    TEXT_MAP.update(key=key, cells=cells, busy=False, version=TEXT_MAP["version"] + 1)
                    for g in TEXT_MAP["waiting"]:
                        g.queue_draw()  # its lens built again, with the mirror where the lettering is
                        root = g.get_root()
                        if hasattr(root, "refresh_effects"):
                            root.refresh_effects()
                    TEXT_MAP["waiting"] = []
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _render_strips(self, w: float, h: float, b: float) -> list:
        native = self.get_native()
        if not native:
            return []
        surf = native.get_surface()
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
        x0, sw = -self._edge_reach_x - 2, w + 2 * self._edge_reach_x + 4
        out = []
        self._mirroring = True
        try:
            for y0 in (-b, h):  # the strip above, the strip below
                spans = self._text_spans(y0, b)
                if not spans:
                    continue  # no lettering beyond this edge: plain fisheye there
                st = Gtk.Snapshot()
                st.scale(k, k)
                st.translate(Graphene.Point().init(-x0, -y0))
                rect = Graphene.Rect().init(x0, y0, sw, b)
                st.push_clip(rect)

                def span_mask():  # only where the lettering is, its ends soft
                    st.push_mask(Gsk.MaskMode.ALPHA)
                    for xa, xb in spans:
                        soft = (TEXT_CELL / TEXT_Q) * 0.8
                        st.append_linear_gradient(Graphene.Rect().init(xa, y0, xb - xa, b),
                                                  Graphene.Point().init(xa, 0), Graphene.Point().init(xb, 0),
                                                  [_stop(0.0, 255, 255, 255, 0.0),
                                                   _stop(min(0.45, soft / (xb - xa)), 255, 255, 255, 1.0),
                                                   _stop(max(0.55, 1 - soft / (xb - xa)), 255, 255, 255, 1.0),
                                                   _stop(1.0, 255, 255, 255, 0.0)])
                    st.pop()

                # under it, what's inside the glass mirrored at its edge (continuous with it there): the lettering
                # beyond the edge is seen only mirrored, never also as it is (an unmirrored ghost through the fade)
                edge = 0.0 if y0 < 0 else h
                span_mask()
                st.save()
                st.translate(Graphene.Point().init(0, 2 * edge))  # y -> 2*edge - y
                st.scale(1, -1)
                self._behind_pictures(st, w, h)
                st.restore()
                st.pop()
                # fading out towards the glass's edge side, where the rim turns from mirror to see-through: no
                # hard line between the two
                far_y, near_y = (y0, y0 + b) if y0 < 0 else (y0 + b, y0)
                st.push_mask(Gsk.MaskMode.ALPHA)
                st.append_linear_gradient(rect, Graphene.Point().init(0, far_y), Graphene.Point().init(0, near_y),
                                          [_stop(0.0, 255, 255, 255, 1.0), _stop(0.45, 255, 255, 255, 1.0),
                                           _stop(1.0, 255, 255, 255, 0.0)])
                st.pop()
                span_mask()
                st.save()
                st.translate(Graphene.Point().init(0, 2 * y0 + b))  # y -> 2*y0 + b - y: flipped within it
                st.scale(1, -1)
                self._behind_pictures(st, w, h)
                st.restore()
                st.pop()
                st.pop()
                st.pop()
                node = st.to_node()
                if node is not None:
                    tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, sw * k, b * k))
                    out.append((tex, Graphene.Rect().init(x0, y0, sw, b)))
        finally:
            self._mirroring = False
        return out

    def _behind_pictures(self, snap, w: float, h: float) -> None:
        """What's behind, unzoomed, without the mirrored buttons."""
        keep, self.mirrors = self.mirrors, []
        try:
            self._behind(snap, w, h, 1.0, 1.0)
        finally:
            self.mirrors = keep

    def _where(self, widget):
        """Where `widget` (behind the card) starts, in the card's coordinates."""
        return widget.compute_point(self, Graphene.Point().init(0, 0))

    def _backdrop(self, view, pic, vw: int, vh: int, darken: float):
        """The backdrop as the window shows it (blurred, darkened, a bit enlarged), as a texture: drawn once,
        not once per lens layer."""
        key = (id(pic), vw, vh, darken)
        for k, t in self._blurred:
            if k == key:
                return t
        native = self.get_native()
        if not native or vw <= 0 or vh <= 0:
            return None
        s = Gtk.Snapshot()
        s.push_clip(Graphene.Rect().init(0, 0, vw, vh))
        s.push_blur(BACKDROP_BLUR)
        s.translate(Graphene.Point().init(vw / 2, vh / 2))
        s.scale(BACKDROP_SCALE, BACKDROP_SCALE)
        s.translate(Graphene.Point().init(-vw / 2, -vh / 2))
        view.draw(s, pic, vw, vh)
        s.pop()
        s.append_color(_rgba(0, 0, 0, darken), Graphene.Rect().init(0, 0, vw, vh))  # its CSS brightness
        s.pop()
        node = s.to_node()
        if node is None:
            return None
        tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, vw, vh))
        self._blurred = [e for e in self._blurred if e[0][0] in (id(getattr(view, "pic", None)),
                                                                  id(getattr(view, "old", None)))][-2:] + [(key, tex)]
        return tex

    def _ring_r(self, d: float, w: float, h: float) -> float:
        """A lens ring's corners, `d` in from the edge: round all the way in (the edge's radius less `d` turns
        sharp once deeper than it, and the rings' corners then crease the picture diagonally)."""
        return max(0.0, min(max(self.radius - d, self.radius * 0.6 + d * 0.9), (w - 2 * d) / 2, (h - 2 * d) / 2))

    def _rounded(self, x: float, y: float, w: float, h: float, r: float) -> Gsk.RoundedRect:
        rr = Gsk.RoundedRect()
        rr.init_from_rect(Graphene.Rect().init(x, y, max(w, 1), max(h, 1)), max(r, 0))
        return rr

    def _warped_children(self, snap, w: float, h: float) -> None:
        """What's inside the glass bent by it too: true in the middle, squeezed and curving away towards the
        rounded edge (text scrolling to the bottom bends over it, the tabs follow the rim)."""
        native = self.get_native()
        s = Gtk.Snapshot()
        surf = native.get_surface() if native else None
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
        s.scale(k, k)
        Gtk.Box.do_snapshot(self, s)
        node = s.to_node()
        if node is None or not native:
            return
        tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w * k, h * k))
        box = Graphene.Rect().init(0, 0, w, h)
        lens = min(h / 2, w / 2)
        most = lens * self.reach * 0.35
        rx, ry = 1 / (1 + 2 * most / w), 1 / (1 + 2 * most / h)
        steps = max(1, math.ceil(most / 1.5))  # coarser than the background's: text hides the steps
        cx, cy = w / 2, h / 2
        def ring(d: float):
            return self._rounded(d, d, w - 2 * d, h - 2 * d, self._ring_r(d, w, h))

        # each ring of the content drawn only in its own band (see-through content drawn over itself would
        # stack its shadows up)
        prev = 0.0
        for i in range(0, steps + 1):
            bend = 1 - i / steps  # 1 at the edge, 0 inside
            d = lens * 0.5 * (1 - bend ** (1 / self.curve))  # the bend: near the edge only
            nxt = lens * 0.5 * (1 - max(0.0, 1 - (i + 1) / steps) ** (1 / self.curve)) if i < steps else None
            sx, sy = 1 + (rx - 1) * bend, 1 + (ry - 1) * bend
            pb = Gsk.PathBuilder.new()
            pb.add_rounded_rect(ring(d))
            if nxt is not None:
                if nxt - d < 0.05:
                    continue
                pb.add_rounded_rect(ring(nxt))
            snap.push_fill(pb.to_path(), Gsk.FillRule.EVEN_ODD)
            snap.save()
            snap.translate(Graphene.Point().init(cx, cy))
            snap.scale(sx, sy)
            snap.translate(Graphene.Point().init(-cx, -cy))
            snap.append_texture(tex, box)
            snap.restore()
            snap.pop()

    def do_snapshot(self, snap) -> None:
        if self.lift:
            w, h = self.get_width(), self.get_height()
            snap.save()
            snap.translate(Graphene.Point().init(w / 2, h / 2))
            snap.scale(1 + self.lift, 1 + self.lift)
            snap.translate(Graphene.Point().init(-w / 2, -h / 2))
        self.draw_glass(snap)
        if self.warp_content and getattr(self, "reveal", 1.0) >= 1.0 and self.get_width() > 0 and self.get_height() > 0:
            # (bent once it's open: while it flows, the content as it is, light)
            self._warped_children(snap, self.get_width(), self.get_height())
        else:
            Gtk.Box.do_snapshot(self, snap)
        if self.lift:
            snap.restore()

    def draw_mirrored(self, snap, tile) -> None:
        """A button seen through the glass upside down, as in a mirror: `tile` (its glass) where it is, the
        button on it flipped top to bottom. `snap` at the tile's corner."""
        tile.draw_glass(snap, shadow=False)
        child = tile.get_first_child()
        while child is not None:
            ok, cp = child.compute_point(tile, Graphene.Point().init(0, 0))
            cw, ch = child.get_width(), child.get_height()
            # (not a popup of its own: the quality menu hangs from its pill)
            if ok and child.get_visible() and cw > 0 and ch > 0 and not isinstance(child, Gtk.Native):
                paint = self._mirror_paints.get(child)
                if paint is None:
                    paint = self._mirror_paints[child] = Gtk.WidgetPaintable.new(child)
                snap.save()
                snap.translate(Graphene.Point().init(cp.x, cp.y + ch))
                snap.scale(1, -1)
                paint.snapshot(snap, cw, ch)
                snap.restore()
            child = child.get_next_sibling()

    def draw_plain(self, snap) -> None:
        """What lies under the card, as if it weren't there (the cover, the bars, the panel's shading), out to
        where its shadow reaches: no sharp edge or shadow of it left."""
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        rim = min(self.radius * self.RIM, h / 2, w / 2)
        m = rim * 1.3 + 4 if self.depth else 3  # the shadow's reach (and the edge's antialiasing)
        snap.push_rounded_clip(self._rounded(-m, -m, w + 2 * m, h + 2 * m, self.radius + m))
        self._behind(snap, w, h, 1.0, 1.0)
        ok, sb = self.scrim.compute_bounds(self) if self.scrim else (False, None)
        if ok:
            k = self.shade
            snap.append_linear_gradient(Graphene.Rect().init(0, 0, w, h),
                                        Graphene.Point().init(0, sb.get_y() + sb.get_height()),
                                        Graphene.Point().init(0, sb.get_y()),
                                        [_stop(0.0, 0, 0, 0, 0.92 * k), _stop(0.55, 0, 0, 0, 0.75 * k),
                                         _stop(1.0, 0, 0, 0, 0.0)])
        snap.pop()

    def draw_glass(self, snap, shadow: bool = True) -> None:
        """The glass alone (what's inside it drawn by the caller)."""
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        box = Graphene.Rect().init(0, 0, w, h)
        rr = self._rounded(0, 0, w, h, self.radius)
        rim = min(self.radius * self.wall, h / 2, w / 2)
        if self.drop and shadow and EFFECTS["shadows"]:  # the letters' shadow (down and left): a tight dark one and a soft one, 3D
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.9), -1, 2, 0, 1)
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.65), -2, 3, 0, 5)
        if self.depth and shadow:  # it floats above the cover: a wide soft shadow and a tight one where it nearly touches
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.55), 0, rim * 0.3, 0, rim * 0.9)
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.35), 0, 2, 0, 4)
        snap.push_rounded_clip(rr)
        gpu = self._gpu_lens(w, h) if EFFECTS["gpu"] and GPU.start() else None
        tex = gpu or (self._lens_texture(w, h) if self.cache_lens else None)
        if tex:
            snap.append_texture(tex, box)
        elif self.cache_lens:  # its first texture still being built: plainly magnified meanwhile
            self._behind(snap, w, h, self.mag, self.mag)
        else:
            self._lens(snap, w, h)
        self._snow_through(snap, w, h)
        self._finish(snap, w, h, box, rr, rim)

    def _snow_origin(self):
        """Where the window starts in its coordinates, when the falling motifs are seen through it."""
        return False, None

    SNOW_FAINT = 1.0  # the falling motifs behind glass as bright as beside it (the glass darker instead)

    def _snow_through(self, snap, w: float, h: float) -> None:
        """The falling motifs seen through it: magnified like its middle, fading out towards the edges, where
        those just beyond are mirrored in the rim; drawn every frame over the lens."""
        snow = getattr(getattr(self, "win", None), "snow", None)
        if snow is None or not snow.area:
            return
        ok, o = self._snow_origin()
        if not ok:
            return
        m = min(getattr(self, "_edge_reach", 0.0) * 0.8, w / 3, h / 3)
        squeeze = 0.6  # the rim shows what's beyond its edge squeezed: 1/0.6 as far out as it's wide
        e = m / squeeze + 2
        gx, gy = -o.x, -o.y  # where it lies on the window
        # all over it, fading out only towards the edges (into the rim's mirror), not cut off at the rim: each
        # fainter by how near its edge it's seen (magnified about the middle); no masks (each one a picture the
        # renderer draws apart every frame: a third of the work at 60 Hz)
        f, mag, faint = max(1.0, m * 0.7), self.mag, self.SNOW_FAINT

        def middle(fx, fy):
            px, py = w / 2 + (fx - gx - w / 2) * mag, h / 2 + (fy - gy - h / 2) * mag
            return faint * max(0.0, min(1.0, min(px, w - px, py, h - py) / f))
        snap.save()
        snap.translate(Graphene.Point().init(w / 2, h / 2))
        snap.scale(mag, mag)
        snap.translate(Graphene.Point().init(-w / 2, -h / 2))
        snow.draw(snap, o.x, o.y, (gx, gy, w, h), middle)
        snap.restore()
        if m < 2 or not EFFECTS.get("mirror", True):
            return
        # In the rim, those just beyond each edge mirrored (like the date and time there): upside down, the
        # nearest right at the edge, fading out inwards where the middle's magnified view takes over.
        reach = m / squeeze

        def beyond(d):
            return 0.95 * faint * max(0.0, 1.0 - max(0.0, d) / reach)
        for band, flip, view, fade in (  # (view: those beyond that edge, on the window)
                ((0, 0, w, m), (0, 0, 1, -squeeze), (gx - e, gy - e, w + 2 * e, e + 2),
                 lambda fx, fy: beyond(gy - fy)),  # above the top edge
                ((0, h - m, w, m), (0, h * (1 + squeeze), 1, -squeeze), (gx - e, gy + h - 2, w + 2 * e, e + 2),
                 lambda fx, fy: beyond(fy - gy - h)),  # below the bottom edge
                ((0, 0, m, h), (0, 0, -squeeze, 1), (gx - e, gy - e, e + 2, h + 2 * e),
                 lambda fx, fy: beyond(gx - fx)),  # left of the left edge
                ((w - m, 0, m, h), (w * (1 + squeeze), 0, -squeeze, 1), (gx + w - 2, gy - e, e + 2, h + 2 * e),
                 lambda fx, fy: beyond(fx - gx - w))):  # right of the right edge
            snap.push_clip(Graphene.Rect().init(*band))
            snap.save()
            snap.translate(Graphene.Point().init(flip[0], flip[1]))
            snap.scale(flip[2], flip[3])
            snow.draw(snap, o.x, o.y, view, fade)
            snap.restore()
            snap.pop()

    def _lens_key(self, w: float, h: float):
        """What the lens shows depends on: which pictures, where they lie, the glass's size and shape."""
        key = [w, h, self.radius, self.mag, self.sharp_bars, self.edge_mirror,
               TEXT_MAP["version"] if self.edge_mirror else 0, getattr(self, "_tex_version", None)]
        for view, _ in self.behind:
            for wdg in (view, view.get_parent()):
                ok, p = self._where(wdg)
                key += [round(p.x, 1), round(p.y, 1)] if ok else [None]
            old, t = self._morph(view)
            key += [id(view.pic), view.get_width(), view.get_height(), id(old), t]
        for tile in self.mirrors:
            ok, p = self._where(tile)
            key += [round(p.x, 1), round(p.y, 1), tile.get_mapped()] if ok else [None]
        return tuple(key)

    BUILD_MS = 8.0  # time a frame gives to building lens textures, all glass together (its own drawing on top)
    _spent = [None, 0.0]  # [frame, s]: the building time this frame has had so far

    def _lens_texture(self, w: float, h: float):
        """The lens layers rendered into a texture (at the screen's scale, sharp) and drawn from it until
        something behind it changes: one texture a frame instead of a hundred layers (smooth animations).
        A big one is built over several frames, BUILD_MS each: the big panel's 2800 rings took 2.1 s in one go,
        a frozen window, and past ~2 s the kernel resets the GPU (the app's drawing gone with it). Meanwhile
        the last one shows (None at first: the caller shows it plainly magnified)."""
        key = self._lens_key(w, h)
        if self._lens_cache[0] == key:
            return self._lens_cache[1]
        native = self.get_native()
        if not native:
            return None
        build = self._build
        if build is None or build["key"] != key:
            surf = native.get_surface()
            k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
            plan = self._lens_plan(w, h)
            build = self._build = {"key": key, "k": k, "plan": plan, "next": 0, "per": 64, "tex": None,
                                   "flat": "later" if len(plan["rings"]) > 9 else None}
        renderer, k = native.get_renderer(), build["k"]
        area = Graphene.Rect().init(0, 0, w * k, h * k)
        rings = build["plan"]["rings"]
        clock = self.get_frame_clock()
        frame = clock.get_frame_counter() if clock else None
        if GlassCard._spent[0] != frame:
            GlassCard._spent[:] = [frame, 0.0]
        start = time.perf_counter()
        end = start + max(0.0, self.BUILD_MS / 1000 - GlassCard._spent[1])
        if build["flat"] == "later":  # what's behind flattened: a step of its own
            if time.perf_counter() < end:
                build["flat"] = self._flat(w, h, build["plan"]["rx"], build["plan"]["ry"])
                end = start  # its frame is used up
        while build["flat"] != "later" and build["next"] < len(rings) and time.perf_counter() < end:
            first, last = build["next"], min(len(rings), build["next"] + build["per"])
            t = time.perf_counter()
            s = Gtk.Snapshot()
            if build["tex"]:
                s.append_texture(build["tex"], area)  # the rings so far: the next ones go on top
            s.scale(k, k)
            self._rings(s, w, h, rings, first, last, build["flat"])
            node = s.to_node()
            if node is not None:
                build["tex"] = renderer.render_texture(node, area)
            build["next"] = last
            took = time.perf_counter() - t  # render_texture waits for the GPU: its real time
            # each piece has a cost of its own (the rings so far copied in and out): grow or shrink towards
            # BUILD_MS rather than scaling by it (that shrank the pieces to a ring each, never done)
            build["per"] = min(4096, build["per"] * 3 // 2 + 1) if took * 1000 < self.BUILD_MS \
                else max(1, build["per"] * 2 // 3)
        GlassCard._spent[1] += time.perf_counter() - start
        if build["next"] >= len(rings):
            self._build = None
            self._lens_cache = (key, _opaque(build["tex"]) if build["tex"] else None)  # drawn every frame: opaque
            self._lens_what = self._what_behind()
            return self._lens_cache[1]
        if self._build_tick is None:  # frames keep coming until it's built
            self._build_tick = self.add_tick_callback(self._build_step)
        if self.edge_mirror and getattr(self, "_lens_what", None) != self._what_behind():
            return None  # the last lens shows an earlier picture (and its mirror): the current one plainly meanwhile
        return self._lens_cache[1]

    def _gpu_lens(self, w: float, h: float):
        """The lens worked out by the graphics card (GPU): what's behind as one picture (uploaded when it
        changes), bent in one pass; drawn again only when the glass, its magnification or the picture change."""
        try:
            native = self.get_native()
            if not native:
                return None
            surf = native.get_surface()
            k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
            lens = min(h / 2, w / 2, self.lens_max) if not self.band else min(self.band, h / 2, w / 2)
            most = lens * (self.reach if not self.band else 0.9)
            rx, ry = 1 / (1 + 2 * most / w), 1 / (1 + 2 * most / h)
            self._edge_reach, self._edge_reach_x = most, w / 2 * (1 / rx - 1)
            self._flat_mirror = self.edge_mirror
            try:
                flat = self._flat(w, h, rx, ry)
            finally:
                self._flat_mirror = False
            if not flat:
                return None
            ftex, box = flat
            g = self._gpu
            if g.get("flat") is not ftex:
                t0 = time.perf_counter()
                old = g.get("src", 0)
                if old and g.get("size") != (ftex.get_width(), ftex.get_height()):
                    GPU.forget(old)
                    old = 0
                g.update(src=GPU.upload(ftex, old), flat=ftex, size=(ftex.get_width(), ftex.get_height()), out=None)
                took = time.perf_counter() - t0  # (the live window's pictures: how often they can be afforded)
                self._live_cost = took if not getattr(self, "_live_cost", 0) else 0.7 * self._live_cost + 0.3 * took
            key = (w, h, k, self.radius, round(self.mag, 4), rx, ry, self.curve, lens, box.get_x(), box.get_y())
            if g.get("key") != key or g.get("out") is None:
                u = {"w": w, "h": h, "box": (box.get_x(), box.get_y(), box.get_width(), box.get_height()),
                     "radius": self.radius, "mag": self.mag, "rx": rx, "ry": ry, "depth": lens * 0.75,
                     "curve": self.curve}
                g["out"] = GPU.lens(g["src"], max(1, round(w * k)), max(1, round(h * k)), u)
                g["key"] = key
            return g["out"]
        except Exception as e:  # the layers from now on
            GPU.ok, GPU.why = False, f"selhalo ({e})"
            print("Rádio Beat: GPU sklo vypnuto:", e, file=sys.stderr)
            return None

    LIVE_FPS = 15  # the window seen through it, moving (the falling motifs): pictured anew this often at most

    own_redraw = 0  # > 0: glass redrawing itself (a menu hangs from the clock or the pill: they'd seem changed)

    def _live(self) -> None:
        """What's on the window behind it seems changed: looked at (is anything it shows really different?),
        and if so pictured anew, at most LIVE_FPS times a second (each time a picture taken off the graphics
        card and put back for the lens: ~12 ms, a frame of the falling motifs lost), once a second without the
        GPU (the lens built bit by bit). The clock's colon blinking every second, or the glass redrawn for the
        falling motifs (the clock or the pill a menu hangs from seeming changed with it), don't count: they
        made the motifs stutter every second or two."""
        if getattr(self, "_late", None) or GlassCard.own_redraw:
            return
        gap = 1.0 / self.LIVE_FPS if EFFECTS["gpu"] and GPU.ok is not False else 1.0
        gap = max(gap, 4 * getattr(self, "_live_cost", 0.0), 0.15)  # (a quarter of a CPU core at most)
        wait = gap - (time.monotonic() - getattr(self, "_live_at", 0.0))
        if wait <= 0:
            self._live_late()
        else:
            self._late = GLib.timeout_add(max(1, int(wait * 1000)), self._live_late)

    def _live_now(self) -> None:
        self._live_at = time.monotonic()
        self._live_sig = self._layers_sig()
        self._tex_version += 1
        GlassCard.own_redraw += 1
        try:
            self.queue_draw()
        finally:
            GlassCard.own_redraw -= 1

    def _live_late(self) -> bool:
        self._late = None
        self._live_at = time.monotonic()  # (looked at no more often than pictured)
        if self._layers_sig() != getattr(self, "_live_sig", None):
            self._live_now()
        return False

    def _live_layers(self) -> list:
        """The widgets on the window it pictures live."""
        return []

    def _layers_sig(self) -> tuple:
        win = getattr(self, "win", None)
        out = [win.get_width(), win.get_height()] if win is not None else []
        for wdg in self._live_layers():
            widget_sig(wdg, out)
        return tuple(out)

    def _what_behind(self) -> tuple:
        """Which pictures (and which lettering map) a lens shows."""
        return tuple((id(v.pic), id(self._morph(v)[0]), self._morph(v)[1]) for v, _ in self.behind) + \
            (TEXT_MAP["version"],)

    def fresh(self) -> None:
        """Forget the lens and its mirrored strips: drawn anew from what's behind now (a menu opening)."""
        self._lens_cache = (None, None)
        self._flat_cache = (None, None)
        self._build = None
        self._mirror_tex = None
        self._lens_what = None
        self._gpu.update(flat=None, out=None)
        self.queue_draw()

    def _build_step(self, *_) -> bool:
        if self._build is None:
            self._build_tick = None
            return GLib.SOURCE_REMOVE
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _lens_plan(self, w: float, h: float) -> dict:
        # A fish-eye lens, sharp: the middle magnified most (mag), less and less towards the edges, which are
        # bent the other way (they show what lies beyond, squeezed in). No blending (that ghosts and looks
        # blurred): hard-edged layers from the edge in, each shifted under 1 px from the one outside it.
        # the bend over the whole glass (fish-eye), or for a big one in a band along its rounded edge
        lens = min(h / 2, w / 2, self.lens_max) if not self.band else min(self.band, h / 2, w / 2)
        most = lens * (self.reach if not self.band else 0.9)
        rx, ry = 1 / (1 + 2 * most / w), 1 / (1 + 2 * most / h)  # the edge's zoom
        self._edge_reach = most  # the rim shows this far beyond the edge
        self._edge_reach_x = w / 2 * (1 / rx - 1)
        # each ring shifted under a pixel from the next (half a pixel on big glass: its seams would show)
        # rings half a pixel apart (a fifth on big glass: its rim bends fastest, coarser steps show as lines there)
        step = 2.5 if self.pulse_tick else (0.2 if w * h > 120000 else 0.5)
        steps = max(1, math.ceil((most + abs(self.mag - 1) * max(w, h) / 2) / step))
        # The bend runs over the outer three quarters only: the middle is one layer, magnified evenly (rings
        # reaching the middle become thin strips, their seams lines across the glass)
        depth = lens * 0.75
        rings = []
        for k in range(0, steps + 1):
            bend = 1 - k / steps  # 1 at the edge, 0 inside
            d = depth * (1 - bend ** (1 / self.curve))  # where the curve falls to `bend`
            rings.append((d, self.mag + (rx - self.mag) * bend, self.mag + (ry - self.mag) * bend))
        rings = self._thin_rings(rings, w, h)
        return {"rx": rx, "ry": ry, "rings": rings}

    def _thin_rings(self, rings: list, w: float, h: float) -> list:
        """Only rings that move the picture at least half a screen pixel from the last one kept: in the
        flat middle thousands of near-identical hairline rings, each blended in at its antialiased edge, left
        concentric stripes (8-bit rounding piling up) over the picture; fewer is also faster."""
        native = self.get_native()
        surf = native.get_surface() if native else None
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
        least = 0.5 / max(1.0, k)
        kept = rings[:1]
        for r in rings[1:-1]:
            d, sx, sy = r
            _, px, py = kept[-1]
            if max((w / 2 - d) * abs(1 / px - 1 / sx), (h / 2 - d) * abs(1 / py - 1 / sy)) >= least:
                kept.append(r)
        if len(rings) > 1:
            kept.append(rings[-1])
        return kept

    def _lens(self, snap, w: float, h: float) -> None:
        """The whole lens drawn at once (live glass: the gear menu's)."""
        rings = self._lens_plan(w, h)["rings"]
        self._rings(snap, w, h, rings, 0, len(rings), None)

    def _rings(self, snap, w: float, h: float, rings: list, first: int, last: int, flat) -> None:
        """Rings first..last-1, each clipped to its rounded rect. With `flat` (what's behind as one texture),
        each draws that one picture (a ring of all the layers behind, the side bars' blur nine times over,
        took 0.8 ms; one picture 0.08). Drawing only each ring's band was slower: more pieces, not less paint."""
        for i in range(first, last):
            d, sx, sy = rings[i]
            if i:
                snap.push_rounded_clip(self._rounded(d, d, w - 2 * d, h - 2 * d, self._ring_r(d, w, h)))
            # Echoes: the rim's rings (squeezing in what's beyond) drawn layer by layer, as the user likes them;
            # inside, one flattened picture: layer by layer each ring's soft edge let the shade under the cover
            # through, thin dark rings all over the middle.
            if flat and not (self.echoes and min(sx, sy) < 1):
                snap.save()
                snap.translate(Graphene.Point().init(w / 2, h / 2))
                snap.scale(sx, sy)
                snap.translate(Graphene.Point().init(-w / 2, -h / 2))
                snap.append_texture(*flat)
                snap.restore()
            else:
                self._behind(snap, w, h, sx, sy)
            if i:
                snap.pop()

    def _flat(self, w: float, h: float, rx: float, ry: float):
        """(texture, where) of what's behind the glass, as far out as its edge squeezes in (rx, ry), at the
        screen's scale times the middle's magnification: as sharp as the layers drawn one by one."""
        lk = self._lens_key(w, h)
        key = (lk[:3] + lk[4:], rx, ry, bool(self.pulse_tick), self._flat_mirror,
               getattr(self, "_tex_version", None))  # not the magnification: it breathes
        if self._flat_cache[0] == key:
            return self._flat_cache[1]
        native = self.get_native()
        if not native:
            return None
        surf = native.get_surface()
        most = self._base_mag + self.PULSE if self.pulse_tick else self.mag
        k = (surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()) * max(1.0, most)
        ew, eh = w / min(1.0, rx) + 4, h / min(1.0, ry) + 4
        box = Graphene.Rect().init((w - ew) / 2, (h - eh) / 2, ew, eh)
        s = Gtk.Snapshot()
        s.scale(k, k)
        s.translate(Graphene.Point().init(-box.get_x(), -box.get_y()))
        self._behind(s, w, h, 1.0, 1.0)
        node = s.to_node()
        if node is None:
            return None
        # kept with its alpha: opaque, GTK skips what each ring seems to cover, and on the DeskMini's 780M the
        # round buttons then showed a crosshair through the middle (hairline gaps where a circle's quarters meet)
        self._flat_cache = (key, (native.get_renderer().render_texture(
            node, Graphene.Rect().init(0, 0, ew * k, eh * k)), box))
        return self._flat_cache[1]

    def _finish(self, snap, w: float, h: float, box, rr, rim: float) -> None:
        """Over the lens: the flash, the shading, the light on the edges (cheap, drawn each time)."""
        if self.flash > 0:
            snap.append_color(_rgba(255, 255, 255, 0.28 * self.flash), box)
        ok, sb = self.scrim.compute_bounds(self) if self.scrim else (False, None)
        if ok:
            # See-through: the same shading the window has here (the text panel's gradient), nothing more.
            k = self.shade
            top, bottom = sb.get_y(), sb.get_y() + sb.get_height()
            snap.append_linear_gradient(box, Graphene.Point().init(0, bottom), Graphene.Point().init(0, top),
                                        [_stop(0.0, 0, 0, 0, 0.92 * k), _stop(0.55, 0, 0, 0, 0.75 * k),
                                         _stop(1.0, 0, 0, 0, 0.0)])
        else:
            snap.append_color(_rgba(18, 18, 22, self.tint * self.shade), box)
        if self.color:
            snap.append_color(_rgba(*self.color), box)
        # See-through: no milky sheen over it (that made it a gray slab on a dark cover), only a faint one along
        # the top; the light is in the edges.
        hl = Graphene.Rect().init(0, 0, w, rim)
        snap.append_linear_gradient(hl, Graphene.Point().init(0, 0), Graphene.Point().init(0, rim),
                                    [_stop(0.0, 255, 255, 255, 0.07), _stop(1.0, 255, 255, 255, 0.0)])
        # Thick glass, rounded on every side: its curved side wall darker all round, light catching the top edge
        # (a bright line, a softer glow under it), light coming out through the bottom edge (a fainter line),
        # the bottom-right of the wall in shade.
        if self.shine:  # a lens's curve: light caught near the top left, the rim in shade
            snap.append_radial_gradient(box, Graphene.Point().init(w * 0.3, h * 0.16), w * 0.55, h * 0.3, 0, 1,
                                        [_stop(0.0, 255, 255, 255, 0.16), _stop(0.5, 255, 255, 255, 0.05),
                                         _stop(1.0, 255, 255, 255, 0.0)])
            snap.append_inset_shadow(rr, _rgba(0, 0, 0, 0.28), 0, 0, 6, min(w, h) * 0.12)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.55), 0, 2.5, 0, 2.5)  # the thick top edge, lit
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.22), 0, -2, 0, 3)  # light out at the bottom edge
        if not self.frame:
            snap.pop()
            return
        if self.concave:
            # A dish: the top inside edge in shade, light gathering along the bottom, a bright lip all round.
            snap.append_inset_shadow(rr, _rgba(0, 0, 0, 0.5), 0, rim * 0.18, 0, rim * 0.35)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.32), 0, -rim * 0.12, 0, rim * 0.3)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.55), 0, 0, 1.0, 1.0)
        else:
            snap.append_inset_shadow(rr, _rgba(0, 0, 0, 0.32), 0, 0, 0, rim * 0.7)
            snap.append_inset_shadow(rr, _rgba(0, 0, 0, 0.35), -rim * 0.12, -rim * 0.2, 0, rim * 0.5)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.85), 0, 1.5, 0, 1.0)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.14), rim * 0.06, rim * 0.1, 0, rim * 0.25)
            snap.append_inset_shadow(rr, _rgba(255, 255, 255, 0.35), 0, -1.5, 0, 1.5)
        snap.pop()
        edge = [_rgba(255, 255, 255, 0.30), _rgba(255, 255, 255, 0.12),
                _rgba(255, 255, 255, 0.08), _rgba(255, 255, 255, 0.12)]  # top, right, bottom, left
        snap.append_border(rr, [1.0, 1.0, 1.0, 1.0], edge)


class PillGlass(GlassCard):
    """Glass the shape of a pill (round ends, half its height): the 'i' panel's tabs, like the round buttons."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.drop = True  # the letters' shadow, like the buttons

    def do_size_allocate(self, width, height, baseline):
        self.radius = height / 2
        Gtk.Box.do_size_allocate(self, width, height, baseline)


class MenuGlass(GlassCard):
    """The gear menu's glass, over the whole window: the cover, the text and the show card's glass all show
    through it, magnified. The menu is a popup surface of its own, so where the window lies behind it comes
    from where the popup was put (its position on the window's surface); the window is drawn once per redraw
    into a texture (at the screen's scale, sharp), then bent like the card's."""

    def __init__(self, win, behind: list, **kw):
        super().__init__(behind, **kw)
        self.win = win
        self.paint = None  # the window's content, live
        self._tex = None
        self.cache_lens = False  # it shows the live window, a new texture each time anyway
        self.tint = 0.0  # clear like the rest (its text readable by the dark halo round the letters)
        self.sharp_bars = True
        self.depth = False  # the popup's own shadow does it
        self._tex_version = 0  # the window's content changed this many times (a new picture of it then)
        self._tex_key = None
        self.connect("unmap", lambda *_: setattr(self, "_tex", None))  # (closed: its picture let go)
        # the falling motifs seen through it, moving: redrawn with them while it's open
        self.connect("map", lambda *_: getattr(win, "snow", None) and win.snow.viewers.add(self))
        self.connect("unmap", lambda *_: getattr(win, "snow", None) and win.snow.viewers.discard(self))

    def _snow_origin(self):
        content = self.win.get_content()
        return self._where(content) if content else (False, None)

    def _where(self, widget):
        pop = self.get_native()
        surf = pop.get_surface() if pop else None
        ok_w, wp = widget.compute_point(self.win, Graphene.Point().init(0, 0))
        ok_c, cp = self.compute_point(pop, Graphene.Point().init(0, 0)) if pop else (False, None)
        if not (surf and ok_w and ok_c and hasattr(surf, "get_position_x")):
            return False, None
        ptx, pty = pop.get_surface_transform()
        wtx, wty = self.win.get_surface_transform()
        # The popup's place on the window's surface: added up through popups opened from popups.
        sx = sy = 0
        while isinstance(surf, Gdk.Popup):
            sx, sy = sx + surf.get_position_x(), sy + surf.get_position_y()
            surf = surf.get_parent()
        x = sx + ptx + cp.x - wtx  # the glass's corner, in the window
        y = sy + pty + cp.y - wty
        return True, Graphene.Point().init(wp.x - x, wp.y - y)

    def _window_texture(self):
        content, native = self.win.get_content(), self.get_native()
        if not content or not native:
            return None
        if self.paint is None:
            # each layer of the window but the falling motifs' (drawn into the glass every frame instead)
            self.paint = []
            c = content.get_first_child()
            while c is not None:
                if c is not getattr(self.win, "snow_view", None):
                    paint = Gtk.WidgetPaintable.new(c)
                    # the window changed (a new cover, a new song): the menu shows it
                    paint.connect("invalidate-contents", self._window_changed)
                    self.paint.append((c, paint))
                c = c.get_next_sibling()
        cw, ch = content.get_width(), content.get_height()
        if cw <= 0 or ch <= 0:
            return None
        surf = self.win.get_surface()
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.win.get_scale_factor()
        s = Gtk.Snapshot()
        s.scale(k, k)
        draw_layers(s, self.paint, content, self.mirrors)  # (the mirrored buttons: only mirrored)
        for tile in self.mirrors:  # seen through it upside down
            ok, tp = tile.compute_point(content, Graphene.Point().init(0, 0))
            if ok and tile.get_mapped():
                s.save()
                s.translate(tp)
                self.draw_mirrored(s, tile)
                s.restore()
        self._blur_card_text(s, content)
        node = s.to_node()
        if node is None:
            return None
        # only around the glass (as far as its rim reaches, and then some), not the whole window: a fifth of
        # the pixels to render, take off the graphics card and put back for the lens (opening it: a frame lost)
        x0, y0, x1, y1 = 0.0, 0.0, cw, ch
        ok, p = self._where(content)
        w, h = self.get_width(), self.get_height()
        if ok and w > 0 and h > 0:
            mx, my = w * 0.6 + 48, h * 0.6 + 48
            x0, y0 = max(0.0, math.floor(-p.x - mx)), max(0.0, math.floor(-p.y - my))
            x1, y1 = min(cw, math.ceil(-p.x + w + mx)), min(ch, math.ceil(-p.y + h + my))
            if x1 - x0 < 8 or y1 - y0 < 8:
                x0, y0, x1, y1 = 0.0, 0.0, cw, ch
        self._tex_rect = Graphene.Rect().init(x0, y0, x1 - x0, y1 - y0)
        return native.get_renderer().render_texture(node, Graphene.Rect().init(x0 * k, y0 * k, (x1 - x0) * k, (y1 - y0) * k))

    def _blur_card_text(self, s, content) -> None:
        """Under the menu the show card is out of focus, glass, edges and text: what lies under it drawn
        over the sharp one, then the card again, blurred."""
        card = self.win.show_card
        if not card.get_mapped():
            return
        ok, cp = card.compute_point(content, Graphene.Point().init(0, 0))
        if not ok:
            return
        s.save()
        s.translate(cp)
        card.draw_plain(s)
        s.push_blur(5.0)
        card.draw_glass(s)
        for child in (self.win.show_pic, self.win.show_card_text):
            ok, p = child.compute_point(card, Graphene.Point().init(0, 0))
            if ok:
                s.save()
                s.translate(p)
                Gtk.WidgetPaintable.new(child).snapshot(s, child.get_width(), child.get_height())
                s.restore()
        s.pop()
        s.restore()

    def _behind(self, snap, w: float, h: float, sx: float, sy: float) -> None:
        content = self.win.get_content()
        ok, p = self._where(content) if content else (False, None)
        if not (self._tex and ok):
            return
        snap.save()
        snap.translate(Graphene.Point().init(w / 2, h / 2))
        snap.scale(sx, sy)
        snap.translate(Graphene.Point().init(-w / 2, -h / 2))
        win = Graphene.Rect().init(p.x, p.y, content.get_width(), content.get_height())
        self._window(snap, win)
        if self.edge_mirror and self._flat_mirror:  # (GPU only: the rings would draw them thousands of times)
            self._mirror_sides(snap, w, h, win)
        snap.restore()

    def _window(self, snap, win) -> None:
        """The window's picture; with edge mirrors also mirrored past its edges (the rim near the screen's edge
        shows no emptiness)."""
        r = getattr(self, "_tex_rect", None) or Graphene.Rect().init(0, 0, win.get_width(), win.get_height())
        if not (self.edge_mirror and self._flat_mirror):
            snap.append_texture(self._tex, Graphene.Rect().init(win.get_x() + r.get_x(), win.get_y() + r.get_y(),
                                                                r.get_width(), r.get_height()))
            return
        x, y, ww, wh = win.get_x(), win.get_y(), win.get_width(), win.get_height()
        for mx in (-1, 0, 1):
            for my in (-1, 0, 1):
                snap.save()
                snap.translate(Graphene.Point().init(x + (2 * ww if mx == 1 else 0), y + (2 * wh if my == 1 else 0)))
                snap.scale(-1 if mx else 1, -1 if my else 1)
                snap.append_texture(self._tex, r)
                snap.restore()

    BOTTOM_STRETCH = 2.2  # the bottom edge's mirror: what's below drawn this much taller (the slider is thin)
    BOTTOM_GAIN = 1.35  # and this much brighter

    def _mirror_sides(self, snap, w: float, h: float, win) -> None:
        """Beyond each of its four edges, as far as the rim reaches: what lies there flipped within that strip,
        so the rim shows what's closest to the edge right at it, upside down (left and right: mirrored side to
        side), further things further in: a mirror at every edge (the song info, the date and time, the
        slider and the line under it). Faded out towards the edge, where the rim turns see-through."""
        bx, by = self._edge_reach_x + 2, self._edge_reach + 2
        if bx <= 2 or by <= 2:
            return
        g = self.BOTTOM_STRETCH  # below: the thin slider (and the line under it) stretched, to be seen
        sides = (  # strip, flip (sx, sy) and offset, gradient from its far side to its near one, brighter
            (Graphene.Rect().init(-bx, -by, w + 2 * bx, by), (1, -1, 0, -by), (0, -by), (0, 0), 1.0),
            (Graphene.Rect().init(-bx, h, w + 2 * bx, by), (1, -g, 0, h + by + g * h), (0, h + by), (0, h),
             self.BOTTOM_GAIN),
            (Graphene.Rect().init(-bx, 0, bx, h), (-1, 1, -bx, 0), (-bx, 0), (0, 0), 1.0),
            (Graphene.Rect().init(w, 0, bx, h), (-1, 1, 2 * w + bx, 0), (w + bx, 0), (w, 0), 1.0),
        )
        for rect, (fx, fy, ox, oy), far, near, gain in sides:
            snap.push_clip(rect)
            # under it, the glass's inside mirrored at this edge (continuous with it there): what lies beyond the
            # edge is seen only mirrored, never also as it is (an unmirrored ghost through the fade)
            snap.save()
            if fx < 0:  # left or right: x -> 2*edge - x
                snap.translate(Graphene.Point().init(0.0 if rect.get_x() < 0 else 2 * w, 0))
                snap.scale(-1, 1)
            else:  # above or below: y -> 2*edge - y
                snap.translate(Graphene.Point().init(0, 0.0 if rect.get_y() < 0 else 2 * h))
                snap.scale(1, -1)
            self._window(snap, win)
            snap.restore()
            if gain != 1.0:
                snap.push_color_matrix(Graphene.Matrix().init_scale(gain, gain, gain), Graphene.Vec4().init(0, 0, 0, 0))
            snap.push_mask(Gsk.MaskMode.ALPHA)
            snap.append_linear_gradient(rect, Graphene.Point().init(*far), Graphene.Point().init(*near),
                                        [_stop(0.0, 255, 255, 255, 1.0), _stop(0.45, 255, 255, 255, 1.0),
                                         _stop(1.0, 255, 255, 255, 0.0)])
            snap.pop()
            snap.save()
            snap.translate(Graphene.Point().init(ox, oy))  # flipped within the strip: x -> ox - x, y -> oy - y
            snap.scale(fx, fy)
            self._window(snap, win)
            snap.restore()
            snap.pop()
            if gain != 1.0:
                snap.pop()
            snap.pop()

    def _live_layers(self) -> list:
        return [c for c, _ in self.paint] if self.paint else []

    def _window_changed(self, *_) -> None:
        if GlassCard.own_redraw:
            return
        if self.get_mapped():
            self._live()
        else:
            self._tex_version += 1  # (pictured anew when it opens)

    def do_snapshot(self, snap) -> None:
        self.shade = 1.0  # always readable, whatever Background shade is
        content = self.win.get_content()
        ok, p = self._where(content) if content else (False, None)
        key = (self._tex_version, content.get_width() if content else 0, content.get_height() if content else 0,
               (round(p.x), round(p.y), self.get_width(), self.get_height()) if ok else None)
        if self._tex is None or self._tex_key != key:  # the window as it is now (only when it changed)
            self._tex, self._tex_key = self._window_texture(), key
        GlassCard.do_snapshot(self, snap)


# The clock's lines in the month's colors (autumn ones in October and November, frost in winter, blossom in
# spring, sun in summer), bright enough on the dark sides; shuffled anew every day.
MONTH_COLORS = {
    1: ("#cfe8ff", "#9ecbff", "#ffffff", "#b8c0ff", "#e0e1dd", "#a0c4ff"),
    2: ("#ffb3c6", "#ff8fab", "#e0aaff", "#cdb4db", "#ffffff", "#bde0fe"),
    3: ("#b9fbc0", "#98f5e1", "#fff3b0", "#ffd6a5", "#caffbf", "#a0e7e5"),
    4: ("#ffd1dc", "#c1fba4", "#fdffb6", "#9bf6ff", "#ffc6ff", "#b5ead7"),
    5: ("#ff99c8", "#fcf6bd", "#d0f4de", "#a9def9", "#e4c1f9", "#ffe066"),
    6: ("#ffe066", "#ff9f1c", "#2ec4b6", "#cbf3f0", "#ffbf69", "#ff6b6b"),
    7: ("#ffd166", "#ef476f", "#06d6a0", "#4cc9f0", "#ff9e00", "#ffd60a"),
    8: ("#ffb703", "#fb8500", "#8ecae6", "#ffd166", "#f4a261", "#e9c46a"),
    9: ("#e9c46a", "#f4a261", "#e76f51", "#a7c957", "#f2e8cf", "#ffb347"),
    10: ("#ff8c1a", "#ffb347", "#ff6b4a", "#ffd166", "#e25822", "#f4a259"),
    11: ("#d4a373", "#e76f51", "#f4a261", "#ffcc80", "#bc8a5f", "#c9ada7"),
    12: ("#e0f7ff", "#a8dadc", "#ffffff", "#ff4d4d", "#7bd389", "#ffd700"),
}


def day_colors(n: int) -> list:
    """`n` colors from this month's palette, in today's order."""
    t = time.localtime()
    pal = list(MONTH_COLORS[t.tm_mon])
    random.Random(t.tm_year * 1000 + t.tm_yday).shuffle(pal)
    return [_rgba(*(int(c[i:i + 2], 16) for i in (1, 3, 5)), 1) for c in (pal * n)[:n]]


# The season's motifs falling like snow beside the cover (SnowFall): autumn leaves, snowflakes and frost,
# spring blossoms, summer suns, palms and shells. Picked anew each day (the date seeds it); in a season's last
# month more and more of the next one's come in.
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
NEXT_SEASON = {"winter": "spring", "spring": "summer", "summer": "autumn", "autumn": "winter"}
SEASON_COLORS = {
    "autumn": ("#c0392b", "#e67e22", "#f39c12", "#d35400", "#8e4b10", "#b7950b", "#a04000", "#e74c3c"),
    "winter": ("#ffffff", "#e3f2fd", "#bbdefb", "#90caf9", "#cfd8dc", "#e1f5fe"),
    "spring": ("#f8bbd0", "#f48fb1", "#ffffff", "#e1bee7", "#fff59d", "#ffccbc", "#c5e1a5"),
    "summer": ("#ffd54f", "#ffb300", "#ff7043", "#4fc3f7", "#26c6da", "#81c784", "#fff176"),
}
SEASON_KINDS = {"autumn": ("maple", "maple", "maple", "maple", "leaf"), "winter": ("flake", "flake", "frost"),
                "spring": ("blossom", "blossom", "blossom", "bud", "sprig"), "summer": ("sun", "palm", "palm", "shell")}


def _hex(c: str, a: float = 1.0) -> Gdk.RGBA:
    return _rgba(int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), a)


def season_today() -> tuple:
    """(season, next season, share of the next one's motifs): 0 most of the season, rising to 0.5 over its last
    month."""
    t = time.localtime()
    season = SEASONS[t.tm_mon]
    share = 0.0
    if SEASONS[t.tm_mon % 12 + 1] != season:
        share = 0.5 * t.tm_mday / 31
    return season, NEXT_SEASON[season], share



# --- special days: their own falling motifs instead of the season's --------------------------------------------
def _easter(y: int) -> tuple:
    """Easter Sunday (Gregorian; anonymous algorithm): (month, day)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return month, (h + l - 7 * m + 114) % 31 + 1


def _shift(y: int, md: tuple, days: int) -> tuple:
    import datetime
    t = datetime.date(y, *md) + datetime.timedelta(days=days)
    return t.month, t.day


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> tuple:
    """The n-th `weekday` (0 Monday) of the month; n = -1 the last."""
    import calendar
    days = [d for d in range(1, calendar.monthrange(y, month)[1] + 1) if calendar.weekday(y, month, d) == weekday]
    return month, days[n - 1 if n > 0 else n]


# Lunar and lunisolar days, looked up (first day): Chinese New Year, Diwali, Hanukkah (8 days).
_LUNAR = {
    "cny": {2026: (2, 17), 2027: (2, 6), 2028: (1, 26), 2029: (2, 13), 2030: (2, 3), 2031: (1, 23), 2032: (2, 11),
            2033: (1, 31), 2034: (2, 19), 2035: (2, 8)},
    "diwali": {2026: (11, 8), 2027: (10, 29), 2028: (10, 17), 2029: (11, 5), 2030: (10, 26), 2031: (11, 14),
               2032: (11, 2), 2033: (10, 22), 2034: (11, 10), 2035: (10, 30)},
    "hanukkah": {2026: (12, 5), 2027: (12, 25), 2028: (12, 13), 2029: (12, 2), 2030: (12, 21), 2031: (12, 10),
                 2032: (11, 28), 2033: (12, 17), 2034: (12, 7), 2035: (12, 26)},
}

# id: (Czech name, what's on the letters, days(year) -> [(month, day)], motifs, colors, base fill)
HOLIDAYS = {
    "newyear": ("Nový rok", "ohňostroje a hvězdy", lambda y: [(1, 1)], ("firework", "firework", "star"),
                ("#ffd54f", "#ff4081", "#40c4ff", "#b2ff59", "#ffffff", "#e040fb"), None),
    "kings": ("Tři králové", "koruny a hvězdy", lambda y: [(1, 6)], ("crown", "star", "star"),
              ("#ffd54f", "#ffca28", "#ffffff", "#ce93d8"), None),
    "radio": ("Světový den rádia", "rádia a noty", lambda y: [(2, 13)], ("radio", "note", "note"),
              ("#e3051a", "#ffffff", "#ffd54f", "#212121"), None),
    "valentine": ("Valentýn", "srdce", lambda y: [(2, 14)], ("heart", "heart", "heart", "blossom"),
                  ("#e91e63", "#ff1744", "#f48fb1", "#ffffff", "#d50000"), None),
    "carnival": ("Masopust", "masky a konfety", lambda y: [_shift(y, _easter(y), -47), _shift(y, _easter(y), -51)],
                 ("mask", "mask", "confetti"), ("#e040fb", "#ffd54f", "#00e676", "#ff1744", "#40c4ff"), None),
    "women": ("Mezinárodní den žen", "mimózy a květy", lambda y: [(3, 8)], ("mimosa", "mimosa", "blossom"),
              ("#fff176", "#ffeb3b", "#f8bbd0", "#ffffff"), None),
    "pi": ("Den čísla π", "π", lambda y: [(3, 14)], ("pi", "pi"), ("#40c4ff", "#ffffff", "#ffd54f", "#b388ff"), None),
    "patrick": ("Den svatého Patrika", "trojlístky", lambda y: [(3, 17)], ("shamrock", "shamrock", "shamrock"),
                ("#00c853", "#64dd17", "#2e7d32", "#b9f6ca"), None),
    "easter": ("Velikonoce", "kraslice, květy a pomlázky",
               lambda y: [_shift(y, _easter(y), d) for d in (-2, -1, 0, 1)], ("egg", "egg", "egg", "blossom", "sprig"),
               ("#ff7043", "#ffd54f", "#4fc3f7", "#e040fb", "#66bb6a", "#f44336"), None),
    "earth": ("Den Země", "zeměkoule a listí", lambda y: [(4, 22)], ("globe", "leaf", "sprig"),
              ("#29b6f6", "#66bb6a", "#2e7d32", "#81d4fa"), None),
    "witches": ("Pálení čarodějnic", "ohně a košťata", lambda y: [(4, 30)], ("flame", "flame", "broom", "star"),
                ("#ff6d00", "#ffab00", "#d50000", "#ffd54f"), None),
    "may": ("1. máj, lásky čas", "srdce a květy", lambda y: [(5, 1)], ("heart", "blossom", "blossom"),
            ("#f48fb1", "#ffffff", "#e91e63", "#f8bbd0"), None),
    "starwars": ("Den Star Wars (May the 4th)", "světelné meče a hvězdy", lambda y: [(5, 4)],
                 ("saber", "saber", "star"), ("#00e5ff", "#ff1744", "#76ff03", "#ffffff", "#d500f9"), None),
    "victory": ("Den vítězství", "česká vlajka", lambda y: [(5, 8)], ("star",), ("#ffffff",), "cz"),
    "mothers": ("Den matek", "srdce a květy", lambda y: [_nth_weekday(y, 5, 6, 2)], ("heart", "blossom", "blossom"),
                ("#f06292", "#ffffff", "#f8bbd0", "#e91e63"), None),
    "children": ("Den dětí", "balonky a hvězdy", lambda y: [(6, 1)], ("balloon", "balloon", "star"),
                 ("#ff1744", "#2979ff", "#ffea00", "#00e676", "#e040fb", "#ff9100"), None),
    "fathers": ("Den otců", "hvězdy a kytary", lambda y: [_nth_weekday(y, 6, 6, 3)], ("star", "guitar"),
                ("#40c4ff", "#ffd54f", "#ffffff"), None),
    "cyril": ("Den slovanských věrozvěstů Cyrila a Metoděje", "česká vlajka", lambda y: [(7, 5)], ("star",),
              ("#ffd54f",), "cz"),
    "hus": ("Den upálení mistra Jana Husa", "česká vlajka a plameny", lambda y: [(7, 6)], ("flame",),
            ("#ff6d00", "#ffab00"), "cz"),
    "july4": ("Den nezávislosti USA", "hvězdy a pruhy, ohňostroje", lambda y: [(7, 4)], ("firework", "star"),
              ("#ffffff", "#ff1744", "#2979ff", "#ffd54f"), "us"),
    "rock": ("Světový den rocku", "kytary, blesky a noty", lambda y: [(7, 13)], ("guitar", "bolt", "note"),
             ("#e3051a", "#ffd54f", "#ffffff", "#212121"), None),
    "wenceslas": ("Den české státnosti (sv. Václav)", "česká vlajka a koruny", lambda y: [(9, 28)], ("crown",),
                  ("#ffd54f",), "cz"),
    "music": ("Mezinárodní den hudby", "noty", lambda y: [(10, 1)], ("note", "note", "note"),
              ("#ffd54f", "#ffffff", "#e3051a", "#40c4ff"), None),
    "republic": ("Den vzniku Československa", "česká vlajka", lambda y: [(10, 28)], ("star",), ("#ffffff",), "cz"),
    # Halloween runs on into Všech svatých and Dušičky (1–2 Nov), together with them and Día de los Muertos
    "halloween": ("Halloween", "dýně, netopýři, duchové a pavouci", lambda y: [(10, 31), (11, 1), (11, 2)],
                  ("pumpkin", "pumpkin", "bat", "bat", "ghost", "spider", "web"),
                  ("#ff6d00", "#212121", "#ffffff", "#7c4dff", "#76ff03"), None),
    "dusicky": ("Dušičky", "hřbitovní lampičky, svíčky, věnce a chryzantémy", lambda y: [(11, 1), (11, 2)],
                ("gravelight", "gravelight", "candle", "wreath", "mum"),
                ("#d50000", "#ffd54f", "#ffffff", "#ffb300", "#b71c1c"), None),
    "dead": ("Día de los Muertos", "calaveras, cempasúchil a papel picado", lambda y: [(11, 1), (11, 2)],
             ("calavera", "calavera", "marigold", "marigold", "papel", "skull"),
             ("#ff9100", "#ffd54f", "#e040fb", "#00e5ff", "#ff1744", "#76ff03"), None),
    "freedom": ("Den boje za svobodu a demokracii", "česká vlajka a svíčky", lambda y: [(11, 17)], ("candle",),
                ("#ffffff", "#ffd54f"), "cz"),
    "thanks": ("Den díkůvzdání (USA)", "dýně a listí", lambda y: [_nth_weekday(y, 11, 3, 4)],
               ("pumpkin", "maple", "leaf"), ("#ff6d00", "#c0392b", "#f39c12", "#8e4b10"), None),
    "nicholas": ("Mikuláš", "hvězdy, andělé a čerti (plamínky)", lambda y: [(12, 5)], ("star", "flame", "gift"),
                 ("#ffd54f", "#ffffff", "#ff1744", "#ff6d00"), None),
    "advent": ("Advent", "baňky, hvězdy a stromky", lambda y: [(12, d) for d in range(1, 24)],
               ("bauble", "star", "tree", "flake"), ("#e53935", "#ffd54f", "#43a047", "#ffffff", "#1e88e5"), None),
    "christmas": ("Vánoce", "stromky, dárky, baňky a hvězdy", lambda y: [(12, 24), (12, 25), (12, 26)],
                  ("tree", "tree", "gift", "bauble", "star", "flake"),
                  ("#e53935", "#43a047", "#ffd54f", "#ffffff", "#1e88e5", "#c62828"), None),
    "silvester": ("Silvestr", "ohňostroje", lambda y: [(12, 31)], ("firework", "firework", "star"),
                  ("#ffd54f", "#ff4081", "#40c4ff", "#b2ff59", "#ffffff", "#e040fb"), None),
    "cny": ("Čínský nový rok", "lampiony, mince a ohňostroje",
            lambda y: [_LUNAR["cny"][y]] if y in _LUNAR["cny"] else [], ("lantern", "lantern", "coin", "firework"),
            ("#d50000", "#ffd54f", "#ff6d00", "#ffab00"), None),
    "diwali": ("Diwali", "lampičky dija a ohňostroje",
               lambda y: [_LUNAR["diwali"][y]] if y in _LUNAR["diwali"] else [], ("diya", "diya", "firework", "star"),
               ("#ffab00", "#ff6d00", "#e040fb", "#ffd54f"), None),
    "hanukkah": ("Chanuka", "svíce a Davidovy hvězdy",
                 lambda y: [_shift(y, _LUNAR["hanukkah"][y], d) for d in range(8)] if y in _LUNAR["hanukkah"] else [],
                 ("candle", "candle", "david"), ("#1e88e5", "#ffffff", "#90caf9", "#ffd54f"), None),
}
HOLIDAY_PERIODS = {"advent"}  # whole stretches: mixed with the season, not instead of it
UPRIGHT = {"palm", "sprig", "tree", "candle", "pumpkin", "ghost", "diya", "lantern", "flame", "radio", "balloon",
           "skull", "gift", "crown", "broom", "guitar", "mimosa", "gravelight", "calavera", "papel", "mum"}


def holidays_today() -> list:
    """Today's special days (ids), from the HOLIDAYS calendar."""
    t = time.localtime()
    out = []
    for hid, h in HOLIDAYS.items():
        try:
            if (t.tm_mon, t.tm_mday) in h[2](t.tm_year):
                out.append(hid)
        except (KeyError, ValueError, IndexError):
            pass
    return out


# The Czech calendar (days_cz.json, gathered from penize.cz): each day's name day, its state holiday and the
# international and other notable days on it (who declared them, since when). Easter's holidays move: computed.
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "days_cz.json"), encoding="utf-8") as _f:
        DAYS_CZ = json.load(_f)
except (OSError, ValueError):
    DAYS_CZ = {}


def day_cz(t=None) -> dict:
    """Today's entry of the Czech calendar: {"names", "state", "days": [[name, declared by, since]]}."""
    t = t or time.localtime()
    d = dict(DAYS_CZ.get(f"{t.tm_mon:02d}-{t.tm_mday:02d}", {}))
    e = _easter(t.tm_year)
    for shift, name in ((-2, "Velký pátek"), (1, "Velikonoční pondělí")):
        if _shift(t.tm_year, e, shift) == (t.tm_mon, t.tm_mday):
            d["state"] = name
    return d


def name_day() -> str:
    """Whose name day it is, in capitals: "FELIX", "BRONISLAV / BRONISLAVA"; "" when nobody's."""
    names = [n.strip() for n in day_cz().get("names", "").split(",") if n.strip()]
    return " / ".join(names).upper()


def special_day_name() -> str:
    """The day's name for the clock: the state holiday, else the special days the letters are dressed for,
    else the first international day; "" on an ordinary day."""
    d = day_cz()
    if d.get("state"):
        return d["state"]
    days = holidays_today()
    themed = [HOLIDAYS[h][0] for h in days if h not in HOLIDAY_PERIODS] or [HOLIDAYS[h][0] for h in days]
    if themed:
        return " · ".join(themed)
    return d["days"][0][0] if d.get("days") else ""


def balanced_rows(text: str, most: int = 20) -> list:
    """A long name in two or three rows as even as can be (at spaces; between names when it can), a short one
    as it is."""
    words = text.split()
    if len(text) <= most or len(words) < 2:
        return [text] if text else []
    n = min(3, len(words), -(-len(text) // (most + 4)))

    def join(a, b):  # a dot between names at a break goes
        return " ".join(words[a:b]).strip(" ·")
    best = None
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        edges = (0,) + cuts + (len(words),)
        rows = [join(a, b) for a, b in zip(edges, edges[1:])]
        dots = sum("·" not in (words[c - 1], words[c]) for c in cuts)  # breaks not between names
        score = (max(map(len, rows)) + 6 * dots if "·" in text else max(map(len, rows)))
        if all(rows) and (best is None or score < best[0]):
            best = (score, rows)
    return best[1] if best else [text]


# What each special day is, in brief (the hover box over the date)
HOLIDAY_ABOUT = {
    "newyear": "První den roku: přání, předsevzetí a dozvuky silvestrovských ohňostrojů.",
    "kings": "Svátek Tří králů (Kašpar, Melichar, Baltazar) končí Vánoce; koledníci píšou na dveře K+M+B.",
    "radio": "Den vyhlášený UNESCO (2011) na počest rádia: od 13. února 1946 vysílá rozhlas OSN.",
    "valentine": "Svátek zamilovaných podle sv. Valentina: přáníčka, srdíčka a květiny.",
    "carnival": "Masopust (končí úterkem před Popeleční středou): průvody masek, zabijačky a veselí před půstem.",
    "women": "Mezinárodní den žen (OSN od 1977), u nás s karafiáty a mimózami.",
    "pi": "14. 3. jako 3,14: den matematiky, koláčů (pie) a narozenin Alberta Einsteina.",
    "patrick": "Svátek patrona Irska: zelená, trojlístky a irská hudba po celém světě.",
    "easter": "Velikonoce, svátky jara a zmrtvýchvstání Krista: kraslice, pomlázka a beránek.",
    "earth": "Den Země (od 1970) připomíná ochranu přírody a planety.",
    "witches": "Filipojakubská noc: pálí se čarodějnice a ohně mají zahnat zlé síly.",
    "may": "Lásky čas: polibek pod rozkvetlou třešní (a Svátek práce).",
    "starwars": "„May the 4th be with you“: den fanoušků Hvězdných válek.",
    "victory": "Konec 2. světové války v Evropě (8. května 1945).",
    "mothers": "Druhá květnová neděle patří maminkám: květiny a díky.",
    "children": "Mezinárodní den dětí: hry, výlety a dárky pro děti.",
    "fathers": "Třetí červnová neděle patří tátům.",
    "cyril": "Příchod věrozvěstů Cyrila a Metoděje na Velkou Moravu (863): písmo a křesťanství.",
    "hus": "Mistr Jan Hus byl upálen v Kostnici 6. července 1415 pro své učení.",
    "july4": "Den nezávislosti USA (1776): přehlídky, grilování a ohňostroje.",
    "rock": "Světový den rocku: na počest koncertu Live Aid 13. července 1985.",
    "wenceslas": "Svatý Václav, patron české země, zavražděn ve Staré Boleslavi (935).",
    "music": "Mezinárodní den hudby (UNESCO, od 1975).",
    "republic": "Vznik samostatného Československa 28. října 1918.",
    "halloween": "Předvečer Všech svatých: vydlabané dýně, kostýmy a „koledu, nebo vám něco provedu“. "
                 "U nás svítí od 31. října do Dušiček.",
    "dusicky": "Všech svatých (1. 11.) a Památka zesnulých (2. 11.): svíčky, věnce a chryzantémy na hrobech blízkých.",
    "dead": "Mexický Den mrtvých (1.–2. 11.): oltáře ofrendas, cukrové lebky, měsíčky cempasúchil a papel "
            "picado vítají duše předků domů. Na seznamu UNESCO.",
    "freedom": "17. listopad 1939 a 1989: studenti proti nacismu a sametová revoluce.",
    "thanks": "Den díkůvzdání v USA (čtvrtý čtvrtek v listopadu): krocan, dýňový koláč a rodina.",
    "nicholas": "Mikulášská nadílka: Mikuláš, anděl a čert chodí za dětmi.",
    "advent": "Čtyři týdny před Vánoci: věnec se svíčkami, kalendáře a těšení.",
    "christmas": "Vánoce: Štědrý den, stromeček, dárky a koledy.",
    "silvester": "Poslední den roku: oslavy, přípitek o půlnoci a ohňostroje.",
    "cny": "Čínský (lunární) nový rok, Svátek jara: draci, lampiony a červené obálky.",
    "diwali": "Indický svátek světel: lampičky dijá, ohňostroje a sladkosti.",
    "hanukkah": "Židovský svátek světel: osm dní se zapaluje svíce chanukije.",
}
STATE_ABOUT = {
    "Nový rok a Den obnovy samostatného českého státu": "Od 1. ledna 1993 samostatná Česká republika.",
    "Velký pátek": "Den ukřižování Krista; státní svátek od 2016.",
    "Velikonoční pondělí": "Pomlázka, koledníci a kraslice.",
    "Svátek práce": "Mezinárodní svátek pracujících (od 1890).",
    "Den vítězství": "Konec 2. světové války v Evropě (8. května 1945).",
    "Den slovanských věrozvěstů Cyrila a Metoděje": "Příchod Cyrila a Metoděje na Velkou Moravu (863).",
    "Den upálení mistra Jana Husa": "Jan Hus upálen v Kostnici (1415).",
    "Den české státnosti": "Svatý Václav, patron české země (zavražděn 935).",
    "Den vzniku samostatného československého státu": "Vznik Československa 28. října 1918.",
    "Den boje za svobodu a demokracii a Mezinárodní den studentstva":
        "17. listopad 1939 (uzavření vysokých škol nacisty) a 1989 (začátek sametové revoluce).",
    "Štědrý den": "Večeře s kaprem, stromeček a dárky od Ježíška.",
    "1. svátek vánoční": "Boží hod vánoční, narození Krista.",
    "2. svátek vánoční": "Svatý Štěpán: koledy a návštěvy.",
}


def day_summary() -> list:
    """The hover box's text (Czech) as (kind, Pango markup) pieces, kind "head" (a headline: a red pill, like ON
    AIR) or "text": the date, the name day, the state holiday and special days with a word on each, the
    international days, and what's on the letters."""
    t = time.localtime()
    esc = GLib.markup_escape_text
    d = day_cz(t)
    out = [("head", f"{DAYS[t.tm_wday].capitalize()} {t.tm_mday}. {MONTHS[t.tm_mon - 1]} {t.tm_year}")]
    names = [n.strip() for n in d.get("names", "").split(",") if n.strip()]
    if names:
        out.append(("text", f"Jmeniny {'má' if len(names) == 1 else 'mají'}: <b>{esc(' a '.join(names))}</b>"))
    if d.get("state"):
        out.append(("head", f"Státní svátek: {esc(d['state'])}"))
        if d["state"] in STATE_ABOUT:
            out.append(("text", esc(STATE_ABOUT[d["state"]])))
    for h in holidays_today():
        if d.get("state") and HOLIDAYS[h][5] == "cz":
            continue  # the state holiday said it already
        out.append(("head", esc(HOLIDAYS[h][0])))
        about = [esc(HOLIDAY_ABOUT[h])] if h in HOLIDAY_ABOUT else []
        if EFFECTS.get("season"):
            about.append(f"<i>Padají: {esc(HOLIDAYS[h][1])}</i>")
        if about:
            out.append(("text", "\n".join(about)))
    days = d.get("days") or []
    if days:
        out.append(("head", "Mezinárodní a významné dny"))
        rows = []
        for name, by, since in days:
            more = ", ".join(p for p in (f"vyhlásil(a) {by}" if by else "", f"od {since}" if since else "") if p)
            rows.append("• " + esc(name) + (f" <span alpha='70%'>({esc(more)})</span>" if more else ""))
        out.append(("text", "\n".join(rows)))
    if EFFECTS.get("season"):  # (the season's fall every day, a special day's with them)
        season = season_today()[0]
        out.append(("text", f"<i>Padají: {esc(SEASON_CZ[season][2])} ({SEASON_CZ[season][0]})</i>"))
    return out


for _hid, _h in HOLIDAYS.items():  # each day's motifs and colors, like a season's
    SEASON_KINDS[_hid], SEASON_COLORS[_hid] = _h[3], _h[4]

SEASON_CZ = {"autumn": ("podzim", "podzimu", "listí"), "winter": ("zima", "zimy", "vločky a jinovatka"),
             "spring": ("jaro", "jara", "květy, poupata a větvičky"), "summer": ("léto", "léta", "slunce, palmy a mušle")}


def season_text(clock_bytes: int = None) -> str:
    if not EFFECTS["season"]:
        return "Vypnuto: nic nepadá, datum a čas v barvách měsíce"
    season, nxt, share = season_today()
    name, _, motifs = SEASON_CZ[season]
    days = holidays_today()
    if days:  # the season's always, the day's as many again
        name += " + " + " + ".join(HOLIDAYS[d][0] for d in days)
        motifs += "; napůl s: " + ", ".join(HOLIDAYS[d][1] for d in days)
    text = (f"Padají pomalu jako sníh a pootáčejí se za datem a časem, tlačítky, kvalitou, hlasitostí a jmény "
            f"(písmena jen v barvách měsíce, čitelnější); bez animací stojí; klik na datum je vypne a zapne; "
            f"každý den trochu jinak; o svátcích a zvláštních dnech (české, americké i světové: Vánoce, Halloween, "
            f"4. červenec, Velikonoce, Den rocku…) napůl i jejich vlastní · dnes: {name} ({motifs})")
    if share > 0:
        text += f", přidává se {round(share * 100)} % {SEASON_CZ[nxt][1]}"
    if clock_bytes is not None:
        text += f" · paměť: GPU {mem_text(clock_bytes)} (obrázky vykreslené jednou za den)"
    return text


class SeasonDecor:
    """The season's (and special days') motifs, each drawn round (0, 0) at a given size: the snow's pictures."""

    # -- pieces --------------------------------------------------------------------------------------------
    @staticmethod
    def _dot(s, x, y, r, color):
        pb = Gsk.PathBuilder.new()
        pb.add_circle(Graphene.Point().init(x, y), r)
        s.append_fill(pb.to_path(), Gsk.FillRule.WINDING, color)

    @staticmethod
    def _line(s, pts, width, color):
        pb = Gsk.PathBuilder.new()
        pb.move_to(*pts[0])
        for p in pts[1:]:
            pb.line_to(*p)
        st = Gsk.Stroke.new(width)
        st.set_line_cap(Gsk.LineCap.ROUND)
        s.append_stroke(pb.to_path(), st, color)

    @staticmethod
    def _darker(c: Gdk.RGBA, k: float = 0.6) -> Gdk.RGBA:
        return _rgba(int(c.red * 255 * k), int(c.green * 255 * k), int(c.blue * 255 * k), c.alpha)

    @staticmethod
    def _leaf(s, rng, size, color, _sea):
        """An almond leaf with its middle vein."""
        L, W = size, size * rng.uniform(0.32, 0.45)
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, -L / 2)
        pb.quad_to(W, 0, 0, L / 2)
        pb.quad_to(-W, 0, 0, -L / 2)
        path = pb.to_path()
        s.append_fill(path, Gsk.FillRule.WINDING, color)
        s.append_stroke(path, Gsk.Stroke.new(max(0.8, size * 0.03)), SeasonDecor._darker(color, 0.45))
        SeasonDecor._line(s, [(0, -L / 2), (0, L / 2 + L * 0.12)], max(1.0, size * 0.035), SeasonDecor._darker(color))

    @staticmethod
    def _maple(s, rng, size, color, _sea):
        """A lobed leaf (five points) with veins out from its stem."""
        R = size / 2
        pb = Gsk.PathBuilder.new()
        steps = 60
        for i in range(steps + 1):
            a = -math.pi / 2 + 2 * math.pi * i / steps
            lobe = abs(math.cos(2.5 * (a + math.pi / 2)))
            r = R * (0.45 + 0.55 * lobe ** 0.6) * (0.75 if math.sin(a) > 0.6 else 1)
            x, y = r * math.cos(a), r * math.sin(a)
            (pb.move_to if i == 0 else pb.line_to)(x, y)
        pb.close()
        path = pb.to_path()
        s.append_fill(path, Gsk.FillRule.WINDING, color)
        dark = SeasonDecor._darker(color, 0.45)
        s.append_stroke(path, Gsk.Stroke.new(max(0.8, size * 0.025)), dark)
        for k in range(5):
            a = -math.pi / 2 + 2 * math.pi * k / 5
            SeasonDecor._line(s, [(0, R * 0.25), (R * 0.85 * math.cos(a), R * 0.85 * math.sin(a))],
                              max(0.8, size * 0.025), dark)
        SeasonDecor._line(s, [(0, R * 0.25), (0, R * 1.05)], max(1.0, size * 0.04), dark)

    @staticmethod
    def _flake(s, rng, size, color, _sea):
        """A six-armed snowflake, branched."""
        R, wid = size / 2, max(1.0, size * 0.045)
        for k in range(6):
            a = math.pi / 3 * k
            ca, sa = math.cos(a), math.sin(a)
            SeasonDecor._line(s, [(0, 0), (R * ca, R * sa)], wid, color)
            for f in (0.45, 0.72):
                bx, by = R * f * ca, R * f * sa
                for side in (-1, 1):
                    b = a + side * math.pi / 4
                    l = R * 0.28 * (1.2 - f)
                    SeasonDecor._line(s, [(bx, by), (bx + l * math.cos(b), by + l * math.sin(b))], wid * 0.8, color)

    @staticmethod
    def _frost(s, rng, size, color, _sea):
        """Frost crystals: a star of fine needles."""
        for _ in range(rng.randint(7, 12)):
            a = rng.uniform(0, 2 * math.pi)
            l = size * rng.uniform(0.2, 0.55)
            SeasonDecor._line(s, [(0, 0), (l * math.cos(a), l * math.sin(a))], max(0.7, size * 0.02), color)

    @staticmethod
    def _blossom(s, rng, size, color, _sea):
        """Five round petals around a yellow heart."""
        R = size / 2
        for k in range(5):
            a = 2 * math.pi * k / 5
            pb = Gsk.PathBuilder.new()
            pb.add_circle(Graphene.Point().init(R * 0.5 * math.cos(a), R * 0.5 * math.sin(a)), R * 0.42)
            s.append_stroke(pb.to_path(), Gsk.Stroke.new(max(0.8, size * 0.03)), SeasonDecor._darker(color, 0.55))
        for k in range(5):
            a = 2 * math.pi * k / 5
            SeasonDecor._dot(s, R * 0.5 * math.cos(a), R * 0.5 * math.sin(a), R * 0.42, color)
        SeasonDecor._dot(s, 0, 0, R * 0.22, _hex("#ffd54f", 0.95))
        for k in range(5):
            a = 2 * math.pi * (k + 0.5) / 5
            SeasonDecor._dot(s, R * 0.3 * math.cos(a), R * 0.3 * math.sin(a), R * 0.05, _hex("#e65100", 0.9))

    @staticmethod
    def _bud(s, rng, size, color, sea):
        SeasonDecor._dot(s, 0, 0, size * 0.18, color)
        SeasonDecor._leaf(s, rng, size * 0.5, _hex("#7cb342", 0.9), sea)

    @staticmethod
    def _sprig(s, rng, size, color, sea):
        """A young twig with leaves either side."""
        green = _hex(rng.choice(("#8bc34a", "#7cb342", "#aed581")), 0.9)
        SeasonDecor._line(s, [(0, size / 2), (0, -size / 2)], max(1.0, size * 0.04), _hex("#558b2f", 0.9))
        for k in range(4):
            y = size / 2 - size * (k + 0.6) / 4.5
            for side in (-1, 1):
                s.save()
                s.translate(Graphene.Point().init(side * size * 0.12, y))
                s.rotate(side * 50)
                SeasonDecor._leaf(s, rng, size * 0.3, green, sea)
                s.restore()

    @staticmethod
    def _sun(s, rng, size, color, _sea):
        """A sun: a disc with pointed rays."""
        R = size / 2
        gold = _hex(rng.choice(("#ffd54f", "#ffca28", "#ffb300")), 0.95)
        pb = Gsk.PathBuilder.new()
        rays = rng.randint(9, 14)
        for i in range(rays * 2 + 1):
            a = math.pi * i / rays
            r = R if i % 2 == 0 else R * 0.62
            (pb.move_to if i == 0 else pb.line_to)(r * math.cos(a), r * math.sin(a))
        pb.close()
        s.append_fill(pb.to_path(), Gsk.FillRule.WINDING, _hex("#ff8f00", 0.9))
        SeasonDecor._dot(s, 0, 0, R * 0.5, gold)

    @staticmethod
    def _palm(s, rng, size, color, sea):
        """A palm: a bent trunk, a crown of fronds."""
        H = size * 1.2
        bend = size * rng.uniform(-0.25, 0.25)
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, H / 2)
        pb.quad_to(bend, 0, bend * 0.6, -H / 2)
        st = Gsk.Stroke.new(max(1.5, size * 0.09))
        st.set_line_cap(Gsk.LineCap.ROUND)
        s.append_stroke(pb.to_path(), st, _hex("#8d6e63", 0.95))
        green = _hex(rng.choice(("#43a047", "#2e7d32", "#66bb6a")), 0.95)
        for k in range(rng.randint(5, 7)):
            a = math.pi + math.pi * (k + 0.5) / 6.5 + rng.uniform(-0.15, 0.15)
            s.save()
            s.translate(Graphene.Point().init(bend * 0.6, -H / 2))
            s.rotate(math.degrees(a) + 90)
            s.translate(Graphene.Point().init(0, -size * 0.28))
            SeasonDecor._leaf(s, rng, size * 0.6, green, sea)
            s.restore()
        for k in range(rng.randint(1, 3)):  # coconuts
            SeasonDecor._dot(s, bend * 0.6 + rng.uniform(-1, 1) * size * 0.06, -H / 2 + size * 0.06, size * 0.06,
                             _hex("#5d4037", 0.95))

    @staticmethod
    def _shell(s, rng, size, color, _sea):
        """A scallop shell: a fan with ribs."""
        R = size / 2.2
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, R * 0.5)
        for i in range(13):
            a = math.pi + math.pi * i / 12
            pb.line_to(R * math.cos(a), R * 0.2 + R * math.sin(a))
        pb.close()
        s.append_fill(pb.to_path(), Gsk.FillRule.WINDING, _hex(rng.choice(("#ffccbc", "#ffe0b2", "#f8bbd0")), 0.95))
        for i in range(1, 8):
            a = math.pi + math.pi * i / 8
            SeasonDecor._line(s, [(0, R * 0.5), (R * math.cos(a), R * 0.2 + R * math.sin(a))],
                              max(0.7, size * 0.02), _hex("#bf8a73", 0.9))


    # -- special days' pieces ------------------------------------------------------------------------------
    @staticmethod
    def _fill(s, pb, color):
        s.append_fill(pb.to_path(), Gsk.FillRule.WINDING, color)

    @staticmethod
    def _poly(s, pts, color, close=True):
        pb = Gsk.PathBuilder.new()
        pb.move_to(*pts[0])
        for p in pts[1:]:
            pb.line_to(*p)
        if close:
            pb.close()
        SeasonDecor._fill(s, pb, color)

    @staticmethod
    def _oval(s, x, y, rx, ry, color):
        s.save()
        s.translate(Graphene.Point().init(x, y))
        s.scale(1, ry / max(rx, 0.01))
        SeasonDecor._dot(s, 0, 0, rx, color)
        s.restore()

    @staticmethod
    def _star(s, rng, size, color, _sea, points=5):
        R = size / 2
        pts = [((R if i % 2 == 0 else R * 0.42) * math.cos(-math.pi / 2 + math.pi * i / points),
                (R if i % 2 == 0 else R * 0.42) * math.sin(-math.pi / 2 + math.pi * i / points))
               for i in range(points * 2)]
        SeasonDecor._poly(s, pts, color)

    @staticmethod
    def _heart(s, rng, size, color, _sea):
        R = size / 2
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, R * 0.95)
        pb.cubic_to(-R * 1.3, R * 0.1, -R * 0.75, -R * 0.95, 0, -R * 0.35)
        pb.cubic_to(R * 0.75, -R * 0.95, R * 1.3, R * 0.1, 0, R * 0.95)
        pb.close()
        SeasonDecor._fill(s, pb, color)
        SeasonDecor._dot(s, -R * 0.35, -R * 0.35, R * 0.12, _hex("#ffffff", 0.5))

    @staticmethod
    def _firework(s, rng, size, color, _sea):
        R = size / 2
        rays = rng.randint(10, 16)
        for i in range(rays):
            a = 2 * math.pi * i / rays + rng.uniform(-0.1, 0.1)
            r = R * rng.uniform(0.7, 1.0)
            c = _hex(rng.choice(("#ffd54f", "#ff4081", "#40c4ff", "#b2ff59", "#ffffff", "#e040fb")), 0.95)
            SeasonDecor._line(s, [(R * 0.15 * math.cos(a), R * 0.15 * math.sin(a)), (r * math.cos(a), r * math.sin(a))],
                              max(1.0, size * 0.03), c)
            SeasonDecor._dot(s, r * 1.08 * math.cos(a), r * 1.08 * math.sin(a), size * 0.035, c)
        SeasonDecor._dot(s, 0, 0, size * 0.05, _hex("#ffffff"))

    @staticmethod
    def _crown(s, rng, size, color, _sea):
        W, H = size * 0.9, size * 0.6
        SeasonDecor._poly(s, [(-W / 2, H / 2), (-W / 2, -H / 4), (-W / 4, H / 8), (0, -H / 2), (W / 4, H / 8),
                              (W / 2, -H / 4), (W / 2, H / 2)], _hex("#ffc107", 0.95))
        for x, c in ((-W / 4, "#e53935"), (0, "#1e88e5"), (W / 4, "#43a047")):
            SeasonDecor._dot(s, x, H * 0.3, size * 0.06, _hex(c))

    @staticmethod
    def _note(s, rng, size, color, _sea):
        SeasonDecor._oval(s, -size * 0.12, size * 0.3, size * 0.16, size * 0.11, color)
        SeasonDecor._line(s, [(size * 0.03, size * 0.3), (size * 0.03, -size * 0.4)], max(1.0, size * 0.06), color)
        SeasonDecor._poly(s, [(size * 0.03, -size * 0.4), (size * 0.3, -size * 0.2), (size * 0.28, -size * 0.08),
                              (size * 0.03, -size * 0.25)], color)

    @staticmethod
    def _radio(s, rng, size, color, _sea):
        W, H = size * 0.9, size * 0.55
        pb = Gsk.PathBuilder.new()
        rr = Gsk.RoundedRect()
        rr.init_from_rect(Graphene.Rect().init(-W / 2, -H / 2, W, H), size * 0.08)
        pb.add_rounded_rect(rr)
        SeasonDecor._fill(s, pb, _hex("#e3051a", 0.95))
        SeasonDecor._dot(s, -W * 0.2, 0, H * 0.3, _hex("#212121", 0.95))
        for k in range(3):
            SeasonDecor._line(s, [(W * 0.08, -H * 0.2 + k * H * 0.2), (W * 0.4, -H * 0.2 + k * H * 0.2)],
                              max(1.0, size * 0.03), _hex("#ffffff", 0.9))
        SeasonDecor._line(s, [(W * 0.3, -H / 2), (W * 0.5, -H * 1.1)], max(1.0, size * 0.03), _hex("#bdbdbd"))

    @staticmethod
    def _mask(s, rng, size, color, _sea):
        W, H = size * 0.9, size * 0.4
        pb = Gsk.PathBuilder.new()
        pb.move_to(-W / 2, -H * 0.3)
        pb.quad_to(0, -H * 0.9, W / 2, -H * 0.3)
        pb.quad_to(W * 0.45, H * 0.7, W * 0.1, H * 0.4)
        pb.quad_to(0, H * 0.1, -W * 0.1, H * 0.4)
        pb.quad_to(-W * 0.45, H * 0.7, -W / 2, -H * 0.3)
        pb.close()
        for x in (-W * 0.22, W * 0.22):
            pb.add_circle(Graphene.Point().init(x, 0), H * 0.22)
        s.append_fill(pb.to_path(), Gsk.FillRule.EVEN_ODD, color)

    @staticmethod
    def _confetti(s, rng, size, color, _sea):
        for _ in range(8):
            c = _hex(rng.choice(("#e040fb", "#ffd54f", "#00e676", "#ff1744", "#40c4ff")), 0.95)
            x, y = rng.uniform(-1, 1) * size / 2, rng.uniform(-1, 1) * size / 2
            SeasonDecor._poly(s, [(x, y), (x + size * 0.1, y + size * 0.03), (x + size * 0.08, y + size * 0.1),
                                  (x - size * 0.02, y + size * 0.07)], c)

    @staticmethod
    def _mimosa(s, rng, size, color, sea):
        s.save()
        s.translate(Graphene.Point().init(0, size * 0.25))
        SeasonDecor._leaf(s, rng, size * 0.6, _hex("#7cb342", 0.9), sea)
        s.restore()
        for _ in range(9):
            SeasonDecor._dot(s, rng.uniform(-0.3, 0.3) * size, rng.uniform(-0.35, 0.05) * size, size * 0.08,
                             _hex(rng.choice(("#fff176", "#ffeb3b", "#fdd835")), 0.97))

    @staticmethod
    def _pi(s, rng, size, color, _sea):
        w = max(1.5, size * 0.09)
        SeasonDecor._line(s, [(-size * 0.35, -size * 0.25), (size * 0.38, -size * 0.28)], w, color)
        SeasonDecor._line(s, [(-size * 0.15, -size * 0.25), (-size * 0.2, size * 0.32)], w, color)
        SeasonDecor._line(s, [(size * 0.15, -size * 0.26), (size * 0.15, size * 0.28), (size * 0.25, size * 0.32)],
                          w, color)

    @staticmethod
    def _shamrock(s, rng, size, color, sea):
        for a in (0, 120, 240):
            s.save()
            s.rotate(a)
            s.translate(Graphene.Point().init(0, -size * 0.2))
            SeasonDecor._heart(s, rng, size * 0.45, color, sea)
            s.restore()
        SeasonDecor._line(s, [(0, 0), (size * 0.1, size * 0.5)], max(1.0, size * 0.05), _hex("#1b5e20"))

    @staticmethod
    def _egg(s, rng, size, color, _sea):
        rx, ry = size * 0.3, size * 0.4
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, -ry)
        pb.cubic_to(rx * 1.1, -ry, rx * 1.3, ry, 0, ry)
        pb.cubic_to(-rx * 1.3, ry, -rx * 1.1, -ry, 0, -ry)
        pb.close()
        path = pb.to_path()
        s.append_fill(path, Gsk.FillRule.WINDING, color)
        s.push_fill(path, Gsk.FillRule.WINDING)
        for k in range(-2, 3):
            y = k * ry * 0.38
            c = _hex(rng.choice(("#ffffff", "#ffd54f", "#4fc3f7", "#e040fb", "#43a047", "#f44336")), 0.95)
            pts = [(-rx * 1.3 + i * rx * 0.26, y + (ry * 0.08 if i % 2 else -ry * 0.08)) for i in range(11)]
            SeasonDecor._line(s, pts, max(1.0, size * 0.045), c, )
        s.pop()

    @staticmethod
    def _globe(s, rng, size, color, _sea):
        R = size / 2
        pb = Gsk.PathBuilder.new()
        pb.add_circle(Graphene.Point().init(0, 0), R)
        path = pb.to_path()
        s.append_fill(path, Gsk.FillRule.WINDING, _hex("#29b6f6", 0.95))
        s.push_fill(path, Gsk.FillRule.WINDING)
        for _ in range(5):
            SeasonDecor._oval(s, rng.uniform(-R, R) * 0.7, rng.uniform(-R, R) * 0.7, R * rng.uniform(0.2, 0.4),
                              R * rng.uniform(0.15, 0.3), _hex("#43a047", 0.95))
        s.pop()
        s.append_stroke(path, Gsk.Stroke.new(max(1.0, size * 0.03)), _hex("#01579b", 0.9))

    @staticmethod
    def _flame(s, rng, size, color, _sea):
        for k, c in ((1.0, "#ff3d00"), (0.7, "#ff9100"), (0.42, "#ffea00")):
            pb = Gsk.PathBuilder.new()
            pb.move_to(0, -size * 0.5 * k)
            pb.cubic_to(size * 0.35 * k, -size * 0.1 * k, size * 0.3 * k, size * 0.4 * k, 0, size * 0.4 * k)
            pb.cubic_to(-size * 0.3 * k, size * 0.4 * k, -size * 0.35 * k, -size * 0.1 * k, 0, -size * 0.5 * k)
            pb.close()
            s.save()
            s.translate(Graphene.Point().init(0, size * 0.1 * (1 - k)))
            SeasonDecor._fill(s, pb, _hex(c, 0.95))
            s.restore()

    @staticmethod
    def _broom(s, rng, size, color, _sea):
        SeasonDecor._line(s, [(0, -size * 0.55), (0, size * 0.15)], max(1.5, size * 0.06), _hex("#6d4c41"))
        for k in range(-4, 5):
            SeasonDecor._line(s, [(k * size * 0.02, size * 0.12), (k * size * 0.05, size * 0.5)],
                              max(1.0, size * 0.03), _hex("#d7a86e"))

    @staticmethod
    def _saber(s, rng, size, color, _sea):
        L = size * 1.1
        st = Gsk.Stroke.new(max(3.0, size * 0.14))
        st.set_line_cap(Gsk.LineCap.ROUND)
        pb = Gsk.PathBuilder.new()
        pb.move_to(0, -L / 2)
        pb.line_to(0, L * 0.3)
        s.append_stroke(pb.to_path(), st, _rgba(int(color.red * 255), int(color.green * 255), int(color.blue * 255), 0.45))
        SeasonDecor._line(s, [(0, -L / 2), (0, L * 0.3)], max(1.0, size * 0.05), _hex("#ffffff"))
        SeasonDecor._line(s, [(0, L * 0.3), (0, L / 2)], max(2.0, size * 0.09), _hex("#9e9e9e"))

    @staticmethod
    def _balloon(s, rng, size, color, _sea):
        SeasonDecor._oval(s, 0, -size * 0.12, size * 0.26, size * 0.32, color)
        SeasonDecor._dot(s, -size * 0.09, -size * 0.25, size * 0.05, _hex("#ffffff", 0.6))
        SeasonDecor._line(s, [(0, size * 0.2), (size * 0.05, size * 0.35), (-size * 0.03, size * 0.55)],
                          max(0.8, size * 0.02), _hex("#eeeeee"))

    @staticmethod
    def _guitar(s, rng, size, color, _sea):
        body = _hex(rng.choice(("#e3051a", "#ffb300", "#212121", "#1e88e5")), 0.97)
        SeasonDecor._dot(s, 0, size * 0.22, size * 0.24, body)
        SeasonDecor._dot(s, 0, -size * 0.02, size * 0.17, body)
        SeasonDecor._dot(s, 0, size * 0.12, size * 0.06, _hex("#000000", 0.9))
        SeasonDecor._line(s, [(0, size * 0.05), (0, -size * 0.55)], max(1.5, size * 0.07), _hex("#5d4037"))
        SeasonDecor._poly(s, [(-size * 0.05, -size * 0.5), (size * 0.05, -size * 0.5), (size * 0.06, -size * 0.65),
                              (-size * 0.06, -size * 0.65)], _hex("#3e2723"))

    @staticmethod
    def _bolt(s, rng, size, color, _sea):
        SeasonDecor._poly(s, [(size * 0.1, -size * 0.5), (-size * 0.2, size * 0.05), (0, size * 0.05),
                              (-size * 0.1, size * 0.5), (size * 0.22, -size * 0.08), (0.02 * size, -size * 0.08)],
                          _hex("#ffea00", 0.97))

    @staticmethod
    def _pumpkin(s, rng, size, color, _sea):
        R = size / 2
        for dx, k in ((-0.45, 0.7), (0.45, 0.7), (0, 0.8)):
            SeasonDecor._oval(s, dx * R, 0, R * k, R * 0.8, _hex("#ff6d00" if dx else "#ff9100", 0.97))
        SeasonDecor._line(s, [(0, -R * 0.75), (R * 0.1, -R * 1.05)], max(1.5, size * 0.07), _hex("#2e7d32"))
        if rng.random() < 0.7:  # a jack-o'-lantern face
            dark = _hex("#3e1a00", 0.95)
            for x in (-R * 0.32, R * 0.32):
                SeasonDecor._poly(s, [(x - R * 0.13, -R * 0.05), (x + R * 0.13, -R * 0.05), (x, -R * 0.3)], dark)
            SeasonDecor._poly(s, [(-R * 0.45, R * 0.2), (-R * 0.25, R * 0.35), (-R * 0.1, R * 0.22), (0, R * 0.38),
                                  (R * 0.1, R * 0.22), (R * 0.25, R * 0.35), (R * 0.45, R * 0.2), (0, R * 0.55)], dark)

    @staticmethod
    def _bat(s, rng, size, color, _sea):
        R = size / 2
        c = _hex("#1a1a1a", 0.95)
        SeasonDecor._poly(s, [(0, -R * 0.2), (R * 0.3, -R * 0.45), (R * 0.55, -R * 0.2), (R, -R * 0.4),
                              (R * 0.85, R * 0.05), (R * 0.65, -R * 0.02), (R * 0.5, R * 0.2), (R * 0.3, R * 0.05),
                              (0, R * 0.3), (-R * 0.3, R * 0.05), (-R * 0.5, R * 0.2), (-R * 0.65, -R * 0.02),
                              (-R * 0.85, R * 0.05), (-R, -R * 0.4), (-R * 0.55, -R * 0.2), (-R * 0.3, -R * 0.45)], c)
        for x in (-R * 0.08, R * 0.08):
            SeasonDecor._dot(s, x, -R * 0.12, R * 0.04, _hex("#ffea00"))

    @staticmethod
    def _ghost(s, rng, size, color, _sea):
        R = size / 2
        pb = Gsk.PathBuilder.new()
        pb.move_to(-R * 0.6, R * 0.7)
        pb.line_to(-R * 0.6, -R * 0.2)
        pb.cubic_to(-R * 0.6, -R * 0.95, R * 0.6, -R * 0.95, R * 0.6, -R * 0.2)
        pb.line_to(R * 0.6, R * 0.7)
        for i in range(4):
            x = R * 0.6 - (i + 0.5) * R * 0.3
            pb.quad_to(x, R * (0.45 if i % 2 == 0 else 0.95), x - R * 0.15, R * 0.7)
        pb.close()
        SeasonDecor._fill(s, pb, _hex("#fafafa", 0.93))
        for x in (-R * 0.22, R * 0.22):
            SeasonDecor._oval(s, x, -R * 0.2, R * 0.09, R * 0.14, _hex("#212121"))

    @staticmethod
    def _spider(s, rng, size, color, _sea):
        c = _hex("#111111", 0.95)
        R = size / 2
        for side in (-1, 1):
            for k in range(4):
                a = math.radians(-50 + k * 33)
                SeasonDecor._line(s, [(0, 0), (side * R * 0.5 * math.cos(a), R * 0.5 * math.sin(a) - R * 0.15),
                                      (side * R * 0.85 * math.cos(a), R * 0.85 * math.sin(a) + R * 0.1)],
                                  max(0.8, size * 0.03), c)
        SeasonDecor._dot(s, 0, 0, R * 0.22, c)
        SeasonDecor._dot(s, 0, -R * 0.25, R * 0.13, c)
        SeasonDecor._line(s, [(0, -R * 0.35), (0, -R * 1.6)], max(0.6, size * 0.012), _hex("#e0e0e0", 0.8))

    @staticmethod
    def _web(s, rng, size, color, _sea):
        R, c, w = size * 0.6, _hex("#eeeeee", 0.85), max(0.6, size * 0.015)
        spokes = 8
        for i in range(spokes):
            a = 2 * math.pi * i / spokes
            SeasonDecor._line(s, [(0, 0), (R * math.cos(a), R * math.sin(a))], w, c)
        for k in (0.3, 0.55, 0.8, 1.0):
            pts = [(R * k * math.cos(2 * math.pi * i / spokes), R * k * math.sin(2 * math.pi * i / spokes))
                   for i in range(spokes + 1)]
            SeasonDecor._line(s, pts, w, c)

    @staticmethod
    def _candle(s, rng, size, color, _sea):
        W, H = size * 0.18, size * 0.6
        SeasonDecor._poly(s, [(-W / 2, -H * 0.3), (W / 2, -H * 0.3), (W / 2, H * 0.7), (-W / 2, H * 0.7)], color)
        s.save()
        s.translate(Graphene.Point().init(0, -H * 0.5))
        SeasonDecor._flame(s, rng, size * 0.35, color, _sea)
        s.restore()

    @staticmethod
    def _skull(s, rng, size, color, _sea):
        R = size / 2
        white = _hex("#fafafa", 0.95)
        SeasonDecor._dot(s, 0, -R * 0.1, R * 0.55, white)
        SeasonDecor._poly(s, [(-R * 0.3, R * 0.3), (R * 0.3, R * 0.3), (R * 0.25, R * 0.62), (-R * 0.25, R * 0.62)], white)
        for x in (-R * 0.22, R * 0.22):
            SeasonDecor._dot(s, x, -R * 0.12, R * 0.15, _hex("#212121"))
            SeasonDecor._dot(s, x, -R * 0.12, R * 0.06, _hex(rng.choice(("#e040fb", "#ff9100", "#40c4ff"))))
        SeasonDecor._poly(s, [(0, R * 0.05), (-R * 0.07, R * 0.2), (R * 0.07, R * 0.2)], _hex("#212121"))
        for k in range(-2, 3):
            SeasonDecor._line(s, [(k * R * 0.1, R * 0.38), (k * R * 0.1, R * 0.58)], max(0.6, size * 0.015), _hex("#424242"))

    @staticmethod
    def _gravelight(s, rng, size, color, _sea):
        """A Czech grave lamp: red glass cup with a gold lid and a glow inside."""
        D = SeasonDecor
        W, H = size * 0.36, size * 0.5
        glass = _hex(rng.choice(("#d50000", "#b71c1c", "#e53935", "#ff6f00")), 0.93)
        D._dot(s, 0, 0, size * 0.42, _hex("#ffab00", 0.22))
        D._poly(s, [(-W * 0.4, -H * 0.4), (W * 0.4, -H * 0.4), (W * 0.5, H * 0.5), (-W * 0.5, H * 0.5)], glass)
        D._dot(s, 0, H * 0.05, W * 0.28, _hex("#ffe082", 0.85))
        D._dot(s, 0, H * 0.05, W * 0.12, _hex("#ffffff", 0.95))
        gold = _hex("#ffc107", 0.97)
        D._poly(s, [(-W * 0.5, -H * 0.4), (W * 0.5, -H * 0.4), (W * 0.3, -H * 0.6), (-W * 0.3, -H * 0.6)], gold)
        D._poly(s, [(-W * 0.08, -H * 0.6), (W * 0.08, -H * 0.6), (W * 0.05, -H * 0.78), (-W * 0.05, -H * 0.78)], gold)
        D._poly(s, [(-W * 0.55, H * 0.5), (W * 0.55, H * 0.5), (W * 0.55, H * 0.6), (-W * 0.55, H * 0.6)], gold)

    @staticmethod
    def _wreath(s, rng, size, color, _sea):
        """A spruce wreath with a few red berries and a ribbon."""
        D = SeasonDecor
        R = size * 0.38
        for k in range(16):
            a = 2 * math.pi * k / 16 + rng.uniform(-0.1, 0.1)
            D._dot(s, R * math.cos(a), R * math.sin(a), size * 0.11,
                   _hex(rng.choice(("#1b5e20", "#2e7d32", "#33691e")), 0.95))
        for k in range(5):
            a = rng.uniform(0, 2 * math.pi)
            D._dot(s, R * math.cos(a), R * math.sin(a), size * 0.045, _hex("#e53935", 0.97))
        ribbon = _hex(rng.choice(("#c62828", "#6a1b9a", "#f5f5f5")), 0.95)
        D._poly(s, [(0, R), (-size * 0.12, R + size * 0.3), (-size * 0.04, R + size * 0.28)], ribbon)
        D._poly(s, [(0, R), (size * 0.12, R + size * 0.3), (size * 0.04, R + size * 0.28)], ribbon)
        D._dot(s, 0, R, size * 0.06, ribbon)

    @staticmethod
    def _mum(s, rng, size, color, _sea):
        """A chrysanthemum: rings of thin petals."""
        D = SeasonDecor
        R = size / 2
        c = _hex(rng.choice(("#fff8e1", "#ffd54f", "#ffb300", "#f48fb1", "#ce93d8")), 0.95)
        for ring, (r, n) in enumerate(((R * 0.85, 18), (R * 0.6, 14), (R * 0.38, 10))):
            col = c if ring % 2 == 0 else D._darker(c, 0.85)
            for k in range(n):
                a = 2 * math.pi * (k + ring * 0.5) / n
                D._line(s, [(0, 0), (r * math.cos(a), r * math.sin(a))], max(0.8, size * 0.07), col)
        D._dot(s, 0, 0, R * 0.16, _hex("#f9a825", 0.97))

    @staticmethod
    def _marigold(s, rng, size, color, _sea):
        """Cempasúchil: a round, ruffled orange pompom."""
        D = SeasonDecor
        R = size / 2
        base = rng.choice(("#ff9100", "#ff6d00", "#ffab00", "#ffc400"))
        for ring, (r, n) in enumerate(((R * 0.75, 12), (R * 0.52, 9), (R * 0.3, 6))):
            for k in range(n):
                a = 2 * math.pi * (k + ring * 0.37) / n
                D._dot(s, r * math.cos(a), r * math.sin(a), R * (0.3 - ring * 0.05),
                       D._darker(_hex(base, 0.96), 1.0 - 0.12 * ((k + ring) % 2)))
        D._dot(s, 0, 0, R * 0.18, _hex("#e65100", 0.97))

    @staticmethod
    def _calavera(s, rng, size, color, _sea):
        """A sugar skull: white, flower eyes, painted forehead and stitched smile."""
        D = SeasonDecor
        R = size / 2
        white = _hex("#fafafa", 0.97)
        D._dot(s, 0, -R * 0.1, R * 0.6, white)
        D._poly(s, [(-R * 0.35, R * 0.25), (R * 0.35, R * 0.25), (R * 0.27, R * 0.68), (-R * 0.27, R * 0.68)], white)
        bright = ("#e040fb", "#00e5ff", "#ff1744", "#76ff03", "#ffd600", "#ff9100")
        for x in (-R * 0.24, R * 0.24):
            ring = _hex(rng.choice(bright), 0.97)
            for k in range(6):
                a = 2 * math.pi * k / 6
                D._dot(s, x + R * 0.12 * math.cos(a), -R * 0.1 + R * 0.12 * math.sin(a), R * 0.07, ring)
            D._dot(s, x, -R * 0.1, R * 0.11, _hex("#212121"))
        D._poly(s, [(0, R * 0.08), (-R * 0.06, R * 0.22), (R * 0.06, R * 0.22)], _hex("#212121"))
        D._dot(s, 0, -R * 0.5, R * 0.1, _hex(rng.choice(bright), 0.97))  # forehead flower
        for a in (-0.6, 0.6):
            D._dot(s, R * 0.25 * math.sin(a) * 1.4, -R * 0.45, R * 0.05, _hex(rng.choice(bright), 0.95))
        smile = _hex("#212121", 0.9)
        D._line(s, [(-R * 0.28, R * 0.45), (R * 0.28, R * 0.45)], max(0.6, size * 0.02), smile)
        for k in range(-3, 4):
            D._line(s, [(k * R * 0.08, R * 0.38), (k * R * 0.08, R * 0.52)], max(0.6, size * 0.015), smile)

    @staticmethod
    def _papel(s, rng, size, color, _sea):
        """Papel picado: a bright tissue flag on a string, with cut-out holes."""
        D = SeasonDecor
        W, H = size * 0.6, size * 0.7
        paper = _hex(rng.choice(("#e040fb", "#00e5ff", "#ff1744", "#76ff03", "#ffd600", "#ff9100")), 0.9)
        D._line(s, [(-W * 0.8, -H / 2), (W * 0.8, -H / 2)], max(0.7, size * 0.02), _hex("#fafafa", 0.8))
        pts = [(-W / 2, -H / 2), (W / 2, -H / 2), (W / 2, H * 0.35)]
        for k in range(5, -1, -1):  # zigzag bottom edge
            pts.append((-W / 2 + W * k / 5 - W / 10, H * (0.5 if k % 2 else 0.35)) if 0 < k < 5 else
                       (-W / 2 + W * k / 5, H * 0.35))
        D._poly(s, pts, paper)
        cut = D._darker(paper, 0.55)
        D._dot(s, 0, -H * 0.05, W * 0.14, cut)
        for k in range(6):
            a = 2 * math.pi * k / 6
            D._dot(s, W * 0.25 * math.cos(a), -H * 0.05 + W * 0.25 * math.sin(a), W * 0.05, cut)

    @staticmethod
    def _tree(s, rng, size, color, _sea):
        green = _hex(rng.choice(("#2e7d32", "#388e3c", "#1b5e20")), 0.97)
        for k, (y, wd) in enumerate(((-0.3, 0.3), (-0.05, 0.4), (0.2, 0.5))):
            SeasonDecor._poly(s, [(0, (y - 0.3) * size), (wd * size, y * size + size * 0.08),
                                  (-wd * size, y * size + size * 0.08)], green)
        SeasonDecor._poly(s, [(-size * 0.06, size * 0.28), (size * 0.06, size * 0.28), (size * 0.06, size * 0.42),
                              (-size * 0.06, size * 0.42)], _hex("#5d4037"))
        s.save()
        s.translate(Graphene.Point().init(0, -size * 0.6))
        SeasonDecor._star(s, rng, size * 0.25, _hex("#ffd54f"), _sea)
        s.restore()
        for _ in range(5):
            SeasonDecor._dot(s, rng.uniform(-0.3, 0.3) * size, rng.uniform(-0.3, 0.25) * size, size * 0.04,
                             _hex(rng.choice(("#e53935", "#ffd54f", "#1e88e5", "#ffffff"))))

    @staticmethod
    def _gift(s, rng, size, color, _sea):
        W = size * 0.55
        box = _hex(rng.choice(("#e53935", "#1e88e5", "#43a047", "#8e24aa")), 0.97)
        ribbon = _hex(rng.choice(("#ffd54f", "#ffffff")), 0.97)
        SeasonDecor._poly(s, [(-W / 2, -W / 2), (W / 2, -W / 2), (W / 2, W / 2), (-W / 2, W / 2)], box)
        SeasonDecor._line(s, [(0, -W / 2), (0, W / 2)], max(1.0, size * 0.07), ribbon)
        SeasonDecor._line(s, [(-W / 2, 0), (W / 2, 0)], max(1.0, size * 0.07), ribbon)
        SeasonDecor._oval(s, -W * 0.18, -W * 0.62, W * 0.18, W * 0.1, ribbon)
        SeasonDecor._oval(s, W * 0.18, -W * 0.62, W * 0.18, W * 0.1, ribbon)

    @staticmethod
    def _bauble(s, rng, size, color, _sea):
        R = size * 0.3
        SeasonDecor._dot(s, 0, 0, R, color)
        SeasonDecor._dot(s, -R * 0.35, -R * 0.35, R * 0.22, _hex("#ffffff", 0.6))
        SeasonDecor._poly(s, [(-R * 0.25, -R * 1.2), (R * 0.25, -R * 1.2), (R * 0.25, -R * 0.9), (-R * 0.25, -R * 0.9)],
                          _hex("#bdbdbd"))

    @staticmethod
    def _lantern(s, rng, size, color, _sea):
        SeasonDecor._oval(s, 0, 0, size * 0.3, size * 0.36, _hex("#d50000", 0.97))
        for y in (-size * 0.36, size * 0.36):
            SeasonDecor._poly(s, [(-size * 0.14, y - size * 0.04), (size * 0.14, y - size * 0.04),
                                  (size * 0.14, y + size * 0.04), (-size * 0.14, y + size * 0.04)], _hex("#ffd54f"))
        for x in (-size * 0.12, 0, size * 0.12):
            SeasonDecor._line(s, [(x, -size * 0.32), (x * 1.3, 0), (x, size * 0.32)], max(0.6, size * 0.015),
                              _hex("#ff8a80", 0.8))
        SeasonDecor._line(s, [(0, size * 0.4), (0, size * 0.62)], max(0.8, size * 0.03), _hex("#ffd54f"))

    @staticmethod
    def _coin(s, rng, size, color, _sea):
        R = size * 0.28
        pb = Gsk.PathBuilder.new()
        pb.add_circle(Graphene.Point().init(0, 0), R)
        pb.add_rect(Graphene.Rect().init(-R * 0.25, -R * 0.25, R * 0.5, R * 0.5))
        s.append_fill(pb.to_path(), Gsk.FillRule.EVEN_ODD, _hex("#ffc107", 0.97))

    @staticmethod
    def _diya(s, rng, size, color, _sea):
        pb = Gsk.PathBuilder.new()
        pb.move_to(-size * 0.35, 0)
        pb.quad_to(0, size * 0.45, size * 0.35, 0)
        pb.quad_to(size * 0.42, -size * 0.04, size * 0.45, -size * 0.1)
        pb.line_to(-size * 0.35, 0)
        pb.close()
        SeasonDecor._fill(s, pb, _hex("#e65100", 0.97))
        s.save()
        s.translate(Graphene.Point().init(size * 0.3, -size * 0.28))
        SeasonDecor._flame(s, rng, size * 0.4, color, _sea)
        s.restore()

    @staticmethod
    def _david(s, rng, size, color, _sea):
        R, w = size / 2, max(1.2, size * 0.06)
        for rot in (0, math.pi):
            pts = [(R * math.cos(rot - math.pi / 2 + 2 * math.pi * i / 3), R * math.sin(rot - math.pi / 2 + 2 * math.pi * i / 3))
                   for i in range(4)]
            SeasonDecor._line(s, pts, w, color)



class ClockView(Gtk.Widget):
    """Above the volume slider, right of the cover, one under another: the weekday, the day, the month, the
    year and the time (lowest, just above the slider). Each as wide as the room (their letters' outlines, not
    the fonts' line boxes), all made smaller together when they wouldn't fit its height; never touching.
    In the Beat Radio fonts, white with a shadow like the song info's."""

    SIG_SKIP = ("colon",)  # (blinking every second: not a change the glass pictures anew for)

    FONTS = (("Anton", Pango.Weight.NORMAL),) + (("Oswald", Pango.Weight.SEMIBOLD),) * 7
    # top to bottom: weekday, day, month, the special day's name (one to three rows, when there is one), year, time
    ORDER = (1, 2, 3, 5, 6, 7, 4, 0)
    DAY_LINE = 2  # the day number: a one-digit day as big as a two-digit one
    SEED = 0  # its lines' decorations and colors (another view: others)
    GAP = 0.05  # between the lines, of the width

    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts = ("",) * len(self.FONTS)
        self.order = []  # the lines drawn (with text), top to bottom: their indexes in texts
        self.sx = 1.0  # the letters stretched wider (the names: to fill their room)
        self.room = (0, 0)
        self.lays = None
        self.colon = True  # the time's colon blinks: a second on, a second off

    def _layout(self, which: int, pt: float, text: str = None):
        family, weight = self.FONTS[which]
        lay = self.create_pango_layout(self.texts[which] if text is None else text)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new(family))
        attrs.insert(Pango.attr_weight_new(weight))
        attrs.insert(Pango.attr_size_new(int(pt * Pango.SCALE)))
        lay.set_attributes(attrs)
        return lay, lay.get_pixel_extents()[0]

    def _fit(self, which: int, w: float, h: float):
        _, ink = self._layout(which, 100)  # measured at 100 pt, then scaled to fit
        width = ink.width
        if which == self.DAY_LINE:  # the day: a one-digit day (1, 2…) as big as a two-digit one, not a giant
            width = max(width, self._layout(which, 100, "28")[1].width)
        return self._layout(which, 100 * min(w / max(1, width), h / max(1, ink.height)) * 0.97)

    def set_room(self, w: int, h: int, texts: tuple) -> None:
        if (w, h) == self.room and texts == self.texts and self.lays:
            return
        self.room, self.texts = (w, h), texts
        self.order = [i for i in self.ORDER if i < len(texts) and texts[i]]
        gap = max(4.0, w * self.GAP)
        lays = [self._fit(i, w, 10000) for i in self.order]
        gaps = (len(lays) - 1) * gap
        total = sum(ink.height for _, ink in lays) + gaps
        # the whole room filled, as the song info: every line as wide as the room, all of them squeezed lower
        # (too tall) or stretched taller (too short) together until they fill its height
        self.sy = h / total if total > 0 else 1.0
        self.lays = (lays, gap)
        self.set_size_request(w, int(total * self.sy) + 1)
        self.queue_draw()

    def do_snapshot(self, snap) -> None:
        if not self.lays:
            return
        lays, gap = self.lays
        w = self.room[0]
        tl = lays[-1][0]
        i = self.texts[0].find(":")
        if i >= 0 and self.order[-1] == 0:  # off: drawn see-through (the time keeps its place)
            attrs = tl.get_attributes().copy()
            alpha = Pango.attr_foreground_alpha_new(65535 if self.colon else 1)
            alpha.start_index, alpha.end_index = i, i + 1
            attrs.change(alpha)
            tl.set_attributes(attrs)
        shadow = Gsk.Shadow()
        shadow.color, shadow.dx, shadow.dy, shadow.radius = _rgba(0, 0, 0, 0.8), -1, 2, 3
        snap.push_shadow([shadow])
        y, sx, sy = 0.0, self.sx, getattr(self, "sy", 1.0)
        for (lay, ink), color in zip(lays, self._colors()):
            snap.save()
            snap.translate(Graphene.Point().init(0, y * sy))
            snap.scale(sx, sy)
            snap.translate(Graphene.Point().init((w / sx - ink.width) / 2 - ink.x, -ink.y))
            snap.append_layout(lay, color)
            snap.restore()
            y += ink.height + gap
        snap.pop()

    def lines_width(self) -> float:
        """How wide its lines are drawn (the widest)."""
        return max((ink.width * self.sx for _, ink in self.lays[0]), default=0.0) if self.lays else 0.0

    def _colors(self) -> list:
        """Each line's color from the month's palette, by which line it is (not where it lands)."""
        cols = day_colors(len(self.ORDER) + 1)
        return [cols[(i + self.SEED) % len(cols)] for i in self.order]


class FitText(Gtk.Widget):
    """One line of small text (the status) as big as fits its room, no bigger than MAX_PT, in the middle of it."""

    MAX_PT = 13.0

    def __init__(self, **kw):
        super().__init__(**kw)
        self.text, self.room, self.lay = "", (0, 0), None

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text, self.lay = text, None
            self.set_visible(bool(text) and self.get_visible())
            self._fit()

    def set_room(self, w: int, h: int) -> None:
        if (w, h) != self.room or self.lay is None:
            self.room = (w, h)
            self._fit()

    def _fit(self) -> None:
        w, h = self.room
        if not self.text or w < 1 or h < 1:
            return
        lay = self.create_pango_layout(self.text)

        def size(pt):
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_size_new(int(pt * Pango.SCALE)))
            lay.set_attributes(attrs)
            return lay.get_pixel_extents()[1]
        ink = size(100)
        pt = min(self.MAX_PT, 100 * 0.97 * min(w / max(1, ink.width), h / max(1, ink.height)))
        for _ in range(4):  # small sizes aren't quite in proportion (hinting): measured again until it fits
            ink = size(pt)
            if ink.width <= w and ink.height <= h:
                break
            pt *= 0.97 * min(w / max(1, ink.width), h / max(1, ink.height))
        self.lay = lay
        self.set_size_request(w, h)
        self.queue_draw()

    def do_snapshot(self, snap) -> None:
        if not self.lay:
            return
        w, h = self.room
        ink = self.lay.get_pixel_extents()[1]
        shadow = Gsk.Shadow()
        shadow.color, shadow.dx, shadow.dy, shadow.radius = _rgba(0, 0, 0, 0.8), -1, 1, 2
        snap.push_shadow([shadow])
        snap.translate(Graphene.Point().init((w - ink.width) / 2 - ink.x, (h - ink.height) / 2 - ink.y))
        snap.append_layout(self.lay, _rgba(255, 255, 255, 0.8))
        snap.pop()


class NamesView(ClockView):
    """Whose name day it is ("FELIX", "BRONISLAV / BRONISLAVA"), one line in the clock's letters, as wide as its
    room: below the volume slider and the quality pill, above the screen's bottom edge."""

    FONTS = (("Oswald", Pango.Weight.SEMIBOLD),)
    ORDER = (0,)
    DAY_LINE = None
    SEED = 7

    def set_room(self, w: int, h: int, texts: tuple, width: float = None) -> None:
        """The whole room filled, as the song info and the date: as tall as the room, then stretched as wide
        (or, a long name, as wide, then stretched as tall). `width`: as wide as that (the date's lines above),
        in the middle of the room."""
        width = min(w, width or w)
        if (w, h, width) == self.room + (getattr(self, "_width", None),) and texts == self.texts and self.lays:
            return
        self.room, self.texts, self._width = (w, h), texts, width
        self.order = [0] if texts and texts[0] else []
        if not self.order:
            self.lays = None
            return
        ink = self._layout(0, 100)[1]
        lay, ink = self._layout(0, 100 * min(width / max(1, ink.width), h / max(1, ink.height)))
        self.sx, self.sy = width / max(1, ink.width), h / max(1, ink.height)
        self.lays = ([(lay, ink)], 0.0)
        self.set_size_request(w, h)
        self.queue_draw()


class SnowFall:
    """The season's motifs (a special day's instead, Advent's half and half; in a season's last month more and
    more of the next one's) falling slowly like snow, each turning a little its own way, behind the date and
    time, the top buttons, the quality pill, the volume slider and the names: right of the cover, the window's
    whole height. On a layer of its own (SnowView) over the shading, under the buttons and the slider; drawn
    into the glass menus and panels too, magnified like their middle, every frame."""

    SPRITES = 24  # different pictures (each drawn once a day into a texture)

    def __init__(self, widget):
        self.widget = widget  # draws it, ticks it
        self.area = None  # (x, y, w, h) in its coordinates: where the window shows them (right of the cover)
        self.field = None  # where they fall (the whole window): the glass shows them all over, not just there
        self.flakes: list = []
        self.sprites: list = []  # [(texture, box px, upright)]
        self._key = None
        self._tick = None
        self._last = None
        self.viewers: set = set()  # glass showing it (menus, panels): redrawn with it
        self._frame = 0  # frames moved
        self._node = (None, [])  # (key, [(x, y, reach, node, alpha)]): as they are this frame, built once a frame
        self._tex_nodes = (None, [])  # (sprites, a render node of each)

    def set_area(self, area, field=None) -> None:
        field = (field or area) if area else None
        if area != self.area or field != self.field:
            old, self.area, self.field = self.field, area, field
            if not area:
                self.flakes = []
            elif not old or abs(old[2] * old[3] - field[2] * field[3]) > 0.15 * max(1, old[2] * old[3]):
                self.flakes = []  # much bigger or smaller: sown again
            self.update()

    def update(self) -> None:
        """Ticking only when it moves: on, animations on, with room."""
        want = bool(EFFECTS["season"] and EFFECTS["anim"] and self.area)
        if want and self._tick is None:
            self._last = None
            self._tick = self.widget.add_tick_callback(self._step)
        elif not want and self._tick is not None:
            self.widget.remove_tick_callback(self._tick)
            self._tick = None
        self.widget.queue_draw()

    def _base(self) -> float:
        x, y, w, h = self.area
        return max(18.0, min(60.0, min(w, h) * 0.11))

    def _make_sprites(self, base: float, k: float) -> None:
        t = time.localtime()
        key = (t.tm_year, t.tm_yday, round(base), k)
        if key == self._key:
            return
        self._key, self.sprites = key, []
        season, nxt, share = season_today()
        days = holidays_today()
        rng = random.Random(t.tm_year * 1000 + t.tm_yday)
        native = self.widget.get_native()
        if not native:
            return
        size = base * 1.3  # the biggest flakes' size: drawn sharp at it, smaller ones scaled down
        box = size * 1.7  # pieces reach a little past their size (stems, rays)
        for i in range(self.SPRITES):
            # the season's always (in its last month more and more of the next one's); a special day's (Advent's,
            # Halloween's…) as many again: half and half
            if days and i % 2 == 0:
                sea = days[(i // 2) % len(days)]
            else:
                j = i // 2 if days else i
                sea = nxt if j < round((self.SPRITES // (2 if days else 1)) * share) else season
            kind = rng.choice(SEASON_KINDS[sea])
            color = _hex(rng.choice(SEASON_COLORS[sea]), rng.uniform(0.85, 0.97))
            s = Gtk.Snapshot()
            s.scale(k, k)
            s.translate(Graphene.Point().init(box / 2, box / 2))
            # a faint glow round each: brown leaves stand out on a brown cover too
            glow = Gsk.Shadow()
            glow.color, glow.dx, glow.dy, glow.radius = _rgba(255, 248, 225, 0.9), 0, 0, max(3.0, size * 0.1)
            near = Gsk.Shadow()  # (and a tighter one: the edge lit, not only a haze)
            near.color, near.dx, near.dy, near.radius = _rgba(255, 250, 235, 0.7), 0, 0, max(1.5, size * 0.03)
            s.push_shadow([glow, near])
            getattr(SeasonDecor, "_" + kind)(s, rng, size, color, sea)
            s.pop()
            node = s.to_node()
            if node is None:
                continue
            tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, box * k, box * k))
            self.sprites.append((tex, box, kind in UPRIGHT))

    def _sow(self, base: float, anywhere: bool) -> dict:
        x, y, w, h = self.field
        rng = random
        near = rng.random()  # 0 far (small, slow, paler) .. 1 near
        scale = 0.5 + 0.55 * near
        return {
            "x": x + rng.uniform(0, w), "y": y + (rng.uniform(0, h) if anywhere else -base * 1.2),
            "vy": base * (0.9 + 1.1 * near) * rng.uniform(0.8, 1.2),  # px a second: about a flake a second
            "sway": base * rng.uniform(0.3, 0.9), "freq": rng.uniform(0.12, 0.3), "phase": rng.uniform(0, 6.3),
            "angle": rng.uniform(0, 360), "spin": rng.choice((-1, 1)) * rng.uniform(8, 40),
            "sprite": rng.randrange(max(1, len(self.sprites))), "scale": scale,
            "alpha": 0.55 + 0.4 * near, "t": rng.uniform(0, 100),
        }

    def _count(self, base: float) -> int:
        x, y, w, h = self.area
        here = max(10, min(100, int(w * h / (base * 2.1) ** 2)))  # as many right of the cover as before
        return min(400, int(here * self.field[2] * self.field[3] / max(1.0, w * h)))  # the same all over

    def _step(self, widget, clock) -> bool:
        now = clock.get_frame_time() / 1e6
        dt = 0.0 if self._last is None else min(0.1, now - self._last)
        self._last = now
        if not self.area:
            return True
        x, y, w, h = self.field
        base = self._base()
        self._frame += 1
        for f in self.flakes:
            f["t"] += dt
            f["y"] += f["vy"] * dt
            f["angle"] += f["spin"] * dt
            if f["y"] > y + h + base * 1.2:  # gone below: a new one from above
                f.update(self._sow(base, False))
        widget.queue_draw()  # every frame the screen shows (60 a second and more)
        GlassCard.own_redraw += 1  # (the glass redrawn for them: not a change on the window to picture anew)
        try:
            for v in self.viewers:  # the glass it's seen through
                if v.get_mapped() and getattr(v, "reveal", 1.0) > 0:
                    v.queue_draw()
        finally:
            GlassCard.own_redraw -= 1
        return True

    def draw(self, snap, dx: float = 0.0, dy: float = 0.0, view=None, fade=None) -> None:
        """Into `snap`, shifted by (dx, dy) (the drawing widget isn't where the area's coordinates start): those
        inside `view` (x, y, w, h; the area right of the cover by default; the glass: what it sees), each
        fainter by fade(x, y) 0..1 if given (the glass's edges)."""
        if not EFFECTS["season"] or not self.area:
            return
        x, y, w, h = view or self.area
        if w < 20 or h < 20:
            return
        native = self.widget.get_native()
        surf = native.get_surface() if native else None
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.widget.get_scale_factor()
        base = self._base()
        self._make_sprites(base, k)
        if not self.sprites:
            return
        want = self._count(base)
        while len(self.flakes) < want:
            self.flakes.append(self._sow(base, True))
        del self.flakes[want:]
        nodes = []
        Opacity, Turn = Gsk.OpacityNode.new, Gsk.TransformNode.new
        for fx, fy, r, tex, tr, alpha in self._all():  # those inside it only: the renderer has less to do
            if fx + r < x or fx - r > x + w or fy + r < y or fy - r > y + h:
                continue
            a = alpha * fade(fx, fy) if fade else alpha
            if a > 0.02:  # (faded, then turned: the other way round each would be drawn apart first)
                nodes.append(Turn(Opacity(tex, a), tr))
        if not nodes:
            return
        snap.save()
        snap.translate(Graphene.Point().init(dx, dy))
        snap.push_clip(Graphene.Rect().init(x, y, w, h))
        group = Gsk.ContainerNode.new(nodes)
        # as one group (drawn once, then put in place): turned pictures under the glass's round corners would
        # otherwise each be drawn apart first, the renderer can't round them off turned (3x the work)
        snap.append_node(Gsk.OpacityNode.new(group, 0.999) if fade else group)
        snap.pop()
        snap.restore()

    def _all(self) -> list:
        """[(x, y, reach, picture node, transform, alpha)]: each as it is now (moved, turned, sized), worked out once a
        frame, whoever draws them (the window, and every glass up to five times: its middle and four mirrored
        edges), each picking those it shows: at 165 Hz too (a frame is 6 ms)."""
        key = (self._frame, len(self.flakes), id(self.sprites), self.field)
        if self._node[0] == key:
            return self._node[1]
        sprites, n = self.sprites, len(self.sprites)
        if self._tex_nodes[0] is not sprites:
            self._tex_nodes = (sprites, [Gsk.TextureNode.new(t, Graphene.Rect().init(-b / 2, -b / 2, b, b))
                                         for t, b, _ in sprites])
        tex_nodes = self._tex_nodes[1]
        P, T, sin = Graphene.Point, Gsk.Transform, math.sin
        out = []
        for f in self.flakes:
            i = f["sprite"] % n
            _, box, upright = sprites[i]
            fx = f["x"] + f["sway"] * sin(f["t"] * f["freq"] * 6.283 + f["phase"])
            # pictures that stand (palms, candles, pumpkins): rocking a little, the rest turning round slowly
            turn = 18 * sin(f["t"] * f["freq"] * 4 + f["phase"]) if upright else f["angle"]
            tr = T.new().translate(P().init(fx, f["y"])).rotate(turn).scale(f["scale"], f["scale"])
            out.append((fx, f["y"], box * f["scale"] * 0.75, tex_nodes[i], tr, f["alpha"]))
        self._node = (key, out)
        return out

    def bytes(self) -> int:
        return sum(tex_bytes(t) for t, _, _ in self.sprites)


class Shade(Gtk.Widget):
    """The shading under the song info and the buttons (darker towards the bottom): a layer of its own, just
    the panel's size, so the falling motifs lie over it and under what's on the panel."""

    def __init__(self, panel, **kw):
        super().__init__(css_classes=["scrim-shade"], can_target=False, **kw)
        self.panel = panel

    def do_measure(self, orientation, for_size):
        return self.panel.measure(orientation, for_size)


class SnowView(Gtk.Widget):
    """The falling motifs' own layer (over the shading, under the buttons and the letters), the whole window;
    left out of what the glass menus picture of the window: they draw the motifs into their glass themselves,
    every frame (a picture of the window taken off the graphics card each frame couldn't keep up)."""

    def __init__(self, win, **kw):
        super().__init__(can_target=False, hexpand=True, vexpand=True, **kw)
        self.win = win
        self.snow = SnowFall(self)

    def do_snapshot(self, snap) -> None:
        ok, p = self.compute_point(self.win, Graphene.Point().init(0, 0))
        if ok:
            self.snow.draw(snap, -p.x, -p.y)


class InfoStack(Gtk.Widget):
    """The song info in a wide window, like the clock: the title, the artist, the album and the track info,
    each broken into lines and every line as wide as the room (beside the cover, below the logo, above the
    buttons), all made smaller together when they wouldn't fit its height; never touching."""

    # each part: its font, its weight, its share of the size (as the labels' sizes), its alpha
    PARTS = (("Anton", Pango.Weight.NORMAL, 24, 1.0), ("Oswald", Pango.Weight.SEMIBOLD, 16.5, 1.0),
             ("Oswald", Pango.Weight.NORMAL, 13, 0.8), ("Oswald", Pango.Weight.NORMAL, 10.5, 1.0))
    GAP_IN, GAP_OUT = 0.04, 0.07  # between a part's lines, between the parts (of the width)
    STRETCH = 2.0  # at most this much taller than their font makes them, to fill the room's height
    ROW_MAX = 1.6  # a row no taller than this many times its part's usual line height

    def __init__(self, **kw):
        super().__init__(**kw)
        self.key = None
        self.lines = []  # [(layout, ink, y, alpha)]
        self.beat = True
        self.sy = 1.0  # the lines' vertical stretch (filling the room's height)
        self.flash = 0.0  # a new or updated song: a bright glow fading out (like the labels' flash)
        self.flash_anim = None

    def blink(self) -> None:
        if self.flash_anim:
            self.flash_anim.pause()
        target = Adw.CallbackAnimationTarget.new(lambda v: (setattr(self, "flash", v), self.queue_draw()))
        self.flash_anim = Adw.TimedAnimation.new(self, 1.0, 0.0, 900, target)
        self.flash_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim_hold(True)
        GLib.timeout_add(950, lambda: (anim_hold(False), False)[1])
        self.flash_anim.play()

    def _lay(self, text: str, part: int, pt: float):
        family, weight = self.PARTS[part][:2]
        lay = self.create_pango_layout(text)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new(family if self.beat else "Sans"))
        attrs.insert(Pango.attr_weight_new(weight if self.beat else Pango.Weight.BOLD if part < 2 else weight))
        attrs.insert(Pango.attr_size_new(int(pt * Pango.SCALE)))
        lay.set_attributes(attrs)
        return lay, lay.get_pixel_extents()[0]

    @staticmethod
    def _split(widths: list, space: float, n: int) -> list:
        """The words split into `n` lines as even as can be (the widest line as narrow as it gets)."""
        k = len(widths)
        best = {}

        def go(i, m):  # words i.. in m lines: (widest line, breaks)
            if (i, m) in best:
                return best[(i, m)]
            if m == 1:
                res = (sum(widths[i:]) + space * (k - i - 1), [])
            else:
                res = None
                for j in range(i + 1, k - m + 2):
                    line = sum(widths[i:j]) + space * (j - i - 1)
                    rest, cuts = go(j, m - 1)
                    cand = (max(line, rest), [j] + cuts)
                    if res is None or cand[0] < res[0]:
                        res = cand
            best[(i, m)] = res
            return res
        return go(0, n)[1]

    def _part_lines(self, text: str, part: int, w: float, target: float) -> list:
        """A part's text in lines as wide as `w`, as many as bring each line's height nearest `target`."""
        words = text.split()
        if not words:
            return []
        widths = [self._lay(x, part, 100)[1].width for x in words]
        space = self._lay("a b", part, 100)[1].width - self._lay("ab", part, 100)[1].width
        best = None
        for n in range(1, min(len(words), 8) + 1):
            cuts = [0] + self._split(widths, space, n) + [len(words)]
            rows = [" ".join(words[a:b]) for a, b in zip(cuts, cuts[1:])]
            hs = []
            for row in rows:
                ink = self._lay(row, part, 100)[1]
                hs.append(100 * w / max(1, ink.width) * ink.height / 100)
            err = sum(abs(h - target) for h in hs) / len(hs)
            if best is None or err < best[0]:
                best = (err, rows)
        return best[1]

    def set_room(self, w: int, h: int, texts: tuple, beat: bool) -> None:
        key = (w, h, texts, beat)
        if key == self.key:
            return
        if self.key and self.key[2] != texts and any(texts):
            self.blink()  # the song changed, or its lines did: they flash
        self.key, self.beat = key, beat
        texts = [t.upper() if i == 1 and beat else t for i, t in enumerate(texts)]
        gin, gout = w * self.GAP_IN, w * self.GAP_OUT

        def build(scale: float, shrink: float = 1.0):
            out, y = [], 0.0
            for part, text in enumerate(texts):
                # the track info's parts ("Poprvé vydáno …", "Nahráno v …") each from a line of its own, without
                # the dot between them
                segs = [x.strip() for x in text.split(" · ")] if part == 3 else [text]
                rows = [row for seg in segs if seg for row in self._part_lines(seg, part, w, self.PARTS[part][2] * scale)]
                if not rows:
                    continue
                if out:
                    y += gout - gin
                for row in rows:
                    lay, ink = self._lay(row, part, 100)
                    pt = 100 * w * shrink / max(1, ink.width) * 0.98
                    # a short row ("1", "Help!") as wide as the room would be a giant: no taller than a bit
                    # over its part's usual line (as the clock's one-digit day), narrower instead
                    cap = self.PARTS[part][2] * scale * shrink * self.ROW_MAX
                    capped = pt > 100 * cap / max(1, ink.height)
                    pt = min(pt, 100 * cap / max(1, ink.height))
                    lay, ink = self._lay(row, part, pt)
                    x = (w - ink.width) / 2 if capped else 0.0  # (a capped one in the middle)
                    out.append((lay, ink, y, self.PARTS[part][3], x))
                    y += ink.height + gin
            return out, y - gin if out else 0.0

        # the biggest size whose lines fit the height (each line as wide as the room)
        lo, hi = 0.3, 12.0
        lines, tall = build(lo)
        for _ in range(14):
            mid = (lo + hi) / 2
            cand, t = build(mid)
            if t <= h:
                lo, lines, tall = mid, cand, t
            else:
                hi = mid
        if tall > h:  # even the smallest too tall: all smaller together, as much
            lines, tall = build(lo, max(0.1, h / max(1, tall)))
        # shorter than the room: the lines stretched taller (as wide as before) until they fill it
        self.sy = min(self.STRETCH, h / tall) if 0 < tall < h else 1.0
        self.lines = lines
        self.set_size_request(w, int(tall * self.sy) + 1)
        self.queue_draw()

    def do_snapshot(self, snap) -> None:
        shadow = Gsk.Shadow()
        shadow.color, shadow.dx, shadow.dy, shadow.radius = _rgba(0, 0, 0, 0.8), -1, 2, 3
        shadows = [shadow]
        if self.flash > 0.01:  # the glow, as the labels' flash: 14 and 34 px of white
            for radius, a in ((14, 1.0), (34, 0.8)):
                glow = Gsk.Shadow()
                glow.color, glow.dx, glow.dy, glow.radius = _rgba(255, 255, 255, a * self.flash), 0, 0, radius
                shadows.append(glow)
        snap.push_shadow(shadows)
        for lay, ink, y, alpha, x in self.lines:
            snap.save()
            snap.translate(Graphene.Point().init(x, y * self.sy))
            snap.scale(1.0, self.sy)
            snap.translate(Graphene.Point().init(-ink.x, -ink.y))
            snap.append_layout(lay, _rgba(255, 255, 255, alpha))
            snap.restore()
        snap.pop()


class PinnedScroll(Gtk.ScrolledWindow):
    """A scrolled list with a widget pinned over it (`pin`, in an overlay): what scrolls behind the pin isn't
    drawn, so the pin stays clearly readable."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.pin = None

    def do_snapshot(self, snap) -> None:
        w, h = self.get_width(), self.get_height()
        ok, b = (self.pin.compute_bounds(self) if self.pin is not None and self.pin.get_visible()
                 else (False, None))
        if not ok or b.get_height() <= 0 or h <= 0:
            Gtk.ScrolledWindow.do_snapshot(self, snap)
            return
        y0, y1, soft = b.get_y() - 6, b.get_y() + b.get_height() + 6, 10  # the pin's band, a bit wider, soft
        snap.push_mask(Gsk.MaskMode.ALPHA)
        snap.append_linear_gradient(
            Graphene.Rect().init(0, 0, w, h), Graphene.Point().init(0, 0), Graphene.Point().init(0, h),
            [_stop(max(0.0, (y0 - soft) / h), 0, 0, 0, 1), _stop(max(0.0, y0 / h), 0, 0, 0, 0),
             _stop(min(1.0, y1 / h), 0, 0, 0, 0), _stop(min(1.0, (y1 + soft) / h), 0, 0, 0, 1)])
        snap.pop()
        Gtk.ScrolledWindow.do_snapshot(self, snap)
        snap.pop()


class Puddle(GlassCard):
    """The 'i' button's water: glass hanging from it (a neck, then a tall body), flowing down when opened
    (its front edge round like a drop) and drawn back up into the button when closed. `reveal` 0..1."""

    def __init__(self, behind: list, neck: bool = False, **kw):
        super().__init__(behind, **kw)
        self.neck = neck
        self.reveal = 0.0
        self.frame = not neck  # the neck is water between the two: no edge of its own
        self.win = None  # see_window(): what's on the window seen through it too, live
        self._layers = None  # (version, texture, rect in the window)
        self._tex_version = None

    def see_window(self, win) -> None:
        """Like the quality menu: through it not only the cover but what's on the window, as it moves: the
        date and time, the falling motifs, the song info and the buttons (the top buttons: mirrored)."""
        self.win, self._tex_version = win, 0
        self._paints = []
        for w in (win.scrim, win.clock, win.names, win.status_view):
            paint = Gtk.WidgetPaintable.new(w)
            paint.connect("invalidate-contents", self._layers_changed)
            self._paints.append((w, paint))
        win.snow.viewers.add(self)

    def _snow_origin(self):
        return self.win.compute_point(self, Graphene.Point().init(0, 0)) if self.win is not None else (False, None)

    def _live_layers(self) -> list:
        return [w for w, _ in self._paints] if self.win is not None else []

    def _layers_changed(self, *_) -> None:
        if self.reveal > 0 and self.get_mapped():
            self._live()

    def _layers_texture(self):
        """What's on the window around it (the glass's bend reaches a little past its edges), without the
        shading under the buttons (the glass has that copied in already) and the falling motifs (drawn over
        the lens every frame), at the screen's scale."""
        win, native = self.win, self.get_native()
        ok, b = self.compute_bounds(win)
        if not ok or not native:
            return None
        W, H = win.get_width(), win.get_height()
        mx, my = b.get_width() * 0.4 + 20, b.get_height() * 0.4 + 20
        x0, y0 = max(0.0, b.get_x() - mx), max(0.0, b.get_y() - my)
        x1, y1 = min(W, b.get_x() + b.get_width() + mx), min(H, b.get_y() + b.get_height() + my)
        if x1 - x0 < 1 or y1 - y0 < 1:
            return None
        surf = native.get_surface()
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.get_scale_factor()
        s = Gtk.Snapshot()
        s.scale(k, k)
        s.translate(Graphene.Point().init(-x0, -y0))
        draw_layers(s, self._paints, win, self.mirrors)  # (the mirrored buttons: not also as they are)
        node = s.to_node()
        if node is None:
            return None
        rect = Graphene.Rect().init(x0, y0, x1 - x0, y1 - y0)
        return native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, (x1 - x0) * k, (y1 - y0) * k)), rect

    def _behind(self, snap, w: float, h: float, sx: float, sy: float) -> None:
        GlassCard._behind(self, snap, w, h, sx, sy)
        lay = self._layers
        if not lay or not lay[1] or getattr(self, "_mirroring", False):  # (the edges' mirrors: kept, not live)
            return
        ok, p = self.win.compute_point(self, Graphene.Point().init(0, 0))
        if not ok:
            return
        tex, r = lay[1]
        snap.save()
        snap.translate(Graphene.Point().init(w / 2, h / 2))
        snap.scale(sx, sy)
        snap.translate(Graphene.Point().init(-w / 2, -h / 2))
        snap.append_texture(tex, Graphene.Rect().init(p.x + r.get_x(), p.y + r.get_y(), r.get_width(), r.get_height()))
        snap.restore()

    def _neck_path(self, w: float, h: float):
        """As wide as the button at the top, drawn in at the waist, spreading out into the body below:
        water squeezing from one into the other."""
        f = w * 0.18  # the button's width = w - 2 f
        waist = w * 0.22
        pb = Gsk.PathBuilder.new()
        pb.move_to(f, 0)
        pb.line_to(w - f, 0)
        pb.cubic_to(w - f, h * 0.35, w / 2 + waist / 2, h * 0.3, w / 2 + waist / 2, h * 0.55)
        pb.cubic_to(w / 2 + waist / 2, h * 0.85, w, h * 0.8, w, h)
        pb.line_to(0, h)
        pb.cubic_to(0, h * 0.8, w / 2 - waist / 2, h * 0.85, w / 2 - waist / 2, h * 0.55)
        pb.cubic_to(w / 2 - waist / 2, h * 0.3, f, h * 0.35, f, 0)
        pb.close()
        return pb.to_path()

    def do_snapshot(self, snap) -> None:
        r = self.reveal
        if r <= 0:
            self._layers = None
            return
        if self.win is not None and (self._layers is None or self._layers[0] != self._tex_version):
            self._layers = (self._tex_version, self._layers_texture())
        w, h = self.get_width(), self.get_height()
        if self.neck:
            snap.push_clip(Graphene.Rect().init(0, 0, w, max(1.0, h * r)))
            snap.push_fill(self._neck_path(w, h), Gsk.FillRule.WINDING)
            GlassCard.do_snapshot(self, snap)
            snap.pop()
            snap.pop()
            return
        vis = max(1.0, h * r)
        bottom = self.radius if r >= 1 else min(w / 2, vis, max(self.radius, (1 - r) * w * 0.6))  # a drop's front
        rr = Gsk.RoundedRect()
        top = Graphene.Size().init(0, 0) if self.neck else Graphene.Size().init(self.radius, self.radius)
        bot = Graphene.Size().init(bottom, bottom)
        rr.init(Graphene.Rect().init(0, 0, w, vis), top, top, bot, bot)
        if self.depth:  # the show card's shadow (outside the clip below)
            rim = min(self.radius * self.RIM, h / 2, w / 2)
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.55), 0, rim * 0.3, 0, rim * 0.9)
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.35), 0, 2, 0, 4)
        snap.push_rounded_clip(rr)
        GlassCard.do_snapshot(self, snap)
        snap.pop()


class Water:
    """A glass button's water: a neck from the button, then a tall body with what it holds; it flows down
    when opened and is drawn back up into the button when closed. Over the cover when there's room (from its
    left edge to the screen's right one, down to above the volume slider), else under the button; touching
    none."""

    def __init__(self, win, tile, body, overlay):
        self.win, self.tile, self.body = win, tile, body
        behind = [(win.backdrop, 0.45), (win.cover, 0.0)]
        self.neck = Puddle(behind, neck=True, halign=Gtk.Align.START, valign=Gtk.Align.START)
        for g in (self.neck, body):
            # exactly the show card's glass (as HHC's buttons have it): only its size differs; its curved edge
            # as deep as the card's (spread over a big panel it would look thin)
            g.scrim, g.sharp_bars = win.scrim, True  # what's under it sharp, the bars beside the cover too
            g.echoes = True  # the picture's echoes along its edges
            if g is body:  # big glass: its edge a thick rounded wall (the bend there), lit and shaded like one
                g.curve, g.wall, g.reach, g.shine = 3.0, 2.8, 0.9, True  # a strong bend at the rim: thick
                g.warp_content = False  # what's inside stays flat (the glass behind it bends)
            g.add_css_class("puddle")
            g.set_visible(False)
            win.backdrop.watchers.append(g)
            win.cover.watchers.append(g)
        self.neck.depth = False  # water between the two: no shadow box of its own
        body.edge_mirror = True  # its top and bottom edges mirror what lies beyond them, upside down
        # the top buttons and the quality pill (AAC 128 kbps) seen through its glass upside down, like KVALITA's
        body.mirrors = [*win.head_glass, win.quality_pill]
        body.see_window(win)  # and the date and time, the falling motifs, the song info: live
        # its own scrolling stays its own: not the window's volume
        block = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        block.connect("scroll", lambda *_: True)
        body.add_controller(block)
        overlay.add_overlay(self.neck)
        overlay.add_overlay(body)
        self.open = False
        self.v = 0.0
        self.anim = None

    def contains(self, w) -> bool:
        while w is not None:
            if w in (self.body, self.neck, self.tile):
                return True
            w = w.get_parent()
        return False

    def place(self) -> None:
        win = self.win
        ok, b = self.tile.compute_bounds(win)
        if not ok:
            return
        W, H = win.get_width(), win.get_height()
        gap = 10
        bottom = b.get_y() + b.get_height()
        # the neck: from under the button (overlapping it) into the body (overlapping it too)
        fil = b.get_width() * 0.18 / 0.64  # so its top is the button's width
        self.neck.set_margin_start(int(b.get_x() - fil))
        self.neck.set_margin_top(int(bottom - 6))
        self.neck.set_size_request(int(b.get_width() + 2 * fil), gap + 6 + 12)
        self.neck.radius = 0  # its shape is its path (no rounded box cutting it)
        top = bottom + gap
        width = int(max(300, min(W - 20, W * 0.45)))
        height = int(max(220, min(H * 0.75, H - top - 20)))
        cover = win.cover_rect()
        ok_v, vb = win.vol.compute_bounds(win)
        ok_p, pb = win.quality_pill.compute_bounds(win)
        if ok_v and ok_p and pb.get_y() < vb.get_y():  # the pill stands taller than the slider: above it too
            vb = pb  # (just past the glass's bottom edge, its rim mirrors it upside down)
        beside = bool(cover and W - cover[0] - 2 * gap >= 150)
        if beside and ok_v and vb.get_y() - gap - top >= 200:
            height = int(vb.get_y() - gap - top)
        if ok_v and vb.get_y() > top:  # never down onto the volume slider
            height = int(min(height, vb.get_y() - gap - top))
        self.body.set_margin_top(int(top))
        self.body.set_margin_end(gap)
        if beside:  # from the cover's left edge to the screen's right one (its own width would be its text's)
            self.body.set_halign(Gtk.Align.FILL)
            self.body.set_margin_start(int(cover[0] + gap))
            self.body.set_size_request(-1, height)
        else:
            self.body.set_halign(Gtk.Align.END)
            self.body.set_margin_start(0)
            self.body.set_size_request(width, height)
        self.body.radius = 28 * max(1.0, win._scale)  # the card's corners
        # what's inside stays within the rectangle joining the corners' circle centers: the radius on every side
        r = int(self.body.radius)
        kids = []
        c = self.body.get_first_child()
        while c:
            kids.append(c)
            c = c.get_next_sibling()
        for i, c in enumerate(kids):
            c.set_margin_start(r)
            c.set_margin_end(r)
            c.set_margin_top(r if i == 0 else 0)
            c.set_margin_bottom(r if i == len(kids) - 1 else 0)

    def show(self) -> None:
        self.open = True
        self.win.app.enhancer.set_quiet(True)
        self.tile.remove_css_class("pumping")
        self.place()
        for g in (self.neck, self.body):
            g.set_visible(True)
        self._flow(1.0, 750, Adw.Easing.EASE_OUT_BACK)

    def hide(self) -> None:
        if self.open:
            self.open = False
            self._flow(0.0, 420, Adw.Easing.EASE_IN_CUBIC)

    def _flow(self, to: float, ms: int, easing) -> None:
        """The water flowing down (neck first, then the body) or drawn back up."""
        if self.anim:
            self.anim.pause()

        def set_v(v):
            self.v = v
            self.neck.reveal = min(1.0, max(0.0, v / 0.25))
            self.body.reveal = min(1.05, max(0.0, (v - 0.15) / 0.85))
            self.neck.queue_draw()
            self.body.queue_draw()

        anim = self.anim = Adw.TimedAnimation.new(self.body, self.v, to, ms, Adw.CallbackAnimationTarget.new(set_v))
        anim.set_easing(easing)
        anim_hold(True)

        def done(*_):
            anim_hold(False)
            if to == 0.0 and not self.open:
                for g in (self.neck, self.body):
                    g.set_visible(False)
                if not any(w.open for w in self.win.waters):  # all closed: the full enhancing goes on
                    self.win.app.enhancer.set_quiet(False)
                    self.win.app.enhance_small_full()
        anim.connect("done", done)
        anim.play()


def _rgba(r: int, g: int, b: int, a: float) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r / 255, g / 255, b / 255, a
    return c


def _stop(offset: float, r: int, g: int, b: int, a: float) -> Gsk.ColorStop:
    s = Gsk.ColorStop()
    s.offset, s.color = offset, _rgba(r, g, b, a)
    return s


# Text style "Beat Radio" (the default; a switch in the gear menu): fonts like the station logo's, shipped in fonts/
# (both SIL Open Font License): Anton, heavy and condensed like "BEAT", for the
# song and the show; Oswald, condensed like "RADIO", for the rest.
FONTS_DIR = os.path.join(HERE, "fonts")
FONT_FILES = ("Anton-Regular.ttf", "Oswald.ttf")
BEAT_CSS = """
.song, .show-title { font-family: "Anton"; font-weight: 400; letter-spacing: 0.02em; }
.artist { font-family: "Oswald"; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
.album, .facts, .status, .show-sub, .show-next { font-family: "Oswald"; font-weight: 400; letter-spacing: 0.02em; }
.on-air, .day-head { font-family: "Oswald"; font-weight: 700; letter-spacing: 0.08em; }
.gear-menu label { font-family: "Oswald"; font-weight: 400; letter-spacing: 0.03em; }
.gear-menu row .title { font-weight: 600; }
"""


def load_fonts() -> bool:
    """The Beat Radio fonts for this app only (Pango 1.56+), else copied to
    ~/.local/share/fonts, where they're found from the next start."""
    files = [os.path.join(FONTS_DIR, f) for f in FONT_FILES]
    if not all(os.path.exists(f) for f in files):
        return False
    try:
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        fm = PangoCairo.FontMap.get_default()
        if hasattr(fm, "add_font_file"):
            return all(fm.add_font_file(f) for f in files)
    except (ValueError, ImportError, GLib.Error):
        pass
    dest = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "fonts", "radio-beat")
    try:
        os.makedirs(dest, exist_ok=True)
        for f in files:
            shutil.copyfile(f, os.path.join(dest, os.path.basename(f)))
    except OSError:
        pass
    return False


# Sizes at the window's first size (BASE_SIZE); a larger window or full screen
# scales them all by how much bigger it is (the smaller of width and height).
BASE_SIZE = (440, 600)
TEXT_PT = {"song": 24, "artist": 16.5, "album": 13, "facts": 10.5, "status": 10, "on-air": 9,
           "show-title": 13.5, "show-sub": 10.5, "show-next": 10.5, "pic": 96}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rádio Beat", default_width=420, default_height=620)
        self.app = app
        self.set_icon_name(APP_ID)
        self._scale = 1.0
        self._glass_buttons: list = []  # extra glass buttons (back, HRÁLO's): sized with the window
        self._fit = 1.0  # the song info's text: as big as fits its place (smaller or up to 1.6x)
        self._fit_queued = False
        self._scale_pending = False
        self._fit_bad = 9.0  # the smallest fit seen not fitting (a line more wraps): not grown back to
        self._fit_for = None  # (width, height, title) that was measured for
        self.scale_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.scale_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.look_css = Gtk.CssProvider()  # text style and background shade (gear menu)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.look_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)

        # The cover, whole and as big as it fits (at the top in a tall window,
        # centred in a wide one), over a blurred copy of itself that fills the
        # rest of the window, whatever its shape; everything else lies on top.
        # A new cover morphs in (CoverView).
        self.backdrop = CoverView("cover", soft=BACKDROP_BLUR, dim=BACKDROP_DIM)
        self.backdrop.add_css_class("backdrop")
        self.cover = CoverView("contain")
        bg = Gtk.Overlay(child=self.backdrop, hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        bg.add_css_class("cover-bg")
        bg.add_overlay(self.cover)
        overlay = Gtk.Overlay(child=bg)

        # Above everything: the station logo at the left, the gear menu and a red close (to the tray) at the
        # right. No other window buttons: a double click anywhere switches full screen and back.
        header = self.header = Adw.HeaderBar(show_title=False, valign=Gtk.Align.START,
                                             show_start_title_buttons=False, show_end_title_buttons=False)
        header.add_css_class("overlay-header")
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Zavřít (hraje dál v oznamovací oblasti)")
        close.connect("clicked", lambda *_: self.close())
        close.add_css_class("close-button")
        # Close and the gear in glass tiles like the dishes below (close in red glass).
        self.head_glass = []
        for child, color, cls in ((close, (227, 5, 26, 0.6), "glass-disc"), (self._settings_button(), None, "glass-head")):
            tile = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, hexpand=False)
            tile.add_css_class(cls)
            tile.mag, tile.concave, tile.depth, tile.color = 1.3, True, False, color
            tile.drop = True  # the letters' shadow: 3D
            tile.set_homogeneous(True)
            tile.append(child)
            self.backdrop.watchers.append(tile)
            self.cover.watchers.append(tile)
            self.head_glass.append(tile)
            hold_while_hovered(tile)
            if cls == "glass-head":
                self.gear_tile = tile
                # The gear pumps while the mouse is on it, not while its menu is open.
                gear = child
                m = Gtk.EventControllerMotion()
                m.connect("enter", lambda *_, t=tile: self.gear_water.open or t.add_css_class("pumping"))
                m.connect("leave", lambda *_, t=tile: t.remove_css_class("pumping"))
                gear.add_controller(m)
            header.pack_end(tile)
        # 'i': the programme and curiosities, in glass water flowing down from it (the puddle)
        info_btn = Gtk.Button(label="i", tooltip_text="Program a zajímavosti")
        info_btn.add_css_class("info-button")
        info_btn.connect("clicked", lambda *_: self.toggle_puddle())
        tile = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, hexpand=False)
        tile.add_css_class("glass-head")  # like the gear's tile (no pumping)
        tile.mag, tile.concave, tile.depth = 1.3, True, False
        tile.drop = True  # the letters' shadow: 3D
        tile.set_homogeneous(True)
        tile.append(info_btn)
        self.backdrop.watchers.append(tile)
        self.cover.watchers.append(tile)
        self.head_glass.append(tile)
        hold_while_hovered(tile)
        header.pack_end(tile)
        self.info_tile = tile
        # back, left of 'i' (a tile like it): a HRÁLO song playing or on the screen → the radio again
        back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Zpět na rádio (Esc)")
        back_btn.connect("clicked", lambda *_: self.app.back_to_radio())
        tile = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, hexpand=False,
                         visible=False)
        tile.add_css_class("glass-head")
        tile.mag, tile.concave, tile.depth = 1.3, True, False
        tile.drop = True  # the letters' shadow: 3D
        tile.set_homogeneous(True)
        tile.append(back_btn)
        self.backdrop.watchers.append(tile)
        self.cover.watchers.append(tile)
        self.head_glass.append(tile)
        hold_while_hovered(tile)
        header.pack_end(tile)
        self.back_disc = tile
        self.logo = FlagLogo(34)  # the (enhanced) logo itself, drawn sharp; waves under the mouse
        self.logo.add_css_class("top-logo")
        header.pack_start(self.logo)
        self.connect("notify::fullscreened", self.on_fullscreened)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        # Double click anywhere (buttons and the volume slider keep their clicks): full screen and back.
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", self.on_double)
        self.add_controller(dbl)
        # A right click anywhere: mute, and again sound (like the speaker button).
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", lambda *_: app.toggle_mute())
        self.add_controller(right)
        # The mouse wheel anywhere: volume (the slider itself, and the open menu, scroll their own way).
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                          | Gtk.EventControllerScrollFlags.DISCRETE)
        wheel.connect("scroll", self.on_wheel)
        self.add_controller(wheel)

        scrim = self.scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.END,
                                     margin_top=0)
        # under it, its shading, then the falling motifs (so they're seen over the shading, under the buttons)
        self.shade_view = Shade(scrim, valign=Gtk.Align.END)
        self.snow_view = SnowView(self)
        self.snow = self.snow_view.snow
        scrim.add_css_class("scrim")
        inner = self.inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=90,
                        margin_bottom=22, margin_start=22, margin_end=22)
        scrim.append(inner)
        # What's on: the show now (picture, name, host) and the next one.
        # Only as wide as it needs, at the right (the song info's right edge): less of the cover hidden.
        self.show_card = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], spacing=12, margin_bottom=10,
                                   halign=Gtk.Align.END)
        self.show_card.scrim = scrim
        self.show_card.breathe()
        for tile in self.head_glass:
            tile.scrim = scrim
        self.backdrop.watchers.append(self.show_card)
        self.cover.watchers.append(self.show_card)
        self.show_card.add_css_class("show-card")
        self.show_pic = Gtk.Image(pixel_size=TEXT_PT["pic"], valign=Gtk.Align.CENTER)
        self.show_pic.add_css_class("show-pic")
        self.show_pic.set_overflow(Gtk.Overflow.HIDDEN)
        self.show_card.append(self.show_pic)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        top = Gtk.Box(spacing=8, margin_bottom=4)
        badge = self.badge = Gtk.Label(label="ON AIR", valign=Gtk.Align.START)
        badge.add_css_class("on-air")
        # Text widths capped (in characters, so they grow with the text in full
        # screen): a wrapping label would otherwise ask for its whole text's width.
        self.show_title = Gtk.Label(xalign=0, wrap=True, lines=3, ellipsize=3, max_width_chars=30, hexpand=True)
        self.show_title.add_css_class("show-title")
        top.append(badge)
        top.append(self.show_title)
        self.show_sub = Gtk.Label(xalign=0, wrap=True, lines=5, ellipsize=3, max_width_chars=38)
        self.show_sub.add_css_class("show-sub")
        self.show_next = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=3, max_width_chars=38)
        self.show_next.add_css_class("show-next")
        # The show's text, or the typed one (it scrolls up as it's typed when there's more than 5 lines).
        self.show_sub.set_valign(Gtk.Align.START)
        self.sub_scroll = Gtk.ScrolledWindow(child=self.show_sub, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                             vscrollbar_policy=Gtk.PolicyType.EXTERNAL, propagate_natural_height=True)
        for w in (top, self.sub_scroll, self.show_next):
            text.append(w)
        self.show_card.append(text)
        self.show_card_text = text
        self.show_card.set_visible(False)
        # The song and the show: side by side in a wide window (the show at the
        # right, level with the song), the show above the song in a narrow one.
        self.info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True,
                            valign=Gtk.Align.START)
        self.info_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.info_row.append(self.show_card)
        self.info_row.append(self.info)
        self._wide = False
        inner.append(self.info_row)

        self.song = Gtk.Label(wrap=True, xalign=0)
        self.song.add_css_class("song")
        self.artist = Gtk.Label(wrap=True, xalign=0)
        self.artist.add_css_class("artist")
        self.album = Gtk.Label(wrap=True, xalign=0)
        self.album.add_css_class("album")
        self.facts = Gtk.Label(wrap=True, xalign=0)  # first release, where it was recorded
        self.facts.add_css_class("facts")
        for w in (self.song, self.artist, self.album, self.facts):
            w.set_wrap_mode(Pango.WrapMode.WORD)  # between words only: a word too wide shrinks the text instead
            self.info.append(w)
        # in a wide window: the same lines, each as wide as the room (the labels above keep the text)
        self.info_stack = InfoStack(halign=Gtk.Align.START, can_target=False, visible=False)
        self.info.append(self.info_stack)

        controls = self.controls = Gtk.Box(spacing=12, margin_top=12)
        self.play_btn = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER)
        self.play_btn.add_css_class("round")
        self.play_btn.add_css_class("play-button")
        self.play_btn.connect("clicked", lambda *_: app.toggle_pause())
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Zastavit a ukončit",
                          valign=Gtk.Align.CENTER)
        stop.add_css_class("round")
        stop.add_css_class("stop-button")
        stop.connect("clicked", lambda *_: app.back_to_radio() if app.track_view else app.quit_all())
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_round_digits(1)  # follows the mouse pixel by pixel, not in whole percents
        self.vol.set_hexpand(True)
        self.vol.add_css_class("vol-slider")
        hold_while_hovered(self.vol)
        self.vol.set_valign(Gtk.Align.CENTER)
        self.vol.set_value(app.volume_now())
        self.vol.connect("value-changed", lambda s: app.set_volume(s.get_value()))
        vol_icon = Gtk.Image(icon_name="audio-volume-high-symbolic", halign=Gtk.Align.CENTER,
                             valign=Gtk.Align.CENTER)
        vol_icon.add_css_class("vol-icon")
        self.vol_icon = vol_icon
        # Pause, stop and the speaker in round dishes of glass, magnifying sharply in the middle, bent at the
        # rim, lit like a dish; pause in red glass; each a little smaller than the one before.
        self.discs = []
        for child, color, margin in ((self.play_btn, (227, 5, 26, 0.55), 0), (stop, None, 0), (vol_icon, None, 0)):
            disc = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER,
                             halign=Gtk.Align.CENTER, margin_start=margin)
            disc.add_css_class("glass-disc")
            disc.scrim, disc.mag, disc.concave, disc.depth, disc.color = scrim, 1.3, True, False, color
            disc.drop = True  # the letters' shadow: 3D
            # (the bars beside the cover blurred under it, as around it: sharp they made a hard edge in it)
            disc.set_hexpand(False)  # only the slider takes the row's spare width
            disc.set_homogeneous(True)  # the button or icon fills the dish, centred
            disc.append(child)
            self.backdrop.watchers.append(disc)
            self.cover.watchers.append(disc)
            self.discs.append(disc)
            hold_while_hovered(disc)
            if child is vol_icon:  # a click on the speaker mutes, and again lets it play
                click = Gtk.GestureClick()
                click.connect("pressed", lambda g, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                                        app.toggle_mute()))
                disc.add_controller(click)
                disc.set_tooltip_text("Ztlumit")
            controls.append(disc)
        controls.append(self.vol)
        # The status (paused, reconnecting, a HRÁLO song's quality) above the buttons, only when it says something:
        # the buttons sit as far from the bottom edge as from the left one, the quality pill at the row's end.
        # (its text; drawn by status_view under the volume slider, centred, fitted to the room there)
        self.status = Gtk.Label(visible=False)
        self.status.connect("notify::label", lambda l, _: (self.status_view.set_text(l.get_label()),
                                                            GLib.idle_add(self._place_clock)))
        controls.append(self._quality_pill(scrim))
        inner.append(controls)
        overlay.add_overlay(self.shade_view)
        overlay.add_overlay(self.snow_view)
        overlay.add_overlay(scrim)
        # The time and date (in Czech, capitals: "8:50" over "PÁTEK 30. ŘÍJNA 2026") at the bottom right: below
        # the volume slider, right of the cover, against the screen's right edge, filling that room.
        self.clock = ClockView(halign=Gtk.Align.END, valign=Gtk.Align.END)
        # A click on the date and time: the season's decorations off, and on again (the gear's switch follows)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                                   n == 1 and app.set_look(fx_season=not EFFECTS["season"])))
        self.clock.add_controller(click)
        self.clock.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.clock.set_visible(False)
        overlay.add_overlay(self.clock)
        # the status (paused, reconnecting, a HRÁLO song's quality and video) under the volume slider, centred
        self.status_view = FitText(halign=Gtk.Align.START, valign=Gtk.Align.END, can_target=False, visible=False)
        overlay.add_overlay(self.status_view)
        # whose name day it is, below the volume slider and the quality pill (a click: the same as on the date)
        self.names = NamesView(halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                                   n == 1 and app.set_look(fx_season=not EFFECTS["season"])))
        self.names.add_controller(click)
        self.names.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        overlay.add_overlay(self.names)
        # the mouse on the date, time or names: what day it is, in a glass box beside them
        # its text: headlines in red pills (like ON AIR), each followed by its text (labels made per showing)
        self.day_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=10, margin_bottom=10,
                                margin_start=14, margin_end=14, valign=Gtk.Align.CENTER, vexpand=True, hexpand=True)
        self._day_labels = []
        dglass = MenuGlass(self, [(self.backdrop, 0.45), (self.cover, 0.0)])
        dglass.radius = 16.0
        # over the cover, filling it: a thick edge like the panels', mirroring what lies beyond each edge
        dglass.curve, dglass.wall, dglass.reach, dglass.shine = 3.0, 2.8, 0.9, True
        dglass.edge_mirror = True
        # (its natural size the box's, not the text's on one line: the popover takes its child's natural size)
        self.day_fit = Gtk.ScrolledWindow(child=self.day_text, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True)
        dglass.append(self.day_fit)
        self.day_pop = Gtk.Popover(child=dglass, has_arrow=False, position=Gtk.PositionType.LEFT, autohide=False,
                                   can_focus=False)
        self.day_pop.add_css_class("glass-menu")
        self.day_pop.set_can_target(False)
        self.day_pop.set_parent(self.clock)
        self._day_on = None
        for view in (self.clock, self.names):
            m = Gtk.EventControllerMotion()
            m.connect("enter", lambda *_, v=view: self._day_hover(v, True))
            m.connect("leave", lambda *_, v=view: self._day_hover(v, False))
            view.add_controller(m)
        GLib.timeout_add_seconds(1, self._tick_clock)
        self._tick_clock()
        self._make_puddle(overlay, scrim)
        overlay.add_overlay(header)  # last: on top of the text

        self.set_content(overlay)
        self._apply_scale()
        self.apply_look()
        self.connect("close-request", self.on_close)
        self.card_turn = "show"
        self._turn_content = None  # what the text turn shows, chosen when it starts
        self._minute_at = time.monotonic()  # when the programme's turn last began
        self._type_id = 0
        self._type_ms = self.TYPE_MS
        self._typing_text = ""
        self._turn_id = 0  # (the card's turns: the puddle shows its info now)
        self.minimised = False
        self.connect("notify::is-active", lambda w, _: w.is_active() and setattr(w, "minimised", False))
        self.refresh()

    def _settings_button(self) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)  # its room: Water.place
        group = Adw.PreferencesGroup()
        auto = Adw.SwitchRow(title="Automatické spuštění",
                             subtitle="Po přihlášení hraje jako ikona v oznamovací oblasti", subtitle_lines=0)
        auto.set_active(autostart_on())
        group.add(auto)
        auto_full = Adw.SwitchRow(title="Spustit v celoobrazovkovém režimu",
                                  subtitle="Po přihlášení otevře rovnou okno přes celou obrazovku, ne jen ikonu",
                                  subtitle_lines=0, sensitive=autostart_on())
        auto_full.set_active(self.app.settings.get("autostart_full", False))
        auto_full.connect("notify::active", lambda r, _: self.app.set_look(autostart_full=r.get_active()))
        auto.connect("notify::active", lambda r, _: (set_autostart(r.get_active()),
                                                      auto_full.set_sensitive(r.get_active())))
        group.add(auto_full)
        self.card_row = Adw.SwitchRow(title="Ovládat hlasitost zvukové karty", subtitle=self.app.card_text(),
                                      subtitle_lines=0, sensitive=self.app.card.ok)
        self.card_row.set_active(self.app.card_on())
        self.card_row.connect("notify::active", lambda r, _: self.app.set_card_volume(r.get_active()))
        group.add(self.card_row)
        self.picture_row = Adw.ActionRow(title="Poslední vylepšený obrázek", subtitle=self.app.picture_subtitle(), subtitle_lines=0)
        group.add(self.picture_row)
        self.recognise_row = Adw.SwitchRow(title="Rozpoznávat skladby (testování)", subtitle_lines=0)
        self.recognise_row.set_active(self.app.settings.get("recognise", False))
        self.recognise_row.connect("notify::active", lambda r, _: self.app.set_recognise(r.get_active()))
        group.add(self.recognise_row)
        if is_kde():
            near = Adw.SwitchRow(title="Otevírat v rohu", subtitle="Vpravo dole, na liště u hodin (i po celé obrazovce)")
            near.set_active(self.app.settings.get("tray_popup", True))
            near.connect("notify::active", lambda r, _: self.app.set_tray_popup(r.get_active()))
            group.add(near)
        self.enhance_row = Adw.SwitchRow(title="Vylepšovat obrázky", subtitle=self.app.enhance_status(),
                                         subtitle_lines=0)
        self.enhance_row.set_active(self.app.settings.get("enhance", True))
        self.enhance_row.connect("notify::active", lambda r, _: self.app.set_enhance(r.get_active()))
        group.add(self.enhance_row)
        self.share_row = Adw.SwitchRow(title="Sdílet obrázky v této síti", subtitle_lines=0,
                                       subtitle=GLib.markup_escape_text(self.app.share_status()))
        self.share_row.set_active(self.app.settings.get("share", True))
        self.share_row.connect("notify::active", lambda r, _: self.app.set_share(r.get_active()))
        group.add(self.share_row)
        self.npu_row = Adw.SwitchRow(title="Použít NPU (testování)", subtitle=self.app.enhancer.npu_text,
                                     subtitle_lines=0)
        self.npu_row.set_active(self.app.settings.get("npu", False))
        self.npu_row.connect("notify::active", lambda r, _: self.app.set_npu(r.get_active()))
        group.add(self.npu_row)
        dialect = Adw.SwitchRow(title="Ostravština",
                                subtitle="Texty o skladbě, interpretovi a pořadu po ostravsku "
                                         "(krátké samohlásky, fest, fajne); vypnuto: spisovně", subtitle_lines=0)
        dialect.set_active(self.app.settings.get("ostrava", True))
        dialect.connect("notify::active", lambda r, _: (self.app.set_look(ostrava=r.get_active()), self.app.update()))
        group.add(dialect)
        style = Adw.SwitchRow(title="Písmo jako Beat Radio",
                              subtitle="Písma jako v logu stanice (Anton, Oswald); vypnuto: běžná"
                              if self.app.fonts_ok else "Vypnuto: chybí jeho písma (složka fonts/)",
                              subtitle_lines=0, sensitive=self.app.fonts_ok)
        style.set_active(self.app.fonts_ok and self.app.settings.get("font", "beat") == "beat")
        style.connect("notify::active", lambda r, _: self.app.set_look(font="beat" if r.get_active() else "default"))
        group.add(style)
        self.fx_rows = {}
        for key, title in (("mirror", "Odrazy písma na okrajích skla"), ("shadows", "Stíny tlačítek (3D)"),
                           ("anim", "Animace"), ("season", "Padající ozdoby podle ročního období"),
                           ("gpu", "Sklo počítané grafickou kartou (shader)")):
            row = Adw.SwitchRow(title=title, subtitle_lines=0)
            row.set_active(self.app.settings.get("fx_" + key, True))
            row.connect("notify::active", lambda r, _, k=key: self.app.set_look(**{"fx_" + k: r.get_active()}))
            group.add(row)
            self.fx_rows[key] = row
        shade = Adw.ActionRow(title="Ztmavení pozadí",
                              subtitle="Jak moc tmavé plochy za textem překryjí obal", subtitle_lines=0)
        self.shade_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.shade_scale.set_value(self.app.settings.get("shade", 100))
        self.shade_scale.set_size_request(220, -1)  # the menu is wide now: room for a slider you can see
        self.shade_scale.set_hexpand(True)
        self.shade_scale.set_valign(Gtk.Align.CENTER)
        self.shade_scale.set_draw_value(True)
        self.shade_scale.set_value_pos(Gtk.PositionType.LEFT)
        self.shade_scale.set_format_value_func(lambda _s, v: f"{v:.0f} %")
        self.shade_scale.connect("value-changed", lambda sc: self.app.set_look(shade=round(sc.get_value())))
        shade.add_suffix(self.shade_scale)
        group.add(shade)
        start_full = Adw.SwitchRow(title="Spouštět na celou obrazovku",
                                   subtitle="Otevře se na celou obrazovku; dvojklikem se změní v okno"
                                   + (" v pravém dolním rohu" if is_kde() else ""), subtitle_lines=0)
        start_full.set_active(self.app.settings.get("start_full", True))
        start_full.connect("notify::active", lambda r, _: self.app.set_look(start_full=r.get_active()))
        group.add(start_full)
        auto_up = Adw.SwitchRow(title="Automatické aktualizace",
                                subtitle="Každých 5 minut zkontroluje novou verzi, nainstaluje ji a spustí se znovu",
                                subtitle_lines=0)
        auto_up.set_active(self.app.settings.get("auto_update", True))
        auto_up.connect("notify::active", lambda r, _: self.app.set_look(auto_update=r.get_active()))
        group.add(auto_up)
        self.update_row = Adw.ActionRow(title="Aktualizace")
        self.update_btn = Gtk.Button(label="Zkontrolovat", valign=Gtk.Align.CENTER)
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
        # Notes at the bottom: what each picture step runs and on what.
        self.enhance_note = Gtk.Label(label=self.app.notes_text(), xalign=0, wrap=True,
                                      max_width_chars=1, hexpand=False, margin_start=12, margin_end=12)
        self.enhance_note.add_css_class("dim-label")
        self.enhance_note.add_css_class("caption")
        box.append(self.enhance_note)
        # Opens leftwards from the gear (at the right edge), so it stays inside the window.
        # A popup takes its content's full natural width; this caps it near the app's width.
        # In the gear's water (Water, made with the puddle): it scrolls when taller.
        fit = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                 vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vexpand=True)
        fit.add_css_class("gear-menu")
        self.menu_content = fit
        btn = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Nastavení")
        btn.add_css_class("gear-button")
        btn.connect("clicked", lambda *_: self.toggle_water(self.gear_water))
        return btn

    def _quality_pill(self, scrim) -> Gtk.Widget:
        """Quality at the bottom right: a pill of glass like the 'i' panel's tabs. Its list opens upwards, glass
        like the gear menu: what plays now (the codec and bitrate the stream sends), then the streams with their
        notes, best sound first."""
        self.quality_tab = Gtk.Button(label="KVALITA", has_frame=False)
        self.quality_tab.add_css_class("quality-tab")
        pill = self.quality_pill = PillGlass([(self.backdrop, 0.45), (self.cover, 0.0)], halign=Gtk.Align.END, margin_start=16,
                                             valign=Gtk.Align.CENTER)
        pill.scrim, pill.mag, pill.concave, pill.depth = scrim, 1.3, True, False
        pill.set_homogeneous(True)
        pill.append(self.quality_tab)
        self.backdrop.watchers.append(pill)
        self.cover.watchers.append(pill)
        hold_while_hovered(pill)
        title = Gtk.Label(label="Kvalita", xalign=0, margin_start=12, margin_top=8)
        title.add_css_class("heading")
        self.quality_now = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12)
        head = Gtk.Label(label="Nejlepší zvuk nahoře", xalign=0, margin_start=12, margin_top=8, margin_bottom=4)
        head.add_css_class("dim-label")
        head.add_css_class("caption")
        self.quality_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.quality_list.add_css_class("boxed-list")
        self.quality_list.connect("row-activated", self.on_quality)
        qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        for w in (title, self.quality_now, head, self.quality_list):
            qbox.append(w)
        self.quality_fit = Gtk.ScrolledWindow(child=qbox, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                              hexpand=True)
        qglass = MenuGlass(self, [(self.backdrop, 0.45), (self.cover, 0.0)])
        qglass.radius = 16.0
        qglass.mirrors = [pill]  # the pill seen through its glass upside down
        qglass.append(self.quality_fit)
        self.quality_pop = Gtk.Popover(child=qglass, has_arrow=False, position=Gtk.PositionType.TOP)
        self.quality_pop.add_css_class("glass-menu")
        # hung from the tab, not the pill: a popover parented to the (homogeneous) pill box counts in its width
        # when open, and the pill grew to the menu's width, squeezing the volume slider to half
        self.quality_pop.set_parent(self.quality_tab)
        self.quality_pop.connect("show", self._place_quality)
        self.quality_tab.connect("clicked", lambda *_: self.quality_pop.popup())
        self.fill_quality()
        return pill

    def fill_quality(self) -> None:
        while (row := self.quality_list.get_row_at_index(0)) is not None:
            self.quality_list.remove(row)
        q = self.app.settings["quality"]
        ids = [s[0] for s in self.app.streams]
        now = q if q in ids else ids[0]
        for sid, label, _ in self.app.streams:
            row = Adw.ActionRow(title=label, subtitle=STREAM_NOTES.get(sid, ""), subtitle_lines=0, activatable=True)
            row.sid = sid
            if sid == now:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            self.quality_list.append(row)
        self._quality_text()

    def _quality_text(self) -> None:
        """The menu's first line: the stream chosen and what it sends now."""
        a = self.app
        ids = [s[0] for s in a.streams]
        q = a.settings["quality"]
        name = dict((s[0], s[1]) for s in a.streams)[q if q in ids else ids[0]]
        live = " · ".join(p for p in (a.codec, f"{a.bitrate} kbps" if a.bitrate else None) if p)
        text = f"Zvolený stream: {name}" + (f"\nPrávě přichází: {live}" if live else "")
        if a.alt:
            text += f"\nHraje záložní: {a.stream()[1]}"
        if self.quality_now.get_text() != text:
            self.quality_now.set_text(text)

    def on_quality(self, _list, row) -> None:
        self.quality_pop.popdown()
        if getattr(row, "sid", None) and row.sid != self.app.settings["quality"]:
            self.app.set_quality(row.sid)
            self.fill_quality()

    QUALITY_GAP = 10  # between the pill and its menu (as the gear menu's)

    def _place_quality(self, pop) -> None:
        """Above the pill, a gap between, filling the room right of the cover (from its edge to the screen's,
        a gap at each; the notes wrap); without that room, its right edge on the pill's."""
        pw, W = self.quality_pill.get_width(), self.get_width()
        cover = self.cover_rect()
        m = self.QUALITY_GAP + 4
        room = W - cover[2] - 2 * m if cover else 0
        ok, at = self.quality_pill.compute_point(self.quality_tab, Graphene.Point().init(0, 0))
        px, py = (at.x, at.y) if ok else (0, 0)  # the pill's corner, in the tab (the popover's parent)
        ok_w, wx = self.compute_point(self.quality_tab, Graphene.Point().init(W - m, 0))  # the screen's edge
        if room >= max(pw, 240) and ok_w:
            w, right = int(room), wx.x
        else:
            w, right = max(pw, min(W - 44, 460)), px + pw
        self.quality_fit.set_size_request(w, -1)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = round(right - w), round(py - self.QUALITY_GAP), w, 1
        pop.set_pointing_to(r)
        GLib.idle_add(lambda: (pop.get_child().queue_draw(), False)[1])

    def _warm_glass(self) -> bool:
        """Once, soon after start: a small piece of the window pictured, taken off the graphics card and bent
        by the GPU lens, so their first time (shaders built, buffers set up: ~20 ms) isn't when a glass menu
        or panel first opens (a stutter then)."""
        try:
            content, native = self.get_content(), self.get_native()
            if not (EFFECTS["gpu"] and GPU.start() and content and native and content.get_width() > 0):
                return False
            s = Gtk.Snapshot()
            Gtk.WidgetPaintable.new(content).snapshot(s, content.get_width(), content.get_height())
            node = s.to_node()
            if node is None:
                return False
            tex = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, 128, 128))
            src = GPU.upload(tex)
            GPU.lens(src, 64, 64, {"w": 64, "h": 64, "box": (0, 0, 128, 128), "radius": 16.0, "mag": 1.1,
                                   "rx": 0.8, "ry": 0.8, "depth": 10.0, "curve": 3.0})
            GPU.forget(src)
        except Exception as e:  # (only a warm-up)
            print("Rádio Beat: zahřátí skla:", e, file=sys.stderr)
        return False

    def toggle_full(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_fullscreened(self, *_):
        full = self.is_fullscreen()
        (self.add_css_class if full else self.remove_css_class)("full")

    def do_size_allocate(self, width, height, baseline):
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        f = round(max(1.0, min(width / BASE_SIZE[0], height / BASE_SIZE[1])), 2)  # the fit sizes the song info only
        if f != self._scale:
            self._scale, self._scale_pending = f, True
            GLib.idle_add(self._apply_scale)  # not while laying out
        # Measured only once the new sizes are in (the layout after _apply_scale), not with the old ones.
        if not self._fit_queued and not self._scale_pending:
            self._fit_queued = True
            GLib.idle_add(self._check_fit)
        wide = width >= 1.3 * height
        if wide != self._wide or (wide and width != getattr(self, "_arranged_w", 0)):
            self._wide, self._arranged_w = wide, width
            GLib.idle_add(self._arrange)
            GLib.idle_add(self._fit_info)
        if getattr(self, "puddle_open", False):
            GLib.idle_add(lambda: (self._place_puddle(), False)[1])  # it follows the window
        GLib.idle_add(self._place_clock)

    def _day_hover(self, view, on: bool) -> None:
        """The day's glass box: shown beside the view the mouse is on, gone when it leaves."""
        self._day_on = view if on else (None if self._day_on is view else self._day_on)
        if not self._day_on:
            self.day_pop.popdown()
            return
        self._day_show(view)

    DAY_AUTO = (20, 15)  # by itself once an hour: at :20 (well away from the panel's tours at :00 and :30), 15 s

    def _day_auto(self) -> None:
        """The day's box shown by itself for a while; not while a panel or menu is open, a tour runs, the clock's
        hidden or the mouse is already on it."""
        if not hasattr(self, "quality_pop") or not hasattr(self, "_tour"):
            return  # (still being built)
        if (not self.get_visible() or not self.clock.get_visible() or self._tour is not None
                or any(w.open for w in self.waters) or self._day_on or self.quality_pop.get_visible()):
            return
        self._day_show(self.clock)
        GLib.timeout_add_seconds(self.DAY_AUTO[1], lambda: (self._day_on or self.day_pop.popdown(), False)[1])

    DAY_GAP = 18  # the day's box keeps this far inside the cover's edges and above the slider

    def _day_area(self):
        """(x, y, w, h) in the window for the day's box: inside the cover, a gap from each of its edges, above
        the volume slider (and the quality pill); None when there's no cover or too little of it."""
        cover = self.cover_rect()
        if not cover:
            return None
        g = self.DAY_GAP
        left, top = max(0, cover[0]) + g, max(0, cover[1]) + g
        right, bottom = min(self.get_width(), cover[2]) - g, min(self.get_height(), cover[3]) - g
        for w in (self.vol, self.quality_pill):
            ok, b = w.compute_bounds(self)
            if ok and w.get_mapped() and b.get_y() > top and b.get_x() < right and b.get_x() + b.get_width() > left:
                bottom = min(bottom, b.get_y() - g)
        if right - left < 200 or bottom - top < 120:
            return None
        return left, top, right - left, bottom - top

    def _day_fit(self, w: int, h: int) -> None:
        """The day's text as big as fits the box (wrapped to its width), between 0.3 and three times."""
        box = self.day_text
        r = self.day_fit.get_margin_start()  # (the glass's corner radius on every side)
        tw = w - box.get_margin_start() - box.get_margin_end() - 2 * r - 4
        th = h - box.get_margin_top() - box.get_margin_bottom() - 2 * r
        lo, hi = 0.3, 3.0
        for _ in range(12):  # (a binary search over the scale)
            f = (lo + hi) / 2
            self._day_scale(f)
            if (box.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= tw  # (the headlines, on one line, fit across)
                    and box.measure(Gtk.Orientation.VERTICAL, max(1, tw))[0] <= th):
                lo = f
            else:
                hi = f
        self._day_scale(lo)

    def _day_scale(self, f) -> None:
        """All the day's labels at `f` times their size (None: as they are); the gaps before headlines too."""
        attrs = None
        if f is not None:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_scale_new(f))
        for lab in self._day_labels:
            lab.set_attributes(attrs)
            if lab.has_css_class("day-head") and lab.get_prev_sibling() is not None:
                lab.set_margin_top(round(10 * (f or 1.0)))

    def _day_fill(self) -> None:
        """The day's pieces as labels: headlines in red pills, the rest wrapped text."""
        while (c := self.day_text.get_first_child()) is not None:
            self.day_text.remove(c)
        self._day_labels = []
        for kind, markup in day_summary():
            lab = Gtk.Label(label=markup, use_markup=True, wrap=True, xalign=0)
            if kind == "head":
                lab.add_css_class("day-head")
                lab.set_halign(Gtk.Align.START)  # the pill as wide as its words
                lab.set_wrap(False)  # on one line (wrapped, Pango broke it a pixel short of its own width)
                if self._day_labels:
                    lab.set_margin_top(10)
            else:
                lab.set_halign(Gtk.Align.FILL)
                lab.set_margin_top(2)
            self.day_text.append(lab)
            self._day_labels.append(lab)

    def _day_margins(self, glass: int, tb: int, lr: int) -> None:
        """The day text's borders: `glass` px around its scroller, tb/lr px around the label itself."""
        for f in (self.day_fit.set_margin_top, self.day_fit.set_margin_bottom, self.day_fit.set_margin_start,
                  self.day_fit.set_margin_end):
            f(glass)
        self.day_text.set_margin_top(tb)
        self.day_text.set_margin_bottom(tb)
        self.day_text.set_margin_start(lr)
        self.day_text.set_margin_end(lr)

    def _day_show(self, view) -> None:
        self._day_fill()
        area = self._day_area()
        glass = self.day_pop.get_child()
        if area:  # over the cover, filling it
            x, y, w, h = (round(v) for v in area)
            glass.set_size_request(w, h)
            glass.radius = 28 * max(1.0, self._scale)  # the panels' corners
            # the text within the rectangle joining the corners' circle centers, as in the ⓘ panel
            r = int(glass.radius)
            self._day_margins(r, 0, 0)
            self.day_fit.set_propagate_natural_width(False)
            self.day_fit.set_propagate_natural_height(False)
            self.day_fit.set_size_request(w - 2 * r, h - 2 * r)
            self._day_fit(w, h)
            self.day_pop.set_position(Gtk.PositionType.BOTTOM)
            ok, at = self.compute_point(self.clock, Graphene.Point().init(x, y))
            if ok:
                r = Gdk.Rectangle()
                r.x, r.y, r.width, r.height = round(at.x), round(at.y) - 1, w, 1
                self.day_pop.set_pointing_to(r)
        else:  # no cover: beside the date and time as before
            glass.set_size_request(-1, -1)
            self.day_fit.set_size_request(-1, -1)
            self._day_margins(0, 10, 14)
            self.day_fit.set_propagate_natural_width(True)
            self.day_fit.set_propagate_natural_height(True)
            glass.radius = 16.0
            for lab in self._day_labels:
                if not lab.has_css_class("day-head"):
                    lab.set_max_width_chars(46)
            self._day_scale(None)
            self.day_pop.set_position(Gtk.PositionType.LEFT)
            ok, b = view.compute_bounds(self.clock)  # where it is, in the clock (the box's parent)
            if ok:
                r = Gdk.Rectangle()
                r.x, r.y, r.width, r.height = round(b.get_x()) - 12, round(b.get_y()), 1, max(1, round(b.get_height()))
                self.day_pop.set_pointing_to(r)
        self.day_pop.get_child().fresh()  # never an earlier picture behind it
        if not area:
            self.day_pop.set_offset(0, 0)
        self.day_pop.popup()
        GLib.idle_add(lambda: (self.day_pop.get_child().queue_draw(), False)[1])
        if area:
            GLib.idle_add(self._day_true, area)

    def _day_true(self, area) -> bool:
        """The popover puts its box a few px off where it was pointed (a gap of its own, the theme's): measured
        once shown and moved by that, so it keeps exactly the gaps from the cover's edges and the slider."""
        glass, content = self.day_pop.get_child(), self.get_content()
        ok, p = glass._where(content) if content and self.day_pop.get_visible() else (False, None)
        ok_c, cp = content.compute_point(glass.win, Graphene.Point().init(0, 0)) if ok else (False, None)
        if not ok_c:
            return False
        dx, dy = round(area[0] - (cp.x - p.x)), round(area[1] - (cp.y - p.y))
        if dx or dy:
            ox, oy = self.day_pop.get_offset()
            self.day_pop.set_offset(ox + dx, oy + dy)
            self.day_pop.present()
            glass.fresh()
            glass.queue_draw()
        return False

    def _place_status(self, top: float = None, stop: float = None) -> None:
        """The status under the volume slider (below its knob, `top`), centred on it, from its start to its end
        or to `stop` (where the names begin), as big as fits, touching nothing."""
        sv, H = self.status_view, self.get_height()
        ok, vb = self.vol.compute_bounds(self)
        if not ok or not sv.text:
            sv.set_visible(False)
            return
        if top is None:
            top = vb.get_y() + vb.get_height() / 2 + 12  # (the knob's bottom, about)
        x0 = vb.get_x() + 8
        x1 = vb.get_x() + vb.get_width() - 8
        if stop is not None:
            x1 = min(x1, stop - 12)
        sh = int(min(H - top - 2 * self.CLOCK_PAD, 40))
        if x1 - x0 < 60 or sh < 8:
            sv.set_visible(False)
            return
        sv.set_room(int(x1 - x0), sh)
        sv.set_margin_start(int(x0))
        sv.set_margin_bottom(int((H - top - sh) / 2))
        sv.set_visible(True)

    CLOCK_GAP = 22  # from the screen's right edge (as the song info from its left)
    CLOCK_PAD = 6  # off the volume slider's knob and the screen's bottom

    def _tick_clock(self) -> bool:
        now = time.localtime()
        special = (balanced_rows(special_day_name().upper()) + ["", "", ""])[:3]
        self._clock_texts = (f"{now.tm_hour}:{now.tm_min:02d}", DAYS[now.tm_wday].upper(),
                             str(now.tm_mday), MONTHS_1[now.tm_mon - 1].upper(), str(now.tm_year), *special)
        self._names_text = name_day()
        if self._clock_texts != self.clock.texts or (self._names_text,) != self.names.texts:
            self._place_clock()
        self.clock.colon = now.tm_sec % 2 == 0
        self.clock.queue_draw()
        hour = (now.tm_yday, now.tm_hour)
        if now.tm_min == self.DAY_AUTO[0] and getattr(self, "_day_auto_hour", None) != hour:
            self._day_auto_hour = hour
            self._day_auto()
        return GLib.SOURCE_CONTINUE

    def _place_clock(self) -> bool:
        cover, W, H = self.cover_rect(), self.get_width(), self.get_height()
        # the falling motifs: right of the cover, the window's whole height
        left = cover[2] if cover else W
        self.snow.set_area((left, 0, W - left, H) if W - left >= 40 and H >= 40 else None, (0, 0, W, H))
        ok, vol = self.vol.compute_bounds(self)
        if not cover or not ok or not getattr(self, "_clock_texts", None):
            self.clock.set_visible(False)
            self.names.set_visible(False)
            self._place_status()
            return False
        # just below the slider's knob (the slider widget itself is taller than what's drawn of it)
        top = vol.get_y() + vol.get_height()
        knob_top = vol.get_y()
        # the knob measured as it rests: while the mouse is on the slider it pumps (bigger), and the date and
        # clock would pump with it
        hovered = bool(self.vol.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        stack = [] if hovered and getattr(self, "_knob_rest", None) else [self.vol]
        if hovered and getattr(self, "_knob_rest", None):
            top, knob_top = self._knob_rest
        while stack:
            w = stack.pop()
            if w.get_css_name() == "slider":
                ok2, knob = w.compute_bounds(self)
                if ok2:
                    top = knob.get_y() + knob.get_height()
                    knob_top = knob.get_y()
                    if not hovered:
                        self._knob_rest = (top, knob_top)
                break
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        ok3, pill = self.quality_pill.compute_bounds(self)  # the quality pill at the slider's end: above it too
        if ok3 and pill.get_height() > 0:
            knob_top = min(knob_top, pill.get_y())
        room_w = int(W - self.CLOCK_GAP - (cover[2] + self.CLOCK_GAP))
        # above the slider's knob, below the top buttons (a little off both)
        ok_h, hb = self.header.compute_bounds(self)
        below = hb.get_y() + hb.get_height() if ok_h else H * 0.1
        room_h = int(knob_top - below - 3 * self.CLOCK_PAD)
        if room_w < 60 or room_h < 40:
            self.clock.set_visible(False)
            self.names.set_visible(False)
            self._place_status()
            return False
        self.clock.set_room(room_w, room_h, self._clock_texts)
        self.clock.set_margin_end(self.CLOCK_GAP)
        self.clock.set_margin_bottom(int(H - knob_top + self.CLOCK_PAD))
        self.clock.set_visible(True)
        # the names: below the slider's knob and the pill, above the screen's bottom edge
        under = top
        if ok3 and pill.get_height() > 0:
            under = max(under, pill.get_y() + pill.get_height())
        names_h = int(H - under - 2 * self.CLOCK_PAD)
        self._place_status(top, cover[2] + self.CLOCK_GAP if self._names_text and names_h >= 12 else None)
        if self._names_text and names_h >= 12:
            self.names.set_room(room_w, names_h, (self._names_text,))
            self.names.set_margin_end(self.CLOCK_GAP)
            self.names.set_margin_bottom(self.CLOCK_PAD)
            self.names.set_visible(True)
        else:
            self.names.set_visible(False)
        return False

    FIT_STEP = 0.95

    def _check_fit(self) -> bool:
        """The song info's text as big as fits its place: below the top bar (never up over the logo), above
        the buttons, beside the cover; full screen too. 85 % of its usual size at the least, 1.6x at most."""
        self._fit_queued = False
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return False
        fit = self._fit
        if (w, h, self.song.get_text()) != self._fit_for:  # a new size or song: everything may fit again
            self._fit_for, self._fit_bad = (w, h, self.song.get_text()), 9.0
        # Where things really are (GTK's estimate of a wrapped text's height came out short): the title's top
        # below the top bar, the song info's right edge left of the cover.
        ok_s, sb = self.song.compute_bounds(self)
        ok_h, hb = self.header.compute_bounds(self)
        ok_i, ib = self.info.compute_bounds(self)
        if not (ok_s and ok_h and ok_i):
            return False
        top = hb.get_y() + hb.get_height() + 8
        cover = self.cover_rect()
        right = (cover[0] - self.INFO_GAP) if cover and self._wide and self.info.get_margin_end() else None
        over = sb.get_y() < top or (right is not None and ib.get_x() + ib.get_width() > right + 1)
        slack = sb.get_y() - top
        if over and fit > 0.55:  # up over the logo, or out over the cover
            self._fit_bad = min(self._fit_bad, fit)
            fit = max(0.55, fit * self.FIT_STEP)
        elif not over and fit < 1.6 and slack > ib.get_height() * 0.12 and fit / self.FIT_STEP < self._fit_bad - 1e-6:
            fit = min(1.6, fit / self.FIT_STEP)  # grows to fill its place
        if fit != self._fit:
            self._fit = fit
            self._scale_pending = True
            GLib.idle_add(self._apply_scale)  # the text's new size, laid out, then checked again
        return False

    # The show card: every minute the programme (ON AIR) for 8 s, the rest of the minute a new text every 15 s
    # (the song playing, then the mix: App.turn_content), typed out letter by letter at a fast typist's pace,
    # faster when the text is long, so it's done with time left to read.
    TYPE_MS = 115  # a letter at most (about 100 words a minute), a little uneven like a hand's
    PROGRAMME_S, TEXT_S, MINUTE_S = 8, 15, 60
    SONG_S = 45  # the song's own text may be longer: up to this
    READ_S = 4  # left to read once typed

    def _turn(self) -> bool:
        a = self.app
        self._turn_id = 0
        now = time.monotonic()
        left = self._minute_at + self.MINUTE_S - now  # until the programme's next turn
        turn = a.turn_content() if left >= 6 and (self.card_turn == "song"
                                                  or now - self._minute_at >= self.PROGRAMME_S - 0.5) else None
        if turn:
            slot = min(self.SONG_S if turn[0] == "NOW PLAYING" else self.TEXT_S, left)
            if left - slot < 8:  # no stub of a turn before the programme: this one lasts to the minute
                slot = left
            self.card_turn, self._turn_content = "song", turn
            text = turn[2]
            self._start_typing(text, min(self.TYPE_MS, (slot - self.READ_S) * 1000 / max(1, len(text)) / 1.15))
        else:
            if left < 6 or not turn:  # the minute's up (or nothing else to show): the programme
                self._minute_at, slot = now, self.PROGRAMME_S if turn or a.turn_content_ready() else self.TEXT_S
            else:
                slot = self.PROGRAMME_S
            self.card_turn, self._turn_content = "show", None
            self._stop_typing()
        self.refresh()
        self._turn_id = GLib.timeout_add(int(slot * 1000), self._turn)
        return False

    def _start_typing(self, text: str, ms: float = TYPE_MS) -> None:
        self._stop_typing()
        self._type_ms = ms
        lab = self.show_sub
        lab.set_lines(-1)
        lab.set_ellipsize(Pango.EllipsizeMode.NONE)
        # Room for all of it from the first letter (the card doesn't grow line by line), at most 5 lines: more
        # scrolls up as it's typed.
        w = lab.get_width() or self.show_card_text.get_width() or 300
        lab.set_text("X")
        line = lab.measure(Gtk.Orientation.VERTICAL, w)[1]
        lab.set_text(text)
        full = lab.measure(Gtk.Orientation.VERTICAL, w)[1]
        self.sub_scroll.set_size_request(-1, min(full, line * 5))
        self._typed, self._typing_text = 0, text
        lab.set_text("")
        self._type()

    def _type(self) -> bool:
        self._type_id = 0
        text = self._typing_text
        if self.card_turn != "song" or not text:
            return False
        self._typed += 1
        done = self._typed >= len(text)
        self.show_sub.set_text(text[:self._typed] + ("" if done else "▌"))
        GLib.idle_add(self._scroll_typed)
        if done:
            return False
        ch = text[self._typed - 1]
        k = self._type_ms / self.TYPE_MS  # pauses shrink with the pace
        delay = self._type_ms * random.uniform(0.6, 1.4) + k * (260 if ch in ".!?" else 120 if ch in ",;:" else 0)
        self._type_id = GLib.timeout_add(int(delay), self._type)
        return False

    # --- the 'i' button's puddle: programme and curiosities ------------------------------------------
    PUDDLE_MODES = ("DNES", "ZAJÍMAVOSTI", "AKTUALITY", "ZÍTRA", "POZÍTŘÍ", "HRÁLO")
    PUDDLE_SPEED = 7  # px a second the views scroll by themselves
    PUDDLE_WAIT = 15  # s before they start: after opening, a tab or the wheel

    def cover_rect(self):
        """(left, top, right, bottom) of the cover as shown, in the window; None when there's none."""
        pic = self.cover.pic
        if not pic:
            return None
        ok, p = self.cover.compute_point(self, Graphene.Point().init(0, 0))
        if not ok:
            return None
        x, y, dw, dh = self.cover.rect(pic, self.cover.get_width(), self.cover.get_height())
        return p.x + x, p.y + y, p.x + x + dw, p.y + y + dh

    INFO_GAP = 18  # the song info keeps this far from the screen's edge and the cover's

    def _fit_info(self) -> bool:
        """In a wide window the song info stays between the screen's left edge and the cover's (touching
        neither); when the cover leaves too little room there, as it was."""
        cover, W = self.cover_rect(), self.get_width()
        end = 0
        if cover and self._wide:
            room = cover[0] - self.INFO_GAP - 22  # the text panel's own margin at the left
            if room >= 120:
                end = max(0, int((W - 22) - (cover[0] - self.INFO_GAP)))
        if self.info.get_margin_end() != end:
            self.info.set_margin_end(end)
        self._place_clock()  # beside the cover as it is now
        self._place_info()
        return False

    def _place_info(self) -> bool:
        """In a wide window the song info as fitted lines (InfoStack) between the screen's left edge and the
        cover's, below the logo, above the buttons; otherwise the labels as ever."""
        labels = (self.song, self.artist, self.album, self.facts)
        cover, W = self.cover_rect(), self.get_width()
        ok_h, hb = self.header.compute_bounds(self)
        ok_c, cb = self.controls.compute_bounds(self)
        ok_i, ib = self.info.compute_bounds(self)
        room_w = int(cover[0] - self.INFO_GAP - (ib.get_x() if ok_i else 22)) if cover else 0
        room_h = int(cb.get_y() - self.controls.get_margin_top() - (hb.get_y() + hb.get_height()) - 16) \
            if ok_h and ok_c else 0
        on = self._wide and self.info.get_margin_end() > 0 and room_w >= 120 and room_h >= 60
        if on:
            texts = tuple(l.get_text() if l.get_visible() else "" for l in labels)
            beat = self.app.settings.get("font", "beat") == "beat" and self.app.fonts_ok
            self.info_stack.set_room(room_w, room_h, texts, beat)
        for l in labels:
            l.set_child_visible(not on)
            l.set_size_request(-1, 0 if on else -1)
        self.info_stack.set_visible(on)
        return False

    def _make_puddle(self, overlay, scrim) -> None:
        behind = [(self.backdrop, 0.45), (self.cover, 0.0)]
        self.puddle = Puddle(behind, orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.END,
                             valign=Gtk.Align.START)
        # The views as tabs (a click), and the wheel's button goes to the next one.
        # They wrap when narrow. Each tab's cell gets the same share of the spare width, the tab centred in it:
        # the gaps between the tabs are all the same, whatever their names' lengths.
        self.puddle_head = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                                       min_children_per_line=1, max_children_per_line=len(self.PUDDLE_MODES),
                                       homogeneous=False)
        self.puddle_tabs = []
        self.puddle_pills = []
        for i in range(len(self.PUDDLE_MODES)):
            tab = Gtk.Button(label="", has_frame=False)
            tab.add_css_class("puddle-tab")
            tab.connect("clicked", lambda _b, i=i: self._puddle_go(i))
            # in a pill of glass like the pause, stop and gear buttons (the chosen one red like pause)
            pill = PillGlass([(self.backdrop, 0.45), (self.cover, 0.0)], halign=Gtk.Align.CENTER, hexpand=True)
            pill.mag, pill.concave, pill.depth = 1.3, True, False
            pill.set_homogeneous(True)
            pill.append(tab)
            self.backdrop.watchers.append(pill)
            self.cover.watchers.append(pill)
            self.puddle_tabs.append(tab)
            self.puddle_pills.append(pill)
            self.puddle_head.append(pill)
        self.puddle_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.puddle_scroll = PinnedScroll(child=self.puddle_list, vexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          vscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        # DNES: the show on air pinned one show from the top, the day's lineup scrolling behind it
        self.puddle_pin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START, visible=False)
        self.puddle_scroll.pin = self.puddle_pin
        # HRÁLO: older plays loaded as the list nears its end (scrolled by hand or by itself): never-ending
        self.puddle_scroll.get_vadjustment().connect("value-changed", lambda *_: self._played_more())
        self._played_before = None  # the oldest play listed (None: not this view)
        self._played_day = None
        self._art_wait: dict = {}  # (artist, title) -> its pictures in HRÁLO waiting for the lookup
        self._played_rows: list[dict] = []  # HRÁLO's songs as listed (the full-screen viewer goes through them)
        stage = Gtk.Overlay(child=self.puddle_scroll, vexpand=True)
        stage.add_overlay(self.puddle_pin)
        self.puddle.append(self.puddle_head)
        self.puddle.append(stage)
        mid = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        mid.connect("pressed", lambda g, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED), self._puddle_next()))
        self.puddle.add_controller(mid)
        # the wheel anywhere on it: noted first (the views wait before scrolling by themselves again) ...
        touch = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        touch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        touch.connect("scroll", lambda *_: (self._puddle_touch(), False)[1])
        self.puddle.add_controller(touch)
        # ... and where the list doesn't take it itself (over the pinned show, the tabs), it scrolls the list
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._puddle_wheel)
        self.puddle.add_controller(wheel)
        self.info_water = Water(self, self.info_tile, self.puddle, overlay)
        self.neck = self.info_water.neck
        # the gear's water: the settings, the same way
        gear_body = Puddle(behind, orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, valign=Gtk.Align.START)
        gear_body.append(self.menu_content)
        self.gear_water = Water(self, self.gear_tile, gear_body, overlay)

        self.waters = [self.info_water, self.gear_water]
        # a click anywhere else draws it back into its button
        outside = Gtk.GestureClick(button=0)
        outside.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        outside.connect("pressed", self._puddle_outside)
        self.add_controller(outside)
        self.puddle_mode = 0
        self.puddle_items: list = []  # the curiosities shown this session
        self._puddle_touched = 0.0
        self._puddle_tick = None
        # A new show on air, and every half hour: the panel opens by itself and goes through its tabs (TOUR)
        self._show_seen = None
        self._half_seen = None
        self._tour = None
        self._tour_pages: dict[str, tuple] = {}  # view -> (where the next tour shows it from, where the last did)
        GLib.timeout_add_seconds(15, self._watch_show)

    @property
    def puddle_open(self) -> bool:
        return self.info_water.open

    def toggle_water(self, water) -> None:
        if water.open:
            water.hide()
            return
        for other in self.waters:
            if other is not water:
                other.hide()
        if water is self.info_water:
            self.puddle_mode = 0
            self._puddle_touched = time.monotonic()
            self._fill_puddle()
            if self._puddle_tick is None:
                self._puddle_last = None
                self._puddle_tick = self.puddle_scroll.add_tick_callback(self._puddle_scroll_step)
        self.day_pop.popdown()  # the day's box makes way
        water.body.fresh()  # never an earlier picture's lens or mirror while it opens: drawn anew
        self.refresh_effects()
        GLib.timeout_add_seconds(3, lambda: (self.refresh_effects(), False)[1])  # the strips' memory, once built
        GLib.timeout_add_seconds(4, self._warm_glass)
        water.show()
        if water is self.gear_water:  # from the top (a control taking the focus scrolled it a row down)
            GLib.idle_add(lambda: (self.menu_content.get_vadjustment().set_value(0), False)[1])

    # the tabs it goes through by itself, and for how long (s)
    TOUR = (("DNES", 10), ("ZAJÍMAVOSTI", 10), ("AKTUALITY", 10), ("ZÍTRA", 10), ("POZÍTŘÍ", 10), ("HRÁLO", 10))

    def _watch_show(self) -> bool:
        now = time.localtime()
        cur, _ = on_air(self.app.schedule, now)
        key = cur and (cur["title"], cur["start"])
        seen, self._show_seen = self._show_seen, key or self._show_seen
        half = (now.tm_yday, now.tm_hour, now.tm_min // 30)  # at :00 and :30
        half_seen, self._half_seen = self._half_seen, half
        if (key and seen is not None and key != seen) or (half_seen is not None and half != half_seen):
            self._start_tour()
        return GLib.SOURCE_CONTINUE

    def _start_tour(self) -> None:
        """Opens the panel and shows its tabs one after another (TOUR), then closes it; not when the window's
        hidden or a panel's already open, and it stops (leaving the panel open) once it's touched."""
        if not self.get_visible() or any(w.open for w in self.waters) or self._tour is not None:
            return
        self.toggle_water(self.info_water)
        self.app.enhancer.set_quiet(False)  # nobody's scrolling it: the enhancing goes on (paused once touched)
        self._tour = object()
        self._tour_step(self._tour, 0)

    def _tour_page(self, token, name: str, tries: int) -> bool:
        """The tour shows each view a page further than the last tour did (back at the top past its end), not
        the same top every time; DNES stays at the show on air."""
        if self._tour is not token or self.PUDDLE_MODES[self.puddle_mode] != name:
            return False
        adj = self.puddle_scroll.get_vadjustment()
        page, end = adj.get_page_size(), adj.get_upper() - adj.get_page_size()
        if page <= 0 or (end <= 0 and tries > 0):  # not laid out yet
            if tries > 0:
                GLib.timeout_add(60, self._tour_page, token, name, tries - 1)
            return False
        at, last = self._tour_pages.get(name, (0.0, -1.0))
        if at > end + 1:  # past the end: its last part first (if the last tour stopped short of it), then the top
            at = max(0.0, end) if last < end - 1 else 0.0
        at = min(at, max(0.0, end))
        adj.set_value(at)
        self._tour_touched = self._puddle_touched  # (a scroll it made itself, not a touch)
        self._tour_pages[name] = (at + page * 0.9, at)  # (a line of overlap with the last time)
        return False

    def _tour_step(self, token, i: int) -> bool:
        if self._tour is not token:
            return False
        if not self.info_water.open or (i and self._puddle_touched != self._tour_touched):
            self._tour = None  # closed or touched meanwhile: left as it is
            if self.info_water.open:
                self.app.enhancer.set_quiet(True)  # someone's using it: fluid, as when opened by hand
            return False
        if i == len(self.TOUR):
            self._tour = None
            self.info_water.hide()
            return False
        name, secs = self.TOUR[i]
        if i:
            self._puddle_go(self.PUDDLE_MODES.index(name))
            GLib.timeout_add(60, self._tour_page, token, name, 20)
        self._tour_touched = self._puddle_touched
        GLib.timeout_add_seconds(secs, self._tour_step, token, i + 1)
        return False

    def toggle_puddle(self) -> None:
        self.toggle_water(self.info_water)

    def open_puddle(self) -> None:
        if not self.info_water.open:
            self.toggle_water(self.info_water)

    def close_puddle(self) -> None:
        self.info_water.hide()

    def _place_puddle(self) -> None:
        for water in self.waters:
            if water.open:
                water.place()

    def _puddle_outside(self, _gesture, _n, x, y) -> None:
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        for water in self.waters:
            if water.open and not water.contains(w):
                water.hide()

    def _puddle_wheel(self, _ctl, _dx, dy) -> bool:
        adj = self.puddle_scroll.get_vadjustment()
        adj.set_value(adj.get_value() + dy * 60)
        return True  # never the window's volume

    def _puddle_touch(self) -> None:
        self._puddle_touched = time.monotonic()
        if self._tour is not None:  # the tour ends here: the panel is someone's now, fluid
            self._tour = None
            self.app.enhancer.set_quiet(True)

    def _puddle_next(self) -> None:
        self._puddle_go((self.puddle_mode + 1) % len(self.PUDDLE_MODES))

    def _puddle_go(self, mode: int) -> None:
        self.puddle_mode = mode
        self._puddle_touched = time.monotonic()
        self._fill_puddle()

    def _clear_puddle(self) -> None:
        for box in (self.puddle_list, self.puddle_pin):
            while (c := box.get_first_child()) is not None:
                box.remove(c)
        self.puddle_pin.set_visible(False)
        self._puddle_mark = None
        self.puddle_scroll.get_vadjustment().set_value(0)

    def _center_now(self, tries: int = 100) -> bool:
        """DNES: the show on air pinned one show from the top (the one before it above, more of the coming ones
        below); the gap left for it in the lineup as tall as it is, and scrolled to just behind it."""
        mark = self._puddle_mark
        if mark is None or mark.get_parent() is not self.puddle_list:
            return False
        page, width = self.puddle_scroll.get_height(), self.puddle_scroll.get_width()
        if page <= 0 or width <= 0:
            if tries:
                GLib.timeout_add(30, self._center_now, tries - 1)  # not laid out yet (the water still flowing)
            return False
        pin = self.puddle_pin
        ph = pin.measure(Gtk.Orientation.VERTICAL, width)[1] - pin.get_margin_top() if pin.get_visible() else 0  # (without its margin)
        before = mark.get_prev_sibling()
        # the show before it above, or as much of it as the pinned one is tall (a long one would push the
        # coming shows out of view)
        top = min(before.measure(Gtk.Orientation.VERTICAL, width)[1], ph) + 14 if before is not None else 0
        top = max(0, min(top, page - ph))  # the pinned one always whole in view
        self.puddle_pin.set_margin_top(top)
        mark.set_size_request(-1, ph)
        self.puddle_list.get_last_child().set_size_request(-1, max(0, int(page - top - ph - 28)))  # room below
        ok, b = mark.compute_bounds(self.puddle_list)
        if (not ok or abs(b.get_height() - ph) > 1) and tries:
            GLib.timeout_add(30, self._center_now, tries - 1)  # once the new sizes are laid out
            return False
        if ok:
            self.puddle_scroll.get_vadjustment().set_value(max(0.0, b.get_y() - top))
        return False

    def _puddle_block(self, badge: str, title: str, text: str, now: bool = False, img: str | None = None,
                      url: str = "", past: bool = False, into=None) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if img is not None:  # a show: its picture beside its lines
            row = Gtk.Box(spacing=12)
            pic = Gtk.Image(pixel_size=int(TEXT_PT["pic"] * 0.7 * max(1.0, self._scale)), valign=Gtk.Align.START)
            pic.add_css_class("show-pic")
            pic.set_overflow(Gtk.Overflow.HIDDEN)
            pic.img_url = img
            tex = self.app.show_pics.get(img) if img else None
            pic.set_from_paintable(tex or self.app.logo_tex)
            self.app.want_show_pic(img)
            self._last_pic = pic
            row.append(pic)
            box.set_hexpand(True)
            row.append(box)
            outer = row
        else:
            outer = box
        top = Gtk.Box(spacing=8)
        if badge:
            b = Gtk.Label(label=badge, valign=Gtk.Align.START)
            b.add_css_class("on-air")
            top.append(b)
        if title:
            t = Gtk.Label(label=title, xalign=0, wrap=True, hexpand=True)
            t.add_css_class("show-title")
            top.append(t)
        if badge or title:
            box.append(top)
        self._last_top = top
        if text:
            body = Gtk.Label(label=text, xalign=0, wrap=True)
            body.add_css_class("show-sub")
            box.append(body)
            self._last_body = body
        if now:
            outer.add_css_class("puddle-now")
        if past:
            outer.add_css_class("puddle-past")
        if url:  # a news item: the whole article in the browser
            click = Gtk.GestureClick()
            click.connect("released", lambda *_: Gtk.UriLauncher.new(url).launch(self, None, None, None))
            outer.add_controller(click)
            outer.set_cursor_from_name("pointer")
            outer.set_tooltip_text("Celý článek na radiobeat.cz")
        (into or self.puddle_list).append(outer)

    def puddle_pics(self) -> None:
        """The lineup's pictures that came in since."""
        stack = [self.puddle_list, self.puddle_pin]
        while stack:
            w = stack.pop()
            url = getattr(w, "img_url", None)
            if url and self.app.show_pics.get(url):
                w.set_from_paintable(self.app.show_pics[url])
            c = w.get_first_child()
            while c:
                stack.append(c)
                c = c.get_next_sibling()

    def _fill_puddle(self) -> None:
        a = self.app
        mode = self.PUDDLE_MODES[self.puddle_mode]
        for i, tab in enumerate(self.puddle_tabs):
            tab.set_label(a.cz(self.PUDDLE_MODES[i]))
            (tab.add_css_class if i == self.puddle_mode else tab.remove_css_class)("on-air")
            self.puddle_pills[i].color = (227, 5, 26, 0.55) if i == self.puddle_mode else None
            self.puddle_pills[i].queue_draw()
        self._clear_puddle()
        self._played_before = None
        self._art_wait.clear()
        self._played_rows = []
        self._drop_glass_buttons()
        now = time.localtime()
        if mode == "ZAJÍMAVOSTI":
            if a.about:
                info = a.info or {}
                self._puddle_block(a.badge("NOW PLAYING"), plain_title(info.get("title") or a.song or ""),
                                   a.cz(a.about) + (f"\n{a.cz(a.facts)}" if a.facts else ""))
            while len(self.puddle_items) < 4 and (item := a.next_curiosity()):
                self.puddle_items.append(item)
            for item in self.puddle_items:  # under ZAJÍMAVOSTI: just the name, the picture beside
                self._puddle_block("", item[1] or "Rádio Beat", item[2], img=item[3] if len(item) > 3 else "")
            return
        if mode == "HRÁLO":  # every song played, newest first, a heading for each day; more as it scrolls
            self._played_before, self._played_day = time.time() + 1, None
            self._played_more(force=True)
            return
        if mode == "AKTUALITY":  # the station's news: a click opens the article
            if not a.news:
                self._puddle_block("", "", a.cz("Aktuality se načítají…"))
            for item in a.news:
                self._puddle_block(item["date"], item["title"], a.cz(item["text"]), img=item["img"],
                                   url=item["url"])
            return
        day = (now.tm_wday + ("DNES", "ZÍTRA", "POZÍTŘÍ").index(mode)) % 7
        mins = now.tm_hour * 60 + now.tm_min
        cur, _ = on_air(a.schedule, now)
        shows = a.schedule.get(day) or []
        if mode == "DNES":  # today by time: what's been on (a little grey), the show on air, what's coming
            self._puddle_today(shows, cur, mins)
            return
        if not shows:
            self._puddle_block("", "", a.cz("Program pro tento den zatím není."))
        for show in shows:
            when = f'{show["start"] // 60}:{show["start"] % 60:02d}'
            self._puddle_block(when, show["title"], a.cz(show.get("desc") or ""), img=show.get("img") or "")

    def _played_more(self, force: bool = False) -> None:
        """HRÁLO: the next page of older plays, when the list is near its end."""
        if self._played_before is None or self.PUDDLE_MODES[self.puddle_mode] != "HRÁLO":
            return
        adj = self.puddle_scroll.get_vadjustment()
        if not force and adj.get_value() < adj.get_upper() - adj.get_page_size() * 2:
            return
        rows = self.app.db.page(self._played_before)
        if not rows:
            if force:
                self._puddle_block("", "", self.app.cz("Zatím nic nehrálo."))
            self._played_before = 0.0  # all shown
            return
        for at, artist, title, album, show, cover, facts, shown_artist, shown_title in rows:
            last = self._played_rows[-1] if self._played_rows else None
            if last and (last["artist"].lower(), last["title"].lower()) == (artist.lower(), title.lower()) \
                    and last["at"] - at < 3 * 3600:
                continue  # the same song right after itself: one play
            t = time.localtime(at)
            day = (t.tm_year, t.tm_yday)
            if day != self._played_day:
                self._played_day = day
                self._puddle_block("", f"{DAYS[t.tm_wday].upper()} {t.tm_mday}. {MONTHS[t.tm_mon - 1].upper()} "
                                       f"{t.tm_year}", "")
            # with its album's picture like the shows' and its track info (looked up when missing: the logo meanwhile)
            row = {"at": at, "artist": artist, "title": title, "album": album, "show": show, "cover": cover,
                   "facts": facts, "shown_artist": shown_artist, "shown_title": shown_title}
            self._last_body = None
            self._puddle_block(f"{t.tm_hour}:{t.tm_min:02d}", f"{shown_artist} – {plain_title(shown_title)}",
                               self._played_line(row) or " ", img=small_cover(cover) if cover else "")
            row["label"] = self._last_body
            # play / pause and stop in glass like the radio's: the song itself from YouTube (the radio waits)
            row["play"] = self._glass_button("media-playback-start-symbolic", 40,
                                             "Přehrát z YouTube (studiová verze, nejlepší kvalita)",
                                             lambda a=artist, ti=title: self.app.play_song(a, ti))
            row["play"].pad = True  # room to pump in: the list's edge would cut it
            self._last_top.append(row["play"])
            self._played_rows.append(row)
            self._track_row(row)
            click = Gtk.GestureClick()  # the picture full screen
            click.connect("released", lambda *_, i=len(self._played_rows) - 1: self.view_played(i))
            self._last_pic.add_controller(click)
            self._last_pic.set_cursor_from_name("pointer")
            if cover is None or facts is None:
                if cover is None:
                    self._art_wait.setdefault((artist.lower(), title.lower()), []).append(self._last_pic)
                self.app.want_song_art(artist, title, cover is None, facts is None)
        self._played_before = rows[-1][0]

    def song_art_got(self, artist: str, title: str, cover: str, album: str | None, facts: str | None) -> None:
        """A played song's picture and track info looked up: its rows in HRÁLO show them (the picture once it's
        fetched)."""
        url = small_cover(cover) if cover else ""
        for pic in self._art_wait.pop((artist.lower(), title.lower()), []):
            if url:
                pic.img_url = url
                if self.app.show_pics.get(url):
                    pic.set_from_paintable(self.app.show_pics[url])
        for row in self._played_rows:
            if (row["artist"].lower(), row["title"].lower()) == (artist.lower(), title.lower()):
                row["cover"] = cover or row["cover"]
                row["album"] = row["album"] or album
                row["facts"] = row["facts"] or facts
                if row.get("label"):
                    row["label"].set_text(self._played_line(row))
        if url:
            self.app.want_show_pic(url)

    def _played_line(self, row: dict) -> str:
        """A played song's line under it: the album, its first release, the show."""
        return " · ".join(x for x in (plain_title(row["album"] or ""), self.app.cz(row["facts"] or ""), row["show"]) if x)

    def _glass_button(self, icon: str, size: int, tooltip: str, on_click, color=None) -> GlassCard:
        """A button in a round dish of glass, like pause and stop below the cover (it pumps under the mouse);
        `size` px at the window's base size, growing with it."""
        btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
        btn.add_css_class("disc-btn")
        btn.connect("clicked", lambda *_: on_click())
        disc = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        disc.add_css_class("glass-disc")
        disc.mag, disc.concave, disc.depth, disc.color = 1.3, True, False, color
        disc.drop = True  # the letters' shadow: 3D
        disc.set_hexpand(False)
        disc.set_homogeneous(True)
        disc.append(btn)
        disc.btn, disc.size = btn, size
        self.backdrop.watchers.append(disc)
        self.cover.watchers.append(disc)
        hold_while_hovered(disc)
        self._glass_buttons.append(disc)
        self._size_glass_button(disc)
        return disc

    def _size_glass_button(self, disc) -> None:
        px = disc.size * self._scale
        disc.radius = px / 2
        disc.set_size_request(int(px), int(px))
        if getattr(disc, "pad", False):  # it pumps 12 % bigger: half of that on each side, and a little more
            m = math.ceil(px * 0.08)
            disc.set_margin_start(m), disc.set_margin_end(m), disc.set_margin_top(m), disc.set_margin_bottom(m)
        disc.btn.get_first_child() and disc.btn.get_first_child().set_pixel_size(int(px * 0.42))
        disc.queue_draw()

    def _drop_glass_buttons(self) -> None:
        """The dishes of rows no longer listed: not redrawn along with the cover any more."""
        gone = [d for d in self._glass_buttons if d.get_root() is None]
        for d in gone:
            self._glass_buttons.remove(d)
            for view in (self.backdrop, self.cover):
                if d in view.watchers:
                    view.watchers.remove(d)

    def _track_row(self, row: dict) -> None:
        """A row's play / pause and stop buttons as the song stands (playing, paused, being looked for)."""
        t = self.app.song_track
        mine = bool(t and (t["artist"], t["title"]) == (row["artist"], row["title"]))
        state = t["state"] if mine else ""
        play = row["play"]
        play.btn.set_icon_name("media-playback-pause-symbolic" if state == "playing"
                               else "content-loading-symbolic" if state == "finding"
                               else "dialog-warning-symbolic" if state == "failed"
                               else "media-playback-start-symbolic")
        self._size_glass_button(play)
        play.btn.set_tooltip_text(t["note"] if mine else "Přehrát z YouTube (studiová verze, nejlepší kvalita)")
        # red like the radio's pause; paused, clear like its stop (the colours switched)
        play.color = None if mine and state == "paused" else (227, 5, 26, 0.55)
        play.queue_draw()

    def track_changed(self) -> None:
        """The HRÁLO song playing changed: the rows' buttons and the main screen show it."""
        for row in self._played_rows:
            if "play" in row:
                self._track_row(row)
        self.refresh()

    def view_played(self, i: int | None) -> None:
        """A song of HRÁLO on the main screen like the radio's (its cover, its lines, pause, stop, volume) and
        playing; back (or Esc) returns to the radio and to HRÁLO where it was."""
        if i is None or not 0 <= i < len(self._played_rows):
            return
        self._hralo_at = self.puddle_scroll.get_vadjustment().get_value()
        row = self._played_rows[i]
        self.app.show_track({k: row[k] for k in ("at", "artist", "title", "album", "show", "cover", "facts",
                                                 "shown_artist", "shown_title")})
        self.info_water.hide()

    def back_from_track(self) -> None:
        """Back from a HRÁLO song: the panel open again on HRÁLO, scrolled where it was."""
        if self.puddle_open:
            return
        self.toggle_water(self.info_water)
        self._puddle_go(self.PUDDLE_MODES.index("HRÁLO"))
        at = getattr(self, "_hralo_at", 0.0)

        def again(tries=40):
            adj = self.puddle_scroll.get_vadjustment()
            if adj.get_upper() - adj.get_page_size() < at and self._played_before and tries:
                self._played_more(force=True)  # the older plays it had loaded
            if adj.get_upper() - adj.get_page_size() >= at or not tries:
                adj.set_value(at)
                self._puddle_touched = time.monotonic()  # (it waits before scrolling by itself)
                return False
            GLib.timeout_add(50, again, tries - 1)
            return False
        GLib.timeout_add(80, again)

    def played_new(self) -> None:
        """A song just logged: HRÁLO open at its top shows it (further down, it's there on the next look)."""
        if self.PUDDLE_MODES[self.puddle_mode] == "HRÁLO" and self.puddle_open \
                and self.puddle_scroll.get_vadjustment().get_value() < 40:
            self._fill_puddle()

    def _puddle_today(self, shows: list, cur, mins: int) -> None:
        """The show on air pinned one show from the top (it may have begun yesterday evening), today's shows
        before it above, greyed a little, the coming ones below; they scroll behind it, unseen there."""
        a = self.app
        for show in shows:
            if show is not cur and show["start"] <= mins:
                when = f'{show["start"] // 60}:{show["start"] % 60:02d}'
                self._puddle_block(when, show["title"], a.cz(show.get("desc") or ""), img=show.get("img") or "",
                                   past=True)
        self._puddle_mark = Gtk.Box()  # where the show on air would be: a gap as tall as the pinned one
        self.puddle_list.append(self._puddle_mark)
        if cur:
            host = a.host if a.host and norm(a.host) != norm(cur["title"]) else None
            self._puddle_block("ON AIR", cur["title"], a.cz(" · ".join(x for x in (host, cur["desc"]) if x)),
                               now=True, img=cur.get("img") or "", into=self.puddle_pin)
            self.puddle_pin.set_visible(True)
        for show in shows:
            if show is not cur and show["start"] > mins:
                when = f'{show["start"] // 60}:{show["start"] % 60:02d}'
                self._puddle_block(when, show["title"], a.cz(show.get("desc") or ""), img=show.get("img") or "")
        if not shows and not cur:
            self._puddle_block("", "", a.cz("Program pro tento den zatím není."))
        self.puddle_list.append(Gtk.Box())  # room below (_center_now)
        GLib.idle_add(self._center_now)

    def _puddle_scroll_step(self, _w, clock) -> bool:
        if not self.puddle_open and not self.puddle.get_visible():
            self._puddle_tick = None
            return GLib.SOURCE_REMOVE
        t = clock.get_frame_time() / 1e6
        last, self._puddle_last = self._puddle_last, t
        if last is None or time.monotonic() - self._puddle_touched < self.PUDDLE_WAIT:
            return GLib.SOURCE_CONTINUE  # every view scrolls by itself, a while after it's shown or the wheel moved
        adj = self.puddle_scroll.get_vadjustment()
        adj.set_value(adj.get_value() + self.PUDDLE_SPEED * (t - last))
        if self.PUDDLE_MODES[self.puddle_mode] == "ZAJÍMAVOSTI" and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 80 \
                and time.monotonic() - getattr(self, "_puddle_more_at", 0) > 4:  # near the end: one more
            self._puddle_more_at = time.monotonic()
            item = self.app.next_curiosity()
            if item:
                self.puddle_items.append(item)
                self._puddle_block("", item[1] or "Rádio Beat", item[2], img=item[3] if len(item) > 3 else "")
        return GLib.SOURCE_CONTINUE

    def _scroll_typed(self) -> bool:
        adj = self.sub_scroll.get_vadjustment()
        adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))  # the line being typed stays in view
        return False

    def _stop_typing(self) -> None:
        if getattr(self, "_type_id", 0):
            GLib.source_remove(self._type_id)
        self._type_id = 0
        self._typing_text = ""
        self.show_sub.set_lines(5)
        self.show_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub_scroll.set_size_request(-1, -1)
        self.sub_scroll.get_vadjustment().set_value(0)

    def blink(self) -> None:
        """A new song: its lines and the show card flash briefly."""
        labels = (self.song, self.artist, self.album, self.show_title, self.show_sub, self.show_next)
        for w in labels:
            w.remove_css_class("flash")
        GLib.idle_add(lambda: [w.add_css_class("flash") for w in labels] and False)
        if getattr(self, "_unflash", None):
            GLib.source_remove(self._unflash)
        self._unflash = GLib.timeout_add(1000, lambda: ([w.remove_css_class("flash") for w in labels],
                                                        setattr(self, "_unflash", None))[0] and False)
        self.show_card.blink()

    def _arrange(self) -> bool:
        """Show card beside the song (wide window, as tall as the song's lines,
        text centred in it) or above it (narrow)."""
        if self._wide:
            self.info_row.set_orientation(Gtk.Orientation.HORIZONTAL)
            # The show gets 55 % of the width: its text fits.
            self.show_card.set_size_request(int(self.get_width() * 0.55), -1)
            self.info_row.set_spacing(int(24 * self._scale))
            self.info_row.reorder_child_after(self.show_card, self.info)
            self.show_card.set_valign(Gtk.Align.FILL)
            self.show_card.set_halign(Gtk.Align.FILL)
            self.show_card.set_margin_bottom(0)
        else:
            self.info_row.set_orientation(Gtk.Orientation.VERTICAL)
            self.show_card.set_size_request(-1, -1)
            self.info_row.set_spacing(6)
            # The window's width (not its own): the wrapped text's height is then worked out for the width it
            # really gets, and the lines don't overlap.
            self.show_card.set_halign(Gtk.Align.FILL)
            self.info_row.reorder_child_after(self.show_card, None)
            self.show_card.set_valign(Gtk.Align.FILL)
            self.show_card.set_margin_bottom(10)
        return False

    def _apply_scale(self) -> bool:
        """Text and the show picture grow with the window: in full screen
        they fill the screen instead of staying window-sized."""
        f = self._scale
        # the song's lines as big as fit their place (beside the cover, below the logo, above the buttons)
        info = ("song", "artist", "album", "facts")
        css = "".join(f".{cls} {{ font-size: {pt * f * (self._fit if cls in info else 1):.1f}pt; }}\n"
                      for cls, pt in TEXT_PT.items() if cls != "pic")
        # the buttons grow too, so they don't look lost next to the text
        css += (f".round {{ min-width: {56 * f:.0f}px; min-height: {56 * f:.0f}px; }}\n"
                f".play-button {{ min-width: {68 * f:.0f}px; min-height: {68 * f:.0f}px; }}\n"
                f".round image {{ -gtk-icon-size: {16 * f:.0f}px; }}\n"
                f".vol-icon {{ -gtk-icon-size: {22 * f:.0f}px; }}\n"
                f".overlay-header button {{ min-width: {34 * f:.0f}px; min-height: {34 * f:.0f}px; }}\n"
                f".overlay-header button image {{ -gtk-icon-size: {16 * f:.0f}px; }}\n"
                f".show-card {{ border-radius: {28 * max(1.0, f):.0f}px; }}\n"
                # the gear menu's text grows too (on a Full HD TV the standard sizes were too small to read)
                f".gear-menu label {{ font-size: {8.5 * f:.1f}pt; }}\n"
                f".gear-menu row .title {{ font-size: {9.5 * f:.1f}pt; }}\n"
                f".gear-menu row .subtitle, .gear-menu label.caption {{ font-size: {8 * f:.1f}pt; }}\n"
                )
        self.scale_css.load_from_data(css.encode())
        self.show_pic.set_pixel_size(int(TEXT_PT["pic"] * f))
        self.logo.set_pixel_size(int(34 * f))
        # the text starts below the top bar (the logo, the buttons), however big they are
        self.inner.set_margin_top(int(34 * max(1.0, f) + 48))
        for tile in self.head_glass:
            tile.radius = 12 * f
            tile.set_size_request(int(34 * f), int(34 * f))
            btn = tile.get_first_child()
            if btn is not None:
                btn.set_size_request(-1, -1)
            tile.queue_draw()
        GLib.idle_add(self._square_tiles)
        for disc in getattr(self, "_glass_buttons", []):
            self._size_glass_button(disc)
        for disc, size in zip(self.discs, (68, 56, 46)):  # pause, stop (a little smaller), speaker (smaller still)
            disc.radius = size * f / 2
            disc.set_size_request(int(size * f), int(size * f))
            disc.queue_draw()
        self.show_card.radius = 28 * max(1.0, f)  # corners r 28, straight sides between (bigger only in full screen)
        # Room inside the glass (margins, not CSS padding: the glass is drawn in the content box).
        v, hz = int(16 * f), int(20 * f)
        self.show_pic.set_margin_start(hz)
        self.show_card_text.set_margin_end(hz)
        for w in (self.show_pic, self.show_card_text):
            w.set_margin_top(v)
            w.set_margin_bottom(v)
        self.show_card.queue_draw()
        self._arrange()
        self._scale_pending = False
        self.queue_resize()  # lays out with the new sizes, then the fit is checked
        return False

    def _square_tiles(self) -> bool:
        """The top tiles ('i', gear, close) one size, square: the biggest one's."""
        side = 0
        for tile in self.head_glass:
            for o in (Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL):
                side = max(side, tile.get_first_child().measure(o, -1)[1] if tile.get_first_child() else 0)
        for tile in self.head_glass:
            tile.set_size_request(side, side)
            btn = tile.get_first_child()
            if btn is not None:
                btn.set_size_request(side, side)
        return False

    def apply_look(self) -> None:
        """Text style (default / Beat Radio) and how much the backgrounds
        behind the text darken the cover (0 to 100 %, 100 = as designed)."""
        a = self.app
        k = max(0, min(100, a.settings.get("shade", 100))) / 100
        css = (".scrim-shade { background: linear-gradient(to top, "
               f"rgba(0,0,0,{0.92 * k:.3f}) 0%, rgba(0,0,0,{0.75 * k:.3f}) 55%, rgba(0,0,0,0) 100%); }}\n")
        if a.settings.get("font", "beat") == "beat" and a.fonts_ok:
            css += BEAT_CSS
        for name in EFFECTS:
            EFFECTS[name] = a.settings.get("fx_" + name, True)
        if not EFFECTS["gpu"] and GPU.ctx is not None and GPU.ok is False:
            GPU.ok, GPU.why = True, getattr(GPU, "name", GPU.why)  # it started once and failed while drawing: switching it off and on tries again
        if not EFFECTS["shadows"]:
            css += NO_SHADOW_CSS
        if not EFFECTS["anim"]:
            css += NO_ANIM_CSS
        for w in getattr(self, "waters", []):
            w.body.edge_mirror = EFFECTS["mirror"]
        if getattr(self, "clock", None):
            self.clock.queue_draw()
            self.names.queue_draw()
        if getattr(self, "snow", None):
            self.snow.update()  # (falling with the season's switch, still without animations)
        if getattr(self, "logo", None) and not EFFECTS["anim"]:
            self.logo.hover = False
        self.refresh_effects()
        self.look_css.load_from_data(css.encode())
        waters = [g for w in getattr(self, "waters", []) for g in (w.body, w.neck)]
        pill = [self.quality_pill] if getattr(self, "quality_pill", None) else []  # (it was always at full: black)
        for card in (self.show_card, *self.discs, *self.head_glass, *waters, *pill):
            card.shade = k
            card.queue_draw()
        if getattr(self, "quality_pill", None):
            self.quality_pill.queue_draw()  # (its shadow on or off)

    def refresh_effects(self) -> None:
        """The effect switches' notes: what each does, and what it's doing now."""
        for key, row in getattr(self, "fx_rows", {}).items():
            if row.get_active() != EFFECTS[key]:
                row.set_active(EFFECTS[key])  # changed elsewhere (a click on the date and time)
        for key, text in (("mirror", self.mirror_text), ("anim", self.app.anim_text),
                          ("season", lambda: season_text(self.snow.bytes() if getattr(self, "snow", None) else None)),
                          ("shadows", self.shadow_text), ("gpu", gpu_text)):
            row = getattr(self, "fx_rows", {}).get(key)
            if row:
                t = text()
                if row.get_subtitle() != t:
                    row.set_subtitle(t)

    def shadow_text(self) -> str:
        if not EFFECTS["shadows"]:
            return "Vypnuto: tlačítka, ikony, pilulky a posuvník jsou ploché"
        return ("Tlačítka, jejich ikony, pilulky a posuvník hlasitosti vrhají stín jako písmo (dolů a doleva): víc "
                "vystupují · paměť: žádná vlastní (GTK je kreslí v každém snímku)")

    def mirror_text(self) -> str:
        if not EFFECTS["mirror"]:
            return "Vypnuto: okraje panelů ⓘ a ⚙ jsou jen lupa a rybí oko"
        what = "Horní a dolní okraj panelů ⓘ a ⚙ zrcadlí vzhůru nohama jen písmo těsně za nimi (třeba název na obalu); jinde jen lupa a rybí oko"
        if TEXT_MAP["busy"]:
            now = "hledá se písmo v obrázku…"
        elif TEXT_MAP["key"] is None:
            now = "písmo se hledá po otevření panelu"
        else:
            n = len(TEXT_MAP["cells"])
            now = f"v tomto obrázku nalezeno písmo na {n} ploškách" if n else "v tomto obrázku žádné písmo nenalezeno"
        strips = sum(tex_bytes(t) for w in getattr(self, "waters", []) if w.body._mirror_tex
                     for t, _ in w.body._mirror_tex[1])
        cells = TEXT_MAP["cells"]
        ram = sys.getsizeof(cells) + len(cells) * 64  # the lettering map: a set of cells (each a small tuple)
        return f"{what} · teď: {now} · paměť: GPU {mem_text(strips)} (zrcadlené pruhy), RAM {mem_text(ram)} (mapa písma)"
    def on_double(self, _gesture, n, x, y) -> None:
        if n != 2:
            return
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not self:
            if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Adw.HeaderBar, ClockView)):
                return  # their own clicks (the title bar maximises on double click; the clock: its decorations)
            w = w.get_parent()
        self.toggle_full()

    def on_wheel(self, _ctl, _dx, dy) -> bool:
        if not dy:
            return False
        self.app.nudge_volume(-5 if dy > 0 else 5)
        return True

    def on_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape and (self.app.track_view or self.app.song_track):
            self.app.back_to_radio()
            return True
        if keyval == Gdk.KEY_F11 or (keyval == Gdk.KEY_Escape and self.is_fullscreen()):
            self.toggle_full()
            return True
        return False

    def on_close(self, *_):
        self.set_visible(False)  # keeps playing; the tray icon brings it back
        return True

    def refresh(self) -> None:
        a = self.app
        if a.track_view:
            self._refresh_track()
            return
        self.back_disc.set_visible(bool(a.song_track))
        info = a.info or {}
        title = plain_title(info.get("title") or a.song or "Rádio Beat")
        if title != self.song.get_text() and self.song.get_text():
            self.blink()
        self.song.set_text(title)
        artist = info.get("artist") or a.artist_raw or "První bigbeat u nás"
        self.artist.set_text(artist)
        self.album.set_text(plain_title(info.get("album") or ""))
        self.album.set_visible(bool(info.get("album")))
        self.facts.set_text(a.cz(a.facts))
        self.facts.set_visible(bool(a.facts))
        self._place_info()
        if a.logo_tex is not None and self.logo.get_paintable() is not a.logo_tex:
            self.logo.set_from_paintable(a.logo_tex)
        pic = a.cover_tex or a.logo_tex
        # An enhancing step of the picture already up is redrawn slowly; a new one morphs.
        redraw = pic is not None and a.refined == (self.cover.pic, pic)
        self.cover.set_paintable(pic, redraw)
        GLib.idle_add(self._fit_info)
        if getattr(self, "puddle_open", False):
            GLib.idle_add(lambda: (self._place_puddle(), False)[1])  # beside the new cover
        self.backdrop.set_paintable(pic, redraw)
        cur, nxt = on_air(a.schedule, time.localtime())
        turn = self._turn_content if self.card_turn == "song" else None
        self.show_card.set_visible(False)  # its info lives in the 'i' button's puddle now
        self.badge.set_text(a.badge(turn[0]) if turn else "ON AIR")
        if turn:
            self.show_title.set_text(turn[1])
            facts = a.cz(a.facts) if turn[0] == "NOW PLAYING" else ""
            self.show_next.set_text(facts)
            self.show_next.set_visible(bool(facts))
            self.show_sub.set_visible(True)  # its text is typed (_type)
            self.show_pic.set_from_paintable(turn[3])
        elif cur:
            self.show_title.set_text(cur["title"])
            host = a.host if a.host and norm(a.host) != norm(cur["title"]) else None
            self.show_sub.set_text(" · ".join(x for x in (host, cur["desc"]) if x))
            self.show_sub.set_visible(bool(host or cur["desc"]))
            if nxt:
                when = f'{nxt["start"] // 60}:{nxt["start"] % 60:02d}'
                then = "Potym" if a.settings.get("ostrava", True) else "Potom"
                self.show_next.set_text(a.cz(f'{then} {nxt["day"] + " " if nxt["day"] else ""}{when} · {nxt["title"]}')
                                        if a.settings.get("ostrava", True) else
                                        f'{then} {nxt["day"] + " " if nxt["day"] else ""}{when} · {nxt["title"]}')
            self.show_next.set_visible(nxt is not None)
            tex = a.show_pics.get(cur["img"]) if cur["img"] else None
            self.show_pic.set_from_paintable(tex or a.logo_tex)
        self.vol_icon.set_from_icon_name("audio-volume-muted-symbolic" if a.muted else "audio-volume-high-symbolic")
        self.discs[2].set_tooltip_text("Zapnout zvuk" if a.muted else "Ztlumit")
        paused = a.song_track["state"] != "playing" if a.song_track and a.song_live() else a.paused  # a HRÁLO song: its state
        self.play_btn.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        self.play_btn.set_tooltip_text("Přehrát" if paused else "Pozastavit")
        if a.error:
            self.status.set_text(a.error)
        elif a.paused:
            self.status.set_text("Pozastaveno")
        elif a.tries:
            self.status.set_text(f"Znovu se připojuje… ({a.stream()[1]})")
        else:
            # (the codec and bitrate are in the quality menu now, bottom right)
            self.status.set_text("" if a.codec or a.bitrate else "Připojuje se…")
        self._quality_text()
        ids = [s[0] for s in a.streams]  # the pill says what's chosen (its server note left to the menu)
        tab = dict((s[0], s[1]) for s in a.streams)[q if (q := a.settings["quality"]) in ids else ids[0]]
        tab = tab.split(" (")[0]
        if self.quality_tab.get_label() != tab:
            self.quality_tab.set_label(tab)
        self.update_note.set_text(a.update_text)
        self.update_btn.set_label("Aktualizovat" if a.update_ready else "Zkontrolovat")
        self.update_btn.set_sensitive(not a.update_busy)
        (self.update_btn.add_css_class if a.update_ready else self.update_btn.remove_css_class)("suggested-action")
        if abs(self.vol.get_value() - a.volume_now()) >= 1:
            self.vol.set_value(a.volume_now())
        self.card_row.set_subtitle(a.card_text())
        self.recognise_row.set_subtitle(GLib.markup_escape_text(
            "Když stream nepojmenuje skladbu (znělky, přestávky), jednou za minutu se 12s ukázka rozpozná přes "
            f"neoficiální API Shazamu (shazamio). {a.recognise_text}"))


def _track_refresh(self) -> None:
    """The main screen for a HRÁLO song: like the radio's (its cover big, its lines, pause, stop, volume) with
    back beside pause; the status says what plays (the quality, which video)."""
    a = self.app
    tv, t = a.track_view, a.song_track
    title = plain_title(tv.get("shown_title") or tv["title"])
    if title != self.song.get_text() and self.song.get_text():
        self.blink()
    self.song.set_text(title)
    self.artist.set_text(tv.get("shown_artist") or tv["artist"])
    self.album.set_text(plain_title(tv.get("album") or ""))
    self.album.set_visible(bool(tv.get("album")))
    self.facts.set_text(a.cz(tv.get("facts") or ""))
    self.facts.set_visible(bool(tv.get("facts")))
    self._place_info()
    small = a.show_pics.get(small_cover(tv["cover"])) if tv.get("cover") else None
    pic = tv.get("tex") or small or a.logo_tex
    redraw = (small is not None and self.cover.pic is small and pic is not small  # the big one over the small
              or a.refined == (self.cover.pic, pic))  # or its enhanced version: drawn in slowly
    self.cover.set_paintable(pic, redraw)
    self.backdrop.set_paintable(pic, redraw)
    GLib.idle_add(self._fit_info)
    self.back_disc.set_visible(True)
    playing = bool(t and t["state"] == "playing")
    self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
    self.play_btn.set_tooltip_text("Pozastavit" if playing else "Přehrát")
    self.vol_icon.set_from_icon_name("audio-volume-muted-symbolic" if a.muted else "audio-volume-high-symbolic")
    when = time.localtime(tv["at"])
    played = f"Hrálo {DAYS[when.tm_wday]} {when.tm_mday}. {when.tm_mon}. {when.tm_hour}:{when.tm_min:02d}"
    self.status.set_text(" · ".join(x for x in ((t["note"] if t else "Zastaveno"), played) if x))
    if abs(self.vol.get_value() - a.volume_now()) >= 1:
        self.vol.set_value(a.volume_now())


Window._refresh_track = _track_refresh


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
        self.show_src: dict[str, bytes] = {}  # show picture url -> its bytes, for enhancing
        self.show_done: set[str] = set()  # show pictures sent for enhancing
        self.codec = self.bitrate = None
        self.facts = ""  # first release, where it was recorded
        self.news: list[dict] = []  # radiobeat.cz's news (AKTUALITY in the 'i' panel)
        self.about = ""  # a few sentences about the song (Wikipedia)
        self.song_shown = False  # its turn came already (then the mix)
        self.history: list[str] = []  # artists played lately (oldest first, the newest last)
        self.pools: dict[str, dict] = {}  # name -> its Wikipedia article as sentences
        self.orders: dict[str, list] = {}  # name -> its sentences not shown yet (shuffled)
        self.ready = collections.deque()  # mix items made ahead (kind, name, text)
        self.shown = self._shown_load()  # what the mix showed within the last month
        self._feed = threading.Event()
        threading.Thread(target=self._feeder, daemon=True).start()
        self.db = History(os.path.join(DATA_DIR, "history.db"))
        self.song_track = None  # a song from HRÁLO playing (from YouTube): {artist, title, state, note}
        self.tmpv = None  # its own mpv
        self.track_view = None  # that song on the main screen instead of the radio's (back returns)
        self._art_asked: set = set()  # played songs whose picture was looked up this run (HRÁLO)
        self._art_queue: list = []
        self._art_busy = False
        self.host_info: dict[str, str] = {}  # every host seen -> what Wikipedia says (Czech)
        self._last_lead = ""
        self.station_about = ""
        self.band_about = self.host_about = ""  # 'Did you know?' about the artist, about who's on air
        self.cover_tex = None
        self.logo_tex = None
        self.refined = None  # (before, after) of the last enhancing step: redrawn in, not morphed
        self.enhancer = Enhancer(on_status=self._enhance_status_changed, on_picture=self._picture_changed)
        self.share = CacheShare(self.enhancer, self._share_changed, self.db)
        self.share.hub_url = self.settings.get("hub_url", "")
        self.db.on_play = lambda: GLib.idle_add(lambda: (self.win and self.win.played_new(), False)[1])
        self.share.set_on(self.settings.get("share", True))
        if self.settings.get("npu", False):
            self.enhancer.npu_wanted = True
            threading.Thread(target=lambda: (self.enhancer.npu_probe(), GLib.idle_add(self._npu_checked)),
                             daemon=True).start()
        self.card = CardVolume(self._card_changed)
        self._holds = 0  # animations running: the enhancing waits while any is
        self._frame_tick_id = None  # their frames timed meanwhile (the gear notes: Animations)
        self._frame_last = None
        self._frame_run: list[float] = []
        self._frames: list = []  # [(when, [frame intervals ms])], the last 10 minutes
        _ANIM_HOLD[0] = self.anim_hold
        self.card_vol = None  # the sound card's volume, percent (card_on)
        self.card_name = ""
        self.card_set_at = 0.0  # when the slider last set it
        self._vol_cur = self._vol_target = 0.0  # mpv's volume now, and where it's gliding to
        self.muted = False  # the speaker button
        self.recognise_text = "Vypnuto"
        self._recognise_id = 0
        self._recognising = False
        self._recognised_last = None
        self._vol_glide = self._vol_save = 0  # timers
        self.upscale_src: dict[str, tuple[str, bytes]] = {}  # kind -> (key, picture bytes), for after the install
        self.orig_tex: dict[str, object] = {}  # kind -> the picture as it came
        # the last picture enhanced and its sizes (kept over restarts: shown until the next one is done)
        last = self.settings.get("last_picture") or ["Zatím žádný obrázek nebyl vylepšen", ""]
        self.picture_text, self.picture_size = cz_steps(last[0]), last[1]
        threading.Thread(target=recaption_cache, daemon=True).start()
        self.picture_doing = ""  # the one being enhanced now
        self.icon_text = ""
        self._lookup_id = 0
        self._started = False
        self.last_audio = time.monotonic()  # when playback last moved on
        self.last_pos = None
        self.tries = 0  # reconnects since audio last flowed
        self.alt = 0  # streams moved on from the chosen one (backup servers)
        self.last_reconnect = 0.0
        self.quitting = False
        self.update_text = f"Verze {APP_VERSION} · zatím nezkontrolováno"
        self.update_ready = False
        self.update_busy = False
        for name, which in (("info", "info_water"), ("settings", "gear_water")):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_, which=which: self.win and self.win.toggle_water(getattr(self.win, which)))
            self.add_action(act)
        self.set_accels_for_action("app.info", ["i"])
        self.set_accels_for_action("app.settings", ["g"])
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.quit_all())
        self.add_action(q)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    # startup / command line
    TEXT_SCALE = 1.6364  # the desktop's text size as on Omarchy (its 20 px fonts): the same on every computer

    def _fill_history(self) -> bool:
        """In the background, now and then: every song in the history lacking its picture or track info (new
        plays, another computer's, ones cleared after the lookups got better) looked up, one at a time."""
        import sqlite3
        try:
            queued = {(a.lower(), t.lower()) for a, t, *_ in self._art_queue}
            songs = self.db.missing()
            # Two computers on: each from its own end of the history (newest first on the one whose id comes
            # first), sharing what they find: each song looked up once.
            peers = list(self.share.peers.values()) if self.share.on else []
            if peers and self.share.id > min(peers):
                songs.reverse()
            for artist, title, cover, facts in songs:
                if (artist.lower(), title.lower()) not in queued:  # (asked before: plays come in later too)
                    self._art_asked.discard((artist.lower(), title.lower()))
                self.want_song_art(artist, title, bool(cover), bool(facts), soon=False)
        except sqlite3.Error:
            pass
        return GLib.SOURCE_CONTINUE

    def do_startup(self):
        Adw.Application.do_startup(self)
        # text as big as on this desktop everywhere (KDE says 1.0, Omarchy 1.64): the 'i' panel and the menus
        # look the same on the DeskMini's full HD as on a 4K screen at 2x
        Gtk.Settings.get_default().set_property("gtk-xft-dpi", int(96 * 1024 * self.TEXT_SCALE))
        GLib.timeout_add_seconds(60, lambda: (self._fill_history(), False)[1])
        GLib.timeout_add_seconds(1800, self._fill_history)
        self.fonts_ok = load_fonts()
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
        GLib.idle_add(lambda: (self._enhance("logo"), False)[1])  # enhanced before: from the cache at once
        if self.settings.get("recognise", False):
            self.set_recognise(True)
        threading.Thread(target=lambda: GLib.idle_add(setattr, self, "station_about",
                                                      name_about("Rádio Beat", HOST_WORDS + MUSIC_WORDS, ("cs", "en"))),
                         daemon=True).start()
        threading.Thread(target=self._load_past, daemon=True).start()
        threading.Thread(target=self._install_upscaler, daemon=True).start()
        if self.card_on():
            self.card.start()
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
        GLib.timeout_add_seconds(2, lambda: (self.check_update(), False)[1])  # at start (installed and restarted if new)
        GLib.timeout_add_seconds(AUTO_UPDATE_S, lambda: (self.check_update(), True)[1])  # then every 5 minutes

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        first = not self._started
        self._started = True
        if first:
            self.play()
        if first and ("--hidden" in args or ("--autostart" in args and not self.settings.get("autostart_full", False))):
            return 0  # (started at login: as the tray icon only, unless set to open in full screen)
        if "--autostart" in args and first:
            self.show_window()
            self.win.fullscreen()
            return 0
        self.show_window()
        return 0

    def show_window(self):
        if not self.win:
            self.win = Window(self)
            if self.settings.get("start_full", True):
                self.win.fullscreen()  # opens in full screen; a double click makes it a window (in the corner)
        self.win.minimised = False
        self.win.set_visible(True)
        self.win.present()

    def toggle_window(self):
        """The tray icon: brings the window up, or minimises it when it's up (like its taskbar button).
        Wayland doesn't tell an app it's minimised: the app remembers it did it, until the window is active again."""
        w = self.win
        if w and w.get_visible() and not w.minimised and w.is_active():
            if is_kde():
                w.minimised = True
                w.minimize()
            else:
                w.set_visible(False)  # not every desktop minimises (Hyprland): to the tray
        else:
            self.show_window()
            if w and w.is_fullscreen():  # always a window in the bottom-right corner (KWin's script places it)
                w.unfullscreen()

    # updates / tray placement
    def set_look(self, **changes) -> None:
        self.settings.update(changes)
        save_settings(self.settings)
        if self.win:
            self.win.apply_look()

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
        self.update_busy, self.update_text = True, "Kontroluje se…"
        self.update()

        def work():
            ready, text = update_status()
            GLib.idle_add(self._checked, bool(ready), text)
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, ready, text):
        self.update_busy, self.update_ready, self.update_text = False, ready, text
        self.update()
        if ready and self.settings.get("auto_update", True):
            self.update_clicked()  # installs it and restarts
        return False

    def update_clicked(self):
        if not self.update_ready:
            self.check_update()
            return
        self.update_busy, self.update_text = True, "Aktualizuje se…"
        self.update()

        def work():
            err = run_update()
            GLib.idle_add(self._updated, err)
        threading.Thread(target=work, daemon=True).start()

    def _updated(self, err):
        if err:
            self.update_busy, self.update_text = False, f"Aktualizace selhala: {err}"
            self.update()
            return False
        # Start the new version once this one is gone (same app id), then quit; with the window only if it was up.
        shown = self.win is not None and self.win.get_visible()
        # the shared server too, on the computer running it (nothing happens elsewhere)
        subprocess.Popen(["systemctl", "--user", "try-restart", "radio-beat-hub.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["sh", "-c", f"sleep 2; exec {launcher()}{'' if shown else ' --hidden'}"],
                         start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.quit_all()
        return False

    def _probe_flac(self):
        url = probe_flac()
        if url:
            GLib.idle_add(self._add_flac, url)

    def _add_flac(self, url):
        self.streams.insert(0, ("flac", "FLAC (bezeztrátově)", url))
        if self.win:
            self.win.fill_quality()

    def start_mpv(self):
        try:
            self.mpv = Mpv(self.on_prop)
            self._vol_cur = self._vol_target = 100 if self.card_on() else self.settings["volume"]
            self.mpv.volume(self._vol_cur)
            if self.muted and not self.card_on():
                self.mpv.mute(True)  # a restarted mpv stays muted
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

    # --- a song from HRÁLO, played from YouTube (the radio pauses meanwhile) -------------------------
    def play_song(self, artist: str, title: str) -> None:
        t = self.song_track
        if t and (t["artist"], t["title"]) == (artist, title) and t["state"] != "failed":
            self.pause_song()
            return
        self.stop_song(resume=False)
        self.song_track = {"artist": artist, "title": title, "state": "finding", "note": "Hledám na YouTube…"}
        self._track_changed()
        track = self.song_track

        def find():
            found = find_track(artist, title)
            GLib.idle_add(self._song_found, track, found)
        threading.Thread(target=find, daemon=True).start()

    def _song_found(self, track, found) -> bool:
        if track is not self.song_track:
            return False  # another song chosen (or stopped) meanwhile
        if not found:
            track.update(state="failed", note="Na YouTube se nenašla")
            self._track_changed()
            return False
        url, what = found
        if not self.tmpv:
            try:
                self.tmpv = Mpv(self._track_prop, "track")
            except (RuntimeError, OSError) as e:
                track.update(state="failed", note=str(e))
                self._track_changed()
                return False
        if self.mpv:  # the radio goes on, muted: live when the song's over (enhancing, history: as ever)
            self.mpv.mute(True)
        self.tmpv.volume(self._vol_cur)
        self.tmpv.mute(self.muted)
        self.tmpv.pause(False)
        self.tmpv.play(url)
        track.update(state="playing", note=what, started=False)
        self._track_changed()
        self.update()
        return False

    def pause_song(self) -> None:
        t = self.song_track
        if not t or t["state"] not in ("playing", "paused") or not self.tmpv:
            return
        t["state"] = "paused" if t["state"] == "playing" else "playing"
        self.tmpv.pause(t["state"] == "paused")
        if self.mpv:  # the song paused: the radio heard meanwhile; playing again: the radio muted again
            self.mpv.mute(self.muted or t["state"] == "playing")
        self._track_changed()

    def stop_song(self, resume: bool = True) -> None:
        if not self.song_track:
            return
        self.song_track = None
        if self.tmpv:
            self.tmpv.stop()
        if self.mpv:
            self.mpv.mute(self.muted)  # the radio heard again (it played on, live)
        self._track_changed()

    def show_track(self, row: dict) -> None:
        """A HRÁLO song on the main screen (its big cover, its lines) and playing; the radio muted meanwhile."""
        self.track_view = dict(row, tex=None)
        t = self.song_track
        tv = self.track_view
        if row.get("cover"):
            def got(data):
                try:
                    tv["tex"] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)) if data else None
                except GLib.Error:
                    tv["tex"] = None
                if self.track_view is tv:
                    self.update()
                    if tv["tex"]:  # enhanced like the radio's cover (upscaled to the screen), not as fetched
                        key = row["cover"] + ("#square2" if "ytimg.com" in row["cover"] else "")
                        self.orig_tex["track"], self.upscale_src["track"] = tv["tex"], (key, data)
                        self._enhance("track")
                return False
            threading.Thread(target=lambda: GLib.idle_add(got, square_cover(fetch(row["cover"]))), daemon=True).start()
        if not (t and (t["artist"], t["title"]) == (row["artist"], row["title"]) and t["state"] != "failed"):
            self.play_song(row["artist"], row["title"])  # (already playing or paused: left as it is)
        self.update()

    def back_to_radio(self) -> None:
        """Back from a HRÁLO song: the radio's screen and sound again, HRÁLO open where it was."""
        if not self.track_view and not self.song_track:
            return
        shown, self.track_view = self.track_view, None
        self.stop_song()  # the song reset: every play button shows play again
        self.update()
        if self.win and shown:
            self.win.back_from_track()

    def _track_prop(self, name, value) -> bool:
        t = self.song_track
        if name == "mpv-exit":
            self.tmpv = None
            if t:
                self.stop_song()
        elif t and name == "idle-active":
            if not value:
                t["started"] = True
            elif t.get("started") and t["state"] in ("playing", "paused"):
                self.stop_song()  # it played to its end: the radio again
                if self.track_view:
                    self.track_view = None
                    self.update()
        return False

    def _track_changed(self) -> None:
        if self.win:
            self.win.track_changed()

    def song_live(self) -> bool:
        """The buttons below the cover act on the HRÁLO song only while it's on the main screen (it only looks
        like the radio's); otherwise they're the radio's, a song playing in HRÁLO or not."""
        return bool(self.track_view)

    def toggle_pause(self):
        if self.song_live():  # a HRÁLO song playing or on the screen: its pause
            t = self.song_track
            if t and t["state"] in ("playing", "paused"):
                self.pause_song()
            elif self.track_view and (not t or t["state"] == "failed"):
                self.play_song(self.track_view["artist"], self.track_view["title"])
            return
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

    def anim_hold(self, on: bool) -> None:
        self._holds = max(0, self._holds + (1 if on else -1))
        self.enhancer.hold(self._holds > 0)
        if self._holds and self.win and self._frame_tick_id is None:  # an animation: its frames timed
            self._frame_last = None
            self._frame_run = []
            self._frame_tick_id = self.win.add_tick_callback(self._frame_tick)

    def _frame_tick(self, _widget, clock) -> bool:
        t = clock.get_frame_time() / 1000
        if self._frame_last is not None:
            self._frame_run.append(t - self._frame_last)
        self._frame_last = t
        if self._holds:
            return GLib.SOURCE_CONTINUE
        self._frame_tick_id = None
        if len(self._frame_run) >= 10:
            self._frames.append((time.monotonic(), self._frame_run))
            self._frames = [f for f in self._frames if time.monotonic() - f[0] < 600]
            self.win.enhance_note.set_text(self.notes_text())
        return GLib.SOURCE_REMOVE

    def anim_text(self) -> str:
        what = "Tlačítka pod myší pulzují, ozubené kolo tiká, logo vlaje, šipky poskakují"
        if not self.settings.get("fx_anim", True):
            return "Vypnuto: tlačítka a logo se pod myší nehýbou (méně práce pro GPU)"
        return f"{what} · {self.frames_text()} · paměť: žádná vlastní (kreslí se v každém snímku)"

    def frames_text(self) -> str:
        """How smooth the animations run on this screen: measured, the last one and the last 10 minutes."""
        if not self._frames:
            return "zatím žádná animace nezměřena"
        hz = 60.0
        try:
            mon = self.win.get_display().get_monitor_at_surface(self.win.get_surface())
            hz = (mon.get_refresh_rate() / 1000) or 60.0
        except (AttributeError, TypeError):
            pass
        late = 1000 / hz * 1.25
        last = sorted(self._frames[-1][1])
        every = [x for _, run in self._frames for x in run]
        on_time = sum(1 for x in every if x <= late) / len(every)
        size = self.screen_px()
        where = f"{size[0]}×{size[1]} px, {hz:.0f} Hz" if size else f"{hz:.0f} Hz"
        return (f"{where}: poslední {1000 / last[len(last) // 2]:.0f} fps, nejpomalejší snímek {last[-1]:.0f} ms; "
                f"posledních 10 minut {on_time:.0%} z {len(every)} snímků včas (do {late:.0f} ms)")

    # volume: Rádio Beat's own (mpv), or the sound card's (card_on: mpv stays at 100 %, a knob on the card moves the slider)
    def card_on(self) -> bool:
        return self.card.ok and self.settings.get("card_volume", False)

    def card_text(self) -> str:
        if not self.card.ok:
            return "Vypnuto: chybí pactl (zvukové nástroje PipeWire/PulseAudio)"
        if not self.card_on():
            return "Vypnuto: posuvník je vlastní hlasitost Rádia Beat"
        return f"Posuvník je hlasitost karty {self.card_name or 'zvukové karty'}: její knoflík hýbe i posuvníkem"

    def volume_now(self) -> int:
        return self.card_vol if self.card_on() and self.card_vol is not None else self.settings["volume"]

    def set_volume(self, v: float):
        v = round(max(0.0, min(100.0, float(v))), 1)
        if self.card_on():
            if round(v) != self.card_vol:
                self.card_vol, self.card_set_at = round(v), time.monotonic()
                self.card.set(round(v))
        elif v != self.settings["volume"]:
            self.settings["volume"] = v
            self.glide_volume(v)
            if self._vol_save:
                GLib.source_remove(self._vol_save)
            self._vol_save = GLib.timeout_add(600, self._save_volume)  # once the dragging stops

    def _save_volume(self) -> bool:
        self._vol_save = 0
        save_settings(self.settings)
        return False

    def glide_volume(self, v: float) -> None:
        """mpv's volume follows softly (a fifth of a second), not in jumps."""
        self._vol_target = v
        if not self._vol_glide:
            self._vol_glide = GLib.timeout_add(16, self._glide_step)

    def _glide_step(self) -> bool:
        cur = self._vol_cur + (self._vol_target - self._vol_cur) * 0.22
        if abs(self._vol_target - cur) < 0.05:
            cur = self._vol_target
        self._vol_cur = cur
        if self.mpv:
            self.mpv.volume(cur)
        if self.tmpv:
            self.tmpv.volume(cur)
        if cur == self._vol_target:
            self._vol_glide = 0
            return False
        return True

    def toggle_mute(self) -> None:
        """The speaker button: silent, and sound again (the volume stays as set)."""
        self.muted = not self.muted
        if self.card_on():
            target = self.card.sink or "@DEFAULT_SINK@"
            threading.Thread(target=lambda: subprocess.run(["pactl", "set-sink-mute", target, "1" if self.muted else "0"],
                                                           capture_output=True), daemon=True).start()
        else:
            if self.mpv:  # (the radio stays muted while a HRÁLO song plays)
                self.mpv.mute(self.muted or bool(self.song_track and self.song_track["state"] == "playing"))
            if self.tmpv:
                self.tmpv.mute(self.muted)
        self.update()

    def nudge_volume(self, d: int):
        self.set_volume(self.volume_now() + d)
        self.update()

    def _card_changed(self, v: int, name: str):
        self.card_name = name
        # the echo of the slider's own changes would pull it back while it's being dragged
        if time.monotonic() - self.card_set_at > 0.6 or self.card_vol is None:
            self.card_vol = min(100, v)
        self.update()
        return False

    def set_card_volume(self, on: bool):
        self.settings["card_volume"] = on
        save_settings(self.settings)
        if on:
            self.card.start()
        else:
            self.card.stop()
            self.card = CardVolume(self._card_changed)  # its threads end; a new one for turning it on again
            self.card_vol = None
        if self.mpv:
            self._vol_cur = self._vol_target = 100 if on else self.settings["volume"]
            self.mpv.volume(self._vol_cur)
        self.update()

    def quit_all(self):
        self.quitting = True
        if self._vol_save:  # a volume change not saved yet
            self._save_volume()
        self.enhancer.stop_all()
        if self.tmpv:
            self.tmpv.quit()
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
                self._recognised_last = None  # the stream's own title: a song recognised before shows again
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
            news = None
            if not host_only:  # the homepage's newest and the news page's, newest first (each once)
                news = parse_news(page.decode("utf-8", "replace")) if page else []
                more = fetch(SITE + "/cs/clanky/1")
                news += parse_news(more.decode("utf-8", "replace")) if more else []
                seen, merged = set(), []
                for item in news:
                    if item["url"] not in seen:
                        seen.add(item["url"])
                        merged.append(item)
                num = lambda i: int((re.search(r"/cs/1/(\d+)/", i["url"]) or [0, 0])[1])
                news = sorted(merged, key=num, reverse=True)[:30]
            GLib.idle_add(self._programme_got, sched, host, news)
        threading.Thread(target=work, daemon=True).start()

    def _programme_got(self, sched, host, news=None):
        if news:
            self.news = news
        if sched:
            self.schedule = sched
        cur, _ = on_air(self.schedule, time.localtime())
        if host and cur and norm(host) == norm(cur["title"]):
            host = None  # the site names the show there when nobody's on air: not a host
        if host:
            self.db.host(host, cur["title"] if cur else None)
        if host and host != self.host:
            self.host, self.host_about = host, ""
            threading.Thread(target=lambda h=host: GLib.idle_add(self._got_host_about, h,
                                                                 name_about(h, HOST_WORDS, ("cs", "en"))),
                             daemon=True).start()
        self._show_pic()
        self.update()
        return False

    def _got_host_about(self, host, text):
        self.host_info[host] = text
        if host == self.host:
            self.host_about = text
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
        self._enhance_show()

    def want_song_art(self, artist: str, title: str, cover: bool = True, facts: bool = True,
                      soon: bool = True) -> None:
        """A played song lacking its picture (cover) or its track info (facts: first release…): looked up (one
        at a time, a little apart: the services' limits), remembered in the history, shown in HRÁLO."""
        key = (artist.lower(), title.lower())
        if key in self._art_asked:
            return
        self._art_asked.add(key)
        if soon:  # one HRÁLO shows: before the background's
            self._art_queue.insert(0, (artist, title, cover, facts))
        else:
            self._art_queue.append((artist, title, cover, facts))
        if not self._art_busy:
            self._art_busy = True
            threading.Thread(target=self._art_worker, daemon=True).start()

    def _art_worker(self) -> None:
        while self._art_queue:
            artist, title, want_cover, want_facts = self._art_queue.pop(0)
            lacks = self.db.lacks(artist, title)  # (the other computer may have sent them meanwhile)
            want_cover, want_facts = want_cover and lacks[0], want_facts and lacks[1]
            if not want_cover and not want_facts and not lacks[2]:
                continue
            info = lookup(artist, title)
            if lacks[2]:  # (HRÁLO shows them the next time it lists the song)
                self.db.set_names(artist, title, *proper_names(artist, title, info))
            cover = (info or {}).get("cover") or ""
            album = None if (info or {}).get("artist_photo") else (info or {}).get("album")
            if want_cover:
                self.db.song_art(artist, title, album, cover)
            facts = None
            if want_facts:
                facts = track_facts(artist, title, info)
                self.db.set_facts(artist, title, facts)
            GLib.idle_add(lambda a=artist, t=title, c=cover if want_cover else "", al=album, f=facts:
                          (self.win and self.win.song_art_got(a, t, c, al, f), False)[1])
            time.sleep(0.4)
        self._art_busy = False

    def want_show_pic(self, url: str) -> None:
        """A show's picture for the 'i' puddle's lineup: fetched once; the puddle fills it in."""
        if url and url not in self.show_pics:
            self.show_pics[url] = None  # fetching
            threading.Thread(target=lambda: GLib.idle_add(self._show_pic_got, url, fetch(url)), daemon=True).start()

    def _show_pic_got(self, url, data):
        try:
            self.show_pics[url] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)) if data else None
        except GLib.Error:
            self.show_pics[url] = None
        if self.show_pics[url]:
            self.show_src[url] = data
            self._enhance_small(url, data)
        self._enhance_show()
        self.update()
        if self.win and self.win.puddle_open:
            self.win.puddle_pics()
        return False

    def _enhance_small(self, url: str, data: bytes) -> None:
        """A picture in the 'i' panel (a show's, an artist's, a news item's) enhanced like the cover: AI-upscaled
        to twice its shown size when it's smaller (a sharp picture on any screen), sharpened, denoised;
        one after another, after the cover and the logo; then it replaces the plain one."""
        if not self.settings.get("enhance", True):
            return
        tex = self.show_pics.get(url)
        if not tex:
            return
        w, h = tex.get_width(), tex.get_height()
        px = self.enhance_px()
        if not px:
            return
        # (for the 4K full screen every computer enhances for: the same file everywhere, CacheShare)
        want = int(TEXT_PT["pic"] * 0.7 * 2 * max(1.0, min(px[0] / BASE_SIZE[0], px[1] / BASE_SIZE[1]) / 2))
        f = max(1.0, want / max(1, min(w, h)))
        size = (round(w * f), round(h * f)) if f >= UPSCALE_FROM else (w, h)

        def done(pic: bytes):
            try:
                self.show_pics[url] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(pic))
            except GLib.Error:
                return
            if self.win and self.win.puddle_open:
                self.win.puddle_pics()
        self.enhancer.request(f"pic:{url}", url, data, (w, h), size, done, screen=px)

    def enhance_small_full(self) -> None:
        """The panels closed: the small pictures shown with the quick pass get the full enhancing now."""
        for kind in list(self.enhancer.light_done):
            url = kind[4:]
            data = self.show_src.get(url)
            if data:
                self._enhance_small(url, data)

    def _enhance_show(self) -> None:
        """The show on air's picture, sharpened and denoised (AI-upscaled if
        full screen shows it bigger than it is), like the cover."""
        cur, _ = on_air(self.schedule, time.localtime())
        url = cur and cur["img"]
        tex, data, size = self.show_pics.get(url), self.show_src.get(url), self.enhance_px()
        if not url or not tex or not data or not size or url in self.show_done:
            return
        if not self.settings.get("enhance", True) or (self.enhancer.ai is None and not self.enhancer.ffmpeg):
            return
        self.show_done.add(url)
        w, h = tex.get_width(), tex.get_height()
        full = max(1.0, min(size[0] / BASE_SIZE[0], size[1] / BASE_SIZE[1]))
        px = TEXT_PT["pic"] * full  # its size in full screen
        f = px / max(w, h)
        target = (round(w * f), round(h * f)) if f >= UPSCALE_FROM else (w, h)

        def done(pic: bytes):
            try:
                self.show_pics[url] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(pic))
            except GLib.Error:
                return
            self.update()

        self.enhancer.request("show", url, data, (w, h), target, done, screen=size)

    # --- recognising songs the stream doesn't name (testing, off by default) -----------------------
    # While the stream's title is just the station (jingles, breaks, some stretches), a 12 s clip is taken once
    # a minute and recognised through Shazam's unofficial API (shazamio, in the app's own Python environment);
    # a song found is shown and remembered like a named one.
    SHAZAM_DIR = os.path.join(DATA_DIR, "shazam")
    RECOGNISE_S = 60

    def station_only(self) -> bool:
        parts = split_title(self.icy or "")
        return not parts or norm(first_artist(parts[0])) in ("radio beat", "beat")

    def set_recognise(self, on: bool) -> None:
        self.settings["recognise"] = on
        save_settings(self.settings)
        if on:
            self.recognise_text = "Připravuje se…"
            threading.Thread(target=self._shazam_setup, daemon=True).start()
        else:
            self.recognise_text = "Vypnuto"
        self._recognise_changed()

    def _shazam_setup(self) -> None:
        """The private Python environment with shazamio (once)."""
        py = os.path.join(self.SHAZAM_DIR, "bin", "python3")
        ok = os.path.exists(py) and subprocess.run([py, "-c", "import shazamio"], capture_output=True).returncode == 0
        if not ok:
            GLib.idle_add(self._recognise_status, "Instaluje se shazamio (jednou)…")
            r = subprocess.run(["python3", "-m", "venv", self.SHAZAM_DIR], capture_output=True, text=True)
            if r.returncode == 0:
                r = subprocess.run([py, "-m", "pip", "install", "--quiet", "shazamio"], capture_output=True, text=True,
                                   timeout=900)
            ok = r.returncode == 0
            if not ok:
                GLib.idle_add(self._recognise_status, "shazamio nešlo nainstalovat: "
                              + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:120])
                return
        GLib.idle_add(self._recognise_status, "Připraveno: skladby, které stream nepojmenuje, se rozpoznávají (Shazam)")
        GLib.idle_add(self._recognise_tick)

    def _recognise_status(self, text: str) -> bool:
        self.recognise_text = text
        self._recognise_changed()
        return False

    def _recognise_changed(self) -> None:
        if self.win:
            self.win.enhance_note.set_text(self.notes_text())
        self.update()

    def notes_text(self) -> str:
        """The gear menu's notes: the picture steps, and what the song recognition did last."""
        text = self.enhancer.notes()
        text += f"\n• Paměť celé aplikace: {app_memory()}"
        if self.win:
            GLib.idle_add(lambda: (self.win.refresh_effects(), False)[1])  # the animations' result is on its switch
        if self.settings.get("recognise", False):
            text += f"\n• Rozpoznávání skladeb (testování): {self.recognise_text}"
        return text

    def _recognise_tick(self) -> bool:
        if not self.settings.get("recognise", False):
            self._recognise_id = 0
            return False
        if self.station_only() and not self.paused and not self._recognising:
            self._recognising = True
            threading.Thread(target=self._recognise, args=(self.stream()[2],), daemon=True).start()
        if not self._recognise_id:
            self._recognise_id = GLib.timeout_add_seconds(self.RECOGNISE_S, self._recognise_tick)
            return False
        return True

    def _recognise(self, url: str) -> None:
        clip = os.path.join(tempfile.gettempdir(), f"radio-beat-clip-{os.getpid()}.wav")
        found = None
        t0 = time.monotonic()
        took = rec = 0.0
        try:
            r = subprocess.run([self.enhancer.ffmpeg or "ffmpeg", "-v", "error", "-y", "-t", "12", "-i", url,
                                "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", clip],
                               capture_output=True, timeout=40)
            rec = time.monotonic() - t0
            if r.returncode == 0:
                t1 = time.monotonic()
                r = subprocess.run([os.path.join(self.SHAZAM_DIR, "bin", "python3"), os.path.join(HERE, "recognise.py"),
                                    clip], capture_output=True, text=True, timeout=60)
                took = time.monotonic() - t1
                got = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else {}
                if got.get("artist") and got.get("title"):
                    found = (got["artist"], got["title"])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        finally:
            try:
                os.remove(clip)
            except OSError:
                pass
        GLib.idle_add(self._recognised, found, rec, took)

    def _recognised(self, found, rec: float = 0.0, took: float = 0.0) -> bool:
        self._recognising = False
        when = time.strftime("%H:%M")
        how = f"(12s ukázka nahraná za {_secs(rec)}, Shazam, neoficiální API)"
        if not found:
            self.recognise_text = f"{when}: nic nenalezeno za {_secs(took)} {how}"
            self._recognise_changed()
            return False
        self.recognise_text = f"{when}: nalezeno {found[0]} – {found[1]} za {_secs(took)} {how}"
        if not self.station_only():
            self.recognise_text += " – not shown: the stream names a song by now"
        elif found == self._recognised_last:
            self.recognise_text += " – shown already"
        self._recognise_changed()
        if self.station_only() and found != self._recognised_last:
            self._recognised_last = found
            icy = self.icy
            self.new_title(f"{found[0]} - {found[1]}")  # shown and remembered like a named song
            self.icy = icy  # the stream's own title stays (a named song next replaces it as usual)
        self.update()
        return False

    def new_title(self, icy: str):
        self.icy = icy
        parts = split_title(icy)
        self.artist_raw, self.song = (parts if parts else (None, icy))
        # The last cover stays up until the new one is found (no logo flashing
        # in between); the logo only when there's no song or no cover.
        self.info = None
        self.facts = self.about = self.band_about = ""
        self.song_shown = False
        if parts:
            self._remember_artist(parts[0])
            cur, _ = on_air(self.schedule, time.localtime())
            if norm(first_artist(parts[0])) not in ("radio beat", "beat"):  # not the station's own jingles
                self.db.play(first_artist(parts[0]), parts[1], cur["title"] if cur else None, self.host)
        if not parts:
            self.cover_tex = None
        self.enhancer.drop("cover")
        self.upscale_src.pop("cover", None)
        self.orig_tex.pop("cover", None)
        self._lookup_id += 1
        if parts:
            threading.Thread(target=self._lookup, args=(self._lookup_id, *parts), daemon=True).start()
            threading.Thread(target=self._facts, args=(self._lookup_id, *parts), daemon=True).start()

    def _lookup(self, lid, artist, song):
        info = lookup(artist, song)
        if info:  # its proper spelling, for HRÁLO
            self.db.set_names(first_artist(artist), song, *proper_names(first_artist(artist), song, info))
        data = square_cover(fetch(info["cover"])) if info and info.get("cover") else None
        GLib.idle_add(self._looked_up, lid, info, data)
        facts = track_facts(artist, song, info)
        self.db.set_facts(first_artist(artist), song, facts)  # HRÁLO shows it too
        GLib.idle_add(self._got_facts, lid, facts)

    def _facts(self, lid, artist, song):
        about = song_about(artist, song)
        band = "" if about else name_about(first_artist(artist), MUSIC_WORDS)
        GLib.idle_add(self._got_about, lid, about, band)

    def _got_about(self, lid, text, band):
        if lid == self._lookup_id:
            self.about, self.band_about = text, band
            self.update()
        return False

    def cz(self, text: str) -> str:
        """Czech as shown: in ostravština unless that's switched off ('Víte, že…' becomes 'Viš co?')."""
        text = years_ago(text)
        if not self.settings.get("ostrava", True):
            return text
        if text.startswith("Víte, že… "):
            return "Viš co? " + ostrava(text[len("Víte, že… "):])
        return ostrava(text)

    BADGES = {"NOW PLAYING": ("PRÁVĚ HRAJE", "PRAVĚ HRAJE"), "DID YOU KNOW": ("VÍTE, ŽE…", "VIŠ CO?")}

    def badge(self, name: str) -> str:
        cs = self.BADGES.get(name)
        return name if cs is None else cs[1 if self.settings.get("ostrava", True) else 0]

    # The show card's text turns (about 3 a minute, 180 an hour): the song playing once, then a mix made ahead
    # in the background from whole Wikipedia articles: 10 % the station, 90 % the artists and songs played
    # lately (from the history) and now and then the hosts; each source's sentences in a shuffled order, each
    # with another lead-in, and none shown again within a month (SHOWN).
    SHOWN = os.path.join(os.path.dirname(FACTS_CACHE), "shown.json")  # sentence -> when it was last shown
    MONTH = 30 * 86400
    LEADS = {"artist": ("Víte, že… ", "Zajímavost: ", "Mimochodem: ", "Z hudební kroniky: ", "Na okraj: ",
                        "Hrálo se u nás – {n}. ", "Pár slov k interpretovi {n}: ", "K tématu {n}: ",
                        "Ještě něco o {n}: ", "Z historie: "),
             "host": ("U mikrofonu: {n}. ", "Dnes vás provází {n}. ", "Moderuje {n}. ", "Kdo vás teď baví? {n}. "),
             "hosts": ("Z našich moderátorů: {n}. ", "Na Beatu uslyšíte i {n}. ", "Znáte naše hlasy? {n}. ",
                       "Moderátor{a} Beatu {n}: "),
             "song": ("O písni {n}: ", "Píseň {n}: ", "K písni {n}: ", "Ze zákulisí písně {n}: ", "Víte, že… "),
             "station": ("Něco o nás: ", "Víte, kdo vám hraje? ", "Z historie Beatu: ", "Víte, že… ")}
    BADGE = {"artist": "ZAJÍMAVOST", "song": "O PÍSNI", "host": "U MIKROFONU", "hosts": "NAŠE HLASY",
             "station": "RÁDIO BEAT"}
    READY = 4  # items made ahead

    def _set_history(self, past: list[str]) -> bool:
        self.history[:] = [a for a in past if a not in self.history] + self.history  # this session's last
        self._feed.set()
        return False

    def _load_past(self) -> None:
        """At start: the artists played before (the history), for the mix."""
        GLib.idle_add(self._set_history, list(reversed(self.db.artists(40))))

    def _remember_artist(self, artist: str) -> None:
        name = first_artist(artist)
        if not name or norm(name) in ("radio beat", "beat"):
            return
        self.history = [n for n in self.history if norm(n) != norm(name)][-39:] + [name]
        self._feed.set()

    def _feeder(self) -> None:
        """Keeps READY items made: their articles fetched (remembered), English sentences translated."""
        while True:
            self._feed.wait(60)
            self._feed.clear()
            tries = 0
            while len(self.ready) < self.READY and tries < 40:
                tries += 1
                item = self._make_item()
                if item and not any(set(item[4]) & set(x[4]) for x in self.ready):
                    self.ready.append(item)

    def _pool(self, name: str, words: tuple, langs=("cs", "en")) -> dict:
        if name not in self.pools:
            self.pools[name] = wiki_sentences(name, words, langs)
        return self.pools[name]

    def _shown_load(self) -> dict:
        try:
            with open(self.SHOWN) as f:
                shown = json.load(f)
        except (OSError, ValueError):
            shown = {}
        now = time.time()
        return {k: t for k, t in shown.items() if now - t < self.MONTH}

    def _shown_save(self) -> None:
        try:
            with open(self.SHOWN + ".tmp", "w") as f:
                json.dump(self.shown, f)
            os.replace(self.SHOWN + ".tmp", self.SHOWN)
        except OSError:
            pass

    WINDOWS = (3600, 2 * 3600, 4 * 3600, 8 * 3600, 86400, 3 * 86400, 7 * 86400, 30 * 86400, 1e12)

    def _fresh(self, pool: dict, key: str):
        """The next sentence(s) of a source not shown within the month, or None (all shown, or English while
        there's no translating)."""
        sents = (pool or {}).get("s") or []
        if not sents:
            return None
        if pool.get("lang") == "en" and time.time() < _translate_off[0]:
            return None
        order = self.orders.get(key)
        if order is None:
            order = self.orders[key] = random.sample(range(len(sents)), len(sents))
        while order:
            i = order.pop()
            if sents[i] in self.shown:
                continue
            pick = sents[i:i + 2] if i + 1 < len(sents) and len(sents[i]) + len(sents[i + 1]) < 220 \
                and sents[i + 1] not in self.shown else [sents[i]]
            if pool.get("lang") == "en":
                cs = [czech_sentence(x) for x in pick]
                if not all(cs):
                    order.append(i)  # again once there's translating
                    return None
                return pick, cs
            return pick, pick
        return None

    def _make_item(self):
        """The next curiosity: about what played in the last hour first, then two hours, four, a day, a week…
        back through the whole history, then the station's usual artists; 10 % the station, now and then hosts."""
        r = random.random()
        host = self.host
        tries = []
        if r < 0.10:
            tries.append(("station", "Rádio Beat", "Rádio Beat", HOST_WORDS + MUSIC_WORDS))
        elif r < 0.16 and host:
            tries.append(("host", host, host, HOST_WORDS))
        elif r < 0.20:
            others = [n for n, _ in self.db.hosts() if n != host]
            if others:
                name = random.choice(others)
                tries.append(("hosts", name, name, HOST_WORDS))
        for window in self.WINDOWS:
            artists, songs = self.db.since(window)
            batch = [("artist", a, a, MUSIC_WORDS) for a in artists] + \
                    [("song", t, f"{a} – {t}", (a.lower(),)) for a, t in songs]
            random.shuffle(batch)
            tries += batch
            if len(tries) > 12:
                break
        tries += [("artist", a, a, MUSIC_WORDS) for a in random.sample(SEED_ARTISTS, len(SEED_ARTISTS))]
        seen = set()
        for kind, name, key, words in tries:
            if key in seen:
                continue
            seen.add(key)
            if key not in self.pools:
                title = plain_title(name) if kind == "song" else name
                self.pools[key] = wiki_sentences(title, words)
            pool = self.pools[key]
            got = self._fresh(pool, key)
            if not got:
                continue
            originals, shown = got
            leads = [x for x in self.LEADS[kind] if x != self._last_lead] or self.LEADS[kind]
            lead = random.choice(leads)
            self._last_lead = lead
            a = "ka" if name.split() and name.split()[0].endswith("a") else ""
            title = plain_title(name) if kind == "song" else name
            return kind, title, lead.format(n=title, a=a) + " ".join(shown), pool.get("img", ""), originals
        return None

    def mark_shown(self, originals) -> None:
        """Shown on screen: not again within the month."""
        now = time.time()
        for x in originals or ():
            self.shown[x] = now
        self._shown_save()

    def next_curiosity(self):
        """The next item of the mix for the 'i' puddle: (badge, title, text, picture url), or None for now."""
        self._feed.set()
        if not self.ready:
            return None
        kind, name, text, img, originals = self.ready.popleft()
        if any(x in self.shown for x in originals):  # shown meanwhile
            return self.next_curiosity()
        self.mark_shown(originals)
        return self.cz(self.BADGE[kind]), "" if kind == "station" else name, self.cz(text), img

    def turn_content_ready(self) -> bool:
        return bool(self.about or self.ready)

    def turn_content(self):
        """What the show card's next text turn shows: (badge, title, text, picture) or None (the programme)."""
        if self.about and not self.song_shown:
            self.song_shown = True
            info = self.info or {}
            return "NOW PLAYING", info.get("title") or self.song or "", self.cz(self.about), \
                self.cover_tex or self.logo_tex
        self._feed.set()
        if not self.ready:
            return None
        kind, name, text, _img, originals = self.ready.popleft()
        self.mark_shown(originals)
        # the station's badge says it already: no headline beside it
        return self.cz(self.BADGE[kind]), "" if kind == "station" else name, self.cz(text), self.logo_tex

    def _got_facts(self, lid, text):
        if lid == self._lookup_id:
            self.facts = text
            self.update()
        return False

    def _looked_up(self, lid, info, data):
        if lid != self._lookup_id:
            return False  # song changed meanwhile
        self.info = info
        if info and info.get("album") and self.artist_raw and self.song:
            self.db.album(first_artist(self.artist_raw), self.song, info["album"], info.get("cover"))
        if data:
            try:
                self.cover_tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                # (a video's still is squared at its own size now: not the old blown-up one's cached result)
                key = info["cover"] + ("#square2" if "ytimg.com" in info["cover"] else "")
                self.upscale_src["cover"] = (key, data)
                self.orig_tex["cover"] = self.cover_tex
                self._enhance("cover")
            except GLib.Error:
                self.cover_tex = None
        else:
            self.cover_tex = None
        self.update()
        return False


    # --- picture enhancing -----------------------------------------------------------

    def _install_upscaler(self):
        self.enhancer.maintain(then=lambda: (self._enhance("logo"), self._enhance("cover"), self._enhance_show(),
                                             self._make_icons()))

    def enhance_status(self) -> str:
        if not self.settings.get("enhance", True):
            return "Vypnuto: obrázky se ukazují, jak přijdou."
        return f"{self.enhancer.status} {self.icon_text}".strip()

    def _enhance_status_changed(self, _text: str) -> None:
        if self.win:
            self.win.enhance_row.set_subtitle(self.enhance_status())
            self.win.enhance_note.set_text(self.notes_text())

    def _picture_changed(self, text: str, kind: str = "cover") -> None:
        if kind in ("show", "icon") or kind.startswith("pic:") or (kind == "logo" and self.orig_tex.get("cover")):
            return  # the cover is what's shown
        if re.search(r"výsledek|z mezipaměti|staženo z|ukázáno, jak|jak přijde", text):  # done
            self.picture_text, self.picture_doing = text, ""
            self.picture_size = self.enhancer.size_text(kind)
            if self.settings.get("last_picture") != [self.picture_text, self.picture_size]:
                self.settings["last_picture"] = [self.picture_text, self.picture_size]
                save_settings(self.settings)
        else:
            self.picture_doing = text
        if self.win:
            self.win.picture_row.set_subtitle(self.picture_subtitle())

    def picture_subtitle(self) -> str:
        """The last picture enhanced: what was done, the sizes (original / enhanced); the one being enhanced now;
        the cache."""
        doing = f"Teď se vylepšuje: {self.picture_doing}" if self.picture_doing else ""
        return "\n".join(x for x in (self.picture_text, self.picture_size, doing, self.enhancer.cache_text()) if x)

    def set_share(self, on: bool) -> None:
        self.settings["share"] = on
        save_settings(self.settings)
        self.share.set_on(on, asked=True)

    def _share_changed(self) -> None:
        if self.win:
            self.win.share_row.set_subtitle(GLib.markup_escape_text(self.share_status()))

    def share_status(self) -> str:
        return ("Každý počítač s Rádiem Beat v této síti i přes internet (společný server) má stejné vylepšené obrázky, "
                "hrané skladby a údaje o skladbách (každý svou kopii): co udělá jeden, mají i ostatní. "
                f"{self.share.text} {self.share.hub_text} {self.share.port_note}").strip()

    def enhance_px(self) -> tuple[int, int] | None:
        """The screen size pictures are enhanced for: at least 4K, so every computer makes (and shares) the same
        files whatever its screen (a smaller screen shows them scaled down)."""
        size = self.screen_px()
        return (max(size[0], SHARE_SCREEN[0]), max(size[1], SHARE_SCREEN[1])) if size else None

    def set_npu(self, on: bool) -> None:
        """AI upscaling on the NPU first (testing): checks what's there, says so in the menu."""
        self.settings["npu"] = on
        save_settings(self.settings)
        self.enhancer.npu_wanted = on
        if self.win:
            self.win.npu_row.set_subtitle("Kontroluje se…" if on else "Vypnuto: AI běží na GPU")

        def work():
            self.enhancer.npu_probe()
            GLib.idle_add(self._npu_checked)
        threading.Thread(target=work, daemon=True).start()

    def _npu_checked(self) -> bool:
        if self.win:
            self.win.npu_row.set_subtitle(self.enhancer.npu_text)
            self.win.enhance_note.set_text(self.notes_text())
        return False

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
        """Ask for an enhanced picture: upscaled if full screen would stretch it,
        and sharpened and denoised either way."""
        tex = self.orig_tex.get(kind)
        src = self.upscale_src.get(kind)
        size = self.enhance_px()
        if not tex or not src or not size:
            return
        w, h = tex.get_width(), tex.get_height()
        what = "Obal" if kind in ("cover", "track") else "Logo"
        f = min(size[0] / w, size[1] / h)
        if not self.settings.get("enhance", True):
            self._picture_changed(f"{what}: {w}×{h} px, ukázáno, jak přišel (vylepšování je vypnuté)", kind)
            return
        upscale = f >= UPSCALE_FROM  # else only sharpened and denoised, at its own size
        if not self.enhancer.ffmpeg and (not upscale or self.enhancer.ai is None):
            self._picture_changed(f"{what}: {w}×{h} px, ukázáno, jak přišel, dokud se nestáhnou nástroje "
                                  "na vylepšování", kind)
            return
        lid = self._lookup_id
        track = self.track_view

        def done(pic: bytes):
            try:
                t = Gdk.Texture.new_from_bytes(GLib.Bytes.new(pic))
            except GLib.Error:
                return
            if not self.settings.get("enhance", True):
                return
            if kind == "track":  # a HRÁLO song's cover on the main screen: still that song's
                if self.track_view is not track or not track:
                    return
                self.refined = (track.get("tex"), t)
                track["tex"] = t
            elif kind == "cover":
                if lid != self._lookup_id:
                    return
                self.refined = (self.cover_tex, t)
                self.cover_tex = t
            else:
                self.refined = (self.logo_tex, t)
                self.logo_tex = t
            self.update()

        target = (round(w * f), round(h * f)) if upscale else (w, h)
        self.enhancer.request(kind, src[0], src[1], (w, h), target, done, screen=size)

    # --- app icon ----------------------------------------------------------------------
    # The icon (taskbar, launcher, the window's corner): the station logo on a
    # white rounded square, made from an AI-enhanced master (4x, denoised,
    # sharpened) and scaled down with Lanczos for every size, so the edges are
    # clean at 16 px as at 512. Made once; again when the logo changes or an
    # update put the plain icons back.

    ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
    ICON_DIR = os.path.join(os.path.expanduser("~/.local/share/icons"), "hicolor")
    ICON_MARK = os.path.join(DATA_DIR, "icons.json")

    def _icon_paths(self, size: int) -> list[str]:
        out = [os.path.join(self.ICON_DIR, f"{size}x{size}", "apps", f"{APP_ID}.png")]
        own = os.path.join(HERE, "icons", "hicolor")
        if os.path.isdir(own):
            out.append(os.path.join(own, f"{size}x{size}", "apps", f"{APP_ID}.png"))
        return out

    @staticmethod
    def _sha(path: str) -> str | None:
        import hashlib
        try:
            with open(path, "rb") as f:
                return hashlib.sha1(f.read()).hexdigest()
        except OSError:
            return None

    def _make_icons(self) -> None:
        logo = os.path.join(HERE, "logo.png")
        if not os.path.exists(logo) or not self.enhancer.ffmpeg:
            return
        try:
            with open(self.ICON_MARK) as f:
                mark = json.load(f)
        except (OSError, ValueError):
            mark = {}
        if mark.get("logo") == self._sha(logo) and mark.get("256") == self._sha(self._icon_paths(256)[0]):
            self.icon_text = "Ikona aplikace: AI zostřená ve všech velikostech."
            self._enhance_status_changed("")
            return
        with open(logo, "rb") as f:
            data = f.read()
        tex = Gdk.Texture.new_from_filename(logo)
        w, h = tex.get_width(), tex.get_height()
        self.icon_text = "Ikona aplikace: zostřuje se…"
        self._enhance_status_changed("")
        self.enhancer.request("icon", f"icon:{self._sha(logo)}", data, (w, h), (w * 4, h * 4),
                              lambda pic: threading.Thread(target=self._icons_from, args=(pic, logo),
                                                           daemon=True).start())

    def _icons_from(self, master: bytes, logo: str) -> None:
        """Every size from the master (in a thread: ffmpeg); drawn on the main thread."""
        work = tempfile.mkdtemp(prefix="radio-beat-esr-")
        try:
            src = os.path.join(work, "master.jpg")
            with open(src, "wb") as f:
                f.write(master)
            inner = {}
            for size in self.ICON_SIZES:
                px = max(1, round(size * 0.84))
                out = os.path.join(work, f"{size}.png")
                vf = f"scale={px}:{px}:flags=lanczos" + (",unsharp=3:3:0.6:3:3:0" if size <= 48 else "")
                if self.enhancer._ff(["-i", src], vf, out):
                    with open(out, "rb") as f:
                        inner[size] = f.read()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if len(inner) == len(self.ICON_SIZES):
            GLib.idle_add(self._draw_icons, inner, logo)

    def _draw_icons(self, inner: dict, logo: str) -> bool:
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(Gdk.Display.get_default())
        try:
            for size, data in inner.items():
                pic = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                snap = Gtk.Snapshot()
                pad = size * 0.04
                box = Graphene.Rect().init(pad, pad, size - 2 * pad, size - 2 * pad)
                rr = Gsk.RoundedRect()
                rr.init_from_rect(box, size * 0.2)
                snap.push_rounded_clip(rr)
                snap.append_color(_rgba(255, 255, 255, 1.0), box)
                off = (size - pic.get_width()) / 2
                snap.append_texture(pic, Graphene.Rect().init(off, off, pic.get_width(), pic.get_height()))
                snap.pop()
                out = renderer.render_texture(snap.to_node(), Graphene.Rect().init(0, 0, size, size))
                for path in self._icon_paths(size):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    out.save_to_png(path)
        except (GLib.Error, OSError) as e:
            print(f"icons: {e}", file=sys.stderr)
            return False
        finally:
            renderer.unrealize()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(self.ICON_MARK, "w") as f:
            json.dump({"logo": self._sha(logo), "256": self._sha(self._icon_paths(256)[0])}, f)
        # Desktops read the icons from their caches.
        for cmd in (["gtk-update-icon-cache", "-q", "-t", self.ICON_DIR], ["kbuildsycoca6"], ["kbuildsycoca5"]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if cmd[0].startswith("kbuildsycoca"):
                    break
        self.icon_text = "Ikona aplikace: AI zostřená ve všech velikostech (nové ikony po příštím spuštění)."
        self._enhance_status_changed("")
        return False


LOG = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "radio-beat",
                   "radio-beat.log")


LOG_MAX = 200_000_000  # bytes: then it starts afresh, the previous one kept as .1


class _Stamped:
    """stderr with the time at the start of each line (what the app itself writes)."""

    def __init__(self, out):
        self.out, self.fresh = out, True

    def write(self, text):
        for part in text.splitlines(keepends=True):
            if self.fresh:
                self.out.write(time.strftime("%Y-%m-%d %H:%M:%S "))
            self.out.write(part)
            self.fresh = part.endswith("\n")
        self.out.flush()
        return len(text)

    def __getattr__(self, name):
        return getattr(self.out, name)


def start_log() -> None:
    """Errors and warnings into LOG (kept to LOG_MAX, the one before as .1): Python's and also GTK's, GLib's
    and mpv's (stderr itself points there unless started from a terminal, which keeps seeing them)."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
        f = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    if sys.stderr is not None and sys.stderr.isatty():  # in a terminal: there and in the file
        term = sys.stderr

        class Both:
            def write(self, t):
                term.write(t)
                return f.write(t)

            def flush(self):
                term.flush()
                f.flush()

        sys.stderr = _Stamped(Both())
    else:
        os.dup2(f.fileno(), 2)  # C libraries (GTK, GLib, mpv) write to fd 2 directly
        sys.stderr = _Stamped(f)
    try:
        with open(os.path.join(HERE, "VERSION")) as v:
            rev = v.read().strip()[:7]
    except OSError:
        rev = ""
    def trim():  # running for weeks: checked every hour too
        try:
            if os.fstat(f.fileno()).st_size > LOG_MAX:
                shutil.copyfile(LOG, LOG + ".1")
                os.ftruncate(f.fileno(), 0)  # (opened for appending: writing goes on from the start)
        except OSError:
            pass
        return True

    GLib.timeout_add_seconds(3600, trim)
    print(f"--- Rádio Beat {APP_VERSION} start (pid {os.getpid()}{', ' + rev if rev else ''}, {HERE})", file=sys.stderr)
    layers = [os.path.join(d, "vulkan", "implicit_layer.d")
              for d in [GLib.get_user_data_dir(), *GLib.get_system_data_dirs(), "/etc"]]
    if any(glob.glob(os.path.join(d, "*LSFG*")) for d in layers):
        print("Pozn.: lsfg-vk (Lossless Scaling, generování snímků do her) je nainstalované jako vrstva Vulkanu. "
              "Hlášku „Loader Message: … Failed to find 'vkGetInstanceProcAddr' in layer … liblsfg-vk-layer.so … "
              "Skipping layer“ níže vypíše GTK: nová verze lsfg-vk se připojí jen k programům ze svého nastavení, "
              "Rádio Beat mezi nimi není, tak ji Vulkan přeskočí. Neškodí, nic nechybí; necháno tak.",
              file=sys.stderr)


if __name__ == "__main__":
    start_log()
    sys.exit(App().run(sys.argv))
