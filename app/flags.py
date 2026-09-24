"""功能开关判定：启用/停用/按比例/按分组，请求内一致。

语义：
- on            : 始终启用
- off           : 始终停用
- percentage    : 依据 (flag, user_id) 的稳定哈希分桶，bucket < percentage 即放量；
                  缺少 user_id 时不放量（保证可复现，而非随机）
- allow_groups  : 请求所属分组与规则分组合集有交集即放量
请求内对同一开关的多次判定通过 ContextVar 缓存保证结果一致。
"""
from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError
from .schema import FlagRule, Snapshot

logger = logging.getLogger("runtime_config")

# 请求内判定缓存（flag_name -> bool），保证同一请求多次判定一致
_decision_cache: ContextVar[dict[str, bool] | None] = ContextVar(
    "flag_decision_cache", default=None
)


@dataclass(frozen=True)
class Evaluation:
    """一次判定的可解释结果。"""

    flag: str
    enabled: bool
    mode: str
    reason: str
    deprecated: bool = False
    replacement: str | None = None


class UnknownFlagError(ConfigError):
    """访问未定义开关。"""


def stable_bucket(flag: str, key: str) -> int:
    """把 (flag, key) 稳定哈希到 0..99。"""
    digest = hashlib.sha256(f"{flag}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def reset_request_cache() -> None:
    _decision_cache.set(None)


class request_scope:
    """请求级判定上下文：进入时建立缓存，退出时恢复外层状态。"""

    def __enter__(self):
        self._token = _decision_cache.set({})
        return self

    def __exit__(self, *exc):
        _decision_cache.reset(self._token)
        return False


def _decide(
    flag: str,
    rule: FlagRule,
    *,
    user_id: str | None,
    groups: list[str] | None,
) -> tuple[bool, str]:
    if rule.mode == "on":
        return True, "mode=on，对所有请求启用"
    if rule.mode == "off":
        return False, "mode=off，对所有请求停用"

    if rule.mode == "percentage":
        if not user_id:
            return False, "percentage 模式缺少稳定 user_id，按默认不放量处理"
        bucket = stable_bucket(flag, user_id)
        enabled = bucket < rule.percentage
        return enabled, (
            f"percentage 模式：user_id={user_id!r} 分桶 bucket={bucket}，"
            f"阈值 percentage={rule.percentage}，bucket < 阈值 => {enabled}"
        )

    # allow_groups
    request_groups = frozenset(groups or ())
    hit = request_groups & rule.groups
    enabled = bool(hit)
    return enabled, (
        f"allow_groups 模式：请求分组={sorted(request_groups) or '∅'}，"
        f"允许分组={sorted(rule.groups)}，命中={sorted(hit) or '∅'} => {enabled}"
    )


def evaluate(
    snapshot: Snapshot,
    spec: Any,
    flag: str,
    *,
    user_id: str | None = None,
    groups: list[str] | None = None,
    use_cache: bool = True,
) -> Evaluation:
    """判定开关是否生效；请求内同开关结果一致。"""
    flag_map = spec.flag_map()
    if flag not in flag_map:
        raise UnknownFlagError(f"开关 {flag!r} 未定义，拒绝访问")

    cache = _decision_cache.get() if use_cache else None
    if cache is not None and flag in cache:
        enabled = cache[flag]
        rule = snapshot.flags[flag]
        reason = f"请求内缓存命中，沿用首次判定结果 enabled={enabled}（mode={rule.mode}）"
    else:
        rule = snapshot.flags.get(flag, FlagRule(flag_map[flag].default_mode))
        enabled, reason = _decide(flag, rule, user_id=user_id, groups=groups)
        if cache is not None:
            cache[flag] = enabled

    fspec = flag_map[flag]
    if fspec.deprecated:
        logger.warning(
            "访问已废弃开关 %r，建议改用 %r", flag, fspec.replacement
        )
    logger.info(
        "开关判定 flag=%s user_id=%s groups=%s => enabled=%s | %s",
        flag,
        user_id,
        groups,
        enabled,
        reason,
    )
    return Evaluation(
        flag=flag,
        enabled=enabled,
        mode=rule.mode,
        reason=reason,
        deprecated=fspec.deprecated,
        replacement=fspec.replacement,
    )
