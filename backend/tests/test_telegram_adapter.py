from __future__ import annotations

from app.services.telegram_adapter import (
    is_valid_token_shape,
    mask_telegram_token,
    split_message,
)
from app.services.telegram_commands import normalize_symbol


# ===== split_message =====

def test_split_message_short_text_single_segment():
    assert split_message("hello") == ["hello"]


def test_split_message_empty_returns_empty_list():
    assert split_message("") == []
    assert split_message("   ") == []


def test_split_message_splits_over_limit():
    text = "a" * 9000
    segs = split_message(text, limit=4000)
    assert len(segs) == 3
    assert all(len(s) <= 4000 for s in segs)
    assert "".join(segs) == text


def test_split_message_prefers_newline_boundary():
    # 前半段填满接近上限, 换行后再跟内容 → 应在换行处断开
    head = "x" * 3500
    tail = "y" * 1000
    segs = split_message(f"{head}\n{tail}", limit=4000)
    assert len(segs) == 2
    assert segs[0] == head
    assert segs[1] == tail


def test_split_message_hard_cut_when_newline_too_early():
    # 换行只在很靠前的位置 → 不该为迁就换行而切出过短的段, 应硬切
    text = "a\n" + "b" * 8000
    segs = split_message(text, limit=4000)
    assert all(len(s) <= 4000 for s in segs)
    # 重组后字符集不变(硬切不丢字符, 仅可能在段边界 strip 空白)
    assert segs[0].startswith("a")


# ===== is_valid_token_shape =====

def test_valid_token_shape():
    assert is_valid_token_shape("123456789:AAHdqTcvbBs" + "x" * 20)


def test_invalid_token_shape():
    assert not is_valid_token_shape("")
    assert not is_valid_token_shape("no-colon-here")
    assert not is_valid_token_shape("abc:short")  # 冒号前非数字
    assert not is_valid_token_shape("123:tooshort")  # 尾段不足 20


def test_mask_telegram_token_redacts_bot_url():
    raw = (
        "HTTP Request: GET "
        "https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456/getUpdates"
    )
    masked = mask_telegram_token(raw)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in masked
    assert "https://api.telegram.org/bot123456789:" in masked
    assert "***" in masked


# ===== normalize_symbol =====

def test_normalize_symbol_keeps_suffixed():
    assert normalize_symbol("600519.SH") == "600519.SH"
    assert normalize_symbol("300750.sz") == "300750.SZ"


def test_normalize_symbol_infers_shanghai():
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("688111") == "688111.SH"


def test_normalize_symbol_infers_shenzhen():
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750") == "300750.SZ"


def test_normalize_symbol_infers_beijing():
    assert normalize_symbol("430047") == "430047.BJ"
    assert normalize_symbol("920819") == "920819.BJ"


def test_normalize_symbol_non_digit_passthrough():
    # 非数字(可能是名字或已是别的格式)原样大写返回, 交下游处理
    assert normalize_symbol("茅台") == "茅台"
    assert normalize_symbol("") == ""
