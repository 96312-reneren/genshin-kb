#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人工投喂 → SQLite 中转 → MAF 工作流（本地+云端混合）→ PPT / Word / PDF

子命令：
    app           桌面应用（拖文本进来，出 PPT / Word / PDF）
    ingest        人工投喂：文本/文件 → 切条 → 补日期 → 顺序入库
    news-report   从库里取条目，跑工作流出时间轴 + 观察报告
    probe-local   扫回环端口，找本地推理服务
    crawl         跑爬虫，结果进 SQLite（旧的 HTML 爬取线，保留备用）
    digest        从 SQLite 取页面喂给 MAF 工作流，输出 markdown + JSON
    all           crawl 完接着 digest

典型用法：
    # 桌面应用（推荐入口）
    python pipeline.py app

    # 命令行投喂
    python pipeline.py ingest --file 材料.txt --subject 原神
    python pipeline.py news-report --deck --condenser cloud

    # 本地模型接入
    python pipeline.py probe-local --scan
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from content import (DEFAULT_KIND, KIND_LABEL, NEWS_KINDS, SOURCE_NAME,
                     is_known_date, render_created)
from crawler import DEFAULT_USER_AGENT, Crawler
from ingest import (MODE_LABEL, PARSE_MODES, DateFiller, parse_sources,
                    read_text, to_docs)
from llm import (DEFAULT_ENV_FILE, LOCAL_START_HINT, Backend, build_cloud_backend,
                 build_local_backend, default_concurrency, find_local_service,
                 load_env_file, local_ready)
from news_workflow import (DECK_MAX_TOKENS, DEFAULT_SUBJECT, NEWS_PROMPT_VERSION,
                           REPORT_MAX_TOKENS, SUMMARY_MAX_TOKENS, docs_from_rows,
                           run_news_digest)
from safety import NetworkGuard, NetworkGuardError, is_loopback_host
from store import Store
from workflow import DEFAULT_THRESHOLD, PROMPT_VERSION, Doc, run_digest

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_DIR / "data" / "crawl.db"
DEFAULT_OUT = PROJECT_DIR / "out"
DEFAULT_NEWS_OUT = PROJECT_DIR / "out" / "news"
# 人工投喂这条线用独立的库和产物目录：和早期爬虫时代的 crawl.db 分开，
# 免得老数据混进新报告里。
DEFAULT_APP_DB = PROJECT_DIR / "data" / "app.db"
DEFAULT_APP_OUT = PROJECT_DIR / "out" / "app"


# ─────────────────────────── 护栏 ───────────────────────────
def guard_for(seeds: list[str], *, strict_public: bool = False) -> NetworkGuard:
    """全回环就锁死回环；否则只放行给定的种子主机。"""
    hosts = {urlsplit(s).hostname for s in seeds}
    if hosts and all(h and is_loopback_host(h) for h in hosts):
        print(f"     护栏：loopback_only（只允许 {sorted(hosts)}）", file=sys.stderr)
        return NetworkGuard(loopback_only=True)
    print(f"     护栏：主机白名单 {sorted(h for h in hosts if h)}"
          f"{'，禁止内网' if strict_public else ''}", file=sys.stderr)
    return NetworkGuard(allowed_hosts=hosts, allow_private=not strict_public)


# ─────────────────────────── 后端选择 ───────────────────────────
def make_backend(name: str, args) -> Backend:
    if args.dry_run:
        return Backend(name=f"{name}-stub", kind="stub", client=None,
                       model="stub", base_url="")
    if name == "cloud":
        load_env_file(args.env_file)
        return build_cloud_backend()
    if name == "local":
        return build_local_backend(getattr(args, "local_url", None),
                                   getattr(args, "local_token", None),
                                   getattr(args, "local_model", None))
    raise SystemExit(f"未知后端：{name}（可选 local / cloud）")


def resolve_concurrency(value: int, backend: Backend) -> int:
    """并发数的最终裁决：命令行给了就用给的，没给（0）就按后端类型自动定。

    为什么要自动：本地单进程推理服务多开几路并不更快，只是把请求排进同一队列，
    却要同时占住 N 份 KV cache —— 手机上会直接把内存吃光。云端才是并发越多越快。
    """
    if value and value > 0:
        return value
    return default_concurrency(backend)


