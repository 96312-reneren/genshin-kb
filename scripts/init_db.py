# -*- coding: utf-8 -*-
"""
init_db.py —— 建立 SQLite 知识库
用法:  python init_db.py
产物:  ../genshin_kb.db
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_data import (META, SOURCES, CHARACTERS, TEAMS, TEAM_MEMBERS, REACTIONS,
                     ABYSS_FLOORS, ABYSS_REWARDS, ABYSS_MECHANICS, MONETIZATION,
                     CRYSTALS, PRIMOGEM_INCOME, GACHA, RESOURCES, OPERATIONS,
                     INCIDENTS, INCIDENT_TIMELINE, INCIDENT_IMPACTS,
                     INFLATION_METRICS)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "genshin_kb.db")
SCHEMA = os.path.join(HERE, "schema.sql")


def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("[1/3] 已移除旧的数据库文件")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    with open(SCHEMA, "r", encoding="utf-8") as f:
        cur.executescript(f.read())
    print("[1/3] 已按 schema.sql 建表")

    cur.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", META)
    cur.executemany(
        "INSERT INTO sources(title, publisher, url, license_note, used_for, accessed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)", SOURCES)
    cur.executemany(
        "INSERT INTO characters(name, element, weapon, rarity, region, roles,"
        " limited, version_added, confidence) VALUES (?,?,?,?,?,?,?,?,?)",
        [(n, e, w, r, reg, roles, lim, ver, conf)
         for (n, e, w, r, reg, roles, lim, ver, conf) in CHARACTERS])

    cur.executemany(
        "INSERT INTO teams(name, archetype, core_reaction, cost_level, difficulty,"
        " summary, rotation, strengths, weaknesses, abyss_fit, tags)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", TEAMS)

    name_to_id = {row[0]: row[1] for row in
                  cur.execute("SELECT name, id FROM teams").fetchall()}
    member_rows = []
    for team_name, members in TEAM_MEMBERS.items():
        tid = name_to_id.get(team_name)
        if tid is None:
            print("  ! 未找到队伍:", team_name)
            continue
        for slot, char, note in members:
            member_rows.append((tid, slot, char, note))
    cur.executemany(
        "INSERT INTO team_members(team_id, slot, char_name, role_note) VALUES (?,?,?,?)",
        member_rows)

    cur.executemany(
        "INSERT INTO reactions(name, category, elements, base_multiplier, trigger, note)"
        " VALUES (?,?,?,?,?,?)", REACTIONS)

    # abyss_floors 的列顺序: floor, chambers, halves, typical_enemies, environment, reward_note
    cur.executemany(
        "INSERT INTO abyss_floors(floor, chambers, halves, typical_enemies, environment,"
        " reward_note) VALUES (?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in ABYSS_FLOORS])

    cur.executemany(
        "INSERT INTO abyss_rewards(scope, item, amount, period, note) VALUES (?,?,?,?,?)",
        ABYSS_REWARDS)
    cur.executemany(
        "INSERT INTO abyss_mechanics(name, category, description, strategy) VALUES (?,?,?,?)",
        ABYSS_MECHANICS)

    cur.executemany(
        "INSERT INTO monetization(name, price_cny, kind, contents, primogem_equiv,"
        " pulls_equiv, cny_per_pull, design_intent) VALUES (?,?,?,?,?,?,?,?)",
        MONETIZATION)

    cur.executemany(
        "INSERT INTO crystals(price_cny, crystals, is_first_double, note) VALUES (?,?,?,?)",
        CRYSTALS)

    cur.executemany(
        "INSERT INTO primogem_income(source, amount_low, amount_high, period,"
        " player_type, note) VALUES (?,?,?,?,?,?)", PRIMOGEM_INCOME)

    cur.executemany(
        "INSERT INTO gacha(banner, rarity, base_rate, comprehensive_rate, hard_pity,"
        " soft_pity_start, avg_pulls, up_rate, guarantee_rule, cost_primogems, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", GACHA)

    cur.executemany(
        "INSERT INTO resources(name, resin_cost, weekly_limit, output, priority, note)"
        " VALUES (?,?,?,?,?,?)", RESOURCES)

    cur.executemany(
        "INSERT INTO operations(seq, topic, content_side, monetization_side, linkage,"
        " analysis) VALUES (?,?,?,?,?,?)", OPERATIONS)

    # ---- 运营事故专题（那维莱特事件 → 数值膨胀的长期影响） ----
    cur.executemany(
        "INSERT INTO incidents(code, name, version, happened_on, resolved_on, category,"
        " severity, summary, trigger, official_response, compensation, root_cause,"
        " compliance_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", INCIDENTS)

    code_to_id = {row[0]: row[1] for row in
                  cur.execute("SELECT code, id FROM incidents").fetchall()}

    tl_rows = []
    for code, rows in INCIDENT_TIMELINE.items():
        iid = code_to_id.get(code)
        if iid is None:
            print("  ! 未找到事件:", code)
            continue
        for seq, date_label, actor, event, evidence in rows:
            tl_rows.append((iid, seq, date_label, actor, event, evidence))
    cur.executemany(
        "INSERT INTO incident_timeline(incident_id, seq, date_label, actor, event,"
        " evidence) VALUES (?,?,?,?,?,?)", tl_rows)

    imp_rows = []
    for code, rows in INCIDENT_IMPACTS.items():
        iid = code_to_id.get(code)
        if iid is None:
            print("  ! 未找到事件:", code)
            continue
        for (seq, dimension, horizon, impact, mechanism, evidence, confidence,
             related_op, note) in rows:
            imp_rows.append((iid, seq, dimension, horizon, impact, mechanism,
                             evidence, confidence, related_op, note))
    cur.executemany(
        "INSERT INTO incident_impacts(incident_id, seq, dimension, horizon, impact,"
        " mechanism, evidence, confidence, related_op, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        imp_rows)

    cur.executemany(
        "INSERT INTO inflation_metrics(category, metric, period, value_text, direction,"
        " nature, source_note, note) VALUES (?,?,?,?,?,?,?,?)", INFLATION_METRICS)

    conn.commit()
    print("[2/3] 已灌入数据")

    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name").fetchall()]
    print("[3/3] 统计")
    for t in tables:
        n = cur.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("        %-18s %4d 行" % (t, n))

    conn.close()
    print("\n数据库已生成:", DB_PATH)


if __name__ == "__main__":
    build()
