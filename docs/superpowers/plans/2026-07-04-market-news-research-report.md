# Market News And Research Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 tickflow 接入四类市场资讯 —— 市场快讯(财联社电报,落库)、个股研报、行业研报、个股公告(实时拉),支持前端网页浏览与 Telegram 对话查询。

**Architecture:** 抓取层(`news_source.py`,仿 `free_adapter` 的 httpx + UA/Referer)与存储层(`news_store.py`,SQLite)分离;快讯由独立 daemon 轮询线程(`news_poller.py`,仿 `telegram_bot`)持续落库,研报/公告由 REST 路由实时拉。Telegram 命令复用现有 COMMANDS 表,前端新增单页 Tab。

**Tech Stack:** FastAPI · httpx · selectolax(HTML 解析)· sqlite3(标准库)· React 18 + Tanstack Query · lucide-react

---

## File Structure

**Backend (new):**
- `backend/app/services/news_source.py` — 四类抓取适配器(纯函数)
- `backend/app/services/news_store.py` — 快讯 SQLite 落库/去重/分页
- `backend/app/services/news_poller.py` — 独立轮询线程
- `backend/app/api/news.py` — 4 个 REST 路由
- `backend/tests/test_news_store.py` — 落库/去重/分页单测
- `backend/tests/test_news_source.py` — 解析函数单测(固定样本)

**Backend (modify):**
- `backend/pyproject.toml` — 加 `selectolax` 依赖
- `backend/app/services/preferences.py` — 新增快讯轮询开关/间隔
- `backend/app/main.py` — 装配 poller 单例 + include_router
- `backend/app/services/telegram_commands.py` — 注册 4 个命令

**Frontend (new):**
- `frontend/src/pages/News.tsx` — 快讯页(Tab 切四类)

**Frontend (modify):**
- `frontend/src/lib/api.ts` — 类型 + fetch 函数
- `frontend/src/lib/queryKeys.ts` — 新增 query key
- `frontend/src/router.tsx` — `/news` 路由
- `frontend/src/components/Layout.tsx` — 导航菜单项

---

## Task 1: 加 selectolax 依赖

**Files:**
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: 在 dependencies 列表加 selectolax**

在 `backend/pyproject.toml` 的 `dependencies` 数组里,`plyer` 那行之后加一行:

```toml
    "selectolax>=0.3.21",         # 财联社电报 HTML 解析 (lexbor 内核, CSS 选择器)
```

- [ ] **Step 2: 安装并验证导入**

Run: `cd backend && uv sync`
Expected: 安装成功,无报错。

Run: `cd backend && uv run python -c "from selectolax.parser import HTMLParser; print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock
git commit -m "chore: 加 selectolax 依赖 (财联社电报 HTML 解析)"
```

---

## Task 2: news_store — SQLite 落库层

**Files:**
- Create: `backend/app/services/news_store.py`
- Test: `backend/tests/test_news_store.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_news_store.py`:

```python
"""news_store 落库/去重/倒序分页单测 (SQLite 临时文件)。"""
from __future__ import annotations

from pathlib import Path

from app.services import news_store


def _mk_item(content: str, time: str = "10:00:00", source: str = "财联社电报",
             is_red: bool = False, url: str = "",
             subjects: list[str] | None = None,
             stocks: list[str] | None = None) -> dict:
    return {
        "time": time,
        "content": content,
        "is_red": is_red,
        "url": url,
        "source": source,
        "subjects": subjects or [],
        "stocks": stocks or [],
    }


def test_init_db_idempotent(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    news_store.init_db(db)  # 再来一次不应报错
    assert db.exists()


def test_insert_and_list(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    n = news_store.insert_telegraphs(db, [
        _mk_item("利好消息 A", time="10:00:00"),
        _mk_item("利好消息 B", time="10:01:00"),
    ])
    assert n == 2
    rows = news_store.list_telegraphs(db)
    assert len(rows) == 2
    # 倒序: 最后插入的在最前
    assert rows[0]["content"] == "利好消息 B"


def test_insert_dedup(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    item = _mk_item("重复内容", time="10:00:00")
    assert news_store.insert_telegraphs(db, [item]) == 1
    # 同 source + 同内容 → 跳过
    assert news_store.insert_telegraphs(db, [item]) == 0
    assert len(news_store.list_telegraphs(db)) == 1


def test_list_filter_by_source(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    news_store.insert_telegraphs(db, [
        _mk_item("A", source="财联社电报"),
        _mk_item("B", source="新浪财经"),
    ])
    rows = news_store.list_telegraphs(db, source="新浪财经")
    assert len(rows) == 1
    assert rows[0]["content"] == "B"


def test_list_limit_and_before_id(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    news_store.insert_telegraphs(db, [_mk_item(f"C{i}", time=f"10:0{i}:00") for i in range(5)])
    page1 = news_store.list_telegraphs(db, limit=2)
    assert len(page1) == 2
    last_id = page1[-1]["id"]
    page2 = news_store.list_telegraphs(db, limit=2, before_id=last_id)
    assert len(page2) == 2
    # 分页不重叠
    assert page2[0]["id"] < last_id


def test_tags_roundtrip(tmp_path: Path):
    db = tmp_path / "news.db"
    news_store.init_db(db)
    news_store.insert_telegraphs(db, [
        _mk_item("带标签", subjects=["半导体", "AI"], stocks=["贵州茅台"]),
    ])
    row = news_store.list_telegraphs(db)[0]
    assert set(row["subjects"]) == {"半导体", "AI"}
    assert row["stocks"] == ["贵州茅台"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_news_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.news_store'`

- [ ] **Step 3: 实现 news_store.py**

Create `backend/app/services/news_store.py`:

