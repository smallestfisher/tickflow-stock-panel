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
