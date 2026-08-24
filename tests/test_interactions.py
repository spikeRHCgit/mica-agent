"""P5-01 前端无关的交互响应契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mica_agent.cancellation import CancellationRequested, CancellationToken
from mica_agent.interactions import (
    InteractionBroker,
    InteractionKind,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
)


@pytest.mark.parametrize(
    ("answer", "wire_value"),
    [
        (PermissionAnswer.ALLOW_ONCE, "allow_once"),
        (PermissionAnswer.DENY, "deny"),
    ],
)
def test_permission_answer_wire_values(
    answer: PermissionAnswer, wire_value: str
) -> None:
    assert answer.value == wire_value


def test_permission_response_expresses_once_or_deny_only() -> None:
    allowed = PermissionResponse("interaction-1", PermissionAnswer.ALLOW_ONCE)
    denied = PermissionResponse("interaction-2", PermissionAnswer.DENY)

    assert allowed.interaction_id == "interaction-1"
    assert allowed.answer is PermissionAnswer.ALLOW_ONCE
    assert denied.answer is PermissionAnswer.DENY


def test_user_input_response_carries_string_answer() -> None:
    response = UserInputResponse("interaction-1", "Use UTF-8")

    assert response.interaction_id == "interaction-1"
    assert response.answer == "Use UTF-8"
    assert UserInputResponse("interaction-2", "").answer == ""


@pytest.mark.parametrize("interaction_id", ["", "   ", "\t\n"])
def test_responses_reject_blank_interaction_ids(interaction_id: str) -> None:
    with pytest.raises(ValueError, match="interaction_id"):
        PermissionResponse(interaction_id, PermissionAnswer.DENY)
    with pytest.raises(ValueError, match="interaction_id"):
        UserInputResponse(interaction_id, "answer")


@pytest.mark.parametrize("interaction_id", [None, 1, b"id"])
def test_responses_reject_non_string_interaction_ids(
    interaction_id: object,
) -> None:
    with pytest.raises(TypeError, match="interaction_id"):
        PermissionResponse(  # type: ignore[arg-type]
            interaction_id, PermissionAnswer.DENY
        )
    with pytest.raises(TypeError, match="interaction_id"):
        UserInputResponse(interaction_id, "answer")  # type: ignore[arg-type]


def test_responses_validate_answer_types() -> None:
    with pytest.raises(TypeError, match="answer"):
        PermissionResponse("interaction-1", "deny")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="answer"):
        UserInputResponse("interaction-1", b"answer")  # type: ignore[arg-type]


def test_interaction_broker_matches_permission_response_once() -> None:
    async def scenario() -> None:
        broker = InteractionBroker(
            interaction_id_factory=iter(["permission-1"]).__next__
        )
        cancellation = CancellationToken()
        waiting = asyncio.create_task(
            broker.request_permission(
                call_id="call-1",
                tool_name="write",
                message="Permission required",
                cancellation=cancellation,
            )
        )
        request = await asyncio.wait_for(broker.next_request(), timeout=0.1)
        assert request.kind is InteractionKind.PERMISSION
        assert request.interaction_id == "permission-1"
        assert request.call_id == "call-1"
        assert broker.respond(UserInputResponse("permission-1", "wrong")) is False
        assert (
            broker.respond(PermissionResponse("wrong-id", PermissionAnswer.ALLOW_ONCE))
            is False
        )
        assert (
            broker.respond(
                PermissionResponse("permission-1", PermissionAnswer.ALLOW_ONCE)
            )
            is True
        )
        assert await asyncio.wait_for(waiting, timeout=0.1) == PermissionResponse(
            "permission-1", PermissionAnswer.ALLOW_ONCE
        )
        assert (
            broker.respond(PermissionResponse("permission-1", PermissionAnswer.DENY))
            is False
        )

    asyncio.run(scenario())


def test_interaction_broker_user_input_cancellation_cleans_waiter() -> None:
    async def scenario() -> None:
        broker = InteractionBroker()
        cancellation = CancellationToken()
        waiting = asyncio.create_task(
            broker.request_user_input("call-ask", "What?", cancellation)
        )
        request = await asyncio.wait_for(broker.next_request(), timeout=0.1)
        assert request.kind is InteractionKind.USER_INPUT
        assert request.message == "What?"
        cancellation.cancel()
        with pytest.raises(CancellationRequested):
            await asyncio.wait_for(waiting, timeout=0.1)
        assert (
            broker.respond(UserInputResponse(request.interaction_id, "late")) is False
        )

    asyncio.run(scenario())


def test_interaction_broker_user_input_ids_are_unique_and_reassociated() -> None:
    async def scenario() -> None:
        broker = InteractionBroker(
            interaction_id_factory=iter(["first-ui", "second-ui"]).__next__
        )
        cancellation = CancellationToken()
        first_waiting = asyncio.create_task(
            broker.request_user_input("reused-call", "First?", cancellation)
        )
        first_request = await asyncio.wait_for(broker.next_request(), timeout=0.1)
        broker.clear()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_waiting, timeout=0.1)

        second_waiting = asyncio.create_task(
            broker.request_user_input("reused-call", "Second?", cancellation)
        )
        second_request = await asyncio.wait_for(broker.next_request(), timeout=0.1)
        assert first_request.interaction_id != second_request.interaction_id
        assert (
            broker.respond(UserInputResponse(first_request.interaction_id, "late-old"))
            is False
        )
        assert broker.respond(UserInputResponse(second_request.interaction_id, "new"))
        assert await asyncio.wait_for(second_waiting, timeout=0.1) == UserInputResponse(
            "reused-call", "new"
        )

    asyncio.run(scenario())


def test_interaction_broker_never_reuses_completed_factory_id() -> None:
    async def scenario() -> None:
        broker = InteractionBroker(interaction_id_factory=lambda: "same-id")
        cancellation = CancellationToken()
        waiting = asyncio.create_task(
            broker.request_permission("call-1", "write", "Allow?", cancellation)
        )
        request = await asyncio.wait_for(broker.next_request(), timeout=0.1)
        assert broker.respond(
            PermissionResponse(request.interaction_id, PermissionAnswer.DENY)
        )
        assert await asyncio.wait_for(waiting, timeout=0.1) == PermissionResponse(
            "same-id", PermissionAnswer.DENY
        )
        with pytest.raises(RuntimeError, match="allocate interaction id"):
            await broker.request_permission("call-2", "write", "Allow?", cancellation)
        assert broker.pending_count == 0

    asyncio.run(scenario())

    async def scenario() -> None:
        broker = InteractionBroker()
        cancellation = CancellationToken()
        waiting = asyncio.create_task(
            broker.request_user_input("call-ask", "What?", cancellation)
        )
        await asyncio.wait_for(broker.next_request(), timeout=0.1)
        broker.clear()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=0.1)

    asyncio.run(scenario())


def test_responses_are_immutable() -> None:
    permission = PermissionResponse("interaction-1", PermissionAnswer.DENY)
    user_input = UserInputResponse("interaction-2", "answer")

    with pytest.raises(FrozenInstanceError):
        permission.answer = PermissionAnswer.ALLOW_ONCE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        user_input.answer = "changed"  # type: ignore[misc]