```python
"""市场快讯 SQLite 落库层 —— 建表 / 去重插入 / 倒序分页查询。

为什么用 SQLite 而非 parquet/DuckDB:
  快讯是「持续追加、按时间倒序读最新、需去重」的小记录流, 与行情大宽表特性不同。
  SQLite 是 Python 标准库 (零新增依赖), 天然支持唯一索引去重、倒序分页、标签关联。

DB 路径由调用方传入 (通常 data_dir / "news.db"), 便于测试用临时文件。
连接每次打开/关闭 (轮询低频, 无需连接池); WAL 模式降低读写锁争用。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:32]


def init_db(db_path: Path) -> None:
    """建表 + 索引 (幂等)。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegraph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                content TEXT NOT NULL,
                is_red INTEGER DEFAULT 0,
                url TEXT DEFAULT '',
                source TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_telegraph_dedup
                ON telegraph(source, content_hash);
            CREATE INDEX IF NOT EXISTS ix_telegraph_source_id
                ON telegraph(source, id);
            CREATE TABLE IF NOT EXISTS telegraph_tags (
                telegraph_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                tag_type TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_telegraph_tags_tid
                ON telegraph_tags(telegraph_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def insert_telegraphs(db_path: Path, items: list[dict]) -> int:
    """批量插入, 已存在 (source, content_hash) 跳过。返回新增条数。

    items 每项: {time, content, is_red, url, source, subjects: [], stocks: []}
    """
    if not items:
        return 0
    conn = _connect(db_path)
    inserted = 0
    try:
        now = datetime.now().isoformat(timespec="seconds")
        for it in items:
            content = str(it.get("content") or "").strip()
            if not content:
                continue
            source = str(it.get("source") or "").strip() or "未知"
            chash = _content_hash(content)
            cur = conn.execute(
                "INSERT OR IGNORE INTO telegraph "
                "(time, content, is_red, url, source, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(it.get("time") or ""),
                    content,
                    1 if it.get("is_red") else 0,
                    str(it.get("url") or ""),
                    source,
                    chash,
                    now,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
                tid = cur.lastrowid
                tag_rows = [
                    (tid, str(t), "subject")
                    for t in (it.get("subjects") or []) if str(t).strip()
                ] + [
                    (tid, str(t), "stock")
                    for t in (it.get("stocks") or []) if str(t).strip()
                ]
                if tag_rows:
                    conn.executemany(
                        "INSERT INTO telegraph_tags (telegraph_id, tag, tag_type) "
                        "VALUES (?, ?, ?)",
                        tag_rows,
                    )
        conn.commit()
    finally:
        conn.close()
    return inserted


def list_telegraphs(
    db_path: Path,
    source: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    """倒序分页查询, 带标签聚合。

    - source: 按来源过滤 (None = 全部)
    - before_id: 游标分页 (只取 id < before_id 的, 用于「加载更多」)
    """
    conn = _connect(db_path)
    try:
        where = []
        params: list = []
        if source:
            where.append("source = ?")
            params.append(source)
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT id, time, content, is_red, url, source, created_at "
            f"FROM telegraph{clause} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        result = [dict(r) for r in rows]
        if result:
            ids = [r["id"] for r in result]
            qmarks = ",".join("?" * len(ids))
            tag_rows = conn.execute(
                f"SELECT telegraph_id, tag, tag_type FROM telegraph_tags "
                f"WHERE telegraph_id IN ({qmarks})",
                ids,
            ).fetchall()
            by_id: dict[int, dict[str, list[str]]] = {
                i: {"subjects": [], "stocks": []} for i in ids
            }
            for tr in tag_rows:
                bucket = by_id[tr["telegraph_id"]]
                if tr["tag_type"] == "subject":
                    bucket["subjects"].append(tr["tag"])
                else:
                    bucket["stocks"].append(tr["tag"])
            for r in result:
                r["is_red"] = bool(r["is_red"])
                r["subjects"] = by_id[r["id"]]["subjects"]
                r["stocks"] = by_id[r["id"]]["stocks"]
        return result
    finally:
        conn.close()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_news_store.py -v`
Expected: 全部 PASS (6 个测试)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/news_store.py backend/tests/test_news_store.py
git commit -m "feat: news_store 快讯 SQLite 落库层 (去重/倒序分页/标签)"
```

---

## Task 3: news_source — 四类抓取适配器

**Files:**
- Create: `backend/app/services/news_source.py`
- Test: `backend/tests/test_news_source.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_news_source.py`:

```python
"""news_source 解析函数单测 —— 喂固定 HTML/JSON 样本, 不打真网。"""
from __future__ import annotations

from app.services import news_source

# 财联社电报页面结构简化样本 (对齐 go-stock 的 .telegraph-content-box 选择器)
_CLS_HTML = """
<html><body>
  <div class="telegraph-content-box">
    <div class="telegraph-content-box">
      <span>10:30:00</span>
      <span class="c-de0422">重磅利好: 某政策落地</span>
    </div>
    <div>
      <a class="label-item">半导体</a>
      <a class="label-item link-label-item" href="https://cls.cn/detail/1">详情</a>
    </div>
    <div class="telegraph-stock-plate-box">
      <a>贵州茅台</a>
    </div>
  </div>
  <div class="telegraph-content-box">
    <div class="telegraph-content-box">
      <span>10:31:00</span>
      <span>普通消息</span>
    </div>
  </div>
</body></html>
"""


def test_parse_telegraph_html():
    items = news_source.parse_telegraph_html(_CLS_HTML)
    assert len(items) == 2
    first = items[0]
    assert first["time"] == "10:30:00"
    assert first["content"] == "重磅利好: 某政策落地"
    assert first["is_red"] is True
    assert first["source"] == "财联社电报"
    assert first["url"] == "https://cls.cn/detail/1"
    assert "半导体" in first["subjects"]
    assert "贵州茅台" in first["stocks"]
    # 第二条无红头/标签
    assert items[1]["is_red"] is False
    assert items[1]["content"] == "普通消息"


def test_parse_telegraph_empty():
    assert news_source.parse_telegraph_html("") == []
    assert news_source.parse_telegraph_html("<html></html>") == []


def test_parse_report_list():
    # 东财 report/list2 返回 {"data": [...]} 结构
    raw = {
        "data": [
            {
                "title": "买入评级报告",
                "orgSName": "某某证券",
                "publishDate": "2026-07-01 00:00:00",
                "researcher": "张三",
                "emRatingName": "买入",
                "infoCode": "AP123",
            }
        ]
    }
    items = news_source.parse_report_list(raw)
    assert len(items) == 1
    assert items[0]["title"] == "买入评级报告"
    assert items[0]["org"] == "某某证券"
    assert items[0]["rating"] == "买入"
    assert items[0]["url"].startswith("https://data.eastmoney.com/report/")


def test_parse_report_list_empty():
    assert news_source.parse_report_list({}) == []
    assert news_source.parse_report_list({"data": None}) == []


