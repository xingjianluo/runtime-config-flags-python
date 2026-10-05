"""HTTP 端到端：历史版本/回退接口、降级时各读接口的 503 与诊断、恢复。"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.manager import ConfigManager
from app.schema import default_spec
from app.web import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps({"values": {"app_name": "file-app", "db_password": "fp"}})
    )
    # 让 /refresh 重新读取文件 + 环境变量时不受外部环境干扰
    for key in list(os.environ):
        if key.startswith(("APP_VALUES_", "APP_FLAGS_")):
            monkeypatch.delenv(key, raising=False)
    manager = ConfigManager(default_spec(), file_path=str(cfg))
    app.state.manager = manager
    with TestClient(app) as c:
        yield c, manager, cfg


def test_versions_endpoint_lists_history(client):
    c, _, _ = client
    r = c.get("/versions")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "active"
    assert body["history_limit"] == 10
    assert body["current_version"] == body["versions"][-1]["version"]
    print(f"[输入] GET /versions => [依据] {len(body['versions'])} 份历史，当前 v{body['current_version']}")


def test_rollback_endpoint_flow(client):
    c, _, _ = client
    c.post("/refresh", json={"values": {"app_name": "second-app", "db_password": "p2"}})
    c.post("/refresh", json={"values": {"app_name": "third-app", "db_password": "p3"}})
    assert c.get("/config").json()["values"]["app_name"] == "third-app"

    r = c.post("/versions/1/rollback")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rolled_back_from"] == 1
    assert c.get("/config").json()["values"]["app_name"] == "file-app"
    print(f"[输入] POST /versions/1/rollback => [依据] 生成新版本 v{body['version']}，值回到 v1 内容")

    missing = c.post("/versions/999/rollback")
    assert missing.status_code == 422
    assert missing.json()["error_type"] == "rollback_rejected"
    print(f"[输入] 回退不存在的 v999 => [依据] 422 rollback_rejected: {missing.json()['reason']}")


def test_health_config_flags_check_all_degrade_to_503(client):
    c, _, cfg = client
    cfg.write_text("{broken-json")
    r = c.post("/refresh", json={})
    assert r.status_code == 503
    assert r.json()["error_type"] == "config_unavailable"

    health = c.get("/health")
    assert health.status_code == 503
    h = health.json()
    assert h["state"] == "degraded"
    assert h["source"] == "file"
    assert h["kind"] == "source"
    assert "cfg.json" in h["detail"]

    for path in ("/config", "/flags"):
        resp = c.get(path)
        assert resp.status_code == 503
        assert resp.json()["state"] == "degraded"
        assert resp.json()["source"] == "file"

    check = c.post("/flags/new_checkout/check", json={"user_id": "u1"})
    assert check.status_code == 503
    assert check.json()["error_type"] == "config_unavailable"
    print("[输入] 来源损坏后访问 health/config/flags/check => [依据] 均 503 且带 source=file/kind=source，非笼统内部错误")


def test_refresh_with_multiple_issues_lists_all(client):
    c, _, _ = client
    r = c.post(
        "/refresh",
        json={
            "values": {
                "app_name": "Bad Name",
                "db_password": 12345,
                "max_connections": 0,
                "ghost_field": 1,
            }
        },
    )
    assert r.status_code == 422
    body = r.json()
    keys = {(i["key"], i["category"], i["source"]) for i in body["issues"]}
    assert ("app_name", "format", "file") in keys or ("app_name", "format", "api") in keys
    assert ("max_connections", "range", "api") in keys
    assert ("ghost_field", "unknown", "api") in keys
    # 敏感值的诊断不含真实数字
    db_issue = next(i for i in body["issues"] if i["key"] == "db_password")
    assert "12345" not in db_issue["message"]
    # 旧版本仍在
    assert c.get("/config").json()["values"]["app_name"] == "file-app"
    print(f"[输入] 一次提交含 4 类问题 => [依据] issues={body['issues']}")


def test_refresh_reports_shadowed_conflicts(client):
    c, _, _ = client
    r = c.post(
        "/refresh",
        json={"values": {"app_name": "api-app", "db_password": "p", "max_connections": 300}},
    )
    assert r.status_code == 200
    shadowed = {(s["key"], s["source"], s["winning_source"]) for s in r.json()["shadowed"]}
    assert ("app_name", "file", "api") in shadowed
    cfg_body = c.get("/config").json()
    assert cfg_body["field_origins"]["max_connections"] == "api"
    assert cfg_body["shadowed"]
    print(f"[输入] api 覆盖同名 file 值 => [依据] shadowed={r.json()['shadowed']}")


def test_recovery_after_file_fixed(client):
    c, _, cfg = client
    cfg.write_text("{bad")
    c.post("/refresh", json={})
    assert c.get("/health").status_code == 503

    cfg.write_text(
        json.dumps({"values": {"app_name": "healed-app", "db_password": "hp", "log_level": "debug"}})
    )
    r = c.post("/refresh", json={})
    assert r.status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/config").json()["values"]["app_name"] == "healed-app"
    print("[输入] 修复文件后再次刷新 => [依据] 健康检查与读取全部恢复 200 active")
