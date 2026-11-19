"""数据接口中转网关。

把真正的 TikHub Key 只放在这台服务器上，给每个使用者发一个"子 Key"（hk_ 开头）。
子 Key 能做什么、能用多少次、多快，都由这里说了算，随时可以停用，不用换你自己的 Key。

  使用者的获客系统 --(子 Key)--> 本网关 --(真 Key)--> TikHub

只转发"采集所需的各平台数据接口"：账号、余额、充值、Key 管理这类 /api/v1/tikhub/* 一律不转发。
运行：gunicorn -w 1 --threads 16 -b 127.0.0.1:5002 gateway:app   （必须 1 个进程：限速在内存里）
"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GATEWAY_DB", os.path.join(BASE, "gateway.db"))
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "https://api.tikhub.io").rstrip("/")
UPSTREAM_KEY = os.environ.get("TIKHUB_API_KEY", "")
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")   # 管理接口的令牌；不设置 = 管理接口关闭
TIMEOUT = 45
MAX_BODY = 64 * 1024
MAX_CONCURRENT = 12          # 同时在等上游的请求数，防止线程被占满
DEFAULT_RATE = 120           # 每个子 Key 每分钟最多请求数
KEY_PREFIX = "hk_"

# 只放行这些平台下的接口（获客系统采集用到的）；不含 tikhub（账号 / 余额 / Key 管理）
ALLOWED = ("douyin", "xiaohongshu", "kuaishou", "bilibili", "weibo", "zhihu", "wechat_search",
           "wechat_channels", "wechat_mp", "xigua", "toutiao", "pipixia")
PATH_OK = re.compile(r"^/api/v1/(?:" + "|".join(ALLOWED) + r")/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*$")
ME_PATH = "/api/v1/tikhub/user/get_user_info"   # 获客系统用它显示余额：这里改成返回该子 Key 自己的剩余次数

_ctx = ssl.create_default_context()
_gate = threading.BoundedSemaphore(MAX_CONCURRENT)
_hits = defaultdict(deque)   # key_id -> 最近一分钟的请求时间
_hits_lock = threading.Lock()
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextlib.contextmanager
def conn():
    """开库、提交、关库。（sqlite3 自带的 with 只提交不关闭）"""
    c = db()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _db_lock, conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS keys (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                prefix       TEXT NOT NULL,
                key_hash     TEXT NOT NULL UNIQUE,
                note         TEXT NOT NULL DEFAULT '',
                quota        INTEGER,                       -- 可用总次数；NULL = 不限
                used         INTEGER NOT NULL DEFAULT 0,
                rate_per_min INTEGER NOT NULL DEFAULT 120,
                enabled      INTEGER NOT NULL DEFAULT 1,
                expires_at   INTEGER,                       -- 到期时间戳；NULL = 不过期
                created_at   INTEGER NOT NULL,
                last_used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                ts INTEGER NOT NULL, key_id INTEGER NOT NULL, path TEXT NOT NULL,
                status INTEGER NOT NULL, ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
            CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(key_id, ts);
        """)
        # 日志只留 30 天；只记路径，不记查询参数（里面是客户搜的关键词）
        c.execute("DELETE FROM usage_log WHERE ts < ?", (int(time.time()) - 30 * 86400,))


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(note="", quota=None, days=None, rate=DEFAULT_RATE):
    """生成子 Key。明文只在这里返回一次，库里只存哈希。"""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    with _db_lock, conn() as c:
        cur = c.execute(
            "INSERT INTO keys (prefix, key_hash, note, quota, rate_per_min, expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (key[:10], hash_key(key), note, quota, rate, now + days * 86400 if days else None, now))
        return cur.lastrowid, key


def list_keys():
    with _db_lock, conn() as c:
        rows = c.execute("SELECT * FROM keys ORDER BY id DESC").fetchall()
        week = int(time.time()) - 7 * 86400
        recent = dict(c.execute("SELECT key_id, COUNT(*) FROM usage_log WHERE ts >= ? AND status BETWEEN 200 AND 299 "
                                "GROUP BY key_id", (week,)).fetchall())
    now = time.time()
    return [{
        "id": r["id"], "prefix": r["prefix"], "note": r["note"], "quota": r["quota"], "used": r["used"],
        "left": None if r["quota"] is None else max(r["quota"] - r["used"], 0),
        "rate_per_min": r["rate_per_min"], "enabled": bool(r["enabled"]), "expires_at": r["expires_at"],
        "expired": bool(r["expires_at"] and r["expires_at"] <= now), "created_at": r["created_at"],
        "last_used_at": r["last_used_at"], "week_calls": recent.get(r["id"], 0),
    } for r in rows]


def set_enabled(key_id, enabled):
    with _db_lock, conn() as c:
        return c.execute("UPDATE keys SET enabled=? WHERE id=?", (1 if enabled else 0, key_id)).rowcount == 1


def topup(key_id, n):
    with _db_lock, conn() as c:
        return c.execute("UPDATE keys SET quota = quota + ? WHERE id=? AND quota IS NOT NULL", (n, key_id)).rowcount == 1


def extend(key_id, days):
    """到期时间往后延 days 天（已过期的从现在算起；不过期的 Key 不动）。"""
    now = int(time.time())
    with _db_lock, conn() as c:
        return c.execute("UPDATE keys SET expires_at = MAX(expires_at, ?) + ? WHERE id=? AND expires_at IS NOT NULL",
                         (now, days * 86400, key_id)).rowcount == 1


def delete_key(key_id):
    with _db_lock, conn() as c:
        n = c.execute("DELETE FROM keys WHERE id=?", (key_id,)).rowcount
        c.execute("DELETE FROM usage_log WHERE key_id=?", (key_id,))
    _hits.pop(key_id, None)
    return n == 1


def usage_summary(key_id, days=7):
    since = int(time.time()) - days * 86400
    with _db_lock, conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT path, COUNT(*) n, SUM(status BETWEEN 200 AND 299) ok, CAST(AVG(ms) AS INTEGER) ms FROM usage_log "
            "WHERE key_id=? AND ts>=? GROUP BY path ORDER BY n DESC", (key_id, since)).fetchall()]