def test_parse_notice_list():
    raw = {
        "data": {
            "list": [
                {
                    "title": "关于股东减持的公告",
                    "notice_date": "2026-07-02 16:00:00",
                    "art_code": "AN456",
                    "codes": [{"stock_code": "600519", "short_name": "贵州茅台"}],
                    "columns": [{"column_name": "股东股本"}],
                }
            ]
        }
    }
    items = news_source.parse_notice_list(raw)
    assert len(items) == 1
    assert items[0]["title"] == "关于股东减持的公告"
    assert items[0]["date"] == "2026-07-02 16:00:00"
    assert items[0]["url"].startswith("https://data.eastmoney.com/notices/detail/")


def test_parse_notice_list_empty():
    assert news_source.parse_notice_list({}) == []
    assert news_source.parse_notice_list({"data": {}}) == []


def test_em_code_strips_suffix():
    assert news_source._em_code("600519.SH") == "600519"
    assert news_source._em_code("300750.SZ") == "300750"
    assert news_source._em_code("600519") == "600519"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_news_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.news_source'`

- [ ] **Step 3: 实现 news_source.py**

Create `backend/app/services/news_source.py`:

```python
"""市场资讯抓取适配器 —— 财联社电报 / 东财研报 / 东财公告 (免费公开端点)。

设计对齐 free_adapter: 纯 httpx, UA/Referer 伪装, 失败静默返回空 (不抛异常, 不阻断主流程)。
解析逻辑抽成纯函数 (parse_*), 便于喂固定样本单测, 不打真网。

数据源:
  - 财联社电报: https://www.cls.cn/telegraph (HTML, selectolax 解析)
  - 个股研报:   https://reportapi.eastmoney.com/report/list2 (POST JSON)
  - 行业研报:   https://reportapi.eastmoney.com/report/list  (GET JSON)
  - 个股公告:   https://np-anotice-stock.eastmoney.com/api/security/ann (GET JSON)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 15.0

_SOURCE_CLS = "财联社电报"


def _em_code(code: str) -> str:
    """600519.SH → 600519; 裸代码原样返回。东财端点只认纯数字代码。"""
    s = (code or "").strip().upper()
    if "." in s:
        s = s.split(".")[0]
    for prefix in ("SH", "SZ", "BJ", "GB_", "US_", "US"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


# ================================================================
# 解析函数 (纯逻辑, 可单测)
# ================================================================

def parse_telegraph_html(html: str) -> list[dict]:
    """解析财联社电报页面。返回 [{time, content, is_red, url, source, subjects, stocks}]。"""
    if not html or not html.strip():
        return []
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    items: list[dict] = []
    for box in tree.css(".telegraph-content-box"):
        inner = box.css("div.telegraph-content-box span")
        # 只处理有 2 个 span 的内层结构 (时间 + 内容)
        if len(inner) != 2:
            continue
        time_txt = inner[0].text(strip=True)
        content = inner[1].text(strip=True)
        if not content:
            continue
        is_red = "c-de0422" in (inner[1].attributes.get("class") or "")

        url = ""
        subjects: list[str] = []
        for a in box.css("div a.label-item"):
            classes = a.attributes.get("class") or ""
            if "link-label-item" in classes:
                url = a.attributes.get("href") or ""
            else:
                txt = a.text(strip=True)
                if txt:
                    subjects.append(txt)

        stocks = [
            a.text(strip=True)
            for a in box.css("div.telegraph-stock-plate-box a")
            if a.text(strip=True)
        ]

        items.append({
            "time": time_txt,
            "content": content,
            "is_red": is_red,
            "url": url,
            "source": _SOURCE_CLS,
            "subjects": subjects,
            "stocks": stocks,
        })
    return items


def parse_report_list(raw: dict) -> list[dict]:
    """解析东财研报列表 (report/list, report/list2 同构)。

    返回 [{title, org, date, author, rating, url}]。
    """
    if not isinstance(raw, dict):
        return []
    data = raw.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        info_code = str(d.get("infoCode") or "")
        out.append({
            "title": str(d.get("title") or ""),
            "org": str(d.get("orgSName") or d.get("orgName") or ""),
            "date": str(d.get("publishDate") or "").split(" ")[0],
            "author": str(d.get("researcher") or ""),
            "rating": str(d.get("emRatingName") or d.get("sRatingName") or ""),
            "url": f"https://data.eastmoney.com/report/info/{info_code}.html" if info_code else "",
        })
    return out


def parse_notice_list(raw: dict) -> list[dict]:
    """解析东财公告列表 (security/ann)。

    返回 [{title, date, stocks, columns, url}]。
    """
    if not isinstance(raw, dict):
        return []
    data = raw.get("data")
    if not isinstance(data, dict):
        return []
    lst = data.get("list")
    if not isinstance(lst, list):
        return []
    out: list[dict] = []
    for d in lst:
        if not isinstance(d, dict):
            continue
        art_code = str(d.get("art_code") or "")
        stocks = [
            str(c.get("short_name") or c.get("stock_code") or "")
            for c in (d.get("codes") or []) if isinstance(c, dict)
        ]
        columns = [
            str(c.get("column_name") or "")
            for c in (d.get("columns") or []) if isinstance(c, dict)
        ]
        out.append({
            "title": str(d.get("title") or ""),
            "date": str(d.get("notice_date") or ""),
            "stocks": [s for s in stocks if s],
            "columns": [c for c in columns if c],
            "url": f"https://data.eastmoney.com/notices/detail/{art_code}.html" if art_code else "",
        })
    return out


# ================================================================
# 抓取函数 (打真网, 失败静默返回空)
# ================================================================

def fetch_telegraph(timeout: float = _TIMEOUT) -> list[dict]:
    """抓财联社电报。失败返回空列表。"""
    try:
        import httpx

        resp = httpx.get(
            "https://www.cls.cn/telegraph",
            headers={"User-Agent": _UA, "Referer": "https://www.cls.cn/"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return parse_telegraph_html(resp.text)
        logger.debug("fetch_telegraph HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch_telegraph 失败: %s", e)
    return []


def fetch_stock_report(code: str, days: int = 90, timeout: float = _TIMEOUT) -> list[dict]:
    """个股研报 (东财 report/list2, POST)。失败返回空。"""
    em = _em_code(code)
    if not em:
        return []
    begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    try:
        import httpx

        resp = httpx.post(
            "https://reportapi.eastmoney.com/report/list2",
            headers={
                "User-Agent": _UA,
                "Origin": "https://data.eastmoney.com",
                "Referer": "https://data.eastmoney.com/report/stock.jshtml",
                "Content-Type": "application/json",
            },
            json={
                "code": em, "industryCode": "*",
                "beginTime": begin, "endTime": end,
                "pageNo": 1, "pageSize": 50, "p": 1, "pageNum": 1, "pageNumber": 1,
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            return parse_report_list(resp.json())
        logger.debug("fetch_stock_report HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch_stock_report 失败: %s", e)
    return []


def fetch_industry_report(industry_code: str = "", days: int = 90,
                          timeout: float = _TIMEOUT) -> list[dict]:
    """行业研报 (东财 report/list, GET)。industry_code 空 = 全行业。失败返回空。"""
    begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    try:
        import httpx

        resp = httpx.get(
            "https://reportapi.eastmoney.com/report/list",
            headers={
                "User-Agent": _UA,
                "Origin": "https://data.eastmoney.com",
                "Referer": "https://data.eastmoney.com/report/industry.jshtml",
            },
            params={
                "industry": "*",
                "industryCode": (industry_code or "").strip() or "*",
                "beginTime": begin, "endTime": end,
                "pageNo": "1", "pageSize": "50", "p": "1",
                "pageNum": "1", "pageNumber": "1", "qType": "1",
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            return parse_report_list(resp.json())
        logger.debug("fetch_industry_report HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch_industry_report 失败: %s", e)
    return []


def fetch_stock_notice(codes: str, timeout: float = _TIMEOUT) -> list[dict]:
    """个股公告 (东财 security/ann, GET)。codes 支持逗号分隔多个。失败返回空。"""
    parts = [_em_code(c) for c in (codes or "").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return []
    try:
        import httpx

        resp = httpx.get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            headers={
                "User-Agent": _UA,
                "Referer": "https://data.eastmoney.com/notices/hsa/5.html",
            },
            params={
                "page_size": "50", "page_index": "1",
                "ann_type": "SHA,CYB,SZA,BJA,INV",
                "client_source": "web", "f_node": "0",
                "stock_list": ",".join(parts),
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            return parse_notice_list(resp.json())
        logger.debug("fetch_stock_notice HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch_stock_notice 失败: %s", e)
    return []
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_news_source.py -v`
Expected: 全部 PASS (8 个测试)

