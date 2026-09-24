"""解析后的不可变配置快照。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dynconfig.schema import FlagRule


@dataclass(frozen=True)
class FieldTrace:
    """单个字段的取值解释：最终值来自哪个来源。"""
    key: str
    source: str
    sensitive: bool


@dataclass(frozen=True)
class ConfigSnapshot:
    """某一版本的完整配置。不可变，保证读侧永远看到完整版本。"""
    version: int
    values: dict[str, Any]
    traces: dict[str, FieldTrace]
    flags: dict[str, FlagRule] = field(default_factory=dict)
