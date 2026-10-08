#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据中转层 —— 为什么选 SQLite 而不是 JSON

判断结论：**中转用 SQLite，JSON 只做两个边界的交换格式**（喂给工作流的输入快照 + 最终产物）。

理由，按重要性排序：

1. 增量写 + 崩溃安全。爬虫是长事务，可能跑几分钟到几十分钟。JSON 方案每抓一页就要
   重写整个文件（O(n²) 写入），进程被杀就得到一个截断的坏文件。SQLite 单页一事务，
   WAL 模式下断电也不会写坏。
2. 去重与幂等重爬。`pages.url` 做主键，`INSERT ... ON CONFLICT DO UPDATE` 让重复爬取
   天然幂等。JSON 得自己维护索引，重爬一次就是一堆重复条目。
3. 选择性读取。喂给 MAF 之前经常要筛「status=200 且正文长度 > N 且 robots_allowed=1」。
   SQLite 一条 WHERE 就够，不用把几十 MB 正文全读进内存再筛。
4. 读写并发。WAL 模式下爬虫在写、工作流在读，互不阻塞。JSON 做不到。
5. 可审计。被 robots.txt 拒绝的 URL 也会落库（robots_allowed=0），事后能回答
   「你到底有没有爬我不让爬的东西」。这在 JSON 里很容易被漏掉。

JSON 仍然有用，但只用在两头：给工作流的输入快照（可读、可 diff、可当 fixture），
以及最终摘要产物（人要看、要分享）。中间态不该是 JSON。

对外接口刻意保持窄：Store 只认「页面/摘要/总览」三类事实，换存储实现不影响爬虫与工作流。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from content import UNKNOWN_TS

__all__ = ["Store"]

# 库结构版本。改了 SCHEMA 或 _migrate() 就加一，老库下次打开会自动升级。
# 放在 PRAGMA user_version 里，好处是不用额外建一张「版本表」，
# 也让每次 open 都能跳过已经建好的那 24 条 DDL。
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    seeds       TEXT,
    user_agent  TEXT,
    max_pages   INTEGER,
    max_depth   INTEGER,
    ok          INTEGER,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    url             TEXT PRIMARY KEY,
    run_id          INTEGER REFERENCES crawl_runs(run_id),
    host            TEXT NOT NULL,
    depth           INTEGER NOT NULL DEFAULT 0,
    status          INTEGER,
    content_type    TEXT,
    title           TEXT,
    text            TEXT,
    text_len        INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,
    robots_allowed  INTEGER NOT NULL DEFAULT 1,
    fetched_at      TEXT,
    error           TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_run  ON pages(run_id);
CREATE INDEX IF NOT EXISTS idx_pages_host ON pages(host);

CREATE TABLE IF NOT EXISTS links (
    from_url TEXT NOT NULL,
    to_url   TEXT NOT NULL,
    PRIMARY KEY (from_url, to_url)
);

