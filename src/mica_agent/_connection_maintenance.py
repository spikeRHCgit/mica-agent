"""Connection maintenance orchestration over configuration and credential stores."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import httpx

from ._config_validation import (
    API_IDS,
    CHANNELS,
    RESERVED_PREFIXES,
    is_text,
    validate_url,
)
from ._file_lock import locked_file
from .cancellation import CancellationToken
from .config_contracts import (
    ConfigConflictError,
    ConfigError,
    CredentialProtectionError,
)
from .config_store import ConfigStore, _validate_provider_record
from .credential_store import CredentialStore, _api_key
from .model_catalog import (
    CatalogResult,
    CatalogTarget,
    connection_defaults,
    discover,
    save_selection,
)
from .reasoning import ReasoningEffort

_COMPLETE = "complete"
_PARTIAL = "partial"
_CONFLICT = "conflict"
_UNCONFIRMED = "unconfirmed"
_NOT_CONFIRMED = "not_confirmed"
_FAILED = "failed"
_STATUSES = frozenset(
    {_COMPLETE, _PARTIAL, _CONFLICT, _UNCONFIRMED, _NOT_CONFIRMED, _FAILED}
)

CREDENTIAL_AVAILABLE = "available"
CREDENTIAL_MISSING = "missing"
CREDENTIAL_UNSUPPORTED = "unsupported"
_CREDENTIAL_STATES = frozenset(
    {CREDENTIAL_AVAILABLE, CREDENTIAL_MISSING, CREDENTIAL_UNSUPPORTED}
)

SCOPE_MODELS = "models"
SCOPE_CREDENTIAL = "credential"
_SCOPES = frozenset({SCOPE_MODELS, SCOPE_CREDENTIAL})

_CHANNEL_NAMES = MappingProxyType(
    {
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "opencode-go": "OpenCode Go",
    }
)
_MAX_ID_ATTEMPTS = 5
_CONNECTION_UNAVAILABLE = "connection is not available; refresh the connection list"
_CONFLICT_HINT = (
    "connection or credential changed after it was read; refresh the "
    "connection list before retrying"
)
_PARTIAL_MODELS_HINT = (
    "connection record was not saved; the saved credential remains unused"
)
_PARTIAL_CREDENTIAL_HINT = (
    "connection record was removed; credential deletion did not complete"
)
_KEY_SAVED_MODELS_CHANGED_HINT = (
    "API key was saved, but the connection changed during the save; refresh "
    "the connection list before continuing"
)
_KEY_SAVED_MODELS_UNKNOWN_HINT = (
    "API key was saved, but the connection state could not be checked; refresh "
    "the connection list before continuing"
)
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """Safe connection facts; addresses and credentials never travel here."""

    provider_id: str
    name: str
    channel: str
    enabled: bool
    credential: str

    def __post_init__(self) -> None:
        if not is_text(self.provider_id):
            raise ValueError("connection identifier is invalid")
        if not is_text(self.name):
            raise ValueError("connection name is invalid")
        if self.channel not in CHANNELS:
            raise ValueError("connection channel is invalid")
        if type(self.enabled) is not bool:
            raise ValueError("connection enabled flag is invalid")
        if self.credential not in _CREDENTIAL_STATES:
            raise ValueError("connection credential state is invalid")


@dataclass(frozen=True, slots=True)
class MaintenanceOutcome:
    """Fixed result category plus confirmed and unconfirmed scopes.

    completed contains only scopes whose commit is confirmed. remaining
    contains scopes that are not done or, for an unconfirmed result, could not
    be confirmed as done or undone.
    """

    status: str
    connection: ConnectionSummary | None = None
    completed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("maintenance status is invalid")
        for scopes in (self.completed, self.remaining):
            if any(scope not in _SCOPES for scope in scopes):
                raise ValueError("maintenance scope is invalid")
        if self.hint is not None and not isinstance(self.hint, str):
            raise ValueError("maintenance hint is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ConnectionEdit:
    """Short-lived edit handle; baselines stay private and deeply immutable."""

    provider_id: str
    name: str
    channel: str
    enabled: bool
    custom_url: str | None
    custom_api: str | None
    _expected_provider: Mapping[str, object] = field(repr=False)
    _expected_credential: Mapping[str, object] | None = field(repr=False)

    def __repr__(self) -> str:
        return "ConnectionEdit(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class CatalogDiscovery:
    """Catalog outcome tied to the edit baselines that produced it."""

    provider_id: str
    result: CatalogResult
    _expected_provider: Mapping[str, object] = field(repr=False)
    _expected_credential: Mapping[str, object] | None = field(repr=False)

    def __repr__(self) -> str:
        return "CatalogDiscovery(<redacted>)"


@dataclass(frozen=True, slots=True)
class ManualModel:
    """Explicit user-entered model; absent capabilities stay absent."""

    identifier: str
    api: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    input: tuple[str, ...] | None = None
    reasoning: bool | None = None
    thinking_level_map: Mapping[ReasoningEffort, str] | None = None


class ConnectionMaintenance:
    """Single maintenance implementation used by the internal ConfigService entry."""

    def __init__(self, store: ConfigStore) -> None:
        if not isinstance(store, ConfigStore):
            raise TypeError("store must be a ConfigStore")
        self._store = store
        self._credentials = CredentialStore(store.data_root)

    def list_connections(self) -> tuple[ConnectionSummary, ...]:
        """List saved connections without reading or exposing any credential value."""
        models = self._store.read("models")
        auth = self._store.read("auth")
        return self._summaries(models, auth)

    def begin_edit(self, provider_id: str) -> ConnectionEdit:
        """Capture complete models and credential targets for one connection."""
        provider_id = _target_id(provider_id)
        models = self._store.read("models")
        auth = self._store.read("auth")
        entries = _provider_entries(models)
        if provider_id not in entries:
            raise ConfigError(_CONNECTION_UNAVAILABLE)
        record = entries[provider_id]
        if not isinstance(record, Mapping):
            raise ConfigError("connection entry is invalid")
        credential = _credential_target(_credential_entries(auth), provider_id)
        raw_channel = record.get("channel", "custom")
        channel = (
            raw_channel
            if isinstance(raw_channel, str) and raw_channel in CHANNELS
            else "custom"
        )
        name = record.get("name")
        if not is_text(name):
            name = provider_id
        enabled = record.get("enabled", True)
        if type(enabled) is not bool:
            enabled = False
        base_url = record.get("baseUrl")
        api = record.get("api")
        custom = raw_channel == "custom"
        return ConnectionEdit(
            provider_id,
            name,
            channel,
            enabled,
            base_url if custom and is_text(base_url) else None,
            api if custom and isinstance(api, str) and api in API_IDS else None,
            _freeze_mapping(record),
            None if credential is None else _freeze_mapping(credential),
        )

    def create_connection(
        self,
        channel: str,
        *,
        api_key: str,
        name: str | None = None,
        base_url: str | None = None,
        api: str | None = None,
    ) -> MaintenanceOutcome:
        """Create a connection: validate first, save the credential, then models."""
        if (
            not isinstance(channel, str)
            or channel not in CHANNELS
            or channel == "chatgpt-subscription"
        ):
            raise ConfigError("connection channel is invalid")
        key = _api_key(api_key)
        record = _new_provider(channel, name, base_url, api)
        _validate_provider_record(record)
        provider_id = self._new_id()
        try:
            self._credentials.save_api_key(provider_id, key, expected_record=None)
        except CredentialProtectionError as error:
            # Protection can fail before the replace, so the exception type
            # alone cannot confirm that the credential was committed.
            return MaintenanceOutcome(
                _UNCONFIRMED,
                remaining=(SCOPE_CREDENTIAL, SCOPE_MODELS),
                hint=str(error),
            )
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_CREDENTIAL, SCOPE_MODELS),
                hint="credential identifier was used concurrently; no connection was saved",
            )
        except ConfigError as error:
            return MaintenanceOutcome(
                _FAILED,
                remaining=(SCOPE_CREDENTIAL, SCOPE_MODELS),
                hint=str(error),
            )
        try:
            self._store.update_provider(provider_id, record, expected_value=None)
        except ConfigError as error:
            return MaintenanceOutcome(
                _PARTIAL,
                completed=(SCOPE_CREDENTIAL,),
                remaining=(SCOPE_MODELS,),
                hint=_PARTIAL_MODELS_HINT
                if isinstance(error, ConfigConflictError)
                else str(error),
            )
        summary = self._summary(provider_id, record, CREDENTIAL_AVAILABLE)
        hint = (
            None
            if record["models"]
            else "connection saved without models; choose catalog models or "
            "enter one manually before use"
        )
        return MaintenanceOutcome(
            _COMPLETE,
            summary,
            completed=(SCOPE_MODELS, SCOPE_CREDENTIAL),
            hint=hint,
        )

    def rename(self, edit: ConnectionEdit, name: str) -> MaintenanceOutcome:
        """Change only the display name; the stable identifier is retained."""
        _require_edit(edit)
        if not is_text(name):
            raise ConfigError("connection name is invalid")
        return self._update_models(edit, {"name": name})

    def set_enabled(self, edit: ConnectionEdit, enabled: bool) -> MaintenanceOutcome:
        """Toggle selection availability while preserving models and credentials."""
        _require_edit(edit)
        if type(enabled) is not bool:
            raise TypeError("enabled must be boolean")
        return self._update_models(edit, {"enabled": enabled})

    def update_custom_address(
        self,
        edit: ConnectionEdit,
        *,
        base_url: str | None = None,
        api: str | None = None,
    ) -> MaintenanceOutcome:
        """Change a custom connection's address or default protocol only."""
        _require_edit(edit)
        if edit._expected_provider.get("channel") != "custom":
            raise ConfigError(
                "built-in connection address and protocol cannot be changed"
            )
        changes: dict[str, object] = {}
        if base_url is not None:
            changes["baseUrl"] = validate_url(base_url)
        if api is not None:
            if not isinstance(api, str) or api not in API_IDS:
                raise ConfigError("connection protocol is invalid")
            changes["api"] = api
        if not changes:
            raise ConfigError("custom connection change is empty")
        return self._update_models(edit, changes)

    def change_api_key(self, edit: ConnectionEdit, api_key: str) -> MaintenanceOutcome:
        """Replace one API key only after validating it and the current baseline."""
        _require_edit(edit)
        key = _api_key(api_key)
        try:
            self._require_current_models(edit)
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_CREDENTIAL,),
                hint=_CONFLICT_HINT,
            )
        record = edit._expected_credential
        if record is not None and record.get("type") != "api_key":
            raise ConfigError("connection credential type cannot be changed")
        try:
            self._credentials.save_api_key(
                edit.provider_id,
                key,
                expected_record=None if record is None else _thaw(record),
            )
        except CredentialProtectionError as error:
            # The replace may or may not have happened; do not claim either
            # state. The protection hint still reports the unconfirmed risk.
            return MaintenanceOutcome(
                _UNCONFIRMED,
                remaining=(SCOPE_CREDENTIAL,),
                hint=str(error),
            )
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_CREDENTIAL,),
                hint="credential changed after it was read; refresh before retrying",
            )
        except ConfigError as error:
            return MaintenanceOutcome(
                _FAILED,
                remaining=(SCOPE_CREDENTIAL,),
                hint=str(error),
            )
        changed = self._models_after_credential(edit)
        if changed is not None:
            return changed
        return MaintenanceOutcome(
            _COMPLETE,
            self._summary(
                edit.provider_id,
                edit._expected_provider,
                CREDENTIAL_AVAILABLE,
            ),
            completed=(SCOPE_CREDENTIAL,),
        )

    def save_manual_model(
        self, edit: ConnectionEdit, model: ManualModel
    ) -> MaintenanceOutcome:
        """Save one explicitly entered model without inventing capabilities."""
        _require_edit(edit)
        entry = _manual_entry(model)
        entries = _baseline_models(edit)
        existing = next((item for item in entries if item["id"] == entry["id"]), None)
        if existing is None:
            entries.append(entry)
        else:
            existing.update(entry)
        record = dict(_thaw(edit._expected_provider))
        record["models"] = entries
        _validate_provider_record(record)
        return self._update_models(edit, {"models": entries})

    def delete(self, edit: ConnectionEdit, *, confirmed: bool) -> MaintenanceOutcome:
        """Remove models first, then the credential, reporting partial commits."""
        _require_edit(edit)
        if type(confirmed) is not bool:
            raise TypeError("confirmed must be boolean")
        if not confirmed:
            return MaintenanceOutcome(
                _NOT_CONFIRMED,
                remaining=(SCOPE_MODELS, SCOPE_CREDENTIAL),
                hint="deletion requires explicit confirmation; no file was changed",
            )
        try:
            self._store.delete_provider(
                edit.provider_id, expected_value=_thaw(edit._expected_provider)
            )
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_MODELS, SCOPE_CREDENTIAL),
                hint=_CONFLICT_HINT,
            )
        except ConfigError as error:
            return MaintenanceOutcome(
                _FAILED,
                remaining=(SCOPE_MODELS, SCOPE_CREDENTIAL),
                hint=str(error),
            )
        record = edit._expected_credential
        if record is None:
            return MaintenanceOutcome(_COMPLETE, completed=(SCOPE_MODELS,))
        try:
            self._credentials.delete(edit.provider_id, expected_record=_thaw(record))
        except CredentialProtectionError as error:
            return MaintenanceOutcome(
                _UNCONFIRMED,
                completed=(SCOPE_MODELS,),
                remaining=(SCOPE_CREDENTIAL,),
                hint=str(error),
            )
        except ConfigConflictError:
            return MaintenanceOutcome(
                _PARTIAL,
                completed=(SCOPE_MODELS,),
                remaining=(SCOPE_CREDENTIAL,),
                hint=_PARTIAL_CREDENTIAL_HINT,
            )
        except ConfigError as error:
            return MaintenanceOutcome(
                _PARTIAL,
                completed=(SCOPE_MODELS,),
                remaining=(SCOPE_CREDENTIAL,),
                hint=str(error),
            )
        return MaintenanceOutcome(_COMPLETE, completed=(SCOPE_MODELS, SCOPE_CREDENTIAL))

    async def discover_catalog(
        self,
        edit: ConnectionEdit,
        token: CancellationToken,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        catalog_url: str | None = None,
        session_id: str | None = None,
    ) -> CatalogDiscovery:
        """Call bounded discovery with the edit's protected key and facts.

        The network wait holds no configuration or credential lock; the
        returned discovery remains bound to this edit's baselines.
        """
        _require_edit(edit)
        record = edit._expected_provider
        channel = record.get("channel", "custom")
        if not isinstance(channel, str):
            raise ConfigError("connection channel is invalid")
        api = record.get("api")
        if api is not None and (not isinstance(api, str) or api not in API_IDS):
            raise ConfigError("connection protocol is invalid")
        key = _catalog_key(edit, channel)
        target = CatalogTarget(
            channel=channel,
            base_url=record.get("baseUrl"),
            api=api,
            api_key=key,
            catalog_url=catalog_url,
            session_id=session_id,
        )
        result = await discover(target, token, transport=transport)
        return CatalogDiscovery(
            edit.provider_id,
            result,
            edit._expected_provider,
            edit._expected_credential,
        )

    def save_catalog_selection(
        self,
        discovery: CatalogDiscovery,
        selected_ids: Collection[str],
        *,
        api_choices: Mapping[str, str] | None = None,
    ) -> MaintenanceOutcome:
        """Reject stale credential baselines, then merge the explicit selection."""
        if not isinstance(discovery, CatalogDiscovery):
            raise TypeError("discovery must be a CatalogDiscovery")
        auth_path = self._credentials.path
        # Hold only the existing auth lock for the protected re-read and the
        # synchronous models CAS. This path never calls CredentialStore, so it
        # cannot recurse into the non-reentrant auth lock; no network await
        # happens here and the lock order stays auth then models.
        try:
            with locked_file(auth_path.with_suffix(".json.lock")):
                self._require_current_credential(discovery)
                save_selection(
                    self._store,
                    discovery.provider_id,
                    _thaw(discovery._expected_provider),
                    discovery.result,
                    selected_ids,
                    api_choices=api_choices,
                )
        except TimeoutError:
            raise ConfigError(
                "credential check failed; retry after the other save finishes"
            ) from None
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_MODELS,),
                hint=_CONFLICT_HINT,
            )
        credential = (
            CREDENTIAL_MISSING
            if discovery._expected_credential is None
            else _credential_state(discovery._expected_credential)
        )
        return MaintenanceOutcome(
            _COMPLETE,
            self._summary(
                discovery.provider_id,
                discovery._expected_provider,
                credential,
            ),
            completed=(SCOPE_MODELS,),
        )

    def _require_current_models(self, edit: ConnectionEdit) -> None:
        entries = _provider_entries(self._store.read("models"))
        current = entries.get(edit.provider_id, _ABSENT)
        if current != _thaw(edit._expected_provider):
            raise ConfigConflictError(
                "connection changed after it was read; refresh before retrying"
            )

    def _require_current_credential(self, discovery: CatalogDiscovery) -> None:
        entries = _credential_entries(self._store.read("auth"))
        if discovery.provider_id in entries:
            record = entries[discovery.provider_id]
            if not isinstance(record, Mapping):
                raise ConfigError("credential entry is invalid")
            current: Mapping[str, object] | None = record
        else:
            current = None
        if current != _thaw(discovery._expected_credential):
            raise ConfigConflictError(
                "credential changed after the catalog result was captured"
            )

    def _models_after_credential(
        self, edit: ConnectionEdit
    ) -> MaintenanceOutcome | None:
        """Reject a stale connection after the credential has been committed."""
        try:
            self._require_current_models(edit)
        except ConfigConflictError:
            return MaintenanceOutcome(
                _PARTIAL,
                completed=(SCOPE_CREDENTIAL,),
                remaining=(SCOPE_MODELS,),
                hint=_KEY_SAVED_MODELS_CHANGED_HINT,
            )
        except ConfigError:
            return MaintenanceOutcome(
                _UNCONFIRMED,
                completed=(SCOPE_CREDENTIAL,),
                remaining=(SCOPE_MODELS,),
                hint=_KEY_SAVED_MODELS_UNKNOWN_HINT,
            )
        return None

    def _update_models(
        self, edit: ConnectionEdit, changes: Mapping[str, object]
    ) -> MaintenanceOutcome:
        try:
            self._store.update_provider(
                edit.provider_id,
                changes,
                expected_value=_thaw(edit._expected_provider),
            )
        except ConfigConflictError:
            return MaintenanceOutcome(
                _CONFLICT,
                remaining=(SCOPE_MODELS,),
                hint=_CONFLICT_HINT,
            )
        except ConfigError as error:
            return MaintenanceOutcome(
                _FAILED,
                remaining=(SCOPE_MODELS,),
                hint=str(error),
            )
        record = dict(_thaw(edit._expected_provider))
        record.update(changes)
        return MaintenanceOutcome(
            _COMPLETE,
            self._summary(edit.provider_id, record, _edit_credential_state(edit)),
            completed=(SCOPE_MODELS,),
        )

    def _new_id(self) -> str:
        models = _provider_entries(self._store.read("models"))
        credentials = _credential_entries(self._store.read("auth"))
        for _ in range(_MAX_ID_ATTEMPTS):
            candidate = uuid.uuid4().hex
            if candidate not in models and candidate not in credentials:
                return candidate
        raise ConfigError("connection identifier cannot be allocated")

    def _summaries(
        self,
        models: Mapping[str, object],
        auth: Mapping[str, object],
    ) -> tuple[ConnectionSummary, ...]:
        entries = _provider_entries(models)
        credentials = _credential_entries(auth)
        summaries: list[ConnectionSummary] = []
        for provider_id in sorted(entries):
            if not is_text(provider_id) or provider_id.startswith(RESERVED_PREFIXES):
                continue
            record = entries[provider_id]
            if not isinstance(record, Mapping):
                raise ConfigError("connection entry is invalid")
            state = _credential_state(credentials.get(provider_id, _ABSENT))
            summaries.append(self._summary(provider_id, record, state))
        return tuple(summaries)

    @staticmethod
    def _summary(
        provider_id: str,
        record: Mapping[str, object],
        credential: str,
    ) -> ConnectionSummary:
        name = record.get("name")
        if not is_text(name):
            name = provider_id
        channel = record.get("channel", "custom")
        if not isinstance(channel, str) or channel not in CHANNELS:
            channel = "custom"
        enabled = record.get("enabled", True)
        if type(enabled) is not bool:
            enabled = False
        return ConnectionSummary(provider_id, name, channel, enabled, credential)


