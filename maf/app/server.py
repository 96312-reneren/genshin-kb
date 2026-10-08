#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工投喂桌面应用的后端：本机服务 + 界面 + 渲染管线。

设计前提：
- 服务只绑 127.0.0.1 的随机端口，界面就是 app/ui 下的静态文件；
- 应用唯一的输入是使用者拖进来 / 粘进来的文本，代码里不存在任何抓取；
- 生成是长任务，放后台线程跑，前端拿 job_id 轮询进度和日志。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UI_DIR = HERE / "ui"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_BODY = 8 * 1024 * 1024
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class UserError(Exception):
    """能直接给使用者看的错误（不打印堆栈）。"""


# ───────────────────── 外部工具定位 ─────────────────────
def _first_existing(candidates) -> str:
    for cand in candidates:
        if cand and Path(cand).exists():
            return str(cand)
    return ""


def find_node() -> str:
    return _first_existing((
        os.getenv("MAF_NODE"),
        shutil.which("node") or "",
        r"C:\Program Files\nodejs\node.exe",
        str(Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime"
            / "dependencies" / "node" / "bin" / "node.exe"),
    ))


def find_libreoffice_cli() -> str:
    env = os.getenv("MAF_LIBREOFFICE_CLI")
    if env and Path(env).exists():
        return env
    for base in (
        Path(r"C:\Users\TheUn\AppData\Local\Programs\DeepSeek Harness"
             r"\resources\app.asar.unpacked"),
        Path(r"C:\Users\TheUn\AppData\Local\Programs\DeepSeek Harness"
             r"\resources"),
    ):
        if not base.exists():
            continue
        for hit in sorted(base.glob("**/libreoffice-kit/lib/cli.js")):
            return str(hit)
    return ""


_RENDER_PY_CACHE: dict[str, str] = {}


def _has_render_libs(python: str) -> bool:
    """这个解释器能不能 import pptx / docx / PIL。"""
    if python in _RENDER_PY_CACHE:
        return bool(_RENDER_PY_CACHE[python])
    try:
        proc = subprocess.run([python, "-c", "import pptx, docx, PIL"],
                              capture_output=True, timeout=60)
        ok = proc.returncode == 0
    except Exception:                                      # noqa: BLE001
        ok = False
    _RENDER_PY_CACHE[python] = python if ok else ""
    return ok


def find_render_python() -> str:
    """渲染 PPT / Word 用一个装了 python-pptx / python-docx / Pillow 的解释器。

    优先 MAF_PYTHON；否则先看当前解释器（项目自带 venv），再退到 DSH 内置
    Python —— 那一个本来就带着这三个库。都没有就还用当前解释器，
    让报错信息原样抛给使用者。
    """
    cands = [os.getenv("MAF_PYTHON") or "", sys.executable,
             str(Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime"
                 / "dependencies" / "python" / "python.exe")]
    fallback = sys.executable
    for c in cands:
        if c and Path(c).exists() and _has_render_libs(c):
            return c
    return fallback


# ───────────────────── 任务与状态 ─────────────────────
class Job:
    """一次生成任务。日志逐行追加，前端轮询着看。"""

    def __init__(self, jid: str, label: str) -> None:
        self.id = jid
        self.label = label
        self.status = "running"          # running / done / failed
        self.log: list[str] = []
        self.artifacts: list[dict] = []
        self.error = ""
        self.summary: dict = {}
        self.started = time.time()

    def say(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "status": self.status,
                "log": self.log, "artifacts": self.artifacts,
                "error": self.error, "summary": self.summary,
                "elapsed": round(time.time() - self.started, 1)}


