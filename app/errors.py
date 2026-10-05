"""可区分的配置错误分类。"""
from __future__ import annotations

from dataclasses import dataclass


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


class ConfigUnknownKeyError(ConfigFormatError):
    """来源中出现规格里未定义的配置项。"""


@dataclass(frozen=True)
class ValidationIssue:
    """一次提交里单条校验问题的可解释描述。

    - key: 出问题的配置项/开关名
    - kind: 问题类别（missing/type/range/format/unknown）
    - source: 问题值来自哪个来源层
    - message: 人类可读描述；敏感项的真实值已脱敏
    - sensitive: 该项是否为敏感项
    """

    key: str
    kind: str
    source: str
    message: str
    sensitive: bool = False


class ConfigValidationError(ConfigError):
    """聚合错误：一次提交中收集到的全部问题，一次性上报。"""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues: list[ValidationIssue] = list(issues)
        summary = "; ".join(
            f"[{i.kind}] {i.key}@{i.source}: {i.message}" for i in self.issues
        )
        super().__init__(f"本次提交共发现 {len(self.issues)} 处问题: {summary}")


class ConfigUnavailableError(ConfigError):
    """降级态：来源故障导致当前没有任何可用配置。

    与“配置尚未初始化”区分：这是运行期来源损坏导致的明确状态，
    并携带故障来源与故障类别，供健康检查和读取接口如实上报。
    """

    def __init__(self, message: str, *, source: str, error_type: str):
        super().__init__(message, source=source)
        self.fault_source = source
        self.fault_error_type = error_type