注意: 若 `test_parse_telegraph_html` 因 selectolax 对嵌套 `.telegraph-content-box` 的选择行为失败, 检查外层遍历是否误匹配内层 —— 外层 box 是含 `div.telegraph-content-box` 子节点的容器。样本已按 go-stock 的真实结构(外层 box 内嵌一个同名 inner div 装 span)构造。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/news_source.py backend/tests/test_news_source.py
git commit -m "feat: news_source 四类资讯抓取适配器 (财联社/东财研报/公告)"
```

---

## Task 4: preferences — 快讯轮询开关/间隔

**Files:**
- Modify: `backend/app/services/preferences.py`

- [ ] **Step 1: 加 getter/setter**

在 `backend/app/services/preferences.py` 的 Telegram 段之后(`set_telegram_allowed_chat_ids` 函数结束处)加入:

```python
# ===== 市场快讯轮询 =====
# 独立 daemon 线程持续抓财联社电报入库。默认关闭 (需用户显式开启)。

_NEWS_POLL_MIN_INTERVAL = 60.0
_NEWS_POLL_MAX_INTERVAL = 3600.0


def get_news_poll_enabled() -> bool:
    """快讯轮询总开关。默认关闭。"""
    return bool(load().get("news_poll_enabled", False))


def set_news_poll_enabled(enabled: bool) -> bool:
    save({"news_poll_enabled": bool(enabled)})
    return bool(enabled)


