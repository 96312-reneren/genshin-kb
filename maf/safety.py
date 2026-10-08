#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络护栏 —— 让爬虫在物理上无法跑到不该去的地方。

两层防护：
  1. NetworkGuard：每一次请求前做主机校验（协议、主机白名单、loopback 限定、
     私网/链路本地/保留地址拒绝）。
  2. no_external_socket()：上下文管理器，直接把 socket.getaddrinfo 和
     socket.create_connection 换成带校验的版本。测试期间即使代码写错，
     也解析不了外网域名、连不上外部 IP。
"""
from __future__ import annotations

import contextlib
import ipaddress
import socket
from urllib.parse import urlsplit

__all__ = ["NetworkGuardError", "NetworkGuard", "no_external_socket", "is_loopback_host"]

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class NetworkGuardError(RuntimeError):
    """违反了网络护栏。"""


def _strip_brackets(host: str) -> str:
    return host.strip("[]").lower()


def is_loopback_host(host: str) -> bool:
    """主机名/IP 是否指向本机回环。"""
    h = _strip_brackets(host or "")
    if not h:
        return False
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _is_private_target(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


class NetworkGuard:
    """逐请求校验目标主机。"""

    def __init__(self, *, allowed_hosts: set[str] | None = None,
                 loopback_only: bool = False, allow_private: bool = False,
                 allowed_ports: set[int] | None = None) -> None:
        self.allowed_hosts = ({h.lower() for h in allowed_hosts}
                              if allowed_hosts else None)
        self.loopback_only = loopback_only
        self.allow_private = allow_private
        self.allowed_ports = allowed_ports
        self.blocked: list[tuple[str, str]] = []      # (url, 原因)
        self.allowed: list[str] = []

    # ── 校验 ────────────────────────────────────────────────
    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            self._reject(url, f"协议不允许: {parts.scheme or '(空)'}")
        if not parts.hostname:
            self._reject(url, "缺少主机名")
        host = parts.hostname.lower()
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            self._reject(url, f"主机不在白名单: {host}")
        if parts.port is not None and self.allowed_ports is not None \
                and parts.port not in self.allowed_ports:
            self._reject(url, f"端口不在白名单: {parts.port}")

        # 显式写进白名单的主机，用户已经拍过板了，不再按内网地址拒绝
        explicitly_allowed = self.allowed_hosts is not None and host in self.allowed_hosts

        if self.loopback_only:
            if not is_loopback_host(host):
                self._reject(url, f"loopback 模式下禁止访问非本机主机: {host}")
            for ip in self._resolve(host):
                if not ip.is_loopback:
                    self._reject(url, f"{host} 解析到了非回环地址 {ip}")
        elif not self.allow_private and not explicitly_allowed:
            for ip in self._resolve(host):
                if _is_private_target(ip):
                    self._reject(url, f"{host} 解析到了内网/保留地址 {ip}")

        self.allowed.append(url)
        return host

    def _resolve(self, host: str) -> list[ipaddress._BaseAddress]:
        if is_loopback_host(host):
            return [ipaddress.ip_address("127.0.0.1")]
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            self._reject(host, f"DNS 解析失败: {exc}")
            return []
        out = []
        for info in infos:
            addr = info[4][0]
            try:
                out.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
        if not out:
            self._reject(host, "DNS 没有返回可用地址")
        return out

    def _reject(self, url: str, reason: str) -> None:
        self.blocked.append((url, reason))
        raise NetworkGuardError(f"{reason}  ← {url}")

    def is_allowed(self, url: str) -> bool:
        """静默探测：只回答能否访问，不污染 blocked/allowed 记录。"""
        blocked_mark, allowed_mark = len(self.blocked), len(self.allowed)
        try:
            self.check_url(url)
            return True
        except NetworkGuardError:
            del self.blocked[blocked_mark:]
            del self.allowed[allowed_mark:]
            return False

    # ── 统计 ────────────────────────────────────────────────
    @property
    def blocked_count(self) -> int:
        return len(self.blocked)


@contextlib.contextmanager
def no_external_socket():
    """把 socket 层锁死在回环地址上，用于离线测试。"""
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not is_loopback_host(str(host)):
            raise NetworkGuardError(f"离线模式：禁止解析外部主机 {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not is_loopback_host(str(host)):
            raise NetworkGuardError(f"离线模式：禁止连接外部地址 {address!r}")
        return real_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.create_connection = guarded_create_connection
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.create_connection = real_create_connection
