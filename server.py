"""
"My Videos" - local server

Run in this folder:
    python server.py

On the computer open:  http://localhost:8000   (add videos and channels here)
Settings work in any browser; the Android app is view-only and finds this computer automatically on the same Wi-Fi.
Data is saved in data.json next to this file.
"""
import hashlib
import hmac
import http.server
import time
import json
import re
import os
import socket
import socketserver
import sys
import tempfile
import urllib.parse
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
APP_FILE = "index.html"
DATA_DIR = os.environ.get("DATA_DIR", HERE)
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# A token derived from the password: stays valid across restarts, changes when the password changes.
ADMIN_TOKEN = hmac.new(ADMIN_PASSWORD.encode("utf-8"), b"my-videos-admin", hashlib.sha256).hexdigest() if ADMIN_PASSWORD else ""
ALLOWED_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com"}
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",  # skip YouTube's cookie-consent page
}


# ---------- YouTube list reader (no API key needed) ----------
YT_CFG = {"key": "", "ver": "2.20250901.00.00"}


def yt_fetch(url, data=None):
    headers = dict(YT_HEADERS)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://www.youtube.com"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def yt_text(t):
    if not isinstance(t, dict):
        return ""
    if "simpleText" in t:
        return t["simpleText"]
    if "content" in t and isinstance(t["content"], str):
        return t["content"]
    return "".join(run.get("text", "") for run in t.get("runs", []))


