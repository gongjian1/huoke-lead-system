"""网关管理命令（在网关所在服务器上运行，需要和网关用同一个 GATEWAY_DB）

  python manage.py create --note 张三 --quota 3000 [--days 30] [--rate 60]    发一个子 Key（只显示这一次）
  python manage.py create --note 内部 --unlimited                              不限次数
  python manage.py list                                                        看所有子 Key 和用量
  python manage.py topup ID 2000                                               加 2000 次
  python manage.py disable ID | enable ID | delete ID
  python manage.py usage ID [天数]                                             某个 Key 最近都调了什么
"""
import argparse
import sys
import time

import gateway as gw


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "—"


def main():
    gw.init_db()
    p = argparse.ArgumentParser(usage=__doc__)
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("create")
    c.add_argument("--note", default="")
    c.add_argument("--quota", type=int)
    c.add_argument("--unlimited", action="store_true")
    c.add_argument("--days", type=int)
    c.add_argument("--rate", type=int, default=gw.DEFAULT_RATE)
    sub.add_parser("list")
    for name in ("disable", "enable", "delete"):
        sub.add_parser(name).add_argument("id", type=int)
    t = sub.add_parser("topup")
    t.add_argument("id", type=int)
    t.add_argument("n", type=int)
    u = sub.add_parser("usage")
    u.add_argument("id", type=int)
    u.add_argument("days", type=int, nargs="?", default=7)
    a = p.parse_args()

    if a.cmd == "create":
        if a.quota is None and not a.unlimited:
            sys.exit("请指定 --quota 次数，或者明确写 --unlimited")
        if a.quota is not None and a.quota <= 0:
            sys.exit("--quota 要大于 0")
        kid, key = gw.new_key(a.note, None if a.unlimited else a.quota, a.days, a.rate)
        print(f"已创建 #{kid}（{a.note or '无备注'}）")
        print(f"子 Key（只显示这一次，请马上复制给对方）：\n\n  {key}\n")
    elif a.cmd == "list":
        rows = gw.list_keys()[::-1]
        print(f"{'ID':>3}  {'前缀':<11} {'状态':<4} {'已用/额度':<14} {'限速/分':>6}  {'到期':<10} {'最近使用':<10} 备注")
        for r in rows:
            quota = "不限" if r["quota"] is None else r["quota"]
            state = "过期" if r["expired"] else ("启用" if r["enabled"] else "停用")
            print(f"{r['id']:>3}  {r['prefix']:<11} {state:<4} {str(r['used']) + '/' + str(quota):<14} "
                  f"{r['rate_per_min']:>6}  {fmt_ts(r['expires_at']):<10} {fmt_ts(r['last_used_at']):<10} {r['note']}")
        if not rows:
            print("（还没有子 Key）")
    elif a.cmd in ("disable", "enable"):
        ok = gw.set_enabled(a.id, a.cmd == "enable")
        print(("已启用" if a.cmd == "enable" else "已停用") if ok else "没有这个 ID")
    elif a.cmd == "delete":
        print("已删除" if gw.delete_key(a.id) else "没有这个 ID")
    elif a.cmd == "topup":
        print(f"已加 {a.n} 次" if gw.topup(a.id, a.n) else "没有这个 ID，或者它本来就是不限次数")
    elif a.cmd == "usage":
        rows = gw.usage_summary(a.id, a.days)
        for r in rows:
            print(f"{r['n']:>6} 次请求（成功 {r['ok']}，共计 {r['cost']} 次额度）平均 {r['ms']}ms  {r['path']}")
        if not rows:
            print(f"最近 {a.days} 天没有调用")
    else:
        p.print_help()


if __name__ == "__main__":
    main()
