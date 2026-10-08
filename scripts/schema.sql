-- ============================================================
-- 原神知识库 (Genshin Knowledge Base)  —— 数据库结构
-- DBMS: SQLite 3
-- 用途: 个人学习与研究
-- 说明: 所有数据均为公开信息的整理与二次评注, 不存储游戏内文本原文
-- ============================================================

PRAGMA foreign_keys = ON;

-- 1. 元信息: 数据基准版本、更新日期等 -------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- 2. 来源台账: 每条来源记录可追溯到公开出处 -------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    publisher    TEXT,           -- 发布方 / 站点
    url          TEXT,
    license_note TEXT,           -- 版权 / 许可说明
    used_for     TEXT,           -- 本库用它支撑了哪部分内容
    accessed_at  TEXT            -- 核验日期
);

-- 3. 角色: 配队的基础词汇表 ----------------------------------------------
CREATE TABLE IF NOT EXISTS characters (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    element        TEXT NOT NULL,   -- 火 / 水 / 风 / 雷 / 草 / 冰 / 岩
    weapon         TEXT NOT NULL,   -- 单手剑 / 双手剑 / 长柄武器 / 弓 / 法器
    rarity         INTEGER NOT NULL,-- 4 / 5
    region         TEXT,            -- 蒙德 / 璃月 / 稻妻 / 须弥 / 枫丹 / 纳塔 ...
    roles          TEXT,            -- 逗号分隔: 主C,副C,增伤,护盾,治疗,挂水,挂草,聚怪,充能
    limited        INTEGER,         -- 1 = 限定, 0 = 常驻
    version_added  TEXT,            -- 首次登场版本
    confidence     TEXT DEFAULT '高',-- 高 / 待核验
    note           TEXT
);

-- 4. 配队: 本库的分析主体 ------------------------------------------------
CREATE TABLE IF NOT EXISTS teams (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    archetype     TEXT,       -- 蒸发/融化/冻结/绽放/超绽放/激化/纯色/物理
    core_reaction TEXT,
    cost_level    TEXT,       -- 低 / 中 / 高  (金数成本)
    difficulty    TEXT,       -- 低 / 中 / 高  (操作难度)
    summary       TEXT,       -- 核心思路
    rotation      TEXT,       -- 循环手法
    strengths     TEXT,       -- 优势
    weaknesses    TEXT,       -- 劣势
    abyss_fit     TEXT,       -- 深渊适配场景
    tags          TEXT
);

-- 5. 队伍成员 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS team_members (
    id        INTEGER PRIMARY KEY,
    team_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    slot      TEXT,          -- 主C / 副C / 增益 / 生存 / 体系核心
    char_name TEXT,
    role_note TEXT
);

-- 6. 元素反应: 所有配队策略的物理底层 -----------------------------------
CREATE TABLE IF NOT EXISTS reactions (
    id               INTEGER PRIMARY KEY,
    name             TEXT,
    category         TEXT,   -- 增幅 / 剧变 / 结晶 / 激化 / 绽放 / 月兆系
    elements         TEXT,
    base_multiplier  TEXT,
    trigger          TEXT,
    note             TEXT
);

-- 7. 深境螺旋: 楼层结构 -------------------------------------------------
CREATE TABLE IF NOT EXISTS abyss_floors (
    floor           INTEGER PRIMARY KEY,
    chambers        INTEGER,   -- 每层间数
    halves          INTEGER,   -- 每间分段数(1=单队, 2=上下半双队)
    typical_enemies TEXT,
    environment     TEXT,      -- 地脉异常 / 环境效果
    reward_note     TEXT
);

-- 8. 深境螺旋: 奖励结构 -------------------------------------------------
CREATE TABLE IF NOT EXISTS abyss_rewards (
    id     INTEGER PRIMARY KEY,
    scope  TEXT,   -- 1-8层 / 9-12层
    item   TEXT,
    amount TEXT,
    period TEXT,   -- 一次性 / 每期刷新
    note   TEXT
);

