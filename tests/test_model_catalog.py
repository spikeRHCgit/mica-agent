"""模型目录发现、解析、取消收口与勾选合并的隔离测试。"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest
from sse_transport_helpers import RecordingTransport
from test_credential_permissions import protect

from mica_agent import model_catalog
from mica_agent.cancellation import CancellationRequested, CancellationToken
from mica_agent.config_contracts import ConfigConflictError, ConfigError
from mica_agent.config_service import ConfigService
from mica_agent.config_store import ConfigStore
from mica_agent.model_catalog import (
    CatalogModel,
    CatalogResult,
    CatalogTarget,
    discover,
    save_selection,
)
from mica_agent.reasoning import ReasoningEffort

DEEPSEEK_TARGET = CatalogTarget(
    "deepseek",
    base_url="https://api.deepseek.com",
    api_key="TEST_ONLY_KEY",
)
OPENROUTER_TARGET = CatalogTarget(
    "openrouter",
    base_url="https://openrouter.ai/api/v1",
    api_key="TEST_ONLY_KEY",
)
GO_TARGET = CatalogTarget(
    "opencode-go",
    base_url="https://opencode.ai/inference/go/openai/v1",
    api_key="TEST_ONLY_KEY",
    session_id="session-123",
)
CUSTOM_TARGET = CatalogTarget(
    "custom",
    base_url="https://custom.test/v1",
    api="openai-completions",
    api_key="TEST_ONLY_KEY",
    catalog_url="https://custom.test/v1/models",
)

DEEPSEEK_PAYLOAD = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-chat",
            "object": "model",
            "owned_by": "deepseek",
            "name": "DeepSeek Chat",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "input_modalities": ["text", "image", "audio"],
            "effort": {
                "supported_levels": ["low", "medium", "high"],
                "default_level": "medium",
            },
            "future": {"ignored": True},
        },
        {
            "id": "deepseek-reasoner",
            "object": "model",
            "owned_by": "deepseek",
            "name": "DeepSeek Reasoner",
            "context_window": 65536,
            "max_output_tokens": 4096,
            "input_modalities": ["text"],
            "effort": {"supported_levels": ["low"], "default_level": "low"},
        },
    ],
}

OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "or-chat",
            "name": "OR Chat",
            "context_length": 200000,
            "top_provider": {
                "max_completion_tokens": 16000,
                "context_length": 999999,
            },
            "architecture": {
                "input_modalities": ["text", "image", "file"],
                "output_modalities": ["text"],
            },
            "supported_parameters": ["temperature", "reasoning"],
            "pricing": {"prompt": "0.1"},
        },
        {
            "id": "or-basic",
            "name": "OR Basic",
            "context_length": 8000,
            "top_provider": {"max_completion_tokens": None},
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": [],
        },
    ],
    "total_count": 2,
    "links": {"next": "https://openrouter.ai/api/v1/models?offset=2&limit=2"},
}

GO_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "go-alpha", "object": "model", "created": 1, "owned_by": "opencode"},
        {"id": "go-beta", "object": "model", "created": 2, "owned_by": "opencode"},
    ],
}

CUSTOM_PAYLOAD = {
    "data": [
        {
            "id": "custom-a",
            "name": "Custom A",
            "context_length": 99999,
            "future": {"ignored": True},
        }
    ]
}

_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "opencode-go": "https://opencode.ai/inference/go/openai/v1",
    "custom": "https://custom.test/v1",
}


def _response(
    payload: object = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode("utf-8") if content is None else content
    merged = {"content-type": "application/json"}
    if headers:
        merged.update(headers)
    return httpx.Response(status_code=status, headers=merged, content=body)


def _handler(
    payload: object = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
):
    async def handle(request: httpx.Request) -> httpx.Response:
        return _response(payload, status=status, headers=headers, content=content)

    return handle


def _pending_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    ]


def test_target_repr_hides_credentials_addresses_and_session() -> None:
    text = repr(
        CatalogTarget(
            "custom",
            base_url="https://private.test/v1",
            api="openai-completions",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://private.test/v1/models",
            session_id="session-123",
        )
    )
    assert "redacted" in text
    assert "TEST_ONLY_KEY" not in text
    assert "private.test" not in text
    assert "session-123" not in text


def test_deepseek_request_and_explicit_metadata_mapping() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler(DEEPSEEK_PAYLOAD))
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "ok"
        assert result.invalid_metadata == 0
        assert [model.identifier for model in result.models] == [
            "deepseek-chat",
            "deepseek-reasoner",
        ]
        chat = result.models[0]
        assert chat.name == "DeepSeek Chat"
        assert chat.context_window == 128000
        assert chat.max_tokens == 8192
        assert chat.input == ("text", "image")
        assert chat.reasoning is True
        assert chat.thinking_level_map == {
            ReasoningEffort.LOW: "low",
            ReasoningEffort.MEDIUM: "medium",
            ReasoningEffort.HIGH: "high",
        }
        reasoner = result.models[1]
        assert reasoner.input == ("text",)
        assert reasoner.thinking_level_map == {ReasoningEffort.LOW: "low"}
        assert len(transport.requests) == 1
        request = transport.requests[0]
        assert request.method == "GET"
        assert str(request.url) == "https://api.deepseek.com/models"
        assert request.headers["authorization"] == "Bearer TEST_ONLY_KEY"
        assert request.headers["user-agent"].startswith("Mica/")
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_deepseek_v1_base_keeps_the_fixed_directory_url() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler({"data": []}))
        target = CatalogTarget(
            "deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key="TEST_ONLY_KEY",
        )
        result = await discover(target, CancellationToken(), transport=transport)
        assert result.status == "empty"
        assert str(transport.requests[0].url) == "https://api.deepseek.com/models"

    asyncio.run(scenario())


def test_openrouter_metadata_incomplete_next_and_no_pagination() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler(OPENROUTER_PAYLOAD))
        result = await discover(
            OPENROUTER_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "incomplete"
        assert result.invalid_metadata == 0
        assert [model.identifier for model in result.models] == ["or-chat", "or-basic"]
        chat = result.models[0]
        assert chat.name == "OR Chat"
        assert chat.context_window == 200000
        assert chat.max_tokens == 16000
        assert chat.input == ("text", "image")
        assert chat.reasoning is None
        assert chat.thinking_level_map is None
        assert result.models[1].max_tokens is None
        request = transport.requests[0]
        assert str(request.url) == "https://openrouter.ai/api/v1/models"
        assert not request.url.params
        assert request.headers["authorization"] == "Bearer TEST_ONLY_KEY"
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_openrouter_complete_result_when_next_is_absent() -> None:
    async def scenario() -> None:
        payload = {**OPENROUTER_PAYLOAD, "links": {"next": None}}
        transport = RecordingTransport(_handler(payload))
        result = await discover(
            OPENROUTER_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "ok"
        assert len(result.models) == 2

    asyncio.run(scenario())


def test_openrouter_empty_page_with_next_is_incomplete_not_empty() -> None:
    async def scenario() -> None:
        payload = {"data": [], "links": {"next": "https://example.test/page"}}
        transport = RecordingTransport(_handler(payload))
        result = await discover(
            OPENROUTER_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "incomplete"
        assert result.models == ()

    asyncio.run(scenario())


def test_opencode_go_is_public_and_sends_mica_session_without_key() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler(GO_PAYLOAD))
        result = await discover(GO_TARGET, CancellationToken(), transport=transport)
        assert result.status == "ok"
        assert [model.identifier for model in result.models] == ["go-alpha", "go-beta"]
        assert all(model.name is None for model in result.models)
        assert all(model.context_window is None for model in result.models)
        assert all(model.reasoning is None for model in result.models)
        request = transport.requests[0]
        assert str(request.url) == "https://opencode.ai/inference/go/v1/models"
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-api-key") is None
        assert "TEST_ONLY_KEY" not in str(request.headers)
        assert request.headers["x-opencode-session"] == "session-123"
        assert request.headers["user-agent"].startswith("Mica/")
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        CatalogTarget(
            "opencode-go", base_url="https://example.test/inference/go/openai/v1"
        ),
        CatalogTarget(
            "opencode-go",
            base_url="https://opencode.ai/inference/go/openai/v1",
            session_id="path/name",
        ),
        CatalogTarget(
            "opencode-go",
            base_url="https://opencode.ai/inference/go/openai/v1",
            session_id="space id",
        ),
        CatalogTarget(
            "opencode-go",
            base_url="https://opencode.ai/inference/go/openai/v1",
            catalog_url="https://opencode.ai/other/models",
        ),
    ],
)
def test_go_target_mismatch_is_rejected_before_request(
    target: CatalogTarget,
) -> None:
    transport = RecordingTransport(_handler(GO_PAYLOAD))
    with pytest.raises(ConfigError):
        asyncio.run(discover(target, CancellationToken(), transport=transport))
    assert transport.requests == []
    assert transport.aclose_count == 0


def test_custom_without_runtime_url_requires_manual_entry() -> None:
    async def scenario() -> None:
        target = CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="openai-completions",
            api_key="TEST_ONLY_KEY",
        )
        transport = RecordingTransport(_handler(CUSTOM_PAYLOAD))
        result = await discover(target, CancellationToken(), transport=transport)
        assert result.status == "manual"
        assert result.models == ()
        assert transport.requests == []
        assert transport.aclose_count == 0

    asyncio.run(scenario())


def test_custom_same_origin_directory_uses_minimal_shape() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler(CUSTOM_PAYLOAD))
        result = await discover(CUSTOM_TARGET, CancellationToken(), transport=transport)
        assert result.status == "ok"
        assert result.invalid_metadata == 0
        model = result.models[0]
        assert model.identifier == "custom-a"
        assert model.name == "Custom A"
        assert model.context_window is None
        assert model.reasoning is None
        request = transport.requests[0]
        assert str(request.url) == "https://custom.test/v1/models"
        assert request.headers["authorization"] == "Bearer TEST_ONLY_KEY"
        assert request.headers["user-agent"].startswith("Mica/")
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_custom_anthropic_uses_the_explicit_interface_headers() -> None:
    async def scenario() -> None:
        target = CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="anthropic-messages",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://custom.test/v1/models",
        )
        transport = RecordingTransport(_handler(CUSTOM_PAYLOAD))
        result = await discover(target, CancellationToken(), transport=transport)
        assert result.status == "ok"
        request = transport.requests[0]
        assert request.headers["x-api-key"] == "TEST_ONLY_KEY"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers.get("authorization") is None

    asyncio.run(scenario())


def _assert_header_value_is_rejected(
    target: CatalogTarget,
    secret: str,
    *,
    address: str,
    path: str,
) -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler())
        with pytest.raises(ConfigError) as excinfo:
            await discover(target, CancellationToken(), transport=transport)
        message = str(excinfo.value)
        assert secret not in message
        assert address not in message
        assert path not in message
        assert "UnicodeEncodeError" not in message
        assert transport.requests == []
        assert transport.responses == []
        assert transport.aclose_count == 0
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_non_ascii_bearer_key_is_rejected_before_request() -> None:
    _assert_header_value_is_rejected(
        CatalogTarget(
            "deepseek",
            base_url="https://api.deepseek.com",
            api_key="TEST_ONLY_密钥",
        ),
        "TEST_ONLY_密钥",
        address="api.deepseek.com",
        path="/models",
    )


def test_non_ascii_anthropic_key_is_rejected_before_request() -> None:
    _assert_header_value_is_rejected(
        CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="anthropic-messages",
            api_key="TEST_ONLY_密钥",
            catalog_url="https://custom.test/v1/models",
        ),
        "TEST_ONLY_密钥",
        address="custom.test",
        path="/v1/models",
    )


def test_non_ascii_go_session_is_rejected_before_request() -> None:
    _assert_header_value_is_rejected(
        CatalogTarget(
            "opencode-go",
            base_url="https://opencode.ai/inference/go/openai/v1",
            session_id="会话-123",
        ),
        "会话-123",
        address="opencode.ai",
        path="/inference/go/v1/models",
    )


def test_go_does_not_reject_or_send_unused_non_ascii_key() -> None:
    async def scenario() -> None:
        target = CatalogTarget(
            "opencode-go",
            base_url="https://opencode.ai/inference/go/openai/v1",
            api_key="TEST_ONLY_密钥",
            session_id="session-123",
        )
        transport = RecordingTransport(_handler(GO_PAYLOAD))
        result = await discover(target, CancellationToken(), transport=transport)
        assert result.status == "ok"
        request = transport.requests[0]
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-api-key") is None
        assert "TEST_ONLY_密钥" not in str(request.headers)
        assert request.headers["x-opencode-session"] == "session-123"
        assert _pending_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="openai-completions",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://other.test/v1/models",
        ),
        CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="openai-completions",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://custom.test/v1/models?page=2",
        ),
        CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="unknown",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://custom.test/v1/models",
        ),
        CatalogTarget(
            "custom",
            base_url="https://custom.test/v1",
            api="openai-completions",
            catalog_url="https://custom.test/v1/models",
        ),
    ],
)
def test_custom_target_mismatch_is_rejected_before_request(
    target: CatalogTarget,
) -> None:
    transport = RecordingTransport(_handler(CUSTOM_PAYLOAD))
    with pytest.raises(ConfigError):
        asyncio.run(discover(target, CancellationToken(), transport=transport))
    assert transport.requests == []
    assert transport.aclose_count == 0


def test_custom_unknown_directory_shape_fails_safely() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler({"models": [{"id": "custom-a"}]}))
        result = await discover(CUSTOM_TARGET, CancellationToken(), transport=transport)
        assert result.status == "failed"
        assert result.error == "invalid_response"
        assert transport.requests

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        CatalogTarget(
            "deepseek", base_url="https://evil.test", api_key="TEST_ONLY_KEY"
        ),
        CatalogTarget(
            "deepseek",
            base_url="https://api.deepseek.com",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://api.deepseek.com/other",
        ),
        CatalogTarget(
            "deepseek",
            base_url="https://api.deepseek.com",
            api_key="TEST_ONLY_KEY",
            api="openai-responses",
        ),
        CatalogTarget("deepseek", base_url="https://api.deepseek.com"),
        CatalogTarget(
            "openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="TEST_ONLY_KEY",
            catalog_url="https://evil.test/models",
        ),
        CatalogTarget(
            "chatgpt-subscription",
            base_url="https://api.openai.com/v1",
            api_key="TEST_ONLY_KEY",
        ),
    ],
)
def test_builtin_target_mismatch_is_rejected_before_request(
    target: CatalogTarget,
) -> None:
    transport = RecordingTransport(_handler(DEEPSEEK_PAYLOAD))
    with pytest.raises(ConfigError):
        asyncio.run(discover(target, CancellationToken(), transport=transport))
    assert transport.requests == []
    assert transport.aclose_count == 0


def test_empty_directory_is_distinct_from_failure() -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler({"data": []}))
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "empty"
        assert result.models == ()
        assert result.error is None
        assert _pending_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error", "headers"),
    [
        (401, "authentication", None),
        (403, "authentication", None),
        (429, "rate_limited", None),
        (500, "http", None),
        (302, "redirect", {"location": "https://evil.test/models"}),
    ],
)
def test_http_failures_are_fixed_categories_without_remote_text(
    status: int, error: str, headers: dict[str, str] | None
) -> None:
    async def scenario() -> None:
        transport = RecordingTransport(
            _handler({"error": "TEST_ONLY_SECRET"}, status=status, headers=headers)
        )
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == error
        assert "TEST_ONLY_SECRET" not in repr(result)
        assert len(transport.requests) == 1
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_network_failure_is_safe_and_closes_resources() -> None:
    async def scenario() -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("TEST_ONLY_SECRET")

        transport = RecordingTransport(handle)
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == "network"
        assert "TEST_ONLY_SECRET" not in repr(result)
        assert len(transport.requests) == 1
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_disconnected_body_is_safe_and_closes_resources() -> None:
    class BrokenBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield b'{"data":'
            raise httpx.ReadError("TEST_ONLY_SECRET")

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        body = BrokenBody()

        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"content-type": "application/json"},
                stream=body,
            )

        transport = RecordingTransport(handle)
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == "network"
        assert "TEST_ONLY_SECRET" not in repr(result)
        assert body.closed is True
        assert transport.responses[0].is_closed is True
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b'{"data":{}}',
        b'{"data":[1]}',
        b'{"data":[{"name":"missing-id"}]}',
        b'{"data":[{"id":"dup"},{"id":"dup"}]}',
        b'{"data":[{"id":"one","id":"two"}]}',
        b'{"data":[{"id":"one","context_window":NaN}]}',
        b"\xff",
    ],
)
def test_invalid_payloads_are_not_reported_as_empty(body: bytes) -> None:
    async def scenario() -> None:
        transport = RecordingTransport(_handler(content=body))
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == "invalid_response"
        assert result.models == ()
        assert len(transport.requests) == 1
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_invalid_metadata_is_counted_and_not_written_as_discovered() -> None:
    async def scenario() -> None:
        payload = {
            "data": [
                {
                    "id": "invalid-metadata",
                    "name": 5,
                    "context_window": True,
                    "max_output_tokens": 0,
                    "input_modalities": "text",
                    "effort": {"supported_levels": "low"},
                },
                {
                    "id": "partial-metadata",
                    "name": "Partial",
                    "context_window": 1000,
                    "max_output_tokens": None,
                    "input_modalities": ["text", "video"],
                    "effort": {"supported_levels": ["low", "turbo"]},
                },
            ]
        }
        transport = RecordingTransport(_handler(payload))
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "ok"
        assert result.invalid_metadata == 5
        invalid = result.models[0]
        assert invalid.name is None
        assert invalid.context_window is None
        assert invalid.max_tokens is None
        assert invalid.input is None
        assert invalid.reasoning is None
        partial = result.models[1]
        assert partial.name == "Partial"
        assert partial.context_window == 1000
        assert partial.max_tokens is None
        assert partial.input == ("text",)
        assert partial.thinking_level_map == {ReasoningEffort.LOW: "low"}

    asyncio.run(scenario())


def test_gzip_and_chunked_bodies_are_parsed_and_closed() -> None:
    class ChunkStream(httpx.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks
            self.closed = False

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        compressed = gzip.compress(json.dumps(DEEPSEEK_PAYLOAD).encode("utf-8"))

        async def gzip_handle(request: httpx.Request) -> httpx.Response:
            return _response(content=compressed, headers={"content-encoding": "gzip"})

        gzip_transport = RecordingTransport(gzip_handle)
        gzip_result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=gzip_transport
        )
        assert gzip_result.status == "ok"
        assert len(gzip_result.models) == 2
        assert gzip_transport.responses[0].is_closed is True

        encoded = json.dumps(DEEPSEEK_PAYLOAD).encode("utf-8")
        stream = ChunkStream([encoded[:17], encoded[17:53], encoded[53:]])

        async def chunked_handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={
                    "content-type": "application/json",
                    "transfer-encoding": "chunked",
                },
                stream=stream,
            )

        chunked_transport = RecordingTransport(chunked_handle)
        chunked_result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=chunked_transport
        )
        assert chunked_result.status == "ok"
        assert len(chunked_result.models) == 2
        assert stream.closed is True
        assert chunked_transport.responses[0].is_closed is True
        assert chunked_transport.aclose_count == 1
        assert gzip_transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_decompressed_body_limit_is_enforced() -> None:
    async def scenario() -> None:
        body = gzip.compress(b"x" * (16 * 1024 * 1024 + 1))
        transport = RecordingTransport(
            _handler(
                None,
                content=body,
                headers={"content-encoding": "gzip"},
            )
        )
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == "too_large"
        assert len(transport.requests) == 1
        assert transport.aclose_count == 1
        assert transport.responses[0].is_closed is True
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_entry_limit_is_enforced() -> None:
    async def scenario() -> None:
        payload = {"data": [{"id": f"model-{index}"} for index in range(10001)]}
        transport = RecordingTransport(_handler(payload))
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert result.status == "failed"
        assert result.error == "too_large"

    asyncio.run(scenario())


def test_total_deadline_returns_timeout_and_closes_resources(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await never.wait()
            return httpx.Response(200, json=[])

        monkeypatch.setattr(model_catalog, "_DEADLINE_SECONDS", 0.2)
        transport = RecordingTransport(handle)
        result = await discover(
            DEEPSEEK_TARGET, CancellationToken(), transport=transport
        )
        assert started.is_set()
        assert result.status == "failed"
        assert result.error == "timeout"
        assert len(transport.requests) == 1
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_cancel_before_request_sends_nothing() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        token.cancel()
        transport = RecordingTransport(_handler(DEEPSEEK_PAYLOAD))
        with pytest.raises(CancellationRequested):
            await discover(DEEPSEEK_TARGET, token, transport=transport)
        assert transport.requests == []
        assert transport.aclose_count == 0
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_cancel_while_waiting_for_headers_closes_client_and_tasks() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await never.wait()
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        token = CancellationToken()
        task = asyncio.create_task(
            discover(DEEPSEEK_TARGET, token, transport=transport)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(CancellationRequested):
            await task
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_external_cancel_propagates_and_closes_client_and_tasks() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            started.set()
            await never.wait()
            return _response(DEEPSEEK_PAYLOAD)

        transport = RecordingTransport(handle)
        task = asyncio.create_task(
            discover(DEEPSEEK_TARGET, CancellationToken(), transport=transport)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def test_cancel_while_reading_body_closes_response_and_stream() -> None:
    class StalledBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield b'{"data":'
            self.started.set()
            await self.release.wait()

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        body = StalledBody()

        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"content-type": "application/json"},
                stream=body,
            )

        transport = RecordingTransport(handle)
        token = CancellationToken()
        task = asyncio.create_task(
            discover(DEEPSEEK_TARGET, token, transport=transport)
        )
        await asyncio.wait_for(body.started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(CancellationRequested):
            await task
        assert body.closed is True
        assert transport.responses[0].is_closed is True
        assert transport.aclose_count == 1
        assert _pending_tasks() == []

    asyncio.run(scenario())


def _seed_store(
    tmp_path: Path,
    *,
    channel: str = "deepseek",
    models: list[dict[str, object]] | None = None,
    provider_api: str | None = "openai-completions",
    protect_auth: bool = False,
) -> tuple[ConfigStore, Path]:
    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    (workspace / ".mica").mkdir(parents=True)
    models = models or []
    provider = {
        "name": "Catalog",
        "channel": channel,
        "enabled": True,
        "baseUrl": _BASE_URLS[channel],
        "future": {"keep": True},
        "models": models,
    }
    if provider_api is not None:
        provider["api"] = provider_api
    other = {
        "name": "Other",
        "channel": "custom",
        "enabled": True,
        "baseUrl": "https://other.test/v1",
        "api": "openai-completions",
        "models": [{"id": "other-model"}],
    }
    (root / "models.json").write_text(
        json.dumps(
            {
                "providers": {"catalog": provider, "other": other},
                "futureRoot": {"keep": True},
            }
        )
    )
    default_model = models[0]["id"] if models else "seed"
    (root / "config.json").write_text(
        json.dumps({"defaultModel": {"provider": "catalog", "model": default_model}})
    )
    auth = root / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "catalog": {"type": "api_key", "apiKey": "TEST_ONLY_SECRET"},
                    "other": {"type": "api_key", "apiKey": "TEST_ONLY_OTHER"},
                }
            }
        )
    )
    if protect_auth:
        protect(auth)
    return ConfigStore(root, workspace), root


def test_discovery_selection_save_round_trips_through_config_service(
    tmp_path: Path,
) -> None:
    seeded = [
        {
            "id": "deepseek-chat",
            "name": "Manual Name",
            "contextWindow": 999999,
            "api": "openai-responses",
            "manual": {"kept": 1},
        },
        {"id": "remote-gone", "name": "Gone Locally", "input": ["text"]},
    ]
    store, root = _seed_store(tmp_path, models=seeded, protect_auth=True)
    expected = store.read("models")["providers"]["catalog"]
    before = json.loads((root / "models.json").read_text())
    config_before = (root / "config.json").read_bytes()
    auth_before = (root / "auth.json").read_bytes()

    async def handle(request: httpx.Request) -> httpx.Response:
        return _response(DEEPSEEK_PAYLOAD)

    catalog = asyncio.run(
        discover(
            DEEPSEEK_TARGET,
            CancellationToken(),
            transport=RecordingTransport(handle),
        )
    )
    assert catalog.status == "ok"
    save_selection(
        store,
        "catalog",
        expected,
        catalog,
        {"deepseek-chat", "remote-gone", "deepseek-reasoner"},
    )
    document = json.loads((root / "models.json").read_text())
    saved = document["providers"]["catalog"]
    assert [model["id"] for model in saved["models"]] == [
        "deepseek-chat",
        "remote-gone",
        "deepseek-reasoner",
    ]
    existing = saved["models"][0]
    assert existing["name"] == "Manual Name"
    assert existing["contextWindow"] == 999999
    assert existing["api"] == "openai-responses"
    assert existing["manual"] == {"kept": 1}
    assert existing["maxTokens"] == 8192
    assert existing["input"] == ["text", "image"]
    assert existing["reasoning"] is True
    assert existing["thinkingLevelMap"] == {
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    assert saved["models"][1] == {
        "id": "remote-gone",
        "name": "Gone Locally",
        "input": ["text"],
    }
    new = saved["models"][2]
    assert new["id"] == "deepseek-reasoner"
    assert new["api"] == "openai-completions"
    assert new["name"] == "DeepSeek Reasoner"
    assert new["contextWindow"] == 65536
    assert new["maxTokens"] == 4096
    assert new["input"] == ["text"]
    assert new["reasoning"] is True
    assert new["thinkingLevelMap"] == {"low": "low"}
    assert document["providers"]["other"] == before["providers"]["other"]
    assert document["futureRoot"] == before["futureRoot"]
    assert (root / "config.json").read_bytes() == config_before
    assert (root / "auth.json").read_bytes() == auth_before

    service = ConfigService(store, environment={})
    snapshot = service.load()
    provider = next(
        item for item in snapshot.providers if item.provider_id == "catalog"
    )
    parsed = {model.selection.model_id: model for model in provider.models}
    assert parsed["deepseek-chat"].name == "Manual Name"
    assert parsed["deepseek-chat"].api == "openai-responses"
    assert parsed["deepseek-chat"].capabilities.context_window == 999999
    assert parsed["deepseek-chat"].capabilities.max_tokens == 8192
    assert parsed["deepseek-reasoner"].api == "openai-completions"
    capabilities = parsed["deepseek-reasoner"].capabilities
    assert capabilities.context_window == 65536
    assert capabilities.max_tokens == 4096
    assert capabilities.input == ("text",)
    assert capabilities.reasoning is True
    assert capabilities.thinking_level_map == {ReasoningEffort.LOW: "low"}

    expected_second = store.read("models")["providers"]["catalog"]
    save_selection(
        store,
        "catalog",
        expected_second,
        catalog,
        {"deepseek-reasoner"},
    )
    final = json.loads((root / "models.json").read_text())
    assert [model["id"] for model in final["providers"]["catalog"]["models"]] == [
        "deepseek-reasoner"
    ]


def test_incomplete_catalog_saves_seen_models_and_keeps_selected_absent_model(
    tmp_path: Path,
) -> None:
    store, root = _seed_store(
        tmp_path,
        channel="openrouter",
        models=[
            {"id": "selected-old", "name": "Selected Old"},
            {"id": "dropped-old"},
        ],
    )
    expected = store.read("models")["providers"]["catalog"]

    async def handle(request: httpx.Request) -> httpx.Response:
        return _response(OPENROUTER_PAYLOAD)

    catalog = asyncio.run(
        discover(
            OPENROUTER_TARGET,
            CancellationToken(),
            transport=RecordingTransport(handle),
        )
    )
    assert catalog.status == "incomplete"
    save_selection(
        store,
        "catalog",
        expected,
        catalog,
        {"selected-old", "or-chat"},
    )
    saved = json.loads((root / "models.json").read_text())["providers"]["catalog"]
    assert [model["id"] for model in saved["models"]] == [
        "selected-old",
        "or-chat",
    ]
    assert saved["models"][0]["name"] == "Selected Old"
    assert "dropped-old" not in [model["id"] for model in saved["models"]]
    assert saved["models"][1]["api"] == "openai-completions"
    assert saved["models"][1]["maxTokens"] == 16000


def test_go_new_models_need_explicit_api_choices_and_keep_existing_api(
    tmp_path: Path,
) -> None:
    store, root = _seed_store(
        tmp_path,
        channel="opencode-go",
        models=[
            {"id": "go-old", "name": "Old"},
            {"id": "go-explicit", "api": "anthropic-messages"},
        ],
    )
    expected = store.read("models")["providers"]["catalog"]
    before = (root / "models.json").read_bytes()
    payload = {
        "object": "list",
        "data": [
            {"id": "go-explicit", "object": "model", "name": "Go Explicit"},
            {"id": "go-alpha", "object": "model"},
        ],
    }

    async def handle(request: httpx.Request) -> httpx.Response:
        return _response(payload)

    catalog = asyncio.run(
        discover(GO_TARGET, CancellationToken(), transport=RecordingTransport(handle))
    )
    assert catalog.status == "ok"
    selected = {"go-old", "go-explicit", "go-alpha"}
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, catalog, selected)
    assert (root / "models.json").read_bytes() == before
    with pytest.raises(ConfigError):
        save_selection(
            store,
            "catalog",
            expected,
            catalog,
            selected,
            api_choices={"go-alpha": "google"},
        )
    assert (root / "models.json").read_bytes() == before
    save_selection(
        store,
        "catalog",
        expected,
        catalog,
        selected,
        api_choices={"go-alpha": "openai-completions"},
    )
    saved = json.loads((root / "models.json").read_text())["providers"]["catalog"]
    assert [model["id"] for model in saved["models"]] == [
        "go-old",
        "go-explicit",
        "go-alpha",
    ]
    assert "api" not in saved["models"][0]
    assert saved["models"][1]["api"] == "anthropic-messages"
    assert saved["models"][2]["api"] == "openai-completions"
    assert saved["api"] == "openai-completions"


def test_unknown_provider_api_needs_an_explicit_model_choice(tmp_path: Path) -> None:
    store, root = _seed_store(
        tmp_path,
        channel="custom",
        models=[{"id": "old"}],
        provider_api=None,
    )
    expected = store.read("models")["providers"]["catalog"]
    before = (root / "models.json").read_bytes()
    catalog = CatalogResult("ok", (CatalogModel("new"),))
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, catalog, {"new"})
    assert (root / "models.json").read_bytes() == before
    save_selection(
        store,
        "catalog",
        expected,
        catalog,
        {"new"},
        api_choices={"new": "openai-completions"},
    )
    saved = json.loads((root / "models.json").read_text())["providers"]["catalog"]
    assert saved["models"] == [{"id": "new", "api": "openai-completions"}]


def test_failed_empty_and_manual_results_never_overwrite_models(
    tmp_path: Path,
) -> None:
    store, root = _seed_store(tmp_path, models=[{"id": "existing"}])
    expected = store.read("models")["providers"]["catalog"]
    before = (root / "models.json").read_bytes()

    async def unauthorized(request: httpx.Request) -> httpx.Response:
        return _response({"error": "TEST_ONLY_SECRET"}, status=401)

    failed = asyncio.run(
        discover(
            DEEPSEEK_TARGET,
            CancellationToken(),
            transport=RecordingTransport(unauthorized),
        )
    )
    assert failed.status == "failed"
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, failed, {"existing"})
    assert (root / "models.json").read_bytes() == before

    async def empty(request: httpx.Request) -> httpx.Response:
        return _response({"data": []})

    empty_result = asyncio.run(
        discover(
            DEEPSEEK_TARGET,
            CancellationToken(),
            transport=RecordingTransport(empty),
        )
    )
    assert empty_result.status == "empty"
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, empty_result, {"existing"})
    assert (root / "models.json").read_bytes() == before

    with pytest.raises(ConfigError):
        save_selection(
            store,
            "catalog",
            expected,
            CatalogResult("manual"),
            {"existing"},
        )
    assert (root / "models.json").read_bytes() == before


def test_unknown_selected_identifier_rejects_the_whole_save(tmp_path: Path) -> None:
    store, root = _seed_store(tmp_path, models=[{"id": "existing"}])
    expected = store.read("models")["providers"]["catalog"]
    before = (root / "models.json").read_bytes()
    catalog = CatalogResult("ok", (CatalogModel("known"),))
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, catalog, {"existing", "missing"})
    assert (root / "models.json").read_bytes() == before


@pytest.mark.parametrize("mutation", ["edit", "disable", "delete"])
def test_stale_expected_provider_rejects_save_without_replacing_connection(
    tmp_path: Path, mutation: str
) -> None:
    store, root = _seed_store(tmp_path, models=[{"id": "existing"}])
    expected = store.read("models")["providers"]["catalog"]

    async def handle(request: httpx.Request) -> httpx.Response:
        return _response(DEEPSEEK_PAYLOAD)

    catalog = asyncio.run(
        discover(
            DEEPSEEK_TARGET,
            CancellationToken(),
            transport=RecordingTransport(handle),
        )
    )
    document = json.loads((root / "models.json").read_text())
    if mutation == "edit":
        document["providers"]["catalog"]["name"] = "Changed Elsewhere"
    elif mutation == "disable":
        document["providers"]["catalog"]["enabled"] = False
    else:
        del document["providers"]["catalog"]
    (root / "models.json").write_text(json.dumps(document))
    stale_bytes = (root / "models.json").read_bytes()
    with pytest.raises(ConfigConflictError):
        save_selection(store, "catalog", expected, catalog, {"existing"})
    assert (root / "models.json").read_bytes() == stale_bytes


def test_save_requires_the_complete_expected_record(tmp_path: Path) -> None:
    store, root = _seed_store(tmp_path, models=[{"id": "existing"}])
    before = (root / "models.json").read_bytes()
    catalog = CatalogResult("ok", (CatalogModel("new"),))
    with pytest.raises(TypeError):
        save_selection(store, "catalog", None, catalog, {"new"})
    with pytest.raises(TypeError):
        save_selection(store, "catalog", {}, catalog, "new")
    assert (root / "models.json").read_bytes() == before


def test_validation_failure_does_not_partially_save(tmp_path: Path) -> None:
    store, root = _seed_store(tmp_path, models=[{"id": "existing"}])
    expected = store.read("models")["providers"]["catalog"]
    before = (root / "models.json").read_bytes()
    huge = CatalogModel("huge", name="x" * (4 * 1024 * 1024))
    catalog = CatalogResult("ok", (huge,))
    with pytest.raises(ConfigError):
        save_selection(store, "catalog", expected, catalog, {"huge"})
    assert (root / "models.json").read_bytes() == before
    assert not list(root.glob("*.tmp"))
