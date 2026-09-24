"""合并多来源原始配置并按 Schema 解析校验，产出不可变快照。

规则：
- 来源按 priority 升序合并，同键高优先级覆盖低优先级，取值来源可追溯；
- 任一来源提供的任一字段非法都会被拒绝（不因其被更高优先级覆盖而放过），
  错误聚合后一次性抛出，原因可区分（缺项/类型/越界/格式）；
- 必填项全来源缺失 -> MissingFieldError，绝不静默沿用默认值；
- flags 段用于覆盖开关规则，未知开关/未知属性一律拒绝。
"""
from __future__ import annotations

import re
from typing import Any

from dynconfig.errors import (
    ConfigError,
    FormatError,
    MissingFieldError,
    OutOfRangeError,
    RefreshError,
    TypeError_,
)
from dynconfig.model import ConfigSnapshot, FieldTrace
from dynconfig.schema import FieldSpec, FlagRule, Schema
from dynconfig.sources import FLAGS_SECTION, RawSource

_BOOL_TRUE = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "off"})
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _flatten(data: dict[str, Any], errors: list[ConfigError],
             source: str) -> dict[str, Any]:
    """把嵌套 dict 拍平为点分键。"""
    out: dict[str, Any] = {}

    def walk(obj: dict[str, Any], pfx: str) -> None:
        for k, v in obj.items():
            if not isinstance(k, str) or not k:
                errors.append(FormatError("配置键必须是非空字符串",
                                          field=pfx or "<root>", source=source))
                continue
            full = f"{pfx}.{k}" if pfx else k
            # flags 段保留为嵌套对象交给 _build_flags，不做拍平
            if pfx == "" and k == FLAGS_SECTION:
                out[FLAGS_SECTION] = v
                continue
            if isinstance(v, dict):
                walk(v, full)
            else:
                out[full] = v  # list/标量原样保留，类型交给 _coerce/flags

    walk(data, "")
    return out


def _coerce(spec: FieldSpec, raw: Any, literal: bool,
            source: str) -> Any:
    """按字段类型转换并校验单个原始值，非法即抛对应分类错误。"""
    t = spec.type
    if t == "int":
        if isinstance(raw, bool):
            raise TypeError_(f"字段 {spec.key} 需要 int，得到 bool",
                             field=spec.key, source=source)
        if literal and isinstance(raw, str):
            if not _INT_RE.match(raw.strip()):
                raise TypeError_(f"字段 {spec.key} 需要整数，得到 {raw!r}",
                                 field=spec.key, source=source)
            value: Any = int(raw.strip())
        elif isinstance(raw, int):
            value = raw
        else:
            raise TypeError_(
                f"字段 {spec.key} 需要 int，得到 {type(raw).__name__}",
                field=spec.key, source=source)
    elif t == "float":
        if isinstance(raw, bool):
            raise TypeError_(f"字段 {spec.key} 需要 float，得到 bool",
                             field=spec.key, source=source)
        if literal and isinstance(raw, str):
            if not _FLOAT_RE.match(raw.strip()):
                raise TypeError_(f"字段 {spec.key} 需要数值，得到 {raw!r}",
                                 field=spec.key, source=source)
            value = float(raw.strip())
        elif isinstance(raw, (int, float)):
            value = float(raw)
        else:
            raise TypeError_(
                f"字段 {spec.key} 需要 float，得到 {type(raw).__name__}",
                field=spec.key, source=source)
    elif t == "bool":
        if literal and isinstance(raw, str):
            token = raw.strip().lower()
            if token in _BOOL_TRUE:
                value = True
            elif token in _BOOL_FALSE:
                value = False
            else:
                raise FormatError(
                    f"字段 {spec.key} 需要布尔值(true/false)，得到 {raw!r}",
                    field=spec.key, source=source)
        elif isinstance(raw, bool):
            value = raw
        else:
            raise TypeError_(
                f"字段 {spec.key} 需要 bool，得到 {type(raw).__name__}",
                field=spec.key, source=source)
    elif t in ("str", "enum"):
        if not isinstance(raw, str):
            raise TypeError_(
                f"字段 {spec.key} 需要字符串，得到 {type(raw).__name__}",
                field=spec.key, source=source)
        value = raw
        if t == "enum" and spec.choices is not None and value not in spec.choices:
            raise FormatError(
                f"字段 {spec.key} 取值 {value!r} 非法，"
                f"允许值: {', '.join(spec.choices)}",
                field=spec.key, source=source)
    else:  # pragma: no cover
        raise TypeError_(f"未知字段类型 {t}", field=spec.key, source=source)

    if t in ("int", "float"):
        if spec.min is not None and value < spec.min:
            raise OutOfRangeError(
                f"字段 {spec.key}={value} 小于下限 {spec.min}",
                field=spec.key, source=source)
        if spec.max is not None and value > spec.max:
            raise OutOfRangeError(
                f"字段 {spec.key}={value} 超过上限 {spec.max}",
                field=spec.key, source=source)
    if t == "str" and spec.pattern is not None and re.fullmatch(
            spec.pattern, value) is None:
        raise FormatError(
            f"字段 {spec.key}={value!r} 不匹配格式 {spec.pattern}",
            field=spec.key, source=source)
    return value


