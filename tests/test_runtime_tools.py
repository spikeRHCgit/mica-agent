"""P5-04 bash 与 ask 的公共行为测试。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.interactions import UserInputResponse
from mica_agent.runtime_tools import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    register_runtime_tools,
)
from mica_agent.schema_validation import validate_tool_schema
from mica_agent.tool_contracts import ToolRequest, ToolResultStatus
from mica_agent.tool_registry import DuplicateToolError, ToolRegistry
from mica_agent.workspace import resolve_workspace


async def _answer(interaction_id: str, question: str, cancellation) -> object:
    raise AssertionError("requester should not be called during registration")


def test_ask_returns_matching_user_response(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ):
        assert question == "Continue?"
        return UserInputResponse(interaction_id, "")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            __import__("mica_agent").ToolRequest(
                "ask-1", "ask", {"question": "Continue?"}
            ),
            CancellationToken(),
        )
    )

    assert result.status.value == "success"
    assert result.message == "user input received"
    assert result.data == ""


def test_ask_cancellation_cancels_waiting_requester(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    requester_cancelled = asyncio.Event()

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            requester_cancelled.set()
            raise

    register_runtime_tools(registry, resolve_workspace(workspace), requester)

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("ask").handler(
                __import__("mica_agent").ToolRequest(
                    "ask-2", "ask", {"question": "Wait"}
                ),
                cancellation,
            )
        )
        await asyncio.sleep(0)
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=0.2)

    result = asyncio.run(scenario())
    assert result.status.value == "cancelled"
    assert result.message == "operation cancelled"
    assert requester_cancelled.is_set()


@pytest.mark.parametrize(
    "response",
    [object(), UserInputResponse("different-interaction", "answer")],
)
def test_ask_rejects_invalid_requester_response(
    tmp_path: Path, response: object
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> object:
        return response

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest("ask-invalid-response", "ask", {"question": "Continue?"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "user input failed"
    assert result.data is None


def test_ask_handler_task_cancellation_waits_for_requester(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    requester_cancelled = asyncio.Event()

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            requester_cancelled.set()
            raise

    register_runtime_tools(registry, resolve_workspace(workspace), requester)

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("ask").handler(
                ToolRequest("ask-task-cancel", "ask", {"question": "Wait"}),
                CancellationToken(),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert requester_cancelled.is_set()


def test_ask_requester_exception_is_safe_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        raise RuntimeError("secret exception detail")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest("ask-error", "ask", {"question": "Continue?"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "user input failed"
    assert result.data is None
    assert "secret exception detail" not in result.message


def test_ask_does_not_call_requester_when_already_cancelled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    called = False

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        nonlocal called
        called = True
        return UserInputResponse(interaction_id, "unexpected")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    cancellation = CancellationToken()
    cancellation.cancel()
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest("ask-pre-cancelled", "ask", {"question": "Continue?"}),
            cancellation,
        )
    )

    assert result.status is ToolResultStatus.CANCELLED
    assert result.message == "operation cancelled"
    assert result.data is None
    assert not called


def test_ask_accepts_requester_returning_future(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> asyncio.Future[UserInputResponse]:
        future: asyncio.Future[UserInputResponse] = (
            asyncio.get_running_loop().create_future()
        )
        future.set_result(UserInputResponse(interaction_id, "answer"))
        return future

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest("ask-future", "ask", {"question": "Continue?"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == "answer"


def test_ask_rejects_missing_extra_and_blank_question(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    calls = 0

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        nonlocal calls
        calls += 1
        return UserInputResponse(interaction_id, "answer")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)

    async def scenario() -> list[object]:
        handler = registry.resolve("ask").handler
        token = CancellationToken()
        return [
            await handler(ToolRequest("ask-3", "ask", {}), token),
            await handler(
                ToolRequest("ask-4", "ask", {"question": "x", "extra": True}), token
            ),
            await handler(ToolRequest("ask-5", "ask", {"question": "  "}), token),
        ]

    results = asyncio.run(scenario())
    assert [result.status for result in results] == [
        ToolResultStatus.INVALID_ARGUMENTS
    ] * 3
    assert [result.message for result in results] == ["invalid question"] * 3
    assert calls == 0


def test_bash_discovers_git_bash_and_uses_unicode_workspace_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "工作区 space"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-1", "bash", {"command": "pwd -W"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.message == "command completed"
    assert result.data is not None
    assert result.data["exit_code"] == 0
    assert result.data["output"].strip() == workspace.as_posix()


def test_existing_non_executable_bash_returns_safe_start_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_bash = workspace / "bash.exe"
    fake_bash.write_bytes(b"not an executable")
    registry = ToolRegistry()
    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, bash_executable=fake_bash
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-start-failure", "bash", {"command": "echo ignored"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "bash failed to start"
    assert result.data is None
    assert str(fake_bash) not in result.message


@pytest.mark.parametrize("directory_name", ["System32", "WindowsApps"])
def test_path_bash_launcher_is_not_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher_dir = tmp_path / directory_name
    launcher_dir.mkdir()
    (launcher_dir / "bash.exe").write_bytes(b"launcher")
    monkeypatch.setenv("PATH", str(launcher_dir))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-git"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local-git"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-x86-git"))
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-path-launcher", "bash", {"command": "echo ignored"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "Bash is unavailable; install Git for Windows"
    assert result.data is None


def test_windowsapps_bash_launcher_is_not_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    launcher = (
        Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps" / "bash.exe"
    )
    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, bash_executable=launcher
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-windowsapps", "bash", {"command": "echo ignored"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "Bash is unavailable; install Git for Windows"
    assert result.data is None
    assert "WindowsApps" not in result.message


def test_system32_bash_launcher_is_not_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    system_root = Path(os.environ["SystemRoot"])
    registry = ToolRegistry()
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        bash_executable=system_root / "System32" / "bash.exe",
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-system32", "bash", {"command": "echo ignored"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "Bash is unavailable; install Git for Windows"
    assert result.data is None
    assert "System32" not in result.message


def test_missing_explicit_bash_returns_safe_unavailable_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        bash_executable=workspace / "missing" / "bash.exe",
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-missing", "bash", {"command": "echo ignored"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "Bash is unavailable; install Git for Windows"
    assert result.data is None
    assert str(workspace) not in result.message


@pytest.mark.skipif(os.name != "nt", reason="Windows Git installation boundary")
def test_bash_discovers_bash_from_git_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git.exe")
    assert git is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-program-files-git"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-localappdata-git"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-x86-git"))
    monkeypatch.setenv("PATH", str(Path(git).parent))
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-path-git", "bash", {"command": "printf path-derived"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"exit_code": 0, "output": "path-derived"}


def test_bash_missing_cwd_returns_safe_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = resolve_workspace(workspace)
    workspace.rmdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, identity, _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-missing-cwd",
                "bash",
                {"command": "printf cwd-secret-sentinel"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "bash failed to start"
    assert result.data is None
    assert str(workspace) not in result.message
    assert "cwd-secret-sentinel" not in result.message
    assert "traceback" not in result.message.lower()


def test_bash_empty_output_is_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-empty", "bash", {"command": ":"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"exit_code": 0, "output": ""}


def test_bash_unicode_output_is_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-unicode", "bash", {"command": "printf '你好，世界 🌍'"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"exit_code": 0, "output": "你好，世界 🌍"}


def test_bash_invalid_utf8_uses_replacement_character(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-invalid-utf8", "bash", {"command": "printf '\\377'"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"exit_code": 0, "output": "�"}
    assert "traceback" not in result.message.lower()


def test_bash_accepts_output_at_exact_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-exact-limit", "bash", {"command": "head -c 262144 /dev/zero"}
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data is not None
    assert len(result.data["output"]) == 262_144


def test_bash_stops_infinite_output_at_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, command_timeout_seconds=5
    )

    async def scenario() -> object:
        return await asyncio.wait_for(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-infinite", "bash", {"command": "while :; do printf x; done"}
                ),
                CancellationToken(),
            ),
            timeout=3,
        )

    result = asyncio.run(scenario())
    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output exceeded limit"
    assert result.data is None


def test_bash_stops_at_output_limit_without_returning_partial_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-limit", "bash", {"command": "head -c 262145 /dev/zero"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output exceeded limit"
    assert result.data is None


def test_bash_combines_streams_and_reports_nonzero_exit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-nonzero",
                "bash",
                {"command": "printf out; printf err >&2; exit 7"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command exited with non-zero status"
    assert result.data == {"exit_code": 7, "output": "outerr"}


def test_bash_environment_filters_sensitive_and_injection_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    injection_markers = [
        workspace / "bash-env.marker",
        workspace / "env.marker",
        workspace / "prompt.marker",
        workspace / "function.marker",
    ]
    bash_env = workspace / "bash_env.sh"
    bash_env.write_text(
        'printf executed > \\"$PWD/bash-env.marker\\"\\n', encoding="utf-8"
    )
    env_script = workspace / "env.sh"
    env_script.write_text(
        'printf executed > \\"$PWD/env.marker\\"\\n', encoding="utf-8"
    )
    values = {
        "MICA_API_KEY": "mica-secret-sentinel",
        "OPENAI_API_KEY": "openai-secret-sentinel",
        "ANTHROPIC_API_KEY": "anthropic-secret-sentinel",
        "AWS_ACCESS_KEY_ID": "aws-id-sentinel",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-sentinel",
        "CUSTOM_API_KEY": "custom-api-sentinel",
        "CUSTOM_TOKEN": "custom-token-sentinel",
        "CUSTOM_SECRET": "custom-secret-sentinel",
        "CUSTOM_PASSWORD": "custom-password-sentinel",
        "CUSTOM_CREDENTIAL": "custom-credential-sentinel",
        "CUSTOM_CREDENTIALS": "custom-credentials-sentinel",
        "custom_api_key": "lower-api-sentinel",
        "custom_token": "lower-token-sentinel",
        "custom_secret": "lower-secret-sentinel",
        "custom_password": "lower-password-sentinel",
        "custom_credential": "lower-credential-sentinel",
        "custom_credentials": "lower-credentials-sentinel",
        "BASH_ENV": str(bash_env),
        "ENV": str(env_script),
        "PROMPT_COMMAND": 'printf executed > \\"$PWD/prompt.marker\\"',
        "BASH_FUNC_mica%%": '() { printf executed > \\"$PWD/function.marker\\"; }',
        "MICA_NORMAL_SENTINEL": "normal-sentinel",
    }
    filtered_names = [
        "MICA_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CUSTOM_API_KEY",
        "CUSTOM_TOKEN",
        "CUSTOM_SECRET",
        "CUSTOM_PASSWORD",
        "CUSTOM_CREDENTIAL",
        "CUSTOM_CREDENTIALS",
        "custom_api_key",
        "custom_token",
        "custom_secret",
        "custom_password",
        "custom_credential",
        "custom_credentials",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
    ]
    filtered_command = "printf '%s|' " + " ".join(
        f'"${{{name}-unset}}"' for name in filtered_names
    )
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-env-complete",
                "bash",
                {
                    "command": filtered_command,
                },
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data is not None
    assert result.data["output"] == "".join("unset|" for _ in filtered_names)

    normal_result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-normal-env",
                "bash",
                {
                    "command": "printf '%s|%s|%s|%s|%s' "
                    '"${MICA_NORMAL_SENTINEL-unset}" '
                    '"${PATH:+present}" '
                    '"${SYSTEMROOT:+present}" '
                    '"${TEMP:+present}" '
                    '"${HOME:+present}"'
                },
            ),
            CancellationToken(),
        )
    )
    assert normal_result.status is ToolResultStatus.SUCCESS
    assert normal_result.data == {
        "exit_code": 0,
        "output": "normal-sentinel|present|present|present|present",
    }
    assert all(not marker.exists() for marker in injection_markers)
    assert all(value not in result.message for value in values.values())


def test_bash_filters_credentials_but_preserves_normal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MICA_API_KEY", "do-not-leak")
    monkeypatch.setenv("NORMAL_RUNTIME_SENTINEL", "available")
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-env",
                "bash",
                {
                    "command": 'printf \'%s:%s\' "${MICA_API_KEY-unset}" "${NORMAL_RUNTIME_SENTINEL-unset}"'
                },
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"exit_code": 0, "output": "unset:available"}


def test_bash_output_limit_reaps_real_delayed_descendant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        command_timeout_seconds=5,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-descendant-output-limit",
                    "bash",
                    {
                        "command": 'printf started > "$PWD/started.marker"; '
                        '(sleep 2; printf alive > "$PWD/alive.marker") & '
                        "yes x"
                    },
                ),
                CancellationToken(),
            )
        )
        started = workspace / "started.marker"
        deadline = asyncio.get_running_loop().time() + 2
        while not started.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert started.exists()
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())
    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output exceeded limit"
    assert result.data is None
    time.sleep(2.3)
    assert not (workspace / "alive.marker").exists()


def test_bash_timeout_reaps_real_delayed_descendant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        command_timeout_seconds=0.2,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-descendant-timeout",
                    "bash",
                    {
                        "command": 'printf started > "$PWD/started.marker"; '
                        '(sleep 2; printf alive > "$PWD/alive.marker") & sleep 5'
                    },
                ),
                CancellationToken(),
            )
        )
        started = workspace / "started.marker"
        deadline = asyncio.get_running_loop().time() + 2
        while not started.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert started.exists()
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())
    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command timed out"
    assert result.data is None
    time.sleep(2.3)
    assert not (workspace / "alive.marker").exists()


def test_bash_token_cancellation_reaps_real_delayed_descendant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-descendant-cancel",
                    "bash",
                    {
                        "command": 'printf started > "$PWD/started.marker"; '
                        '(sleep 2; printf alive > "$PWD/alive.marker") & sleep 5'
                    },
                ),
                cancellation,
            )
        )
        started = workspace / "started.marker"
        deadline = asyncio.get_running_loop().time() + 2
        while not started.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert started.exists()
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())
    assert result.status is ToolResultStatus.CANCELLED
    assert result.message == "operation cancelled"
    assert result.data is None
    time.sleep(2.3)
    assert not (workspace / "alive.marker").exists()


def test_bash_handler_task_cancel_reaps_real_delayed_descendant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-descendant-task-cancel",
                    "bash",
                    {
                        "command": 'printf started > "$PWD/started.marker"; '
                        '(sleep 2; printf alive > "$PWD/alive.marker") & sleep 5'
                    },
                ),
                CancellationToken(),
            )
        )
        started = workspace / "started.marker"
        deadline = asyncio.get_running_loop().time() + 2
        while not started.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert started.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    time.sleep(2.3)
    assert not (workspace / "alive.marker").exists()


def test_bash_handler_task_cancellation_re_raises_after_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest("bash-task-cancel", "bash", {"command": "sleep 5"}),
                CancellationToken(),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_bash_timeout_returns_safe_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, command_timeout_seconds=0.05
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest("bash-timeout", "bash", {"command": "sleep 5"}),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command timed out"
    assert result.data is None


def test_bash_running_cancellation_returns_cancelled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest("bash-cancel", "bash", {"command": "sleep 5"}), cancellation
            )
        )
        await asyncio.sleep(0.05)
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=2.0)

    result = asyncio.run(scenario())
    assert result.status is ToolResultStatus.CANCELLED
    assert result.message == "operation cancelled"
    assert result.data is None


def test_register_runtime_tools_rejects_invalid_dependencies(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(TypeError):
        register_runtime_tools(object(), resolve_workspace(workspace), _answer)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_runtime_tools(ToolRegistry(), object(), _answer)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_runtime_tools(ToolRegistry(), resolve_workspace(workspace), object())  # type: ignore[arg-type]


def test_register_runtime_tools_rejects_non_path_executable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(TypeError):
        register_runtime_tools(
            ToolRegistry(),
            resolve_workspace(workspace),
            _answer,
            bash_executable=1,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("timeout", "error"),
    [
        (True, ValueError),
        (0, ValueError),
        (-1.0, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (DEFAULT_COMMAND_TIMEOUT_SECONDS + 1, ValueError),
        ("bad", ValueError),
    ],
)
def test_register_runtime_tools_rejects_invalid_timeout(
    tmp_path: Path, timeout: object, error: type[Exception]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(error):
        register_runtime_tools(
            ToolRegistry(),
            resolve_workspace(workspace),
            _answer,
            command_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("name", ["powershell.exe", "pwsh", "cmd.exe", "sh.exe"])
def test_register_runtime_tools_rejects_non_bash_executable_name(
    tmp_path: Path, name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError):
        register_runtime_tools(
            ToolRegistry(), resolve_workspace(workspace), _answer, bash_executable=name
        )


def test_register_runtime_tools_rejects_duplicate_without_partial_registration(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    with pytest.raises(DuplicateToolError):
        register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    assert [spec.name for spec in registry.specs()] == ["ask", "bash"]


def test_register_runtime_tools_exposes_valid_bash_and_ask_specs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    register_runtime_tools(registry, resolve_workspace(workspace), _answer)

    specs = registry.specs()
    assert [spec.name for spec in specs] == ["ask", "bash"]
    for spec in specs:
        validate_tool_schema(spec)
    assert specs[1].input_schema == {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1}},
        "required": ("command",),
        "additionalProperties": False,
    }
