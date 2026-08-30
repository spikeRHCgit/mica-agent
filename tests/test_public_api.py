"""Mica 公共导入面契约测试。

验证跨模块公共对象从稳定入口可导入：Engine、Provider 与 CLI
只通过公开导入面交换对象，不共享供应商私有数据。
"""

from __future__ import annotations

import subprocess
import sys

import mica_agent
from mica_agent import (
    CURRENT_SESSION_FORMAT_VERSION,
    AgentEngine,
    CancellationRequested,
    CancellationToken,
    DuplicateToolError,
    EngineEvent,
    ErrorCategory,
    InteractionBroker,
    InteractionKind,
    InteractionRequest,
    InvalidToolSchemaError,
    MicaApp,
    PermissionAnswer,
    PermissionMode,
    PermissionRequested,
    PermissionResponse,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
    PolicyOperation,
    RegisteredTool,
    RunStatus,
    SessionError,
    SessionFormatError,
    SessionMeta,
    SessionRecord,
    SessionRecordType,
    ToolArgumentsValidationError,
    ToolCall,
    ToolExecutor,
    ToolFinished,
    ToolHandler,
    ToolNotFoundError,
    ToolRegistry,
    ToolRegistryError,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    ToolStarted,
    TurnEntry,
    UserInputRequested,
    UserInputResponse,
    deserialize_session_record,
    serialize_session_record,
    to_mutable_json,
    validate_tool_arguments,
    validate_tool_schema,
)
from mica_agent.events import (
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from mica_agent.events import (
    TextDelta as EngineTextDelta,
)
from mica_agent.models import Message as ModelsMessage
from mica_agent.models import MessageRole as ModelsMessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    Provider,
    ProviderError,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION as ContractsFormatVersion,
)
from mica_agent.session_contracts import (
    SessionError as ContractsSessionError,
)
from mica_agent.session_contracts import (
    SessionFormatError as ContractsSessionFormatError,
)
from mica_agent.session_contracts import (
    SessionMeta as ContractsSessionMeta,
)
from mica_agent.session_contracts import (
    SessionRecord as ContractsSessionRecord,
)
from mica_agent.session_contracts import (
    SessionRecordType as ContractsSessionRecordType,
)
from mica_agent.session_contracts import (
    TurnEntry as ContractsTurnEntry,
)
from mica_agent.session_contracts import (
    deserialize_session_record as contracts_deserialize_session_record,
)
from mica_agent.session_contracts import (
    serialize_session_record as contracts_serialize_session_record,
)


def test_top_level_exports_context_builder() -> None:
    from mica_agent.context_builder import (
        MAX_AGENTS_FILE_BYTES,
        AgentFileSource,
        ContextBuilder,
        ContextBuildResult,
        ContextDiagnostic,
        ContextDiagnosticKind,
    )

    assert mica_agent.MAX_AGENTS_FILE_BYTES == MAX_AGENTS_FILE_BYTES
    assert mica_agent.AgentFileSource is AgentFileSource
    assert mica_agent.ContextBuildResult is ContextBuildResult
    assert mica_agent.ContextBuilder is ContextBuilder
    assert mica_agent.ContextDiagnostic is ContextDiagnostic
    assert mica_agent.ContextDiagnosticKind is ContextDiagnosticKind


def test_top_level_exports_models_and_events() -> None:
    assert mica_agent.Message is ModelsMessage
    assert mica_agent.MessageRole is ModelsMessageRole
    assert mica_agent.EngineEvent is EngineEvent
    assert mica_agent.RunStatus is RunStatus
    assert mica_agent.ErrorCategory is ErrorCategory


def test_top_level_exports_engine_and_app() -> None:
    assert mica_agent.AgentEngine is AgentEngine
    assert mica_agent.MicaApp is MicaApp
    assert mica_agent.RunStarted is RunStarted
    assert mica_agent.RunCompleted is RunCompleted
    assert mica_agent.RunFailed is RunFailed
    assert mica_agent.RunCancelled is RunCancelled


