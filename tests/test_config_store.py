from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mica_agent.config_contracts import ConfigConflictError, ConfigError
from mica_agent.config_store import ConfigStore, parse_json, serialize_json
from mica_agent.session_contracts import SessionSelection


@pytest.mark.parametrize(
    "payload",
    [b"[]", b"{", b'{"a":1,"a":2}', b'{"a":{"x":1,"x":2}}', b'{"a":NaN}', b"\xff"],
)
def test_bad_json_is_rejected_without_echo(payload: bytes) -> None:
    with pytest.raises(ConfigError):
        parse_json(payload)


def test_save_merges_unrelated_changes_and_preserves_unknown_fields(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path, tmp_path / "workspace")
    path = tmp_path / "config.json"
    old = SessionSelection("local", "old")
    path.write_text(
        json.dumps(
            {
                "defaultModel": {"provider": "local", "model": "old", "future": 42},
                "futureRoot": [1, 2],
                "permissionMode": "solo",
            }
        )
    )
    store.save_default_model(SessionSelection("local", "new"), old)
    assert json.loads(path.read_text()) == {
        "defaultModel": {"provider": "local", "model": "new", "future": 42},
        "futureRoot": [1, 2],
        "permissionMode": "solo",
    }
    before = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        store.save_default_model(old, old)
    assert path.read_bytes() == before


def test_concurrent_default_saves_have_one_winner(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path, tmp_path)

    def save(model):
        try:
            store.save_default_model(SessionSelection("local", model), None)
            return "saved"
        except ConfigConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("a", "b")))
    assert sorted(results) == ["conflict", "saved"]


def test_corruption_and_replace_failure_keep_original_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "config.json"
    path.write_bytes(b"{ broken TEST_ONLY_SECRET")
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.save_default_model(SessionSelection("local", "new"), None)
    assert path.read_bytes() == before
    path.write_bytes(b'{"future":17}\n')
    before = path.read_bytes()

    def fail(*args):
        raise OSError("TEST_ONLY_SECRET")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ConfigError) as caught:
        store.save_default_model(SessionSelection("local", "new"), None)
    assert "TEST_ONLY_SECRET" not in str(caught.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_reads_are_side_effect_free_and_temporary_defaults_rejected(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path / "missing", tmp_path)
    assert store.read("config") == {}
    assert not store.data_root.exists()
    with pytest.raises(ConfigError):
        store.save_default_model(SessionSelection("mica-env:hash", "chat"), None)
    assert not store.data_root.exists()


def _seed_models(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_provider_update_creates_and_preserves_unrelated_records(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    other = {"baseUrl": "https://other.test/v1", "future": 1}
    _seed_models(
        path,
        {"futureRoot": {"keep": True}, "providers": {"other": other}},
    )
    store.update_provider(
        "local",
        {
            "name": "Local",
            "baseUrl": "https://example.test/v1",
            "api": "openai-completions",
            "models": [{"id": "a"}, {"id": "b", "name": "B"}],
        },
        expected_value=None,
    )
    document = json.loads(path.read_text())
    assert document["futureRoot"] == {"keep": True}
    assert document["providers"]["other"] == other
    local = document["providers"]["local"]
    assert local["name"] == "Local"
    assert local["models"] == [{"id": "a"}, {"id": "b", "name": "B"}]

    local["future"] = 7
    _seed_models(path, document)
    store.update_provider(
        "local",
        {"name": "Renamed", "enabled": False, "models": [{"id": "c"}]},
        expected_value=local,
    )
    document = json.loads(path.read_text())
    assert document["providers"]["local"] == {
        **local,
        "name": "Renamed",
        "enabled": False,
        "models": [{"id": "c"}],
    }
    assert document["providers"]["other"] == other


def test_provider_delete_keeps_default_and_other_records(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"defaultModel": {"provider": "local", "model": "a"}}))
    path = tmp_path / "models.json"
    other = {"baseUrl": "https://other.test/v1"}
    _seed_models(
        path,
        {
            "providers": {
                "local": {"baseUrl": "https://example.test/v1"},
                "other": other,
            }
        },
    )
    local = store.read("models")["providers"]["local"]
    store.delete_provider("local", expected_value=local)
    document = json.loads(path.read_text())
    assert document["providers"] == {"other": other}
    assert json.loads(config.read_text()) == {
        "defaultModel": {"provider": "local", "model": "a"}
    }


def test_provider_updates_require_the_record_that_was_read(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(
        path,
        {"providers": {"local": {"baseUrl": "https://example.test/v1"}}},
    )
    before = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        store.update_provider("local", {"name": "New"}, expected_value=None)
    with pytest.raises(ConfigConflictError):
        store.update_provider(
            "local",
            {"name": "New"},
            expected_value={"baseUrl": "https://stale.test/v1"},
        )
    with pytest.raises(ConfigConflictError):
        store.delete_provider(
            "local", expected_value={"baseUrl": "https://stale.test/v1"}
        )
    with pytest.raises(ConfigConflictError):
        store.delete_provider("missing", expected_value={})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "provider_id",
    ["", "   ", "mica-env:hash", "mica-direct:hash", "line\nbreak"],
)
def test_provider_identifier_validation(tmp_path: Path, provider_id: str) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    with pytest.raises(ConfigError):
        store.update_provider(provider_id, {}, expected_value=None)
    assert not (tmp_path / "models.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "other"},
        {"enabled": "yes"},
        {"api": "unknown"},
        {"channel": "unknown"},
        {"baseUrl": "file:///etc/passwd"},
        {"models": {"a": 1}},
        {"models": [{"id": "a"}, {"id": "a"}]},
        {"models": [{"id": "a", "contextWindow": 0}]},
        {"models": [{"id": "a", "api": []}]},
        {"models": [{"id": "a", "input": [["x"]]}]},
        {"models": [{"id": "a", "input": ["image"]}]},
        {"models": [{"id": "a", "reasoning": "yes"}]},
        {"models": [{"id": "a", "thinkingLevelMap": {"unknown": "x"}}]},
    ],
)
def test_provider_changes_are_validated_before_writing(
    tmp_path: Path, changes: dict
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(
        path,
        {"providers": {"local": {"baseUrl": "https://example.test/v1"}}},
    )
    before = path.read_bytes()
    with pytest.raises((ConfigError, TypeError)) as caught:
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1", **changes},
            expected_value=None,
        )
    assert "TEST_ONLY" not in str(caught.value)
    assert path.read_bytes() == before