class AppState:
    def __init__(self, db: Path, out_dir: Path, *, host: str, port: int) -> None:
        self.db = Path(db)
        self.out_dir = Path(out_dir)
        self.host = host
        self.port = port
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.window = None

    def new_job(self, label: str) -> Job:
        job = Job(uuid.uuid4().hex[:12], label)
        with self.lock:
            self.jobs[job.id] = job
        return job

    def run_job(self, job: "Job", payload: dict) -> None:
        """后台线程入口：任何异常都变成任务面板上的一行，不静默。"""
        try:
            run_generate(self, job, payload)
        except UserError as exc:
            job.status = "failed"
            job.error = str(exc)
            job.say("失败：" + job.error)
        except Exception as exc:                           # noqa: BLE001
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.say("失败：" + job.error)
            for line in traceback.format_exc().splitlines():
                job.say("      " + line)
        else:
            job.status = "done"

    def shutdown(self) -> None:
        window = getattr(self, "window", None)
        if window is not None:
            try:
                window.destroy()
            except Exception:                              # noqa: BLE001
                pass
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()


def build_sources(payload: dict) -> list[dict]:
    """把前端传来的文件/粘贴文本收成 [{name, content}]，跳掉空内容。

    文件走 base64 原始字节：浏览器默认按 UTF-8 解码，GBK 材料会变乱码，
    所以解码统一交给 ingest.decode_bytes 在服务端做。
    """
    from ingest import decode_bytes
    sources: list[dict] = []
    for item in payload.get("files") or []:
        name = str(item.get("name") or "拖入的文本").strip()
        blob = item.get("data_b64")
        if blob:
            try:
                content = decode_bytes(base64.b64decode(blob))
            except Exception as exc:                       # noqa: BLE001
                content = ""
                name = f"{name}（读取失败：{type(exc).__name__}）"
        else:
            content = str(item.get("content") or "")
        if content.strip():
            sources.append({"name": name, "content": content})
    pasted = str(payload.get("text") or "").strip()
    if pasted:
        sources.append({"name": "粘贴文本", "content": pasted})
    return sources


def reset_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()