def yt_walk(o, items, tokens):
    if isinstance(o, dict):
        r = o.get("playlistVideoRenderer")
        if isinstance(r, dict) and r.get("videoId") and r.get("isPlayable", True) is not False:
            items.append({"id": r["videoId"], "title": yt_text(r.get("title"))})
        lv = o.get("lockupViewModel")
        if isinstance(lv, dict) and lv.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and lv.get("contentId"):
            title = yt_text(((lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title"))
            items.append({"id": lv["contentId"], "title": title})
        cc = o.get("continuationCommand")
        if isinstance(cc, dict) and cc.get("token"):
            tokens.append(cc["token"])
        for v in o.values():
            if isinstance(v, (dict, list)):
                yt_walk(v, items, tokens)
    elif isinstance(o, list):
        for v in o:
            yt_walk(v, items, tokens)


def yt_initial_data(html):
    m = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        m = re.search(r"window\[\"ytInitialData\"\]\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        raise ValueError("YouTube page could not be read")
    return json.loads(m.group(1))


def yt_list_page(list_id, token=""):
    """One page of a playlist (a channel's uploads are the playlist UU...)."""
    if not re.fullmatch(r"[\w-]{10,64}", list_id or ""):
        raise ValueError("bad list id")
    items, tokens, title = [], [], ""
    if not token:
        html = yt_fetch("https://www.youtube.com/playlist?list=%s&hl=en&gl=US" % list_id)
        k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        v = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if k:
            YT_CFG["key"] = k.group(1)
        if v:
            YT_CFG["ver"] = v.group(1)
        data = yt_initial_data(html)
        title = (((data.get("metadata") or {}).get("playlistMetadataRenderer") or {}).get("title")) or ""
        alerts = json.dumps(data.get("alerts", []))
        yt_walk(data.get("contents", data), items, tokens)
        if not items and ("does not exist" in alerts or "unavailable" in alerts.lower()):
            raise ValueError("This list is private or does not exist")
    else:
        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        if YT_CFG["key"]:
            url += "&key=" + YT_CFG["key"]
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": YT_CFG["ver"], "hl": "en", "gl": "US"}},
                "continuation": token}
        data = json.loads(yt_fetch(url, body))
        yt_walk(data, items, tokens)
    seen, out = set(), []
    for it in items:
        if it["id"] not in seen and it["title"] not in ("[Private video]", "[Deleted video]"):
            seen.add(it["id"])
            out.append(it)
    nxt = tokens[-1] if tokens and tokens[-1] != token else ""
    return {"items": out, "next": nxt, "title": title}


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_data(obj):
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    try:
        # Safe way: write a temp file, then swap it in.
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, DATA_FILE)
    except (PermissionError, OSError):
        # The folder isn't writable (or data.json is a mounted file): write the file directly.
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            f.write(text)


class Handler(http.server.SimpleHTTPRequestHandler):
    def is_app(self):
        # The Android app adds "MyVideosApp" to its user agent: view-only there.
        return "MyVideosApp" in (self.headers.get("User-Agent") or "")

    def is_admin(self):
        if not ADMIN_PASSWORD:
            return True
        return hmac.compare_digest(self.headers.get("X-Admin-Token") or "", ADMIN_TOKEN)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise ValueError("bad size")
        obj = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("bad json")
        return obj

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.path = "/" + APP_FILE
            return super().do_GET()
        if parsed.path == "/ping":
            return self.json(200, {"app": "my-videos"})
        if parsed.path == "/api/state":
            data = load_data()
            admin = self.is_admin() and not self.is_app()
            if not admin:
                data = dict(data)
                data.pop("apiKey", None)   # the YouTube API key is only sent after login
            return self.json(200, {"canEdit": not self.is_app(), "needPassword": bool(ADMIN_PASSWORD),
                                   "authorized": admin, "data": data})
        if parsed.path == "/proxy":
            return self.proxy(parsed)
        if parsed.path == "/api/list":
            q = urllib.parse.parse_qs(parsed.query)
            try:
                return self.json(200, yt_list_page(q.get("list", [""])[0], q.get("token", [""])[0]))
            except Exception as e:
                sys.stderr.write("list failed: %s\n" % e)
                return self.json(502, {"error": str(e)})
        if parsed.path.endswith((".py", ".json")) and parsed.path != "/manifest.webmanifest":
            return self.reply(404, b"Not found")
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            try:
                pw = str(self.read_json().get("password", ""))
            except Exception:
                return self.json(400, {"error": "Bad request"})
            if not ADMIN_PASSWORD:
                return self.json(200, {"token": ""})
            if hmac.compare_digest(pw.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
                return self.json(200, {"token": ADMIN_TOKEN})
            time.sleep(1.5)  # slow down password guessing
            sys.stderr.write("wrong settings password from %s\n" % self.client_address[0])
            return self.json(401, {"error": "Wrong password"})
        if path != "/api/state":
            return self.reply(404, b"Not found")
        if self.is_app():
            return self.reply(403, b"Changes are not allowed from the Android app")
        if not self.is_admin():
            return self.json(401, {"error": "Password required"})
        try:
            obj = self.read_json()
        except Exception:
            return self.reply(400, b"Bad JSON")
        try:
            save_data(obj)
        except Exception as e:
            sys.stderr.write("could not save %s: %s\n" % (DATA_FILE, e))
            return self.json(500, {"error": "Could not save data.json: %s" % e})
        print("saved %s  (%d videos, %d channels/playlists)" % (
            DATA_FILE, len(obj.get("videos") or []), len(obj.get("channels") or [])))
        return self.json(200, {"ok": True})

    def proxy(self, parsed):
        target = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        t = urllib.parse.urlparse(target)
        if t.scheme != "https" or t.hostname not in ALLOWED_HOSTS:
            return self.reply(400, b"Only youtube.com pages are allowed")
        try:
            req = urllib.request.Request(target, headers=YT_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            self.reply(200, body, "text/html; charset=utf-8")
        except Exception as e:
            sys.stderr.write("youtube lookup failed: %s -> %s\n" % (target, e))
            self.reply(502, ("Fetch failed: %s" % e).encode("utf-8"))

    def json(self, code, obj):
        self.reply(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def reply(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(HERE)
    if not os.path.exists(APP_FILE):
        print("index.html was not found next to server.py")
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print("Data file: %s" % DATA_FILE)
        print("Settings password: %s" % ("ON (ADMIN_PASSWORD)" if ADMIN_PASSWORD else "OFF - set ADMIN_PASSWORD to protect Settings"))
        print("On this computer:  http://localhost:%d" % PORT)
        if os.environ.get("IN_DOCKER"):
            print("Running in Docker: on the phone use http://<this computer's LAN IP>:%d" % PORT)
        else:
            for ip in lan_ips():
                print("On the phone (same Wi-Fi):  http://%s:%d" % (ip, PORT))
        print("Keep this window open. Ctrl+C to stop.")
        sys.stdout.flush()
        httpd.serve_forever()
