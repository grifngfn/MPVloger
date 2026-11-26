#!/usr/bin/env python3
"""
MPVloger · watch-history server

  * serves dashboard.html (it must sit next to this file)
  * reads your mpv log, strictly read-only:  ~/.config/mpv/watch-history.jsonl
  * keeps everything in one SQLite file next to this script:  history.db
      - sessions: a copy of the log, imported incrementally (each sync reads only the new lines)
      - your edits and "watched outside mpv" episodes, numbered with a rev and an updated time

The log is never written to. The database remembers how far it has read. If the log is cleared,
replaced or edited, the existing history is kept and the dashboard offers "Rebuild from log".

Standard library only (no pip). Listens on 127.0.0.1 only. Stop it with Ctrl+C.

    python server.py            # then open http://127.0.0.1:8787
    python server.py --open     # also opens your browser
    python server.py --port 9000 --log /path/to/watch-history.jsonl --db /path/to/history.db

Fallback without the server: open dashboard.html straight from disk and choose the log file
(read-only, no editing).
"""
import argparse
import datetime
import difflib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ.get("WH_LOG", "~/.config/mpv/watch-history.jsonl")).expanduser()
DB_PATH = Path(os.environ.get("WH_DB", str(HERE / "history.db"))).expanduser()
MAX_BODY = 1_000_000  # bytes
FP_N = 4096  # bytes fingerprinted at the start of the log and just before the read position
BATCH = 2000  # rows per insert while importing

DB = None
LOCK = threading.RLock()  # one connection, shared by the request threads, always used under this lock

STATUSES = {"watching", "completed", "hold", "dropped", "plan"}
KINDS = {"anime", "kdrama", "cdrama", "jdrama", "western", "other"}
FIELDS = {  # allowed per-show fields and their types
    "title": str,
    "total": int,
    "status": str,
    "rating": (int, float),
    "poster": str,
    "notes": str,
    "kind": str,
}
STAMPS = ("rev", "updated")  # owned by the server; anything the browser sends for these is ignored

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened TEXT NOT NULL, closed TEXT NOT NULL, filename TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
  watched_secs REAL, duration_secs REAL, percent REAL, size_bytes INTEGER,
  UNIQUE (opened, filename)
);
CREATE TABLE IF NOT EXISTS shows (
  key TEXT PRIMARY KEY,
  title TEXT, total INTEGER, status TEXT, rating REAL, poster TEXT, notes TEXT, kind TEXT,
  rev INTEGER NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_eps (
  show_key TEXT NOT NULL, season INTEGER NOT NULL, ep INTEGER NOT NULL,
  added_rev INTEGER NOT NULL, added TEXT NOT NULL,
  PRIMARY KEY (show_key, season, ep)
);
CREATE TABLE IF NOT EXISTS lookups (
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, matched TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0, confident INTEGER NOT NULL DEFAULT 0, looked_up TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- database basics

def open_db():
    global DB
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=10)
    DB.execute("PRAGMA journal_mode=WAL")
    DB.execute("PRAGMA synchronous=NORMAL")
    DB.executescript(SCHEMA)
    if "kind" not in [r[1] for r in DB.execute("PRAGMA table_info(shows)")]:  # database made before types existed
        DB.execute("ALTER TABLE shows ADD COLUMN kind TEXT")
    if not meta_get("epoch"):
        meta_set("epoch", secrets.token_hex(4))


def meta_get(k, default=""):
    row = DB.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(k, v):
    DB.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, str(v)))


@contextmanager
def tx():
    """One atomic transaction: everything inside is saved together or not at all."""
    with LOCK:
        DB.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            DB.execute("ROLLBACK")
            raise
        else:
            DB.execute("COMMIT")


# ---------------------------------------------------------------- importing the log (read-only)

def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:
        return None


def parse_record(line):
    """One log line -> a row tuple, or None when the line is not a usable record."""
    try:
        r = json.loads(line)
    except ValueError:  # includes bad UTF-8
        return None
    if not isinstance(r, dict):
        return None
    fn, op, cl = r.get("filename"), r.get("opened"), r.get("closed")
    if not (isinstance(fn, str) and fn and isinstance(op, str) and op and isinstance(cl, str) and cl):
        return None
    folder = r.get("folder") if isinstance(r.get("folder"), str) else ""
    size = _num(r.get("size_bytes"))
    return (op, cl, fn, folder, _num(r.get("watched_secs")), _num(r.get("duration_secs")),
            _num(r.get("percent")), None if size is None else int(size))


