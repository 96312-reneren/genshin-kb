#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人工投喂版的离线测试 —— 全程不出网。

盯的是四件事：

  1. 日期前缀能不能确定性解析，解析不出来就不猜；
  2. 顺序：入库和读回都按发布时间升序，日期未知的排在最后；
  3. 幂等：同一份材料投两次不会变成两行；
  4. 合规：产物里的声明说的是「使用者本人提供」，代码里再也搜不到网络路径。

跑法：powershell -File run_tests.ps1   /   bash run_tests.sh
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline                                                          # noqa: E402
from content import (DEFAULT_KIND, SOURCE_NAME, UNKNOWN_TS,              # noqa: E402
                     is_known_date, render_created)
from ingest import (DateFiller, date_to_ts, normalize_date, parse_file,   # noqa: E402
                    parse_sources, parse_text, read_text, split_uid, to_docs)
from llm import Backend                                                  # noqa: E402
from news_workflow import (NewsDoc, docs_from_rows, parse_deck_spec,     # noqa: E402
                           render_rhythm, rhythm_stats, run_news_digest)
from safety import no_external_socket                                    # noqa: E402
from store import Store                                                  # noqa: E402

TEXT_DATED = """2026-09-01 4.2 版本更新说明
本次更新调整了若干角色数值，并修复了已知问题。

2026/9/3 周年庆活动开启
登录即可领取奖励。

2026年9月10日
补偿公告
因服务器波动，向全服发放补偿。
"""

TEXT_HEADING = """# 上半月安排
本月上半月共有三场活动。

# 版本预告
下个版本将于十月开启。
"""

TEXT_PLAIN = """这是一段没有任何标记的正文。
它应该被当成一条材料处理。
"""


def stub(name: str = "local-stub") -> Backend:
    return Backend(name=name, kind="stub", client=None, model="stub",
                   base_url="")


def setUpModule():
    global _guard
    _guard = no_external_socket()
    _guard.__enter__()


def tearDownModule():
    _guard.__exit__(None, None, None)


# ─────────────────────────── 日期解析 ───────────────────────────
class TestDateParsing(unittest.TestCase):
    def test_common_chinese_date_formats(self):
        want = "2026-09-01"
        for raw in ("2026-09-01", "2026/9/1", "2026.09.01", "2026年9月1日",
                    "2026年9月1日 10:30", " 2026-09-01 "):
            self.assertEqual(normalize_date(raw), want, raw)

    def test_no_guessing(self):
        for raw in ("", None, "乱写", "九月一日", "2026-13-40", "下周三"):
            self.assertIsNone(normalize_date(raw), str(raw))

    def test_timestamp_is_east_eight(self):
        ts = date_to_ts("2026-09-01")
        self.assertTrue(is_known_date(ts))
        self.assertEqual(render_created(ts), "2026-09-01")

    def test_unknown_date_never_renders_a_bogus_year(self):
        self.assertFalse(is_known_date(UNKNOWN_TS))
        self.assertEqual(render_created(date_to_ts(None)), "日期未知")
        self.assertEqual(date_to_ts(None), UNKNOWN_TS)

