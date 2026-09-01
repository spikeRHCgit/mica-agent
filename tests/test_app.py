"""MicaApp 公共行为测试（P2-03）。

最小 Provider 与 httpx.MockTransport 均只存在于测试文件内；
跨层链路（MockTransport → OpenAICompatibleProvider → AgentEngine
→ MicaApp → EngineEvent → 进程内 history）在文件末尾验证。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from mica_agent.app import AppBusyError, AppClosedError, MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.context_builder import ContextBuilder, ContextBuildResult
from mica_agent.events import (
    EngineEvent,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
)
from mica_agent.interactions import PermissionAnswer, PermissionResponse
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    OpenAICompatibleProvider,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderReasoningDelta,
    ProviderRequest,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationStatus,
)
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace

_TEST_TEMP_DIRS: list[TemporaryDirectory[str]] = []


@pytest.fixture(autouse=True)
def _cleanup_test_tempdirs():
    try:
        yield
    finally:
        while _TEST_TEMP_DIRS:
            _TEST_TEMP_DIRS.pop().cleanup()


class _ScriptedProvider:
    """按请求次序产出固定事件序列的最小 Provider（本文件专用）。"""

    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self._scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        events = self._scripts[min(len(self._scripts) - 1, len(self.requests) - 1)]
        for event in events:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _make_session_components(
    provider: object,
) -> tuple[SessionService, str, ContextBuilder]:
    tempdir = TemporaryDirectory(prefix="mica-app-test-")
    _TEST_TEMP_DIRS.append(tempdir)
    root = Path(tempdir.name)
    workspace_path = root / "workspace"
    workspace_path.mkdir()
    data_root = root / "mica-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    # Keep the temporary directory alive for the duration of the provider/app.
    provider._mica_test_tempdir = tempdir  # type: ignore[attr-defined]
    return (
        service,
        session.meta.session_id,
        ContextBuilder(
            data_root=data_root,
            workspace=workspace,
        ),
    )


def _make_app(
    provider: _ScriptedProvider,
    *,
    model: str = "test-model",
    system_prompt: str | None = None,
) -> MicaApp:
    service, session_id, context_builder = _make_session_components(provider)
    run_number = 0

    def run_id_factory() -> str:
        nonlocal run_number
        run_number += 1
        return "run-test" if run_number == 1 else f"run-test-{run_number}"

    return MicaApp(
        provider=provider,
        model=model,
        session_service=service,
        session_id=session_id,
        context_builder=context_builder,
        system_prompt=system_prompt,
        run_id_factory=run_id_factory,
    )


async def _collect(
    app: MicaApp,
    prompt: str,
    collected: list[EngineEvent] | None = None,
) -> list[EngineEvent]:
    if collected is None:
        collected = []
    async for event in app.run(prompt):
        collected.append(event)
    return collected


def _run_collect(app: MicaApp, prompt: str) -> list[EngineEvent]:
    return asyncio.run(_collect(app, prompt))


def _user(content: str) -> Message:
    return Message(role=MessageRole.USER, content=content)


def _assistant(content: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=content)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["", "   ", "\t\n"])
def test_blank_model_is_rejected(model: str) -> None:
    with pytest.raises(ValueError):
        _make_app(_ScriptedProvider([[]]), model=model)


def test_non_str_model_is_rejected() -> None:
    with pytest.raises(TypeError):
        _make_app(_ScriptedProvider([[]]), model=123)  # type: ignore[arg-type]


def test_non_str_system_prompt_is_rejected() -> None:
    with pytest.raises(TypeError):
        _make_app(_ScriptedProvider([[]]), system_prompt=42)  # type: ignore[arg-type]


@pytest.mark.parametrize("prompt", ["", "   ", "\t\n"])
def test_blank_prompt_is_rejected_before_provider(prompt: str) -> None:
    provider = _ScriptedProvider([[]])
    app = _make_app(provider)

    with pytest.raises(ValueError):
        _run_collect(app, prompt)

    assert provider.requests == []  # 网络请求前拒绝


def test_non_str_prompt_is_rejected_before_provider() -> None:
    provider = _ScriptedProvider([[]])
    app = _make_app(provider)

    with pytest.raises(TypeError):
        _run_collect(app, 123)  # type: ignore[arg-type]

    assert provider.requests == []


def test_each_run_selects_reasoning_effort_without_rebuilding_header() -> None:
    provider = _ScriptedProvider([[Finish()], [Finish()]])
    app = _make_app(provider, system_prompt="frozen system prompt")

    async def collect(prompt: str, effort: ReasoningEffort) -> None:
        async for _ in app.run(prompt, reasoning_effort=effort):
            pass

    asyncio.run(collect("first", ReasoningEffort.HIGH))
    asyncio.run(collect("second", ReasoningEffort.LOW))

    assert [request.reasoning_effort for request in provider.requests] == [
        ReasoningEffort.HIGH,
        ReasoningEffort.LOW,
    ]
    assert provider.requests[0].messages[0] is provider.requests[1].messages[0]
    assert provider.requests[0].messages[0].content == "frozen system prompt"


def test_reasoning_is_visible_per_run_but_not_saved_or_restored() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderReasoningDelta("private thought"),
                ProviderTextDelta("answer"),
                Finish(),
            ]
        ]
    )
    service, session_id, builder = _make_session_components(provider)
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        system_prompt="system snapshot",
        run_id_factory=lambda: "run-reasoning-app",
        turn_id_factory=lambda: "turn-reasoning-app",
    )

    async def collect() -> list[EngineEvent]:
        return [
            event
            async for event in app.run(
                "question", reasoning_effort=ReasoningEffort.HIGH
            )
        ]

    events = asyncio.run(collect())

    assert ReasoningDelta("run-reasoning-app", "private thought", 1) in events
    assert app.history == (_user("question"), _assistant("answer"))
    raw = service.store.path_for_session(session_id).read_text(encoding="utf-8")
    assert "private thought" not in raw

    restored_provider = _ScriptedProvider([[Finish()]])
    restored_app = MicaApp(
        restored_provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        system_prompt="system snapshot",
    )
    _run_collect(restored_app, "next")
    assert restored_provider.requests[0].messages == (
        Message(MessageRole.SYSTEM, "system snapshot"),
        _user("question"),
        _assistant("answer"),
        _user("next"),
    )
    assert provider.requests[0].reasoning_effort is ReasoningEffort.HIGH
    assert restored_provider.requests[0].reasoning_effort is ReasoningEffort.OFF


def test_reasoning_then_failure_does_not_commit_partial_turn() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderReasoningDelta("private failed thought"),
                ProviderErrorEvent(AuthenticationError("auth")),
            ]
        ]
    )
    app = _make_app(provider)

    events = _run_collect(app, "question")

    assert any(isinstance(event, ReasoningDelta) for event in events)
    assert isinstance(events[-1], RunFailed)
    assert app.history == ()


def test_consumer_closing_app_after_reasoning_cleans_stream_and_history() -> None:
    class Provider:
        def __init__(self) -> None:
            self.stream_closed = False
            self.aclose_calls = 0

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderReasoningDelta("thinking")
                await asyncio.sleep(30)
                yield Finish()
            finally:
                self.stream_closed = True

        async def aclose(self) -> None:
            self.aclose_calls += 1

    async def scenario() -> tuple[MicaApp, Provider, list[EngineEvent]]:
        provider = Provider()
        service, session_id, builder = _make_session_components(provider)
        app = MicaApp(
            provider,
            "test-model",
            session_service=service,
            session_id=session_id,
            context_builder=builder,
            system_prompt="system snapshot",
        )
        stream = app.run("question", reasoning_effort=ReasoningEffort.MEDIUM)
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        await app.aclose()
        await app.aclose()
        return app, provider, events

    app, provider, events = asyncio.run(scenario())

    assert isinstance(events[1], ReasoningDelta)
    assert app.history == ()
    assert provider.stream_closed is True
    assert provider.aclose_calls == 1


# ---------------------------------------------------------------------------
# 成功 Run 与原子提交
# ---------------------------------------------------------------------------


def test_tool_run_commits_one_complete_turn_and_restores_full_history() -> None:
    call = ToolCall("call-1", "read", {"path": "notes.txt"})
    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "read complete",
            {"content": "saved result"},
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    provider = _ScriptedProvider(
        [
            [Finish(tool_calls=(call,))],
            [ProviderTextDelta("final answer"), Finish()],
        ]
    )
    service, session_id, context_builder = _make_session_components(provider)
    context_builder.data_root.mkdir(exist_ok=True)
    context_builder.data_root.joinpath("AGENTS.md").write_text(
        "global tool rules", encoding="utf-8"
    )
    context_builder.workspace.workspace_path.joinpath("AGENTS.md").write_text(
        "workspace tool rules", encoding="utf-8"
    )
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=context_builder,
        system_prompt="tool system snapshot",
        run_id_factory=lambda: "run-tools",
        turn_id_factory=lambda: "turn-tools",
        tool_executor=executor,
    )

    events = _run_collect(app, "inspect")

    assert isinstance(events[-1], RunCompleted)
    assert provider.requests[0].tools == (spec,)
    assert provider.requests[0].messages[:3] == provider.requests[1].messages[:3]
    assert [message.role for message in provider.requests[0].messages[:3]] == [
        MessageRole.SYSTEM,
        MessageRole.SYSTEM,
        MessageRole.SYSTEM,
    ]
    assert len(service.load_session(session_id).turns) == 1
    stored = service.load_session(session_id)
    assert stored.meta.format_version == 3
    assert len(stored.turns[0].tool_presentations) == 1
    presentation = stored.turns[0].tool_presentations[0]
    assert presentation.kind is ToolPresentationKind.READ
    assert presentation.status is ToolPresentationStatus.SUCCESS
    assert presentation.payload == {"path": "notes.txt"}

    assert app.history == (
        Message(MessageRole.USER, "inspect"),
        Message(MessageRole.ASSISTANT, "", (call,)),
        Message(
            MessageRole.TOOL,
            '{"status":"success","message":"read complete",'
            '"data":{"content":"saved result"}}',
            tool_call_id="call-1",
        ),
        Message(MessageRole.ASSISTANT, "final answer"),
    )


def test_storage_failure_after_side_effect_warns_and_keeps_session_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ToolSpec("write", "Write", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "write complete"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.ACCEPT_EDITS),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.WRITE, False, False
        ),
    )
    provider = _ScriptedProvider(
        [
            [Finish(tool_calls=(ToolCall("call-write", "write", {}),))],
            [ProviderTextDelta("done"), Finish()],
        ]
    )
    service, session_id, context_builder = _make_session_components(provider)
    path = service.store.path_for_session(session_id)
    before = path.read_bytes()
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=context_builder,
        tool_executor=executor,
    )

    def fail_commit(*args: object, **kwargs: object) -> object:
        raise RuntimeError("sk-private C:/secret/file")

    monkeypatch.setattr(service, "commit_turn", fail_commit)
    events = _run_collect(app, "write")

    assert path.read_bytes() == before
    assert len([event for event in events if isinstance(event, RunFailed)]) == 1
    assert not any(isinstance(event, RunCompleted) for event in events)
    terminal = events[-1]
    assert isinstance(terminal, RunFailed)
    assert terminal.category.value == "storage"
    assert "check the workspace" in terminal.message
    assert "private" not in terminal.message


def test_respond_interaction_forwards_only_current_matching_response() -> None:
    spec = ToolSpec("write", "Write", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "written"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT))
    provider = _ScriptedProvider(
        [
            [Finish(tool_calls=(ToolCall("call-write", "write", {}),))],
            [Finish()],
        ]
    )
    service, session_id, builder = _make_session_components(provider)
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        tool_executor=executor,
    )

    async def scenario() -> list[EngineEvent]:
        stream = app.run("write")
        events: list[EngineEvent] = []
        async for event in stream:
            events.append(event)
            if isinstance(event, PermissionRequested):
                wrong = PermissionResponse("wrong-id", PermissionAnswer.ALLOW_ONCE)
                assert app.respond_interaction(wrong) is False
                response = PermissionResponse(
                    event.interaction_id, PermissionAnswer.ALLOW_ONCE
                )
                assert app.respond_interaction(response) is True
                assert app.respond_interaction(response) is False
        return events

    events = asyncio.run(scenario())

    assert isinstance(events[-1], RunCompleted)
    no_tools = _make_app(_ScriptedProvider([[Finish()]]))
    assert (
        no_tools.respond_interaction(
            PermissionResponse("unused", PermissionAnswer.ALLOW_ONCE)
        )
        is False
    )


def test_successful_run_commits_user_and_assistant_atomically() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="hello"),
                ProviderTextDelta(text=" world"),
                Finish(),
            ]
        ]
    )
    app = _make_app(provider)

    events = _run_collect(app, "first prompt")

    assert events == [
        RunStarted(run_id="run-test"),
        TextDelta(run_id="run-test", text="hello", seq=1),
        TextDelta(run_id="run-test", text=" world", seq=2),
        RunCompleted(run_id="run-test", seq=3),
    ]
    assert app.history == (_user("first prompt"), _assistant("hello world"))
    assert provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[0].messages[0].content.startswith("# Mica\n")
    assert provider.requests[0].messages[1:] == (_user("first prompt"),)


def test_successful_run_without_text_commits_empty_assistant() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    _run_collect(app, "empty reply")

    assert app.history == (_user("empty reply"), _assistant(""))


def test_system_prompt_is_first_and_appears_once() -> None:
    provider = _ScriptedProvider(
        [
            [Finish()],
            [ProviderTextDelta(text="ok"), Finish()],
        ]
    )
    app = _make_app(provider, system_prompt="be brief")

    _run_collect(app, "q1")
    _run_collect(app, "q2")

    assert provider.requests[0].messages == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        _user("q1"),
    )
    assert provider.requests[1].messages == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        _user("q1"),
        _assistant(""),
        _user("q2"),
    )
    assert app.history == (
        _user("q1"),
        _assistant(""),
        _user("q2"),
        _assistant("ok"),
    )


def test_app_freezes_header_once_but_refreshes_history_for_each_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ScriptedProvider(
        [
            [ProviderTextDelta("answer one"), Finish()],
            [ProviderTextDelta("answer two"), Finish()],
        ]
    )
    service, session_id, builder = _make_session_components(provider)
    builder.data_root.mkdir(exist_ok=True)
    global_path = builder.data_root / "AGENTS.md"
    workspace_path = builder.workspace.workspace_path / "AGENTS.md"
    global_path.write_text("global one", encoding="utf-8")
    workspace_path.write_text("workspace one", encoding="utf-8")
    original_build = builder.build
    build_calls = 0

    def counted_build(*args: object, **kwargs: object):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(builder, "build", counted_build)
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        system_prompt="system snapshot 2026-08-28",
    )

    assert build_calls == 1
    global_path.write_text("global two", encoding="utf-8")
    workspace_path.unlink()
    _run_collect(app, "question one")
    _run_collect(app, "question two")

    expected_header = (
        Message(MessageRole.SYSTEM, "system snapshot 2026-08-28"),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="global">\nglobal one\n</INSTRUCTIONS>',
        ),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="workspace">\nworkspace one\n</INSTRUCTIONS>',
        ),
    )
    assert build_calls == 1
    assert provider.requests[0].messages[:3] == expected_header
    assert provider.requests[1].messages[:3] == expected_header
    assert provider.requests[1].messages[3:] == (
        _user("question one"),
        _assistant("answer one"),
        _user("question two"),
    )
    raw = service.store.path_for_session(session_id).read_text(encoding="utf-8")
    for header_text in ("system snapshot", "global one", "workspace one"):
        assert header_text not in raw

    second_provider = _ScriptedProvider([[Finish()]])
    second_app = MicaApp(
        second_provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        system_prompt="system snapshot 2026-08-29",
    )
    assert build_calls == 2
    _run_collect(second_app, "question three")
    assert second_provider.requests[0].messages[:2] == (
        Message(MessageRole.SYSTEM, "system snapshot 2026-08-29"),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="global">\nglobal two\n</INSTRUCTIONS>',
        ),
    )


def test_default_system_prompt_is_built_once_across_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mica_agent.app as app_module

    provider = _ScriptedProvider([[Finish()], [Finish()]])
    service, session_id, builder = _make_session_components(provider)
    dates = iter(("2026-08-28", "2026-08-29"))
    prompt_calls = 0

    def prompt_for_activation(_workspace: object) -> str:
        nonlocal prompt_calls
        prompt_calls += 1
        return f"system date {next(dates)}"

    monkeypatch.setattr(app_module, "build_system_prompt", prompt_for_activation)
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
    )
    _run_collect(app, "before midnight")
    _run_collect(app, "after midnight")

    assert prompt_calls == 1
    assert provider.requests[0].messages[0] == Message(
        MessageRole.SYSTEM, "system date 2026-08-28"
    )
    assert provider.requests[1].messages[0] == provider.requests[0].messages[0]

    second_provider = _ScriptedProvider([[Finish()]])
    second_app = MicaApp(
        second_provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
    )
    _run_collect(second_app, "new activation")
    assert prompt_calls == 2
    assert second_provider.requests[0].messages[0] == Message(
        MessageRole.SYSTEM, "system date 2026-08-29"
    )


def test_prebuilt_context_snapshot_is_used_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ScriptedProvider([[Finish()]])
    service, session_id, builder = _make_session_components(provider)
    snapshot = ContextBuildResult(
        messages=(
            Message(
                MessageRole.SYSTEM,
                '<INSTRUCTIONS source="global">\nfrozen\n</INSTRUCTIONS>',
            ),
        ),
        diagnostics=(),
    )

    def forbidden_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("context must not be rebuilt")

    monkeypatch.setattr(builder, "build", forbidden_build)
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        system_prompt="system snapshot",
        context_snapshot=snapshot,
    )
    _run_collect(app, "question")

    assert provider.requests[0].messages == (
        Message(MessageRole.SYSTEM, "system snapshot"),
        *snapshot.messages,
        _user("question"),
    )


# ---------------------------------------------------------------------------
# 失败 / 取消 / 提前关闭时历史回滚
# ---------------------------------------------------------------------------


def test_failed_run_rolls_back_history() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderErrorEvent(error=AuthenticationError("auth")),
            ]
        ]
    )
    app = _make_app(provider)

    events = _run_collect(app, "question")

    assert isinstance(events[-1], RunFailed)
    assert app.history == ()  # 用户消息与部分文本都不提交
    assert provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[0].messages[1:] == (_user("question"),)


def test_cancelled_run_rolls_back_history() -> None:
    class HangingProvider:
        def __init__(self) -> None:
            self.requests: list[ProviderRequest] = []

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            yield ProviderTextDelta(text="partial")
            await asyncio.sleep(30)
            yield Finish()

        async def aclose(self) -> None:
            return None

    app = _make_app(HangingProvider())  # type: ignore[arg-type]
    collected: list[EngineEvent] = []

    async def main() -> None:
        task = asyncio.create_task(_collect(app, "question", collected))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    assert isinstance(collected[-1], RunCancelled)
    assert app.history == ()  # 取消不提交任何内容


def test_consumer_closing_stream_releases_active_state_and_rolls_back() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderTextDelta(text=" more"),
                Finish(),
            ],
            [ProviderTextDelta(text="second ok"), Finish()],
        ]
    )
    app = _make_app(provider)

    async def main() -> None:
        # 显式保存事件流，消费部分事件后关闭，不依赖事件循环清理。
        stream = app.run("question")
        collected: list[EngineEvent] = []
        async for event in stream:
            collected.append(event)
            if isinstance(event, TextDelta):
                break
        await stream.aclose()
        # 同一事件循环内立即启动第二个 Run：活动状态必须已释放。
        second = await _collect(app, "follow-up")
        assert isinstance(second[-1], RunCompleted)

    asyncio.run(main())

    # 第一轮未收到 RunCompleted → 全部回滚；第二轮正常提交。
    assert app.history == (
        _user("follow-up"),
        _assistant("second ok"),
    )


# ---------------------------------------------------------------------------
# 多轮上下文
# ---------------------------------------------------------------------------


def test_second_run_carries_full_committed_context() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderTextDelta(text="answer one"), Finish()],
            [ProviderTextDelta(text="answer two"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "question one")
    _run_collect(app, "question two")

    assert len(provider.requests) == 2
    header = provider.requests[0].messages[:1]
    assert header[0].role is MessageRole.SYSTEM
    assert provider.requests[0].messages[1:] == (_user("question one"),)
    assert provider.requests[1].messages == header + (
        _user("question one"),
        _assistant("answer one"),
        _user("question two"),
    )
    assert app.history == (
        _user("question one"),
        _assistant("answer one"),
        _user("question two"),
        _assistant("answer two"),
    )


def test_failed_run_does_not_break_following_successful_run() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderErrorEvent(error=AuthenticationError("auth"))],
            [ProviderTextDelta(text="retry ok"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "first")
    _run_collect(app, "second")

    # 失败轮未提交，第二轮请求在固定头部后只有自己的用户消息。
    assert provider.requests[1].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[1].messages[1:] == (_user("second"),)
    assert app.history == (_user("second"), _assistant("retry ok"))


# ---------------------------------------------------------------------------
# history 快照
# ---------------------------------------------------------------------------


def test_history_returns_immutable_snapshot() -> None:
    provider = _ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])
    app = _make_app(provider)

    _run_collect(app, "q")
    snapshot = app.history

    assert isinstance(snapshot, tuple)
    assert snapshot == (_user("q"), _assistant("ok"))
    # 快照是独立对象：重复取值得到相同内容，但内部历史可继续增长。
    assert app.history == snapshot
    _run_collect(app, "q2")
    assert snapshot == (_user("q"), _assistant("ok"))  # 旧快照不受影响
    assert app.history == (_user("q"), _assistant("ok"), _user("q2"), _assistant("ok"))


# ---------------------------------------------------------------------------
# 并发与关闭
# ---------------------------------------------------------------------------


def test_second_run_is_rejected_while_first_is_active() -> None:
    class SlowProvider:
        def __init__(self) -> None:
            self.requests: list[ProviderRequest] = []

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            yield ProviderTextDelta(text="first")
            await asyncio.sleep(30)
            yield Finish()

        async def aclose(self) -> None:
            return None

    app = _make_app(SlowProvider())  # type: ignore[arg-type]
    first_events: list[EngineEvent] = []

    async def first_run() -> None:
        async for event in app.run("first"):
            first_events.append(event)

    async def main() -> None:
        task = asyncio.create_task(first_run())
        await asyncio.sleep(0.05)  # 等待第一个 Run 进入活动状态
        with pytest.raises(AppBusyError):
            async for _ in app.run("second"):  # 第二个 Run 必须失败
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    # 第一个 Run 未被破坏：文本事件已产出，历史未因并发尝试而修改。
    assert any(isinstance(event, TextDelta) for event in first_events)
    assert app.history == ()


def test_run_allowed_again_after_previous_run_terminates() -> None:
    provider = _ScriptedProvider(
        [
            [Finish()],
            [ProviderTextDelta(text="ok"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "one")
    events = _run_collect(app, "two")  # 第一个 Run 终止后允许再次运行

    assert isinstance(events[-1], RunCompleted)
    assert app.history == (_user("one"), _assistant(""), _user("two"), _assistant("ok"))


def test_run_allowed_after_failed_run_releases_active_state() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderErrorEvent(error=AuthenticationError("auth"))],
            [Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "one")
    events = _run_collect(app, "two")

    assert isinstance(events[-1], RunCompleted)


def test_aclose_closes_provider_and_blocks_further_runs() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    _run_collect(app, "q")
    asyncio.run(app.aclose())

    assert provider.aclose_calls == 1
    with pytest.raises(AppClosedError):
        _run_collect(app, "after close")
    assert len(provider.requests) == 1  # 关闭后未访问 Provider


def test_aclose_is_idempotent() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    asyncio.run(app.aclose())
    asyncio.run(app.aclose())

    assert provider.aclose_calls == 1  # 第二次调用不重复关闭


def test_closed_app_never_touches_provider() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)
    asyncio.run(app.aclose())

    with pytest.raises(AppClosedError):
        _run_collect(app, "late")  # type: ignore[arg-type]

    assert provider.requests == []


# ---------------------------------------------------------------------------
# 跨层链路：MockTransport → OpenAICompatibleProvider → Engine → App
# ---------------------------------------------------------------------------


def test_full_stack_via_openai_provider_and_mock_transport() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))  # type: ignore[arg-type]
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            request=request,
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test-key",
        base_url="https://api.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    service, session_id, context_builder = _make_session_components(provider)
    run_number = 0

    def run_id_factory() -> str:
        nonlocal run_number
        run_number += 1
        return "run-test" if run_number == 1 else f"run-test-{run_number}"

    app = MicaApp(
        provider=provider,
        model="test-model",
        session_service=service,
        session_id=session_id,
        context_builder=context_builder,
        system_prompt="be brief",
        run_id_factory=run_id_factory,
    )

    first = _run_collect(app, "hello")
    second = _run_collect(app, "again")

    # 事件流透传 Engine 事件；两轮都以 RunCompleted 结束。
    assert isinstance(first[-1], RunCompleted)
    assert isinstance(second[-1], RunCompleted)
    assert [event.text for event in first if isinstance(event, TextDelta)] == [
        "hi",
        "!",
    ]
    # 第二轮请求携带完整前文；system prompt 始终在最前且只出现一次。
    assert captured[0]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    assert captured[1]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi!"},
        {"role": "user", "content": "again"},
    ]
    # 进程内 history 与提交规则一致。
    assert app.history == (
        _user("hello"),
        _assistant("hi!"),
        _user("again"),
        _assistant("hi!"),
    )

    asyncio.run(app.aclose())
