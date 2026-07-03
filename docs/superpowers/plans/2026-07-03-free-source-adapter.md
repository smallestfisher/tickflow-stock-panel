# 免费公开数据源 adapter 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `FreeSourceClient`(鸭子类型对齐 TickFlow SDK),在「免费源模式」下由 `client.py` 返回它,使十几个 service 零改动地用东方财富/新浪/腾讯公开接口替代 TickFlow 付费 key。

**Architecture:** 一个 `free_adapter.py` 文件产出 `FreeSourceClient`,内部用 httpx 把 SDK 方法调用翻译成公开 HTTP 接口,返回结构与 SDK `as_dataframe=False`(dict/list[dict])及 `as_dataframe=True`(pandas DataFrame)一致。`policy.py` 在免费源模式直接构造全能力 capset(不经探测),`client.py` 三个 getter 在该模式下返回 `FreeSourceClient`。开关 `data_backend` 存 `secrets_store`,经 settings API 切换。

**Tech Stack:** Python 3.11、FastAPI、httpx(已在依赖)、polars、pandas(已在依赖,仅 `as_dataframe=True` 路径)、pytest。

**关键消费形态(已从代码核实,adapter 必须满足):**
- `klines.batch(symbols, period, count, adjust, start_time, end_time, as_dataframe=True, show_progress=False)` → `dict{symbol: pandas_df}`,列 `date/open/high/low/close/volume/amount`(`kline_sync._normalize_daily` 经 `pl.from_pandas` 消费)。
- `klines.get(sym, period, count, as_dataframe=False)` → 单 symbol 的 `list[dict]`(仅 `policy.py` 探测调用,不抛即可)。
- `klines.ex_factors(symbols, as_dataframe=True/False, ...)` → `dict{symbol: [records]}`;空返回 `{}` 即可(`_normalize_adj_factor` 对空返回空 df)。
- `klines.intraday(sym, count, as_dataframe=False)` / `intraday_batch(symbols, count, as_dataframe=False)` → `list[dict]`(仅探测调用)。
- `quotes.get(symbols, as_dataframe=False/True)` → False: `list[dict]`(`quote_service` 消费 `symbol/name/last_price/prev_close/open/high/low/volume/amount/ext{change_amount,change_pct,amplitude,turnover_rate}/timestamp/session`);True: pandas DataFrame(`watchlist` 经 `pl.from_pandas` 消费,列含 `last_price`、`ext.change_pct`、`ext.name`)。
- `quotes.get_by_symbols(symbols, as_dataframe=False)` → `list[dict]`(仅探测)。
- `quotes.get_by_universes(universes, as_dataframe=True/False)` → True: pandas DataFrame 含 `symbol` 列(`pools` 消费 `df["symbol"]`);False: `list[dict]`(`quote_service`/`index_sync` 消费)。
- `depth.batch(symbols)` → `dict{symbol: {ask_volumes: [...5], bid_volumes: [...5], timestamp: ms}}`(`depth_service` 消费 `ask_volumes[0]`/`bid_volumes[0]`)。
- `financials.metrics/income/balance_sheet/cash_flow(symbols, latest=True)` → `dict{symbol: [records]}`(`financial_sync` 给每条 record 补 `symbol` 列)。
- `exchanges.get_instruments(ex, instrument_type="stock"|"index"|"etf")` → `list[dict]`,字段 `symbol/name/code/exchange/region/type/ext{listing_date,total_shares,float_shares,tick_size,limit_up,limit_down}`(`instrument_sync._flatten_instruments` 消费)。
- `universes.list()` → `list[dict]`,字段 `id/name`(`pools._find_universe_id` 按 id/name 子串匹配)。

**测试依赖:** 用 httpx 自带 `MockTransport`(零新依赖)mock 公开接口响应,不写真实网络测试。

---

## File Structure

- **Create** `backend/app/tickflow/free_adapter.py` — `FreeSourceClient` 及内部 `_Klines/_Quotes/_Depth/_Financials/_Exchanges/_Universes` 子对象,httpx 客户端 + 符号/ secid 转换 + 重试。
- **Modify** `backend/app/tickflow/client.py` — 三个 getter 在 `data_backend=="free_source"` 时返回 `FreeSourceClient`;`_should_use_free_server`/`current_mode`/`current_endpoint` 增加分支。
- **Modify** `backend/app/tickflow/policy.py` — `detect_capabilities` 增加 free_source 分支(全能力 capset,不经探测);`base_tier_name` 兼容;`_CACHE_SCHEMA_VERSION` bump 到 6。
- **Modify** `backend/app/secrets_store.py` — 增加 `get_data_backend()`/`set_data_backend()`。
- **Modify** `backend/app/api/settings.py` — `GET /api/settings` 返回 `data_backend`;新增 `POST /api/settings/data-backend`。
- **Create** `backend/tests/tickflow/__init__.py`、`conftest.py`、`test_free_adapter_mapping.py`、`test_free_adapter_capabilities.py`、`test_free_adapter_client_routing.py`。