# ─────────────────────────── 切条 ───────────────────────────
class TestParsing(unittest.TestCase):
    def test_date_mode_splits_by_date_prefix(self):
        res = parse_text(TEXT_DATED, source="公告.txt", mode="auto")
        self.assertEqual(res.mode_used, "date")
        self.assertEqual([it.date for it in res.items],
                         ["2026-09-01", "2026-09-03", "2026-09-10"])
        self.assertEqual(res.items[0].title, "4.2 版本更新说明")
        self.assertEqual(res.items[2].title, "补偿公告")
        self.assertTrue(all(it.kind == DEFAULT_KIND for it in res.items))
        self.assertEqual(res.warnings, [])

    def test_heading_mode(self):
        res = parse_text(TEXT_HEADING, source="安排.md", mode="auto")
        self.assertEqual(res.mode_used, "heading")
        self.assertEqual([it.title for it in res.items],
                         ["上半月安排", "版本预告"])

    def test_whole_mode_and_the_warning(self):
        res = parse_text(TEXT_PLAIN, source="手记.txt", mode="auto")
        self.assertEqual(res.mode_used, "whole")
        self.assertEqual(len(res.items), 1)
        self.assertTrue(any("没有日期前缀" in w for w in res.warnings),
                        res.warnings)

    def test_explicit_mode_is_respected(self):
        res = parse_text(TEXT_DATED, source="x.txt", mode="whole")
        self.assertEqual(res.mode_used, "whole")
        self.assertEqual(len(res.items), 1)

    def test_two_sources_merge_and_get_renumbered(self):
        res = parse_sources([
            {"name": "a.txt", "content": "2026-09-02 第二条"},
            {"name": "b.txt", "content": "2026-09-01 第一条"},
        ])
        self.assertEqual([it.ordinal for it in res.items], [1, 2])
        self.assertEqual([it.source for it in res.items], ["a.txt", "b.txt"])
        docs = to_docs(res.items)
        self.assertEqual([d.title for d in docs], ["第一条", "第二条"])

    def test_gbk_file_is_read_and_parsed(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "gbk.txt"
        p.write_bytes("2026-09-01 标题\n正文".encode("gb18030"))
        self.assertIn("标题", read_text(p))
        self.assertEqual(parse_file(p).items[0].date, "2026-09-01")


# ─────────────────────────── 指纹与顺序 ───────────────────────────
class TestIdentityAndOrder(unittest.TestCase):
    def test_same_text_twice_is_the_same_row(self):
        a = to_docs(parse_text(TEXT_DATED, source="x.txt").items)
        b = to_docs(parse_text(TEXT_DATED, source="换了个文件名.txt").items)
        self.assertEqual([d.uid for d in a], [d.uid for d in b])

    def test_changed_body_changes_the_uid(self):
        a = to_docs(parse_text(TEXT_DATED, source="x.txt").items)
        other = TEXT_DATED.replace("修复了已知问题", "修复了另一个问题")
        b = to_docs(parse_text(other, source="x.txt").items)
        self.assertNotEqual(a[0].uid, b[0].uid)

    def test_docs_are_sorted_and_unknown_goes_last(self):
        items = parse_sources([
            {"name": "a.txt", "content": "没有日期的一段"},
            {"name": "b.txt", "content": "2026-09-02 有日期的一段"},
        ]).items
        docs = to_docs(items)
        self.assertEqual(docs[0].title, "有日期的一段")
        self.assertTrue(is_known_date(docs[0].created_at))
        self.assertEqual(docs[-1].created_at, UNKNOWN_TS)
        self.assertEqual(docs[-1].created_iso, "")
        self.assertEqual(docs[-1].date_cn, "日期未知")

    def test_to_row_has_every_column_the_store_needs(self):
        row = to_docs(parse_text(TEXT_DATED, source="材料.txt").items)[0].to_row()
        for key in ("uid", "game", "kind", "post_id", "title", "description",
                    "url", "banner", "created_at", "created_iso", "source_api",
                    "source_name", "content_hash", "fetched_at"):
            self.assertIn(key, row, key)
        self.assertEqual(row["url"], "")
        self.assertEqual(row["source_name"], "材料.txt")
        self.assertEqual(row["source_api"], "manual")
        self.assertTrue(split_uid(row["uid"]))


# ─────────────────────────── 顺序存储 ───────────────────────────
class TestOrderedStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "n.db")

    def tearDown(self):
        self.store.close()

    def _docs(self, text: str = TEXT_DATED):
        return to_docs(parse_text(text, source="材料.txt").items)

    def _put(self, docs):
        return self.store.upsert_news([d.to_row() for d in docs])

    def test_read_back_is_chronological(self):
        docs = to_docs(parse_sources([
            {"name": "a.txt", "content": "2026-09-02 第二条"},
            {"name": "b.txt", "content": "2026-09-01 第一条"},
            {"name": "c.txt", "content": "2026-09-03 第三条"},
        ]).items)
        self._put(docs)
        rows = list(self.store.iter_news())
        ts = [r["created_at"] for r in rows]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([r["title"] for r in rows],
                         ["第一条", "第二条", "第三条"])

    def test_upsert_is_idempotent_and_keeps_first_seen(self):
        docs = self._docs()
        first = self._put(docs)
        self.assertEqual((first["inserted"], first["updated"]), (3, 0))
        before = list(self.store.iter_news())[0]["first_seen_at"]
        again = self._put(docs)
        self.assertEqual((again["inserted"], again["updated"]), (0, 3))
        self.assertEqual(self.store.news_stats()["总数"], 3)
        self.assertEqual(list(self.store.iter_news())[0]["first_seen_at"], before)

    def test_audit_columns_and_unknown_count(self):
        docs = self._docs() + to_docs(parse_text(TEXT_PLAIN,
                                                 source="手记.txt").items)
        self._put(docs)
        st = self.store.news_stats()
        self.assertEqual(st["缺正文"], 0)
        self.assertEqual(st["缺来源标记"], 0)
        self.assertEqual(st["缺载入时间"], 0)
        self.assertEqual(st["日期未知"], 1)
        self.assertEqual(st["总数"], 4)
        self.assertTrue(is_known_date(st["最新"]),
                        "最新不该被「日期未知」的占位时间戳顶掉")

    def test_kind_filter_and_limit(self):
        self._put(self._docs())
        self.assertEqual(len(list(self.store.iter_news(limit=2))), 2)
        self.assertEqual(len(list(self.store.iter_news(kind="notices"))), 3)
        self.assertEqual(len(list(self.store.iter_news(kind="events"))), 0)


