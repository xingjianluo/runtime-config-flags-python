"""FastAPI 入口：配置查询/脱敏、刷新、开关判定。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    ConfigError,
    ConfigFormatError,
    ConfigMissingError,
    ConfigRangeError,
    ConfigSourceError,
    ConfigTypeError,
    ConfigUnavailableError,
)
from .flags import (
    Evaluation,
    UnknownFlagError,
    evaluate,
    request_scope,
)
from .manager import ConfigManager
from .schema import SENSITIVE_PLACEHOLDER, default_spec, redact
from .sources import make_api_payload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("runtime_config")

# 错误分类 -> HTTP 状态码（error_type 供调用方区分失败原因）
_ERROR_STATUS = {
    ConfigMissingError: ("missing", 422),
    ConfigTypeError: ("type", 422),
    ConfigRangeError: ("range", 422),
    ConfigFormatError: ("format", 422),
    ConfigSourceError: ("source", 400),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager: ConfigManager = app.state.manager
    result = manager.refresh()
    if result.ok:
        logger.info("启动加载配置成功，版本 %s", result.version)
    else:
        # 不静默沿用：服务启动但处于未初始化态，读取接口显式报错
        logger.error("启动加载配置失败：[%s] %s", result.error_type, result.reason)
    yield


app = FastAPI(title="runtime-config-flags", lifespan=lifespan)
app.state.manager = ConfigManager(
    default_spec(), file_path=os.environ.get("CONFIG_FILE")
)


def get_manager(request: Request) -> ConfigManager:
    return request.app.state.manager


def _require_snapshot(manager: ConfigManager):
    """读取当前快照；不可用时返回说明降级/未初始化的 503 响应。"""
    try:
        return manager.current()
    except ConfigUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "config_unavailable",
                "state": "degraded",
                "reason": "配置来源故障，当前暂时没有可用配置",
                "source": exc.source,
                "kind": exc.kind,
                "detail": exc.detail,
            },
        )
    except ConfigError:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "uninitialized",
                "state": "uninitialized",
                "reason": "当前没有任何可用配置，请先通过 POST /refresh 推送合法配置",
            },
        )


@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError):
    for cls, (code, status) in _ERROR_STATUS.items():
        if isinstance(exc, cls):
            return JSONResponse(
                status_code=status,
                content={
                    "ok": False,
                    "error_type": code,
                    "reason": exc.message,
                    "source": exc.source,
                    "key": exc.key,
                },
            )
    if isinstance(exc, UnknownFlagError):
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error_type": "unknown_flag", "reason": exc.message},
        )
    return JSONResponse(  # pragma: no cover
        status_code=500,
        content={"ok": False, "error_type": "unknown", "reason": exc.message},
    )


class RefreshRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)


class FlagCheckRequest(BaseModel):
    user_id: str | None = None
    groups: list[str] | None = None


@app.get("/health")
def health(manager: ConfigManager = Depends(get_manager)):
    status = manager.status()
    if status["state"] == "active":
        return {"ok": True, **status}
    if status["state"] == "degraded":
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "config_unavailable",
                **status,
            },
        )
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error_type": "uninitialized", **status},
    )


@app.get("/config")
def get_config(manager: ConfigManager = Depends(get_manager)):
    """返回脱敏配置、版本、来源与每个字段的出处（敏感值永不外泄）。

    另带 shadowed：本次生效版本提交时被高优先级来源挡下的低优先级合法取值。
    """
    snap_or_resp = _require_snapshot(manager)
    if not hasattr(snap_or_resp, "version"):
        return snap_or_resp
    snap = snap_or_resp
    return {
        "version": snap.version,
        "sources": list(snap.sources),
        "values": redact(snap, manager.spec),
        "field_origins": snap.field_origins,
        "shadowed": [
            {
                "key": s.key,
                "source": s.source,
                "winning_source": s.winning_source,
                "section": s.section,
            }
            for s in manager.last_shadowed()
        ],
        "sensitive_placeholder": SENSITIVE_PLACEHOLDER,
    }


@app.post("/refresh")
def refresh(
    payload: RefreshRequest | None = None,
    manager: ConfigManager = Depends(get_manager),
):
    """运行时刷新：文件+环境+本次推送（API 优先级最高）。

    - 语义校验失败：422，一次性给出所有问题（key/分类/来源），保留当前版本；
    - 来源故障：503，进入降级态并说明来源与故障类别；
    - 成功：200，返回版本、来源以及被挡下的低优先级冲突 shadowed。
    """
    payload = payload or RefreshRequest()
    result = manager.refresh(
        make_api_payload(values=payload.values, flags=payload.flags)
    )
    if result.state == "degraded":
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "config_unavailable",
                "state": "degraded",
                "detail_error_type": result.error_type,
                "reason": result.reason,
                "retained_version": result.version,
            },
        )
    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_type": "refresh_rejected",
                "detail_error_type": result.error_type,
                "reason": result.reason,
                "retained_version": result.version,
                "issues": [
                    {
                        "key": i.key,
                        "category": i.category,
                        "source": i.source,
                        "section": i.section,
                        "message": i.message,
                    }
                    for i in result.issues
                ],
            },
        )
    return {
        "ok": True,
        "version": result.version,
        "sources": list(result.sources),
        "rolled_back_from": result.rolled_back_from,
        "shadowed": [
            {
                "key": s.key,
                "source": s.source,
                "winning_source": s.winning_source,
                "section": s.section,
            }
            for s in result.shadowed
        ],
    }


@app.get("/versions")
def list_versions(manager: ConfigManager = Depends(get_manager)):
    """历史版本清单：版本号、来源、切换时间、是否为回退产物。"""
    return {
        "current_version": (
            manager.current().version if manager.state == "active" else None
        ),
        "state": manager.state,
        "history_limit": manager.history_limit,
        "versions": manager.history(),
    }


@app.post("/versions/{version}/rollback")
def rollback_version(
    version: int, manager: ConfigManager = Depends(get_manager)
):
    """一键回退：按与普通刷新相同的规则校验目标版本，失败则停在当前版本。"""
    result = manager.rollback(version)
    if result.state == "degraded" and not result.ok:
        # 回退的层来自历史快照，理论上不含来源故障；保留防御性分支
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "config_unavailable",
                "state": "degraded",
                "reason": result.reason,
            },
        )
    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_type": "rollback_rejected",
                "detail_error_type": result.error_type,
                "reason": result.reason,
                "retained_version": result.version,
                "issues": [
                    {
                        "key": i.key,
                        "category": i.category,
                        "source": i.source,
                        "section": i.section,
                        "message": i.message,
                    }
                    for i in result.issues
                ],
            },
        )
    return {
        "ok": True,
        "version": result.version,
        "rolled_back_from": result.rolled_back_from,
        "sources": list(result.sources),
    }


@app.get("/flags")
def list_flags(manager: ConfigManager = Depends(get_manager)):
    snap_or_resp = _require_snapshot(manager)
    if not hasattr(snap_or_resp, "version"):
        return snap_or_resp
    snap = snap_or_resp
    out = []
    for fspec in manager.spec.flags:
        rule = snap.flags[fspec.name]
        out.append(
            {
                "name": fspec.name,
                "mode": rule.mode,
                "percentage": rule.percentage,
                "groups": sorted(rule.groups),
                "deprecated": fspec.deprecated,
                "replacement": fspec.replacement,
            }
        )
    return {"version": snap.version, "flags": out}


@app.post("/flags/{name}/check")
def check_flag(
    name: str,
    payload: FlagCheckRequest | None = None,
    manager: ConfigManager = Depends(get_manager),
):
    """在请求级上下文中判定开关，多次判定结果一致，返回可解释依据。"""
    payload = payload or FlagCheckRequest()
    snap_or_resp = _require_snapshot(manager)
    if not hasattr(snap_or_resp, "version"):
        return snap_or_resp
    snap = snap_or_resp
    with request_scope():
        result: Evaluation = evaluate(
            snap,
            manager.spec,
            name,
            user_id=payload.user_id,
            groups=payload.groups,
        )
    body = {
        "flag": result.flag,
        "enabled": result.enabled,
        "mode": result.mode,
        "reason": result.reason,
        "deprecated": result.deprecated,
        "replacement": result.replacement,
        "version": snap.version,
    }
    return JSONResponse(status_code=200, content=body)


@app.get("/")
def read_root():
    return {"service": "runtime-config-flags", "docs": "/docs"}
