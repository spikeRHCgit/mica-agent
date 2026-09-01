"""Mica 的最小命令行入口包。

顶层只暴露跨模块共享的公共契约对象：标准消息、引擎事件、
AgentEngine 与 MicaApp 装配入口。Engine/App 通过模块级
``__getattr__`` 延迟加载，保持 ``import mica_agent.models``
等轻量子模块导入不触发 Provider 网络栈加载。
"""

from .cancellation import CancellationRequested, CancellationToken
from .context_builder import (
    MAX_AGENTS_FILE_BYTES,
    AgentFileSource,
    ContextBuilder,
    ContextBuildResult,
    ContextDiagnostic,
    ContextDiagnosticKind,
)
from .events import (
    EngineEvent,
    ErrorCategory,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStatus,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from .interactions import (
    AskOption,
    AskPrompt,
    InteractionBroker,
    InteractionKind,
    InteractionRequest,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
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
from .permissions import PermissionMode, PolicyDecision
from .policy import PolicyEngine, PolicyInput, PolicyOperation
from .reasoning import ReasoningEffort
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
from .tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    to_mutable_json,
)
from .tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
    complete_tool_presentation,
    prepare_tool_presentation,
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
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "MAX_AGENTS_FILE_BYTES",
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_FILE_BYTES",
    "AgentEngine",
    "AgentFileSource",
    "AskOption",
    "AskPrompt",
    "CancellationRequested",
    "CancellationToken",
    "CleanupResult",
    "ContextBuildResult",
    "ContextBuilder",
    "ContextDiagnostic",
    "ContextDiagnosticKind",
    "DuplicateToolError",
    "EngineEvent",
    "ErrorCategory",
    "InteractionBroker",
    "InteractionKind",
    "InteractionRequest",
    "InvalidToolSchemaError",
    "JsonlSessionStore",
    "JsonlStore",
    "Message",
    "MessageRole",
    "MicaApp",
    "NoHealthySessionError",
    "PermissionAnswer",
    "PermissionMode",
    "PermissionRequested",
    "PermissionResponse",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyInput",
    "PolicyOperation",
    "ReasoningDelta",
    "ReasoningEffort",
    "RegisteredTool",
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
    "ToolArgumentsValidationError",
    "ToolCall",
    "ToolExecutor",
    "ToolFinished",
    "ToolHandler",
    "ToolNotFoundError",
    "ToolOutputDelta",
    "ToolOutputReporter",
    "ToolPrepared",
    "ToolPresentationKind",
    "ToolPresentationSnapshot",
    "ToolPresentationStatus",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRequest",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "ToolStarted",
    "TurnEntry",
    "TurnNotFoundError",
    "UserInputRequested",
    "UserInputRequester",
    "UserInputResponse",
    "WorkspaceIdentity",
    "WorkspacePathError",
    "WorkspacePathResolver",
    "WorkspaceResolutionError",
    "complete_tool_presentation",
    "default_data_root",
    "deserialize_session_record",
    "prepare_tool_presentation",
    "register_file_tools",
    "register_runtime_tools",
    "resolve_workspace",
    "resolve_workspace_identity",
    "serialize_session_record",
    "session_filename",
    "to_mutable_json",
    "validate_tool_arguments",
    "validate_tool_schema",
    "workspace_slug",
]


def __getattr__(name: str) -> object:
    """延迟加载运行时装配入口和带第三方依赖的 P5-02 边界。"""
    if name == "AgentEngine":
        from .engine import AgentEngine

        return AgentEngine
    if name == "MicaApp":
        from .app import MicaApp

        return MicaApp
    if name == "ToolExecutor":
        from .tool_executor import ToolExecutor

        return ToolExecutor
    schema_exports = {
        "InvalidToolSchemaError",
        "ToolArgumentsValidationError",
        "validate_tool_arguments",
        "validate_tool_schema",
    }
    if name in schema_exports:
        from . import schema_validation

        return getattr(schema_validation, name)
    registry_exports = {
        "DuplicateToolError",
        "RegisteredTool",
        "ToolHandler",
        "ToolNotFoundError",
        "ToolRegistry",
        "ToolRegistryError",
    }
    if name in registry_exports:
        from . import tool_registry

        return getattr(tool_registry, name)
    if name in {"WorkspacePathError", "WorkspacePathResolver"}:
        from . import workspace_paths

        return getattr(workspace_paths, name)
    file_tool_exports = {
        "MAX_FILE_BYTES",
        "register_file_tools",
    }
    if name in file_tool_exports:
        from . import file_tools

        return getattr(file_tools, name)
    runtime_tool_exports = {
        "DEFAULT_COMMAND_TIMEOUT_SECONDS",
        "MAX_COMMAND_OUTPUT_BYTES",
        "ToolOutputReporter",
        "UserInputRequester",
        "register_runtime_tools",
    }
    if name in runtime_tool_exports:
        from . import runtime_tools

        return getattr(runtime_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
