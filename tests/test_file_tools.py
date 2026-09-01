"""read、write、edit 三个文件工具的公共行为测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.file_tools import MAX_FILE_BYTES, register_file_tools
from mica_agent.schema_validation import validate_tool_schema
from mica_agent.tool_contracts import (
    ToolRequest,
    ToolResultStatus,
    to_mutable_json,
)
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


def test_register_file_tools_exposes_three_valid_sorted_specs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    register_file_tools(registry, resolve_workspace(workspace))

    specs = registry.specs()
    assert [spec.name for spec in specs] == ["edit", "read", "write"]
    for spec in specs:
        validate_tool_schema(spec)
    assert specs[1].input_schema == {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ("path",),
        "additionalProperties": False,
    }


def test_register_file_tools_does_not_access_filesystem_or_run_handlers(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = resolve_workspace(workspace)

    def forbidden(*args, **kwargs):
        raise AssertionError("registration must not access the filesystem")

    monkeypatch.setattr(Path, "cwd", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    registry = ToolRegistry()

    register_file_tools(registry, identity)

    assert [spec.name for spec in registry.specs()] == ["edit", "read", "write"]


def test_registered_read_returns_complete_utf8_text_preserving_newlines(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_bytes("你好\r\nworld\n".encode())
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    result = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-1", "read", {"path": "notes.txt"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == "你好\r\nworld\n"


def test_read_and_edit_bound_binary_reads_to_maximum_plus_one_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("replace me", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))
    original_open = Path.open
    read_sizes: list[int] = []

    class RecordingStream:
        def __init__(self, stream) -> None:
            self._stream = stream

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self._stream.read(size)

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *arguments) -> None:
            self._stream.__exit__(*arguments)

    def recording_open(path: Path, *arguments, **keywords):
        return RecordingStream(original_open(path, *arguments, **keywords))

    monkeypatch.setattr(Path, "open", recording_open)
    read = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("bounded-read", "read", {"path": "notes.txt"}),
            CancellationToken(),
        )
    )
    edited = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "bounded-edit",
                "edit",
                {"path": "notes.txt", "old_text": "replace", "new_text": "edited"},
            ),
            CancellationToken(),
        )
    )

    assert read.status is ToolResultStatus.SUCCESS
    assert edited.status is ToolResultStatus.SUCCESS
    assert read_sizes == [MAX_FILE_BYTES + 1, MAX_FILE_BYTES + 1]


def test_registered_read_rejects_oversized_file_without_partial_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "credential-like-value"
    (workspace / "large.txt").write_bytes(("x" * MAX_FILE_BYTES + secret).encode())
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    result = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-2", "read", {"path": "large.txt"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.data is None
    assert secret not in result.message


@pytest.mark.parametrize(
    "path",
    [
        ".npmrc.",
        ".npmrc ",
        ".git./config",
        ".git /config",
        "normal.txt:secret",
        "NUL",
        "NUL.txt",
        "CON",
        "COM1.log",
        "bad\x00path",
    ],
)
def test_registered_handlers_safely_deny_windows_path_aliases(
    tmp_path: Path, path: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "credential-like-value"
    npmrc = workspace / ".npmrc"
    npmrc.write_text(secret, encoding="utf-8")
    git_config = workspace / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text(secret, encoding="utf-8")
    normal = workspace / "normal.txt"
    normal.write_text("unchanged", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    requests = (
        ("read", {"path": path}),
        ("write", {"path": path, "content": "changed"}),
        ("edit", {"path": path, "old_text": "unchanged", "new_text": "changed"}),
    )
    results = [
        asyncio.run(
            registry.resolve(name).handler(
                ToolRequest(f"denied-{name}", name, arguments), CancellationToken()
            )
        )
        for name, arguments in requests
    ]

    assert all(
        result.status is ToolResultStatus.PERMISSION_DENIED for result in results
    )
    assert all(
        result.data is None
        and all(
            value not in result.message
            for value in (
                str(workspace),
                str(Path.home()),
                secret,
                "ValueError",
                "Traceback",
            )
        )
        for result in results
    )
    assert npmrc.read_text(encoding="utf-8") == secret
    assert git_config.read_text(encoding="utf-8") == secret
    assert normal.read_text(encoding="utf-8") == "unchanged"


def test_edit_rejects_oversized_input_without_temp_file_or_byte_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "large.txt"
    original = b"x" * (MAX_FILE_BYTES + 1)
    target.write_bytes(original)
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    result = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "large-edit",
                "edit",
                {"path": "large.txt", "old_text": "x", "new_text": "y"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.data is None
    assert target.read_bytes() == original
    assert not list(workspace.glob(".large.txt.*.tmp"))


def test_registered_write_creates_exact_utf8_bytes_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    result = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest("call-3", "write", {"path": "new.txt", "content": "你好\r\n"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert (workspace / "new.txt").read_bytes() == "你好\r\n".encode()
    assert not list(workspace.glob(".new.txt.*.tmp"))


def test_registered_edit_replaces_one_unique_match_without_changing_crlf(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes("first\r\nTARGET\r\n你好\r\n".encode())
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    result = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-6",
                "edit",
                {"path": "notes.txt", "old_text": "TARGET", "new_text": "done"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert target.read_bytes() == "first\r\ndone\r\n你好\r\n".encode()
    assert to_mutable_json(result.presentation) == {
        "matched": True,
        "lines": [
            {"kind": "context", "text": "first", "old_line": 1, "new_line": 1},
            {"kind": "removed", "text": "TARGET", "old_line": 2, "new_line": None},
            {"kind": "added", "text": "done", "old_line": None, "new_line": 2},
            {"kind": "context", "text": "你好", "old_line": 3, "new_line": 3},
        ],
    }


def test_file_tools_deny_sensitive_paths_but_allow_explicit_env_example(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "token=credential-like-value"
    (workspace / ".env").write_text(secret, encoding="utf-8")
    (workspace / ".env.example").write_text("KEY=example", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    denied = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-7", "read", {"path": ".env"}), CancellationToken()
        )
    )
    allowed = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-8", "read", {"path": ".env.example"}),
            CancellationToken(),
        )
    )

    assert denied.status is ToolResultStatus.PERMISSION_DENIED
    assert secret not in denied.message
    assert allowed.status is ToolResultStatus.SUCCESS
    assert allowed.data == "KEY=example"


def test_registered_write_replace_failure_preserves_bytes_cleans_temp_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    failed = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest(
                "call-11", "write", {"path": "notes.txt", "content": "changed"}
            ),
            CancellationToken(),
        )
    )

    assert failed.status is ToolResultStatus.EXECUTION_FAILED
    assert failed.presentation is None
    assert target.read_bytes() == b"original"
    assert not list(workspace.glob(".notes.txt.*.tmp"))

    monkeypatch.undo()
    recovered = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest(
                "call-12", "write", {"path": "notes.txt", "content": "changed"}
            ),
            CancellationToken(),
        )
    )
    assert recovered.status is ToolResultStatus.SUCCESS
    assert target.read_bytes() == b"changed"


def test_read_rejects_invalid_utf8_and_edit_requires_exactly_one_match(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invalid.txt").write_bytes(b"\xff")
    target = workspace / "repeated.txt"
    target.write_text("same same", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    invalid = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-13", "read", {"path": "invalid.txt"}),
            CancellationToken(),
        )
    )
    multiple = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-14",
                "edit",
                {"path": "repeated.txt", "old_text": "same", "new_text": "new"},
            ),
            CancellationToken(),
        )
    )
    missing = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-15",
                "edit",
                {"path": "repeated.txt", "old_text": "absent", "new_text": "new"},
            ),
            CancellationToken(),
        )
    )

    assert invalid.status is ToolResultStatus.EXECUTION_FAILED
    assert multiple.status is ToolResultStatus.EXECUTION_FAILED
    assert missing.status is ToolResultStatus.EXECUTION_FAILED
    assert to_mutable_json(multiple.presentation) == {
        "matched": False,
        "lines": [
            {"kind": "removed", "text": "same", "old_line": None, "new_line": None},
            {"kind": "added", "text": "new", "old_line": None, "new_line": None},
        ],
    }
    assert target.read_text(encoding="utf-8") == "same same"


def test_write_and_edit_size_failures_leave_existing_file_unchanged(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"keep")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    write_result = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest(
                "call-16",
                "write",
                {"path": "notes.txt", "content": "x" * (MAX_FILE_BYTES + 1)},
            ),
            CancellationToken(),
        )
    )
    edit_result = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-17",
                "edit",
                {
                    "path": "notes.txt",
                    "old_text": "keep",
                    "new_text": "x" * (MAX_FILE_BYTES + 1),
                },
            ),
            CancellationToken(),
        )
    )

    assert write_result.status is ToolResultStatus.EXECUTION_FAILED
    assert edit_result.status is ToolResultStatus.EXECUTION_FAILED
    assert target.read_bytes() == b"keep"


def test_file_tools_return_cancelled_before_access_and_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))
    cancelled = CancellationToken()
    cancelled.cancel()

    before_access = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-18", "read", {"path": "missing.txt"}), cancelled
        )
    )
    before_replace = CancellationToken()
    original_fsync = os.fsync

    def cancel_before_replace(fd: int) -> None:
        original_fsync(fd)
        before_replace.cancel()

    monkeypatch.setattr(os, "fsync", cancel_before_replace)
    replacement = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest("call-19", "write", {"path": "notes.txt", "content": "new"}),
            before_replace,
        )
    )

    assert before_access.status is ToolResultStatus.CANCELLED
    assert replacement.status is ToolResultStatus.CANCELLED
    assert target.read_bytes() == b"original"
    assert not list(workspace.glob(".notes.txt.*.tmp"))


def test_edit_replace_failure_preserves_original_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"before target after")
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    monkeypatch.setattr(
        os, "replace", lambda source, destination: (_ for _ in ()).throw(OSError())
    )
    failed = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-20",
                "edit",
                {"path": "notes.txt", "old_text": "target", "new_text": "changed"},
            ),
            CancellationToken(),
        )
    )

    assert failed.status is ToolResultStatus.EXECUTION_FAILED
    assert to_mutable_json(failed.presentation) == {
        "matched": True,
        "lines": [
            {
                "kind": "removed",
                "text": "before target after",
                "old_line": 1,
                "new_line": None,
            },
            {
                "kind": "added",
                "text": "before changed after",
                "old_line": None,
                "new_line": 1,
            },
        ],
    }
    assert target.read_bytes() == b"before target after"
    assert not list(workspace.glob(".notes.txt.*.tmp"))

    monkeypatch.undo()
    recovered = asyncio.run(
        registry.resolve("edit").handler(
            ToolRequest(
                "call-21",
                "edit",
                {"path": "notes.txt", "old_text": "target", "new_text": "changed"},
            ),
            CancellationToken(),
        )
    )
    assert recovered.status is ToolResultStatus.SUCCESS
    assert target.read_bytes() == b"before changed after"


@pytest.mark.parametrize("duplicate_name", ["edit", "read", "write"])
def test_register_file_tools_is_atomic_when_any_name_is_already_registered(
    tmp_path: Path, duplicate_name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    existing = ToolRegistry()
    register_file_tools(existing, resolve_workspace(workspace))
    registered = existing.resolve(duplicate_name)
    registry.register(registered.spec, registered.handler)

    with pytest.raises(Exception, match="already registered"):
        register_file_tools(registry, resolve_workspace(workspace))

    assert [spec.name for spec in registry.specs()] == [duplicate_name]


def test_file_size_limit_allows_exactly_maximum_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = "x" * MAX_FILE_BYTES
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace))

    written = asyncio.run(
        registry.resolve("write").handler(
            ToolRequest(
                "call-22", "write", {"path": "maximum.txt", "content": payload}
            ),
            CancellationToken(),
        )
    )
    read = asyncio.run(
        registry.resolve("read").handler(
            ToolRequest("call-23", "read", {"path": "maximum.txt"}), CancellationToken()
        )
    )

    assert written.status is ToolResultStatus.SUCCESS
    assert read.status is ToolResultStatus.SUCCESS
    assert read.data == payload
