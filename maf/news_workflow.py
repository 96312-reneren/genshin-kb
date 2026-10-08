#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAF 工作流（人工投喂版）—— 时间轴节点 + 最终报告。

拓扑：

    NewsBatch ──> TimelineBuilder ──> ReportWriter ──> NewsDigest
                                          └─> DeckPlanner（可选）

两个产出，服务两个目的：

    timeline   每条素材一个节点，**严格按发布时间升序**。节点里带日期、标题、
               一句话概括、出处、载入时间。这份是「顺序存储」在产出侧的体现，
               排序在存储和渲染两侧各保一次。
    report     模型读完整条时间轴之后写的观察报告。它不罗列条目，而是给结论，
               所以对提示词的要求和逐条摘要完全不同。

素材**全部由使用者本人提供**：这个模块不导入任何网络库，也不发任何请求。
结构化字段（日期/标题）由 ingest.py 从文本里解析出来，这里只负责调用模型、
排序、渲染和合规措辞。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from agent_framework import Agent, Executor, WorkflowBuilder, WorkflowContext, handler

from content import (DEFAULT_SUBJECT, DISPLAY_TZ, KIND_LABEL, NEWS_KINDS,
                     SOURCE_NAME, SOURCE_NOTE, SOURCE_ORIGIN, UNKNOWN_LABEL,
                     UNKNOWN_TS, is_known_date, render_created)
from llm import Backend, agent_options

__all__ = [
    "DEFAULT_SUBJECT",
    "NewsDoc", "NewsBatch", "TimelineNode", "Timeline", "NewsDigest",
    "docs_from_rows", "render_timeline", "render_report",
    "rhythm_stats", "render_rhythm",
    "DeckPlanner", "parse_deck_spec",
    "build_workflow", "run_news_digest",
    "SUMMARY_MAX_TOKENS", "REPORT_MAX_TOKENS", "DECK_MAX_TOKENS",
    "DEFAULT_DOC_CHARS",
]

# 逐条摘要一句话就够，256 预算对 qwen3:8b 很宽裕（实测 26~47 token 就答完）
SUMMARY_MAX_TOKENS = 256
# 报告要分三节、每节 2-4 句，256 肯定不够；本地 8B 不需要像手机上那样抠预算
REPORT_MAX_TOKENS = 1200
DEFAULT_DOC_CHARS = 1500
DEFAULT_CONCURRENCY = 4

# 概括口径版本。改了 NODE_INSTRUCTIONS 或 `_one()` 里的提示词就加一，
# 否则库里那些按老口径算出来的概括会被当成同一版继续复用。
NEWS_PROMPT_VERSION = "v1"
# 报告标题里的主题词 DEFAULT_SUBJECT 住在 content.py（界面层也要用同一个默认值）


# ─────────────────────────── 数据契约 ───────────────────────────
@dataclass
class NewsDoc:
    """一条待摘要的公告。字段一一对应新闻表，出处和时间是必带的。"""
    uid: str
    title: str
    text: str
    created_at: int
    created_iso: str
    url: str
    source_api: str
    source_name: str
    fetched_at: str
    kind: str = "notices"
    game: str = ""        # 主题词（原神 / 别的），只用来分组，不影响排序

    @property
    def date_cn(self) -> str:
        return render_created(self.created_at)

    @property
    def datetime_cn(self) -> str:
        return render_created(self.created_at, with_time=True)

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def to_row(self) -> dict:
        """→ news_items 表的一行。存储层要求的键一个都不能少。"""
        return {
            "uid": self.uid, "game": self.game, "kind": self.kind,
            # post_id 只用来在 created_at 相同时定一个稳定次序
            "post_id": self.uid.split(":", 1)[-1] if self.uid else "",
            "title": self.title, "description": self.text,
            "url": self.url, "banner": "",
            "created_at": int(self.created_at),
            "created_iso": self.created_iso,
            "source_api": self.source_api, "source_name": self.source_name,
            "content_hash": self.fingerprint, "fetched_at": self.fetched_at,
        }

    @property
    def fingerprint(self) -> str:
        """正文指纹 —— 和写进 `news_items.content_hash` 的是同一个值。

        概括缓存拿它判断「这条材料的正文有没有变过」：变了就得重算，
        没变就该直接复用，而不是每次都把同一段正文再喂一遍模型。
        """
        return hashlib.sha1(
            f"{self.title}\n{self.text}".encode("utf-8")).hexdigest()[:16]


@dataclass
class NewsBatch:
    docs: list[NewsDoc] = field(default_factory=list)


@dataclass
class TimelineNode:
    ordinal: int          # 恒等于「按时间升序排完之后的第几位」
    uid: str
    date: str
    datetime: str
    created_at: int       # 排序用的原始 Unix 秒，不靠渲染出来的字符串比大小
    title: str
    url: str
    source_api: str
    source_name: str
    fetched_at: str
    summary: str
    kind: str = "notices"

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {
            "ordinal": self.ordinal, "uid": self.uid, "date": self.date,
            "datetime": self.datetime, "created_at": self.created_at,
            "title": self.title, "kind": self.kind, "kind_label": self.kind_label,
            "url": self.url, "source_api": self.source_api,
            "source_name": self.source_name, "fetched_at": self.fetched_at,
            "summary": self.summary,
        }


