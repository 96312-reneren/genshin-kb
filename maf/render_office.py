#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把工作流产物渲染成 office 文件（PPTX / DOCX）。

用 DSH 内置的 Python 跑：那边已经有 python-pptx 1.0.2 / python-docx 1.2.0 / Pillow 12。
不要用项目的 myvenv —— 那个是给 agent-framework 用的，没必要再往里塞绘图依赖。

输入（都由 pipeline.py news-report --deck 产出）：
    news_digest.json   时间轴 + 确定性统计 + 报告正文
    deck_spec.json     模型给的幻灯片要点（只有文字，没有排版）
输出：
    deck.pptx / report.docx
    figures/gen-*.png  Pillow 按数据画的信息图

为什么模型不直接产 pptx：
    大模型 API 返回的是文本，没有「产出 .pptx」这种接口。让模型写 python-pptx
    代码再执行，等于把任意代码执行引进流水线。所以分工是：模型只给要点 JSON，
    这一层负责全部排版 —— 版面可控、每页署名可控、结果可复现。

合规（这条是硬约束，不是偏好）：
    本文件不发起任何网络请求，也不读取任何外部图片。所有图形都由 Pillow 按
    news_digest.json 里的数据现画。既不用官方美术素材，也不引外链图片 ——
    作品里出现的每一个像素都是本地生成的。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _missing_dep(name: str) -> str:
    return (f"渲染 Office 产物需要 {name}，当前 Python（{sys.executable}）里没装。\n"
            "装上就行；文字版产物不受影响，report.md / timeline.md / "
            "news_digest.json 照常输出。\n"
            f"    {sys.executable} -m pip install Pillow python-pptx python-docx\n"
            "DSH 内置的 Python 一般已经带齐这三个包。")


# 绘图与 Office 三件套都是可选的：手机 proot 环境里常常没装。缺了要报一句人话，
# 而不是甩一段 ImportError 栈 —— 上层（app/server.py）是当子进程调它的。
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:                              # pragma: no cover
    raise SystemExit(_missing_dep(f"Pillow（缺 {exc.name}）")) from None

try:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Inches, Pt
except ImportError as exc:                              # pragma: no cover
    raise SystemExit(_missing_dep(f"python-pptx（缺 {exc.name}）")) from None

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt as DocxPt
except ImportError as exc:                              # pragma: no cover
    raise SystemExit(_missing_dep(f"python-docx（缺 {exc.name}）")) from None

# ── 主题与版式常量 ──────────────────────────────────────────────
INK = RGBColor(0x25, 0x2A, 0x38)
MUTED = RGBColor(0x6B, 0x72, 0x84)
ACCENT = RGBColor(0xC0, 0x94, 0x4C)
PRIMARY = RGBColor(0x2F, 0x3E, 0x5E)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CREAM = RGBColor(0xE8, 0xE1, 0xCE)
BG_HEX = (245, 242, 236)

SLIDE_W, SLIDE_H = 13.333, 7.5
M = 0.72                      # 左右安全边距
CW = SLIDE_W - 2 * M          # 内容宽度
BODY_TOP = 1.62               # 正文起始高度
FOOTER_Y = 6.86               # 页脚位置，留出 0.64in 下边距

FONT_CJK = "Microsoft YaHei"

# Pillow 只认字体文件路径，没有「系统字体名」这一说。按 Windows → macOS →
# Linux → Android 的顺序把常见中文字体排一遍。MAF_FONT 可以覆盖（指向自己那份字体）。
FONT_FILES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/fonts/NotoSansSC-Regular.otf",
    "/system/fonts/DroidSansFallback.ttf",
]

# 人工投喂版：所有材料都来自使用者本人，这份材料不访问任何网络接口
SUBJECT_FALLBACK = "原神"
SOURCE_NOTE = ("材料由使用者本人提供并拖入本应用；内容与相关商标版权归原权利人所有，"
               "仅作个人作品展示与非商业研究用途。")