def write_if_changed(path: Path, text: str) -> bool:
    """内容没变就不动文件，返回是否真的写了。

    无条件重写产物看着省事，但在 /sdcard 上每次几百 KB 的写都要过一遍 FUSE，
    而且会让 git 里堆满「内容一样、时间戳变了」的改动。
    """
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.write_text(text, encoding="utf-8")
    return True


# ─────────────────────────── 子命令 ───────────────────────────
def cmd_probe_local(args) -> int:
    scan_range = None
    if args.scan:
        scan_range = tuple(int(x) for x in args.scan_range.split("-"))
    print(f"扫描 127.0.0.1 …（{'全端口 ' + args.scan_range if scan_range else '常见端口'}）",
          file=sys.stderr)
    state = {"done": 0}

    def on_scan(done: int, total: int, hits: int) -> None:
        # 全段扫描要几十秒，不给进度就像卡死了
        if not scan_range or (done - state["done"] < 8192 and done != total):
            return
        state["done"] = done
        print(f"      · 已探 {done}/{total} 个端口（命中 {hits} 个）",
              file=sys.stderr, flush=True)

    found = find_local_service(token=args.local_token, scan_range=scan_range,
                               on_scan=on_scan if scan_range else None)
    if not found:
        print("没找到任何 OpenAI 兼容的本地服务。")
        print()
        print(LOCAL_START_HINT)
        print("  · ollama 里没有模型：WSL 里 ollama list 看看有没有 qwen3:8b")
        return 1
    for base, report in found:
        models = ", ".join(report["models"]) or "(未返回模型)"
        print(f"  {base}  可达={report['reachable']}  模型：{models}")
    return 0


def cmd_crawl(args) -> int:
    seeds = args.seed
    if not seeds:
        raise SystemExit("至少给一个 --seed")
    guard = guard_for(seeds, strict_public=args.strict_public)
    crawler = Crawler(
        seeds, guard=guard, user_agent=args.user_agent,
        max_pages=args.max_pages, max_depth=args.max_depth,
        delay=args.delay, same_host=not args.cross_host,
        on_event=lambda kind, msg: print(f"     [{kind}] {msg}", file=sys.stderr),
    )
    with Store(args.db) as store:
        run_id = store.start_run(seeds, args.user_agent, args.max_pages, args.max_depth)
        print(f"[1/2] 开始爬取，run_id={run_id}", file=sys.stderr)
        pages = crawler.crawl()
        # 被 robots 拒绝的 URL 也入库，保证审计口径完整
        store.upsert_pages(pages, run_id)
        store.upsert_pages(crawler.skipped, run_id)
        for page in pages:
            if page.links:
                store.upsert_links(page.url, page.links)
        store.finish_run(run_id, ok=True,
                         note=f"抓到 {len(pages)} 页，跳过 {len(crawler.skipped)} 个")
        stats = store.stats(run_id)
        # 中转快照在这里落地（digest 只管消费它，不再重复导出整库正文）
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pages_json = store.export_json(out_dir / "pages.json", run_id=run_id)
    print(f"      护栏拦截 {guard.blocked_count} 次；"
          f"入库：{stats['总数']} 条（HTTP 200 {stats['HTTP_OK']}，"
          f"被 robots 拒绝 {stats['被robots拒绝']}，失败 {stats['抓取失败']}）",
          file=sys.stderr)
    print(json.dumps({"run_id": run_id, "stats": stats}, ensure_ascii=False))
    return 0


