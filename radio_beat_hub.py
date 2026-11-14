#!/usr/bin/env python3
"""Rádio Beat hub: the shared database every Rádio Beat reaches over the internet.

It keeps what the apps send it (the enhanced pictures with their notes, the songs played, the track info looked
up) and hands it to every other app. Each thing it takes gets the next number (seq), so an app asks for "what
came after n" and never misses something another app sent late. Standard library only; one folder of data:

    files/<sha1>.jpg|.txt   pictures and their notes (never replaced once there)
    hub.db                  SQLite: files, plays, songinfo (each row with its seq)

Anyone can send, so everything is checked: names (40 hex + .jpg/.txt), whole JPEGs, sizes, text lengths, cover
links only from the music services' picture servers, a total size cap, and a per-address rate limit.

    radio_beat_hub.py [--port 48766] [--data ~/.local/share/radio-beat-hub] [--max-gb 20]
"""

import argparse
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque

NAME = re.compile(r"^[0-9a-f]{40}\.(jpg|txt)$")
FILE_MAX = 40 << 20  # a picture's bytes at most
BODY_MAX = 4 << 20  # a list of plays or song info at most
PAGE = 2000  # rows per answer
SAME = 1800  # s: the same song this close in time is the same play (as the app's History.SAME)
COVER_HOSTS = re.compile(r"^https://([a-z0-9-]+\.)*(dzcdn\.net|mzstatic\.com|ytimg\.com|deezer\.com|"
                         r"apple\.com|googleusercontent\.com|radiobeat\.cz)/", re.I)
LOG_MAX = 256 << 10  # bytes of a computer's log kept (its newest part); never served back over the internet
SHOT_MAX = 12 << 20  # bytes of a window's picture at most
LOGS_MAX = 40  # computers' logs at most (the oldest dropped)
LOG_ID = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,80}$")  # (no leading dot: never .. or a hidden file)
CLAIM_TTL = 240  # s: a picture being made is that computer's this long (it may have gone: then anyone's again)
CLAIMS_MAX = 5000
RATE = (3000, 600)  # requests per address in this many seconds at most (a first upload sends ~1000 pictures)


