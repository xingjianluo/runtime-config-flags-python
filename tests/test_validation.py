"""非法配置必须被整体拒绝，并给出可区分的错误类型。"""
from __future__ import annotations

import pytest

from app.errors import (
    ConfigFormatError,
    ConfigMissingError,
    ConfigRangeError,
    ConfigTypeError,
)
from app.schema import build_snapshot, validate_field, validate_flag
from app.sources import make_api_payload


def _build(spec, values, flags=None, origins=None):
    return build_snapshot(
        spec,
        values,
        flags or {},
        version=1,
        sources=("api",),
        origins=origins or {k: "api" for k in values},
    )


def test_missing_required_field_rejected(spec):
    with pytest.raises(ConfigMissingError) as ei:
        _build(spec, {"app_name": "my-app"})
    assert ei.value.key == "db_password"
    print(f"[输入] values={{'app_name':'my-app'}} => [依据] {ei.value.message}")


def test_type_mismatch_int_rejected(spec):
    fspec = spec.field_map()["max_connections"]
    with pytest.raises(ConfigTypeError) as ei:
        validate_field(fspec, "500", source="api")
    assert ei.value.key == "max_connections"
    print(f"[输入] max_connections='500' => [依据] {ei.value.message}")


def test_bool_is_not_accepted_as_int(spec):
    fspec = spec.field_map()["max_connections"]
    with pytest.raises(ConfigTypeError):
        validate_field(fspec, True, source="api")


def test_range_below_and_above_rejected(spec):
    fspec = spec.field_map()["max_connections"]
    with pytest.raises(ConfigRangeError):
        validate_field(fspec, 0, source="api")
    with pytest.raises(ConfigRangeError) as ei:
        validate_field(fspec, 10_001, source="api")
    assert "上限" in ei.value.message
    print(f"[输入] max_connections=10001 => [依据] {ei.value.message}")


def test_float_out_of_range_rejected(spec):
    fspec = spec.field_map()["sample_rate"]
    with pytest.raises(ConfigRangeError):
        validate_field(fspec, 1.5, source="api")


def test_choice_violation_is_format_error(spec):
    fspec = spec.field_map()["log_level"]
    with pytest.raises(ConfigFormatError) as ei:
        validate_field(fspec, "trace", source="api")
    print(f"[输入] log_level='trace' => [依据] {ei.value.message}")


def test_pattern_violation_is_format_error(spec):
    with pytest.raises(ConfigFormatError) as ei:
        _build(spec, {"app_name": "INVALID NAME", "db_password": "s"})
    assert ei.value.key == "app_name"
    print(f"[输入] app_name='INVALID NAME' => [依据] {ei.value.message}")


def test_unknown_flag_rejected(spec):
    with pytest.raises(ConfigFormatError) as ei:
        _build(spec, {"app_name": "a-app", "db_password": "s"}, {"ghost": True})
    assert "ghost" in ei.value.message


def test_flag_percentage_out_of_range():
    with pytest.raises(ConfigRangeError):
        validate_flag("f", {"mode": "percentage", "percentage": 101})


def test_flag_percentage_wrong_type():
    with pytest.raises(ConfigTypeError):
        validate_flag("f", {"mode": "percentage", "percentage": "50"})


def test_flag_groups_must_be_nonempty_strings():
    with pytest.raises(ConfigFormatError):
        validate_flag("f", {"mode": "allow_groups", "groups": []})
    with pytest.raises(ConfigTypeError):
        validate_flag("f", {"mode": "allow_groups", "groups": [1, 2]})


def test_flag_duplicate_groups_rejected():
    with pytest.raises(ConfigFormatError):
        validate_flag("f", {"mode": "allow_groups", "groups": ["a", "a"]})


def test_flag_bad_mode_string_rejected():
    with pytest.raises(ConfigFormatError):
        validate_flag("f", "halfway")


def test_optional_fields_fall_back_to_defaults(spec):
    snap = _build(spec, {"app_name": "a-app", "db_password": "s"})
    assert snap.values["log_level"] == "info"
    assert snap.values["max_connections"] == 100
    assert snap.field_origins["log_level"] == "defaults"