@dataclass
class Timeline:
    nodes: list[TimelineNode]
    backend: str
    model: str
    source_api: str
    span: str
    subject: str = DEFAULT_SUBJECT


@dataclass
class NewsDigest:
    timeline_md: str
    report_md: str
    nodes: list[TimelineNode]
    report_body: str
    backend: str
    model: str
    source_api: str
    span: str
    condenser_backend: str = ""
    condenser_model: str = ""
    deck_spec: dict | None = None
    elapsed: float = 0.0
    subject: str = DEFAULT_SUBJECT
    unknown_dates: int = 0

    def to_dict(self) -> dict:
        return {
            "generated_by": {
                "summarizer": {"backend": self.backend, "model": self.model},
                "condenser": ({"backend": self.condenser_backend,
                               "model": self.condenser_model}
                              if self.condenser_backend else None),
            },
            "source_api": self.source_api,
            "source_apis": sorted({n.source_api for n in self.nodes if n.source_api}),
            "source_files": sorted({n.source_name for n in self.nodes if n.source_name}),
            "subject": self.subject,
            "unknown_dates": self.unknown_dates,
            "span": self.span,
            "items": len(self.nodes),
            "by_kind": rhythm_stats(self.nodes).get("by_kind", {}),
            "stats": rhythm_stats(self.nodes),
            "elapsed_sec": round(self.elapsed, 2),
            "timeline": [n.to_dict() for n in self.nodes],
            "report_body": self.report_body,
            "deck_spec": self.deck_spec,
        }


def docs_from_rows(rows) -> list[NewsDoc]:
    """SQLite 行 → NewsDoc，并再排一次序。

    存储层已经 ORDER BY created_at 了，这里再排一次是有意的冗余：
    只要哪天有人改了查询或者换了数据源，产出侧的先后顺序也不会错。
    """
    docs = []
    for r in rows:
        try:
            kind = r["kind"] or "notices"
        except (KeyError, IndexError):
            kind = "notices"          # 老查询 / 手工构造的行没有 kind 列时兜底
        try:
            game = r["game"] or ""
        except (KeyError, IndexError):
            game = ""
        docs.append(NewsDoc(
            uid=r["uid"], title=r["title"] or "", text=r["description"] or "",
            created_at=int(r["created_at"]), created_iso=r["created_iso"] or "",
            url=r["url"] or "", source_api=r["source_api"] or "",
            source_name=r["source_name"] or "", fetched_at=r["fetched_at"] or "",
            kind=kind, game=game))
    docs.sort(key=lambda d: (d.created_at, d.uid))
    return docs


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 版本节点按标题里的版本号归组：「月之七」「月之八」「7.0」「V7.0」「7.1」。
# 真实标题写法很散（预告 / 更新说明 / 游戏更新修复与优化说明），按关键词数会
# 把「一个版本的三条公告」算成三个节点，按版本号归组才是人理解的「一个版本」。
# 注意不要写成 [Vv]?\d+\.\d+ —— 那样「V7.0」和「7.0」会被当成两个版本，
# 只抓数字部分才能把同一个版本的三条公告归到一起。
_VERSION_LABEL_RE = re.compile(r"(月之[一二三四五六七八九十]+|\d+\.\d+)")


