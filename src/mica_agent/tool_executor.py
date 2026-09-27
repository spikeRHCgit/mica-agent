"""Unified, provider-agnostic tool execution entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeAlias

from .cancellation import CancellationRequested, CancellationToken
from .command_risk import analyze_command
from .interactions import InteractionBroker, PermissionAnswer
from .permissions import PermissionMode, PolicyDecision
from .policy import PolicyEngine, PolicyInput, PolicyOperation
from .runtime_tools import ToolOutputReporter
from .schema_validation import ToolArgumentsValidationError, validate_tool_arguments
from .tool_contracts import ToolRequest, ToolResult, ToolResultStatus, ToolSpec
from .tool_registry import ToolNotFoundError, ToolRegistry
from .workspace import WorkspaceIdentity
from .workspace_paths import (
    WorkspacePathError,
    WorkspacePathResolver,
    is_sensitive_path_text,
)

PolicyFactFactory: TypeAlias = Callable[[ToolRequest, ToolSpec], PolicyInput]
LifecycleCallback: TypeAlias = Callable[[ToolRequest | ToolResult], None]

_OPERATION_BY_NAME = {
    "read": PolicyOperation.READ,
    "write": PolicyOperation.WRITE,
    "edit": PolicyOperation.EDIT,
    "bash": PolicyOperation.COMMAND,
    "ask": PolicyOperation.ASK,
}


def _default_policy_input(
    request: ToolRequest,
    spec: ToolSpec,
    workspace: WorkspaceIdentity | None = None,
) -> PolicyInput:
    operation = _OPERATION_BY_NAME.get(request.name, PolicyOperation.OTHER)
    protected_resource = False
    command_risk = False
    if request.name in {"read", "write", "edit"}:
        path = request.arguments.get("path")
        if isinstance(path, str):
            protected_resource = is_sensitive_path_text(path)
            if workspace is not None:
                resolver = WorkspacePathResolver(workspace)
                try:
                    protected_resource = resolver.is_sensitive(resolver.resolve(path))
                except WorkspacePathError:
                    pass  # The handler retains all capability validation.
    if request.name == "bash":
        command = request.arguments.get("command")
        if isinstance(command, str):
            risk = analyze_command(command)
            command_risk = risk.requires_confirmation

    return PolicyInput(
        tool_name=request.name,
        operation=operation,
        hard_denied=False,
        authorized_once=False,
        protected_resource=protected_resource,
        command_risk=command_risk,
    )


def _result(
    request: ToolRequest,
    status: ToolResultStatus,
    message: str,
    data: object = None,
) -> ToolResult:
    return ToolResult(request.call_id, request.name, status, message, data)


class ToolExecutor:
    """Single public entry point for tool lookup, validation, authorization, and handler
    invocation."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        *,
        policy_input_factory: PolicyFactFactory | None = None,
        interaction_broker: InteractionBroker | None = None,
        workspace: WorkspaceIdentity | None = None,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        if not isinstance(policy, PolicyEngine):
            raise TypeError("policy must be a PolicyEngine")
        if policy_input_factory is not None and not callable(policy_input_factory):
            raise TypeError("policy_input_factory must be callable")
        if interaction_broker is not None and not isinstance(
            interaction_broker, InteractionBroker
        ):
            raise TypeError("interaction_broker must be an InteractionBroker")
        if workspace is not None and not isinstance(workspace, WorkspaceIdentity):
            raise TypeError("workspace must be a WorkspaceIdentity or None")
        self._registry = registry
        self._policy = policy
        self._policy_input_factory = policy_input_factory or (
            lambda request, spec: _default_policy_input(request, spec, workspace)
        )
        self._interaction_broker = interaction_broker or InteractionBroker()
        self._output_routes: dict[tuple[str, str], ToolOutputReporter] = {}

    @property
    def output_reporter(self) -> ToolOutputReporter:
        """Return the synchronous safe-output route for the current tool call."""
        return self._report_output

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Return a deterministic immutable snapshot of public tool specs only."""
        return tuple(self._registry.specs())

    @property
    def interaction_broker(self) -> InteractionBroker:
        """Broker the engine or frontend uses to observe requests and submit responses."""
        return self._interaction_broker

    def _report_output(self, call_id: str, tool_name: str, safe_text: str) -> None:
        if not isinstance(call_id, str) or not isinstance(tool_name, str):
            return
        callback = self._output_routes.get((call_id, tool_name))
        if callback is None:
            return
        if not isinstance(safe_text, str) or not safe_text:
            raise ValueError("tool output must be a non-empty string")
        callback(call_id, tool_name, safe_text)

    async def execute(
        self,
        request: ToolRequest,
        cancellation: CancellationToken,
        *,
        on_started: LifecycleCallback | None = None,
        on_finished: LifecycleCallback | None = None,
        on_output: ToolOutputReporter | None = None,
    ) -> ToolResult:
        """Execute one ``ToolRequest``; every failure becomes a safe ``ToolResult``."""

        if not isinstance(request, ToolRequest):
            raise TypeError("request must be a ToolRequest")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if on_output is not None and not callable(on_output):
            raise TypeError("on_output must be callable or None")

        def finish(result: ToolResult) -> ToolResult:
            if on_finished is not None:
                on_finished(result)
            return result

        try:
            cancellation.raise_if_cancelled()
            registered = self._registry.resolve(request.name)
        except CancellationRequested:
            return finish(
                _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
            )
        except ToolNotFoundError:
            return finish(
                _result(request, ToolResultStatus.UNKNOWN_TOOL, "tool is not available")
            )
        except Exception:  # noqa: BLE001 - execution boundary is intentionally safe
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )

        try:
            validate_tool_arguments(registered.spec, request.arguments)
        except ToolArgumentsValidationError:
            return finish(
                _result(
                    request,
                    ToolResultStatus.INVALID_ARGUMENTS,
                    "tool arguments are invalid",
                )
            )
        except Exception:  # noqa: BLE001 - validation backend stays private
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )

        try:
            cancellation.raise_if_cancelled()
            if self._policy.mode is PermissionMode.SOLO:
                decision = PolicyDecision.ALLOW
            else:
                policy_input = self._policy_input_factory(request, registered.spec)
                decision = self._policy.decide(policy_input)
        except CancellationRequested:
            return finish(
                _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
            )
        except Exception:  # noqa: BLE001 - policy boundary fails closed
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )

        if decision is PolicyDecision.DENY:
            return finish(
                _result(
                    request, ToolResultStatus.PERMISSION_DENIED, "permission denied"
                )
            )
        if decision is PolicyDecision.ASK:
            try:
                response = await self._interaction_broker.request_permission(
                    request.call_id,
                    request.name,
                    f"Permission required for tool {request.name}",
                    cancellation,
                )
            except CancellationRequested:
                return finish(
                    _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - interaction boundary is safe
                return finish(
                    _result(
                        request,
                        ToolResultStatus.EXECUTION_FAILED,
                        "permission request failed",
                    )
                )
            if response.answer is not PermissionAnswer.ALLOW_ONCE:
                return finish(
                    _result(
                        request, ToolResultStatus.PERMISSION_DENIED, "permission denied"
                    )
                )

        try:
            cancellation.raise_if_cancelled()
        except CancellationRequested:
            return finish(
                _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
            )

        if on_started is not None:
            on_started(request)
        output_key = (request.call_id, request.name)
        if on_output is not None:
            self._output_routes[output_key] = on_output
        try:
            try:
                result = await registered.handler(request, cancellation)
                cancellation.raise_if_cancelled()
            except CancellationRequested:
                result = _result(
                    request, ToolResultStatus.CANCELLED, "operation cancelled"
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - handler boundary is intentionally safe
                result = _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
        finally:
            if on_output is not None:
                self._output_routes.pop(output_key, None)

        if not isinstance(result, ToolResult):
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )
        if result.call_id != request.call_id or result.tool_name != request.name:
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )
        return finish(result)


__all__ = ["LifecycleCallback", "PolicyFactFactory", "ToolExecutor"]