def convert_pdf(src: Path, node: str, cli: str, job: "Job") -> Path | None:
    """office → PDF。转换器要求输出文件不存在，所以先删旧的。"""
    target = src.with_suffix(".pdf")
    if target.exists():
        target.unlink()
    proc = subprocess.run([node, cli, "convert", "--input", str(src),
                           "--output", str(target)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not target.exists():
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip().replace("\n", " ")
        job.say(f"· {src.name} 转 PDF 失败：{detail[:160]}")
        return None
    return target


def list_artifacts(out_dir: Path) -> list[dict]:
    """产物区展示用的清单：只要最终交付物和中间文本，不要图片。"""
    wanted = [("deck.pptx", "PPT 演示文稿"), ("deck.pdf", "PPT 版 PDF"),
              ("report.docx", "Word 报告"), ("report.pdf", "Word 版 PDF"),
              ("report.md", "报告 Markdown"), ("timeline.md", "时间轴 Markdown"),
              ("news_digest.json", "工作流数据"), ("deck_spec.json", "幻灯片要点")]
    found = []
    for name, label in wanted:
        path = out_dir / name
        if path.exists():
            found.append({"name": name, "label": label, "path": str(path),
                          "size": path.stat().st_size})
    return found


def run_generate(state: "AppState", job: "Job", payload: dict) -> None:
    """投喂文本 → 条目 → 入库 → 工作流 → 报告 / 时间轴 / PPT / Word / PDF。"""
    import asyncio

    from content import DEFAULT_KIND, DEFAULT_SUBJECT, KIND_LABEL, KIND_ORDER, SOURCE_NAME
    from ingest import MODE_LABEL, DateFiller, parse_sources, to_docs
    from news_workflow import run_news_digest
    from pipeline import build_backend_for
    from store import Store

    sources = build_sources(payload)
    if not sources:
        raise UserError("还没有内容：把文本文件拖进左边的框，或者直接粘贴一段文本。")

    mode = str(payload.get("mode") or "auto")
    subject = str(payload.get("subject") or "").strip() or DEFAULT_SUBJECT
    kind = str(payload.get("kind") or DEFAULT_KIND)
    dry_run = bool(payload.get("dry_run"))
    fill = bool(payload.get("fill", True)) and not dry_run
    office = bool(payload.get("office", True))
    out_dir = Path(payload.get("out_dir") or state.out_dir).expanduser()
    try:
        concurrency = max(1, min(8, int(payload.get("concurrency") or 4)))
    except (TypeError, ValueError):
        concurrency = 4

    if payload.get("fresh"):
        job.say(f"按设置清空旧库：{state.db}")
        reset_db(state.db)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = parse_sources(sources, mode=mode, default_kind=kind)
    job.say(f"[1/4] 解析 {len(sources)} 份材料 → {len(result.items)} 条"
            f"（切分方式：{MODE_LABEL.get(result.mode_used, result.mode_used)}）")
    for warning in result.warnings:
        job.say("      · " + warning)
    if not result.items:
        raise UserError("解析出来 0 条内容：换一种切分方式，或者确认文本不是空的。")

    overrides = payload.get("overrides") or {}
    if isinstance(overrides, dict) and overrides:
        from ingest import normalize_date
        touched = 0
        for item in result.items:
            raw = overrides.get(str(item.ordinal))
            if raw is None:
                continue
            fixed = normalize_date(str(raw))
            item.date = fixed
            item.date_source = "手填" if fixed else "unknown"
            item.note = ((item.note + "；") if item.note else "") + \
                ("日期为手工填写" if fixed else "日期被改为未知")
            touched += 1
        if touched:
            job.say(f"      按预览里的改动覆盖了 {touched} 条的日期")

    kinds_over = payload.get("kinds") or {}
    if isinstance(kinds_over, dict) and kinds_over:
        from content import NEWS_KINDS
        changed = 0
        for item in result.items:
            want = str(kinds_over.get(str(item.ordinal)) or "")
            if want in NEWS_KINDS and want != item.kind:
                item.kind = want
                changed += 1
        if changed:
            job.say(f"      按预览里的改动调整了 {changed} 条的类别")

    name = str(payload.get("summarizer") or "local")
    condenser_name = str(payload.get("condenser") or name)
    local_url = str(payload.get("local_url") or "")
    summarizer = build_backend_for(name, dry_run=dry_run, local_url=local_url)
    condenser = build_backend_for(condenser_name, dry_run=dry_run, local_url=local_url)
    job.say(f"      逐条概括 = {summarizer}；报告与文案 = {condenser}")

    if not dry_run:
        # 本地模型是使用者自己在 WSL 里起的：这里只探测，探不到就把「怎么手动起来」
        # 原样告诉他，而不是让他在一堆连接错误里猜。
        from llm import LOCAL_START_HINT, local_ready
        for role, backend in (("逐条概括", summarizer), ("报告与文案", condenser)):
            ready, detail = local_ready(backend)
            if not ready:
                raise UserError(f"{role}要用的本地模型现在连不上（{detail}）。\n\n"
                                + LOCAL_START_HINT)
        if getattr(summarizer, "kind", "") == "chat_completions":
            job.say(f"      本地服务在线：{summarizer.base_url}")

    missing = [it for it in result.items if not it.date]
    if missing and fill:
        job.say(f"[2/4] {len(missing)} 条没写日期，交给 {summarizer} 从正文补抽…")
        try:
            info = asyncio.run(DateFiller(summarizer, concurrency=concurrency)
                               .fill(result.items))
            job.say(f"      补到 {info['resolved']} 条，仍未知 {info['unknown']} 条")
        except Exception as exc:                                   # noqa: BLE001
            job.say(f"      补日期失败，按「日期未知」继续：{exc}")
    elif missing:
        job.say(f"[2/4] {len(missing)} 条没写日期，已编号排在时间轴末尾")
    else:
        job.say("[2/4] 每条都自带日期，不用模型补。")

    docs = to_docs(result.items, subject=subject)
    with Store(state.db) as store:
        run_id = store.start_news_run(SOURCE_NAME, subject)
        counts = store.upsert_news([d.to_row() for d in docs], run_id)
        store.finish_news_run(run_id, fetched=len(docs),
                              inserted=counts["inserted"],
                              updated=counts["updated"], ok=True,
                              note=f"app/{result.mode_used}")
        stats = store.news_stats()
        by_kind = store.news_kind_counts()
        rows = list(store.iter_news(kinds=list(KIND_ORDER)))
    kinds_text = "、".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in by_kind.items())
    job.say(f"      入库（按发布时间升序）：新增 {counts['inserted']}，"
            f"更新 {counts['updated']}；库内共 {stats['总数']} 条（{kinds_text}）")
    if stats["日期未知"]:
        job.say(f"      其中 {stats['日期未知']} 条日期未知，排在时间轴末尾")

    job.say(f"[3/4] 跑工作流：逐条概括 {len(rows)} 条"
            + ("，写报告，再规划幻灯片要点" if office else "，再写报告") + " …")
    digest = asyncio.run(run_news_digest(
        docs, summarizer, condenser, concurrency=concurrency, subject=subject,
        with_deck=office, dry_run=dry_run))
    job.say(f"      工作流完成，用时 {digest.elapsed:.1f}s")

    with Store(state.db) as store:
        store.save_news_summaries(
            [{"uid": n.uid, "ordinal": n.ordinal, "backend": digest.backend,
              "model": digest.model, "summary": n.summary} for n in digest.nodes])
        store.save_news_report(backend=digest.condenser_backend or digest.backend,
                               model=digest.condenser_model or digest.model,
                               items=len(digest.nodes), span=digest.span,
                               source_api=digest.source_api,
                               timeline=digest.timeline_md,
                               report=digest.report_md)

    report_path = out_dir / "report.md"
    timeline_path = out_dir / "timeline.md"
    json_path = out_dir / "news_digest.json"
    report_path.write_text(digest.report_md, encoding="utf-8")
    timeline_path.write_text(digest.timeline_md, encoding="utf-8")
    json_path.write_text(json.dumps(digest.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    job.say(f"      报告 / 时间轴已写出：{report_path.name}、{timeline_path.name}")

    if office:
        job.say("[4/4] 渲染 PPT 与 Word …")
        spec_path = out_dir / "deck_spec.json"
        spec_path.write_text(json.dumps(digest.deck_spec or {}, ensure_ascii=False,
                                        indent=2), encoding="utf-8")
        render_py = find_render_python()
        if Path(render_py) != Path(sys.executable):
            job.say(f"      渲染用解释器：{render_py}")
        cmd = [render_py, str(ROOT / "render_office.py"),
               "--digest", str(json_path), "--deck", str(spec_path),
               "--out-dir", str(out_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        for line in (proc.stdout or "").splitlines():
            job.say("      " + line.strip())
        if proc.returncode != 0:
            raise UserError("PPT / Word 渲染失败："
                            + ((proc.stderr or proc.stdout or "").strip()[-300:]))
        node, cli = find_node(), find_libreoffice_cli()
        if node and cli:
            for src in (out_dir / "deck.pptx", out_dir / "report.docx"):
                if src.exists():
                    job.say(f"      转 PDF：{src.name}")
                    convert_pdf(src, node, cli, job)
        else:
            job.say("      · 没找到 node 或 LibreOffice 转换器，跳过 PDF（PPTX/DOCX 已生成）")
    else:
        job.say("[4/4] 按设置跳过 PPT / Word 渲染")

    job.artifacts = list_artifacts(out_dir)
    job.summary = {"items": len(digest.nodes), "span": digest.span,
                   "subject": subject, "unknown_dates": digest.unknown_dates,
                   "model": str(digest.condenser_model or digest.model),
                   "out_dir": str(out_dir), "elapsed": round(digest.elapsed, 1)}
    job.say(f"完成：{len(job.artifacts)} 个产物在 {out_dir}")

# ───────────────────── HTTP 接口 ─────────────────────
def safe_ui_file(rel: str) -> Path | None:
    """把 URL 路径映射到 app/ui 下的文件，挡掉 ../ 穿越。"""
    try:
        target = (UI_DIR / rel).resolve()
    except OSError:
        return None
    if target.is_file() and target.is_relative_to(UI_DIR.resolve()):
        return target
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "maf-desktop"
    state: "AppState"

    # 日志静音：界面上的任务面板已经够了
    def log_message(self, fmt, *args) -> None:            # noqa: A003
        pass

    # ── 回包 ──
    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(path.suffix.lower(),
                                                   "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        """只收 application/json —— 跨站页面发不出这种请求（会先被预检挡掉）。"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise UserError("请求格式不对：只接受 application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise UserError("请求体为空或太大")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UserError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(data, dict):
            raise UserError("请求体必须是一个 JSON 对象")
        return data

    # ── GET ──
    def do_GET(self) -> None:                              # noqa: N802
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                self.send_file(UI_DIR / "index.html")
                return
            if route == "/api/state":
                self.send_json(self.api_state())
                return
            if route.startswith("/api/job/"):
                job = self.state.jobs.get(route.rsplit("/", 1)[-1])
                if job is None:
                    self.send_json({"error": "没有这个任务"}, 404)
                else:
                    self.send_json(job.as_dict())
                return
            if route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            found = safe_ui_file(unquote(route).lstrip("/"))
            if found:
                self.send_file(found)
            else:
                self.send_json({"error": "没有这个资源"}, 404)
        except UserError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:                           # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def api_state(self) -> dict:
        from content import DEFAULT_KIND, DEFAULT_SUBJECT, KIND_LABEL, KIND_ORDER
        from ingest import MODE_LABEL, PARSE_MODES
        out_dir = self.state.out_dir
        state = {
            "db": str(self.state.db), "out_dir": str(out_dir),
            "defaults": {"subject": DEFAULT_SUBJECT, "kind": DEFAULT_KIND,
                         "mode": "auto"},
            "kind_options": [{"value": k, "label": KIND_LABEL.get(k, k)}
                             for k in KIND_ORDER],
            "mode_options": [{"value": m,
                              "label": MODE_LABEL.get(m, m)} for m in PARSE_MODES],
            "node": bool(find_node()),
            "libreoffice": bool(find_libreoffice_cli()),
            "artifacts": list_artifacts(out_dir) if out_dir.exists() else [],
            "local": {},
        }
        try:
            from llm import probe_local
            report = probe_local(os.getenv("MAF_LOCAL_URL")
                                 or "http://127.0.0.1:11434", timeout=1.2)
            state["local"] = {"reachable": bool(report.get("reachable")),
                              "models": report.get("models") or [],
                              "error": report.get("error")}
        except Exception as exc:                           # noqa: BLE001
            state["local"] = {"reachable": False, "models": [],
                              "error": f"{type(exc).__name__}: {exc}"}
        return state

    # ── POST ──
    def do_POST(self) -> None:                             # noqa: N802
        route = urlparse(self.path).path
        try:
            payload = self.read_json()
            if route == "/api/parse":
                self.send_json(self.api_parse(payload))
            elif route == "/api/generate":
                self.send_json(self.api_generate(payload))
            elif route == "/api/reveal":
                self.send_json(self.api_reveal(payload))
            elif route == "/api/quit":
                self.send_json({"ok": True})
                threading.Thread(target=self.state.shutdown, daemon=True).start()
            else:
                self.send_json({"error": "没有这个接口"}, 404)
        except UserError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:                           # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def api_parse(self, payload: dict) -> dict:
        from content import DEFAULT_KIND, KIND_LABEL
        from ingest import MODE_LABEL, parse_sources
        sources = build_sources(payload)
        if not sources:
            raise UserError("还没有内容：把文本文件拖进来，或者粘贴一段文本。")
        result = parse_sources(sources,
                               mode=str(payload.get("mode") or "auto"),
                               default_kind=str(payload.get("kind") or DEFAULT_KIND))
        return {
            "count": len(result.items),
            "mode_used": result.mode_used,
            "mode_label": MODE_LABEL.get(result.mode_used, result.mode_used),
            "doc_title": result.doc_title,
            "warnings": result.warnings,
            "missing_dates": sum(1 for it in result.items if not it.date),
            "items": [{"ordinal": it.ordinal, "title": it.title,
                       "date": it.date or "", "kind": it.kind,
                       "kind_label": KIND_LABEL.get(it.kind, it.kind),
                       "date_source": it.date_source,
                       "chars": len(it.body), "note": it.note}
                      for it in result.items],
        }

    def api_generate(self, payload: dict) -> dict:
        job = self.state.new_job(str(payload.get("subject") or "生成"))
        threading.Thread(target=self.state.run_job, args=(job, payload),
                         daemon=True).start()
        return {"job_id": job.id}

    def api_reveal(self, payload: dict) -> dict:
        raw = str(payload.get("path") or "")
        if not raw:
            raise UserError("没有给路径")
        target = Path(raw).expanduser().resolve()
        roots = [self.state.out_dir.resolve(), self.state.db.resolve().parent]
        if not any(target == r or target.is_relative_to(r) for r in roots):
            raise UserError("只允许打开本应用的输出目录或数据目录里的文件")
        if not target.exists():
            raise UserError(f"文件不在了：{target}")
        where = "select" if payload.get("select") else "open"
        if where == "select" and os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif os.name == "nt":
            os.startfile(str(target))                      # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True, "path": str(target)}


# ───────────────────── 启动 ─────────────────────
def open_window(url: str, state: "AppState") -> bool:
    """开窗。返回 True 表示窗口是自己关的（调用方该退出了）。"""
    try:
        import webview
    except Exception as exc:                               # noqa: BLE001
        print(f"pywebview 不可用（{type(exc).__name__}: {exc}），"
              "改用 Edge 的应用窗口。", file=sys.stderr)
    else:
        try:
            state.window = webview.create_window(          # type: ignore[attr-defined]
                "MAF 内容观察工作台", url, width=1320, height=880,
                min_size=(1000, 660))
            webview.start()
            return True
        except Exception as exc:                           # noqa: BLE001
            print(f"pywebview 启动失败（{type(exc).__name__}: {exc}），"
                  "改用 Edge 的应用窗口。", file=sys.stderr)

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if edge.exists():
        try:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1320,880"])
            return False
        except OSError:
            pass
    webbrowser.open(url)
    return False


def run_app(*, db, out_dir, host: str = "127.0.0.1", port: int = 0,
            window: bool = True) -> int:
    """起本地服务（+ 窗口）。绑 127.0.0.1，端口给 0 就随机取。"""
    state = AppState(Path(db), Path(out_dir), host=host, port=port)
    handler = type("BoundHandler", (Handler,), {"state": state})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"{host}:{port} 起不来：{exc}", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    state.httpd = httpd
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"MAF 内容观察工作台已启动：{url}", file=sys.stderr)
    print(f"  数据：{state.db}", file=sys.stderr)
    print(f"  输出：{state.out_dir}", file=sys.stderr)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if window and open_window(url, state):
        httpd.shutdown()
        return 0
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("收到中断，正在关闭…", file=sys.stderr)
    finally:
        httpd.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="人工投喂文本 → 时间轴 / 报告 / PPT / Word / PDF（本机应用）")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "out" / "app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = 随机取空闲端口")
    parser.add_argument("--no-window", action="store_true",
                        help="不开窗口，只打印地址（调试用）")
    args = parser.parse_args(argv)
    return run_app(db=args.db, out_dir=args.out_dir, host=args.host,
                   port=args.port, window=not args.no_window)


if __name__ == "__main__":
    raise SystemExit(main())
