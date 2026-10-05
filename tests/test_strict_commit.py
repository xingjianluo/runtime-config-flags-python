"""严格提交校验：逐层合法性、未知项拒绝、多问题一次性上报、敏感值脱敏、冲突溯源。

覆盖场景：
1. 低优先级层的非法值即使被高优先级盖掉，也整体拒绝；
2. 未定义的配置项/开关（任一层）整体拒绝，不偷偷丢弃；
3. 一次提交多处问题一次性列全，每条带 key/kind/source；
4. 敏感项问题的诊断信息不带真实值；
5. 刷新成功后可查每个生效值的出处，以及被盖掉的低优先级冲突。
"""
from __future__ import annotations

from app.errors import ConfigValidationError
from app.schema import build_strict_snapshot
from app.sources import Loaded, PRIORITY, make_api_payload, merge_with_trace


def _build(layers, spec, **kw):
    return build_strict_snapshot(
        spec,
        layers,
        merge_with_trace(layers),
        version=1,
        created_at="2026-10-06T00:00:00+00:00",
        **kw,
    )


def _issues(layers, spec):
    try:
        _build(layers, spec)
    except ConfigValidationError as exc:
        return exc.issues
    raise AssertionError("本应被拒绝的提交却成功了")


def test_invalid_value_in_lower_priority_layer_rejected_even_if_shadowed(spec):
    """场景1：file 层 db_password 类型非法，虽被 api 层盖掉，仍整体拒绝。"""
    layers = [
        Loaded(
            "file",
            PRIORITY["file"],
            {"app_name": "file-app", "db_password": 12345},
            {},
        ),
        make_api_payload({"app_name": "api-app", "db_password": "good-pw"}),
    ]
    issues = _issues(layers, spec)
    issue = next(i for i in issues if i.key == "db_password" and i.source == "file")
    assert issue.kind == "type"
    # 最终值明明来自 api，但 file 的问题照样被挡下
    print(
        "[场景1] file.db_password=12345 被 api 盖掉 => [依据] "
        f"仍按逐层严格校验拒绝: {issue.message}"
    )


def test_range_violation_shadowed_by_higher_layer_rejected(spec):
    """场景1b：低优先级越界值被覆盖也拒绝。"""
    layers = [
        Loaded(
            "file",
            PRIORITY["file"],
            {"app_name": "file-app", "max_connections": 99_999},
            {},
        ),
        make_api_payload({"app_name": "api-app", "db_password": "p", "max_connections": 50}),
    ]
    issues = _issues(layers, spec)
    assert any(
        i.key == "max_connections" and i.kind == "range" and i.source == "file"
        for i in issues
    )
    print("[场景1b] file.max_connections=99999 被 api 覆盖 => [依据] 越界问题仍上报")


def test_unknown_config_key_in_any_layer_rejected(spec):
    """场景2：未定义配置项（即使在低优先级层）整体拒绝。"""
    layers = [
        Loaded("file", PRIORITY["file"], {"not_a_thing": 1}, {}),
        make_api_payload({"app_name": "api-app", "db_password": "p"}),
    ]
    issues = _issues(layers, spec)
    issue = next(i for i in issues if i.key == "not_a_thing")
    assert issue.kind == "unknown" and issue.source == "file"
    print(f"[场景2] file 层出现未定义项 not_a_thing => [依据] 拒绝: {issue.message}")


def test_unknown_flag_in_any_layer_rejected(spec):
    """场景2b：未定义开关整体拒绝。"""
    layers = [
        Loaded("file", PRIORITY["file"], {}, {"ghost_switch": True}),
        make_api_payload({"app_name": "api-app", "db_password": "p"}),
    ]
    issues = _issues(layers, spec)
    assert any(
        i.key == "ghost_switch" and i.kind == "unknown" and i.source == "file"
        for i in issues
    )
    print("[场景2b] file 层出现未定义开关 ghost_switch => [依据] 拒绝")


