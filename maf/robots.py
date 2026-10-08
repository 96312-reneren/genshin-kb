#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
robots.txt 解析与匹配 —— 按 RFC 9309（Robots Exclusion Protocol）实现。

覆盖的规则：
  * User-agent / Allow / Disallow / Crawl-delay / Sitemap
  * '*' 通配任意字符，结尾 '$' 表示完全匹配
  * 最长匹配优先；长度相同时 Allow 赢
  * 同一 agent 的多个分组要合并
  * 4xx -> 视为无限制；5xx / 取不到 -> 视为完全禁止（RFC 9309 §2.3.1.4）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

__all__ = ["Rule", "RobotsGroup", "RobotsPolicy", "parse_robots"]

# 匹配权重：越具体的 agent 令牌权重越高；'*' 权重 0
_WILDCARD = "*"


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern
    rx = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile("^" + rx + ("$" if end_anchor else ""))


class Rule:
    """一条 Allow / Disallow 规则。"""

    __slots__ = ("allow", "pattern", "_rx")

    def __init__(self, allow: bool, pattern: str) -> None:
        self.allow = allow
        self.pattern = pattern
        self._rx = _compile_pattern(pattern)

    def matches(self, path: str) -> bool:
        return self._rx.match(path) is not None

    @property
    def weight(self) -> int:
        """具体度：模式越长越具体（'$' 不计入）。"""
        return len(self.pattern.rstrip("$"))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Rule({'Allow' if self.allow else 'Disallow'}, {self.pattern!r})"


@dataclass
class RobotsGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None

    def specificity_for(self, user_agent: str) -> int:
        """该分组对本 UA 的具体度；-1 表示不匹配。"""
        ua = user_agent.lower()
        best = -1
        for token in self.agents:
            t = token.lower()
            if t == _WILDCARD:
                best = max(best, 0)
            elif t == ua or ua.startswith(t):
                best = max(best, len(t))
        return best


def parse_robots(text: str) -> tuple[list[RobotsGroup], list[str]]:
    """解析 robots.txt 文本，返回 (分组列表, sitemap 列表)。"""
    groups: list[RobotsGroup] = []
    sitemaps: list[str] = []
    current: RobotsGroup | None = None
    # 连续出现的 user-agent 行共享同一个分组体
    expecting_agent = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        fieldname, _, value = line.partition(":")
        fieldname = fieldname.strip().lower()
        value = value.strip()

        if fieldname == "user-agent":
            if current is None or not expecting_agent:
                current = RobotsGroup()
                groups.append(current)
            current.agents.append(value)
            expecting_agent = True
        elif fieldname == "sitemap":
            if value:
                sitemaps.append(value)
            expecting_agent = False
        elif fieldname in ("allow", "disallow"):
            if current is None:
                current = RobotsGroup(agents=[_WILDCARD])
                groups.append(current)
            expecting_agent = False
            # 空值的 Disallow/Allow 表示不设限制，直接跳过
            if not value:
                continue
            current.rules.append(Rule(fieldname == "allow", value))
        elif fieldname == "crawl-delay":
            expecting_agent = False
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                if current is None:
                    current = RobotsGroup(agents=[_WILDCARD])
                    groups.append(current)
                current.crawl_delay = delay
    return groups, sitemaps


class RobotsPolicy:
    """某个 origin 的 robots 判定结果。"""

    def __init__(self, *, status_code: int | None, text: str = "",
                 user_agent: str, source: str = "") -> None:
        self.status_code = status_code
        self.user_agent = user_agent
        self.source = source
        self.sitemaps: list[str] = []
        self._groups: list[RobotsGroup] = []
        self._rules: list[Rule] = []
        self._crawl_delay: float | None = None
        self._mode = "rules"          # rules | allow-all | deny-all

        if status_code is None:
            # 取不到（超时 / DNS 失败等）：按 RFC 保守处理为完全禁止
            self._mode = "deny-all"
            return
        if 400 <= status_code < 500:
            # 明确不存在：视为无任何限制
            self._mode = "allow-all"
            return
        if status_code >= 500:
            self._mode = "deny-all"
            return

        self._groups, self.sitemaps = parse_robots(text)
        self._select_group()

    def _select_group(self) -> None:
        scored = [(g.specificity_for(self.user_agent), i, g)
                  for i, g in enumerate(self._groups)]
        matched = [(s, i, g) for s, i, g in scored if s >= 0]
        if not matched:
            return
        best = max(s for s, _, _ in matched)
        # RFC 9309：同一 agent 的多个分组必须合并
        for score, _, group in matched:
            if score == best:
                self._rules.extend(group.rules)
                if group.crawl_delay is not None:
                    self._crawl_delay = (group.crawl_delay if self._crawl_delay is None
                                         else min(self._crawl_delay, group.crawl_delay))

    # ── 查询接口 ──────────────────────────────────────────────
    @property
    def allowed(self) -> str:
        return self._mode

    def can_fetch(self, url: str) -> bool:
        if self._mode == "allow-all":
            return True
        if self._mode == "deny-all":
            return False
        path = _request_path(url)
        if path is None:
            return False
        best: Rule | None = None
        for rule in self._rules:
            if not rule.matches(path):
                continue
            if best is None or rule.weight > best.weight:
                best = rule
            elif rule.weight == best.weight and rule.allow and not best.allow:
                best = rule          # 长度相同 Allow 赢
        return True if best is None else best.allow

    @property
    def crawl_delay(self) -> float | None:
        if self._mode != "rules":
            return None
        return self._crawl_delay

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"RobotsPolicy(mode={self._mode}, rules={len(self._rules)}, "
                f"delay={self._crawl_delay})")


def _request_path(url: str) -> str | None:
    """取出用于匹配的 path?query 部分，按 RFC 做百分号编码。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path or "/"
    path = quote(path, safe="/%:@!$&'()*+,;=~-._")
    if parts.query:
        query = quote(parts.query, safe="/?%:@!$&'()*+,;=~-._[]")
        path = f"{path}?{query}"
    return path
