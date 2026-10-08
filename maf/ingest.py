#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工投喂层 —— 把「使用者拖进来的一堆文本」变成可入库、可排时间轴的 NewsDoc。

原来是 hoyo_api.py 从社区接口取结构化 JSON，每条自带 created_at、title、url。
现在换成人工文本，结构化字段得自己从文本里长出来：

    日期     先做**确定性解析**：行首的「2026-09-01」「2026年9月1日」「2026/9/1」
             都能认。认不出来的条目，才交给本地模型从正文里抽一次。
   标题     日期同一行剩下的部分；没有就把正文第一行当标题。
   正文     整块原文，一个字都不删 —— 喂给模型的必须是原文。
   出处     来自哪个文件 / 哪一次粘贴，逐条记下来。

设计原则：**能用规则定的，绝不问模型**。日期前缀是最稳的信号，模型只做兜底。
抽不到日期就老实标「日期未知」，丢到时间轴最后 —— 不能编一个日期出来，
那会直接污染后面所有节奏统计。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from content import (DEFAULT_KIND, DISPLAY_TZ, KIND_LABEL, NEWS_KINDS, SOURCE_NAME,
                     SOURCE_TAG, UNKNOWN_TS, now_iso)

__all__ = [
    "RawItem", "ParseResult", "parse_text", "parse_sources", "parse_file",
    "read_text", "decode_bytes", "to_docs", "normalize_date", "date_to_ts",
    "split_uid",
    "DateFiller", "PARSE_MODES", "MODE_LABEL",
]

PARSE_MODES = ("auto", "date", "heading", "blank", "whole")
MODE_LABEL = {
    "auto": "自动判断",
    "date": "按日期行分条",
    "heading": "按标题行分条",
    "blank": "按空行分条",
    "whole": "整篇算一条",
}

# 行首日期：2026-09-01 / 2026/9/1 / 2026.09.01 / 2026年9月1日，后面可跟时间
_DATE_LINE_RE = re.compile(
    r"^[\s\u3000>#*\-•·、]*"
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?"
    r"(?:[\s\u3000]+(?P<hh>\d{1,2})\s*[:：]\s*(?P<mi>\d{2}))?"
    r"[\s\u3000:：\-—–|｜,，.。)\]）】]*"
    r"(?P<rest>.*)$"
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<t>.+?)\s*#*\s*$")
_BRACKET_RE = re.compile(r"^\s*[【\[](?P<t>[^】\]]{2,60})[】\]]\s*$")
_ONLY_DATE_RE = re.compile(
    r"^[\s\u3000>#*\-•·、]*\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?[\s\u3000:：]*$")

# 标题最多这么长；再长就说明这行其实是正文，不该当标题
TITLE_MAX = 80
# 文档级的短前置块（第一段还没出现日期时）当成标题，不单独算一条
FRONT_MATTER_MAX = 120


# ─────────────────────────── 数据契约 ───────────────────────────
@dataclass
class RawItem:
    """解析出来的一条原始素材。date 为 None 表示还没定日期。"""
    ordinal: int
    title: str
    body: str
    date: str | None = None
    source: str = SOURCE_NAME
    kind: str = DEFAULT_KIND
    date_source: str = ""        # prefix / model / unknown
    note: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "title": self.title, "body": self.body,
                "date": self.date, "source": self.source, "kind": self.kind,
                "kind_label": self.kind_label, "date_source": self.date_source,
                "note": self.note, "chars": len(self.body)}

    @classmethod
    def from_dict(cls, d: dict) -> "RawItem":
        return cls(ordinal=int(d.get("ordinal") or 0),
                   title=str(d.get("title") or ""), body=str(d.get("body") or ""),
                   date=(str(d["date"]) if d.get("date") else None),
                   source=str(d.get("source") or SOURCE_NAME),
                   kind=str(d.get("kind") or DEFAULT_KIND),
                   date_source=str(d.get("date_source") or ""),
                   note=str(d.get("note") or ""))


@dataclass
class ParseResult:
    items: list[RawItem] = field(default_factory=list)
    doc_title: str = ""
    warnings: list[str] = field(default_factory=list)
    mode_used: str = "whole"

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in self.items],
                "doc_title": self.doc_title, "warnings": self.warnings,
                "mode_used": self.mode_used, "count": len(self.items),
                "undated": sum(1 for i in self.items if not i.date)}