-- 9. 深境螺旋: 机制名词 ------------------------------------------------
CREATE TABLE IF NOT EXISTS abyss_mechanics (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    category    TEXT,   -- 结构 / 环境 / 敌人 / 策略
    description TEXT,
    strategy    TEXT
);

-- 10. 商业化: 付费档位 --------------------------------------------------
CREATE TABLE IF NOT EXISTS monetization (
    id             INTEGER PRIMARY KEY,
    name           TEXT,
    price_cny      REAL,
    kind           TEXT,     -- 月卡 / 通行证 / 直充 / 外观
    contents       TEXT,
    primogem_equiv INTEGER,  -- 折算原石当量
    pulls_equiv    REAL,     -- 折算抽数
    cny_per_pull   REAL,     -- 单抽人民币成本
    design_intent  TEXT      -- 设计意图分析
);

-- 11. 创世结晶直充档位 (原始价格表) --------------------------------------
CREATE TABLE IF NOT EXISTS crystals (
    id          INTEGER PRIMARY KEY,
    price_cny   REAL,
    crystals    INTEGER,
    is_first_double INTEGER,  -- 1 = 首充双倍后
    note        TEXT
);

-- 12. 免费原石产出结构 --------------------------------------------------
CREATE TABLE IF NOT EXISTS primogem_income (
    id          INTEGER PRIMARY KEY,
    source      TEXT,
    amount_low  INTEGER,      -- 每周期原石下限 (NULL = 视版本浮动, 计入"其他")
    amount_high INTEGER,      -- 每周期原石上限
    period      TEXT,
    player_type TEXT,         -- 0氪 / 月卡 / 月卡+纪行
    note        TEXT
);

-- 13. 抽卡规则 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS gacha (
    id                 INTEGER PRIMARY KEY,
    banner             TEXT,
    rarity             TEXT,
    base_rate          TEXT,
    comprehensive_rate TEXT,
    hard_pity          INTEGER,
    soft_pity_start    INTEGER,
    avg_pulls          TEXT,
    up_rate            TEXT,
    guarantee_rule     TEXT,
    cost_primogems     INTEGER,
    note               TEXT
);

-- 14. 养成资源与树脂消耗 ------------------------------------------------
CREATE TABLE IF NOT EXISTS resources (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    resin_cost  INTEGER,
    weekly_limit TEXT,
    output      TEXT,
    priority    TEXT,
    note        TEXT
);

-- 15. 运营机制分析: 内容侧与付费侧的结合点 -------------------------------
CREATE TABLE IF NOT EXISTS operations (
    id                INTEGER PRIMARY KEY,
    seq               INTEGER,
    topic             TEXT,
    content_side      TEXT,   -- 内容侧做了什么
    monetization_side TEXT,   -- 付费侧如何变现
    linkage           TEXT,   -- 两者的咬合点
    analysis          TEXT    -- 评注
);

-- 16. 运营事故档案: 记录一次引发广泛讨论的运营事故 -------------------------
-- 说明: 本表只记录"公开可核验的事实 + 作者本人的分析", 不保存公告原文段落。
CREATE TABLE IF NOT EXISTS incidents (
    id                INTEGER PRIMARY KEY,
    code              TEXT UNIQUE,   -- 事件代号, 如 NEUV-4.8
    name              TEXT NOT NULL, -- 事件名
    version           TEXT,          -- 相关版本
    happened_on       TEXT,          -- 触发日 (改动实装)
    resolved_on       TEXT,          -- 官方回应日
    category          TEXT,          -- 数值调整 / 表达与信任 / 技术缺陷 ...
    severity          TEXT,          -- 高 / 中 / 低
    summary           TEXT,          -- 一句话概括
    trigger           TEXT,          -- 起因
    official_response TEXT,          -- 官方处理方式(要点转述)
    compensation      TEXT,          -- 补偿
    root_cause        TEXT,          -- 根因判断
    compliance_note   TEXT           -- 引用与数据性质说明
);

-- 17. 事件时间线 ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS incident_timeline (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    seq         INTEGER,
    date_label  TEXT,   -- 日期或相对时间
    actor       TEXT,   -- 官方 / 玩家 / 观察
    event       TEXT,
    evidence    TEXT    -- 依据说明(来源性质), 不复制原文
);