def _fp(f, start, end):
    f.seek(start)
    return hashlib.sha1(f.read(max(0, end - start))).hexdigest()


def log_change_reason(f, size, mtime_ns):
    """Does the log still look like the one we read up to the saved position? None = yes."""
    off = int(meta_get("offset", "0"))
    if off <= 0:
        return None
    if size < off:
        return "The log file is shorter than at the last sync (it was cleared or replaced)."
    if _fp(f, 0, min(FP_N, off)) != meta_get("head") or _fp(f, max(0, off - FP_N), off) != meta_get("tail"):
        return "Earlier lines of the log file changed since the last sync."
    if size == int(meta_get("size", "-1")) and mtime_ns != int(meta_get("mtime", "-1")):
        return "The log file was modified without any new sessions being added."
    return None


def _status(added, exists):
    total = DB.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return {"ok": True, "log_exists": exists, "added": added, "total": total,
            "bad": int(meta_get("bad", "0")), "notice": meta_get("notice")}


INSERT_SESSION = ("INSERT OR IGNORE INTO sessions "
                  "(opened, closed, filename, folder, watched_secs, duration_secs, percent, size_bytes) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")


def sync(rebuild=False):
    """Import new log lines. Normal sync: continue from the saved position. If the log looks changed,
    keep all existing rows, re-read from the start (duplicates are ignored) and raise a notice.
    rebuild=True: replace the sessions with exactly what the log holds now (edits are kept).
    Everything happens in one transaction, so a failure leaves the database as it was."""
    with LOCK:
        try:
            f = open(LOG, "rb")  # read-only, always
        except OSError:
            if rebuild:
                raise
            return _status(0, False)
        with f:
            st = os.fstat(f.fileno())
            added = 0
            with tx():
                reason = None if rebuild else log_change_reason(f, st.st_size, st.st_mtime_ns)
                if reason:
                    meta_set("notice", reason)
                start = 0 if (rebuild or reason) else int(meta_get("offset", "0"))
                if rebuild:
                    DB.execute("DELETE FROM sessions")
                    meta_set("notice", "")
                    meta_set("epoch", secrets.token_hex(4))  # tells open pages to reload everything
                f.seek(start)
                pos, bad, batch = start, 0, []
                while True:
                    line = f.readline()
                    if not line.endswith(b"\n"):
                        break  # end of file, or a line mpv is still writing: leave it for next time
                    pos += len(line)
                    if not line.strip():
                        continue
                    rec = parse_record(line)
                    if rec is None:
                        bad += 1
                        continue
                    batch.append(rec)
                    if len(batch) >= BATCH:
                        added += DB.executemany(INSERT_SESSION, batch).rowcount
                        batch = []
                if batch:
                    added += DB.executemany(INSERT_SESSION, batch).rowcount
                meta_set("bad", bad if start == 0 else int(meta_get("bad", "0")) + bad)
                end = os.fstat(f.fileno())
                meta_set("offset", pos)
                meta_set("head", _fp(f, 0, min(FP_N, pos)))
                meta_set("tail", _fp(f, max(0, pos - FP_N), pos))
                meta_set("size", end.st_size)
                meta_set("mtime", end.st_mtime_ns)
            return _status(added, True)


def sessions_since(after, epoch):
    """Rows newer than `after`. If the page's epoch is out of date (rebuild), send everything again."""
    keys = ("id", "opened", "closed", "filename", "folder", "watched_secs", "duration_secs", "percent", "size_bytes")
    with LOCK:
        current = meta_get("epoch")
        reset = epoch is not None and epoch != current
        if reset:
            after = 0
        rows = DB.execute("SELECT " + ", ".join(keys) + " FROM sessions WHERE id > ? ORDER BY id", (after,)).fetchall()
    return {"epoch": current, "reset": reset, "last_id": rows[-1][0] if rows else after,
            "rows": [dict(zip(keys, r)) for r in rows]}


# ---------------------------------------------------------------- edits (stored in the database)

def clean_manual(v):
    """Episodes watched outside mpv: {"1": [1, 2, 3]} = season number as text ("0" = no season)
    mapped to sorted, unique episode numbers. Returns None when nothing valid is left."""
    if not isinstance(v, dict):
        return None
    out = {}
    for season, eps in v.items():
        if not isinstance(season, str) or not (season.isascii() and season.isdigit()) or int(season) > 99:
            continue
        if not isinstance(eps, list):
            continue
        nums = sorted({e for e in eps if isinstance(e, int) and not isinstance(e, bool) and 1 <= e <= 9999})
        if nums:
            out[str(int(season))] = nums[:2000]
    return out or None


