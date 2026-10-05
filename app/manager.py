"""线程安全的配置管理器：原子快照、刷新、版本历史/回退与来源故障降级。

并发模型：
- 当前配置保存在不可变 Snapshot 中，读路径只取一次引用，
  永远只可能看到“完整的旧版本”或“完整的新版本”；
- 刷新在锁内完成“加载 -> 逐层校验 -> 构造新快照”，
  只有全部成功才原子替换引用；任何一步失败都保留上一份快照。

状态机：
- UNINITIALIZED：从未成功加载，读接口报 uninitialized；
- ACTIVE：有一份生效版本，正常服务；
- DEGRADED：来源故障（文件损坏/不可读等）后暂时没有可信配置，
  旧版本立即下线、读接口显式报告来源与故障类别，下一次合法提交后自动恢复。

版本历史：
- 每次成功切换（含回退）都留存一份不可变记录，带来源与时间；
- 保留份数 history_limit 可配，超出后淘汰最旧记录，但当前生效版本永不淘汰；
- 回退 = 用目标版本留存的原始层重新走一遍标准校验并生成新版本，
  校验失败则停在当前版本。
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import (
    ConfigError,
    ConfigSourceError,
    ConfigUnavailableError,
    ConfigValidationError,
)
from .schema import (
    Issue,
    ShadowedValue,
    Snapshot,
    Spec,
    commit_snapshot,
)
from .sources import Loaded, load_env, load_file

logger = logging.getLogger("runtime_config")

STATE_UNINITIALIZED = "uninitialized"
STATE_ACTIVE = "active"
STATE_DEGRADED = "degraded"

_DEFAULT_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class VersionEntry:
    """一份历史版本的留存信息（原始层也一并冻结保存，供回退重建）。"""

    version: int
    sources: tuple[str, ...]
    created_at: str
    layers: tuple[Loaded, ...]
    rolled_back_from: int | None = None

    def metadata(self) -> dict:
        return {
            "version": self.version,
            "sources": list(self.sources),
            "created_at": self.created_at,
            "rolled_back_from": self.rolled_back_from,
        }


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    version: int
    reason: str | None
    error_type: str | None
    sources: tuple[str, ...]
    state: str = STATE_UNINITIALIZED
    issues: tuple[Issue, ...] = ()
    shadowed: tuple[ShadowedValue, ...] = ()
    rolled_back_from: int | None = None


_SINGLE_ISSUE_ERROR_TYPE = {
    "missing": "ConfigMissingError",
    "type": "ConfigTypeError",
    "range": "ConfigRangeError",
    "format": "ConfigFormatError",
    "unknown": "ConfigFormatError",
}


class ConfigManager:
    """持有当前快照与版本历史，保证并发读永远看到完整版本。"""

    def __init__(
        self,
        spec: Spec,
        *,
        file_path: str | None = None,
        initial_layers: list[Loaded] | None = None,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
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
        self._state = STATE_UNINITIALIZED
        # 降级态诊断信息
        self._degraded_source: str | None = None
        self._degraded_kind: str | None = None
        self._degraded_detail: str | None = None
        self._degraded_at: str | None = None
        self._history: list[VersionEntry] = []
        self._last_shadowed: tuple[ShadowedValue, ...] = ()
        self._ever_active = False
        if initial_layers is not None:
            # 初始构造必须成功，否则服务不应启动
            result = self.reload_from(initial_layers)
            if not result.ok:
                raise ConfigError(f"初始配置无效: {result.reason}")

    @property
    def spec(self) -> Spec:
        return self._spec

    @property
    def state(self) -> str:
        return self._state

    @property
    def history_limit(self) -> int:
        return self._history_limit

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def current(self) -> Snapshot:
        snap = self._snapshot
        if self._state == STATE_DEGRADED:
            raise ConfigUnavailableError(
                "配置来源故障，当前暂时没有可用配置",
                source=self._degraded_source,
                kind=self._degraded_kind,
                detail=self._degraded_detail,
            )
        if snap is None:
            raise ConfigError("配置尚未初始化")
        return snap

    def status(self) -> dict:
        """供健康检查/运维查询的状态视图。"""
        snap = self._snapshot
        status = {
            "state": self._state,
            "version": snap.version if snap and self._state == STATE_ACTIVE else None,
        }
        if self._state == STATE_DEGRADED:
            status.update(
                source=self._degraded_source,
                kind=self._degraded_kind,
                detail=self._degraded_detail,
                failed_at=self._degraded_at,
            )
        return status

    def last_shadowed(self) -> tuple[ShadowedValue, ...]:
        """最近一次成功提交中被高优先级挡下的低优先级合法取值。"""
        return self._last_shadowed

    # ---- 版本历史 -------------------------------------------------------

    def history(self) -> list[dict]:
        """按时间从旧到新返回历史版本元数据（不含配置值）。"""
        with self._lock:
            return [entry.metadata() for entry in self._history]

    def rollback(self, target_version: int) -> RefreshResult:
        """回退到历史版本：用其留存层重新按标准规则校验后原子切换。

        校验失败则停在当前版本，服务不受影响。
        """
        with self._lock:
            entry = next(
                (e for e in self._history if e.version == target_version), None
            )
            if entry is None:
                return self._fail_result(
                    f"历史中不存在版本 {target_version}，无法回退",
                    error_type="VersionNotFoundError",
                )
            result = self._commit_layers(
                list(entry.layers), rolled_back_from=target_version
            )
            if result.ok:
                logger.info(
                    "已回退：基于历史版本 %s 生成新版本 %s",
                    target_version,
                    result.version,
                )
            else:
                logger.warning(
                    "回退到版本 %s 被拒绝，停在版本 %s：%s",
                    target_version,
                    result.version,
                    result.reason,
                )
            return result

    # ---- 加载与刷新 -----------------------------------------------------

    def _load_base_layers(self) -> list[Loaded]:
        layers: list[Loaded] = []
        if self._file_path is not None:
            layers.append(load_file(self._file_path))
        layers.append(load_env())
        return layers

    def refresh(self, extra: Loaded | None = None) -> RefreshResult:
        """重新加载（文件+环境，可选叠加 API 推送层）并原子切换。

        - 来源读取失败（文件损坏/不可读）=> 进入降级态；
        - 语义校验失败 => 整次拒绝，保留当前版本；
        - 全部成功 => 原子切换并自动恢复为正常态。
        """
        with self._lock:
            try:
                layers = self._load_base_layers()
            except ConfigSourceError as exc:
                return self._enter_degraded(exc)
            if extra is not None:
                layers.append(extra)
            return self._commit_layers(layers)

    def reload_from(self, layers: list[Loaded]) -> RefreshResult:
        """基于给定来源层重建快照（便于测试/API 推送）。

        整个“校验+构造”在锁内完成；构造成功才替换引用。
        """
        with self._lock:
            return self._commit_layers(list(layers))

    # ---- 锁内内部方法（调用方必须持锁） ----------------------------------

    def _fail_result(self, reason: str, *, error_type: str) -> RefreshResult:
        old = self._snapshot
        return RefreshResult(
            ok=False,
            version=old.version if old and self._state == STATE_ACTIVE else 0,
            reason=reason,
            error_type=error_type,
            sources=(
                old.sources
                if old and self._state == STATE_ACTIVE
                else ()
            ),
            state=self._state,
        )

    def _enter_degraded(self, exc: ConfigSourceError) -> RefreshResult:
        old = self._snapshot
        had_config = self._ever_active
        # 从未成功加载过 => 属于“未初始化”；只有曾健康过才进入“降级”
        self._state = STATE_DEGRADED if had_config else STATE_UNINITIALIZED
        self._degraded_source = exc.source or "unknown"
        self._degraded_kind = "source"
        self._degraded_detail = exc.message
        self._degraded_at = datetime.now(UTC).isoformat()
        self._last_error = exc.message
        # 旧快照立即下线：读路径不再返回它，避免假装没事继续沿用旧值
        self._snapshot = None
        logger.error(
            "来源 %s 故障，进入 %s 态（下线版本 %s）：%s",
            self._degraded_source,
            self._state,
            old.version if old else 0,
            exc.message,
        )
        return RefreshResult(
            ok=False,
            version=old.version if old else 0,
            reason=exc.message,
            error_type="ConfigSourceError",
            sources=old.sources if old else (),
            state=self._state,
        )

    def _commit_layers(
        self,
        layers: list[Loaded],
        *,
        rolled_back_from: int | None = None,
    ) -> RefreshResult:
        old = self._snapshot
        try:
            new_version = self._version + 1
            commit = commit_snapshot(self._spec, layers, version=new_version)
        except ConfigValidationError as exc:
            self._last_error = exc.message
            logger.warning(
                "配置提交被拒绝（%d 处问题），保留版本 %s",
                len(exc.issues),
                old.version if old else 0,
            )
            error_type = (
                _SINGLE_ISSUE_ERROR_TYPE.get(exc.issues[0].category)
                if len(exc.issues) == 1
                else None
            ) or "ConfigValidationError"
            return RefreshResult(
                ok=False,
                version=old.version if old and self._state == STATE_ACTIVE else 0,
                reason=exc.message,
                error_type=error_type,
                sources=old.sources if old and self._state == STATE_ACTIVE else (),
                state=self._state,
                issues=tuple(exc.issues),
            )
        except ConfigError as exc:
            # 缺必填等来自兼容路径的单错误
            self._last_error = exc.message
            logger.warning(
                "配置提交被拒绝，保留版本 %s：[%s] %s",
                old.version if old else 0,
                type(exc).__name__,
                exc.message,
            )
            return RefreshResult(
                ok=False,
                version=old.version if old and self._state == STATE_ACTIVE else 0,
                reason=exc.message,
                error_type=type(exc).__name__,
                sources=old.sources if old and self._state == STATE_ACTIVE else (),
                state=self._state,
            )

        new_snapshot = commit.snapshot
        self._snapshot = new_snapshot
        self._version = new_version
        self._last_error = None
        self._ever_active = True
        # 合法提交成功 => 自动恢复
        self._state = STATE_ACTIVE
        self._degraded_source = None
        self._degraded_kind = None
        self._degraded_detail = None
        self._degraded_at = None

        frozen_layers = tuple(copy.deepcopy(layer) for layer in layers)
        entry = VersionEntry(
            version=new_version,
            sources=new_snapshot.sources,
            created_at=datetime.now(UTC).isoformat(),
            layers=frozen_layers,
            rolled_back_from=rolled_back_from,
        )
        self._history.append(entry)
        self._trim_history_locked(protected_version=new_version)
        self._last_shadowed = commit.shadowed

        logger.info(
            "配置已切换到版本 %s，来源=%s%s",
            new_version,
            list(new_snapshot.sources),
            f"（回退自版本 {rolled_back_from}）" if rolled_back_from else "",
        )
        return RefreshResult(
            ok=True,
            version=new_version,
            reason=None,
            error_type=None,
            sources=new_snapshot.sources,
            state=STATE_ACTIVE,
            shadowed=commit.shadowed,
            rolled_back_from=rolled_back_from,
        )

    def _trim_history_locked(self, *, protected_version: int) -> None:
        """超出容量时淘汰最旧记录；当前生效版本永不淘汰。"""
        while len(self._history) > self._history_limit:
            for idx, entry in enumerate(self._history):
                if entry.version != protected_version:
                    del self._history[idx]
                    logger.info(
                        "历史版本 %s 因超出保留上限 %s 被淘汰",
                        entry.version,
                        self._history_limit,
                    )
                    break
            else:  # pragma: no cover - 理论不可达：至少有一条非当前版本
                return
