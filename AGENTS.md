# AGENTS.md — 给 AI 的项目记忆

> 给接手这个仓库的 AI（或几个月后忘了细节的人）看的一份简报。
> **动手前先读完第 1 节的三条硬约束。** 其余章节按需要查。

---

## 0. 这是什么

一个**本地、离线、单文件**的原神资料与分析知识库，作者是个人学习者，用途是个人学习与研究。

- 数据全部落在一个 SQLite 文件 `genshin_kb.db` 里（24 张表）。
- 可视化页面 `site/index.html` 是**从数据库导出**的静态单文件，双击就能开，不需要服务器。
- `maf/` 是一个被移动进来的独立子项目（MAF 内容观察工作台），负责把一批文本材料批量处理成概括、时间轴与报告。
- 仓库里还有一份可编辑的专题报告（那维莱特 4.8 事件 × 数值膨胀）。

**它不是**：攻略站、数据挖掘项目、自动化工具、爬虫。见第 1 节。

---

## 1. 三条硬约束（不要违反）

### 1.1 合规：不抄文字、不存素材、不抓取

完整口径见 `数据来源与合规说明.md`。落到代码上的四条：

1. **能用官方公示就用官方公示**；第三方 / 社区内容只用来**对参数**，表述一律**重写**，绝不复制原文段落。
2. **只存机制，不存素材**：不收录游戏内文本、立绘、图标、音频、剧情原文。
3. **估算必须标注**：涉及金额与概率的推算一律标「社区测算」/「第三方估算」/「玩家观点」，
   与「官方公示」「官方公告」严格区分。`inflation_metrics.nature` 与页面上的标签就是这个用途。
4. **官方没说的不推定**。抽不到的日期写「日期未知」，不猜、不补。

⚠️ `maf/` 里有一条更硬的版本：它的测试 `maf/tests/test_news_offline.py::TestCompliance`
会**直接扫源码里的 `urllib` / `requests` / `socket` / `http:`**，并断言采集层文件不存在。
历史上 maf 接过第三方社区接口，后来按要求把 `hoyo_api.py` 连测试一起删了。
**不要往 maf 里加任何网络访问代码**，那会让它的测试直接红。

### 1.2 单一数据源：手写内容只在 `scripts/kb_data.py`

1-19 号表的内容 100% 来自 `scripts/kb_data.py` 里的常量。
**不要在数据库里手改数据**，也不要在 `template.html` 里硬编码内容 —— 下次跑 `init_db.py` 就没了。

**唯一的例外**是 20-24 号表（`maf_*`）：它们由 `scripts/import_maf.py` 从 `maf/data/*.db` 导入。
`init_db.py` 会**清空**这几张表，所以顺序必须是 `init_db.py` → `import_maf.py` → `build_site.py`。

### 1.3 产物可编辑：Markdown 是入口，docx 是生成物

`那维莱特运营事故与数值膨胀长期影响分析报告.md` 是唯一手改入口；
`.docx` 由 `scripts/md_to_docx.py` 生成，**直接改 docx 不会同步回去**。

---

## 2. 目录与职责

```
原神知识库/
├── genshin_kb.db                 24 张表；构建产物，不进版本历史（但当前选择入库备份）
├── AGENTS.md                     本文件
├── README.md                     面向人的说明
├── 数据来源与合规说明.md           合规细则（第 4 节讲运营事故与膨胀数据）
├── 那维莱特…报告.md / .docx        专题报告（.md 可编辑，.docx 生成物）
├── site/
│   ├── template.html             页面模板（改界面的唯一入口）
│   ├── index.html                构建产物（自包含，~530 KB，含内嵌 JSON）
│   └── data.json                 同内容快照，便于二次开发
├── maf/                          独立子项目，见第 5 节
└── scripts/
    ├── schema.sql                24 张表的建表语句 + 索引
    ├── kb_data.py                手写内容常量（~725 行，1-19 号表的数据源）
    ├── init_db.py                删库 → 跑 schema.sql → 灌常量
    ├── import_maf.py             读 maf 的两个库 → 写 20-24 号表（幂等）
    ├── build_site.py             导 24 张表 → 替换模板占位符 → 写 index.html + data.json
    ├── query.py                  命令行查询
    ├── md_to_docx.py             Markdown → docx（纯 python-docx）
    └── smoke_test.js             页面脚本自检（Node，最小 DOM 桩）
```

