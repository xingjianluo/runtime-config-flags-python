"""HTTP 端到端：版本历史查询与一键回退接口。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

VALID = {"values": {"app_name": "my-app", "db_password": "p"}}


@pytest.fixture
def client():
    from app.web import app

    with TestClient(app) as c:
        yield c


def test_versions_list_refresh_and_rollback(client):
    """连续刷新产生历史；回退接口恢复旧配置并生成新版本。"""
    client.post("/refresh", json=VALID)
    client.post(
        "/refresh",
        json={
            "values": {"app_name": "my-app", "db_password": "p", "max_connections": 222}
        },
    )

    listing = client.get("/versions").json()
    versions = listing["versions"]
    assert versions[0]["current"] is True
    assert len(versions) >= 2
    target = versions[1]["version"]
    assert listing["history_limit"] >= 1
    print(f"[HTTP 版本] 历史列表: {[(v['version'], v['current']) for v in versions]}")

    r = client.post(f"/versions/{target}/rollback")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["origin_version"] == target

    cfg = client.get("/config").json()
    # 回退后 max_connections 回到默认 100（目标版本里没有该键）
    assert cfg["values"]["max_connections"] == 100
    print(f"[HTTP 版本] 回退到 v{target} => [依据] 新版本 v{body['version']}, max_connections=100")


def test_rollback_unknown_version_returns_404(client):
    r = client.post("/versions/999999/rollback")
    assert r.status_code == 404
    body = r.json()
    assert body["error_type"] == "rollback_rejected"
    assert body["detail_error_type"] == "VersionNotFound"
    print(f"[HTTP 版本] 回退不存在版本 => [依据] 404: {body['reason']}")
