# -*- coding: utf-8 -*-
"""
build_site.py —— 把 SQLite 知识库导出为自包含的可视化网页
用法:  python build_site.py
产物:  ../site/index.html   （双击即可打开，无需服务器、无需联网）
       ../site/data.json   （数据快照，便于二次开发）
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "genshin_kb.db")
SITE_DIR = os.path.join(ROOT, "site")
TEMPLATE = os.path.join(SITE_DIR, "template.html")
OUT_HTML = os.path.join(SITE_DIR, "index.html")
OUT_JSON = os.path.join(SITE_DIR, "data.json")

TABLES = ["meta", "sources", "characters", "teams", "team_members", "reactions",
          "abyss_floors", "abyss_rewards", "abyss_mechanics", "monetization",
          "crystals", "primogem_income", "gacha", "resources", "operations",
          "incidents", "incident_timeline", "incident_impacts", "inflation_metrics",
          # 20-24 号表不来自 kb_data.py，而是 scripts/import_maf.py 从
          # maf/data/*.db 导入的"批量处理结果"。init_db.py 会清空它们。
          "maf_runs", "maf_items", "maf_summaries", "maf_reports",
          "maf_item_links"]


def dump():
    if not os.path.exists(DB_PATH):
        sys.exit("找不到数据库，请先运行 scripts/init_db.py")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    data = {}
    for t in TABLES:
        rows = conn.execute("SELECT * FROM %s" % t).fetchall()
        data[t] = [dict(r) for r in rows]
    conn.close()
    return data


def main():
    data = dump()
    os.makedirs(SITE_DIR, exist_ok=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        tpl = f.read()

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if tpl.count("@@KB_JSON@@") != 1:
        sys.exit("模板中 @@KB_JSON@@ 出现 %d 次（应为 1 次）" % tpl.count("@@KB_JSON@@"))
    html = tpl.replace("@@KB_JSON@@", payload, 1)

    if html.count(payload) != 1:
        sys.exit("生成异常：数据块在页面中出现 %d 次（应为 1 次）" % html.count(payload))

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUT_HTML) / 1024.0
    print("已生成:", OUT_HTML, "(%.0f KB)" % size_kb)
    print("已生成:", OUT_JSON)
    for t in TABLES:
        print("   %-18s %4d 行" % (t, len(data[t])))


if __name__ == "__main__":
    main()