-- 18. 长期影响条目: 事件 → 机制 → 长期后果 --------------------------------
CREATE TABLE IF NOT EXISTS incident_impacts (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    seq         INTEGER,
    dimension   TEXT,   -- 角色供给 / 深渊需求 / 玩家预期 / 官方决策 / 商业化
    horizon     TEXT,   -- 短期 / 中期 / 长期
    impact      TEXT,   -- 影响是什么
    mechanism   TEXT,   -- 通过什么机制发生
    evidence    TEXT,   -- 观察依据
    confidence  TEXT,   -- 高 / 中 / 低
    related_op  INTEGER,-- 关联 operations.seq
    note        TEXT
);

-- 19. 数值膨胀观测指标: 把"膨胀"拆成可观测、可分级的指标 --------------------
CREATE TABLE IF NOT EXISTS inflation_metrics (
    id           INTEGER PRIMARY KEY,
    category     TEXT,   -- 需求侧·深渊 / 供给侧·角色 / 机制侧 / 商业化
    metric       TEXT,   -- 指标名
    period       TEXT,   -- 观测区间
    value_text   TEXT,   -- 观测值(含单位, 允许区间写法)
    direction    TEXT,   -- 上升 / 持平 / 下降 / 未知
    nature       TEXT,   -- 数据性质: 官方公示 / 官方公告 / 官方前瞻 / 媒体整理 / 社区测算 / 第三方估算 / 玩家观点
    source_note  TEXT,   -- 来源标注
    note         TEXT
);

