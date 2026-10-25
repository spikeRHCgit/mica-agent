"""Bounded directory discovery and selected-model merging for saved connections."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import httpx

from ._config_validation import API_IDS, CHANNELS, is_text, validate_url
from .cancellation import CancellationRequested, CancellationToken
from .config_contracts import ConfigError
from .config_store import ConfigStore
from .reasoning import ReasoningEffort

_DEADLINE_SECONDS = 30.0
_MAX_BODY_BYTES = 16 * 1024 * 1024
_MAX_ENTRIES = 10000
_USER_AGENT = "Mica/0.1 model-catalog"
_ANTHROPIC_VERSION = "2023-06-01"
_GO_CATALOG_URL = "https://opencode.ai/inference/go/v1/models"
_GO_BASE_PREFIX = "/inference/go/"

_STATUS_OK = "ok"
_STATUS_INCOMPLETE = "incomplete"
_STATUS_EMPTY = "empty"
_STATUS_MANUAL = "manual"
_STATUS_FAILED = "failed"
_RESULT_STATUSES = frozenset(
    {
        _STATUS_OK,
        _STATUS_INCOMPLETE,
        _STATUS_EMPTY,
        _STATUS_MANUAL,
        _STATUS_FAILED,
    }
)

_ERROR_AUTHENTICATION = "authentication"
_ERROR_RATE_LIMITED = "rate_limited"
_ERROR_HTTP = "http"
_ERROR_REDIRECT = "redirect"
_ERROR_NETWORK = "network"
_ERROR_TIMEOUT = "timeout"
_ERROR_TOO_LARGE = "too_large"
_ERROR_INVALID_RESPONSE = "invalid_response"
_ERRORS = frozenset(
    {
        _ERROR_AUTHENTICATION,
        _ERROR_RATE_LIMITED,
        _ERROR_HTTP,
        _ERROR_REDIRECT,
        _ERROR_NETWORK,
        _ERROR_TIMEOUT,
        _ERROR_TOO_LARGE,
        _ERROR_INVALID_RESPONSE,
    }
)

_LEVELS = {
    effort.value: effort
    for effort in ReasoningEffort
    if effort is not ReasoningEffort.OFF
}
_INPUT_FLAGS = frozenset({"text", "image"})


@dataclass(frozen=True, slots=True)
class _Directory:
    base_url: str
    catalog_url: str
    api: str
    base_paths: frozenset[str]


_DIRECTORIES = MappingProxyType(
    {
        "deepseek": _Directory(
            "https://api.deepseek.com",
            "https://api.deepseek.com/models",
            "openai-completions",
            frozenset({"", "/v1"}),
        ),
        "openrouter": _Directory(
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/models",
            "openai-completions",
            frozenset({"/api/v1"}),
        ),
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class CatalogTarget:
    """Caller-confirmed connection facts; credentials and addresses stay out of repr."""

    channel: str
    base_url: str | None = None
    api: str | None = None
    api_key: str | None = None
    catalog_url: str | None = None
    session_id: str | None = None

    def __repr__(self) -> str:
        return "CatalogTarget(<redacted>)"


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One parsed model entry; unknown metadata stays absent rather than defaulted."""

    identifier: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    input: tuple[str, ...] | None = None
    reasoning: bool | None = None
    thinking_level_map: Mapping[ReasoningEffort, str] | None = None

    def __post_init__(self) -> None:
        if not is_text(self.identifier):
            raise ValueError("catalog model identifier is invalid")
        if self.name is not None and not is_text(self.name):
            raise ValueError("catalog model name is invalid")
        for value in (self.context_window, self.max_tokens):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("catalog model budget is invalid")
        if self.input is not None:
            flags = tuple(self.input)
            if (
                not flags
                or "text" not in flags
                or any(flag not in _INPUT_FLAGS for flag in flags)
                or len(set(flags)) != len(flags)
            ):
                raise ValueError("catalog model input capabilities are invalid")
            object.__setattr__(self, "input", flags)
        if self.reasoning is not None and type(self.reasoning) is not bool:
            raise ValueError("catalog model reasoning flag is invalid")
        if self.thinking_level_map is not None:
            if not isinstance(self.thinking_level_map, Mapping):
                raise ValueError("catalog model thinking map is invalid")
            mapping: dict[ReasoningEffort, str] = {}
            for level, label in self.thinking_level_map.items():
                if (
                    not isinstance(level, ReasoningEffort)
                    or level is ReasoningEffort.OFF
                    or not is_text(label)
                ):
                    raise ValueError("catalog model thinking map is invalid")
                mapping[level] = label
            if mapping and self.reasoning is not True:
                raise ValueError("catalog model reasoning metadata is inconsistent")
            object.__setattr__(
                self,
                "thinking_level_map",
                MappingProxyType(mapping) if mapping else None,
            )


