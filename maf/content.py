#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容层公共词汇 —— 类别、时区、时间渲染、来源与合规措辞。

这批名字原本住在 hoyo_api.py 里，跟着那个社区接口一起走。接口按使用者的
要求整个删掉之后，它们仍然是工作流层、存储层和渲染层共用的词汇表，
所以单独拆出一层：这里不导入任何网络库，也不会发任何请求。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "DISPLAY_TZ", "NEWS_KINDS", "KIND_LABEL", "KIND_ORDER", "DEFAULT_KIND",
    "UNKNOWN_TS", "UNKNOWN_LABEL",
    "DEFAULT_SUBJECT",
    "SOURCE_NAME", "SOURCE_NOTE", "SOURCE_ORIGIN", "SOURCE_TAG",
    "render_created", "is_known_date", "now_local", "now_iso",
]

# 报告里的日期一律按东八区展示 —— 使用者在国内，材料也是中文材料
DISPLAY_TZ = timezone(timedelta(hours=8))

NEWS_KINDS = ("notices", "events", "info")
KIND_ORDER = NEWS_KINDS
KIND_LABEL = {"notices": "公告", "events": "活动", "info": "资讯"}
# 人工投喂时无法自动分类，一律先归到「公告」；界面上可以逐条改
DEFAULT_KIND = "notices"
# 报告标题里的主题词。人工投喂什么材料都可能，界面上可以改
DEFAULT_SUBJECT = "原神"

# 「日期未知」的占位时间戳。取一个远未来的值，升序排序天然把它压到末尾，
# 不用在每一处排序里再写一遍 key=(is_unknown, ts) 这种复合键。
UNKNOWN_TS = 9999999999
UNKNOWN_LABEL = "日期未知"

# 来源：不再是任何接口，而是使用者自己拖进来 / 粘贴进来的文本
SOURCE_TAG = "manual"
SOURCE_NAME = "人工投喂文本"
SOURCE_ORIGIN = "使用者本人提供并拖入本应用的人工文本"
SOURCE_NOTE = ("材料由使用者本人提供，经大模型自动整理生成，"
               "未经人工逐条核实，不代表任何官方发布。")


def is_known_date(ts) -> bool:
    """真实日期返回 True；「日期未知」占位与 0/None 都算未知。"""
    if ts is None:
        return False
    try:
        return 0 < int(ts) < UNKNOWN_TS
    except (TypeError, ValueError):
        return False


def render_created(ts, with_time: bool = False) -> str:
    """Unix 秒 → 东八区字符串。未知日期返回「日期未知」，不返回 2286 年。"""
    if not is_known_date(ts):
        return UNKNOWN_LABEL
    dt = datetime.fromtimestamp(int(ts), DISPLAY_TZ)
    return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d")


def now_local() -> datetime:
    return datetime.now(DISPLAY_TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")
