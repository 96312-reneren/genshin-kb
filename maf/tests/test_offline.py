#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试套件 —— 全程锁死在回环，一个字节都不会出网。

防护是三层的：
  1. 整个模块的测试都跑在 no_external_socket() 里，socket.getaddrinfo /
     socket.create_connection 对非回环目标直接抛异常；
  2. 爬虫自身带 NetworkGuard；
  3. 假站点只监听 127.0.0.1，测试断言 /private/hidden.html 从未被请求。

跑法：bash run_tests.sh      （或 /root/venvs/maf/bin/python -m unittest discover）
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_site                                                     # noqa: E402
import pipeline                                                      # noqa: E402
from crawler import Crawler, Fetcher, Page, canonicalize, extract_html  # noqa: E402
from llm import Backend                                              # noqa: E402
from robots import RobotsPolicy                                      # noqa: E402
from safety import NetworkGuard, NetworkGuardError, no_external_socket  # noqa: E402
from store import Store                                              # noqa: E402
from workflow import Doc, run_digest                                 # noqa: E402

UA = "DSHA-PoliteCrawler/1.0"
_GUARD = None


def setUpModule():
    """整个测试模块期间，socket 层只允许回环。"""
    global _GUARD
    _GUARD = no_external_socket()
    _GUARD.__enter__()


def tearDownModule():
    _GUARD.__exit__(None, None, None)


def stub(name: str) -> Backend:
    return Backend(name=f"{name}-stub", kind="stub", client=None, model="stub", base_url="")


# ─────────────────────────── robots.txt ───────────────────────────
class TestRobots(unittest.TestCase):
    def policy(self, text: str, status: int | None = 200, ua: str | None = None) -> RobotsPolicy:
        return RobotsPolicy(status_code=status, text=text, user_agent=ua or UA)

    def test_basic_disallow(self):
        p = self.policy("User-agent: *\nDisallow: /private/\n")
        self.assertFalse(p.can_fetch("http://h/private/x"))
        self.assertTrue(p.can_fetch("http://h/public"))

    def test_allow_longest_match_wins(self):
        p = self.policy("User-agent: *\nDisallow: /private/\nAllow: /private/public.html\n")
        self.assertFalse(p.can_fetch("http://h/private/hidden.html"))
        self.assertTrue(p.can_fetch("http://h/private/public.html"))

    def test_allow_wins_on_tie(self):
        p = self.policy("User-agent: *\nDisallow: /a\nAllow: /a\n")
        self.assertTrue(p.can_fetch("http://h/a"))

    def test_dollar_anchor(self):
        p = self.policy("User-agent: *\nDisallow: /*.php$\n")
        self.assertFalse(p.can_fetch("http://h/x.php"))
        self.assertTrue(p.can_fetch("http://h/x.php?y=1"))

    def test_wildcard(self):
        p = self.policy("User-agent: *\nDisallow: /*/secret/\n")
        self.assertFalse(p.can_fetch("http://h/a/b/secret/x"))
        self.assertTrue(p.can_fetch("http://h/a/b/open/x"))

    def test_specific_agent_beats_wildcard(self):
        txt = "User-agent: *\nDisallow: /\n\nUser-agent: DSHA-PoliteCrawler\nDisallow: /nope\n"
        p = self.policy(txt)
        self.assertTrue(p.can_fetch("http://h/ok"))
        self.assertFalse(p.can_fetch("http://h/nope"))

    def test_same_agent_groups_are_merged(self):
        txt = ("User-agent: DSHA-PoliteCrawler\nDisallow: /a\n\n"
               "User-agent: DSHA-PoliteCrawler\nDisallow: /b\n")
        p = self.policy(txt)
        self.assertFalse(p.can_fetch("http://h/a"))
        self.assertFalse(p.can_fetch("http://h/b"))

    def test_404_allows_all(self):
        p = self.policy("", status=404)
        self.assertEqual(p.allowed, "allow-all")
        self.assertTrue(p.can_fetch("http://h/anything"))

    def test_5xx_denies_all(self):
        p = self.policy("", status=503)
        self.assertEqual(p.allowed, "deny-all")
        self.assertFalse(p.can_fetch("http://h/anything"))

    def test_unreachable_denies_all(self):
        p = self.policy("", status=None)
        self.assertEqual(p.allowed, "deny-all")
        self.assertFalse(p.can_fetch("http://h/x"))

    def test_empty_disallow_means_no_limit(self):
        p = self.policy("User-agent: *\nDisallow:\n")
        self.assertTrue(p.can_fetch("http://h/x"))

    def test_crawl_delay_parsed(self):
        p = self.policy("User-agent: *\nCrawl-delay: 1.5\nDisallow:\n")
        self.assertEqual(p.crawl_delay, 1.5)

    def test_sitemap_parsed(self):
        p = self.policy("Sitemap: http://h/sitemap.xml\nUser-agent: *\nDisallow:\n")
        self.assertIn("http://h/sitemap.xml", p.sitemaps)


