"""多来源配置加载：默认值 < 配置文件 < 环境变量 < 管理API。

优先级数值越大越优先（DEFAULT=0 < FILE=10 < ENV=20 < API=30）。
各来源输出原始键值，由 parser 统一做类型与边界校验：
文件/API 提供 JSON 原生类型，环境变量提供字符串，解析规则按来源区分。
任何来源文件存在但损坏/不可解析都抛 SourceError，绝不静默忽略。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynconfig.errors import SourceError

PRIORITY_DEFAULT = 0
PRIORITY_FILE = 10
PRIORITY_ENV = 20
PRIORITY_API = 30

FLAGS_SECTION = "flags"


@dataclass
class RawSource:
    name: str
    priority: int
    values: dict[str, Any]
    # 字符串型来源（环境变量），允许按受控规则做字面量转换
    literal: bool = False


def load_file(path: str | Path) -> RawSource:
    """从 JSON 文件加载。

    - 文件不存在：返回空来源（该层不提供任何覆盖项，明确规则）；
    - 文件存在但不可读或 JSON 非法：抛 SourceError，不回退默认。
    """
    p = Path(path)
    name = f"file:{p}"
    if not p.exists():
        return RawSource(name=name, priority=PRIORITY_FILE, values={})
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"配置文件不可读: {exc}", source=name) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(
            f"配置文件 JSON 非法: 第{exc.lineno}行第{exc.colno}列 {exc.msg}",
            source=name) from exc
    if not isinstance(data, dict):
        raise SourceError("配置文件顶层必须是 JSON 对象", source=name)
    return RawSource(name=name, priority=PRIORITY_FILE, values=data)


def load_env(prefix: str = "APP_") -> RawSource:
    """从环境变量加载。

    映射规则：APP_LOG__LEVEL -> "log.level"；
    双下划线 `__` 为层级分隔，单下划线保留在键名中。
    值恒为字符串，parser 按字段类型做受控字面量转换。
    """
    values: dict[str, Any] = {}
    for env_key, raw in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        key = env_key[len(prefix):].lower().replace("__", ".")
        if not key:
            continue
        values[key] = raw
    return RawSource(name="env", priority=PRIORITY_ENV,
                     values=values, literal=True)


def api_source(payload: dict[str, Any]) -> RawSource:
    """管理 API 提交的覆盖配置，优先级最高。"""
    return RawSource(name="api", priority=PRIORITY_API, values=payload)


def default_source(values: dict[str, Any]) -> RawSource:
    """内置默认层，优先级最低。"""
    return RawSource(name="default", priority=PRIORITY_DEFAULT, values=values)
