"""刷新失败必须保留上一份可用配置，并说明可区分的失败原因。"""
from __future__ import annotations

from app.sources import make_api_payload


def test_failed_refresh_keeps_previous_snapshot(manager, valid_values):
    old = manager.current()
    result = manager.reload_from(
        [
            make_api_payload(
                values={**valid_values, "max_connections": 9_999_999},
            )
        ]
    )
    assert result.ok is False
    assert result.error_type == "ConfigRangeError"
    assert result.reason and "max_connections" in result.reason
    # 旧版本原样保留
    assert manager.current() is old
    assert manager.current().values["max_connections"] == 100
    print(f"[输入] max_connections=9999999 => [依据] 拒绝类型 {result.error_type}: {result.reason}")


def test_type_error_refresh_rolls_back(manager, valid_values):
    old_version = manager.current().version
    result = manager.reload_from(
        [make_api_payload(values={**valid_values, "enable_cache": "no"})]
    )
    assert result.ok is False
    assert result.error_type == "ConfigTypeError"
    assert manager.current().version == old_version
    print(f"[输入] enable_cache='no' => [依据] {result.error_type}: {result.reason}")


def test_format_error_refresh_rolls_back(manager):
    result = manager.reload_from(
        [make_api_payload(values={"app_name": "Bad Name", "db_password": "s"})]
    )
    assert result.ok is False
    assert result.error_type == "ConfigFormatError"


def test_successful_refresh_switches_version(manager, valid_values):
    result = manager.reload_from(
        [
            make_api_payload(
                values={**valid_values, "log_level": "debug", "max_connections": 222},
                flags={"dark_mode": {"mode": "percentage", "percentage": 25}},
            )
        ]
    )
    assert result.ok is True
    snap = manager.current()
    assert snap.version == 2
    assert snap.values["log_level"] == "debug"
    assert snap.values["max_connections"] == 222
    assert snap.flags["dark_mode"].percentage == 25
    print("[输入] 合法新配置 => [依据] 原子切换到版本 2，旧版本不再可见")


def test_partial_bad_payload_rejected_as_a_whole(manager):
    """非法项即使混在合法项中，整份配置也必须被拒绝（不半生效）。"""
    before = manager.current()
    result = manager.reload_from(
        [
            make_api_payload(
                values={"app_name": "a-app", "db_password": "s", "log_level": "nope"}
            )
        ]
    )
    assert result.ok is False
    assert manager.current() is before
