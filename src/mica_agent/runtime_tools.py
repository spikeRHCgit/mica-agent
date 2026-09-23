"""P4-04 的 bash 与 ask 工具注册和运行时边界。"""

from __future__ import annotations

import asyncio
import codecs
import inspect
import math
import os
import shutil
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Protocol

from .cancellation import CancellationRequested, CancellationToken
from .interactions import AskOption, AskPrompt, UserInputResponse
from .message_sanitizer import sanitize_presentation_text
from .schema_validation import (
    ToolArgumentsValidationError,
    validate_tool_arguments,
)
from .tool_contracts import ToolRequest, ToolResult, ToolResultStatus, ToolSpec
from .tool_registry import DuplicateToolError, ToolRegistry
from .workspace import WorkspaceIdentity

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
MAX_COMMAND_OUTPUT_BYTES = 262_144

ToolOutputReporter = Callable[[str, str, str], None]


class _OutputReportingError(Exception):
    """工具输出展示回调失败，不暴露回调内部异常。"""


class _OutputReporterGate:
    __slots__ = ("_enabled", "_reporter")

    def __init__(self, reporter: ToolOutputReporter) -> None:
        self._enabled = True
        self._reporter = reporter

    def close(self) -> None:
        self._enabled = False

    def __call__(self, call_id: str, tool_name: str, text: str) -> None:
        if self._enabled:
            _invoke_output_reporter(self._reporter, call_id, tool_name, text)


class UserInputRequester(Protocol):
    """向外部请求一次用户文本输入的前端无关协议。"""

    def __call__(
        self,
        interaction_id: str,
        prompt: AskPrompt,
        cancellation: CancellationToken,
    ) -> Awaitable[UserInputResponse]: ...


_BASH_SPEC = ToolSpec(
    name="bash",
    description="Run a Bash command in the workspace.",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1}},
        "required": ["command"],
        "additionalProperties": False,
    },
)
_ASK_SPEC = ToolSpec(
    name="ask",
    description="Request text or a structured choice from the user.",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1},
                        "description": {"type": "string", "minLength": 1},
                        "recommended": {"type": "boolean"},
                    },
                    "required": ["label"],
                    "additionalProperties": False,
                },
            },
            "allow_custom": {"type": "boolean"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
)


