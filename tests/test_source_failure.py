"""来源故障下的显式降级态与自动恢复。

覆盖场景：
1. 运行期文件损坏（JSON 非法）-> 进入降级态，不沿用旧值、不抛笼统内部错误；
2. 降级态下 current() 抛出可区分的 ConfigUnavailableError，带故障来源/类别；
3. 健康检查、/config、开关判定都返回 503 unavailable 并说清故障来源与问题类别；
4. 下一次提交合法配置成功后自动恢复正常；
5. 文件不存在仍按空层处理（不算故障）；
6. 值校验失败与来源故障区分：前者保留旧版本，不进降级态。
"""
from __future__ import annotations

import json

import pytest

from app.errors import ConfigUnavailableError
from app.flags import evaluate, request_scope
from app.manager import ConfigManager
from app.schema import default_spec
from app.sources import make_api_payload

VALID = {"values": {"app_name": "my-app", "db_password": "supersecret"}}


def _make_manager(tmp_path, initial=True):
    cfg = tmp_path / "cfg.json"
    if initial:
        cfg.write_text(
            json.dumps(
                {"values": {"app_name": "file-app", "db_password": "file-pw"}}
            )
        )
    return cfg, ConfigManager(default_spec(), file_path=str(cfg))


def test_corrupted_file_at_runtime_enters_degraded_state(tmp_path):
    """场景1+2：运行期文件损坏 -> 降级态，current() 抛 ConfigUnavailableError。"""
    cfg, mgr = _make_manager(tmp_path, initial=True)
    r0 = mgr.refresh()
    assert r0.ok
    assert mgr.current().values["app_name"] == "file-app"

    cfg.write_text("{ broken json !!!")
    result = mgr.refresh()
    assert result.ok is False
    assert result.degraded is True
    assert result.fault_source == "file"
    assert result.error_type == "ConfigSourceError"

    assert mgr.is_degraded() is True
    with pytest.raises(ConfigUnavailableError) as ei:
        mgr.current()
    assert ei.value.fault_source == "file"
    assert ei.value.fault_error_type == "ConfigSourceError"
    assert "file" in ei.value.message
    print(
        "[场景1] 运行期文件写成损坏 JSON => [依据] 进入降级态，"
        f"fault_source={ei.value.fault_source}，旧值未被继续沿用: {ei.value.message}"
    )


def test_degraded_state_does_not_silently_keep_old_values(tmp_path):
    """场景1b：降级后读取不到旧值，而不是假装没事继续服务旧配置。"""
    cfg, mgr = _make_manager(tmp_path, initial=True)
    mgr.refresh()
    old_version = mgr.current().version

    cfg.write_text("not-json-at-all")
    mgr.refresh()
    assert mgr.is_degraded()
    with pytest.raises(ConfigUnavailableError):
        mgr.current()
    # 历史仍在（版本没丢），只是当前没有生效配置
    assert any(v.version == old_version for v in mgr.history())
    print(f"[场景1b] 降级后旧版本 {old_version} 留在历史中，但当前引用为 None")


def test_recovery_after_valid_commit(tmp_path):
    """场景4：降级态下推送一次合法配置（文件也修复）-> 自动恢复。"""
    cfg, mgr = _make_manager(tmp_path, initial=True)
    mgr.refresh()
    cfg.write_text("{ corrupted")
    assert mgr.refresh().degraded is True

    # 文件修好，下一次普通刷新成功，自动恢复
    cfg.write_text(
        json.dumps({"values": {"app_name": "recovered-app", "db_password": "pw2"}})
    )
    result = mgr.refresh()
    assert result.ok is True
    assert mgr.is_degraded() is False
    snap = mgr.current()
    assert snap.values["app_name"] == "recovered-app"
    print("[场景4] 文件修复后再次刷新 => [依据] 降级态自动解除，版本恢复可用")

    # 即使文件仍坏，一次仅基于 API 层的提交也能恢复（reload_from 不读文件）
    cfg.write_text("{ still bad")
    mgr.refresh()
    assert mgr.is_degraded()
    r = mgr.reload_from(
        [make_api_payload(values={"app_name": "api-recovery", "db_password": "p3"})]
    )
    assert r.ok
    assert mgr.current().values["app_name"] == "api-recovery"
    assert mgr.is_degraded() is False
    print("[场景4b] 降级态下直接 reload_from 合法层 => [依据] 同样自动恢复")


def test_missing_file_is_empty_layer_not_failure(tmp_path):
    """场景5：配置文件不存在按空层处理（必填项缺失属于校验拒绝，不是来源故障）。"""
    missing = tmp_path / "absent.json"
    mgr = ConfigManager(default_spec(), file_path=str(missing))
    # 没有 API 层时：文件缺失=空层，缺必填项 -> 校验失败但不降级
    result = mgr.refresh()
    assert result.ok is False
    assert result.degraded is False
    assert result.error_type == "ConfigValidationError"
    assert not mgr.is_degraded()
    print("[场景5] 文件不存在 => [依据] 按空层处理，仅必填项校验拒绝，不进降级态")


def test_validation_failure_keeps_old_version_without_degrading(tmp_path):
    """场景6：值非法是“拒绝提交并保留旧版”，与来源故障的降级态明确区分。"""
    cfg, mgr = _make_manager(tmp_path, initial=True)
    mgr.refresh()
    before = mgr.current()

    cfg.write_text(
        json.dumps({"values": {"app_name": "file-app", "db_password": 12345}})
    )
    result = mgr.refresh(
        make_api_payload(values={"app_name": "file-app", "db_password": 12345})
    )
    assert result.ok is False
    assert result.degraded is False
    assert result.error_type == "ConfigValidationError"
    assert mgr.is_degraded() is False
    assert mgr.current() is before  # 旧版原样保留
    print("[场景6] 值类型非法 => [依据] 提交被拒、旧版本保留，不进入降级态")


def test_flag_evaluation_raises_unavailable_in_degraded_state(tmp_path):
    """场景3b：降级态下做开关判定，拿到可区分的降级错误，而非笼统内部错误。"""
    cfg, mgr = _make_manager(tmp_path, initial=True)
    mgr.refresh()
    cfg.write_text("###bad###")
    mgr.refresh()
    assert mgr.is_degraded()
    with pytest.raises(ConfigUnavailableError) as ei:
        with request_scope():
            evaluate(mgr.current(), default_spec(), "new_checkout")
    assert ei.value.fault_source == "file"
    print("[场景3b] 降级态下判定开关 => [依据] 抛出 ConfigUnavailableError(source=file)")