---

## Task 1: secrets_store 增加 data_backend 字段

**Files:**
- Modify: `backend/app/secrets_store.py`
- Test: `backend/tests/tickflow/test_secrets_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/tickflow/test_secrets_backend.py
from pathlib import Path
from app import secrets_store


def test_data_backend_default(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    assert secrets_store.get_data_backend() == "tickflow"


def test_data_backend_set_and_get(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    secrets_store.set_data_backend("free_source")
    assert secrets_store.get_data_backend() == "free_source"
    # 持久化
    import json
    data = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert data["data_backend"] == "free_source"


def test_data_backend_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    try:
        secrets_store.set_data_backend("bogus")
    except ValueError:
        return
    raise AssertionError("should reject invalid backend")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_secrets_backend.py -v`
Expected: FAIL — `get_data_backend` not defined.

- [ ] **Step 3: Add the functions**

Append to `backend/app/secrets_store.py` (after `mask`):

```python
_VALID_BACKENDS = ("tickflow", "free_source")


def get_data_backend() -> str:
    """数据后端: "tickflow"(默认) / "free_source"(免费公开源 adapter)。"""
    return load().get("data_backend") or "tickflow"


def set_data_backend(backend: str) -> str:
    """持久化数据后端选择。非法值抛 ValueError。"""
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"invalid data_backend: {backend}; expected one of {_VALID_BACKENDS}")
    save({"data_backend": backend})
    return backend
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_secrets_backend.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/secrets_store.py backend/tests/tickflow/test_secrets_backend.py
git commit -m "feat: secrets_store 增加 data_backend 开关"
```

---

## Task 2: policy.py 增加免费源全能力 capset 分支

**Files:**
- Modify: `backend/app/tickflow/policy.py`
- Test: `backend/tests/tickflow/test_free_adapter_capabilities.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/tickflow/test_free_adapter_capabilities.py
from app.tickflow import policy
from app.tickflow.capabilities import Cap, CapabilitySet
from app import secrets_store


def test_free_source_full_capset(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "get_data_backend", lambda: "free_source")
    monkeypatch.setattr(policy.settings, "data_dir", tmp_path)
    capset = policy.detect_capabilities(force=True)
    # 全能力都在
    for cap in Cap:
        assert capset.has(cap), f"free_source 应有 {cap}"
    assert policy.base_tier_name() == "free_source"
    assert "免费源" in policy.tier_label()
    assert policy.is_invalid_key() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_capabilities.py -v`
Expected: FAIL — 免费源分支未实现,走默认探测或 none。

- [ ] **Step 3: Bump schema version**

In `backend/app/tickflow/policy.py`, change:

```python
_CACHE_SCHEMA_VERSION = 5
```
to:

```python
_CACHE_SCHEMA_VERSION = 6   # v6: 新增 free_source 模式(免费公开源 adapter,全能力不经探测)
```

- [ ] **Step 4: Add free_source branch to detect_capabilities**

In `detect_capabilities`, immediately after `tiers = _load_tiers_yaml()` (and before the `if settings.use_free_mode:` block), insert:

```python
    from app import secrets_store
    if secrets_store.get_data_backend() == "free_source":
        # 免费公开源 adapter — 不经 TickFlow 探测,直接给全能力 capset
        # 限速取 expert 档(公开源无 key 限制,用此值做 adapter 内部自我节流)
        capset = _tier_to_capset(tiers["expert"])
        # 确保所有 Cap 都在(expert 档若缺个别 cap 也补上,默认空 limits)
        for cap in Cap:
            if not capset.has(cap):
                capset._caps[cap] = CapabilityLimits()
        _persist(
            capset,
            "免费源(东财/新浪/腾讯)",
            log=["免费公开数据源 adapter 模式(全能力,不经 TickFlow 探测)"],
            missing=[],
            extras=[],
        )
        return capset
```

- [ ] **Step 5: Make base_tier_name free_source-aware**

`base_tier_name` reads `tier_label()` from cache; the persisted label `"免费源(东财/新浪/腾讯)"` would make `base_tier_name()` return `"免费源(东财/新浪/腾讯)".split()[0]...` = garbled. Override at the source. Replace the `base_tier_name` function body with:

