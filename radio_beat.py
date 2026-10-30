#!/usr/bin/env python3
"""Rádio Beat player: GTK4 / libadwaita window, mpv plays the stream.

mpv runs as a child process controlled over its JSON IPC socket; its MPRIS
plugin (mpv-mpris) gives media keys and the bar's player widget for free.
The stream's ICY title ("ARTIST - SONG") is looked up on Deezer (then
iTunes) for the album, proper spelling and the cover.

It starts playing right away. Closing the window keeps it playing, with an
icon in the bar's tray (StatusNotifierItem) that shows the window again;
Stop quits. Started at login (--autostart) it opens in full screen; --hidden (a restart after an update while
the window was closed) plays with only the tray icon.
"""

import collections
import ctypes
import glob
import html
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
    ("mp3backup", "MP3 128 kbps (backup server)", "https://icecast1.play.cz/beat64.mp3"),
    ("aac64", "AAC 64 kbps (radiobeat.cz)", "https://stream.rcs.revma.com/3d47nqvb938uv"),
]
# What each stream sounds like (the list shows them best first).
STREAM_NOTES = {
    "flac": "Best: lossless, exactly as broadcast",
    "aac128": "Very good: AAC keeps more detail than MP3 at the same rate",
    "mp3128": "Good: MP3 needs more data than AAC for the same sound",
    "mp3backup": "Good: the same MP3 from another server",
    "aac64": "Lowest: half the data, fine on small speakers",
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
function watch(w) {
    if (!w || w.resourceClass !== "%s") return;
    // KWin gives the window its size back after these signals: it's placed at the next fitting geometry.
    // Once placed, moving it by hand stays.
    var due = !place(w);
    function again() { due = !place(w); }
    if (w.frameGeometryChanged) w.frameGeometryChanged.connect(function () { if (due && place(w)) due = false; });
    if (w.fullScreenChanged) w.fullScreenChanged.connect(again);
    if (w.minimizedChanged) w.minimizedChanged.connect(function () { if (!w.minimized) again(); });
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
        return None, "Couldn't reach GitHub"
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            mine = f.read().strip()
    except OSError:
        mine = ""
    if not re.fullmatch(r"[0-9a-f]{7,40}", mine):
        # not an installed copy (no VERSION: run from the source folder): never replaced by an update
        return False, "Running from the source folder: not updated by itself"
    if mine == sha:
        return False, f"Up to date ({info['committer']['date'][:10]})"
    # Only forwards: GitHub's commit must come after the installed one (a newer local build stays).
    if re.fullmatch(r"[0-9a-f]{7,40}", mine):
        try:
            status = _json(f"https://api.github.com/repos/{REPO}/compare/{mine}...{sha}")["status"]
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not on GitHub: built here, newer than what's published
                return False, "This version is newer than GitHub's (not published yet)"
            return None, "Couldn't reach GitHub"
        except (OSError, ValueError, KeyError):
            return None, "Couldn't reach GitHub"
        if status in ("behind", "identical"):
            return False, "Up to date (newer than GitHub's)" if status == "behind" else "Up to date"
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
    return best if best_score >= 3 else None


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
            area = (pl.get("area") or {}).get("name")
            out.append(pl["name"] + (f", {area}" if area and area not in pl["name"] else ""))
        elif r.get("type") == "recorded in" and r.get("area"):
            out.append(r["area"]["name"])
    return list(dict.fromkeys(out))


def song_facts(artist: str, song: str) -> str:
    """'First released 8 November 1971 · Recorded at Olympic Studios, London', what MusicBrainz knows (may be
    ''), remembered per song."""
    key = f"{norm(first_artist(artist))}|{norm(song)}"
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
    # Where that first release came out (the release carrying the earliest date).
    country = ""
    for r in recs:
        for rel in r.get("releases", []):
            if first and rel.get("date") == first and rel.get("country") in COUNTRY_IN:
                country = COUNTRY_IN[rel["country"]]
                break
        if country:
            break
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
    bits = ([f"Poprvé vydáno {_date(first)}" + (f" {country}" if country else "") + f" ({{ago:{first[:4]}}})"]
            if first else []) + \
           ([f"Nahráno v {' a '.join(places[:2])}"] if places else [])
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
    seen (their shows, when last on), in SQLite under ~/.local/share/radio-beat. The newest come first; the
    oldest plays go past KEEP. Feeds the show card's mix: artists played earlier, all the station's hosts."""

    KEEP = 50000

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

    def play(self, artist: str, title: str, show: str | None, host: str | None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO plays VALUES (?, ?, ?, NULL, ?, ?)", (time.time(), artist, title, show, host))
            self.db.execute("DELETE FROM plays WHERE rowid IN (SELECT rowid FROM plays ORDER BY at DESC "
                            f"LIMIT -1 OFFSET {self.KEEP})")

    def album(self, artist: str, title: str, album: str) -> None:
        """The album, once the lookup found it (the newest play of that song)."""
        with self.lock, self.db:
            self.db.execute("UPDATE plays SET album = ? WHERE rowid = (SELECT rowid FROM plays WHERE artist = ? "
                            "AND title = ? ORDER BY at DESC LIMIT 1)", (album, artist, title))

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
        return f"{n / 2**30:.2f} GB"
    if n >= 2**20:
        return f"{n / 2**20:.1f} MB"
    return f"{n / 2**10:.0f} KB"


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
    """How busy the GPU was during a step and at what clock (amdgpu's sysfs; nothing where there's none)."""

    def __init__(self):
        self.busy: list[int] = []
        self.mhz: list[int] = []
        self.top = 0
        self.dev = next((os.path.dirname(p) for p in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"))),
                        None)
        self.run = self.dev is not None
        if self.run:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        end = time.monotonic() + 3600
        while self.run and time.monotonic() < end:
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

    def stop(self) -> str:
        """ "84 % busy at 1980 of 2000 MHz" (averages over the step), or "" when it couldn't tell."""
        self.run = False
        if len(self.busy) < 2:
            return ""
        text = f"{round(sum(self.busy) / len(self.busy))} % busy"
        if self.mhz and self.top:
            text += f" at {round(sum(self.mhz) / len(self.mhz))} of {self.top} MHz"
        return text


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
        self.status = "Starting…"
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
        self.watches: list[GpuWatch] = []  # the GPU watched during the job's steps
        self.sizes: dict[str, list] = {}  # kind -> [original bytes, enhanced bytes or None while working]
        threading.Thread(target=self._trim, daemon=True).start()
        # NPU (testing, off unless switched on): AMD XDNA through ONNX Runtime's Vitis AI provider.
        self.npu_wanted = False
        self.npu_ready = False  # driver, runtime and a model all found
        self.npu_text = "Off"
        self.npu_model = None
        self.plock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._duty, daemon=True).start()

    NPU_DIR = os.path.join(DATA_DIR, "npu")  # an ONNX super-resolution model built for the NPU goes here

    def npu_probe(self) -> None:
        """What the NPU path has and lacks (for the gear menu): the amdxdna driver, ONNX Runtime with the Vitis
        AI provider (AMD's XRT and Ryzen AI Software), and a model for it."""
        if not self.npu_wanted:
            self.npu_ready, self.npu_text = False, "Off: AI runs on the GPU"
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
        parts = ["NPU: " + ("found (amdxdna driver)" if have_npu else "not found (no amdxdna device)"),
                 "runtime: " + ("ONNX Runtime with Vitis AI" if has_vai else
                                "ONNX Runtime without the Vitis AI provider (AMD's XRT + Ryzen AI Software)" if has_ort
                                else "missing (ONNX Runtime with Vitis AI: AMD's XRT + Ryzen AI Software)"),
                 "model: " + (os.path.basename(self.npu_model) if self.npu_model else
                              f"none (an NPU-built ONNX super-resolution model in {self.NPU_DIR})")]
        self.npu_ready = have_npu and has_vai and bool(self.npu_model)
        self.npu_text = " · ".join(parts) + (" — AI upscaling tries the NPU first" if self.npu_ready
                                              else " — AI stays on the GPU")
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

    def _signal(self, sig) -> None:
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
            parts = [f"No GPU for Vulkan here, so AI upscaling (Real-ESRGAN) on {self.gpu}: slow, but AI"]
        elif self.ai:
            parts = [f"AI upscaling and AI redraw for detail (Real-ESRGAN: x4plus for covers, "
                     f"x4plus-anime for the logo): {gpu}"]
        else:
            parts = [f"No Vulkan at all here (not even software), so smooth (Lanczos) upscaling: CPU {cpu}"]
        if self.ffmpeg:
            parts.append(f"sharpening: CPU {cpu}")
            parts.append(f"denoising: {gpu}" if self.vk_denoise else
                         "denoising: testing the GPU…" if self.vk_denoise is None else f"denoising: CPU {cpu}")
        else:
            parts.append("sharpening and denoising wait for ffmpeg (downloading)")
        self._say("On. " + "; ".join(parts) + ".")

    def notes(self) -> str:
        """For the gear menu's notes: every picture step, its tool / AI model, and where it runs."""
        cpu = f"CPU {_cpu_name()}" + (f" ({SIMD}: ffmpeg's widest code on it)" if SIMD else "")
        on = (self.gpu if self.soft else f"GPU {self.gpu} (Vulkan)") if self.ai else None
        lines = ["How pictures are made better:"]
        if self.ai is None:
            lines.append("• AI: waiting for the GPU test (nothing is resized without AI until then)")
        elif self.ai:
            lines += [f"• Quick AI pass: Real-ESRGAN {ESR_QUICK} (ncnn) on {on}",
                      f"• Covers and show pictures: Real-ESRGAN {ESR_MODEL} (RealESRGAN_x4plus, 4x then "
                      f"scaled down to the screen, Lanczos) on {on}",
                      f"• Logo and app icon: Real-ESRGAN {ESR_MODEL_ART} (RealESRGAN_x4plus_anime_6B, "
                      f"for flat colours and letters) on {on}",
                      f"• Big pictures: AI redraw for detail from half size, same models, on {on}"]
        else:
            lines.append(f"• Upscaling: no Vulkan at all, so Lanczos (ffmpeg, not AI) on the {cpu}")
        if self.ffmpeg:
            lines.append(f"• Sharpening: ffmpeg unsharp (fine + wide) and CAS on the {cpu}")
            if self.vk_denoise:
                big = (f"; pictures of {self.vk_fail_px / 1e6:.1f} MP and more on the {cpu} (this GPU gives them "
                       f"back black)" if self.vk_fail_px < 1 << 62 else "")
                lines.append(f"• Denoising: ffmpeg nlmeans_vulkan on GPU {self.gpu}{big}")
            elif self.vk_denoise is None:
                lines.append("• Denoising: testing the GPU (ffmpeg nlmeans_vulkan); the CPU meanwhile")
            else:
                lines.append(f"• Denoising: ffmpeg NL-means on the {cpu}, {DENOISE_THREADS} threads (more aren't "
                             f"faster: it waits on the memory; ffmpeg's Vulkan denoiser can't run here)")
            lines.append(f"• App icon sizes: Lanczos down from the AI master (ffmpeg) on the {cpu}")
        else:
            lines.append("• Sharpening and denoising: waiting for ffmpeg")
        lines.append("• Morph between steps: GTK (GSK) cross-fade on the GPU")
        lines.append(f"• NPU (testing): {self.npu_text}")
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
                    self.gpu = (f"the CPU {_cpu_name()} (software Vulkan)" if self.soft
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
        if kind != "icon" and os.path.exists(hit) and self._jpeg_ok(hit) and self._not_black(hit):
            self._used(hit)
            with open(hit, "rb") as f:
                pic = f.read()
            self.sizes[kind] = [len(data), None, len(pic)]
            done(pic)
            try:
                with open(hit[:-4] + ".txt") as f:
                    steps = f.read().strip()
            except OSError:
                steps = "enhanced"
            what = {"cover": "Cover", "logo": "Logo"}.get(kind, "Show picture")
            if self.on_picture:
                self.on_picture(f"{what}: {src_size[0]}×{src_size[1]} px (original {_size(len(data))}) → "
                                f"{size[0]}×{size[1]} px, {steps} (from the cache)", kind)
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
        what = {"cover": "Cover", "logo": "Logo"}.get(kind, "Show picture")
        self.model = ESR_MODEL_ART if kind in ("logo", "icon") else ESR_MODEL
        same = size == (w0, h0)  # big enough already: no upscaling
        orig = _size(len(data))
        self.sizes[kind] = [len(data), None, None]  # source, fast enhanced (the first step shown), final
        if same:
            scr = f"the {screen[0]}×{screen[1]} px screen" if screen else "the screen"
            head = f"{what}: original {w0}×{h0} px ({orig}), no upscaling needed (big enough for {scr})"
        else:
            head = f"{what}: original {w0}×{h0} px ({orig}) → {size[0]}×{size[1]} px"
        # The icon master only wants the final picture; everything else shows each step.
        sizes = self.sizes
        steps: list[str] = []

        def emit(pic, final=False):
            # each step: what, how long, where, and the size it left: "sharpened 0.4 s on CPU (3.9 MB)"
            if steps and not steps[-1].endswith(")"):
                steps[-1] += f" ({_size(len(pic))})"
            got = sizes.setdefault(kind, [len(data), None, None])
            if final:
                got[2] = len(pic)
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
        if os.path.exists(cached):
            if self._jpeg_ok(cached) and self._not_black(cached):  # black ones came from the GPU giving up
                self._used(cached)
                with open(cached, "rb") as f:
                    pic = f.read()
                self.sizes[kind] = [len(data), None, len(pic)]
                emit(pic, True)
                try:
                    with open(cached[:-4] + ".txt") as f:
                        was = f.read().strip()
                except OSError:
                    was = "enhanced"
                if "final " not in was:
                    was += f", final {_size(len(pic))}"
                self._picture(f"{head}, {was} ({f'fetched from {fetched}' if fetched else 'from the cache'})")
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
            show(f": quick AI pass on {self.gpu}…")
            t, watch = time.monotonic(), self._watch()
            quick = os.path.join(work, "quick.png")
            out = self._ai(src, src_size, size, quick, work, ESR_QUICK)
            if out == "killed" or not live():
                return
            if out:
                steps.append(f"quick AI redraw {_secs(time.monotonic() - t)} on {self._where(watch)}")
                emit(open(quick, "rb").read())
            if self.light and out:  # a small picture: the quick pass is enough
                shutil.copyfile(quick, up)
                if self._sharpen(up, work, steps, show, live, emit):
                    sharp = os.path.join(work, "sharp.png")
                    self._keep(sharp, steps, emit, cached)
                return
            show(f", AI upscaling ({self.model}) on {self.gpu}…")
            t = time.monotonic()
            watch = self._watch()
            full = self._ai(src, src_size, size, up, work, self.model)
            if full == "killed" or not live():
                return
            if full:
                label = f"AI {full} ({self.model}) {_secs(time.monotonic() - t)} on {self._where(watch)}"
            elif out:
                shutil.copyfile(quick, up)
                label = "(full AI pass failed, the quick one kept)"
                cached = None  # the full pass again next time
        if label is None:
            if not self._resize(src, size, up) or not live():
                if live():
                    show(": couldn't be upscaled, shown as it is")
                return
            label = "upscaled (smooth: no Vulkan here for AI)" if not self.ai else "upscaled (smooth: AI failed)"
            if self.ai:
                cached = None  # AI again next time
        steps.append(f"{label} {_secs(time.monotonic() - t)} on CPU" if label.startswith("up") else label)
        emit(open(up, "rb").read())
        if not self.ffmpeg:
            show(" (sharpening and denoising: no ffmpeg yet)")
            return
        if self._sharpen(up, work, steps, show, live, emit):
            self._final(up, steps, show, live, emit, cached, work)

    def _same_size(self, src, size, work, steps, show, live, emit, cached) -> None:
        """1. sharpened as it is, shown at once; 2. AI-redrawn for detail
        (when there's a GPU); 3. denoised and sharpened: the final one."""
        if not self.ffmpeg:
            show(" (sharpening and denoising: no ffmpeg yet)")
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
            show(f", AI redrawing for detail on {self.gpu}…")
            t, watch = time.monotonic(), self._watch()
            redrawn = os.path.join(work, "redrawn.png")
            out = self._ai(orig, size, size, redrawn, work, self.model)
            if out == "killed" or not live():
                return
            if out:
                steps.append(f"AI redraw for detail ({self.model}) {_secs(time.monotonic() - t)} "
                             f"on {self._where(watch)}")
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
        feed, how = os.path.join(work, "feed.png"), "upscaled"
        if not self._ff(["-i", src], "format=rgb24", feed):
            feed = src
        if min(w0, h0) >= REDRAW_FROM:
            half = os.path.join(work, "half.png")
            if self._ff(["-i", src], f"scale={w0 // 2}:{h0 // 2}:flags=lanczos,format=rgb24", half):
                feed, how = half, "redrawn" if size == src_size else "redrawn and upscaled"
        big = os.path.join(work, "x4.png")
        if self.npu_ready and self._npu(feed, big, work):
            return how if self._resize(big, size, out) else None
        res, err = self._esr(feed, big, model)
        while res is None and self.tile != self.TILES[-1]:
            # The GPU timed out (black or failed): smaller tiles from now on, and again.
            self.tile = self.TILES[self.TILES.index(self.tile) + 1]
            self._save_tile()
            res, err = self._esr(feed, big, model)
        if res == "killed":
            return "killed"
        if res is None or not self._resize(big, size, out):
            print(f"enhancer: Real-ESRGAN failed: {err.strip()[-200:]}", file=sys.stderr)
            return None
        return how

    def _sharpen(self, pic, work, steps, show, live, emit) -> bool:
        show(", sharpening…")
        t = time.monotonic()
        sharp = os.path.join(work, "sharp.png")
        if not self._ff(["-i", pic], SHARPEN, sharp) or not live():
            if live():
                show(" (sharpening failed)")
            return False
        steps.append(f"sharpened {_secs(time.monotonic() - t)} on CPU{', ' + SIMD if SIMD else ''}")
        emit(open(sharp, "rb").read())
        return True

    def _where(self, watch: GpuWatch | None = None) -> str:
        """Where the AI runs, for the steps' notes: on the GPU with how busy it was and its clock."""
        where = "NPU" if self.npu_ready else "CPU (no GPU for Vulkan)" if self.soft else "GPU"
        load = watch.stop() if watch else ""
        return f"{where}, {load}" if where == "GPU" and load else where

    def _watch(self) -> GpuWatch:
        watch = GpuWatch()
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
        steps.append(f"final {_size(len(pic))}")
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
        show(", denoising…")
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
            t = time.monotonic()  # the CPU's own time
            ok = self._ff(["-i", base], f"{DENOISE},{SHARPEN}", final, jpeg=True, threads=DENOISE_THREADS)
            where = f"CPU, {SIMD + ', ' if SIMD else ''}{DENOISE_THREADS} threads"
        if not ok or not live():
            if live():
                show(" (denoising failed)")
            return
        steps.append(f"denoised {_secs(time.monotonic() - t)} on {where}")
        with open(final, "rb") as f:
            pic = f.read()
        emit(pic, True)
        steps.append(f"final {_size(len(pic))}")
        show("" if cached else " (not cached: tried again next time)")
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
        cmd += inputs + ["-vf", vf, "-frames:v", "1"] + (["-q:v", "2"] if jpeg else []) + [out]
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
        src, fast, final = (self.sizes.get(kind) or [None, None, None])[:3]
        if not src:
            return ""
        parts = [f"source {_size(src)}"]
        if fast:  # no first pass (from the cache, or a picture big enough already): left out
            parts.append(f"fast enhanced {_size(fast)}")
        parts.append(f"final enhanced {_size(final) if final is not None else '…'}")
        return "Size: " + " / ".join(parts)

    def cache_text(self) -> str:
        if self.cache_used < 0:
            return "Cache: counting…"
        return (f"Cache: {_size(self.cache_used)} used of {self.cache_allowed / 2**30:.1f} GB set aside "
                f"(80 % of the free space, on this computer)")

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


class CacheShare:
    def __init__(self, enhancer, on_status):
        self.enh = enhancer
        self.on_status = on_status
        self.on = False
        self.peers: dict[str, str] = {}  # address -> its name
        self.text = "Off"
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
        self._say("Looking for other computers on this network…" if on else "Off")
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
                name = self.path.rsplit("/", 1)[-1]
                n = int(self.headers.get("Content-Length") or 0)
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
            self._say(f"Can't serve the pictures (port {SHARE_PORT}: {e.strerror}); still fetches from others")
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
        how = f"run once in a terminal: sudo sh -c '{cmd}'"
        if os.path.exists(self.REFUSED):
            self._note(f"The firewall ({wall}) turns the others away; this one still fetches and hands over. "
                      f"To let them in, turn sharing off and on (asks again), or {how}")
            return
        self._note(f"Asking for the administrator's password to open port {SHARE_PORT} ({wall}, this network only)…")
        r = self._run(["pkexec", "sh", "-c", cmd]) if shutil.which("pkexec") else None
        if r is not None and r.returncode == 0:
            self._note(f"Port {SHARE_PORT} opened in the firewall ({wall}, local network only).")
            self.wake.set()
            return
        if r is not None and r.returncode in (126, 127) and "agent" not in (r.stderr or "").lower():
            try:  # turned down: not asked at every start
                with open(self.REFUSED, "w") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M"))
            except OSError:
                pass
        self._note(f"Couldn't open port {SHARE_PORT} in the firewall ({wall}); this one still fetches and hands "
                  f"over. To let the others in too, {how}")

    # finding the others, and swapping pictures with them
    def _loop(self) -> None:
        time.sleep(30)  # the cache counted first (its room)
        while True:
            if self.on:
                try:
                    self._round()
                except Exception as e:  # never the app's end
                    self._say(f"Sharing failed: {e}")
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
        net = me.rsplit(".", 1)[0]

        def knock(ip):
            try:
                with socket.create_connection((ip, SHARE_PORT), timeout=0.4):
                    return ip
            except OSError:
                return None

        cands = [f"{net}.{i}" for i in range(1, 255) if f"{net}.{i}" != me] + list(self.peers)
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

    def fetch(self, name: str) -> str | None:
        """A picture about to be made: fetched from a computer that has it already (with its notes).
        Returns that computer's name, None when none has it."""
        if not self.on or name in self.gone:
            return None
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

    def _round(self) -> None:
        self.peers = self._find()
        if not self.peers:
            self._say(f"No other computer with Rádio Beat on this network yet (looks every {SHARE_EVERY // 60} min)")
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
            self._say(f"Shared with {who.rsplit('-', 1)[0]} ({ip}) at {time.strftime('%H:%M')}: "
                      f"{got} pictures fetched, {gave} handed over (since the start: {self.got} in, {self.gave} out)")
        if self.got:
            self.enh._trim()


# --- window ---------------------------------------------------------------------

CSS = b"""
.cover-bg { background: #111; }
.backdrop { transform: scale(1.15); }  /* blurred and darkened by CoverView itself; GlassCard draws it the same: BACKDROP_* */
.scrim { background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.75) 55%, rgba(0,0,0,0) 100%); }
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
.overlay-header { background: none; box-shadow: none; }
.show-card { padding: 0; }
.show-pic { border-radius: 10px; }
.on-air { background: #E3051A; color: white; border-radius: 999px; padding: 1px 8px; font-weight: 800; }
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
.puddle label { color: white; text-shadow: -1px 2px 1px rgba(0,0,0,0.9), -2px 3px 5px rgba(0,0,0,0.65); }
.puddle .puddle-now label { font-weight: 700; }
.puddle .boxed-list, .puddle .boxed-list > row { background: rgba(255,255,255,0.04); }
.puddle button.puddle-tab { padding: 2px 10px; min-height: 0; border-radius: 999px; color: white;
                            background: rgba(255,255,255,0.10); font-weight: 700; }
.puddle button.puddle-tab:hover { background: rgba(255,255,255,0.22); }
.puddle button.puddle-tab.on-air { background: #E3051A; }
.puddle .puddle-now { background: rgba(227,5,26,0.25); border-radius: 10px; padding: 6px; }
.puddle .puddle-past { opacity: 0.55; }
popover.glass-menu > contents { background: none; box-shadow: 0 6px 22px rgba(0,0,0,0.45); padding: 0; border-radius: 16px; }
popover.glass-menu .boxed-list, popover.glass-menu .boxed-list > row { background: rgba(255,255,255,0.04); }
popover.glass-menu label { text-shadow: -1px 1px 2px rgba(0,0,0,0.8); }
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


def _opaque(tex):
    """A rendered texture as a plain opaque one: GTK then skips whatever it covers (an alpha texture is
    blended over everything under it: the lens's 2800 stacked rings cost 1 ms each, opaque 0.1 ms)."""
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
        self._blurred = (None, None)  # (key, texture): the blurred backdrop, made once per picture and size
        self.shade = 1.0  # scales the darkening (gear menu: Background shade)
        self.tint = 0.34  # the dark tint over the glass (times shade)
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
        self._build_tick = None
        self.concave = False  # lit like a dish: shade along the top inside edge, light along the bottom
        self.color = None  # (r, g, b, alpha): colored glass
        self.scrim = None  # the shaded panel it lies on: its shading copied into the glass (see-through)
        self.sharp_bars = False  # the blurred bars beside the cover seen sharp (the cover filling them) instead
        self.depth = True  # a soft shadow below the card: it floats over the cover
        self.flash = 0.0  # 1 → 0 when the song changes
        self.flash_anim = None

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
            vw, vh = view.get_width(), view.get_height()
            if view.has_css_class("backdrop") and self.sharp_bars:
                # Sharp: the cover filling the window (what the bars are a blur of), zoomed like them.
                ok, p = self._where(view.get_parent())
                if ok:
                    snap.save()
                    snap.translate(p)
                    snap.translate(Graphene.Point().init(vw / 2, vh / 2))
                    snap.scale(BACKDROP_SCALE * 1.3, BACKDROP_SCALE * 1.3)  # past the window's edges too
                    snap.translate(Graphene.Point().init(-vw / 2, -vh / 2))
                    view.draw(snap, pic, vw, vh)
                    snap.restore()
                    snap.save()
                    snap.translate(p)
                    snap.translate(Graphene.Point().init(vw / 2, vh / 2))
                    snap.scale(BACKDROP_SCALE, BACKDROP_SCALE)
                    snap.translate(Graphene.Point().init(-vw / 2, -vh / 2))
                    view.draw(snap, pic, vw, vh)
                    snap.restore()
                    if darken:
                        snap.append_color(_rgba(0, 0, 0, darken), Graphene.Rect().init(p.x, p.y, vw, vh))
                continue
            if view.has_css_class("backdrop"):
                # The blurred bars beside the cover: blurred here too, like around the card, and bent like
                # the cover (thick glass all over, just blurry there).
                tex = self._backdrop(view, pic, vw, vh, darken)
                # Where it sits before its CSS zoom (the parent's origin): the texture has the zoom in it.
                ok, p = self._where(view.get_parent())
                if tex and ok:
                    # past the window's edges too (glass near them bends what lies beyond): the blur mirrored at
                    # each edge, seamless (a stretched copy left a seam the lens repeated in rings)
                    for mx in (-1, 0, 1):
                        for my in (-1, 0, 1):
                            # a mirror copy: flipped about the window's edge it touches
                            snap.save()
                            snap.translate(Graphene.Point().init(p.x + (2 * vw if mx == 1 else 0),
                                                                 p.y + (2 * vh if my == 1 else 0)))
                            snap.scale(-1 if mx else 1, -1 if my else 1)
                            snap.append_texture(tex, Graphene.Rect().init(0, 0, vw, vh))
                            snap.restore()
                continue
            snap.save()
            snap.translate(p)
            view.draw(snap, pic, vw, vh)
            if darken:
                snap.append_color(_rgba(0, 0, 0, darken), Graphene.Rect().init(0, 0, vw, vh))
            snap.restore()
        snap.restore()

    def _where(self, widget):
        """Where `widget` (behind the card) starts, in the card's coordinates."""
        return widget.compute_point(self, Graphene.Point().init(0, 0))

    def _backdrop(self, view, pic, vw: int, vh: int, darken: float):
        """The backdrop as the window shows it (blurred, darkened, a bit enlarged), as a texture: drawn once,
        not once per lens layer."""
        key = (id(pic), vw, vh, darken)
        if self._blurred[0] == key:
            return self._blurred[1]
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
        self._blurred = (key, tex)
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
        if self.depth and shadow:  # it floats above the cover: a wide soft shadow and a tight one where it nearly touches
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.55), 0, rim * 0.3, 0, rim * 0.9)
            snap.append_outset_shadow(rr, _rgba(0, 0, 0, 0.35), 0, 2, 0, 4)
        snap.push_rounded_clip(rr)
        tex = self._lens_texture(w, h) if self.cache_lens else None
        if tex:
            snap.append_texture(tex, box)
        elif self.cache_lens:  # its first texture still being built: plainly magnified meanwhile
            self._behind(snap, w, h, self.mag, self.mag)
        else:
            self._lens(snap, w, h)
        self._finish(snap, w, h, box, rr, rim)

    def _lens_key(self, w: float, h: float):
        """What the lens shows depends on: which pictures, where they lie, the glass's size and shape."""
        key = [w, h, self.radius, self.mag, self.sharp_bars]
        for view, _ in self.behind:
            for wdg in (view, view.get_parent()):
                ok, p = self._where(wdg)
                key += [round(p.x, 1), round(p.y, 1)] if ok else [None]
            key += [id(view.pic), view.get_width(), view.get_height()]
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
            return self._lens_cache[1]
        if self._build_tick is None:  # frames keep coming until it's built
            self._build_tick = self.add_tick_callback(self._build_step)
        return self._lens_cache[1]

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
        return {"rx": rx, "ry": ry, "rings": rings}

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
            if flat:
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
        key = (lk[:3] + lk[4:], rx, ry, bool(self.pulse_tick))  # not the magnification: it breathes
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
        self._flat_cache = (key, (_opaque(native.get_renderer().render_texture(
            node, Graphene.Rect().init(0, 0, ew * k, eh * k))), box))
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
        self.tint = 0.42  # a little darker than the show card: the menu's small text stays readable
        self.sharp_bars = True
        self.depth = False  # the popup's own shadow does it

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
            self.paint = Gtk.WidgetPaintable.new(content)
            # the window changed (a new cover, a new song): the menu shows it
            self.paint.connect("invalidate-contents", lambda *_: self.get_mapped() and self.queue_draw())
        cw, ch = content.get_width(), content.get_height()
        if cw <= 0 or ch <= 0:
            return None
        surf = self.win.get_surface()
        k = surf.get_scale() if surf and hasattr(surf, "get_scale") else self.win.get_scale_factor()
        s = Gtk.Snapshot()
        s.scale(k, k)
        self.paint.snapshot(s, cw, ch)
        self._blur_card_text(s, content)
        node = s.to_node()
        return native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, cw * k, ch * k)) if node else None

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
        snap.append_texture(self._tex, Graphene.Rect().init(p.x, p.y, content.get_width(), content.get_height()))
        snap.restore()

    def do_snapshot(self, snap) -> None:
        self.shade = 1.0  # always readable, whatever Background shade is
        self._tex = self._window_texture()
        GlassCard.do_snapshot(self, snap)
        self._tex = None


