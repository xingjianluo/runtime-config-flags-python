"""配置来源：默认值 < 配置文件 < 环境变量 < 运行时 API。

优先级低 -> 高，高优先级覆盖低优先级；merge 时记录每个键的来源，
以保证“每个取值可解释”。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .errors import ConfigSourceError

DEFAULT_SOURCE = "defaults"
FILE_SOURCE = "file"
ENV_SOURCE = "env"
API_SOURCE = "api"

# 数值越小优先级越低
PRIORITY = {
    DEFAULT_SOURCE: 0,
    FILE_SOURCE: 10,
    ENV_SOURCE: 20,
    API_SOURCE: 30,
}

ENV_VALUE_PREFIX = "APP_VALUES_"
ENV_FLAG_PREFIX = "APP_FLAGS_"


@dataclass(frozen=True)
class Loaded:
    """单个来源加载出的原始数据。"""

    name: str
    priority: int
    data: dict[str, Any]
    flags: dict[str, Any]


def _parse_env_scalar(raw: str) -> Any:
    """把环境变量字符串尽量解析成 JSON 标量；失败则保留为字符串。"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _check_section(name: str, section: Any, section_name: str) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ConfigSourceError(
            f"来源 {name!r} 的 {section_name} 段必须是对象，实际为 {type(section).__name__}",
            source=name,
        )
    return section


def load_file(path: str) -> Loaded:
    """从 JSON 文件加载；文件缺失按空数据处理，内容非法则报错。

    文件结构：{"values": {...}, "flags": {...}}
    """
    if not os.path.exists(path):
        return Loaded(FILE_SOURCE, PRIORITY[FILE_SOURCE], {}, {})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigSourceError(
            f"配置文件 {path!r} 无法读取或解析: {exc}", source=FILE_SOURCE
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigSourceError(
            f"配置文件 {path!r} 顶层必须是对象", source=FILE_SOURCE
        )
    data = _check_section(FILE_SOURCE, payload.get("values", {}), "values")
    flags = _check_section(FILE_SOURCE, payload.get("flags", {}), "flags")
    return Loaded(FILE_SOURCE, PRIORITY[FILE_SOURCE], data, flags)


def load_env(
    prefix_values: str = ENV_VALUE_PREFIX,
    prefix_flags: str = ENV_FLAG_PREFIX,
) -> Loaded:
    """从环境变量加载。

    APP_VALUES_<KEY> -> 配置项（标量按 JSON 解析）
    APP_FLAGS_<NAME> -> 开关（按 JSON 解析）
    """
    data: dict[str, Any] = {}
    flags: dict[str, Any] = {}
    for env_key, raw in os.environ.items():
        if env_key.startswith(prefix_values):
            key = env_key[len(prefix_values) :].lower()
            data[key] = _parse_env_scalar(raw)
        elif env_key.startswith(prefix_flags):
            name = env_key[len(prefix_flags) :].lower()
            flags[name] = _parse_env_scalar(raw)
    return Loaded(ENV_SOURCE, PRIORITY[ENV_SOURCE], data, flags)


def make_api_payload(
    values: dict[str, Any] | None = None, flags: dict[str, Any] | None = None
) -> Loaded:
    """把 API 推送体包装成最高优先级来源。"""
    return Loaded(
        API_SOURCE,
        PRIORITY[API_SOURCE],
        dict(values or {}),
        dict(flags or {}),
    )


def merge(
    layers: list[Loaded],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], tuple[str, ...]]:
    """按优先级稳定合并。

    同优先级按入参顺序后者覆盖前者；返回 (values, flags, origins, source_names)。
    """
    result = merge_with_trace(layers)
    origins = dict(result.value_origins)
    origins.update(result.flag_origins)
    return result.values, result.flags, origins, result.source_names


@dataclass(frozen=True)
class MergeResult:
    """合并结果：获胜值 + 逐键来源溯源。

    - value_origins/flag_origins: 键 -> 最终生效来源
    - value_contributors/flag_contributors: 键 -> 提供过该键的来源序列
      （按优先级从低到高），最后一个即获胜来源，其余为被盖掉的冲突方
    """

    values: dict[str, Any]
    flags: dict[str, Any]
    value_origins: dict[str, str]
    flag_origins: dict[str, str]
    value_contributors: dict[str, tuple[str, ...]]
    flag_contributors: dict[str, tuple[str, ...]]
    source_names: tuple[str, ...]


def merge_with_trace(layers: list[Loaded]) -> MergeResult:
    """按优先级稳定合并，并记录每个键的全部贡献来源。"""
    ordered = sorted(layers, key=lambda layer: layer.priority)
    values: dict[str, Any] = {}
    flags: dict[str, Any] = {}
    value_origins: dict[str, str] = {}
    flag_origins: dict[str, str] = {}
    value_contributors: dict[str, list[str]] = {}
    flag_contributors: dict[str, list[str]] = {}
    used: list[str] = []
    for layer in ordered:
        if layer.name not in used:
            used.append(layer.name)
        for key, val in layer.data.items():
            values[key] = val
            value_origins[key] = layer.name
            value_contributors.setdefault(key, []).append(layer.name)
        for name, rule in layer.flags.items():
            flags[name] = rule
            flag_origins[name] = layer.name
            flag_contributors.setdefault(name, []).append(layer.name)
    return MergeResult(
        values=values,
        flags=flags,
        value_origins=value_origins,
        flag_origins=flag_origins,
        value_contributors={k: tuple(v) for k, v in value_contributors.items()},
        flag_contributors={k: tuple(v) for k, v in flag_contributors.items()},
        source_names=tuple(used),
    )