def _result(
    request: ToolRequest,
    status: ToolResultStatus,
    message: str,
    data: object = None,
) -> ToolResult:
    return ToolResult(request.call_id, request.name, status, message, data)


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    blocked = {
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "MICA_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
    suffixes = (
        "_API_KEY",
        "_TOKEN",
        "_SECRET",
        "_PASSWORD",
        "_CREDENTIAL",
        "_CREDENTIALS",
    )
    for key in tuple(environment):
        folded = key.upper()
        if (
            folded in blocked
            or folded.startswith("BASH_FUNC_")
            or folded.endswith(suffixes)
        ):
            environment.pop(key, None)
    return environment


def _is_windows_launcher(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return "system32" in parts or "windowsapps" in parts


def _find_bash(executable: Path | None) -> Path | None:
    if executable is not None:
        if os.name == "nt" and _is_windows_launcher(executable):
            return None
        return executable if executable.is_file() else None
    if os.name != "nt":
        found = shutil.which("bash")
        return Path(found) if found else None
    candidates: list[Path] = []
    for variable in ("ProgramFiles", "LOCALAPPDATA", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value:
            suffix = (
                ("Programs", "Git", "bin", "bash.exe")
                if variable == "LOCALAPPDATA"
                else ("Git", "bin", "bash.exe")
            )
            candidates.append(Path(value).joinpath(*suffix))
    git = shutil.which("git.exe") or shutil.which("git")
    if git:
        git_path = Path(git)
        roots = {
            git_path.parent.parent,
            git_path.parent.parent.parent,
        }
        for root in roots:
            candidates.extend(
                (
                    root / "bin" / "bash.exe",
                    root / "usr" / "bin" / "bash.exe",
                    root / "mingw64" / "bin" / "bash.exe",
                )
            )
    for candidate in candidates:
        if candidate.is_file() and not _is_windows_launcher(candidate):
            return candidate
    return None


async def _stop_process_tree(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    if os.name == "nt":
        taskkill: asyncio.subprocess.Process | None = None
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(taskkill.wait(), timeout=2)
        except (TimeoutError, OSError, ProcessLookupError):
            if taskkill is not None and taskkill.returncode is None:
                try:
                    taskkill.kill()
                except ProcessLookupError:
                    pass
                await asyncio.gather(taskkill.wait(), return_exceptions=True)
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=0.2)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


def _invoke_output_reporter(
    reporter: ToolOutputReporter,
    call_id: str,
    tool_name: str,
    text: str,
) -> None:
    try:
        result = reporter(call_id, tool_name, text)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            elif isinstance(result, asyncio.Future):
                result.cancel()
            raise _OutputReportingError
    except _OutputReportingError:
        raise
    except Exception:  # noqa: BLE001 - output reporting is a tool boundary.
        raise _OutputReportingError from None


def _is_async_output_reporter(reporter: object) -> bool:
    call = reporter.__call__ if callable(reporter) else None  # type: ignore[attr-defined]
    return any(
        inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(candidate)
        for candidate in (reporter, call)
    )


def _report_output_record(
    reporter: ToolOutputReporter,
    call_id: str,
    tool_name: str,
    record: str,
) -> None:
    try:
        safe_text = sanitize_presentation_text(record)
        if safe_text:
            _invoke_output_reporter(reporter, call_id, tool_name, safe_text)
    except Exception:  # noqa: BLE001 - output reporting is a tool boundary.
        raise _OutputReportingError from None


async def _read_limited(
    stream: asyncio.StreamReader,
    limit: int,
    *,
    reporter: ToolOutputReporter | None = None,
    call_id: str = "",
    tool_name: str = "bash",
) -> tuple[str | None, bool]:
    output: list[str] = []
    record_buffer = ""
    byte_count = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(min(65_536, limit + 1 - byte_count))
        if not chunk:
            text = decoder.decode(b"", final=True)
            output.append(text)
            if reporter is not None:
                record_buffer += text
                if record_buffer:
                    _report_output_record(reporter, call_id, tool_name, record_buffer)
            return "".join(output), False
        byte_count += len(chunk)
        if byte_count > limit:
            return None, True
        text = decoder.decode(chunk, final=False)
        output.append(text)
        if reporter is None:
            continue
        record_buffer += text
        while (newline := record_buffer.find("\n")) >= 0:
            record = record_buffer[: newline + 1]
            record_buffer = record_buffer[newline + 1 :]
            _report_output_record(reporter, call_id, tool_name, record)


async def _bash_handler(
    request: ToolRequest,
    cancellation: CancellationToken,
    workspace: WorkspaceIdentity,
    executable: Path | None,
    timeout: float,
    output_reporter: ToolOutputReporter | None = None,
) -> ToolResult:
    process: asyncio.subprocess.Process | None = None
    output_task: asyncio.Task[tuple[str | None, bool]] | None = None
    cancellation_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[int] | None = None
    reporter_gate = (
        _OutputReporterGate(output_reporter) if output_reporter is not None else None
    )

    async def cleanup() -> None:
        if reporter_gate is not None:
            reporter_gate.close()
        await _stop_process_tree(process)
        if output_task is not None and not output_task.done():
            try:
                await asyncio.wait_for(output_task, timeout=2)
            except TimeoutError:
                output_task.cancel()
        if output_task is not None:
            await asyncio.gather(output_task, return_exceptions=True)
        for task in (cancellation_task, wait_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (cancellation_task, wait_task) if task),
            return_exceptions=True,
        )
        if process is not None and process.stdout is not None:
            transport = getattr(process.stdout, "_transport", None)
            if transport is not None:
                transport.close()

    try:
        cancellation.raise_if_cancelled()
        arguments = request.arguments
        if set(arguments) != {"command"}:
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid command"
            )
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid command"
            )
        bash = _find_bash(executable)
        if bash is None:
            return _result(
                request,
                ToolResultStatus.EXECUTION_FAILED,
                "Bash is unavailable; install Git for Windows",
            )
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            import subprocess

            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                str(bash),
                "--noprofile",
                "--norc",
                "-c",
                command,
                cwd=workspace.workspace_path,
                env=_clean_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **kwargs,
            )
        except (OSError, ValueError):
            return _result(
                request, ToolResultStatus.EXECUTION_FAILED, "bash failed to start"
            )
        assert process.stdout is not None
        output_task = asyncio.create_task(
            _read_limited(
                process.stdout,
                MAX_COMMAND_OUTPUT_BYTES,
                reporter=reporter_gate,
                call_id=request.call_id,
                tool_name="bash",
            )
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        wait_task = asyncio.create_task(process.wait())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await cleanup()
                return _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "command timed out"
                )
            waiting = {cancellation_task}
            if not wait_task.done():
                waiting.add(wait_task)
            if not output_task.done():
                waiting.add(output_task)
            done, _ = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done:
                await cleanup()
                return _result(
                    request, ToolResultStatus.CANCELLED, "operation cancelled"
                )
            if output_task in done:
                output, exceeded = output_task.result()
                if exceeded:
                    await cleanup()
                    return _result(
                        request,
                        ToolResultStatus.EXECUTION_FAILED,
                        "command output exceeded limit",
                    )
                if wait_task.done():
                    break
            if wait_task in done and output_task.done():
                break
        output, exceeded = await output_task
        if exceeded or output is None:
            await cleanup()
            return _result(
                request,
                ToolResultStatus.EXECUTION_FAILED,
                "command output exceeded limit",
            )
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)
        text = output
        if process.returncode == 0:
            return _result(
                request,
                ToolResultStatus.SUCCESS,
                "command completed",
                {"exit_code": 0, "output": text},
            )
        return _result(
            request,
            ToolResultStatus.EXECUTION_FAILED,
            "command exited with non-zero status",
            {"exit_code": process.returncode, "output": text},
        )
    except _OutputReportingError:
        await cleanup()
        return _result(
            request,
            ToolResultStatus.EXECUTION_FAILED,
            "command output reporting failed",
        )
    except CancellationRequested:
        return _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
    except asyncio.CancelledError:
        await cleanup()
        raise
    except (OSError, ValueError):
        await cleanup()
        return _result(
            request, ToolResultStatus.EXECUTION_FAILED, "bash failed to start"
        )


