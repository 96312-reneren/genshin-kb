#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
礼貌爬虫 —— 每一步都先过 robots.txt，再过网络护栏。

设计要点：
  * 只抓 http(s)，先校验护栏（主机白名单 / loopback / 私网拒绝）
  * 每个 origin 只取一次 robots.txt 并缓存；4xx 放行、5xx 或取不到则整站禁抓
  * 遵守 Crawl-delay 与自定义最小间隔（同主机串行限速）
  * 重定向逐跳校验护栏，防止被 302 骗到白名单外
  * 被 robots 拒绝的 URL 也会留档（robots_allowed=0），方便审计「没抓什么」
"""
from __future__ import annotations

import gzip
import hashlib
import re
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from robots import RobotsPolicy
from safety import NetworkGuard, NetworkGuardError

__all__ = ["Page", "FetchResult", "Fetcher", "Crawler", "canonicalize", "extract_html",
           "DEFAULT_USER_AGENT"]

DEFAULT_USER_AGENT = "DSHA-PoliteCrawler/1.0 (+robots.txt compliant; offline-testable)"

# 明显不是 HTML 文档的后缀，不跟
_SKIP_EXT = (".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
             ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
             ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
             ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf",
             ".exe", ".apk", ".dmg", ".iso", ".bin")


# ─────────────────────────── URL 规范化 ───────────────────────────
def canonicalize(url: str) -> str:
    """去 fragment、统一大小写、去掉默认端口。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


# ─────────────────────────── HTML 解析 ───────────────────────────
class _HTMLExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
              "footer", "blockquote", "pre", "form", "nav", "aside", "main", "hr"}
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_body = False

    def handle_starttag(self, tag: str, attrs) -> None:
        mapping = dict(attrs)
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "body":
            self._in_body = True
        elif tag == "a":
            href = (mapping.get("href") or "").strip()
            if href:
                self.links.append(href)
        if tag in self._BLOCK:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_body:
            self.text_parts.append(data)


def extract_html(html_text: str, base_url: str) -> tuple[str, str, list[str]]:
    """返回 (标题, 正文纯文本, 绝对链接列表)。"""
    ex = _HTMLExtractor(base_url)
    try:
        ex.feed(html_text)
        ex.close()
    except Exception:
        pass                                     # 畸形 HTML 尽量拿到多少算多少

    title = " ".join("".join(ex.title_parts).split())
    raw = "".join(ex.text_parts)
    lines = [re.sub(r"[ \t\u00a0\u200b]+", " ", ln).strip() for ln in raw.split("\n")]
    text = "\n".join(ln for ln in lines if ln)

    links: list[str] = []
    for href in ex.links:
        low = href.lower()
        if low.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        links.append(canonicalize(urljoin(base_url, href)))
    return title, text, links


# ─────────────────────────── 抓取 ───────────────────────────
@dataclass
class FetchResult:
    url: str
    status: int | None = None
    content_type: str = ""
    body: str | None = None
    error: str | None = None
    elapsed: float = 0.0
    final_url: str = ""
    redirects: int = 0


@dataclass
class Page:
    url: str
    depth: int = 0
    status: int | None = None
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    content_type: str = ""
    fetched_at: str = ""
    error: str | None = None
    robots_allowed: bool = True
    content_hash: str = ""
    note: str = ""

    @property
    def text_len(self) -> int:
        return len(self.text)