def test_top_level_exports_p5_06_execution_contracts() -> None:
    from mica_agent.interactions import (
        InteractionBroker as InteractionBrokerClass,
    )
    from mica_agent.interactions import (
        InteractionKind as InteractionKindEnum,
    )
    from mica_agent.interactions import (
        InteractionRequest as InteractionRequestClass,
    )
    from mica_agent.tool_executor import ToolExecutor as ToolExecutorClass

    assert InteractionBroker is InteractionBrokerClass
    assert InteractionKind is InteractionKindEnum
    assert InteractionRequest is InteractionRequestClass
    assert ToolExecutor is ToolExecutorClass

    from mica_agent.cancellation import (
        CancellationRequested as CancellationRequestedClass,
    )
    from mica_agent.cancellation import CancellationToken as CancellationTokenClass
    from mica_agent.events import (
        PermissionRequested as PermissionRequestedEvent,
    )
    from mica_agent.events import ToolFinished as ToolFinishedEvent
    from mica_agent.events import ToolStarted as ToolStartedEvent
    from mica_agent.events import UserInputRequested as UserInputRequestedEvent
    from mica_agent.interactions import PermissionAnswer as PermissionAnswerEnum
    from mica_agent.interactions import PermissionResponse as PermissionResponseClass
    from mica_agent.interactions import UserInputResponse as UserInputResponseClass
    from mica_agent.permissions import PermissionMode as PermissionModeEnum
    from mica_agent.permissions import PolicyDecision as PolicyDecisionEnum
    from mica_agent.tool_contracts import ToolCall as ToolCallClass
    from mica_agent.tool_contracts import ToolRequest as ToolRequestClass
    from mica_agent.tool_contracts import ToolResult as ToolResultClass
    from mica_agent.tool_contracts import ToolResultStatus as ToolResultStatusEnum
    from mica_agent.tool_contracts import ToolSpec as ToolSpecClass

    assert CancellationRequested is CancellationRequestedClass
    assert CancellationToken is CancellationTokenClass
    assert PermissionAnswer is PermissionAnswerEnum
    assert PermissionMode is PermissionModeEnum
    assert PermissionRequested is PermissionRequestedEvent
    assert PermissionResponse is PermissionResponseClass
    assert PolicyDecision is PolicyDecisionEnum
    assert ToolCall is ToolCallClass
    assert ToolFinished is ToolFinishedEvent
    assert ToolRequest is ToolRequestClass
    assert ToolResult is ToolResultClass
    assert ToolResultStatus is ToolResultStatusEnum
    assert ToolSpec is ToolSpecClass
    assert ToolStarted is ToolStartedEvent
    assert UserInputRequested is UserInputRequestedEvent
    assert UserInputResponse is UserInputResponseClass


def test_plain_import_isolates_runtime_tools_and_runtime_side_effects() -> None:
    code = """
import asyncio
import pathlib
import shutil
import subprocess
import sys

def blocked(*args, **kwargs):
    raise AssertionError('runtime side effect')

shutil.which = blocked
subprocess.Popen = blocked
pathlib.Path.is_file = blocked
pathlib.Path.home = blocked
import mica_agent
assert 'mica_agent.runtime_tools' not in sys.modules
assert 'mica_agent.app' not in sys.modules
assert 'mica_agent.engine' not in sys.modules
assert 'mica_agent.cli' not in sys.modules
assert not any(name == 'mica_agent.providers' or name.startswith('mica_agent.providers.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_top_level_runtime_exports_are_lazy_in_isolated_process() -> None:
    code = """
