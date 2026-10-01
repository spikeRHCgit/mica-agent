"""ToolRegistry 公共行为测试。"""

from __future__ import annotations

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.tool_contracts import (
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_registry import (
    DuplicateToolError,
    InvalidToolSchemaError,
    RegisteredTool,
    ToolHandler,
    ToolNotFoundError,
    ToolRegistry,
    ToolRegistryError,
)


async def _successful_handler(
    request: ToolRequest, cancellation: CancellationToken
) -> ToolResult:
    cancellation.raise_if_cancelled()
    return ToolResult(
        request.call_id,
        request.name,
        ToolResultStatus.SUCCESS,
        "completed",
    )


def test_registry_specs_are_sorted_tool_spec_snapshots_without_execution() -> None:
    calls = 0

    async def counted_handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return await _successful_handler(request, cancellation)

    handler: ToolHandler = counted_handler
    registry = ToolRegistry()
    assert registry.specs() == ()
    first_snapshot = registry.specs()

    write_spec = ToolSpec("write", "Write a value", {"type": "object"})
    read_spec = ToolSpec("read", "Read a value", {"type": "object"})
    registry.register(write_spec, handler)
    after_write = registry.specs()
    registry.register(read_spec, handler)

    assert registry.specs() == (read_spec, write_spec)
    assert isinstance(registry.specs(), tuple)
    assert all(isinstance(spec, ToolSpec) for spec in registry.specs())
    assert first_snapshot == ()
    assert after_write == (write_spec,)
    assert calls == 0


def test_registry_resolves_known_tool_without_executing_handler() -> None:
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return await _successful_handler(request, cancellation)

    spec = ToolSpec("read", "Read a value", {"type": "object"})
    registry = ToolRegistry()
    registry.register(spec, handler)

    registered = registry.resolve("read")

    assert isinstance(registered, RegisteredTool)
    assert registered.spec is spec
    assert registered.handler is handler
    assert calls == 0


def test_duplicate_registration_preserves_first_registration() -> None:
    first_spec = ToolSpec("read", "First definition", {"type": "object"})
    second_spec = ToolSpec("read", "Second definition", {"type": "object"})

    async def second_handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return await _successful_handler(request, cancellation)

    registry = ToolRegistry()
    registry.register(first_spec, _successful_handler)

    with pytest.raises(DuplicateToolError, match="already registered") as caught:
        registry.register(second_spec, second_handler)

    assert isinstance(caught.value, ToolRegistryError)
    assert registry.specs() == (first_spec,)
    assert registry.resolve("read").handler is _successful_handler


def test_unknown_tool_error_is_stable_and_does_not_echo_name() -> None:
    unknown_name = "credential-like-unknown-tool"

    with pytest.raises(ToolNotFoundError) as caught:
        ToolRegistry().resolve(unknown_name)

    assert isinstance(caught.value, ToolRegistryError)
    assert str(caught.value) == "tool is not registered"
    assert unknown_name not in str(caught.value)


def test_registry_rejects_invalid_public_inputs() -> None:
    registry = ToolRegistry()

    with pytest.raises(TypeError, match="ToolSpec"):
        registry.register({}, _successful_handler)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable"):
        registry.register(  # type: ignore[arg-type]
            ToolSpec("read", "Read a value", {"type": "object"}),
            object(),
        )
    with pytest.raises(TypeError, match="name"):
        registry.resolve(42)  # type: ignore[arg-type]

    assert registry.specs() == ()


def test_invalid_schema_registration_leaves_registry_empty() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(
        "read",
        "Read a value",
        {"type": "object", "required": "credential-like-invalid-required"},
    )

    with pytest.raises(InvalidToolSchemaError) as caught:
        registry.register(spec, _successful_handler)

    assert str(caught.value) == "tool input schema is invalid"
    assert "credential-like" not in str(caught.value)
    assert registry.specs() == ()