-- summaries 既是「这次的产出」，也是「下次的缓存」。
-- 缓存键 = (url, backend, model, prompt_ver)：同一页、同一后端、同一版提示词，
-- 只留一行。content_hash 用来判断页面正文有没有变过 —— 变了就不该复用旧摘要。
-- 唯一索引不写在这里，因为老库里可能已经有重复行；见 _migrate()。
CREATE TABLE IF NOT EXISTS summaries (
    summary_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES crawl_runs(run_id),
    url          TEXT,
    ordinal      INTEGER,
    backend      TEXT,
    model        TEXT,
    summary      TEXT,
    created_at   TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    prompt_ver   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS digests (
    digest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER REFERENCES crawl_runs(run_id),
    threshold INTEGER,
    condensed INTEGER,
    backend   TEXT,
    model     TEXT,
    segments  INTEGER,
    text      TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ── 以下是第二类数据源：API 采集的公告 ──────────────────────────────
-- 和 pages 那套 HTML 爬取分开建表：两者的「事实」不一样，混在一张表里
-- 既没法保证约束，也说不清每条数据的出处。

CREATE TABLE IF NOT EXISTS news_runs (
    news_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source_api  TEXT,
    lang        TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    uid           TEXT PRIMARY KEY,          -- game:kind:post_id
    news_run_id   INTEGER REFERENCES news_runs(news_run_id),
    game          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    post_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    url           TEXT,                      -- 原始 HoYoLab 链接
    banner        TEXT,
    created_at    INTEGER NOT NULL,          -- 上游发布时间（Unix 秒）
    created_iso   TEXT NOT NULL,             -- 同一时间的 UTC ISO
    source_api    TEXT NOT NULL,             -- 出处：命中的接口 URL
    source_name   TEXT NOT NULL,             -- 出处：上游项目名
    content_hash  TEXT,
    fetched_at    TEXT NOT NULL,             -- 抓取时间
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT
);
-- 顺序存储靠这两条索引兜底：按发布时间取数永远是走索引的有序扫描，
-- 而不是「取出来再在内存里 sort」。
CREATE INDEX IF NOT EXISTS idx_news_created ON news_items(created_at, post_id);
CREATE INDEX IF NOT EXISTS idx_news_kind    ON news_items(kind, created_at);

-- 同 summaries：既存本轮结果，也当下一轮的缓存。键 = (uid, backend, model, prompt_ver)。
-- 之前这张表只写不读，所以每跑一次「观察报告」就把 N 条内容原样再喂一遍模型。
CREATE TABLE IF NOT EXISTS news_summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    news_run_id  INTEGER,
    uid          TEXT,
    ordinal      INTEGER,
    backend      TEXT,
    model        TEXT,
    summary      TEXT,
    created_at   TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    prompt_ver   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS news_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    backend    TEXT,
    model      TEXT,
    items      INTEGER,
    span       TEXT,
    source_api TEXT,
    timeline   TEXT,
    report     TEXT,
    created_at TEXT
);

-- 「这条材料的日期是哪天」的结论缓存。键是标题+正文字段指纹，不是 uid ——
-- uid 里带着日期，而日期正是要推断的东西。date 为空串表示问过模型、结论是「未知」。
-- 日期前缀能认出来的条目根本不走这里，所以这张表只装模型真正答过的那些。
CREATE TABLE IF NOT EXISTS date_inferences (
    fingerprint TEXT PRIMARY KEY,
    date        TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：补列 → 去重 → 建唯一索引。

    三步顺序不能换：唯一索引在有重复行的库上会直接建失败，
    所以必须先按新的缓存键把重复行清成一行。
    """
    for table, key_cols in (("summaries", ("url", "backend", "model", "prompt_ver")),
                            ("news_summaries", ("uid", "backend", "model", "prompt_ver"))):
        for col in ("content_hash", "prompt_ver"):
            if not _has_column(conn, table, col):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # 老库里同一缓存键会有多行（每跑一次追加一行）。只保留最新那条。
        cols = ", ".join(key_cols)
        conn.execute(f"DELETE FROM {table} WHERE rowid NOT IN "
                     f"(SELECT MAX(rowid) FROM {table} GROUP BY {cols})")
        idx = ("idx_summaries_cache" if table == "summaries"
               else "idx_news_summaries_cache")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {table}({cols})")


class Store:
    """SQLite 中转层。用 with 打开，退出自动提交并关闭。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # 已经建过表的库不再重跑 DDL，也不再跑一次去重扫描。
        if self.conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.conn.executescript(SCHEMA)
            _migrate(self.conn)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    # ── 生命周期 ────────────────────────────────────────────
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    PAGE_COLUMNS = ("url,run_id,host,depth,status,content_type,title,text_len,"
                    "content_hash,robots_allowed,fetched_at,error,note")
    NEWS_COLUMNS = ("uid,news_run_id,game,kind,post_id,title,url,created_at,"
                    "created_iso,source_api,source_name,content_hash,fetched_at,"
                    "first_seen_at,updated_at")

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # ── 爬取批次 ────────────────────────────────────────────
    def start_run(self, seeds: Iterable[str], user_agent: str,
                  max_pages: int, max_depth: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (started_at, seeds, user_agent, max_pages, max_depth, ok)"
            " VALUES (?,?,?,?,?,0)",
            (_now(), json.dumps(list(seeds), ensure_ascii=False), user_agent, max_pages, max_depth),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, ok: bool = True, note: str = "") -> None:
        self.conn.execute(
            "UPDATE crawl_runs SET finished_at=?, ok=?, note=? WHERE run_id=?",
            (_now(), 1 if ok else 0, note, run_id),
        )
        self.conn.commit()

    # ── 页面 ────────────────────────────────────────────────
    def upsert_page(self, page: Any, run_id: int | None = None) -> None:
        """按 url 幂等写入；重爬同一页只更新，不产生重复行。"""
        url = page.url
        host = url.split("/")[2] if "//" in url else ""
        self.conn.execute(
            """
            INSERT INTO pages (url, run_id, host, depth, status, content_type, title, text,
                               text_len, content_hash, robots_allowed, fetched_at, error, note)
            VALUES (:url,:run_id,:host,:depth,:status,:content_type,:title,:text,
                    :text_len,:content_hash,:robots_allowed,:fetched_at,:error,:note)
            ON CONFLICT(url) DO UPDATE SET
                run_id=excluded.run_id, status=excluded.status, title=excluded.title,
                text=excluded.text, text_len=excluded.text_len,
                content_hash=excluded.content_hash, robots_allowed=excluded.robots_allowed,
                fetched_at=excluded.fetched_at, error=excluded.error, note=excluded.note
            """,
            {
                "url": url, "run_id": run_id, "host": host,
                "depth": getattr(page, "depth", 0),
                "status": getattr(page, "status", None),
                "content_type": getattr(page, "content_type", "") or "",
                "title": getattr(page, "title", "") or "",
                "text": getattr(page, "text", "") or "",
                "text_len": getattr(page, "text_len", 0),
                "content_hash": getattr(page, "content_hash", "") or "",
                "robots_allowed": 1 if getattr(page, "robots_allowed", True) else 0,
                "fetched_at": getattr(page, "fetched_at", "") or _now(),
                "error": getattr(page, "error", None),
                "note": getattr(page, "note", "") or "",
            },
        )

    def upsert_pages(self, pages: Iterable[Any], run_id: int | None = None) -> int:
        count = 0
        with self.conn:                       # 单事务批量提交
            for page in pages:
                self.upsert_page(page, run_id)
                count += 1
        return count

    def upsert_links(self, from_url: str, to_urls: Iterable[str]) -> None:
        # 单事务提交：每条链接单独 commit 在 /sdcard 上就是每行一次 fsync。
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO links (from_url, to_url) VALUES (?,?)",
                [(from_url, t) for t in to_urls],
            )

    # ── 查询 ────────────────────────────────────────────────
    def iter_pages(self, *, run_id: int | None = None, only_ok: bool = True,
                   min_text_len: int = 1,
                   order: str = "depth, url",
                   columns: str = "*") -> Iterator[sqlite3.Row]:
        sql = f"SELECT {columns} FROM pages WHERE text_len >= ?"
        args: list[Any] = [min_text_len]
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        if only_ok:
            sql += " AND robots_allowed = 1 AND status = 200 AND (error IS NULL OR error='')"
        sql += f" ORDER BY {order}"
        yield from self.conn.execute(sql, args)

    def stats(self, run_id: int | None = None) -> dict[str, int]:
        where, args = ("WHERE run_id = ?", [run_id]) if run_id is not None else ("", [])
        row = self.conn.execute(
            f"""SELECT
                  COUNT(*)                                            AS 总数,
                  SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END)        AS HTTP_OK,
                  SUM(CASE WHEN robots_allowed = 0 THEN 1 ELSE 0 END)  AS 被robots拒绝,
                  SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)   AS 抓取失败,
                  COALESCE(SUM(text_len), 0)                           AS 正文总字数
                FROM pages {where}""", args).fetchone()
        return {k: int(v or 0) for k, v in dict(row).items()}

    # ── 结果落库 ────────────────────────────────────────────
    def save_summaries(self, rows: list[dict], run_id: int | None = None,
                       *, prompt_ver: str = "") -> int:
        """按缓存键 (url, backend, model, prompt_ver) 覆盖写入。

        以前这里是裸 INSERT：重跑一次 digest 就在 summaries 里多叠一层，
        手机上的库就是这么变成「14 页 28 条摘要」的。现在同一把钥匙只有一行，
        重跑既不会长胖，也不会把上面那层旧摘要当成另一个版本留着。
        """
        params = self._summary_rows(rows, run_id, prompt_ver)
        if not params:
            return 0
        keys = [(r["url"], r["backend"], r["model"], r["prompt_ver"]) for r in params]
        with self.conn:
            self.conn.executemany(
                "DELETE FROM summaries WHERE url=? AND backend=? AND model=? AND prompt_ver=?",
                keys)
            self.conn.executemany(
                """INSERT INTO summaries
                   (run_id,url,ordinal,backend,model,summary,created_at,content_hash,prompt_ver)
                   VALUES (:run_id,:url,:ordinal,:backend,:model,:summary,:created_at,
                           :content_hash,:prompt_ver)""",
                params)
        return len(params)

    @staticmethod
    def _summary_rows(rows: list[dict], run_id: int | None, prompt_ver: str,
                      *, key: str = "url", run_field: str = "run_id") -> list[dict]:
        """归一化成非空字符串 + 同键去重（后一条覆盖前一条）。"""
        now = _now()
        out: list[dict] = []
        seen: dict[tuple, int] = {}
        for row in rows:
            item = {
                run_field: run_id,
                key: row.get(key) or "",
                "ordinal": row.get("ordinal"),
                "backend": row.get("backend") or "",
                "model": row.get("model") or "",
                "summary": row.get("summary") or "",
                "created_at": now,
                "content_hash": row.get("content_hash") or "",
                "prompt_ver": row.get("prompt_ver", prompt_ver) or "",
            }
            ident = (item[key], item["backend"], item["model"], item["prompt_ver"])
            if ident in seen:
                out[seen[ident]] = item
                continue
            seen[ident] = len(out)
            out.append(item)
        return out

    def load_summary_cache(self, backend: str, model: str,
                           prompt_ver: str = "") -> dict[str, tuple[str, str]]:
        """读回已算好的逐页摘要：url -> (content_hash, summary)。

        调用方拿 pages.content_hash 一比就知道这份摘要还算不算数：
        正文没变就直接复用，不用再喂一次模型。这是整条链上最省时间的一步。
        """
        rows = self.conn.execute(
            "SELECT url, content_hash, summary FROM summaries"
            " WHERE backend=? AND model=? AND prompt_ver=?", (backend, model, prompt_ver))
        return {r["url"]: (r["content_hash"] or "", r["summary"] or "") for r in rows}

    def save_digest(self, *, text: str, threshold: int, condensed: bool,
                    backend: str, model: str, segments: int,
                    run_id: int | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO digests (run_id,threshold,condensed,backend,model,segments,text,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, threshold, 1 if condensed else 0, backend, model, segments, text, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_digest(self, run_id: int | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM digests"
        args: list[Any] = []
        if run_id is not None:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        sql += " ORDER BY digest_id DESC LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ── 内容条目（人工投喂，与 HTML 爬取完全分开）─────────────────
    def start_news_run(self, source_api: str, subject: str = "") -> int:
        # lang 这列是接口采集时代的遗留；人工投喂版往里放主题词（如「原神」）
        cur = self.conn.execute(
            "INSERT INTO news_runs (started_at, source_api, lang, ok) VALUES (?,?,?,0)",
            (_now(), source_api, subject))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_news_run(self, news_run_id: int, *, fetched: int = 0, inserted: int = 0,
                        updated: int = 0, rejected: int = 0, ok: bool = True,
                        note: str = "") -> None:
        self.conn.execute(
            """UPDATE news_runs SET finished_at=?, fetched=?, inserted=?, updated=?,
                   rejected=?, ok=?, note=? WHERE news_run_id=?""",
            (_now(), fetched, inserted, updated, rejected,
             1 if ok else 0, note, news_run_id))
        self.conn.commit()

    def upsert_news(self, items: Iterable[Any],
                    news_run_id: int | None = None) -> dict[str, int]:
        """按 uid 幂等写入；重抓同一批只更新时间，不产生重复行。"""
        rows = [it.to_row() if hasattr(it, "to_row") else dict(it) for it in items]
        uids = [r["uid"] for r in rows]
        existing: set[str] = set()
        for i in range(0, len(uids), 500):        # sqlite 绑定变量有上限，分批查
            chunk = uids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            existing |= {r["uid"] for r in self.conn.execute(
                f"SELECT uid FROM news_items WHERE uid IN ({marks})", chunk)}
        now = _now()
        inserted = updated = 0
        with self.conn:
            for row in rows:
                if row["uid"] in existing:
                    updated += 1
                else:
                    inserted += 1
                row["news_run_id"] = news_run_id
                row["first_seen_at"] = now
                row["updated_at"] = now
                self.conn.execute(
                    """INSERT INTO news_items
                       (uid, news_run_id, game, kind, post_id, title, description, url,
                        banner, created_at, created_iso, source_api, source_name,
                        content_hash, fetched_at, first_seen_at, updated_at)
                       VALUES (:uid,:news_run_id,:game,:kind,:post_id,:title,:description,
                               :url,:banner,:created_at,:created_iso,:source_api,
                               :source_name,:content_hash,:fetched_at,:first_seen_at,
                               :updated_at)
                       ON CONFLICT(uid) DO UPDATE SET
                           news_run_id=excluded.news_run_id, title=excluded.title,
                           description=excluded.description, url=excluded.url,
                           banner=excluded.banner, created_at=excluded.created_at,
                           created_iso=excluded.created_iso,
                           source_api=excluded.source_api,
                           source_name=excluded.source_name,
                           content_hash=excluded.content_hash,
                           fetched_at=excluded.fetched_at,
                           updated_at=excluded.updated_at""",
                    row)
        return {"inserted": inserted, "updated": updated, "total": len(rows)}

    def iter_news(self, *, game: str | None = None, kind: str | None = None,
                  kinds: Iterable[str] | None = None,
                  since: int | None = None, until: int | None = None,
                  limit: int = 0, columns: str = "*") -> Iterator[sqlite3.Row]:
        """按发布时间升序返回 —— 顺序存储的读取端，默认就是有序的。

        kind 传单个类别，kinds 传一组；两个都不给就是全部类别混排。
        混排依然是一条时间轴，类别只作为每个节点的标签。

        columns 用来把 description（全长正文）挡在查询之外：只数条数、只排时间轴的
        调用方没必要把几 MB 正文拉进内存。
        """
        sql = f"SELECT {columns} FROM news_items WHERE 1=1"
        args: list[Any] = []
        if game:
            sql += " AND game = ?"
            args.append(game)
        wanted: list[str] = []
        if kind:
            wanted.extend([kind] if isinstance(kind, str) else list(kind))
        if kinds:
            wanted.extend(kinds)
        if wanted:
            sql += f" AND kind IN ({','.join('?' * len(wanted))})"
            args.extend(wanted)
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(since)
        if until is not None:
            sql += " AND created_at <= ?"
            args.append(until)
        sql += " ORDER BY created_at ASC, post_id ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        yield from self.conn.execute(sql, args)

    def news_stats(self) -> dict[str, Any]:
        """合规自检口径：缺正文/缺来源标记/缺载入时间各有多少条，应该永远是 0。

        人工投喂版没有原始链接这回事（url 恒为空），所以自检口径换成
        「正文有没有落库」「来源标记有没有写」「载入时间有没有记」；
        「日期未知」是允许出现的正常状态，单独计数、不算缺陷。
        """
        row = self.conn.execute(
            """SELECT COUNT(*)                                               AS 总数,
                      COALESCE(MIN(CASE WHEN created_at < ? THEN created_at END), 0)
                          AS 最早,
                      COALESCE(MAX(CASE WHEN created_at < ? THEN created_at END), 0)
                          AS 最新,
                      COUNT(DISTINCT kind)                                AS 类别数,
                      SUM(CASE WHEN description IS NULL OR description='' THEN 1 ELSE 0 END)
                          AS 缺正文,
                      SUM(CASE WHEN source_name IS NULL OR source_name='' THEN 1 ELSE 0 END)
                          AS 缺来源标记,
                      SUM(CASE WHEN fetched_at IS NULL OR fetched_at='' THEN 1 ELSE 0 END)
                          AS 缺载入时间,
                      SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
                          AS 日期未知
                 FROM news_items""", (UNKNOWN_TS, UNKNOWN_TS, UNKNOWN_TS)).fetchone()
        return {k: int(v or 0) for k, v in dict(row).items()}

    def news_kind_counts(self) -> dict[str, int]:
        """各类别各存了多少条 —— 报告里的「按类别」一节用它。"""
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) c FROM news_items GROUP BY kind").fetchall()
        return {r["kind"]: int(r["c"]) for r in rows}

    def save_news_summaries(self, rows: list[dict],
                            news_run_id: int | None = None,
                            *, prompt_ver: str = "") -> int:
        """按缓存键 (uid, backend, model, prompt_ver) 覆盖写入，重跑不再堆重复行。"""
        params = self._summary_rows(rows, news_run_id, prompt_ver,
                                    key="uid", run_field="news_run_id")
        if not params:
            return 0
        keys = [(r["uid"], r["backend"], r["model"], r["prompt_ver"]) for r in params]
        with self.conn:
            self.conn.executemany(
                "DELETE FROM news_summaries WHERE uid=? AND backend=? AND model=? AND prompt_ver=?",
                keys)
            self.conn.executemany(
                """INSERT INTO news_summaries
                   (news_run_id,uid,ordinal,backend,model,summary,created_at,
                    content_hash,prompt_ver)
                   VALUES (:news_run_id,:uid,:ordinal,:backend,:model,:summary,:created_at,
                           :content_hash,:prompt_ver)""",
                params)
        return len(params)

    def load_news_summary_cache(self, backend: str, model: str,
                                prompt_ver: str = "") -> dict[str, tuple[str, str]]:
        """读回已算好的逐条摘要：uid -> (content_hash, summary)。

        喂给报告的那一步以前只写不读，所以每次重跑都把 N 条内容原样再问一遍模型。
        """
        rows = self.conn.execute(
            "SELECT uid, content_hash, summary FROM news_summaries"
            " WHERE backend=? AND model=? AND prompt_ver=?", (backend, model, prompt_ver))
        return {r["uid"]: (r["content_hash"] or "", r["summary"] or "") for r in rows}

    def load_date_cache(self) -> dict[str, str]:
        """读回「日期推断」的结论：正文指纹 -> 日期（空串 = 上次结论是「未知」）。

        没有这层缓存时，每条没写日期的材料每跑一次 ingest 都要重新问模型一遍日期 ——
        同一批材料反复投喂就是同一笔钱反复付。
        """
        rows = self.conn.execute("SELECT fingerprint, date FROM date_inferences")
        return {r["fingerprint"]: (r["date"] or "") for r in rows}

    def save_date_inferences(self, rows: list[dict], model: str = "") -> int:
        """落盘日期推断结论。键是指纹，所以同一条材料只会有一行。"""
        now = _now()
        payload = [(str(r.get("fingerprint") or ""), str(r.get("date") or ""),
                    str(model or ""), now) for r in rows]
        payload = [p for p in payload if p[0]]
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany(
                """INSERT INTO date_inferences(fingerprint,date,model,created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     date=excluded.date, model=excluded.model,
                     created_at=excluded.created_at""", payload)
        return len(payload)

    def save_news_report(self, *, backend: str, model: str, items: int, span: str,
                         source_api: str, timeline: str, report: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO news_reports
               (backend,model,items,span,source_api,timeline,report,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (backend, model, items, span, source_api, timeline, report, _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_news_report(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM news_reports ORDER BY id DESC LIMIT 1").fetchone()

    # ── 导出 ────────────────────────────────────────────────
    def export_json(self, path: str | Path, *, run_id: int | None = None,
                    include_text: bool = True) -> Path:
        """导出成 JSON —— 这是给工作流的输入快照，也是人对得上账的中间产物。

        不这么写的话，几千页的正文会先在内存里拼成一个完整 dict 列表，
        再整块序列化成一个字符串 —— 峰值约是正文总量的两倍。
        这里改成一行一页流式落盘，峰值只剩一页。
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cols = (f"text,{self.PAGE_COLUMNS}" if include_text else self.PAGE_COLUMNS)
        with out.open("w", encoding="utf-8") as fp:
            fp.write("{\n")
            fp.write(f'  "exported_at": {json.dumps(_now(), ensure_ascii=False)},\n')
            fp.write(f'  "run_id": {json.dumps(run_id)},\n')
            fp.write(f'  "stats": {json.dumps(self.stats(run_id), ensure_ascii=False)},\n')
            fp.write('  "pages": [')
            for i, row in enumerate(self.iter_pages(run_id=run_id, columns=cols)):
                item = {k: row[k] for k in row.keys()}
                item["robots_allowed"] = bool(item["robots_allowed"])
                fp.write((",\n    " if i else "\n    ")
                         + json.dumps(item, ensure_ascii=False))
            fp.write("\n  ]\n}\n")
        return out