---

## 3. 三条构建命令（背下来）

```bash
# 改了 kb_data.py / schema.sql / template.html
python scripts/init_db.py && python scripts/import_maf.py && python scripts/build_site.py

# 只改了 maf 的材料
python scripts/import_maf.py && python scripts/build_site.py

# 验证
node scripts/smoke_test.js
```

**必须跑 `smoke_test.js`**：它会用最小 DOM 桩真的执行一遍页面脚本，
检查 9 个标签页各渲染了多少字符、有没有 `undefined` / `NaN` / `[object Object]`，以及关键内容是否还在。
页面脚本是纯字符串拼接，**打错一个变量名不会报错，只会渲染出 `undefined`** —— 这个测试就是为此存在的。

---

## 4. `scripts/kb_data.py` 的常量地图

顶层常量（按文件顺序）：`ACCESSED` → `META` → `SOURCES` → `CHARACTERS` → `TEAMS` →
`TEAM_MEMBERS` → `REACTIONS` → `ABYSS_FLOORS` → `ABYSS_REWARDS` → `ABYSS_MECHANICS` →
`MONETIZATION` → `CRYSTALS` → `PRIMOGEM_INCOME` → `GACHA` → `RESOURCES` → `OPERATIONS` →
`INCIDENTS` → `INCIDENT_TIMELINE` → `INCIDENT_IMPACTS` → `INFLATION_METRICS`。

细节：

- `TEAM_MEMBERS` 是**以队伍名为键的 dict**；`init_db.py` 用 `SELECT name,id FROM teams` 建映射。
- `INCIDENT_TIMELINE` / `INCIDENT_IMPACTS` 是**以事件 code 为键的 dict**（现有 `NEUV-4.8`、`BARB-3.0`）；
  `init_db.py` 用 `SELECT code,id FROM incidents` 建映射。**新增事件时三处都要加。**
- `INFLATION_METRICS.nature` 的合法取值（七类，页面标签与合规说明都按它来）：
  `官方公示` / `官方公告` / `官方前瞻` / `媒体整理` / `社区测算` / `第三方估算` / `玩家观点`。
- 角色 `confidence` 标「待核验」的，页面会用红标签显示 —— 那是给作者自己看的复核提醒。

---

## 5. `maf/` 子项目

原本是独立仓库，现已移动为子目录。**它的 venv、测试、桌面应用都是自洽的，不要打散它。**

- 数据在 `maf/data/app.db`（**人工投喂线**）与 `maf/data/crawl.db`（**历史第三方接口镜像线**）。
- 用自己的虚拟环境：`maf/myvenv/Scripts/python.exe`（Python 3.14.6）。
- **没有 pytest**，只有 `unittest`：
  ```bash
  cd maf
  .\myvenv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t tests -p "test_*.py"
  ```
  期望结果：**0 failures, 0 errors**（迁移后实测通过）。
  ⚠️ 别用 `maf/run_tests.ps1` 判断成败 —— 它设了 `$ErrorActionPreference='Stop'`，
  而测试会往 stderr 写日志，于是脚本会抛 `NativeCommandError`，看起来像失败。**直接跑 unittest 才拿得到干净的退出码。**
- Windows venv 可以随目录整体移动，`pyvenv.cfg` 里的旧路径只是装饰。

### 性能：重跑不再从头付费（2026-10 做过一轮优化）

同一份材料重跑一次，不该再问一遍模型。落地成四张缓存 + 几处写入优化：

