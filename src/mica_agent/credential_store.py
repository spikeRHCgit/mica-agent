"""Protected API-key persistence for the authentication file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ._config_validation import RESERVED_PREFIXES, is_text
from ._credential_permissions import (
    create_protected_temp,
    ensure_credential_directory,
    read_credentials,
)
from ._file_lock import locked_file
from .config_contracts import (
    ConfigConflictError,
    ConfigError,
    CredentialProtectionError,
)
from .config_store import parse_json, serialize_json

_MAX_KEY_LENGTH = 16 * 1024
_COMMITTED_MESSAGE = (
    "credentials were replaced but protection could not be confirmed; protect "
    "authentication file and directory, then restart"
)


def _provider_id(value: object) -> str:
    if not isinstance(value, str) or not is_text(value) or value != value.strip():
        raise ConfigError("provider identifier is invalid")
    if value.startswith(RESERVED_PREFIXES):
        raise ConfigError("temporary connection identifiers cannot be saved")
    return value


def _api_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_KEY_LENGTH
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ConfigError("API key is invalid")
    return value


def _credential_entries(document: Mapping[str, object]) -> dict[str, object]:
    entries = document.get("providers", {})
    if not isinstance(entries, dict):
        raise ConfigError("credential directory is invalid")
    return dict(entries)


class CredentialStore:
    """Own auth.json changes; raw keys never appear in errors or repr."""

    def __init__(self, data_root: Path | str):
        self.data_root = Path(data_root)

    @property
    def path(self) -> Path:
        return self.data_root / "auth.json"

    def save_api_key(
        self,
        provider_id: str,
        api_key: str,
        *,
        expected_record: Mapping[str, object] | None,
    ) -> None:
        """Create or replace one API-key record, preserving unrelated fields."""
        provider_id = _provider_id(provider_id)
        api_key = _api_key(api_key)
        if expected_record is not None and not isinstance(expected_record, Mapping):
            raise TypeError("expected_record must be a mapping or None")
        self._write(
            provider_id, expected_record, {"type": "api_key", "apiKey": api_key}
        )

    def delete(
        self, provider_id: str, *, expected_record: Mapping[str, object]
    ) -> None:
        """Remove one credential record without changing other files."""
        if not isinstance(expected_record, Mapping):
            raise TypeError("expected_record must be a mapping")
        provider_id = _provider_id(provider_id)
        self._write(provider_id, expected_record, None)

    def _write(
        self,
        provider_id: str,
        expected_record: Mapping[str, object] | None,
        replacement: Mapping[str, object] | None,
    ) -> None:
        path = self.path
        temporary = None
        committed = False
        try:
            ensure_credential_directory(self.data_root)
            with locked_file(path.with_suffix(".json.lock")):
                current = self._read_existing(path)
                entries = _credential_entries(current)
                actual: Mapping[str, object] | None
                if provider_id in entries:
                    candidate = entries[provider_id]
                    if not isinstance(candidate, Mapping):
                        raise ConfigError("credential entry is invalid")
                    actual = candidate
                    if actual != expected_record:
                        raise ConfigConflictError(
                            "credential entry changed; read it again before saving"
                        )
                else:
                    if expected_record is not None:
                        raise ConfigConflictError(
                            "credential entry changed; read it again before saving"
                        )
                    actual = None
                if replacement is None:
                    del entries[provider_id]
                else:
                    if actual is not None and actual.get("type") != "api_key":
                        raise ConfigError("credential entry type cannot be changed")
                    record = dict(actual) if actual is not None else {}
                    record.update(replacement)
                    entries[provider_id] = record
                current["providers"] = entries
                payload = serialize_json(current)
                temporary = create_protected_temp(self.data_root)
                try:
                    temporary.write(payload)
                finally:
                    temporary.close()
                if read_credentials(temporary.path) != payload:
                    raise ConfigError("credential write verification failed")
                os.replace(temporary.path, path)
                temporary = None
                committed = True
                try:
                    parse_json(read_credentials(path))
                except ConfigError:
                    raise CredentialProtectionError(_COMMITTED_MESSAGE) from None
        except ConfigError:
            raise
        except (OSError, TimeoutError, ValueError):
            if committed:
                raise CredentialProtectionError(_COMMITTED_MESSAGE) from None
            raise ConfigError(
                "credential save failed; original file retained"
            ) from None
        finally:
            if temporary is not None:
                temporary.close()
                try:
                    os.unlink(temporary.path)
                except OSError:
                    pass

    @staticmethod
    def _read_existing(path: Path) -> dict[str, object]:
        try:
            data = read_credentials(path)
        except FileNotFoundError:
            return {}
        return parse_json(data)
