"""FastAPI 服务入口：运行时配置与功能开关。"""
from __future__ import annotations

from fastapi import FastAPI

from dynconfig.api import create_router
from dynconfig.config import build_initial_manager

manager = build_initial_manager()

app = FastAPI(title="runtime-config-flags", version="0.1.0")
app.include_router(create_router(manager))


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "runtime-config-flags",
            "docs": "/docs", "config_version": str(manager.version())}