async def _ask_handler(
    request: ToolRequest,
    cancellation: CancellationToken,
    requester: UserInputRequester,
) -> ToolResult:
    try:
        cancellation.raise_if_cancelled()
        arguments = request.arguments
        try:
            validate_tool_arguments(_ASK_SPEC, arguments)
            options = tuple(
                AskOption(
                    option["label"],
                    option.get("description"),
                    option.get("recommended", False),
                )
                for option in arguments.get("options", ())
            )
            prompt = AskPrompt(
                question=arguments["question"],
                title=arguments.get("title"),
                options=options,
                allow_custom=arguments.get("allow_custom", False),
            )
        except (ToolArgumentsValidationError, TypeError, ValueError):
            return _result(
                request, ToolResultStatus.INVALID_ARGUMENTS, "invalid question"
            )
        requester_task = asyncio.ensure_future(
            requester(request.call_id, prompt, cancellation)
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {requester_task, cancellation_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done:
                requester_task.cancel()
                await asyncio.gather(requester_task, return_exceptions=True)
                return _result(
                    request, ToolResultStatus.CANCELLED, "operation cancelled"
                )
            response = requester_task.result()
            if (
                not isinstance(response, UserInputResponse)
                or response.interaction_id != request.call_id
            ):
                return _result(
                    request, ToolResultStatus.EXECUTION_FAILED, "user input failed"
                )
            return _result(
                request,
                ToolResultStatus.SUCCESS,
                "user input received",
                {
                    "answer": response.answer,
                    "selected_option_index": response.selected_option_index,
                    "notes": response.notes,
                },
            )
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
    except CancellationRequested:
        return _result(request, ToolResultStatus.CANCELLED, "operation cancelled")
    except asyncio.CancelledError:
        if "requester_task" in locals():
            requester_task.cancel()
            await asyncio.gather(requester_task, return_exceptions=True)
        raise
    except Exception:  # noqa: BLE001 - requester exceptions are a tool boundary.
        return _result(request, ToolResultStatus.EXECUTION_FAILED, "user input failed")


def register_runtime_tools(
    registry: ToolRegistry,
    workspace: WorkspaceIdentity,
    user_input_requester: UserInputRequester,
    *,
    bash_executable: str | Path | None = None,
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    output_reporter: ToolOutputReporter | None = None,
) -> None:
    """显式且原子地注册 bash 与 ask，不执行外部行为。"""
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not isinstance(workspace, WorkspaceIdentity):
        raise TypeError("workspace must be a WorkspaceIdentity")
    if not callable(user_input_requester):
        raise TypeError("user_input_requester must be callable")
    if output_reporter is not None:
        if not callable(output_reporter):
            raise TypeError("output_reporter must be callable or None")
        if _is_async_output_reporter(output_reporter):
            raise TypeError("output_reporter must be a synchronous callable")
    if (
        isinstance(command_timeout_seconds, bool)
        or not isinstance(command_timeout_seconds, (int, float))
        or not math.isfinite(command_timeout_seconds)
        or not 0 < command_timeout_seconds <= DEFAULT_COMMAND_TIMEOUT_SECONDS
    ):
        raise ValueError("command_timeout_seconds must be a finite positive limit")
    if bash_executable is not None and not isinstance(bash_executable, (str, Path)):
        raise TypeError("bash_executable must be a str, Path, or None")
    executable = Path(bash_executable) if bash_executable is not None else None
    if executable is not None and executable.name.casefold() not in {
        "bash",
        "bash.exe",
    }:
        raise ValueError("bash_executable must name bash or bash.exe")
    names = {spec.name for spec in registry.specs()}
    if names.intersection({"bash", "ask"}):
        raise DuplicateToolError("tool name is already registered")
    registry.register(
        _BASH_SPEC,
        partial(
            _bash_handler,
            workspace=workspace,
            executable=executable,
            timeout=float(command_timeout_seconds),
            output_reporter=output_reporter,
        ),
    )
    registry.register(_ASK_SPEC, partial(_ask_handler, requester=user_input_requester))


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "MAX_COMMAND_OUTPUT_BYTES",
    "ToolOutputReporter",
    "UserInputRequester",
    "register_runtime_tools",
]
