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

    @app.errorhandler(413)
    def too_big(_):
        return _err(413, "请求内容太大")

    @app.errorhandler(404)
    def nf(_):
        return _err(404, "not found")

    return app


app = create_app()