def _load_docs(store: Store, run_id: int | None, limit: int) -> tuple[list[Doc], int | None]:
    if run_id is None:
        row = store.conn.execute(
            "SELECT run_id FROM crawl_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        run_id = row["run_id"] if row else None
    docs: list[Doc] = []
    for row in store.iter_pages(run_id=run_id, only_ok=True, min_text_len=1):
        docs.append(Doc(url=row["url"], title=row["title"] or "",
                        text=row["text"] or "",
                        content_hash=row["content_hash"] or ""))
        if limit and len(docs) >= limit:
            break
    return docs, run_id


def cmd_digest(args) -> int:
    summarizer = make_backend(args.summarizer, args)
    condenser = make_backend(args.condenser, args)
    concurrency = resolve_concurrency(args.concurrency, summarizer)
    print(f"     后端：摘要={summarizer}  精简={condenser}（并发 {concurrency}）",
          file=sys.stderr)

    with Store(args.db) as store:
        docs, run_id = _load_docs(store, args.run_id, args.limit)
        if not docs:
            print("中转库里没有可用页面，先跑 crawl。", file=sys.stderr)
            return 1
        print(f"[2/2] 从 SQLite 取到 {len(docs)} 页（run_id={run_id}）", file=sys.stderr)

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # pages.json 是「爬完之后的中转快照」，crawl 写一次就够。
        # digest 再写一遍只是把同一批正文重排一次，几千页时峰值内存翻倍。
        pages_json = out_dir / "pages.json"
        if pages_json.exists():
            print(f"     复用已有 {pages_json.name}（crawl 阶段已导出）", file=sys.stderr)
        else:
            pages_json = store.export_json(pages_json, run_id=run_id)

        # ── 摘要缓存 ──────────────────────────────────────────
        # 已经算过的页不再喂模型。这一条是整条链上最省时间的改动：
        # 本地 3B 模型一页 7 秒，14 页重跑一次就是白烧 100 秒。
        cached = {} if args.refresh else store.load_summary_cache(
            summarizer.name, summarizer.model, PROMPT_VERSION)
        fresh: dict[str, str] = {}

        def lookup(doc: Doc) -> str | None:
            hit = cached.get(doc.url)
            if not hit:
                return None
            old_hash, summary = hit
            # 正文变过的页面，旧摘要作废（老行没有 content_hash，只能信它）
            if old_hash and doc.content_hash and old_hash != doc.content_hash:
                return None
            return summary or None

        def remember(doc: Doc, text: str) -> None:
            fresh[doc.url] = text

        import asyncio
        digest = asyncio.run(run_digest(
            docs, summarizer, condenser,
            threshold=args.threshold, concurrency=concurrency,
            dry_run=args.dry_run,
            lookup=lookup, on_summary=remember,
        ))

        # 命中的也一起回写：刷新时间戳，并给老行补上 content_hash
        store.save_summaries(
            [{"url": d.url, "ordinal": i, "backend": summarizer.name,
              "model": summarizer.model, "summary": s,
              "content_hash": d.content_hash}
             for i, (d, s) in enumerate(zip(docs, digest.summaries), 1)],
            run_id=run_id, prompt_ver=PROMPT_VERSION)
        store.save_digest(text=digest.markdown, threshold=digest.threshold,
                          condensed=digest.condensed,
                          backend=digest.condenser_backend or "",
                          model=digest.condenser_model or "",
                          segments=len(digest.summaries), run_id=run_id)

    md_path = out_dir / "summary.md"
    json_path = out_dir / "digest.json"
    write_if_changed(md_path, digest.markdown)
    write_if_changed(json_path,
                     json.dumps(digest.to_dict(), ensure_ascii=False, indent=2))

    print(digest.markdown)
    print(f"\n产物：{md_path}\n      {json_path}\n      {pages_json}", file=sys.stderr)
    return 0


def cmd_all(args) -> int:
    rc = cmd_crawl(args)
    if rc != 0:
        return rc
    return cmd_digest(args)


# ─────────────────────────── 人工投喂：文本 → 条目 → 报告 ───────────────────────────
def build_backend_for(name: str, *, dry_run: bool = False, env_file=None,
                      local_url: str = "", local_token: str = "",
                      local_model: str = "") -> Backend:
    """后端构造器。不走 argparse —— 桌面应用直接调这个函数。"""
    if dry_run:
        return Backend(name=f"{name}-stub", kind="stub", client=None,
                       model="stub", base_url="")
    if name == "cloud":
        load_env_file(env_file or str(DEFAULT_ENV_FILE))
        return build_cloud_backend()
    if name == "local":
        return build_local_backend(local_url, local_token, local_model)
    raise SystemExit(f"未知后端：{name}（可选 local / cloud）")


def make_news_backend(name: str, args) -> Backend:
    """CLI 版：从 argparse 命名空间里取参数。"""
    return build_backend_for(
        name, dry_run=getattr(args, "dry_run", False),
        env_file=getattr(args, "env_file", None),
        local_url=getattr(args, "local_url", "") or "",
        local_token=getattr(args, "local_token", "") or "",
        local_model=getattr(args, "local_model", "") or "")


def _db_span(stats: dict) -> str:
    """库内时间跨度；全部日期未知时如实说，不硬凑一个年份出来。"""
    if not stats.get("总数") or not is_known_date(stats.get("最早")):
        return "全部日期未知" if stats.get("总数") else "(空)"
    return f"{render_created(stats['最早'])} ~ {render_created(stats['最新'])}"


def collect_sources(args) -> list[dict]:
    """把 --file / --text 收成 [{name, content}]，顺序保持命令行给的顺序。"""
    sources: list[dict] = []
    for path in (getattr(args, "file", None) or []):
        p = Path(path)
        sources.append({"name": p.name, "content": read_text(p)})
    texts = list(getattr(args, "text", None) or [])
    for i, t in enumerate(texts, 1):
        sources.append({"name": "粘贴文本" if len(texts) == 1
                        else f"粘贴文本 {i}", "content": t})
    return sources


def cmd_ingest(args) -> int:
    """人工投喂：文本/文件 → 条目（缺日期的补抽）→ 按时间顺序入库。"""
    sources = collect_sources(args)
    if not sources:
        print("没有输入：用 --file 指定文件，或用 --text 给一段文本。",
              file=sys.stderr)
        return 1

    result = parse_sources(sources, mode=args.mode, default_kind=args.kind)
    print(f"[1/3] 解析 {len(sources)} 份材料 → {len(result.items)} 条"
          f"（切分方式：{MODE_LABEL.get(result.mode_used, result.mode_used)}）",
          file=sys.stderr)
    for w in result.warnings:
        print(f"      · {w}", file=sys.stderr)

    missing = [it for it in result.items if not it.date]
    if missing and not args.no_fill:
        try:
            backend = make_news_backend(args.summarizer, args)
        except (RuntimeError, NetworkGuardError) as exc:
            print(f"      补日期的后端不可用（{exc}），无日期条目保持「日期未知」",
                  file=sys.stderr)
        else:
            print(f"[2/3] {len(missing)} 条没有日期，交给 {backend} 补抽…",
                  file=sys.stderr)
            try:
                concurrency = resolve_concurrency(args.concurrency, backend)
                # 日期推断按「标题+正文」指纹缓存。以前这条线也是只写不读：
                # 同一批没写日期的材料每投喂一次，就原样再问模型一遍。
                with Store(args.db) as cache_store:
                    known_dates = ({} if args.refresh
                                   else cache_store.load_date_cache())
                    inferred: dict[str, str] = {}

                    def remember_date(fp: str, date: str) -> None:
                        inferred[fp] = date

                    print(f"      并发 {concurrency}", file=sys.stderr)
                    filler = DateFiller(backend, concurrency=concurrency,
                                        lookup=known_dates.get,
                                        on_result=remember_date)
                    info = asyncio.run(filler.fill(result.items))
                    if inferred:
                        cache_store.save_date_inferences(
                            [{"fingerprint": k, "date": v}
                             for k, v in inferred.items()],
                            model=f"{backend.name}:{backend.model}")
                print(f"      补到 {info['resolved']} 条，仍未知 "
                      f"{info['unknown']} 条"
                      + (f"（{filler.hits} 种正文用了已存结论）"
                         if filler.hits else ""), file=sys.stderr)
            except Exception as exc:                      # noqa: BLE001
                print(f"      补抽失败，按「日期未知」继续入库：{exc}",
                      file=sys.stderr)
    else:
        print("[2/3] 条目自带日期或已关闭补抽，不用模型。", file=sys.stderr)

    docs = to_docs(result.items, subject=args.subject)
    with Store(args.db) as store:
        run_id = store.start_news_run(SOURCE_NAME, args.subject)
        counts = store.upsert_news([d.to_row() for d in docs], run_id)
        store.finish_news_run(run_id, fetched=len(docs),
                              inserted=counts["inserted"],
                              updated=counts["updated"], ok=True,
                              note=f"ingest/{result.mode_used}")
        stats = store.news_stats()
        by_kind = store.news_kind_counts()

    span = _db_span(stats)
    kind_text = "、".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in by_kind.items())
    print(f"[3/3] 入库：新增 {counts['inserted']}，更新 {counts['updated']}；"
          f"库内共 {stats['总数']} 条（{kind_text}），跨度 {span}"
          "（按发布时间升序存储）", file=sys.stderr)
    if stats["日期未知"]:
        print(f"      其中 {stats['日期未知']} 条日期未知，已排在时间轴末尾",
              file=sys.stderr)
    print(json.dumps({"run_id": run_id, "mode": result.mode_used,
                      "doc_title": result.doc_title, "items": len(docs),
                      "inserted": counts["inserted"],
                      "updated": counts["updated"], "total": stats["总数"],
                      "by_kind": by_kind, "span": span,
                      "unknown_dates": stats["日期未知"],
                      "source": SOURCE_NAME}, ensure_ascii=False))
    return 0