def test_multiple_issues_reported_all_at_once(spec):
    """场景3：一次提交里类型/范围/未知/格式问题一次性列全，不只报第一个。"""
    layers = [
        Loaded(
            "file",
            PRIORITY["file"],
            {
                "app_name": "Bad Name",          # 格式
                "max_connections": 0,            # 越界
                "mystery": True,                 # 未知
            },
            {"ghost_flag": "weird-mode"},        # 未知开关
        ),
        make_api_payload(
            {"db_password": 42, "enable_cache": "yes"}  # 类型问题 x2
        ),
    ]
    issues = _issues(layers, spec)
    by = {(i.key, i.source): i for i in issues}
    assert by[("app_name", "file")].kind == "format"
    assert by[("max_connections", "file")].kind == "range"
    assert by[("mystery", "file")].kind == "unknown"
    assert by[("ghost_flag", "file")].kind == "unknown"
    assert by[("db_password", "api")].kind == "type"
    assert by[("enable_cache", "api")].kind == "type"
    assert len(issues) == 6
    print(
        "[场景3] 6 处问题混在两层 => [依据] 一次性全部上报:\n  "
        + "\n  ".join(f"[{i.kind}] {i.key}@{i.source}" for i in issues)
    )


def test_sensitive_value_never_appears_in_diagnostics(spec):
    """场景4：敏感项的真实值不得出现在任何诊断文本里。"""
    secret = "SUPER-SECRET-VALUE-9001"
    layers = [make_api_payload({"app_name": "ok-app", "db_password": 9001})]
    issues = _issues(layers, spec)
    issue = next(i for i in issues if i.key == "db_password")
    assert issue.sensitive is True
    assert secret not in issue.message
    assert "***REDACTED***" in issue.message
    import json as _json

    assert secret not in _json.dumps(
        {"key": issue.key, "message": issue.message}, ensure_ascii=False
    )
    print(
        f"[场景4] db_password 实际值 {secret!r} => [依据] 诊断仅显示: {issue.message!r}"
    )


def test_effective_value_origins_and_shadowed_conflicts(spec):
    """场景5：成功后可查每个生效值出处，以及被盖掉的低值冲突。"""
    layers = [
        Loaded(
            "file",
            PRIORITY["file"],
            {"app_name": "file-app", "max_connections": 10},
            {"dark_mode": False},
        ),
        Loaded(
            "env",
            PRIORITY["env"],
            {"max_connections": 20},
            {},
        ),
        make_api_payload({"app_name": "api-app", "db_password": "p"}, {"dark_mode": True}),
    ]
    snap = _build(layers, spec)
    # 生效值出处
    assert snap.field_origins == {
        "app_name": "api",
        "db_password": "api",
        "max_connections": "env",
        "log_level": "defaults",
        "sample_rate": "defaults",
        "enable_cache": "defaults",
        "timeout_ms": "defaults",
    }
    assert snap.flag_origins["dark_mode"] == "api"
    # 未提供的开关取默认模式，出处记为 defaults
    assert snap.flag_origins["new_checkout"] == "defaults"
    assert snap.flag_origins["legacy_reports"] == "defaults"
    # 被盖掉的低优先级冲突
    app_shadows = {s.source: s.winning_source for s in snap.shadows["app_name"]}
    assert app_shadows == {"file": "api"}
    conn_shadows = {s.source: s.winning_source for s in snap.shadows["max_connections"]}
    assert conn_shadows == {"file": "env"}
    flag_shadows = {s.source: s.winning_source for s in snap.shadows["dark_mode"]}
    assert flag_shadows == {"file": "api"}
    print(
        "[场景5] file/env/api 三层同名键 => [依据] 生效出处="
        f"app_name@api,max_connections@env；被挡冲突={snap.shadows}"
    )


def test_successful_commit_via_manager_records_shadows(spec):
    """场景5b：通过 manager 刷新成功后，快照携带冲突溯源（供 /config 输出）。"""
    from app.manager import ConfigManager

    mgr = ConfigManager(
        spec,
        initial_layers=[
            Loaded(
                "file",
                PRIORITY["file"],
                {"app_name": "file-app", "db_password": "pf", "max_connections": 10},
                {},
            ),
            make_api_payload({"app_name": "api-app", "db_password": "pa", "max_connections": 30}),
        ],
    )
    snap = mgr.current()
    assert snap.shadows["app_name"][0].source == "file"
    assert snap.shadows["app_name"][0].winning_source == "api"
    print(f"[场景5b] 同名键跨层 => [依据] shadows={snap.shadows}")
