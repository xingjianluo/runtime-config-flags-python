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
    try:
        return manager.current()
    except ConfigError:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": "uninitialized",
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
    try:
        snap = manager.current()
        return {"ok": True, "version": snap.version}
    except ConfigError:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error_type": "uninitialized"},
        )


@app.get("/config")
def get_config(manager: ConfigManager = Depends(get_manager)):
    """返回脱敏配置、版本、来源与每个字段的出处（敏感值永不外泄）。"""
    snap = manager.current()
    return {
        "version": snap.version,
        "sources": list(snap.sources),
        "values": redact(snap, manager.spec),
        "field_origins": snap.field_origins,
        "sensitive_placeholder": SENSITIVE_PLACEHOLDER,
    }


@app.post("/refresh")
def refresh(
    payload: RefreshRequest | None = None,
    manager: ConfigManager = Depends(get_manager),
):
    """运行时刷新：文件+环境+本次推送（API 优先级最高）。

    校验失败时返回 4xx 且服务仍保留上一份配置。
    """
    payload = payload or RefreshRequest()
    result = manager.refresh(
        make_api_payload(values=payload.values, flags=payload.flags)
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
            },
        )
    return {
        "ok": True,
        "version": result.version,
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