def cmd_news_report(args) -> int:
    """从库里按发布时间升序取条目，跑工作流出时间轴 + 报告（+ 幻灯片文案）。"""
    kinds = list(args.kind) if args.kind else list(NEWS_KINDS)
    subject = args.subject or DEFAULT_SUBJECT
    try:
        summarizer = make_news_backend(args.summarizer, args)
        condenser = make_news_backend(args.condenser, args)
    except (RuntimeError, NetworkGuardError) as exc:
        print(f"后端不可用：{exc}", file=sys.stderr)
        return 1
    concurrency = resolve_concurrency(args.concurrency, summarizer)
    print(f"     后端：摘要={summarizer}  报告={condenser}（并发 {concurrency}）",
          file=sys.stderr)

    # 本地模型是使用者在 WSL 里手动起的；探不到就直说，别让他对着连接错误猜。
    for role, backend in (("摘要", summarizer), ("报告", condenser)):
        ready, detail = local_ready(backend)
        if not ready:
            print(f"{role}要用的本地模型现在连不上（{detail}）。", file=sys.stderr)
            print(LOCAL_START_HINT, file=sys.stderr)
            return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Store(args.db) as store:
        rows = list(store.iter_news(game=args.subject or None, kinds=kinds,
                                    limit=args.limit))
        if not rows:
            print("库里还没有内容，先跑 ingest 投喂文本。", file=sys.stderr)
            return 1
        docs = docs_from_rows(rows)
        by_kind: dict[str, int] = {}
        for d in docs:
            by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
        print(f"[1/3] 从 SQLite 取到 {len(docs)} 条（已按发布时间升序）："
              + "、".join(f"{KIND_LABEL.get(k, k)} {v}"
                          for k, v in by_kind.items()), file=sys.stderr)

        # ── 概括缓存 ──────────────────────────────────────────
        # 这条线以前只写不读：每跑一次报告，就把 N 条材料的正文原样再喂一遍模型。
        # 正文（标题 + 描述）没变过的条目现在直接复用库里那条概括。
        cached = {} if args.refresh else store.load_news_summary_cache(
            summarizer.name, summarizer.model, NEWS_PROMPT_VERSION)
        if cached:
            fresh_hashes = {d.uid: d.fingerprint for d in docs}
            reusable = sum(1 for uid, (h, s) in cached.items()
                           if s and uid in fresh_hashes
                           and (not h or h == fresh_hashes[uid]))
            print(f"     库里已有 {len(cached)} 条旧概括，其中 {reusable} 条仍可用",
                  file=sys.stderr)

        def lookup(doc) -> str | None:
            hit = cached.get(doc.uid)
            if not hit:
                return None
            old_hash, summary = hit
            # 正文变过的条目，旧概括作废（老行没有 content_hash，只能信它）
            if old_hash and old_hash != doc.fingerprint:
                return None
            return summary or None

        fresh: dict[str, str] = {}

        def remember(doc, text: str) -> None:
            fresh[doc.uid] = text

        digest = asyncio.run(run_news_digest(
            docs, summarizer, condenser, concurrency=concurrency,
            summary_tokens=args.summary_tokens, report_tokens=args.report_tokens,
            deck_tokens=args.deck_tokens, subject=subject,
            with_deck=args.deck, dry_run=args.dry_run,
            plain_deck=args.deck_plain,
            lookup=lookup, on_summary=remember))

        print(f"      概括落库：新算 {len(fresh)} 条，其余 {len(digest.nodes) - len(fresh)} 条"
              f"用的是库里存好的", file=sys.stderr)

        # uid → 正文指纹，落库时一并存下，下次才知道这条概括还算不算数
        fingerprints = {d.uid: d.fingerprint for d in docs}
        store.save_news_summaries(
            [{"uid": n.uid, "ordinal": n.ordinal, "backend": digest.backend,
              "model": digest.model, "summary": n.summary,
              "content_hash": fingerprints.get(n.uid, "")} for n in digest.nodes],
            prompt_ver=NEWS_PROMPT_VERSION)
        # 这条记录记的是「谁写的报告」，不是谁写的摘要
        store.save_news_report(backend=digest.condenser_backend or digest.backend,
                               model=digest.condenser_model or digest.model,
                               items=len(digest.nodes), span=digest.span,
                               source_api=digest.source_api,
                               timeline=digest.timeline_md, report=digest.report_md)

    report_path = out_dir / "report.md"
    timeline_path = out_dir / "timeline.md"
    json_path = out_dir / "news_digest.json"
    write_if_changed(report_path, digest.report_md)
    write_if_changed(timeline_path, digest.timeline_md)
    write_if_changed(json_path,
                     json.dumps(digest.to_dict(), ensure_ascii=False, indent=2))
    made = [report_path, timeline_path, json_path]

    if args.deck:
        spec_path = out_dir / "deck_spec.json"
        spec = digest.deck_spec or {}
        write_if_changed(spec_path,
                         json.dumps(spec, ensure_ascii=False, indent=2))
        made.append(spec_path)
        print(f"      幻灯片文案：{len(spec.get('sections', []))} 个小节 → {spec_path}",
              file=sys.stderr)

    print(digest.report_md)
    print("\n产物：" + "\n      ".join(str(p) for p in made), file=sys.stderr)
    return 0


