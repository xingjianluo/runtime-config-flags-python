"""错误分类：所有配置错误携带字段来源、原因码与可读说明。"""
from __future__ import annotations


class ConfigError(Exception):
    """配置错误基类。"""

    code: str = "config_error"

    def __init__(self, message: str, *, field: str | None = None,
                 source: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.source = source

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "field": self.field, "source": self.source}


class MissingFieldError(ConfigError):
    """所有来源均未提供必填项。"""
    code = "missing_field"


class TypeError_(ConfigError):
    """配置值类型与规格不匹配。"""
    code = "type_error"


class OutOfRangeError(ConfigError):
    """数值取值越界。"""
    code = "out_of_range"


class FormatError(ConfigError):
    """字符串格式非法（枚举/正则等）。"""
    code = "format_error"


class SourceError(ConfigError):
    """配置来源本身不可读取/不可解析。"""
    code = "source_error"


class RefreshError(ConfigError):
    """整体刷新失败：聚合各原因，不替换线上配置。"""
    code = "refresh_failed"

    def __init__(self, message: str, reasons: list[dict]) -> None:
        super().__init__(message)
        self.reasons = reasons

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "reasons": self.reasons}


class UndefinedFlagError(ConfigError):
    """访问未定义的功能开关。"""
    code = "undefined_flag"


class DeprecatedFlagError(ConfigError):
    """访问已废弃的功能开关。"""
    code = "deprecated_flag"
