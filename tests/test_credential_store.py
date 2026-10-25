from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_credential_permissions import protect

from mica_agent._credential_permissions import (
    create_protected_temp,
    ensure_credential_directory,
    read_credentials,
)
from mica_agent.config_contracts import (
    ConfigConflictError,
    ConfigError,
    CredentialProtectionError,
)
from mica_agent.credential_store import CredentialStore


def seed_auth(
    root: Path, providers: dict, *, extra: dict | None = None
) -> tuple[Path, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "auth.json"
    document = {"providers": providers}
    if extra is not None:
        document.update(extra)
    path.write_text(json.dumps(document))
    protect(path)
    return path, path.read_bytes()


def test_new_api_key_is_saved_with_protected_read(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "data")
    store.save_api_key("local", "TEST_ONLY_KEY", expected_record=None)
    path = store.path
    assert json.loads(read_credentials(path)) == {
        "providers": {"local": {"type": "api_key", "apiKey": "TEST_ONLY_KEY"}}
    }
    if os.name != "nt":
        assert stat.S_IMODE(store.data_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    replace = {"type": "api_key", "apiKey": "TEST_ONLY_KEY"}
    store.save_api_key("local", "TEST_ONLY_KEY_2", expected_record=replace)
    assert json.loads(read_credentials(path))["providers"]["local"] == {
        "type": "api_key",
        "apiKey": "TEST_ONLY_KEY_2",
    }


def test_key_replacement_preserves_unknown_and_unrelated_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    other = {"type": "api_key", "apiKey": "TEST_ONLY_OTHER"}
    path, _ = seed_auth(
        root,
        {
            "other": other,
            "local": {"type": "api_key", "apiKey": "OLD", "future": 7},
        },
        extra={"futureRoot": [1, 2]},
    )
    store = CredentialStore(root)
    store.save_api_key(
        "local",
        "NEW",
        expected_record={"type": "api_key", "apiKey": "OLD", "future": 7},
    )
    document = json.loads(read_credentials(path))
    assert document["providers"]["local"] == {
        "type": "api_key",
        "apiKey": "NEW",
        "future": 7,
    }
    assert document["providers"]["other"] == other
    assert document["futureRoot"] == [1, 2]


def test_delete_removes_only_the_target_record(tmp_path: Path) -> None:
    root = tmp_path / "data"
    other = {"type": "api_key", "apiKey": "TEST_ONLY_OTHER"}
    path, _ = seed_auth(
        root,
        {"other": other, "local": {"type": "api_key", "apiKey": "OLD"}},
        extra={"futureRoot": True},
    )
    CredentialStore(root).delete(
        "local", expected_record={"type": "api_key", "apiKey": "OLD"}
    )
    document = json.loads(read_credentials(path))
    assert document == {"providers": {"other": other}, "futureRoot": True}


def test_stale_or_absent_expected_record_conflicts_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    path, before = seed_auth(root, {"local": {"type": "api_key", "apiKey": "OLD"}})
    store = CredentialStore(root)
    with pytest.raises(ConfigConflictError):
        store.save_api_key(
            "local", "NEW", expected_record={"type": "api_key", "apiKey": "STALE"}
        )
    with pytest.raises(ConfigConflictError):
        store.save_api_key("local", "NEW", expected_record=None)
    with pytest.raises(ConfigConflictError):
        store.delete("local", expected_record={"type": "api_key", "apiKey": "STALE"})
    assert path.read_bytes() == before


def test_concurrent_saves_have_one_winner_per_target(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = CredentialStore(root)

    def save(provider_id: str, api_key: str) -> str:
        try:
            store.save_api_key(provider_id, api_key, expected_record=None)
            return "saved"
        except ConfigConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: save("local", key),
                ("TEST_ONLY_A", "TEST_ONLY_B"),
            )
        )
    assert sorted(results) == ["conflict", "saved"]
    providers = json.loads(read_credentials(root / "auth.json"))["providers"]
    assert list(providers) == ["local"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: save(*item),
                (("first", "TEST_ONLY_C"), ("second", "TEST_ONLY_D")),
            )
        )
    assert results == ["saved", "saved"]
    providers = json.loads(read_credentials(root / "auth.json"))["providers"]
    assert sorted(providers) == ["first", "local", "second"]


@pytest.mark.parametrize(
    "provider_id", ["", "   ", "mica-env:hash", "mica-direct:hash", "line\nbreak"]
)
def test_invalid_provider_ids_are_rejected_before_creating_files(
    tmp_path: Path, provider_id: str
) -> None:
    store = CredentialStore(tmp_path / "data")
    with pytest.raises(ConfigError) as caught:
        store.save_api_key(provider_id, "TEST_ONLY_KEY", expected_record=None)
    assert "TEST_ONLY_KEY" not in str(caught.value)
    assert str(store.data_root) not in str(caught.value)
    assert not store.data_root.exists()


@pytest.mark.parametrize(
    "api_key", ["", " TEST_ONLY", "TEST_ONLY\n", 42, "x" * (16 * 1024 + 1)]
)
def test_invalid_api_keys_are_rejected_without_echo(
    tmp_path: Path, api_key: object
) -> None:
    store = CredentialStore(tmp_path / "data")
    with pytest.raises(ConfigError) as caught:
        store.save_api_key("local", api_key, expected_record=None)  # type: ignore[arg-type]
    assert "TEST_ONLY" not in str(caught.value)
    assert str(store.data_root) not in str(caught.value)
    assert not store.data_root.exists()