def _now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rhythm_stats(nodes: list[TimelineNode]) -> dict:
    """节奏统计 —— 全部由时间轴直接算，一个数都不让模型猜。

    为什么要有这个：8B 模型看见 15 条按时间排好的公告，会顺手编出
    「集中在每周一和周四」「大约每月一次」这类听着合理、实际对不上的结论。
    凡是能从数据里直接算出来的，就不该交给模型「归纳」。
    """
    if not nodes:
        return {}
    ordered = sorted(nodes, key=lambda n: (n.created_at, n.uid))
    # 日期未知的条目只计数，不参与任何日期运算 —— 它们的 created_at 是占位值
    dated = [n for n in ordered if is_known_date(n.created_at)]
    unknown = len(ordered) - len(dated)
    days = [datetime.fromtimestamp(n.created_at, DISPLAY_TZ).date() for n in dated]
    versions: dict[str, int] = {}
    for n in ordered:
        m = _VERSION_LABEL_RE.search(n.title)
        if m:
            versions[m.group(1)] = versions.get(m.group(1), 0) + 1
    kind_counts = Counter(n.kind for n in ordered)
    by_kind = {k: kind_counts[k] for k in NEWS_KINDS if kind_counts.get(k)}
    for k, c in kind_counts.items():
        by_kind.setdefault(k, c)
    if not dated:
        return {
            "count": len(ordered), "by_kind": by_kind, "unknown": unknown,
            "start": UNKNOWN_LABEL, "end": UNKNOWN_LABEL, "span_days": 0,
            "per_week": 0.0, "by_month": {},
            "by_weekday": {w: 0 for w in _WEEKDAYS},
            "gaps": [], "median_gap": 0, "max_gap": 0, "versions": versions,
        }
    months = Counter(d.strftime("%Y-%m") for d in days)
    weekdays = Counter(_WEEKDAYS[d.weekday()] for d in days)
    gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    span_days = (days[-1] - days[0]).days
    return {
        "count": len(ordered),
        "unknown": unknown,
        "by_kind": by_kind,
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "span_days": span_days,
        # 平均频率也直接算好：模型自己做除法十有八九是错的（实测它算出 3.2 条/周，
        # 真实值是 15 条 / 18.3 周 ≈ 0.82）。
        "per_week": (round(len(dated) / (span_days / 7), 2) if span_days
                     else float(len(dated))),
        "by_month": dict(sorted(months.items())),
        "by_weekday": {w: weekdays.get(w, 0) for w in _WEEKDAYS},
        "gaps": gaps,
        "median_gap": statistics.median(gaps) if gaps else 0,
        "max_gap": max(gaps) if gaps else 0,
        "versions": versions,
    }


def render_rhythm(stats: dict) -> list[str]:
    """把统计渲染成报告里的一节。这节永远不带模型成分。"""
    if not stats:
        return []
    lines = ["## 数据速览（由时间轴直接算出，非模型生成）", ""]
    if stats.get("by_month"):
        lines.append(f"- 条目：**{stats['count']}** 条，跨度 **{stats['start']} ~ "
                     f"{stats['end']}**（{stats['span_days']} 天）")
        lines.append("- 按月分布：" + "、".join(
            f"{k} {v} 条" for k, v in stats["by_month"].items()))
    else:
        lines.append(f"- 条目：**{stats['count']}** 条 —— 全部日期未知，"
                     "算不出跨度、频率和节奏")
    if len(stats.get("by_kind", {})) > 1:
        lines.append("- 按类别分布：" + "、".join(
            f"{KIND_LABEL.get(k, k)} {v} 条" for k, v in stats["by_kind"].items()))
    active = "、".join(f"{w} {c}" for w, c in stats.get("by_weekday", {}).items() if c)
    if active:
        lines.append(f"- 按星期分布：{active}")
    if stats.get("gaps"):
        lines.append(f"- 相邻内容间隔：中位数 **{stats['median_gap']}** 天，"
                     f"最长 {stats['max_gap']} 天")
        lines.append(f"- 平均每周 **{stats['per_week']}** 条")
    if stats["versions"]:
        lines.append(f"- 版本节点 **{len(stats['versions'])}** 个："
                     + "、".join(f"{k} {v} 条"
                                 for k, v in stats["versions"].items()))
    if stats.get("unknown"):
        lines.append(f"- **日期未知 {stats['unknown']} 条**：已排在时间轴末尾，"
                     "不计入上面任何统计")
    return lines


def rhythm_facts(stats: dict) -> str:
    """喂给模型的版本：明确告诉它这些数已经算好了，别再自己推断。"""
    if not stats:
        return "(无)"
    facts: list[str] = [f"条目数 {stats['count']}"]
    if stats.get("by_month"):
        facts += [
            f"跨度 {stats['start']} ~ {stats['end']}（{stats['span_days']} 天）",
            "按月分布：" + "、".join(f"{k} {v} 条"
                                     for k, v in stats["by_month"].items()),
        ]
    else:
        facts.append("所有条目的日期都未知，跨度与频率无从计算")
    facts.append("按类别分布：" + "、".join(
        f"{KIND_LABEL.get(k, k)} {v} 条" for k, v in stats.get("by_kind", {}).items()))
    active = "、".join(f"{w} {c}" for w, c in stats.get("by_weekday", {}).items() if c)
    if active:
        facts.append(f"按星期分布：{active}")
    if stats.get("gaps"):
        facts.append(f"相邻内容间隔中位数 {stats['median_gap']} 天，"
                     f"最长 {stats['max_gap']} 天")
        facts.append(f"平均每周 {stats['per_week']} 条")
    facts.append(f"版本节点 {len(stats['versions'])} 个：" + "、".join(
        f"{k} {v} 条" for k, v in stats["versions"].items()))
    if stats.get("unknown"):
        facts.append(f"日期未知 {stats['unknown']} 条（排在末尾，不计入上述统计）")
    return "\n".join(facts)


def _span(docs: list[NewsDoc]) -> str:
    """时间跨度只由**已知日期**的条目决定。

    「日期未知」的条目被排在时间轴末尾，但它们不是「最晚发布的内容」，
    算进跨度里会把整个时间线拉长到 2286 年去。
    """
    dated = [d for d in docs if is_known_date(d.created_at)]
    if not dated:
        return UNKNOWN_LABEL
    lo, hi = dated[0].date_cn, dated[-1].date_cn
    return lo if lo == hi else f"{lo} ~ {hi}"


