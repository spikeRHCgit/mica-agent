"""连接维护业务的隔离测试：真实临时存储、假 Key 和模拟目录网络。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import fields
from pathlib import Path

import httpx
import pytest
from sse_transport_helpers import RecordingTransport
from test_credential_permissions import protect

from mica_agent._connection_maintenance import (
    ConnectionMaintenance,
    ManualModel,
)
from mica_agent._credential_permissions import read_credentials
from mica_agent._file_lock import locked_file
from mica_agent.cancellation import CancellationRequested, CancellationToken
from mica_agent.config_contracts import ConfigError, CredentialProtectionError
from mica_agent.config_service import ConfigService
from mica_agent.config_store import ConfigStore
from mica_agent.credential_store import CredentialStore
from mica_agent.model_catalog import connection_defaults
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_contracts import SessionSelection

DEEPSEEK_PAYLOAD = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-chat",
            "object": "model",
            "name": "DeepSeek Chat",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "input_modalities": ["text"],
            "effort": {"supported_levels": ["low", "high"]},
        },
        {"id": "deepseek-reasoner", "object": "model"},
    ],
}
GO_PAYLOAD = {
    "object": "list",
    "data": [{"id": "go-alpha", "object": "model"}],
}
ANCHOR = {
    "name": "Anchor",
    "channel": "custom",
    "enabled": True,
    "baseUrl": "https://anchor.test/v1",
    "api": "openai-completions",
    "models": [{"id": "anchor-model", "name": "Anchor Model"}],
}
ANCHOR_AUTH = {"type": "api_key", "apiKey": "TEST_ONLY_ANCHOR_KEY"}
ANCHOR_CATALOG = "https://anchor.test/v1/models"
_UNSET = object()


def _response(payload: object, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


def _service(
    tmp_path: Path,
    *,
    models: dict | None = None,
    auth: dict | None = None,
    config: dict | None = None,
) -> tuple[ConfigService, ConnectionMaintenance, Path]:
    root = tmp_path / "data"
    root.mkdir(parents=True)
    protect(root)
    workspace = tmp_path / "workspace"
    (workspace / ".mica").mkdir(parents=True)
    if models is not None:
        (root / "models.json").write_text(json.dumps(models))
    if auth is not None:
        path = root / "auth.json"
        path.write_text(json.dumps(auth))
        protect(path)
    if config is not None:
        (root / "config.json").write_text(json.dumps(config))
    store = ConfigStore(root, workspace)
    service = ConfigService(store, environment={})
    return service, service._connection_maintenance(), root


def _seeded(
    tmp_path: Path,
    *,
    provider: dict | None = None,
    models: object = _UNSET,
    auth: object = _UNSET,
    config: object = _UNSET,
) -> tuple[ConfigService, ConnectionMaintenance, Path]:
    if models is _UNSET:
        models = {
            "providers": {"anchor": dict(provider or ANCHOR)},
            "futureRoot": {"keep": True},
        }
    if auth is _UNSET:
        auth = {
            "providers": {"anchor": dict(ANCHOR_AUTH)},
            "futureRoot": True,
        }
    if config is _UNSET:
        config = {
            "defaultModel": {"provider": "anchor", "model": "anchor-model"},
            "reasoningEffort": "high",
            "future": 1,
        }
    return _service(tmp_path, models=models, auth=auth, config=config)


def _document(root: Path, name: str) -> dict:
    return json.loads((root / f"{name}.json").read_text())


def _pending_tasks() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


def test_full_connection_maintenance_round_trip(tmp_path: Path) -> None:
    service, maintenance, root = _seeded(tmp_path)
    models_before = (root / "models.json").read_bytes()
    config_before = (root / "config.json").read_bytes()

    listed = maintenance.list_connections()
    assert [item.provider_id for item in listed] == ["anchor"]
    assert listed[0].credential == "available"

    created = maintenance.create_connection("deepseek", api_key="TEST_ONLY_OLD_KEY")
    assert created.status == "complete"
    assert created.completed == ("models", "credential")
    assert created.remaining == ()
    connection = created.connection
    assert connection is not None
    assert connection.name == "DeepSeek"
    assert connection.channel == "deepseek"
    assert connection.enabled is True
    assert connection.credential == "available"
    assert len(connection.provider_id) == 32
    assert connection.provider_id.islower()
    assert "deepseek" not in connection.provider_id
    assert created.hint is not None and "without models" in created.hint
    assert [item.name for item in fields(connection)] == [
        "provider_id",
        "name",
        "channel",
        "enabled",
        "credential",
    ]

    provider_id = connection.provider_id
    default_base, default_api = connection_defaults("deepseek")
    saved = _document(root, "models")["providers"][provider_id]
    assert saved == {
        "name": "DeepSeek",
        "channel": "deepseek",
        "enabled": True,
        "baseUrl": default_base,
        "api": default_api,
        "models": [],
    }
    assert (root / "config.json").read_bytes() == config_before

    snapshot = service.load()
    parsed = next(
        item for item in snapshot.providers if item.provider_id == provider_id
    )
    assert parsed.models == ()
    assert snapshot.default_model == SessionSelection("anchor", "anchor-model")
    assert (root / "models.json").read_bytes() != models_before
    auth_after_create = (root / "auth.json").read_bytes()

    edit = maintenance.begin_edit(provider_id)
    assert edit.provider_id == provider_id
    assert edit.custom_url is None
    assert edit.custom_api is None
    assert "TEST_ONLY_OLD_KEY" not in repr(edit)
    assert default_base not in repr(edit)
    assert str(root) not in repr(edit)

    async def scenario():
        seen: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        discovery = await maintenance.discover_catalog(
            edit, CancellationToken(), transport=transport
        )
        assert discovery.result.status == "ok"
        assert transport.aclose_count == 1
        return seen[0], transport, discovery

    request, transport, discovery = asyncio.run(scenario())
    assert str(request.url) == "https://api.deepseek.com/models"
    assert request.headers["Authorization"] == "Bearer TEST_ONLY_OLD_KEY"
    assert transport.responses[0].is_closed is True

    saved_selection = maintenance.save_catalog_selection(
        discovery, {"deepseek-chat", "deepseek-reasoner"}
    )
    assert saved_selection.status == "complete"
    assert saved_selection.completed == ("models",)
    assert (root / "auth.json").read_bytes() == auth_after_create
    assert (root / "config.json").read_bytes() == config_before
    saved_models = _document(root, "models")["providers"][provider_id]["models"]
    assert [item["id"] for item in saved_models] == [
        "deepseek-chat",
        "deepseek-reasoner",
    ]
    assert saved_models[0]["api"] == "openai-completions"
    assert saved_models[0]["contextWindow"] == 128000

    ready = service.load()
    resolution = service.resolve(
        ready, explicit=SessionSelection(provider_id, "deepseek-chat")
    )
    assert resolution.status == "ready"
    assert resolution.connection is not None
    assert resolution.connection.api_url == default_base
    assert resolution.connection.api_key == "TEST_ONLY_OLD_KEY"

    renamed = maintenance.rename(maintenance.begin_edit(provider_id), "My DeepSeek")
    assert renamed.status == "complete"
    assert renamed.connection is not None
    assert renamed.connection.provider_id == provider_id
    assert renamed.connection.name == "My DeepSeek"
    record = _document(root, "models")["providers"][provider_id]
    assert record["name"] == "My DeepSeek"
    assert record["models"] == saved_models
    assert (root / "auth.json").read_bytes() == auth_after_create

    disabled = maintenance.set_enabled(maintenance.begin_edit(provider_id), False)
    assert disabled.status == "complete"
    assert disabled.connection is not None
    assert disabled.connection.enabled is False
    snapshot = service.load()
    assert (
        service.resolve(
            snapshot, explicit=SessionSelection(provider_id, "deepseek-chat")
        ).status
        == "unresolved"
    )
    assert (root / "auth.json").read_bytes() == auth_after_create

    enabled = maintenance.set_enabled(maintenance.begin_edit(provider_id), True)
    assert enabled.status == "complete"
    snapshot = service.load()
    assert (
        service.resolve(
            snapshot, explicit=SessionSelection(provider_id, "deepseek-chat")
        ).status
        == "ready"
    )

    models_before_key_change = (root / "models.json").read_bytes()
    changed = maintenance.change_api_key(
        maintenance.begin_edit(provider_id), "TEST_ONLY_NEW_KEY"
    )
    assert changed.status == "complete"
    assert changed.connection is not None
    assert changed.connection.credential == "available"
    assert (root / "models.json").read_bytes() == models_before_key_change
    auth_text = (root / "auth.json").read_text()
    assert "TEST_ONLY_NEW_KEY" in auth_text
    assert "TEST_ONLY_OLD_KEY" not in auth_text

    duplicate = maintenance.create_connection(
        "deepseek", api_key="TEST_ONLY_OTHER_KEY", name="My DeepSeek"
    )
    assert duplicate.status == "complete"
    assert duplicate.connection is not None
    assert duplicate.connection.provider_id != provider_id
    assert duplicate.connection.name == "My DeepSeek"
    names = {item.name for item in maintenance.list_connections()}
    assert names == {"Anchor", "My DeepSeek"}

    custom = maintenance.create_connection(
        "custom",
        api_key="TEST_ONLY_CUSTOM_KEY",
        name="Local",
        base_url="https://custom.test/v1",
        api="openai-completions",
    )
    assert custom.status == "complete"
    assert custom.connection is not None
    custom_id = custom.connection.provider_id
    custom_edit = maintenance.begin_edit(custom_id)
    assert custom_edit.custom_url == "https://custom.test/v1"
    assert custom_edit.custom_api == "openai-completions"
    moved = maintenance.update_custom_address(
        custom_edit, base_url="https://custom2.test/v1", api="openai-responses"
    )
    assert moved.status == "complete"
    custom_record = _document(root, "models")["providers"][custom_id]
    assert custom_record["baseUrl"] == "https://custom2.test/v1"
    assert custom_record["api"] == "openai-responses"

    manual = maintenance.save_manual_model(
        maintenance.begin_edit(custom_id),
        ManualModel(
            "manual-model",
            "anthropic-messages",
            name="Manual Model",
            context_window=4096,
        ),
    )
    assert manual.status == "complete"
    manual_record = _document(root, "models")["providers"][custom_id]
    assert manual_record["models"] == [
        {
            "id": "manual-model",
            "api": "anthropic-messages",
            "name": "Manual Model",
            "contextWindow": 4096,
        }
    ]
    parsed_snapshot = service.load()
    custom_parsed = next(
        item for item in parsed_snapshot.providers if item.provider_id == custom_id
    )
    manual_parsed = custom_parsed.models[0]
    assert manual_parsed.capabilities.context_window == 4096
    assert manual_parsed.capabilities.max_tokens is None
    assert manual_parsed.capabilities.sources["maxTokens"] == "adapter default"
    assert manual_parsed.capabilities.sources["input"] == "program default"

    unconfirmed = maintenance.delete(maintenance.begin_edit(custom_id), confirmed=False)
    assert unconfirmed.status == "not_confirmed"
    assert unconfirmed.completed == ()

    removed = maintenance.delete(maintenance.begin_edit(custom_id), confirmed=True)
    assert removed.status == "complete"
    assert removed.completed == ("models", "credential")
    remaining = _document(root, "models")["providers"]
    assert custom_id not in remaining
    assert provider_id in remaining
    assert "anchor" in remaining
    assert _document(root, "auth")["providers"].get(custom_id) is None

    duplicate_removed = maintenance.delete(
        maintenance.begin_edit(duplicate.connection.provider_id), confirmed=True
    )
    assert duplicate_removed.status == "complete"

    final = maintenance.delete(maintenance.begin_edit(provider_id), confirmed=True)
    assert final.status == "complete"
    models_final = _document(root, "models")
    assert models_final["providers"] == {"anchor": ANCHOR}
    assert models_final["futureRoot"] == {"keep": True}
    assert _document(root, "auth") == {
        "providers": {"anchor": ANCHOR_AUTH},
        "futureRoot": True,
    }
    assert (root / "config.json").read_bytes() == config_before
    final_snapshot = service.load()
    assert final_snapshot.default_model == SessionSelection("anchor", "anchor-model")


def test_new_connection_does_not_reuse_a_deleted_identifier(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _service(tmp_path)
    first = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY_A")
    assert first.connection is not None
    first_id = first.connection.provider_id
    removed = maintenance.delete(maintenance.begin_edit(first_id), confirmed=True)
    assert removed.status == "complete"
    second = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY_B")
    assert second.connection is not None
    assert second.connection.provider_id != first_id
    assert first_id not in _document(root, "models")["providers"]


def test_builtin_defaults_and_custom_field_requirements(tmp_path: Path) -> None:
    _, maintenance, root = _service(tmp_path)

    for channel, label in (
        ("deepseek", "DeepSeek"),
        ("openrouter", "OpenRouter"),
        ("opencode-go", "OpenCode Go"),
    ):
        outcome = maintenance.create_connection(channel, api_key="TEST_ONLY_KEY")
        assert outcome.status == "complete"
        assert outcome.connection is not None
        assert outcome.connection.name == label
        saved = _document(root, "models")["providers"][outcome.connection.provider_id]
        base_url, api = connection_defaults(channel)
        assert saved["baseUrl"] == base_url
        assert saved["api"] == api

    with pytest.raises(ConfigError):
        maintenance.create_connection("custom", api_key="TEST_ONLY_KEY")
    with pytest.raises(ConfigError):
        maintenance.create_connection("custom", api_key="TEST_ONLY_KEY", name="X")
    with pytest.raises(ConfigError):
        maintenance.create_connection(
            "custom",
            api_key="TEST_ONLY_KEY",
            name="X",
            base_url="https://custom.test/v1",
        )
    with pytest.raises(ConfigError):
        maintenance.create_connection(
            "deepseek", api_key="TEST_ONLY_KEY", base_url="https://evil.test"
        )
    with pytest.raises(ConfigError):
        maintenance.create_connection("chatgpt-subscription", api_key="TEST_ONLY_KEY")
    with pytest.raises(ConfigError):
        maintenance.create_connection(
            "custom",
            api_key="TEST_ONLY_KEY",
            name="X",
            base_url="https://custom.test/v1",
            api="bogus",
        )


def test_invalid_create_input_does_not_write_any_file(tmp_path: Path) -> None:
    _, maintenance, root = _service(tmp_path)
    with pytest.raises(ConfigError) as caught:
        maintenance.create_connection("deepseek", api_key="TEST_ONLY_SECRET\nKEY")
    assert "TEST_ONLY_SECRET" not in str(caught.value)
    assert not (root / "auth.json").exists()
    assert not (root / "models.json").exists()

    with pytest.raises(ConfigError) as caught:
        maintenance.create_connection(
            "custom",
            api_key="TEST_ONLY_KEY",
            name="X",
            base_url="https://user:TEST_ONLY_PASSWORD@custom.test/v1",
            api="openai-completions",
        )
    assert "TEST_ONLY_PASSWORD" not in str(caught.value)
    assert not (root / "auth.json").exists()
    assert not (root / "models.json").exists()


def test_credential_failure_on_create_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    _, maintenance, root = _service(tmp_path)
    real_replace = os.replace

    def fail_auth(source, destination):
        if Path(destination).name == "auth.json":
            raise OSError("TEST_ONLY_FAILURE")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_auth)
    outcome = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY")
    assert outcome.status == "failed"
    assert outcome.completed == ()
    assert outcome.remaining == ("credential", "models")
    assert outcome.connection is None
    assert "TEST_ONLY_KEY" not in (outcome.hint or "")
    assert not (root / "auth.json").exists()
    assert not (root / "models.json").exists()


def test_models_failure_after_credential_reports_partial_commit(
    tmp_path: Path, monkeypatch
) -> None:
    _, maintenance, root = _service(tmp_path)
    real_replace = os.replace

    def fail_models(source, destination):
        if Path(destination).name == "models.json":
            raise OSError("TEST_ONLY_FAILURE")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_models)
    outcome = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY")
    assert outcome.status == "partial"
    assert outcome.completed == ("credential",)
    assert outcome.remaining == ("models",)
    assert outcome.connection is None
    assert "TEST_ONLY_KEY" not in (outcome.hint or "")
    auth = json.loads(read_credentials(root / "auth.json"))
    assert list(auth["providers"].values()) == [
        {"type": "api_key", "apiKey": "TEST_ONLY_KEY"}
    ]
    assert not (root / "models.json").exists()


def test_unconfirmed_protection_on_create_preserves_meaning(
    tmp_path: Path, monkeypatch
) -> None:
    import mica_agent.credential_store as credential_store_module

    _, maintenance, root = _service(tmp_path)
    auth_path = root / "auth.json"
    real_read = credential_store_module.read_credentials
    seen: list[Path] = []

    def failing_read(target: Path) -> bytes:
        target = Path(target)
        if target == auth_path:
            seen.append(target)
            if len(seen) == 2:
                raise CredentialProtectionError(
                    "credentials were replaced but protection could not be confirmed"
                )
        return real_read(target)

    monkeypatch.setattr(credential_store_module, "read_credentials", failing_read)
    outcome = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY")
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ()
    assert outcome.remaining == ("credential", "models")
    assert outcome.connection is None
    assert "replaced" in (outcome.hint or "")
    assert "TEST_ONLY_KEY" not in (outcome.hint or "")
    assert json.loads(real_read(auth_path))["providers"]
    assert not (root / "models.json").exists()


def test_create_pre_write_protection_failure_does_not_claim_a_commit(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _service(tmp_path)
    try:
        protect(root, shared=True, rights="CreateFiles")
        outcome = maintenance.create_connection("deepseek", api_key="TEST_ONLY_KEY")
    finally:
        protect(root)
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ()
    assert outcome.remaining == ("credential", "models")
    assert outcome.connection is None
    assert outcome.hint
    assert "TEST_ONLY_KEY" not in (outcome.hint or "")
    assert not (root / "auth.json").exists()
    assert not (root / "models.json").exists()


def test_rename_and_toggle_keep_models_credentials_and_default(
    tmp_path: Path,
) -> None:
    provider = {
        **ANCHOR,
        "models": [
            {"id": "anchor-model", "name": "Kept", "future": {"x": 1}},
            {"id": "other-model", "api": "anthropic-messages"},
        ],
    }
    service, maintenance, root = _seeded(tmp_path, provider=provider)
    config_before = (root / "config.json").read_bytes()
    auth_before = (root / "auth.json").read_bytes()
    edit = maintenance.begin_edit("anchor")

    renamed = maintenance.rename(edit, "Renamed")
    assert renamed.status == "complete"
    assert renamed.connection is not None
    assert renamed.connection.provider_id == "anchor"
    assert renamed.connection.name == "Renamed"
    record = _document(root, "models")["providers"]["anchor"]
    assert record["models"] == provider["models"]
    assert record["baseUrl"] == provider["baseUrl"]
    assert record["api"] == provider["api"]
    assert (root / "auth.json").read_bytes() == auth_before
    assert (root / "config.json").read_bytes() == config_before

    disabled = maintenance.set_enabled(maintenance.begin_edit("anchor"), False)
    assert disabled.status == "complete"
    record = _document(root, "models")["providers"]["anchor"]
    assert record["enabled"] is False
    assert record["models"] == provider["models"]
    assert (root / "auth.json").read_bytes() == auth_before
    assert (root / "config.json").read_bytes() == config_before
    assert service.load().default_model == SessionSelection("anchor", "anchor-model")
    assert "futureRoot" in _document(root, "models")


def test_stale_edit_conflicts_without_overwriting_new_state(
    tmp_path: Path,
) -> None:
    service, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    current = _document(root, "models")["providers"]["anchor"]
    changed = {**current, "name": "Concurrent"}
    service.store.update_provider(
        "anchor", {"name": "Concurrent"}, expected_value=current
    )
    before = (root / "models.json").read_bytes()

    outcome = maintenance.rename(edit, "Stale")
    assert outcome.status == "conflict"
    assert outcome.completed == ()
    assert (root / "models.json").read_bytes() == before
    assert _document(root, "models")["providers"]["anchor"] == changed

    deleted = maintenance.delete(edit, confirmed=True)
    assert deleted.status == "conflict"
    assert deleted.completed == ()
    assert (root / "models.json").read_bytes() == before
    assert (root / "auth.json").exists()


def test_nested_edit_baseline_is_immutable_and_still_saves(
    tmp_path: Path,
) -> None:
    provider = {
        **ANCHOR,
        "models": [
            {
                "id": "anchor-model",
                "name": "Anchor Model",
                "future": {"nested": {"x": 1}},
                "input": ["text"],
            }
        ],
    }
    auth = {
        "providers": {
            "anchor": {
                **ANCHOR_AUTH,
                "future": {"nested": ["keep"]},
            }
        },
        "futureRoot": True,
    }
    _, maintenance, root = _seeded(tmp_path, provider=provider, auth=auth)
    edit = maintenance.begin_edit("anchor")
    auth_before = (root / "auth.json").read_bytes()

    models = edit._expected_provider["models"]
    assert isinstance(models, tuple)
    with pytest.raises(TypeError):
        models[0] = {}
    with pytest.raises(TypeError):
        models[0]["future"]["nested"]["x"] = 2
    with pytest.raises(TypeError):
        models[0]["input"][0] = "image"
    credential = edit._expected_credential
    assert credential is not None
    with pytest.raises(TypeError):
        credential["future"]["nested"][0] = "changed"

    renamed = maintenance.rename(edit, "Renamed")
    assert renamed.status == "complete"
    assert renamed.connection is not None
    assert renamed.connection.name == "Renamed"
    saved = _document(root, "models")["providers"]["anchor"]
    assert saved["name"] == "Renamed"
    assert saved["models"] == provider["models"]
    assert (root / "auth.json").read_bytes() == auth_before


def test_nested_discovery_baselines_are_immutable_and_still_save(
    tmp_path: Path,
) -> None:
    provider = {
        **ANCHOR,
        "models": [
            {
                "id": "anchor-model",
                "name": "Anchor Model",
                "future": {"nested": {"x": 1}},
                "input": ["text"],
            }
        ],
    }
    auth = {
        "providers": {
            "anchor": {
                **ANCHOR_AUTH,
                "future": {"nested": ["keep"]},
            }
        },
        "futureRoot": True,
    }
    _, maintenance, root = _seeded(tmp_path, provider=provider, auth=auth)
    edit = maintenance.begin_edit("anchor")
    auth_before = (root / "auth.json").read_bytes()

    async def scenario():
        async def handle(request: httpx.Request) -> httpx.Response:
            return _response(DEEPSEEK_PAYLOAD)

        return await maintenance.discover_catalog(
            edit,
            CancellationToken(),
            transport=RecordingTransport(handle),
            catalog_url=ANCHOR_CATALOG,
        )

    discovery = asyncio.run(scenario())
    models = discovery._expected_provider["models"]
    with pytest.raises(TypeError):
        models[0]["future"]["nested"]["x"] = 2
    credential = discovery._expected_credential
    assert credential is not None
    with pytest.raises(TypeError):
        credential["future"]["nested"][0] = "changed"

    outcome = maintenance.save_catalog_selection(
        discovery, {"anchor-model", "deepseek-chat"}
    )
    assert outcome.status == "complete"
    saved = _document(root, "models")["providers"]["anchor"]
    assert saved["models"][0] == {
        "id": "anchor-model",
        "name": "Anchor Model",
        "future": {"nested": {"x": 1}},
        "input": ["text"],
    }
    assert saved["models"][1]["id"] == "deepseek-chat"
    assert saved["models"][1]["api"] == "openai-completions"
    assert (root / "auth.json").read_bytes() == auth_before


def test_custom_address_changes_but_builtin_rejects_override(
    tmp_path: Path,
) -> None:
    provider = {
        **ANCHOR,
        "models": [{"id": "anchor-model"}],
    }
    _, maintenance, root = _seeded(tmp_path, provider=provider)
    custom_edit = maintenance.begin_edit("anchor")
    outcome = maintenance.update_custom_address(
        custom_edit,
        base_url="https://moved.test/v2",
        api="anthropic-messages",
    )
    assert outcome.status == "complete"
    record = _document(root, "models")["providers"]["anchor"]
    assert record["baseUrl"] == "https://moved.test/v2"
    assert record["api"] == "anthropic-messages"
    assert record["models"] == provider["models"]

    with pytest.raises(ConfigError):
        maintenance.update_custom_address(custom_edit)
    with pytest.raises(ConfigError):
        maintenance.update_custom_address(
            custom_edit, base_url="https://user:TEST_ONLY_PASSWORD@x.test"
        )

    builtin = maintenance.create_connection("openrouter", api_key="TEST_ONLY_KEY")
    assert builtin.connection is not None
    builtin_edit = maintenance.begin_edit(builtin.connection.provider_id)
    with pytest.raises(ConfigError):
        maintenance.update_custom_address(builtin_edit, base_url="https://evil.test/v1")
    with pytest.raises(ConfigError):
        maintenance.update_custom_address(builtin_edit, api="anthropic-messages")


def test_change_key_replaces_only_credential_and_reports_stale_change(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    models_before = (root / "models.json").read_bytes()
    edit = maintenance.begin_edit("anchor")

    outcome = maintenance.change_api_key(edit, "TEST_ONLY_NEW_KEY")
    assert outcome.status == "complete"
    assert outcome.completed == ("credential",)
    assert outcome.connection is not None
    assert outcome.connection.credential == "available"
    assert (root / "models.json").read_bytes() == models_before
    auth = json.loads(read_credentials(root / "auth.json"))
    assert auth["providers"]["anchor"] == {
        "type": "api_key",
        "apiKey": "TEST_ONLY_NEW_KEY",
    }

    stale = maintenance.begin_edit("anchor")
    CredentialStore(root).save_api_key(
        "anchor",
        "TEST_ONLY_CONCURRENT_KEY",
        expected_record={"type": "api_key", "apiKey": "TEST_ONLY_NEW_KEY"},
    )
    before = (root / "auth.json").read_bytes()
    conflict = maintenance.change_api_key(stale, "TEST_ONLY_LATE_KEY")
    assert conflict.status == "conflict"
    assert conflict.completed == ()
    assert (root / "auth.json").read_bytes() == before

    oauth = {
        "providers": {"anchor": {"type": "oauth", "access_token": "TEST_ONLY"}},
    }
    _, oauth_maintenance, oauth_root = _seeded(tmp_path / "oauth", auth=oauth)
    oauth_edit = oauth_maintenance.begin_edit("anchor")
    with pytest.raises(ConfigError) as caught:
        oauth_maintenance.change_api_key(oauth_edit, "TEST_ONLY_KEY")
    assert "TEST_ONLY" not in str(caught.value)
    assert _document(oauth_root, "auth") == oauth


def test_key_change_unconfirmed_protection_preserves_meaning(
    tmp_path: Path, monkeypatch
) -> None:
    import mica_agent.credential_store as credential_store_module

    _, maintenance, root = _seeded(tmp_path)
    auth_path = root / "auth.json"
    real_read = credential_store_module.read_credentials
    seen: list[Path] = []

    def failing_read(target: Path) -> bytes:
        target = Path(target)
        if target == auth_path:
            seen.append(target)
            if len(seen) == 2:
                raise CredentialProtectionError(
                    "credentials were replaced but protection could not be confirmed"
                )
        return real_read(target)

    monkeypatch.setattr(credential_store_module, "read_credentials", failing_read)
    outcome = maintenance.change_api_key(
        maintenance.begin_edit("anchor"), "TEST_ONLY_NEW_KEY"
    )
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ()
    assert outcome.remaining == ("credential",)
    assert outcome.connection is None
    assert "replaced" in (outcome.hint or "")
    assert "TEST_ONLY_NEW_KEY" not in (outcome.hint or "")
    credentials = json.loads(real_read(auth_path))
    assert credentials["providers"]["anchor"]["apiKey"] == "TEST_ONLY_NEW_KEY"


def test_key_change_pre_write_protection_failure_keeps_original_credential(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    auth_before = (root / "auth.json").read_bytes()
    try:
        protect(root, shared=True, rights="CreateFiles")
        outcome = maintenance.change_api_key(edit, "TEST_ONLY_NEW_KEY")
    finally:
        protect(root)
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ()
    assert outcome.remaining == ("credential",)
    assert outcome.connection is None
    assert outcome.hint
    assert "TEST_ONLY_NEW_KEY" not in (outcome.hint or "")
    assert (root / "auth.json").read_bytes() == auth_before
    assert "TEST_ONLY_NEW_KEY" not in (root / "auth.json").read_text()


def test_change_key_rejects_disabled_or_removed_connection(
    tmp_path: Path,
) -> None:
    service, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    current = _document(root, "models")["providers"]["anchor"]
    service.store.update_provider("anchor", {"enabled": False}, expected_value=current)
    before = (root / "auth.json").read_bytes()
    outcome = maintenance.change_api_key(edit, "TEST_ONLY_LATE_KEY")
    assert outcome.status == "conflict"
    assert (root / "auth.json").read_bytes() == before

    edit = maintenance.begin_edit("anchor")
    current = _document(root, "models")["providers"]["anchor"]
    service.store.delete_provider("anchor", expected_value=current)
    outcome = maintenance.change_api_key(edit, "TEST_ONLY_LATE_KEY")
    assert outcome.status == "conflict"
    assert (root / "auth.json").read_bytes() == before


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_key_change_reports_concurrent_models_change_after_credential_commit(
    tmp_path: Path, monkeypatch, action: str
) -> None:
    service, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    current = _document(root, "models")["providers"]["anchor"]
    real_replace = os.replace
    injected = False

    def interleave(source, destination):
        nonlocal injected
        if Path(destination).name == "auth.json" and not injected:
            injected = True
            if action == "disable":
                service.store.update_provider(
                    "anchor", {"enabled": False}, expected_value=current
                )
            else:
                service.store.delete_provider("anchor", expected_value=current)
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", interleave)
    outcome = maintenance.change_api_key(edit, "TEST_ONLY_NEW_KEY")
    assert injected is True
    assert outcome.status == "partial"
    assert outcome.completed == ("credential",)
    assert outcome.remaining == ("models",)
    assert outcome.connection is None
    assert "refresh" in (outcome.hint or "")
    assert "TEST_ONLY_NEW_KEY" not in (outcome.hint or "")

    credentials = json.loads(read_credentials(root / "auth.json"))
    assert credentials["providers"]["anchor"]["apiKey"] == "TEST_ONLY_NEW_KEY"
    providers = _document(root, "models")["providers"]
    if action == "disable":
        assert providers["anchor"]["enabled"] is False
        assert providers["anchor"]["models"] == ANCHOR["models"]
    else:
        assert "anchor" not in providers


def test_key_change_after_credential_read_failure_keeps_confirmed_commit(
    tmp_path: Path, monkeypatch
) -> None:
    service, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    real_replace = os.replace
    real_read = service.store.read
    committed = False

    def mark_commit(source, destination):
        nonlocal committed
        result = real_replace(source, destination)
        if Path(destination).name == "auth.json":
            committed = True
        return result

    def failing_read(name, *, workspace=False):
        if committed and name == "models":
            raise ConfigError("configuration file cannot be read; check access")
        return real_read(name, workspace=workspace)

    monkeypatch.setattr(os, "replace", mark_commit)
    monkeypatch.setattr(service.store, "read", failing_read)
    outcome = maintenance.change_api_key(edit, "TEST_ONLY_NEW_KEY")
    assert committed is True
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ("credential",)
    assert outcome.remaining == ("models",)
    assert outcome.connection is None
    assert "could not be checked" in (outcome.hint or "")
    assert "TEST_ONLY_NEW_KEY" not in (outcome.hint or "")
    credentials = json.loads(read_credentials(root / "auth.json"))
    assert credentials["providers"]["anchor"]["apiKey"] == "TEST_ONLY_NEW_KEY"


def test_manual_model_saves_only_explicit_capabilities(
    tmp_path: Path,
) -> None:
    provider = {
        **ANCHOR,
        "models": [
            {
                "id": "manual-model",
                "api": "openai-completions",
                "name": "Old Name",
                "future": {"x": 1},
            }
        ],
    }
    service, maintenance, root = _seeded(tmp_path, provider=provider)
    config_before = (root / "config.json").read_bytes()
    auth_before = (root / "auth.json").read_bytes()

    added = maintenance.save_manual_model(
        maintenance.begin_edit("anchor"),
        ManualModel(
            "manual-two",
            "anthropic-messages",
            context_window=8192,
            max_tokens=1024,
            input=("text", "image"),
            reasoning=True,
            thinking_level_map={ReasoningEffort.HIGH: "high"},
        ),
    )
    assert added.status == "complete"
    saved = _document(root, "models")["providers"]["anchor"]
    assert saved["models"] == [
        {
            "id": "manual-model",
            "api": "openai-completions",
            "name": "Old Name",
            "future": {"x": 1},
        },
        {
            "id": "manual-two",
            "api": "anthropic-messages",
            "contextWindow": 8192,
            "maxTokens": 1024,
            "input": ["text", "image"],
            "reasoning": True,
            "thinkingLevelMap": {"high": "high"},
        },
    ]
    assert (root / "auth.json").read_bytes() == auth_before
    assert (root / "config.json").read_bytes() == config_before
    assert _document(root, "models")["futureRoot"] == {"keep": True}

    updated = maintenance.save_manual_model(
        maintenance.begin_edit("anchor"),
        ManualModel("manual-model", "openai-responses", name="New Name"),
    )
    assert updated.status == "complete"
    saved = _document(root, "models")["providers"]["anchor"]
    assert saved["models"][0] == {
        "id": "manual-model",
        "api": "openai-responses",
        "name": "New Name",
        "future": {"x": 1},
    }
    assert (root / "auth.json").read_bytes() == auth_before
    assert (root / "config.json").read_bytes() == config_before

    parsed = service.load()
    anchor = next(item for item in parsed.providers if item.provider_id == "anchor")
    manual = next(
        model for model in anchor.models if model.selection.model_id == "manual-two"
    )
    assert manual.capabilities.max_tokens == 1024
    assert manual.capabilities.input == ("text", "image")


def test_manual_model_conflicts_on_stale_baseline_and_rejects_bad_input(
    tmp_path: Path,
) -> None:
    service, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    current = _document(root, "models")["providers"]["anchor"]
    service.store.update_provider("anchor", {"enabled": False}, expected_value=current)
    before = (root / "models.json").read_bytes()
    outcome = maintenance.save_manual_model(
        edit, ManualModel("late-model", "openai-completions")
    )
    assert outcome.status == "conflict"
    assert (root / "models.json").read_bytes() == before

    fresh = maintenance.begin_edit("anchor")
    for bad in (
        ManualModel("", "openai-completions"),
        ManualModel("model", "bogus"),
        ManualModel("model", "openai-completions", context_window=0),
        ManualModel("model", "openai-completions", input=("image",)),
        ManualModel("model", "openai-completions", thinking_level_map={}),
        ManualModel(
            "model",
            "openai-completions",
            thinking_level_map={ReasoningEffort.HIGH: "high"},
        ),
    ):
        with pytest.raises(ConfigError):
            maintenance.save_manual_model(fresh, bad)
    assert (root / "models.json").read_bytes() == before


def test_delete_requires_confirmation_and_removes_only_target(
    tmp_path: Path,
) -> None:
    models_before = {
        "providers": {
            "anchor": dict(ANCHOR),
            "other": {
                "name": "Other",
                "channel": "custom",
                "enabled": True,
                "baseUrl": "https://other.test/v1",
                "api": "openai-completions",
                "models": [{"id": "other-model"}],
            },
        },
        "futureRoot": {"keep": True},
    }
    auth_before = {
        "providers": {
            "anchor": dict(ANCHOR_AUTH),
            "other": {"type": "api_key", "apiKey": "TEST_ONLY_OTHER"},
        },
        "futureRoot": True,
    }
    service, maintenance, root = _seeded(
        tmp_path, models=models_before, auth=auth_before
    )
    config_before = (root / "config.json").read_bytes()
    models_bytes = (root / "models.json").read_bytes()
    auth_bytes = (root / "auth.json").read_bytes()

    refused = maintenance.delete(maintenance.begin_edit("anchor"), confirmed=False)
    assert refused.status == "not_confirmed"
    assert refused.completed == ()
    assert refused.remaining == ("models", "credential")
    assert (root / "models.json").read_bytes() == models_bytes
    assert (root / "auth.json").read_bytes() == auth_bytes
    assert (root / "config.json").read_bytes() == config_before

    removed = maintenance.delete(maintenance.begin_edit("anchor"), confirmed=True)
    assert removed.status == "complete"
    assert removed.completed == ("models", "credential")
    result = _document(root, "models")
    assert list(result["providers"]) == ["other"]
    assert result["futureRoot"] == {"keep": True}
    credentials = _document(root, "auth")
    assert list(credentials["providers"]) == ["other"]
    assert credentials["futureRoot"] is True
    assert (root / "config.json").read_bytes() == config_before
    assert service.load().default_model == SessionSelection("anchor", "anchor-model")


def test_delete_without_credential_does_not_create_auth_target(
    tmp_path: Path,
) -> None:
    models = {"providers": {"anchor": dict(ANCHOR)}, "futureRoot": True}
    _, maintenance, root = _seeded(tmp_path, models=models, auth=None)
    assert not (root / "auth.json").exists()
    assert maintenance.list_connections()[0].credential == "missing"
    outcome = maintenance.delete(maintenance.begin_edit("anchor"), confirmed=True)
    assert outcome.status == "complete"
    assert outcome.completed == ("models",)
    assert outcome.remaining == ()
    assert not (root / "auth.json").exists()
    assert _document(root, "models") == {"providers": {}, "futureRoot": True}


def test_delete_partial_when_credential_changed_after_edit(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    CredentialStore(root).save_api_key(
        "anchor",
        "TEST_ONLY_CONCURRENT_KEY",
        expected_record=dict(ANCHOR_AUTH),
    )
    auth_before = (root / "auth.json").read_bytes()
    outcome = maintenance.delete(edit, confirmed=True)
    assert outcome.status == "partial"
    assert outcome.completed == ("models",)
    assert outcome.remaining == ("credential",)
    assert "TEST_ONLY_CONCURRENT_KEY" not in (outcome.hint or "")
    assert "anchor" not in _document(root, "models")["providers"]
    assert (root / "auth.json").read_bytes() == auth_before


def test_delete_unconfirmed_protection_reports_unconfirmed(
    tmp_path: Path, monkeypatch
) -> None:
    import mica_agent.credential_store as credential_store_module

    _, maintenance, root = _seeded(tmp_path)
    auth_path = root / "auth.json"
    real_read = credential_store_module.read_credentials
    seen: list[Path] = []

    def failing_read(target: Path) -> bytes:
        target = Path(target)
        if target == auth_path:
            seen.append(target)
            if len(seen) == 2:
                raise CredentialProtectionError(
                    "credentials were replaced but protection could not be confirmed"
                )
        return real_read(target)

    monkeypatch.setattr(credential_store_module, "read_credentials", failing_read)
    outcome = maintenance.delete(maintenance.begin_edit("anchor"), confirmed=True)
    assert outcome.status == "unconfirmed"
    assert outcome.completed == ("models",)
    assert outcome.remaining == ("credential",)
    assert "replaced" in (outcome.hint or "")
    assert "anchor" not in _document(root, "models")["providers"]
    assert json.loads(real_read(auth_path))["providers"] == {}


def test_catalog_failure_keeps_connection_and_allows_manual_fill(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    models_before = (root / "models.json").read_bytes()
    auth_before = (root / "auth.json").read_bytes()

    async def scenario():
        async def handle(request: httpx.Request) -> httpx.Response:
            return _response({"error": "denied"}, status=401)

        return await maintenance.discover_catalog(
            edit,
            CancellationToken(),
            transport=RecordingTransport(handle),
            catalog_url=ANCHOR_CATALOG,
        )

    discovery = asyncio.run(scenario())
    assert discovery.result.status == "failed"
    assert discovery.result.error == "authentication"
    with pytest.raises(ConfigError):
        maintenance.save_catalog_selection(discovery, {"remote"})
    assert (root / "models.json").read_bytes() == models_before
    assert (root / "auth.json").read_bytes() == auth_before

    manual = maintenance.save_manual_model(
        maintenance.begin_edit("anchor"),
        ManualModel("fallback", "openai-completions"),
    )
    assert manual.status == "complete"
    assert _document(root, "models")["providers"]["anchor"]["models"] == [
        {"id": "anchor-model", "name": "Anchor Model"},
        {"id": "fallback", "api": "openai-completions"},
    ]


def test_custom_without_catalog_url_requires_manual_fill(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    discovery = asyncio.run(maintenance.discover_catalog(edit, CancellationToken()))
    assert discovery.result.status == "manual"
    with pytest.raises(ConfigError):
        maintenance.save_catalog_selection(discovery, set())
    assert _document(root, "models")["providers"]["anchor"]["models"] == [
        {"id": "anchor-model", "name": "Anchor Model"}
    ]


def test_discovery_without_credential_is_rejected_before_request(
    tmp_path: Path,
) -> None:
    _, maintenance, _ = _seeded(
        tmp_path,
        models={"providers": {"anchor": dict(ANCHOR)}},
        auth=None,
    )
    edit = maintenance.begin_edit("anchor")

    async def scenario():
        async def handle(request: httpx.Request) -> httpx.Response:
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        with pytest.raises(ConfigError) as caught:
            await maintenance.discover_catalog(
                edit,
                CancellationToken(),
                transport=transport,
                catalog_url=ANCHOR_CATALOG,
            )
        assert "TEST_ONLY" not in str(caught.value)
        assert transport.requests == []
        assert transport.aclose_count == 0

    asyncio.run(scenario())


def test_go_new_model_requires_explicit_api_choice(tmp_path: Path) -> None:
    provider = {
        "name": "Go",
        "channel": "opencode-go",
        "enabled": True,
        "baseUrl": "https://opencode.ai/inference/go/openai/v1",
        "api": "openai-completions",
        "models": [{"id": "go-old", "api": "anthropic-messages"}],
    }
    auth = {
        "providers": {"anchor": {"type": "oauth", "access_token": "TEST_ONLY_OAUTH"}},
        "futureRoot": True,
    }
    _, maintenance, root = _service(
        tmp_path,
        models={"providers": {"anchor": provider}, "futureRoot": True},
        auth=auth,
        config={"defaultModel": {"provider": "anchor", "model": "go-old"}},
    )
    assert maintenance.list_connections()[0].credential == "unsupported"
    edit = maintenance.begin_edit("anchor")

    async def scenario():
        seen: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _response(GO_PAYLOAD)

        transport = RecordingTransport(handle)
        discovery = await maintenance.discover_catalog(
            edit,
            CancellationToken(),
            transport=transport,
            session_id="session-123",
        )
        return seen[0], transport, discovery

    request, transport, discovery = asyncio.run(scenario())
    assert str(request.url) == "https://opencode.ai/inference/go/v1/models"
    assert request.headers["x-opencode-session"] == "session-123"
    assert "Authorization" not in request.headers
    assert transport.aclose_count == 1

    models_before = (root / "models.json").read_bytes()
    with pytest.raises(ConfigError):
        maintenance.save_catalog_selection(discovery, {"go-old", "go-alpha"})
    assert (root / "models.json").read_bytes() == models_before

    saved = maintenance.save_catalog_selection(
        discovery,
        {"go-old", "go-alpha"},
        api_choices={"go-alpha": "anthropic-messages"},
    )
    assert saved.status == "complete"
    entries = _document(root, "models")["providers"]["anchor"]["models"]
    assert [entry["id"] for entry in entries] == ["go-old", "go-alpha"]
    assert entries[0]["api"] == "anthropic-messages"
    assert entries[1]["api"] == "anthropic-messages"


def test_discovery_uses_baseline_key_without_holding_store_locks(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        seen: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            started.set()
            await release.wait()
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        task = asyncio.create_task(
            maintenance.discover_catalog(
                edit,
                CancellationToken(),
                transport=transport,
                catalog_url=ANCHOR_CATALOG,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        with locked_file(root / "auth.json.lock", timeout=0.2):
            pass
        with locked_file(root / "models.json.lock", timeout=0.2):
            pass
        release.set()
        discovery = await task
        assert _pending_tasks() == []
        return seen[0], transport, discovery

    request, transport, discovery = asyncio.run(scenario())
    assert request.headers["Authorization"] == "Bearer TEST_ONLY_ANCHOR_KEY"
    assert transport.responses[0].is_closed is True
    assert discovery.result.status == "ok"


def test_key_change_during_discovery_rejects_stale_selection(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    models_before = (root / "models.json").read_bytes()

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return _response(DEEPSEEK_PAYLOAD)

        task = asyncio.create_task(
            maintenance.discover_catalog(
                edit,
                CancellationToken(),
                transport=RecordingTransport(handle),
                catalog_url=ANCHOR_CATALOG,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        changed = maintenance.change_api_key(
            maintenance.begin_edit("anchor"), "TEST_ONLY_DURING_KEY"
        )
        assert changed.status == "complete"
        release.set()
        return await task

    discovery = asyncio.run(scenario())
    outcome = maintenance.save_catalog_selection(discovery, {"deepseek-chat"})
    assert outcome.status == "conflict"
    assert outcome.completed == ()
    assert (root / "models.json").read_bytes() == models_before
    auth = json.loads(read_credentials(root / "auth.json"))
    assert auth["providers"]["anchor"]["apiKey"] == "TEST_ONLY_DURING_KEY"


def test_disable_during_discovery_rejects_stale_selection(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    auth_before = (root / "auth.json").read_bytes()

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return _response(DEEPSEEK_PAYLOAD)

        task = asyncio.create_task(
            maintenance.discover_catalog(
                edit,
                CancellationToken(),
                transport=RecordingTransport(handle),
                catalog_url=ANCHOR_CATALOG,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        disabled = maintenance.set_enabled(maintenance.begin_edit("anchor"), False)
        assert disabled.status == "complete"
        release.set()
        return await task

    discovery = asyncio.run(scenario())
    outcome = maintenance.save_catalog_selection(discovery, {"deepseek-chat"})
    assert outcome.status == "conflict"
    assert (root / "auth.json").read_bytes() == auth_before
    assert _document(root, "models")["providers"]["anchor"]["enabled"] is False
    assert _document(root, "models")["providers"]["anchor"]["models"] == [
        {"id": "anchor-model", "name": "Anchor Model"}
    ]


def test_delete_during_discovery_and_late_save_do_not_recreate(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return _response(DEEPSEEK_PAYLOAD)

        task = asyncio.create_task(
            maintenance.discover_catalog(
                edit,
                CancellationToken(),
                transport=RecordingTransport(handle),
                catalog_url=ANCHOR_CATALOG,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        removed = maintenance.delete(maintenance.begin_edit("anchor"), confirmed=True)
        assert removed.status == "complete"
        release.set()
        return await task

    discovery = asyncio.run(scenario())
    outcome = maintenance.save_catalog_selection(discovery, {"deepseek-chat"})
    assert outcome.status == "conflict"
    assert _document(root, "models")["providers"] == {}
    assert _document(root, "auth")["providers"] == {}


def test_cancellation_closes_transport_and_leaves_no_tasks(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")

    async def scenario():
        started = asyncio.Event()
        never = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await never.wait()
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        token = CancellationToken()
        task = asyncio.create_task(
            maintenance.discover_catalog(
                edit,
                token,
                transport=transport,
                catalog_url=ANCHOR_CATALOG,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(CancellationRequested):
            await task
        assert transport.aclose_count == 1
        assert transport.responses == []
        assert _pending_tasks() == []
        with locked_file(root / "auth.json.lock", timeout=0.2):
            pass
        with locked_file(root / "models.json.lock", timeout=0.2):
            pass

    asyncio.run(scenario())


def test_invalid_directory_structures_and_protection_are_rejected(
    tmp_path: Path,
) -> None:
    _, maintenance, _ = _service(
        tmp_path,
        models={"providers": []},
    )
    with pytest.raises(ConfigError):
        maintenance.list_connections()
    with pytest.raises(ConfigError):
        maintenance.begin_edit("anchor")

    _, broken, broken_root = _service(
        tmp_path / "broken",
        models={"providers": {"anchor": "not-an-object"}},
    )
    with pytest.raises(ConfigError):
        broken.list_connections()
    with pytest.raises(ConfigError):
        broken.begin_edit("anchor")

    _, bad_auth, _ = _service(
        tmp_path / "bad-auth",
        models={"providers": {"anchor": dict(ANCHOR)}},
        auth={"providers": {"anchor": "not-an-object"}},
    )
    with pytest.raises(ConfigError):
        bad_auth.list_connections()
    with pytest.raises(ConfigError):
        bad_auth.begin_edit("anchor")

    invalid_json = broken_root / "models.json"
    invalid_json.write_text("{")
    with pytest.raises(ConfigError):
        broken.list_connections()

    _, protected, protected_root = _seeded(tmp_path / "protected")
    protect(protected_root / "auth.json", shared=True)
    with pytest.raises(CredentialProtectionError):
        protected.list_connections()
    with pytest.raises(CredentialProtectionError):
        protected.begin_edit("anchor")


def test_safe_repr_and_errors_do_not_leak_secrets_or_addresses(
    tmp_path: Path,
) -> None:
    _, maintenance, root = _seeded(tmp_path)
    edit = maintenance.begin_edit("anchor")
    assert "TEST_ONLY_ANCHOR_KEY" not in repr(edit)
    assert "https://anchor.test" not in repr(edit)
    assert str(root) not in repr(edit)

    async def scenario():
        async def handle(request: httpx.Request) -> httpx.Response:
            return _response(DEEPSEEK_PAYLOAD)

        return await maintenance.discover_catalog(
            edit, CancellationToken(), transport=RecordingTransport(handle)
        )

    discovery = asyncio.run(scenario())
    assert "TEST_ONLY_ANCHOR_KEY" not in repr(discovery)
    assert "https://anchor.test" not in repr(discovery)
    assert str(root) not in repr(discovery)

    with pytest.raises(ConfigError) as caught:
        maintenance.rename(edit, "TEST_ONLY_BAD\x00NAME")
    assert "TEST_ONLY_BAD" not in str(caught.value)

    with pytest.raises(ConfigError) as caught:
        maintenance.update_custom_address(
            edit,
            base_url="https://user:TEST_ONLY_PASSWORD@anchor.test/v1",
        )
    assert "TEST_ONLY_PASSWORD" not in str(caught.value)
    assert "anchor.test" not in str(caught.value)

    with pytest.raises(ConfigError) as caught:
        maintenance.save_manual_model(
            edit, ManualModel("model", "openai-completions", input=("audio",))
        )
    assert str(caught.value) == "model input capabilities are invalid"
    assert b"audio" not in (root / "models.json").read_bytes()