import sys
import mica_agent
assert 'mica_agent.runtime_tools' not in sys.modules
assert mica_agent.DEFAULT_COMMAND_TIMEOUT_SECONDS == 30.0
assert 'mica_agent.runtime_tools' in sys.modules
assert mica_agent.MAX_COMMAND_OUTPUT_BYTES == 262144
assert mica_agent.UserInputRequester is not None
assert callable(mica_agent.register_runtime_tools)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_top_level_exports_p5_04_runtime_tools() -> None:
    from mica_agent.runtime_tools import (
        DEFAULT_COMMAND_TIMEOUT_SECONDS,
        MAX_COMMAND_OUTPUT_BYTES,
        UserInputRequester,
        register_runtime_tools,
    )

    assert mica_agent.DEFAULT_COMMAND_TIMEOUT_SECONDS == DEFAULT_COMMAND_TIMEOUT_SECONDS
    assert mica_agent.MAX_COMMAND_OUTPUT_BYTES == MAX_COMMAND_OUTPUT_BYTES
    assert mica_agent.UserInputRequester is UserInputRequester
    assert mica_agent.register_runtime_tools is register_runtime_tools


def test_top_level_exports_p5_02_contracts() -> None:
    from mica_agent.policy import PolicyEngine as PolicyEngineClass
    from mica_agent.policy import PolicyInput as PolicyInputClass
    from mica_agent.policy import PolicyOperation as PolicyOperationEnum
    from mica_agent.schema_validation import (
        InvalidToolSchemaError as InvalidToolSchemaErrorClass,
    )
    from mica_agent.schema_validation import (
        ToolArgumentsValidationError as ToolArgumentsValidationErrorClass,
    )
    from mica_agent.schema_validation import (
        validate_tool_arguments as validate_tool_arguments_function,
    )
    from mica_agent.schema_validation import (
        validate_tool_schema as validate_tool_schema_function,
    )
    from mica_agent.tool_contracts import to_mutable_json as to_mutable_json_function
    from mica_agent.tool_registry import DuplicateToolError as DuplicateToolErrorClass
    from mica_agent.tool_registry import RegisteredTool as RegisteredToolClass
    from mica_agent.tool_registry import ToolHandler as ToolHandlerProtocol
    from mica_agent.tool_registry import ToolNotFoundError as ToolNotFoundErrorClass
    from mica_agent.tool_registry import ToolRegistry as ToolRegistryClass
    from mica_agent.tool_registry import ToolRegistryError as ToolRegistryErrorClass

    assert DuplicateToolError is DuplicateToolErrorClass
    assert InvalidToolSchemaError is InvalidToolSchemaErrorClass
    assert PolicyEngine is PolicyEngineClass
    assert PolicyInput is PolicyInputClass
    assert PolicyOperation is PolicyOperationEnum
    assert RegisteredTool is RegisteredToolClass
    assert ToolArgumentsValidationError is ToolArgumentsValidationErrorClass
    assert ToolHandler is ToolHandlerProtocol
    assert ToolNotFoundError is ToolNotFoundErrorClass
    assert ToolRegistry is ToolRegistryClass
    assert ToolRegistryError is ToolRegistryErrorClass
    assert to_mutable_json is to_mutable_json_function
    assert validate_tool_arguments is validate_tool_arguments_function
    assert validate_tool_schema is validate_tool_schema_function


def test_top_level_exports_p5_03_contracts() -> None:
    from mica_agent.file_tools import MAX_FILE_BYTES
    from mica_agent.file_tools import (
        register_file_tools as register_file_tools_function,
    )
    from mica_agent.workspace_paths import WorkspacePathError as WorkspacePathErrorClass
    from mica_agent.workspace_paths import (
        WorkspacePathResolver as WorkspacePathResolverClass,
    )

    assert mica_agent.MAX_FILE_BYTES == MAX_FILE_BYTES
    assert mica_agent.register_file_tools is register_file_tools_function
    assert not hasattr(mica_agent, "MAX_LIST_ENTRIES")
    assert not hasattr(mica_agent, "MAX_LIST_OUTPUT_BYTES")
    assert "MAX_LIST_ENTRIES" not in mica_agent.__all__
    assert "MAX_LIST_OUTPUT_BYTES" not in mica_agent.__all__
    assert mica_agent.WorkspacePathError is WorkspacePathErrorClass
    assert mica_agent.WorkspacePathResolver is WorkspacePathResolverClass