```python
def base_tier_name() -> str:
    """当前档位的基础名(小写): none / free / starter / pro / expert / free_source。"""
    from app import secrets_store
    if secrets_store.get_data_backend() == "free_source":
        return "free_source"
    label = tier_label()
    return label.split()[0].split("+")[0].strip().lower()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_capabilities.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/tickflow/policy.py backend/tests/tickflow/test_free_adapter_capabilities.py
git commit -m "feat: policy 增加免费源全能力 capset 分支"
```

---

## Task 3: free_adapter.py 骨架 + HTTP 基础 + 符号转换

**Files:**
- Create: `backend/app/tickflow/free_adapter.py`
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`

- [ ] **Step 1: Write the failing test for helpers**

```python
# backend/tests/tickflow/test_free_adapter_mapping.py
import httpx
from app.tickflow.free_adapter import _symbol_to_secid, _secid_to_symbol, _sina_symbols_param


def test_symbol_to_secid_sh():
    assert _symbol_to_secid("600000.SH") == "1.600000"


def test_symbol_to_secid_sz():
    assert _symbol_to_secid("000001.SZ") == "0.000001"


def test_symbol_to_secid_bj():
    assert _symbol_to_secid("430047.BJ") == "0.430047"


def test_secid_to_symbol_roundtrip():
    assert _secid_to_symbol("1.600000") == "600000.SH"
    assert _secid_to_symbol("0.000001") == "000001.SZ"


def test_sina_symbols_param():
    # 新浪 list= 接受 sh600000 / sz000001 / bj430047 前缀
    assert _sina_symbols_param(["600000.SH", "000001.SZ"]) == "sh600000,sz000001"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write free_adapter.py skeleton with helpers**

```python
# backend/app/tickflow/free_adapter.py
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
            r = http.get(url, params=params, headers={"User-Agent": UA, "Referer": referer}, timeout=TIMEOUT)
            # 5xx 视为瞬时
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_transient(e):
                raise
            if i < 2:
                time.sleep(0.6 * (i + 1))
    assert last is not None
    raise last


class _Klines:
    def __init__(self, client: "FreeSourceClient"):
        self._c = client

    def batch(self, symbols, period="1d", count=250, adjust="none",
              start_time=None, end_time=None, as_dataframe=True, show_progress=False):
        raise NotImplementedError

    def get(self, symbol, period="1d", count=250, adjust="none",
            start_time=None, end_time=None, as_dataframe=False, show_progress=False):
        raise NotImplementedError

    def ex_factors(self, symbols, as_dataframe=False, batch_size=None, show_progress=False,
                   start_time=None, end_time=None):
        return {}  # 公开源直接返回前复权价,无原始因子;空 dict 让 _normalize_adj_factor 自然跳过

    def intraday(self, symbol, count=None, as_dataframe=False):
        raise NotImplementedError

    def intraday_batch(self, symbols, count=None, as_dataframe=False):
        raise NotImplementedError


class _Quotes:
    def __init__(self, client: "FreeSourceClient"):
        self._c = client

    def get(self, symbols, as_dataframe=False):
        raise NotImplementedError

    def get_by_symbols(self, symbols, as_dataframe=False):
        return self.get(symbols, as_dataframe=as_dataframe)

    def get_by_universes(self, universes, as_dataframe=False):
        raise NotImplementedError


class _Depth:
    def __init__(self, client: "FreeSourceClient"):
        self._c = client

    def batch(self, symbols):
        raise NotImplementedError

    def get(self, symbol):
        d = self.batch([symbol])
        return d.get(symbol) if d else None


class _Financials:
    def __init__(self, client: "FreeSourceClient"):
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
    def __init__(self, client: "FreeSourceClient"):
        self._c = client

    def get_instruments(self, exchange, instrument_type="stock"):
        raise NotImplementedError


class _Universes:
    def __init__(self, client: "FreeSourceClient"):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 骨架 + HTTP 基础 + 符号转换"
```

---

## Task 4: exchanges.get_instruments(东财 clist 翻页)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Exchanges.get_instruments`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
# 追加到 test_free_adapter_mapping.py
import json
from app.tickflow.free_adapter import FreeSourceClient


def _em_clist_transport(rows, total=None):
    """构造一个 mock transport,对 clist 请求返回给定 rows。"""
    total = total if total is not None else len(rows)
    def handler(request):
        return httpx.Response(200, json={"data": {"total": total, "diff": rows}})
    return httpx.MockTransport(handler)


