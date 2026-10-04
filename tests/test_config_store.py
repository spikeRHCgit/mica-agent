from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mica_agent.config_contracts import ConfigConflictError, ConfigError
from mica_agent.config_store import ConfigStore, parse_json
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