def _new_provider(
    channel: str,
    name: str | None,
    base_url: str | None,
    api: str | None,
) -> dict[str, object]:
    if channel == "custom":
        if base_url is None or api is None:
            raise ConfigError("custom connection fields are required")
        if name is None or not is_text(name):
            raise ConfigError("connection name is invalid")
        if not isinstance(api, str) or api not in API_IDS:
            raise ConfigError("connection protocol is invalid")
        return {
            "name": name,
            "channel": channel,
            "enabled": True,
            "baseUrl": validate_url(base_url),
            "api": api,
            "models": [],
        }
    if base_url is not None or api is not None:
        raise ConfigError("built-in connection defaults cannot be overridden")
    defaults = connection_defaults(channel)
    if defaults is None:
        raise ConfigError("connection channel is invalid")
    if name is None:
        name = _CHANNEL_NAMES[channel]
    elif not is_text(name):
        raise ConfigError("connection name is invalid")
    default_base, default_api = defaults
    return {
        "name": name,
        "channel": channel,
        "enabled": True,
        "baseUrl": default_base,
        "api": default_api,
        "models": [],
    }


def _manual_entry(model: ManualModel) -> dict[str, object]:
    if not isinstance(model, ManualModel):
        raise TypeError("model must be a ManualModel")
    if not is_text(model.identifier):
        raise ConfigError("model identifier is invalid")
    if not isinstance(model.api, str) or model.api not in API_IDS:
        raise ConfigError("model protocol is invalid")
    entry: dict[str, object] = {"id": model.identifier, "api": model.api}
    if model.name is not None:
        if not is_text(model.name):
            raise ConfigError("model name is invalid")
        entry["name"] = model.name
    for key, value in (
        ("contextWindow", model.context_window),
        ("maxTokens", model.max_tokens),
    ):
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ConfigError("model budget is invalid")
            entry[key] = value
    if model.input is not None:
        flags = tuple(model.input) if isinstance(model.input, (tuple, list)) else ()
        if (
            not flags
            or "text" not in flags
            or any(flag not in {"text", "image"} for flag in flags)
            or len(set(flags)) != len(flags)
        ):
            raise ConfigError("model input capabilities are invalid")
        entry["input"] = list(flags)
    if model.reasoning is not None:
        if type(model.reasoning) is not bool:
            raise ConfigError("model reasoning flag is invalid")
        entry["reasoning"] = model.reasoning
    if model.thinking_level_map is not None:
        if not isinstance(model.thinking_level_map, Mapping):
            raise ConfigError("model thinking map is invalid")
        mapping: dict[str, str] = {}
        for level, label in model.thinking_level_map.items():
            if (
                not isinstance(level, ReasoningEffort)
                or level is ReasoningEffort.OFF
                or not is_text(label)
            ):
                raise ConfigError("model thinking map is invalid")
            mapping[level.value] = label
        if not mapping:
            raise ConfigError("model thinking map is invalid")
        if model.reasoning is not True:
            raise ConfigError("model thinking map requires reasoning")
        entry["thinkingLevelMap"] = mapping
    return entry


