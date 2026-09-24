"""功能开关判定：启用/停用、按比例与分组放量。

放量语义（state=conditional）：
1. 先判分组：ctx.groups 与规则 groups 有交集即启用，reason=group:<name>；
2. 再判比例：对 (flag_key, user_id) 做稳定哈希取模，bucket < rollout 即启用。
   同一 user_id 对同一开关跨请求结果稳定；不同开关独立取桶，互不相关。
3. 无 user_id 的匿名请求无法稳定分桶，不放量（可调用方应显式分配请求ID）。

判定基于传入的不可变 ConfigSnapshot，因此同一版本内结果恒一致。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dynconfig.errors import DeprecatedFlagError, UndefinedFlagError
from dynconfig.model import ConfigSnapshot


@dataclass
class FlagContext:
    """单次请求上下文：user_id + 分组。

    同时作为请求内一致性载体：首次判定时钉住快照版本与结果，
    此后同一上下文内对同一开关的判定恒为同一结果，
    即使处理途中发生配置刷新也不受影响。
    """
    user_id: str | None = None
    groups: frozenset[str] = frozenset()
    pinned_version: int | None = None
    _snapshot: object = None
    _decisions: dict[str, FlagDecision] = None  # type: ignore[assignment]

    def cached(self, key: str) -> FlagDecision | None:
        if self._decisions is None:
            return None
        return self._decisions.get(key)

    def pinned_snapshot(self) -> object | None:
        return self._snapshot

    def remember(self, snapshot: ConfigSnapshot,
                 decision: FlagDecision) -> None:
        if self._snapshot is None:
            self._snapshot = snapshot
        if self._decisions is None:
            self._decisions = {}
        self._decisions[decision.key] = decision
        self.pinned_version = decision.version


@dataclass(frozen=True)
class FlagDecision:
    key: str
    enabled: bool
    reason: str          # on / off / group:<name> / rollout:<bucket>/<pct> / anon
    version: int          # 判定所依据的配置版本，便于解释与审计


def _bucket(key: str, user_id: str) -> int:
    """对 (key, user_id) 稳定取 0..99 的桶。"""
    digest = hashlib.sha256(f"{key}\x1f{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def evaluate(snapshot: ConfigSnapshot, key: str,
             ctx: FlagContext) -> FlagDecision:
    """基于不可变快照判定单个开关。"""
    rule = snapshot.flags.get(key)
    if rule is None:
        raise UndefinedFlagError(
            f"未定义的功能开关 {key!r}，拒绝猜测默认值", field=key)
    if rule.deprecated:
        raise DeprecatedFlagError(
            f"功能开关 {key!r} 已废弃，调用方必须移除相关引用", field=key)

    if rule.state == "on":
        return FlagDecision(key, True, "on", snapshot.version)
    if rule.state == "off":
        return FlagDecision(key, False, "off", snapshot.version)

    # conditional：分组优先于比例
    hit_groups = sorted(ctx.groups & rule.groups)
    if hit_groups:
        return FlagDecision(key, True, f"group:{hit_groups[0]}",
                            snapshot.version)
    if ctx.user_id is None:
        return FlagDecision(key, False, "anon", snapshot.version)
    bucket = _bucket(key, ctx.user_id)
    enabled = bucket < rule.rollout
    return FlagDecision(key, enabled,
                        f"rollout:{bucket}/{rule.rollout}", snapshot.version)
