"""P6-08 TUI runtime lifecycle, interaction, and shutdown tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.context_builder import ContextBuilder
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
    InteractionBroker,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.runtime_tools import register_runtime_tools
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.tui import (
    AssistantResponseView,
    MicaComposer,
    MicaTuiApp,
    MicaTuiRuntime,
    ReadToolCard,
    WriteToolCard,
)
from mica_agent.tui.interactions import AskInteractionView
from mica_agent.tui.permissions import PermissionInteractionView
from mica_agent.tui.tool_cards import ToolCard, ToolCardState
from mica_agent.tui.transcript import ConversationTranscript, UserMessageCard
from mica_agent.workspace import resolve_workspace


class ScriptedProvider:
    def __init__(
        self, scripts: list[list[ProviderEvent]], *, block_after_text: bool = False
    ) -> None:
        self.scripts = scripts
        self.block_after_text = block_after_text
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0
        self.stream_close_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        self.started.set()
        try:
            for event in self.scripts[
                min(len(self.requests) - 1, len(self.scripts) - 1)
            ]:
                yield event
                if (
                    self.block_after_text
                    and isinstance(event, ProviderTextDelta)
                    and not self.release.is_set()
                ):
                    await self.release.wait()
        finally:
            self.stream_close_calls += 1

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.release.set()


class RecordingBroker(InteractionBroker):
    def __init__(self) -> None:
        super().__init__()
        self.responses = []

    def respond(self, response):
        self.responses.append(response)
        return super().respond(response)


def make_app(
    tmp_path: Path,
    provider: ScriptedProvider,
    *,
    tool_executor: ToolExecutor | None = None,
    workspace_path: Path | None = None,
) -> MicaApp:
    workspace_path = workspace_path or tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "data"
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    return MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        run_id_factory=lambda: "run-runtime",
        turn_id_factory=lambda: "turn-runtime",
        tool_executor=tool_executor,
    )


async def wait_for_view(pilot, app: MicaTuiApp, view_type):
    for _ in range(100):
        if app.query(view_type):
            return app.query_one(view_type)
        await pilot.pause()
    raise AssertionError(f"{view_type.__name__} did not mount")


def make_interaction_runtime(tmp_path: Path, kind: str):
    broker = RecordingBroker()
    calls: list[str] = []
    registry = ToolRegistry()
    if kind == "permission":
        spec = ToolSpec("write", "Write", {"type": "object"})

        async def handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            calls.append(request.call_id)
            return ToolResult(
                request.call_id, request.name, ToolResultStatus.SUCCESS, "written"
            )

        operation = PolicyOperation.OTHER
        call = ToolCall("call-write", "write", {})
    else:
        spec = ToolSpec("ask", "Ask", {"type": "object", "required": ["question"]})

        async def handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            response = await broker(
                request.call_id,
                AskPrompt(
                    request.arguments["question"],
                    options=(AskOption("Alice"), AskOption("Bob")),
                ),
                cancellation,
            )
            calls.append(request.call_id)
            return ToolResult(
                request.call_id,
                request.name,
                ToolResultStatus.SUCCESS,
                "answer received",
                {"answer": response.answer},
            )

        operation = PolicyOperation.ASK
        call = ToolCall("call-ask", "ask", {"question": "Who?"})
    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        interaction_broker=broker,
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, operation, False, False
        ),
    )
    provider = ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("final"), Finish()]]
    )
    app = make_app(tmp_path, provider, tool_executor=executor)
    return provider, app, MicaTuiRuntime(app), broker, calls


def test_runtime_submits_once_projects_events_and_commits_one_turn(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("answer"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        runtime = MicaTuiRuntime(app)
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            await pilot.press(*"full original prompt", "enter")
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            assert runtime.submit("second prompt") is False
            provider.release.set()
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert len(runtime.tui.query(UserMessageCard)) == 1
            assert runtime.tui.query_one(MicaComposer).disabled is False
            assert app.history[0].content == "full original prompt"
            assert app.history[-1].content == "answer"
        assert provider.aclose_calls == 1
        assert provider.stream_close_calls == 1

    asyncio.run(scenario())


def test_runtime_projects_complete_tool_lifecycle_on_one_card(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        registry = ToolRegistry()
        spec = ToolSpec("read", "Read", {"type": "object"})

        async def handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            calls.append(request.call_id)
            return ToolResult(
                request.call_id,
                request.name,
                ToolResultStatus.SUCCESS,
                "read complete",
                {"content": "safe result"},
            )

        registry.register(spec, handler)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.READ, False, False
            ),
        )
        provider = ScriptedProvider(
            [
                [Finish(tool_calls=(ToolCall("call-read", "read", {}),))],
                [ProviderTextDelta("final"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        runtime = MicaTuiRuntime(app)
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("read safely") is True
            card = await wait_for_view(pilot, runtime.tui, ToolCard)
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert runtime.tui.query_one(ToolCard) is card
            assert card.state is ToolCardState.SUCCESS
            assert calls == ["call-read"]
            assert len(app.history) == 4
            assert app.history[-1].content == "final"
            assert runtime.tui.query_one(MicaComposer).disabled is False
        assert provider.stream_close_calls == 2
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_two_model_steps_keep_transcript_event_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        registry = ToolRegistry()

        async def read_handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            calls.append(request.call_id)
            return ToolResult(
                request.call_id,
                request.name,
                ToolResultStatus.SUCCESS,
                "read complete",
            )

        async def write_handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            calls.append(request.call_id)
            return ToolResult(
                request.call_id,
                request.name,
                ToolResultStatus.SUCCESS,
                "write complete",
            )

        registry.register(ToolSpec("read", "Read", {"type": "object"}), read_handler)
        registry.register(ToolSpec("write", "Write", {"type": "object"}), write_handler)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.READ, False, False
            ),
        )
        provider = ScriptedProvider(
            [
                [
                    ProviderTextDelta("step-1"),
                    Finish(
                        tool_calls=(
                            ToolCall("call-read", "read", {"path": "docs.md"}),
                            ToolCall(
                                "call-write",
                                "write",
                                {"path": "notes.md", "content": "safe"},
                            ),
                        )
                    ),
                ],
                [ProviderTextDelta("step-2"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        runtime = MicaTuiRuntime(app)
        async with runtime.tui.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert runtime.submit("use two tools") is True
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            children = [
                child
                for child in runtime.tui.query_one("#transcript").children
                if isinstance(
                    child, (AssistantResponseView, ReadToolCard, WriteToolCard)
                )
            ]
            assert [type(child) for child in children] == [
                AssistantResponseView,
                ReadToolCard,
                WriteToolCard,
                AssistantResponseView,
            ]
            responses = list(runtime.tui.query(AssistantResponseView))
            assert responses[0].text == "step-1"
            assert responses[1].text == "step-2"
            assert calls == ["call-read", "call-write"]
            assert app.history[-1].content == "step-2"
        assert provider.stream_close_calls == 2
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_routes_permission_panel_response_to_mica_app(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(
            tmp_path, "permission"
        )
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("approve tool") is True
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            panel = await wait_for_view(pilot, runtime.tui, PermissionInteractionView)
            panel.focus()
            await pilot.press("enter")
            await asyncio.wait_for(runtime.wait_idle(), timeout=2.0)
            assert calls == ["call-write"]
            assert broker.pending_count == 0
            assert app.history[-1].content == "final"
            assert len(runtime.tui.query(PermissionInteractionView)) == 0
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["deny", "escape"])
def test_runtime_permission_deny_and_escape_have_distinct_public_paths(
    tmp_path: Path, action: str
) -> None:
    async def scenario() -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(
            tmp_path, "permission"
        )
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("permission boundary") is True
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            panel = await wait_for_view(pilot, runtime.tui, PermissionInteractionView)
            panel.focus()
            await pilot.pause()
            assert runtime.tui.screen.focused is panel
            if action == "deny":
                await pilot.press("2")
                assert panel.selected_index == 1
                await pilot.press("enter")
            else:
                await pilot.press("escape")
            assert panel.is_locked is True
            assert panel.is_active is False
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            assert broker.pending_count == 0
            assert calls == []
            if action == "deny":
                assert len(broker.responses) == 1
                assert broker.responses[0].answer.value == "deny"
            else:
                assert broker.responses == []
            assert runtime.tui.query_one(MicaComposer).disabled is False
            assert panel.is_active is False
            if action == "deny":
                assert app.history[-1].content == "final"
                assert not any(
                    type(event).__name__ == "RunCancelled" for event in events
                )
            else:
                assert app.history == ()
                assert any(type(event).__name__ == "RunCancelled" for event in events)
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_ask_response_and_escape_keep_view_and_pending_contract(
    tmp_path: Path,
) -> None:
    async def scenario(action: str) -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(
            tmp_path / action, "ask"
        )
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("ask boundary") is True
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            view = await wait_for_view(pilot, runtime.tui, AskInteractionView)
            view.focus()
            if action == "answer":
                await pilot.press("enter")
            else:
                await pilot.press("escape")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            assert broker.pending_count == 0
            assert runtime.tui.query_one(MicaComposer).disabled is False
            assert view.is_active is False
            if action == "answer":
                assert broker.responses[0].answer == "Alice"
                assert app.history[-1].content == "final"
                assert view.terminal_status is ToolResultStatus.SUCCESS
            else:
                assert calls == []
                assert app.history == ()
                assert view.terminal_status is ToolResultStatus.CANCELLED
                assert any(type(event).__name__ == "RunCancelled" for event in events)
        assert provider.aclose_calls == 1

    for action in ("answer", "escape"):
        asyncio.run(scenario(action))


def test_runtime_cancels_after_projected_tool_output_and_closes_stream(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        workspace = resolve_workspace(workspace_path)
        registry = ToolRegistry()
        register_runtime_tools(registry, workspace, lambda *_args: None)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.COMMAND, False, False
            ),
        )
        provider = ScriptedProvider(
            [
                [
                    Finish(
                        tool_calls=(
                            ToolCall(
                                "call-bash",
                                "bash",
                                {"command": "printf first-output; sleep 30"},
                            ),
                        )
                    )
                ]
            ]
        )
        app = make_app(
            tmp_path, provider, tool_executor=executor, workspace_path=workspace_path
        )
        runtime = MicaTuiRuntime(app)
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("run output") is True
            card = await wait_for_view(pilot, runtime.tui, ToolCard)
            for _ in range(100):
                if "first-output" in card.render().plain:
                    break
                await pilot.pause()
            else:
                raise AssertionError("tool output was not projected")
            assert runtime.run_id == "run-runtime"
            assert runtime.cancel(runtime.run_id) is True
            await asyncio.wait_for(runtime.wait_idle(), timeout=3.0)
            await pilot.pause()
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert card.state is ToolCardState.CANCELLED
            assert "first-output" in card.render().plain
            assert (
                runtime.tui.query_one("#work-status")
                .render()
                .plain.startswith("Mica · interrupt")
            )
            assert app.history == ()
            assert runtime.active_task is None
        assert provider.stream_close_calls == 1
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


async def wait_for_response_text(
    pilot, app: MicaTuiApp, text: str
) -> AssistantResponseView:
    for _ in range(100):
        for view in app.query(AssistantResponseView):
            if text in view.text:
                return view
        await pilot.pause()
    raise AssertionError("response text was not projected")


def test_runtime_escape_cancels_streaming_provider_without_exiting(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        runtime = MicaTuiRuntime(app)
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("stream output") is True
            view = await wait_for_response_text(pilot, runtime.tui, "partial")
            runtime.tui.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert view.text == "partial"
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0
            assert app.history == ()
            assert runtime.active_task is None
            assert runtime.tui.query_one(MicaComposer).disabled is False
            assert runtime.tui.is_running
        assert provider.stream_close_calls == 1
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_escape_cancels_bash_after_projected_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        workspace = resolve_workspace(workspace_path)
        registry = ToolRegistry()
        register_runtime_tools(registry, workspace, lambda *_args: None)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.COMMAND, False, False
            ),
        )
        provider = ScriptedProvider(
            [
                [
                    Finish(
                        tool_calls=(
                            ToolCall(
                                "call-bash",
                                "bash",
                                {"command": "printf first-output; sleep 30"},
                            ),
                        )
                    )
                ]
            ]
        )
        app = make_app(
            tmp_path, provider, tool_executor=executor, workspace_path=workspace_path
        )
        runtime = MicaTuiRuntime(app)
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("run output") is True
            card = await wait_for_view(pilot, runtime.tui, ToolCard)
            for _ in range(100):
                if "first-output" in card.render().plain:
                    break
                await pilot.pause()
            else:
                raise AssertionError("tool output was not projected")
            runtime.tui.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert card.state is ToolCardState.CANCELLED
            assert "first-output" in card.render().plain
            assert app.history == ()
            assert runtime.active_task is None
            assert runtime.tui.is_running
            assert runtime.tui.query_one(MicaComposer).disabled is False
        assert provider.stream_close_calls == 1
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_escape_cancels_ask_without_synthesizing_an_answer(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(
            tmp_path, "ask"
        )
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("ask boundary") is True
            view = await wait_for_view(pilot, runtime.tui, AskInteractionView)
            runtime.tui.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert broker.pending_count == 0
            assert broker.responses == []
            assert calls == []
            assert app.history == ()
            assert view.is_active is False
            assert view.terminal_status is ToolResultStatus.CANCELLED
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert runtime.active_task is None
            assert runtime.tui.is_running
            view._request_cancel()
            view._submit_selection()
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_escape_cancels_permission_without_deny_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(
            tmp_path, "permission"
        )
        events = []
        original_project = runtime.tui.project_engine_event

        async def project(event):
            events.append(event)
            return await original_project(event)

        runtime.tui.project_engine_event = project
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("permission boundary") is True
            panel = await wait_for_view(pilot, runtime.tui, PermissionInteractionView)
            runtime.tui.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert broker.pending_count == 0
            assert broker.responses == []
            assert calls == []
            assert app.history == ()
            assert panel.is_active is False
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert runtime.active_task is None
            assert runtime.tui.is_running
            panel._request_cancel()
            panel._submit_selection()
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_runtime_ctrl_c_cancels_active_run_then_second_press_exits(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        class Clock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        runtime = MicaTuiRuntime(app, clock=clock)
        events = []
        exits: list[int] = []
        original_project = runtime.tui.project_engine_event
        original_exit = runtime.tui.exit

        async def project(event):
            events.append(event)
            return await original_project(event)

        def tracking_exit(result=None, return_code: int = 0, message=None):
            exits.append(return_code)
            original_exit(result, return_code, message)

        runtime.tui.project_engine_event = project
        runtime.tui.exit = tracking_exit
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("stream output") is True
            await wait_for_response_text(pilot, runtime.tui, "partial")
            await pilot.press("ctrl+c")
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            await pilot.pause()
            assert any(type(event).__name__ == "RunCancelled" for event in events)
            assert exits == []
            assert runtime.tui.is_running
            assert app.history == ()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert exits == [0]
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["permission", "ask"])
def test_runtime_close_waiting_interaction_clears_pending_and_rejects_late_input(
    tmp_path: Path, kind: str
) -> None:
    async def scenario() -> None:
        provider, app, runtime, broker, calls = make_interaction_runtime(tmp_path, kind)
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("close while waiting") is True
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            view_type = (
                PermissionInteractionView
                if kind == "permission"
                else AskInteractionView
            )
            old_view = await wait_for_view(pilot, runtime.tui, view_type)
            interaction_id = old_view.interaction_id
            await runtime.aclose()
            await runtime.aclose()
            assert runtime.is_closed
            assert runtime.active_task is None
            assert broker.pending_count == 0
            assert app.history == ()
            assert old_view.is_active is False
            assert runtime.submit("late") is False
            assert runtime.cancel("run-runtime") is False
            late_response = (
                PermissionResponse(interaction_id, PermissionAnswer.ALLOW_ONCE)
                if kind == "permission"
                else UserInputResponse(interaction_id, "late")
            )
            assert runtime.respond_interaction(late_response) is False
            old_view._request_cancel()
            old_view._submit_selection()
            assert interaction_id
            assert calls == []
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_external_consumer_cancellation_releases_owned_task_and_stream(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        runtime = MicaTuiRuntime(app)
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("external cancel") is True
            await asyncio.wait_for(provider.started.wait(), timeout=1.0)
            task = runtime.active_task
            assert task is not None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(100):
                if runtime.active_task is None:
                    break
                await pilot.pause()
            assert runtime.active_task is None
            assert provider.stream_close_calls == 1
            assert app.history == ()
            assert runtime.tui.query_one(MicaComposer).disabled is False
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_projection_failure_is_safely_contained_without_partial_turn(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[ProviderTextDelta("safe partial"), Finish()]])
        app = make_app(tmp_path, provider)
        runtime = MicaTuiRuntime(app)
        original_project = runtime.tui.project_engine_event

        async def fail_on_text(event):
            if type(event).__name__ == "TextDelta":
                raise RuntimeError("sk-secret C:/private/path")
            return await original_project(event)

        runtime.tui.project_engine_event = fail_on_text
        async with runtime.tui.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert runtime.submit("projection failure") is True
            await asyncio.wait_for(runtime.wait_idle(), timeout=5.0)
            assert app.history == ()
            assert runtime.active_task is None
            assert runtime.tui.query_one(MicaComposer).disabled is False
            status = runtime.tui.query_one("#work-status").render().plain
            assert status.startswith("Mica · interrupt")
            assert "secret" not in status and "private" not in status
        assert provider.stream_close_calls == 1
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_run_requester_safe_rejection_does_not_create_card_or_leak_awaitable() -> None:
    async def scenario() -> None:
        created: list[object] = []

        async def callback(*_args: object) -> bool:
            return True

        with pytest.raises(TypeError, match="synchronous callable"):
            MicaTuiApp(run_requester=callback)

        async def value() -> bool:
            return True

        def awaitable_callback(_prompt: str):
            result = value()
            created.append(result)
            return result

        app = MicaTuiApp(run_requester=awaitable_callback)
        async with app.run_test(size=(60, 16)) as pilot:
            await pilot.pause()
            await pilot.press(*"safe callback", "enter")
            await pilot.pause()
            assert len(app.query(UserMessageCard)) == 0
            assert app.query_one(MicaComposer).disabled is False
            assert len(created) == 1
            assert inspect.getcoroutinestate(created[0]) is inspect.CORO_CLOSED

        errors = MicaTuiApp(
            run_requester=lambda _prompt: (_ for _ in ()).throw(RuntimeError("secret"))
        )
        async with errors.run_test(size=(60, 16)) as pilot:
            await pilot.pause()
            await pilot.press(*"error callback", "enter")
            await pilot.pause()
            assert len(errors.query(UserMessageCard)) == 0
            assert errors.query_one(MicaComposer).disabled is False

    asyncio.run(scenario())
