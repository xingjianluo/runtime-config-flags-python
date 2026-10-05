"""线程安全的配置管理器：原子快照、刷新、版本历史/回退与来源故障降级。

并发模型：
- 当前配置保存在一个不可变 Snapshot 中，通过单一引用读取；
- 读路径无锁，永远只可能看到“完整的旧版本”或“完整的新版本”；
- 刷新在锁内完成“加载 -> 合并 -> 校验 -> 构造新快照”，
  只有全部成功才原子替换引用；值校验失败保留上一份快照。

版本历史：
- 每成功切换一次就把该版本（连同其原始来源层）入历史，可查、可回退；
- 历史份数有上限 history_limit，超了淘汰最旧版本；当前正在用的版本
  即使最旧也不会被淘汰；
- 回退按与普通刷新完全相同的规则重新校验，失败则停在当前版本。

来源故障：
- 运行期重新加载来源时来源本身损坏（文件损坏/解析失败），进入显式的
  “暂时没有可用配置”降级态（ConfigUnavailableError），读接口能区分并
  看到故障来源与类别；下一次提交合法配置成功后自动恢复。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import (
    ConfigError,
    ConfigSourceError,
    ConfigUnavailableError,
    ConfigValidationError,
)
from .schema import Snapshot, Spec, build_strict_snapshot
from .sources import Loaded, load_env, load_file, merge_with_trace

logger = logging.getLogger("runtime_config")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VersionInfo:
    """历史中一个可用版本的元数据（不含敏感值，可直接对外）。"""

    version: int
    sources: tuple[str, ...]
    created_at: str
    reason: str
    origin_version: int | None
    current: bool


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    version: int
    reason: str | None
    error_type: str | None
    sources: tuple[str, ...]
    degraded: bool = False
    fault_source: str | None = None
    issues: tuple[Any, ...] = ()


class ConfigManager:
    """持有当前快照与版本历史，保证并发读永远看到完整版本。"""

    def __init__(
        self,
        spec: Spec,
        *,
        file_path: str | None = None,
        initial_layers: list[Loaded] | None = None,
        history_limit: int = 10,
    ):
        if history_limit < 1:
            raise ValueError("history_limit 必须 >= 1")
        self._spec = spec
        self._file_path = file_path
        self._history_limit = history_limit
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._version = 0
        self._last_error: str | None = None
        self._history: list[Snapshot] = []
        self._degraded: ConfigUnavailableError | None = None
        if initial_layers is not None:
            # 初始构造必须成功，否则服务不应启动
            result = self.reload_from(initial_layers)
            if not result.ok:
                raise ConfigError(f"初始配置无效: {result.reason}")

    @property
    def spec(self) -> Spec:
        return self._spec

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def history_limit(self) -> int:
        return self._history_limit

    def current(self) -> Snapshot:
        snap = self._snapshot
        if snap is None:
            if self._degraded is not None:
                raise self._degraded
            raise ConfigError("配置尚未初始化")
        return snap

    def is_degraded(self) -> bool:
        return self._snapshot is None and self._degraded is not None

    def fault(self) -> ConfigUnavailableError | None:
        return self._degraded

    def _load_base_layers(self) -> list[Loaded]:
        layers: list[Loaded] = []
        if self._file_path is not None:
            layers.append(load_file(self._file_path))
        layers.append(load_env())
        return layers

    def refresh(self, extra: Loaded | None = None) -> RefreshResult:
        """重新加载（文件+环境，可选叠加 API 推送层）并原子切换。

        - 来源本身损坏 -> 进入降级态（没有可用配置），等待下次合法提交；
        - 值校验失败 -> 拒绝本次提交，保留上一份快照。
        """
        try:
            layers = self._load_base_layers()
        except ConfigSourceError as exc:
            return self._enter_degraded(exc)
        if extra is not None:
            layers.append(extra)
        return self.reload_from(layers)

    # ---- 版本历史 -------------------------------------------------------

    def history(self) -> list[VersionInfo]:
        """返回最近若干份可用版本，最新在前。"""
        with self._lock:
            current_version = self._snapshot.version if self._snapshot else None
            return [
                VersionInfo(
                    version=snap.version,
                    sources=snap.sources,
                    created_at=snap.created_at or "",
                    reason=snap.reason,
                    origin_version=snap.origin_version,
                    current=(snap.version == current_version),
                )
                for snap in reversed(self._history)
            ]

    def rollback(self, version: int) -> RefreshResult:
        """一键回退到历史中的某一版本。

        回退按普通刷新的同样规则重新加载该版本的原始来源层并重新校验；
        目标不存在或重新校验失败都停在当前版本。
        """
        with self._lock:
            target = next(
                (s for s in self._history if s.version == version), None
            )
        if target is None:
            cur = self._snapshot.version if self._snapshot else 0
            return RefreshResult(
                ok=False,
                version=cur,
                reason=f"历史中不存在版本 {version}，无法回退",
                error_type="VersionNotFound",
                sources=self._snapshot.sources if self._snapshot else (),
            )
        layers = [
            Loaded(name, priority, dict(data), dict(flags))
            for name, priority, data, flags in target.raw_layers
        ]
        return self.reload_from(
            layers, reason="rollback", origin_version=target.version
        )

    def _evict_history(self) -> None:
        """超过上限时淘汰最旧版本；当前正在用的版本不淘汰。调用方持锁。"""
        current_version = self._snapshot.version if self._snapshot else None
        while len(self._history) > self._history_limit:
            # 从最旧开始找可淘汰项
            for idx, snap in enumerate(self._history):
                if snap.version != current_version:
                    removed = self._history.pop(idx)
                    logger.info("历史版本 %s 因容量上限被淘汰", removed.version)
                    break
            else:
                # 仅剩当前版本占着名额，无法继续淘汰
                break

    def _enter_degraded(self, exc: ConfigSourceError) -> RefreshResult:
        with self._lock:
            old_version = self._snapshot.version if self._snapshot else 0
            unavail = ConfigUnavailableError(
                f"配置来源 {exc.source!r} 故障，当前没有可用配置: {exc.message}",
                source=exc.source or "unknown",
                error_type=type(exc).__name__,
            )
            # 明确的“暂时没有可用配置”态：旧引用撤下，绝不假装沿用旧值
            self._snapshot = None
            self._degraded = unavail
            self._last_error = unavail.message
            logger.error(
                "来源 %s 故障，进入降级态（原版本 %s 撤下）：%s",
                exc.source,
                old_version,
                exc.message,
            )
        return RefreshResult(
            ok=False,
            version=0,
            reason=unavail.message,
            error_type=type(exc).__name__,
            sources=(),
            degraded=True,
            fault_source=exc.source,
        )

    def reload_from(
        self,
        layers: list[Loaded],
        *,
        reason: str = "refresh",
        origin_version: int | None = None,
    ) -> RefreshResult:
        """基于给定来源层重建快照（便于测试/API 推送/回退）。

        整个“合并+校验+构造”在锁内完成；构造成功才替换引用并入历史。
        """
        with self._lock:
            old = self._snapshot
            merged = merge_with_trace(list(layers))
            new_version = self._version + 1
            try:
                new_snapshot = build_strict_snapshot(
                    self._spec,
                    list(layers),
                    merged,
                    version=new_version,
                    created_at=_now_iso(),
                    reason=reason,
                    origin_version=origin_version,
                )
            except ConfigValidationError as exc:
                self._last_error = exc.message
                logger.warning(
                    "配置刷新被拒绝（%d 处问题），保留版本 %s：%s",
                    len(exc.issues),
                    old.version if old else 0,
                    exc.message,
                )
                return RefreshResult(
                    ok=False,
                    version=old.version if old else 0,
                    reason=exc.message,
                    error_type=type(exc).__name__,
                    sources=old.sources if old else (),
                    issues=tuple(exc.issues),
                )
            except ConfigError as exc:  # pragma: no cover - 兜底
                self._last_error = exc.message
                return RefreshResult(
                    ok=False,
                    version=old.version if old else 0,
                    reason=exc.message,
                    error_type=type(exc).__name__,
                    sources=old.sources if old else (),
                )

            self._snapshot = new_snapshot
            self._version = new_version
            self._degraded = None
            self._last_error = None
            self._history.append(new_snapshot)
            self._evict_history()
            logger.info(
                "配置已切换到版本 %s（%s%s），来源=%s",
                new_version,
                reason,
                f"<-v{origin_version}" if origin_version is not None else "",
                new_snapshot.sources,
            )
            return RefreshResult(
                ok=True,
                version=new_version,
                reason=None,
                error_type=None,
                sources=new_snapshot.sources,
            )
