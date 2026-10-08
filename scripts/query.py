# -*- coding: utf-8 -*-
"""
query.py —— 命令行查询工具（不需要写 SQL 也能查）

示例:
  python query.py teams
  python query.py teams --archetype 蒸发
  python query.py teams --name 超绽放
  python query.py chars --element 火 --rarity 5
  python query.py ops --grep 树脂
  python query.py abyss
  python query.py tables
  python query.py sql "SELECT name,element,weapon FROM characters WHERE rarity=5 ORDER BY element"
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "genshin_kb.db")


def conn():
    if not os.path.exists(DB_PATH):
        sys.exit("找不到数据库，请先运行 scripts/init_db.py")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def show(rows, width=88):
    rows = [dict(r) for r in rows]
    if not rows:
        print("（无结果）")
        return
    for r in rows:
        print("-" * width)
        for k, v in r.items():
            if v is None or v == "":
                continue
            s = str(v).replace("\n", " ")
            if len(s) > 300:
                s = s[:300] + " …"
            print("  %-16s %s" % (k, s))
    print("-" * width)
    print("共 %d 条" % len(rows))


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("cmd", nargs="?", default="help")
    ap.add_argument("sql_text", nargs="?", default=None)
    ap.add_argument("--archetype")
    ap.add_argument("--name")
    ap.add_argument("--element")
    ap.add_argument("--rarity", type=int)
    ap.add_argument("--grep")
    args, _ = ap.parse_known_args()

    c = conn()

    if args.cmd == "help":
        print(__doc__)

    elif args.cmd == "tables":
        ts = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in ts:
            n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            print("  %-18s %4d 行" % (t, n))

    elif args.cmd == "teams":
        q = "SELECT * FROM teams WHERE 1=1"
        p = []
        if args.archetype:
            q += " AND archetype LIKE ?"; p.append("%" + args.archetype + "%")
        if args.name:
            q += " AND name LIKE ?"; p.append("%" + args.name + "%")
        if args.grep:
            q += " AND (summary LIKE ? OR strengths LIKE ? OR weaknesses LIKE ?)"
            p += ["%" + args.grep + "%"] * 3
        show(c.execute(q, p).fetchall())
        if args.name:
            t = c.execute("SELECT id FROM teams WHERE name LIKE ?", ("%" + args.name + "%",)).fetchone()
            if t:
                print("队伍成员：")
                for m in c.execute("SELECT slot, char_name, role_note FROM team_members WHERE team_id=?",
                                   (t["id"],)):
                    print("  %-8s %-14s %s" % (m["slot"], m["char_name"], m["role_note"]))

    elif args.cmd == "chars":
        q = "SELECT * FROM characters WHERE 1=1"
        p = []
        if args.element:
            q += " AND element=?"; p.append(args.element)
        if args.rarity:
            q += " AND rarity=?"; p.append(args.rarity)
        if args.grep:
            q += " AND (name LIKE ? OR roles LIKE ?)"; p += ["%" + args.grep + "%"] * 2
        q += " ORDER BY element, rarity DESC, name"
        show(c.execute(q, p).fetchall())

    elif args.cmd == "ops":
        q = "SELECT seq, topic, analysis FROM operations WHERE 1=1"
        p = []
        if args.grep:
            q += " AND (topic LIKE ? OR content_side LIKE ? OR monetization_side LIKE ?" \
                 " OR linkage LIKE ? OR analysis LIKE ?)"
            p += ["%" + args.grep + "%"] * 5
        q += " ORDER BY seq"
        show(c.execute(q, p).fetchall())

    elif args.cmd == "abyss":
        print("=== 楼层结构 ===")
        show(c.execute("SELECT * FROM abyss_floors").fetchall())
        print("=== 奖励 ===")
        show(c.execute("SELECT * FROM abyss_rewards").fetchall())
        print("=== 机制 ===")
        show(c.execute("SELECT name, category, strategy FROM abyss_mechanics").fetchall())

    elif args.cmd == "sql":
        if not args.sql_text:
            sys.exit("用法: python query.py sql \"SELECT ...\"")
        show(c.execute(args.sql_text).fetchall())

    else:
        print(__doc__)

    c.close()


if __name__ == "__main__":
    main()
