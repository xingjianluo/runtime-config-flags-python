"""来源优先级：default < file < env < api，高优先级逐字段覆盖，来源可追溯。"""
from __future__ import annotations

from dynconfig.parser import build_snapshot
from dynconfig.sources import (
    PRIORITY_API,
    PRIORITY_DEFAULT,
    PRIORITY_ENV,
    PRIORITY_FILE,
    RawSource,
)

from .conftest import case_log


def test_high_priority_wins_per_field(schema, base_values):
    sources = [
        RawSource("default", PRIORITY_DEFAULT, base_values),
        RawSource("file:cfg.json", PRIORITY_FILE,
                  {"http.port": 9000, "sample.ratio": 0.9}),
        RawSource("env", PRIORITY_ENV,
                  {"http.port": "9090"}, literal=True),
        RawSource("api", PRIORITY_API, {"log.level": "warn"}),
    ]
    snap = build_snapshot(sources, schema, 1)
    case_log.info("输入来源=%r -> 合并结果=%r",
                  [(s.name, s.priority, s.values) for s in sources], snap.values)

    assert snap.values["http.port"] == 9090
    assert snap.traces["http.port"].source == "env"
    assert snap.values["log.level"] == "warn"
    assert snap.traces["log.level"].source == "api"
    # 文件提供但未被更高层覆盖的字段保留文件层
    assert snap.values["sample.ratio"] == 0.9
    assert snap.traces["sample.ratio"].source == "file:cfg.json"
    # 无人覆盖的字段落在默认层
    assert snap.traces["service.name"].source == "default"


def test_env_literal_conversion(schema, base_values):
    """环境变量字符串按字段类型受控转换。"""
    sources = [
        RawSource("default", PRIORITY_DEFAULT, base_values),
        RawSource("env", PRIORITY_ENV,
                  {"sample.ratio": "0.25", "log.level": "debug",
                   "http.port": "80"}, literal=True),
    ]
    snap = build_snapshot(sources, schema, 1)
    case_log.info("环境变量字面量输入=%r -> 转换=%r",
                  sources[1].values,
                  {k: (v, type(v).__name__) for k, v in snap.values.items()})
    assert snap.values["sample.ratio"] == 0.25
    assert isinstance(snap.values["http.port"], int)
    assert snap.values["log.level"] == "debug"


def test_flag_override_merge(schema, base_values):
    """开关规则只覆盖被指定的属性，其余属性沿用 Schema 基线。"""
    sources = [
        RawSource("default", PRIORITY_DEFAULT, base_values),
        RawSource("api", PRIORITY_API,
                  {"flags": {"new_checkout": {"rollout": 55}}}),
    ]
    snap = build_snapshot(sources, schema, 1)
    rule = snap.flags["new_checkout"]
    case_log.info("开关覆盖输入 flags.new_checkout.rollout=55 -> 规则=%r", rule)
    assert rule.state == "conditional"
    assert rule.rollout == 55
    assert rule.groups == frozenset({"beta", "staff"})