def _core(item):
    """A stored show without its server stamps (for comparing old and new)."""
    if not isinstance(item, dict):
        return None
    return {k: v for k, v in item.items() if k not in STAMPS}


def stamp(old, new):
    """Tracking: bump the global rev when anything changed, and mark every changed show with that
    rev and a UTC time. Unchanged shows keep the stamps they already had."""
    old_shows = old.get("shows", {})
    rev = old.get("rev") if isinstance(old.get("rev"), int) else 0
    changed = {k for k, item in new["shows"].items() if _core(old_shows.get(k)) != item}
    removed = any(k not in new["shows"] for k in old_shows)
    if changed or removed:
        rev += 1
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for key, item in new["shows"].items():
        if key in changed:
            item["rev"] = rev
            item["updated"] = now
        else:
            prev = old_shows[key]  # unchanged means it exists and matches
            if isinstance(prev.get("rev"), int):
                item["rev"] = prev["rev"]
            if isinstance(prev.get("updated"), str):
                item["updated"] = prev["updated"]
    new["rev"] = rev
    return new


def clean_edits(data):
    """Keep only the fields we know about; return None if the payload is not usable."""
    if not isinstance(data, dict) or not isinstance(data.get("shows"), dict):
        return None
    shows = {}
    for key, ed in data["shows"].items():
        if not isinstance(key, str) or len(key) > 300 or not isinstance(ed, dict):
            continue
        item = {}
        for field, typ in FIELDS.items():
            v = ed.get(field)
            if isinstance(v, bool) or not isinstance(v, typ):  # bool is a subclass of int
                continue
            if isinstance(v, str):
                v = v[:2000]
                if field == "poster" and not v.lower().startswith(("http://", "https://")):
                    continue
                if field == "status" and v not in STATUSES:
                    continue
                if field == "kind" and v not in KINDS:
                    continue
            if field == "total" and v < 1:
                continue
            if field == "rating" and not 0 < v <= 10:
                continue
            item[field] = v
        manual = clean_manual(ed.get("manual"))
        if manual:
            item["manual"] = manual
        if item:
            shows[key] = item
    return {"version": 2, "shows": shows}


def load_edits():
    """The edits in the shape the dashboard uses: {"version", "rev", "shows": {key: {...}}}."""
    with LOCK:
        shows = {}
        for key, title, total, status, rating, poster, notes, kind, rev, updated in DB.execute(
                "SELECT key, title, total, status, rating, poster, notes, kind, rev, updated FROM shows"):
            item = {}
            for name, val in (("title", title), ("total", total), ("status", status),
                              ("rating", rating), ("poster", poster), ("notes", notes), ("kind", kind)):
                if val is not None:
                    item[name] = val
            item["rev"] = rev
            item["updated"] = updated
            shows[key] = item
        manual = {}
        for key, season, ep in DB.execute("SELECT show_key, season, ep FROM manual_eps ORDER BY show_key, season, ep"):
            manual.setdefault(key, {}).setdefault(str(season), []).append(ep)
        for key, m in manual.items():
            shows.setdefault(key, {"rev": 0, "updated": ""})["manual"] = m
        return {"version": 2, "rev": int(meta_get("rev", "0")), "shows": shows}


def put_edits(new):
    """Save the page's edits: only shows that really changed are written, each stamped with the new rev.
    Manual episodes remember the rev and time they were added."""
    with tx():
        old = load_edits()
        changed = {k for k, item in new["shows"].items() if _core(old["shows"].get(k)) != item}
        removed = [k for k in old["shows"] if k not in new["shows"]]
        if not changed and not removed:
            return
        data = stamp(old, new)
        for key in removed:
            DB.execute("DELETE FROM manual_eps WHERE show_key = ?", (key,))
            DB.execute("DELETE FROM shows WHERE key = ?", (key,))
        for key in changed:
            item = data["shows"][key]
            DB.execute(
                "INSERT INTO shows (key, title, total, status, rating, poster, notes, kind, rev, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "title = excluded.title, total = excluded.total, status = excluded.status, "
                "rating = excluded.rating, poster = excluded.poster, notes = excluded.notes, kind = excluded.kind, "
                "rev = excluded.rev, updated = excluded.updated",
                (key, item.get("title"), item.get("total"), item.get("status"), item.get("rating"),
                 item.get("poster"), item.get("notes"), item.get("kind"), item["rev"], item["updated"]))
            have = {(s, e) for s, e in DB.execute("SELECT season, ep FROM manual_eps WHERE show_key = ?", (key,))}
            want = {(int(s), e) for s, eps in item.get("manual", {}).items() for e in eps}
            for s, e in have - want:
                DB.execute("DELETE FROM manual_eps WHERE show_key = ? AND season = ? AND ep = ?", (key, s, e))
            for s, e in sorted(want - have):
                DB.execute("INSERT INTO manual_eps (show_key, season, ep, added_rev, added) VALUES (?, ?, ?, ?, ?)",
                           (key, s, e, item["rev"], item["updated"]))
        meta_set("rev", data["rev"])


