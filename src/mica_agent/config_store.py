"""Configuration JSON reading and one compare-and-save operation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ._file_lock import locked_file
from .config_contracts import ConfigConflictError, ConfigError
from .session_contracts import SessionSelection

_MAX_BYTES = 4 * 1024 * 1024


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
        if selection.provider_id.startswith(("mica-env:", "mica-direct:")):
            raise ConfigError("temporary connections cannot be saved as defaults")
        path = self.data_root / "config.json"
        temporary: str | None = None
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
                payload = (
                    json.dumps(current, ensure_ascii=False, indent=2, allow_nan=False)
                    + "\n"
                ).encode("utf-8")
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
            raise ConfigError(
                "configuration save failed; original file retained"
            ) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