| 缓存 | 键 | 落在哪 |
|---|---|---|
| 逐页 / 逐条摘要 | `(url 或 uid, backend, model, prompt_ver)` | `summaries` / `news_summaries` 的 UNIQUE 索引 |
| 日期推断 | `fingerprint = 标题 + 正文` | `date_inferences` 表（`fingerprint TEXT PRIMARY KEY`） |
| 本地服务探测 | `(base_url, token)` | 进程内 `lru_cache` |
| 并发路数 | 按后端类型 | 云端 4 路、本地 1 路（单进程 llama-server 上 4 路只是排队） |

⚠️ **改了提示词就必须同时改 `PROMPT_VERSION` / `NEWS_PROMPT_VERSION`**，否则旧概括会被当成
新提示词的答案直接复用。同理，缓存按 `backend + model` 隔离，换模型不会串味。

⚠️ **缓存表要靠 UNIQUE 索引兜底去重，不能靠调用方自觉** —— `save_summaries` 现在是
`INSERT ... ON CONFLICT(...) DO UPDATE`，重跑覆盖而不是堆行。历史上这里没有约束，
手机上一次 14 页的 digest 跑两遍就存成了 28 条摘要。

其它写入优化：`pages.json` 只在 `crawl` 时导出一次（`digest` 复用并检查 `st_mtime`）、
`export_json` 改流式写盘不再全量物化、`write_if_changed` 内容没变就不重写文件、
`upsert_links` 补了事务、`probe-local --scan` 改 128 路并发并按块打印进度、
`asyncio.gather(return_exceptions=True)` 不让一条失败毁掉整批已付费的结果。
`--deck-plain` 可以在没有 python-pptx 时只出大纲。

### 桥接脚本的合规取舍（重要）

`maf_items.body` **只对人工投喂线保留正文**。第三方镜像线一律留空，理由写在同一行的 `body_policy` 字段。
要让镜像线的正文也入库，必须显式加 `--include-third-party-body` —— 默认不开，
是为了让「库里有没有上游原文」这件事必须被主动选择一次。

### 别踩的坑

- `maf_items.news_run_id` 会被后续批次**覆盖**（原库的 upsert 行为），所以同一批公告最终只归属最后一次触碰它的批次；
  采到 0 条的批次不是失败。
- 同理，`first_seen_at` 在 `ON CONFLICT DO UPDATE` 时也会被覆盖 —— 原库没有真正做到「保留首次时间」。
- `news_items.created_iso` 可能为空字符串（已有一条），`created_at` 可能是 `UNKNOWN_TS = 9999999999`。
  **两者都要当「日期未知」处理，不要推断。**
- `maf_items` 的条目是纯字符串关联到知识库实体的（`maf_item_links`），用 `VERSION_RE` 抽版本号、
  用角色名子串匹配。**当前 54 条材料里没有任何一位知识库收录的角色出现**，所以角色/事故关联数是 0 ——
  这是数据事实，不是匹配坏了。宁可关联为空，也不要挂一条查不出出处的关联。
- ⚠️ `maf_summaries` 上有 `UNIQUE(uid, backend, model, summary)`：同一材料 + 同一模型 + **同一段文字**的
  重复概括只会留一条。所以 `import_maf.py` 在插入前必须把 `is_primary=1` 的行排到最前面
  （`main()` 里那句 `sum_rows.sort(key=lambda row: -row[8])` 加随后的 id 重排），
  否则主概括会被 `INSERT OR IGNORE` 丢掉，条目在页面上就没有概括可展示，下游的关联匹配也拿不到它的概括文本。
  **这个坑踩过一次**：修复前 54 个条目里只有 36 个有主概括。脚本末尾会回读数据库打印
  「落库后有主概括的条目: N / 54」，这个数不等于条目总数就说明又出问题了。

---

## 6. `site/template.html` 的约定

- 文件里有且只有一个 `@@KB_JSON@@` 占位符；`build_site.py` 要求替换前后它都**恰好出现 1 次**，否则报错退出。
- 页面结构：`<main>` 里 9 个 `<section id="tab-xxx">`（空的，运行时填），
  `const TABS` 是 `[id, 中文, 英文]` 数组，`buildNav()` 按它生成导航。
