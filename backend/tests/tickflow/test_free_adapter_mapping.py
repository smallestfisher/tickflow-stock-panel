from app.tickflow.free_adapter import (
    FreeSourceClient,
    _secid_to_symbol,
    _sina_symbols_param,
    _symbol_to_secid,
)

import httpx


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
        {"f12": "600000", "f13": 1, "f14": "浦发银行", "f6": 100000.0, "f3": 1.5},
    ]
    client = FreeSourceClient(transport=_em_clist_transport(rows, total=1))
    items = client.exchanges.get_instruments("SH", instrument_type="stock")
    assert items and items[0]["symbol"] == "600000.SH"
    assert items[0]["name"] == "浦发银行"
    assert items[0]["code"] == "600000"
    assert items[0]["exchange"] == "SH"
    assert items[0]["type"] == "stock"
    assert "ext" in items[0]


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