# ─────────────────────────── 调用模型 ───────────────────────────
async def _ask(agent: Agent, prompt: str, backend: Backend, *, max_tokens: int,
               attempts: int = 2) -> str:
    """取正文；推理模型可能把预算烧在思考上，正文为空就翻倍重试。"""
    budget = max_tokens
    text = ""
    for i in range(attempts + 1):
        resp = await agent.run(prompt, options=agent_options(backend, budget))
        text = (getattr(resp, "text", "") or "").strip()
        if text or i == attempts:
            break
        budget = min(budget * 2, 65536)
        print(f"      · {backend.name} 返回空正文，提到 max_tokens={budget} 重试",
              file=sys.stderr, flush=True)
    return text


def _agent(backend: Backend, instructions: str, max_tokens: int) -> Agent:
    return Agent(client=backend.client, instructions=instructions,
                 default_options=agent_options(backend, max_tokens))


NODE_INSTRUCTIONS = (
    "你是内容速读助手。用户给你一条材料的发布时间、标题和正文，"
    "你用一句话概括它在说什么，不超过 40 个字。只输出概括本身，"
    "不要重复标题、不要编号、不要写「这条材料」之类的套话。"
)

REPORT_INSTRUCTIONS = (
    "你是内容运营分析助手。用户给你一条按时间排好的素材时间轴，"
    "你基于这些材料写一份观察报告。只依据材料说话，不要编造材料里没有的"
    "活动名、版本号、角色名或数字。"
)


def _report_sections(by_kind: dict) -> list[str]:
    """报告要写哪几节 —— 只有一个类别时不硬凑「分类观察」。"""
    sections = ["## 整体概况"]
    if len(by_kind) > 1:
        sections.append("## 分类观察")
    sections += ["## 主题与脉络", "## 节奏观察"]
    return sections


# ─────────────────────────── 执行器 ───────────────────────────
class TimelineBuilder(Executor):
    """输入 NewsBatch → 输出 Timeline（逐条一句话概括，顺序原样保持）

    lookup / on_summary 是概括缓存的接缝：调用方（pipeline）知道库里
    哪些 uid 已经有一条对应的概括，工作流本身不认识数据库。
    命中就一条模型调用都不发 —— 本地 3B 上一条 7 秒，50 条就是 6 分钟。
    """

    def __init__(self, backend: Backend, *, id: str = "timeline_builder",
                 concurrency: int = DEFAULT_CONCURRENCY,
                 max_tokens: int = SUMMARY_MAX_TOKENS,
                 doc_chars: int = DEFAULT_DOC_CHARS,
                 subject: str = DEFAULT_SUBJECT,
                 dry_run: bool = False,
                 lookup=None, on_summary=None) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.subject = subject or DEFAULT_SUBJECT
        self._sem = asyncio.Semaphore(concurrency)
        self._limit = concurrency
        self._max_tokens = max_tokens
        self._doc_chars = doc_chars
        self._dry_run = dry_run
        self._lookup = lookup
        self._on_summary = on_summary
        self.hits = 0

    async def _one(self, idx: int, total: int, doc: NewsDoc) -> str:
        # 缓存查询放在信号量外面：命中不该占并发额度，也不该排在队列里等
        if self._lookup is not None:
            cached = self._lookup(doc)
            if cached:
                self.hits += 1
                print(f"      · 第 {idx}/{total} 条命中已存概括（省一次模型调用）",
                      file=sys.stderr, flush=True)
                return cached
        async with self._sem:
            if self._dry_run:
                await asyncio.sleep(0)
                return f"[dry-run] {doc.title} 的概括占位"
            body = doc.text[: self._doc_chars]
            prompt = (
                f"请阅读下面这条与《{self.subject}》相关的材料，"
                "用一句话（不超过 40 字）概括它在说什么。"
                "只输出概括本身，不要重复标题，不要编号。\n\n"
                f"=== 材料开始 ===\n发布时间：{doc.datetime_cn}（UTC+8）\n"
                f"标题：{doc.title or '(无标题)'}\n正文：\n{body}\n"
                "=== 材料结束 ===\n\n一句话概括："
            )
            text = await _ask(_agent(self.backend, NODE_INSTRUCTIONS, self._max_tokens),
                              prompt, self.backend, max_tokens=self._max_tokens)
            text = text or "（该条概括为空）"
            if self._on_summary is not None:
                self._on_summary(doc, text)
            print(f"      · 第 {idx}/{total} 条概括完成（{len(text)} 字）",
                  file=sys.stderr, flush=True)
            return text

    @handler
    async def build(self, batch: NewsBatch,
                    ctx: WorkflowContext[Timeline]) -> None:
        # 进工作流的顺序不能假设是对的：这里自己排一次，后面所有编号都以它为准
        docs = sorted(batch.docs, key=lambda d: (d.created_at, d.uid))
        total = len(docs)
        if total == 0:
            raise ValueError("没有任何公告可以处理")
        print(f"[2/3] 时间轴逐条概括：{total} 条，后端 {self.backend.name}"
              f"（{self.backend.model}），并发 {self._limit}", file=sys.stderr, flush=True)
        t0 = time.monotonic()
        # return_exceptions=True：一条 429 / 连接重置不该让整批已经算好的概括全废。
        # 但「全都失败」时还是得报错退出，不能产出一份空报告。
        raw = await asyncio.gather(
            *(self._one(i, total, d) for i, d in enumerate(docs, 1)),
            return_exceptions=True)
        summaries: list[str] = []
        failures = 0
        for i, item in enumerate(raw, 1):
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                failures += 1
                summaries.append(f"（第 {i} 条概括失败：{type(item).__name__}）")
                print(f"      · 第 {i}/{total} 条概括失败，继续跑其余的："
                      f"{type(item).__name__}: {item}", file=sys.stderr, flush=True)
            else:
                summaries.append(item)
        if failures and failures == total:
            raise RuntimeError(f"{total} 条概括全部失败，第一条是：{raw[0]!r}")
        spent = time.monotonic() - t0
        extra = f"，其中 {self.hits} 条复用已存概括" if self.hits else ""
        if failures:
            extra += f"，{failures} 条失败已标注"
        print(f"      概括阶段用时 {spent:.1f}s{extra}", file=sys.stderr, flush=True)

        nodes = [
            TimelineNode(ordinal=0, uid=d.uid, date=d.date_cn,
                         datetime=d.datetime_cn, created_at=d.created_at,
                         title=d.title, url=d.url, source_api=d.source_api,
                         source_name=d.source_name, fetched_at=d.fetched_at,
                         summary=s, kind=d.kind)
            for d, s in zip(docs, summaries)
        ]
        nodes.sort(key=lambda n: (n.created_at, n.uid))
        for i, node in enumerate(nodes, 1):
            node.ordinal = i          # 编号 = 时间顺序上的位置，不是输入顺序
        await ctx.send_message(Timeline(
            nodes=nodes, backend=self.backend.name, model=self.backend.model,
            source_api=docs[0].source_api if docs else "",
            span=_span(docs), subject=self.subject))


