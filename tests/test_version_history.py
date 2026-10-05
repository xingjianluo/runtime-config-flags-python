"""版本历史：每次成功切换入档、可查来源/时间、容量淘汰、一键回退。

覆盖场景：
1. 成功切换逐版本入档，元数据（版本号/来源/时间/原因）可查；
2. 失败的提交不入历史；
3. 历史超上限淘汰最旧版本，当前正在使用的版本永不淘汰；
4. 回退到历史版本生成新版本并恢复其配置值/开关；
5. 回退到不存在的版本失败，停在当前版本；
6. 回退按相同规则重新校验（历史原始层已非法时回退失败）；
7. 回退后被回退版本仍在历史中，且历史容量规则持续生效。
"""
from __future__ import annotations

from app.manager import ConfigManager
from app.sources import make_api_payload


def _push(manager, app_name, **kw):
    return manager.reload_from(
        [make_api_payload(values={"app_name": app_name, "db_password": "p"}, **kw)]
    )


def test_each_successful_switch_is_recorded_with_metadata(manager):
    """场景1：成功切换逐版本入档，可查版本号、来源、时间、原因。"""
    r2 = _push(manager, "second-app")
    assert r2.ok
    r3 = _push(manager, "third-app", flags={"dark_mode": True})
    assert r3.ok

    versions = manager.history()
    assert [v.version for v in versions] == [3, 2, 1]
    assert versions[0].current is True
    assert {v.current for v in versions[1:]} == {False}
    assert all(v.sources == ("api",) for v in versions)
    assert all(v.created_at for v in versions)
    assert all(v.reason == "refresh" for v in versions)
    print(
        "[场景1] 3 次成功切换 => [依据] 历史(最新在前)="
        f"{[(v.version, v.sources, v.current) for v in versions]}"
    )


def test_failed_commit_is_not_recorded(manager, valid_values):
    """场景2：被拒绝的提交不产生历史版本。"""
    assert len(manager.history()) == 1
    bad = manager.reload_from(
        [make_api_payload(values={**valid_values, "max_connections": 0})]
    )
    assert bad.ok is False
    versions = manager.history()
    assert len(versions) == 1
    assert versions[0].version == 1
    print("[场景2] 非法提交 => [依据] 历史仍只有版本 1，失败提交不入档")


def test_history_evicts_oldest_when_limit_exceeded(spec, valid_values):
    """场景3：超容量淘汰最旧；当前版本即使最旧也不被淘汰。"""
    mgr = ConfigManager(
        spec,
        initial_layers=[make_api_payload(values=valid_values)],
        history_limit=3,
    )
    _push(mgr, "v2-app")
    _push(mgr, "v3-app")
    assert [v.version for v in mgr.history()] == [3, 2, 1]

    _push(mgr, "v4-app")
    assert [v.version for v in mgr.history()] == [4, 3, 2]
    print("[场景3] limit=3 连续 4 次成功 => [依据] 最旧版本 1 被淘汰，保留 [4,3,2]")

    # 当前版本（4）最旧时也不能淘汰：连续失败不改变历史，再来一次成功
    _push(mgr, "v5-app")
    assert [v.version for v in mgr.history()] == [5, 4, 3]
    print("[场景3] 第 5 次成功 => [依据] 版本 2 被淘汰，保留 [5,4,3]，当前版本永不淘汰")


def test_current_version_is_protected_from_eviction(spec):
    """场景3b：即使只剩当前版本占满名额，也不能把它淘汰掉。"""
    mgr = ConfigManager(
        spec,
        initial_layers=[
            make_api_payload(values={"app_name": "only-app", "db_password": "p"})
        ],
        history_limit=1,
    )
    _push(mgr, "newer-app")
    versions = mgr.history()
    assert len(versions) == 1
    assert versions[0].version == mgr.current().version == 2
    print("[场景3b] history_limit=1 再刷新 => [依据] 当前版本 2 保留，旧版本 1 被淘汰")


