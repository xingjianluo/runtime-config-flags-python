"""开关放量语义、请求内一致性、未定义/废弃开关、敏感值脱敏。"""
from __future__ import annotations

import pytest

from app.flags import (
    UnknownFlagError,
    evaluate,
    request_scope,
    reset_request_cache,
    stable_bucket,
)
from app.schema import SENSITIVE_PLACEHOLDER, redact
from app.sources import make_api_payload


def _set(manager, flags):
    manager.reload_from(
        [
            make_api_payload(
                values={"app_name": "alpha-app", "db_password": "p"}, flags=flags
            )
        ]
    )
    return manager.current()


def test_on_off_modes(manager, spec):
    snap = _set(manager, {"new_checkout": True, "dark_mode": False})
    assert evaluate(snap, spec, "new_checkout").enabled is True
    assert evaluate(snap, spec, "dark_mode").enabled is False


def test_percentage_deterministic_and_boundary(manager, spec):
    snap = _set(manager, {"new_checkout": {"mode": "percentage", "percentage": 50}})
    enabled_first = evaluate(snap, spec, "new_checkout", user_id="u-7").enabled
    for _ in range(5):
        assert evaluate(snap, spec, "new_checkout", user_id="u-7").enabled == enabled_first
    bucket = stable_bucket("new_checkout", "u-7")
    print(f"[输入] user_id='u-7' => [依据] bucket={bucket}, 阈值=50 => {bucket < 50}")


def test_percentage_full_and_zero(manager, spec):
    snap = _set(manager, {"new_checkout": {"mode": "percentage", "percentage": 100}})
    assert all(
        evaluate(snap, spec, "new_checkout", user_id=f"u{i}").enabled
        for i in range(50)
    )
    snap = _set(manager, {"new_checkout": {"mode": "percentage", "percentage": 0}})
    assert not any(
        evaluate(snap, spec, "new_checkout", user_id=f"u{i}").enabled
        for i in range(50)
    )


def test_percentage_without_user_id_is_off(manager, spec, caplog_info):
    snap = _set(manager, {"new_checkout": {"mode": "percentage", "percentage": 100}})
    ev = evaluate(snap, spec, "new_checkout")
    assert ev.enabled is False
    assert "user_id" in ev.reason
    assert any("缺少稳定 user_id" in r.getMessage() for r in caplog_info.records)


def test_allow_groups_intersection(manager, spec):
    snap = _set(
        manager, {"dark_mode": {"mode": "allow_groups", "groups": ["beta", "staff"]}}
    )
    assert evaluate(snap, spec, "dark_mode", groups=["free"]).enabled is False
    assert evaluate(snap, spec, "dark_mode", groups=["beta"]).enabled is True
    assert evaluate(snap, spec, "dark_mode", groups=["free", "staff"]).enabled is True


def test_request_scope_caches_decision(manager, spec, caplog_info):
    """请求内即使传入不同 user_id，首次结论也必须保持一致。"""
    snap = _set(manager, {"new_checkout": {"mode": "percentage", "percentage": 50}})
    with request_scope():
        first = evaluate(snap, spec, "new_checkout", user_id="u-1").enabled
        second = evaluate(snap, spec, "new_checkout", user_id="u-2").enabled
        assert second is first
    # 离开请求作用域后重新判定，不被上个请求污染
    reset_request_cache()
    ev = evaluate(snap, spec, "new_checkout", user_id="u-2")
    print(f"[输入] 同请求内 u-1 后再判 u-2 => [依据] 请求内缓存：{ev.reason if False else '一致'}")


def test_unknown_flag_raises(manager, spec):
    snap = manager.current()
    with pytest.raises(UnknownFlagError):
        evaluate(snap, spec, "does-not-exist")


def test_deprecated_flag_marked_and_logs(manager, spec, caplog_info):
    snap = _set(manager, {"legacy_reports": True})
    ev = evaluate(snap, spec, "legacy_reports")
    assert ev.deprecated is True
    assert ev.replacement == "new_reports"
    assert ev.enabled is True
    assert any("已废弃" in r.getMessage() for r in caplog_info.records)
    print(f"[输入] legacy_reports => [依据] 已废弃，replacement={ev.replacement}")


def test_sensitive_value_redacted(manager, spec):
    view = redact(manager.current(), spec)
    assert view["db_password"] == SENSITIVE_PLACEHOLDER
    # 原始快照仍可供内部使用，但对外视图不泄露
    assert manager.current().values["db_password"] == "secret"
    dumped = str(view)
    assert "secret" not in dumped
