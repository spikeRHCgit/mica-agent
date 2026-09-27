"""Tool presentation contracts and safe snapshot builders a frontend can consume."""

from __future__ import annotations

import ntpath
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from .message_sanitizer import sanitize_presentation_text
from .tool_contracts import (
    JsonObject,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    _freeze_json_object,
    _validate_non_blank_text,
    _validate_tool_name,
)
from .workspace import WorkspaceIdentity
from .workspace_paths import WorkspacePathError, WorkspacePathResolver

_PRESENTATION_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "apikey",
        "token",
        "access_token",
        "access-token",
        "secret",
        "password",
        "authorization",
        "client_secret",
        "private_key",
    }
)
_SAFE_PRESENTATION_MESSAGES = frozenset(
    {
        "file path is not available",
        "file content is not available",
        "file edit is not available",
        "command is not available",
        "question is not available",
        "tool details are unavailable",
        "tool execution failed",
        "operation cancelled",
        "permission denied",
        "tool arguments are invalid",
        "file was not changed",
    }
)


def _sanitize_payload(value: object, active: set[int] | None = None) -> object:
    if isinstance(value, str):
        return sanitize_presentation_text(value)
    if active is None:
        active = set()
    if isinstance(value, (list, tuple, Mapping)):
        identity = id(value)
        if identity in active:
            raise ValueError("payload must not contain recursive values")
        active.add(identity)
        try:
            if isinstance(value, (list, tuple)):
                return [_sanitize_payload(item, active) for item in value]
            sanitized: dict[object, object] = {}
            for key, item in value.items():
                if (
                    isinstance(key, str)
                    and key.casefold() in _PRESENTATION_SENSITIVE_KEYS
                ):
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = _sanitize_payload(item, active)
            return sanitized
        finally:
            active.remove(identity)
    return value


class ToolPresentationKind(Enum):
    """How a tool card interprets the presentation payload."""

    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    BASH = "bash"
    ASK = "ask"
    GENERIC = "generic"