def test_p5_01_contract_modules_import_without_runtime_cycles() -> None:
    code = (
        "import sys\n"
        "import mica_agent.cancellation\n"
        "import mica_agent.interactions\n"
        "import mica_agent.permissions\n"
        "import mica_agent.tool_contracts\n"
        "import mica_agent.events\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_top_level_exports_session_contracts() -> None:
    assert CURRENT_SESSION_FORMAT_VERSION == ContractsFormatVersion == 2
    assert SessionMeta is ContractsSessionMeta
    assert TurnEntry is ContractsTurnEntry
    assert SessionRecord is ContractsSessionRecord
    assert SessionRecordType is ContractsSessionRecordType
    assert SessionError is ContractsSessionError
    assert SessionFormatError is ContractsSessionFormatError
    assert serialize_session_record is contracts_serialize_session_record
    assert deserialize_session_record is contracts_deserialize_session_record


def test_top_level_exports_session_service_boundaries() -> None:
    from mica_agent.jsonl_store import SessionDeleteStatus
    from mica_agent.session_service import (
        CleanupResult,
        NoHealthySessionError,
        SessionListResult,
        SessionService,
        SessionServiceError,
        TurnNotFoundError,
    )

    assert mica_agent.SessionService is SessionService
    assert mica_agent.SessionListResult is SessionListResult
    assert mica_agent.CleanupResult is CleanupResult
    assert mica_agent.SessionServiceError is SessionServiceError
    assert mica_agent.NoHealthySessionError is NoHealthySessionError
    assert mica_agent.TurnNotFoundError is TurnNotFoundError
    assert mica_agent.SessionDeleteStatus is SessionDeleteStatus


def test_top_level_exports_workspace_and_store_boundaries() -> None:
    from mica_agent.jsonl_store import JsonlSessionStore as StoreClass
    from mica_agent.workspace import WorkspaceIdentity as IdentityClass

    assert mica_agent.JsonlSessionStore is StoreClass
    assert mica_agent.JsonlStore is StoreClass
    assert mica_agent.WorkspaceIdentity is IdentityClass
    assert mica_agent.session_filename is not None


def test_events_are_importable_from_events_module() -> None:
    assert RunStarted is not None
    assert EngineTextDelta is not None
    assert RunCompleted is not None
    assert RunFailed is not None
    assert RunCancelled is not None


def test_provider_contract_is_importable_from_providers_package() -> None:
    assert isinstance(Provider, type)
    for public_name in (
        ProviderRequest,
        ProviderEvent,
        ProviderTextDelta,
        Finish,
        ProviderErrorEvent,
        ProviderError,
        AuthenticationError,
        RateLimitError,
        ProviderTimeoutError,
        TemporaryServerError,
        MalformedResponseError,
    ):
        assert public_name is not None


def test_models_module_is_independent_of_providers_and_events() -> None:
    code = (
        "import sys\n"
        "import mica_agent.models\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_p5_02_modules_import_without_runtime_cycles() -> None:
    code = (
        "import sys\n"
        "import mica_agent.tool_registry\n"
        "import mica_agent.schema_validation\n"
        "import mica_agent.policy\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_p5_03_imports_have_no_filesystem_or_runtime_side_effects() -> None:
    code = (
        "import pathlib, sys\n"
        "def forbidden(*args, **kwargs): raise AssertionError('filesystem access')\n"
        "pathlib.Path.home = forbidden\n"
        "pathlib.Path.cwd = forbidden\n"
        "import mica_agent\n"
        "import mica_agent.file_tools\n"
        "import mica_agent.workspace_paths\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_top_level_import_keeps_runtime_and_provider_modules_lazy() -> None:
    code = (
        "import sys\n"
        "import mica_agent\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_providers_do_not_import_cli() -> None:
    code = (
        "import sys\n"
        "import mica_agent.providers\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
        "assert 'mica_agent.models' in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
