import httpx

from app.tickflow.free_adapter import (
    FreeSourceClient,
    _secid_to_symbol,
    _sina_symbols_param,
    _symbol_to_secid,
)


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


def test_client_constructs_subobjects():
    c = FreeSourceClient()
    for attr in ("klines", "quotes", "depth", "financials", "exchanges", "universes"):
        assert hasattr(c, attr)
    assert c.universes.list() and c.universes.list()[0]["id"] == "CN_Equity_A"


def _em_clist_transport(rows, total=None):
    """构造一个 mock transport,对 clist 请求返回给定 rows。"""
    total = total if total is not None else len(rows)

    def handler(request):
        return httpx.Response(200, json={"data": {"total": total, "diff": rows}})

    return httpx.MockTransport(handler)


def test_get_instruments_stock():
    rows = [
        {"f12": "600000", "f13": 1, "f14": "浦发银行", "f6": 100000.0, "f3": 1.5,
         "f38": 29352000000.0, "f39": 29352000000.0},
    ]
    client = FreeSourceClient(transport=_em_clist_transport(rows, total=1))
    items = client.exchanges.get_instruments("SH", instrument_type="stock")
    assert items and items[0]["symbol"] == "600000.SH"
    assert items[0]["name"] == "浦发银行"
    assert items[0]["code"] == "600000"
    assert items[0]["exchange"] == "SH"
    assert items[0]["type"] == "stock"
    # Fix 2: ext 需带总股本/流通股本(instrument_sync._flatten_instruments 读它算市值)
    assert items[0]["ext"]["total_shares"] == 29352000000.0
    assert items[0]["ext"]["float_shares"] == 29352000000.0


def test_get_instruments_pagination():
    # 两页,每页满 pz=100 → 触发翻页
    page1 = [{"f12": f"60000{i}", "f13": 1, "f14": f"x{i}"} for i in range(100)]
    page2 = [{"f12": "600100", "f13": 1, "f14": "last"}]

    def handler(request: httpx.Request):
        pn = int(request.url.params.get("pn", "1"))
        diff = page1 if pn == 1 else page2
        total = 101
        return httpx.Response(200, json={"data": {"total": total, "diff": diff}})

    client = FreeSourceClient(transport=httpx.MockTransport(handler))
    items = client.exchanges.get_instruments("SH", instrument_type="stock")
    assert len(items) == 101
    assert items[-1]["symbol"] == "600100.SH"


def _em_kline_transport(klines_by_secid):
    """klines_by_secid: {"1.600000": ["2026-07-01,9.3,9.4,9.5,9.2,1000,9300.0,1.5"]}"""

    def handler(request: httpx.Request):
        secid = request.url.params.get("secid")
        body = {"data": {"code": secid.split(".")[-1] if secid else "",
                         "klines": klines_by_secid.get(secid, [])}}
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
    assert list(df.columns) == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
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


def test_klines_batch_minute_klt():
    transport = _em_kline_transport({"1.600000": ["2026-07-03 09:35,8.69,8.76,8.78,8.66,57973,50589982.00"]})
    client = FreeSourceClient(transport=transport)
    raw = client.klines.batch(["600000.SH"], period="1m", count=1, as_dataframe=False)
    rec = raw["600000.SH"][0]
    assert rec["date"] == "2026-07-03 09:35"
    assert rec["close"] == 8.76


def test_klines_batch_failure_isolated():
    """单只失败不影响其他标的(返回空 df/list,不抛)。"""
    transport = _em_kline_transport({"1.600000": ["2026-07-01,9.30,9.40,9.50,9.20,1000,9300.00,1.50"]})
    client = FreeSourceClient(transport=transport)
    # 000001.SZ → 0.000001 不在 transport,但应被 try 包住返回空
    raw = client.klines.batch(["600000.SH", "000001.SZ"], period="1d", count=1, as_dataframe=False)
    assert raw.get("600000.SH")
    assert "000001.SZ" in raw and raw["000001.SZ"] == []


def test_daily_forces_qfq_minute_stays_raw():
    """Fix 1: ex_factors 返回空,pipeline 不再复权,故日K必须直接返回前复权价(fqt=1);
    分钟K下游要 raw,保持 fqt=0。"""
    seen: list[tuple[str, str]] = []  # (klt, fqt)

    def handler(request: httpx.Request):
        seen.append((request.url.params.get("klt"), request.url.params.get("fqt")))
        return httpx.Response(200, json={"rc": 0, "data": {"code": "600000", "klines": []}})

    client = FreeSourceClient(transport=httpx.MockTransport(handler))
    # 日K 即使 adjust="none" 也应 fqt=1
    client.klines.batch(["600000.SH"], period="1d", count=1, adjust="none", as_dataframe=False)
    assert ("101", "1") in seen
    seen.clear()
    # 分钟K fqt=0
    client.klines.batch(["600000.SH"], period="1m", count=1, as_dataframe=False)
    assert ("1", "0") in seen


