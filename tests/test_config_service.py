from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_credential_permissions import protect

from mica_agent.config_contracts import ConfigError
from mica_agent.config_service import ConfigService
from mica_agent.config_store import ConfigStore
from mica_agent.permissions import PermissionMode
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_contracts import SessionSelection

APIS = ("openai-completions", "openai-responses", "anthropic-messages")
EXPECTED_DEFAULT_MAPS = {
    "openai-completions": {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
    "openai-responses": {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
    "anthropic-messages": {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
}


def make_service(tmp_path: Path, *, environment=None, override=None):
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    (workspace / ".mica").mkdir(parents=True, exist_ok=True)
    models = {
        "providers": {
            "local": {
                "baseUrl": "https://example.test/v1",
                "models": [{"id": "a", "reasoning": True}, {"id": "b"}],
            }
        }
    }
    (root / "models.json").write_text(json.dumps(models))
    auth = root / "auth.json"
    auth.write_text(
        json.dumps(
            {"providers": {"local": {"type": "api_key", "apiKey": "TEST_ONLY_SECRET"}}}
        )
    )
    protect(auth)
    return ConfigService(
        ConfigStore(root, workspace),
        environment={} if environment is None else environment,
        permission_override=override,
    )


@pytest.fixture(params=APIS)
def api_service(tmp_path, request):
    service = make_service(tmp_path)
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["api"] = request.param
    path.write_text(json.dumps(directory))
    return service, request.param


def test_settings_precedence_invalid_high_source_and_snapshot_safety(tmp_path):
    service = make_service(tmp_path, override=PermissionMode.DEFAULT)
    (service.store.data_root / "config.json").write_text(
        json.dumps(
            {
                "defaultModel": {"provider": "local", "model": "a"},
                "reasoningEffort": "high",
                "permissionMode": "solo",
            }
        )
    )
    (service.store.workspace / ".mica/config.json").write_text(
        json.dumps({"reasoningEffort": None, "unknown": {"apiKey": "TEST_ONLY_SECRET"}})
    )
    snapshot = service.load()
    assert snapshot.default_model == SessionSelection("local", "a")
    assert snapshot.settings.reasoning_effort is ReasoningEffort.OFF
    assert snapshot.settings.permission_mode is PermissionMode.DEFAULT
    assert snapshot.settings.sources["permissionMode"] == "CLI"
    assert snapshot.diagnostics
    assert "TEST_ONLY_SECRET" not in repr(snapshot)
    assert "example.test" not in repr(snapshot)
    with pytest.raises(TypeError):
        snapshot.settings.sources["permissionMode"] = "other"


def test_selection_precedence_and_stale_reference_never_falls_back(tmp_path):
    env = {
        "MICA_API_URL": "https://env.example.test/v1",
        "MICA_API_KEY": "ENV_ONLY_SECRET",
        "MICA_MODEL": "env-model",
    }
    service = make_service(tmp_path, environment=env)
    (service.store.data_root / "config.json").write_text(
        '{"defaultModel":{"provider":"local","model":"b"}}'
    )
    snapshot = service.load()
    assert service.resolve(snapshot).source == "environment"
    assert (
        service.resolve(snapshot, saved=SessionSelection("local", "a")).source
        == "session"
    )
    assert (
        service.resolve(
            snapshot,
            explicit=SessionSelection("local", "b"),
            saved=SessionSelection("missing", "bad"),
        ).status
        == "ready"
    )
    assert (
        service.resolve(snapshot, saved=SessionSelection("missing", "bad")).status
        == "unresolved"
    )
    temporary = service.resolve(snapshot)
    assert "ENV_ONLY_SECRET" not in repr(temporary)
    assert "env.example.test" not in repr(temporary)
    other = make_service(tmp_path, environment={})
    assert other.resolve(other.load(), saved=temporary.selection).status == "unresolved"


def test_temporary_identity_is_endpoint_based_key_independent_and_domain_separated():
    a = ConfigService.direct_connection(
        "HTTPS://Example.test:443/v1/", "KEY_A", "chat", environment=True
    )
    b = ConfigService.direct_connection(
        "https://example.test/v1/chat/completions", "KEY_B", "chat", environment=True
    )
    assert a.selection == b.selection
    assert (
        a.selection
        != ConfigService.direct_connection(
            "https://example.test/V1", "KEY_A", "chat", environment=True
        ).selection
    )
    assert (
        a.selection
        != ConfigService.direct_connection(
            "https://example.test/v1", "KEY_A", "chat"
        ).selection
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.test/v1",
        "https://example.test/?secret=x",
        "https://example.test/#secret",
        "file:///tmp/a",
        "https://example.test:bad/v1",
        "\nhttps://example.test",
    ],
)
def test_invalid_addresses_are_rejected_without_echo(url):
    with pytest.raises(ConfigError) as caught:
        ConfigService.direct_connection(url, "TEST_ONLY_SECRET", "chat")
    assert url not in str(caught.value)
    assert "TEST_ONLY_SECRET" not in str(caught.value)


def test_capabilities_unknown_map_member_retains_neighbors_and_defaults_are_bounded(
    api_service,
):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    model = directory["providers"]["local"]["models"][0]
    model.update(
        maxTokens=True,
        contextWindow=False,
        thinkingLevelMap={"high": "vendor-high", "future": "ignore"},
    )
    path.write_text(json.dumps(directory))
    snapshot = service.load()
    selection = SessionSelection("local", "a")
    caps = snapshot.providers[0].models[0].capabilities
    assert caps.context_window == 200000
    assert caps.max_tokens is None
    assert dict(caps.thinking_level_map) == {ReasoningEffort.HIGH: "vendor-high"}
    assert "unknown thinking level ignored" in snapshot.diagnostics
    (service.store.data_root / "config.json").write_text('{"reasoningEffort":"high"}')
    _, caps, budget, mapping, effort = service.for_run(selection)
    assert budget == 4096
    assert dict(mapping) == {ReasoningEffort.HIGH: "vendor-high"}
    assert effort is ReasoningEffort.HIGH
    (service.store.data_root / "config.json").write_text('{"reasoningEffort":"low"}')
    assert service.for_run(selection)[-1] is ReasoningEffort.OFF
    assert service.for_run(SessionSelection("local", "b"))[-1] is ReasoningEffort.OFF


def test_duplicate_model_ids_all_unavailable_but_valid_neighbor_survives(api_service):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"].append({"id": "a"})
    path.write_text(json.dumps(directory))
    snapshot = service.load()
    assert (
        service.resolve(snapshot, explicit=SessionSelection("local", "a")).status
        == "unresolved"
    )
    assert (
        service.resolve(snapshot, explicit=SessionSelection("local", "b")).status
        == "ready"
    )


def test_explicit_capacity_budget_conflict_and_corrupt_file_block_run(api_service):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0].update(contextWindow=10, maxTokens=10)
    path.write_text(json.dumps(directory))
    with pytest.raises(ConfigError):
        service.for_run(SessionSelection("local", "a"))
    (service.store.workspace / ".mica/config.json").write_bytes(b'{"secret": broken')
    with pytest.raises(ConfigError):
        service.load()


def test_saved_interfaces_inherit_or_override_without_rewriting_address(api_service):
    service, api = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    provider = directory["providers"]["local"]
    address = "https://example.test/custom/v1/"
    provider["baseUrl"] = address
    provider["models"] = [{"id": "inherited", "reasoning": True}] + [
        {"id": target, "api": target, "reasoning": True} for target in APIS
    ]
    path.write_text(json.dumps(directory))
    (service.store.data_root / "config.json").write_text(
        '{"reasoningEffort":"minimal"}'
    )
    snapshot = service.load()
    assert snapshot.providers[0].api == api
    assert len(snapshot.providers[0].models) == 4
    assert snapshot.diagnostics == ()
    resolutions = [
        service.resolve(snapshot, explicit=model.selection)
        for model in snapshot.providers[0].models
    ]
    for model, expected_api, resolution in zip(
        snapshot.providers[0].models, (api, *APIS), resolutions
    ):
        target = SessionSelection("local", model.selection.model_id)
        assert resolution.status == "ready" and resolution.source == "CLI"
        assert resolution.selection == target
        binding = resolution.connection
        assert binding is not None and binding.model is model
        assert model.api == expected_api
        assert model.selection == binding.selection == target
        assert binding.api_url == address
        assert binding.source == "saved directory"
        assert binding.api_key == "TEST_ONLY_SECRET"
        # 用模型覆盖后的接口计算能力，而非供应商默认接口。
        _, _, budget, mapping, effort = service.for_run(target)
        assert budget == 4096
        assert {
            key.value: value for key, value in mapping.items()
        } == EXPECTED_DEFAULT_MAPS[expected_api]
        assert effort is (
            ReasoningEffort.OFF
            if expected_api == "anthropic-messages"
            else ReasoningEffort.MINIMAL
        )
        for view in (snapshot, resolution, binding, model):
            assert "TEST_ONLY_SECRET" not in repr(view)
            assert address not in repr(view)


@pytest.mark.parametrize(
    "failure", ["disabled", "missing-key", "model-api", "provider-api"]
)
def test_unavailable_saved_interface_cannot_be_selected(api_service, failure):
    service, api = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    provider = directory["providers"]["local"]
    if failure == "disabled":
        provider["enabled"] = False
    elif failure == "missing-key":
        (service.store.data_root / "auth.json").write_text('{"providers":{}}')
    elif failure == "model-api":
        provider["models"][0]["api"] = "unknown-protocol"
    else:
        provider["api"] = "unknown-protocol"
        provider["models"][0]["api"] = api
    path.write_text(json.dumps(directory))
    snapshot = service.load()
    target = SessionSelection("local", "a")
    resolution = service.resolve(snapshot, explicit=target)
    assert resolution.status == "unresolved" and resolution.connection is None
    assert resolution.selection == target and resolution.source == "CLI"
    with pytest.raises(ConfigError, match="selected connection is unavailable"):
        service.for_run(target)
    with pytest.raises(ConfigError, match="available saved connection"):
        service.save_default_model(target, None)
    if failure == "model-api":
        assert (
            service.resolve(snapshot, explicit=SessionSelection("local", "b")).status
            == "ready"
        )
    if failure == "provider-api":
        assert snapshot.providers == ()


def test_saved_interface_selection_precedence_and_default_save(api_service):
    service, api = api_service
    config = service.store.data_root / "config.json"
    config.write_text(
        json.dumps(
            {
                "defaultModel": {"provider": "local", "model": "b"},
                "reasoningEffort": "high",
            }
        )
    )
    snapshot = service.load()
    a, b = SessionSelection("local", "a"), SessionSelection("local", "b")
    for kwargs, target, source in (
        ({}, b, "global default"),
        ({"saved": a}, a, "session"),
        ({"explicit": b, "saved": a}, b, "CLI"),
    ):
        resolution = service.resolve(snapshot, **kwargs)
        assert (resolution.status, resolution.selection, resolution.source) == (
            "ready",
            target,
            source,
        )
        assert resolution.connection.model.api == api
    for source in ("explicit", "saved"):
        missing = SessionSelection("missing", "a")
        resolution = service.resolve(snapshot, **{source: missing})
        assert resolution.status == "unresolved" and resolution.selection == missing
    service.save_default_model(a, b)
    snapshot = service.load()
    assert snapshot.default_model == a
    assert service.resolve(snapshot).status == "ready"
    assert json.loads(config.read_text())["reasoningEffort"] == "high"
    workspace_config = service.store.workspace / ".mica/config.json"
    workspace_config.write_text('{"defaultModel":{"provider":"local","model":"b"}}')
    snapshot = service.load()
    assert snapshot.default_source == "workspace default"
    assert service.resolve(snapshot).selection == b
    environment = {
        "MICA_API_URL": "https://env.example.test/v1",
        "MICA_API_KEY": "ENV_ONLY_SECRET",
        "MICA_MODEL": "env-model",
    }
    other = ConfigService(service.store, environment=environment)
    snapshot = other.load()
    temporary = other.resolve(snapshot)
    assert temporary.source == "environment" and temporary.status == "ready"
    assert temporary.connection.model.api == "openai-completions"
    assert other.resolve(snapshot, saved=a).selection == a
    assert other.resolve(snapshot, explicit=b, saved=a).selection == b
    for source in ("explicit", "saved"):
        assert (
            other.resolve(snapshot, **{source: SessionSelection("missing", "a")}).status
            == "unresolved"
        )


@pytest.mark.parametrize("empty_map", [False, True], ids=["missing-map", "empty-map"])
def test_interface_default_mapping_budget_and_all_efforts(api_service, empty_map):
    service, api = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    if empty_map:
        directory["providers"]["local"]["models"][0]["thinkingLevelMap"] = {}
    path.write_text(json.dumps(directory))
    original = path.read_bytes()
    config = service.store.data_root / "config.json"
    for preference in ReasoningEffort:
        config.write_text(json.dumps({"reasoningEffort": preference.value}))
        settings_bytes = config.read_bytes()
        settings, caps, budget, mapping, effort = service.for_run(
            SessionSelection("local", "a")
        )
        assert budget == 4096
        assert {
            key.value: value for key, value in mapping.items()
        } == EXPECTED_DEFAULT_MAPS[api]
        assert effort is (
            preference
            if preference.value in EXPECTED_DEFAULT_MAPS[api]
            else ReasoningEffort.OFF
        )
        assert caps.context_window == 200000 and caps.max_tokens is None
        assert caps.input == ("text",) and caps.reasoning
        assert dict(caps.thinking_level_map) == {}
        assert (
            caps.sources["thinkingLevelMap"]
            == caps.sources["maxTokens"]
            == "adapter default"
        )
        assert caps.sources["contextWindow"] == "program default"
        assert settings.reasoning_effort is preference
        assert settings.sources["reasoningEffort"] == "global"
        assert path.read_bytes() == original and config.read_bytes() == settings_bytes
        with pytest.raises(TypeError):
            mapping[ReasoningEffort.LOW] = "changed"
        with pytest.raises(TypeError):
            caps.thinking_level_map[ReasoningEffort.LOW] = "changed"
        with pytest.raises(TypeError):
            caps.sources["thinkingLevelMap"] = "changed"


def test_explicit_mapping_is_complete_without_default_neighbors(api_service):
    service, api = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    target = "low" if api == "anthropic-messages" else "vendor-minimal"
    raw_map = {"minimal": target, "high": "high"}
    directory["providers"]["local"]["models"][0]["thinkingLevelMap"] = raw_map
    path.write_text(json.dumps(directory))
    original = path.read_bytes()
    config = service.store.workspace / ".mica/config.json"
    for preference in ("minimal", "high", "low"):
        config.write_text(json.dumps({"reasoningEffort": preference}))
        settings_bytes = config.read_bytes()
        before = service.load().settings
        settings, caps, budget, mapping, effort = service.for_run(
            SessionSelection("local", "a")
        )
        assert budget == 4096
        assert {key.value: value for key, value in mapping.items()} == raw_map
        assert mapping == caps.thinking_level_map
        assert caps.sources["thinkingLevelMap"] == "explicit"
        assert effort is (
            ReasoningEffort(preference)
            if preference in raw_map
            else ReasoningEffort.OFF
        )
        assert settings == before and settings.reasoning_effort.value == preference
        assert settings.sources["reasoningEffort"] == "workspace"
        assert path.read_bytes() == original and config.read_bytes() == settings_bytes
        with pytest.raises(TypeError):
            mapping[ReasoningEffort.LOW] = "changed"


@pytest.mark.parametrize("reasoning", [None, False], ids=["missing", "false"])
@pytest.mark.parametrize(
    "explicit_map", [False, True], ids=["default-map", "explicit-map"]
)
def test_nonreasoning_model_preserves_preference_but_has_no_effective_map(
    api_service, reasoning, explicit_map
):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    model = directory["providers"]["local"]["models"][0]
    model.pop("reasoning")
    if reasoning is not None:
        model["reasoning"] = reasoning
    if explicit_map:
        model["thinkingLevelMap"] = {"high": "high"}
    path.write_text(json.dumps(directory))
    config = service.store.data_root / "config.json"
    config.write_text('{"reasoningEffort":"high"}')
    original = (path.read_bytes(), config.read_bytes())
    before = service.load().settings
    settings, caps, _, mapping, effort = service.for_run(SessionSelection("local", "a"))
    assert not caps.reasoning and mapping == {} and effort is ReasoningEffort.OFF
    assert dict(caps.thinking_level_map) == (
        {ReasoningEffort.HIGH: "high"} if explicit_map else {}
    )
    assert caps.sources["reasoning"] == (
        "program default" if reasoning is None else "explicit"
    )
    assert settings == before and settings.reasoning_effort is ReasoningEffort.HIGH
    assert settings.sources["reasoningEffort"] == "global"
    assert (path.read_bytes(), config.read_bytes()) == original


@pytest.mark.parametrize(
    "raw_map, warning",
    [
        ("UNTRUSTED_MAP_TARGET", "invalid thinking map; adapter default used"),
        (["UNTRUSTED_MAP_TARGET"], "invalid thinking map; adapter default used"),
        (None, "invalid thinking map; adapter default used"),
        ({"high": "", "low": "low"}, "invalid thinking map; adapter default used"),
        ({"high": "   ", "low": "low"}, "invalid thinking map; adapter default used"),
        (
            {"high": "UNTRUSTED_MAP_TARGET\n", "low": "low"},
            "invalid thinking map; adapter default used",
        ),
        ({"high": True, "low": "low"}, "invalid thinking map; adapter default used"),
        ({"high": 17, "low": "low"}, "invalid thinking map; adapter default used"),
        ({"high": None, "low": "low"}, "invalid thinking map; adapter default used"),
        ({"future": "UNTRUSTED_MAP_TARGET"}, "unknown thinking level ignored"),
    ],
    ids=[
        "string",
        "list",
        "null",
        "empty-target",
        "blank-target",
        "control-target",
        "bool-target",
        "int-target",
        "null-target",
        "unknown-only",
    ],
)
def test_malformed_mapping_uses_actual_interface_defaults_without_echo(
    api_service, raw_map, warning
):
    service, api = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0]["thinkingLevelMap"] = raw_map
    path.write_text(json.dumps(directory))
    (service.store.data_root / "config.json").write_text('{"reasoningEffort":"high"}')
    snapshot = service.load()
    assert warning in snapshot.diagnostics
    resolution = service.resolve(snapshot, explicit=SessionSelection("local", "a"))
    assert resolution.status == "ready"
    for view in (snapshot, resolution, resolution.connection):
        assert "UNTRUSTED_MAP_TARGET" not in repr(view)
        assert "TEST_ONLY_SECRET" not in repr(view)
        assert "example.test" not in repr(view)
    _, caps, budget, mapping, effort = service.for_run(resolution.selection)
    assert budget == 4096 and effort is ReasoningEffort.HIGH
    assert dict(caps.thinking_level_map) == {}
    assert caps.sources["thinkingLevelMap"] == "adapter default"
    assert {
        key.value: value for key, value in mapping.items()
    } == EXPECTED_DEFAULT_MAPS[api]


