"""场景三：来源运行期损坏 -> 明确降级、读接口可区分、合法提交后自动恢复。"""
from __future__ import annotations

import json

import pytest

from app.errors import ConfigError, ConfigUnavailableError
from app.manager import STATE_ACTIVE, STATE_DEGRADED, STATE_UNINITIALIZED, ConfigManager
from app.schema import default_spec
from app.sources import make_api_payload


def _write(path, payload):
    path.write_text(json.dumps(payload))


@pytest.fixture
def file_manager(tmp_path):
    cfg = tmp_path / "cfg.json"
    _write(
        cfg,
        {"values": {"app_name": "file-app", "db_password": "fp", "max_connections": 10}},
    )
    manager = ConfigManager(default_spec(), file_path=str(cfg))
    result = manager.refresh()
    assert result.ok
    return manager, cfg


def test_broken_source_enters_degraded_state(file_manager):
    manager, cfg = file_manager

    cfg.write_text("{ broken json !!!")  # 运行期文件损坏
    result = manager.refresh()

    assert result.ok is False
    assert result.state == STATE_DEGRADED
    assert result.error_type == "ConfigSourceError"
    assert manager.state == STATE_DEGRADED
    status = manager.status()
    assert status["state"] == "degraded"
    assert status["source"] == "file"
    assert status["kind"] == "source"
    assert "cfg.json" in status["detail"]
    assert status["failed_at"]
    print(f"[输入] 运行期把配置文件改成损坏 JSON 后刷新 => [依据] 降级 status={status}")


def test_degraded_reads_raise_distinguishable_error_not_generic_500(file_manager):
    manager, cfg = file_manager
    cfg.write_text("not-json-at-all")
    manager.refresh()

    with pytest.raises(ConfigUnavailableError) as ei:
        manager.current()
    exc = ei.value
    assert exc.source == "file"
    assert exc.kind == "source"
    print(f"[输入] 降级态读取配置/开关 => [依据] 抛 ConfigUnavailableError(source={exc.source}, kind={exc.kind})，非笼统内部错误")


def test_degraded_does_not_silently_keep_serving_old_value(file_manager):
    """降级后旧值必须立即下线，不能假装没事继续沿用。"""
    manager, cfg = file_manager
    assert manager.current().values["app_name"] == "file-app"
    cfg.write_text("{bad")
    manager.refresh()
    assert manager.state == STATE_DEGRADED
    with pytest.raises(ConfigUnavailableError):
        manager.current()
    print("[输入] 来源损坏 => [依据] 旧快照 file-app 立即下线，读路径不再返回旧值")


def test_invalid_structural_content_also_degrades(tmp_path):
    """文件能解析但结构非法（如顶层是数组）也属于来源故障。"""
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"values": {"app_name": "a-app", "db_password": "p"}}))
    manager = ConfigManager(default_spec(), file_path=str(cfg))
    assert manager.refresh().ok
    cfg.write_text(json.dumps([1, 2, 3]))
    result = manager.refresh()
    assert result.state == STATE_DEGRADED
    print(f"[输入] 文件顶层被改成数组 => [依据] {result.error_type}: {result.reason}")


def test_semantically_invalid_api_push_does_not_degrade(file_manager):
    """API 推送语义非法：拒绝并保留当前版本，不进入降级态。"""
    manager, _ = file_manager
    result = manager.reload_from(
        [make_api_payload(values={"app_name": "x", "db_password": "p", "max_connections": 0})]
    )
    assert result.ok is False
    assert manager.state == STATE_ACTIVE
    assert manager.current().values["app_name"] == "file-app"
    print("[输入] API 推送越界值 => [依据] 422 拒绝、保持 active，未误伤为降级")


def test_degraded_state_persists_until_valid_submission(file_manager):
    """文件没修好时，重试刷新仍停在降级态，不会自动“恢复”到旧值。"""
    manager, cfg = file_manager
    cfg.write_text("{still broken")
    assert manager.refresh().state == STATE_DEGRADED
    assert manager.refresh().state == STATE_DEGRADED
    assert manager.state == STATE_DEGRADED
    print("[输入] 损坏未修复，连续两次刷新 => [依据] 持续 degraded，不偷偷恢复旧值")


def test_recovers_automatically_after_valid_config(file_manager):
    """来源修复后下一次合法刷新成功，自动恢复为正常状态。"""
    manager, cfg = file_manager
    cfg.write_text("{broken")
    manager.refresh()
    assert manager.state == STATE_DEGRADED

    _write(
        cfg,
        {"values": {"app_name": "restored-app", "db_password": "new-pw", "max_connections": 77}},
    )
    result = manager.refresh()
    assert result.ok
    assert result.state == STATE_ACTIVE
    assert manager.state == STATE_ACTIVE
    snap = manager.current()
    assert snap.values["app_name"] == "restored-app"
    assert snap.values["max_connections"] == 77
    print(f"[输入] 修复文件后重新刷新 => [依据] 自动恢复 active，生效 v{snap.version} restored-app/77")


def test_version_keeps_monotonic_after_recovery(file_manager):
    manager, cfg = file_manager
    v_before = manager.current().version
    cfg.write_text("{broken")
    manager.refresh()
    cfg.write_text(json.dumps({"values": {"app_name": "back-app", "db_password": "p"}}))
    result = manager.refresh()
    assert result.ok
    assert result.version > v_before
    print(f"[输入] 降级后恢复 => [依据] 版本号单调递增 {v_before} -> {result.version}")


def test_startup_with_broken_source_is_uninitialized_not_crash(tmp_path):
    """启动时来源即损坏：进入未初始化态，等待合法提交，而不是抛异常起不来。"""
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{broken")
    manager = ConfigManager(default_spec(), file_path=str(cfg))
    result = manager.refresh()
    assert result.ok is False
    assert manager.state == STATE_UNINITIALIZED
    with pytest.raises(ConfigError):
        manager.current()
    print("[输入] 启动时配置文件就损坏 => [依据] 服务不崩溃，进入 uninitialized 等待合法提交")


def test_history_retained_across_degradation_and_recovery(file_manager):
    manager, cfg = file_manager
    _write(cfg, {"values": {"app_name": "v2-app", "db_password": "p2"}})
    manager.refresh()
    versions_before = [h["version"] for h in manager.history()]
    cfg.write_text("{broken")
    manager.refresh()
    # 降级不清空历史，便于运维回看
    assert [h["version"] for h in manager.history()] == versions_before
    _write(cfg, {"values": {"app_name": "v3-app", "db_password": "p3"}})
    manager.refresh()
    assert manager.state == STATE_ACTIVE
    print(f"[输入] 降级前后 => [依据] 历史版本 {[h['version'] for h in manager.history()]} 全程保留")
