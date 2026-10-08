#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAF 工作流 —— 爬取结果 → 分段摘要 → 超阈值 AI 精简，两段可以挂不同后端。

拓扑：

    DocBatch ──> PageSummarizer ──┬─(页数 >= 阈值)─> Condenser ────┐
                                  └─(页数 <  阈值)─> PlainEmitter ─┴─> Digest

默认分工（可命令行反转）：
    PageSummarizer(本地模型)   逐页摘要，次数多、难度低 —— 放本地省云调用
    Condenser(云端模型)        全局精简，只有一次但最吃质量 —— 放云端把关

输出不是裸字符串，而是 `Digest` 数据结构，方便落库 / 落文件 / 二次消费。
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field

from agent_framework import Agent, Executor, WorkflowBuilder, WorkflowContext, handler

from llm import Backend, agent_options

__all__ = ["Doc", "DocBatch", "PageSummaries", "Digest",
           "build_workflow", "run_digest", "DEFAULT_THRESHOLD"]

DEFAULT_THRESHOLD = 10
DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONCURRENCY = 4
DEFAULT_DOC_CHARS = 4000
# 本地小模型不需要也不该给 8192 的预算，容易开始絮叨
LOCAL_MAX_TOKENS = 256


def _budget(backend: Backend, configured: int) -> int:
    """按后端类型收窄 token 预算。"""
    if backend.kind == "chat_completions":
        return min(configured, LOCAL_MAX_TOKENS)
    return configured


# ─────────────────────────── 数据契约 ───────────────────────────
@dataclass
class Doc:
    """一篇待摘要的文档（这里就是一页爬取结果）。"""
    url: str
    title: str = ""
    text: str = ""


@dataclass
class DocBatch:
    docs: list[Doc] = field(default_factory=list)


@dataclass
class PageSummaries:
    docs: list[Doc]
    summaries: list[str]
    backend: str
    model: str

    @property
    def count(self) -> int:
        return len(self.summaries)


@dataclass
class Digest:
    """最终产物：既有给人看的 markdown，也有给程序用的结构化字段。"""
    markdown: str
    condensed: bool
    threshold: int
    summaries: list[str]
    urls: list[str]
    summarizer_backend: str
    summarizer_model: str
    condenser_backend: str = ""
    condenser_model: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "condensed": self.condensed,
            "threshold": self.threshold,
            "summarizer": {"backend": self.summarizer_backend, "model": self.summarizer_model},
            "condenser": ({"backend": self.condenser_backend, "model": self.condenser_model}
                          if self.condensed else None),
            "pages": len(self.urls),
            "urls": self.urls,
            "summaries": self.summaries,
            "markdown": self.markdown,
            "elapsed_sec": round(self.elapsed, 2),
        }


# ─────────────────────────── 模型调用 ───────────────────────────
async def _run_text(agent, prompt: str, backend: Backend, *, max_tokens: int,
                    attempts: int = 2) -> str:
    """取正文；推理模型可能把预算烧在思维链上，正文为空就翻倍重试。"""
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


def _make_agent(backend: Backend, instructions: str, max_tokens: int) -> Agent:
    return Agent(client=backend.client, instructions=instructions,
                 default_options=agent_options(backend, max_tokens))


PAGE_INSTRUCTIONS = (
    "你是网页摘要助手。用户给你一页网页的标题和正文，"
    "你用一到三句话概括这一页的核心内容。只输出摘要正文，"
    "不写前缀、不写编号、不写「这一页」之类的套话。"
)

DIGEST_INSTRUCTIONS = (
    "你是长文总览编辑。用户给你多页网页各自的摘要，"
    "你把它们压缩成一段连贯的总览：合并重复信息、保留关键结论、"
    "不要罗列编号、不要添加原文没有的内容。只输出总览正文。"
)


