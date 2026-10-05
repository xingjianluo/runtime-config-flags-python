"""公共测试夹具与日志校验辅助。"""
from __future__ import annotations

import logging

import pytest

from app.manager import ConfigManager
from app.schema import Spec, default_spec
from app.sources import make_api_payload

# 测试模块 -> 业务场景名称；pytest -s 时每个用例都会带场景抬头打印
SCENARIOS = {
    "test_validation": "场景：提交值校验（类型/范围/格式/缺项/未知开关）",
    "test_sources_priority": "场景：多来源稳定优先级与来源追踪",
    "test_refresh_rollback": "场景：刷新失败回退保留旧版本",
    "test_flags_and_redaction": "场景：开关放量语义/请求内一致性/敏感脱敏",
    "test_concurrency": "场景：并发刷新读取一致性",
    "test_http_api": "场景：HTTP 端到端接口",
    "test_version_history_rollback": "场景一：版本历史、容量淘汰与一键回退",
    "test_strict_commit": "场景二：严格提交校验、多问题聚合与来源可解释",
    "test_degraded_state": "场景三：来源故障降级与自动恢复",
}


@pytest.fixture(autouse=True)
def _print_scenario_case(request):
    module = request.module.__name__.rsplit(".", 1)[-1]
    scenario = SCENARIOS.get(module, "场景：未分类")
    print(f"\n===== {scenario} | 用例：{request.node.name} =====")
    yield


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