class ReportWriter(Executor):
    """输入 Timeline → 输出 NewsDigest（时间轴 markdown + 报告正文）"""

    def __init__(self, backend: Backend, *, id: str = "report_writer",
                 max_tokens: int = REPORT_MAX_TOKENS,
                 subject: str = DEFAULT_SUBJECT,
                 dry_run: bool = False, emit: bool = True) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.subject = subject or DEFAULT_SUBJECT
        self._max_tokens = max_tokens
        self._dry_run = dry_run
        # 后面还接 DeckPlanner 时它不是终点，只有终点才 yield_output
        self.emit = emit

    @handler
    async def write(self, timeline: Timeline,
                    ctx: WorkflowContext[NewsDigest]) -> None:
        print(f"[3/3] 生成最终报告（{len(timeline.nodes)} 个节点，"
              f"报告后端 {self.backend.name}/{self.backend.model}）…",
              file=sys.stderr, flush=True)
        entries = "\n".join(
            f"{n.ordinal}. {n.datetime} · [{n.kind_label}] {n.title}\n   {n.summary}"
            for n in timeline.nodes)
        stats = rhythm_stats(timeline.nodes)
        if self._dry_run:
            body = "[dry-run] 报告占位"
        else:
            sections = _report_sections(stats.get("by_kind", {}))
            kind_text = "、".join(KIND_LABEL.get(k, k)
                                  for k in stats.get("by_kind", {})) or "材料"
            rules = [
                "1. 用中文 Markdown，只使用二级标题（##）分节。",
                f"2. 必须包含下面 {len(sections)} 节，标题照抄：\n"
                + "\n".join(sections),
            ]
            if len(sections) == 4:
                rules.append("3. 「分类观察」要分别说清各类别各自在讲什么、"
                             "发布节奏有什么不同。")
            rules += [
                "4. 每节 2-4 句话，结论必须落在材料上；材料里没有的版本号、"
                "角色名、活动名、数字一律不要写。",
                "5. 不要逐条罗列，逐条清单在时间轴里已经有了。",
                "6. 直接输出报告正文，不要写开场白和结束语。",
                "7. 凡涉及星期几、更新周期、发布频率的结论，只能引用上面给出的"
                "统计；统计里没有的，就不要写。",
                "8. 不要自己做任何算术：需要数字就直接抄统计里的，没有的数不要出现。",
            ]
            prompt = (
                f"下面是使用者提供的、与《{self.subject}》相关的{kind_text}，"
                f"已按时间从早到晚排成一条完整时间轴（共 {len(timeline.nodes)} 条，"
                f"跨度 {timeline.span}）。每条前面的方括号是它的类别：\n\n"
                f"{entries}\n\n"
                "程序已经算好的统计，直接引用这些数字，不要另行推断：\n"
                f"{rhythm_facts(stats)}\n\n"
                "请基于这些材料写一份「运营观察报告」。要求：\n"
                + "\n".join(rules)
            )
            budget = _budget_for(self.backend, self._max_tokens)
            body = await _ask(
                _agent(self.backend, REPORT_INSTRUCTIONS, budget),
                prompt, self.backend, max_tokens=budget)
            if not body:
                print("      报告返回空正文，回退为只输出时间轴",
                      file=sys.stderr, flush=True)

        generated_at = _now_local()
        condenser_label = f"{self.backend.name} / {self.backend.model}"
        digest = NewsDigest(
            timeline_md=render_timeline(timeline, generated_at=generated_at,
                                        subject=self.subject),
            report_md=render_report(timeline, body or "",
                                    generated_at=generated_at, stats=stats,
                                    condenser_label=condenser_label,
                                    subject=self.subject),
            nodes=timeline.nodes,
            report_body=body or "",
            backend=timeline.backend, model=timeline.model,
            condenser_backend=self.backend.name,
            condenser_model=self.backend.model,
            source_api=timeline.source_api, span=timeline.span,
            subject=self.subject,
            unknown_dates=stats.get("unknown", 0),
        )
        if self.emit:
            await ctx.yield_output(digest)
        else:
            await ctx.send_message(digest)


