#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 后端适配层 —— 一个工作流里同时挂「本地模型」和「云端模型」。

关键差异（踩过坑，别再踩）：

1. MAF 的 `OpenAIChatClient` 走的是 **OpenAI Responses API**（POST /v1/responses）。
   DesktopAI 只实现了 `/v1/chat/completions`，所以本地后端必须用
   `OpenAIChatCompletionClient`，否则直接 404。
2. 两边的 options 键不一样：
   - Responses 端：`{"max_tokens": N, "reasoning": {"effort": "low"}}`
   - Chat Completions 端：`{"max_tokens": N}`（没有 reasoning 键，传了会报错）
3. 本地服务（DesktopAI）端口是启动时随机分配的，token 由 App 侧生成，
   两者都要用户从 App 里读出来。这里提供探测工具帮忙确认。

桌面版（非 Android）可以固定端口启动：
    cargo run --release --example api_serve -- <模型.gguf> 11434 da-local
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient

from safety import NetworkGuard, NetworkGuardError

__all__ = ["Backend", "build_cloud_backend", "build_local_backend",
           "probe_local", "find_local_service", "agent_options",
           "load_env_file", "DEFAULT_ENV_FILE", "DEFAULT_LOCAL_BASE_URL",
           "COMMON_LOCAL_PORTS", "LOCAL_START_HINT", "local_ready",
           "_split_base"]

# 本地模型由使用者自己在 WSL 里起 —— 应用和脚本都不启动 WSL，也不留常驻进程。
# 这句话会在所有「连不上本地服务」的报错里原样出现，省得使用者去猜。
LOCAL_START_HINT = (
    "本地模型要你自己在 WSL 里起，这个应用不会替你启动 WSL：\n"
    "    wsl -d Ubuntu-22.04 -- ollama serve\n"
    "已经装成服务的也可以用：wsl -d Ubuntu-22.04 -- sudo systemctl start ollama\n"
    "起来之后再回来点「开始生成」；只想验证流程可以勾「试跑」（占位文案，不调模型）。")

# 跨平台：手机/Linux 上是 /root/.config/maf/credentials.env，
# 而 Path.home() 在 Linux 下就等于 /root，在 Windows 下是 C:\Users\<你>。
DEFAULT_ENV_FILE = Path(os.getenv("MAF_ENV_FILE")
                        or (Path.home() / ".config" / "maf" / "credentials.env"))
DEFAULT_CLOUD_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_CLOUD_MODEL = "deepseek-flash"

# 迁移后本地模型换成 WSL 里的 ollama（Windows 侧通过 WSL localhost 转发直连），
# 所以不再强制传 --local-url，默认就指到这里。
DEFAULT_LOCAL_BASE_URL = os.getenv("MAF_LOCAL_URL", "http://127.0.0.1:11434")

# ollama 默认 11434；llama.cpp / DesktopAI 等其它 OpenAI 兼容服务的端口一并扫
COMMON_LOCAL_PORTS = (11434, 8080, 8000, 8081, 5000, 5001, 1234, 7860,
                      9000, 9997, 8888, 4141, 3001, 8090, 18080, 18081)

_LOCAL_GUARD = NetworkGuard(loopback_only=True)     # 本地服务只可能在回环上


@dataclass
class Backend:
    name: str                    # "local" / "cloud"
    kind: str                    # "responses" / "chat_completions"
    client: Any
    model: str
    base_url: str
    healthy: bool = True
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.name}[{self.kind}] {self.model} @ {self.base_url}"


