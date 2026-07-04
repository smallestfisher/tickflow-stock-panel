"""免费公开数据源 adapter —— 鸭子类型对齐 TickFlow SDK。

FreeSourceClient 的属性结构(.klines/.quotes/.depth/.financials/.exchanges/.universes)
与方法签名与 TickFlow SDK 一致,内部翻译成东方财富/新浪/腾讯的公开 HTTP 接口。
client.py 在 data_backend=="free_source" 时返回本对象,十几个 service 零改动。

返回结构对齐 SDK:
  - as_dataframe=False → dict / list[dict]
  - as_dataframe=True  → pandas DataFrame(消费方经 pl.from_pandas 取用)
"""
from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"
EM_REFERER = "https://quote.eastmoney.com/"
SINA_REFERER = "https://finance.sina.com.cn"
SINA_KLINE_REFERER = "https://finance.sina.com.cn"
TIMEOUT = 10.0

# 新浪 K 线接口的 scale 映射(period → 分钟数)。
# 注意:scale=1(1 分钟)新浪实测返回 null,故 1m 也走 scale=5;真 1m 当日分时由 intraday() 走腾讯。
SINA_SCALE = {"1d": 240, "1m": 5, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

# 东财 push2his 日K/分钟K 会话级熔断阈值:某些网络环境对 push2his 稳定 RST,
# 连续失败达此次数后本进程内标记东财不可用、剩余标的直接走新浪,避免每只白重试。
_EM_KLINE_TRIP_THRESHOLD = 3

# 交易所后缀 → 东财 secid 市场前缀
_EXCHANGE_TO_EM_MARKET = {"SH": "1", "SZ": "0", "BJ": "0"}
_EM_MARKET_TO_EXCHANGE = {"1": "SH", "0": "SZ"}  # BJ 与 SZ 同为 0,反查时按代码段区分


def _split_symbol(symbol: str) -> tuple[str, str]:
    """600000.SH → ("600000", "SH")。"""
    code, _, exch = symbol.rpartition(".")
    if not exch:
        raise ValueError(f"bad symbol: {symbol}")
    return code, exch


def _symbol_to_secid(symbol: str) -> str:
    code, exch = _split_symbol(symbol)
    market = _EXCHANGE_TO_EM_MARKET.get(exch.upper())
    if market is None:
        raise ValueError(f"unsupported exchange: {symbol}")
    return f"{market}.{code}"


def _secid_to_symbol(secid: str) -> str:
    market, _, code = secid.partition(".")
    if code.startswith(("430", "830", "920")):
        # 北交所代码段(430/830/920 开头),市场前缀同为 0
        return f"{code}.BJ"
    exch = _EM_MARKET_TO_EXCHANGE.get(market, "SZ")
    return f"{code}.{exch}"


def _sina_symbol(symbol: str) -> str:
    """600000.SH → sh600000。"""
    code, exch = _split_symbol(symbol)
    return f"{exch.lower()}{code}"


def _sina_klines(http: httpx.Client, symbol: str, period: str, count: int) -> list[dict]:
    """新浪日K/分钟K(免费源主路径,实测稳定)。

    返回对齐东财 _parse_em_klines 的 8 列结构(symbol/date/open/high/low/close/volume/amount)。
    新浪不直接给 amount,用 0 占位(下游未强依赖);date 用新浪返回的 day 字段(日K为日期、分钟K为 datetime)。
    注意:scale=1(1分钟)新浪返回 null,故 1m 已在 SINA_SCALE 里映射到 5。
    """
    scale = SINA_SCALE.get(period, 240)
    # count 转成 datalen:日K每根=1天,分钟K每根=1根。新浪按 datalen 返回最近 N 根。
    datalen = max(int(count or 250), 1)
    sina = _sina_symbol(symbol)
    try:
        r = _http_get(
            http,
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            referer=SINA_KLINE_REFERER,
            symbol=sina, scale=scale, ma="no", datalen=datalen,
        )
        items = r.json()
    except Exception as e:
        logger.warning("sina klines %s (%s) failed: %s", symbol, period, e)
        return []
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    for it in items:
        day = it.get("day")
        if not day:
            continue
        try:
            rows.append({
                "symbol": symbol,
                "date": day,
                "open": float(it["open"]),
                "high": float(it["high"]),
                "low": float(it["low"]),
                "close": float(it["close"]),
                "volume": float(it.get("volume") or 0),
                "amount": 0.0,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def _sina_symbols_param(symbols: list[str]) -> str:
    return ",".join(_sina_symbol(s) for s in symbols)


def _f(d, key, default=None):
    """安全取值并转 float;东财 '-' 表示无数据。支持 dict 按键 / list 按下标。"""
    if isinstance(d, dict):
        v = d.get(key)
    elif isinstance(d, (list, tuple)) and isinstance(key, int) and 0 <= key < len(d):
        v = d[key]
    else:
        return default
    if v in (None, "-", ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _sina_timestamp(parts, date_idx, time_idx) -> int | None:
    """新浪 date(2026-07-03)+time(10:13:29) → ms epoch。无则 None。"""
    try:
        from datetime import datetime
        d = parts[date_idx]
        t = parts[time_idx]
        dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _is_transient(e: Exception) -> bool:
    """与 policy._is_transient 同语义:5xx/429/超时/连接为瞬时。

    httpx.HTTPStatusError 的状态码在 e.response.status_code(它本身无 status_code 属性),
    直接读 e 上的 status_code 恒为 None —— 这会让 _http_get 主动抛出的 5xx 被判成
    "非瞬时"而不重试。故这里同时兼容 response.status_code 与直接属性两种来源。
    """
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(e, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return isinstance(e, (httpx.TimeoutException, httpx.TransportError))


def _http_get(http: httpx.Client, url: str, *, referer: str = EM_REFERER, **params) -> Any:
    """带 UA/Referer/重试的 GET。瞬时错误退避 2 次。"""
    last: Exception | None = None
    for i in range(3):
        try:
            r = http.get(url, params=params, headers={"User-Agent": UA, "Referer": referer},
                         timeout=TIMEOUT)
            # 5xx 视为瞬时
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            return r
        except Exception as e:
            last = e
            if not _is_transient(e):
                raise
            if i < 2:
                time.sleep(0.6 * (i + 1))
    assert last is not None
    raise last


# ---- 共享源访问(URL/referer/翻页/解码集中一处,各 sub-object 只声明用哪个源)----

def _resolve_exchange(code: str, mkt: str) -> str:
    """clist 交易所判定:北交所代码段(430/830/920)优先,否则按市场号(1=沪,0=深)。"""
    if code.startswith(("430", "830", "920")):
        return "BJ"
    return "SH" if mkt == "1" else "SZ"


def _em_clist_pages(http: httpx.Client, fs: str, fields: str, fid: str,
                    *, pz: int = 100, max_pages: int = 200) -> list[dict]:
    """东财 push2 clist 翻页,返回所有页的原始 diff dict 合集。

    _Quotes(实时行情)与 _Exchanges(标的维表)共用:两者仅 fid 与字段消费方式不同,
    翻页循环 / 退避 / break 条件(空页、末页不满、页数上限)在此统一。
    r.json() 抛错时按已累计结果提前返回(与原两处内联逻辑一致)。
    """
    out: list[dict] = []
    pn = 1
    while True:
        r = _http_get(http,
                      "https://push2.eastmoney.com/api/qt/clist/get",
                      referer=EM_REFERER,
                      pn=pn, pz=pz, po=1, np=1, fltt=2, invt=2,
                      fid=fid, fs=fs, fields=fields)
        try:
            data = r.json().get("data") or {}
        except Exception:
            break
        diff = data.get("diff") or []
        if not diff:
            break
        out.extend(diff)
        if len(diff) < pz:
            break
        pn += 1
        if pn > max_pages:
            break
        time.sleep(0.05)  # 翻页自我节流
    return out


def _sina_hq_lines(http: httpx.Client, symbols: list[str]) -> list[str]:
    """新浪 hq_str 批量行情:抓取 + gbk 解码,返回原始行(var hq_str_xxx=...)。

    _Quotes(解析成 quote)与 _Depth(取五档 parts)共用同一次抓取与解码。
    """
    sina_list = _sina_symbols_param(list(symbols))
    r = _http_get(http, "http://hq.sinajs.cn/rn=0",
                  referer=SINA_REFERER, list=sina_list)
    return r.content.decode("gbk", errors="replace").splitlines()


def _em_datacenter_page(http: httpx.Client, *, report: str, columns: str, filter: str,
                        page_number: int, page_size: int,
                        sort_columns: str, sort_types: int) -> list[dict]:
    """东财 datacenter v1 单页取数,返回 result.data(json 解析失败→空 list)。

    _Financials(单页取最新)与 _Exchanges 的 datacenter fallback(翻页)共用取数+解析,
    URL/referer 只在此出现一次。_http_get 的异常向调用方传播(由各自决定传播还是 break)。
    """
    r = _http_get(http,
                  "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                  referer="https://emweb.securities.eastmoney.com/",
                  reportName=report, columns=columns, filter=filter,
                  pageNumber=page_number, pageSize=page_size,
                  sortColumns=sort_columns, sortTypes=sort_types)
    try:
        return (r.json().get("result") or {}).get("data") or []
    except Exception:
        return []


class _Klines:
    def __init__(self, client: FreeSourceClient):
        self._c = client
        # 会话级熔断:东财 push2his 连续失败达阈值后本进程不再试探,剩余标的直接走新浪。
        self._em_tripped = False
        self._em_fail_streak = 0

    @staticmethod
    def _parse_em_klines(klines: list[str], symbol: str) -> list[dict]:
        """东财 klines 字符串: "date,open,close,high,low,volume,amount,change_pct"。"""
        rows = []
        for s in klines or []:
            parts = s.split(",")
            if len(parts) < 7:
                continue
            rows.append({
                "symbol": symbol,
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
            })
        return rows

    def _fetch_em(self, symbol, klt, count, adjust) -> list[dict]:
        """东财 push2his 主路径(部分网络环境被针对 RST,作为可失败的首选)。"""
        secid = _symbol_to_secid(symbol)
        # fqt: 0=不复权 1=前复权 2=后复权
        # 关键(免费源复权语义):本 adapter 的 ex_factors() 恒返回空 dict,
        # pipeline 的 _apply_adj_factor 遇空因子会原样返回 raw、不再复权。
        # 因此【日K】必须由东财服务端直接返回前复权价(fqt=1),否则 enriched 里
        # 标注"前复权"的 open/high/low/close 会是未复权原始价(除权跳空缺口未消除)。
        # 上游 kline_sync.sync_daily_batch 写死 adjust="none",这里对日K忽略该值强制 qfq。
        # 【分钟K】下游明确只存 raw、不复权(见 sync_and_persist_minute 注释),保持 fqt=0。
        if klt == 101:  # 日K:强制前复权
            fqt = 1
        else:           # 分钟K/其他:按 adjust 映射,默认不复权
            fqt = {"none": 0, "qfq": 1, "hfq": 2, "front": 1, "back": 2}.get(adjust, 0)
        beg = "19900101"
        end = "20990101"
        r = _http_get(self._c._http,
                      "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                      referer=EM_REFERER,
                      secid=secid,
                      fields1="f1,f2,f3,f4,f5,f6",
                      fields2="f51,f52,f53,f54,f55,f56,f57,f58",
                      klt=klt, fqt=fqt, beg=beg, end=end, lmt=count or 10000)
        try:
            klines = (r.json().get("data") or {}).get("klines") or []
        except Exception:
            klines = []
        return self._parse_em_klines(klines, symbol)

    def _fetch_one(self, symbol, klt, count, adjust) -> list[dict]:
        """K 线拉取:东财 push2his 主用,失败(RST/502 等)→ 新浪 fallback。

        新浪日K(scale=240)与分钟K(scale≥5)实测稳定,作为 push2his 被网络阻断时的替补。
        注意新浪 1 分钟(scale=1)返回 null,1m 已在 SINA_SCALE 映射到 5m。
        """
        # period by klt:101→1d, 1→1m, 5→5m, 15/30/60 同名
        period = {101: "1d", 1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "60m"}.get(klt, "1d")
        # 会话级熔断:东财已被判定不可用(连续失败达阈值)时,直接走新浪,
        # 跳过注定失败的 push2his 试探(每只省下 3 次重试 + 退避 ≈ 2s)。
        if not self._em_tripped:
            try:
                rows = self._fetch_em(symbol, klt, count, adjust)
                if rows:
                    self._em_fail_streak = 0  # 成功 → 清零连续失败
                    return rows
            except Exception as e:
                self._em_fail_streak += 1
                if self._em_fail_streak >= _EM_KLINE_TRIP_THRESHOLD:
                    self._em_tripped = True
                    logger.warning(
                        "free klines EM 连续失败 %d 次,本会话改用新浪(重启进程后重试东财)",
                        self._em_fail_streak)
                else:
                    logger.warning("free klines EM %s failed, fallback to SINA: %s", symbol, e)
        # 东财熔断 / 失败 / 空 → 新浪
        return _sina_klines(self._c._http, symbol, period, count)

    def batch(self, symbols, period="1d", count=250, adjust="none",
              start_time=None, end_time=None, as_dataframe=True, show_progress=False):
        # period: "1d" → klt=101; "1m" → klt=1; "5m" → klt=5
        klt = {"1d": 101, "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(period, 101)
        out: dict = {}
        for i, sym in enumerate(symbols):
            if i > 0:
                time.sleep(0.05)  # 自我节流
            try:
                rows = self._fetch_one(sym, klt, count, adjust)
            except Exception as e:
                logger.warning("free klines %s failed: %s", sym, e)
                rows = []
            if as_dataframe:
                cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
                out[sym] = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()
            else:
                out[sym] = rows
        return out

    def get(self, symbol, period="1d", count=250, adjust="none",
            start_time=None, end_time=None, as_dataframe=False, show_progress=False):
        d = self.batch([symbol], period=period, count=count, adjust=adjust, as_dataframe=as_dataframe)
        return d.get(symbol)

    def ex_factors(self, symbols, as_dataframe=False, batch_size=None, show_progress=False,
                   start_time=None, end_time=None):
        return {}  # 公开源直接返回前复权价,无原始因子;空 dict 让 _normalize_adj_factor 自然跳过

    @staticmethod
    def _parse_tx_minute(arr: list[str], symbol: str) -> list[dict]:
        """腾讯分时: "0930 8.69 4253 3695857.00" → time/price/volume/amount。"""
        rows = []
        for s in arr or []:
            parts = s.split(" ")
            if len(parts) < 4:
                continue
            rows.append({
                "symbol": symbol,
                "time": parts[0],
                "price": float(parts[1]),
                "volume": float(parts[2]),
                "amount": float(parts[3]),
            })
        return rows

    def intraday(self, symbol, count=None, as_dataframe=False):
        sina = _sina_symbol(symbol)
        r = _http_get(self._c._http,
                      "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                      referer="https://gu.qq.com/", code=sina)
        try:
            arr = (((r.json().get("data") or {}).get(sina) or {}).get("data") or {}).get("data") or []
        except Exception:
            arr = []
        rows = self._parse_tx_minute(arr, symbol)
        if count:
            rows = rows[-count:]
        return rows

    def intraday_batch(self, symbols, count=None, as_dataframe=False):
        out: dict = {}
        for i, sym in enumerate(symbols):
            if i > 0:
                time.sleep(0.05)
            out[sym] = self.intraday(sym, count=count, as_dataframe=False)
        return out


class _Quotes:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    @staticmethod
    def _parse_sina_line(line: str) -> dict | None:
        """解析 hq_str_xxx="..." 一行 → quote dict(对齐 SDK 字段)。"""
        if "=" not in line or not line.startswith("var "):
            return None
        prefix, _, rest = line.partition("=")
        # prefix: var hq_str_sh600000 → sh600000
        sina_code = prefix.split("_")[-1].strip()
        rest = rest.strip().rstrip(";").strip('"')
        parts = rest.split(",")
        if len(parts) < 32:
            return None
        # 新浪 hq_str 字段(实测,到状态位为止,无涨跌幅/换手):
        #   0=name 1=today_open 2=prev_close 3=current 4=high 5=low
        #   6=bid 7=ask 8=volume(股) 9=amount(元)
        #   10~19=买5量价 20~29=卖5量价 30=date 31=time 32=status(00)
        # 涨跌额/涨跌幅新浪不直接给,按 last-prev 计算;换手新浪无,置 None
        # (enriched 的换手由 volume/float_shares 在 pipeline 另算)。
        name = parts[0]
        open_p = _f(parts, 1)
        prev_close = _f(parts, 2)
        last_price = _f(parts, 3)
        high = _f(parts, 4)
        low = _f(parts, 5)
        volume = _f(parts, 8)
        amount = _f(parts, 9)
        if last_price is not None and prev_close is not None:
            change_amount = round(last_price - prev_close, 3)
            change_pct = round(change_amount / prev_close * 100, 2) if prev_close else None
        else:
            change_amount = None
            change_pct = None
        turnover_rate = None
        # 还原 symbol: sh600000 → 600000.SH / sz000001 → 000001.SZ / bj430047 → 430047.BJ
        code = sina_code[2:]
        exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sina_code[:2], "SH")
        symbol = f"{code}.{exch}"
        return {
            "symbol": symbol,
            "name": name,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_p,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "timestamp": _sina_timestamp(parts, 30, 31),
            "session": "regular",
            "ext": {
                "name": name,
                "change_amount": change_amount,
                "change_pct": change_pct,
                "turnover_rate": turnover_rate,
                "amplitude": None,
            },
        }

    def _fetch_sina(self, symbols: list[str]) -> list[dict]:
        out = []
        for line in _sina_hq_lines(self._c._http, symbols):
            q = self._parse_sina_line(line)
            if q:
                out.append(q)
        return out

    def get(self, symbols, as_dataframe=False):
        rows = self._fetch_sina(list(symbols))
        if as_dataframe:
            # 扁平化 ext.* 为列名(消费方按 ext.change_pct / ext.name 取)
            flat = []
            for r in rows:
                ext = r.pop("ext") or {}
                for k, v in ext.items():
                    r[f"ext.{k}"] = v
                flat.append(r)
            return pd.DataFrame(flat)
        return rows

    def get_by_symbols(self, symbols, as_dataframe=False):
        return self.get(symbols, as_dataframe=as_dataframe)

    def get_by_universes(self, universes, as_dataframe=False):
        """东财 clist 按 universe 拉对应板块行情。"""
        fs_map = {
            "CN_Equity_A": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80,m:0+t:81",
            "CN_ETF": "b:MK0021,m:1+t:10,m:0+t:10",
            "CN_Index": "m:1+s:2,m:0+t:5",
            "CSI300": "b:BK0500",
            "CSI500": "b:BK0806",
            "SSE50": "b:BK0007",
        }
        out: list[dict] = []
        for u in universes or []:
            fs = fs_map.get(u)
            if not fs:
                continue
            rows = self._fetch_em_clist(fs, fields="f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18")
            out.extend(rows)
        if as_dataframe:
            return pd.DataFrame(out)
        return out

    def _fetch_em_clist(self, fs: str, fields: str) -> list[dict]:
        """东财 clist 实时行情 → SDK quote dict。"""
        out = []
        for d in _em_clist_pages(self._c._http, fs, fields, fid="f3"):
            code = str(d.get("f12") or "")
            if not code:
                continue
            sym_ex = _resolve_exchange(code, str(d.get("f13") or ""))
            out.append({
                "symbol": f"{code}.{sym_ex}",
                "name": d.get("f14") or code,
                "last_price": _f(d, "f2"),
                "prev_close": _f(d, "f18"),
                "open": _f(d, "f17"),
                "high": _f(d, "f15"),
                "low": _f(d, "f16"),
                "volume": _f(d, "f5"),
                "amount": _f(d, "f6"),
                "timestamp": None,
                "session": "regular",
                "ext": {
                    "name": d.get("f14") or code,
                    "change_amount": _f(d, "f4"),
                    "change_pct": _f(d, "f3"),
                    "turnover_rate": None,
                    "amplitude": None,
                },
            })
        return out


class _Depth:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def batch(self, symbols):
        # 复用 quotes 的新浪解析,五档在行情串 parts 10~29
        out: dict = {}
        for line in _sina_hq_lines(self._c._http, list(symbols)):
            parsed = _Quotes._parse_sina_line(line)
            if not parsed:
                continue
            # 重新从原始 line 取原始 parts(解析函数已丢弃五档)
            rest = line.split("=", 1)[-1].strip().rstrip(";").strip('"')
            parts = rest.split(",")
            if len(parts) < 30:
                continue
            sym = parsed["symbol"]
            # 买5: parts[10..19] = b1vol,b1price,b2vol,b2price,...
            bid_vols = [_f(parts, i) for i in range(10, 20, 2)]
            ask_vols = [_f(parts, i) for i in range(20, 30, 2)]
            out[sym] = {
                "ask_volumes": ask_vols,
                "bid_volumes": bid_vols,
                "timestamp": parsed.get("timestamp") or 0,
            }
        return out

    def get(self, symbol):
        d = self.batch([symbol])
        return d.get(symbol) if d else None


class _Financials:
    # 东财 reportName(实测有效值):四张表统一用 datacenter v1 端点。
    # 注意:income/cash_flow 曾误用 RPT_LICO_FN_CPD / RPT_LICO_FN_CFR —— 前者报
    # "REPORT_DATE排序列不存在"、后者"报表配置不存在",均无效。改用 RPT_DMSK_FN_* 系列。
    _REPORTS: ClassVar[dict[str, str]] = {
        "metrics": "RPT_F10_FINANCE_DUPONT",       # 杜邦,含 ROE/净利率/ROA/资产负债率
        "income": "RPT_DMSK_FN_INCOME",            # 利润表(单季主要科目)
        "balance_sheet": "RPT_DMSK_FN_BALANCE",    # 资产负债表(主要科目)
        "cash_flow": "RPT_DMSK_FN_CASHFLOW",       # 现金流量表(三项净额)
    }

    # 东财大写字段 → 前端 FIELD_DEFS 期望的 SDK 蛇形键(方案 B:只映射能对上的核心字段,
    # 东财缺失的字段前端自然显示 —)。金额单位为元(与 fmtBigNum 一致)、比率为百分点
    # (与前端 pct 格式一致),无需换算。原始东财键保留,供 AI 分析(它整体喂 LLM)。
    _FIELD_MAP: ClassVar[dict[str, dict[str, str]]] = {
        "metrics": {
            "ROE_AVG": "roe",
            "JROA": "roa",
            "SALE_NPR": "net_margin",
            "DEBT_ASSET_RATIO": "debt_to_asset_ratio",
        },
        "income": {
            "TOTAL_OPERATE_INCOME": "revenue",
            "OPERATE_COST": "operating_cost",
            "OPERATE_PROFIT": "operating_profit",
            "SALE_EXPENSE": "selling_expense",
            "MANAGE_EXPENSE": "admin_expense",
            "FINANCE_EXPENSE": "financial_expense",
            "TOTAL_PROFIT": "total_profit",
            "INCOME_TAX": "income_tax",
            "NETPROFIT": "net_income",
            "PARENT_NETPROFIT": "net_income_attributable",
            "DEDUCT_PARENT_NETPROFIT": "net_income_deducted",
            "BASIC_EPS": "basic_eps",
            "DILUTED_EPS": "diluted_eps",
        },
        "balance_sheet": {
            "TOTAL_ASSETS": "total_assets",
            "TOTAL_CURRENT_ASSETS": "total_current_assets",
            "TOTAL_NONCURRENT_ASSETS": "total_non_current_assets",
            "MONETARYFUNDS": "cash_and_equivalents",
            "ACCOUNTS_RECE": "accounts_receivable",
            "INVENTORY": "inventory",
            "FIXED_ASSET": "fixed_assets",
            "TOTAL_LIABILITIES": "total_liabilities",
            "TOTAL_CURRENT_LIAB": "total_current_liabilities",
            "TOTAL_NONCURRENT_LIAB": "total_non_current_liabilities",
            "ACCOUNTS_PAYABLE": "accounts_payable",
            "TOTAL_EQUITY": "total_equity",
            "TOTAL_PARENT_EQUITY": "equity_attributable",
        },
        "cash_flow": {
            "NETCASH_OPERATE": "net_operating_cash_flow",
            "NETCASH_INVEST": "net_investing_cash_flow",
            "NETCASH_FINANCE": "net_financing_cash_flow",
            "CCE_ADD": "net_cash_change",
        },
    }

    def __init__(self, client: FreeSourceClient):
        self._c = client

    def _fetch(self, table, symbols, latest=True):
        report = self._REPORTS.get(table)
        if not report:
            return {}
        field_map = self._FIELD_MAP.get(table, {})
        out: dict = {}
        for i, sym in enumerate(symbols):
            if i > 0:
                time.sleep(0.05)
            code, _ = _split_symbol(sym)
            try:
                rows = _em_datacenter_page(
                    self._c._http, report=report, columns="ALL",
                    filter=f'(SECURITY_CODE="{code}")',
                    page_number=1, page_size=2 if latest else 50,
                    sort_columns="REPORT_DATE", sort_types=-1)
            except Exception:
                rows = []
            # 每条 record 上补三样东西(原始东财键全部保留,供 AI 整体喂 LLM):
            #   1. symbol —— financial_sync 期待 record["symbol"] 存在
            #   2. period_end —— financial_analyzer 排序/摘要依赖(东财为 REPORT_DATE)
            #   3. 前端 FIELD_DEFS 期望的 SDK 蛇形键 —— 否则财务详情页四个 Tab 全显示 —
            for rec in rows:
                rec["symbol"] = sym
                if "period_end" not in rec:
                    rd = rec.get("REPORT_DATE")
                    if rd:
                        rec["period_end"] = str(rd)[:10]
                for em_key, sdk_key in field_map.items():
                    if sdk_key not in rec and em_key in rec:
                        rec[sdk_key] = rec[em_key]
            out[sym] = rows
        return out

    def metrics(self, symbols, latest=True, as_dataframe=False):
        return self._fetch("metrics", list(symbols), latest)

    def income(self, symbols, latest=True, as_dataframe=False):
        return self._fetch("income", list(symbols), latest)

    def balance_sheet(self, symbols, latest=True, as_dataframe=False):
        return self._fetch("balance_sheet", list(symbols), latest)

    def cash_flow(self, symbols, latest=True, as_dataframe=False):
        return self._fetch("cash_flow", list(symbols), latest)


class _Exchanges:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def get_instruments(self, exchange, instrument_type="stock"):
        ex = exchange.upper()
        # 东财 clist 的 fs 过滤器:按交易所 x 品种
        fs_map = {
            ("SH", "stock"): "m:1+t:2,m:1+t:23",      # 沪主板 + 科创板
            ("SZ", "stock"): "m:0+t:6,m:0+t:80",      # 深主板 + 创业板
            ("BJ", "stock"): "m:0+t:81",              # 北证
            ("SH", "index"): "m:1+s:2",               # 上证指数
            ("SZ", "index"): "m:0+t:5",               # 深证指数
            ("SH", "etf"): "b:MK0021,m:1+t:10",       # 沪市 ETF
            ("SZ", "etf"): "b:MK0021,m:0+t:10",       # 深市 ETF
        }
        fs = fs_map.get((ex, instrument_type))
        if fs is None:
            return []
        # f12 代码 f13 市场 f14 名 f38 总股本 f39 流通股本
        # (total_shares/float_shares 供 strategy 市值过滤 close*total_shares;
        #  listing_date/limit_up clist 不提供,置 None,下游按缺省处理)
        fields = "f12,f13,f14,f38,f39"
        out: list[dict] = []
        try:
            out = self._clist_instruments(fs, fields, instrument_type)
        except Exception as e:
            logger.warning("free instruments EM clist %s/%s failed, fallback to datacenter: %s",
                           exchange, instrument_type, e)
            out = []
        if out:
            return out
        # push2 clist 不可用(502/RST)→ datacenter 标的列表(同源,字段降级:无股本)
        return self._datacenter_instruments(ex, instrument_type)

    def _clist_instruments(self, fs, fields, instrument_type) -> list[dict]:
        """东财 push2 clist(主,提供股本)。"""
        out: list[dict] = []
        for d in _em_clist_pages(self._c._http, fs, fields, fid="f12"):
            code = str(d.get("f12") or "")
            if not code:
                continue
            # f13 是市场代码(1=沪,0=深),用于精确判交易所;BJ 代码段单独识别
            sym_ex = _resolve_exchange(code, str(d.get("f13") or ""))
            out.append({
                "symbol": f"{code}.{sym_ex}",
                "name": d.get("f14") or code,
                "code": code,
                "exchange": sym_ex,
                "region": "CN",
                "type": instrument_type,
                "ext": {
                    "total_shares": _f(d, "f38"),
                    "float_shares": _f(d, "f39"),
                    "listing_date": None,
                    "tick_size": None,
                    "limit_up": None,
                    "limit_down": None,
                },
            })
        return out

    def _datacenter_instruments(self, ex: str, instrument_type: str) -> list[dict]:
        """东财 datacenter 标的列表(push2 clist 的 fallback,同源可通,字段降级)。

        reportName=RPT_F10_ORG_BASICINFO;按 TRADE_MARKET_CODE / SECURITY_TYPE 过滤。
        无 total_shares/float_shares(置 None,strategy 市值过滤将按缺省跳过)。
        """
        if instrument_type != "stock":
            return []  # index/etf 维表来源少,clist 失败时返回空,后续重试
        market_filter = {
            "SH": 'TRADE_MARKET_CODE="069001001001"',  # 沪主板
            "SZ": 'TRADE_MARKET_CODE="069001002001"',  # 深主板
        }.get(ex)
        if not market_filter:
            return []
        out: list[dict] = []
        pn = 1
        pz = 200
        while True:
            try:
                data = _em_datacenter_page(
                    self._c._http,
                    report="RPT_F10_ORG_BASICINFO",
                    columns="SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,LISTING_DATE",
                    filter=market_filter, page_number=pn, page_size=pz,
                    sort_columns="SECURITY_CODE", sort_types=1)
            except Exception:
                break
            if not data:
                break
            for d in data:
                secucode = str(d.get("SECUCODE") or "")
                code = str(d.get("SECURITY_CODE") or "")
                if not code:
                    continue
                # SECUCODE 形如 600000.SH / 000001.SZ / 430047.BJ,直接取后缀
                sym_ex = secucode.rsplit(".", 1)[-1] if "." in secucode else ex
                listing = d.get("LISTING_DATE")
                out.append({
                    "symbol": f"{code}.{sym_ex}",
                    "name": d.get("SECURITY_NAME_ABBR") or code,
                    "code": code,
                    "exchange": sym_ex,
                    "region": "CN",
                    "type": instrument_type,
                    "ext": {
                        "total_shares": None,
                        "float_shares": None,
                        "listing_date": str(listing)[:10] if listing else None,
                        "tick_size": None,
                        "limit_up": None,
                        "limit_down": None,
                    },
                })
            if len(data) < pz:
                break
            pn += 1
            if pn > 100:
                break
            time.sleep(0.05)
        return out


class _Universes:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def list(self):
        # 本地固定列表(pools._find_universe_id 按 id/name 子串匹配)
        return [
            {"id": "CN_Equity_A", "name": "沪深京A股"},
            {"id": "CN_ETF", "name": "沪深ETF"},
            {"id": "CN_Index", "name": "沪深指数"},
            {"id": "CSI300", "name": "沪深300"},
            {"id": "CSI500", "name": "中证500"},
            {"id": "SSE50", "name": "上证50"},
        ]


class FreeSourceClient:
    """鸭子类型 TickFlow SDK 对象(同步)。"""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(transport=transport, timeout=TIMEOUT,
                                  headers={"User-Agent": UA})
        self.klines = _Klines(self)
        self.quotes = _Quotes(self)
        self.depth = _Depth(self)
        self.financials = _Financials(self)
        self.exchanges = _Exchanges(self)
        self.universes = _Universes(self)
