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
