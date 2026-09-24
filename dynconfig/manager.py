"""配置管理器：原子切换、失败回退、读写并发安全。

并发模型：
- 快照是不可变对象；读路径只取一次引用，期间不持锁，
  因此请求要么看到完整旧版本，要么看到完整新版本，不可能字段跨版本混合。
- 刷新路径持互斥锁串行化：先在锁外构建并校验新快照，成功后一次性替换引用；
  构建失败（含来源错误/校验错误）完全不触碰当前快照，并抛出 RefreshError，
  调用方可拿到上一份版本号与逐条失败原因。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from dynconfig.errors import RefreshError
from dynconfig.model import ConfigSnapshot
from dynconfig.parser import build_snapshot
from dynconfig.schema import Schema
from dynconfig.sources import RawSource


class ConfigManager:
    def __init__(self, schema: Schema,
                 sources: list[RawSource] | None = None,
                 providers: list[Callable[[], list[RawSource]]] | None = None
                 ) -> None:
        self._schema = schema
        self._providers = list(providers or [])
        self._lock = threading.Lock()
        initial = sources if sources is not None else self._collect()
        # 未提供 providers 时，把初始来源固化为兜底 provider，
        # 保证无参 refresh 不会凭空丢失来源
        if not self._providers:
            frozen = list(initial)
            self._providers.append(lambda: frozen)
        # 初始构建必须成功，否则服务不应启动（错误向上抛出）
        self._snapshot: ConfigSnapshot = build_snapshot(initial, schema, 1)

    def _collect(self) -> list[RawSource]:
        collected: list[RawSource] = []
        for provider in self._providers:
            collected.extend(provider())
        return collected

    def snapshot(self) -> ConfigSnapshot:
        """获取当前生效快照（一次引用读取，永远是完整版本）。"""
        return self._snapshot

    def get(self, key: str) -> Any:
        return self._snapshot.values[key]

    def version(self) -> int:
        return self._snapshot.version

    def refresh(self, overlay: RawSource | None = None) -> ConfigSnapshot:
        """整体重建并原子替换。

        - 始终重新执行 providers（重新读文件/环境变量）重建完整来源栈；
        - overlay 非空时作为最高优先级层叠加（用于管理 API 的即时覆盖）；
        - 构建失败：保留旧快照，抛 RefreshError（含原因与旧版本号）；
        - 并发刷新由锁串行化，后到者基于最新版本继续，状态不会损坏。
        """
        with self._lock:
            old = self._snapshot
            try:
                raw = self._collect()
                if overlay is not None:
                    raw.append(overlay)
                candidate = build_snapshot(raw, self._schema, old.version + 1)
            except RefreshError as exc:
                for reason in exc.reasons:
                    reason["kept_version"] = old.version
                exc.message = (f"刷新失败，继续使用版本 {old.version}："
                               f"{exc.message}")
                raise
            except Exception as exc:  # 来源读取/解析等意外失败同样回退
                wrapped = RefreshError(
                    f"刷新失败，继续使用版本 {old.version}：{exc}",
                    [{"code": "source_error", "message": str(exc),
                      "field": None, "source": None,
                      "kept_version": old.version}])
                raise wrapped from exc
            # 校验全部通过后才替换，唯一一次引用写入
            self._snapshot = candidate
            return candidate
