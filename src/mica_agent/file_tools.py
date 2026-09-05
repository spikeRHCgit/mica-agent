"""P5-03 的显式注册文件工具入口。"""

from __future__ import annotations

import os
import stat
import tempfile
from functools import partial
from pathlib import Path

from .cancellation import CancellationRequested, CancellationToken
from .tool_contracts import (
    JsonObject,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from .tool_presentation import build_edit_presentation_detail
from .tool_registry import DuplicateToolError, ToolRegistry
from .workspace import WorkspaceIdentity
from .workspace_paths import WorkspacePathError, WorkspacePathResolver

MAX_FILE_BYTES = 262_144


class _FileTooLargeError(Exception):
    pass


_READ_SPEC = ToolSpec(
    name="read",
    description="Read a UTF-8 text file from the workspace.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    },
)
_WRITE_SPEC = ToolSpec(
    name="write",
    description="Create or replace a UTF-8 text file in the workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)
_EDIT_SPEC = ToolSpec(
    name="edit",
    description="Replace one unique text occurrence in a workspace file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    },
)


def _result(
    request: ToolRequest,
    status: ToolResultStatus,
    message: str,
    data: object = None,
    *,
    presentation: JsonObject | None = None,
) -> ToolResult:
    return ToolResult(
        request.call_id,
        request.name,
        status,
        message,
        data,
        presentation,
    )


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_regular_or_missing(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _read_bounded_utf8(path: Path) -> str:
    with path.open("rb") as stream:
        payload = stream.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise _FileTooLargeError
    return payload.decode("utf-8", errors="strict")


def _path_argument(request: ToolRequest) -> str | None:
    arguments = request.arguments
    if set(arguments) != {"path"}:
        return None
    path = arguments["path"]
    return path if isinstance(path, str) and path.strip() else None


async def _read_handler(
    request: ToolRequest,
    cancellation: CancellationToken,
    resolver: WorkspacePathResolver,
) -> ToolResult:
    try:
        cancellation.raise_if_cancelled()
        path = _path_argument(request)
        if path is None:
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid file arguments"
            )
        target = resolver.resolve(path)
        if not _is_regular_file(target):
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
            )
        text = _read_bounded_utf8(target)
        cancellation.raise_if_cancelled()
        return _result(request, ToolResultStatus.SUCCESS, "file read", text)
    except CancellationRequested:
        return _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
    except WorkspacePathError:
        return _result(
            request, ToolResultStatus.PERMISSION_DENIED, "file access denied"
        )
    except (_FileTooLargeError, OSError, UnicodeError):
        return _result(
            request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
        )


def _write_arguments(request: ToolRequest) -> tuple[str, str] | None:
    arguments = request.arguments
    if set(arguments) != {"path", "content"}:
        return None
    path = arguments["path"]
    content = arguments["content"]
    if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
        return None
    return path, content


def _atomic_write(
    target: Path, payload: bytes, cancellation: CancellationToken
) -> None:
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        cancellation.raise_if_cancelled()
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


async def _write_handler(
    request: ToolRequest,
    cancellation: CancellationToken,
    resolver: WorkspacePathResolver,
) -> ToolResult:
    try:
        cancellation.raise_if_cancelled()
        arguments = _write_arguments(request)
        if arguments is None:
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid file arguments"
            )
        path, content = arguments
        payload = content.encode("utf-8", errors="strict")
        if len(payload) > MAX_FILE_BYTES:
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
            )
        target = resolver.resolve(path)
        if not target.parent.is_dir() or not _is_regular_or_missing(target):
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
            )
        _atomic_write(target, payload, cancellation)
        return _result(request, ToolResultStatus.SUCCESS, "file written")
    except CancellationRequested:
        return _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
    except (OSError, UnicodeError):
        return _result(
            request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
        )
    except WorkspacePathError:
        return _result(
            request, ToolResultStatus.PERMISSION_DENIED, "file access denied"
        )


def _edit_arguments(request: ToolRequest) -> tuple[str, str, str] | None:
    arguments = request.arguments
    if set(arguments) != {"path", "old_text", "new_text"}:
        return None
    path = arguments["path"]
    old_text = arguments["old_text"]
    new_text = arguments["new_text"]
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(old_text, str)
        or not old_text
        or not isinstance(new_text, str)
    ):
        return None
    return path, old_text, new_text


async def _edit_handler(
    request: ToolRequest,
    cancellation: CancellationToken,
    resolver: WorkspacePathResolver,
) -> ToolResult:
    presentation: dict[str, object] | None = None
    try:
        cancellation.raise_if_cancelled()
        arguments = _edit_arguments(request)
        if arguments is None:
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid file arguments"
            )
        path, old_text, new_text = arguments
        target = resolver.resolve(path)
        if not _is_regular_file(target):
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
            )
        text = _read_bounded_utf8(target)
        if text.count(old_text) != 1:
            presentation = build_edit_presentation_detail(
                text,
                text,
                matched=False,
                old_text=old_text,
                new_text=new_text,
            )
            return _result(
                request,
                ToolResultStatus.EXECUTION_FAILED,
                "file operation failed",
                presentation=presentation,
            )
        replaced = text.replace(old_text, new_text, 1)
        presentation = build_edit_presentation_detail(text, replaced, matched=True)
        encoded = replaced.encode("utf-8", errors="strict")
        if len(encoded) > MAX_FILE_BYTES:
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "file operation failed"
            )
        _atomic_write(target, encoded, cancellation)
        return _result(
            request,
            ToolResultStatus.SUCCESS,
            "file edited",
            presentation=presentation,
        )
    except CancellationRequested:
        return _result(
            request,
            ToolResultStatus.CANCELLED,
            "operation cancelled",
            presentation=presentation,
        )
    except WorkspacePathError:
        return _result(
            request,
            ToolResultStatus.PERMISSION_DENIED,
            "file access denied",
            presentation=presentation,
        )
    except (_FileTooLargeError, OSError, UnicodeError):
        return _result(
            request,
            ToolResultStatus.EXECUTION_FAILED,
            "file operation failed",
            presentation=presentation,
        )


async def _not_available(
    request: ToolRequest, cancellation: CancellationToken
) -> ToolResult:
    if cancellation.is_cancelled:
        return _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
    return _result(request, ToolResultStatus.EXECUTION_FAILED, "file operation failed")


def register_file_tools(registry: ToolRegistry, workspace: WorkspaceIdentity) -> None:
    """显式、原子地注册当前工作区的三个文件工具。"""
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not isinstance(workspace, WorkspaceIdentity):
        raise TypeError("workspace must be a WorkspaceIdentity")
    resolver = WorkspacePathResolver(workspace)
    specs = (_READ_SPEC, _WRITE_SPEC, _EDIT_SPEC)
    names = {spec.name for spec in registry.specs()}
    if names.intersection(spec.name for spec in specs):
        raise DuplicateToolError("tool name is already registered")
    handlers = {
        "read": partial(_read_handler, resolver=resolver),
        "write": partial(_write_handler, resolver=resolver),
        "edit": partial(_edit_handler, resolver=resolver),
    }
    for spec in specs:
        registry.register(spec, handlers[spec.name])


__all__ = ["register_file_tools"]