# ─────────────────────────── 幻灯片文案 ───────────────────────────
DECK_MAX_TOKENS = 1200

# 云端 deepseek-chat 是推理模型：实测 1200 token 的预算会被 reasoning 吃光、
# 正文返回空（日志里那句「返回空正文，提到 max_tokens=2400 重试」就是它）。
# 本地 qwen3 已经在模板里关掉了 thinking，不需要垫这一层。
CLOUD_REASONING_FLOOR = 3000


def _budget_for(backend: Backend, configured: int) -> int:
    """推理型后端给一个预算下限，避免第一次调用必然空手而归。"""
    if getattr(backend, "kind", "") == "responses":
        return max(configured, CLOUD_REASONING_FLOOR)
    return configured


DECK_INSTRUCTIONS = (
    "你是汇报材料编辑。用户给你一份内容观察报告，"
    "你把每个小节改写成适合投影的要点。只依据给定材料，不要新增任何事实。"
)


def _spec_from_body(body: str) -> dict:
    """兜底：直接从报告正文的 markdown 小节切要点，不用再调一次模型。"""
    sections: list[dict] = []
    current: dict | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), "bullets": []}
            sections.append(current)
        elif current is not None and not line.startswith("#"):
            for sentence in re.split(r"(?<=[。！？])", line):
                sentence = sentence.strip()
                if sentence and len(current["bullets"]) < 4:
                    current["bullets"].append(sentence)
    return {"deck_title": "", "deck_subtitle": "", "sections": sections}


