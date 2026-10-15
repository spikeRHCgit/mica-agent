"""The single parser, source resolver and default-model save entry point."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping

import httpx

from .config_contracts import (
    ConfigError,
    ConfigSnapshot,
    ConnectionBinding,
    CredentialProtectionError,
    EffectiveSettings,
    ModelCapabilities,
    ModelConfig,
    ProviderConfig,
    SelectionResolution,
)
from .config_store import ConfigStore
from .permissions import PermissionMode
from .providers.anthropic_messages import (
    DEFAULT_MAX_TOKENS as MESSAGES_MAX_TOKENS,
)
from .providers.anthropic_messages import (
    DEFAULT_THINKING_LEVEL_MAP as MESSAGES_THINKING_LEVEL_MAP,
)
from .providers.openai_compatible import (
    DEFAULT_MAX_TOKENS as COMPLETIONS_MAX_TOKENS,
)
from .providers.openai_compatible import (
    DEFAULT_THINKING_LEVEL_MAP as COMPLETIONS_THINKING_LEVEL_MAP,
)
from .providers.openai_compatible import (
    resolve_endpoint,
)
from .providers.openai_responses import (
    DEFAULT_MAX_TOKENS as RESPONSES_MAX_TOKENS,
)
from .providers.openai_responses import (
    DEFAULT_THINKING_LEVEL_MAP as RESPONSES_THINKING_LEVEL_MAP,
)
from .reasoning import ReasoningDisplayMode, ReasoningEffort
from .session_contracts import SessionSelection

_API_DEFAULTS = {
    "openai-completions": (COMPLETIONS_MAX_TOKENS, COMPLETIONS_THINKING_LEVEL_MAP),
    "openai-responses": (RESPONSES_MAX_TOKENS, RESPONSES_THINKING_LEVEL_MAP),
    "anthropic-messages": (MESSAGES_MAX_TOKENS, MESSAGES_THINKING_LEVEL_MAP),
}
_CHANNELS = {"custom", "chatgpt-subscription", "openrouter", "deepseek", "opencode-go"}
_SETTINGS = {
    "reasoningEffort": (ReasoningEffort, ReasoningEffort.OFF),
    "permissionMode": (PermissionMode, PermissionMode.DEFAULT),
    "reasoningDisplay": (ReasoningDisplayMode, ReasoningDisplayMode.COLLAPSED),
}
_RESERVED = ("mica-env:", "mica-direct:")
_SECRET_KEYS = {
    "apikey",
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "authorization",
    "secret",
}


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def _url(value: object) -> str:
    if not _text(value):
        raise ConfigError("connection address is invalid")
    try:
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError()
        return str(url)
    except (ValueError, httpx.InvalidURL):
        raise ConfigError("connection address is invalid") from None


def _has_secrets(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            any(part in str(k).lower() for part in _SECRET_KEYS) or _has_secrets(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_secrets(v) for v in value)
    return False


class ConfigService:
    def __init__(
        self,
        store: ConfigStore,
        *,
        environment: Mapping[str, str] | None = None,
        permission_override: PermissionMode | None = None,
    ):
        self.store = store
        self._environment = dict(os.environ if environment is None else environment)
        self._permission_override = permission_override
        self._connections: dict[SessionSelection, ConnectionBinding] = {}

    def load(self) -> ConfigSnapshot:
        global_settings = self.store.read("config")
        workspace_settings = self.store.read("config", workspace=True)
        models = self.store.read("models")
        warnings: list[str] = []
        try:
            auth = self.store.read("auth")
        except CredentialProtectionError:
            auth = {}
            warnings.append(
                "credential protection could not be verified; authentication source disabled; protect authentication file and directory, then restart"
            )
        if _has_secrets(workspace_settings):
            warnings.append(
                "workspace credentials are ignored; use the protected authentication file"
            )
        for settings in (global_settings, workspace_settings):
            self._unknown(settings, set(_SETTINGS) | {"defaultModel"}, warnings)
        values, sources = {}, {}
        for key, (enum, default) in _SETTINGS.items():
            value, source = default, "program default"
            for raw, label in (
                (global_settings, "global"),
                (workspace_settings, "workspace"),
            ):
                if key in raw:
                    try:
                        value = enum(raw[key]) if isinstance(raw[key], str) else default
                        if not isinstance(raw[key], str):
                            raise TypeError()
                        source = label
                    except (ValueError, TypeError):
                        value, source = default, "invalid field: program default"
                        warnings.append("invalid shared setting; program default used")
            values[key], sources[key] = value, source
        if self._permission_override is not None:
            values["permissionMode"], sources["permissionMode"] = (
                self._permission_override,
                "CLI",
            )
        default_model, default_source = None, "unconfigured"
        for raw, label in (
            (global_settings, "global default"),
            (workspace_settings, "workspace default"),
        ):
            if "defaultModel" in raw:
                default_source = label
                target = raw["defaultModel"]
                default_model = None
                if (
                    isinstance(target, dict)
                    and _text(target.get("provider"))
                    and _text(target.get("model"))
                ):
                    self._unknown(target, {"provider", "model"}, warnings)
                    default_model = SessionSelection(
                        target["provider"], target["model"]
                    )
                else:
                    warnings.append("invalid default model; no default selected")
        settings = EffectiveSettings(
            values["reasoningEffort"],
            values["permissionMode"],
            values["reasoningDisplay"],
            sources,
        )
        connections: dict[SessionSelection, ConnectionBinding] = {}
        providers = self._parse_directory(models, auth, connections, warnings)
        snapshot = ConfigSnapshot(
            settings, default_model, default_source, providers, tuple(warnings)
        )
        self._connections = connections
        return snapshot

    @staticmethod
    def _unknown(
        raw: Mapping[str, object], allowed: set[str], warnings: list[str]
    ) -> None:
        if set(raw) - allowed:
            warnings.append("unknown configuration fields ignored")

    def _capabilities(
        self, raw: dict[str, object], warnings: list[str]
    ) -> ModelCapabilities:
        sources: dict[str, str] = {}

        def integer(key: str, default: int | None) -> int | None:
            value = raw.get(key, default)
            if (
                value is not None
                and (type(value) is not int or value <= 0)
                or key in raw
                and value is None
            ):
                warnings.append("invalid model budget; bounded default used")
                value = default
                sources[key] = "program default"
            else:
                sources[key] = "explicit" if key in raw else "program default"
            return value

        context = integer("contextWindow", 200000)
        budget = integer("maxTokens", None)
        if budget is None:
            sources["maxTokens"] = "adapter default"
        input_value = raw.get("input", ["text"])
        if (
            not isinstance(input_value, list)
            or not input_value
            or not all(
                isinstance(v, str) and v in {"text", "image"} for v in input_value
            )
            or "text" not in input_value
            or len(set(input_value)) != len(input_value)
        ):
            input_value = ["text"]
            warnings.append("invalid input capability; text default used")
        reasoning = raw.get("reasoning", False)
        if type(reasoning) is not bool:
            reasoning = False
            warnings.append("invalid reasoning capability; disabled")
        mapping = raw.get("thinkingLevelMap", {})
        parsed: dict[ReasoningEffort, str] = {}
        if not isinstance(mapping, dict):
            warnings.append("invalid thinking map; adapter default used")
        else:
            invalid = False
            for key, value in mapping.items():
                if key not in {
                    e.value for e in ReasoningEffort if e is not ReasoningEffort.OFF
                }:
                    warnings.append("unknown thinking level ignored")
                    continue
                if not _text(value):
                    invalid = True
                else:
                    parsed[ReasoningEffort(key)] = value
            if invalid:
                parsed = {}
                warnings.append("invalid thinking map; adapter default used")
        sources.update(
            reasoning="explicit" if "reasoning" in raw else "program default",
            input="explicit" if "input" in raw else "program default",
            thinkingLevelMap="explicit" if parsed else "adapter default",
        )
        return ModelCapabilities(
            context, budget, tuple(input_value), reasoning, parsed, sources
        )

    def _parse_directory(
        self, directory, auth, connections, warnings
    ) -> tuple[ProviderConfig, ...]:
        self._unknown(directory, {"providers"}, warnings)
        self._unknown(auth, {"providers"}, warnings)
        entries = directory.get("providers", {})
        credentials = auth.get("providers", {})
        if not isinstance(entries, dict):
            warnings.append("invalid provider directory; no connections available")
            entries = {}
        if not isinstance(credentials, dict):
            warnings.append("invalid credential directory; no credentials available")
            credentials = {}
        result = []
        for provider_id, raw in entries.items():
            if (
                not _text(provider_id)
                or provider_id.startswith(_RESERVED)
                or not isinstance(raw, dict)
            ):
                warnings.append("invalid provider entry ignored")
                continue
            self._unknown(
                raw,
                {"name", "channel", "enabled", "baseUrl", "api", "models"},
                warnings,
            )
            enabled = raw.get("enabled", True)
            if type(enabled) is not bool:
                enabled = False
                warnings.append("invalid enabled field; connection disabled")
            channel = raw.get("channel", "custom")
            if not isinstance(channel, str) or channel not in _CHANNELS:
                channel = "custom"
                warnings.append("invalid channel; custom default used")
            api = raw.get("api", "openai-completions")
            if not isinstance(api, str) or api not in _API_DEFAULTS:
                warnings.append("invalid provider protocol; connection unavailable")
                continue
            try:
                address = _url(raw.get("baseUrl"))
            except ConfigError:
                warnings.append("invalid connection address; connection unavailable")
                continue
            key_record = credentials.get(provider_id, {})
            key = None
            if isinstance(key_record, dict) and key_record.get("type") == "api_key":
                self._unknown(key_record, {"type", "apiKey"}, warnings)
                if _text(key_record.get("apiKey")):
                    key = key_record["apiKey"]
            if key is None:
                warnings.append("credential unavailable for a saved connection")
            name = raw.get("name", provider_id)
            if not _text(name):
                name = provider_id
                warnings.append("invalid provider name; identifier used")
            model_entries = raw.get("models", [])
            if not isinstance(model_entries, list):
                warnings.append("invalid model list; no models available")
                model_entries = []
            ids = [
                v.get("id")
                for v in model_entries
                if isinstance(v, dict) and _text(v.get("id"))
            ]
            parsed = []
            for model_raw in model_entries:
                if (
                    not isinstance(model_raw, dict)
                    or not _text(model_raw.get("id"))
                    or ids.count(model_raw.get("id")) != 1
                ):
                    warnings.append("invalid or duplicate model identifier ignored")
                    continue
                self._unknown(
                    model_raw,
                    {
                        "id",
                        "name",
                        "api",
                        "contextWindow",
                        "maxTokens",
                        "input",
                        "reasoning",
                        "thinkingLevelMap",
                    },
                    warnings,
                )
                model_api = model_raw.get("api", api)
                if not isinstance(model_api, str) or model_api not in _API_DEFAULTS:
                    warnings.append("invalid model protocol; model unavailable")
                    continue
                model_name = model_raw.get("name", model_raw["id"])
                if not _text(model_name):
                    model_name = model_raw["id"]
                    warnings.append("invalid model name; identifier used")
                selection = SessionSelection(provider_id, model_raw["id"])
                model = ModelConfig(
                    selection,
                    model_name,
                    model_api,
                    self._capabilities(model_raw, warnings),
                )
                parsed.append(model)
                if enabled and key is not None:
                    connections[selection] = ConnectionBinding(
                        selection, address, key, model, "saved directory"
                    )
            result.append(
                ProviderConfig(provider_id, name, channel, enabled, api, tuple(parsed))
            )
        return tuple(result)

    @staticmethod
    def direct_connection(
        api_url: str, api_key: str, model: str, *, environment: bool = False
    ) -> ConnectionBinding:
        address = _url(api_url)
        if not _text(api_key) or not _text(model):
            raise ConfigError("temporary connection fields are invalid")
        endpoint = str(httpx.URL(resolve_endpoint(address)))
        namespace = "mica-env" if environment else "mica-direct"
        digest = hashlib.sha256(
            json.dumps(
                [namespace + "-v1", "openai-completions", endpoint],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        selection = SessionSelection(namespace + ":" + digest, model)
        capabilities = ModelCapabilities()
        config = ModelConfig(selection, model, "openai-completions", capabilities)
        return ConnectionBinding(
            selection,
            address,
            api_key,
            config,
            "environment" if environment else "direct",
        )

    def resolve(
        self,
        snapshot: ConfigSnapshot,
        *,
        explicit: SessionSelection | None = None,
        saved: SessionSelection | None = None,
    ) -> SelectionResolution:
        values = [
            self._environment.get(k, "").strip()
            for k in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL")
        ]
        temporary = None
        if all(values):
            try:
                temporary = self.direct_connection(*values, environment=True)
            except ConfigError:
                if explicit is None and saved is None:
                    return SelectionResolution(
                        None,
                        "environment",
                        "unresolved",
                        diagnostic="temporary connection fields are invalid",
                    )
        target, source = (
            (explicit, "CLI")
            if explicit is not None
            else (saved, "session")
            if saved is not None
            else (temporary.selection, "environment")
            if temporary is not None
            else (snapshot.default_model, snapshot.default_source)
        )
        if target is None:
            return SelectionResolution(
                None,
                source,
                "unconfigured",
                diagnostic="No model configured. Configure models.json, auth.json and config.json, then restart.",
            )
        connection = (
            temporary
            if temporary is not None and target == temporary.selection
            else self._connections.get(target)
        )
        if connection is None:
            return SelectionResolution(
                target,
                source,
                "unresolved",
                diagnostic="Selected connection is unavailable; repair its configuration or provide an explicit CLI selection.",
            )
        return SelectionResolution(target, source, "ready", connection)

    def for_run(
        self, selection: SessionSelection, *, current: ConnectionBinding | None = None
    ):
        snapshot = self.load()
        resolution = self.resolve(snapshot, explicit=selection)
        temporary = (
            current
            if current is not None
            and current.selection.provider_id.startswith(_RESERVED)
            else None
        )
        connection = temporary if temporary is not None else resolution.connection
        if connection is None:
            raise ConfigError(
                "selected connection is unavailable; repair configuration before submitting"
            )
        if (
            current is not None
            and temporary is None
            and (
                current.api_url != connection.api_url
                or current.api_key != connection.api_key
            )
        ):
            raise ConfigError("connection changed on disk; restart before submitting")
        capabilities = connection.model.capabilities
        default_budget, default_mapping = _API_DEFAULTS[connection.model.api]
        budget = (
            capabilities.max_tokens
            if capabilities.max_tokens is not None
            else default_budget
        )
        if (
            capabilities.sources.get("contextWindow") == "explicit"
            and capabilities.context_window <= budget
        ):
            raise ConfigError(
                "output budget conflicts with the explicit context capacity"
            )
        mapping = capabilities.thinking_level_map or default_mapping
        effort = snapshot.settings.reasoning_effort
        if not capabilities.reasoning or effort not in mapping:
            effort = ReasoningEffort.OFF
        return (
            snapshot.settings,
            capabilities,
            budget,
            mapping if capabilities.reasoning else {},
            effort,
        )

    def save_default_model(
        self, selection: SessionSelection, expected_value: SessionSelection | None
    ) -> None:
        snapshot = self.load()
        if self.resolve(snapshot, explicit=selection).status != "ready":
            raise ConfigError(
                "default model must reference an available saved connection"
            )
        self.store.save_default_model(selection, expected_value)
