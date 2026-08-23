"""P5-01 前端无关的交互响应契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mica_agent.interactions import (
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


def test_responses_are_immutable() -> None:
    permission = PermissionResponse("interaction-1", PermissionAnswer.DENY)
    user_input = UserInputResponse("interaction-2", "answer")

    with pytest.raises(FrozenInstanceError):
        permission.answer = PermissionAnswer.ALLOW_ONCE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        user_input.answer = "changed"  # type: ignore[misc]
