"""场景一：版本历史留存、容量淘汰与一键回退。"""
from __future__ import annotations

from app.manager import ConfigManager
from app.sources import make_api_payload


def _values(name: str, conn: int | None = None) -> dict:
    values = {"app_name": name, "db_password": "pw"}
    if conn is not None:
        values["max_connections"] = conn
    return values


def _push(manager: ConfigManager, name: str, conn: int | None = None):
    return manager.reload_from(
        [make_api_payload(values=_values(name, conn))]
    )


def test_every_successful_switch_leaves_history_entry(manager):
    _push(manager, "second-app", 200)
    _push(manager, "third-app", 300)
    history = manager.history()
    assert [h["version"] for h in history] == [1, 2, 3]
    entry_v2 = history[1]
    assert entry_v2["sources"] == ["api"]
    assert entry_v2["created_at"]
    assert entry_v2["rolled_back_from"] is None
    print(f"[输入] 连续切换 v1->v2->v3 => [依据] 历史版本={[h['version'] for h in history]}，v2 来源={entry_v2['sources']} 时间={entry_v2['created_at']}")


def test_history_cap_evicts_oldest_but_never_current(manager):
    limited = ConfigManager(
        manager.spec,
        history_limit=3,
        initial_layers=[make_api_payload(values=_values("init-app"))],
    )
    for i in range(2, 7):
        result = _push(limited, f"app-{i}", 100 + i)
        assert result.ok
    versions = [h["version"] for h in limited.history()]
    # 容量 3：只保留最近三份；当前生效版本（v6）必在其中
    assert versions == [4, 5, 6]
    assert limited.current().version == 6
    print(f"[输入] history_limit=3，连续 6 次切换 => [依据] 留存版本={versions}，当前 v6 未被淘汰")


def test_current_version_protected_from_eviction(spec):
    """当前生效版本即使“最旧”也不能被容量淘汰。

    通过回退制造“当前版本号小于后续历史版本号”的场景。
    """
    m = ConfigManager(
        spec,
        history_limit=3,
        initial_layers=[make_api_payload(values=_values("v1-app"))],
    )
    _push(m, "v2-app", 2)
    _push(m, "v3-app", 3)
    # 回退到 v1 => 生成 v4（内容同 v1），此时历史 [v2,v3,v4]，v4 为当前
    back = m.rollback(1)
    assert back.ok and back.version == 4
    assert back.rolled_back_from == 1
    versions = [h["version"] for h in m.history()]
    assert versions == [2, 3, 4]
    # 再切换两次：淘汰发生时当前 v5、v6 都必须保留
    _push(m, "v5-app", 5)
    _push(m, "v6-app", 6)
    versions = [h["version"] for h in m.history()]
    assert m.current().version in versions
    assert len(versions) == 3
    print(f"[输入] 回退后继续切换 => [依据] 留存={versions}，当前 v{m.current().version} 受保护")


def test_rollback_restores_values_and_creates_new_version(manager):
    _push(manager, "app-a", 111)
    _push(manager, "app-b", 222)
    assert manager.current().values["app_name"] == "app-b"

    result = manager.rollback(2)  # 回到 app-a
    assert result.ok
    new = manager.current()
    assert new.version == 4  # 回退生成新版本号，不重用旧号
    assert new.values["app_name"] == "app-a"
    assert new.values["max_connections"] == 111
    meta = manager.history()[-1]
    assert meta["rolled_back_from"] == 2
    print(f"[输入] 从 v3 回退到 v2 => [依据] 生成 v{new.version}，值恢复为 app-a/111，记录 rolled_back_from=2")


def test_rollback_validates_like_normal_refresh_and_keeps_current_on_failure(
    manager, monkeypatch
):
    """回退也要过标准校验：历史层合法时可回退；目标不存在时停在当前。"""
    _push(manager, "app-a", 111)
    current_before = manager.current()

    missing = manager.rollback(999)
    assert missing.ok is False
    assert missing.error_type == "VersionNotFoundError"
    assert manager.current() is current_before

    # 篡改一份历史层使其非法后回退，应被标准校验挡下
    entry = manager._history[1]
    object.__setattr__(
        entry,
        "layers",
        (make_api_payload(values={"app_name": "app-a", "db_password": "pw", "max_connections": 0}),),
    )
    rejected = manager.rollback(2)
    assert rejected.ok is False
    assert rejected.error_type == "ConfigRangeError"
    assert manager.current() is current_before
    print(f"[输入] 回退目标 v2 被改成越界值 => [依据] {rejected.error_type}: {rejected.reason}；仍停在 v{current_before.version}")


def test_failed_switch_is_not_recorded_in_history(manager):
    _push(manager, "app-a", 111)
    bad = manager.reload_from(
        [make_api_payload(values={"app_name": "app-b", "db_password": "pw", "max_connections": 0})]
    )
    assert bad.ok is False
    versions = [h["version"] for h in manager.history()]
    assert versions == [1, 2]
    print(f"[输入] 一次非法切换 => [依据] 历史仍为 {versions}，失败不留存")
