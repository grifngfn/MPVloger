#!/usr/bin/env python3
"""
watch-history server

  * serves dashboard.html (it must sit next to this file)
  * reads your mpv log, read-only:  ~/.config/mpv/watch-history.jsonl
  * stores your edits in:           ~/.local/share/watch-history/edits.json

Standard library only (no pip). Listens on 127.0.0.1 only. Stop it with Ctrl+C.

    python server.py            # then open http://127.0.0.1:8787
    python server.py --open     # also opens your browser
    python server.py --port 9000 --log /path/to/watch-history.jsonl
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ.get("WH_LOG", "~/.config/mpv/watch-history.jsonl")).expanduser()
DATA = Path(os.environ.get("WH_DATA", "~/.local/share/watch-history")).expanduser()
EDITS = DATA / "edits.json"
MAX_BODY = 1_000_000  # bytes

STATUSES = {"watching", "completed", "hold", "dropped", "plan"}
FIELDS = {  # allowed per-show fields and their types
    "title": str,
    "total": int,
    "status": str,
    "rating": (int, float),
    "poster": str,
    "notes": str,
}


def load_edits():
    try:
        data = json.loads(EDITS.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("shows"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "shows": {}}


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
            if field == "total" and v < 1:
                continue
            if field == "rating" and not 0 < v <= 10:
                continue
            item[field] = v
        if item:
            shows[key] = item
    return {"version": 1, "shows": shows}


def save_edits(data):
    """Atomic write: temp file, then rename. The previous version is kept as edits.json.bak."""
    DATA.mkdir(parents=True, exist_ok=True)
    if EDITS.exists():
        shutil.copy2(EDITS, DATA / "edits.json.bak")
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix="edits.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, EDITS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    def do_GET(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                html = (HERE / "dashboard.html").read_bytes()
            except OSError:
                return self._send(500, {"error": "dashboard.html not found next to server.py"})
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/info":
            return self._send(200, {"server": True, "log": str(LOG), "log_exists": LOG.is_file(), "edits": str(EDITS)})
        if path == "/api/log":
            try:
                data = LOG.read_bytes()
            except OSError:
                return self._send(404, {"error": f"log not found: {LOG}"})
            return self._send(200, data, "text/plain; charset=utf-8")
        if path == "/api/edits":
            return self._send(200, load_edits())
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        if not self._guard():
            return
        if urlsplit(self.path).path != "/api/edits":
            return self._send(404, {"error": "not found"})
        # Only our own page may save: matching Origin plus a custom header that other sites cannot send.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc not in self._hosts():
            return self._send(403, {"error": "bad origin"})
        if self.headers.get("X-WH") != "1":
            return self._send(403, {"error": "missing header"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad size"})
        try:
            data = clean_edits(json.loads(self.rfile.read(length).decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            data = None
        if data is None:
            return self._send(400, {"error": "invalid edits"})
        try:
            save_edits(data)
        except OSError as e:
            return self._send(500, {"error": str(e)})
        self._send(200, {"ok": True})


def main():
    global LOG
    ap = argparse.ArgumentParser(description="Local server for the watch-history dashboard.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--log", help="path to watch-history.jsonl (default: ~/.config/mpv/watch-history.jsonl)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = ap.parse_args()
    if args.log:
        LOG = Path(args.log).expanduser()
    if not (HERE / "dashboard.html").is_file():
        sys.exit("dashboard.html must be in the same folder as server.py")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on port {args.port}: {e}")
    srv.port = args.port
    url = f"http://127.0.0.1:{args.port}"
    print(f"watch-history running at {url}")
    print(f"  log (read-only): {LOG}")
    print(f"  edits:           {EDITS}")
    print("  stop with Ctrl+C")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
