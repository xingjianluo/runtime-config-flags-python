"""配置与功能开关的规格定义、校验、敏感值脱敏。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from .errors import (
    ConfigError,
    ConfigFormatError,
    ConfigMissingError,
    ConfigRangeError,
    ConfigTypeError,
    ConfigValidationError,
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
class Snapshot:
    """一份不可变的、完整一致的配置版本。"""

    version: int
    values: dict[str, Any]
    flags: dict[str, FlagRule]
    sources: tuple[str, ...]
    field_origins: dict[str, str]


@dataclass(frozen=True)
class Issue:
    """提交校验中发现的一处问题（可解释、按来源定位）。"""

    key: str
    category: str  # missing | type | range | format | unknown
    source: str
    message: str
    section: str = "values"  # values | flags


@dataclass(frozen=True)
class ShadowedValue:
    """本次提交里被更高优先级来源挡下的一层合法取值。"""

    key: str
    source: str
    winning_source: str
    section: str = "values"


@dataclass(frozen=True)
class CommitResult:
    """一次提交的完整构造结果：快照 + 本次被挡下的低优先级冲突。"""

    snapshot: Snapshot
    shadowed: tuple[ShadowedValue, ...]


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


def validate_field(spec: FieldSpec, raw: Any, *, source: str) -> Any:
    """按规格校验并强制转换单个字段，非法时抛出可区分错误。"""
    key = spec.key
    if spec.type is ValueType.BOOL:
        if not _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 bool，实际为 {type(raw).__name__}: {raw!r}",
                source=source,
                key=key,
            )
        return raw

    if spec.type is ValueType.INT:
        # 注意：bool 是 int 的子类，必须排除
        if not isinstance(raw, int) or _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 int，实际为 {type(raw).__name__}: {raw!r}",
                source=source,
                key=key,
            )
        value: Any = raw
    elif spec.type is ValueType.FLOAT:
        if not isinstance(raw, (int, float)) or _is_bool(raw):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 float，实际为 {type(raw).__name__}: {raw!r}",
                source=source,
                key=key,
            )
        value = float(raw)
    elif spec.type is ValueType.STRING:
        if not isinstance(raw, str):
            raise ConfigTypeError(
                f"字段 {key!r} 需要 string，实际为 {type(raw).__name__}: {raw!r}",
                source=source,
                key=key,
            )
        value = raw
    else:  # pragma: no cover - 枚举穷尽
        raise ConfigTypeError(f"字段 {key!r} 具有未知类型 {spec.type}", source=source, key=key)

    if spec.min is not None and value < spec.min:
        raise ConfigRangeError(
            f"字段 {key!r} 取值 {value!r} 小于下限 {spec.min}",
            source=source,
            key=key,
        )
    if spec.max is not None and value > spec.max:
        raise ConfigRangeError(
            f"字段 {key!r} 取值 {value!r} 大于上限 {spec.max}",
            source=source,
            key=key,
        )
    if spec.choices is not None and value not in spec.choices:
        raise ConfigFormatError(
            f"字段 {key!r} 取值 {value!r} 不在允许集合 {list(spec.choices)} 中",
            source=source,
            key=key,
        )
    if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
        raise ConfigFormatError(
            f"字段 {key!r} 取值 {value!r} 不匹配格式 {spec.pattern!r}",
            source=source,
            key=key,
        )
    return value


def validate_flag(name: str, raw: Any) -> FlagRule:
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
                key=name,
            )
        if raw == PERCENTAGE:
            raise ConfigFormatError(
                f"开关 {name!r} 使用 percentage 模式时必须提供 percentage 数值",
                key=name,
            )
        if raw == ALLOW_GROUPS:
            raise ConfigFormatError(
                f"开关 {name!r} 使用 allow_groups 模式时必须提供 groups 列表",
                key=name,
            )
        return FlagRule(raw)  # type: ignore[arg-type]

    if not isinstance(raw, dict):
        raise ConfigTypeError(
            f"开关 {name!r} 需要 bool/string/dict，实际为 {type(raw).__name__}: {raw!r}",
            key=name,
        )

    mode = raw.get("mode")
    if mode not in _FLAG_MODES:
        raise ConfigFormatError(
            f"开关 {name!r} 的 mode {mode!r} 非法，允许 {list(_FLAG_MODES)}",
            key=name,
        )

    if mode == ON or mode == OFF:
        return FlagRule(mode)

    if mode == PERCENTAGE:
        pct = raw.get("percentage")
        if not isinstance(pct, int) or isinstance(pct, bool):
            raise ConfigTypeError(
                f"开关 {name!r} 的 percentage 需要 int，实际为 {type(pct).__name__}",
                key=name,
            )
        if not 0 <= pct <= 100:
            raise ConfigRangeError(
                f"开关 {name!r} 的 percentage {pct} 越界，允许 0..100",
                key=name,
            )
        return FlagRule(PERCENTAGE, percentage=pct)

    # allow_groups
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigFormatError(
            f"开关 {name!r} 的 groups 必须是非空字符串列表",
            key=name,
        )
    if not all(isinstance(g, str) and g for g in groups):
        raise ConfigTypeError(
            f"开关 {name!r} 的 groups 中存在非字符串或空字符串元素",
            key=name,
        )
    if len(set(groups)) != len(groups):
        raise ConfigFormatError(
            f"开关 {name!r} 的 groups 存在重复项",
            key=name,
        )
    return FlagRule(ALLOW_GROUPS, groups=frozenset(groups))


_ISSUE_CATEGORY = {
    ConfigMissingError: "missing",
    ConfigTypeError: "type",
    ConfigRangeError: "range",
    ConfigFormatError: "format",
}


def _issue_from_error(key: str, source: str, exc: ConfigError, *, section: str) -> Issue:
    category = _ISSUE_CATEGORY.get(type(exc), "format")
    return Issue(
        key=key,
        category=category,
        source=source,
        message=exc.message,
        section=section,
    )


def _validate_field_collect(
    fspec: FieldSpec, raw: Any, *, source: str, issues: list[Issue]
) -> Any | None:
    """校验单个字段；敏感字段即使抛错也不允许原始值进入诊断信息。"""
    key = fspec.key
    try:
        return validate_field(fspec, raw, source=source)
    except ConfigError as exc:
        if fspec.sensitive:
            category = _ISSUE_CATEGORY.get(type(exc), "format")
            detail = {
                "type": f"需要 {fspec.type.value}，实际为 {type(raw).__name__}",
                "range": f"超出允许范围 [{fspec.min}, {fspec.max}]",
                "format": "不满足枚举/格式约束",
                "missing": "缺失",
            }.get(category, "不满足约束")
            issues.append(
                Issue(
                    key=key,
                    category=category,
                    source=source,
                    message=f"敏感配置项 {key!r} 非法：{detail}（真实值不展示）",
                )
            )
        else:
            issues.append(_issue_from_error(key, source, exc, section="values"))
        return None


def commit_snapshot(
    spec: Spec,
    layers: list,
    *,
    version: int,
) -> CommitResult:
    """按层做严格提交校验并构造快照。

    规则：
    - 每一层独立校验：只要本层自带的值/开关非法（哪怕会被高层盖掉），整次提交拒绝；
    - 任何层出现未定义字段/开关 => unknown 问题，拒绝，不静默丢弃；
    - 一次提交内所有问题一次性收集、上报（key/分类/来源齐全），敏感项不带真实值；
    - 合法但被更高优先级盖掉的低优先级取值记录为 shadowed，随成功结果返回；
    - 必填项在合并后仍缺失 => missing。
    layers 中每项需有 name / priority / data / flags 属性（如 sources.Loaded）。
    """
    issues: list[Issue] = []
    field_map = spec.field_map()
    flag_map = spec.flag_map()
    ordered = sorted(layers, key=lambda layer: layer.priority)

    # 每层校验后的合法值；并追踪每个键的“获胜来源”与被挡层
    layer_values: list[tuple[str, dict[str, Any]]] = []
    layer_flags: list[tuple[str, dict[str, Any]]] = []

    for layer in ordered:
        ok_values: dict[str, Any] = {}
        for key, raw in layer.data.items():
            fspec = field_map.get(key)
            if fspec is None:
                issues.append(
                    Issue(
                        key=key,
                        category="unknown",
                        source=layer.name,
                        message=f"来源 {layer.name!r} 出现未定义配置项 {key!r}，拒绝提交",
                        section="values",
                    )
                )
                continue
            converted = _validate_field_collect(
                fspec, raw, source=layer.name, issues=issues
            )
            if converted is not None:
                ok_values[key] = converted
        layer_values.append((layer.name, ok_values))

        ok_flags: dict[str, Any] = {}
        for name, raw in layer.flags.items():
            if name not in flag_map:
                issues.append(
                    Issue(
                        key=name,
                        category="unknown",
                        source=layer.name,
                        message=(
                            f"来源 {layer.name!r} 出现未定义开关 {name!r}，拒绝提交"
                        ),
                        section="flags",
                    )
                )
                continue
            try:
                ok_flags[name] = validate_flag(name, raw)
            except ConfigError as exc:
                # 开关值本身不含敏感数据；若将来有需要也可在此脱敏
                issues.append(_issue_from_error(name, layer.name, exc, section="flags"))
        layer_flags.append((layer.name, ok_flags))

    # 记录每一层都提供了哪些键（即使值非法也算“出现过”，避免重复报缺项）
    provided_value_keys = set()
    provided_flag_keys = set()
    for layer in ordered:
        provided_value_keys.update(layer.data.keys())
        provided_flag_keys.update(layer.flags.keys())

    if issues:
        # 必填缺项也聚合进来，保证一次提交的问题一次性列全
        issue_keys = {(i.section, i.key) for i in issues}
        for fspec in spec.fields:
            if (
                fspec.required
                and fspec.key not in provided_value_keys
                and ("values", fspec.key) not in issue_keys
            ):
                issues.append(
                    Issue(
                        key=fspec.key,
                        category="missing",
                        source="-",
                        message=f"必填配置项 {fspec.key!r} 缺失，任何来源均未提供",
                        section="values",
                    )
                )
        issues.sort(key=lambda i: (i.section, i.key, i.source, i.category))
        summary = "；".join(
            f"[{i.section}/{i.category}] {i.key}@{i.source}: {i.message}" for i in issues
        )
        raise ConfigValidationError(
            f"提交校验发现 {len(issues)} 处问题，整次提交被拒绝：{summary}",
            issues=issues,
        )

    # 合并合法层，计算最终值、出处与被挡冲突
    merged: dict[str, Any] = {}
    flag_rules: dict[str, FlagRule] = {}
    origins: dict[str, str] = {}
    shadowed: list[ShadowedValue] = []
    used_sources: list[str] = []

    for source_name, values in layer_values:
        if source_name not in used_sources:
            used_sources.append(source_name)
        for key, value in values.items():
            if key in merged:
                shadowed.append(
                    ShadowedValue(
                        key=key,
                        source=origins[key],
                        winning_source=source_name,
                        section="values",
                    )
                )
            merged[key] = value
            origins[key] = source_name

    for source_name, flags in layer_flags:
        if source_name not in used_sources:
            used_sources.append(source_name)
        for name, rule in flags.items():
            if name in flag_rules:
                shadowed.append(
                    ShadowedValue(
                        key=name,
                        source=origins[name],
                        winning_source=source_name,
                        section="flags",
                    )
                )
            flag_rules[name] = rule
            origins[name] = source_name

    # 必填项检查
    final_values: dict[str, Any] = {}
    final_origins: dict[str, str] = {}
    for fspec in spec.fields:
        if fspec.key in merged:
            final_values[fspec.key] = merged[fspec.key]
            final_origins[fspec.key] = origins[fspec.key]
        elif not fspec.required:
            final_values[fspec.key] = fspec.default
            final_origins[fspec.key] = "defaults"
        else:
            raise ConfigMissingError(
                f"必填配置项 {fspec.key!r} 缺失，任何来源均未提供",
                key=fspec.key,
            )

    final_flags = {fspec.name: FlagRule(fspec.default_mode) for fspec in spec.flags}
    final_flags.update(flag_rules)

    snapshot = Snapshot(
        version=version,
        values=final_values,
        flags=final_flags,
        sources=tuple(used_sources),
        field_origins=final_origins,
    )
    return CommitResult(snapshot=snapshot, shadowed=tuple(shadowed))


def build_snapshot(
    spec: Spec,
    merged: dict[str, Any],
    flag_defs: dict[str, Any],
    *,
    version: int,
    sources: tuple[str, ...],
    origins: dict[str, str],
) -> Snapshot:
    """兼容旧接口：基于已合并结果构造快照。

    新代码应使用 commit_snapshot 获得逐层严格校验与 shadowed 信息。
    """
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


def redact(snapshot: Snapshot, spec: Spec) -> dict[str, Any]:
    """输出脱敏后的配置视图（敏感值替换为占位符）。"""
    sensitive = {f.key for f in spec.fields if f.sensitive}
    return {
        key: (SENSITIVE_PLACEHOLDER if key in sensitive else value)
        for key, value in snapshot.values.items()
    }
