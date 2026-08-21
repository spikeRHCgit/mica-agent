"""Mica 的最小命令行入口包。

顶层只暴露跨模块共享的公共契约对象：标准消息、引擎事件、
AgentEngine 与 MicaApp 装配入口。Engine/App 通过模块级
``__getattr__`` 延迟加载，保持 ``import mica_agent.models``
等轻量子模块导入不触发 Provider 网络栈加载。
"""

from .events import (
    EngineEvent,
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStatus,
    TextDelta,
)
from .models import Message, MessageRole
from .session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionError,
    SessionFormatError,
    SessionMeta,
    SessionRecord,
    SessionRecordType,
    TurnEntry,
    deserialize_session_record,
    serialize_session_record,
)

__all__ = [
    "CURRENT_SESSION_FORMAT_VERSION",
    "AgentEngine",
    "EngineEvent",
    "ErrorCategory",
    "Message",
    "MessageRole",
    "MicaApp",
    "RunCancelled",
    "RunCompleted",
    "RunFailed",
    "RunStarted",
    "RunStatus",
    "SessionError",
    "SessionFormatError",
    "SessionMeta",
    "SessionRecord",
    "SessionRecordType",
    "TextDelta",
    "TurnEntry",
    "deserialize_session_record",
    "serialize_session_record",
]


def __getattr__(name: str) -> object:
    """延迟加载需要 Provider 栈的装配入口，避免顶层导入副作用。"""
    if name == "AgentEngine":
        from .engine import AgentEngine

        return AgentEngine
    if name == "MicaApp":
        from .app import MicaApp

        return MicaApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