def get_news_poll_interval() -> float:
    """快讯抓取间隔 (秒)。默认 300, 夹在 [60, 3600]。"""
    raw = load().get("news_poll_interval", 300.0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 300.0
    return max(_NEWS_POLL_MIN_INTERVAL, min(v, _NEWS_POLL_MAX_INTERVAL))


def set_news_poll_interval(interval: float) -> float:
    v = max(_NEWS_POLL_MIN_INTERVAL, min(float(interval), _NEWS_POLL_MAX_INTERVAL))
    save({"news_poll_interval": v})
    return v
```

- [ ] **Step 2: 验证导入**

Run: `cd backend && uv run python -c "from app.services import preferences; print(preferences.get_news_poll_enabled(), preferences.get_news_poll_interval())"`
Expected: 输出 `False 300.0`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/preferences.py
git commit -m "feat: preferences 加快讯轮询开关/间隔"
```

---

## Task 5: news_poller — 独立轮询线程

**Files:**
- Create: `backend/app/services/news_poller.py`

- [ ] **Step 1: 实现 news_poller.py**

Create `backend/app/services/news_poller.py`:

```python
"""市场快讯轮询服务 —— 独立 daemon 线程持续抓财联社电报入库。

线程模型对齐 telegram_bot / quote_service: daemon 线程 + 自持 event loop,
start()/stop()/restart() 由 lifespan 与设置页调用。

配置热读 (不缓存, 便于设置页改动即时生效):
  - enabled:  preferences.get_news_poll_enabled()  (默认关)
  - interval: preferences.get_news_poll_interval()  (默认 300s)

设计: 单轮抓取失败静默退避, 循环永不崩; 未启用时 start() 内部跳过不影响主启动。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class NewsPollerService:
    """快讯轮询服务 (单例, 挂 app.state)。"""

    def __init__(self) -> None:
        self._db_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._lock = threading.Lock()

    def set_db_path(self, db_path: Path) -> None:
        self._db_path = db_path

    # ── 生命周期 ─────────────────────────────────────────

    def start(self) -> bool:
        """按配置启动轮询。未启用时跳过, 返回是否真正启动。"""
        from app.services import news_store, preferences

        if not preferences.get_news_poll_enabled():
            logger.info("news poller: 未启用, 跳过")
            return False
        if self._db_path is None:
            logger.warning("news poller: db_path 未设置, 跳过")
            return False

        with self._lock:
            if self._running:
                return True
            self._running = True
            news_store.init_db(self._db_path)
            self._thread = threading.Thread(
                target=self._thread_main, name="news-poller", daemon=True,
            )
            self._thread.start()
        logger.info("news poller: 已启动")
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=20)
        self._thread = None
        logger.info("news poller: 已停止")

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def is_running(self) -> bool:
        return self._running

    # ── 线程主体 ─────────────────────────────────────────

    def _thread_main(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:  # noqa: BLE001
            logger.warning("news poller 线程异常退出: %s", e)
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    async def _poll_loop(self) -> None:
        from app.services import news_source, news_store, preferences

        backoff = 5.0
        while self._running:
            try:
                items = await asyncio.to_thread(news_source.fetch_telegraph)
                if items and self._db_path is not None:
                    n = await asyncio.to_thread(
                        news_store.insert_telegraphs, self._db_path, items,
                    )
                    if n:
                        logger.info("news poller: 新增 %d 条快讯", n)
                backoff = 5.0
            except Exception as e:  # noqa: BLE001
                logger.debug("news poller 单轮失败: %s", e)
                backoff = min(backoff * 1.5, 60.0)

            interval = preferences.get_news_poll_interval()
            # 分片 sleep, 便于 stop 时快速响应
            slept = 0.0
            step = 2.0
            target = max(interval, backoff)
            while self._running and slept < target:
                await asyncio.sleep(step)
                slept += step
```

- [ ] **Step 2: 验证导入**

Run: `cd backend && uv run python -c "from app.services.news_poller import NewsPollerService; s = NewsPollerService(); print(s.is_running())"`
Expected: 输出 `False`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/news_poller.py
git commit -m "feat: news_poller 快讯轮询 daemon 线程"
```

---

## Task 6: api/news.py — REST 路由

**Files:**
- Create: `backend/app/api/news.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: 实现 news.py**

Create `backend/app/api/news.py`:

```python
"""市场资讯 API —— 快讯 (读库) + 研报/公告 (实时拉)。

- 快讯: 由 news_poller 后台落库, 此处只读 news.db。
- 研报/公告: 一次性查询, 直接调 news_source 实时拉, 不落库。
所有数据源为免费公开端点, 不依赖 CapabilitySet。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request

from app.services import news_source, news_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


def _db_path(request: Request) -> Path | None:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        return None
    return repo.store.data_dir / "news.db"


@router.get("/telegraph")
def get_telegraph(
    request: Request,
    source: str = Query("", description="来源过滤, 空=全部"),
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, description="游标分页: 取 id 小于此值的记录"),
) -> dict:
    """快讯列表 (读 news.db, 倒序分页)。"""
    db = _db_path(request)
    if db is None or not db.exists():
        return {"items": [], "count": 0}
    items = news_store.list_telegraphs(
        db, source=source or None, limit=limit, before_id=before_id,
    )
    return {"items": items, "count": len(items)}


@router.get("/report/{code}")
def get_stock_report(
    code: str,
    days: int = Query(90, ge=7, le=365),
) -> dict:
    """个股研报 (实时拉)。"""
    return {"items": news_source.fetch_stock_report(code, days)}


@router.get("/industry-report")
def get_industry_report(
    industry: str = Query("", description="行业代码, 空=全行业"),
    days: int = Query(90, ge=7, le=365),
) -> dict:
    """行业研报 (实时拉)。"""
    return {"items": news_source.fetch_industry_report(industry, days)}


@router.get("/notice/{code}")
def get_stock_notice(code: str) -> dict:
    """个股公告 (实时拉, code 支持逗号分隔多个)。"""
    return {"items": news_source.fetch_stock_notice(code)}
```

- [ ] **Step 2: 在 main.py 装配 poller + include_router**

在 `backend/app/main.py` 的 lifespan 中, telegram bot 装配块之后(`app.state.telegram_bot = None` 那个 except 块结束后)加入:

```python
    # 市场快讯轮询: 独立线程持续抓财联社电报入库 (未启用则跳过)。
    try:
        from app.services.news_poller import NewsPollerService
        news_poller = NewsPollerService()
        news_poller.set_db_path(repo.store.data_dir / "news.db")
        news_poller.start()
        app.state.news_poller = news_poller
    except Exception as e:  # noqa: BLE001
        logger.warning("news poller not started: %s", e)
        app.state.news_poller = None
```

在 shutdown 段, telegram bot 的 stop 之后(`tbot.stop()` 之后)加入:

```python
    npoll = getattr(app.state, "news_poller", None)
    if npoll:
        npoll.stop()
```

在文件顶部的 router import 段加入 `news`(与其他 api 模块 import 一致),并在 include_router 段(`app.include_router(rps.router)` 之后)加入:

```python
app.include_router(news.router)
```

具体 import: 找到形如 `from app.api import (...)` 或逐个 import 的位置,按同款风格加 `news`。若是 `from app.api import alerts, analysis, ...` 形式,把 `news` 加进去;若是逐行 `from app.api import rps`,则加 `from app.api import news`。

- [ ] **Step 3: 启动后端冒烟测试**

Run: `cd backend && uv run python -c "from app.main import app; print([r.path for r in app.routes if '/api/news' in getattr(r, 'path', '')])"`
Expected: 输出包含 `/api/news/telegraph`、`/api/news/report/{code}`、`/api/news/industry-report`、`/api/news/notice/{code}` 四条路由。

- [ ] **Step 4: lint + 类型检查**

Run: `cd backend && uv run ruff check app/api/news.py app/services/news_source.py app/services/news_store.py app/services/news_poller.py`
Expected: 无错误(或仅无关告警)

Run: `cd backend && uv run mypy app/api/news.py`
Expected: 无致命类型错误

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/news.py backend/app/main.py
git commit -m "feat: news REST API + lifespan 装配快讯轮询"
```

---

## Task 7: Telegram 命令

**Files:**
- Modify: `backend/app/services/telegram_commands.py`

- [ ] **Step 1: 在 telegram_commands.py 末尾注册 4 个命令**

在 `backend/app/services/telegram_commands.py` 文件末尾(最后一个 `@command` 处理器之后)加入:

```python
# ================================================================
# 市场资讯命令 (快讯 / 研报 / 公告)
# ================================================================

@command("news", "最近市场快讯 (财联社电报)", usage="[条数]",
         args_hint="可选条数, 如 10; 留空默认 10 条")
async def _cmd_news(ctx: CommandContext, args: str) -> str:
    from app.services import news_store

    limit = 10
    a = (args or "").strip()
    if a.isdigit():
        limit = max(1, min(int(a), 30))
    repo = ctx.repo
    if repo is None:
        return "数据尚未就绪。"
    db = repo.store.data_dir / "news.db"
    if not db.exists():
        return "暂无快讯 (轮询未开启或尚未抓到)。可在「设置」开启快讯轮询。"
    rows = news_store.list_telegraphs(db, limit=limit)
    if not rows:
        return "暂无快讯。"
    lines = ["<b>📰 市场快讯</b>"]
    for r in rows:
        flag = "🔴 " if r.get("is_red") else ""
        t = r.get("time") or ""
        content = str(r.get("content") or "")
        lines.append(f"\n{flag}<b>{t}</b>  {content}")
        subs = r.get("subjects") or []
        if subs:
            lines.append(f"  🏷 {' · '.join(subs)}")
    return "\n".join(lines)


@command("report", "个股研报 (最近 90 天)", usage="<代码>",
         args_hint="股票代码或名字, 如 600519")
async def _cmd_report(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    code = normalize_symbol((args or "").strip())
    if not code:
        return "用法: /report &lt;代码&gt;, 如 /report 600519"
    items = news_source.fetch_stock_report(code)
    if not items:
        return f"未查到 {code} 的研报 (或近 90 天无)。"
    lines = [f"<b>📑 {code} 个股研报</b>"]
    for it in items[:10]:
        rating = f"[{it['rating']}] " if it.get("rating") else ""
        lines.append(f"\n{rating}<b>{it.get('title', '')}</b>")
        meta = " · ".join(x for x in [it.get("org", ""), it.get("date", "")] if x)
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


@command("ireport", "行业研报 (最近 90 天)", usage="[行业代码]",
         args_hint="行业代码, 留空=全行业")
async def _cmd_ireport(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    industry = (args or "").strip()
    items = news_source.fetch_industry_report(industry)
    if not items:
        return "未查到行业研报 (或近 90 天无)。"
    lines = ["<b>📊 行业研报</b>"]
    for it in items[:10]:
        lines.append(f"\n<b>{it.get('title', '')}</b>")
        meta = " · ".join(x for x in [it.get("org", ""), it.get("date", "")] if x)
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


@command("notice", "个股公告", usage="<代码>",
         args_hint="股票代码或名字, 如 600519")
async def _cmd_notice(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    code = normalize_symbol((args or "").strip())
    if not code:
        return "用法: /notice &lt;代码&gt;, 如 /notice 600519"
    items = news_source.fetch_stock_notice(code)
    if not items:
        return f"未查到 {code} 的公告。"
    lines = [f"<b>📢 {code} 公告</b>"]
    for it in items[:10]:
        cols = f"[{' '.join(it['columns'])}] " if it.get("columns") else ""
        lines.append(f"\n{cols}<b>{it.get('title', '')}</b>")
        if it.get("date"):
            lines.append(f"  {it['date']}")
    return "\n".join(lines)
```

- [ ] **Step 2: 验证命令注册**

Run: `cd backend && uv run python -c "from app.services import telegram_commands; print([k for k in ['news','report','ireport','notice'] if k in telegram_commands.COMMANDS])"`
Expected: 输出 `['news', 'report', 'ireport', 'notice']`

- [ ] **Step 3: lint**

Run: `cd backend && uv run ruff check app/services/telegram_commands.py`
Expected: 无错误

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/telegram_commands.py
git commit -m "feat: Telegram 加 /news /report /ireport /notice 四个资讯命令"
```

---

## Task 8: 前端 API 层

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/queryKeys.ts`

- [ ] **Step 1: 在 api.ts 加类型 (接口定义段, 靠近其他 interface)**

在 `frontend/src/lib/api.ts` 的接口定义区(如 `LimitLadderTier` 之后)加入:

```typescript
// ===== 市场资讯 (快讯 / 研报 / 公告) =====
export interface Telegraph {
  id: number
  time: string
  content: string
  is_red: boolean
  url: string
  source: string
  created_at: string
  subjects: string[]
  stocks: string[]
}

export interface ResearchReport {
  title: string
  org: string
  date: string
  author: string
  rating: string
  url: string
}

export interface StockNotice {
  title: string
  date: string
  stocks: string[]
  columns: string[]
  url: string
}
```

- [ ] **Step 2: 在 api 对象加 fetch 函数**

在 `frontend/src/lib/api.ts` 的 `export const api = {` 对象内(任意现有条目之后)加入:

```typescript
  newsTelegraph: (source = '', limit = 50, beforeId?: number) => {
    const p = new URLSearchParams()
    if (source) p.set('source', source)
    p.set('limit', String(limit))
    if (beforeId != null) p.set('before_id', String(beforeId))
    return request<{ items: Telegraph[]; count: number }>(`/api/news/telegraph?${p.toString()}`)
  },
  newsStockReport: (code: string, days = 90) =>
    request<{ items: ResearchReport[] }>(`/api/news/report/${encodeURIComponent(code)}?days=${days}`),
  newsIndustryReport: (industry = '', days = 90) => {
    const p = new URLSearchParams()
    if (industry) p.set('industry', industry)
    p.set('days', String(days))
    return request<{ items: ResearchReport[] }>(`/api/news/industry-report?${p.toString()}`)
  },
  newsStockNotice: (code: string) =>
    request<{ items: StockNotice[] }>(`/api/news/notice/${encodeURIComponent(code)}`),
```

- [ ] **Step 3: 在 queryKeys.ts 加 key**

在 `frontend/src/lib/queryKeys.ts` 的 `QK` 对象内(任意位置)加入:

```typescript
  // 市场资讯
  newsTelegraph:      (source: string) => ['news-telegraph', source] as const,
  newsStockReport:    (code: string) => ['news-stock-report', code] as const,
  newsIndustryReport: (industry: string) => ['news-industry-report', industry] as const,
  newsStockNotice:    (code: string) => ['news-stock-notice', code] as const,
```

- [ ] **Step 4: 类型检查**

Run: `cd frontend && pnpm exec tsc -b --noEmit`
Expected: 无类型错误(或仅与本改动无关的既有告警)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/queryKeys.ts
git commit -m "feat: 前端 news API 类型 + fetch 函数 + query key"
```

---

## Task 9: 前端 News 页面

**Files:**
- Create: `frontend/src/pages/News.tsx`

- [ ] **Step 1: 实现 News.tsx**

Create `frontend/src/pages/News.tsx`:

```tsx
/**
 * 市场资讯页 —— Tab 切换四类: 快讯 / 个股研报 / 行业研报 / 公告。
 *  - 快讯: GET /api/news/telegraph, 定时刷新 (后台轮询落库)。
 *  - 个股研报/公告: 输入代码实时查询。
 *  - 行业研报: 可选行业代码, 默认全行业。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Newspaper, FileSearch, Landmark, Megaphone, RefreshCw, ExternalLink } from 'lucide-react'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'

type Tab = 'telegraph' | 'report' | 'industry' | 'notice'

const TABS: { key: Tab; label: string; icon: typeof Newspaper }[] = [
  { key: 'telegraph', label: '快讯', icon: Newspaper },
  { key: 'report', label: '个股研报', icon: FileSearch },
  { key: 'industry', label: '行业研报', icon: Landmark },
  { key: 'notice', label: '公告', icon: Megaphone },
]

export function News() {
  const [tab, setTab] = useState<Tab>('telegraph')

  return (
    <div className="p-6 space-y-4">
      <PageHeader icon={Newspaper} title="市场资讯" subtitle="快讯 · 研报 · 公告" />

      <div className="flex gap-2 border-b border-border">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={cn(
              'flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 -mb-px transition-colors',
              tab === key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted hover:text-foreground',
            )}
          >
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'telegraph' && <TelegraphTab />}
      {tab === 'report' && <StockReportTab />}
      {tab === 'industry' && <IndustryReportTab />}
      {tab === 'notice' && <NoticeTab />}
    </div>
  )
}

function TelegraphTab() {
  const { data, isFetching, refetch } = useQuery({
    queryKey: QK.newsTelegraph(''),
    queryFn: () => api.newsTelegraph('', 50),
    refetchInterval: 60_000,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <button onClick={() => refetch()} className="flex items-center gap-1 text-sm text-muted hover:text-foreground">
          <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} /> 刷新
        </button>
      </div>
      {items.length === 0 && (
        <div className="text-muted text-sm py-8 text-center">
          暂无快讯。请在「设置」开启快讯轮询后等待抓取。
        </div>
      )}
      {items.map((it) => (
        <div key={it.id} className="rounded-lg border border-border p-3 space-y-1">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className={it.is_red ? 'text-bear font-semibold' : ''}>{it.time}</span>
            <span>·</span>
            <span>{it.source}</span>
            {it.url && (
              <a href={it.url} target="_blank" rel="noreferrer" className="ml-auto hover:text-primary">
                <ExternalLink size={13} />
              </a>
            )}
          </div>
          <div className={cn('text-sm leading-relaxed', it.is_red && 'text-bear')}>{it.content}</div>
          {it.subjects.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {it.subjects.map((s) => (
                <span key={s} className="text-xs px-1.5 py-0.5 rounded bg-muted/10 text-muted">
                  {s}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function StockReportTab() {
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsStockReport(query),
    queryFn: () => api.newsStockReport(query),
    enabled: query.length > 0,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={code}
        onChange={setCode}
        onSubmit={() => setQuery(code.trim())}
        placeholder="输入股票代码, 如 600519"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {query && !isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到研报。</div>
      )}
      {items.map((it, i) => (
        <ReportCard key={i} report={it} />
      ))}
    </div>
  )
}

function IndustryReportTab() {
  const [industry, setIndustry] = useState('')
  const [query, setQuery] = useState('__all__')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsIndustryReport(query),
    queryFn: () => api.newsIndustryReport(query === '__all__' ? '' : query),
    enabled: true,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={industry}
        onChange={setIndustry}
        onSubmit={() => setQuery(industry.trim() || '__all__')}
        placeholder="行业代码 (留空=全行业)"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {!isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到行业研报。</div>
      )}
      {items.map((it, i) => (
        <ReportCard key={i} report={it} />
      ))}
    </div>
  )
}

function NoticeTab() {
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsStockNotice(query),
    queryFn: () => api.newsStockNotice(query),
    enabled: query.length > 0,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={code}
        onChange={setCode}
        onSubmit={() => setQuery(code.trim())}
        placeholder="输入股票代码, 如 600519"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {query && !isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到公告。</div>
      )}
      {items.map((it, i) => (
        <div key={i} className="rounded-lg border border-border p-3 space-y-1">
          <div className="flex items-center gap-2">
            {it.columns.length > 0 && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-muted/10 text-muted">
                {it.columns.join(' ')}
              </span>
            )}
            <span className="text-sm font-medium flex-1">{it.title}</span>
            {it.url && (
              <a href={it.url} target="_blank" rel="noreferrer" className="hover:text-primary">
                <ExternalLink size={13} />
              </a>
            )}
          </div>
          <div className="text-xs text-muted">{it.date}{it.stocks.length > 0 ? ` · ${it.stocks.join(', ')}` : ''}</div>
        </div>
      ))}
    </div>
  )
}

function ReportCard({ report }: { report: import('@/lib/api').ResearchReport }) {
  return (
    <div className="rounded-lg border border-border p-3 space-y-1">
      <div className="flex items-center gap-2">
        {report.rating && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-bear/10 text-bear">{report.rating}</span>
        )}
        <span className="text-sm font-medium flex-1">{report.title}</span>
        {report.url && (
          <a href={report.url} target="_blank" rel="noreferrer" className="hover:text-primary">
            <ExternalLink size={13} />
          </a>
        )}
      </div>
      <div className="text-xs text-muted">
        {[report.org, report.author, report.date].filter(Boolean).join(' · ')}
      </div>
    </div>
  )
}

function SearchBar({
  value, onChange, onSubmit, placeholder,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  placeholder: string
}) {
  return (
    <div className="flex gap-2">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && onSubmit()}
        placeholder={placeholder}
        className="flex-1 rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary"
      />
      <button
        onClick={onSubmit}
        className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground hover:opacity-90"
      >
        查询
      </button>
    </div>
  )
}
```

注意: `PageHeader` 的 props 以实际组件为准。若 `PageHeader` 不接受 `icon`/`subtitle`,先 Read `frontend/src/components/PageHeader.tsx` 对齐其签名(参考 Review.tsx 的用法)。`text-bear`/`text-primary`/`border-border`/`text-muted` 等 class 沿用项目 Tailwind 主题;若某个 class 不存在,参考同目录现有页面替换为等价 class。

- [ ] **Step 2: 类型检查**

Run: `cd frontend && pnpm exec tsc -b --noEmit`
Expected: 无类型错误

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/News.tsx
git commit -m "feat: 市场资讯页 (快讯/个股研报/行业研报/公告 Tab)"
```

---

## Task 10: 前端路由 + 导航

**Files:**
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/components/Layout.tsx`

- [ ] **Step 1: router.tsx 加路由**

在 `frontend/src/router.tsx` 顶部 import 段加入:

```typescript
import { News } from './pages/News'
```

在子路由数组中(如 `{ path: 'review', element: <Review /> },` 之后)加入:

```typescript
      { path: 'news', element: <News /> },
```

- [ ] **Step 2: Layout.tsx 加导航项 + icon import**

在 `frontend/src/components/Layout.tsx` 的 lucide-react import 中加入 `Newspaper`(找到现有的 `import { ... } from 'lucide-react'` 或逐个图标 import,按同款加入)。

在导航项数组(`navItems` / 形如 `{ to: '/review', label: '复盘', icon: BookOpenCheck },` 那组)中,复盘项之后加入:

```typescript
  { to: '/news',       label: '资讯',   icon: Newspaper },
```

- [ ] **Step 3: 类型检查 + build**

Run: `cd frontend && pnpm exec tsc -b --noEmit`
Expected: 无类型错误

Run: `cd frontend && pnpm build`
Expected: build 成功

- [ ] **Step 4: Commit**

```bash
git add frontend/src/router.tsx frontend/src/components/Layout.tsx
git commit -m "feat: 资讯页接入路由 + 侧栏导航"
```

---

## Task 11: 设置页 — 快讯轮询开关 (可选增强)

**Files:**
- Modify: `backend/app/api/settings.py`
- Modify: `frontend/src/lib/api.ts`

> 若时间紧张可跳过: poller 默认关, 用户可临时靠改 preferences.json + 重启开启。此任务提供 UI 开关闭环。

- [ ] **Step 1: 后端加设置端点**

在 `backend/app/api/settings.py` 的 Telegram 段之后加入:

```python
class NewsPollPrefsIn(BaseModel):
    enabled: bool | None = None
    interval: float | None = None


@router.put("/preferences/news-poll")
def update_news_poll(req: NewsPollPrefsIn, request: Request) -> dict:
    """快讯轮询开关/间隔, 改动后重启轮询线程使即时生效。"""
    from app.services import preferences

    if req.enabled is not None:
        preferences.set_news_poll_enabled(req.enabled)
    if req.interval is not None:
        preferences.set_news_poll_interval(req.interval)

    poller = getattr(request.app.state, "news_poller", None)
    running = False
    if poller is not None:
        try:
            running = poller.restart()
        except Exception as e:  # noqa: BLE001
            logger.warning("news poller restart failed: %s", e)
    return {
        "news_poll_enabled": preferences.get_news_poll_enabled(),
        "news_poll_interval": preferences.get_news_poll_interval(),
        "news_poll_running": running,
    }
```

同时在 `get_settings()` 返回的 dict 中加入(与 telegram 字段并列):

```python
        "news_poll_enabled": preferences.get_news_poll_enabled(),
        "news_poll_interval": preferences.get_news_poll_interval(),
```

- [ ] **Step 2: 前端 api.ts 加函数**

在 `frontend/src/lib/api.ts` 的 `api` 对象内加入:

```typescript
  updateNewsPoll: (enabled?: boolean, interval?: number) =>
    request<{ news_poll_enabled: boolean; news_poll_interval: number; news_poll_running: boolean }>(
      '/api/settings/preferences/news-poll',
      { method: 'PUT', body: JSON.stringify({ enabled, interval }) },
    ),
```

- [ ] **Step 3: 冒烟 + 类型检查**

Run: `cd backend && uv run python -c "from app.main import app; print([r.path for r in app.routes if 'news-poll' in getattr(r, 'path', '')])"`
Expected: 输出 `['/api/settings/preferences/news-poll']`

Run: `cd frontend && pnpm exec tsc -b --noEmit`
Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/settings.py frontend/src/lib/api.ts
git commit -m "feat: 设置页快讯轮询开关端点"
```

---

## Task 12: 全量验证

- [ ] **Step 1: 后端全测 + lint + 类型**

Run: `cd backend && uv run pytest tests/test_news_store.py tests/test_news_source.py -v`
Expected: 全部 PASS

Run: `cd backend && uv run ruff check app`
Expected: 无错误

Run: `cd backend && uv run mypy app`
Expected: 无新增致命错误

- [ ] **Step 2: 前端 build + lint**

Run: `cd frontend && pnpm build && pnpm lint`
Expected: build 成功, lint 无错误

- [ ] **Step 3: 端到端冒烟 (手动, 可选)**

启动 `./dev.sh`, 打开前端 `/news`:
- 快讯 Tab: 若未开轮询显示空态提示;临时在 `data/user_data/preferences.json` 设 `"news_poll_enabled": true` 重启后应逐步抓到快讯。
- 个股研报 Tab: 输入 `600519` 查询, 应返回研报列表(依赖外网可达东财)。
- 公告 Tab: 输入 `600519` 应返回公告。

Telegram(若已配置 bot): 发 `/news`、`/report 600519`、`/notice 600519` 验证回复。

- [ ] **Step 4: 最终提交(若有零散改动)**

```bash
git add -A
git commit -m "chore: 市场资讯功能收尾验证"
```

---

## Self-Review 覆盖检查

- 快讯落库 → Task 2 (news_store) + Task 5 (poller) ✓
- 个股/行业研报 + 公告实时拉 → Task 3 (news_source) + Task 6 (api) ✓
- 4 个 Telegram 命令 → Task 7 ✓
- 前端网页 (Tab 四类) → Task 8/9/10 ✓
- selectolax 依赖 → Task 1 ✓
- 轮询开关/间隔 → Task 4 + Task 11 ✓
- 测试 (落库/去重/分页 + 解析) → Task 2/3 ✓
- 类型一致性: `Telegraph`/`ResearchReport`/`StockNotice` 在 api.ts 定义, News.tsx 消费; news_source 的 parse_* 返回字段与前端类型字段对齐 ✓
