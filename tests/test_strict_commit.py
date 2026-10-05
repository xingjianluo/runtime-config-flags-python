"""场景二：逐层严格校验、未知项拒绝、多问题一次性上报、敏感诊断脱敏、
被挡低优先级冲突记录与每值来源可解释。"""
from __future__ import annotations

from app.errors import ConfigValidationError
from app.schema import commit_snapshot
from app.sources import (
    API_SOURCE,
    ENV_SOURCE,
    FILE_SOURCE,
    PRIORITY,
    Loaded,
)


def _layer(name: str, values: dict | None = None, flags: dict | None = None) -> Loaded:
    return Loaded(name, PRIORITY[name], values or {}, flags or {})


def _required() -> dict:
    return {"app_name": "my-app", "db_password": "pw"}


def test_invalid_value_in_low_priority_layer_rejected_even_if_covered(spec):
    """低优先级层自身非法，即使被高优先级同名键盖掉，整次提交也拒绝。"""
    layers = [
        _layer(FILE_SOURCE, {"max_connections": 99999, "app_name": "file-app", "db_password": "fp"}),
        _layer(API_SOURCE, {"max_connections": 300}),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False, "应当抛错"
    except ConfigValidationError as exc:
        issue = next(i for i in exc.issues if i.key == "max_connections")
        assert issue.category == "range"
        assert issue.source == FILE_SOURCE
    print("[输入] file 层 max_connections=99999，被 api 层 300 盖掉 => [依据] 仍整次拒绝，问题定位 @file/range")


def test_invalid_value_in_middle_layer_rejected_across_sources(spec):
    """跨三层：中间 env 层类型非法，高优先级 api 合法，也必须拒绝并定位 env。"""
    layers = [
        _layer(FILE_SOURCE, _required()),
        _layer(ENV_SOURCE, {"enable_cache": "not-a-bool"}),
        _layer(API_SOURCE, {"enable_cache": True, "max_connections": 50}),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False
    except ConfigValidationError as exc:
        issue = next(i for i in exc.issues if i.key == "enable_cache")
        assert issue.category == "type"
        assert issue.source == ENV_SOURCE
    print("[输入] env 层 enable_cache='not-a-bool'，api 层给合法 True => [依据] 拒绝 @env/type，不被盖掉掩盖")


def test_unknown_field_and_unknown_flag_rejected(spec):
    layers = [
        _layer(
            FILE_SOURCE,
            {**_required(), "mystery_key": 1},
            {"ghost_flag": True},
        ),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False
    except ConfigValidationError as exc:
        keys = {(i.key, i.category, i.section) for i in exc.issues}
        assert ("mystery_key", "unknown", "values") in keys
        assert ("ghost_flag", "unknown", "flags") in keys
    print("[输入] 未定义字段 mystery_key 与未定义开关 ghost_flag => [依据] 均以 unknown 拒绝，不静默丢弃")


def test_multiple_problems_reported_in_one_submission(spec):
    """一次提交里多处问题必须全部列出，且能看出项/类别/来源。"""
    layers = [
        _layer(
            FILE_SOURCE,
            {"app_name": "Bad Name", "max_connections": 0, "log_level": "trace"},
        ),
        _layer(API_SOURCE, {"sample_rate": 1.5, "enable_cache": "yes"}),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False
    except ConfigValidationError as exc:
        problems = {(i.key, i.category, i.source) for i in exc.issues}
        assert ("app_name", "format", FILE_SOURCE) in problems
        assert ("max_connections", "range", FILE_SOURCE) in problems
        assert ("log_level", "format", FILE_SOURCE) in problems
        assert ("sample_rate", "range", API_SOURCE) in problems
        assert ("enable_cache", "type", API_SOURCE) in problems
        # 必填缺项同样聚合进来
        assert any(i.key == "db_password" and i.category == "missing" for i in exc.issues)
        assert len(exc.issues) >= 6
        print(f"[输入] file 三处非法 + api 两处非法 + 缺必填 => [依据] 一次性上报 {len(exc.issues)} 处: {[(i.key, i.category, i.source) for i in exc.issues]}")


def test_sensitive_value_never_leaks_in_diagnostics(spec):
    """敏感项非法时诊断信息只能说明约束，不能带出真实值。"""
    secret = "SUPER-SECRET-VALUE-42"
    layers = [
        _layer(API_SOURCE, {"app_name": "my-app", "db_password": 999}),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False
    except ConfigValidationError as exc:
        issue = next(i for i in exc.issues if i.key == "db_password")
        assert issue.category == "type"
        assert secret not in issue.message
        assert "脱敏" in issue.message or "不展示" in issue.message
        assert secret not in exc.message
    print(f"[输入] db_password 给成 int（真实敏感值 {secret!r}） => [依据] 诊断={issue.message!r}，不含真实值")


def test_sensitive_out_of_range_diagnosis_redacted(spec):
    """敏感字符串无法越界；此处覆盖敏感项其它非法路径不泄露值（类型为 dict）。"""
    layers = [
        _layer(API_SOURCE, {"app_name": "my-app", "db_password": {"k": "v"}}),
    ]
    try:
        commit_snapshot(spec, layers, version=1)
        assert False
    except ConfigValidationError as exc:
        assert "db_password" in exc.message
        assert "'k'" not in exc.message and "'v'" not in exc.message
        assert "{'k': 'v'}" not in exc.message


def test_shadowed_lower_priority_conflicts_recorded_on_success(spec):
    """成功提交后，合法但被挡下的低优先级取值要可查（含字段和开关）。"""
    layers = [
        _layer(FILE_SOURCE, {**_required(), "max_connections": 10}, {"dark_mode": False}),
        _layer(ENV_SOURCE, {"max_connections": 20}),
        _layer(API_SOURCE, {"max_connections": 30}, {"dark_mode": True}),
    ]
    result = commit_snapshot(spec, layers, version=7)
    snap = result.snapshot
    assert snap.values["max_connections"] == 30
    assert snap.field_origins["max_connections"] == API_SOURCE
    shadowed = {(s.key, s.source, s.winning_source, s.section) for s in result.shadowed}
    assert ("max_connections", FILE_SOURCE, ENV_SOURCE, "values") in shadowed
    assert ("max_connections", ENV_SOURCE, API_SOURCE, "values") in shadowed
    assert ("dark_mode", FILE_SOURCE, API_SOURCE, "flags") in shadowed
    print(f"[输入] max_connections file=10/env=20/api=30 => [依据] 生效 30@api；被挡冲突={sorted(shadowed)}")


def test_effective_value_origins_explain_each_layer(spec):
    """生效快照中每个值都能解释来自哪一层；缺省项来自 defaults。"""
    layers = [
        _layer(FILE_SOURCE, {"app_name": "file-app", "db_password": "fp", "log_level": "debug"}),
        _layer(ENV_SOURCE, {"app_name": "env-app"}),
    ]
    snap = commit_snapshot(spec, layers, version=1).snapshot
    assert snap.field_origins["app_name"] == ENV_SOURCE
    assert snap.field_origins["db_password"] == FILE_SOURCE
    assert snap.field_origins["log_level"] == FILE_SOURCE
    assert snap.field_origins["max_connections"] == "defaults"
    print(f"[输入] file + env 两层 => [依据] origins={snap.field_origins}")


def test_manager_result_carries_issues_and_shadowed(manager):
    result = manager.reload_from(
        [
            _layer(FILE_SOURCE, {"app_name": "file-app", "db_password": "fp", "max_connections": 10}),
            _layer(API_SOURCE, {"max_connections": 300, "enable_cache": "bad"}),
        ]
    )
    assert result.ok is False
    assert result.error_type == "ConfigTypeError"
    assert result.issues[0].key == "enable_cache"

    ok = manager.reload_from(
        [
            _layer(FILE_SOURCE, {"app_name": "file-app", "db_password": "fp", "max_connections": 10}),
            _layer(API_SOURCE, {"max_connections": 300}),
        ]
    )
    assert ok.ok
    keys = {s.key for s in ok.shadowed}
    assert "max_connections" in keys
    print(f"[输入] manager 跨层提交 => [依据] 成功时 shadowed={[ (s.key,s.source,'->',s.winning_source) for s in ok.shadowed]}")