def test_get_instruments_stock():
    rows = [
        {"f12": "600000", "f14": "浦发银行", "f6": 100000.0, "f3": 1.5},
    ]
    client = FreeSourceClient(transport=_em_clist_transport(rows, total=1))
    items = client.exchanges.get_instruments("SH", instrument_type="stock")
    assert items and items[0]["symbol"] == "600000.SH"
    assert items[0]["name"] == "浦发银行"
    assert items[0]["code"] == "600000"
    assert items[0]["exchange"] == "SH"
    assert items[0]["type"] == "stock"
    assert "ext" in items[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_get_instruments_stock -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement get_instruments**

Replace `_Exchanges.get_instruments` body:

```python
    def get_instruments(self, exchange, instrument_type="stock"):
        ex = exchange.upper()
        # 东财 clist 的 fs 过滤器:按交易所 × 品种
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
        fields = ("f12,f13,f14,f3,f6")  # code, market, name, change_pct, amount
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
            except Exception:  # noqa: BLE001
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_get_instruments_stock -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 exchanges.get_instruments(东财 clist 翻页)"
```

---

## Task 5: klines 日K + 分钟K(batch/get,as_dataframe 两态)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Klines.batch`/`get`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
def _em_kline_transport(klines_str_by_secid):
    """klines_str_by_secid: {"1.600000": "2026-07-01,9.3,9.4,9.5,9.2,1000,9300.0,1.5"}"""
    def handler(request):
        secid = request.url.params.get("secid")
        klt = request.url.params.get("klt")
        body = {"data": {"code": secid.split(".")[-1] if secid else "",
                         "klines": klines_str_by_secid.get(secid, [])}}
        return httpx.Response(200, json={"rc": 0, "data": body.get("data")})
    return httpx.MockTransport(handler)


def test_klines_batch_daily_as_dataframe_true():
    transport = _em_kline_transport({"1.600000": [
        "2026-07-01,9.30,9.40,9.50,9.20,1000,9300.00,1.50",
        "2026-07-02,9.40,9.45,9.60,9.35,1200,11220.00,0.53",
    ]})
    client = FreeSourceClient(transport=transport)
    raw = client.klines.batch(["600000.SH"], period="1d", count=2,
                              adjust="qfq", as_dataframe=True)
    assert isinstance(raw, dict)
    df = raw["600000.SH"]
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2
    assert float(df.iloc[0]["close"]) == 9.40


def test_klines_batch_daily_as_dataframe_false():
    transport = _em_kline_transport({"1.600000": ["2026-07-01,9.30,9.40,9.50,9.20,1000,9300.00,1.50"]})
    client = FreeSourceClient(transport=transport)
    raw = client.klines.batch(["600000.SH"], period="1d", count=1, as_dataframe=False)
    assert isinstance(raw, dict)
    rec = raw["600000.SH"][0]
    assert rec["date"] == "2026-07-01"
    assert rec["close"] == 9.40


def test_klines_get_single():
    transport = _em_kline_transport({"1.600000": ["2026-07-01,9.30,9.40,9.50,9.20,1000,9300.00,1.50"]})
    client = FreeSourceClient(transport=transport)
    recs = client.klines.get("600000.SH", period="1d", count=1, as_dataframe=False)
    assert recs and recs[0]["symbol"] == "600000.SH"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -k klines -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement batch + get**

Add a private parser and replace `_Klines.batch`/`get` bodies:

```python
    # 在 _Klines 类内,作为方法
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
        except Exception:  # noqa: BLE001
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
            except Exception as e:  # noqa: BLE001
                logger.warning("free klines %s failed: %s", sym, e)
                rows = []
            if as_dataframe:
                out[sym] = pd.DataFrame(rows, columns=[
                    "symbol", "date", "open", "high", "low", "close", "volume", "amount"]) if rows else pd.DataFrame()
            else:
                out[sym] = rows
        return out

    def get(self, symbol, period="1d", count=250, adjust="none",
            start_time=None, end_time=None, as_dataframe=False, show_progress=False):
        d = self.batch([symbol], period=period, count=count, adjust=adjust, as_dataframe=as_dataframe)
        return d.get(symbol)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -k klines -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 klines 日K/分钟K batch+get"
```

---

## Task 6: klines.intraday / intraday_batch(腾讯分时)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Klines.intraday`/`intraday_batch`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
def _tx_minute_transport(data_by_code):
    """data_by_code: {"sh600000": ["0930 8.69 4253 3695857.00", ...]}"""
    def handler(request):
        code = request.url.params.get("code")
        arr = data_by_code.get(code, [])
        return httpx.Response(200, json={"code": 0, "data": {code: {"data": {"data": arr}}}})
    return httpx.MockTransport(handler)


def test_intraday_single():
    transport = _tx_minute_transport({"sh600000": ["0930 8.69 4253 3695857.00", "0931 8.71 20774 18047793.00"]})
    client = FreeSourceClient(transport=transport)
    recs = client.klines.intraday("600000.SH", as_dataframe=False)
    assert recs and recs[0]["price"] == 8.69
    assert recs[1]["time"] == "0931"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_intraday_single -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement intraday + intraday_batch**

Replace `_Klines.intraday`/`intraday_batch` bodies:

```python
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
        except Exception:  # noqa: BLE001
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_intraday_single -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 intraday(腾讯分时)"
```

---

## Task 7: quotes.get / get_by_universes(新浪批量 + 东财 clist)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Quotes`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
def _sina_hq_transport(raw_by_sina):
    """raw_by_sina: {"sh600000": 'var hq_str_sh600000="浦发银行,8.690,8.700,...";'}"""
    def handler(request):
        # list= 可能多只;返回拼接
        list_param = request.url.params.get("list", "")
        body = ""
        for s in list_param.split(","):
            if s in raw_by_sina:
                body += raw_by_sina[s] + "\n"
        return httpx.Response(200, text=body,
                              headers={"Content-Type": "text/html; charset=gbk"})
    return httpx.MockTransport(handler)


_SINA_600000 = (
    'var hq_str_sh600000="浦发银行,8.690,8.700,8.750,8.820,8.660,8.750,8.760,'
    '22910251,200620309.000,53000,8.750,115800,8.740,222600,8.730,176400,8.720,'
    '407500,8.710,220000,8.760,44900,8.770,163400,8.780,192200,8.790,272000,'
    '8.800,2026-07-03,10:13:29,00,0.050,0.57,8.820,8.660,8.73/22910251/200620309,'
    '22910251,196767,1.84,5.78,8.82,8.66,1.84,2907.60,2907.60,0.39,9.57,7.83,2.05,'
    '11563,8.76,4.07,5.81,0.17,19676.7350,0.0000,0,GP-A,20260703101329";'
)


def test_quotes_get_as_dataframe_false():
    transport = _sina_hq_transport({"sh600000": _SINA_600000})
    client = FreeSourceClient(transport=transport)
    resp = client.quotes.get(["600000.SH"], as_dataframe=False)
    assert resp and resp[0]["symbol"] == "600000.SH"
    assert resp[0]["name"] == "浦发银行"
    assert resp[0]["last_price"] == 8.750
    assert resp[0]["prev_close"] == 8.700
    assert resp[0]["open"] == 8.690
    assert resp[0]["high"] == 8.820
    assert resp[0]["low"] == 8.660
    assert resp[0]["volume"] == 22910251
    assert resp[0]["amount"] == 200620309.0
    assert resp[0]["ext"]["change_amount"] == 0.050
    assert resp[0]["ext"]["change_pct"] == 0.57
    assert resp[0]["ext"]["turnover_rate"] == 1.84


def test_quotes_get_as_dataframe_true():
    transport = _sina_hq_transport({"sh600000": _SINA_600000})
    client = FreeSourceClient(transport=transport)
    df = client.quotes.get(["600000.SH"], as_dataframe=True)
    assert "symbol" in df.columns
    assert "last_price" in df.columns
    assert "ext.change_pct" in df.columns
    assert "ext.name" in df.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -k quotes -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement _Quotes with sina parser**

Replace `_Quotes` class body:

```python
class _Quotes:
    def __init__(self, client: "FreeSourceClient"):
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
        # 新浪字段: 0=name 1=today_open 2=prev_close 3=current 4=high 5=low
        # 6=bid? 7=ask? 8=volume(股) 9=amount(元)
        # 10~19=买5量价 20~29=卖5量价 30=date 31=time
        # 32=change 33=change_pct 34=high2? 35=low2? ... 38=turnover_rate
        name = parts[0]
        open_p = _f(parts, 1)
        prev_close = _f(parts, 2)
        last_price = _f(parts, 3)
        high = _f(parts, 4)
        low = _f(parts, 5)
        volume = _f(parts, 8)
        amount = _f(parts, 9)
        change_amount = _f(parts, 32)
        change_pct = _f(parts, 33)
        turnover_rate = _f(parts, 38)
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
            # 用 clist 的实时字段 f2/f3/f4/f5/f6/f15/f16/f17/f18 + f12(代码)/f14(名)/f13(市场)
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
            except Exception:  # noqa: BLE001
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
```

Add module-level helpers near the top (after `_sina_symbols_param`):

```python
def _f(d, key, default=None):
    """安全取值并转 float;东财 '-' 表示无数据。"""
    v = d.get(key) if isinstance(d, dict) else (d[key] if isinstance(d, (list, tuple)) and isinstance(key, int) and key < len(d) else None)
    if v in (None, "-", ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _sina_timestamp(parts, date_idx, time_idx) -> int | None:
    """新浪 date(2026-07-03)+time(10:13:29) → ms epoch。无则 None。"""
    try:
        d = parts[date_idx]
        t = parts[time_idx]
        from datetime import datetime
        dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py -k quotes -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 quotes(新浪批量+东财 clist)"
```

---

## Task 8: depth.batch(新浪五档)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Depth.batch`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
def test_depth_batch():
    transport = _sina_hq_transport({"sh600000": _SINA_600000})
    client = FreeSourceClient(transport=transport)
    d = client.depth.batch(["600000.SH"])
    assert "600000.SH" in d
    entry = d["600000.SH"]
    # 新浪买5(parts 10~19): 量,价 交替;卖5(parts 20~29)
    assert entry["bid_volumes"][0] == 53000
    assert entry["ask_volumes"][0] == 220000
    assert isinstance(entry["timestamp"], (int, float))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_depth_batch -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement depth.batch**

Replace `_Depth.batch` body:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_depth_batch -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 depth.batch(新浪五档)"
```

---

## Task 9: financials(东财 F10)

**Files:**
- Modify: `backend/app/tickflow/free_adapter.py`(`_Financials`)
- Test: `backend/tests/tickflow/test_free_adapter_mapping.py`(追加)

- [ ] **Step 1: Append the failing test**

```python
def _em_datacenter_transport(rows_by_report):
    """rows_by_report: {"RPT_LICO_FN_CPD": [{"SECURITY_CODE":"600000",...}]}"""
    def handler(request):
        rn = request.url.params.get("reportName")
        rows = rows_by_report.get(rn, [])
        return httpx.Response(200, json={"result": {"data": rows, "pages": 1}})
    return httpx.MockTransport(handler)


def test_financials_metrics():
    transport = _em_datacenter_transport({"RPT_F10_FINANCE_DUPONT": [
        {"SECURITY_CODE": "600000", "REPORT_DATE": "2026-03-31", "JROA": 0.0045,
         "SALE_NPR": 39.01, "PARENT_NETPROFIT": 17861000000},
    ]})
    client = FreeSourceClient(transport=transport)
    data = client.financials.metrics(["600000.SH"], latest=True)
    assert "600000.SH" in data
    rec = data["600000.SH"][0]
    assert rec["SECURITY_CODE"] == "600000"
    assert rec["symbol"] == "600000.SH"  # financial_sync 期待 record 上有 symbol
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_financials_metrics -v`
Expected: FAIL — NotImplementedError。

- [ ] **Step 3: Implement _Financials**

Replace `_Financials` class body:

```python
class _Financials:
    # 东财 reportName 映射
    _REPORTS = {
        "metrics": "RPT_F10_FINANCE_DUPONT",       # 杜邦,含 ROE/净利率/周转率
        "income": "RPT_LICO_FN_CPD",               # 利润表
        "balance_sheet": "RPT_DMSK_FN_BALANCE",    # 资产负债表
        "cash_flow": "RPT_LICO_FN_CFR",            # 现金流量表
    }

    def __init__(self, client: "FreeSourceClient"):
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
            except Exception:  # noqa: BLE001
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_mapping.py::test_financials_metrics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/free_adapter.py backend/tests/tickflow/test_free_adapter_mapping.py
git commit -m "feat: free_adapter 实现 financials(东财 F10)"
```

---

## Task 10: client.py 路由到 FreeSourceClient

**Files:**
- Modify: `backend/app/tickflow/client.py`
- Test: `backend/tests/tickflow/test_free_adapter_client_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/tickflow/test_free_adapter_client_routing.py
from app.tickflow import client as tf_client
from app.tickflow.free_adapter import FreeSourceClient
from app import secrets_store


def test_get_client_returns_free_source_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "get_data_backend", lambda: "free_source")
    tf_client.reset_clients()
    c = tf_client.get_client()
    assert isinstance(c, FreeSourceClient)


