"""P3-05 App/Session/CLI 持久化闭环集成测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mica_agent.app import MicaApp
from mica_agent.cli import run_interactive
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
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
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


class InputScript:
    def __init__(self, *items: object) -> None:
        self.items = list(items)

    async def __call__(self) -> str:
        if not self.items:
            raise EOFError
        value = self.items.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def text(self) -> str:
        return "".join(self.calls)


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
    assert second_provider.requests[0].messages == (
        Message(MessageRole.SYSTEM, "global rules"),
        Message(MessageRole.SYSTEM, "workspace rules"),
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
    assert provider.requests[1].messages == (
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
    before = service.store.path_for_session(session_id).read_bytes()

    first_events = run_collect(app, "failed question")
    after_failure = service.store.path_for_session(session_id).read_bytes()
    second_events = run_collect(app, "successful question")

    assert isinstance(first_events[-1], RunFailed)
    assert after_failure == before
    assert isinstance(second_events[-1], RunCompleted)
    stored = service.load_session(session_id)
    assert len(stored.turns) == 1
    assert stored.turns[0].user_message.content == "successful question"


@pytest.mark.parametrize("mode", ["failed", "cancelled", "closed"])
def test_non_successful_run_modes_do_not_append_turn(
    tmp_path: Path,
    mode: str,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    before = service.store.path_for_session(session_id).read_bytes()

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
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_run())

    assert service.store.path_for_session(session_id).read_bytes() == before
    assert service.load_session(session_id).turns == ()


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


def _run_cli_with_injected_session(
    service: SessionService,
    builder: ContextBuilder,
    provider: ScriptedProvider,
    *,
    mode: str = "new",
    inputs: tuple[object, ...] = (),
) -> tuple[int, Recorder, Recorder, list[bool]]:
    out = Recorder()
    err = Recorder()
    provider_created: list[bool] = []

    def factory(_url: str, _key: str) -> ScriptedProvider:
        provider_created.append(True)
        return provider

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-key",
            "test-model",
            session_mode=mode,  # type: ignore[arg-type]
            provider_factory=factory,
            read_line=InputScript(*inputs),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
            clock=lambda: TEST_TIME,
        )
    )
    return code, out, err, provider_created


def test_cli_default_creates_new_session_and_enters_app(tmp_path: Path) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before = len(service.list_sessions().sessions)
    provider = ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, inputs=("hello",)
    )

    assert code == 0
    assert created == [True]
    assert out.text == "ok\n"
    assert err.text == ""
    assert len(service.list_sessions().sessions) == before + 1


def test_cli_default_blank_eof_keeps_one_empty_session_without_request(
    tmp_path: Path,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before_ids = {
        session.meta.session_id for session in service.list_sessions().sessions
    }
    provider = ScriptedProvider([])

    code, out, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        inputs=("", "   ", "\t"),
    )

    after_sessions = service.list_sessions().sessions
    after_ids = {session.meta.session_id for session in after_sessions}
    new_ids = after_ids - before_ids
    assert code == 0
    assert created == [True]
    assert provider.requests == []
    assert provider.aclose_calls == 1
    assert out.text == ""
    assert err.text == ""
    assert len(new_ids) == 1
    new_session = next(
        session for session in after_sessions if session.meta.session_id in new_ids
    )
    assert new_session.turns == ()


def test_cli_context_preflight_failure_does_not_construct_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    provider = ScriptedProvider([])

    def fail_build(_messages: object) -> object:
        raise RuntimeError("context failure with private path")

    monkeypatch.setattr(builder, "build", fail_build)
    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("question",)
    )

    assert code == 2
    assert created == []
    assert provider.aclose_calls == 0
    assert out.text == ""
    assert err.text == "mica: unable to prepare session context\n"


def test_cli_continue_reuses_last_active_session_and_history(tmp_path: Path) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    first_provider = ScriptedProvider(
        [[ProviderTextDelta(text="old answer"), Finish()]]
    )
    first_app = make_app(first_provider, service, builder, session_id)
    assert isinstance(run_collect(first_app, "old question")[-1], RunCompleted)
    provider = ScriptedProvider([[ProviderTextDelta(text="new answer"), Finish()]])

    code, _, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        mode="continue",
        inputs=("new question",),
    )

    assert code == 0
    assert created == [True]
    assert err.text == ""
    assert provider.requests[0].messages[-3:] == (
        Message(MessageRole.USER, "old question"),
        Message(MessageRole.ASSISTANT, "old answer"),
        Message(MessageRole.USER, "new question"),
    )
    assert len(service.list_sessions().sessions) == 1


def test_cli_resume_lists_numbered_sessions_and_accepts_valid_choice(
    tmp_path: Path,
) -> None:
    service, builder, first_id, _, _ = setup_session(tmp_path)
    service.rename_session(first_id, "first session")
    second = service.create_session(title="second session")
    listed = service.list_sessions().sessions
    selected_index = next(
        index
        for index, session in enumerate(listed, start=1)
        if session.meta.session_id == second.meta.session_id
    )
    provider = ScriptedProvider([[ProviderTextDelta(text="resumed"), Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        mode="resume",
        inputs=(str(selected_index), "resume question"),
    )

    assert code == 0
    assert created == [True]
    listed_titles = {line.split(". ", 1)[1] for line in out.text.splitlines()[:2]}
    assert {"first session", "second session"} == listed_titles
    assert "resumed\n" in out.text
    assert err.text == ""
    assert provider.requests[0].messages[-1] == Message(
        MessageRole.USER, "resume question"
    )


def test_cli_continue_without_healthy_session_never_creates_provider(
    tmp_path: Path,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    service.delete_session(session_id)
    provider = ScriptedProvider([[Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("should not read",)
    )

    assert code != 0
    assert created == []
    assert out.text == ""
    assert "no healthy session" in err.text
    assert "session.jsonl" not in err.text


def test_cli_resume_eof_or_invalid_choice_never_creates_provider(
    tmp_path: Path,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before_count = len(service.list_sessions().sessions)
    provider = ScriptedProvider([[Finish()]])

    eof_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=()
    )
    invalid_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=("999",)
    )
    unicode_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=("²",)
    )

    for code, out, err, created in (eof_result, invalid_result, unicode_result):
        assert code != 0
        assert out.text.startswith("1. ")
        assert created == []
        assert "session selection" in err.text
        assert "session.jsonl" not in err.text

    code, out, err, created = unicode_result
    assert code == 2
    assert out.text == "1. untitled session\n"
    assert err.text == "mica: invalid session selection; enter a listed number\n"
    assert created == []
    assert "Traceback" not in err.text
    assert "sk-test-key" not in err.text
    assert str(tmp_path) not in err.text
    assert len(service.list_sessions().sessions) == before_count


def test_cli_preflight_displays_safe_agents_diagnostics(
    tmp_path: Path,
) -> None:
    service, builder, session_id, data_root, workspace_path = setup_session(tmp_path)
    data_root.joinpath("AGENTS.md").write_bytes(b"\xff")
    workspace_path.joinpath("AGENTS.md").write_text(
        "workspace warning rules", encoding="utf-8"
    )
    provider = ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])

    code, _, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("question",)
    )

    assert code == 0
    assert created == [True]
    assert err.text == "warning: global AGENTS.md is not valid UTF-8\n"
    assert provider.requests[0].messages == (
        Message(MessageRole.SYSTEM, "workspace warning rules"),
        Message(MessageRole.USER, "question"),
    )
    assert (
        b"workspace warning rules"
        not in service.store.path_for_session(session_id).read_bytes()
    )
    assert (
        b"workspace warning rules"
        not in service.store.path_for_session(session_id).read_bytes()
    )


class _HangingProvider:
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        yield ProviderTextDelta(text="partial")
        await asyncio.sleep(30)

    async def aclose(self) -> None:
        return None