# ─────────────────────────── 执行器 ───────────────────────────
class PageSummarizer(Executor):
    """输入 DocBatch → 输出 PageSummaries（并发逐页摘要）"""

    def __init__(self, backend: Backend, *, id: str = "page_summarizer",
                 concurrency: int = DEFAULT_CONCURRENCY,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 doc_chars: int = DEFAULT_DOC_CHARS,
                 dry_run: bool = False) -> None:
        super().__init__(id=id)
        self.backend = backend
        self._limit = concurrency
        self._sem = asyncio.Semaphore(concurrency)
        self._max_tokens = max_tokens
        self._doc_chars = doc_chars
        self._dry_run = dry_run

    async def _one(self, idx: int, total: int, doc: Doc) -> str:
        async with self._sem:
            body = doc.text[: self._doc_chars]
            # 提示词必须给明确边界：小模型看到「第 N/M 页」这种脚手架会当成正文往下续写
            prompt = (
                "请阅读下面这页网页，用一到两句中文概括它的主要内容。"
                "只输出摘要本身，不要重复原文，不要输出标题、编号、「正文」等字样。\n\n"
                f"=== 网页开始 ===\n标题：{doc.title or '(无标题)'}\n正文：\n{body}\n"
                "=== 网页结束 ===\n\n概括："
            )
            budget = _budget(self.backend, self._max_tokens)
            if self._dry_run:
                await asyncio.sleep(0)
                return f"[dry-run] {doc.title or doc.url} 的摘要占位"
            text = await _run_text(_make_agent(self.backend, PAGE_INSTRUCTIONS, budget),
                                   prompt, self.backend, max_tokens=budget)
            print(f"      · 第 {idx}/{total} 页摘要完成（{len(text)} 字）",
                  file=sys.stderr, flush=True)
            return text or "（该页摘要为空）"

    @handler
    async def summarize(self, batch: DocBatch,
                        ctx: WorkflowContext[PageSummaries]) -> None:
        total = len(batch.docs)
        if total == 0:
            raise ValueError("没有任何文档可以摘要")
        print(f"[2/3] 逐页摘要：{total} 页，后端 {self.backend.name}"
              f"（{self.backend.model}），并发 {self._limit}", file=sys.stderr, flush=True)
        t0 = time.monotonic()
        summaries = list(await asyncio.gather(
            *(self._one(i, total, d) for i, d in enumerate(batch.docs, 1))
        ))
        print(f"      摘要阶段用时 {time.monotonic() - t0:.1f}s", file=sys.stderr, flush=True)
        await ctx.send_message(PageSummaries(docs=batch.docs, summaries=summaries,
                                             backend=self.backend.name,
                                             model=self.backend.model))


def _render(summaries: PageSummaries, threshold: int,
            digest: str | None = None, condenser: str | None = None) -> str:
    lines: list[str] = []
    if digest:
        via = f"{summaries.backend} → {condenser}" if condenser else summaries.backend
        lines.append(f"【总览 · AI 精简（由 {summaries.count} 页摘要压缩，后端 {via}）】")
        lines.append(digest.strip())
        lines.append("")
    lines.append(f"【逐页摘要 · 共 {summaries.count} 页"
                 f"（后端 {summaries.backend}；阈值 {threshold}）】")
    for i, (doc, summary) in enumerate(zip(summaries.docs, summaries.summaries), 1):
        label = doc.title or doc.url
        lines.append(f"{i}. [{label}]({doc.url})")
        lines.append(f"   {summary}")
    return "\n".join(lines)