KIND_CN = {"notices": "公告", "events": "活动", "info": "资讯"}
LANES = [("notices", "公告", (47, 62, 94)),
         ("events", "活动", (192, 148, 76)),
         ("info", "资讯", (110, 130, 120))]


_FONT_CACHE: dict[int, object] = {}
_font_warned = False


def _extra_font() -> str:
    import os
    return os.environ.get("MAF_FONT", "").strip()


def _pil_font(size: int):
    """按字号缓存字体对象。

    Pillow 每次 truetype() 都要重新解析字体文件（中文字体十几 MB 起步），
    而 bar_chart / timeline_strip 里同一个字号会被反复要。找不到中文字体时
    只警告一次 —— 悄悄画出一堆方框比报错还难查。
    """
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    global _font_warned
    extra = _extra_font()
    for path in ([extra] if extra else []) + FONT_FILES:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:                              # noqa: BLE001
            continue
        _FONT_CACHE[size] = font
        return font
    if not _font_warned:
        _font_warned = True
        print("没找到可用的中文字体，图上文字会变成方框。"
              "把字体文件路径写进环境变量 MAF_FONT 可以修好。", file=sys.stderr)
    fallback = ImageFont.load_default()
    _FONT_CACHE[size] = fallback
    return fallback


# ── 信息图：全部本地按数据生成 ──────────────────────────────────
def bar_chart(pairs, path: Path, *, width: int = 1000, height: int = 440) -> Path:
    """横向条形图。pairs = [(标签, 数值), ...]"""
    img = Image.new("RGB", (width, height), BG_HEX)
    d = ImageDraw.Draw(img)
    if not pairs:
        img.save(path)
        return path
    f_lab, f_val = _pil_font(26), _pil_font(26)
    top, bottom, left = 24, 24, 152
    right = int(max(110, max(d.textlength(str(v), font=f_val) for _, v in pairs) + 34))
    value_max = max(v for _, v in pairs) or 1
    row_h = max(30, (height - top - bottom) // len(pairs))
    bar_h = int(row_h * 0.5)
    track = width - left - right
    for i, (label, value) in enumerate(pairs):
        y = top + i * row_h + (row_h - bar_h) // 2
        mid = y + bar_h // 2
        d.text((26, mid), str(label), font=f_lab, fill=(37, 42, 56), anchor="lm")
        w = max(4, int(track * value / value_max))
        d.rounded_rectangle([left, y, left + track, y + bar_h], radius=bar_h // 2,
                            fill=(232, 228, 219))          # 底轨，让长短更好比
        d.rounded_rectangle([left, y, left + w, y + bar_h], radius=bar_h // 2,
                            fill=(47, 62, 94))
        d.text((left + w + 14, mid), str(value), font=f_val, fill=(37, 42, 56),
               anchor="lm")
    img.save(path)
    return path


def _node_ts(n) -> int:
    return int(n.get("created_at") or 0)


def _known_ts(ts: int) -> bool:
    """日期未知的条目用的是占位时间戳，不能拿它算坐标。"""
    return 0 < ts < 9999999999


def timeline_strip(nodes, path: Path, *, width: int = 1500, height: int = 430) -> Path:
    """事件线：每条内容按发布时间落在对应类别泳道上，空窗和高峰一眼可见。

    「日期未知」的条目时间戳是占位值（9999999999）：要是把它算进坐标范围，
    真实日期会被压到最左边几个像素里，整张图就废了。所以刻度只按已知日期算，
    未知的单独画成空心圈贴在轨道末端，另外注明条数。
    """
    img = Image.new("RGB", (width, height), BG_HEX)
    d = ImageDraw.Draw(img)
    if not nodes:
        img.save(path)
        return path
    f_lane, f_tick, f_note = _pil_font(26), _pil_font(21), _pil_font(21)
    known = [n for n in nodes if _known_ts(_node_ts(n))]
    unknown = [n for n in nodes if not _known_ts(_node_ts(n))]
    times = [_node_ts(n) for n in (known or nodes)]
    lo, hi = min(times), max(times)
    span = max(1, hi - lo)
    left, right, top = 148, 56, 74
    track = width - left - right
    lane_h = 78
    for li, (kind, label, color) in enumerate(LANES):
        cy = top + li * lane_h + 14
        d.text((24, cy), label, font=f_lane, fill=(37, 42, 56), anchor="lm")
        d.line([left, cy, left + track, cy], fill=(214, 208, 196), width=2)
        for n in known:
            if n.get("kind") != kind:
                continue
            x = left + int(track * (_node_ts(n) - lo) / span)
            d.ellipse([x - 7, cy - 7, x + 7, cy + 7], fill=color)
        if any(n.get("kind") == kind for n in unknown):
            x = left + track
            d.ellipse([x - 8, cy - 8, x + 8, cy + 8], outline=color, width=3)
    axis_y = top + len(LANES) * lane_h + 4
    d.line([left, axis_y, left + track, axis_y], fill=(160, 152, 138), width=2)
    tz = timezone(timedelta(hours=8))
    cur = datetime.fromtimestamp(lo, tz).replace(day=1)
    while cur.timestamp() <= hi:
        x = left + int(track * (cur.timestamp() - lo) / span)
        d.line([x, axis_y, x, axis_y + 9], fill=(160, 152, 138), width=2)
        label = cur.strftime("%Y-%m")
        # 首尾两个刻度会被画布边缘切掉，把标签夹回画布内
        tw = d.textlength(label, font=f_tick)
        lx = min(max(x, tw / 2 + 3), width - tw / 2 - 3)
        d.text((lx, axis_y + 16), label, font=f_tick, fill=(107, 114, 132),
               anchor="ma")
        cur = (cur.replace(day=28) + timedelta(days=5)).replace(day=1)
    if unknown:
        d.text((left, axis_y + 42),
               f"另有 {len(unknown)} 条日期未知（空心圈，不参与刻度）",
               font=f_note, fill=(150, 146, 138))
    img.save(path)
    return path


# ── 读输入 ──────────────────────────────────────────────────────
def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _meta(digest: dict) -> dict:
    stats = digest.get("stats") or {}
    gen = digest.get("generated_by") or {}
    summ = gen.get("summarizer") or {}
    cond = gen.get("condenser") or summ
    materials = [m for m in (digest.get("source_files") or []) if m]
    return {
        "items": digest.get("items", 0),
        "span": digest.get("span", ""),
        "subject": digest.get("subject") or SUBJECT_FALLBACK,
        "materials": materials or ["人工投喂文本"],
        "unknown": digest.get("unknown_dates", 0),
        "model": f"{summ.get('backend', '?')}/{summ.get('model', '?')}",
        "condenser": f"{cond.get('backend', '?')}/{cond.get('model', '?')}",
        "stats": stats,
        "kinds": stats.get("by_kind") or {},
    }


# ── PPTX ───────────────────────────────────────────────────────
def _cjk(run, name: str = FONT_CJK) -> None:
    run.font.name = name
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", name)


def _textbox(slide, text, l, t, w, h, *, size=18, bold=False, color=INK,
             align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    _cjk(run)
    return box


def _bullets(slide, items, l, t, w, h, *, size=17, color=INK, gap=10):
    box = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        run = p.add_run()
        run.text = "· " + str(item)
        run.font.size = Pt(size)
        run.font.color.rgb = color
        _cjk(run)
    return box


def _no_style(shp) -> None:
    """把 add_shape 自动带的 <p:style> 摘掉。

    只设 shadow.inherit = False 还不够 —— 主题样式里的 effectRef 仍会让
    LibreOffice / PowerPoint 给形状加上一层投影，看上去很旧。
    """
    el = shp._element.find(qn("p:style"))
    if el is not None:
        shp._element.remove(el)


def _rect(slide, l, t, w, h, color):
    shp = slide.shapes.add_shape(1, Inches(l), Inches(t), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    _no_style(shp)
    return shp


def _footer(slide, meta: dict) -> None:
    mats = " ｜ ".join(meta["materials"])
    line = (f"材料：{mats}    ｜    {meta['items']} 条    ｜    {SOURCE_NOTE}")
    box = slide.shapes.add_textbox(Inches(M), Inches(FOOTER_Y), Inches(CW), Inches(0.5))
    tf = box.text_frame
    tf.word_wrap = True
    run = tf.paragraphs[0].add_run()
    run.text = line
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED
    _cjk(run)


def _slide(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


def _kpi_card(slide, l: float, t: float, w: float, h: float,
              value: str, label: str) -> None:
    """一个数字卡：底色块 + 顶部色条 + 大号数字 + 小标签。"""
    _rect(slide, l, t, w, h, RGBColor(0xF7, 0xF4, 0xEE))
    _rect(slide, l, t, w, 0.055, ACCENT)
    _textbox(slide, value, l + 0.2, t + 0.26, w - 0.4, 0.72, size=30, bold=True,
             color=PRIMARY)
    _textbox(slide, label, l + 0.2, t + 1.0, w - 0.4, 0.4, size=13, color=MUTED)


def _badge(slide, n: int) -> None:
    """右上角的节次标记，让叙述页有编号可循。"""
    box = slide.shapes.add_shape(5, Inches(SLIDE_W - M - 1.0), Inches(0.5),
                                 Inches(1.0), Inches(0.5))
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(0xF0, 0xEA, 0xDC)
    box.line.fill.background()
    box.shadow.inherit = False
    _no_style(box)
    tf = box.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = f"{n:02d}"
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = ACCENT
    _cjk(run)


def _place(slide, png: Path, l: float, t: float, w: float) -> float:
    """按源图真实宽高比显式给出宽和高。

    只传 width 时由库去推 height，实测在本机的渲染链路上会跑偏
    （图片被放大并从右侧溢出画布）。这里自己算清楚，宽高都给死。
    """
    with Image.open(png) as im:
        h = w * im.height / im.width
    slide.shapes.add_picture(str(png), Inches(l), Inches(t), Inches(w), Inches(h))
    return h


def _titled(prs, title: str, sub: str = ""):
    """统一的内容页头：左侧色条 + 标题 + 可选副标题。"""
    s = _slide(prs)
    _rect(s, M, 0.46, 0.10, 0.58, ACCENT)
    _textbox(s, title, M + 0.26, 0.38, CW - 0.26, 0.8, size=27, bold=True, color=PRIMARY)
    if sub:
        _textbox(s, sub, M + 0.26, 1.12, CW - 0.26, 0.42, size=13, color=MUTED)
    return s


def build_pptx(digest: dict, spec: dict, out_dir: Path) -> Path:
    meta = _meta(digest)
    stats = meta["stats"]
    figs = out_dir / "figures"
    figs.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    # 关键：python-pptx 默认模板是 10x7.5in（4:3），改了宽高却不会改 sldSz 的
    # type 属性，会留下 type="screen4x3" 与 16:9 的真实尺寸自相矛盾。实测本机
    # 渲染链路会因此把整页内容按 1.5 倍放大 —— 表现为图表溢出画布、封面文字被裁。
    _sldsz = prs._element.find(qn("p:sldSz"))
    if _sldsz is not None:
        _sldsz.set("type", "screen16x9")

    deck_title = (spec.get("deck_title")
                  or f"{meta['subject']} 内容运营观察").strip()
    deck_sub = (spec.get("deck_subtitle")
                or f"{meta['items']} 条内容 · {meta['span']}").strip()

    # 1 封面
    s = _slide(prs)
    _rect(s, 0, 0, SLIDE_W, 3.0, PRIMARY)
    _rect(s, 0, 3.0, SLIDE_W, 0.07, ACCENT)
    _textbox(s, deck_title, M, 0.86, CW, 1.2, size=38, bold=True, color=WHITE)
    _textbox(s, deck_sub, M, 2.02, CW, 0.7, size=17, color=CREAM)
    _textbox(s, f"逐条概括 {meta['model']}    报告与文案 {meta['condenser']}",
             M, 3.45, CW, 0.5, size=14, color=MUTED)
    _textbox(s, "本材料由大模型基于使用者本人提供的材料自动生成，"
                "未经人工逐条核实，不代表任何官方发布。",
             M, 3.92, CW, 0.5, size=13, color=MUTED)
    if meta["kinds"]:
        kinds = "　".join(f"{KIND_CN.get(k, k)} {v} 条" for k, v in meta["kinds"].items())
        _textbox(s, kinds, M, 4.72, CW, 0.7, size=21, bold=True, color=ACCENT)
    _footer(s, meta)

    # 2 数据速览：四个数字卡 + 按月分布 + 节奏要点
    s = _titled(prs, "数据速览", "以下数字由程序从时间轴直接算出，不经过模型")
    cards = [
        (str(stats.get("count", 0)), "条内容"),
        (str(stats.get("span_days", 0)), "天跨度"),
        (str(stats.get("per_week", 0)), "条 / 周（平均）"),
        (str(len(stats.get("versions") or {})), "个版本节点"),
    ]
    gap = 0.30
    cw = (CW - gap * (len(cards) - 1)) / len(cards)
    for i, (value, label) in enumerate(cards):
        _kpi_card(s, M + i * (cw + gap), BODY_TOP + 0.05, cw, 1.5, value, label)

    months = list((stats.get("by_month") or {}).items())
    if months:
        p = bar_chart(months, figs / "gen-by-month.png")
        _place(s, p, M, BODY_TOP + 1.82, 6.35)
    _textbox(s, "节奏特征", 7.4, BODY_TOP + 1.9, 5.2, 0.5, size=17, bold=True,
             color=PRIMARY)
    months_all = stats.get("by_month") or {}
    peak = max(months_all.items(), key=lambda kv: kv[1]) if months_all else ("", 0)
    kinds_all = meta["kinds"] or {}
    top_kind = max(kinds_all.items(), key=lambda kv: kv[1]) if kinds_all else ("", 0)
    versions_all = stats.get("versions") or {}
    facts = [
        f"相邻内容间隔中位数 {stats.get('median_gap', 0)} 天，"
        f"最长空窗 {stats.get('max_gap', 0)} 天。",
        (f"发布最集中的一个月是 {peak[0]}（{peak[1]} 条），"
         f"时间轴共覆盖 {len(months_all)} 个月。"
         if months_all else "材料里没有能定位到日期的条目，跨度与节奏都算不出来。"),
        (f"条目里最多的是{KIND_CN.get(top_kind[0], top_kind[0])}"
         f"（{top_kind[1]} 条）；版本节点 {len(versions_all)} 个。"),
    ]
    _bullets(s, facts, 7.4, BODY_TOP + 2.5, 5.2, 2.6, size=14, gap=12)
    _footer(s, meta)

    # 3 时间轴事件线
    tl = digest.get("timeline") or []
    if tl:
        s = _titled(prs, "时间轴事件线", "每条内容按发布时间落在对应类别上")
        p = timeline_strip(tl, figs / "gen-timeline.png")
        _place(s, p, M, BODY_TOP + 0.1, CW)
        _textbox(s, "三条泳道分别是公告 / 活动 / 资讯；点越密，发布越集中。",
                 M, BODY_TOP + 3.75, CW, 0.5, size=14, color=MUTED)
        _footer(s, meta)

    # 4 三类分布
    if meta["kinds"]:
        s = _titled(prs, "三类内容分布")
        pairs = [(KIND_CN.get(k, k), v) for k, v in meta["kinds"].items()]
        p = bar_chart(pairs, figs / "gen-by-kind.png", height=380)
        _place(s, p, M, BODY_TOP + 0.35, 5.8)
        _textbox(s, "为什么混成一条时间轴", 7.0, BODY_TOP + 0.15, 5.6, 0.6,
                 size=19, bold=True, color=PRIMARY)
        _bullets(s, ["三类材料由使用者一次性投喂，排在一起才看得出运营节奏。",
                     "类别只作为节点标签，不做分区罗列。",
                     (f"{peak[0]} 的 {peak[1]} 条是这段材料里的密度高点，"
                      "只有混排才一眼看得出来。"
                      if months_all else "日期缺失时，这张图只统计能定位到日期的条目。")],
                 7.0, BODY_TOP + 0.95, 5.6, 3.4, size=15)
        _footer(s, meta)

    # 5..N 模型的叙述小节
    for idx, sec in enumerate(spec.get("sections") or [], 1):
        s = _titled(prs, str(sec.get("heading", "")))
        _badge(s, idx)
        _bullets(s, [str(b) for b in (sec.get("bullets") or [])], M, BODY_TOP + 0.35,
                 CW - 0.6, 4.6, size=18, gap=16)
        _footer(s, meta)

    # 版本节点
    versions = stats.get("versions") or {}
    if versions:
        s = _titled(prs, "版本节点")
        _bullets(s, [f"{k}：{v} 条" for k, v in versions.items()],
                 M, BODY_TOP + 0.35, CW, 3.6, size=18, gap=16)
        _textbox(s, f"全部 {stats.get('count', 0)} 条明细见随附的 report.docx 与 timeline.md。",
                 M, 5.6, CW, 0.6, size=14, color=MUTED)
        _footer(s, meta)

    # 结尾：来源与合规
    s = _titled(prs, "数据来源与合规声明")
    _bullets(s, [
        *[f"材料：{m}" for m in meta["materials"]],
        f"主题：《{meta['subject']}》｜材料由使用者本人提供，共 {len(meta['materials'])} 份。",
        "本材料由大模型自动生成，未经人工逐条核实，不代表任何官方发布。",
        "材料内容与相关商标版权归原权利人所有，此处仅作个人作品展示与非商业研究用途。",
        "本应用不访问任何网络接口、不抓取任何站点、不使用任何游戏客户端文件。",
        "全文图表均由本地程序按数据生成，未内嵌任何官方美术素材或外链图片。",
    ], M, BODY_TOP + 0.25, CW, 4.7, size=15, gap=12)
    _footer(s, meta)

    out = out_dir / "deck.pptx"
    prs.save(out)
    return out


# ── DOCX ───────────────────────────────────────────────────────
def _docx_cjk(run, name: str = FONT_CJK) -> None:
    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()
    rFonts.set(qn("w:eastAsia"), name)


def _para(doc, text, *, size=11, bold=False, style=None, align=None):
    p = doc.add_paragraph(style=style)
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    run.font.size = DocxPt(size)
    run.font.bold = bold
    _docx_cjk(run)
    return p


def build_docx(digest: dict, spec: dict, out_dir: Path) -> Path:
    meta = _meta(digest)
    stats = meta["stats"]
    doc = Document()

    _para(doc, spec.get("deck_title") or f"{meta['subject']} 内容运营观察报告",
          size=20, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    _para(doc, f"{meta['items']} 条 · {meta['span']} · 逐条概括 {meta['model']} · "
               f"报告 {meta['condenser']}",
          size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
    _para(doc, f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
          size=9, align=WD_ALIGN_PARAGRAPH.CENTER)

    _para(doc, "数据来源", size=13, bold=True, style="Heading 1")
    for m in meta["materials"]:
        _para(doc, f"材料：{m}", size=10)
    _para(doc, f"主题：《{meta['subject']}》；材料由使用者本人提供，本应用不访问任何网络接口。",
          size=10)

    _para(doc, "一、数据速览（由时间轴直接算出，非模型生成）", size=13, bold=True,
          style="Heading 1")
    rows = [
        ("条目数", f"{stats.get('count', 0)} 条"),
        ("时间跨度", f"{stats.get('start', '')} ~ {stats.get('end', '')}"
                     f"（{stats.get('span_days', 0)} 天）"),
        ("按类别", "、".join(f"{KIND_CN.get(k, k)} {v} 条"
                             for k, v in meta["kinds"].items())),
        ("按月分布", "、".join(f"{k} {v} 条"
                               for k, v in (stats.get("by_month") or {}).items())),
        ("按星期分布", "、".join(f"{k} {v}"
                                 for k, v in (stats.get("by_weekday") or {}).items() if v)),
        ("相邻间隔", f"中位数 {stats.get('median_gap', 0)} 天，"
                     f"最长 {stats.get('max_gap', 0)} 天"),
        ("平均频率", f"每周 {stats.get('per_week', 0)} 条"),
        ("版本节点", "、".join(f"{k} {v} 条"
                               for k, v in (stats.get("versions") or {}).items())),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for k, v in rows:
        cells = table.add_row().cells
        r0 = cells[0].paragraphs[0].add_run(k)
        _docx_cjk(r0)
        r0.font.bold = True
        r1 = cells[1].paragraphs[0].add_run(str(v))
        _docx_cjk(r1)

    _para(doc, "二、运营观察（大模型生成）", size=13, bold=True, style="Heading 1")
    body = digest.get("report_body") or ""
    if body.strip():
        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## "):
                _para(doc, line[3:].strip(), size=12, bold=True, style="Heading 2")
            else:
                _para(doc, line, size=11)
    else:
        _para(doc, "（本次未生成报告正文）", size=11)

    _para(doc, "三、时间轴（按发布时间升序）", size=13, bold=True, style="Heading 1")
    tl = doc.add_table(rows=1, cols=4)
    tl.style = "Light Grid Accent 1"
    for i, head in enumerate(("序号", "日期", "类别", "标题 / 一句话概括")):
        run = tl.rows[0].cells[i].paragraphs[0].add_run(head)
        _docx_cjk(run)
        run.font.bold = True
    for node in digest.get("timeline") or []:
        cells = tl.add_row().cells
        cells[0].text = str(node.get("ordinal", ""))
        cells[1].text = str(node.get("date", ""))
        cells[2].text = str(node.get("kind_label", ""))
        p = cells[3].paragraphs[0]
        r = p.add_run(str(node.get("title", "")))
        _docx_cjk(r)
        r.font.bold = True
        p2 = cells[3].add_paragraph()
        r2 = p2.add_run(str(node.get("summary", "")))
        _docx_cjk(r2)
        r2.font.size = DocxPt(9)

    _para(doc, "四、完整出处清单", size=13, bold=True, style="Heading 1")
    for node in digest.get("timeline") or []:
        _para(doc, f"{node.get('ordinal')}. {node.get('date')} · "
                   f"[{node.get('kind_label')}] {node.get('title')} — {node.get('url', '')}",
              size=9)

    _para(doc, "五、合规声明", size=13, bold=True, style="Heading 1")
    gen = digest.get("generated_by") or {}
    condenser = (gen.get("condenser") or {}).get("backend", "local")
    writer = ("本地模型（非云端）" if str(condenser).startswith("local")
              else f"云端模型（{condenser}）")
    _para(doc, f"本报告由{writer}基于使用者本人提供的材料自动生成，"
               f"未经人工逐条核实，不代表任何官方发布。", size=10)
    _para(doc, "本应用不访问任何网络接口、不抓取任何站点、不使用任何游戏客户端文件。",
          size=10)
    _para(doc, "未内嵌任何官方美术素材或外链图片，全文图表均由本地程序按数据生成。",
          size=10)
    _para(doc, SOURCE_NOTE, size=10)

    out = out_dir / "report.docx"
    doc.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="把工作流产物渲染成 PPTX / DOCX")
    ap.add_argument("--digest", type=Path, required=True)
    ap.add_argument("--deck", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    digest = load_json(args.digest)
    spec = load_json(args.deck) if args.deck and Path(args.deck).exists() else {}
    if not isinstance(spec, dict):
        spec = {}
    args.out_dir.mkdir(parents=True, exist_ok=True)

    pp = build_pptx(digest, spec, args.out_dir)
    dd = build_docx(digest, spec, args.out_dir)
    print(f"PPTX: {pp}")
    print(f"DOCX: {dd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