# ---------------------------------------------------------------- online type lookup (only when you ask for it)
# Sends only a cleaned title and year to the sources you switched on. Never filenames, folders or watch data.
# APIs (checked Nov 2026): AniList GraphQL (no key, may be limited to 30 requests/min), TVmaze /search/shows
# (no key, 20 calls/10 s), TMDB /3/search/tv (free key: "API Read Access Token" as Bearer, or the older v3 api_key).
UA = "MPVloger"
SOURCES = ("anilist", "tmdb", "tvmaze")  # order tried
GAP = {"anilist": 2.1, "tmdb": 0.3, "tvmaze": 0.6}  # seconds between calls
MAX_ITEMS = 10  # titles per detect call
ANILIST_Q = ("query($s:String){Media(search:$s,type:ANIME){title{romaji english} synonyms "
             "startDate{year} countryOfOrigin}}")


class RateLimited(Exception):
    pass


def _fetch(url, payload=None, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=12) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited() from e
        if e.code == 404:  # AniList answers "no match" with a 404
            return {}
        raise


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _year_ok(want, got):
    return want is None or got is None or abs(want - got) <= 1


def _year_of(text):
    return int(text[:4]) if isinstance(text, str) and text[:4].isdigit() else None


def _kind_from(country, language, animation):
    c, lang = (country or "").upper(), (language or "").lower()
    if c == "KR" or lang in ("ko", "korean"):
        return "kdrama"
    if c in ("CN", "TW", "HK") or lang in ("zh", "cn", "chinese", "mandarin", "cantonese"):
        return "anime" if animation else "cdrama"
    if c == "JP" or lang in ("ja", "japanese"):
        return "anime" if animation else "jdrama"
    return "western"


def look_anilist(title, year, key):
    m = (_fetch("https://graphql.anilist.co", {"query": ANILIST_Q, "variables": {"s": title}}).get("data") or {}).get("Media")
    if not m:
        return None
    t = m.get("title") or {}
    names = [n for n in [t.get("romaji"), t.get("english")] + list(m.get("synonyms") or []) if isinstance(n, str) and n]
    if not names:
        return None
    best = max(names, key=lambda n: _sim(title, n))
    return "anime", best, _sim(title, best), (m.get("startDate") or {}).get("year")


def look_tvmaze(title, year, key):
    res = _fetch("https://api.tvmaze.com/search/shows?" + urlencode({"q": title}))
    best = None
    for r in (res if isinstance(res, list) else [])[:5]:
        s = r.get("show") or {}
        sc = _sim(title, s.get("name", ""))
        if best is None or sc > best[0]:
            best = (sc, s)
    if not best:
        return None
    sc, s = best
    ch = s.get("network") or s.get("webChannel") or {}
    kind = _kind_from((ch.get("country") or {}).get("code"), s.get("language"), s.get("type") == "Animation")
    return kind, s.get("name", ""), sc, _year_of(s.get("premiered"))


def look_tmdb(title, year, key):
    q = {"query": title, "include_adult": "false", "language": "en-US", "page": "1"}
    headers = {}
    if key.startswith("eyJ"):  # API Read Access Token (TMDB's default)
        headers["Authorization"] = "Bearer " + key
    else:  # older v3 API key
        q["api_key"] = key
    res = _fetch("https://api.themoviedb.org/3/search/tv?" + urlencode(q), headers=headers)
    best = None
    for r in (res.get("results") or [])[:5]:
        sc = max(_sim(title, r.get("name", "")), _sim(title, r.get("original_name", "")))
        if best is None or sc > best[0]:
            best = (sc, r)
    if not best:
        return None
    sc, r = best
    kind = _kind_from((r.get("origin_country") or [None])[0], r.get("original_language"), 16 in (r.get("genre_ids") or []))
    return kind, r.get("name", ""), sc, _year_of(r.get("first_air_date"))