class Condenser(Executor):
    """输入 PageSummaries（页数 >= 阈值）→ 输出 Digest，带一次云端精简"""

    def __init__(self, backend: Backend, *, id: str = "condenser",
                 threshold: int = DEFAULT_THRESHOLD,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 dry_run: bool = False) -> None:
        super().__init__(id=id)
        self.backend = backend
        self._threshold = threshold
        self._max_tokens = max_tokens
        self._dry_run = dry_run

    @handler
    async def condense(self, batch: PageSummaries,
                       ctx: WorkflowContext[PageSummaries, Digest]) -> None:
        print(f"[3/3] 页数 {batch.count} >= 阈值 {self._threshold}，"
              f"触发 AI 精简（后端 {self.backend.name}）…", file=sys.stderr, flush=True)
        joined = "\n".join(f"{i}. {s}" for i, s in enumerate(batch.summaries, 1))
        digest_text = ""
        if self._dry_run:
            digest_text = "[dry-run] 总览占位"
        else:
            prompt = (f"下面是同一批抓取结果中 {batch.count} 页各自的摘要，"
                      f"请精简成一段连贯的总览（300 字以内）：\n\n{joined}")
            digest_text = await _run_text(
                _make_agent(self.backend, DIGEST_INSTRUCTIONS,
                            _budget(self.backend, self._max_tokens)),
                prompt, self.backend,
                max_tokens=_budget(self.backend, self._max_tokens))
        if not digest_text:
            print("      精简返回空正文，回退为只输出逐页摘要", file=sys.stderr, flush=True)
        await ctx.yield_output(Digest(
            markdown=_render(batch, self._threshold, digest_text or None,
                             self.backend.name),
            condensed=bool(digest_text),
            threshold=self._threshold,
            summaries=batch.summaries,
            urls=[d.url for d in batch.docs],
            summarizer_backend=batch.backend, summarizer_model=batch.model,
            condenser_backend=self.backend.name if digest_text else "",
            condenser_model=self.backend.model if digest_text else "",
        ))


class PlainEmitter(Executor):
    """输入 PageSummaries（页数 < 阈值）→ 输出 Digest，不再调精简模型"""

    def __init__(self, *, id: str = "emitter", threshold: int = DEFAULT_THRESHOLD) -> None:
        super().__init__(id=id)
        self._threshold = threshold

    @handler
    async def emit(self, batch: PageSummaries,
                   ctx: WorkflowContext[PageSummaries, Digest]) -> None:
        print(f"[3/3] 页数 {batch.count} < 阈值 {self._threshold}，跳过 AI 精简",
              file=sys.stderr, flush=True)
        await ctx.yield_output(Digest(
            markdown=_render(batch, self._threshold),
            condensed=False,
            threshold=self._threshold,
            summaries=batch.summaries,
            urls=[d.url for d in batch.docs],
            summarizer_backend=batch.backend, summarizer_model=batch.model,
        ))


# ─────────────────────────── 组装 ───────────────────────────
def build_workflow(summarizer: Backend, condenser: Backend, *,
                   threshold: int = DEFAULT_THRESHOLD,
                   concurrency: int = DEFAULT_CONCURRENCY,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   doc_chars: int = DEFAULT_DOC_CHARS,
                   dry_run: bool = False):
    page_summarizer = PageSummarizer(summarizer, id="page_summarizer",
                                     concurrency=concurrency, max_tokens=max_tokens,
                                     doc_chars=doc_chars, dry_run=dry_run)
    condense = Condenser(condenser, id="condenser", threshold=threshold,
                         max_tokens=max_tokens, dry_run=dry_run)
    emitter = PlainEmitter(id="emitter", threshold=threshold)

    builder = WorkflowBuilder(start_executor=page_summarizer, name="crawl-digest")
    builder.add_edge(page_summarizer, condense, condition=lambda b: b.count >= threshold)
    builder.add_edge(page_summarizer, emitter, condition=lambda b: b.count < threshold)
    return builder.build()


async def run_digest(docs: list[Doc], summarizer: Backend, condenser: Backend,
                     **kwargs) -> Digest:
    """跑一遍工作流，返回 Digest。"""
    workflow = build_workflow(summarizer, condenser, **kwargs)
    t0 = time.monotonic()
    result = await workflow.run(DocBatch(docs=docs))
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError("工作流产出了 0 个结果")
    digest: Digest = outputs[0]
    digest.elapsed = time.monotonic() - t0
    return digest
