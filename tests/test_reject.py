"""非法配置拒绝：缺项/类型/越界/格式/来源错误均可区分，绝不静默回退。"""
from __future__ import annotations

import pytest

from dynconfig.errors import (
    RefreshError,
    SourceError,
)
from dynconfig.parser import build_snapshot
from dynconfig.sources import PRIORITY_API, PRIORITY_DEFAULT, RawSource, load_file

from .conftest import case_log


def codes(exc: RefreshError) -> dict[str, str]:
    return {r["field"]: r["code"] for r in exc.reasons}


def test_missing_required(schema):
    case_log.info("触发输入: 仅提供 log.level（缺其余必填项）")
    with pytest.raises(RefreshError) as ei:
        build_snapshot(
            [RawSource("default", PRIORITY_DEFAULT, {"log.level": "info"})],
            schema, 1)
    got = codes(ei.value)
    case_log.info("拒绝依据=%r", got)
    assert got["http.port"] == "missing_field"
    assert got["sample.ratio"] == "missing_field"
    assert got["service.name"] == "missing_field"


def test_type_mismatch(schema, base_values):
    bad = {"http.port": "abc", "sample.ratio": [1, 2], "log.level": 7}
    case_log.info("触发输入(类型非法)=%r", bad)
    with pytest.raises(RefreshError) as ei:
        build_snapshot(
            [RawSource("default", PRIORITY_DEFAULT, base_values),
             RawSource("api", PRIORITY_API, bad)], schema, 1)
    got = codes(ei.value)
    case_log.info("拒绝依据=%r", got)
    assert got["http.port"] == "type_error"
    assert got["sample.ratio"] == "type_error"
    assert got["log.level"] == "type_error"


def test_out_of_range(schema, base_values):
    bad = {"http.port": 70000, "sample.ratio": 1.5}
    case_log.info("触发输入(越界)=%r", bad)
    with pytest.raises(RefreshError) as ei:
        build_snapshot(
            [RawSource("default", PRIORITY_DEFAULT, base_values),
             RawSource("api", PRIORITY_API, bad)], schema, 1)
    got = codes(ei.value)
    case_log.info("拒绝依据=%r", got)
    assert got["http.port"] == "out_of_range"
    assert got["sample.ratio"] == "out_of_range"


def test_format_errors(schema, base_values):
    bad = {"log.level": "trace", "service.name": "Invalid_Name"}
    case_log.info("触发输入(格式/枚举非法)=%r", bad)
    with pytest.raises(RefreshError) as ei:
        build_snapshot(
            [RawSource("default", PRIORITY_DEFAULT, base_values),
             RawSource("api", PRIORITY_API, bad)], schema, 1)
    got = codes(ei.value)
    case_log.info("拒绝依据=%r", got)
    assert got["log.level"] == "format_error"
    assert got["service.name"] == "format_error"


def test_unknown_keys_and_flags(schema, base_values):
    bad = {"unknown.key": 1,
           "flags": {"ghost": {"state": "on"},
                     "new_checkout": {"rollout": 101, "bogus": 1}}}
    case_log.info("触发输入(未知项/开关越界)=%r", bad)
    with pytest.raises(RefreshError) as ei:
        build_snapshot(
            [RawSource("default", PRIORITY_DEFAULT, base_values),
             RawSource("api", PRIORITY_API, bad)], schema, 1)
    got = codes(ei.value)
    case_log.info("拒绝依据=%r", got)
    assert got["unknown.key"] == "format_error"
    assert got["flags.ghost"] == "format_error"
    assert got["flags.new_checkout.rollout"] == "out_of_range"
    assert got["flags.new_checkout.bogus"] == "format_error"


def test_invalid_value_in_lower_source_still_rejected(schema, base_values):
    """即使高优先级提供了合法值，低优先级的非法值也不得被静默放过。"""
    with pytest.raises(RefreshError) as ei:
        build_snapshot([
            RawSource("default", PRIORITY_DEFAULT, base_values),
            RawSource("file:x", 10, {"http.port": "oops"}),
            RawSource("api", PRIORITY_API, {"http.port": 9090}),
        ], schema, 1)
    assert any(r["field"] == "http.port" and r["code"] == "type_error"
               and r["source"] == "file:x" for r in ei.value.reasons)


def test_broken_file_source(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    case_log.info("触发输入: 损坏文件 %s", p)
    with pytest.raises(SourceError) as ei:
        load_file(p)
    case_log.info("拒绝依据 code=%s message=%s", ei.value.code, ei.value.message)
    assert ei.value.code == "source_error"


def test_missing_file_is_empty_layer(tmp_path):
    src = load_file(tmp_path / "absent.json")
    assert src.values == {}


def test_bool_is_not_int(schema, base_values):
    with pytest.raises(RefreshError) as ei:
        build_snapshot([
            RawSource("default", PRIORITY_DEFAULT, base_values),
            RawSource("api", PRIORITY_API, {"http.port": True}),
        ], schema, 1)
    assert codes(ei.value)["http.port"] == "type_error"
