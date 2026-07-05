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