def test_corrupt_or_unsupported_structure_is_not_rebuilt(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    path = root / "auth.json"
    path.write_bytes(b'{"providers": [')
    protect(path)
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        CredentialStore(root).save_api_key("local", "NEW", expected_record=None)
    assert path.read_bytes() == before

    path.write_text(json.dumps({"providers": []}))
    protect(path)
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        CredentialStore(root).save_api_key("local", "NEW", expected_record=None)
    assert path.read_bytes() == before

    record = {"type": "oauth", "token": "TEST_ONLY_TOKEN"}
    path.write_text(json.dumps({"providers": {"local": record}}))
    protect(path)
    before = path.read_bytes()
    with pytest.raises(ConfigError) as caught:
        CredentialStore(root).save_api_key("local", "NEW", expected_record=record)
    assert "TEST_ONLY" not in str(caught.value)
    assert path.read_bytes() == before


def test_null_or_non_object_credential_target_is_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    path, before = seed_auth(root, {"local": None})
    store = CredentialStore(root)
    with pytest.raises(ConfigError) as caught:
        store.save_api_key("local", "NEW", expected_record=None)
    assert "NEW" not in str(caught.value)
    assert path.read_bytes() == before
    with pytest.raises(ConfigError):
        store.delete("local", expected_record={"type": "api_key", "apiKey": "OLD"})
    assert path.read_bytes() == before

    path.write_text(json.dumps({"providers": {"local": []}}))
    protect(path)
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        store.save_api_key("local", "NEW", expected_record=None)
    assert path.read_bytes() == before


def test_replace_failure_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "data"
    path, before = seed_auth(root, {"local": {"type": "api_key", "apiKey": "OLD"}})

    def fail(*args):
        raise OSError("TEST_ONLY_SECRET")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ConfigError) as caught:
        CredentialStore(root).save_api_key(
            "local", "NEW", expected_record={"type": "api_key", "apiKey": "OLD"}
        )
    assert "TEST_ONLY_SECRET" not in str(caught.value)
    assert "NEW" not in str(caught.value)
    assert str(root) not in str(caught.value)
    assert path.read_bytes() == before
    assert not [item for item in root.iterdir() if item.suffix == ".tmp"]


def test_temp_write_failure_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    from mica_agent._credential_permissions import ProtectedTempFile

    root = tmp_path / "data"
    path, before = seed_auth(root, {"local": {"type": "api_key", "apiKey": "OLD"}})

    def fail(self, data):
        raise OSError("TEST_ONLY_SECRET")

    monkeypatch.setattr(ProtectedTempFile, "write", fail)
    with pytest.raises(ConfigError) as caught:
        CredentialStore(root).save_api_key(
            "local", "NEW", expected_record={"type": "api_key", "apiKey": "OLD"}
        )
    assert "TEST_ONLY_SECRET" not in str(caught.value)
    assert "NEW" not in str(caught.value)
    assert path.read_bytes() == before
    assert not [item for item in root.iterdir() if item.suffix == ".tmp"]


def test_failure_after_replace_reports_unconfirmed_protection(
    tmp_path: Path, monkeypatch
) -> None:
    import mica_agent.credential_store as credential_store_module

    root = tmp_path / "data"
    path, _ = seed_auth(root, {"local": {"type": "api_key", "apiKey": "OLD"}})
    real_read = credential_store_module.read_credentials
    calls: list[Path] = []

    def failing_read(target: Path) -> bytes:
        calls.append(Path(target))
        if len(calls) == 3:
            raise CredentialProtectionError("protection lost")
        return real_read(target)

    monkeypatch.setattr(credential_store_module, "read_credentials", failing_read)
    with pytest.raises(CredentialProtectionError) as caught:
        CredentialStore(root).save_api_key(
            "local", "NEW", expected_record={"type": "api_key", "apiKey": "OLD"}
        )
    assert "replaced" in str(caught.value)
    assert "NEW" not in str(caught.value)
    assert "OLD" not in str(caught.value)
    assert json.loads(real_read(path))["providers"]["local"]["apiKey"] == "NEW"


def test_protected_temp_is_verified_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    protect(root)
    ensure_credential_directory(root)
    temporary = create_protected_temp(root)
    try:
        assert temporary.protection_verified is True
        temporary.verify()
        temporary.write(b'{"providers":{}}')
        temporary.close()
        assert read_credentials(temporary.path) == b'{"providers":{}}'
    finally:
        temporary.close()
        temporary.path.unlink(missing_ok=True)


def test_unsafe_directory_is_rejected_without_repair(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    protect(root, shared=True, rights="CreateFiles")
    try:
        with pytest.raises(CredentialProtectionError):
            ensure_credential_directory(root)
        with pytest.raises(CredentialProtectionError):
            create_protected_temp(root)
        # The unsafe directory is reported again, not silently repaired.
        with pytest.raises(CredentialProtectionError):
            ensure_credential_directory(root)
        if os.name != "nt":
            assert stat.S_IMODE(root.stat().st_mode) == 0o722
    finally:
        protect(root)


def test_missing_directory_is_created_private_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    root = tmp_path / "data"
    ensure_credential_directory(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    temporary = create_protected_temp(root)
    try:
        assert stat.S_IMODE(temporary.path.stat().st_mode) == 0o600
        temporary.write(b"{}")
    finally:
        temporary.close()
        temporary.path.unlink(missing_ok=True)