@dataclass(frozen=True, slots=True)
class CatalogResult:
    """Safe directory outcome; the error field is a fixed category, never remote text."""

    status: str
    models: tuple[CatalogModel, ...] = ()
    error: str | None = None
    invalid_metadata: int = 0

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError("catalog status is invalid")
        if not isinstance(self.models, (tuple, list)):
            raise TypeError("catalog model collection is invalid")
        models = tuple(self.models)
        if any(not isinstance(model, CatalogModel) for model in models):
            raise ValueError("catalog model collection is invalid")
        if len({model.identifier for model in models}) != len(models):
            raise ValueError("catalog model identifiers must be unique")
        if self.error is not None and self.error not in _ERRORS:
            raise ValueError("catalog error category is invalid")
        if type(self.invalid_metadata) is not int or self.invalid_metadata < 0:
            raise ValueError("catalog metadata counter is invalid")
        if self.status == _STATUS_FAILED:
            if self.error is None or models:
                raise ValueError("failed catalog result is invalid")
        elif self.error is not None:
            raise ValueError("catalog error on non-failed result is invalid")
        if self.status == _STATUS_OK and not models:
            raise ValueError("successful catalog result must contain models")
        if self.status in {_STATUS_EMPTY, _STATUS_MANUAL} and models:
            raise ValueError("empty or manual catalog result must not contain models")
        object.__setattr__(self, "models", models)


@dataclass(frozen=True, slots=True)
class _Request:
    channel: str
    url: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Parsed:
    models: tuple[CatalogModel, ...]
    invalid_metadata: int = 0
    incomplete: bool = False


