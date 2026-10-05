"""HTTP 端到端：来源故障降级态下 health/config/flags/refresh 的可区分响应与恢复。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.manager import ConfigManager
from app.schema import default_spec
from app.web import app

VALID = {"values": {"app_name": "my-app", "db_password": "supersecret"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(VALID))
    manager = ConfigManager(default_spec(), file_path=str(cfg))
    token = app.state.manager
    app.state.manager = manager
    with TestClient(app) as c:
        yield c, cfg
    app.state.manager = token


def test_health_reports_degraded_with_fault_source(client):
    """降级态 /health 返回 503 且说清故障来源与类别。"""
    c, cfg = client
    assert c.get("/health").status_code == 200
    cfg.write_text("{ not valid json")
    r = c.post("/refresh", json={})
    assert r.status_code == 503
    body = r.json()
    assert body["error_type"] == "unavailable"
    assert body["fault_source"] == "file"
    assert body["fault_error_type"] == "ConfigSourceError"

    health = c.get("/health").json()
    assert health["status"] == "degraded"
    assert health["fault_source"] == "file"
    print(f"[HTTP 场景] 文件损坏 => [依据] /health: {health}")


def test_config_and_flags_unavailable_in_degraded_state(client):
    """降级态下配置与开关接口都是 503 unavailable，而不是 500 笼统错误。"""
    c, cfg = client
    cfg.write_text("garbage")
    c.post("/refresh", json={})

    for path in ("/config", "/flags"):
        resp = c.get(path)
        assert resp.status_code == 503
        body = resp.json()
        assert body["error_type"] == "unavailable"
        assert body["fault_source"] == "file"
    check = c.post("/flags/new_checkout/check")
    assert check.status_code == 503
    assert check.json()["error_type"] == "unavailable"
    print("[HTTP 场景] 降级态 /config、/flags、/flags/check => [依据] 全部 503 unavailable(source=file)")


def test_recovers_after_valid_refresh(client):
    """文件修复后下一次 /refresh 成功，健康检查恢复 200。"""
    c, cfg = client
    cfg.write_text("garbage")
    c.post("/refresh", json={})
    assert c.get("/health").status_code == 503

    cfg.write_text(json.dumps({"values": {"app_name": "back-app", "db_password": "pw"}}))
    r = c.post("/refresh", json={})
    assert r.status_code == 200
    assert r.json()["version"] >= 2
    health = c.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert c.get("/config").json()["values"]["app_name"] == "back-app"
    print("[HTTP 场景] 文件修复后 /refresh => [依据] 自动恢复，/health=200 status=ok")
