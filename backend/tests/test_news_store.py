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
