"""应用配置装配：Schema 定义 + 初始来源 + 全局单例管理器。

字段示例覆盖 int/float/bool/enum/str(pattern)/sensitive 各类用法。
默认配置内置于代码（最低优先级层），随后叠加文件与环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path

from dynconfig.manager import ConfigManager
from dynconfig.schema import FieldSpec, FlagRule, Schema
from dynconfig.sources import (
    default_source,
    load_env,
    load_file,
)

DEFAULT_CONFIG_PATH = os.environ.get("APP_CONFIG_FILE", "config/default.json")

DEFAULTS: dict[str, object] = {
    "log.level": "info",
    "http.port": 8080,
    "http.timeout_ms": 5000,
    "sample.ratio": 1.0,
    "feature.tls_enabled": True,
    "service.name": "runtime-config-service",
    # db.password 为可选敏感项：未在任何来源提供时缺省为 None
}

SCHEMA = Schema(
    fields={
        "log.level": FieldSpec(
            "log.level", "enum", choices=("debug", "info", "warn", "error")),
        "http.port": FieldSpec("http.port", "int", min=1, max=65535),
        "http.timeout_ms": FieldSpec("http.timeout_ms", "int", min=1,
                                     max=600000),
        "sample.ratio": FieldSpec("sample.ratio", "float", min=0.0, max=1.0),
        "feature.tls_enabled": FieldSpec("feature.tls_enabled", "bool"),
        "service.name": FieldSpec("service.name", "str",
                                  pattern=r"[a-z][a-z0-9-]{1,31}"),
        "db.password": FieldSpec("db.password", "str", sensitive=True,
                                 required=False, default=None),
    },
    flags={
        # 全量启用 / 全量停用 / 按比例+分组放量 三种形态各一个样例
        "dark_mode": FlagRule("dark_mode", "on"),
        "legacy_export": FlagRule("legacy_export", "off", deprecated=True),
        "new_checkout": FlagRule(
            "new_checkout", "conditional", rollout=10,
            groups=frozenset({"beta", "staff"})),
        "ai_search": FlagRule("ai_search", "conditional", rollout=0),
    },
)


def build_initial_manager(config_path: str | Path | None = None) -> ConfigManager:
    path = Path(config_path or DEFAULT_CONFIG_PATH)

    # 三个 provider 分别产出一层，refresh() 无参调用时会重新读取
    manager = ConfigManager(
        SCHEMA,
        sources=[default_source(DEFAULTS), load_file(path), load_env("APP_")],
        providers=[
            lambda: [default_source(DEFAULTS)],
            lambda: [load_file(path)],
            lambda: [load_env("APP_")],
        ],
    )
    return manager