def _build_flags(schema: Schema, layers: list[tuple[str, dict[str, Any], bool]],
                 errors: list[ConfigError]) -> dict[str, FlagRule]:
    """合并 flags 段覆盖项并校验，产出开关规则。"""
    # key -> attr -> (raw, source)
    merged: dict[str, dict[str, tuple[Any, str]]] = {}
    for source_name, flat, _literal in layers:
        raw_flags = flat.get(FLAGS_SECTION)
        if raw_flags is None:
            continue
        if not isinstance(raw_flags, dict):
            errors.append(TypeError_("flags 段必须是对象",
                                     field=FLAGS_SECTION, source=source_name))
            continue
        for fk, fv in raw_flags.items():
            if not isinstance(fv, dict):
                errors.append(TypeError_(f"开关 {fk} 的配置必须是对象",
                                         field=f"flags.{fk}", source=source_name))
                continue
            merged.setdefault(fk, {})
            for attr, raw in fv.items():
                merged[fk][attr] = (raw, source_name)

    result: dict[str, FlagRule] = {}
    for key, base in schema.flags.items():
        state, rollout, groups = base.state, base.rollout, base.groups
        for attr, (raw, source_name) in merged.pop(key, {}).items():
            if attr == "state":
                if raw not in ("on", "off", "conditional"):
                    errors.append(FormatError(
                        f"开关 {key} 的 state 必须是 on/off/conditional，得到 {raw!r}",
                        field=f"flags.{key}.state", source=source_name))
                else:
                    state = raw
            elif attr == "rollout":
                if isinstance(raw, bool) or not isinstance(raw, int):
                    errors.append(TypeError_(
                        f"开关 {key} 的 rollout 需要 0..100 的整数",
                        field=f"flags.{key}.rollout", source=source_name))
                elif not 0 <= raw <= 100:
                    errors.append(OutOfRangeError(
                        f"开关 {key} 的 rollout={raw} 越界，允许 0..100",
                        field=f"flags.{key}.rollout", source=source_name))
                else:
                    rollout = raw
            elif attr == "groups":
                if not isinstance(raw, list) or not all(
                        isinstance(g, str) for g in raw):
                    errors.append(TypeError_(
                        f"开关 {key} 的 groups 必须是字符串数组",
                        field=f"flags.{key}.groups", source=source_name))
                else:
                    groups = frozenset(raw)
            else:
                errors.append(FormatError(
                    f"开关 {key} 存在未知属性 {attr!r}",
                    field=f"flags.{key}.{attr}", source=source_name))
        result[key] = FlagRule(key=key, state=state, rollout=rollout,
                               groups=groups, deprecated=base.deprecated)
    for unknown in merged:
        errors.append(FormatError(f"覆盖了未定义的开关 {unknown!r}",
                                  field=f"flags.{unknown}", source="merged"))
    return result


def build_snapshot(sources: list[RawSource], schema: Schema,
                   version: int) -> ConfigSnapshot:
    """按优先级合并、逐字段校验并构建快照。任何非法项聚合后抛 RefreshError。"""
    errors: list[ConfigError] = []
    ordered = sorted(sources, key=lambda s: s.priority)

    layers: list[tuple[str, dict[str, Any], bool]] = []
    for src in ordered:
        flat = _flatten(dict(src.values), errors, src.name)
        layers.append((src.name, flat, src.literal))

    # 未知键检测：拍平后的键必须精确命中 schema 字段，或为 flags 段
    known = set(schema.fields) | {FLAGS_SECTION}
    for source_name, flat, _lit in layers:
        for key in flat:
            if key not in known:
                errors.append(FormatError(
                    f"未知配置项 {key!r}", field=key, source=source_name))

    values: dict[str, Any] = {}
    traces: dict[str, FieldTrace] = {}
    for key, spec in schema.fields.items():
        winner_name: str | None = None
        winner_value: Any = None
        found = False
        for source_name, flat, literal in layers:
            if key not in flat:
                continue
            found = True
            try:
                coerced = _coerce(spec, flat[key], literal, source_name)
            except ConfigError as exc:
                errors.append(exc)
                continue
            # 按 priority 升序遍历，后出现即更高优先级
            winner_name, winner_value = source_name, coerced
        if winner_name is None:
            if not found and spec.required:
                errors.append(MissingFieldError(
                    f"必填项 {key} 在所有来源中均缺失", field=key))
            elif not found:
                winner_name, winner_value = "default", spec.default
            # found=True 但值非法的情形已记录具体错误，此处不再补缺
        values[key] = winner_value
        traces[key] = FieldTrace(key=key, source=winner_name or "default",
                                 sensitive=spec.sensitive)

    flags = _build_flags(schema, layers, errors)

    if errors:
        raise RefreshError(
            f"配置解析被拒绝，共 {len(errors)} 处错误",
            [e.to_dict() for e in errors])
    return ConfigSnapshot(version=version, values=values, traces=traces,
                          flags=flags)
