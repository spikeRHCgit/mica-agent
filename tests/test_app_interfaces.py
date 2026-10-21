"""默认工厂到真实适配器、Engine、会话及下一轮的隔离装配验证。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from itertools import permutations

import httpx
import pytest
from sse_transport_helpers import RecordingTransport, sse_event, sse_response
from test_anthropic_messages_provider import (
    block_start,
    delta,
    sample,
    start,
    text,
    thinking,
)
from test_config_service import APIS, make_service
from test_openai_responses_provider import (
    call_item,
    completed,
    delta_frame,
    reasoning_item,
    text_item,
)

from mica_agent import cli
from mica_agent.app import MicaApp, default_provider_factory
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import RunCheckpointKind
from mica_agent.config_contracts import ConfigError
from mica_agent.context_builder import ContextBuilder
from mica_agent.continuation_contracts import ProviderApi
from mica_agent.events import (
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    TextDelta,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_contracts import SessionSelection
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import ToolResult, ToolResultStatus, ToolSpec
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace

MODEL_IDS = dict(zip(APIS, ("chat", "responses", "messages"), strict=True))
ENDPOINTS = dict(zip(APIS, ("chat/completions", "responses", "messages"), strict=True))


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def network(monkeypatch):
    """保留默认选型与真实 HTTP 客户端，只替换传输边界。"""
    responses = deque()
    requests = []
    transports = []
    client = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        assert responses, "不允许未计划的请求或自动剥离续接重试"
        response = responses.popleft()
        return await response(request) if callable(response) else response

    def create_client(*args, **kwargs):
        transport = RecordingTransport(handler)
        transports.append(transport)
        kwargs["transport"] = transport
        return client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    yield responses, requests
    assert not responses
    assert all(t.aclose_count == 1 for t in transports)
    assert all(r.is_closed for t in transports for r in t.responses)


def configuration(tmp_path, *, effort="high", mapping=None, channel="deepseek"):
    config = make_service(tmp_path)
    root = config.store.data_root
    directory = json.loads((root / "models.json").read_text())
    provider = directory["providers"]["local"]
    provider["api"] = "openai-completions"
    provider["channel"] = channel
    provider["models"] = [
        {
            "id": model,
            "api": api,
            "reasoning": True,
            **({} if mapping is None else {"thinkingLevelMap": mapping}),
        }
        for api, model in MODEL_IDS.items()
    ]
    (root / "models.json").write_text(json.dumps(directory))
    (root / "config.json").write_text(
        json.dumps(
            {
                "defaultModel": {"provider": "local", "model": "chat"},
                "reasoningEffort": effort,
                "permissionMode": "solo",
            }
        )
    )
    return config


def assembled_app(config, api, *, service=None, session_id=None, executor=None):
    snapshot = config.load()
    selection = SessionSelection("local", MODEL_IDS[api])
    connection = config.resolve(snapshot, explicit=selection).connection
    if service is None:
        service = SessionService(
            JsonlSessionStore(
                resolve_workspace(config.store.workspace), config.store.data_root
            )
        )
    if session_id is None:
        session_id = service.create_session(selection=selection).meta.session_id
    app = MicaApp(
        None,
        None,
        session_service=service,
        session_id=session_id,
        context_builder=ContextBuilder(
            data_root=config.store.data_root, workspace=service.store.workspace
        ),
        system_prompt="Test system instructions",
        config_service=config,
        connection=connection,
        startup_settings=snapshot.settings,
        provider_factory=default_provider_factory,
        tool_executor=executor,
    )
    return app, service


def stream(api, *, answer="answer", calls=(), private=True, channel="deepseek"):
    """复用专项协议样本；每次只构造一个真实模型步骤。"""
    if api == "openai-completions":
        content = {}
        if answer:
            content["content"] = answer
        if private:
            content["reasoning_content" if channel == "deepseek" else "reasoning"] = (
                "saved thought"
            )
        if calls:
            content["tool_calls"] = [
                {
                    "index": i,
                    "id": call,
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"x"}'},
                }
                for i, call in enumerate(calls)
            ]
        return sse_response(
            [
                sse_event(
                    json.dumps({"choices": [{"delta": content, "finish_reason": None}]})
                ),
                sse_event(
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {},
                                    "finish_reason": "tool_calls" if calls else "stop",
                                }
                            ]
                        }
                    )
                ),
                sse_event("[DONE]"),
            ]
        )
    if api == "openai-responses":
        items = [reasoning_item(encrypted="saved-opaque")] if private else []
        if answer:
            items.append(text_item(answer, phase="final_answer"))
        items.extend(
            call_item(call_id=call, item_id=f"fc-{i}") for i, call in enumerate(calls)
        )
        return sse_response([completed(items)])
    blocks = [thinking("saved thought", signature="saved-signature")] if private else []
    if answer:
        blocks.append(text(answer))
    blocks.extend(
        {"type": "tool_use", "id": call, "name": "lookup", "input": {"query": "x"}}
        for call in calls
    )
    return sse_response(sample(blocks, reason="tool_use" if calls else "end_turn"))


def executor_for(config, executed, *, on_call=None):
    registry = ToolRegistry()
    fact = config.store.workspace / "fact.txt"
    fact.write_text("lookup fact")

    async def lookup(request, cancellation):
        executed.append(request.call_id)
        if on_call is not None:
            on_call()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, fact.read_text()
        )

    registry.register(
        ToolSpec(
            "lookup",
            "Lookup a fact",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        lookup,
    )
    return ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.SOLO),
        workspace=resolve_workspace(config.store.workspace),
    )


async def collect(app, prompt="question", **kwargs):
    return [event async for event in app.run(prompt, **kwargs)]


def payload(request):
    return json.loads(request.content)


def assert_replay(body, api, count):
    encoded = json.dumps(body)
    marker = {
        "openai-completions": "saved thought",
        "openai-responses": "saved-opaque",
        "anthropic-messages": "saved-signature",
    }[api]
    assert encoded.count(marker) == count
    for other in set(APIS) - {api}:
        other_marker = {
            "openai-completions": "reasoning_content",
            "openai-responses": "saved-opaque",
            "anthropic-messages": "saved-signature",
        }[other]
        assert other_marker not in encoded


def assert_tool_fact(body, api, call_id):
    if api == "openai-completions":
        calls = [
            c
            for m in body["messages"]
            for c in m.get("tool_calls", [])
            if c["id"] == call_id
        ]
        results = [m for m in body["messages"] if m.get("tool_call_id") == call_id]
    elif api == "openai-responses":
        calls = [
            i
            for i in body["input"]
            if i.get("type") == "function_call" and i["call_id"] == call_id
        ]
        results = [
            i
            for i in body["input"]
            if i.get("type") == "function_call_output" and i["call_id"] == call_id
        ]
    else:
        blocks = [
            b
            for m in body["messages"]
            if isinstance(m["content"], list)
            for b in m["content"]
        ]
        calls = [
            b for b in blocks if b.get("type") == "tool_use" and b["id"] == call_id
        ]
        results = [
            b
            for b in blocks
            if b.get("type") == "tool_result" and b["tool_use_id"] == call_id
        ]
    assert len(calls) == len(results) == 1
    assert "lookup fact" in json.dumps(results)


@pytest.mark.parametrize("api", APIS)
def test_tool_round_trip_persists_only_current_continuations_and_fresh_snapshot(
    tmp_path, monkeypatch, network, api
):
    config = configuration(tmp_path)
    executed = []
    app, service = assembled_app(config, api, executor=executor_for(config, executed))
    responses, requests = network
    responses.extend(
        [
            stream(api, answer="", calls=("call-one",)),
            stream(api, private=False),
            stream(api),
        ]
    )
    snapshots = []
    original = service.model_context_snapshot

    def read_snapshot(session_id):
        snapshots.append(session_id)
        return original(session_id)

    monkeypatch.setattr(service, "model_context_snapshot", read_snapshot)

    async def scenario():
        first = await collect(app)
        assert isinstance(first[-1], RunCompleted)
        stored = service.load_session(app.session_id)
        assert len(stored.turns) == 1
        turn = stored.turns[0]
        assert len(turn.continuations) == 2
        assert turn.continuations[0].api is ProviderApi(api)
        assert turn.continuations[0].provider_id == "local"
        assert turn.continuations[0].model_id == MODEL_IDS[api]
        assert (
            turn.continuations[1] is None
            if api == "openai-completions"
            else turn.continuations[1] is not None
        )
        assert_replay(payload(requests[1]), api, 1)
        assert_tool_fact(payload(requests[1]), api, "call-one")
        await app.aclose()
        # 新 App 从磁盘重新读入，不借用启动缓存或旧 Provider。
        restored, _ = assembled_app(
            config,
            api,
            service=service,
            session_id=app.session_id,
            executor=executor_for(config, executed),
        )
        try:
            second = await collect(restored, "next")
            assert isinstance(second[-1], RunCompleted)
            assert_replay(payload(requests[2]), api, 1)
            assert_tool_fact(payload(requests[2]), api, "call-one")
            turns = service.load_session(app.session_id).turns
            assert [len(t.continuations) for t in turns] == [2, 1]
            assert turns[0] == turn
        finally:
            await restored.aclose()

    asyncio.run(scenario())
    assert executed == ["call-one"]
    assert all(payload(request)["tools"] for request in requests)
    assert snapshots == [app.session_id, app.session_id]


@pytest.mark.parametrize("source,target", list(permutations(APIS, 2)))
def test_all_interface_switch_directions_preserve_facts_filter_private_and_switch_back(
    tmp_path, network, source, target
):
    config = configuration(tmp_path)
    executed = []
    app, service = assembled_app(
        config, source, executor=executor_for(config, executed)
    )
    responses, requests = network
    responses.extend(
        [
            stream(source, answer="", calls=("call-one",)),
            stream(source),
            stream(target),
            stream(source),
        ]
    )

    async def scenario():
        try:
            assert isinstance((await collect(app))[-1], RunCompleted)
            path = service.store.path_for_session(app.session_id)
            entries = service.load_session(app.session_id).entries
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[target]))
            ).switched
            assert service.load_session(app.session_id).entries == entries
            assert len(requests) == 2 and executed == ["call-one"]
            assert isinstance((await collect(app, "cross interface"))[-1], RunCompleted)
            body = payload(requests[2])
            assert_replay(body, target, 0)
            assert_tool_fact(body, target, "call-one")
            assert "answer" in json.dumps(body)
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[source]))
            ).switched
            assert isinstance((await collect(app, "back"))[-1], RunCompleted)
            assert_replay(payload(requests[3]), source, 2)
            assert_tool_fact(payload(requests[3]), source, "call-one")
            assert (
                sum(
                    json.loads(line)["record_type"] == "turn"
                    for line in path.read_text().splitlines()
                )
                == 3
            )
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert executed == ["call-one"]


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("mode", ["new", "continue", "resume"])
def test_cli_default_factory_uses_saved_interface_and_replays_on_restore(
    tmp_path, network, api, mode
):
    from test_cli_startup import InputScript, Recorder

    config = configuration(tmp_path)
    root = config.store.data_root
    settings = json.loads((root / "config.json").read_text())
    settings["defaultModel"]["model"] = MODEL_IDS[api]
    (root / "config.json").write_text(json.dumps(settings))
    # 保存的 provider 接口继承及 model 覆盖都由实际入口选择。
    directory = json.loads((root / "models.json").read_text())
    provider = directory["providers"]["local"]
    provider["api"] = api
    next(m for m in provider["models"] if m["id"] == MODEL_IDS[api]).pop("api")
    (root / "models.json").write_text(json.dumps(directory))
    service = SessionService(
        JsonlSessionStore(resolve_workspace(config.store.workspace), root)
    )
    responses, requests = network
    responses.append(stream(api))
    out, err = Recorder(), Recorder()

    async def scenario():
        common = {
            "workspace": service.store.workspace,
            "data_root": root,
            "session_service": service,
            "write_out": out,
            "write_err": err,
        }
        assert await cli.run_interactive(read_line=InputScript("first"), **common) == 0
        if mode != "new":
            # 当前默认故意不同，恢复必须使用保存选择。
            settings["defaultModel"]["model"] = next(
                m for m in MODEL_IDS.values() if m != MODEL_IDS[api]
            )
            (root / "config.json").write_text(json.dumps(settings))
            responses.append(stream(api))
            inputs = ("1", "again") if mode == "resume" else ("again",)
            assert (
                await cli.run_interactive(
                    session_mode=mode, read_line=InputScript(*inputs), **common
                )
                == 0
            )
            assert_replay(payload(requests[-1]), api, 1)

    asyncio.run(scenario())
    assert all(
        str(r.url) == "https://example.test/v1/" + ENDPOINTS[api] for r in requests
    )
    assert all(payload(r)["model"] == MODEL_IDS[api] for r in requests)
    assert all(
        payload(r).get("max_output_tokens", payload(r).get("max_tokens")) == 4096
        for r in requests
    )
    assert "TEST_ONLY_SECRET" not in out.text + err.text
    assert service.last_active_session().meta.selection == SessionSelection(
        "local", MODEL_IDS[api]
    )
    for sent in requests:
        body = payload(sent)
        if api == "anthropic-messages":
            assert body["output_config"] == {"effort": "high"}
        elif api == "openai-responses":
            assert body["reasoning"]["effort"] == "high"
        else:
            assert body["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "channel",
    ["deepseek", "openrouter", "custom", "opencode-go", "chatgpt-subscription"],
)
@pytest.mark.parametrize(
    "with_tools", [False, True], ids=["without-tools", "with-tools"]
)
def test_explicit_completions_channel_controls_persisted_replay(
    tmp_path, network, channel, with_tools
):
    config = configuration(tmp_path, channel=channel)
    executed = []
    app, service = assembled_app(
        config,
        APIS[0],
        executor=executor_for(config, executed) if with_tools else None,
    )
    responses, requests = network
    responses.extend(
        [stream(APIS[0], channel=channel), stream(APIS[0], channel=channel)]
    )

    async def scenario():
        try:
            assert isinstance((await collect(app))[-1], RunCompleted)
            assert isinstance((await collect(app, "next"))[-1], RunCompleted)
        finally:
            await app.aclose()

    asyncio.run(scenario())
    record = service.load_session(app.session_id).turns[0].continuations[0]
    enabled = channel in {"deepseek", "openrouter"}
    assert (record is not None) == enabled
    encoded = json.dumps(payload(requests[1]))
    # DeepSeek 仅在带工具定义的请求中回传；记录保存本身不依赖 tools。
    replayed = enabled and (channel != "deepseek" or with_tools)
    assert encoded.count("saved thought") == (1 if replayed else 0)
    assert all(
        bool(payload(request).get("tools")) == with_tools for request in requests
    )
    assert executed == []
    if enabled:
        assert record.items[0].reasoning_content == (
            "saved thought" if channel == "deepseek" else None
        )
        assert record.items[0].reasoning == (
            "saved thought" if channel == "openrouter" else None
        )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "explicit", [False, True], ids=["defaults", "explicit-minimal"]
)
def test_startup_switch_and_prepare_share_options_without_rewriting_preferences(
    tmp_path, network, api, explicit
):
    mapping = {"minimal": "low"} if explicit else None
    config = configuration(tmp_path, effort="minimal", mapping=mapping)
    responses, requests = network
    responses.append(stream(api))
    app, _ = assembled_app(config, APIS[0])
    before = {p: p.read_bytes() for p in config.store.data_root.glob("*.json")}
    expected = (
        ReasoningEffort.OFF
        if api == APIS[2] and not explicit
        else ReasoningEffort.MINIMAL
    )

    async def scenario():
        try:
            assert "Reasoning: minimal" in app.connection_summary
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[api]))
            ).switched
            assert (
                f"Reasoning: {expected.value} (preference minimal"
                in app.connection_summary
            )
            assert (app.reasoning_notice is not None) == (
                expected is ReasoningEffort.OFF
            )
            binding = app.prepare_run()
            assert binding.reasoning_effort is expected
            assert binding.api is ProviderApi(api) and binding.provider_id == "local"
            assert binding.max_tokens == 4096
            assert isinstance((await collect(app, binding=binding))[-1], RunCompleted)
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert all(p.read_bytes() == data for p, data in before.items())
    body = payload(requests[0])
    target = "low" if explicit else "minimal"
    if api == APIS[2]:
        assert body["thinking"]["type"] == (
            "disabled" if expected is ReasoningEffort.OFF else "adaptive"
        )
        assert body.get("output_config") == (
            None if expected is ReasoningEffort.OFF else {"effort": target}
        )
    elif api == APIS[1]:
        assert body["reasoning"]["effort"] == target
    else:
        assert body["reasoning_effort"] == target


@pytest.mark.parametrize("change", ["api", "channel", "url", "key"])
def test_between_turn_source_drift_rejects_without_request_or_commit(
    tmp_path, network, change
):
    config = configuration(tmp_path)
    app, service = assembled_app(config, APIS[0])
    root = config.store.data_root
    path = service.store.path_for_session(app.session_id)
    original = path.read_bytes()
    if change == "key":
        auth = json.loads((root / "auth.json").read_text())
        auth["providers"]["local"]["apiKey"] = "CHANGED_TEST_ONLY_KEY"
        (root / "auth.json").write_text(json.dumps(auth))
    else:
        directory = json.loads((root / "models.json").read_text())
        provider = directory["providers"]["local"]
        if change == "api":
            provider["models"][0]["api"] = APIS[1]
        else:
            provider["channel" if change == "channel" else "baseUrl"] = (
                "openrouter"
                if change == "channel"
                else "https://changed.example.test/v1"
            )
        (root / "models.json").write_text(json.dumps(directory))
    before = app.connection_summary

    async def scenario():
        try:
            with pytest.raises(
                ConfigError, match="connection changed on disk"
            ) as caught:
                await collect(app)
            assert (
                str(caught.value)
                == "connection changed on disk; restart before submitting"
            )
            assert not app.is_busy
            assert app.connection_summary == before
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert network[1] == [] and path.read_bytes() == original


@pytest.mark.parametrize("change", ["capabilities", "source"])
def test_tool_round_trip_freezes_source_capabilities_and_permission_until_next_turn(
    tmp_path, network, change
):
    config = configuration(tmp_path)
    root = config.store.data_root

    def change_on_tool():
        directory = json.loads((root / "models.json").read_text())
        model = directory["providers"]["local"]["models"][0]
        model.update(maxTokens=222, thinkingLevelMap={"low": "vendor-low"})
        if change == "source":
            model["api"] = APIS[2]
            directory["providers"]["local"]["channel"] = "custom"
        (root / "models.json").write_text(json.dumps(directory))
        (root / "config.json").write_text(
            '{"reasoningEffort":"low","permissionMode":"default"}'
        )

    executed = []
    app, service = assembled_app(
        config, APIS[0], executor=executor_for(config, executed, on_call=change_on_tool)
    )
    responses, requests = network
    responses.extend([stream(APIS[0], answer="", calls=("call-one",)), stream(APIS[0])])

    async def scenario():
        try:
            binding = app.prepare_run()
            assert isinstance((await collect(app, binding=binding))[-1], RunCompleted)
            assert binding.settings.permission_mode is PermissionMode.SOLO
            assert binding.api is ProviderApi.OPENAI_COMPLETIONS
            assert all(payload(r)["max_tokens"] == 4096 for r in requests)
            assert all(payload(r)["reasoning_effort"] == "high" for r in requests)
            assert_replay(payload(requests[1]), APIS[0], 1)
            before = service.store.path_for_session(app.session_id).read_bytes()
            if change == "source":
                with pytest.raises(ConfigError, match="connection changed on disk"):
                    app.prepare_run()
                assert (
                    service.store.path_for_session(app.session_id).read_bytes()
                    == before
                )
            else:
                next_binding = app.prepare_run()
                assert next_binding.max_tokens == 222
                assert next_binding.reasoning_effort is ReasoningEffort.LOW
                assert next_binding.settings.permission_mode is PermissionMode.DEFAULT
                assert next_binding.thinking_level_map == {
                    ReasoningEffort.LOW: "vendor-low"
                }
                app.release_run(next_binding)
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert executed == ["call-one"] and len(requests) == 2


@pytest.mark.parametrize("api", APIS)
def test_pure_reasoning_empty_assistant_slot_survives_round_trip(
    tmp_path, network, api
):
    config = configuration(tmp_path)
    executed = []
    app, service = assembled_app(config, api, executor=executor_for(config, executed))
    responses, requests = network
    responses.extend([stream(api, answer=""), stream(api)])

    async def scenario():
        try:
            assert isinstance((await collect(app))[-1], RunCompleted)
            turn = service.load_session(app.session_id).turns[0]
            assert turn.assistant_message.content == "" and len(turn.continuations) == 1
            assert turn.continuations[0] is not None
            assert isinstance((await collect(app, "next"))[-1], RunCompleted)
            assert_replay(payload(requests[1]), api, 1)
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert all(payload(request)["tools"] for request in requests)
    assert executed == []


def unfinished_stream(api):
    if api == APIS[0]:
        return sse_response(
            [
                sse_event(
                    '{"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}'
                )
            ]
        )
    if api == APIS[1]:
        return sse_response([delta_frame("output_text", "partial")])
    return sse_response(
        [start(), block_start(0, text("")), delta(0, "text_delta", text="partial")]
    )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("ending", ["malformed", "cancel", "partial-batch"])
def test_interrupted_tools_restore_completed_records_without_replaying_or_forging_results(
    tmp_path, monkeypatch, network, api, ending
):
    from mica_agent.checkpoint_contracts import RunCheckpointToolStatus
    from mica_agent.events import ToolFinished

    config = configuration(tmp_path)
    executed = []
    app, service = assembled_app(config, api, executor=executor_for(config, executed))
    token = CancellationToken()
    responses, requests = network
    calls = ("call-one", "call-two") if ending == "partial-batch" else ("call-one",)
    responses.append(stream(api, answer="", calls=calls))
    if ending != "partial-batch":
        responses.append(unfinished_stream(api))
    committed = []
    commit = service.commit_checkpoint

    def save(session_id, checkpoint):
        committed.append(checkpoint)
        return commit(session_id, checkpoint)

    monkeypatch.setattr(service, "commit_checkpoint", save)

    async def scenario():
        try:
            events = []
            async for event in app.run("first", cancellation=token):
                events.append(event)
                if (
                    ending == "partial-batch"
                    and isinstance(event, ToolFinished)
                    or ending == "cancel"
                    and isinstance(event, TextDelta)
                ):
                    token.cancel()
                if isinstance(event, (RunFailed, RunCancelled)):
                    assert len(committed) == 1
                    assert service.load_session(app.session_id).checkpoints == tuple(
                        committed
                    )
            assert isinstance(
                events[-1], RunFailed if ending == "malformed" else RunCancelled
            )
            assert not any(isinstance(e, RunCompleted) for e in events)
            checkpoint = service.load_session(app.session_id).checkpoints[0]
            assert checkpoint.kind is (
                RunCheckpointKind.RECOVERABLE_FAILURE
                if ending == "malformed"
                else RunCheckpointKind.CANCELLED
            )
            assert len(checkpoint.progress) == 1
            assert checkpoint.progress[0].continuation is not None
            assert (
                checkpoint.progress[0].tools[0].status
                is RunCheckpointToolStatus.SUCCESS
            )
            if ending == "partial-batch":
                assert (
                    checkpoint.progress[0].tools[1].status
                    is RunCheckpointToolStatus.NOT_STARTED
                )
                assert checkpoint.progress[0].tools[1].result is None
            else:
                assert checkpoint.partial_assistant_message.content == "partial"
            await app.aclose()
            responses.append(stream(api))
            restored, _ = assembled_app(
                config,
                api,
                service=service,
                session_id=app.session_id,
                executor=executor_for(config, executed),
            )
            try:
                assert isinstance((await collect(restored, "resume"))[-1], RunCompleted)
                body = payload(requests[-1])
                assert_replay(body, api, 0 if ending == "partial-batch" else 1)
                if ending == "partial-batch":
                    assert "not_started" in json.dumps(body)
                else:
                    assert_tool_fact(body, api, "call-one")
                    assert "partial assistant output" in json.dumps(body)
                assert "previous run is incomplete" in json.dumps(body)
                assert len(committed) == 1
            finally:
                await restored.aclose()
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert executed == ["call-one"]


@pytest.mark.parametrize("checkpoint", [False, True])
def test_atomic_storage_failure_keeps_bytes_and_emits_only_one_storage_terminal(
    tmp_path, monkeypatch, network, checkpoint
):
    from mica_agent import jsonl_store

    config = configuration(tmp_path)
    app, service = assembled_app(config, APIS[2])
    path = service.store.path_for_session(app.session_id)
    before = path.read_bytes()
    responses, _ = network
    responses.append(unfinished_stream(APIS[2]) if checkpoint else stream(APIS[2]))
    writes = []

    def fail_replace(source, target):
        writes.append(target)
        raise OSError("TEST_ONLY_SECRET private-storage-error")

    monkeypatch.setattr(jsonl_store.os, "replace", fail_replace)

    async def scenario():
        try:
            events = await collect(app)
            assert isinstance(events[-1], RunFailed)
            assert events[-1].category is ErrorCategory.STORAGE
            assert events[-1].message == "session persistence failed"
            assert (
                sum(
                    isinstance(e, (RunFailed, RunCompleted, RunCancelled))
                    for e in events
                )
                == 1
            )
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert writes == [path] and path.read_bytes() == before
    assert service.load_session(app.session_id).entries == ()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("target_kind", ["provider", "model"])
def test_same_interface_other_provider_or_model_filters_and_return_rechecks(
    tmp_path, network, api, target_kind
):
    config = configuration(tmp_path)
    root = config.store.data_root
    directory = json.loads((root / "models.json").read_text())
    model = {"id": "other-model", "api": api, "reasoning": True}
    if target_kind == "model":
        directory["providers"]["local"]["models"].append(model)
        target = SessionSelection("local", "other-model")
    else:
        directory["providers"]["other"] = {
            **directory["providers"]["local"],
            "models": [{**model, "id": MODEL_IDS[api]}],
        }
        auth = json.loads((root / "auth.json").read_text())
        auth["providers"]["other"] = auth["providers"]["local"]
        (root / "auth.json").write_text(json.dumps(auth))
        target = SessionSelection("other", MODEL_IDS[api])
    (root / "models.json").write_text(json.dumps(directory))
    executed = []
    app, service = assembled_app(config, api, executor=executor_for(config, executed))
    responses, requests = network
    responses.extend([stream(api), stream(api), stream(api)])

    async def scenario():
        try:
            assert isinstance((await collect(app))[-1], RunCompleted)
            original = service.load_session(app.session_id).turns[0]
            assert (await app.switch_model(target)).switched
            assert isinstance((await collect(app, "other source"))[-1], RunCompleted)
            assert_replay(payload(requests[1]), api, 0)
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[api]))
            ).switched
            assert isinstance((await collect(app, "back"))[-1], RunCompleted)
            assert_replay(payload(requests[2]), api, 1)
            assert service.load_session(app.session_id).turns[0] == original
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert all(payload(request)["tools"] for request in requests)
    assert executed == []


def test_signed_history_server_rejection_is_reported_without_stripping_or_retry(
    tmp_path, network
):
    config = configuration(tmp_path)
    app, service = assembled_app(config, APIS[2])
    responses, requests = network
    responses.extend(
        [
            stream(APIS[2]),
            stream(APIS[1]),
            httpx.Response(
                400,
                json={
                    "error": {"message": "private signature rejection saved-signature"}
                },
            ),
        ]
    )

    async def scenario():
        try:
            assert isinstance((await collect(app))[-1], RunCompleted)
            original = service.load_session(app.session_id).turns[0]
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[APIS[1]]))
            ).switched
            assert isinstance((await collect(app, "other"))[-1], RunCompleted)
            assert (
                await app.switch_model(SessionSelection("local", MODEL_IDS[APIS[2]]))
            ).switched
            events = await collect(app, "back")
            assert (
                isinstance(events[-1], RunFailed)
                and events[-1].category is ErrorCategory.UNKNOWN
            )
            assert "signature" not in events[-1].message
            assert not any(isinstance(event, RunCompleted) for event in events)
            assert_replay(payload(requests[-1]), APIS[2], 1)
            assert "anthropic-beta" not in requests[-1].headers
            assert service.load_session(app.session_id).turns[0] == original
            assert len(service.load_session(app.session_id).turns) == 2
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert len(requests) == 3


@pytest.mark.parametrize("api", APIS)
def test_default_cli_frontend_receives_actual_saved_model_and_effective_notice(
    tmp_path, monkeypatch, network, api
):
    config = configuration(tmp_path, effort="minimal")
    root = config.store.data_root
    (root / "config.json").write_text(
        json.dumps(
            {
                "defaultModel": {"provider": "local", "model": MODEL_IDS[api]},
                "reasoningEffort": "minimal",
            }
        )
    )
    responses, requests = network
    responses.append(stream(api))
    observed = []

    async def frontend(app, **kwargs):
        try:
            observed.append(
                (
                    kwargs["session_snapshot"].model,
                    app.connection_summary,
                    app.reasoning_notice,
                )
            )
            assert isinstance((await collect(app))[-1], RunCompleted)
            return 0
        finally:
            await app.aclose()

    monkeypatch.setattr(cli, "_run_main_buffer", frontend)
    code = asyncio.run(
        cli.run_interactive(
            workspace=resolve_workspace(config.store.workspace),
            data_root=root,
            plain=False,
            write_out=lambda _: None,
            write_err=lambda _: None,
        )
    )
    assert code == 0
    label, summary, notice = observed[0]
    assert label == MODEL_IDS[api] + " (global default)"
    assert (notice is not None) == (api == APIS[2])
    assert ("Reasoning: off" in summary) == (api == APIS[2])
    assert payload(requests[0])["model"] == MODEL_IDS[api]
