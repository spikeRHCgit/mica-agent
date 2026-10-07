"""``MicaApp``: minimal application assembly entry driven by the session service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TypeAlias
from uuid import uuid4

from .cancellation import CancellationToken
from .checkpoint_contracts import RunCheckpoint, RunCheckpointKind
from .config_contracts import (
    ConfigError,
    ConnectionBinding,
    EffectiveSettings,
    ModelCapabilities,
)
from .config_service import ConfigService
from .context_builder import ContextBuilder, ContextBuildResult
from .engine import AgentEngine, RunIdFactory, RunProgressSnapshot
from .events import (
    EngineEvent,
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
)
from .interactions import PermissionResponse, UserInputResponse
from .models import Message, MessageRole
from .providers import Provider, ProviderRequest
from .providers.openai_compatible import DEFAULT_MAX_TOKENS, DEFAULT_THINKING_LEVEL_MAP
from .reasoning import ReasoningEffort
from .session_contracts import SessionSelection, TurnEntry
from .session_service import SessionService
from .system_prompt import build_system_prompt
from .tool_executor import ToolExecutor
from .tool_presentation import ToolPresentationSnapshot

TurnIdFactory = Callable[[], str]
CheckpointIdFactory = Callable[[], str]
Clock = Callable[[], datetime]
ProviderFactory: TypeAlias = Callable[[str, str], Provider]
_STORAGE_FAILURE_MESSAGE = "session persistence failed"
_STORAGE_FAILURE_AFTER_TOOL_MESSAGE = (
    "session persistence failed; tool changes may have occurred; check the workspace"
)
_SIDE_EFFECT_TOOL_NAMES = frozenset({"write", "edit", "bash"})
_RECOVERABLE_ERROR_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.TIMEOUT,
        ErrorCategory.TEMPORARY,
        ErrorCategory.MALFORMED,
    }
)


class MicaAppError(Exception):
    """Base class for ``MicaApp`` application-layer errors."""


class AppBusyError(MicaAppError):
    """Starting a new run while this app already has an active run."""


class AppClosedError(MicaAppError):
    """Starting a run after the app has been closed."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    selection: SessionSelection
    model_name: str
    provider_name: str
    context_window: int
    context_window_is_default: bool
    is_current: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class ModelSwitchResult:
    switched: bool
    message: str
    cleanup_warning: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RunBinding:
    """One App-owned reservation; immutable request settings for this Run."""

    owner: object
    engine: AgentEngine
    model: str
    max_tokens: int
    thinking_level_map: Mapping[ReasoningEffort, str] | None
    reasoning_effort: ReasoningEffort
    settings: EffectiveSettings
    tools: tuple
    capabilities: ModelCapabilities | None