def test_get_client_returns_tickflow_when_default(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "get_data_backend", lambda: "tickflow")
    tf_client.reset_clients()
    # 无 key → TickFlow.free()(SDK 对象),不是 FreeSourceClient
    c = tf_client.get_client()
    assert not isinstance(c, FreeSourceClient)


def test_paid_realtime_client_free_source(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "get_data_backend", lambda: "free_source")
    tf_client.reset_clients()
    c = tf_client.get_paid_realtime_client()
    assert isinstance(c, FreeSourceClient)


def test_current_mode_free_source(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "get_data_backend", lambda: "free_source")
    tf_client.reset_clients()
    assert tf_client.current_mode() == "free_source"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_client_routing.py -v`
Expected: FAIL — 仍返回 TickFlow/None。

- [ ] **Step 3: Modify client.py**

In `backend/app/tickflow/client.py`, add import after `from app import secrets_store`:

```python
from app.tickflow.free_adapter import FreeSourceClient
```

Add a helper after `_base_url`:

```python
def _is_free_source_mode() -> bool:
    """是否启用免费公开数据源 adapter。"""
    return secrets_store.get_data_backend() == "free_source"
```

Replace `get_client` body:

```python
def get_client() -> TickFlow | FreeSourceClient:
    """同步客户端。能力探测、盘后管道用。"""
    global _sync_client
    if _sync_client is None:
        if _is_free_source_mode():
            _sync_client = FreeSourceClient()
        elif _should_use_free_server():
            _sync_client = TickFlow.free()
        else:
            key = secrets_store.get_tickflow_key()
            _sync_client = TickFlow(api_key=key, base_url=_base_url())
    return _sync_client
