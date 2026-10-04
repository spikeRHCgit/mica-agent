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
    tmp_path,
):
    service = make_service(tmp_path)
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
    (service.store.data_root / "config.json").write_text('{"reasoningEffort":"high"}')
    _, caps, budget, mapping, effort = service.for_run(selection)
    assert budget == 4096 and mapping[ReasoningEffort.HIGH] == "vendor-high"
    assert effort is ReasoningEffort.HIGH
    (service.store.data_root / "config.json").write_text('{"reasoningEffort":"low"}')
    assert service.for_run(selection)[-1] is ReasoningEffort.OFF
    assert service.for_run(SessionSelection("local", "b"))[-1] is ReasoningEffort.OFF


def test_duplicate_model_ids_all_unavailable_but_valid_neighbor_survives(tmp_path):
    service = make_service(tmp_path)
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


def test_explicit_capacity_budget_conflict_and_corrupt_file_block_run(tmp_path):
    service = make_service(tmp_path)
    path = service.store.data_root / "models.json"
    directory = json.loads(path.read_text())
    directory["providers"]["local"]["models"][0].update(contextWindow=10, maxTokens=10)
    path.write_text(json.dumps(directory))
    with pytest.raises(ConfigError):
        service.for_run(SessionSelection("local", "a"))
    (service.store.workspace / ".mica/config.json").write_bytes(b'{"secret": broken')
    with pytest.raises(ConfigError):
        service.load()