# ─────────────────────────── 日期处理 ───────────────────────────
def normalize_date(s: str | None) -> str | None:
    """把各种写法归一到 YYYY-MM-DD；认不出来返回 None（不猜）。"""
    if not s:
        return None
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def date_to_ts(date: str | None) -> int:
    """日期字符串 → 东八区当天 00:00 的 Unix 秒；未知返回占位时间戳。"""
    if not date:
        return UNKNOWN_TS
    try:
        dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=DISPLAY_TZ)
    except ValueError:
        return UNKNOWN_TS
    return int(dt.timestamp())


def _clean_title(s: str) -> str:
    """去掉行首残留的分隔符与行尾空白，过长的截断。"""
    t = re.sub(r"^[\s\u3000:：\-—–|｜、,，.。)\]）】>#*]+", "", s or "")
    t = re.sub(r"[\s\u3000]+$", "", t)
    if len(t) > TITLE_MAX:
        t = t[:TITLE_MAX].rstrip() + "…"
    return t


def _first_line(text: str, limit: int = TITLE_MAX) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return _clean_title(s[:limit + 20]) if len(s) > limit else _clean_title(s)
    return ""


def _date_in_line(line: str) -> tuple[str | None, str]:
    """行首探日期。返回 (YYYY-MM-DD 或 None, 该行剩下来的标题文本)。"""
    m = _DATE_LINE_RE.match(line)
    if not m:
        return None, line
    date = normalize_date(f"{m.group('y')}-{m.group('m')}-{m.group('d')}")
    if not date:
        return None, line
    return date, _clean_title(m.group("rest") or "")


# ─────────────────────────── 切块 ───────────────────────────
def _block(date, inline, lines):
    return {"date": date, "inline": inline, "lines": lines}


def _split_by_date(text: str) -> tuple[str, list[dict]]:
    """按「行首带日期」的行切块。日期之前的内容算前言，不当成一条素材。

    没有日期前缀的行默认接在上一条的正文里 —— 正文里换行、分段都很常见，
    见空行就切会把一条材料切碎。只有 markdown 标题 / 【】标题这种**强信号**
    才另起一条：那种行本身就在说「我是新一条」，日期留空交给模型补抽。
    """
    blocks: list[dict] = []
    front: list[str] = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        date, inline = _date_in_line(line)
        if date:
            cur = _block(date, inline, [])
            blocks.append(cur)
            continue
        m = _HEADING_RE.match(line) or _BRACKET_RE.match(line)
        if m:
            cur = _block(None, _clean_title(m.group("t")), [])
            blocks.append(cur)
            continue
        if cur is None:
            front.append(raw)
        else:
            cur["lines"].append(raw)
    ftext = "\n".join(front).strip()
    doc_title = ""
    if ftext and len(ftext) <= FRONT_MATTER_MAX:
        doc_title = _first_line(ftext)
    elif ftext:
        # 前言太长，说明它本身就是素材，不能丢
        blocks.insert(0, _block(None, "", front))
    return doc_title, blocks


def _split_by_heading(text: str) -> tuple[str, list[dict]]:
    """按 markdown 标题行或【】标题行切块。"""
    blocks: list[dict] = []
    cur = None
    for raw in text.splitlines():
        m = _HEADING_RE.match(raw.rstrip()) or _BRACKET_RE.match(raw.rstrip())
        if m:
            cur = _block(None, _clean_title(m.group("t")), [])
            blocks.append(cur)
            continue
        if cur is None:
            cur = _block(None, "", [])
            blocks.append(cur)
        cur["lines"].append(raw)
    return "", blocks


def _split_by_blank(text: str) -> list[dict]:
    """按空行切块 —— 每条之间空一行的普通写法。

    每条的第一行照样探日期前缀，所以「日期行分条」和「空行分条」不互斥：
    混着来的材料（多数条目带日期、少数没有）用这个方式最顺。
    """
    blocks: list[dict] = []
    for part in re.split(r"\n\s*\n", text):
        if not part.strip():
            continue
        lines = part.splitlines()
        date, inline = _date_in_line(lines[0].rstrip())
        if date:
            blocks.append(_block(date, inline, lines[1:]))
        else:
            blocks.append(_block(None, "", lines))
    return blocks


def _count_headings(text: str) -> int:
    return sum(1 for raw in text.splitlines()
               if _HEADING_RE.match(raw.rstrip()) or _BRACKET_RE.match(raw.rstrip()))


def _has_dated_line(text: str) -> bool:
    return any(_date_in_line(raw.rstrip())[0] for raw in text.splitlines())


