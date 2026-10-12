"""
"My Videos" - server

Run:          python server.py            (or with Docker: docker compose up -d)
Open:         http://localhost:8000
Data:         myvideos.db (SQLite) in DATA_DIR (default: next to this file)

First run:    open the site and complete "First-time setup" with ADMIN_PASSWORD from .env,
              then choose your own admin password (stored hashed in the database).
Forgot the admin password:
              python server.py --reset-admin <admin phone>
              (then unlock once more with ADMIN_PASSWORD and choose a new one)
List users:   python server.py --list-users
"""
import glob
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CLI = len(sys.argv) > 1 and sys.argv[1].startswith("--")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and not CLI else int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
APP_FILE = "index.html"
DATA_DIR = os.environ.get("DATA_DIR", HERE)
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "myvideos.db")
LEGACY_JSON = os.path.join(DATA_DIR, "data.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

COOKIE = "mv_session"
SESSION_SECONDS = 365 * 24 * 3600       # stay signed in for a year (APK, TV)
UNLOCK_SECONDS = 2 * 3600               # Settings stay unlocked for 2 hours of inactivity
MAX_FAILS = 5                           # wrong PIN / password before the account is locked
LOCK_SECONDS = 15 * 60
IP_FAILS = {"login": 10, "secret": 10, "setup": 5}   # wrong tries per IP per minute, by kind
IP_WINDOW = 60
MIN_ADMIN_PW = 8

# Static files the server may hand out. Nothing else on disk is reachable (database, .env, code).
STATIC = {"/index.html": "text/html; charset=utf-8", "/sw.js": "text/javascript; charset=utf-8",
          "/manifest.webmanifest": "application/manifest+json"}
ICON_RE = re.compile(r"^/icons/[\w-]+\.png$")

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


# ---------- Database ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','user')),
  secret_hash TEXT,
  must_change INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  fail_count INTEGER NOT NULL DEFAULT 0,
  locked_until REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_app INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  last_seen REAL NOT NULL,
  unlocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS videos(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  vkey TEXT NOT NULL,
  yt_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  min_age INTEGER,
  max_age INTEGER,
  channel_id TEXT NOT NULL DEFAULT '',
  channel_title TEXT NOT NULL DEFAULT '',
  author_url TEXT NOT NULL DEFAULT '',
  flags INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, yt_id)
);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('channel','playlist')),
  yt_id TEXT NOT NULL,
  uploads TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  thumb TEXT NOT NULL DEFAULT '',
  min_age INTEGER,
  max_age INTEGER,
  auto_title INTEGER NOT NULL DEFAULT 0,
  scraped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, yt_id)
);
CREATE TABLE IF NOT EXISTS logos(channel_id TEXT PRIMARY KEY, url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS author_map(url TEXT PRIMARY KEY, channel_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS videos_user ON videos(user_id, pos);
CREATE INDEX IF NOT EXISTS sources_user ON sources(user_id, pos);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
"""

DB_LOCK = threading.RLock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        c = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            c.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        c.executescript(SCHEMA)
        _conn = c
    return _conn


class tx:
    """with tx() as c: ...  -> one transaction, serialised across threads."""
    def __enter__(self):
        DB_LOCK.acquire()
        self.c = conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, ev, tb):
        try:
            self.c.execute("ROLLBACK" if et else "COMMIT")
        finally:
            DB_LOCK.release()
        return False


def q(sql, args=()):
    with DB_LOCK:
        return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def now():
    return time.time()


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Secrets ----------
def hash_secret(secret):
    salt = secrets.token_bytes(16)
    it = 200000
    h = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, it)
    return "pbkdf2_sha256$%d$%s$%s" % (it, salt.hex(), h.hex())


def check_secret(secret, stored):
    try:
        alg, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- Phones ----------
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm_digits(s):
    return re.sub(r"[^0-9]", "", str(s or "").translate(DIGITS))


def norm_phone(s):
    """Digits only, no leading zeros (so +98 912…, 0098 912… and 0912… compare well)."""
    d = norm_digits(s).lstrip("0")
    return d if 7 <= len(d) <= 15 else ""


def phones_match(a, b):
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and long_.endswith(short)


def find_user_by_phone(p):
    rows = q("SELECT * FROM users")
    exact = [r for r in rows if r["phone"] == p]
    if exact:
        return exact[0]
    near = [r for r in rows if phones_match(r["phone"], p)]
    return near[0] if len(near) == 1 else None


def phone_taken(p, exclude_id=None):
    for r in q("SELECT id, phone FROM users"):
        if r["id"] != exclude_id and phones_match(r["phone"], p):
            return True
    return False


# ---------- Wrong-try limits ----------
_ip_fails = {}
_ip_lock = threading.Lock()


def ip_blocked(ip, kind):
    with _ip_lock:
        t = [x for x in _ip_fails.get((ip, kind), []) if x > now() - IP_WINDOW]
        _ip_fails[(ip, kind)] = t
        return len(t) >= IP_FAILS[kind]


def note_ip_fail(ip, kind):
    with _ip_lock:
        _ip_fails.setdefault((ip, kind), []).append(now())


# ---------- Validation of a user's lists ----------
YT_ID = re.compile(r"^[\w-]{11}$")
CH_ID = re.compile(r"^UC[\w-]{22}$")
LIST_ID = re.compile(r"^[\w-]{10,64}$")


def s_(v, n):
    return v.strip()[:n] if isinstance(v, str) else ""


def age_(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v if 0 <= v <= 120 else None


def url_(v):
    v = s_(v, 1000)
    return v if v.startswith("https://") else ""


def clean_lists(obj):
    videos, seen = [], set()
    for v in (obj.get("videos") or [])[:5000]:
        if not isinstance(v, dict):
            continue
        yid = s_(v.get("id"), 11)
        if not YT_ID.match(yid) or yid in seen:
            continue
        seen.add(yid)
        ch = s_(v.get("channelId"), 24)
        videos.append({
            "key": s_(v.get("key"), 100) or yid, "id": yid, "title": s_(v.get("title"), 300),
            "min": age_(v.get("min")), "max": age_(v.get("max")),
            "channelId": ch if CH_ID.match(ch) else "", "channelTitle": s_(v.get("channelTitle"), 200),
            "authorUrl": url_(v.get("authorUrl")),
            "flags": (1 if v.get("m1") else 0) | (2 if v.get("m2") else 0) | (4 if v.get("m3") else 0)})
    sources, seen = [], set()
    for c in (obj.get("channels") or [])[:500]:
        if not isinstance(c, dict):
            continue
        kind = "playlist" if c.get("kind") == "playlist" else "channel"
        cid = s_(c.get("id"), 64)
        if (kind == "channel" and not CH_ID.match(cid)) or (kind == "playlist" and not LIST_ID.match(cid)) or cid in seen:
            continue
        seen.add(cid)
        up = s_(c.get("uploads"), 64)
        if not LIST_ID.match(up):
            up = cid if kind == "playlist" else "UU" + cid[2:]
        sources.append({
            "kind": kind, "id": cid, "uploads": up, "title": s_(c.get("title"), 200), "owner": s_(c.get("owner"), 200),
            "thumb": url_(c.get("thumb")), "min": age_(c.get("min")), "max": age_(c.get("max")),
            "autoTitle": 1 if c.get("autoTitle") else 0, "scraped": 1 if c.get("scraped") else 0})
    logos = {}
    lg = obj.get("logos") if isinstance(obj.get("logos"), dict) else {}
    for k, v in list(lg.items())[:3000]:
        if isinstance(k, str) and CH_ID.match(k) and url_(v):
            logos[k] = url_(v)
    amap = {}
    am = obj.get("authorMap") if isinstance(obj.get("authorMap"), dict) else {}
    for k, v in list(am.items())[:3000]:
        if isinstance(k, str) and k.startswith("https://") and len(k) <= 300 and isinstance(v, str) and (v == "" or CH_ID.match(v)):
            amap[k] = v
    return videos, sources, logos, amap


def save_lists(c, user_id, videos, sources, logos, amap):
    created = {r["yt_id"]: r["created_at"] for r in c.execute(
        "SELECT yt_id, created_at FROM videos WHERE user_id=? UNION ALL SELECT yt_id, created_at FROM sources WHERE user_id=?",
        (user_id, user_id))}
    c.execute("DELETE FROM videos WHERE user_id=?", (user_id,))
    c.execute("DELETE FROM sources WHERE user_id=?", (user_id,))
    stamp = iso_now()
    for i, v in enumerate(videos):
        c.execute("INSERT INTO videos(user_id,pos,vkey,yt_id,title,min_age,max_age,channel_id,channel_title,author_url,flags,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (user_id, i, v["key"], v["id"], v["title"], v["min"], v["max"], v["channelId"], v["channelTitle"],
                   v["authorUrl"], v["flags"], created.get(v["id"], stamp)))
    for i, s in enumerate(sources):
        c.execute("INSERT INTO sources(user_id,pos,kind,yt_id,uploads,title,owner,thumb,min_age,max_age,auto_title,scraped,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (user_id, i, s["kind"], s["id"], s["uploads"], s["title"], s["owner"], s["thumb"], s["min"], s["max"],
                   s["autoTitle"], s["scraped"], created.get(s["id"], stamp)))
    for k, v in logos.items():
        c.execute("INSERT INTO logos(channel_id,url) VALUES(?,?) ON CONFLICT(channel_id) DO UPDATE SET url=excluded.url", (k, v))
    for k, v in amap.items():
        c.execute("INSERT INTO author_map(url,channel_id) VALUES(?,?) ON CONFLICT(url) DO UPDATE SET channel_id=excluded.channel_id", (k, v))


def load_lists(user_id):
    videos = []
    for r in q("SELECT * FROM videos WHERE user_id=? ORDER BY pos", (user_id,)):
        v = {"key": r["vkey"], "id": r["yt_id"], "title": r["title"], "min": r["min_age"], "max": r["max_age"]}
        if r["channel_id"]:
            v["channelId"] = r["channel_id"]
        if r["channel_title"]:
            v["channelTitle"] = r["channel_title"]
        if r["author_url"]:
            v["authorUrl"] = r["author_url"]
        for bit, name in ((1, "m1"), (2, "m2"), (4, "m3")):
            if r["flags"] & bit:
                v[name] = 1
        videos.append(v)
    channels = []
    for r in q("SELECT * FROM sources WHERE user_id=? ORDER BY pos", (user_id,)):
        c = {"id": r["yt_id"], "uploads": r["uploads"], "title": r["title"], "thumb": r["thumb"],
             "min": r["min_age"], "max": r["max_age"]}
        if r["kind"] == "playlist":
            c["kind"] = "playlist"
            c["owner"] = r["owner"]
        if r["auto_title"]:
            c["autoTitle"] = True
        if r["scraped"]:
            c["scraped"] = 1
        channels.append(c)
    # Only the logos / author links this user's own lists refer to (nothing about other users' lists).
    ids = {v.get("channelId") for v in videos if v.get("channelId")} | {c["id"] for c in channels if "kind" not in c}
    urls = {v.get("authorUrl") for v in videos if v.get("authorUrl")}
    logos, amap = {}, {}
    if ids:
        for r in q("SELECT * FROM logos WHERE channel_id IN (%s)" % ",".join("?" * len(ids)), tuple(ids)):
            logos[r["channel_id"]] = r["url"]
    if urls:
        for r in q("SELECT * FROM author_map WHERE url IN (%s)" % ",".join("?" * len(urls)), tuple(urls)):
            amap[r["url"]] = r["channel_id"]
    return {"videos": videos, "channels": channels, "logos": logos, "authorMap": amap}


# ---------- Users ----------
def has_admin():
    return q1("SELECT 1 FROM users WHERE role='admin' AND active=1") is not None


def active_admin_count(exclude_id=None):
    r = q1("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND id<>?", (exclude_id or 0,))
    return r["n"]


def user_public(u):
    return {"id": u["id"], "name": u["name"], "phone": u["phone"], "role": u["role"], "active": bool(u["active"]),
            "hasSecret": bool(u["secret_hash"]), "createdAt": u["created_at"]}


def legacy_json_info():
    try:
        st = os.stat(LEGACY_JSON)
    except OSError:
        return None
    sig = "%d:%d" % (st.st_size, int(st.st_mtime))
    done = q1("SELECT value FROM meta WHERE key='legacy_import'")
    if done and done["value"] == sig:
        return None
    try:
        with open(LEGACY_JSON, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    return {"sig": sig, "data": d, "videos": len(d.get("videos") or []), "channels": len(d.get("channels") or [])}


def find_apk():
    """The Android app to hand out at /apk: my-videos.apk next to docker-compose.yml (or this file), else the newest *.apk."""
    for d in (DATA_DIR, HERE):
        p = os.path.join(d, "my-videos.apk")
        if os.path.isfile(p):
            return p
    found = [p for d in (DATA_DIR, HERE) for p in glob.glob(os.path.join(d, "*.apk")) if os.path.isfile(p)]
    return max(found, key=os.path.getmtime) if found else None


def lock_message(u):
    mins = max(1, int((u["locked_until"] - now() + 59) // 60))
    return "Too many wrong tries. Try again in %d minute%s." % (mins, "" if mins == 1 else "s")



# ---------- HTTP ----------
class ApiError(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code, self.msg = code, msg


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "MyVideos"
    sys_version = ""

    # --- helpers ---
    def ip(self):
        return self.client_address[0]

    def is_app(self):
        # The Android app adds "MyVideosApp" to its user agent: watch-only there.
        return "MyVideosApp" in (self.headers.get("User-Agent") or "")

    def cookie_token(self):
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
            return c[COOKIE].value if COOKIE in c else ""
        except http.cookies.CookieError:
            return ""

    def session(self):
        """The signed-in session joined with its user, or None."""
        if hasattr(self, "_sess"):
            return self._sess
        self._sess = None
        tok = self.cookie_token()
        if tok:
            r = q1("SELECT s.token_hash, s.is_app, s.last_seen, s.unlocked_until, u.* FROM sessions s"
                   " JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (token_hash(tok),))
            if r and r["active"] and r["last_seen"] > now() - SESSION_SECONDS:
                self._sess = r
                if r["last_seen"] < now() - 3600:
                    with tx() as c:
                        c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now(), r["token_hash"]))
        return self._sess

    def unlocked(self, s):
        return bool(s) and not self.is_app() and s["unlocked_until"] > now()

    def need_session(self):
        s = self.session()
        if not s:
            raise ApiError(401, "Please sign in with your mobile number.")
        return s

    def need_unlocked(self):
        s = self.need_session()
        if self.is_app():
            raise ApiError(403, "Settings are not available in the Android app.")
        if not self.unlocked(s):
            raise ApiError(401, "Settings are locked. Enter your PIN or password.")
        if s["must_change"]:
            raise ApiError(403, "Choose a new password first.")
        with tx() as c:   # sliding unlock window
            c.execute("UPDATE sessions SET unlocked_until=? WHERE token_hash=?", (now() + UNLOCK_SECONDS, s["token_hash"]))
        return s

    def need_admin(self):
        s = self.need_unlocked()
        if s["role"] != "admin":
            raise ApiError(403, "Only an admin can do this.")
        return s

    def target_user(self, s, query):
        """Whose lists: your own, or (admin only) ?user=<id>."""
        uid = (query.get("user") or [""])[0]
        if not uid or str(uid) == str(s["id"]):
            return s["id"]
        if s["role"] != "admin" or not self.unlocked(s) or s["must_change"]:
            raise ApiError(403, "You can only see your own videos.")
        r = q1("SELECT id FROM users WHERE id=?", (uid,))
        if not r:
            raise ApiError(404, "User not found.")
        return r["id"]

    def read_json(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "Expected JSON.")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise ApiError(400, "Bad request size.")
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise ApiError(400, "Bad JSON.")
        if not isinstance(obj, dict):
            raise ApiError(400, "Bad JSON.")
        return obj

    def me(self, s=None, extra_cookie=None):
        s = s if s is not None else self.session()
        out = {"app": self.is_app(), "needSetup": not has_admin(), "setupEnabled": bool(ADMIN_PASSWORD), "loggedIn": bool(s),
               "apk": bool(find_apk())}
        if s:
            out.update({"user": {"id": s["id"], "name": s["name"], "role": s["role"], "phone": s["phone"],
                                 "hasSecret": bool(s["secret_hash"]) or (s["role"] == "admin" and bool(ADMIN_PASSWORD))},
                        "unlocked": self.unlocked(s), "mustChange": bool(s["must_change"]) and self.unlocked(s),
                        "canEdit": not self.is_app()})
        return out

    def start_session(self, user_id, unlocked=False):
        tok = secrets.token_urlsafe(32)
        with tx() as c:
            c.execute("INSERT INTO sessions(token_hash,user_id,is_app,created_at,last_seen,unlocked_until) VALUES(?,?,?,?,?,?)",
                      (token_hash(tok), user_id, 1 if self.is_app() else 0, now(), now(), now() + UNLOCK_SECONDS if unlocked else 0))
            c.execute("DELETE FROM sessions WHERE last_seen < ?", (now() - SESSION_SECONDS,))
        self._set_cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (COOKIE, tok, SESSION_SECONDS)
        self._sess = q1("SELECT s.token_hash, s.is_app, s.last_seen, s.unlocked_until, u.* FROM sessions s"
                        " JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (token_hash(tok),))

    # --- output ---
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        super().end_headers()

    def json(self, code, obj):
        self.reply(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def reply(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def log_event(self, text):
        print("%s  %s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.ip(), text))
        sys.stdout.flush()

    # --- routing ---
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if method == "GET" and path.rstrip("/") in ("/apk", "/my-videos.apk"):
                return self.send_apk()
            if method == "GET" and not path.startswith("/api/") and path not in ("/ping", "/proxy"):
                return self.static(path)
            if path == "/ping" and method == "GET":
                return self.reply(200, b'{"app": "my-videos"}', "application/json", {"Access-Control-Allow-Origin": "*"})
            fn = ROUTES.get((method, path))
            m = re.match(r"^/api/admin/users/(\d+)(/delete)?$", path)
            if not fn and m and method == "POST":
                return self.api_user_update(int(m.group(1))) if not m.group(2) else self.api_user_delete(int(m.group(1)))
            if not fn:
                raise ApiError(404, "Not found.")
            return fn(self, query)
        except ApiError as e:
            return self.json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:  # never leak a traceback to the browser
            sys.stderr.write("error on %s %s: %r\n" % (method, path, e))
            return self.json(500, {"error": "Server error."})

    def send_apk(self):
        p = find_apk()
        if not p:
            return self.reply(404, ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                                    "<title>My Videos</title><body style='font-family:sans-serif;padding:24px'>"
                                    "<h2>The Android app is not on the server yet</h2>"
                                    "<p>Put <b>my-videos.apk</b> next to docker-compose.yml (or server.py) and open this page again.</p>").encode("utf-8"),
                              "text/html; charset=utf-8")
        with open(p, "rb") as f:
            body = f.read()
        self.log_event("APK downloaded")
        return self.reply(200, body, "application/vnd.android.package-archive",
                          {"Content-Disposition": 'attachment; filename="my-videos.apk"'})

    def static(self, path):
        if path == "/":
            path = "/index.html"
        ctype = STATIC.get(path) or ("image/png" if ICON_RE.match(path) else None)
        if not ctype:
            raise ApiError(404, "Not found.")
        try:
            with open(os.path.join(HERE, path.lstrip("/")), "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError(404, "Not found.")
        return self.reply(200, body, ctype)

    # --- account ---
    def api_me(self, query):
        return self.json(200, self.me())

    def api_login(self, query):
        if ip_blocked(self.ip(), "login"):
            raise ApiError(429, "Too many tries. Wait a minute and try again.")
        body = self.read_json()
        p = norm_phone(body.get("phone"))
        u = find_user_by_phone(p) if p else None
        if not u or not u["active"]:
            note_ip_fail(self.ip(), "login")
            raise ApiError(401, "This number is not registered.")
        self.start_session(u["id"])
        self.log_event("signed in: %s (%s)%s" % (u["name"], u["role"], " [app]" if self.is_app() else ""))
        return self.json(200, self.me())

    def api_logout(self, query):
        tok = self.cookie_token()
        if tok:
            with tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))
        self._set_cookie = "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE
        return self.json(200, {"ok": True})

    def api_setup(self, query):
        if self.is_app():
            raise ApiError(403, "Do the first-time setup in a web browser.")
        if has_admin():
            raise ApiError(409, "Setup is already done. Sign in with your mobile number.")
        if not ADMIN_PASSWORD:
            raise ApiError(403, "Set ADMIN_PASSWORD in the .env file and restart the server first.")
        if ip_blocked(self.ip(), "setup"):
            raise ApiError(429, "Too many tries. Wait a minute and try again.")
        body = self.read_json()
        if not hmac.compare_digest(str(body.get("password", "")).encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
            note_ip_fail(self.ip(), "setup")
            self.log_event("first-time setup: wrong ADMIN_PASSWORD")
            raise ApiError(401, "Wrong setup password (ADMIN_PASSWORD).")
        name, p = s_(body.get("name"), 60), norm_phone(body.get("phone"))
        if not name:
            raise ApiError(400, "Enter your name.")
        if not p:
            raise ApiError(400, "Enter a valid mobile number.")
        with tx() as c:
            if c.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
                raise ApiError(409, "Setup is already done.")
            old = c.execute("SELECT id FROM users WHERE phone=?", (p,)).fetchone()
            if old:
                c.execute("UPDATE users SET role='admin', name=?, secret_hash=NULL, must_change=1, active=1 WHERE id=?", (name, old["id"]))
                uid = old["id"]
            else:
                uid = c.execute("INSERT INTO users(phone,name,role,secret_hash,must_change,active,created_at) VALUES(?,?,'admin',NULL,1,1,?)",
                                (p, name, iso_now())).lastrowid
        self.start_session(uid, unlocked=True)
        self.log_event("first-time setup: admin %s created" % name)
        return self.json(200, self.me())

    def api_unlock(self, query):
        s = self.need_session()
        if self.is_app():
            raise ApiError(403, "Settings are not available in the Android app.")
        if s["locked_until"] > now():
            raise ApiError(429, lock_message(s))
        if ip_blocked(self.ip(), "secret"):
            raise ApiError(429, "Too many tries. Wait a minute and try again.")
        secret = str(self.read_json().get("secret", ""))
        if s["role"] == "admin":
            if s["secret_hash"]:
                ok = check_secret(secret, s["secret_hash"])
            else:  # first unlock (or after --reset-admin): the .env password, then a new one is required
                ok = bool(ADMIN_PASSWORD) and hmac.compare_digest(secret.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
        else:
            if not s["secret_hash"]:
                raise ApiError(403, "No PIN is set for you yet. Ask the admin.")
            ok = check_secret(norm_digits(secret), s["secret_hash"])
        what = "password" if s["role"] == "admin" else "PIN"
        with tx() as c:
            if ok:
                c.execute("UPDATE users SET fail_count=0, locked_until=0 WHERE id=?", (s["id"],))
                c.execute("UPDATE sessions SET unlocked_until=? WHERE token_hash=?", (now() + UNLOCK_SECONDS, s["token_hash"]))
            else:
                n = s["fail_count"] + 1
                if n >= MAX_FAILS:
                    c.execute("UPDATE users SET fail_count=0, locked_until=? WHERE id=?", (now() + LOCK_SECONDS, s["id"]))
                else:
                    c.execute("UPDATE users SET fail_count=? WHERE id=?", (n, s["id"]))
        if not ok:
            note_ip_fail(self.ip(), "secret")
            self.log_event("wrong %s for %s" % (what, s["name"]))
            left = MAX_FAILS - (s["fail_count"] + 1)
            if left <= 0:
                raise ApiError(429, "Too many wrong tries. Settings are locked for %d minutes." % (LOCK_SECONDS // 60))
            raise ApiError(401, "Wrong %s. %d tr%s left." % (what, left, "y" if left == 1 else "ies"))
        if hasattr(self, "_sess"):
            del self._sess
        return self.json(200, self.me())

    def api_lock(self, query):
        s = self.need_session()
        with tx() as c:
            c.execute("UPDATE sessions SET unlocked_until=0 WHERE token_hash=?", (s["token_hash"],))
        if hasattr(self, "_sess"):
            del self._sess
        return self.json(200, self.me())

    def api_password(self, query):
        s = self.need_session()
        if self.is_app() or not self.unlocked(s):
            raise ApiError(401, "Settings are locked.")
        if s["role"] != "admin":
            raise ApiError(403, "Only an admin has a password. Ask the admin to change your PIN.")
        pw = str(self.read_json().get("newPassword", ""))
        if len(pw) < MIN_ADMIN_PW or len(pw) > 200:
            raise ApiError(400, "Use at least %d characters." % MIN_ADMIN_PW)
        if ADMIN_PASSWORD and pw == ADMIN_PASSWORD:
            raise ApiError(400, "Choose a password different from ADMIN_PASSWORD in .env.")
        with tx() as c:
            c.execute("UPDATE users SET secret_hash=?, must_change=0 WHERE id=?", (hash_secret(pw), s["id"]))
            # other devices of this admin must unlock again with the new password
            c.execute("UPDATE sessions SET unlocked_until=0 WHERE user_id=? AND token_hash<>?", (s["id"], s["token_hash"]))
        self.log_event("password changed: %s" % s["name"])
        if hasattr(self, "_sess"):
            del self._sess
        return self.json(200, self.me())

    # --- lists ---
    def api_state_get(self, query):
        s = self.need_session()
        uid = self.target_user(s, query)
        return self.json(200, {"data": load_lists(uid), "userId": uid})

    def api_state_post(self, query):
        s = self.need_unlocked()
        uid = self.target_user(s, query)
        videos, sources, logos, amap = clean_lists(self.read_json())
        with tx() as c:
            save_lists(c, uid, videos, sources, logos, amap)
        who = "" if uid == s["id"] else " for user #%d" % uid
        self.log_event("saved %d videos, %d channels/playlists%s (by %s)" % (len(videos), len(sources), who, s["name"]))
        return self.json(200, {"ok": True, "videos": len(videos), "channels": len(sources)})

    def api_list(self, query):
        self.need_session()
        try:
            return self.json(200, yt_list_page(query.get("list", [""])[0], query.get("token", [""])[0]))
        except Exception as e:
            sys.stderr.write("list failed: %s\n" % e)
            return self.json(502, {"error": str(e)})

    def api_proxy(self, query):
        self.need_session()
        target = query.get("url", [""])[0]
        t = urllib.parse.urlparse(target)
        if t.scheme != "https" or t.hostname not in ALLOWED_HOSTS:
            raise ApiError(400, "Only youtube.com pages are allowed.")
        try:
            req = urllib.request.Request(target, headers=YT_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            return self.reply(200, body, "text/html; charset=utf-8")
        except Exception as e:
            sys.stderr.write("youtube lookup failed: %s -> %s\n" % (target, e))
            return self.reply(502, ("Fetch failed: %s" % e).encode("utf-8"))

    # --- admin: users ---
    def api_users(self, query):
        self.need_admin()
        users = []
        for u in q("SELECT u.*, (SELECT COUNT(*) FROM videos v WHERE v.user_id=u.id) AS nv,"
                   " (SELECT COUNT(*) FROM sources c WHERE c.user_id=u.id) AS nc FROM users u ORDER BY u.role, u.name COLLATE NOCASE"):
            d = user_public(u)
            d.update({"videos": u["nv"], "channels": u["nc"], "locked": u["locked_until"] > now()})
            users.append(d)
        info = legacy_json_info()
        return self.json(200, {"users": users, "import": {"videos": info["videos"], "channels": info["channels"]} if info else None})

    def _secret_for(self, role, raw, required):
        raw = "" if raw is None else str(raw)
        if role == "user":
            pin = norm_digits(raw)
            if not raw.strip():
                return None
            if not re.fullmatch(r"\d{4}", pin):
                raise ApiError(400, "The PIN must be exactly 4 digits.")
            return hash_secret(pin)
        if not raw:
            if required:
                raise ApiError(400, "An admin needs a temporary password (at least %d characters)." % MIN_ADMIN_PW)
            return None
        if len(raw) < MIN_ADMIN_PW or len(raw) > 200:
            raise ApiError(400, "The password needs at least %d characters." % MIN_ADMIN_PW)
        return hash_secret(raw)

    def api_user_create(self, query):
        s = self.need_admin()
        b = self.read_json()
        name, p = s_(b.get("name"), 60), norm_phone(b.get("phone"))
        role = "admin" if b.get("role") == "admin" else "user"
        if not name:
            raise ApiError(400, "Enter a name.")
        if not p:
            raise ApiError(400, "Enter a valid mobile number.")
        if phone_taken(p):
            raise ApiError(409, "This mobile number is already registered.")
        h = self._secret_for(role, b.get("secret"), required=(role == "admin"))
        with tx() as c:
            uid = c.execute("INSERT INTO users(phone,name,role,secret_hash,must_change,active,created_at) VALUES(?,?,?,?,?,?,?)",
                            (p, name, role, h, 1 if role == "admin" else 0, 0 if b.get("active") is False else 1, iso_now())).lastrowid
        self.log_event("user added: %s (%s) by %s" % (name, role, s["name"]))
        return self.json(200, {"ok": True, "id": uid})

    def api_user_update(self, uid):
        s = self.need_admin()
        b = self.read_json()
        u = q1("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise ApiError(404, "User not found.")
        name = s_(b.get("name"), 60) if "name" in b else u["name"]
        p = norm_phone(b.get("phone")) if "phone" in b else u["phone"]
        role = ("admin" if b.get("role") == "admin" else "user") if "role" in b else u["role"]
        active = (0 if b.get("active") is False else 1) if "active" in b else u["active"]
        if not name:
            raise ApiError(400, "Enter a name.")
        if not p:
            raise ApiError(400, "Enter a valid mobile number.")
        if p != u["phone"] and phone_taken(p, exclude_id=uid):
            raise ApiError(409, "This mobile number is already registered.")
        self_edit = uid == s["id"]
        if self_edit and (role != u["role"] or not active):
            raise ApiError(400, "You can't change your own role or deactivate yourself.")
        if u["role"] == "admin" and u["active"] and (role != "admin" or not active) and active_admin_count(uid) == 0:
            raise ApiError(400, "There must be at least one active admin.")
        secret_raw = b.get("secret")
        role_changed = role != u["role"]
        h, must, set_secret = u["secret_hash"], u["must_change"], False
        if self_edit and secret_raw:
            raise ApiError(400, "Change your own password with \"Change my password\".")
        if secret_raw or (role_changed and role == "admin"):
            h = self._secret_for(role, secret_raw, required=True)
            must, set_secret = (1 if role == "admin" else 0), True
        elif role_changed:          # admin -> user: the old password is not a PIN
            h, must, set_secret = None, 0, True
        elif b.get("clearSecret") and role == "user":
            h, must, set_secret = None, 0, True
        with tx() as c:
            c.execute("UPDATE users SET name=?, phone=?, role=?, active=?, secret_hash=?, must_change=?"
                      + (", fail_count=0, locked_until=0" if set_secret else "") + " WHERE id=?",
                      (name, p, role, active, h, must, uid))
            if not active:
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            elif set_secret or role_changed:
                c.execute("UPDATE sessions SET unlocked_until=0 WHERE user_id=?", (uid,))
        self.log_event("user updated: %s by %s" % (name, s["name"]))
        return self.json(200, {"ok": True})

    def api_user_delete(self, uid):
        s = self.need_admin()
        u = q1("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise ApiError(404, "User not found.")
        if uid == s["id"]:
            raise ApiError(400, "You can't delete yourself.")
        if u["role"] == "admin" and u["active"] and active_admin_count(uid) == 0:
            raise ApiError(400, "There must be at least one active admin.")
        with tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (uid,))
        self.log_event("user deleted: %s by %s" % (u["name"], s["name"]))
        return self.json(200, {"ok": True})

    def api_import(self, query):
        s = self.need_admin()
        uid = self.read_json().get("userId")
        u = q1("SELECT * FROM users WHERE id=?", (uid,)) if isinstance(uid, int) else None
        if not u:
            raise ApiError(404, "Choose a user.")
        info = legacy_json_info()
        if not info:
            raise ApiError(404, "There is no data.json to import.")
        cur = load_lists(u["id"])
        nv, ns, logos, amap = clean_lists(info["data"])
        have_v = {v["id"] for v in cur["videos"]}
        have_s = {c["id"] for c in cur["channels"]}
        ov, os_, _, _ = clean_lists(cur)
        add_v = [v for v in nv if v["id"] not in have_v]
        add_s = [c for c in ns if c["id"] not in have_s]
        with tx() as c:
            save_lists(c, u["id"], ov + add_v, os_ + add_s, logos, amap)
            c.execute("INSERT INTO meta(key,value) VALUES('legacy_import',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (info["sig"],))
        try:
            os.replace(LEGACY_JSON, LEGACY_JSON + ".imported")
        except OSError:
            pass  # marked as imported in the database anyway
        self.log_event("imported data.json into %s: %d videos, %d channels/playlists" % (u["name"], len(add_v), len(add_s)))
        return self.json(200, {"ok": True, "videos": len(add_v), "channels": len(add_s)})


ROUTES = {
    ("GET", "/api/me"): Handler.api_me,
    ("POST", "/api/login"): Handler.api_login,
    ("POST", "/api/logout"): Handler.api_logout,
    ("POST", "/api/setup"): Handler.api_setup,
    ("POST", "/api/unlock"): Handler.api_unlock,
    ("POST", "/api/lock"): Handler.api_lock,
    ("POST", "/api/password"): Handler.api_password,
    ("GET", "/api/state"): Handler.api_state_get,
    ("POST", "/api/state"): Handler.api_state_post,
    ("GET", "/api/list"): Handler.api_list,
    ("GET", "/proxy"): Handler.api_proxy,
    ("GET", "/api/admin/users"): Handler.api_users,
    ("POST", "/api/admin/users"): Handler.api_user_create,
    ("POST", "/api/admin/import"): Handler.api_import,
}



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


def cli():
    cmd = sys.argv[1]
    if cmd == "--list-users":
        rows = q("SELECT * FROM users ORDER BY role, name")
        if not rows:
            print("No users yet.")
        for u in rows:
            print("%-4d %-6s %-16s %-24s %s%s" % (u["id"], u["role"], u["phone"], u["name"],
                                                "active" if u["active"] else "inactive",
                                                "" if u["secret_hash"] else ("  (uses ADMIN_PASSWORD next)" if u["role"] == "admin" else "  (no PIN)")))
        return 0
    if cmd == "--reset-admin" and len(sys.argv) > 2:
        p = norm_phone(sys.argv[2])
        u = find_user_by_phone(p) if p else None
        if not u or u["role"] != "admin":
            print("No admin with that mobile number. See: python server.py --list-users")
            return 1
        with tx() as c:
            c.execute("UPDATE users SET secret_hash=NULL, must_change=1, fail_count=0, locked_until=0, active=1 WHERE id=?", (u["id"],))
            c.execute("UPDATE sessions SET unlocked_until=0 WHERE user_id=?", (u["id"],))
        print("Password of %s was reset. Unlock Settings once with ADMIN_PASSWORD from .env, then choose a new password." % u["name"])
        if not ADMIN_PASSWORD:
            print("Warning: ADMIN_PASSWORD is empty. Set it in .env and restart, or the reset can't be used.")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    conn()
    if CLI:
        sys.exit(cli())
    os.chdir(HERE)
    if not os.path.exists(APP_FILE):
        print("index.html was not found next to server.py")
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print("Database: %s" % DB_FILE)
        if not has_admin():
            if ADMIN_PASSWORD:
                print("No admin yet: open the site and complete \"First-time setup\" with ADMIN_PASSWORD.")
            else:
                print("No admin yet and ADMIN_PASSWORD is empty: set ADMIN_PASSWORD in .env and restart.")
        if legacy_json_info():
            print("Found an old data.json: import it in Settings > Users.")
        print("On this computer:  http://localhost:%d" % PORT)
        if os.environ.get("IN_DOCKER"):
            print("Running in Docker: on the phone use http://<this computer's address>:%d" % PORT)
        else:
            for ip in lan_ips():
                print("On the phone (same Wi-Fi):  http://%s:%d" % (ip, PORT))
        sys.stdout.flush()
        httpd.serve_forever()
