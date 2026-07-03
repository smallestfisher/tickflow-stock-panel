"""settings API 的 data-backend 开关测试。

不启动完整 app lifespan(会触发网络能力探测 + 后台调度器, 在测试环境会挂起),
也绕过 auth 中间件 —— 只把 settings router 挂到一个裸 app 上, 并隔离 secrets 路径。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api import settings as settings_api
from app.tickflow import client as tf_client
from app.tickflow import policy


def _make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    # 隔离 data_dir: detect_capabilities(force=True) 会写 capabilities.json,
    # 真实路径在测试沙箱不可写。
    monkeypatch.setattr(policy.settings, "data_dir", tmp_path)
    tf_client.reset_clients()
    app = FastAPI()
    app.state.capabilities = None
    app.include_router(settings_api.router)
    return TestClient(app)


def test_get_settings_returns_data_backend(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch)
    r = c.get("/api/settings")
    assert r.status_code == 200
    assert "data_backend" in r.json()
    assert r.json()["data_backend"] == "tickflow"  # 默认


def test_set_data_backend(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch)
    r = c.post("/api/settings/data-backend", json={"backend": "free_source"})
    assert r.status_code == 200
    body = r.json()
    assert body["data_backend"] == "free_source"
    assert body["mode"] == "free_source"
    # 切回
    r2 = c.post("/api/settings/data-backend", json={"backend": "tickflow"})
    assert r2.status_code == 200
    assert r2.json()["data_backend"] == "tickflow"


def test_set_data_backend_invalid(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch)
    r = c.post("/api/settings/data-backend", json={"backend": "bogus"})
    assert r.status_code in (400, 422)
