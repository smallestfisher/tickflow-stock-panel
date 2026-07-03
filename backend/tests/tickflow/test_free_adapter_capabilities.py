from app.tickflow import policy
from app.tickflow.capabilities import Cap
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