class Hub:
    def __init__(self, data: str, max_bytes: int):
        self.dir = os.path.join(data, "files")
        os.makedirs(self.dir, exist_ok=True)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.claims: dict[str, tuple[str, float]] = {}  # picture being made -> (who, until); in memory, short-lived
        self.claims_lock = threading.Lock()
        self.db = sqlite3.connect(os.path.join(data, "hub.db"), check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS files (seq INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, size INTEGER,
                                              at REAL);
            CREATE TABLE IF NOT EXISTS plays (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, artist TEXT,
                                              title TEXT, album TEXT, show TEXT, host TEXT, cover TEXT, facts TEXT);
            CREATE INDEX IF NOT EXISTS plays_song ON plays (lower(artist), lower(title), at);
            CREATE TABLE IF NOT EXISTS songinfo (seq INTEGER UNIQUE, artist TEXT, title TEXT, album TEXT,
                                                 cover TEXT, facts TEXT, shown_artist TEXT, shown_title TEXT,
                                                 PRIMARY KEY (artist, title));
            CREATE TABLE IF NOT EXISTS counter (name TEXT PRIMARY KEY, n INTEGER);
            CREATE TABLE IF NOT EXISTS titles (seq INTEGER UNIQUE, name TEXT PRIMARY KEY, cs TEXT);
        """)
        self.used = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0]

    # --- pictures ---------------------------------------------------------------------------------------------
    def list_files(self, since: int) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT seq, name FROM files WHERE seq > ? ORDER BY seq LIMIT ?",
                                   (since, PAGE * 5)).fetchall()
        return {"id": "hub", "files": [n for _, n in rows], "next": rows[-1][0] if rows else since,
                "more": len(rows) == PAGE * 5}

    def put_log(self, who: str, text: bytes) -> int:
        """A computer's log (its newest part), for whoever runs the hub to read (logs/<who>.log): the app's own
        trouble on computers nobody here can look at. 204 kept, 400 not text."""
        try:
            text.decode()
        except UnicodeDecodeError:
            return 400
        folder = os.path.join(os.path.dirname(self.dir), "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, who + ".log")
        with open(path + ".part", "wb") as f:
            f.write(text[-LOG_MAX:])
        os.replace(path + ".part", path)
        logs = sorted((os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".log")), key=os.path.getmtime)
        for old in logs[:-LOGS_MAX]:
            os.remove(old)
        return 204

    def put_shot(self, who: str, data: bytes) -> int:
        """A picture of a computer's Rádio Beat window (its own window only), beside its log: logs/<who>.jpg."""
        if data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"  # (where the JPEG encoder isn't at hand: Bazzite's sandboxed image loaders)
        else:
            return 400
        folder = os.path.join(os.path.dirname(self.dir), "logs")
        os.makedirs(folder, exist_ok=True)
        for old in (".jpg", ".png"):
            if old != ext and os.path.exists(os.path.join(folder, who + old)):
                os.remove(os.path.join(folder, who + old))
        path = os.path.join(folder, who + ext)
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        return 204

    def claim(self, name: str, by: str) -> dict:
        """A computer about to make a picture asks first, so no two make the same one. state: "have" (it's here:
        fetch it), "busy" (another is making it; left: for how many seconds at most), "yours" (go ahead)."""
        if os.path.exists(os.path.join(self.dir, name)):
            return {"state": "have"}
        now = time.monotonic()
        with self.claims_lock:
            who, until = self.claims.get(name, ("", 0.0))
            if until > now and who != by:
                return {"state": "busy", "left": round(until - now)}
            if len(self.claims) >= CLAIMS_MAX:
                self.claims = {k: v for k, v in self.claims.items() if v[1] > now}
                if len(self.claims) >= CLAIMS_MAX:  # (flooded: nobody is told to wait)
                    return {"state": "yours"}
            self.claims[name] = (by, now + CLAIM_TTL)
        return {"state": "yours"}

    def put_file(self, name: str, data: bytes) -> int:
        """HTTP status: 201 kept, 409 there already / not wanted, 400 not a whole picture, 507 full."""
        path = os.path.join(self.dir, name)
        if os.path.exists(path):
            return 409
        if name.endswith(".txt"):
            if len(data) > 4096 or not os.path.exists(path[:-4] + ".jpg"):
                return 409
            try:
                data.decode()
            except UnicodeDecodeError:
                return 400
        elif not (data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9" and 2000 < len(data) <= FILE_MAX):
            return 400
        with self.lock:
            if self.used + len(data) > self.max_bytes:
                return 507
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO files (name, size, at) VALUES (?, ?, ?)",
                                (name, len(data), time.time()))
            self.used += len(data)
        return 201

    # --- songs played -----------------------------------------------------------------------------------------
    @staticmethod
    def _text(x, n: int, may_be_none: bool = True) -> bool:
        return (x is None and may_be_none) or (isinstance(x, str) and len(x) <= n)

    def _cover(self, c):
        return c if isinstance(c, str) and COVER_HOSTS.match(c) and len(c) <= 500 else None

    def get_plays(self, since: int) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT seq, at, artist, title, album, show, host, cover, facts FROM plays "
                                   "WHERE seq > ? ORDER BY seq LIMIT ?", (since, PAGE)).fetchall()
        return {"plays": [list(r[1:]) for r in rows], "next": rows[-1][0] if rows else since,
                "more": len(rows) == PAGE}

    def put_plays(self, rows) -> int:
        new = 0
        now = time.time()
        with self.lock, self.db:
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, list) and 6 <= len(r) < 8:
                    r = r + [None] * (8 - len(r))
                if not (isinstance(r, list) and len(r) == 8 and isinstance(r[0], (int, float))
                        and 1e9 < r[0] < now + 86400 and self._text(r[1], 300, False) and self._text(r[2], 300, False)
                        and all(self._text(x, 500) for x in r[3:])):
                    continue
                at, artist, title, album, show, host, cover, facts = r
                cover = self._cover(cover)
                same = self.db.execute("SELECT seq, at, artist, title, album, show, host, cover, facts FROM plays "
                                       "WHERE lower(artist) = lower(?) AND lower(title) = lower(?) AND at BETWEEN ? "
                                       "AND ? LIMIT 1", (artist, title, at - SAME, at + SAME)).fetchone()
                if same:  # the same play from another app: only what it lacked, then sent out again (a new seq)
                    o = list(same[1:])
                    merged = o[:3] + [o[3] or album, o[4] or show, o[5] or host, o[6] or cover, o[7] or facts]
                    if merged != o:
                        self.db.execute("DELETE FROM plays WHERE seq = ?", (same[0],))
                        self.db.execute("INSERT INTO plays (at, artist, title, album, show, host, cover, facts) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", merged)
                    continue
                self.db.execute("INSERT INTO plays (at, artist, title, album, show, host, cover, facts) VALUES "
                                "(?, ?, ?, ?, ?, ?, ?, ?)", (at, artist, title, album, show, host, cover, facts))
                new += 1
        return new

    # --- track info -------------------------------------------------------------------------------------------
    def _next(self) -> int:
        n = (self.db.execute("SELECT n FROM counter WHERE name = 'songinfo'").fetchone() or (0,))[0] + 1
        self.db.execute("INSERT OR REPLACE INTO counter VALUES ('songinfo', ?)", (n,))
        return n

    def get_songinfo(self, since: int) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT seq, artist, title, album, cover, facts, shown_artist, shown_title FROM "
                                   "songinfo WHERE seq > ? ORDER BY seq LIMIT ?", (since, PAGE)).fetchall()
        # as the app's History.info_export: [artist, title, album, cover, facts, at, shown_artist, shown_title]
        return {"songs": [[r[1], r[2], r[3], r[4], r[5], 0, r[6], r[7]] for r in rows],
                "next": rows[-1][0] if rows else since, "more": len(rows) == PAGE}

    def put_songinfo(self, rows) -> int:
        n = 0
        with self.lock, self.db:
            for r in rows if isinstance(rows, list) else []:
                if not (isinstance(r, list) and len(r) in (6, 8) and self._text(r[0], 300, False)
                        and self._text(r[1], 300, False) and all(self._text(x, 500) for x in r[2:5] + r[6:])):
                    continue
                artist, title, album, cover, facts = r[0].lower(), r[1].lower(), r[2], self._cover(r[3]), r[4]
                shown = (r[6], r[7]) if len(r) == 8 else (None, None)
                old = self.db.execute("SELECT album, cover, facts, shown_artist, shown_title FROM songinfo WHERE "
                                      "artist = ? AND title = ?", (artist, title)).fetchone()
                if old is None:
                    self.db.execute("INSERT INTO songinfo VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (self._next(), artist, title, album, cover, facts, *shown))
                    n += 1
                    continue
                merged = (old[0] or album, old[1] or cover, old[2] if old[2] else (facts or old[2]),
                          old[3] or shown[0], old[4] or shown[1])
                if merged != tuple(old):  # something new for it: sent out again
                    self.db.execute("UPDATE songinfo SET seq = ?, album = ?, cover = ?, facts = ?, shown_artist = ?, "
                                    "shown_title = ? WHERE artist = ? AND title = ?",
                                    (self._next(), *merged, artist, title))
                    n += 1
        return n

    # --- song and album names in Czech (what an app's translation service made of them; the first one stays) ---
    def get_titles(self, since: int) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT seq, name, cs FROM titles WHERE seq > ? ORDER BY seq LIMIT ?",
                                   (since, PAGE)).fetchall()
        return {"titles": [[r[1], r[2]] for r in rows], "next": rows[-1][0] if rows else since,
                "more": len(rows) == PAGE}

    def put_titles(self, rows) -> int:
        n = 0
        with self.lock, self.db:
            for r in rows if isinstance(rows, list) else []:
                if not (isinstance(r, list) and len(r) == 2 and self._text(r[0], 200, False)
                        and self._text(r[1], 300, False) and r[0] and r[1] and r[0].isascii() and r[0] == r[0].lower()):
                    continue
                if self.db.execute("SELECT 1 FROM titles WHERE name = ?", (r[0],)).fetchone():
                    continue
                seq = (self.db.execute("SELECT n FROM counter WHERE name = 'titles'").fetchone() or (0,))[0] + 1
                self.db.execute("INSERT OR REPLACE INTO counter VALUES ('titles', ?)", (seq,))
                self.db.execute("INSERT INTO titles VALUES (?, ?, ?)", (seq, r[0], r[1]))
                n += 1
        return n

    def stats(self) -> dict:
        with self.lock:
            files = self.db.execute("SELECT COUNT(*) FROM files WHERE name LIKE '%.jpg'").fetchone()[0]
            plays = self.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
            songs = self.db.execute("SELECT COUNT(*) FROM songinfo").fetchone()[0]
        return {"id": "hub", "hub": 1, "pictures": files, "bytes": self.used, "max_bytes": self.max_bytes,
                "plays": plays, "songs": songs}


