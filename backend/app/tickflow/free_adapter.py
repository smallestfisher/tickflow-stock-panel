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
        sina_list = _sina_symbols_param(symbols)
        r = _http_get(self._c._http,
                      "http://hq.sinajs.cn/rn=0",
                      referer=SINA_REFERER, list=sina_list)
        text = r.content.decode("gbk", errors="replace")
        out = []
        for line in text.splitlines():
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
        pn = 1
        pz = 100
        while True:
            r = _http_get(self._c._http,
                          "https://push2.eastmoney.com/api/qt/clist/get",
                          referer=EM_REFERER,
                          pn=pn, pz=pz, po=1, np=1, fltt=2, invt=2,
                          fid="f3", fs=fs, fields=fields)
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
                mkt = str(d.get("f13") or "")
                if code.startswith(("430", "830", "920")):
                    sym_ex = "BJ"
                else:
                    sym_ex = "SH" if mkt == "1" else "SZ"
                last = _f(d, "f2")
                change_pct = _f(d, "f3")
                change_amount = _f(d, "f4")
                volume = _f(d, "f5")
                amount = _f(d, "f6")
                high = _f(d, "f15")
                low = _f(d, "f16")
                open_p = _f(d, "f17")
                prev_close = _f(d, "f18")
                out.append({
                    "symbol": f"{code}.{sym_ex}",
                    "name": d.get("f14") or code,
                    "last_price": last,
                    "prev_close": prev_close,
                    "open": open_p,
                    "high": high,
                    "low": low,
                    "volume": volume,
                    "amount": amount,
                    "timestamp": None,
                    "session": "regular",
                    "ext": {
                        "name": d.get("f14") or code,
                        "change_amount": change_amount,
                        "change_pct": change_pct,
                        "turnover_rate": None,
                        "amplitude": None,
                    },
                })
            if len(diff) < pz:
                break
            pn += 1
            if pn > 200:
                break
            time.sleep(0.05)
        return out


class _Depth:
    def __init__(self, client: FreeSourceClient):
        self._c = client

    def batch(self, symbols):
        # 复用 quotes 的新浪解析,五档在行情串 parts 10~29
        sina_list = _sina_symbols_param(list(symbols))
        r = _http_get(self._c._http, "http://hq.sinajs.cn/rn=0",
                      referer=SINA_REFERER, list=sina_list)
        text = r.content.decode("gbk", errors="replace")
        out: dict = {}
        for line in text.splitlines():
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
    # 东财 reportName 映射
    _REPORTS: ClassVar[dict[str, str]] = {
        "metrics": "RPT_F10_FINANCE_DUPONT",       # 杜邦,含 ROE/净利率/周转率
        "income": "RPT_LICO_FN_CPD",               # 利润表
        "balance_sheet": "RPT_DMSK_FN_BALANCE",    # 资产负债表
        "cash_flow": "RPT_LICO_FN_CFR",            # 现金流量表
    }

    def __init__(self, client: FreeSourceClient):
        self._c = client

    def _fetch(self, table, symbols, latest=True):
        report = self._REPORTS.get(table)
        if not report:
            return {}
        out: dict = {}
        for i, sym in enumerate(symbols):
            if i > 0:
                time.sleep(0.05)
            code, _ = _split_symbol(sym)
            r = _http_get(self._c._http,
                          "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                          referer="https://emweb.securities.eastmoney.com/",
                          reportName=report, columns="ALL",
                          filter=f'(SECURITY_CODE="{code}")',
                          pageNumber=1, pageSize=2 if latest else 50,
                          sortColumns="REPORT_DATE", sortTypes=-1)
            try:
                rows = (r.json().get("result") or {}).get("data") or []
            except Exception:
                rows = []
            # 给每条补 symbol(financial_sync 期待 record["symbol"] 存在)
            for rec in rows:
                rec["symbol"] = sym
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
