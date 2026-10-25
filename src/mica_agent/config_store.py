"""Configuration JSON reading and atomic target-entry saves."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ._config_validation import (
    API_IDS,
    CHANNELS,
    RESERVED_PREFIXES,
    is_text,
    validate_url,
)
from ._file_lock import locked_file
from .config_contracts import ConfigConflictError, ConfigError
from .reasoning import ReasoningEffort
from .session_contracts import SessionSelection

_MAX_BYTES = 4 * 1024 * 1024
_PROVIDER_FIELDS = frozenset({"name", "channel", "enabled", "baseUrl", "api", "models"})
_MODEL_BUDGETS = ("contextWindow", "maxTokens")
_THINKING_LEVELS = frozenset(
    level.value for level in ReasoningEffort if level is not ReasoningEffort.OFF
)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("configuration contains duplicate fields")
        result[key] = value
    return result


def parse_json(data: bytes) -> dict[str, object]:
    if len(data) > _MAX_BYTES:
        raise ConfigError("configuration file is too large")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError):
        raise ConfigError("configuration JSON is invalid; repair the file") from None
    if not isinstance(value, dict):
        raise ConfigError("configuration root must be an object")
    return value


def _reject_non_string_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError("configuration content contains invalid keys")
            _reject_non_string_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_string_keys(item)


def serialize_json(document: Mapping[str, object]) -> bytes:
    """Encode a document that strict parse_json reads back without coercion."""
    try:
        _reject_non_string_keys(document)
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
    except ConfigError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise ConfigError("configuration content is invalid") from None
    if len(payload) > _MAX_BYTES:
        raise ConfigError("configuration content is too large")
    parse_json(payload)
    return payload


def _write_json_atomic(path: Path, document: Mapping[str, object]) -> None:
    """Replace only non-credential JSON; callers must not pass credentials here."""
    payload = serialize_json(document)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".config-", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except ConfigError:
        raise
    except (OSError, TimeoutError, ValueError):
        raise ConfigError("configuration save failed; original file retained") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _provider_id(value: object) -> str:
    if not isinstance(value, str) or not is_text(value):
        raise ConfigError("provider identifier is invalid")
    if value.startswith(RESERVED_PREFIXES):
        raise ConfigError("reserved provider identifiers cannot be saved")
    return value


def _validate_models(value: object) -> None:
    if not isinstance(value, list):
        raise ConfigError("provider model list is invalid")
    identifiers: set[str] = set()
    for item in value:
        if (
            not isinstance(item, Mapping)
            or any(not isinstance(key, str) for key in item)
            or not is_text(item.get("id"))
        ):
            raise ConfigError("provider model entry is invalid")
        identifier = item["id"]
        if identifier in identifiers:
            raise ConfigError("provider model identifiers must be unique")
        identifiers.add(identifier)
        if "name" in item and not is_text(item["name"]):
            raise ConfigError("provider model name is invalid")
        if "api" in item and (
            not isinstance(item["api"], str) or item["api"] not in API_IDS
        ):
            raise ConfigError("provider model protocol is invalid")
        for key in _MODEL_BUDGETS:
            if key in item and (type(item[key]) is not int or item[key] <= 0):
                raise ConfigError("provider model budget is invalid")
        if "input" in item:
            flags = item["input"]
            if (
                not isinstance(flags, list)
                or not flags
                or "text" not in flags
                or any(
                    not isinstance(flag, str) or flag not in {"text", "image"}
                    for flag in flags
                )
                or len(set(flags)) != len(flags)
            ):
                raise ConfigError("provider model input capabilities are invalid")
        if "reasoning" in item and type(item["reasoning"]) is not bool:
            raise ConfigError("provider model reasoning flag must be boolean")
        if "thinkingLevelMap" in item:
            mapping = item["thinkingLevelMap"]
            if not isinstance(mapping, Mapping) or any(
                not isinstance(level, str)
                or level not in _THINKING_LEVELS
                or not is_text(label)
                for level, label in mapping.items()
            ):
                raise ConfigError("provider model thinking map is invalid")


def _validate_provider_record(record: Mapping[str, object]) -> None:
    if "name" in record and not is_text(record["name"]):
        raise ConfigError("provider name is invalid")
    if "channel" in record and (
        not isinstance(record["channel"], str) or record["channel"] not in CHANNELS
    ):
        raise ConfigError("provider channel is invalid")
    if "enabled" in record and type(record["enabled"]) is not bool:
        raise ConfigError("provider enabled flag must be boolean")
    if "api" in record and (
        not isinstance(record["api"], str) or record["api"] not in API_IDS
    ):
        raise ConfigError("provider protocol is invalid")
    validate_url(record.get("baseUrl"))
    if "models" in record:
        _validate_models(record["models"])


def _provider_entries(document: Mapping[str, object]) -> dict[str, object]:
    entries = document.get("providers", {})
    if not isinstance(entries, dict):
        raise ConfigError("provider directory is invalid")
    return dict(entries)


def _provider_target(
    entries: Mapping[str, object], provider_id: str
) -> tuple[bool, Mapping[str, object] | None]:
    """Distinguish a missing key from an existing record that is not an object."""
    if provider_id not in entries:
        return False, None
    record = entries[provider_id]
    if not isinstance(record, Mapping):
        raise ConfigError("provider entry is invalid")
    return True, record


class ConfigStore:
    """Own raw JSON; snapshots never contain it."""

    def __init__(self, data_root: Path | str, workspace: Path | str):
        self.data_root = Path(data_root)
        self.workspace = Path(workspace)

    def read(self, name: str, *, workspace: bool = False) -> dict[str, object]:
        if name not in {"config", "models", "auth"} or (workspace and name != "config"):
            raise ValueError("unsupported configuration file")
        path = (
            self.workspace / ".mica" if workspace else self.data_root
        ) / f"{name}.json"
        try:
            if name == "auth":
                from ._credential_permissions import read_credentials

                data = read_credentials(path)
            else:
                data = path.read_bytes()
        except FileNotFoundError:
            return {}
        except ConfigError:
            raise
        except OSError:
            raise ConfigError(
                "configuration file cannot be read; check access"
            ) from None
        return parse_json(data)

    def save_default_model(
        self, selection: SessionSelection, expected_value: SessionSelection | None
    ) -> None:
        if not isinstance(selection, SessionSelection):
            raise TypeError("selection must be a SessionSelection")
        if selection.provider_id.startswith(RESERVED_PREFIXES):
            raise ConfigError("temporary connections cannot be saved as defaults")
        path = self.data_root / "config.json"
        try:
            with locked_file(path.with_suffix(".json.lock")):
                current = self.read("config")
                target = current.get("defaultModel")
                expected = (
                    None
                    if expected_value is None
                    else {
                        "provider": expected_value.provider_id,
                        "model": expected_value.model_id,
                    }
                )
                # Compare the known fields while preserving unknown nested fields.
                actual = (
                    {k: target.get(k) for k in ("provider", "model")}
                    if isinstance(target, dict)
                    else target
                )
                if actual != expected:
                    raise ConfigConflictError(
                        "default model changed; read it again before saving"
                    )
                updated = dict(target) if isinstance(target, dict) else {}
                updated.update(provider=selection.provider_id, model=selection.model_id)
                current["defaultModel"] = updated
                _write_json_atomic(path, current)
        except ConfigError:
            raise
        except (OSError, TimeoutError, ValueError):
            raise ConfigError(
                "configuration save failed; original file retained"
            ) from None

    def update_provider(
        self,
        provider_id: str,
        changes: Mapping[str, object],
        *,
        expected_value: Mapping[str, object] | None,
    ) -> None:
        """Create or update one provider entry, preserving unrelated records."""
        if not isinstance(changes, Mapping):
            raise TypeError("changes must be a mapping")
        if expected_value is not None and not isinstance(expected_value, Mapping):
            raise TypeError("expected_value must be a mapping or None")
        provider_id = _provider_id(provider_id)
        unknown = set(changes) - _PROVIDER_FIELDS
        if unknown:
            raise ConfigError("provider field is not supported")
        path = self.data_root / "models.json"
        try:
            with locked_file(path.with_suffix(".json.lock")):
                current = self.read("models")
                entries = _provider_entries(current)
                present, actual = _provider_target(entries, provider_id)
                if present:
                    if actual != expected_value:
                        raise ConfigConflictError(
                            "provider entry changed; read it again before saving"
                        )
                    record = dict(actual)
                else:
                    if expected_value is not None:
                        raise ConfigConflictError(
                            "provider entry changed; read it again before saving"
                        )
                    record = {}
                record.update(changes)
                _validate_provider_record(record)
                entries[provider_id] = record
                current["providers"] = entries
                _write_json_atomic(path, current)
        except ConfigError:
            raise
        except (OSError, TimeoutError, ValueError):
            raise ConfigError("provider save failed; original file retained") from None

    def delete_provider(
        self, provider_id: str, *, expected_value: Mapping[str, object]
    ) -> None:
        """Remove one provider entry without touching defaults or other records."""
        if not isinstance(expected_value, Mapping):
            raise TypeError("expected_value must be a mapping")
        provider_id = _provider_id(provider_id)
        path = self.data_root / "models.json"
        try:
            with locked_file(path.with_suffix(".json.lock")):
                current = self.read("models")
                entries = _provider_entries(current)
                present, actual = _provider_target(entries, provider_id)
                if not present or actual != expected_value:
                    raise ConfigConflictError(
                        "provider entry changed; read it again before saving"
                    )
                del entries[provider_id]
                current["providers"] = entries
                _write_json_atomic(path, current)
        except ConfigError:
            raise
        except (OSError, TimeoutError, ValueError):
            raise ConfigError(
                "provider delete failed; original file retained"
            ) from None