```

Replace `get_async_client` body:

```python
def get_async_client() -> AsyncTickFlow | FreeSourceClient:
    """异步客户端。FastAPI 请求路径上用。

    免费源 adapter 为同步实现(项目无异步调用方);返回同一类型对象。
    """
    global _async_client
    if _async_client is None:
        if _is_free_source_mode():
            _async_client = FreeSourceClient()
        elif _should_use_free_server():
            _async_client = AsyncTickFlow.free()
        else:
            key = secrets_store.get_tickflow_key()
            _async_client = AsyncTickFlow(api_key=key, base_url=_base_url())
    return _async_client
```

Replace `get_paid_realtime_client` body:

```python
def get_paid_realtime_client() -> TickFlow | FreeSourceClient | None:
    """实时行情专用客户端。

    免费源模式下返回 FreeSourceClient(实时行情全部走公开源)。
    """
    global _paid_realtime_client
    if _is_free_source_mode():
        if _paid_realtime_client is None:
            _paid_realtime_client = FreeSourceClient()
        return _paid_realtime_client
    key = secrets_store.get_tickflow_key()
    if not key:
        return None
    if _paid_realtime_client is None:
        _paid_realtime_client = TickFlow(api_key=key, base_url=_base_url())
    return _paid_realtime_client
```

Replace `current_mode` body:

```python
def current_mode() -> str:
    """供 UI 显示当前模式。四态: none / free / api_key / free_source。"""
    from app import secrets_store
    if secrets_store.get_data_backend() == "free_source":
        return "free_source"
    if not secrets_store.get_tickflow_key():
        return "none"
    from app.tickflow.policy import base_tier_name
    tier = base_tier_name()
    if tier in ("none", "free"):
        return "free" if tier == "free" else "none"
    return "api_key"
