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
