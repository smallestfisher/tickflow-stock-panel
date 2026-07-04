# Market News And Research Report Design

**Goal**

给 tickflow 接入 go-stock 里的四类市场资讯内容:市场快讯(财联社电报)、个股研报、行业研报、个股公告。四类内容均可在前端网页浏览,并可通过 Telegram 机器人对话查询。

**Scope**

- **市场快讯**:后台独立轮询线程持续抓取财联社电报,落库到 `data/news.db`(SQLite),支持去重、倒序分页、主题/个股标签。**不做情绪标注**。
- **个股研报 / 行业研报 / 个股公告**:实时按需拉取东财公开端点,**不落库**。
- **REST API**:新增 `api/news.py`,4 个路由。
- **Telegram**:注册 4 个只读命令(`/news`、`/report`、`/ireport`、`/notice`),复用现有 COMMANDS 表 + NL 路由。
- **前端**:新增 `pages/News.tsx`(Tab 切换四类),加路由 + 导航菜单项。
- **测试**:落库/去重/分页单测、解析函数单测、命令处理器兜底测试。

数据源全部为东财/财联社**免费公开端点**,不消耗付费 key,不依赖 `CapabilitySet`。

**Approach**

数据获取与存储分层,对齐现有 `free_adapter` 抓取风格与 `telegram_bot`/`quote_service` 的线程模型。快讯持续累积故落库,研报/公告一次性查询故实时拉,减少存储与维护面。

**数据源(免费公开端点,仿 free_adapter 的 httpx + UA/Referer 约定)**

| 内容 | 端点 | 方式 | 落库 |
|------|------|------|------|
| 市场快讯(电报) | 财联社 `https://www.cls.cn/telegraph` | GET HTML,selectolax 解析 | 是 |
| 个股研报 | 东财 `https://reportapi.eastmoney.com/report/list2` | POST JSON | 否 |
| 行业研报 | 东财 `https://reportapi.eastmoney.com/report/list` | GET | 否 |
| 个股公告 | 东财 `https://np-anotice-stock.eastmoney.com/api/security/ann` | GET JSON | 否 |

HTML 解析依赖:**selectolax**(lexbor 内核,轻量,CSS 选择器体验贴近 goquery)。加入 `pyproject.toml`。

**后端结构(新增文件,不改现有源码逻辑)**

```
backend/app/services/
  news_source.py      # 四类内容抓取适配器(纯函数, 仿 free_adapter 风格)
  news_store.py       # 快讯 SQLite 落库: 建表/去重插入/倒序分页查询
  news_poller.py      # 独立 daemon 轮询线程(仿 telegram_bot/quote_service)
backend/app/api/
  news.py             # 4 个 REST 路由
```

- **`news_source.py`**:
  - `fetch_telegraph(timeout) -> list[dict]`:抓财联社电报,selectolax 解析出 time/content/is_red/url/source + 主题标签 + 关联个股。
  - `fetch_stock_report(code, days) -> list[dict]`:个股研报,东财 report/list2 POST。
  - `fetch_industry_report(industry_code, days) -> list[dict]`:行业研报,report/list GET。
  - `fetch_stock_notice(codes, ...) -> list[dict]`:个股公告,security/ann GET。
  - 所有函数失败静默返回空列表,不抛异常。code 归一复用 `telegram_commands.normalize_symbol` 的规则(或抽取共用)。

- **`news_store.py`**(SQLite,标准库 `sqlite3`,DB 路径 `repo.store.data_dir / "news.db"`):
  - 表 `telegraph`:`id INTEGER PK`、`time TEXT`、`content TEXT`、`is_red INTEGER`、`url TEXT`、`source TEXT`、`content_hash TEXT`、`created_at TEXT`。
  - 表 `telegraph_tags`:`telegraph_id INTEGER`、`tag TEXT`、`tag_type TEXT`(`subject` / `stock`)。
  - 去重键:`(source, content_hash)` 唯一索引;`content_hash` = content 的稳定哈希。
  - `init_db(db_path)`:建表 + 索引(幂等)。
  - `insert_telegraphs(db_path, items) -> int`:批量插入,已存在跳过,返回新增数。
  - `list_telegraphs(db_path, source=None, limit=50, before_id=None) -> list[dict]`:倒序分页,带标签聚合。
  - 连接每次调用打开/关闭(轮询低频,无需连接池),`WAL` 模式避免读写锁争用。

- **`news_poller.py`**:`NewsPollerService`,daemon 线程自持事件循环(仿 `telegram_bot.TelegramBotService`)。
  - 配置热读 `preferences`:`get_news_poll_enabled()`(默认关)、`get_news_poll_interval()`(默认 300s)。
  - 循环:`fetch_telegraph` → `insert_telegraphs` → sleep(interval),失败退避。
  - `start()`/`stop()`/`restart()`/`is_running()`,与 telegram_bot 一致。
  - 启动时 `init_db`。

**`preferences.py` 新增**:`get/set_news_poll_enabled`、`get/set_news_poll_interval`(存 preferences JSON,仿现有 `telegram_enabled`)。

**`main.py` lifespan**:装配 `news_poller` 单例挂 `app.state.news_poller`,try/except 保护(失败不影响启动),shutdown 时 `stop()`。`include_router(news.router)`。

**REST API(`api/news.py`,前缀 `/api/news`)**

- `GET /api/news/telegraph?source=&limit=&before_id=` — 快讯列表(读库)。
- `GET /api/news/report/{code}?days=` — 个股研报(实时拉)。
- `GET /api/news/industry-report?industry=&days=` — 行业研报(实时拉)。
- `GET /api/news/notice/{code}` — 个股公告(实时拉)。

**Telegram 命令(`telegram_commands.py` 注册 4 个只读命令)**

- `/news [来源]` — 最近快讯(读 news.db,默认全部来源)。
- `/report <代码>` — 个股研报(实时拉,如 `/report 600519`)。
- `/ireport <行业>` — 行业研报(实时拉)。
- `/notice <代码>` — 个股公告(实时拉)。

处理器复用现有 `normalize_symbol`/`_name_map`,输出 HTML 消息,自行 try/except 返回可读文本。NL 路由(`telegram_agent.py`)自动把这些命令纳入能力目录,无需改动。

**前端(仿现有 pages 结构)**

- 新页面 `pages/News.tsx`:Tab 切「快讯 / 个股研报 / 行业研报 / 公告」。快讯用 Tanstack Query 定时刷新;研报/公告按代码/行业输入查询。
- `router.tsx` 加 `/news` 路由;`components/Layout` 导航加「快讯」项(icon `Newspaper`)。
- `lib/api.ts` 加类型(`Telegraph`/`ResearchReport`/`StockNotice`)+ fetch 函数;`useSharedQueries.ts` 加 hooks。

**Error Handling**

- 所有抓取失败静默降级返回空,不阻断主流程(对齐 `free_adapter`/`webhook_adapter`)。
- 轮询线程单轮异常吞掉并退避,循环永不崩。
- Telegram 命令处理器 try/except 返回可读提示。
- 前端 API 失败走现有 `request` 统一 toast。

**Testing**

- `news_store`:`:memory:` SQLite 测建表/去重/倒序分页/标签聚合。
- `news_source`:喂固定 HTML/JSON 样本测解析函数(不打真网)。
- 命令处理器:参数解析 + 空结果兜底。
- `uv run ruff check .`、`uv run mypy app`、`uv run pytest`;前端 `pnpm build`、`pnpm lint`。