- 每个标签页是一个 IIFE，形如：
  ```js
  (function name(){ let h = sectionHead(标题,英文,导语); h += ...; $("#tab-name").innerHTML = h; })();
  ```
  注意：页面脚本里 `$` 是 `document.querySelector`，**不要和 jQuery 混淆**。
- 可用的小组件：`esc(s)` `fmt(n)` `bars(rows,color,unit)` `tag(t,cls)` `sectionHead(t,en,lead)`
  `kpi(k,v,u,n,cls)` `fnode(n,t,d)` `kv(k,v)` `elBadge(e)`。
  CSS 类：`card` `lead` `muted` `hr` `kpis/kpi(.g/.b/.p)` `flow/fnode(.pay)/farrow` `tl` `bars/brow`
  `tag(.gold/.blue/.purple/.red/.gray)` `tw`（表格横向滚动容器）`calc` `grid2` `note(.warn/.ok)`
  `ops/op(.bd)/lk(.join)/ana` `pill(.warnp)`。
- 文件末尾是全局搜索 IIFE（跨约 10 类实体），也是按 IIFE 分组渲染。

### 硬编码需要同步的地方（改了表结构就会忘）

1. 页面顶部的 `<span class="chip">SQLite NN 张表</span>`
2. `data()` IIFE 里的 `tables` 数组
3. `data()` IIFE 里的 `kv("表数量","NN 张")`
4. `overview()` IIFE 里的 `stat` 数组
5. `smoke_test.js` 的 `tabs` 数组与 `must` 关键词数组
6. `scripts/build_site.py` 的 `TABLES` 数组（少一张表，页面上就是空的）

---

## 7. 那维莱特事件的口径（写报告 / 改文案时别搞错）

- **两个原石数不是一回事**：4.8 更新说明把该改动归入「问题修复」，随更新补偿**原石 ×100**；
  次日（2024-07-18）官方公告宣布**回退**并另补**原石 ×1600**。写文案时必须区分。
- 官方从未把它定义为**平衡性调整**，玩家的反弹正发生在「异常修复 vs 削弱我抽到的角色」这套分类冲突上。
- **官方从未公布回退的实装版本与日期**（只说「后续更新、细则另告」，5.0 更新说明里已无该专项条目）。
  **不要推定日期。** 时间线末条是「核查结论」，不是「回退未执行」。
- 官方从未公开使用「数值膨胀」一词；深渊血量 / DPS 的历史曲线**全部是社区测算**，只能看趋势与倍数。
- 游戏的**主因**是 42 天双卡池的商业节奏，那维莱特事件是加速器不是发动机 —— 报告里有一条专门的「反面证据」在讲这个。
  改结论时请连同反面证据一起改，不要只留论点。

---

## 8. 环境与工具备注

- Python：3.14.6（`python` 在 PATH 上）。Node 可用（`node scripts/smoke_test.js`）。
- 这台机器的工作目录路径含中文与全角括号。**PowerShell 脚本一律用 `-LiteralPath`**，
  并且处理中文输出时先设 `$env:PYTHONIOENCODING='utf-8'`。
  历史上这个工作区出过一次 ACL 故障（`SetNamedSecurityInfoW failed (Win32 5)`），已修复；
  如果 `pwsh` 又整体不可用，先怀疑文件权限而不是命令本身。
- **浏览器工具需要用户的 Edge 装着对应的扩展并保持连接**。没连上时 `browser_navigate` 报
  `no browser extension is connected to the bridge`。
- **`browser_navigate` 不支持 `file:` 协议**。要在浏览器里看页面，先起本地服务：
  ```bash
  python -m http.server 8791 --bind 127.0.0.1 --directory site
  # 然后访问 http://127.0.0.1:8791/index.html
  ```
- 没有浏览器时，可以用无头 Chrome 验证真实 DOM：
  ```bash
  & "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new --disable-gpu `
    --virtual-time-budget=6000 --dump-dom "http://127.0.0.1:8791/index.html"
  ```
  （所有标签页的 `innerHTML` 都会被填好，只有可见性靠 CSS 控制，所以 dump 出来的 DOM 能完整检查。）
- `gh` 已装并已登录（账号 `96312-reneren`，协议 HTTPS）。远程私有仓库就是
  `origin` → `https://github.com/96312-reneren/genshin-kb`（分支 `main`）。