class _Redirected(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


class _NoRedirect(HTTPRedirectHandler):
    """禁止 urllib 自动跟随跳转，改由我们逐跳校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _Redirected(newurl)


class Fetcher:
    """单次 HTTP 抓取；每次请求前过护栏，重定向逐跳校验。"""

    def __init__(self, guard: NetworkGuard, *, user_agent: str = DEFAULT_USER_AGENT,
                 timeout: float = 10.0, max_bytes: int = 2_000_000,
                 max_redirects: int = 5) -> None:
        self.guard = guard
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._opener = build_opener(_NoRedirect)

    def fetch(self, url: str) -> FetchResult:
        start = time.monotonic()
        current = url
        hops = 0
        for _ in range(self.max_redirects + 1):
            self.guard.check_url(current)          # 可能抛 NetworkGuardError
            req = Request(current, headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            })
            try:
                resp = self._opener.open(req, timeout=self.timeout)
            except _Redirected as redir:
                hops += 1
                current = canonicalize(urljoin(current, redir.url))
                continue
            except HTTPError as exc:               # 4xx/5xx 也是有效交互，要记账
                body = None
                try:
                    body = self._decode(exc.read(self.max_bytes), exc.headers)
                except Exception:
                    pass
                return FetchResult(url=url, status=exc.code,
                                   content_type=(exc.headers or {}).get("Content-Type", ""),
                                   body=body, elapsed=time.monotonic() - start,
                                   final_url=current, redirects=hops)
            except (URLError, OSError) as exc:
                return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}",
                                   elapsed=time.monotonic() - start, final_url=current,
                                   redirects=hops)
            with resp:
                raw = resp.read(self.max_bytes)
                body = self._decode(raw, resp.headers)
                return FetchResult(url=url, status=resp.status,
                                   content_type=resp.headers.get("Content-Type", ""),
                                   body=body, elapsed=time.monotonic() - start,
                                   final_url=resp.geturl(), redirects=hops)
        return FetchResult(url=url, error=f"重定向超过 {self.max_redirects} 跳",
                           elapsed=time.monotonic() - start, final_url=current,
                           redirects=hops)

    def _decode(self, raw: bytes, headers) -> str:
        encoding = (headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
        raw = raw[: self.max_bytes * 8]
        charset = "utf-8"
        match = re.search(r"charset=[\"']?([\w\-]+)", headers.get("Content-Type") or "", re.I)
        if match:
            charset = match.group(1)
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


# ─────────────────────────── 爬虫 ───────────────────────────
class Crawler:
    def __init__(self, seeds, *, guard: NetworkGuard,
                 user_agent: str = DEFAULT_USER_AGENT,
                 max_pages: int = 50, max_depth: int = 2,
                 delay: float = 1.0, same_host: bool = True,
                 timeout: float = 10.0, max_bytes: int = 2_000_000,
                 respect_crawl_delay: bool = True,
                 fetcher: Fetcher | None = None,
                 on_event=None) -> None:
        self.seeds = [canonicalize(s) for s in seeds]
        self.guard = guard
        self.user_agent = user_agent
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.delay = delay
        self.same_host = same_host
        self.respect_crawl_delay = respect_crawl_delay
        self.fetcher = fetcher or Fetcher(guard, user_agent=user_agent,
                                          timeout=timeout, max_bytes=max_bytes)
        self.on_event = on_event
        self.pages: list[Page] = []
        self.skipped: list[Page] = []
        self.robots_cache: dict[str, RobotsPolicy] = {}
        self._last_hit: dict[str, float] = {}
        self._seen: set[str] = set()

    # ── 事件日志 ────────────────────────────────────────────
    def _emit(self, kind: str, message: str) -> None:
        if self.on_event:
            self.on_event(kind, message)

    # ── robots ─────────────────────────────────────────────
    def _robots_url(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/robots.txt"

    def policy_for(self, url: str) -> RobotsPolicy:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self.robots_cache:
            return self.robots_cache[origin]
        robots_url = self._robots_url(url)
        try:
            result = self.fetcher.fetch(robots_url)
            policy = RobotsPolicy(status_code=result.status, text=result.body or "",
                                  user_agent=self.user_agent, source=robots_url)
            self._emit("robots", f"{robots_url} -> {policy}")
        except NetworkGuardError as exc:
            policy = RobotsPolicy(status_code=None, user_agent=self.user_agent,
                                  source=robots_url)
            self._emit("robots", f"{robots_url} 被护栏拦截，按禁止处理：{exc}")
        except Exception as exc:                    # noqa: BLE001
            policy = RobotsPolicy(status_code=None, user_agent=self.user_agent,
                                  source=robots_url)
            self._emit("robots", f"{robots_url} 取不到，按禁止处理：{exc}")
        self.robots_cache[origin] = policy
        return policy

    # ── 限速 ───────────────────────────────────────────────
    def _wait_turn(self, url: str, policy: RobotsPolicy | None) -> None:
        host = urlsplit(url).netloc
        want = self.delay
        if self.respect_crawl_delay and policy and policy.crawl_delay:
            want = max(want, policy.crawl_delay)
        last = self._last_hit.get(host)
        if last is not None:
            gap = want - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
        self._last_hit[host] = time.monotonic()

    # ── 主循环 ─────────────────────────────────────────────
    def crawl(self) -> list[Page]:
        queue: deque[tuple[str, int]] = deque((s, 0) for s in self.seeds)
        allowed_hosts = {urlsplit(s).hostname for s in self.seeds}

        while queue and len(self.pages) < self.max_pages:
            url, depth = queue.popleft()
            if url in self._seen:
                continue
            self._seen.add(url)

            if not self.guard.is_allowed(url):
                self.skipped.append(Page(url=url, depth=depth, robots_allowed=False,
                                         error="网络护栏拦截", note="guard"))
                self._emit("skip", f"护栏拦截 {url}")
                continue

            policy = self.policy_for(url)
            if not policy.can_fetch(url):
                self.skipped.append(Page(url=url, depth=depth, robots_allowed=False,
                                         note="robots.txt 禁止"))
                self._emit("skip", f"robots.txt 禁止 {url}")
                continue

            self._wait_turn(url, policy)
            result = self.fetcher.fetch(url)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            page = Page(url=url, depth=depth, status=result.status,
                        content_type=result.content_type, fetched_at=now,
                        error=result.error)
            is_html = "html" in (result.content_type or "").lower() or not result.content_type

            if result.error:
                self._emit("error", f"{url} -> {result.error}")
            elif is_html and result.body:
                page.title, page.text, page.links = extract_html(result.body, url)
                page.content_hash = hashlib.sha256(page.text.encode("utf-8")).hexdigest()[:16]
                self._emit("fetch", f"{url} [{result.status}] {page.text_len} 字 {len(page.links)} 链接")
                if depth < self.max_depth:
                    queue.extend(self._frontier(page.links, depth + 1, allowed_hosts))
            else:
                page.note = f"非 HTML（{result.content_type}），只记账不解析"
                self._emit("skip-parse", f"{url} {page.note}")

            self.pages.append(page)

        return self.pages

    def _frontier(self, links, next_depth: int, allowed_hosts: set[str | None]):
        for link in links:
            if link in self._seen:
                continue
            if link.lower().endswith(_SKIP_EXT):
                continue
            host = urlsplit(link).hostname
            if self.same_host and host not in allowed_hosts:
                continue
            if not self.guard.is_allowed(link):
                continue
            yield (link, next_depth)
