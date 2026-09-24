"""功能开关：启用/停用、按比例/分组放量、稳定分桶、未定义/废弃处理、请求一致。"""
from __future__ import annotations

import pytest

from dynconfig.errors import DeprecatedFlagError, UndefinedFlagError
from dynconfig.flags import evaluate
from dynconfig.service import FeatureFlagService

from .conftest import case_log, ctx


def test_on_off_states(manager):
    snap = manager.snapshot()
    assert evaluate(snap, "dark_mode", ctx("u1")).enabled is True
    d = evaluate(snap, "dark_mode", ctx("u1"))
    case_log.info("输入=dark_mode/u1 判定依据=%s", d.reason)
    assert d.reason == "on"


def test_group_takes_precedence_over_rollout(manager):
    snap = manager.snapshot()
    d = evaluate(snap, "new_checkout", ctx("anon-xyz", "beta"))
    case_log.info("输入=new_checkout/groups=beta 判定依据=%s", d.reason)
    assert d.enabled is True and d.reason == "group:beta"
    d2 = evaluate(snap, "new_checkout", ctx("anon-xyz", "staff"))
    assert d2.reason == "group:staff"


def test_rollout_stable_per_user(manager):
    snap = manager.snapshot()
    decisions = {}
    for i in range(2000):
        uid = f"user-{i}"
        d1 = evaluate(snap, "new_checkout", ctx(uid))
        d2 = evaluate(snap, "new_checkout", ctx(uid))
        assert d1.enabled == d2.enabled, "同一用户多次判定必须一致"
        decisions[uid] = d1.enabled
    enabled_ratio = sum(decisions.values()) / len(decisions)
    case_log.info("2000 用户放量比例=%.3f（目标 0.10）", enabled_ratio)
    assert 0.07 < enabled_ratio < 0.13


def test_anonymous_not_rolled_out(manager):
    d = evaluate(manager.snapshot(), "new_checkout", ctx(None))
    case_log.info("输入=new_checkout/匿名 判定依据=%s", d.reason)
    assert d.enabled is False and d.reason == "anon"


def test_undefined_and_deprecated(manager):
    snap = manager.snapshot()
    with pytest.raises(UndefinedFlagError):
        evaluate(snap, "nope", ctx("u"))
    with pytest.raises(DeprecatedFlagError):
        evaluate(snap, "legacy_export", ctx("u"))
    case_log.info("未定义开关 -> undefined_flag；已废弃开关 -> deprecated_flag")


def test_request_context_pins_version_and_caches(manager):
    """请求进行中刷新，同一上下文后续判定仍基于钉住版本且结果缓存一致。"""
    service = FeatureFlagService(manager)
    c = ctx("user-1")
    first = service.explain("dark_mode", c)
    # 刷新产生新版本
    manager.refresh()
    second = service.explain("dark_mode", c)
    case_log.info("首次版本=%s 刷新后同上下文版本=%s", first.version,
                  second.version)
    assert first is second  # 缓存命中
    assert second.version == first.version
