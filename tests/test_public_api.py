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
    EngineEvent,
    ErrorCategory,
    MicaApp,
    RunStatus,
    SessionError,
    SessionFormatError,
    SessionMeta,
    SessionRecord,
    SessionRecordType,
    TurnEntry,
    deserialize_session_record,
    serialize_session_record,
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


def test_top_level_exports_session_contracts() -> None:
    assert CURRENT_SESSION_FORMAT_VERSION == ContractsFormatVersion == 1
    assert SessionMeta is ContractsSessionMeta
    assert TurnEntry is ContractsTurnEntry
    assert SessionRecord is ContractsSessionRecord
    assert SessionRecordType is ContractsSessionRecordType
    assert SessionError is ContractsSessionError
    assert SessionFormatError is ContractsSessionFormatError
    assert serialize_session_record is contracts_serialize_session_record
    assert deserialize_session_record is contracts_deserialize_session_record


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
