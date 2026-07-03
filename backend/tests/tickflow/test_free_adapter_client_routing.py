from app import secrets_store
from app.tickflow import client as tf_client
from app.tickflow.free_adapter import FreeSourceClient


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
