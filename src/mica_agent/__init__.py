"""Mica 的最小命令行入口包。

顶层只暴露跨模块共享的公共契约对象：标准消息与引擎事件。
"""

from .events import EngineEvent, ErrorCategory, RunStatus
from .models import Message, MessageRole

__all__ = ["EngineEvent", "ErrorCategory", "Message", "MessageRole", "RunStatus"]
