"""Session persistence integration: committed turns, recovery, failure and cancellation paths.

These tests protect the App -> Engine -> SessionService boundary: successful
rounds are persisted and reloaded with their context, non-successful runs never
append a turn, and a storage failure leaves no half turn behind.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mica_agent.app import MicaApp
from mica_agent.checkpoint_contracts import RunCheckpointKind
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import ErrorCategory, RunCompleted, RunFailed, TextDelta
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
)
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_contracts import TurnEntry
from mica_agent.session_service import SessionService
from mica_agent.workspace import resolve_workspace

TEST_TIME = datetime(2026, 8, 23, tzinfo=UTC)


class ScriptedProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.scripts) - 1)
        for event in self.scripts[index]:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1


def setup_session(
    tmp_path: Path,
) -> tuple[SessionService, ContextBuilder, str, Path, Path]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    identity = resolve_workspace(workspace_path)
    service = SessionService(
        JsonlSessionStore(identity, data_root),
        now=lambda: TEST_TIME,
    )
    session = service.create_session()
    builder = ContextBuilder(data_root=data_root, workspace=identity)
    return service, builder, session.meta.session_id, data_root, workspace_path


def make_app(
    provider: ScriptedProvider,
    service: SessionService,
    builder: ContextBuilder,
    session_id: str,
    *,
    run_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MicaApp:
    run_values = iter(run_ids or ["run-1", "run-2", "run-3"])
    turn_values = iter(turn_ids or ["turn-1", "turn-2", "turn-3"])
    return MicaApp(
        provider=provider,
        model="test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        run_id_factory=lambda: next(run_values),
        turn_id_factory=lambda: next(turn_values),
        clock=clock or (lambda: TEST_TIME),
    )


async def collect(app: MicaApp, prompt: str) -> list[object]:
    events: list[object] = []
    async for event in app.run(prompt):
        events.append(event)
    return events


def run_collect(app: MicaApp, prompt: str) -> list[object]:
    return asyncio.run(collect(app, prompt))


def test_successful_app_round_is_persisted_and_recovered_with_agents(
    tmp_path: Path,
) -> None:
    service, builder, session_id, data_root, workspace_path = setup_session(tmp_path)
    data_root.joinpath("AGENTS.md").write_text("global rules", encoding="utf-8")
    workspace_path.joinpath("AGENTS.md").write_text("workspace rules", encoding="utf-8")
    first_provider = ScriptedProvider(
        [[ProviderTextDelta(text="first answer"), Finish()]]
    )
    first_app = make_app(first_provider, service, builder, session_id)

    first_events = run_collect(first_app, "first question")
    stored_after_first = service.load_session(session_id)
    raw_after_first = service.store.path_for_session(session_id).read_bytes()

    assert isinstance(first_events[-1], RunCompleted)
    assert len(stored_after_first.turns) == 1
    assert stored_after_first.turns[0] == TurnEntry(
        turn_id="turn-1",
        run_id="run-1",
        user_message=Message(MessageRole.USER, "first question"),
        assistant_message=Message(MessageRole.ASSISTANT, "first answer"),
        completed_at=TEST_TIME,
    )
    assert b"global rules" not in raw_after_first
    assert b"workspace rules" not in raw_after_first
    assert b"<environment_context>" not in raw_after_first
    assert b"<INSTRUCTIONS" not in raw_after_first

    second_provider = ScriptedProvider(
        [[ProviderTextDelta(text="second answer"), Finish()]]
    )
    second_app = make_app(
        second_provider,
        service,
        builder,
        session_id,
        run_ids=["run-2"],
        turn_ids=["turn-2"],
    )

    second_events = run_collect(second_app, "second question")

    assert isinstance(second_events[-1], RunCompleted)
    assert second_provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert second_provider.requests[0].messages[0].content.startswith("# Mica\n")
    assert second_provider.requests[0].messages[1:] == (
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="global">\nglobal rules\n</INSTRUCTIONS>',
        ),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="workspace">\nworkspace rules\n</INSTRUCTIONS>',
        ),
        Message(MessageRole.USER, "first question"),
        Message(MessageRole.ASSISTANT, "first answer"),
        Message(MessageRole.USER, "second question"),
    )
    assert len(service.load_session(session_id).turns) == 2


def test_two_successful_rounds_have_exactly_two_turns_and_snapshot_history(
    tmp_path: Path,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    provider = ScriptedProvider(
        [
            [ProviderTextDelta(text="answer one"), Finish()],
            [ProviderTextDelta(text="answer two"), Finish()],
        ]
    )
    app = make_app(provider, service, builder, session_id)

    assert isinstance(run_collect(app, "question one")[-1], RunCompleted)
    first_snapshot = app.history
    assert isinstance(run_collect(app, "question two")[-1], RunCompleted)

    stored = service.load_session(session_id)
    assert len(stored.turns) == 2
    assert first_snapshot == (
        Message(MessageRole.USER, "question one"),
        Message(MessageRole.ASSISTANT, "answer one"),
    )
    assert isinstance(first_snapshot, tuple)
    assert app.history == (
        Message(MessageRole.USER, "question one"),
        Message(MessageRole.ASSISTANT, "answer one"),
        Message(MessageRole.USER, "question two"),
        Message(MessageRole.ASSISTANT, "answer two"),
    )
    assert provider.requests[1].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[1].messages[1:] == (
        Message(MessageRole.USER, "question one"),
        Message(MessageRole.ASSISTANT, "answer one"),
        Message(MessageRole.USER, "question two"),
    )


def test_failed_run_does_not_change_jsonl_and_following_run_can_commit(
    tmp_path: Path,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderErrorEvent(error=AuthenticationError("failed")),
            ],
            [ProviderTextDelta(text="success"), Finish()],
        ]
    )
    app = make_app(provider, service, builder, session_id)

    first_events = run_collect(app, "failed question")
    after_failure = service.load_session(session_id)
    second_events = run_collect(app, "successful question")

    assert isinstance(first_events[-1], RunFailed)
    assert after_failure.turns == ()
    assert after_failure.checkpoints[0].kind is RunCheckpointKind.RECOVERABLE_FAILURE
    assert isinstance(second_events[-1], RunCompleted)
    stored = service.load_session(session_id)
    assert len(stored.turns) == 1
    assert stored.turns[0].user_message.content == "successful question"
    assert stored.checkpoints[0].kind is RunCheckpointKind.RECOVERABLE_FAILURE


@pytest.mark.parametrize("mode", ["failed", "cancelled", "closed"])
def test_non_successful_run_modes_do_not_append_turn(
    tmp_path: Path,
    mode: str,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)

    if mode == "failed":
        provider = ScriptedProvider(
            [
                [
                    ProviderTextDelta(text="partial"),
                    ProviderErrorEvent(error=AuthenticationError("failed")),
                ]
            ]
        )
        app = make_app(provider, service, builder, session_id)
        assert isinstance(run_collect(app, "failed question")[-1], RunFailed)
    elif mode == "closed":
        provider = ScriptedProvider([[ProviderTextDelta(text="partial"), Finish()]])
        app = make_app(provider, service, builder, session_id)
        stream = app.run("closed before completion")

        async def consume_part() -> None:
            async with aclosing(stream) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        break

        asyncio.run(consume_part())
    else:
        provider = _HangingProvider()
        app = make_app(provider, service, builder, session_id)

        async def cancel_run() -> None:
            task = asyncio.create_task(collect(app, "cancelled question"))
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_run())

    stored = service.load_session(session_id)
    assert stored.turns == ()
    assert len(stored.checkpoints) == 1
    expected = (
        RunCheckpointKind.RECOVERABLE_FAILURE
        if mode == "failed"
        else RunCheckpointKind.CANCELLED
    )
    assert stored.checkpoints[0].kind is expected


def test_persistence_failure_becomes_safe_storage_failure_without_half_turn(
    tmp_path: Path,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    provider = ScriptedProvider(
        [
            [ProviderTextDelta(text="answer"), Finish()],
            [ProviderTextDelta(text="recovered"), Finish()],
        ]
    )
    app = make_app(
        provider,
        service,
        builder,
        session_id,
        run_ids=["run-storage", "run-recovered"],
        turn_ids=["turn-storage", "turn-recovered"],
    )
    before = service.store.path_for_session(session_id).read_bytes()
    original_commit = service.commit_turn

    def fail_commit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("C:/secret/session.jsonl API key sk-test-secret")

    service.commit_turn = fail_commit  # type: ignore[method-assign]
    failed_events = run_collect(app, "private prompt")
    failed_raw = service.store.path_for_session(session_id).read_bytes()
    assert failed_raw == before
    assert service.load_session(session_id).turns == ()

    service.commit_turn = original_commit  # type: ignore[method-assign]
    recovered_events = run_collect(app, "recovery prompt")

    terminals = [
        event for event in failed_events if isinstance(event, (RunCompleted, RunFailed))
    ]
    assert len(terminals) == 1
    assert isinstance(terminals[0], RunFailed)
    assert terminals[0].category is ErrorCategory.STORAGE
    assert terminals[0].message == "session persistence failed"
    assert "secret" not in terminals[0].message
    assert "private prompt" not in terminals[0].message
    assert isinstance(recovered_events[-1], RunCompleted)
    assert [
        turn.user_message.content for turn in service.load_session(session_id).turns
    ] == ["recovery prompt"]


class _HangingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        yield ProviderTextDelta(text="partial")
        self.started.set()
        await asyncio.sleep(30)

    async def aclose(self) -> None:
        return None