# ─────────────────────────── 网络护栏 ───────────────────────────
class TestNetworkGuard(unittest.TestCase):
    def test_loopback_only_rejects_external(self):
        g = NetworkGuard(loopback_only=True)
        with self.assertRaises(NetworkGuardError):
            g.check_url("http://example.com/")
        self.assertEqual(g.blocked_count, 1)

    def test_bad_scheme_rejected(self):
        g = NetworkGuard(loopback_only=True)
        with self.assertRaises(NetworkGuardError):
            g.check_url("file:///etc/passwd")

    def test_allowed_hosts_enforced(self):
        g = NetworkGuard(allowed_hosts={"127.0.0.1"})
        self.assertTrue(g.is_allowed("http://127.0.0.1/x"))
        self.assertFalse(g.is_allowed("http://localhost/x"))

    def test_is_allowed_is_silent(self):
        g = NetworkGuard(loopback_only=True)
        self.assertFalse(g.is_allowed("http://example.com/"))
        self.assertEqual((g.blocked_count, len(g.allowed)), (0, 0))

    def test_socket_layer_actually_blocks_dns(self):
        """证明离线保险真的生效 —— 就算绕过 guard，也解析不了外网。"""
        with self.assertRaises(NetworkGuardError):
            socket.getaddrinfo("example.com", 80)
        with self.assertRaises(NetworkGuardError):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        # 回环必须仍然能用，否则测试自己就跑不起来
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))


# ─────────────────────────── 解析工具 ───────────────────────────
class TestParsing(unittest.TestCase):
    def test_canonicalize(self):
        self.assertEqual(canonicalize("HTTP://Example.COM:80/a#frag"), "http://example.com/a")
        self.assertEqual(canonicalize("https://a.b:8443/x?y=1"), "https://a.b:8443/x?y=1")

    def test_extract_html_skips_script_and_style(self):
        html = ("<html><head><title>标题</title><style>.x{}</style>"
                "<script>var secret='不该出现';</script></head>"
                "<body><h1>大标题</h1><p>正文一</p><a href='/next'>走</a></body></html>")
        title, text, links = extract_html(html, "http://h/base/")
        self.assertEqual(title, "标题")
        self.assertIn("正文一", text)
        self.assertNotIn("不该出现", text)
        self.assertNotIn(".x{}", text)
        self.assertIn("http://h/next", links)


# ─────────────────────────── 爬虫 ───────────────────────────
class TestCrawlerOffline(unittest.TestCase):
    def _crawl(self, **kwargs):
        ctx = fake_site.serve()
        base, requests, _srv = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        params = dict(max_pages=25, max_depth=1, delay=0.0)
        params.update(kwargs)
        crawler = Crawler([base + "/"], guard=NetworkGuard(loopback_only=True), **params)
        start = time.monotonic()
        pages = crawler.crawl()
        return base, requests, crawler, pages, time.monotonic() - start

    def test_robots_disallowed_page_never_requested(self):
        base, requests, crawler, pages, _ = self._crawl()
        self.assertNotIn("/private/hidden.html", requests)
        skipped = [p.url for p in crawler.skipped]
        self.assertTrue(any(u.endswith("/private/hidden.html") for u in skipped),
                        f"被 robots 拒绝的 URL 应留档，实际 skipped={skipped}")

    def test_allow_exception_is_fetched(self):
        base, requests, crawler, pages, _ = self._crawl()
        self.assertIn("/private/public.html", requests)
        self.assertTrue(any(p.url.endswith("/private/public.html") for p in pages))

    def test_only_crawls_loopback(self):
        base, requests, crawler, pages, _ = self._crawl()
        for page in pages:
            self.assertTrue(page.url.startswith(base), f"跑到站外了：{page.url}")
        self.assertFalse(any("example.com" in p.url for p in pages))

    def test_text_extracted(self):
        base, requests, crawler, pages, _ = self._crawl()
        self.assertGreaterEqual(len(pages), fake_site.PAGE_COUNT)
        sample = next(p for p in pages if p.url.endswith("/page/1.html"))
        self.assertIn("离线测试主题 1", sample.text)
        self.assertNotIn("这段脚本不该出现在正文里", sample.text)
        self.assertEqual(sample.title, "第 1 页")

    def test_crawl_delay_is_honored(self):
        base, requests, crawler, pages, elapsed = self._crawl()
        policy = crawler.policy_for(base + "/")
        self.assertEqual(policy.crawl_delay, 0.05)
        expected = 0.05 * (len(pages) - 1) * 0.8
        self.assertGreaterEqual(elapsed, expected,
                                f"没有遵守 Crawl-delay：{elapsed:.2f}s < {expected:.2f}s")

    def test_max_pages_respected(self):
        base, requests, crawler, pages, _ = self._crawl(max_pages=4)
        self.assertLessEqual(len(pages), 4)

    def test_robots_json_is_not_parsed_as_page(self):
        base, requests, crawler, pages, _ = self._crawl()
        self.assertTrue(any("/robots.txt" in r for r in requests))


