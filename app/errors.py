"""可区分的配置错误分类。"""
from __future__ import annotations


class ConfigError(Exception):
    """所有配置错误的基类。"""

    def __init__(self, message: str, *, source: str | None = None, key: str | None = None):
        super().__init__(message)
        self.message = message
        self.source = source
        self.key = key


class ConfigMissingError(ConfigError):
    """必填配置项缺项。"""


class ConfigTypeError(ConfigError):
    """配置值类型不匹配。"""


class ConfigRangeError(ConfigError):
    """配置值越界。"""


class ConfigFormatError(ConfigError):
    """配置值格式非法（枚举/正则/结构）。"""


class ConfigSourceError(ConfigError):
    """配置来源不可读或解析失败。"""