class _PayloadError(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _failed(category: str) -> CatalogResult:
    return CatalogResult(_STATUS_FAILED, error=category)


async def discover(
    target: CatalogTarget,
    token: CancellationToken,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CatalogResult:
    """Fetch at most one bounded directory response and parse it by channel.

    The client owns the optional injected transport and always closes it. A
    cancellation token raises CancellationRequested; cancellation of the
    calling task propagates as CancelledError after both the request task and
    the cancellation watcher are collected.
    """
    if not isinstance(target, CatalogTarget):
        raise TypeError("target must be a CatalogTarget")
    if not isinstance(token, CancellationToken):
        raise TypeError("token must be a CancellationToken")
    token.raise_if_cancelled()
    request = _prepare(target)
    if isinstance(request, CatalogResult):
        return request
    token.raise_if_cancelled()
    return await _bounded_fetch(request, token, transport)


async def _bounded_fetch(
    request: _Request,
    token: CancellationToken,
    transport: httpx.AsyncBaseTransport | None,
) -> CatalogResult:
    operation = asyncio.create_task(_fetch(request, transport))
    cancellation = asyncio.create_task(token.wait())
    try:
        try:
            async with asyncio.timeout(_DEADLINE_SECONDS):
                await asyncio.wait(
                    (operation, cancellation),
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except TimeoutError:
            return _failed(_ERROR_TIMEOUT)
        if cancellation.done():
            raise CancellationRequested("operation cancelled")
        return operation.result()
    finally:
        for task in (operation, cancellation):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


def _prepare(target: CatalogTarget) -> _Request | CatalogResult:
    channel = target.channel
    if not isinstance(channel, str):
        raise ConfigError("catalog channel is invalid")
    if channel == "custom":
        if target.catalog_url is None or (
            isinstance(target.catalog_url, str) and not target.catalog_url.strip()
        ):
            return CatalogResult(_STATUS_MANUAL)
        return _custom_request(target)
    if channel == "opencode-go":
        return _go_request(target)
    directory = _DIRECTORIES.get(channel)
    if directory is None:
        raise ConfigError("catalog channel is not supported")
    base = _base_url(target.base_url)
    if not _matches_directory(base, directory):
        raise ConfigError("catalog target does not match the channel")
    if target.catalog_url is not None and target.catalog_url != directory.catalog_url:
        raise ConfigError("catalog target does not match the channel")
    if target.api is not None and (
        not isinstance(target.api, str) or target.api != directory.api
    ):
        raise ConfigError("catalog target does not match the channel")
    key = _required_key(target.api_key)
    return _request(
        channel,
        directory.catalog_url,
        bearer=key,
    )


def _custom_request(target: CatalogTarget) -> _Request:
    if not isinstance(target.api, str) or target.api not in API_IDS:
        raise ConfigError("catalog target does not match the channel")
    base = _base_url(target.base_url)
    catalog = httpx.URL(validate_url(target.catalog_url))
    if _origin(base) != _origin(catalog):
        raise ConfigError("catalog target does not match the channel")
    key = _required_key(target.api_key)
    if target.api == "anthropic-messages":
        return _request(
            "custom",
            str(catalog),
            anthropic_key=key,
        )
    return _request(
        "custom",
        str(catalog),
        bearer=key,
    )


def _go_request(target: CatalogTarget) -> _Request:
    base = _base_url(target.base_url)
    if not _is_go_base(base):
        raise ConfigError("catalog target does not match the channel")
    if target.api is not None and (
        not isinstance(target.api, str) or target.api not in API_IDS
    ):
        raise ConfigError("catalog target does not match the channel")
    if target.catalog_url is not None and target.catalog_url != _GO_CATALOG_URL:
        raise ConfigError("catalog target does not match the channel")
    session = _session_id(target.session_id)
    return _request(
        "opencode-go",
        _GO_CATALOG_URL,
        session=session,
    )


def _request(
    channel: str,
    url: str,
    *,
    bearer: str | None = None,
    anthropic_key: str | None = None,
    session: str | None = None,
) -> _Request:
    headers: list[tuple[str, str]] = [("User-Agent", _USER_AGENT)]
    if bearer is not None:
        headers.append(("Authorization", f"Bearer {bearer}"))
    if anthropic_key is not None:
        headers.append(("x-api-key", anthropic_key))
        headers.append(("anthropic-version", _ANTHROPIC_VERSION))
    if session is not None:
        headers.append(("x-opencode-session", session))
    checked = tuple((name, _header_value(value)) for name, value in headers)
    return _Request(channel, url, checked)


def _header_value(value: object) -> str:
    """Reject values that HTTPX cannot encode into an ASCII request header."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isprintable()
    ):
        raise ConfigError("catalog request header is invalid")
    return value


def _base_url(value: object) -> httpx.URL:
    if value is None:
        raise ConfigError("catalog target is incomplete")
    return httpx.URL(validate_url(value))


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port
    if port is None:
        port = 443 if url.scheme == "https" else 80
    return url.scheme.lower(), url.host.lower(), port


def _matches_directory(base: httpx.URL, directory: _Directory) -> bool:
    expected = httpx.URL(directory.base_url)
    return (
        base.scheme == "https"
        and base.host.lower() == expected.host.lower()
        and _origin(base)[2] == 443
        and base.path.rstrip("/") in directory.base_paths
    )


def _is_go_base(base: httpx.URL) -> bool:
    return (
        base.scheme == "https"
        and base.host.lower() == "opencode.ai"
        and _origin(base)[2] == 443
        and base.path.startswith(_GO_BASE_PREFIX)
        and base.path != _GO_BASE_PREFIX
    )


def _required_key(value: object) -> str:
    if not is_text(value):
        raise ConfigError("catalog target is incomplete")
    return value


def _session_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        not is_text(value)
        or len(value) > 256
        or value in {".", ".."}
        or any(character in value for character in "/\\:")
        or any(character.isspace() for character in value)
    ):
        raise ConfigError("catalog session identifier is invalid")
    return value


async def _fetch(
    request: _Request, transport: httpx.AsyncBaseTransport | None
) -> CatalogResult:
    try:
        return await _fetch_result(request, transport)
    except _PayloadError as error:
        return _failed(error.category)
    except httpx.TimeoutException:
        return _failed(_ERROR_TIMEOUT)
    except httpx.DecodingError:
        return _failed(_ERROR_INVALID_RESPONSE)
    except httpx.HTTPError:
        return _failed(_ERROR_NETWORK)
    except OSError:
        return _failed(_ERROR_NETWORK)


async def _fetch_result(
    request: _Request, transport: httpx.AsyncBaseTransport | None
) -> CatalogResult:
    async with (
        httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(_DEADLINE_SECONDS),
        ) as client,
        client.stream("GET", request.url, headers=dict(request.headers)) as response,
    ):
        status = response.status_code
        if status in {401, 403}:
            return _failed(_ERROR_AUTHENTICATION)
        if status == 429:
            return _failed(_ERROR_RATE_LIMITED)
        if 300 <= status < 400:
            return _failed(_ERROR_REDIRECT)
        if not 200 <= status < 300:
            return _failed(_ERROR_HTTP)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_BODY_BYTES:
                return _failed(_ERROR_TOO_LARGE)
    return _parse_response(request.channel, bytes(body))


def _parse_response(channel: str, data: bytes) -> CatalogResult:
    root = _decode(data)
    parser = _PARSERS[channel]
    parsed = parser(root)
    if not parsed.models:
        status = _STATUS_INCOMPLETE if parsed.incomplete else _STATUS_EMPTY
    elif parsed.incomplete:
        status = _STATUS_INCOMPLETE
    else:
        status = _STATUS_OK
    return CatalogResult(
        status,
        parsed.models,
        invalid_metadata=parsed.invalid_metadata,
    )


def _decode(data: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise _PayloadError(_ERROR_INVALID_RESPONSE) from None
    if not isinstance(value, dict):
        raise _PayloadError(_ERROR_INVALID_RESPONSE)
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _entries(root: Mapping[str, object]) -> list[Mapping[str, object]]:
    data = root.get("data")
    if not isinstance(data, list):
        raise _PayloadError(_ERROR_INVALID_RESPONSE)
    if len(data) > _MAX_ENTRIES:
        raise _PayloadError(_ERROR_TOO_LARGE)
    if any(not isinstance(entry, Mapping) for entry in data):
        raise _PayloadError(_ERROR_INVALID_RESPONSE)
    return data


def _identifier(entry: Mapping[str, object], seen: set[str]) -> str:
    identifier = entry.get("id")
    if not is_text(identifier):
        raise _PayloadError(_ERROR_INVALID_RESPONSE)
    if identifier in seen:
        raise _PayloadError(_ERROR_INVALID_RESPONSE)
    seen.add(identifier)
    return identifier


def _text_field(entry: Mapping[str, object], key: str) -> tuple[str | None, int]:
    value = entry.get(key)
    if value is None:
        return None, 0
    if not is_text(value):
        return None, 1
    return value, 0


def _budget_field(entry: Mapping[str, object], key: str) -> tuple[int | None, int]:
    value = entry.get(key)
    if value is None:
        return None, 0
    if type(value) is not int or value <= 0:
        return None, 1
    return value, 0


def _input_field(value: object) -> tuple[tuple[str, ...] | None, int]:
    if value is None:
        return None, 0
    if not isinstance(value, list) or any(not isinstance(flag, str) for flag in value):
        return None, 1
    flags = tuple(dict.fromkeys(flag for flag in value if flag in _INPUT_FLAGS))
    if "text" not in flags:
        return None, 1
    return flags, 0


def _effort_field(
    entry: Mapping[str, object],
) -> tuple[bool | None, Mapping[ReasoningEffort, str] | None, int]:
    effort = entry.get("effort")
    if effort is None:
        return None, None, 0
    if not isinstance(effort, Mapping):
        return None, None, 1
    levels = effort.get("supported_levels")
    default = effort.get("default_level")
    mapping: dict[ReasoningEffort, str] = {}
    if levels is not None:
        if not isinstance(levels, list) or any(
            not isinstance(level, str) for level in levels
        ):
            return None, None, 1
        mapping = {_LEVELS[level]: level for level in levels if level in _LEVELS}
    if not mapping and is_text(default) and default in _LEVELS:
        mapping = {_LEVELS[default]: default}
    if not mapping:
        return None, None, 0
    return True, MappingProxyType(mapping), 0


def _nested_budget(
    entry: Mapping[str, object], container_key: str, key: str
) -> tuple[int | None, int]:
    container = entry.get(container_key)
    if container is None:
        return None, 0
    if not isinstance(container, Mapping):
        return None, 1
    return _budget_field(container, key)


def _nested_input(
    entry: Mapping[str, object], container_key: str, key: str
) -> tuple[tuple[str, ...] | None, int]:
    container = entry.get(container_key)
    if container is None:
        return None, 0
    if not isinstance(container, Mapping):
        return None, 1
    return _input_field(container.get(key))


def _has_next(root: Mapping[str, object]) -> bool:
    links = root.get("links")
    if links is None:
        return False
    if not isinstance(links, Mapping):
        return True
    return links.get("next") not in (None, "")


def _parse_deepseek(root: Mapping[str, object]) -> _Parsed:
    models: list[CatalogModel] = []
    seen: set[str] = set()
    invalid = 0
    for entry in _entries(root):
        identifier = _identifier(entry, seen)
        name, bad = _text_field(entry, "name")
        invalid += bad
        context, bad = _budget_field(entry, "context_window")
        invalid += bad
        max_tokens, bad = _budget_field(entry, "max_output_tokens")
        invalid += bad
        input_flags, bad = _input_field(entry.get("input_modalities"))
        invalid += bad
        reasoning, thinking_map, bad = _effort_field(entry)
        invalid += bad
        models.append(
            CatalogModel(
                identifier,
                name,
                context,
                max_tokens,
                input_flags,
                reasoning,
                thinking_map,
            )
        )
    return _Parsed(tuple(models), invalid)


def _parse_openrouter(root: Mapping[str, object]) -> _Parsed:
    models: list[CatalogModel] = []
    seen: set[str] = set()
    invalid = 0
    for entry in _entries(root):
        identifier = _identifier(entry, seen)
        name, bad = _text_field(entry, "name")
        invalid += bad
        context, bad = _budget_field(entry, "context_length")
        invalid += bad
        max_tokens, bad = _nested_budget(entry, "top_provider", "max_completion_tokens")
        invalid += bad
        input_flags, bad = _nested_input(entry, "architecture", "input_modalities")
        invalid += bad
        models.append(
            CatalogModel(
                identifier,
                name,
                context,
                max_tokens,
                input_flags,
            )
        )
    return _Parsed(tuple(models), invalid, _has_next(root))


def _parse_minimal(root: Mapping[str, object]) -> _Parsed:
    models: list[CatalogModel] = []
    seen: set[str] = set()
    invalid = 0
    for entry in _entries(root):
        identifier = _identifier(entry, seen)
        name, bad = _text_field(entry, "name")
        invalid += bad
        models.append(CatalogModel(identifier, name))
    return _Parsed(tuple(models), invalid)


_PARSERS = MappingProxyType(
    {
        "deepseek": _parse_deepseek,
        "openrouter": _parse_openrouter,
        "opencode-go": _parse_minimal,
        "custom": _parse_minimal,
    }
)


def save_selection(
    store: ConfigStore,
    provider_id: str,
    expected_provider: Mapping[str, object],
    catalog: CatalogResult,
    selected_ids: Collection[str],
    *,
    api_choices: Mapping[str, str] | None = None,
) -> None:
    """Merge explicit selections into one existing provider record.

    The expected_provider mapping is the complete record read before discovery;
    the store compares it under its lock so a concurrent edit, disable, or
    delete rejects the save instead of recreating or overwriting a connection.
    """
    if not isinstance(store, ConfigStore):
        raise TypeError("store must be a ConfigStore")
    if not isinstance(provider_id, str):
        raise TypeError("provider identifier must be text")
    if not isinstance(expected_provider, Mapping):
        raise TypeError("expected provider must be a mapping")
    if not isinstance(catalog, CatalogResult):
        raise TypeError("catalog must be a CatalogResult")
    if isinstance(selected_ids, (str, bytes)) or not isinstance(
        selected_ids, Collection
    ):
        raise TypeError("selected identifiers must be a collection")
    if api_choices is not None and not isinstance(api_choices, Mapping):
        raise TypeError("api choices must be a mapping")
    selected: set[str] = set()
    for identifier in selected_ids:
        if not isinstance(identifier, str) or not is_text(identifier):
            raise TypeError("selected identifiers must be text")
        selected.add(identifier)
    if catalog.status not in {_STATUS_OK, _STATUS_INCOMPLETE} or not catalog.models:
        raise ConfigError("catalog result cannot be saved")
    channel = expected_provider.get("channel", "custom")
    if (
        not isinstance(channel, str)
        or channel not in CHANNELS
        or channel == "chatgpt-subscription"
    ):
        raise ConfigError("catalog provider is invalid")
    provider_api = expected_provider.get("api")
    if provider_api is not None and (
        not isinstance(provider_api, str) or provider_api not in API_IDS
    ):
        raise ConfigError("catalog provider is invalid")
    existing = _existing_models(expected_provider)
    existing_ids = {entry["id"] for entry in existing}
    catalog_by_id = {model.identifier: model for model in catalog.models}
    if selected - existing_ids - set(catalog_by_id):
        raise ConfigError("selected catalog model is unavailable")
    merged: list[dict[str, object]] = []
    for entry in existing:
        identifier = entry["id"]
        if identifier not in selected:
            continue
        updated = dict(entry)
        model = catalog_by_id.get(identifier)
        if model is not None:
            _fill_missing(updated, model)
        merged.append(updated)
    for model in catalog.models:
        if model.identifier not in selected or model.identifier in existing_ids:
            continue
        api = _new_api(model.identifier, channel, provider_api, api_choices)
        merged.append(_new_entry(model, api))
    store.update_provider(
        provider_id,
        {"models": merged},
        expected_value=expected_provider,
    )


def _existing_models(
    expected_provider: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_models = expected_provider.get("models", [])
    if not isinstance(raw_models, list):
        raise ConfigError("provider model list is invalid")
    result: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for item in raw_models:
        if not isinstance(item, Mapping):
            raise ConfigError("provider model list is invalid")
        identifier = item.get("id")
        if not is_text(identifier) or identifier in identifiers:
            raise ConfigError("provider model list is invalid")
        identifiers.add(identifier)
        result.append(dict(item))
    return result


def _new_api(
    identifier: str,
    channel: str,
    provider_api: str | None,
    api_choices: Mapping[str, str] | None,
) -> str:
    if api_choices is not None and identifier in api_choices:
        choice = api_choices[identifier]
        if not isinstance(choice, str) or choice not in API_IDS:
            raise ConfigError("catalog model protocol is invalid")
        return choice
    directory = _DIRECTORIES.get(channel)
    if directory is not None:
        return directory.api
    if channel == "opencode-go":
        raise ConfigError("explicit model protocol is required")
    if provider_api is not None:
        return provider_api
    raise ConfigError("explicit model protocol is required")


def _model_fields(model: CatalogModel) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = []
    if model.name is not None:
        fields.append(("name", model.name))
    if model.context_window is not None:
        fields.append(("contextWindow", model.context_window))
    if model.max_tokens is not None:
        fields.append(("maxTokens", model.max_tokens))
    if model.input is not None:
        fields.append(("input", list(model.input)))
    if model.reasoning is not None:
        fields.append(("reasoning", model.reasoning))
    if model.thinking_level_map is not None:
        fields.append(
            (
                "thinkingLevelMap",
                {
                    level.value: label
                    for level, label in model.thinking_level_map.items()
                },
            )
        )
    return fields


def _fill_missing(entry: dict[str, object], model: CatalogModel) -> None:
    for key, value in _model_fields(model):
        if key not in entry:
            entry[key] = value


def _new_entry(model: CatalogModel, api: str) -> dict[str, object]:
    entry: dict[str, object] = {"id": model.identifier, "api": api}
    for key, value in _model_fields(model):
        entry[key] = value
    return entry