def lookup_settings():
    """Settings and saved lookups for the page. The TMDB key itself is never sent back."""
    cols = ("key", "kind", "source", "matched", "score", "confident", "looked_up")
    with LOCK:
        try:
            src = json.loads(meta_get("lookup_sources") or "{}")
            skip = json.loads(meta_get("lookup_skip") or "[]")
        except ValueError:
            src, skip = {}, []
        has_key = bool(meta_get("tmdb_key"))
        rows = DB.execute("SELECT " + ", ".join(cols) + " FROM lookups").fetchall()
    src = src if isinstance(src, dict) else {}
    skip = skip if isinstance(skip, list) else []
    return {"sources": {"anilist": src.get("anilist") is not False, "tvmaze": src.get("tvmaze") is not False,
                        "tmdb": bool(src.get("tmdb")) and has_key},
            "tmdb_key_set": has_key, "skip": [k for k in skip if isinstance(k, str)],
            "lookups": [dict(zip(cols, r)) for r in rows]}


def save_lookup_settings(body):
    if not isinstance(body, dict):
        raise ValueError("invalid settings")
    with tx():
        if isinstance(body.get("sources"), dict):
            try:
                cur = json.loads(meta_get("lookup_sources") or "{}")
            except ValueError:
                cur = {}
            cur = cur if isinstance(cur, dict) else {}
            for n in SOURCES:
                if isinstance(body["sources"].get(n), bool):
                    cur[n] = body["sources"][n]
            meta_set("lookup_sources", json.dumps(cur))
        k = body.get("tmdb_key")
        if isinstance(k, str):
            k = k.strip()
            if k and not re.fullmatch(r"[A-Za-z0-9._-]{16,800}", k):
                raise ValueError("That does not look like a TMDB key or access token.")
            meta_set("tmdb_key", k)
        if isinstance(body.get("skip"), list):
            keys = {s for s in body["skip"] if isinstance(s, str) and 0 < len(s) <= 300}
            meta_set("lookup_skip", json.dumps(sorted(keys)[:5000]))


def clean_items(body):
    """[{key, title, year}] from the page; anything else is dropped. Returns None if unusable."""
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        key, title, year = it.get("key"), it.get("title"), it.get("year")
        if not (isinstance(key, str) and 0 < len(key) <= 300 and isinstance(title, str) and 0 < len(title.strip()) <= 200):
            continue
        ok = isinstance(year, int) and not isinstance(year, bool) and 1900 <= year <= 2100
        out.append({"key": key, "title": title.strip(), "year": year if ok else None})
    return out


