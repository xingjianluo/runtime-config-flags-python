"""HTTP 端到端：脱敏、刷新接口、开关判定响应码与错误体。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# 直接复用应用真实装配（默认文件+环境变量），保证入口行为被覆盖
import main

from .conftest import case_log


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main.app)


def test_config_masks_sensitive(client):
    r = client.get("/config")
    body = r.json()
    case_log.info("GET /config 脱敏检查 db.password=%r", body["values"]["db.password"])
    assert body["values"]["db.password"] == "***redacted***"
    # 任何字段值与来源信息中都不得出现真实密码
    assert "change-me" not in str(body)


def test_refresh_endpoint_success_and_rollback(client):
    r = client.post("/admin/refresh", json={"http.timeout_ms": 7777})
    assert r.status_code == 200
    case_log.info("刷新输入 http.timeout_ms=7777 -> %s", r.json())
    assert client.get("/config").json()["values"]["http.timeout_ms"] == 7777

    r = client.post("/admin/refresh", json={"sample.ratio": 5.0})
    assert r.status_code == 422
    body = r.json()
    case_log.info("非法刷新被拒 reasons=%r kept=%s",
                  [(x["code"], x["field"]) for x in body["error"]["reasons"]],
                  body["kept_version"])
    # 回退后仍是上一版合法配置
    assert client.get("/config").json()["values"]["http.timeout_ms"] == 7777


def test_sensitive_via_api_never_leaked(client):
    r = client.post("/admin/refresh", json={"db.password": "topsecret"})
    assert r.status_code == 200
    body = client.get("/config").json()
    case_log.info("敏感项经 API 设置后输出=%r", body["values"]["db.password"])
    assert body["values"]["db.password"] == "***redacted***"
    assert "topsecret" not in str(body)
    client.post("/admin/refresh")  # 还原到文件/环境


def test_flag_endpoints(client):
    r = client.get("/flags/dark_mode", headers={"X-User-Id": "u1"})
    assert r.json()["enabled"] is True
    r = client.get("/flags/ai_search", headers={"X-User-Id": "u1"})
    case_log.info("判定输入 ai_search/u1 -> %s", r.json()["reason"])
    assert r.json()["enabled"] is False  # rollout=0

    assert client.get("/flags/nope").status_code == 404
    assert client.get("/flags/legacy_export").status_code == 410

    r = client.post("/flags/check", json={
        "user_id": "u9", "groups": ["beta"],
        "flags": [{"key": "new_checkout"}, {"key": "ghost"}]})
    assert r.status_code == 207
    assert r.json()["results"][0]["reason"] == "group:beta"
    assert r.json()["errors"][0]["code"] == "undefined_flag"
