"""线程安全的配置管理器：原子快照、刷新与回退。

并发模型：
- 当前配置保存在一个不可变 Snapshot 中，通过单一引用读取；
- 读路径无锁，永远只可能看到“完整的旧版本”或“完整的新版本”；
- 刷新在锁内完成“加载 -> 合并 -> 校验 -> 构造新快照”，
  只有全部成功才原子替换引用；任何一步失败都保留上一份快照。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError
from .schema import Spec, Snapshot, build_snapshot
from .sources import Loaded, load_env, load_file, merge

logger = logging.getLogger("runtime_config")


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    version: int
    reason: str | None
    error_type: str | None
    sources: tuple[str, ...]


class ConfigManager:
    """持有当前快照，保证并发读永远看到完整版本。"""

    def __init__(
        self,
        spec: Spec,
        *,
        file_path: str | None = None,
        initial_layers: list[Loaded] | None = None,
    ):
        self._spec = spec
        self._file_path = file_path
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._version = 0
        self._last_error: str | None = None
        if initial_layers is not None:
            # 初始构造必须成功，否则服务不应启动
            result = self.reload_from(initial_layers)
            if not result.ok:
                raise ConfigError(
                    f"初始配置无效: {result.reason}",
                )

    @property
    def spec(self) -> Spec:
        return self._spec

    def current(self) -> Snapshot:
        snap = self._snapshot
        if snap is None:
            raise ConfigError("配置尚未初始化")
        return snap

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _load_base_layers(self) -> list[Loaded]:
        layers: list[Loaded] = []
        if self._file_path is not None:
            layers.append(load_file(self._file_path))
        layers.append(load_env())
        return layers

    def refresh(self, extra: Loaded | None = None) -> RefreshResult:
        """重新加载（文件+环境，可选叠加 API 推送层）并原子切换。

        失败时保留上一份可用快照。
        """
        layers = self._load_base_layers()
        if extra is not None:
            layers.append(extra)
        return self.reload_from(layers)

    def reload_from(self, layers: list[Loaded]) -> RefreshResult:
        """基于给定来源层重建快照（便于测试/API 推送）。

        整个“合并+校验+构造”在锁内完成；构造成功才替换引用。
        """
        with self._lock:
            old = self._snapshot
            try:
                merged, flag_defs, origins, source_names = merge(list(layers))
                new_version = self._version + 1
                new_snapshot = build_snapshot(
                    self._spec,
                    merged,
                    flag_defs,
                    version=new_version,
                    sources=source_names,
                    origins=origins,
                )
            except ConfigError as exc:
                self._last_error = exc.message
                logger.warning(
                    "配置刷新被拒绝，保留版本 %s：[%s] %s",
                    old.version if old else 0,
                    type(exc).__name__,
                    exc.message,
                )
                return RefreshResult(
                    ok=False,
                    version=old.version if old else 0,
                    reason=exc.message,
                    error_type=type(exc).__name__,
                    sources=old.sources if old else (),
                )

            self._snapshot = new_snapshot
            self._version = new_version
            self._last_error = None
            logger.info("配置已切换到版本 %s，来源=%s", new_version, source_names)
            return RefreshResult(
                ok=True,
                version=new_version,
                reason=None,
                error_type=None,
                sources=source_names,
            )
