"""积分计费。金额在库里一律存「分」(int)，1 积分 = 1 元 = 100 分。"""
import math

from config import COMMENTS_PER_TIER
from database import get_setting


class OutOfPoints(Exception):
    pass


def to_cents(points):
    return int(round(float(points) * 100))


def fmt(cents):
    v = (cents or 0) / 100
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def tier_price(platform_key):
    """Price (分) of one tier = up to COMMENTS_PER_TIER comments from one work."""
    key = "price_post_wechat" if platform_key.startswith("wechat") else "price_post"
    try:
        return max(0, to_cents(get_setting(key) or 0))
    except ValueError:
        return 0


def tiers_for(comment_count):
    return max(1, math.ceil(comment_count / COMMENTS_PER_TIER))


def adjust(conn, user_id, delta, kind, note="", operator_id=None, task_id=None):
    """Change a user's balance and write the ledger row. Returns the new balance (分)."""
    recharged = delta if kind == "充值" and delta > 0 else 0
    conn.execute("UPDATE users SET balance = balance + ?, total_recharged = total_recharged + ? WHERE id = ?",
                 (delta, recharged, user_id))
    bal = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
    conn.execute("INSERT INTO points_log (user_id, delta, balance_after, kind, note, task_id, operator_id) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, delta, bal, kind, note, task_id, operator_id))
    return bal


def charge_task(conn, user_id, task_id, cents):
    """Deduct for a task; all deductions of one task accumulate in a single ledger row."""
    conn.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE id = ?",
                 (cents, cents, user_id))
    bal = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
    row = conn.execute("SELECT id FROM points_log WHERE task_id=? AND kind='扣费'", (task_id,)).fetchone()
    if row:
        conn.execute("UPDATE points_log SET delta = delta - ?, balance_after = ? WHERE id = ?", (cents, bal, row[0]))
    else:
        conn.execute("INSERT INTO points_log (user_id, delta, balance_after, kind, note, task_id) "
                     "VALUES (?, ?, ?, '扣费', ?, ?)", (user_id, -cents, bal, f"采集任务 #{task_id}", task_id))
    conn.execute("UPDATE tasks SET points_cost = points_cost + ? WHERE id = ?", (cents, task_id))
    conn.commit()
    return bal