def test_ex_factors_returns_empty():
    """公开源无原始复权因子,ex_factors 返回空 dict(日K已是前复权价)。"""
    client = FreeSourceClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    assert client.klines.ex_factors(["600000.SH"]) == {}


def test_is_transient_5xx_retries():
    """Fix 4: 5xx 应被判为瞬时错误并重试(而非立即抛)。"""
    from app.tickflow.free_adapter import _is_transient
    resp = httpx.Response(503, request=httpx.Request("GET", "http://x"))
    err = httpx.HTTPStatusError("503", request=resp.request, response=resp)
    assert _is_transient(err) is True
    resp404 = httpx.Response(404, request=httpx.Request("GET", "http://x"))
    err404 = httpx.HTTPStatusError("404", request=resp404.request, response=resp404)
    assert _is_transient(err404) is False


def _tx_minute_transport(data_by_code):
    """data_by_code: {"sh600000": ["0930 8.69 4253 3695857.00", ...]}"""

    def handler(request: httpx.Request):
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


def test_intraday_count_tail():
    transport = _tx_minute_transport({"sh600000": ["0930 8.69 4253 3695857.00", "0931 8.71 20774 18047793.00", "0932 8.74 31355 27272090.19"]})
    client = FreeSourceClient(transport=transport)
    recs = client.klines.intraday("600000.SH", count=2, as_dataframe=False)
    assert len(recs) == 2
    assert recs[-1]["time"] == "0932"


def _sina_hq_transport(raw_by_sina):
    """raw_by_sina: {"sh600000": 'var hq_str_sh600000="浦发银行,8.690,8.700,...";'}"""
    def handler(request):
        list_param = request.url.params.get("list", "")
        body = ""
        for s in list_param.split(","):
            if s in raw_by_sina:
                body += raw_by_sina[s] + "\n"
        return httpx.Response(200, content=body.encode("gbk"),
                              headers={"Content-Type": "text/html; charset=gbk"})
    return httpx.MockTransport(handler)


# 实测 sina hq_str 到状态位(00)为止,无涨跌幅/换手字段。
# 字段: name,open,prev_close,last,high,low,bid,ask,vol,amount,
#        [买5量价 10~19],[卖5量价 20~29],date(30),time(31),status(32)
_SINA_600000 = (
    'var hq_str_sh600000="浦发银行,8.690,8.700,8.750,8.820,8.660,8.750,8.760,'
    '22910251,200620309.000,53000,8.750,115800,8.740,222600,8.730,176400,8.720,'
    '407500,8.710,220000,8.760,44900,8.770,163400,8.780,192200,8.790,272000,'
    '8.800,2026-07-03,10:13:29,00";'
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
    # 涨跌额/涨跌幅按 last-prev 计算(sina 不直接给)
    assert resp[0]["ext"]["change_amount"] == 0.050
    assert resp[0]["ext"]["change_pct"] == 0.57
    # sina hq_str 无换手率,置 None(enriched pipeline 另算)
    assert resp[0]["ext"]["turnover_rate"] is None


def test_quotes_get_as_dataframe_true():
    transport = _sina_hq_transport({"sh600000": _SINA_600000})
    client = FreeSourceClient(transport=transport)
    df = client.quotes.get(["600000.SH"], as_dataframe=True)
    assert "symbol" in df.columns
    assert "last_price" in df.columns
    assert "ext.change_pct" in df.columns
    assert "ext.name" in df.columns


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
    # Fix 3: financial_analyzer 按 period_end 排序 + 摘要,东财 REPORT_DATE 映射为 period_end
    assert rec["period_end"] == "2026-03-31"


# ---- Fix 4: 5xx 重试(_is_transient 正确识别 HTTPStatusError)----

def test_is_transient_5xx_httpstatuserror():
    from app.tickflow.free_adapter import _is_transient
    req = httpx.Request("GET", "http://x")
    resp = httpx.Response(503, request=req)
    err = httpx.HTTPStatusError("503", request=req, response=resp)
    assert _is_transient(err) is True
    # 4xx 不是瞬时
    resp4 = httpx.Response(403, request=req)
    err4 = httpx.HTTPStatusError("403", request=req, response=resp4)
    assert _is_transient(err4) is False


def test_http_get_retries_5xx_then_succeeds():
    """首次 500 → 重试后 200。验证 _http_get 真的重试了 5xx。"""
    from app.tickflow.free_adapter import _http_get
    calls = {"n": 0}

    def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    client = FreeSourceClient(transport=httpx.MockTransport(handler))
    r = _http_get(client._http, "https://push2.eastmoney.com/x")
    assert r.status_code == 200
    assert calls["n"] == 2  # 第一次 500,第二次成功