# ─────────────────────────── 产物 ───────────────────────────
class TestDigestOutput(unittest.TestCase):
    def _docs(self):
        return to_docs(parse_sources([
            {"name": "甲.txt", "content": "2026-09-01 第一条\n正文一"},
            {"name": "乙.txt", "content": "2026-09-02 第二条\n正文二"},
        ]).items)

    def _digest(self):
        return asyncio.run(run_news_digest(self._docs(), stub(), dry_run=True,
                                          concurrency=2, subject="原神"))

    def test_docs_from_rows_sorts_even_if_rows_are_shuffled(self):
        rows = [{"uid": f"u{i}", "title": "t", "description": "d",
                 "created_at": 100 + i, "created_iso": "", "url": "",
                 "source_api": "manual", "source_name": "m", "fetched_at": "f",
                 "game": "原神", "kind": "notices"} for i in (3, 1, 2)]
        self.assertEqual([d.uid for d in docs_from_rows(rows)],
                         ["u1", "u2", "u3"])

    def test_nodes_are_chronological_and_renumbered(self):
        d = self._digest()
        self.assertEqual([n.ordinal for n in d.nodes], [1, 2])
        self.assertEqual([n.title for n in d.nodes], ["第一条", "第二条"])

    def test_timeline_says_where_the_material_came_from(self):
        md = self._digest().timeline_md
        for token in ("数据来源", "出处", "类别", "发布时间", "载入时间", "UTC+8"):
            self.assertIn(token, md, token)
        self.assertNotIn("接口", md)
        self.assertNotIn("http", md)
        self.assertIn("使用者本人提供", md)
        self.assertLess(md.index("第一条"), md.index("第二条"))

    def test_report_has_compliance_notice_and_source_list(self):
        md = self._digest().report_md
        self.assertIn("完整出处清单", md)
        self.assertIn("合规声明", md)
        self.assertIn("本地模型", md)
        self.assertIn("出处：甲.txt", md)
        self.assertIn("不访问任何网络接口", md)
        self.assertNotIn("公开接口", md)

    def test_empty_batch_raises(self):
        with self.assertRaises(Exception):
            asyncio.run(run_news_digest([], stub(), dry_run=True))


