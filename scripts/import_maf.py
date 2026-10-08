# -*- coding: utf-8 -*-
"""
import_maf.py —— 把 maf/ 子项目"批量处理出来的结果"导入知识库数据库

背景
----
maf/（MAF 内容观察工作台）是一条独立的生产线：一批材料进库 → 本地/云端模型
逐条概括 → 生成时间轴与观察报告 → 出 PPT / Word / PDF。它的产物落在
maf/data/app.db 与 maf/data/crawl.db 里。

这个脚本是两者的桥：把 maf 那两个库里的"派生结果"（批次台账、条目骨架、
逐条概括、观察报告）搬进 genshin_kb.db 的 20-24 号表，让知识库网站能直接
展示这些材料，并把它们挂到已有的角色 / 版本 / 运营事故上。

用法
----
    python scripts/import_maf.py                 # 正常导入
    python scripts/import_maf.py --dry-run       # 只看会导入什么，不写库
    python scripts/import_maf.py --include-third-party-body
                                                 # 连第三方镜像线的上游正文也存
                                                 # （默认不存，见下）

顺序很重要
----------
    python scripts/init_db.py      # 建库（会清空 20-24 号表！）
    python scripts/import_maf.py   # 重新导入 maf 的产物
    python scripts/build_site.py   # 生成网页

合规口径（与《数据来源与合规说明.md》第四节一致）
------------------------------------------------
本库的原则是"能用官方公示就用官方公示；第三方/社区内容只用来对参数、不抄
文字；只存机制不存素材"。据此把 maf 的条目分成两条线：

  人工投喂线（source_api = manual / 人工投喂文本）
      正文是使用者自己粘贴的材料，不是转载也不是抓取，因此原样保留在
      maf_items.body 里、并公开标出来源。

  第三方接口镜像线（source_api = api.ennead.cc/... 等）
      这些条目历史上来自第三方接口镜像。即便只是"公告"，它也是上游的成段
      文本，落库就等于在本库里存了一份转载。因此默认只保留：
      标题（短事实标识）、发布时间、出处指针，以及 maf 自己生成的派生概括；
      上游正文一律不入库，body 留空并在 body_policy 里写明原因。
      确有必要时可以用 --include-third-party-body 显式打开。

本脚本是幂等的：每次运行都会先清空 20-24 号表再重建，跑多少次结果都一样。
它不会改动 1-19 号表（那些是 kb_data.py 里手写的常量）。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KB_DB = os.path.join(ROOT, "genshin_kb.db")
DEFAULT_MAF_DIR = os.path.join(ROOT, "maf")

# maf/data/ 下要扫描的库。顺序决定同名批次的展示顺序。
MAF_DB_FILES = ("app.db", "crawl.db")

UNKNOWN_TS = 9999999999  # maf 约定的"日期未知"占位时间戳

# 概括质量的取舍顺序：云端 > 本地模型 > 桩
BACKEND_RANK = {"cloud": 0, "local": 1, "local-stub": 2}

ORIGIN_MANUAL = "人工投喂"
ORIGIN_MIRROR = "第三方接口镜像"

BODY_POLICY_MANUAL = "自己粘贴的材料，正文原样保留（非转载、非抓取）。"
BODY_POLICY_MIRROR = (
    "来源是第三方接口镜像的公告。按本库合规原则「第三方内容不抄文字、只存机制」，"
    "这里不保存上游正文，只保留标题、发布时间、出处指针与派生概括。"
)
BODY_POLICY_MIRROR_FORCED = (
    "来源是第三方接口镜像的公告，本次用 --include-third-party-body 显式保存了上游正文，"
    "仅供个人研究核对，对外引用请回到官方公告。"
)

# 「月之X」命名 → 版本号（见 meta.game_version_note：6.0 起启用月之命名）
MONTH_VERSION = {
    "月之一": "6.0", "月之二": "6.1", "月之三": "6.2", "月之四": "6.3",
    "月之五": "6.4", "月之六": "6.5", "月之七": "6.6", "月之八": "6.7",
}
VERSION_RE = re.compile(r"(?<![\d.])([1-7])\.([0-8])(?![\d])")
MONTH_RE = re.compile(r"月之[一二三四五六七八]")


# ---------------------------------------------------------------- 工具

def classify_origin(source_api: str) -> str:
    """按 source_api 判断条目属于哪条线。"""
    s = (source_api or "").strip().lower()
    if s == "manual" or "人工投喂" in (source_api or ""):
        return ORIGIN_MANUAL
    return ORIGIN_MIRROR


def make_date_label(created_at, created_iso) -> str:
    """展示用日期。取不到就老实写「日期未知」，绝不推断。"""
    if created_at == UNKNOWN_TS:
        return "日期未知"
    iso = (created_iso or "").strip()
    if len(iso) >= 10 and iso[:4].isdigit():
        return iso[:10]
    return "日期未知"


def pick_primary(rows, key_rank, key_time):
    """在若干候选里选一条作为主展示项：先比质量等级，再比时间新。"""
    if not rows:
        return None
    return sorted(rows, key=lambda r: (key_rank(r), _neg_time(key_time(r))))[0]


def _neg_time(s):
    """把时间字符串变成可排序值：越大越靠前 → 取负。"""
    return tuple(-ord(c) for c in (s or ""))


# ---------------------------------------------------------------- 读 maf

def read_maf(maf_dir: str):
    """读遍 maf/data/*.db，返回 (runs, items, summaries, reports)。"""
    found = [f for f in MAF_DB_FILES if os.path.exists(os.path.join(maf_dir, "data", f))]
    if not found:
        sys.exit(
            "在 %s\\data\\ 下没找到 %s。\n"
            "请确认 maf 子项目还在原位（它应位于本仓库根目录的 maf\\）。"
            % (maf_dir, " / ".join(MAF_DB_FILES))
        )

    runs, items, summaries, reports = [], [], [], []
    for fname in found:
        path = os.path.join(maf_dir, "data", fname)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row

        def has(tbl):
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone() is not None

        if has("news_runs"):
            for r in conn.execute("SELECT * FROM news_runs ORDER BY news_run_id"):
                runs.append({"source_db": fname, "row": dict(r)})
        if has("news_items"):
            for r in conn.execute("SELECT * FROM news_items"):
                items.append({"source_db": fname, "row": dict(r)})
        if has("news_summaries"):
            for r in conn.execute("SELECT * FROM news_summaries ORDER BY id"):
                summaries.append({"source_db": fname, "row": dict(r)})
        if has("news_reports"):
            for r in conn.execute("SELECT * FROM news_reports ORDER BY id"):
                reports.append({"source_db": fname, "row": dict(r)})
        conn.close()
    return found, runs, items, summaries, reports


# ---------------------------------------------------------------- 关联匹配

def collect_matchers(kb: sqlite3.Connection):
    """从知识库既有内容里归纳出可匹配的实体。纯字符串匹配，不用模型。"""
    chars = [r[0] for r in kb.execute(
        "SELECT name FROM characters ORDER BY LENGTH(name) DESC, name")]
    char_set = set(chars)

    incidents = list(kb.execute("SELECT id, code, name, summary FROM incidents"))
    inc_keywords = {}
    for inc in incidents:
        parts = [inc["summary"] or "", inc["name"] or ""]
        for t in kb.execute(
                "SELECT event FROM incident_timeline WHERE incident_id=?", (inc["id"],)):
            parts.append(t["event"] or "")
        for im in kb.execute(
                "SELECT impact, mechanism FROM incident_impacts WHERE incident_id=?",
                (inc["id"],)):
            parts.append((im["impact"] or "") + " " + (im["mechanism"] or ""))
        blob = " ".join(parts)
        kws = [c for c in chars if c in blob]
        if kws:
            inc_keywords[inc["code"]] = kws
    return chars, char_set, inc_keywords


def find_versions(text: str):
    """找出文本里出现的版本号 token，返回 [(标准化版本, 命中原文)]。"""
    out = []
    for m in VERSION_RE.finditer(text):
        out.append(("%s.%s" % (m.group(1), m.group(2)), m.group(0)))
    for m in MONTH_RE.finditer(text):
        tok = m.group(0)
        if tok in MONTH_VERSION:
            out.append((MONTH_VERSION[tok], tok))
    # 去重，保持出现顺序
    seen, uniq = set(), []
    for v, tok in out:
        if v not in seen:
            seen.add(v)
            uniq.append((v, tok))
    return uniq


def build_links(item_id, title, summary, body, chars, inc_keywords):
    """为一条材料生成到知识库实体的关联。

    匹配是纯字符串子串查找，不用模型 —— 关联可以错，但必须能追溯是怎么来的，
    所以每条关联都记下 matched_by（命中的那个词）与 confidence。
    """
    title = title or ""
    summary = summary or ""
    body = body or ""
    head = title
    text = title + "\n" + summary + "\n" + body
    links = []

    for name in chars:
        if name in text:
            links.append((item_id, "角色", "characters", name, name,
                          "高" if name in head else "中"))
    for code, kws in inc_keywords.items():
        for kw in kws:
            if kw in text:
                links.append((item_id, "运营事故", "incidents", code, kw,
                              "高" if kw in head else "中"))
                break
    for ver, tok in find_versions(text):
        links.append((item_id, "版本", "versions", ver, tok, "中"))
    return links


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="把 maf 子项目批量处理出来的结果导入 genshin_kb.db")
    ap.add_argument("--kb-db", default=KB_DB, help="知识库数据库路径")
    ap.add_argument("--maf-dir", default=DEFAULT_MAF_DIR, help="maf 子项目目录")
    ap.add_argument("--include-third-party-body", action="store_true",
                    help="连第三方接口镜像线的上游正文也一并入库（默认不存）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()

    if not os.path.exists(args.kb_db):
        sys.exit("找不到 %s，请先运行 scripts/init_db.py" % args.kb_db)

    found, runs, items, summaries, reports = read_maf(args.maf_dir)
    print("读取 maf 数据源:", ", ".join(found))
    print("  批次 %d / 条目 %d / 概括 %d / 报告 %d"
          % (len(runs), len(items), len(summaries), len(reports)))

    kb = sqlite3.connect(args.kb_db)
    kb.row_factory = sqlite3.Row
    kb.execute("PRAGMA foreign_keys = ON")

    have = {r[0] for r in kb.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"maf_runs", "maf_items", "maf_summaries", "maf_reports", "maf_item_links"}
    missing = need - have
    if missing:
        sys.exit("数据库缺少这些表：%s\n请先运行 scripts/init_db.py 建库。"
                 % ", ".join(sorted(missing)))

    chars, char_set, inc_keywords = collect_matchers(kb)

    # ---- 批次 ----
    run_ids, run_rows = {}, []
    for rec in sorted(runs, key=lambda r: (r["source_db"], r["row"]["news_run_id"])):
        r = rec["row"]
        key = (rec["source_db"], r["news_run_id"])
        rid = len(run_rows) + 1
        run_ids[key] = rid
        subject = (r.get("lang") or r.get("source_api") or "").strip()
        run_rows.append((
            rid, rec["source_db"], r["news_run_id"], subject,
            classify_origin(r.get("source_api")),
            r.get("source_api"), r.get("started_at"), r.get("finished_at"),
            r.get("fetched"), r.get("inserted"), r.get("updated"),
            r.get("rejected"), r.get("ok"), r.get("note"),
        ))

    # ---- 条目：全局按时间排序，序号稳定 ----
    item_rows, uid_to_id, body_stats = [], {}, {ORIGIN_MANUAL: 0, ORIGIN_MIRROR: 0}
    body_withheld = 0
    ordered = sorted(items, key=lambda r: (
        r["row"].get("created_at") or UNKNOWN_TS,
        str(r["row"].get("post_id") or ""),
        r["row"]["uid"],
    ))
    for seq, rec in enumerate(ordered, start=1):
        r = rec["row"]
        uid = r["uid"]
        origin = classify_origin(r.get("source_api"))
        desc = (r.get("description") or "").strip()
        if origin == ORIGIN_MANUAL:
            body, policy = desc, BODY_POLICY_MANUAL
            body_stats[ORIGIN_MANUAL] += 1
        elif args.include_third_party_body:
            body, policy = desc, BODY_POLICY_MIRROR_FORCED
            body_stats[ORIGIN_MIRROR] += 1
        else:
            body, policy = None, BODY_POLICY_MIRROR
            if desc:
                body_withheld += 1

        iid = len(item_rows) + 1
        uid_to_id[uid] = iid
        item_rows.append((
            iid, uid,
            run_ids.get((rec["source_db"], r.get("news_run_id"))),
            r.get("game"), r.get("kind"), r.get("post_id"), r.get("title"),
            r.get("created_at"), r.get("created_iso"),
            make_date_label(r.get("created_at"), r.get("created_iso")),
            seq, origin, r.get("source_name"), r.get("url"),
            body, policy, r.get("content_hash"),
        ))

    # ---- 概括：每个条目选一条主概括 ----
    by_uid = {}
    for rec in summaries:
        by_uid.setdefault(rec["row"]["uid"], []).append(rec["row"])
    sum_rows, primary_uid = [], set()
    for uid, rows in by_uid.items():
        best = pick_primary(rows, lambda r: BACKEND_RANK.get(r.get("backend"), 9),
                            lambda r: r.get("created_at"))
        if best is not None:
            primary_uid.add(uid)
    for rec in summaries:
        r = rec["row"]
        uid = r["uid"]
        iid = uid_to_id.get(uid)
        if iid is None:
            continue  # 概括没有对应的条目，跳过（不硬塞）
        is_primary = 1 if (uid in primary_uid and _is_same_row(
            r, pick_primary(by_uid[uid],
                            lambda x: BACKEND_RANK.get(x.get("backend"), 9),
                            lambda x: x.get("created_at")))) else 0
        sum_rows.append((len(sum_rows) + 1, uid, iid, r.get("ordinal"),
                         r.get("backend"), r.get("model"), r.get("summary"),
                         r.get("created_at"), is_primary))

    # ---- 报告：每个来源库选一份主报告 ----
    rep_rows, prim_report = [], set()
    by_db = {}
    for rec in reports:
        by_db.setdefault(rec["source_db"], []).append(rec["row"])
    for db, rows in by_db.items():
        best = pick_primary(rows, lambda r: BACKEND_RANK.get(r.get("backend"), 9),
                            lambda r: r.get("created_at"))
        if best is not None:
            prim_report.add((db, best["id"]))
    for rec in sorted(reports, key=lambda r: (r["source_db"], r["row"]["id"])):
        r = rec["row"]
        rep_rows.append((
            len(rep_rows) + 1,
            run_ids.get((rec["source_db"], r.get("news_run_id"))),
            rec["source_db"], r["id"], r.get("backend"), r.get("model"),
            r.get("items"), r.get("span"), classify_origin(r.get("source_api")),
            r.get("timeline"), r.get("report"), r.get("created_at"),
            1 if (rec["source_db"], r["id"]) in prim_report else 0,
        ))

    # ---- 批次条目数回填 ----
    counts = {}
    for row in item_rows:
        rid = row[2]
        if rid is not None:
            counts[rid] = counts.get(rid, 0) + 1
    run_rows = [tuple(list(r) + [counts.get(r[0], 0)]) for r in run_rows]

    # ---- 关联 ----
    prim_by_uid = {}
    for row in sum_rows:
        if row[8]:
            prim_by_uid[row[1]] = row[6]
    link_rows = []
    for row in item_rows:
        iid, uid, title, body = row[0], row[1], row[6], row[14]
        for lk in build_links(iid, title, prim_by_uid.get(uid), body,
                              chars, inc_keywords):
            if lk not in link_rows:
                link_rows.append(lk)

    # ---- 写入 ----
    if args.dry_run:
        print("\n[dry-run] 未写入数据库。")
        sum_written = len(sum_rows)
    else:
        kb.execute("DELETE FROM maf_item_links")
        kb.execute("DELETE FROM maf_summaries")
        kb.execute("DELETE FROM maf_reports")
        kb.execute("DELETE FROM maf_items")
        kb.execute("DELETE FROM maf_runs")
        kb.executemany(
            "INSERT INTO maf_runs (id, source_db, orig_run_id, subject, origin,"
            " source_api, started_at, finished_at, fetched, inserted, updated,"
            " rejected, ok, note, items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            run_rows)
        kb.executemany(
            "INSERT INTO maf_items (id, uid, run_id, game, kind, post_id, title,"
            " created_at, created_iso, date_label, seq, origin, source_name, url,"
            " body, body_policy, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", item_rows)
        cur = kb.executemany(
            "INSERT OR IGNORE INTO maf_summaries (id, uid, item_id, ordinal,"
            " backend, model, summary, created_at, is_primary)"
            " VALUES (?,?,?,?,?,?,?,?,?)", sum_rows)
        sum_written = cur.rowcount
        kb.executemany(
            "INSERT INTO maf_reports (id, run_id, source_db, orig_id, backend,"
            " model, items, span, origin, timeline, report, created_at, is_primary)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rep_rows)
        kb.executemany(
            "INSERT OR IGNORE INTO maf_item_links (item_id, kind, ref_table,"
            " ref_key, matched_by, confidence) VALUES (?,?,?,?,?,?)", link_rows)
        kb.commit()

    # ---- 汇报 ----
    print("\n导入结果")
    print("  maf_runs        %4d" % len(run_rows))
    print("  maf_items       %4d  （人工投喂 %d / 第三方镜像 %d）"
          % (len(item_rows), sum(1 for r in item_rows if r[11] == ORIGIN_MANUAL),
             sum(1 for r in item_rows if r[11] == ORIGIN_MIRROR)))
    dup = len(sum_rows) - sum_written
    print("  maf_summaries   %4d  （主概括 %d%s）"
          % (sum_written, sum(1 for r in sum_rows if r[8]),
             "；另有 %d 条为同一材料+同一模型的重复概括，已去重" % dup if dup else ""))
    print("  maf_reports     %4d  （主报告 %d）"
          % (len(rep_rows), sum(1 for r in rep_rows if r[12])))
    print("  maf_item_links  %4d  （角色 %d / 版本 %d / 运营事故 %d）" % (
        len(link_rows),
        sum(1 for r in link_rows if r[1] == "角色"),
        sum(1 for r in link_rows if r[1] == "版本"),
        sum(1 for r in link_rows if r[1] == "运营事故")))
    print("                  其中材料里确实出现过的角色 %d 名（知识库共 %d 名）；"
          "没出现的不会被硬挂上去"
          % (len({r[4] for r in link_rows if r[1] == "角色"}), len(chars)))

    print("\n合规口径")
    print("  人工投喂线正文入库: %d 条" % body_stats[ORIGIN_MANUAL])
    if args.include_third_party_body:
        print("  第三方镜像线正文入库: %d 条（已显式打开开关）" % body_stats[ORIGIN_MIRROR])
    else:
        print("  第三方镜像线正文入库: 0 条；主动放弃正文 %d 条，只留标题+日期+出处+派生概括"
              % body_withheld)
    print("  没有主概括的条目: %d" % (len(item_rows) - len(prim_by_uid)))
    print("  没有主报告的来源库: %d"
          % (len(by_db) - len({db for db, _ in prim_report})))

    print("\n下一步: python scripts/build_site.py")
    kb.close()


def _is_same_row(a, b):
    if b is None:
        return False
    return (a.get("backend"), a.get("model"), a.get("summary"),
            a.get("created_at")) == (b.get("backend"), b.get("model"),
                                     b.get("summary"), b.get("created_at"))


if __name__ == "__main__":
    main()
