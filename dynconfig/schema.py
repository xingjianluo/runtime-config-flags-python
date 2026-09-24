"""配置项规格定义：默认配置、字段类型与边界、开关放量规则。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FieldType = Literal["int", "float", "bool", "str", "enum"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    type: FieldType
    required: bool = True
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None
    pattern: str | None = None
    sensitive: bool = False


@dataclass(frozen=True)
class FlagRule:
    """开关放量规则。

    state: on/off/conditional
    rollout: 0..100 的放量百分比（仅 conditional 生效）
    groups: 命中即启用的用户分组
    deprecated: 已废弃开关
    """
    key: str
    state: Literal["on", "off", "conditional"]
    rollout: int = 0
    groups: frozenset[str] = frozenset()
    deprecated: bool = False


@dataclass(frozen=True)
class Schema:
    fields: dict[str, FieldSpec]
    flags: dict[str, FlagRule] = field(default_factory=dict)
