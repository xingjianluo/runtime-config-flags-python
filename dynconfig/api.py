"""HTTP 入口：配置查询/刷新、开关判定。

安全与一致性规则：
- 敏感配置项任何输出都只显示 "***redacted***"，绝不返回真实值；
- 判定端点从请求头解析 user/分组，构造请求级 FlagContext，多次判定版本一致；
- 刷新失败返回 422，body 内含逐条可区分原因与保留的旧版本号，配置不变化。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dynconfig.errors import ConfigError, RefreshError
from dynconfig.flags import FlagContext, evaluate
from dynconfig.manager import ConfigManager
from dynconfig.sources import api_source

REDACTED = "***redacted***"

# 错误码 -> HTTP 状态码
_STATUS = {
    "missing_field": 422,
    "type_error": 422,
    "out_of_range": 422,
    "format_error": 422,
    "source_error": 422,
    "refresh_failed": 422,
    "undefined_flag": 404,
    "deprecated_flag": 410,
}


class CheckItem(BaseModel):
    key: str


class CheckRequest(BaseModel):
    user_id: str | None = None
    groups: list[str] = Field(default_factory=list)
    flags: list[CheckItem]


def _context_from_headers(user_id: str | None,
                          x_groups: str | None) -> FlagContext:
    groups = frozenset(g.strip() for g in (x_groups or "").split(",") if g.strip())
    return FlagContext(user_id=user_id, groups=groups)


def _masked_view(manager: ConfigManager) -> dict[str, Any]:
    snap = manager.snapshot()
    values: dict[str, Any] = {}
    for key, value in snap.values.items():
        if snap.traces[key].sensitive:
            values[key] = REDACTED
        else:
            values[key] = value
    return {
        "version": snap.version,
        "values": values,
        "sources": {k: t.source for k, t in snap.traces.items()},
        "flags": {
            k: {"state": r.state, "rollout": r.rollout,
                "groups": sorted(r.groups), "deprecated": r.deprecated}
            for k, r in snap.flags.items()
        },
    }


def _error_response(exc: ConfigError) -> JSONResponse:
    body = exc.to_dict()
    return JSONResponse(status_code=_STATUS.get(exc.code, 400),
                        content={"error": body})


def create_router(manager: ConfigManager) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": manager.version()}

    @router.get("/config")
    def get_config() -> dict[str, Any]:
        return _masked_view(manager)

    @router.post("/admin/refresh")
    def refresh(payload: dict[str, Any] | None = None) -> JSONResponse:
        old_version = manager.version()
        try:
            # payload 作为最高优先级叠加层；无 payload 则纯重载文件/环境变量
            snap = (manager.refresh(api_source(payload)) if payload
                    else manager.refresh())
        except RefreshError as exc:
            return JSONResponse(status_code=422, content={
                "error": exc.to_dict(),
                "kept_version": old_version,
            })
        return JSONResponse(status_code=200, content={
            "status": "refreshed",
            "old_version": old_version,
            "version": snap.version,
        })

    @router.get("/flags/{key}")
    def check_flag(
        key: str,
        x_user_id: str | None = Header(default=None),
        x_groups: str | None = Header(default=None),
    ) -> Any:
        ctx = _context_from_headers(x_user_id, x_groups)
        try:
            d = evaluate(manager.snapshot(), key, ctx)
        except ConfigError as exc:
            return _error_response(exc)
        return {"key": d.key, "enabled": d.enabled, "reason": d.reason,
                "version": d.version}

    @router.post("/flags/check")
    def check_flags(req: CheckRequest) -> JSONResponse:
        ctx = FlagContext(user_id=req.user_id, groups=frozenset(req.groups))
        snap = manager.snapshot()
        results = []
        errors = []
        for item in req.flags:
            try:
                d = evaluate(snap, item.key, ctx)
                results.append({"key": d.key, "enabled": d.enabled,
                                "reason": d.reason, "version": d.version})
            except ConfigError as exc:
                errors.append({"key": item.key, **exc.to_dict()})
        status = 207 if errors else 200
        return JSONResponse(status_code=status, content={
            "version": snap.version,
            "results": results,
            "errors": errors,
        })

    return router