def cmd_app(args) -> int:
    """起桌面应用：本地 HTTP 服务 + pywebview 窗口。"""
    try:
        from app.server import run_app
    except ImportError as exc:
        print(f"桌面应用还不完整：{exc}\n"
              "先在项目 venv 里装依赖：myvenv\\Scripts\\pip install pywebview "
              "python-pptx python-docx Pillow", file=sys.stderr)
        return 1
    return run_app(db=args.db, out_dir=args.out_dir, host=args.host,
                   port=args.port, window=not args.browser)


# ─────────────────────────── CLI ───────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="人工投喂文本 → SQLite → MAF 工作流（本地+云端）→ PPT/Word/PDF")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 中转库路径")
        sp.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="产物目录")
        sp.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
        sp.add_argument("--dry-run", action="store_true", help="不调模型，只验证流程")
        sp.add_argument("--summarizer", choices=["local", "cloud"], default="local",
                        help="逐页摘要用哪个后端（默认 local）")
        sp.add_argument("--condenser", choices=["local", "cloud"], default="cloud",
                        help="超阈值精简用哪个后端（默认 cloud）")
        sp.add_argument("--local-url",
                        help="本地 OpenAI 兼容服务地址"
                             "（默认 WSL 的 ollama：http://127.0.0.1:11434）")
        sp.add_argument("--local-token", help="本地服务 token")
        sp.add_argument("--local-model", help="本地模型 id（省略则从 /v1/models 取）")

    c = sub.add_parser("crawl", help="跑爬虫入库")
    c.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 中转库路径")
    c.add_argument("--out-dir", type=Path, default=DEFAULT_OUT,
                   help="产物目录（爬完在这里落一份 pages.json 快照）")
    c.add_argument("--seed", action="append", default=[], help="种子 URL，可重复")
    c.add_argument("--max-pages", type=int, default=20)
    c.add_argument("--max-depth", type=int, default=1)
    c.add_argument("--delay", type=float, default=0.5, help="同主机最小间隔（秒）")
    c.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    c.add_argument("--cross-host", action="store_true", help="允许跨主机跟链接")
    c.add_argument("--strict-public", action="store_true", help="拒绝任何内网/保留地址")
    c.set_defaults(func=cmd_crawl)

    d = sub.add_parser("digest", help="从库里取页面跑工作流")
    d.add_argument("--run-id", type=int)
    d.add_argument("--limit", type=int, default=0, help="最多取多少页（0=不限）")
    d.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    d.add_argument("--concurrency", type=int, default=0,
                   help="并发数；0 = 按后端自动（本地 1、云端 4）")
    d.add_argument("--refresh", action="store_true",
                   help="忽略已存摘要，全部重新调模型（改过提示词或想换口径时用）")
    common(d)
    d.set_defaults(func=cmd_digest)

    a = sub.add_parser("all", help="先 crawl 再 digest")
    a.add_argument("--seed", action="append", default=[])
    a.add_argument("--max-pages", type=int, default=20)
    a.add_argument("--max-depth", type=int, default=1)
    a.add_argument("--delay", type=float, default=0.5)
    a.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    a.add_argument("--cross-host", action="store_true")
    a.add_argument("--strict-public", action="store_true")
    a.add_argument("--run-id", type=int)
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    a.add_argument("--concurrency", type=int, default=0,
                   help="并发数；0 = 按后端自动（本地 1、云端 4）")
    a.add_argument("--refresh", action="store_true",
                   help="忽略已存摘要，全部重新调模型")
    common(a)
    a.set_defaults(func=cmd_all)

    # ── 人工投喂 ──
    def news_common(sp):
        sp.add_argument("--db", type=Path, default=DEFAULT_APP_DB)
        sp.add_argument("--subject", default=DEFAULT_SUBJECT,
                        help="材料主题词，用于标题与《》里的名字（默认 原神）")
        sp.add_argument("--kind", action="append", choices=list(NEWS_KINDS),
                        help="要处理的类别，可重复；不给就是三类全要")

    def backend_args(sp):
        sp.add_argument("--summarizer", choices=["local", "cloud"],
                        default="local", help="逐条概括用哪个后端（默认 local）")
        sp.add_argument("--condenser", choices=["local", "cloud"],
                        default="local",
                        help="最终报告用哪个后端（默认 local；"
                             "--condenser cloud 就是本地概括 + 云端报告）")
        sp.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                        help="云端凭据文件（默认 ~/.config/maf/credentials.env）")
        sp.add_argument("--local-url",
                        help="本地 OpenAI 兼容服务地址（默认 WSL 的 ollama）")
        sp.add_argument("--local-token")
        sp.add_argument("--local-model")

    def news_report_args(sp):
        sp.add_argument("--out-dir", type=Path, default=DEFAULT_APP_OUT)
        sp.add_argument("--limit", type=int, default=0, help="最多用多少条（0=全部）")
        sp.add_argument("--concurrency", type=int, default=0,
                        help="并发数；0 = 按后端自动（本地 1、云端 4）")
        sp.add_argument("--summary-tokens", type=int, default=SUMMARY_MAX_TOKENS)
        sp.add_argument("--report-tokens", type=int, default=REPORT_MAX_TOKENS)
        sp.add_argument("--deck", action="store_true",
                        help="额外产出幻灯片文案 deck_spec.json")
        sp.add_argument("--deck-plain", action="store_true",
                        help="幻灯片要点直接按报告小节切分，不再为此调一次模型")
        sp.add_argument("--deck-tokens", type=int, default=DECK_MAX_TOKENS)
        sp.add_argument("--refresh", action="store_true",
                        help="忽略库里已存的概括，全部重新调模型")
        sp.add_argument("--dry-run", action="store_true", help="不调模型，只验证流程")
        backend_args(sp)

    ig = sub.add_parser("ingest", help="人工投喂：把文本/文件切条后顺序入库")
    news_common(ig)
    ig.add_argument("--file", action="append", default=[],
                    help="文本文件路径，可重复（UTF-8 / GBK 都能读）")
    ig.add_argument("--text", action="append", default=[],
                    help="直接给一段文本，可重复（界面拖入时走的是这条）")
    ig.add_argument("--mode", choices=list(PARSE_MODES), default="auto",
                    help="切分方式（默认 auto：有日期行按日期，否则按标题/整篇）")
    ig.add_argument("--no-fill", action="store_true",
                    help="不用模型补抽缺失的日期，直接标「日期未知」")
    ig.add_argument("--concurrency", type=int, default=0,
                    help="补日期时的并发数；0 = 按后端自动（本地 1、云端 4）")
    ig.add_argument("--refresh", action="store_true",
                    help="忽略已存的日期推断结论，缺日期的条目全部重新问模型")
    backend_args(ig)
    ig.set_defaults(func=cmd_ingest)

    nr = sub.add_parser("news-report", help="本地模型生成时间轴 + 运营观察报告")
    news_common(nr)
    news_report_args(nr)
    nr.set_defaults(func=cmd_news_report)

    ap = sub.add_parser("app", help="启动桌面应用（拖文本进来 → 出 PPT/Word/PDF）")
    ap.add_argument("--db", type=Path, default=DEFAULT_APP_DB)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_APP_OUT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    ap.add_argument("--browser", action="store_true",
                    help="只用系统浏览器打开，不开桌面窗口")
    ap.set_defaults(func=cmd_app)

    pl = sub.add_parser("probe-local", help="找本地推理服务")
    pl.add_argument("--scan", action="store_true", help="扫全端口段")
    pl.add_argument("--scan-range", default="1024-65535")
    pl.add_argument("--local-token")
    pl.set_defaults(func=cmd_probe_local)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