class ToolPresentationStatus(Enum):
    """Lifecycle status of a tool card."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolPresentationSnapshot:
    """Tool presentation snapshot free of runtime resources and safe to persist."""

    call_id: str
    tool_name: str
    kind: ToolPresentationKind
    status: ToolPresentationStatus
    payload: JsonObject
    message: str | None = None

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        if not isinstance(self.kind, ToolPresentationKind):
            raise TypeError("kind must be a ToolPresentationKind")
        if not isinstance(self.status, ToolPresentationStatus):
            raise TypeError("status must be a ToolPresentationStatus")
        safe_payload = _sanitize_payload(self.payload)
        frozen_payload = _freeze_json_object(safe_payload, "payload")
        object.__setattr__(self, "payload", frozen_payload)
        _validate_snapshot_payload(self.kind, self.status, frozen_payload)
        if self.message is not None:
            _validate_non_blank_text(self.message, "message")
            if self.message not in _SAFE_PRESENTATION_MESSAGES:
                raise ValueError("message is not a safe presentation message")


_MAX_PRESENTATION_TEXT_BYTES = 262_144
_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_KIND_BY_NAME = {
    "read": ToolPresentationKind.READ,
    "write": ToolPresentationKind.WRITE,
    "edit": ToolPresentationKind.EDIT,
    "bash": ToolPresentationKind.BASH,
    "ask": ToolPresentationKind.ASK,
}


def _safe_relative_path(
    value: object, workspace: WorkspaceIdentity | None = None
) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    if sanitize_presentation_text(value) != value:
        return None
    if ntpath.splitdrive(value)[0] or ntpath.isabs(value):
        return None
    parts = value.replace("\\", "/").split("/")
    normalized: list[str] = []
    for part in parts:
        folded = part.casefold()
        if part in {"", "."}:
            continue
        if (
            part == ".."
            or ":" in part
            or part.endswith((".", " "))
            or folded.split(".", 1)[0] in _DEVICE_NAMES
        ):
            return None
        normalized.append(part)
    if not normalized:
        return None
    if any(part.casefold() in {".git", "credentials"} for part in normalized):
        return None
    name = normalized[-1].casefold()
    if name in {
        ".env",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    }:
        return None
    if name.startswith(".env.") and name not in {
        ".env.example",
        ".env.sample",
        ".env.template",
    }:
        return None
    if name.endswith((".pem", ".key")):
        return None
    normalized_path = "/".join(normalized)
    if workspace is not None:
        if not isinstance(workspace, WorkspaceIdentity):
            raise TypeError("workspace must be a WorkspaceIdentity or None")
        resolver = WorkspacePathResolver(workspace)
        try:
            resolved = resolver.resolve(value)
        except WorkspacePathError:
            return None
        if resolver.is_sensitive(resolved):
            return None
    return normalized_path


def _validate_snapshot_payload(
    kind: ToolPresentationKind,
    status: ToolPresentationStatus,
    payload: Mapping[str, object],
) -> None:
    if kind is ToolPresentationKind.GENERIC:
        if payload:
            raise ValueError("generic presentation payload must be empty")
        return
    if kind in {
        ToolPresentationKind.READ,
        ToolPresentationKind.WRITE,
        ToolPresentationKind.EDIT,
    }:
        expected = {
            ToolPresentationKind.READ: {"path"},
            ToolPresentationKind.WRITE: {"path", "content"},
            ToolPresentationKind.EDIT: {"path", "matched", "lines"},
        }[kind]
    elif kind is ToolPresentationKind.BASH:
        expected = (
            {"command"}
            if status is ToolPresentationStatus.PENDING
            else {
                "command",
                "output",
                "exit_code",
            }
        )
    else:
        expected = (
            {"question"}
            if status is ToolPresentationStatus.PENDING
            else {
                "question",
                "answer",
            }
        )
    if set(payload) != expected:
        raise ValueError("presentation payload fields are invalid")
    if "path" in payload:
        path = payload["path"]
        if not isinstance(path, str) or (path and _safe_relative_path(path) is None):
            raise ValueError("presentation path is invalid")
    if kind is ToolPresentationKind.WRITE and not isinstance(payload["content"], str):
        raise TypeError("presentation content must be a string")
    if kind is ToolPresentationKind.EDIT:
        matched = payload["matched"]
        if matched is None and status is not ToolPresentationStatus.PENDING:
            raise ValueError("terminal presentation matched must be a boolean")
        if matched is not None and not isinstance(matched, bool):
            raise TypeError("presentation matched must be a boolean or null")
        lines = payload["lines"]
        if not isinstance(lines, tuple):
            raise TypeError("presentation lines must be an array")
        for line in lines:
            if not isinstance(line, Mapping) or set(line) != {
                "kind",
                "text",
                "old_line",
                "new_line",
            }:
                raise ValueError("presentation line is invalid")
            line_kind = line["kind"]
            if line_kind not in {"context", "removed", "added"}:
                raise ValueError("presentation line kind is invalid")
            if not isinstance(line["text"], str):
                raise TypeError("presentation line text must be a string")
            old_line = line["old_line"]
            new_line = line["new_line"]
            for line_number in (old_line, new_line):
                if line_number is not None and (
                    isinstance(line_number, bool)
                    or not isinstance(line_number, int)
                    or line_number <= 0
                ):
                    raise ValueError("presentation line number is invalid")
            if line_kind == "context" and (old_line is None or new_line is None):
                raise ValueError("context line numbers are required")
            if line_kind == "removed" and new_line is not None:
                raise ValueError("removed line cannot have a new line number")
            if line_kind == "added" and old_line is not None:
                raise ValueError("added line cannot have an old line number")
            if (
                line_kind == "removed"
                and old_line is None
                and status is ToolPresentationStatus.SUCCESS
            ):
                raise ValueError("removed line number is required")
            if (
                line_kind == "added"
                and new_line is None
                and status is ToolPresentationStatus.SUCCESS
            ):
                raise ValueError("added line number is required")
    if kind is ToolPresentationKind.BASH:
        command = payload["command"]
        if not isinstance(command, str):
            raise TypeError("presentation command must be a string")
        if status is not ToolPresentationStatus.PENDING:
            if not isinstance(payload["output"], str):
                raise TypeError("presentation output must be a string")
            exit_code = payload["exit_code"]
            if exit_code is not None and (
                isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
                or exit_code < 0
            ):
                raise ValueError("presentation exit_code is invalid")
    if kind is ToolPresentationKind.ASK:
        if not isinstance(payload["question"], str):
            raise TypeError("presentation question must be a string")
        if status is not ToolPresentationStatus.PENDING and not isinstance(
            payload["answer"], str
        ):
            raise TypeError("presentation answer must be a string")


def _bounded_text(value: str) -> str:
    value = sanitize_presentation_text(value)
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_PRESENTATION_TEXT_BYTES:
        return value
    return encoded[:_MAX_PRESENTATION_TEXT_BYTES].decode("utf-8", errors="ignore")


def _kind(tool_name: str) -> ToolPresentationKind:
    return _KIND_BY_NAME.get(tool_name, ToolPresentationKind.GENERIC)


def _edit_line(
    kind: str, text: str, old_line: int | None, new_line: int | None
) -> dict[str, object]:
    return {
        "kind": kind,
        "text": text,
        "old_line": old_line,
        "new_line": new_line,
    }


def _unlocated_edit_lines(old_text: str, new_text: str) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    lines.extend(
        _edit_line("removed", line, None, None) for line in old_text.splitlines()
    )
    lines.extend(
        _edit_line("added", line, None, None) for line in new_text.splitlines()
    )
    return lines


def build_edit_presentation_detail(
    before: str,
    after: str,
    *,
    matched: bool,
    old_text: str = "",
    new_text: str = "",
) -> dict[str, object]:
    """Return raw edit detail; the public snapshot builder sanitizes its text."""
    if not isinstance(before, str) or not isinstance(after, str):
        raise TypeError("edit contents must be strings")
    if not matched:
        return {"matched": False, "lines": _unlocated_edit_lines(old_text, new_text)}
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    lines: list[dict[str, object]] = []
    for group in SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_grouped_opcodes(3):
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == "equal":
                lines.extend(
                    _edit_line(
                        "context",
                        old_lines[old_index],
                        old_index + 1,
                        new_index + 1,
                    )
                    for old_index, new_index in zip(
                        range(old_start, old_end),
                        range(new_start, new_end),
                        strict=True,
                    )
                )
            elif tag == "replace":
                lines.extend(
                    _edit_line("removed", old_lines[index], index + 1, None)
                    for index in range(old_start, old_end)
                )
                lines.extend(
                    _edit_line("added", new_lines[index], None, index + 1)
                    for index in range(new_start, new_end)
                )
            elif tag == "delete":
                lines.extend(
                    _edit_line("removed", old_lines[index], index + 1, None)
                    for index in range(old_start, old_end)
                )
            elif tag == "insert":
                lines.extend(
                    _edit_line("added", new_lines[index], None, index + 1)
                    for index in range(new_start, new_end)
                )
    return {"matched": True, "lines": lines}


def _safe_edit_lines(
    value: object, *, allow_unlocated: bool
) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    safe: list[dict[str, object]] = []
    for line in value:
        if not isinstance(line, Mapping):
            continue
        kind = line.get("kind")
        text = line.get("text")
        old_line = line.get("old_line")
        new_line = line.get("new_line")
        if kind not in {"context", "removed", "added"} or not isinstance(text, str):
            continue
        if old_line is not None and (
            isinstance(old_line, bool) or not isinstance(old_line, int) or old_line <= 0
        ):
            old_line = None
        if new_line is not None and (
            isinstance(new_line, bool) or not isinstance(new_line, int) or new_line <= 0
        ):
            new_line = None
        if kind == "context" and (old_line is None or new_line is None):
            continue
        if kind == "removed" and (
            new_line is not None or (old_line is None and not allow_unlocated)
        ):
            continue
        if kind == "added" and (
            old_line is not None or (new_line is None and not allow_unlocated)
        ):
            continue
        safe.append(
            _edit_line(
                kind,
                _bounded_text(text),
                old_line,
                new_line,
            )
        )
    return safe


def _prepared_payload(
    request: ToolRequest, workspace: WorkspaceIdentity | None = None
) -> tuple[dict[str, object], str | None]:
    arguments = request.arguments
    kind = _kind(request.name)
    if kind is ToolPresentationKind.GENERIC:
        return {}, "tool details are unavailable"
    if kind in {
        ToolPresentationKind.READ,
        ToolPresentationKind.WRITE,
        ToolPresentationKind.EDIT,
    }:
        path = _safe_relative_path(arguments.get("path"), workspace)
        if path is None:
            if kind is ToolPresentationKind.WRITE:
                content = arguments.get("content")
                safe_content = (
                    _bounded_text(content) if isinstance(content, str) else ""
                )
                return {
                    "path": "",
                    "content": safe_content,
                }, "file path is not available"
            if kind is ToolPresentationKind.EDIT:
                return {
                    "path": "",
                    "matched": None,
                    "lines": [],
                }, "file path is not available"
            return {"path": ""}, "file path is not available"
    if kind is ToolPresentationKind.READ:
        if set(arguments) != {"path"}:
            return {"path": ""}, "file path is not available"
        return {"path": path}, None
    if kind is ToolPresentationKind.WRITE:
        content = arguments.get("content")
        if set(arguments) != {"path", "content"} or not isinstance(content, str):
            return {"path": path, "content": ""}, "file content is not available"
        return {"path": path, "content": _bounded_text(content)}, None
    if kind is ToolPresentationKind.EDIT:
        if (
            set(arguments) != {"path", "old_text", "new_text"}
            or not isinstance(arguments.get("old_text"), str)
            or not arguments.get("old_text")
            or not isinstance(arguments.get("new_text"), str)
        ):
            return {
                "path": path,
                "matched": False,
                "lines": [],
            }, "file edit is not available"
        return {
            "path": path,
            "matched": None,
            "lines": _unlocated_edit_lines(
                arguments["old_text"], arguments["new_text"]
            ),
        }, None
    if kind is ToolPresentationKind.BASH:
        command = arguments.get("command")
        if (
            set(arguments) != {"command"}
            or not isinstance(command, str)
            or not command.strip()
        ):
            return {"command": ""}, "command is not available"
        return {"command": sanitize_presentation_text(command)}, None
    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        return {"question": ""}, "question is not available"
    return {"question": _bounded_text(question)}, None


def prepare_tool_presentation(
    request: ToolRequest,
    *,
    workspace: WorkspaceIdentity | None = None,
) -> ToolPresentationSnapshot:
    """Build one safe PENDING snapshot without executing the tool."""
    if not isinstance(request, ToolRequest):
        raise TypeError("request must be a ToolRequest")
    if workspace is not None and not isinstance(workspace, WorkspaceIdentity):
        raise TypeError("workspace must be a WorkspaceIdentity or None")
    payload, message = _prepared_payload(request, workspace)
    return ToolPresentationSnapshot(
        request.call_id,
        request.name,
        _kind(request.name),
        ToolPresentationStatus.PENDING,
        payload,
        message,
    )


def _terminal_status(status: ToolResultStatus) -> ToolPresentationStatus:
    if status is ToolResultStatus.SUCCESS:
        return ToolPresentationStatus.SUCCESS
    if status is ToolResultStatus.CANCELLED:
        return ToolPresentationStatus.CANCELLED
    return ToolPresentationStatus.FAILED


def complete_tool_presentation(
    request: ToolRequest,
    result: ToolResult,
    *,
    detail: Mapping[str, object] | None = None,
    workspace: WorkspaceIdentity | None = None,
) -> ToolPresentationSnapshot:
    """Build a final snapshot while keeping ToolResult data separate."""
    if not isinstance(request, ToolRequest):
        raise TypeError("request must be a ToolRequest")
    if not isinstance(result, ToolResult):
        raise TypeError("result must be a ToolResult")
    if result.call_id != request.call_id or result.tool_name != request.name:
        raise ValueError("request and result identity must match")
    pending = prepare_tool_presentation(request, workspace=workspace)
    payload = dict(pending.payload)
    presentation_detail = detail
    if presentation_detail is None and isinstance(result.presentation, Mapping):
        presentation_detail = result.presentation
    if pending.kind is ToolPresentationKind.EDIT:
        matched = result.status is ToolResultStatus.SUCCESS
        if presentation_detail is not None and isinstance(
            presentation_detail.get("matched"), bool
        ):
            matched = presentation_detail["matched"]
        payload["matched"] = matched
        if presentation_detail is not None and isinstance(
            presentation_detail.get("lines"), (list, tuple)
        ):
            payload["lines"] = _safe_edit_lines(
                presentation_detail["lines"],
                allow_unlocated=result.status is not ToolResultStatus.SUCCESS,
            )
        else:
            payload["lines"] = []
    elif pending.kind is ToolPresentationKind.BASH:
        data = result.data
        output = ""
        exit_code: int | None = None
        if isinstance(data, Mapping):
            raw_output = data.get("output")
            raw_exit_code = data.get("exit_code")
            if isinstance(raw_output, str):
                output = _bounded_text(raw_output)
            if (
                isinstance(raw_exit_code, int)
                and not isinstance(raw_exit_code, bool)
                and raw_exit_code >= 0
            ):
                exit_code = raw_exit_code
        payload["output"] = output
        payload["exit_code"] = exit_code
    elif pending.kind is ToolPresentationKind.ASK:
        data = result.data
        answer = ""
        if isinstance(data, Mapping):
            raw_answer = data.get("answer")
            if isinstance(raw_answer, str):
                answer = raw_answer
        elif isinstance(data, str):
            answer = data
        payload["answer"] = _bounded_text(answer)
    if result.status is ToolResultStatus.SUCCESS:
        final_message = None
    elif pending.message is not None:
        final_message = pending.message
    elif pending.kind is ToolPresentationKind.EDIT:
        final_message = "file was not changed"
    elif result.status is ToolResultStatus.CANCELLED:
        final_message = "operation cancelled"
    elif result.status is ToolResultStatus.PERMISSION_DENIED:
        final_message = "permission denied"
    elif result.status is ToolResultStatus.INVALID_ARGUMENTS:
        final_message = "tool arguments are invalid"
    elif pending.kind is ToolPresentationKind.GENERIC:
        final_message = "tool details are unavailable"
    else:
        final_message = "tool execution failed"
    return ToolPresentationSnapshot(
        request.call_id,
        request.name,
        pending.kind,
        _terminal_status(result.status),
        payload,
        final_message,
    )


__all__ = [
    "ToolPresentationKind",
    "ToolPresentationSnapshot",
    "ToolPresentationStatus",
    "complete_tool_presentation",
    "prepare_tool_presentation",
]
