"""公共测试夹具与日志校验辅助。"""
from __future__ import annotations

import logging

import pytest

from app.manager import ConfigManager
from app.schema import Spec, default_spec
from app.sources import make_api_payload


@pytest.fixture
def spec() -> Spec:
    return default_spec()


@pytest.fixture
def valid_values() -> dict:
    return {"app_name": "my-app", "db_password": "secret"}


@pytest.fixture
def manager(spec, valid_values) -> ConfigManager:
    return ConfigManager(
        spec,
        initial_layers=[
            make_api_payload(values=valid_values, flags={"new_checkout": True})
        ],
    )


@pytest.fixture
def caplog_info(caplog):
    """捕获 runtime_config 日志，并回显触发输入与判定依据。"""
    caplog.set_level(logging.INFO, logger="runtime_config")
    yield caplog
    for record in caplog.records:
        if record.name == "runtime_config":
            print(f"[LOG {record.levelname}] {record.getMessage()}")
