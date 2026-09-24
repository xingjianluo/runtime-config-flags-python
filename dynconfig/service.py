"""请求级开关服务：同一请求内多次判定 flag 结果一致。

FlagContext 即请求作用域：首次判定时把当时的快照引用与结果钉在上下文上，
此后同一上下文的判定恒基于该快照，即使请求处理途中发生刷新，
本请求也不会看到跨版本的混合判定。
"""
from __future__ import annotations

from dynconfig.flags import FlagContext, FlagDecision, evaluate
from dynconfig.manager import ConfigManager


class FeatureFlagService:
    def __init__(self, manager: ConfigManager) -> None:
        self._manager = manager

    def explain(self, key: str, ctx: FlagContext | None = None) -> FlagDecision:
        ctx = ctx or FlagContext()
        cached = ctx.cached(key)
        if cached is not None:
            return cached
        snapshot = ctx.pinned_snapshot() or self._manager.snapshot()
        decision = evaluate(snapshot, key, ctx)  # type: ignore[arg-type]
        ctx.remember(snapshot, decision)
        return decision

    def is_enabled(self, key: str, ctx: FlagContext | None = None) -> bool:
        return self.explain(key, ctx).enabled