# ─────────────────────────── 凭据 ───────────────────────────
def load_env_file(path: str | Path = DEFAULT_ENV_FILE) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ─────────────────────────── 云端后端 ───────────────────────────
def build_cloud_backend(*, api_key: str | None = None,
                        base_url: str | None = None,
                        model: str | None = None) -> Backend:
    api_key = api_key or os.getenv("MAF_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"缺少云端 API Key（检查 {DEFAULT_ENV_FILE} 的 MAF_API_KEY）")
    base_url = base_url or os.getenv("MAF_BASE_URL", DEFAULT_CLOUD_BASE_URL)
    model = model or os.getenv("MAF_MODEL_ID", DEFAULT_CLOUD_MODEL)
    client = OpenAIChatClient(model=model, api_key=api_key, base_url=base_url)
    return Backend(name="cloud", kind="responses", client=client,
                   model=model, base_url=base_url)


# ─────────────────────────── 本地后端 ───────────────────────────
def _http_json(url: str, token: str | None = None, timeout: float = 3.0) -> dict:
    _LOCAL_GUARD.check_url(url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(65536).decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def _split_base(base_url: str) -> tuple[str, str]:
    """把地址拆成 (服务根, OpenAI base)。

    传进来的带不带 /v1 都行。为什么要拆：探测要打 <根>/v1/models，
    而 MAF 的 OpenAIChatCompletionClient 是拿 base_url 去拼 /chat/completions 的，
    所以 client 的 base_url 必须自带 /v1 —— ollama 只认 /v1/chat/completions，
    少了这一段会直接 404（llama.cpp 两种都收，所以手机上没暴露这个问题）。
    """
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")].rstrip("/")
    return root, root + "/v1"


def probe_local(base_url: str, token: str | None = None,
                timeout: float = 3.0) -> dict:
    """探测一个 OpenAI 兼容本地服务。返回 {health, ready, models, reachable, error}。"""
    root, openai_base = _split_base(base_url)
    report: dict[str, Any] = {"base_url": base_url.rstrip("/"),
                              "openai_base": openai_base, "reachable": False,
                              "health": None, "ready": None, "models": [], "error": None}
    for endpoint, key in (("/health", "health"), ("/ready", "ready")):
        try:
            report[key] = _http_json(root + endpoint, token, timeout)
            report["reachable"] = True
        except Exception as exc:                      # noqa: BLE001
            report.setdefault("errors", {})[endpoint] = f"{type(exc).__name__}: {exc}"
    try:
        payload = _http_json(openai_base + "/models", token, timeout)
        # ollama 模型库为空时返回 {"object":"list","data":null}，这里必须兜住 None
        report["reachable"] = True
        report["models"] = [m.get("id") for m in (payload.get("data") or [])
                            if m.get("id")]
    except Exception as exc:                          # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def local_ready(backend, timeout: float = 2.0) -> tuple[bool, str]:
    """本地服务现在在不在。返回 (可用, 说明)。

    云端后端、占位后端一律当成「可用」—— 这里只关心本地推理服务。
    """
    if backend is None or getattr(backend, "kind", "") != "chat_completions":
        return True, ""
    base = getattr(backend, "base_url", "") or DEFAULT_LOCAL_BASE_URL
    report = probe_local(base, timeout=timeout)
    models = [m for m in (report.get("models") or []) if m]
    if report.get("reachable") or models:
        return True, "、".join(models)
    return False, str(report.get("error") or "探测不到本地服务")


def find_local_service(*, token: str | None = None, host: str = "127.0.0.1",
                       ports: tuple[int, ...] = COMMON_LOCAL_PORTS,
                       scan_range: tuple[int, int] | None = None,
                       timeout: float = 0.35) -> list[tuple[str, dict]]:
    """扫回环上哪些端口在跑 OpenAI 兼容服务。默认只试常见端口。"""
    candidates = list(ports)
    if scan_range:
        lo, hi = scan_range
        candidates = [p for p in range(lo, hi + 1) if p not in candidates]

    def is_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0

    found: list[tuple[str, dict]] = []
    for port in candidates:
        if not is_open(port):
            continue
        base = f"http://{host}:{port}"
        report = probe_local(base, token, timeout=1.5)
        if report["reachable"] or report["models"]:
            found.append((base, report))
    return found


def build_local_backend(base_url: str, token: str | None = None,
                        model: str | None = None, *, strict: bool = True) -> Backend:
    """连本地 OpenAI 兼容服务。model 省略时自动从 /v1/models 取。"""
    load_env_file()
    base_url = (base_url or os.getenv("MAF_LOCAL_URL")
                or DEFAULT_LOCAL_BASE_URL).rstrip("/")
    token = token if token is not None else os.getenv("MAF_LOCAL_TOKEN", "")
    _LOCAL_GUARD.check_url(base_url)

    report = probe_local(base_url, token)
    if strict and not report["reachable"]:
        raise RuntimeError(
            f"本地服务 {base_url} 连不上：{report.get('error') or report.get('errors')}\n"
            f"  · 模型没拉：WSL 里 'ollama list' 看看有没有 qwen3:8b\n"
            f"  · 也可以先跑：python pipeline.py probe-local --scan\n"
            + LOCAL_START_HINT)
    if not model:
        if not report["models"]:
            raise RuntimeError(f"{base_url} 的 /v1/models 没返回任何模型，请用 --local-model 指定")
        model = report["models"][0]

    # 本地服务只实现 /v1/chat/completions（DesktopAI、llama.cpp、ollama 都是），
    # 所以必须用 Chat Completions 客户端，且 base_url 要带上 /v1。
    openai_base = report["openai_base"]
    client = OpenAIChatCompletionClient(model=model, api_key=token or "sk-local",
                                        base_url=openai_base)
    return Backend(name="local", kind="chat_completions", client=client,
                   model=model, base_url=openai_base,
                   healthy=report["reachable"], detail=report)


# ─────────────────────────── 生成参数 ───────────────────────────
def agent_options(backend: Backend, max_tokens: int) -> dict:
    """按后端类型给出正确的 options —— 两边键名不一样。"""
    if backend.kind == "responses":
        return {"max_tokens": max_tokens,
                "reasoning": {"effort": os.getenv("MAF_REASONING_EFFORT", "low")}}
    return {"max_tokens": max_tokens}