def detect(items):
    """Look the titles up in the sources that are switched on; save and return what was found."""
    st = lookup_settings()
    order = [n for n in SOURCES if st["sources"].get(n)]
    if not order:
        return {"results": [], "errors": ["No source is switched on."]}
    with LOCK:
        key = meta_get("tmdb_key")
    funcs = {"anilist": look_anilist, "tmdb": look_tmdb, "tvmaze": look_tvmaze}
    out, errors, stop = [], [], False
    for it in items:
        best = None  # (confident, score, kind, source, matched)
        for name in order:
            try:
                r = funcs[name](it["title"], it["year"], key)
            except RateLimited:
                errors.append(f"{name} is limiting requests. Wait a minute and try again.")
                stop = True
                break
            except (OSError, ValueError) as e:  # network trouble, bad answer, bad key (HTTP 401)
                errors.append(f"{name}: {e}")
                continue
            finally:
                time.sleep(GAP[name])
            if r:
                kind, matched, score, got = r
                cand = (score >= 0.88 and _year_ok(it["year"], got), score, kind, name, matched)
                if best is None or cand[:2] > best[:2]:
                    best = cand
                if cand[0]:
                    break
        if stop:
            break
        out.append({"key": it["key"], "kind": best[2] if best else "unknown", "source": best[3] if best else "",
                    "matched": best[4] if best else "", "score": round(best[1], 2) if best else 0,
                    "confident": bool(best and best[0])})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with tx():
        for r in out:
            DB.execute("INSERT OR REPLACE INTO lookups (key, kind, source, matched, score, confident, looked_up) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (r["key"], r["kind"], r["source"], r["matched"], r["score"], int(r["confident"]), now))
    return {"results": out, "errors": sorted(set(errors))}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "watch-history"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _hosts(self):
        port = self.server.port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        """Only answer requests addressed to us by name (blocks DNS-rebinding tricks)."""
        if self.headers.get("Host", "") not in self._hosts():
            self._send(403, {"error": "bad host"})
            return False
        return True

    def _write_ok(self):
        """Only our own page may change anything: matching Origin plus a header other sites cannot send."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc not in self._hosts():
            self._send(403, {"error": "bad origin"})
            return False
        if self.headers.get("X-WH") != "1":
            self._send(403, {"error": "missing header"})
            return False
        return True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= MAX_BODY:
                return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
        return None

    def do_GET(self):
        if not self._guard():
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                html = (HERE / "dashboard.html").read_bytes()
            except OSError:
                return self._send(500, {"error": "dashboard.html not found next to server.py"})
            return self._send(200, html, "text/html; charset=utf-8")
        try:
            if path == "/api/info":
                return self._send(200, {"server": True, "mode": "database", "db": str(DB_PATH), "edits": str(DB_PATH),
                                        "log": str(LOG), "log_exists": LOG.is_file()})
            if path == "/api/sessions":
                q = parse_qs(parts.query)
                try:
                    after = max(0, int(q.get("after", ["0"])[0]))
                except ValueError:
                    after = 0
                epoch = q.get("epoch", [""])[0] or None
                return self._send(200, sessions_since(after, epoch))
            if path == "/api/prefs":
                with LOCK:
                    look = meta_get("look")
                return self._send(200, {"look": look or None})
            if path == "/api/types":
                return self._send(200, lookup_settings())
            if path == "/api/edits":
                return self._send(200, load_edits())
        except (sqlite3.Error, OSError) as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path not in ("/api/sync", "/api/rebuild", "/api/types/detect", "/api/types/forget"):
            return self._send(404, {"error": "not found"})
        if not self._write_ok():
            return
        try:
            if path == "/api/types/forget":
                with tx():
                    DB.execute("DELETE FROM lookups")
                return self._send(200, lookup_settings())
            if path == "/api/types/detect":
                items = clean_items(self._read_json())
                if items is None:
                    return self._send(400, {"error": "invalid titles"})
                return self._send(200, detect(items))
            self._send(200, sync(rebuild=(path == "/api/rebuild")))
        except FileNotFoundError:
            self._send(404, {"error": f"log not found: {LOG}"})
        except (sqlite3.Error, OSError) as e:
            self._send(500, {"error": str(e)})

    def do_PUT(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path not in ("/api/edits", "/api/prefs", "/api/types/settings"):
            return self._send(404, {"error": "not found"})
        if not self._write_ok():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad size"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = None
        if path == "/api/prefs":  # the page's look: any id of letters, digits and dashes, so new looks need no server change
            look = body.get("look") if isinstance(body, dict) else None
            if not (isinstance(look, str) and re.fullmatch(r"[a-z0-9-]{1,32}", look)):
                return self._send(400, {"error": "invalid look"})
            try:
                with tx():
                    meta_set("look", look)
            except sqlite3.Error as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, {"ok": True})
        if path == "/api/types/settings":
            try:
                save_lookup_settings(body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except sqlite3.Error as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, lookup_settings())
        data = clean_edits(body)
        if data is None:
            return self._send(400, {"error": "invalid edits"})
        try:
            put_edits(data)
        except (sqlite3.Error, OSError) as e:
            return self._send(500, {"error": str(e)})
        self._send(200, {"ok": True})


def main():
    global LOG, DB_PATH
    ap = argparse.ArgumentParser(description="Local server for the watch-history dashboard.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--log", help="path to watch-history.jsonl (default: ~/.config/mpv/watch-history.jsonl)")
    ap.add_argument("--db", help="path to the database (default: history.db next to server.py)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = ap.parse_args()
    if args.log:
        LOG = Path(args.log).expanduser()
    if args.db:
        DB_PATH = Path(args.db).expanduser()
    if not (HERE / "dashboard.html").is_file():
        sys.exit("dashboard.html must be in the same folder as server.py")
    try:
        open_db()
    except (sqlite3.Error, OSError) as e:
        sys.exit(f"cannot open the database {DB_PATH}: {e}")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on port {args.port}: {e}")
    srv.port = args.port
    url = f"http://127.0.0.1:{args.port}"
    print(f"MPVloger (watch history) running at {url}")
    print(f"  log (read-only): {LOG}")
    print(f"  database:        {DB_PATH}")
    print("  stop with Ctrl+C")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
        DB.close()


if __name__ == "__main__":
    main()
