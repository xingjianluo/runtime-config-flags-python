"""配置与功能开关的规格定义、校验、敏感值脱敏。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Sequence

from .errors import (
    ConfigError,
    ConfigFormatError,
    ConfigMissingError,
    ConfigRangeError,
    ConfigTypeError,
    ConfigUnknownKeyError,
    ConfigValidationError,
    ValidationIssue,
)

# 开关状态
ON = "on"
OFF = "off"
PERCENTAGE = "percentage"
ALLOW_GROUPS = "allow_groups"

FlagMode = Literal["on", "off", "percentage", "allow_groups"]
_FLAG_MODES = (ON, OFF, PERCENTAGE, ALLOW_GROUPS)

SENSITIVE_PLACEHOLDER = "***REDACTED***"


class ValueType(str, Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"


@dataclass(frozen=True)
class FieldSpec:
    """单个配置项的规格。"""

    key: str
    type: ValueType
    required: bool = True
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: tuple[Any, ...] | None = None
    pattern: str | None = None
    sensitive: bool = False


@dataclass(frozen=True)
class FlagSpec:
    """功能开关规格。"""

    name: str
    deprecated: bool = False
    replacement: str | None = None
    default_mode: FlagMode = "off"


@dataclass(frozen=True)
class Spec:
    """整套配置的规格。"""

    fields: tuple[FieldSpec, ...]
    flags: tuple[FlagSpec, ...]

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}

    def flag_map(self) -> dict[str, FlagSpec]:
        return {f.name: f for f in self.flags}


@dataclass(frozen=True)
class FlagRule:
    """开关的一条放量规则。"""

    mode: FlagMode
    percentage: int = 0
    groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ShadowedValue:
    """本次合并中被更高优先级盖掉的一个低值冲突。"""

    key: str
    kind: str  # "field" | "flag"
    source: str
    winning_source: str


@dataclass(frozen=True)
class Snapshot:
    """一份不可变的、完整一致的配置版本。"""

    version: int
    values: dict[str, Any]
    flags: dict[str, FlagRule]
    sources: tuple[str, ...]
    field_origins: dict[str, str]
    flag_origins: dict[str, str] = field(default_factory=dict)
    shadows: dict[str, tuple[ShadowedValue, ...]] = field(default_factory=dict)
    created_at: str | None = None
    reason: str = "refresh"
    origin_version: int | None = None
    raw_layers: tuple[Sequence[Any], ...] = field(default_factory=tuple)


def default_spec() -> Spec:
    return Spec(
        fields=(
            FieldSpec(
                "app_name",
                ValueType.STRING,
                required=True,
                pattern=r"^[a-z][a-z0-9_-]{2,31}$",
            ),
            FieldSpec(
                "log_level",
                ValueType.STRING,
                required=False,
                default="info",
                choices=("debug", "info", "warning", "error"),
            ),
            FieldSpec(
                "max_connections",
                ValueType.INT,
                required=False,
                default=100,
                min=1,
                max=10_000,
            ),
            FieldSpec(
                "sample_rate",
                ValueType.FLOAT,
                required=False,
                default=1.0,
                min=0.0,
                max=1.0,
            ),
            FieldSpec(
                "enable_cache", ValueType.BOOL, required=False, default=True
            ),
            FieldSpec(
                "timeout_ms", ValueType.INT, required=False, default=5000, min=1, max=60_000
            ),
            FieldSpec("db_password", ValueType.STRING, required=True, sensitive=True),
        ),
        flags=(
            FlagSpec("new_checkout"),
            FlagSpec("dark_mode"),
            FlagSpec("legacy_reports", deprecated=True, replacement="new_reports"),
        ),
    )


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _render(sensitive: bool, raw: Any) -> str:
    """错误信息里展示输入值；敏感项一律只显示占位符。"""
    if sensitive:
        return SENSITIVE_PLACEHOLDER
    return repr(raw)


def validate_field(spec: FieldSpec, raw: Any, *, source: str) -> Any:
    """按规格校验并强制转换单个字段，非法时抛出可区分错误。"""
    key = spec.key
    shown = _render(spec.sensitive, raw)
    if spec.type is ValueType.BOOL:
        if not _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 bool，实际为 {type(raw).__name__}: {shown}",
                source=source,
                key=key,
            )
        return raw

    if spec.type is ValueType.INT:
        # 注意：bool 是 int 的子类，必须排除
        if not isinstance(raw, int) or _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 int，实际为 {type(raw).__name__}: {shown}",
                source=source,
                key=key,
            )
        value: Any = raw
    elif spec.type is ValueType.FLOAT:
        if not isinstance(raw, (int, float)) or _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 float，实际为 {type(raw).__name__}: {shown}",
                source=source,
                key=key,
            )
        value = float(raw)
    elif spec.type is ValueType.STRING:
        if not isinstance(raw, str):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 string，实际为 {type(raw).__name__}: {shown}",
                source=source,
                key=key,
            )
        value = raw
    else:  # pragma: no cover - 枚举穷尽
        raise ConfigTypeError(f"字段 {key!r} 具有未知类型 {spec.type}", source=source, key=key)

    if spec.min is not None and value < spec.min:
        raise ConfigRangeError(
            f"字段 {key!r} 取值 {shown} 小于下限 {spec.min}",
            source=source,
            key=key,
        )
    if spec.max is not None and value > spec.max:
        raise ConfigRangeError(
            f"字段 {key!r} 取值 {shown} 大于上限 {spec.max}",
            source=source,
            key=key,
        )
    if spec.choices is not None and value not in spec.choices:
        raise ConfigFormatError(
            f"字段 {key!r} 取值 {shown} 不在允许集合 {list(spec.choices)} 中",
            source=source,
            key=key,
        )
    if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
        raise ConfigFormatError(
            f"字段 {key!r} 取值 {shown} 不匹配格式 {spec.pattern!r}",
            source=source,
            key=key,
        )
    return value


def validate_flag(name: str, raw: Any, *, source: str = "unknown") -> FlagRule:
    """校验单个开关的原始定义。

    可接受写法：
      true / false -> on / off
      "on" / "off" / "percentage" / "allow_groups"
      {"mode": ..., "percentage": 0-100, "groups": [...]}
    """
    if raw is True:
        return FlagRule(ON)
    if raw is False:
        return FlagRule(OFF)
    if isinstance(raw, str):
        if raw not in _FLAG_MODES:
            raise ConfigFormatError(
                f"开关 {name!r} 的模式 {raw!r} 非法，允许 {list(_FLAG_MODES)}",
                source=source,
                key=name,
            )
        if raw == PERCENTAGE:
            raise ConfigFormatError(
                f"开关 {name!r} 使用 percentage 模式时必须提供 percentage 数值",
                source=source,
                key=name,
            )
        if raw == ALLOW_GROUPS:
            raise ConfigFormatError(
                f"开关 {name!r} 使用 allow_groups 模式时必须提供 groups 列表",
                source=source,
                key=name,
            )
        return FlagRule(raw)  # type: ignore[arg-type]

    if not isinstance(raw, dict):
        raise ConfigTypeError(
            f"开关 {name!r} 需要 bool/string/dict，实际为 {type(raw).__name__}: {raw!r}",
            source=source,
            key=name,
        )

    mode = raw.get("mode")
    if mode not in _FLAG_MODES:
        raise ConfigFormatError(
            f"开关 {name!r} 的 mode {mode!r} 非法，允许 {list(_FLAG_MODES)}",
            source=source,
            key=name,
        )

    if mode == ON or mode == OFF:
        return FlagRule(mode)

    if mode == PERCENTAGE:
        pct = raw.get("percentage")
        if not isinstance(pct, int) or isinstance(pct, bool):
            raise ConfigTypeError(
                f"开关 {name!r} 的 percentage 需要 int，实际为 {type(pct).__name__}",
                source=source,
                key=name,
            )
        if not 0 <= pct <= 100:
            raise ConfigRangeError(
                f"开关 {name!r} 的 percentage {pct} 越界，允许 0..100",
                source=source,
                key=name,
            )
        return FlagRule(PERCENTAGE, percentage=pct)

    # allow_groups
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigFormatError(
            f"开关 {name!r} 的 groups 必须是非空字符串列表",
            source=source,
            key=name,
        )
    if not all(isinstance(g, str) and g for g in groups):
        raise ConfigTypeError(
            f"开关 {name!r} 的 groups 中存在非字符串或空字符串元素",
            source=source,
            key=name,
        )
    if len(set(groups)) != len(groups):
        raise ConfigFormatError(
            f"开关 {name!r} 的 groups 存在重复项",
            source=source,
            key=name,
        )
    return FlagRule(ALLOW_GROUPS, groups=frozenset(groups))


def build_snapshot(
    spec: Spec,
    merged: dict[str, Any],
    flag_defs: dict[str, Any],
    *,
    version: int,
    sources: tuple[str, ...],
    origins: dict[str, str],
) -> Snapshot:
    """依据合并结果构造完整快照；任一非法项即整体拒绝。"""
    values: dict[str, Any] = {}
    final_origins: dict[str, str] = {}
    for fspec in spec.fields:
        if fspec.key in merged:
            source = origins.get(fspec.key, "unknown")
            values[fspec.key] = validate_field(fspec, merged[fspec.key], source=source)
            final_origins[fspec.key] = source
        elif not fspec.required:
            values[fspec.key] = fspec.default
            final_origins[fspec.key] = "defaults"
        else:
            raise ConfigMissingError(
                f"必填配置项 {fspec.key!r} 缺失，任何来源均未提供",
                key=fspec.key,
            )

    flag_map = spec.flag_map()
    unknown = set(flag_defs) - set(flag_map)
    if unknown:
        raise ConfigFormatError(
            f"存在未在规格中定义的开关: {sorted(unknown)}"
        )

    rules: dict[str, FlagRule] = {}
    for fspec in spec.flags:
        if fspec.name in flag_defs:
            rules[fspec.name] = validate_flag(fspec.name, flag_defs[fspec.name])
        else:
            rules[fspec.name] = FlagRule(fspec.default_mode)

    return Snapshot(
        version=version,
        values=values,
        flags=rules,
        sources=tuple(sources),
        field_origins=final_origins,
    )


def _issue_from(exc: ConfigError, *, key: str, source: str, sensitive: bool) -> ValidationIssue:
    kind = {
        ConfigMissingError: "missing",
        ConfigTypeError: "type",
        ConfigRangeError: "range",
        ConfigUnknownKeyError: "unknown",
        ConfigFormatError: "format",
    }.get(type(exc), "format")
    return ValidationIssue(
        key=key,
        kind=kind,
        source=source,
        message=exc.message,
        sensitive=sensitive,
    )


def build_strict_snapshot(
    spec: Spec,
    layers: Sequence[Any],
    merge_result: Any,
    *,
    version: int,
    created_at: str,
    reason: str = "refresh",
    origin_version: int | None = None,
) -> Snapshot:
    """逐层严格校验后构造快照。

    规则：
    - 每一层独立校验：未知配置项/开关、以及即使被更高优先级盖掉的非法值，
      都记录为问题，全部问题一次性抛出 ConfigValidationError；
    - 合并后的获胜值再次校验（正常情况下与逐层结果一致，属防御性兜底）；
    - 必填项任何来源都未提供 -> missing；
    - 未在任何来源出现的可选项取默认值，出处记为 defaults；
    - 同时计算每个键的最终出处，以及被盖掉的低值冲突 shadows。
    """
    from .sources import Loaded

    field_map = spec.field_map()
    flag_map = spec.flag_map()
    sensitive_keys = {f.key for f in spec.fields if f.sensitive}
    issues: list[ValidationIssue] = []

    # 1) 逐层校验：每一层自身就必须合法，且不得含未定义项（哪怕会被盖掉）
    for layer in layers:
        for key, raw in layer.data.items():
            fspec = field_map.get(key)
            if fspec is None:
                issues.append(
                    ValidationIssue(
                        key=key,
                        kind="unknown",
                        source=layer.name,
                        message=(
                            f"配置项 {key!r} 未在规格中定义，来源 {layer.name!r} 不得提供"
                        ),
                    )
                )
                continue
            try:
                validate_field(fspec, raw, source=layer.name)
            except ConfigError as exc:
                issues.append(
                    _issue_from(
                        exc,
                        key=key,
                        source=layer.name,
                        sensitive=fspec.sensitive,
                    )
                )
        for name, raw in layer.flags.items():
            if name not in flag_map:
                issues.append(
                    ValidationIssue(
                        key=name,
                        kind="unknown",
                        source=layer.name,
                        message=(
                            f"开关 {name!r} 未在规格中定义，来源 {layer.name!r} 不得提供"
                        ),
                    )
                )
                continue
            try:
                validate_flag(name, raw, source=layer.name)
            except ConfigError as exc:
                issues.append(
                    _issue_from(exc, key=name, source=layer.name, sensitive=False)
                )

    # 2) 合并获胜值校验 + 必填项检查
    #    获胜键在逐层阶段已用同一份原始值校验过，这里只对没有问题记录的键
    #    做防御性复校，避免同一问题被报两次；同时收集缺失必填项。
    bad_field_keys = {i.key for i in issues if i.kind != "unknown"}
    values: dict[str, Any] = {}
    final_origins: dict[str, str] = {}
    for fspec in spec.fields:
        if fspec.key in merge_result.values:
            source = merge_result.value_origins.get(fspec.key, "unknown")
            if fspec.key in bad_field_keys:
                continue
            try:
                values[fspec.key] = validate_field(
                    fspec, merge_result.values[fspec.key], source=source
                )
            except ConfigError as exc:
                issues.append(
                    _issue_from(
                        exc,
                        key=fspec.key,
                        source=source,
                        sensitive=fspec.sensitive,
                    )
                )
            else:
                final_origins[fspec.key] = source
        elif not fspec.required:
            values[fspec.key] = fspec.default
            final_origins[fspec.key] = "defaults"
        else:
            issues.append(
                ValidationIssue(
                    key=fspec.key,
                    kind="missing",
                    source="any",
                    message=f"必填配置项 {fspec.key!r} 缺失，任何来源均未提供",
                    sensitive=fspec.sensitive,
                )
            )

    # 3) 开关获胜值构造（未知项已在逐层校验中拦截）
    bad_flag_keys = {i.key for i in issues if i.kind != "unknown"}
    rules: dict[str, FlagRule] = {}
    flag_origins: dict[str, str] = {}
    for fspec in spec.flags:
        if fspec.name in merge_result.flags:
            source = merge_result.flag_origins.get(fspec.name, "unknown")
            if fspec.name in bad_flag_keys:
                continue
            try:
                rules[fspec.name] = validate_flag(
                    fspec.name, merge_result.flags[fspec.name], source=source
                )
            except ConfigError as exc:
                issues.append(
                    _issue_from(exc, key=fspec.name, source=source, sensitive=False)
                )
            else:
                flag_origins[fspec.name] = source
        else:
            rules[fspec.name] = FlagRule(fspec.default_mode)
            flag_origins[fspec.name] = "defaults"

    if issues:
        raise ConfigValidationError(issues)

    # 4) 被盖掉的低值冲突（只读视图，供运维解释本次合并）
    shadows: dict[str, tuple[ShadowedValue, ...]] = {}
    for key, contributors in merge_result.value_contributors.items():
        if len(contributors) > 1:
            winner = contributors[-1]
            shadows[key] = tuple(
                ShadowedValue(
                    key=key,
                    kind="field",
                    source=src,
                    winning_source=winner,
                )
                for src in contributors[:-1]
            )
    for name, contributors in merge_result.flag_contributors.items():
        if len(contributors) > 1:
            winner = contributors[-1]
            shadows[name] = tuple(
                ShadowedValue(
                    key=name,
                    kind="flag",
                    source=src,
                    winning_source=winner,
                )
                for src in contributors[:-1]
            )

    raw_layers = tuple(
        (layer.name, layer.priority, dict(layer.data), dict(layer.flags))
        for layer in layers
        if isinstance(layer, Loaded)
    )

    return Snapshot(
        version=version,
        values=values,
        flags=rules,
        sources=merge_result.source_names,
        field_origins=final_origins,
        flag_origins=flag_origins,
        shadows=shadows,
        created_at=created_at,
        reason=reason,
        origin_version=origin_version,
        raw_layers=raw_layers,
    )


def redact(snapshot: Snapshot, spec: Spec) -> dict[str, Any]:
    """输出脱敏后的配置视图（敏感值替换为占位符）。"""
    sensitive = {f.key for f in spec.fields if f.sensitive}
    return {
        key: (SENSITIVE_PLACEHOLDER if key in sensitive else value)
        for key, value in snapshot.values.items()
    }