-- ==========================================================================
-- 第二类数据源: MAF 内容观察工作台 (maf/ 子项目)
-- --------------------------------------------------------------------------
-- 上面 1-19 号表是作者手写的常量, 由 kb_data.py 提供, init_db.py 灌入。
-- 下面 20-24 号表不是手写的, 而是 maf/ 子里那个离线工作台"批量处理出来的
-- 结果": 一批材料进库 → 本地/云端模型逐条概括 → 生成时间轴与观察报告。
-- 它们由 scripts/import_maf.py 从 maf/data/*.db 导入, 可以反复运行(幂等)。
--
-- 注意: init_db.py 会删库重建, 这五张表会被清空, 必须重新跑一次 import_maf.py。
-- 顺序: init_db.py  →  import_maf.py  →  build_site.py
-- ==========================================================================

-- 20. 观察批次台账: 一次"投喂一批材料"就是一条 -----------------------------
CREATE TABLE IF NOT EXISTS maf_runs (
    id          INTEGER PRIMARY KEY,
    source_db   TEXT,    -- 来自哪个原库: app.db / crawl.db
    orig_run_id INTEGER, -- 原库里的 news_run_id
    subject     TEXT,    -- 批次主题词
    origin      TEXT,    -- 人工投喂 / 第三方接口镜像
    source_api  TEXT,    -- 原库 source_api 原值(出处可审计)
    started_at  TEXT,
    finished_at TEXT,
    fetched     INTEGER,
    inserted    INTEGER,
    updated     INTEGER,
    rejected    INTEGER,
    ok          INTEGER,
    note        TEXT,
    items       INTEGER, -- 导入后落在 maf_items 的条数
    UNIQUE(source_db, orig_run_id)
);

-- 21. 观察条目: 每条材料的"骨架 + 出处 + 处理口径" ------------------------
-- 只存骨架与派生结果, 不存上游正文(见 body_policy)。人工投喂线是自己粘的
-- 文本, 正文保留在 body 里; 第三方镜像线的正文一律不落库。
CREATE TABLE IF NOT EXISTS maf_items (
    id           INTEGER PRIMARY KEY,
    uid          TEXT UNIQUE NOT NULL, -- 原库主键, 也是幂等导入的依据
    run_id       INTEGER REFERENCES maf_runs(id),
    game         TEXT,
    kind         TEXT,     -- notices / events / info (原库分类)
    post_id      TEXT,
    title        TEXT,     -- 标题(短事实标识, 非正文转载)
    created_at   INTEGER,  -- 上游发布时间(Unix 秒), 9999999999 = 日期未知
    created_iso  TEXT,
    date_label   TEXT,     -- 展示用: YYYY-MM-DD 或 "日期未知"
    seq          INTEGER,  -- 全局时间序, 从 1 开始
    origin       TEXT,     -- 人工投喂 / 第三方接口镜像
    source_name  TEXT,
    url          TEXT,     -- 出处指针(可回溯核验)
    body         TEXT,     -- 仅人工投喂线保留正文; 其余为 NULL
    body_policy  TEXT,     -- 正文处理口径说明
    content_hash TEXT
);

-- 22. 逐条概括: 模型对每条材料的一句话概括(可能有多个后端/多次尝试) -------
CREATE TABLE IF NOT EXISTS maf_summaries (
    id         INTEGER PRIMARY KEY,
    uid        TEXT NOT NULL,
    item_id    INTEGER REFERENCES maf_items(id) ON DELETE CASCADE,
    ordinal    INTEGER,   -- 原库里的批次内序号
    backend    TEXT,      -- local / cloud / local-stub
    model      TEXT,      -- qwen3:8b / deepseek-chat / stub
    summary    TEXT,
    created_at TEXT,
    is_primary INTEGER,   -- 1 = 该条目对外展示时选用的那一条
    UNIQUE(uid, backend, model, summary)
);

-- 23. 观察报告: 模型对整批材料生成的时间轴 + 观察报告 ----------------------
CREATE TABLE IF NOT EXISTS maf_reports (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER REFERENCES maf_runs(id),
    source_db  TEXT,
    orig_id    INTEGER,   -- 原库 news_reports.id
    backend    TEXT,
    model      TEXT,
    items      INTEGER,   -- 这份报告覆盖的条目数
    span       TEXT,      -- 覆盖的时间跨度
    origin     TEXT,
    timeline   TEXT,      -- 模型生成的时间轴(要点转述)
    report     TEXT,      -- 模型生成的观察报告
    created_at TEXT,
    is_primary INTEGER,
    UNIQUE(source_db, orig_id)
);

-- 24. 观察条目 ↔ 知识库实体 的关联: 让"批量材料"能挂到已有知识上 ----------
-- 纯关键词匹配, 不用模型, 命中依据记录在 matched_by 里, 便于复核。
CREATE TABLE IF NOT EXISTS maf_item_links (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES maf_items(id) ON DELETE CASCADE,
    kind       TEXT,   -- 角色 / 版本 / 运营事故
    ref_table  TEXT,   -- characters / versions / incidents
    ref_key    TEXT,   -- 角色名 / x.y / 事件代号
    matched_by TEXT,   -- 命中的关键词
    confidence TEXT,   -- 高(命中标题) / 中(仅命中概括)
    UNIQUE(item_id, ref_table, ref_key)
);

-- 索引 ------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_char_element ON characters(element);
CREATE INDEX IF NOT EXISTS idx_team_arch   ON teams(archetype);
CREATE INDEX IF NOT EXISTS idx_member_team ON team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_tl_incident ON incident_timeline(incident_id);
CREATE INDEX IF NOT EXISTS idx_im_incident ON incident_impacts(incident_id);
CREATE INDEX IF NOT EXISTS idx_infl_cat    ON inflation_metrics(category);
CREATE INDEX IF NOT EXISTS idx_maf_item_created ON maf_items(created_at, post_id);
CREATE INDEX IF NOT EXISTS idx_maf_item_kind    ON maf_items(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_maf_sum_item     ON maf_summaries(item_id);
CREATE INDEX IF NOT EXISTS idx_maf_sum_uid      ON maf_summaries(uid);
CREATE INDEX IF NOT EXISTS idx_maf_report_run   ON maf_reports(run_id);
CREATE INDEX IF NOT EXISTS idx_maf_link_item    ON maf_item_links(item_id);
CREATE INDEX IF NOT EXISTS idx_maf_link_ref     ON maf_item_links(ref_table, ref_key);
