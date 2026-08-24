"""统一的、供应商无关的工具执行入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeAlias

from .cancellation import CancellationRequested, CancellationToken
from .interactions import InteractionBroker, PermissionAnswer
from .permissions import PolicyDecision
from .policy import PolicyEngine, PolicyInput, PolicyOperation
from .schema_validation import ToolArgumentsValidationError, validate_tool_arguments
from .tool_contracts import ToolRequest, ToolResult, ToolResultStatus, ToolSpec
from .tool_registry import ToolNotFoundError, ToolRegistry

PolicyFactFactory: TypeAlias = Callable[[ToolRequest, ToolSpec], PolicyInput]
LifecycleCallback: TypeAlias = Callable[[ToolRequest | ToolResult], None]

_OPERATION_BY_NAME = {
    "read": PolicyOperation.READ,
    "ls": PolicyOperation.LIST,
    "write": PolicyOperation.WRITE,
    "edit": PolicyOperation.EDIT,
    "bash": PolicyOperation.COMMAND,
    "ask": PolicyOperation.ASK,
}


def _default_policy_input(request: ToolRequest, spec: ToolSpec) -> PolicyInput:
    return PolicyInput(
        tool_name=request.name,
        operation=_OPERATION_BY_NAME.get(request.name, PolicyOperation.OTHER),
        hard_denied=False,
        authorized_once=False,
    )


def _result(
    request: ToolRequest,
    status: ToolResultStatus,
    message: str,
    data: object = None,
) -> ToolResult:
    return ToolResult(request.call_id, request.name, status, message, data)


class ToolExecutor:
    """工具查找、校验、授权和 handler 调用的唯一公共入口。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        *,
        policy_input_factory: PolicyFactFactory | None = None,
        interaction_broker: InteractionBroker | None = None,
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
        self._registry = registry
        self._policy = policy
        self._policy_input_factory = policy_input_factory or _default_policy_input
        self._interaction_broker = interaction_broker or InteractionBroker()

    @property
    def interaction_broker(self) -> InteractionBroker:
        """Engine 或前端用来观察请求和提交响应的 broker。"""
        return self._interaction_broker

    async def execute(
        self,
        request: ToolRequest,
        cancellation: CancellationToken,
        *,
        on_started: LifecycleCallback | None = None,
        on_finished: LifecycleCallback | None = None,
    ) -> ToolResult:
        """执行一次 ToolRequest；所有失败均转换为安全 ToolResult。"""

        if not isinstance(request, ToolRequest):
            raise TypeError("request must be a ToolRequest")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")

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
        try:
            result = await registered.handler(request, cancellation)
            cancellation.raise_if_cancelled()
        except CancellationRequested:
            return finish(
                _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - handler boundary is intentionally safe
            return finish(
                _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "tool execution failed"
                )
            )

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