@pytest.mark.parametrize("budget", [1, 17, 300000, True, False, 0, -1, None, "17", 1.5])
def test_interface_output_budget_validation_and_sources(api_service, budget):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0]["maxTokens"] = budget
    path.write_text(json.dumps(directory))
    _, caps, effective, _, _ = service.for_run(SessionSelection("local", "a"))
    valid = type(budget) is int and budget > 0
    assert effective == (budget if valid else 4096)
    assert caps.max_tokens == (budget if valid else None)
    assert caps.sources["maxTokens"] == ("explicit" if valid else "adapter default")
    assert caps.context_window == 200000


@pytest.mark.parametrize("context", [True, False, 0, -1, None, "200000", 1.5])
def test_invalid_context_capacity_uses_bounded_default(api_service, context):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0]["contextWindow"] = context
    path.write_text(json.dumps(directory))
    _, caps, budget, _, _ = service.for_run(SessionSelection("local", "a"))
    assert caps.context_window == 200000 and budget == 4096
    assert caps.sources["contextWindow"] == "program default"


@pytest.mark.parametrize("capacity", [4095, 4096, 4097])
def test_interface_default_budget_is_checked_against_explicit_capacity(
    api_service, capacity
):
    service, _ = api_service
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0]["contextWindow"] = capacity
    path.write_text(json.dumps(directory))
    if capacity <= 4096:
        with pytest.raises(ConfigError, match="output budget conflicts"):
            service.for_run(SessionSelection("local", "a"))
    else:
        _, caps, budget, _, _ = service.for_run(SessionSelection("local", "a"))
        assert caps.context_window == capacity and budget == 4096
        assert caps.sources["contextWindow"] == "explicit"