- **手机端（REDMI Turbo 4 Pro）**：那套工作流原型跑在 `com.dsh.client`（DSHA）的 proot 里，
  手机上的位置是 `/sdcard/Download/crawl-maf-pipeline`；`adb` 在
  `E:\AndroidSDK\platform-tools\adb.exe`，设备序列号 `22cfb982`。手机上的短板是
  **Pillow / python-docx / python-pptx 与中文字体**，`maf/render_office.py` 的 `FONT_FILES`
  已经排了 Linux → Android 的候选并支持 `MAF_FONT` 覆盖。
- **不要在 PC 上代跑手机端测试**：Termux（`run-as com.termux`）里没有 Python，
  DSHA 的包不是 debuggable、`/data/data` 进不去，无 root —— 真机验证只能由作者在 DSHA 的
  proot 里手工执行 `bash run_tests.sh`。推送方式：`adb push` 到 `…/crawl-maf-pipeline.new`
  再改名就位，旧目录改名成 `crawl-maf-pipeline.bak-<日期>` 留作回滚（改名是原子的，别原地覆盖）。

---

## 9. 当前状态与待办

**已完成**：19 张手写表 + 5 张 maf 导入表；9 个标签页；专题报告 md/docx；
maf 迁移（文件哈希校验一致）；`import_maf.py` 桥接（幂等）；
`maf/` 性能优化一轮（86 项测试通过）并已推送手机。

**版本管理**：这个目录已经是一个 git 仓库（分支 `main`，首次提交把数据库与产物一起入库当备份）。
`.gitignore` 只排除可重建的环境（`maf/myvenv/`、`__pycache__/`）与运行时副产物（`*.db-wal`）。
**注意**：数据库是二进制，每次重建都会整体换一份 —— 提交信息里请写清「这次重建改了什么」，
否则将来 diff 里只有一个 `Bin xxxxx bytes`，看不出内容变化。

**已知待办**：

- ~~建 GitHub 远程仓库~~ 已完成（`origin` = `https://github.com/96312-reneren/genshin-kb`，私有）。
- **手机端那套原型已按 PC 版 `maf/` 重做性能优化并推送过去**（`/sdcard/Download/crawl-maf-pipeline`，
  35 个文件；旧版留在 `…/crawl-maf-pipeline.bak-20261002` 可回滚）。**但真机上那 86 项测试还没跑过** ——
  adb 进不去 DSHA 的 proot，需要作者在手机里手工执行
  `cd /sdcard/Download/crawl-maf-pipeline && bash run_tests.sh`。
- `maf/data/crawl.db` 的 43 条条目来自**历史第三方接口镜像**，其上游正文按合规取舍没有入库。
  如果将来要用它们，优先考虑回到官方公告取一手内容，而不是重新打开镜像接口。
- 知识库 `characters` 表最远到 5.3 附近的纳塔角色；7.x 的新角色（如材料里提到的菲林斯、伊涅芙、
  薇斯纳、沃雅妮莎）**尚未收录**，这正是观察材料关联不到角色的原因。补角色表是个自然的下一步。
- `maf/` 里 `app/` 桌面应用与 `llm.py` / `news_workflow.py` 尚未被知识库侧完整读过；
  如果要改 maf 的流程，先读它自己的 `maf/README.md`（266 行，写得很细）。

---

## 10. 做事方式（作者偏好）

- 结论要能追溯到出处：每个判断都标清是「官方公示」还是「社区测算」，宁可写「不知道」。
- **分析与事实分开写**：报告里的因果链是作者的分析，必须标明，并同时呈现反面证据。
- 改了数据就重跑构建 + 自检，不要把「应该没问题」当成验证。
- 产物要能被作者手动改：优先 Markdown、SQLite、纯文本；docx/pptx 只作为生成物。
