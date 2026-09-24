"""共享 fixture 与测试日志：打印触发输入与判定依据。"""
from __future__ import annotations

import logging
import sys

import pytest

from dynconfig.flags import FlagContext
from dynconfig.manager import ConfigManager
from dynconfig.schema import FieldSpec, FlagRule, Schema
from dynconfig.sources import (
    PRIORITY_DEFAULT,
    RawSource,
)

# 让单测日志直接打印触发输入与判定依据
logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
case_log = logging.getLogger("dynconfig.test")


@pytest.fixture
def schema() -> Schema:
    return Schema(
        fields={
            "log.level": FieldSpec(
                "log.level", "enum", choices=("debug", "info", "warn")),
            "http.port": FieldSpec("http.port", "int", min=1, max=65535),
            "sample.ratio": FieldSpec("sample.ratio", "float", min=0.0,
                                      max=1.0),
            "service.name": FieldSpec("service.name", "str",
                                      pattern=r"[a-z][a-z0-9-]{1,31}"),
            "db.password": FieldSpec("db.password", "str", sensitive=True,
                                     required=False, default=None),
        },
        flags={
            "dark_mode": FlagRule("dark_mode", "on"),
            "legacy_export": FlagRule("legacy_export", "off", deprecated=True),
            "new_checkout": FlagRule(
                "new_checkout", "conditional", rollout=10,
                groups=frozenset({"beta", "staff"})),
        },
    )


@pytest.fixture
def base_values() -> dict:
    return {
        "log.level": "info",
        "http.port": 8080,
        "sample.ratio": 0.5,
        "service.name": "svc-a",
    }


@pytest.fixture
def manager(schema, base_values) -> ConfigManager:
    return ConfigManager(
        schema,
        sources=[RawSource("default", PRIORITY_DEFAULT, base_values)],
    )


def ctx(user_id: str | None = None, *groups: str) -> FlagContext:
    return FlagContext(user_id=user_id, groups=frozenset(groups))