def test_unprotected_saved_credentials_do_not_fall_back_to_workspace(api_service):
    service, _ = api_service
    auth = service.store.data_root / "auth.json"
    protect(auth, shared=True)
    workspace_config = service.store.workspace / ".mica/config.json"
    workspace_config.write_text('{"apiKey":"WORKSPACE_ONLY_SECRET"}')
    try:
        snapshot = service.load()
        assert any(
            "authentication source disabled" in text for text in snapshot.diagnostics
        )
        assert any(
            "workspace credentials are ignored" in text for text in snapshot.diagnostics
        )
        target = SessionSelection("local", "a")
        resolution = service.resolve(snapshot, explicit=target)
        assert resolution.status == "unresolved" and resolution.connection is None
        assert "TEST_ONLY_SECRET" not in repr(snapshot)
        assert "WORKSPACE_ONLY_SECRET" not in repr(snapshot)
        with pytest.raises(ConfigError, match="selected connection is unavailable"):
            service.for_run(target)
    finally:
        protect(auth)


def test_for_run_reloads_actual_model_interface_without_changing_preference(tmp_path):
    service = make_service(tmp_path)
    config = service.store.data_root / "config.json"
    config.write_text('{"reasoningEffort":"minimal"}')
    target = SessionSelection("local", "a")
    current = service.resolve(service.load(), explicit=target).connection
    assert service.for_run(target, current=current)[-1] is ReasoningEffort.MINIMAL
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0]["api"] = "anthropic-messages"
    path.write_text(json.dumps(directory))
    settings, _, budget, mapping, effort = service.for_run(target, current=current)
    assert budget == 4096 and effort is ReasoningEffort.OFF
    assert {
        key.value: value for key, value in mapping.items()
    } == EXPECTED_DEFAULT_MAPS["anthropic-messages"]
    assert settings.reasoning_effort is ReasoningEffort.MINIMAL
    assert config.read_text() == '{"reasoningEffort":"minimal"}'


@pytest.mark.parametrize("environment", [False, True], ids=["direct", "environment"])
def test_temporary_run_retains_completions_identity_and_defaults(tmp_path, environment):
    service = make_service(tmp_path)
    current = service.direct_connection(
        "https://example.test/v1/", "TEST_ONLY_SECRET", "chat", environment=environment
    )
    settings, caps, budget, mapping, effort = service.for_run(
        current.selection, current=current
    )
    assert current.model.api == "openai-completions"
    assert current.source == ("environment" if environment else "direct")
    assert current.selection.provider_id.startswith(
        "mica-env:" if environment else "mica-direct:"
    )
    assert caps is current.model.capabilities and budget == 4096
    assert mapping == {} and effort is ReasoningEffort.OFF
    assert settings.reasoning_effort is ReasoningEffort.OFF
