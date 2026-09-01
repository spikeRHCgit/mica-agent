"""P5-01 前端无关的交互响应契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mica_agent.cancellation import CancellationRequested, CancellationToken
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
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


def test_ask_option_expresses_label_description_and_recommendation() -> None:
    minimal = AskOption("Continue")
    detailed = AskOption("Continue", "Use the current configuration")
    recommended = AskOption("Recommended", recommended=True)

    assert minimal.label == "Continue"
    assert minimal.description is None
    assert minimal.recommended is False
    assert detailed.description == "Use the current configuration"
    assert recommended.recommended is True


def test_ask_option_is_frozen_and_has_no_instance_dictionary() -> None:
    option = AskOption("Continue")

    assert not hasattr(option, "__dict__")
    with pytest.raises(FrozenInstanceError):
        option.label = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize("label", [None, 1, "", " \t\n "])
def test_ask_option_rejects_invalid_labels(label: object) -> None:
    with pytest.raises((TypeError, ValueError), match="label"):
        AskOption(label)  # type: ignore[arg-type]


@pytest.mark.parametrize("description", [1, "", " \t\n "])
def test_ask_option_rejects_invalid_descriptions(description: object) -> None:
    with pytest.raises((TypeError, ValueError), match="description"):
        AskOption("Continue", description)  # type: ignore[arg-type]


@pytest.mark.parametrize("recommended", [0, 1, "true", object()])
def test_ask_option_requires_a_strict_boolean_recommendation(
    recommended: object,
) -> None:
    with pytest.raises(TypeError, match="recommended"):
        AskOption("Continue", recommended=recommended)  # type: ignore[arg-type]


def test_ask_option_validation_errors_do_not_echo_untrusted_text() -> None:
    sentinel = "ask-option-secret-sentinel"

    with pytest.raises(ValueError) as error:
        AskOption(sentinel, " \t")

    assert sentinel not in str(error.value)
    assert sentinel not in repr(error.value)
    assert "Traceback" not in str(error.value)


@pytest.mark.parametrize("option_count", [2, 3, 4])
def test_ask_prompt_accepts_structured_options(option_count: int) -> None:
    options = tuple(AskOption(f"Option {index}") for index in range(option_count))
    prompt = AskPrompt("Which option?", "Setup", options, allow_custom=True)

    assert prompt.question == "Which option?"
    assert prompt.title == "Setup"
    assert prompt.options == options
    assert prompt.allow_custom is True


def test_ask_prompt_without_options_is_a_free_text_question() -> None:
    prompt = AskPrompt("What should happen next?", allow_custom=False)

    assert prompt.options == ()
    assert prompt.allow_custom is False
    assert all(option.label != "Type something." for option in prompt.options)


def test_ask_prompt_copies_list_options_to_an_immutable_tuple() -> None:
    options = [AskOption("First"), AskOption("Second", recommended=True)]
    prompt = AskPrompt("Choose", options=options)
    options.append(AskOption("Later"))

    assert prompt.options == (AskOption("First"), AskOption("Second", recommended=True))
    assert isinstance(prompt.options, tuple)


def test_ask_prompt_is_frozen_and_has_no_instance_dictionary() -> None:
    prompt = AskPrompt("Choose", options=(AskOption("First"), AskOption("Second")))

    assert not hasattr(prompt, "__dict__")
    with pytest.raises(FrozenInstanceError):
        prompt.question = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "options",
    [
        (AskOption("Only"),),
        tuple(AskOption(f"Option {index}") for index in range(5)),
    ],
)
def test_ask_prompt_rejects_invalid_structured_option_counts(
    options: tuple[AskOption, ...],
) -> None:
    with pytest.raises(ValueError, match="options"):
        AskPrompt("Choose", options=options)


def test_ask_prompt_rejects_multiple_recommended_options() -> None:
    with pytest.raises(ValueError, match="recommended"):
        AskPrompt(
            "Choose",
            options=(
                AskOption("First", recommended=True),
                AskOption("Second", recommended=True),
            ),
        )


@pytest.mark.parametrize(
    "options",
    [
        ("not an option",),
        [{"label": "First"}, {"label": "Second"}],
        (object(), AskOption("Second")),
    ],
)
def test_ask_prompt_rejects_non_option_members(options: object) -> None:
    with pytest.raises((TypeError, ValueError), match="options"):
        AskPrompt("Choose", options=options)  # type: ignore[arg-type]


@pytest.mark.parametrize("question", [None, 1, "", " \t\n "])
def test_ask_prompt_rejects_invalid_questions(question: object) -> None:
    with pytest.raises((TypeError, ValueError), match="question"):
        AskPrompt(question)  # type: ignore[arg-type]


@pytest.mark.parametrize("title", [1, "", " \t\n "])
def test_ask_prompt_rejects_invalid_titles(title: object) -> None:
    with pytest.raises((TypeError, ValueError), match="title"):
        AskPrompt("Choose", title=title)  # type: ignore[arg-type]


@pytest.mark.parametrize("allow_custom", [0, 1, "true", object()])
def test_ask_prompt_requires_a_strict_boolean_allow_custom(
    allow_custom: object,
) -> None:
    with pytest.raises(TypeError, match="allow_custom"):
        AskPrompt("Choose", allow_custom=allow_custom)  # type: ignore[arg-type]


def test_ask_prompt_validation_errors_do_not_echo_untrusted_text() -> None:
    question_sentinel = "ask-prompt-question-secret-sentinel"
    title_sentinel = "ask-prompt-title-secret-sentinel"
    option_sentinel = "ask-prompt-option-secret-sentinel"

    with pytest.raises(ValueError) as question_error:
        AskPrompt(question_sentinel, options=(AskOption("Only"),))
    with pytest.raises(ValueError) as option_error:
        AskPrompt(
            "Choose",
            title=title_sentinel,
            options=(AskOption(option_sentinel),),
        )

    for error in question_error.value, option_error.value:
        assert question_sentinel not in str(error)
        assert question_sentinel not in repr(error)
        assert title_sentinel not in str(error)
        assert title_sentinel not in repr(error)
        assert option_sentinel not in str(error)
        assert option_sentinel not in repr(error)
        assert "Traceback" not in str(error)


def test_user_input_response_preserves_two_argument_compatibility() -> None:
    positional = UserInputResponse("interaction-1", "answer")
    keyword = UserInputResponse(interaction_id="interaction-1", answer="answer")

    assert positional == keyword
    assert positional.selected_option_index is None
    assert positional.notes is None
    assert UserInputResponse("interaction-2", "").answer == ""


def test_user_input_response_carries_structured_selection_and_notes() -> None:
    response = UserInputResponse("interaction-1", "custom", 0, "")

    assert response.selected_option_index == 0
    assert response.notes == ""


@pytest.mark.parametrize("selected_option_index", [-1, True, 1.0, "1", object()])
def test_user_input_response_rejects_invalid_selected_option_indexes(
    selected_option_index: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="selected_option_index"):
        UserInputResponse(
            "interaction-1",
            "answer",
            selected_option_index=selected_option_index,  # type: ignore[arg-type]
        )


def test_user_input_response_rejects_non_string_notes() -> None:
    with pytest.raises(TypeError, match="notes"):
        UserInputResponse("interaction-1", "answer", notes=1)  # type: ignore[arg-type]


def test_user_input_response_keeps_frozen_behavior_for_new_fields() -> None:
    response = UserInputResponse("interaction-1", "answer", 1, "note")

    with pytest.raises(FrozenInstanceError):
        response.selected_option_index = 0  # type: ignore[misc]
