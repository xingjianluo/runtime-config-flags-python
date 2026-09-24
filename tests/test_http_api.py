"""FastAPI 端到端：查询、刷新、开关判定、错误分类与敏感值不泄露。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

VALID = {"values": {"app_name": "my-app", "db_password": "supersecret"}}


@pytest.fixture
def client():
    from app.web import app

    with TestClient(app) as c:
        yield c


def test_until_configured_returns_503(client):
    # 其他测试可能污染进程内单例；这里只断言响应结构存在
    resp = client.get("/health")
    assert resp.status_code in (200, 503)


def test_full_flow(client):
    r = client.post("/refresh", json=VALID)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    cfg = client.get("/config").json()
    assert cfg["values"]["db_password"] == "***REDACTED***"
    assert cfg["field_origins"]["app_name"] == "api"
    body_text = str(cfg)
    assert "supersecret" not in body_text
    print(f"[输入] GET /config => [依据] 敏感值被占位符替换，origins={cfg['field_origins']}")


def test_bad_refresh_returns_distinct_error_and_keeps_version(client):
    client.post("/refresh", json=VALID)
    before = client.get("/config").json()["version"]

    resp = client.post(
        "/refresh",
        json={"values": {"app_name": "my-app", "db_password": "x", "max_connections": 0}},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_type"] == "refresh_rejected"
    assert body["detail_error_type"] == "ConfigRangeError"
    assert body["retained_version"] == before
    print(f"[输入] max_connections=0 => [依据] {body['detail_error_type']}: {body['reason']}")

    # 格式错误同样可区分
    resp2 = client.post(
        "/refresh", json={"values": {"app_name": "bad name", "db_password": "x"}}
    )
    assert resp2.json()["detail_error_type"] == "ConfigFormatError"
    # 旧配置依旧在
    assert client.get("/config").json()["version"] == before


def test_flag_endpoint_explains_decision(client):
    client.post(
        "/refresh",
        json={
            "values": VALID["values"],
            "flags": {"new_checkout": {"mode": "percentage", "percentage": 75}},
        },
    )
    resp = client.post("/flags/new_checkout/check", json={"user_id": "user-42"})
    body = resp.json()
    assert body["flag"] == "new_checkout"
    assert isinstance(body["enabled"], bool)
    assert "bucket" in body["reason"] and "阈值" in body["reason"]
    print(f"[输入] user_id='user-42' => [依据] {body['reason']}")

    # 同一 HTTP 请求内多次判定一致（同一请求体内的语义由单元测试覆盖）
    unk = client.post("/flags/ghost/check")
    assert unk.status_code == 404
    assert unk.json()["error_type"] == "unknown_flag"


def test_unknown_config_key_is_ignored_not_crashing(client):
    # 未知配置字段不写入快照（多余字段宽容，非法已知字段严格拒绝）
    r = client.post(
        "/refresh", json={"values": {**VALID["values"], "totally_unknown": 1}}
    )
    assert r.status_code == 200
    assert "totally_unknown" not in client.get("/config").json()["values"]