# ─────────────────────────── 中转层 ───────────────────────────
class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "t.db")

    def tearDown(self):
        self.store.close()

    @staticmethod
    def page(url: str, text: str = "正文", status: int = 200) -> Page:
        return Page(url=url, depth=0, status=status, title="标题", text=text,
                    content_hash="abc", fetched_at="2026-01-01T00:00:00Z")

    def test_upsert_is_idempotent(self):
        self.store.upsert_pages([self.page("http://127.0.0.1/a"), self.page("http://127.0.0.1/a", "改了")])
        rows = list(self.store.iter_pages())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "改了")

    def test_robots_denied_rows_are_kept_for_audit(self):
        denied = Page(url="http://127.0.0.1/secret", robots_allowed=False, note="robots.txt 禁止")
        self.store.upsert_pages([self.page("http://127.0.0.1/ok"), denied])
        self.assertEqual(self.store.stats()["被robots拒绝"], 1)
        self.assertEqual(len(list(self.store.iter_pages())), 1)      # 但不会被喂给工作流

    def test_export_json_roundtrip(self):
        self.store.upsert_pages([self.page("http://127.0.0.1/a")])
        path = self.store.export_json(self.tmp / "pages.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["url"], "http://127.0.0.1/a")
        self.assertIn("stats", payload)

    def test_digest_saved_and_read_back(self):
        self.store.save_digest(text="总览正文", threshold=10, condensed=True,
                               backend="cloud", model="m", segments=12)
        row = self.store.latest_digest()
        self.assertEqual(row["text"], "总览正文")
        self.assertEqual(row["condensed"], 1)


# ─────────────────────────── 工作流 ───────────────────────────
class TestWorkflow(unittest.TestCase):
    @staticmethod
    def docs(n: int) -> list[Doc]:
        return [Doc(url=f"http://127.0.0.1/page/{i}.html", title=f"第 {i} 页",
                    text=f"第 {i} 页正文。" * 20) for i in range(1, n + 1)]

    def test_condense_branch(self):
        d = asyncio.run(run_digest(self.docs(12), stub("local"), stub("cloud"),
                                   threshold=10, dry_run=True))
        self.assertTrue(d.condensed)
        self.assertEqual(len(d.summaries), 12)
        self.assertIn("总览", d.markdown)
        self.assertEqual(d.condenser_backend, "cloud-stub")

    def test_plain_branch(self):
        d = asyncio.run(run_digest(self.docs(3), stub("local"), stub("cloud"),
                                   threshold=10, dry_run=True))
        self.assertFalse(d.condensed)
        self.assertEqual(len(d.summaries), 3)
        self.assertNotIn("总览", d.markdown)

    def test_backends_are_recorded_per_stage(self):
        d = asyncio.run(run_digest(self.docs(11), stub("local"), stub("cloud"),
                                   threshold=10, dry_run=True))
        payload = d.to_dict()
        self.assertEqual(payload["summarizer"]["backend"], "local-stub")
        self.assertEqual(payload["condenser"]["backend"], "cloud-stub")
        self.assertEqual(payload["pages"], 11)

    def test_empty_batch_raises(self):
        with self.assertRaises(Exception):
            asyncio.run(run_digest([], stub("local"), stub("cloud"), dry_run=True))


# ─────────────────────────── 端到端 ───────────────────────────
class TestEndToEndOffline(unittest.TestCase):
    def test_cli_crawl_then_digest(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "crawl.db"
        out = tmp / "out"

        with fake_site.serve() as (base, requests, _srv):
            rc = pipeline.main(["crawl", "--seed", base + "/", "--db", str(db),
                                "--max-pages", "20", "--max-depth", "1", "--delay", "0"])
            self.assertEqual(rc, 0)
            self.assertNotIn("/private/hidden.html", requests)

            rc = pipeline.main(["digest", "--db", str(db), "--out-dir", str(out),
                                "--dry-run", "--threshold", "10"])
            self.assertEqual(rc, 0)

        self.assertTrue((out / "summary.md").exists())
        self.assertTrue((out / "digest.json").exists())
        self.assertTrue((out / "pages.json").exists())

        digest = json.loads((out / "digest.json").read_text(encoding="utf-8"))
        self.assertTrue(digest["condensed"], "12 页应该触发精简分支")
        self.assertGreaterEqual(digest["pages"], 12)

        # 中转库要能回答「你到底爬了什么、没爬什么」
        with Store(db) as store:
            stats = store.stats()
            self.assertGreaterEqual(stats["HTTP_OK"], 12)
            self.assertGreaterEqual(stats["被robots拒绝"], 1)
            self.assertIsNotNone(store.latest_digest())
            summaries = store.conn.execute("SELECT COUNT(*) c FROM summaries").fetchone()["c"]
            self.assertGreaterEqual(summaries, 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