def serve(hub: Hub, port: int) -> None:
    hits: dict[str, deque] = defaultdict(deque)
    hits_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _who(self) -> str:  # behind a tunnel every request comes from it: the real address in a header
            return (self.headers.get("Cf-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0]
                    or self.client_address[0]).strip()

        def _allowed(self) -> bool:
            if self.client_address[0] == "127.0.0.1" and not (self.headers.get("X-Forwarded-For")
                                                              or self.headers.get("Cf-Connecting-IP")):
                return True  # the app on this computer itself (straight, not through the tunnel)
            now = time.monotonic()
            with hits_lock:
                q = hits[self._who()]
                while q and q[0] < now - RATE[1]:
                    q.popleft()
                if len(q) >= RATE[0]:
                    self._send(429, b"")
                    return False
                q.append(now)
                if len(hits) > 10000:
                    hits.clear()
            return True

        def _send(self, code: int, body: bytes, kind: str = "application/octet-stream") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def _since(self) -> int:
            try:
                return max(0, int(self.path.split("since=", 1)[1].split("&")[0]))
            except (IndexError, ValueError):
                return 0

        def _body(self, limit: int):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                self._send(413 if n > limit else 400, b"")
                return None
            return self.rfile.read(n)

        def do_GET(self):
            if not self._allowed():
                return
            p = self.path
            if p in ("/", "/radio-beat/v1/hello"):
                return self._json(hub.stats())
            if p.startswith("/radio-beat/v1/list"):
                return self._json(hub.list_files(self._since()))
            if p.startswith("/radio-beat/v1/plays"):
                return self._json(hub.get_plays(self._since()))
            if p.startswith("/radio-beat/v1/songinfo"):
                return self._json(hub.get_songinfo(self._since()))
            if p.startswith("/radio-beat/v1/titles"):
                return self._json(hub.get_titles(self._since()))
            name = p.rsplit("/", 1)[-1]
            if p.startswith("/radio-beat/v1/file/") and NAME.match(name):
                try:
                    with open(os.path.join(hub.dir, name), "rb") as f:
                        return self._send(200, f.read(), "image/jpeg" if name.endswith(".jpg") else "text/plain")
                except OSError:
                    pass
            self._send(404, b"")

        def do_PUT(self):
            if not self._allowed():
                return
            p = self.path
            if p in ("/radio-beat/v1/plays", "/radio-beat/v1/songinfo", "/radio-beat/v1/titles"):
                body = self._body(BODY_MAX)
                if body is None:
                    return
                try:
                    rows = json.loads(body)
                except ValueError:
                    return self._send(400, b"")
                got = (hub.put_plays(rows) if p.endswith("plays") else hub.put_titles(rows) if p.endswith("titles")
                       else hub.put_songinfo(rows))
                return self._json({"new": got})
            name = p.rsplit("/", 1)[-1]
            if p.startswith("/radio-beat/v1/shot/") and LOG_ID.match(name):
                body = self._body(SHOT_MAX)
                if body is None:
                    return
                return self._send(hub.put_shot(name, body), b"")
            if p.startswith("/radio-beat/v1/log/") and LOG_ID.match(name):
                body = self._body(LOG_MAX)
                if body is None:
                    return
                return self._send(hub.put_log(name, body), b"")
            if p.startswith("/radio-beat/v1/claim/") and NAME.match(name):
                body = self._body(512)
                if body is None:
                    return
                try:
                    by = str(json.loads(body).get("by", ""))[:80]
                except (ValueError, AttributeError):
                    return self._send(400, b"")
                return self._json(hub.claim(name, by))
            if not (p.startswith("/radio-beat/v1/file/") and NAME.match(name)):
                return self._send(400, b"")
            body = self._body(FILE_MAX)
            if body is None:
                return
            self._send(hub.put_file(name, body), b"")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)  # (only the tunnel reaches it)
    server.daemon_threads = True
    print(f"Rádio Beat hub on 127.0.0.1:{port}, {hub.stats()}", flush=True)
    server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=48766)
    ap.add_argument("--data", default=os.path.expanduser("~/.local/share/radio-beat-hub"))
    ap.add_argument("--max-gb", type=float, default=20.0)
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    try:
        serve(Hub(a.data, int(a.max_gb * 2**30)), a.port)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
