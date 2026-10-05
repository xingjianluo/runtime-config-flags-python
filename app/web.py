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
    ConfigValidationError,
    ConfigUnknownKeyError,
)
from .flags import (
    Evaluation,
    UnknownFlagError,
    evaluate,
    request_scope,
)
from .manager import ConfigManager, RefreshResult, VersionInfo
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
    ConfigUnknownKeyError: ("unknown", 422),
    ConfigFormatError: ("format", 422),
    ConfigValidationError: ("validation", 422),
    ConfigSourceError: ("source", 400),
    ConfigUnavailableError: ("unavailable", 503),
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


def _history_limit_from_env() -> int:
    raw = os.environ.get("CONFIG_HISTORY_LIMIT", "10")
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
    except ValueError:
        logger.warning("CONFIG_HISTORY_LIMIT=%r 非法，回退为 10", raw)
        return 10
    return value


app.state.manager = ConfigManager(
    default_spec(),
    file_path=os.environ.get("CONFIG_FILE"),
    history_limit=_history_limit_from_env(),
)


def get_manager(request: Request) -> ConfigManager:
    return request.app.state.manager


def _require_snapshot(manager: ConfigManager):
    try:
        return manager.current()
    except ConfigUnavailableError as exc:
        return _degraded_response(exc)
    except ConfigError:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "uninitialized",
                "reason": "当前没有任何可用配置，请先通过 POST /refresh 推送合法配置",
            },
        )


def _degraded_response(exc: ConfigUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error_type": "unavailable",
            "reason": exc.message,
            "fault_source": exc.fault_source,
            "fault_error_type": exc.fault_error_type,
        },
    )


@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError):
    if isinstance(exc, ConfigUnavailableError):
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "unavailable",
                "reason": exc.message,
                "fault_source": exc.fault_source,
                "fault_error_type": exc.fault_error_type,
            },
        )
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
    try:
        snap = manager.current()
        return {"ok": True, "status": "ok", "version": snap.version}
    except ConfigUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "status": "degraded",
                "error_type": "unavailable",
                "fault_source": exc.fault_source,
                "fault_error_type": exc.fault_error_type,
                "reason": exc.message,
            },
        )
    except ConfigError:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "status": "uninitialized", "error_type": "uninitialized"},
        )


def _shadows_view(snap) -> dict[str, list[dict[str, str]]]:
    return {
        key: [
            {
                "kind": shadow.kind,
                "source": shadow.source,
                "winning_source": shadow.winning_source,
            }
            for shadow in shadows
        ]
        for key, shadows in snap.shadows.items()
    }


@app.get("/config")
def get_config(manager: ConfigManager = Depends(get_manager)):
    """返回脱敏配置、版本、来源与每个字段的出处（敏感值永不外泄）。"""
    snap = manager.current()
    return {
        "version": snap.version,
        "sources": list(snap.sources),
        "values": redact(snap, manager.spec),
        "field_origins": snap.field_origins,
        "flag_origins": snap.flag_origins,
        "shadows": _shadows_view(snap),
        "created_at": snap.created_at,
        "reason": snap.reason,
        "sensitive_placeholder": SENSITIVE_PLACEHOLDER,
    }


@app.post("/refresh")
def refresh(
    payload: RefreshRequest | None = None,
    manager: ConfigManager = Depends(get_manager),
):
    """运行时刷新：文件+环境+本次推送（API 优先级最高）。

    校验失败时返回 422 且服务仍保留上一份配置；来源故障进入降级态返回 503。
    """
    payload = payload or RefreshRequest()
    result: RefreshResult = manager.refresh(
        make_api_payload(values=payload.values, flags=payload.flags)
    )
    if result.degraded:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "unavailable",
                "detail_error_type": result.error_type,
                "reason": result.reason,
                "fault_source": result.fault_source,
                "fault_error_type": result.error_type,
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
                        "kind": i.kind,
                        "source": i.source,
                        "message": i.message,
                        "sensitive": i.sensitive,
                    }
                    for i in result.issues
                ],
            },
        )
    return {
        "ok": True,
        "version": result.version,
        "sources": list(result.sources),
    }


@app.get("/versions")
def list_versions(manager: ConfigManager = Depends(get_manager)):
    """列出最近若干份可用版本（最新在前），并标记当前正在使用的版本。"""
    versions: list[VersionInfo] = manager.history()
    return {
        "history_limit": manager.history_limit,
        "count": len(versions),
        "versions": [
            {
                "version": v.version,
                "sources": list(v.sources),
                "created_at": v.created_at,
                "reason": v.reason,
                "origin_version": v.origin_version,
                "current": v.current,
            }
            for v in versions
        ],
    }


@app.post("/versions/{version}/rollback")
def rollback_version(version: int, manager: ConfigManager = Depends(get_manager)):
    """回退到历史中的指定版本；按普通刷新规则重新校验，失败则停留当前版本。"""
    result = manager.rollback(version)
    if not result.ok:
        status = 404 if result.error_type == "VersionNotFound" else 422
        return JSONResponse(
            status_code=status,
            content={
                "ok": False,
                "error_type": "rollback_rejected",
                "detail_error_type": result.error_type,
                "reason": result.reason,
                "retained_version": result.version,
                "issues": [
                    {
                        "key": i.key,
                        "kind": i.kind,
                        "source": i.source,
                        "message": i.message,
                        "sensitive": i.sensitive,
                    }
                    for i in result.issues
                ],
            },
        )
    return {
        "ok": True,
        "version": result.version,
        "origin_version": version,
        "sources": list(result.sources),
    }


@app.get("/flags")
def list_flags(manager: ConfigManager = Depends(get_manager)):
    snap = manager.current()
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
    snap = manager.current()
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
