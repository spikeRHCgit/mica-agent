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
from .jsonl_store import (
    JsonlSessionStore,
    JsonlStore,
    ScanProblem,
    ScanResult,
    SessionAlreadyExistsError,
    SessionDeleteStatus,
    SessionLockTimeoutError,
    SessionNotFoundError,
    SessionStorageError,
    SessionStorageIOError,
    StoredSession,
    session_filename,
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
from .session_service import (
    CleanupResult,
    NoHealthySessionError,
    SessionListResult,
    SessionService,
    SessionServiceError,
    TurnNotFoundError,
)
from .workspace import (
    WorkspaceIdentity,
    WorkspaceResolutionError,
    default_data_root,
    resolve_workspace,
    resolve_workspace_identity,
    workspace_slug,
)

__all__ = [
    "CURRENT_SESSION_FORMAT_VERSION",
    "AgentEngine",
    "CleanupResult",
    "EngineEvent",
    "ErrorCategory",
    "JsonlSessionStore",
    "JsonlStore",
    "Message",
    "MessageRole",
    "MicaApp",
    "NoHealthySessionError",
    "RunCancelled",
    "RunCompleted",
    "RunFailed",
    "RunStarted",
    "RunStatus",
    "ScanProblem",
    "ScanResult",
    "SessionAlreadyExistsError",
    "SessionDeleteStatus",
    "SessionError",
    "SessionFormatError",
    "SessionListResult",
    "SessionLockTimeoutError",
    "SessionMeta",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionRecordType",
    "SessionService",
    "SessionServiceError",
    "SessionStorageError",
    "SessionStorageIOError",
    "StoredSession",
    "TextDelta",
    "TurnEntry",
    "TurnNotFoundError",
    "WorkspaceIdentity",
    "WorkspaceResolutionError",
    "default_data_root",
    "deserialize_session_record",
    "resolve_workspace",
    "resolve_workspace_identity",
    "serialize_session_record",
    "session_filename",
    "workspace_slug",
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