```

Replace `current_endpoint` body:

```python
def current_endpoint() -> str:
    """返回当前显示用的端点 URL。"""
    from app import secrets_store
    if secrets_store.get_data_backend() == "free_source":
        return "免费公开数据源(东财/新浪/腾讯)"
    if _should_use_free_server():
        return FREE_ENDPOINT
    base = _base_url()
    if base:
        return base.rstrip("/")
    return PAID_ENDPOINT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/tickflow/test_free_adapter_client_routing.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/tickflow/client.py backend/tests/tickflow/test_free_adapter_client_routing.py
git commit -m "feat: client.py 路由到 FreeSourceClient"
```

---

## Task 11: settings API 增加 data-backend 开关

**Files:**
- Modify: `backend/app/api/settings.py`
- Test: `backend/tests/tickflow/test_settings_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/tickflow/test_settings_backend.py
from fastapi.testclient import TestClient
from app.main import app


def test_get_settings_returns_data_backend():
    with TestClient(app) as c:
        r = c.get("/api/settings")
        assert r.status_code == 200
        assert "data_backend" in r.json()


def test_set_data_backend():
    with TestClient(app) as c:
        r = c.post("/api/settings/data-backend", json={"backend": "free_source"})
        assert r.status_code == 200
        body = r.json()
        assert body["data_backend"] == "free_source"
        assert body["mode"] == "free_source"
        # 切回
        r2 = c.post("/api/settings/data-backend", json={"backend": "tickflow"})
        assert r2.status_code == 200
        assert r2.json()["data_backend"] == "tickflow"