class MicaApp:
    """Wire the session service, context builder, engine, and provider together."""

    def __init__(
        self,
        provider: Provider | None,
        model: str | None,
        *,
        session_service: SessionService,
        session_id: str,
        context_builder: ContextBuilder,
        system_prompt: str | None = None,
        context_snapshot: ContextBuildResult | None = None,
        run_id_factory: RunIdFactory | None = None,
        turn_id_factory: TurnIdFactory | None = None,
        checkpoint_id_factory: CheckpointIdFactory | None = None,
        clock: Clock | None = None,
        tool_executor: ToolExecutor | None = None,
        config_service: ConfigService | None = None,
        provider_factory: ProviderFactory | None = None,
        connection: ConnectionBinding | None = None,
        connection_status: str = "ready",
        connection_diagnostic: str | None = None,
        startup_settings: EffectiveSettings | None = None,
        selection_source: str = "direct",
        close_timeout: float = 2.0,
    ) -> None:
        create_provider = (
            provider is None and connection is not None and provider_factory is not None
        )
        if create_provider:
            model = connection.selection.model_id
        if not create_provider and (provider is None) != (model is None):
            raise ValueError("provider and model must be configured together")
        if model is not None and not isinstance(model, str):
            raise TypeError(f"model must be a str, got {type(model).__name__}")
        if model is not None and not model.strip():
            raise ValueError("model must be a non-empty, non-blank string")
        if not isinstance(session_service, SessionService):
            raise TypeError("session_service must be a SessionService")
        if not isinstance(session_id, str):
            raise TypeError(
                f"session_id must be a str, got {type(session_id).__name__}"
            )
        if not session_id.strip():
            raise ValueError("session_id must be a non-empty, non-blank string")
        if not isinstance(context_builder, ContextBuilder):
            raise TypeError("context_builder must be a ContextBuilder")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError(
                f"system_prompt must be a str or None, got {type(system_prompt).__name__}"
            )
        if context_snapshot is not None and not isinstance(
            context_snapshot, ContextBuildResult
        ):
            raise TypeError("context_snapshot must be a ContextBuildResult or None")
        if tool_executor is not None and not isinstance(tool_executor, ToolExecutor):
            raise TypeError("tool_executor must be a ToolExecutor or None")
        resolved_context = (
            context_builder.build() if context_snapshot is None else context_snapshot
        )
        if any(
            message.role is not MessageRole.SYSTEM
            for message in resolved_context.messages
        ):
            raise ValueError("context_snapshot must contain only system messages")
        resolved_system_prompt = (
            build_system_prompt(context_builder.workspace)
            if system_prompt is None
            else system_prompt
        )
        if create_provider:
            try:
                provider = provider_factory(connection.api_url, connection.api_key)
            except Exception:  # noqa: BLE001 - factory details may contain credentials
                raise MicaAppError("unable to initialize provider") from None
        self._model = model
        self._provider = provider
        self._provider_factory = provider_factory
        self._session_service = session_service
        self._session_id = session_id
        self._header_messages = (
            Message(role=MessageRole.SYSTEM, content=resolved_system_prompt),
            *resolved_context.messages,
        )
        self._turn_id_factory = turn_id_factory or (lambda: uuid4().hex)
        self._checkpoint_id_factory = checkpoint_id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tool_executor = tool_executor
        self._run_id_factory = run_id_factory or (lambda: uuid4().hex)
        self._workspace = context_builder.workspace
        self._config_service = config_service
        self._connection = connection
        self._settings = startup_settings or EffectiveSettings()
        self._capabilities = (
            None if connection is None else connection.model.capabilities
        )
        self._selection_source = selection_source
        self._effective_effort = self._resolve_effort()
        self._connection_status = (
            connection_status
            if provider is not None
            else ("unresolved" if connection_status == "unresolved" else "unconfigured")
        )
        self._connection_diagnostic = connection_diagnostic
        self._close_timeout = close_timeout
        self._close_task: asyncio.Task | None = None
        self._reservation: RunBinding | None = None
        self._switching = False
        self._engine = (
            None
            if provider is None
            else AgentEngine(
                provider=provider,
                run_id_factory=self._run_id_factory,
                tool_executor=tool_executor,
                presentation_workspace=context_builder.workspace,
            )
        )
        self._active_run = False
        self._closed = False

    def _resolve_effort(self) -> ReasoningEffort:
        if self._capabilities is not None and self._capabilities.reasoning:
            available = (
                self._capabilities.thinking_level_map or DEFAULT_THINKING_LEVEL_MAP
            )
            if self._settings.reasoning_effort in available:
                return self._settings.reasoning_effort
        return ReasoningEffort.OFF

    @property
    def current_selection(self) -> SessionSelection | None:
        return None if self._connection is None else self._connection.selection

    def model_choices(self) -> tuple[ModelChoice, ...]:
        """Read the current local directory; configuration failures remain safe."""
        if self._config_service is None:
            return ()
        snapshot = self._config_service.load()
        choices = []
        for provider in snapshot.providers:
            if not provider.enabled:
                continue
            for model in provider.models:
                resolved = self._config_service.resolve(
                    snapshot, explicit=model.selection
                )
                if resolved.status != "ready" or resolved.connection is None:
                    continue
                caps = model.capabilities
                choices.append(
                    ModelChoice(
                        model.selection,
                        model.name,
                        provider.name,
                        caps.context_window,
                        caps.sources.get("contextWindow") != "explicit",
                        model.selection == self.current_selection,
                        model.selection == snapshot.default_model,
                    )
                )
        return tuple(
            sorted(choices, key=lambda item: (item.model_name, item.provider_name))
        )

    async def _close_candidate(self, provider: Provider) -> str | None:
        """Release a replaced or rejected connection without exposing provider errors."""
        task = asyncio.create_task(provider.aclose(), name="mica-model-provider-close")
        task.add_done_callback(
            lambda finished: None if finished.cancelled() else finished.exception()
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=self._close_timeout)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=self._close_timeout)
            raise
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=self._close_timeout)
            return "provider release is unconfirmed"
        if task.cancelled() or task.exception() is not None:
            return "provider release failed"
        return None

    async def switch_model(self, selection: SessionSelection) -> ModelSwitchResult:
        """Persist an idle selection before installing it; failed preparation rolls back."""
        if self._closed:
            raise AppClosedError("app is closed; cannot switch models")
        if self.is_busy:
            raise AppBusyError("app is busy; cannot switch models")
        self._switching = True
        try:
            if selection == self.current_selection:
                return ModelSwitchResult(True, "model is already selected")
            if self._config_service is None:
                return ModelSwitchResult(False, "model configuration is unavailable")
            try:
                snapshot = self._config_service.load()
                resolved = self._config_service.resolve(snapshot, explicit=selection)
            except Exception:  # noqa: BLE001 - configuration details stay private
                return ModelSwitchResult(False, "unable to read model configuration")
            connection = resolved.connection
            if resolved.status != "ready" or connection is None:
                return ModelSwitchResult(False, "selected model is unavailable")
            if self._provider_factory is None:
                return ModelSwitchResult(False, "provider factory is unavailable")
            try:
                provider = self._provider_factory(
                    connection.api_url, connection.api_key
                )
            except Exception:  # noqa: BLE001 - factory details stay private
                return ModelSwitchResult(False, "unable to initialize provider")
            try:
                self._session_service.update_selection(self._session_id, selection)
            except asyncio.CancelledError:
                await self._close_candidate(provider)
                raise
            except Exception:  # noqa: BLE001 - rollback must not reveal storage details
                warning = await self._close_candidate(provider)
                return ModelSwitchResult(False, "unable to save session model", warning)
            previous = self._provider
            self._provider = provider
            self._model = connection.selection.model_id
            self._connection = connection
            self._engine = AgentEngine(
                provider,
                run_id_factory=self._run_id_factory,
                tool_executor=self._tool_executor,
                presentation_workspace=self._workspace,
            )
            self._capabilities = connection.model.capabilities
            self._selection_source = "session"
            self._connection_status = "ready"
            self._connection_diagnostic = None
            self._effective_effort = self._resolve_effort()
            warning = (
                None if previous is None else await self._close_candidate(previous)
            )
            return ModelSwitchResult(True, "model switched", warning)
        finally:
            self._switching = False

    def save_default_model(self, selection: SessionSelection) -> None:
        """Save the global default with the configuration service's conflict check."""
        if self._config_service is None:
            raise ConfigError("model configuration is unavailable")
        snapshot = self._config_service.load()
        self._config_service.save_default_model(selection, snapshot.default_model)

    @property
    def session_id(self) -> str:
        """Session id bound to the current app instance."""
        return self._session_id

    @property
    def history(self) -> tuple[Message, ...]:
        """Immutable persisted-history snapshot freshly read from the session service."""
        return self._session_service.context_snapshot(self._session_id)

    def respond_interaction(
        self, response: PermissionResponse | UserInputResponse
    ) -> bool:
        """Submit one frontend-agnostic response to the current tool interaction."""
        if self._tool_executor is None:
            return False
        return self._tool_executor.interaction_broker.respond(response)

    @property
    def connection_status(self) -> str:
        return "closing" if self._closed else self._connection_status

    @property
    def connection_diagnostic(self) -> str | None:
        return self._connection_diagnostic

    @property
    def connection_summary(self) -> str | None:
        if self._capabilities is None:
            return None
        caps = self._capabilities
        budget = caps.max_tokens if caps.max_tokens is not None else DEFAULT_MAX_TOKENS
        source = caps.sources.get("maxTokens", "adapter default")
        context_source = caps.sources.get("contextWindow", "program default; estimated")
        return f"Source: {self._selection_source}. Output budget: {budget} ({source}). Context: {caps.context_window} ({context_source}). Reasoning: {self._effective_effort.value} (preference {self._settings.reasoning_effort.value}; {self._settings.sources.get('reasoningEffort', 'program default')}). Permissions: {self._settings.permission_mode.value} ({self._settings.sources.get('permissionMode', 'program default')})."

    @property
    def reasoning_notice(self) -> str | None:
        if self._settings.reasoning_effort is not self._effective_effort:
            return "Reasoning preference is unsupported by this model; effective reasoning is off."
        return None

    @property
    def is_busy(self) -> bool:
        return self._reservation is not None or self._switching

    def prepare_run(
        self, *, reasoning_effort: ReasoningEffort | None = None
    ) -> RunBinding:
        if self._closed:
            raise AppClosedError("app is closed; cannot start a new run")
        if self.is_busy:
            raise AppBusyError("another run is already active")
        if self._provider is None or self._model is None:
            raise ConfigError(
                self._connection_diagnostic
                or "No model configured. Configure a connection and restart."
            )
        settings = EffectiveSettings()
        capabilities = None
        budget = DEFAULT_MAX_TOKENS
        mapping = None
        effort = ReasoningEffort.OFF if reasoning_effort is None else reasoning_effort
        executor = self._tool_executor
        engine = self._engine
        if self._config_service is not None and self._connection is not None:
            settings, capabilities, budget, mapping, effort = (
                self._config_service.for_run(
                    self._connection.selection, current=self._connection
                )
            )
            if reasoning_effort is not None:
                effort = (
                    reasoning_effort
                    if capabilities.reasoning and reasoning_effort in mapping
                    else ReasoningEffort.OFF
                )
            if executor is not None:
                executor = executor.bind_policy(settings.permission_mode)
            engine = AgentEngine(
                self._provider,
                run_id_factory=self._run_id_factory,
                tool_executor=executor,
                presentation_workspace=self._workspace,
            )
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning_effort must be a ReasoningEffort")
        assert engine is not None
        binding = RunBinding(
            self,
            engine,
            self._model,
            budget,
            None if mapping is None else MappingProxyType(dict(mapping)),
            effort,
            settings,
            () if executor is None else executor.tool_specs,
            capabilities,
        )
        self._reservation = binding
        self._settings = settings
        self._capabilities = capabilities
        self._effective_effort = effort
        return binding

    def release_run(self, binding: RunBinding) -> None:
        """Release an unconsumed reservation; a running generator releases in finally."""
        if not isinstance(binding, RunBinding) or binding.owner is not self:
            raise ValueError("binding belongs to another application")
        if self._reservation is binding and not self._active_run:
            self._reservation = None

    async def aclose(self) -> None:
        """Close the owned Provider once with a bounded, observable result."""
        self._closed = True
        if not self._active_run:
            self._reservation = None
        if self._provider is None:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._provider.aclose(), name="mica-provider-close"
            )
            self._close_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        if self._close_task.done():
            done = {self._close_task}
        else:
            try:
                done, _ = await asyncio.wait(
                    {self._close_task}, timeout=self._close_timeout
                )
            except asyncio.CancelledError:
                self._close_task.cancel()
                await asyncio.wait({self._close_task}, timeout=self._close_timeout)
                raise
        if not done:
            self._close_task.cancel()
            await asyncio.wait({self._close_task}, timeout=self._close_timeout)
            if self._close_task.done() and not self._close_task.cancelled():
                self._close_task.exception()
            raise MicaAppError("provider release is unconfirmed")
        if self._close_task.cancelled():
            raise MicaAppError("provider release is unconfirmed")
        try:
            self._close_task.result()
        except Exception:  # noqa: BLE001 - provider details must stay private
            raise MicaAppError("provider release failed") from None

    async def run(
        self,
        prompt: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        binding: RunBinding | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[EngineEvent]:
        """Start one run; success commits a ``TurnEntry``, cancellation or recoverable
        failure commits a checkpoint."""
        if self._closed:
            raise AppClosedError("app is closed; cannot start a new run")
        if not isinstance(prompt, str):
            raise TypeError(f"prompt must be a str, got {type(prompt).__name__}")
        if not prompt.strip():
            raise ValueError("prompt must be a non-empty, non-blank string")
        if reasoning_effort is not None and not isinstance(
            reasoning_effort, ReasoningEffort
        ):
            raise TypeError("reasoning_effort must be a ReasoningEffort")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken or None")
        if self._active_run:
            raise AppBusyError("another run is already active")
        binding = (
            self.prepare_run(reasoning_effort=reasoning_effort)
            if binding is None
            else binding
        )
        if (
            not isinstance(binding, RunBinding)
            or binding.owner is not self
            or self._reservation is not binding
        ):
            raise ValueError("binding is invalid or already consumed")
        self._active_run = True
        user_message = Message(role=MessageRole.USER, content=prompt)
        completed_messages: list[tuple[Message, ...]] = []
        completed_tool_presentations: list[tuple[ToolPresentationSnapshot, ...]] = []
        progress_snapshots: list[RunProgressSnapshot] = []
        submitted = False
        try:
            session_messages = self._session_service.context_snapshot(self._session_id)
            request = ProviderRequest(
                model=binding.model,
                messages=self._header_messages + session_messages + (user_message,),
                tools=binding.tools,
                reasoning_effort=binding.reasoning_effort,
                max_tokens=binding.max_tokens,
                thinking_level_map=binding.thinking_level_map,
            )
            async with aclosing(
                binding.engine.run(
                    request,
                    cancellation=cancellation,
                    completed_messages_sink=completed_messages.append,
                    completed_tool_presentations_sink=completed_tool_presentations.append,
                    progress_sink=progress_snapshots.append,
                )
            ) as events:
                async for event in events:
                    if isinstance(event, RunCompleted):
                        if not self._commit_completed_turn(
                            event,
                            user_message,
                            completed_messages,
                            completed_tool_presentations,
                        ):
                            yield RunFailed(
                                run_id=event.run_id,
                                seq=event.seq,
                                category=ErrorCategory.STORAGE,
                                message=self._storage_failure_message(
                                    completed_messages, progress_snapshots
                                ),
                            )
                            return
                        submitted = True
                    elif isinstance(event, (RunCancelled, RunFailed)):
                        kind = self._checkpoint_kind(event)
                        if kind is not None:
                            if not self._commit_checkpoint(
                                event, user_message, progress_snapshots, kind
                            ):
                                yield RunFailed(
                                    run_id=event.run_id,
                                    seq=event.seq,
                                    category=ErrorCategory.STORAGE,
                                    message=self._storage_failure_message(
                                        completed_messages, progress_snapshots
                                    ),
                                )
                                return
                            submitted = True
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            if not submitted and progress_snapshots:
                snapshot = progress_snapshots[-1]
                self._commit_checkpoint(
                    RunCancelled(run_id=snapshot.run_id, seq=0),
                    user_message,
                    progress_snapshots,
                    RunCheckpointKind.CANCELLED,
                )
            raise
        finally:
            self._active_run = False
            self._reservation = None

    @staticmethod
    def _checkpoint_kind(event: RunCancelled | RunFailed) -> RunCheckpointKind | None:
        if isinstance(event, RunCancelled):
            return RunCheckpointKind.CANCELLED
        if event.category in _RECOVERABLE_ERROR_CATEGORIES:
            return RunCheckpointKind.RECOVERABLE_FAILURE
        return None

    @staticmethod
    def _storage_failure_message(
        completed_messages: list[tuple[Message, ...]],
        progress_snapshots: list[RunProgressSnapshot] | None = None,
    ) -> str:
        if completed_messages:
            for message in completed_messages[0]:
                if any(
                    call.name in _SIDE_EFFECT_TOOL_NAMES for call in message.tool_calls
                ):
                    return _STORAGE_FAILURE_AFTER_TOOL_MESSAGE
        if progress_snapshots and progress_snapshots[-1].side_effect_tool_started:
            return _STORAGE_FAILURE_AFTER_TOOL_MESSAGE
        return _STORAGE_FAILURE_MESSAGE

    def _commit_completed_turn(
        self,
        event: RunCompleted,
        user_message: Message,
        completed_messages: list[tuple[Message, ...]],
        completed_tool_presentations: list[tuple[ToolPresentationSnapshot, ...]],
    ) -> bool:
        try:
            if (
                len(completed_messages) != 1
                or not completed_messages[0]
                or len(completed_tool_presentations) != 1
            ):
                return False
            transcript = completed_messages[0]
            presentations = completed_tool_presentations[0]
            turn = TurnEntry(
                turn_id=self._turn_id_factory(),
                run_id=event.run_id,
                user_message=user_message,
                assistant_message=transcript[-1],
                completed_at=self._clock(),
                intermediate_messages=transcript[:-1],
                tool_presentations=presentations,
            )
            self._session_service.commit_turn(self._session_id, turn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is intentionally safe
            return False
        return True

    def _commit_checkpoint(
        self,
        event: RunCancelled | RunFailed,
        user_message: Message,
        progress_snapshots: list[RunProgressSnapshot],
        kind: RunCheckpointKind,
    ) -> bool:
        try:
            snapshot = progress_snapshots[-1] if progress_snapshots else None
            if snapshot is None or snapshot.run_id != event.run_id:
                snapshot = RunProgressSnapshot(event.run_id, ())
            checkpoint = RunCheckpoint(
                checkpoint_id=self._checkpoint_id_factory(),
                run_id=event.run_id,
                kind=kind,
                user_message=user_message,
                recorded_at=self._clock(),
                progress=snapshot.progress,
                partial_assistant_message=snapshot.partial_assistant_message,
            )
            self._session_service.commit_checkpoint(self._session_id, checkpoint)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is intentionally safe
            return False
        return True


__all__ = [
    "AppBusyError",
    "AppClosedError",
    "MicaApp",
    "MicaAppError",
    "ModelChoice",
    "ModelSwitchResult",
    "ProviderFactory",
]
