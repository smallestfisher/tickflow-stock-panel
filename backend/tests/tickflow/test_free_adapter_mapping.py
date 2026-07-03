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