def _finish(blocks: list[dict], source: str, default_kind: str,
            start: int = 1) -> list[RawItem]:
    """块 → RawItem：补齐标题、正文，编号。"""
    items: list[RawItem] = []
    for b in blocks:
        body = "\n".join(b["lines"]).strip()
        title = b["inline"] or _first_line(body)
        if not title and not body:
            continue
        if not title:
            title = _first_line(body) or "(无标题)"
        items.append(RawItem(ordinal=start + len(items), title=title, body=body,
                             date=b["date"], source=source,
                             kind=default_kind,
                             date_source="prefix" if b["date"] else ""))
    return items


# ─────────────────────────── 入口 ───────────────────────────
def parse_text(text: str, *, source: str = SOURCE_NAME, mode: str = "auto",
               default_kind: str = DEFAULT_KIND) -> ParseResult:
    """一段文本 → 若干 RawItem。分条方式可选，默认按最强信号自动判断。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if default_kind not in NEWS_KINDS:
        default_kind = "notices"
    if not text.strip():
        return ParseResult(warnings=[f"{source}：内容是空的，已跳过"], mode_used="whole")

    use = mode if mode in PARSE_MODES else "auto"
    if use == "auto":
        if _has_dated_line(text):
            use = "date"           # 日期前缀是最强信号，优先信它
        elif _count_headings(text) >= 2:
            use = "heading"
        else:
            use = "whole"

    doc_title = ""
    if use == "date":
        doc_title, blocks = _split_by_date(text)
    elif use == "heading":
        _, blocks = _split_by_heading(text)
    elif use == "blank":
        blocks = _split_by_blank(text)
    else:
        blocks = [_block(None, "", text.splitlines())]

    items = _finish(blocks, source, default_kind)
    if len(items) > 1 and not items[0].body:
        doc_title = doc_title or items[0].title     # 首块只有标题行 = 文档标题
        items = items[1:]
    for i, it in enumerate(items, 1):
        it.ordinal = i

    warnings: list[str] = []
    if use == "whole" and len(text) > 20000:
        warnings.append(f"{source}：没找到分条标记，整篇当成一条（{len(text)} 字）")
    undated = sum(1 for it in items if not it.date)
    if undated:
        warnings.append(f"{source}：{undated} 条没有日期前缀，将由本地模型补抽")
    if use == "date":
        # 一条正文里出现空行 = 很可能是两条材料被并进了一条。不自动切（会切碎正文），
        # 但必须说出来，否则使用者看不到被吞掉的那条。
        merged = sum(1 for it in items if re.search(r"\n\s*\n", it.body or ""))
        if merged:
            warnings.append(
                f"{source}：有 {merged} 条的正文里含空行。若空行后其实是新的条目，"
                f"请改用「按空行分条」，或给那条补一个日期前缀")
    return ParseResult(items=items, doc_title=doc_title, warnings=warnings,
                       mode_used=use)


def parse_sources(sources, *, mode: str = "auto",
                  default_kind: str = DEFAULT_KIND) -> ParseResult:
    """多份材料（拖进来的每个文件 / 每次粘贴）合成一个批次。"""
    out = ParseResult(mode_used=mode)
    for s in sources or []:
        name = str((s or {}).get("name") or SOURCE_NAME).strip() or SOURCE_NAME
        res = parse_text((s or {}).get("content") or "", source=name, mode=mode,
                         default_kind=default_kind)
        out.items.extend(res.items)
        out.warnings.extend(res.warnings)
        out.doc_title = out.doc_title or res.doc_title
        if mode == "auto" and res.mode_used != "whole":
            out.mode_used = res.mode_used
    for i, it in enumerate(out.items, 1):
        it.ordinal = i
    return out


_SNIFF_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")


def decode_bytes(raw: bytes) -> str:
    """按几种常见编码依次试读，全失败就 replace 兜底。

    中文材料在老机器上常是 GBK；用户从 Word / 记事本另存的也未必是 UTF-8。
    桌面应用把文件原样传成 base64，也是走这里解码的。
    """
    for enc in _SNIFF_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(path) -> str:
    """读一个本地文本文件（编码交给 decode_bytes 猜）。"""
    from pathlib import Path
    return decode_bytes(Path(path).read_bytes())


def parse_file(path, *, mode: str = "auto",
               default_kind: str = DEFAULT_KIND) -> ParseResult:
    """读一个本地文本文件并切条。"""
    from pathlib import Path
    p = Path(path)
    return parse_text(read_text(p), source=p.name, mode=mode,
                      default_kind=default_kind)


# ─────────────────────────── 落到 NewsDoc ───────────────────────────
def _uid_for(date: str | None, title: str, body: str) -> str:
    """内容指纹当主键：同一段文本拖两次是同一行，改了就换一行。

    出处（文件名）故意不进指纹 —— 同一个文件拖两次不该变成两条。
    """
    h = hashlib.sha1()
    for part in (date or "", (title or "").strip(), (body or "").strip()):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return f"{SOURCE_TAG}:{h.hexdigest()[:16]}"


def split_uid(uid: str) -> str:
    """uid → 存进 post_id 列的那一段（排序时的稳定次序键）。"""
    return uid.split(":", 1)[-1] if uid else ""


def to_docs(items, *, subject: str = "") -> list:
    """RawItem → NewsDoc。未知日期用占位时间戳，升序后自然落在末尾。"""
    from news_workflow import NewsDoc      # 延迟导入：只解析不跑模型时不必装 MAF
    fetched = now_iso()
    docs = []
    for it in items:
        date = normalize_date(it.date) if it.date else None
        docs.append(NewsDoc(
            uid=_uid_for(date, it.title, it.body),
            title=it.title or _first_line(it.body) or "(无标题)",
            text=it.body,
            created_at=date_to_ts(date),
            created_iso=(f"{date}T00:00:00+08:00" if date else ""),
            url="",
            source_api=SOURCE_TAG,
            source_name=it.source or SOURCE_NAME,
            fetched_at=fetched,
            kind=it.kind if it.kind in NEWS_KINDS else DEFAULT_KIND,
            game=subject or "",
        ))
    docs.sort(key=lambda d: (d.created_at, d.uid))
    return docs


# ─────────────────────────── 模型兜底抽日期 ───────────────────────────
DATE_INSTRUCTIONS = (
    "你是日期抽取器。用户给你一段中文材料的标题和正文，你判断这条材料的发布日期。"
    "只输出一个 YYYY-MM-DD 格式的日期；如果正文里没有任何能定位到具体某一天的信息，"
    "就只输出「未知」两个字。不要解释，不要输出任何别的内容，不要用 Markdown。"
)


class DateFiller:
    """给没有日期前缀的条目补抽日期。

    这是**兜底**，不是主力：日期前缀能认出来就不叫模型。抽不到就老实留「日期未知」，
    绝不能让它编一个日子出来 —— 编出来的日期会直接污染时间轴和节奏统计。
    """

    def __init__(self, backend, *, concurrency: int = 4,
                 doc_chars: int = 1200, max_tokens: int = 64) -> None:
        self.backend = backend
        self.concurrency = max(1, int(concurrency))
        self.doc_chars = doc_chars
        self.max_tokens = max_tokens

    def _agent(self):
        from news_workflow import _budget_for, _agent as make_agent
        budget = _budget_for(self.backend, self.max_tokens)
        return make_agent(self.backend, DATE_INSTRUCTIONS, budget), budget

    async def _one(self, agent, item, budget: int) -> None:
        body = (item.body or "")[:self.doc_chars]
        prompt = (f"标题：{item.title}\n\n正文：\n{body}\n\n"
                  f"这条材料的发布日期是？")
        resp = await agent.run(prompt, options=agent_options(self.backend, budget))
        raw = (getattr(resp, "text", "") or "").strip()
        date = normalize_date(raw)
        item.date = date
        item.date_source = "model" if date else "unknown"
        item.note = "" if date else "正文里没有可确定的日期"

    async def fill(self, items) -> dict:
        """就地补日期，返回 {asked, resolved, unknown}。"""
        import asyncio
        targets = [i for i in items if not i.date]
        if not targets:
            return {"asked": 0, "resolved": 0, "unknown": 0}
        if self.backend is None or getattr(self.backend, "client", None) is None:
            for it in targets:
                it.date_source = "unknown"
                it.note = "没有可用的模型，日期保持未知"
            return {"asked": 0, "resolved": 0, "unknown": len(targets)}

        agent, budget = self._agent()
        sem = asyncio.Semaphore(self.concurrency)
        print(f"      · {len(targets)} 条缺日期，交 {self.backend.name} 逐条补抽",
              file=_stderr(), flush=True)

        async def run(it):
            async with sem:
                try:
                    await self._one(agent, it, budget)
                except Exception as exc:            # 单条失败不该毁掉整批
                    it.date_source = "unknown"
                    it.note = f"补抽失败：{type(exc).__name__}"

        await asyncio.gather(*(run(it) for it in targets))
        resolved = sum(1 for it in targets if it.date)
        print(f"      · 补抽完成：认得 {resolved} 条，仍有 "
              f"{len(targets) - resolved} 条日期未知", file=_stderr(), flush=True)
        return {"asked": len(targets), "resolved": resolved,
                "unknown": len(targets) - resolved}


def _stderr():
    import sys
    return sys.stderr