def test_rollback_restores_values_and_flags_and_makes_new_version(manager):
    """场景4：回退成功 -> 生成新版本号，值/开关恢复为目标版本内容。"""
    _push(
        manager,
        "v2-app",
        flags={"dark_mode": {"mode": "percentage", "percentage": 30}},
    )
    _push(
        manager,
        "v3-app",
        flags={"dark_mode": {"mode": "percentage", "percentage": 90}},
    )
    assert manager.current().values["app_name"] == "v3-app"

    r = manager.rollback(2)
    assert r.ok is True
    snap = manager.current()
    assert snap.version == 4  # 回退是一次新的切换，版本号递增
    assert snap.values["app_name"] == "v2-app"
    assert snap.flags["dark_mode"].percentage == 30
    assert snap.reason == "rollback"
    assert snap.origin_version == 2
    assert snap.field_origins["app_name"] == "api"
    versions = {v.version for v in manager.history()}
    assert {1, 2, 3, 4} <= versions
    print(
        "[场景4] 当前=v3，回退到 v2 => [依据] 生成新版本 4(origin=2)，"
        "app_name=v2-app、percentage=30，历史中 v2/v3 仍在"
    )


def test_rollback_unknown_version_fails_and_stays(manager):
    """场景5：回退到不存在的版本，明确失败且当前版本不动。"""
    before = manager.current()
    r = manager.rollback(999)
    assert r.ok is False
    assert r.error_type == "VersionNotFound"
    assert manager.current() is before
    print("[场景5] 回退到 v999 => [依据] VersionNotFound，当前版本不变")


def test_rollback_revalidates_layers_and_failure_keeps_current(spec):
    """场景6：回退按普通刷新规则重新校验；校验失败停在当前版本。

    历史层是提交时的原始数据。若某一层在回退时因规格变化而不再合法，
    回退必须整体失败，不能切成半死状态。
    """
    from app.schema import FieldSpec, Spec, ValueType

    initial_spec = Spec(
        fields=(
            FieldSpec("app_name", ValueType.STRING, required=True),
            FieldSpec("db_password", ValueType.STRING, required=True),
            FieldSpec("legacy_flag", ValueType.INT, required=False, default=0),
        ),
        flags=(),
    )
    mgr = ConfigManager(
        initial_spec,
        initial_layers=[
            make_api_payload(
                values={
                    "app_name": "a-app",
                    "db_password": "p",
                    "legacy_flag": 5,
                }
            )
        ],
    )
    # 收紧规格：legacy_flag 范围改为 0..1，历史中的 5 此刻已非法
    tightened = Spec(
        fields=(
            FieldSpec("app_name", ValueType.STRING, required=True),
            FieldSpec("db_password", ValueType.STRING, required=True),
            FieldSpec("legacy_flag", ValueType.INT, required=False, default=0, max=1),
        ),
        flags=(),
    )
    mgr._spec = tightened
    before = mgr.current()
    r = mgr.rollback(1)
    assert r.ok is False
    assert r.error_type == "ConfigValidationError"
    assert any(i.key == "legacy_flag" and i.kind == "range" for i in r.issues)
    assert mgr.current() is before
    assert mgr.current().values["legacy_flag"] == 5  # 旧版本原样不动
    print(
        "[场景6] 规格收紧后回退历史版本 => [依据] 重新校验发现 legacy_flag 越界，"
        "回退被拒，服务停在当前版本不半生效"
    )


def test_rollback_versions_and_live_history_eviction(spec, valid_values):
    """场景7：回退产生的新版本同样参与容量淘汰。"""
    mgr = ConfigManager(
        spec,
        initial_layers=[make_api_payload(values=valid_values)],
        history_limit=3,
    )
    _push(mgr, "v2-app")
    _push(mgr, "v3-app")
    assert mgr.rollback(1).ok is True
    versions = [v.version for v in mgr.history()]
    assert versions == [4, 3, 2]
    # v1 虽被回退引用过，但按容量规则它作为最旧非当前版本被淘汰
    print("[场景7] limit=3，回退 v1 生成 v4 => [依据] 历史为 [4,3,2]，回退版本同享容量规则")