# ─────────────────────────── 节奏统计 ───────────────────────────
class TestRhythmStats(unittest.TestCase):
    @staticmethod
    def _node(ordinal: int, date: str, kind: str = "notices"):
        from news_workflow import TimelineNode
        return TimelineNode(ordinal=ordinal, uid=f"u{ordinal}", date=date,
                            datetime=f"{date} 00:00", created_at=date_to_ts(date),
                            title=f"第{ordinal}条", url="", source_api="manual",
                            source_name="m", fetched_at="2026-10-06T00:00:00+08:00",
                            summary="s", kind=kind)

    def test_counts_and_the_unknown_split(self):
        nodes = [self._node(1, "2026-09-01"), self._node(2, "2026-09-08"),
                 self._node(3, "2026-09-09"), self._node(4, "日期未知")]
        st = rhythm_stats(nodes)
        # count 是条目总数（含日期未知）；unknown 单独计数
        self.assertEqual(st["count"], 4)
        self.assertEqual(st["unknown"], 1)
        self.assertEqual(st["by_month"], {"2026-09": 3})
        self.assertEqual(st["span_days"], 8)
        self.assertEqual(st["max_gap"], 7)
        self.assertAlmostEqual(st["per_week"], 2.63, delta=0.05)
        self.assertEqual(len(st["gaps"]), 2)

    def test_render_says_the_numbers_are_not_model_made(self):
        nodes = [self._node(1, "2026-09-01"), self._node(2, "日期未知")]
        text = "\n".join(render_rhythm(rhythm_stats(nodes)))
        self.assertIn("不计入", text)
        self.assertIn("日期未知 1 条", text)
        self.assertIn("非模型生成", text)

    def test_empty_is_safe(self):
        # 空输入：stats 为空字典，渲染为空列表，都不许抛异常
        st = rhythm_stats([])
        self.assertEqual(st, {})
        self.assertEqual(render_rhythm(st), [])

# ─────────────────────────── 缺日期的兜底 ───────────────────────────
class TestDateFiller(unittest.TestCase):
    def test_without_a_model_it_stays_unknown(self):
        items = parse_text(TEXT_PLAIN, source="手记.txt", mode="whole").items
        info = asyncio.run(DateFiller(None).fill(items))
        self.assertEqual(info, {"asked": 0, "resolved": 0, "unknown": 1})
        self.assertIsNone(items[0].date)
        self.assertEqual(items[0].date_source, "unknown")

    def test_dated_items_are_never_sent_to_the_model(self):
        items = parse_text(TEXT_DATED, source="公告.txt").items
        info = asyncio.run(DateFiller(None).fill(items))
        self.assertEqual(info["asked"], 0)
        self.assertEqual([it.date for it in items],
                         ["2026-09-01", "2026-09-03", "2026-09-10"])