def _err(status, message):
    return jsonify(code=status, message=message), status


def _token():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _check_rate(key_id, limit):
    now = time.time()
    with _hits_lock:
        q = _hits[key_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _reserve(c, key_id):
    """原子地占用 1 次额度；返回是否成功（并发下不会超发）。"""
    now = int(time.time())
    cur = c.execute(
        "UPDATE keys SET used = used + 1, last_used_at = ? WHERE id = ? AND enabled = 1 "
        "AND (quota IS NULL OR used < quota) AND (expires_at IS NULL OR expires_at > ?)", (now, key_id, now))
    return cur.rowcount == 1


def _refund(key_id):
    with _db_lock, conn() as c:
        c.execute("UPDATE keys SET used = MAX(used - 1, 0) WHERE id = ?", (key_id,))


def _log(key_id, path, status, started):
    try:
        with _db_lock, conn() as c:
            c.execute("INSERT INTO usage_log (ts, key_id, path, status, ms) VALUES (?,?,?,?,?)",
                      (int(time.time()), key_id, path, status, int((time.time() - started) * 1000)))
    except sqlite3.Error:
        pass


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    init_db()

    @app.get("/")
    def index():
        return Response(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>数据接口网关</title><body style='font:16px/1.7 system-ui,sans-serif;max-width:32em;margin:12vh auto;padding:0 20px'>"
            "<h1 style='font-size:22px'>数据接口网关 · 运行正常</h1>"
            "<p>这是获客系统的数据接口中转服务，给程序调用的，没有网页界面。</p>"
            "<p>需要使用的话，请联系提供者获取子 Key，然后在获客系统「管理后台 → 设置」里填写 Key 和本页的地址。</p>"
            "</body>", content_type="text/html; charset=utf-8")

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, upstream_key_set=bool(UPSTREAM_KEY))

    @app.route("/api/v1/<path:rest>", methods=["GET", "POST"])
    def proxy(rest):
        started = time.time()
        path = "/api/v1/" + rest
        token = _token()
        if not token.startswith(KEY_PREFIX):
            return _err(401, "缺少或无效的 Key")
        with _db_lock, conn() as c:
            row = c.execute("SELECT * FROM keys WHERE key_hash = ?", (hash_key(token),)).fetchone()
        if not row or not row["enabled"]:
            return _err(401, "Key 无效或已停用，请联系提供者")
        if row["expires_at"] and row["expires_at"] <= time.time():
            return _err(403, "Key 已过期，请联系提供者续期")

        if path == ME_PATH:   # 不转发到 TikHub：那是网关主人的账户信息
            left = None if row["quota"] is None else max(row["quota"] - row["used"], 0)
            return jsonify(code=200, user_data={"balance": left, "quota": row["quota"], "used": row["used"],
                                                "unit": "calls", "expires_at": row["expires_at"]})
        if not PATH_OK.match(path):
            return _err(403, "这个接口不在允许范围内")
        if not UPSTREAM_KEY:
            return _err(503, "网关还没有配置上游 Key")
        if not _check_rate(row["id"], row["rate_per_min"]):
            return _err(429, f"请求太快了（每分钟最多 {row['rate_per_min']} 次），请稍后再试")

        body = request.get_data() if request.method == "POST" else None   # 先读正文：超大会在这里 413，不扣次数
        with _db_lock, conn() as c:
            ok = _reserve(c, row["id"])
        if not ok:
            return _err(402, "额度已用完或 Key 已失效，请联系提供者充值")

        url = UPSTREAM + path
        if request.query_string:
            url += "?" + request.query_string.decode("utf-8", "replace")
        req = urllib.request.Request(url, data=body, method=request.method, headers={
            "Authorization": f"Bearer {UPSTREAM_KEY}",
            "User-Agent": "tikhub-python-sdk/2.1.1",   # TikHub 前面的 Cloudflare 会拦 urllib 默认 UA
            "Accept": "application/json",
            "Content-Type": request.headers.get("Content-Type", "application/json"),
        })
        if not _gate.acquire(timeout=10):
            _refund(row["id"])
            return _err(503, "网关繁忙，请稍后再试")
        status, payload, ctype = 502, b"", "application/json"
        try:
            try:
                with urllib.request.urlopen(req, context=_ctx, timeout=TIMEOUT) as r:
                    status, payload, ctype = r.status, r.read(), r.headers.get("Content-Type", ctype)
            except urllib.error.HTTPError as e:
                status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type", ctype)
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"[gateway] upstream error {path}: {e}", file=sys.stderr)
            except Exception:
                _refund(row["id"])
                raise
        finally:
            _gate.release()

        if status in (401, 402, 403):
            # 这是上游在拒绝"网关自己的 Key"（欠费 / 失效），别把细节透给使用者
            print(f"[gateway] upstream rejected our key: HTTP {status} on {path}", file=sys.stderr)
            _refund(row["id"])
            _log(row["id"], path, 502, started)
            return _err(502, "上游数据服务暂时不可用，请联系提供者")
        if not 200 <= status < 300:
            _refund(row["id"])       # 只有成功的请求才算次数
            if not payload:
                _log(row["id"], path, 502, started)
                return _err(502, "上游数据服务没有响应，请稍后再试")
        _log(row["id"], path, status, started)
        return Response(payload, status=status, content_type=ctype)

    # ---- 管理接口：给获客系统的「管理后台」调用（令牌鉴权，且不允许从公网反代进来）----
    def _admin_ok():
        if not ADMIN_TOKEN:
            return False
        if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
            return False   # 经过反向代理 = 来自公网，管理接口只接受本机直连
        return secrets.compare_digest(request.headers.get("X-Admin-Token", "").encode(), ADMIN_TOKEN.encode())

    @app.before_request
    def _guard_admin():
        if request.path.startswith("/_admin/") and not _admin_ok():
            return _err(404, "not found")

    @app.get("/_admin/keys")
    def admin_list():
        return jsonify(ok=True, keys=list_keys(), upstream_key_set=bool(UPSTREAM_KEY))

    @app.post("/_admin/keys")
    def admin_create():
        d = request.get_json(silent=True) or {}
        quota = d.get("quota")
        days = d.get("days")
        rate = d.get("rate") or DEFAULT_RATE
        note = str(d.get("note") or "").strip()[:60]
        if quota is not None and not (isinstance(quota, int) and 0 < quota <= 100_000_000):
            return jsonify(ok=False, message="次数要是 1 以上的整数"), 400
        if days is not None and not (isinstance(days, int) and 0 < days <= 3650):
            return jsonify(ok=False, message="有效天数要是 1～3650"), 400
        if not (isinstance(rate, int) and 1 <= rate <= 6000):
            return jsonify(ok=False, message="限速要是 1～6000 次/分钟"), 400
        kid, key = new_key(note, quota, days, rate)
        return jsonify(ok=True, id=kid, key=key)

    @app.post("/_admin/keys/<int:kid>/<action>")
    def admin_action(kid, action):
        d = request.get_json(silent=True) or {}
        if action in ("enable", "disable"):
            ok = set_enabled(kid, action == "enable")
        elif action == "delete":
            ok = delete_key(kid)
        elif action == "topup":
            n = d.get("n")
            if not (isinstance(n, int) and 0 < n <= 100_000_000):
                return jsonify(ok=False, message="次数要是 1 以上的整数"), 400
            ok = topup(kid, n)
        elif action == "extend":
            n = d.get("days")
            if not (isinstance(n, int) and 0 < n <= 3650):
                return jsonify(ok=False, message="天数要是 1～3650"), 400
            ok = extend(kid, n)
        else:
            return jsonify(ok=False, message="未知操作"), 400
        return jsonify(ok=ok, message="" if ok else "没有这个 Key，或这个操作不适用（比如不限次数的 Key 不能加次数）")

    @app.get("/_admin/keys/<int:kid>/usage")
    def admin_usage(kid):
        return jsonify(ok=True, usage=usage_summary(kid, min(request.args.get("days", 7, type=int), 30)))

    @app.errorhandler(413)
    def too_big(_):
        return _err(413, "请求内容太大")

    @app.errorhandler(404)
    def nf(_):
        return _err(404, "not found")

    return app


app = create_app()
