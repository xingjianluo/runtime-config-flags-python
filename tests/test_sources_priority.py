"""多来源合并与稳定优先级。"""
from __future__ import annotations

import json
import os

import pytest

from app.errors import ConfigSourceError
from app.sources import (
    API_SOURCE,
    ENV_SOURCE,
    FILE_SOURCE,
    PRIORITY,
    load_env,
    load_file,
    make_api_payload,
    merge,
)


def test_priority_order_is_stable():
    assert PRIORITY["defaults"] < PRIORITY["file"] < PRIORITY["env"] < PRIORITY["api"]


def test_higher_priority_wins_and_origin_recorded(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "values": {"app_name": "file-app", "max_connections": 10},
                "flags": {"dark_mode": False},
            }
        )
    )
    monkeypatch.setenv("APP_VALUES_MAX_CONNECTIONS", "20")
    monkeypatch.setenv("APP_VALUES_APP_NAME", "env-app")

    values, flags, origins, sources = merge(
        [
            make_api_payload({"app_name": "api-app"}, {}),
            load_env(),
            load_file(str(cfg)),
        ]
    )

    assert values["app_name"] == "api-app"
    assert origins["app_name"] == API_SOURCE
    assert values["max_connections"] == 20
    assert origins["max_connections"] == ENV_SOURCE
    assert flags["dark_mode"] is False
    assert origins["dark_mode"] == FILE_SOURCE
    # 来源按优先级稳定排序
    assert sources == (FILE_SOURCE, ENV_SOURCE, API_SOURCE)


def test_missing_file_means_empty_layer(tmp_path):
    loaded = load_file(str(tmp_path / "absent.json"))
    assert loaded.data == {} and loaded.flags == {}


def test_malformed_file_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    with pytest.raises(ConfigSourceError):
        load_file(str(bad))


def test_env_json_scalars_parsed(monkeypatch):
    monkeypatch.setenv("APP_VALUES_MAX_CONNECTIONS", "42")
    monkeypatch.setenv("APP_VALUES_ENABLE_CACHE", "true")
    monkeypatch.setenv("APP_VALUES_APP_NAME", "plain-string")
    loaded = load_env()
    assert loaded.data["max_connections"] == 42
    assert loaded.data["enable_cache"] is True
    assert loaded.data["app_name"] == "plain-string"