def _baseline_models(edit: ConnectionEdit) -> list[dict[str, object]]:
    raw = _thaw(edit._expected_provider.get("models", []))
    if not isinstance(raw, list):
        raise ConfigError("provider model list is invalid")
    entries: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or not is_text(item.get("id")):
            raise ConfigError("provider model list is invalid")
        identifier = item["id"]
        if identifier in identifiers:
            raise ConfigError("provider model identifiers must be unique")
        identifiers.add(identifier)
        entries.append(dict(item))
    return entries


def _catalog_key(edit: ConnectionEdit, channel: str) -> str | None:
    # The public Go directory needs no credential, so no stored credential or
    # other authentication type is read or consumed for it.
    if channel == "opencode-go":
        return None
    record = edit._expected_credential
    if record is None:
        raise ConfigError("connection credential is unavailable")
    if record.get("type") != "api_key":
        raise ConfigError("connection credential is not an API key")
    key = record.get("apiKey")
    if not is_text(key):
        raise ConfigError("connection credential is unavailable")
    return key


def _credential_target(
    entries: Mapping[str, object], provider_id: str
) -> dict[str, object] | None:
    if provider_id not in entries:
        return None
    record = entries[provider_id]
    if not isinstance(record, Mapping):
        raise ConfigError("credential entry is invalid")
    return dict(record)