class ClockView(Gtk.Widget):
    """The time as tall as the room it's given, at its right; left of it the weekday at the top, as wide as
    the room there, under it the day and month, then the year, each as big as fits (not touching). Their letters' outlines (not the fonts' line boxes) fill the room, in
    the Beat Radio fonts, white with a shadow like the song info's."""

    FONTS = (("Anton", Pango.Weight.NORMAL),) + (("Oswald", Pango.Weight.SEMIBOLD),) * 3
    TIME_MAX = 0.75  # of the width at most: the weekday and date keep the rest
    DAY_MAX = 0.4  # the weekday's share of the height at most
    GAP = 0.06  # between the time and the rest (of the width), and the lines on the left (of the height)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts = ("", "", "", "")
        self.room = (0, 0)
        self.lays = None
        self.colon = True  # the time's colon blinks: a second on, a second off

    def _layout(self, which: int, pt: float):
        family, weight = self.FONTS[which]
        lay = self.create_pango_layout(self.texts[which])
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new(family))
        attrs.insert(Pango.attr_weight_new(weight))
        attrs.insert(Pango.attr_size_new(int(pt * Pango.SCALE)))
        lay.set_attributes(attrs)
        return lay, lay.get_pixel_extents()[0]

    def _fit(self, which: int, w: float, h: float):
        _, ink = self._layout(which, 100)  # measured at 100 pt, then scaled to fit
        return self._layout(which, 100 * min(w / max(1, ink.width), h / max(1, ink.height)) * 0.97)

    def set_room(self, w: int, h: int, texts: tuple[str, str, str, str]) -> None:
        if (w, h) == self.room and texts == self.texts and self.lays:
            return
        self.room, self.texts = (w, h), texts
        tm = self._fit(0, w * self.TIME_MAX, h)
        rest = w - tm[1].width - w * self.GAP
        day = self._fit(1, rest, h * self.DAY_MAX)  # as wide as its room
        each = (h - day[1].height - 2 * h * self.GAP) / 2
        self.lays = (tm, day, self._fit(2, rest, each), self._fit(3, rest, each), rest)
        self.set_size_request(w, h)
        self.queue_draw()

    def do_snapshot(self, snap) -> None:
        if not self.lays:
            return
        w, h = self.room
        (tl, ti), (yl, yi), (dl, di), (ql, qi), rest = self.lays
        mid = (yi.height + h - qi.height - di.height) / 2  # the day and month between the weekday and the year
        i = self.texts[0].find(":")
        if i >= 0:  # off: drawn see-through (the time keeps its place)
            attrs = tl.get_attributes().copy()
            alpha = Pango.attr_foreground_alpha_new(65535 if self.colon else 1)
            alpha.start_index, alpha.end_index = i, i + 1
            attrs.change(alpha)
            tl.set_attributes(attrs)
        shadow = Gsk.Shadow()
        shadow.color, shadow.dx, shadow.dy, shadow.radius = _rgba(0, 0, 0, 0.8), -1, 2, 3
        snap.push_shadow([shadow])
        for lay, ink, x, y in ((tl, ti, w - ti.width, (h - ti.height) / 2),  # right, the room's full height
                               (yl, yi, (rest - yi.width) / 2, 0),  # the weekday: top, centred
                               (dl, di, (rest - di.width) / 2, mid),  # the day and month
                               (ql, qi, (rest - qi.width) / 2, h - qi.height)):  # the year at the bottom
            snap.save()
            snap.translate(Graphene.Point().init(x - ink.x, y - ink.y))
            snap.append_layout(lay, _rgba(255, 255, 255, 1))
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
        self.mirror = None  # a widget above it (the top buttons), mirrored on its top
        self._mirror_paint = None

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
            return
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
        if self.mirror is not None:
            self._reflect(snap, w, vis)
        snap.pop()

    def _reflect(self, snap, w: float, vis: float) -> None:
        """The buttons above mirrored faintly on the glass's top, as on polished glass, fading downwards."""
        src = self.mirror
        ok, p = src.compute_point(self, Graphene.Point().init(0, 0))
        sw, sh = src.get_width(), src.get_height()
        if not ok or sw <= 0 or sh <= 0:
            return
        if self._mirror_paint is None:
            self._mirror_paint = Gtk.WidgetPaintable.new(src)
            # the buttons change (pumping, ticking): their reflection along
            self._mirror_paint.connect("invalidate-contents", lambda *_: self.get_mapped() and self.queue_draw())
        band = min(vis, 70.0)
        snap.push_mask(Gsk.MaskMode.ALPHA)
        snap.append_linear_gradient(Graphene.Rect().init(0, 0, w, band), Graphene.Point().init(0, 0),
                                    Graphene.Point().init(0, band),
                                    [_stop(0.0, 0, 0, 0, 0.22), _stop(1.0, 0, 0, 0, 0.0)])
        snap.pop()
        snap.push_blur(1.5)
        snap.save()
        snap.translate(Graphene.Point().init(p.x, -p.y))  # mirrored about the glass's top edge
        snap.scale(1, -1)
        self._mirror_paint.snapshot(snap, sw, sh)
        snap.restore()
        snap.pop()
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
            if g is body:  # big glass: its edge a thick rounded wall (the bend there), lit and shaded like one
                g.curve, g.wall, g.reach, g.shine = 3.0, 2.8, 0.9, True  # a strong bend at the rim: thick
                g.warp_content = False  # what's inside stays flat (the glass behind it bends)
            g.add_css_class("puddle")
            g.set_visible(False)
            win.backdrop.watchers.append(g)
            win.cover.watchers.append(g)
        self.neck.depth = False  # water between the two: no shadow box of its own
        body.mirror = win.header  # the top buttons reflected on its glass
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
.on-air { font-family: "Oswald"; font-weight: 700; letter-spacing: 0.08em; }
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
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close (keeps playing in the tray)")
        close.connect("clicked", lambda *_: self.close())
        close.add_css_class("close-button")
        # Close and the gear in glass tiles like the dishes below (close in red glass).
        self.head_glass = []
        for child, color, cls in ((close, (227, 5, 26, 0.6), "glass-disc"), (self._settings_button(), None, "glass-head")):
            tile = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, hexpand=False)
            tile.add_css_class(cls)
            tile.mag, tile.concave, tile.depth, tile.color = 1.3, True, False, color
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
        info_btn = Gtk.Button(label="i", tooltip_text="Programme and curiosities")
        info_btn.add_css_class("info-button")
        info_btn.connect("clicked", lambda *_: self.toggle_puddle())
        tile = GlassCard([(self.backdrop, 0.45), (self.cover, 0.0)], valign=Gtk.Align.CENTER, hexpand=False)
        tile.add_css_class("glass-head")  # like the gear's tile (no pumping)
        tile.mag, tile.concave, tile.depth = 1.3, True, False
        tile.set_homogeneous(True)
        tile.append(info_btn)
        self.backdrop.watchers.append(tile)
        self.cover.watchers.append(tile)
        self.head_glass.append(tile)
        hold_while_hovered(tile)
        header.pack_end(tile)
        self.info_tile = tile
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
                disc.set_tooltip_text("Mute")
            controls.append(disc)
        controls.append(self.vol)
        inner.append(controls)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("status")
        inner.append(self.status)
        overlay.add_overlay(scrim)
        # The time and date (in Czech, capitals: "8:50" over "PÁTEK 30. ŘÍJNA 2026") at the bottom right: below
        # the volume slider, right of the cover, against the screen's right edge, filling that room.
        self.clock = ClockView(halign=Gtk.Align.END, valign=Gtk.Align.END, can_target=False)
        self.clock.set_visible(False)
        overlay.add_overlay(self.clock)
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
        # Quality: the stream now and a bobbing arrow; its list opens from the menu's left edge, glass like
        # the menu, best sound first.
        self.quality = Adw.ActionRow(title="Quality", activatable=True)
        self.quality_now = Gtk.Label(xalign=1)
        self.quality_now.add_css_class("dim-label")
        arrow = Gtk.Image(icon_name="pan-down-symbolic")
        arrow.add_css_class("bob-arrow")
        self.quality.add_suffix(self.quality_now)
        self.quality.add_suffix(arrow)
        hold_while_hovered(self.quality)
        self.quality_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.quality_list.add_css_class("boxed-list")
        self.quality_list.connect("row-activated", self.on_quality)
        head = Gtk.Label(label="Best sound first", xalign=0, margin_start=12, margin_top=8, margin_bottom=4)
        head.add_css_class("dim-label")
        head.add_css_class("caption")
        qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6, margin_start=6,
                       margin_end=6)
        qbox.append(head)
        qbox.append(self.quality_list)
        # As wide as the menu (the notes wrap), however long they are.
        self.quality_fit = Gtk.ScrolledWindow(child=qbox, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                              hexpand=True)
        qglass = MenuGlass(self, [(self.backdrop, 0.45), (self.cover, 0.0)])
        qglass.radius = 16.0
        qglass.append(self.quality_fit)
        self.quality_pop = Gtk.Popover(child=qglass, has_arrow=False, position=Gtk.PositionType.BOTTOM)
        self.quality_pop.add_css_class("glass-menu")
        self.quality_pop.set_parent(self.quality)
        self.quality_pop.connect("show", self._place_quality)
        self.quality.connect("activated", lambda *_: self.quality_pop.popup())
        self.fill_quality()
        group.add(self.quality)
        auto = Adw.SwitchRow(title="Start at login", subtitle="Plays with just the tray icon")
        auto.set_active(autostart_on())
        auto.connect("notify::active", lambda r, _: set_autostart(r.get_active()))
        group.add(auto)
        self.card_row = Adw.SwitchRow(title="Volume on the sound card", subtitle=self.app.card_text(),
                                      subtitle_lines=0, sensitive=self.app.card.ok)
        self.card_row.set_active(self.app.card_on())
        self.card_row.connect("notify::active", lambda r, _: self.app.set_card_volume(r.get_active()))
        group.add(self.card_row)
        self.picture_row = Adw.ActionRow(title="Picture now", subtitle=self.app.picture_subtitle(), subtitle_lines=0)
        group.add(self.picture_row)
        self.recognise_row = Adw.SwitchRow(title="Recognise songs (testing)", subtitle_lines=0)
        self.recognise_row.set_active(self.app.settings.get("recognise", False))
        self.recognise_row.connect("notify::active", lambda r, _: self.app.set_recognise(r.get_active()))
        group.add(self.recognise_row)
        if is_kde():
            near = Adw.SwitchRow(title="Open in the corner", subtitle="Bottom right, on the taskbar by the clock (also after full screen)")
            near.set_active(self.app.settings.get("tray_popup", True))
            near.connect("notify::active", lambda r, _: self.app.set_tray_popup(r.get_active()))
            group.add(near)
        self.enhance_row = Adw.SwitchRow(title="Enhance pictures", subtitle=self.app.enhance_status(),
                                         subtitle_lines=0)
        self.enhance_row.set_active(self.app.settings.get("enhance", True))
        self.enhance_row.connect("notify::active", lambda r, _: self.app.set_enhance(r.get_active()))
        group.add(self.enhance_row)
        self.share_row = Adw.SwitchRow(title="Share pictures on this network", subtitle_lines=0,
                                       subtitle=GLib.markup_escape_text(self.app.share_status()))
        self.share_row.set_active(self.app.settings.get("share", True))
        self.share_row.connect("notify::active", lambda r, _: self.app.set_share(r.get_active()))
        group.add(self.share_row)
        self.npu_row = Adw.SwitchRow(title="Use the NPU (testing)", subtitle=self.app.enhancer.npu_text,
                                     subtitle_lines=0)
        self.npu_row.set_active(self.app.settings.get("npu", False))
        self.npu_row.connect("notify::active", lambda r, _: self.app.set_npu(r.get_active()))
        group.add(self.npu_row)
        dialect = Adw.SwitchRow(title="Ostravština",
                                subtitle="The texts about the song, the artist and the show in the Ostrava dialect "
                                         "(short vowels, fest, fajne); off: standard Czech", subtitle_lines=0)
        dialect.set_active(self.app.settings.get("ostrava", True))
        dialect.connect("notify::active", lambda r, _: (self.app.set_look(ostrava=r.get_active()), self.app.update()))
        group.add(dialect)
        style = Adw.SwitchRow(title="Beat Radio text style",
                              subtitle="Fonts like the station logo's (Anton, Oswald); off: the normal ones"
                              if self.app.fonts_ok else "Off: its fonts are missing (fonts/ folder)",
                              subtitle_lines=0, sensitive=self.app.fonts_ok)
        style.set_active(self.app.fonts_ok and self.app.settings.get("font", "beat") == "beat")
        style.connect("notify::active", lambda r, _: self.app.set_look(font="beat" if r.get_active() else "default"))
        group.add(style)
        shade = Adw.ActionRow(title="Background shade",
                              subtitle="How much the panels behind the text darken the cover", subtitle_lines=0)
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
        start_full = Adw.SwitchRow(title="Start in full screen",
                                   subtitle="Opens filling the screen; a double click makes it a window"
                                   + (" in the bottom-right corner" if is_kde() else ""), subtitle_lines=0)
        start_full.set_active(self.app.settings.get("start_full", True))
        start_full.connect("notify::active", lambda r, _: self.app.set_look(start_full=r.get_active()))
        group.add(start_full)
        auto_up = Adw.SwitchRow(title="Update by itself",
                                subtitle="Looks for a new version every 5 minutes, installs it and restarts",
                                subtitle_lines=0)
        auto_up.set_active(self.app.settings.get("auto_update", True))
        auto_up.connect("notify::active", lambda r, _: self.app.set_look(auto_update=r.get_active()))
        group.add(auto_up)
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
        self.menu_content = fit
        btn = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        btn.add_css_class("gear-button")
        btn.connect("clicked", lambda *_: self.toggle_water(self.gear_water))
        return btn

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
        self.quality_now.set_text(dict((s[0], s[1]) for s in self.app.streams)[now])

    def on_quality(self, _list, row) -> None:
        self.quality_pop.popdown()
        if getattr(row, "sid", None) and row.sid != self.app.settings["quality"]:
            self.app.set_quality(row.sid)
            self.fill_quality()

    def _place_quality(self, pop) -> None:
        """Its left edge on the menu's, as wide as the menu, below the row."""
        w = self.quality.get_width()
        self.quality_fit.set_size_request(w, -1)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = 0, self.quality.get_height(), w, 1
        pop.set_pointing_to(r)
        GLib.idle_add(lambda: (pop.get_child().queue_draw(), False)[1])

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

    CLOCK_GAP = 22  # from the screen's right edge (as the song info from its left)
    CLOCK_PAD = 6  # off the volume slider's knob and the screen's bottom

    def _tick_clock(self) -> bool:
        now = time.localtime()
        self._clock_texts = (f"{now.tm_hour}:{now.tm_min:02d}", DAYS[now.tm_wday].upper(),
                             f"{now.tm_mday}. {MONTHS[now.tm_mon - 1]}".upper(), str(now.tm_year))
        if self._clock_texts != self.clock.texts:
            self._place_clock()
        self.clock.colon = now.tm_sec % 2 == 0
        self.clock.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _place_clock(self) -> bool:
        cover, W, H = self.cover_rect(), self.get_width(), self.get_height()
        ok, vol = self.vol.compute_bounds(self)
        if not cover or not ok or not getattr(self, "_clock_texts", None):
            self.clock.set_visible(False)
            return False
        # just below the slider's knob (the slider widget itself is taller than what's drawn of it)
        top = vol.get_y() + vol.get_height()
        stack = [self.vol]
        while stack:
            w = stack.pop()
            if w.get_css_name() == "slider":
                ok2, knob = w.compute_bounds(self)
                if ok2:
                    top = knob.get_y() + knob.get_height()
                break
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        room_w = int(W - self.CLOCK_GAP - (cover[2] + self.CLOCK_GAP))
        room_h = int(H - top - 2 * self.CLOCK_PAD)  # (a little off the slider and the screen's bottom)
        if room_w < 60 or room_h < 16:
            self.clock.set_visible(False)
            return False
        self.clock.set_room(room_w, room_h, self._clock_texts)
        self.clock.set_margin_end(self.CLOCK_GAP)
        self.clock.set_margin_bottom(self.CLOCK_PAD)
        self.clock.set_visible(True)
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
    PUDDLE_MODES = ("DNES", "ZAJÍMAVOSTI", "AKTUALITY", "ZÍTRA", "POZÍTŘÍ")
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
        return False

    def _make_puddle(self, overlay, scrim) -> None:
        behind = [(self.backdrop, 0.45), (self.cover, 0.0)]
        self.puddle = Puddle(behind, orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.END,
                             valign=Gtk.Align.START)
        # The views as tabs (a click), and the wheel's button goes to the next one.
        # They wrap when narrow. Each tab's cell gets the same share of the spare width, the tab centred in it:
        # the gaps between the tabs are all the same, whatever their names' lengths.
        self.puddle_head = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                                       min_children_per_line=1, max_children_per_line=5, homogeneous=False)
        self.puddle_tabs = []
        for i in range(len(self.PUDDLE_MODES)):
            tab = Gtk.Button(label="", has_frame=False, halign=Gtk.Align.CENTER, hexpand=True)
            tab.add_css_class("puddle-tab")
            tab.connect("clicked", lambda _b, i=i: self._puddle_go(i))
            self.puddle_tabs.append(tab)
            self.puddle_head.append(tab)
        self.puddle_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.puddle_scroll = PinnedScroll(child=self.puddle_list, vexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          vscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        # DNES: the show on air pinned one show from the top, the day's lineup scrolling behind it
        self.puddle_pin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START, visible=False)
        self.puddle_scroll.pin = self.puddle_pin
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
        water.show()

    # the tabs it goes through by itself, and for how long (s)
    TOUR = (("DNES", 20), ("ZAJÍMAVOSTI", 10), ("AKTUALITY", 10), ("ZÍTRA", 10), ("POZÍTŘÍ", 10))

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
        if text:
            body = Gtk.Label(label=text, xalign=0, wrap=True)
            body.add_css_class("show-sub")
            box.append(body)
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
        self._clear_puddle()
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
                f".show-card {{ border-radius: {28 * max(1.0, f):.0f}px; }}\n")
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
        css = (".scrim { background: linear-gradient(to top, "
               f"rgba(0,0,0,{0.92 * k:.3f}) 0%, rgba(0,0,0,{0.75 * k:.3f}) 55%, rgba(0,0,0,0) 100%); }}\n")
        if a.settings.get("font", "beat") == "beat" and a.fonts_ok:
            css += BEAT_CSS
        self.look_css.load_from_data(css.encode())
        waters = [g for w in getattr(self, "waters", []) for g in (w.body, w.neck)]
        for card in (self.show_card, *self.discs, *self.head_glass, *waters):
            card.shade = k
            card.queue_draw()

    def on_double(self, _gesture, n, x, y) -> None:
        if n != 2:
            return
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not self:
            if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Adw.HeaderBar)):
                return  # their own clicks (the title bar maximises on double click)
            w = w.get_parent()
        self.toggle_full()

    def on_wheel(self, _ctl, _dx, dy) -> bool:
        if not dy:
            return False
        self.app.nudge_volume(-5 if dy > 0 else 5)
        return True

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
        self.discs[2].set_tooltip_text("Sound on" if a.muted else "Mute")
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
        if abs(self.vol.get_value() - a.volume_now()) >= 1:
            self.vol.set_value(a.volume_now())
        self.card_row.set_subtitle(a.card_text())
        self.recognise_row.set_subtitle(GLib.markup_escape_text(
            "When the stream names no song (jingles, breaks), a 12 s clip is recognised once a minute through "
            f"Shazam's unofficial API (shazamio). {a.recognise_text}"))


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
        self.host_info: dict[str, str] = {}  # every host seen -> what Wikipedia says (Czech)
        self._last_lead = ""
        self.station_about = ""
        self.band_about = self.host_about = ""  # 'Did you know?' about the artist, about who's on air
        self.cover_tex = None
        self.logo_tex = None
        self.refined = None  # (before, after) of the last enhancing step: redrawn in, not morphed
        self.enhancer = Enhancer(on_status=self._enhance_status_changed, on_picture=self._picture_changed)
        self.share = CacheShare(self.enhancer, self._share_changed)
        self.share.set_on(self.settings.get("share", True))
        if self.settings.get("npu", False):
            self.enhancer.npu_wanted = True
            threading.Thread(target=lambda: (self.enhancer.npu_probe(), GLib.idle_add(self._npu_checked)),
                             daemon=True).start()
        self.card = CardVolume(self._card_changed)
        self._holds = 0  # animations running: the enhancing waits while any is
        _ANIM_HOLD[0] = self.anim_hold
        self.card_vol = None  # the sound card's volume, percent (card_on)
        self.card_name = ""
        self.card_set_at = 0.0  # when the slider last set it
        self._vol_cur = self._vol_target = 0.0  # mpv's volume now, and where it's gliding to
        self.muted = False  # the speaker button
        self.recognise_text = "Off"
        self._recognise_id = 0
        self._recognising = False
        self._recognised_last = None
        self._vol_glide = self._vol_save = 0  # timers
        self.upscale_src: dict[str, tuple[str, bytes]] = {}  # kind -> (key, picture bytes), for after the install
        self.orig_tex: dict[str, object] = {}  # kind -> the picture as it came
        self.picture_text = "No picture yet"
        self.icon_text = ""
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
    def do_startup(self):
        Adw.Application.do_startup(self)
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
        if "--hidden" in args and first:
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
        if w and w.get_visible() and not w.minimised:
            if is_kde():
                w.minimised = True
                w.minimize()
            else:
                w.set_visible(False)  # not every desktop minimises (Hyprland): to the tray
        else:
            self.show_window()

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
        self.update_busy, self.update_text = True, "Checking…"
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
        # Start the new version once this one is gone (same app id), then quit; with the window only if it was up.
        shown = self.win is not None and self.win.get_visible()
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
        self.streams.insert(0, ("flac", "FLAC (lossless)", url))
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

    def anim_hold(self, on: bool) -> None:
        self._holds = max(0, self._holds + (1 if on else -1))
        self.enhancer.hold(self._holds > 0)

    # volume: Rádio Beat's own (mpv), or the sound card's (card_on: mpv stays at 100 %, a knob on the card moves the slider)
    def card_on(self) -> bool:
        return self.card.ok and self.settings.get("card_volume", False)

    def card_text(self) -> str:
        if not self.card.ok:
            return "Off: needs pactl (the PipeWire/PulseAudio sound tools)"
        if not self.card_on():
            return "Off: the slider is Rádio Beat's own volume"
        return f"The slider is {self.card_name or 'the sound card'}'s volume: its knob moves the slider too"

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
        elif self.mpv:
            self.mpv.mute(self.muted)
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
            self.recognise_text = "Getting ready…"
            threading.Thread(target=self._shazam_setup, daemon=True).start()
        else:
            self.recognise_text = "Off"
        self._recognise_changed()

    def _shazam_setup(self) -> None:
        """The private Python environment with shazamio (once)."""
        py = os.path.join(self.SHAZAM_DIR, "bin", "python3")
        ok = os.path.exists(py) and subprocess.run([py, "-c", "import shazamio"], capture_output=True).returncode == 0
        if not ok:
            GLib.idle_add(self._recognise_status, "Installing shazamio (once)…")
            r = subprocess.run(["python3", "-m", "venv", self.SHAZAM_DIR], capture_output=True, text=True)
            if r.returncode == 0:
                r = subprocess.run([py, "-m", "pip", "install", "--quiet", "shazamio"], capture_output=True, text=True,
                                   timeout=900)
            ok = r.returncode == 0
            if not ok:
                GLib.idle_add(self._recognise_status, "Couldn't install shazamio: "
                              + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:120])
                return
        GLib.idle_add(self._recognise_status, "Ready: songs the stream doesn't name are recognised (Shazam)")
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
        if self.settings.get("recognise", False):
            text += f"\n• Song recognition (testing): {self.recognise_text}"
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
        how = f"(12 s clip recorded in {_secs(rec)}, Shazam, unofficial API)"
        if not found:
            self.recognise_text = f"{when}: nothing found in {_secs(took)} {how}"
            self._recognise_changed()
            return False
        self.recognise_text = f"{when}: found {found[0]} – {found[1]} in {_secs(took)} {how}"
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
        data = fetch(info["cover"]) if info and info.get("cover") else None
        GLib.idle_add(self._looked_up, lid, info, data)
        # first release and where: asked with the names the lookup found (the stream's may be misspelt:
        # "PAVLOVS ´DOG"), else at least when its album came out
        facts = song_facts(info["artist"], info["title"]) if info else ""
        if not facts:
            facts = song_facts(artist, song)
        if not facts and info:
            day = album_released(info)
            if day:
                facts = f"Album vydáno {_date(day)} ({{ago:{day[:4]}}})"
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
            self.db.album(first_artist(self.artist_raw), self.song, info["album"])
        if data:
            try:
                self.cover_tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self.upscale_src["cover"] = (info["cover"], data)
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
            return "Off: pictures are shown as they come."
        return f"{self.enhancer.status} {self.icon_text}".strip()

    def _enhance_status_changed(self, _text: str) -> None:
        if self.win:
            self.win.enhance_row.set_subtitle(self.enhance_status())
            self.win.enhance_note.set_text(self.notes_text())

    def _picture_changed(self, text: str, kind: str = "cover") -> None:
        if kind in ("show", "icon") or kind.startswith("pic:") or (kind == "logo" and self.orig_tex.get("cover")):
            return  # the cover is what's shown
        self.picture_text = text
        self.picture_kind = kind
        if self.win:
            self.win.picture_row.set_subtitle(self.picture_subtitle())

    def picture_subtitle(self) -> str:
        """Picture now: what was done, the sizes (original / enhanced), the cache."""
        size = self.enhancer.size_text(getattr(self, "picture_kind", "cover"))
        return "\n".join(x for x in (self.picture_text, size, self.enhancer.cache_text()) if x)

    def set_share(self, on: bool) -> None:
        self.settings["share"] = on
        save_settings(self.settings)
        self.share.set_on(on, asked=True)

    def _share_changed(self) -> None:
        if self.win:
            self.win.share_row.set_subtitle(GLib.markup_escape_text(self.share_status()))

    def share_status(self) -> str:
        return ("Every computer with Rádio Beat on this network keeps the same enhanced pictures (each its own "
                f"copy, none twice): what one makes, the others have too. {self.share.text} "
                f"{self.share.port_note}").strip()

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
            self.win.npu_row.set_subtitle("Checking…" if on else "Off: AI runs on the GPU")

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
        what = "Cover" if kind == "cover" else "Logo"
        f = min(size[0] / w, size[1] / h)
        if not self.settings.get("enhance", True):
            self._picture_changed(f"{what}: {w}×{h} px, shown as it comes (enhancing is off)", kind)
            return
        upscale = f >= UPSCALE_FROM  # else only sharpened and denoised, at its own size
        if not self.enhancer.ffmpeg and (not upscale or self.enhancer.ai is None):
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
            self.icon_text = "App icon: AI-sharpened in every size."
            self._enhance_status_changed("")
            return
        with open(logo, "rb") as f:
            data = f.read()
        tex = Gdk.Texture.new_from_filename(logo)
        w, h = tex.get_width(), tex.get_height()
        self.icon_text = "App icon: being made sharper…"
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
        self.icon_text = "App icon: AI-sharpened in every size (new icons show after the next start)."
        self._enhance_status_changed("")
        return False


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