def test_set_data_backend_invalid():
    with TestClient(app) as c:
        r = c.post("/api/settings/data-backend", json={"backend": "bogus"})
        assert r.status_code in (400, 422)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/tickflow/test_settings_backend.py -v`
Expected: FAIL — 端点不存在(404)。

- [ ] **Step 3: Modify settings.py**

In `backend/app/api/settings.py`, add `data_backend` to the `get_settings` return dict (inside the returned dict, near `has_tickflow_key`):

```python
        "data_backend": secrets_store.get_data_backend(),
```

Add the new endpoint (after `clear_tickflow_key`):

```python
class DataBackendIn(BaseModel):
    backend: str  # "tickflow" / "free_source"


@router.post("/data-backend")
def set_data_backend(req: DataBackendIn) -> dict:
    """切换数据后端: tickflow(默认) / free_source(免费公开源 adapter)。

    切换后重置客户端 + 强制重新探测能力。
    """
    from app.tickflow.policy import detect_capabilities
    try:
        secrets_store.set_data_backend(req.backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tf_client.reset_clients()
    capset = detect_capabilities(force=True)
    from app.tickflow.policy import tier_label
    return {
        "data_backend": secrets_store.get_data_backend(),
        "mode": tf_client.current_mode(),
        "tier_label": tier_label(),
        "capabilities": capset.to_dict(),
    }
```

Ensure `HTTPException` is imported at the top of `settings.py` (add to the existing fastapi import if not present):

```python
from fastapi import APIRouter, HTTPException
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/tickflow/test_settings_backend.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/settings.py backend/tests/tickflow/test_settings_backend.py
git commit -m "feat: settings API 增加 data-backend 开关"
```

---

## Task 12: 全量测试 + lint + 类型

**Files:**
- (无新文件,跑全量校验)

- [ ] **Step 1: 确认测试基础设施**

确保 `backend/tests/tickflow/__init__.py` 存在(空文件即可)。若不存在:

```bash
touch backend/tests/tickflow/__init__.py
```

- [ ] **Step 2: 跑免费源相关全部测试**

Run: `cd backend && uv run pytest tests/tickflow/ -v`
Expected: 全部 PASS(mapping 14 + capabilities 1 + routing 4 + settings 3 + secrets 3 = 25 左右)。

- [ ] **Step 3: 跑全量测试,确认无回归**

Run: `cd backend && uv run pytest -x`
Expected: 全部 PASS(无回归)。

- [ ] **Step 4: lint**

Run: `cd backend && uv run ruff check app/tickflow/free_adapter.py app/tickflow/client.py app/tickflow/policy.py app/api/settings.py app/secrets_store.py tests/tickflow/`
Expected: 无错误(若有自动可修,`ruff check --fix` 后再跑)。

- [ ] **Step 5: 类型检查**

Run: `cd backend && uv run mypy app/tickflow/free_adapter.py app/tickflow/client.py`
Expected: 无错误(允许已有 baseline 警告,不引入新错误)。

- [ ] **Step 6: Commit 收尾**

```bash
git add backend/tests/tickflow/__init__.py
git commit -m "test: 免费源 adapter 全量测试通过 + lint"
```

---

## 自审清单(已完成)

- **Spec 覆盖**:7 类接口(instruments/daily/minute/intraday/quotes/depth/financials)分别由 Task 4/5/5/6/7/8/9 覆盖;capset 与档位 Task 2;开关 Task 1+11;client 路由 Task 10;ex_factors 返回空 Task 3 骨架已含;universes.list Task 3 骨架已含;WebSocket 不实现(spec 已声明)。
- **占位符扫描**:无 TBD/TODO;每个代码步骤含完整代码。
- **类型一致**:`FreeSourceClient`/`_Klines`/`_Quotes`/`_Depth`/`_Financials`/`_Exchanges`/`_Universes` 命名贯穿一致;`get_data_backend`/`set_data_backend` 在 secrets_store/settings/测试中一致;`_is_free_source_mode` 在 client.py 内部一致。
- **测试依赖**:用 httpx 内置 `MockTransport`(零新依赖),spec 中提到的 respx 改用 MockTransport 等价替代,已标注。
