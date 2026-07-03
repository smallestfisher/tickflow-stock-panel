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
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"
EM_REFERER = "https://quote.eastmoney.com/"
SINA_REFERER = "https://finance.sina.com.cn"
TIMEOUT = 10.0

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
    """与 policy._is_transient 同语义:5xx/429/超时/连接为瞬时。"""
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


class _Klines:
    def __init__(self, client: FreeSourceClient):
        self._c = client

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

    def _fetch_one(self, symbol, klt, count, adjust):
        secid = _symbol_to_secid(symbol)
        # fqt: 0=不复权 1=前复权 2=后复权
        fqt = {"none": 0, "qfq": 1, "hfq": 2, "front": 1, "back": 2}.get(adjust, 1)
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

    def get(self, symbols, as_dataframe=False):
        raise NotImplementedError

    def get_by_symbols(self, symbols, as_dataframe=False):
        return self.get(symbols, as_dataframe=as_dataframe)

    def get_by_universes(self, universes, as_dataframe=False):
        raise NotImplementedError


class _Depth:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def batch(self, symbols):
        raise NotImplementedError

    def get(self, symbol):
        d = self.batch([symbol])
        return d.get(symbol) if d else None


class _Financials:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def metrics(self, symbols, latest=True, as_dataframe=False):
        raise NotImplementedError

    def income(self, symbols, latest=True, as_dataframe=False):
        raise NotImplementedError

    def balance_sheet(self, symbols, latest=True, as_dataframe=False):
        raise NotImplementedError

    def cash_flow(self, symbols, latest=True, as_dataframe=False):
        raise NotImplementedError


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
        fields = "f12,f13,f14,f3,f6"  # code, market, name, change_pct, amount
        out: list[dict] = []
        pn = 1
        pz = 100
        while True:
            r = _http_get(self._c._http,
                          "https://push2.eastmoney.com/api/qt/clist/get",
                          referer=EM_REFERER,
                          pn=pn, pz=pz, po=1, np=1, fltt=2, invt=2,
                          fid="f12", fs=fs, fields=fields)
            try:
                data = r.json().get("data") or {}
            except Exception:
                break
            diff = data.get("diff") or []
            if not diff:
                break
            for d in diff:
                code = str(d.get("f12") or "")
                if not code:
                    continue
                # f13 是市场代码(1=沪,0=深),用于精确判交易所;BJ 代码段单独识别
                mkt = str(d.get("f13") or "")
                if code.startswith(("430", "830", "920")):
                    sym_ex = "BJ"
                else:
                    sym_ex = "SH" if mkt == "1" else "SZ"
                out.append({
                    "symbol": f"{code}.{sym_ex}",
                    "name": d.get("f14") or code,
                    "code": code,
                    "exchange": sym_ex,
                    "region": "CN",
                    "type": instrument_type,
                    "ext": {},
                })
            if len(diff) < pz:
                break
            pn += 1
            if pn > 200:  # 安全上限
                break
            time.sleep(0.05)  # 翻页自我节流
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