def _credential_state(record: object) -> str:
    if record is _ABSENT:
        return CREDENTIAL_MISSING
    if not isinstance(record, Mapping):
        raise ConfigError("credential entry is invalid")
    if record.get("type") == "api_key" and is_text(record.get("apiKey")):
        return CREDENTIAL_AVAILABLE
    return CREDENTIAL_UNSUPPORTED


def _edit_credential_state(edit: ConnectionEdit) -> str:
    record = edit._expected_credential
    if record is None:
        return CREDENTIAL_MISSING
    return _credential_state(record)


def _provider_entries(document: Mapping[str, object]) -> dict[str, object]:
    entries = document.get("providers", {})
    if not isinstance(entries, dict):
        raise ConfigError("provider directory is invalid")
    return entries


def _credential_entries(document: Mapping[str, object]) -> dict[str, object]:
    entries = document.get("providers", {})
    if not isinstance(entries, dict):
        raise ConfigError("credential directory is invalid")
    return entries


def _target_id(value: object) -> str:
    if not isinstance(value, str) or not is_text(value) or value != value.strip():
        raise ConfigError("connection identifier is invalid")
    if value.startswith(RESERVED_PREFIXES):
        raise ConfigError("connection identifier is invalid")
    return value


def _require_edit(edit: object) -> None:
    if not isinstance(edit, ConnectionEdit):
        raise TypeError("edit must be a ConnectionEdit")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: object) -> object:
    """Copy a JSON baseline into a deeply immutable representation."""
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    """Return a fresh JSON-native copy for an existing store comparison."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value
