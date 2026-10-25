"""Low-level configuration field rules shared by storage and the read service."""

from __future__ import annotations

from .config_contracts import ConfigError

API_IDS = frozenset({"openai-completions", "openai-responses", "anthropic-messages"})
CHANNELS = frozenset(
    {"custom", "chatgpt-subscription", "openrouter", "deepseek", "opencode-go"}
)
RESERVED_PREFIXES = ("mica-env:", "mica-direct:")


def is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def validate_url(value: object) -> str:
    if not is_text(value):
        raise ConfigError("connection address is invalid")
    # Keep lightweight configuration imports usable before runtime dependencies load.
    import httpx

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