def parse_deck_spec(raw: str) -> dict | None:
    """模型偶尔会包 markdown 代码块或加解释，这里只认最外层那对花括号。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:                                  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return None
    clean: list[dict] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "").strip()
        bullets = [str(b).strip() for b in (sec.get("bullets") or [])
                   if str(b).strip()]
        if heading and bullets:
            clean.append({"heading": heading, "bullets": bullets[:5]})
    if not clean:
        return None
    return {
        "deck_title": str(data.get("deck_title") or "").strip(),
        "deck_subtitle": str(data.get("deck_subtitle") or "").strip(),
        "sections": clean,
    }


class DeckPlanner(Executor):
    """输入 NewsDigest（已含报告正文）→ 输出填好 deck_spec 的 NewsDigest。

    为什么让模型出 JSON 而不是直接产 pptx：
        大模型 API 返回的是文本，没有「产出 .pptx 文件」这种接口。退一步，
        让模型写 python-pptx 代码再去执行，等于把任意代码执行引进流水线。
        所以这里的分工是「模型给结构化的要点 JSON，本地代码负责排版」——
        版面可控、每页署名可控、结果可复现。
    """

    def __init__(self, backend: Backend, *, id: str = "deck_planner",
                 max_tokens: int = DECK_MAX_TOKENS,
                 subject: str = DEFAULT_SUBJECT,
                 dry_run: bool = False,
                 plain: bool = False) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.subject = subject or DEFAULT_SUBJECT
        self._max_tokens = max_tokens
        self._dry_run = dry_run
        self._plain = plain

    @handler
    async def plan(self, digest: NewsDigest,
                   ctx: WorkflowContext[NewsDigest]) -> None:
        spec = _spec_from_body(digest.report_body)
        if self._plain:
            # 直接切分报告正文。少一次「整份报告再进模型」的调用 ——
            # 在本地模型上，这一次调用的输入是全文，prefill 比输出贵得多。
            print(f"[4/4] plain 模式：不调模型，直接按报告小节切出幻灯片要点",
                  file=sys.stderr, flush=True)
        else:
            print(f"[4/4] 生成幻灯片文案（后端 {self.backend.name}）…",
                  file=sys.stderr, flush=True)
        if not self._plain and not self._dry_run and digest.report_body.strip():
            prompt = (
                f"下面是《{digest.subject or self.subject}》内容观察报告的报告正文：\n\n"
                f"{digest.report_body}\n\n"
                "请把它改写成幻灯片要点，只输出严格的 JSON，不要 markdown 代码块，"
                "不要任何解释文字：\n"
                "{\n"
                '  "deck_title": "标题，不超过 20 字",\n'
                '  "deck_subtitle": "副标题，不超过 30 字",\n'
                '  "sections": [\n'
                '    {"heading": "小节标题", "bullets": ["要点一", "要点二"]}\n'
                "  ]\n"
                "}\n"
                "要求：\n"
                "1. sections 保留报告里的每一个小节，顺序不变，小节标题照抄。\n"
                "2. 每个小节 2-4 条 bullet，每条不超过 40 字，是完整的一句话，"
                "不要编号，不要以 - 或 * 开头。\n"
                "3. 只依据报告正文，不要新增版本号、角色名、活动名或任何数字。\n"
                "4. 不要输出 JSON 之外的任何字符。"
            )
            dbudget = _budget_for(self.backend, self._max_tokens)
            raw = await _ask(
                _agent(self.backend, DECK_INSTRUCTIONS, dbudget),
                prompt, self.backend, max_tokens=dbudget)
            parsed = parse_deck_spec(raw)
            if parsed:
                spec = parsed
            else:
                print("      幻灯片文案不是合法 JSON，回退为直接切分报告正文",
                      file=sys.stderr, flush=True)
        digest.deck_spec = spec
        await ctx.yield_output(digest)


# ─────────────────────────── 渲染 ───────────────────────────
def _topic(nodes: list[TimelineNode]) -> str:
    """标题里的主题词：三类都在就是「公告、活动、资讯」。"""
    kinds: list[str] = []
    for n in nodes:
        label = KIND_LABEL.get(n.kind, n.kind)
        if label not in kinds:
            kinds.append(label)
    return "、".join(kinds) if kinds else "公告"


def _model_line(summarizer: str, condenser: str = "") -> str:
    if condenser and condenser != summarizer:
        return f"**摘要模型**：{summarizer} ｜ **报告模型**：{condenser}"
    return f"**模型**：{summarizer}"


def _source_summary(nodes: list[TimelineNode]) -> str:
    """材料清单：这批材料来自哪几份来源（人工投喂时就是文件名 / 粘贴批次）。"""
    names: list[str] = []
    for n in nodes:
        name = (n.source_name or "").strip() or SOURCE_NAME
        if name not in names:
            names.append(name)
    if not names:
        return SOURCE_NAME
    if len(names) > 4:
        return "、".join(names[:4]) + f" 等 {len(names)} 份材料"
    return "、".join(names)


def _header(subject: str, nodes: list[TimelineNode],
            span: str, model_line: str, generated_at: str) -> list[str]:
    return [
        f"> **数据来源**：{SOURCE_ORIGIN}",
        f"> **材料**：{_source_summary(nodes)} ｜ **主题**：《{subject}》",
        f"> **条目**：{len(nodes)} 条（{_topic(nodes)}） ｜ "
        f"**时间跨度**：{span}（时间为东八区 UTC+8）",
        f"> **生成时间**：{generated_at} ｜ {model_line}",
        "",
        f"> {SOURCE_NOTE}",
    ]


def render_timeline(timeline: Timeline, *, generated_at: str = "",
                    subject: str = "") -> str:
    subject = subject or getattr(timeline, "subject", "") or DEFAULT_SUBJECT
    nodes = sorted(timeline.nodes, key=lambda n: (n.created_at, n.ordinal))
    lines = [f"# 《{subject}》{_topic(nodes)}时间轴", ""]
    lines += _header(subject,
                     nodes, timeline.span,
                     _model_line(f"{timeline.backend} / {timeline.model}"),
                     generated_at or _now_local())
    lines += ["", "---", ""]
    for n in nodes:
        lines.append(f"## {n.ordinal:02d} · {n.date} · {n.kind_label}")
        lines.append("")
        lines.append(f"**{n.title}**")
        lines.append("")
        lines.append(n.summary)
        lines.append("")
        lines.append(f"> 出处：{n.source_name or SOURCE_NAME} ｜ "
                     f"类别：{n.kind_label}")
        lines.append(f"> 发布时间：{n.datetime}（UTC+8） ｜ "
                     f"载入时间：{n.fetched_at}")
        if n.url:
            lines.append(f"> 原始链接：{n.url}")
        lines += ["", "---", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_report(timeline: Timeline, body: str, *, generated_at: str = "",
                  stats: dict | None = None, condenser_label: str = "",
                  subject: str = "") -> str:
    subject = subject or getattr(timeline, "subject", "") or DEFAULT_SUBJECT
    nodes = sorted(timeline.nodes, key=lambda n: (n.created_at, n.ordinal))
    summarizer = f"{timeline.backend} / {timeline.model}"
    condenser = condenser_label or summarizer
    lines = [f"# 《{subject}》{_topic(nodes)}观察报告", ""]
    lines += _header(subject,
                     nodes, timeline.span, _model_line(summarizer, condenser),
                     generated_at or _now_local())
    # 先放程序算出来的硬数据，再放模型写的分析：读者一眼能分清哪部分是谁产出的
    rhythm = render_rhythm(stats if stats is not None else rhythm_stats(nodes))
    if rhythm:
        lines += ["", "---", ""]
        lines += rhythm
    lines += ["", "---", ""]
    lines.append(body.strip() if body.strip() else "（本次未生成报告正文）")
    lines += ["", "---", "", "## 附：完整出处清单", ""]
    for n in nodes:
        link = f" — {n.url}" if n.url else ""
        src = n.source_name or SOURCE_NAME
        lines.append(f"{n.ordinal}. {n.date} · [{n.kind_label}] {n.title}"
                     f" — 出处：{src}{link}")
    # 合规声明必须如实反映报告是谁写的：接了云端就不能再写「由本地模型生成」
    writer = ("本地模型（非云端）" if condenser.startswith("local")
              else f"云端模型（{condenser.split('/')[0].strip()}）")
    lines += [
        "",
        "---",
        "",
        f"> **合规声明**：本报告由{writer}基于**使用者本人提供的材料**自动生成。"
        "本应用不访问任何网络接口、不抓取任何站点、不使用任何游戏客户端文件。",
        "> 材料内容与相关商标版权归原权利人所有，此处仅作个人作品展示与"
        "非商业研究用途；材料由使用者提供，未经人工逐条核实，不代表任何官方发布。",
    ]
    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────── 组装 ───────────────────────────
def build_workflow(summarizer: Backend, condenser: Backend | None = None, *,
                   concurrency: int = DEFAULT_CONCURRENCY,
                   summary_tokens: int = SUMMARY_MAX_TOKENS,
                   report_tokens: int = REPORT_MAX_TOKENS,
                   deck_tokens: int = DECK_MAX_TOKENS,
                   doc_chars: int = DEFAULT_DOC_CHARS,
                   subject: str = DEFAULT_SUBJECT,
                   with_deck: bool = False,
                   dry_run: bool = False,
                   plain_deck: bool = False,
                   lookup=None, on_summary=None):
    """时间轴 → 报告（→ 幻灯片文案）。两段可以挂不同后端。

    默认两段同一个后端；给了 condenser 就是混合模式（逐条摘要走本地、
    最终报告走云端），这也是原项目 README 里主张的分工。

    with_deck=True 时再接一个 DeckPlanner：模型只产出结构化的要点 JSON，
    真正的排版由 render_office.py 完成。plain_deck=True 则跳过这一次模型调用，
    直接用报告正文切出要点（本地模型上这一刀能省一整份报告正文的 prefill）。

    lookup / on_summary 是概括缓存的接缝，见 TimelineBuilder。
    """
    condenser = condenser or summarizer
    subject = subject or DEFAULT_SUBJECT
    timeline = TimelineBuilder(summarizer, id="timeline_builder",
                               concurrency=concurrency,
                               max_tokens=summary_tokens, doc_chars=doc_chars,
                               subject=subject, dry_run=dry_run,
                               lookup=lookup, on_summary=on_summary)
    writer = ReportWriter(condenser, id="report_writer",
                          max_tokens=report_tokens, subject=subject,
                          dry_run=dry_run,
                          emit=not with_deck)
    builder = WorkflowBuilder(start_executor=timeline, name="news-digest")
    builder.add_edge(timeline, writer)
    if with_deck:
        planner = DeckPlanner(condenser, id="deck_planner",
                              max_tokens=deck_tokens, subject=subject,
                              dry_run=dry_run, plain=plain_deck)
        builder.add_edge(writer, planner)
    return builder.build()


async def run_news_digest(docs: list[NewsDoc], summarizer: Backend,
                          condenser: Backend | None = None,
                          **kwargs) -> NewsDigest:
    """跑一遍内容工作流，返回 NewsDigest（时间轴 + 报告）。"""
    workflow = build_workflow(summarizer, condenser, **kwargs)

    t0 = time.monotonic()
    result = await workflow.run(NewsBatch(docs=docs))
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError("工作流产出了 0 个结果")
    digest: NewsDigest = outputs[0]
    digest.elapsed = time.monotonic() - t0
    return digest