def test_invalid_provider_directory_and_target_are_not_rebuilt(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(path, {"providers": []})
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1"},
            expected_value=None,
        )
    assert path.read_bytes() == before
    _seed_models(path, {"providers": {"local": "invalid"}})
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1"},
            expected_value=None,
        )
    assert path.read_bytes() == before
    with pytest.raises(TypeError):
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1"},
            expected_value="invalid",
        )


def test_concurrent_provider_updates_conflict_or_merge(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path, tmp_path)

    def create(provider_id):
        try:
            store.update_provider(
                provider_id,
                {"baseUrl": "https://example.test/v1"},
                expected_value=None,
            )
            return "saved"
        except ConfigConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("same", "same")))
    assert sorted(results) == ["conflict", "saved"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("first", "second")))
    assert results == ["saved", "saved"]
    providers = json.loads((tmp_path / "models.json").read_text())["providers"]
    assert sorted(providers) == ["first", "same", "second"]


def test_provider_save_failure_keeps_original_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(
        path,
        {"providers": {"local": {"baseUrl": "https://example.test/v1"}}},
    )
    before = path.read_bytes()

    def fail(*args):
        raise OSError("TEST_ONLY_SECRET")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ConfigError) as caught:
        store.update_provider("local", {"name": "New"}, expected_value=None)
    assert "TEST_ONLY_SECRET" not in str(caught.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_oversized_provider_content_is_rejected_before_writing(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(
        path,
        {"providers": {"local": {"baseUrl": "https://example.test/v1"}}},
    )
    before = path.read_bytes()
    with pytest.raises(ConfigError) as caught:
        store.update_provider(
            "local", {"name": "x" * (4 * 1024 * 1024)}, expected_value=None
        )
    assert "xxxx" not in str(caught.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_null_or_non_object_provider_target_is_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(path, {"providers": {"local": None}})
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1"},
            expected_value=None,
        )
    assert path.read_bytes() == before
    with pytest.raises(ConfigError):
        store.delete_provider("local", expected_value={})
    assert path.read_bytes() == before

    _seed_models(path, {"providers": {"local": []}})
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.update_provider(
            "local",
            {"baseUrl": "https://example.test/v1"},
            expected_value=None,
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "future",
    [
        {1: "one", "1": "two"},
        {1: "one"},
        {"nested": [{2: "two"}]},
        {"nested": ({2: "two"},)},
    ],
)
def test_non_string_keys_are_rejected_before_writing(
    tmp_path: Path, future: dict
) -> None:
    store = ConfigStore(tmp_path, tmp_path)
    path = tmp_path / "models.json"
    _seed_models(
        path,
        {"providers": {"local": {"baseUrl": "https://example.test/v1"}}},
    )
    record = store.read("models")["providers"]["local"]
    before = path.read_bytes()
    with pytest.raises(ConfigError) as caught:
        store.update_provider(
            "local",
            {"models": [{"id": "model", "future": future}]},
            expected_value=record,
        )
    assert "TEST_ONLY" not in str(caught.value)
    assert path.read_bytes() == before


def test_serialized_configuration_is_strictly_readable() -> None:
    document = {
        "providers": {
            "local": {
                "baseUrl": "https://example.test/v1",
                "models": [{"id": "a", "future": {"nested": [{"x": 1}]}}],
            }
        }
    }
    payload = serialize_json(document)
    assert parse_json(payload) == document
    with pytest.raises(ConfigError):
        serialize_json({"providers": {"local": {"future": {1: "one"}}}})


def test_config_store_depends_on_shared_validation_not_the_service() -> None:
    import ast
    import inspect

    source = Path(inspect.getsourcefile(ConfigStore)).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            elif node.level:
                modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert "config_service" not in modules
    assert "_config_validation" in modules