# ─────────────────────────── 命令行 ───────────────────────────
class TestCliOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "n.db"
        self.out = self.tmp / "news"

    def test_ingest_then_report(self):
        self.assertEqual(pipeline.main(["ingest", "--db", str(self.db),
                                        "--text", TEXT_DATED, "--no-fill"]), 0)
        with Store(self.db) as store:
            self.assertEqual(store.news_stats()["总数"], 3)
            ts = [r["created_at"] for r in store.iter_news()]
            self.assertEqual(ts, sorted(ts))
            runs = store.conn.execute(
                "SELECT note FROM news_runs ORDER BY news_run_id").fetchall()
            self.assertTrue(runs and runs[0]["note"].startswith("ingest/"))

        self.assertEqual(pipeline.main(["news-report", "--db", str(self.db),
                                        "--out-dir", str(self.out),
                                        "--dry-run"]), 0)
        for name in ("report.md", "timeline.md", "news_digest.json"):
            self.assertTrue((self.out / name).exists(), name)
        timeline = (self.out / "timeline.md").read_text(encoding="utf-8")
        self.assertLess(timeline.index("4.2 版本更新说明"),
                        timeline.index("补偿公告"))
        self.assertIn("合规声明",
                      (self.out / "report.md").read_text(encoding="utf-8"))
        payload = json.loads((self.out / "news_digest.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["items"], 3)
        self.assertEqual([n["ordinal"] for n in payload["timeline"]], [1, 2, 3])
        self.assertEqual(payload["subject"], "原神")
        self.assertTrue(payload["source_files"])

    def test_ingest_without_input_fails_cleanly(self):
        self.assertEqual(pipeline.main(["ingest", "--db", str(self.db)]), 1)

    def test_a_report_with_only_unknown_dates_still_renders(self):
        self.assertEqual(pipeline.main(["ingest", "--db", str(self.db),
                                        "--text", TEXT_PLAIN, "--no-fill"]), 0)
        with Store(self.db) as store:
            self.assertEqual(store.news_stats()["日期未知"], 1)
        self.assertEqual(pipeline.main(["news-report", "--db", str(self.db),
                                        "--out-dir", str(self.out),
                                        "--dry-run"]), 0)
        timeline = (self.out / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("日期未知", timeline)


# ─────────────────────────── 幻灯片文案 ───────────────────────────
class TestDeckSpec(unittest.TestCase):
    GOOD = ('{"deck_title": "标题", "deck_subtitle": "副标题", "sections": '
            '[{"heading": "概况", "bullets": ["要点一", "要点二"]}]}')

    def test_parses_plain_json(self):
        spec = parse_deck_spec(self.GOOD)
        self.assertEqual(spec["deck_title"], "标题")
        self.assertEqual(spec["sections"][0]["bullets"], ["要点一", "要点二"])

    def test_strips_markdown_fence(self):
        raw = "```json\n" + self.GOOD + "\n```"
        self.assertEqual(parse_deck_spec(raw)["deck_title"], "标题")

    def test_tolerates_leading_chatter(self):
        spec = parse_deck_spec("好的，这是要点：\n" + self.GOOD)
        self.assertEqual(spec["deck_title"], "标题")

    def test_rejects_garbage(self):
        self.assertIsNone(parse_deck_spec("完全不是 JSON"))

    def test_drops_sections_without_bullets(self):
        raw = ('{"deck_title": "t", "sections": [{"heading": "空", "bullets": []},'
               ' {"heading": "有", "bullets": ["x"]}]}')
        spec = parse_deck_spec(raw)
        self.assertEqual([s["heading"] for s in spec["sections"]], ["有"])


# ─────────────────────────── 合规 ───────────────────────────
class TestCompliance(unittest.TestCase):
    def test_renderer_has_no_network_path(self):
        src = (ROOT / "render_office.py").read_text(encoding="utf-8")
        for token in ("urllib", "requests", "http.client", "socket", "urlopen",
                      "http:", "https:"):
            self.assertNotIn(token, src, token)

    def test_workflow_has_no_network_imports(self):
        src = (ROOT / "news_workflow.py").read_text(encoding="utf-8")
        for token in ("urllib", "requests", "http.client", "socket"):
            self.assertNotIn(token, src, token)

    def test_api_layer_and_its_image_host_are_gone(self):
        self.assertFalse((ROOT / "hoyo_api.py").exists())
        self.assertFalse((ROOT / "assets.py").exists())
        bad = ("ennead", "hoyoverse-api", "upload-os-bbs", "hoyolab.com/upload")
        for path in sorted(ROOT.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in bad:
                self.assertNotIn(token, text, f"{path.name}: {token}")

    def test_one_click_scripts_exist(self):
        for name in ("run_all.ps1", "run_all.bat"):
            self.assertTrue((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
