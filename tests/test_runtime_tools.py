"""bash 与 ask 的公共行为测试。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.interactions import AskOption, AskPrompt, UserInputResponse
from mica_agent.runtime_tools import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_BYTES,
    _read_limited,
    register_runtime_tools,
)
from mica_agent.schema_validation import validate_tool_schema
from mica_agent.tool_contracts import ToolRequest, ToolResultStatus
from mica_agent.tool_registry import DuplicateToolError, ToolRegistry
from mica_agent.workspace import resolve_workspace


async def _answer(interaction_id: str, prompt: AskPrompt, cancellation) -> object:
    raise AssertionError("requester should not be called during registration")


def test_ask_returns_matching_user_response(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
    ) -> UserInputResponse:
        assert prompt == AskPrompt("Continue?")
        return UserInputResponse(interaction_id, "", 0, "kept")

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
    assert result.data == {
        "answer": "",
        "selected_option_index": 0,
        "notes": "kept",
    }


def test_ask_builds_structured_prompt_without_rewriting_input(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    source_options: list[dict[str, object]] = [
        {
            "label": " UTF-8 ",
            "description": "跨平台 文件",
            "recommended": True,
        },
        {"label": "原样 保留"},
    ]
    arguments = {
        "question": "选择 编码？",
        "title": " 文件设置 ",
        "options": source_options,
        "allow_custom": True,
    }
    request = ToolRequest("ask-structured", "ask", arguments)

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
    ) -> UserInputResponse:
        assert prompt == AskPrompt(
            question="选择 编码？",
            title=" 文件设置 ",
            options=(
                AskOption(" UTF-8 ", "跨平台 文件", True),
                AskOption("原样 保留", None, False),
            ),
            allow_custom=True,
        )
        assert isinstance(prompt.options, tuple)
        source_options[0]["label"] = "mutated"
        source_options.append({"label": "late"})
        assert prompt.options[0].label == " UTF-8 "
        return UserInputResponse(interaction_id, "原样 保留", 1, "备注")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(registry.resolve("ask").handler(request, CancellationToken()))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {
        "answer": "原样 保留",
        "selected_option_index": 1,
        "notes": "备注",
    }


def test_ask_structured_fields_use_contract_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
    ) -> UserInputResponse:
        assert prompt == AskPrompt(
            "Choose",
            options=(AskOption("First"), AskOption("Second")),
        )
        return UserInputResponse(interaction_id, "First", 0)

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest(
                "ask-defaults",
                "ask",
                {
                    "question": "Choose",
                    "options": [{"label": "First"}, {"label": "Second"}],
                },
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "question": "Choose",
            "options": [
                {"label": "First", "description": ""},
                {"label": "Second"},
            ],
        },
        {
            "question": "Choose",
            "options": [
                {"label": "First", "description": "   "},
                {"label": "Second"},
            ],
        },
        {"question": "Choose", "options": []},
        {"question": "Choose", "options": [{"label": "Only"}]},
        {
            "question": "Choose",
            "options": [{"label": str(index)} for index in range(5)],
        },
        {
            "question": "Choose",
            "options": [
                {"label": "First", "recommended": True},
                {"label": "Second", "recommended": True},
            ],
        },
        {"question": "Choose", "allow_custom": "true"},
        {
            "question": "Choose",
            "options": [{"label": "First"}, {"label": "Second", "extra": True}],
        },
        {"question": "Choose", "title": 1},
        {"question": "Choose", "options": ["First", "Second"]},
    ],
)
def test_ask_rejects_invalid_structured_arguments_without_requesting_input(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    calls = 0

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
    ) -> UserInputResponse:
        nonlocal calls
        calls += 1
        return UserInputResponse(interaction_id, "unexpected")

    register_runtime_tools(registry, resolve_workspace(workspace), requester)
    result = asyncio.run(
        registry.resolve("ask").handler(
            ToolRequest("ask-invalid-structured", "ask", arguments),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.INVALID_ARGUMENTS
    assert result.message == "invalid question"
    assert result.data is None
    assert calls == 0
    rendered = repr(result)
    for sentinel in ("Choose", "First", "Second", "true", "extra"):
        assert sentinel not in rendered


def test_ask_cancellation_cancels_waiting_requester(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    requester_cancelled = asyncio.Event()

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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
    assert result.data == {
        "answer": "answer",
        "selected_option_index": None,
        "notes": None,
    }


def test_ask_rejects_missing_extra_and_blank_question(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    calls = 0

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
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


@pytest.mark.slow
@pytest.mark.process_tree
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


@pytest.mark.slow
@pytest.mark.process_tree
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


@pytest.mark.slow
@pytest.mark.process_tree
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


@pytest.mark.slow
@pytest.mark.process_tree
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
    started = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        if text == "started\n":
            started.set()

    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, output_reporter=reporter
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-task-cancel",
                    "bash",
                    {"command": "printf 'started\\n'; sleep 5"},
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=3)
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
    started = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        if text == "started\n":
            started.set()

    register_runtime_tools(
        registry, resolve_workspace(workspace), _answer, output_reporter=reporter
    )

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-cancel", "bash", {"command": "printf 'started\\n'; sleep 5"}
                ),
                cancellation,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=3)
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
    assert specs[0].input_schema == {
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
                    "required": ("label",),
                    "additionalProperties": False,
                },
            },
            "allow_custom": {"type": "boolean"},
        },
        "required": ("question",),
        "additionalProperties": False,
    }
    assert specs[1].input_schema == {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1}},
        "required": ("command",),
        "additionalProperties": False,
    }


def test_register_runtime_tools_accepts_output_reporter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[tuple[str, str, str]] = []

    def reporter(call_id: str, tool_name: str, safe_text: str) -> None:
        reported.append((call_id, tool_name, safe_text))

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )
    request = ToolRequest("bash-reporter", "bash", {"command": "printf 'one\ntwo\n'"})
    original_arguments = request.arguments
    result = asyncio.run(registry.resolve("bash").handler(request, CancellationToken()))

    assert result.status is ToolResultStatus.SUCCESS
    assert request.arguments == original_arguments
    assert result.data == {"exit_code": 0, "output": "one\ntwo\n"}
    assert reported == [
        ("bash-reporter", "bash", "one\n"),
        ("bash-reporter", "bash", "two\n"),
    ]


def test_register_runtime_tools_rejects_async_output_reporter(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    async def reporter(_call_id: str, _tool_name: str, _text: str) -> None:
        return None

    with pytest.raises(TypeError, match="synchronous callable"):
        register_runtime_tools(
            registry,
            resolve_workspace(workspace),
            _answer,
            output_reporter=reporter,
        )

    assert registry.specs() == ()


def test_register_runtime_tools_rejects_non_callable_output_reporter(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    with pytest.raises(TypeError, match="output_reporter"):
        register_runtime_tools(
            registry,
            resolve_workspace(workspace),
            _answer,
            output_reporter=object(),  # type: ignore[arg-type]
        )

    assert registry.specs() == ()

    with pytest.raises(ValueError, match="command_timeout_seconds"):
        register_runtime_tools(
            registry,
            resolve_workspace(workspace),
            _answer,
            command_timeout_seconds=0,
            output_reporter=lambda _call_id, _tool_name, _text: None,
        )
    assert registry.specs() == ()


class _ChunkedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int) -> bytes:
        assert size > 0
        return next(self._chunks, b"")


def _tail_wait_command() -> str:
    return (
        "printf 'complete line\\nunterminated tail'; "
        ": > waiting.marker; "
        "while [ ! -f release.marker ]; do sleep 0.01; done; "
        ": > after.marker"
    )


async def _wait_for_marker(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0)


def test_limited_reader_uses_incremental_utf8_and_complete_records() -> None:
    chunks = [b"alpha \xe4", b"\xbd\xa0\r", b"\npartial \xff", b"\n"]
    reported: list[tuple[str, str, str]] = []

    def reporter(call_id: str, tool_name: str, safe_text: str) -> None:
        reported.append((call_id, tool_name, safe_text))

    output, exceeded = asyncio.run(
        _read_limited(
            _ChunkedStream(chunks),
            100,
            reporter=reporter,
            call_id="call-1",
            tool_name="bash",
        )
    )

    assert not exceeded
    assert output == "alpha 你\r\npartial �\n"
    assert reported == [
        ("call-1", "bash", "alpha 你\r\n"),
        ("call-1", "bash", "partial �\n"),
    ]


def test_limited_reader_reports_eof_partial_record_without_empty_records() -> None:
    reported: list[str] = []

    output, exceeded = asyncio.run(
        _read_limited(
            _ChunkedStream([b"one\r", b"\n\npartial"]),
            100,
            reporter=lambda _call_id, _tool_name, text: reported.append(text),
            call_id="call-1",
        )
    )

    assert not exceeded
    assert output == "one\r\n\npartial"
    assert reported == ["one\r\n", "\n", "partial"]


def test_limited_reader_without_reporter_keeps_complete_decoded_output() -> None:
    output, exceeded = asyncio.run(
        _read_limited(_ChunkedStream([b"plain \xe4", b"\xbd\xa0\npartial"]), 100)
    )

    assert not exceeded
    assert output == "plain 你\npartial"


def test_limited_reader_counts_raw_bytes_around_utf8_limit() -> None:
    exact_payload = b"a" * (MAX_COMMAND_OUTPUT_BYTES - 3) + "你".encode()
    output, exceeded = asyncio.run(
        _read_limited(_ChunkedStream([exact_payload]), MAX_COMMAND_OUTPUT_BYTES)
    )

    assert not exceeded
    assert output is not None
    assert output.endswith("你")
    assert len(output.encode("utf-8")) == MAX_COMMAND_OUTPUT_BYTES

    over_payload = exact_payload + b"x"
    output, exceeded = asyncio.run(
        _read_limited(_ChunkedStream([over_payload]), MAX_COMMAND_OUTPUT_BYTES)
    )

    assert exceeded
    assert output is None


def test_limited_reader_sanitizes_each_complete_record_after_chunk_assembly() -> None:
    payload = (
        "plain 中文  继续\n"
        "TOKEN=token-sensitive-marker with spaces\n"
        "api_key='single-sensitive-marker'\n"
        'authorization="double-sensitive-marker"\n'
        'client_secret=\\"escaped-sensitive-marker\\"\n'
        "Authorization: Bearer bearer-sensitive-marker\n"
        "sk-sk-sensitive-marker\n"
        "normal  text\n"
    )
    sensitive_values = [
        "token-sensitive-marker",
        "single-sensitive-marker",
        "double-sensitive-marker",
        "escaped-sensitive-marker",
        "bearer-sensitive-marker",
        "sk-sensitive-marker",
    ]
    reported: list[tuple[str, str, str]] = []

    output, exceeded = asyncio.run(
        _read_limited(
            _ChunkedStream(
                [
                    payload.encode("utf-8")[index : index + 3]
                    for index in range(0, len(payload.encode("utf-8")), 3)
                ]
            ),
            1000,
            reporter=lambda call_id, tool_name, text: reported.append(
                (call_id, tool_name, text)
            ),
            call_id="call-credentials",
            tool_name="bash",
        )
    )

    assert not exceeded
    assert output == payload
    report_text = "".join(text for _call_id, _tool_name, text in reported)
    assert all(value not in report_text for value in sensitive_values)
    assert all(value not in repr(reported) for value in sensitive_values)
    assert any("plain 中文  继续\n" == text for _call_id, _name, text in reported)
    assert any("normal  text\n" == text for _call_id, _name, text in reported)
    assert all(call_id == "call-credentials" for call_id, _name, _text in reported)
    assert all(tool_name == "bash" for _call_id, tool_name, _text in reported)
    assert all(text for _call_id, _tool_name, text in reported)


def test_bash_reported_output_is_preserved_before_token_cancellation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-report-cancel",
                    "bash",
                    {"command": "printf 'before cancel\\n'; sleep 5"},
                ),
                cancellation,
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.CANCELLED
    assert result.message == "operation cancelled"
    assert reported == ["before cancel\n"]


def test_bash_reported_output_is_preserved_before_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        command_timeout_seconds=0.2,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-report-timeout",
                    "bash",
                    {"command": "printf 'before timeout\\n'; sleep 5"},
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command timed out"
    assert reported == ["before timeout\n"]


def test_bash_external_task_cancellation_re_raises_after_reporting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-report-task-cancel",
                    "bash",
                    {"command": "printf 'before task cancel\\n'; sleep 5"},
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    assert reported == ["before task cancel\n"]


def test_bash_token_cancellation_closes_unterminated_report(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = workspace / "release.marker"
    after = workspace / "after.marker"
    waiting = workspace / "waiting.marker"
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-tail-cancel",
                    "bash",
                    {"command": _tail_wait_command()},
                ),
                cancellation,
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        await asyncio.wait_for(_wait_for_marker(waiting), timeout=3)
        assert reported == ["complete line\n"]
        cancellation.cancel()
        result = await asyncio.wait_for(task, timeout=3)
        assert reported == ["complete line\n"]
        release.write_text("release", encoding="utf-8")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_wait_for_marker(after), timeout=0.3)
        current = asyncio.current_task()
        assert current is not None
        assert {live for live in asyncio.all_tasks() if live is not current} == set()
        return result

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.CANCELLED
    assert result.message == "operation cancelled"
    assert result.data is None
    assert reported == ["complete line\n"]


def test_bash_external_cancellation_closes_unterminated_report_and_reraises(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = workspace / "release.marker"
    after = workspace / "after.marker"
    waiting = workspace / "waiting.marker"
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-tail-task-cancel",
                    "bash",
                    {"command": _tail_wait_command()},
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        await asyncio.wait_for(_wait_for_marker(waiting), timeout=3)
        assert reported == ["complete line\n"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert reported == ["complete line\n"]
        release.write_text("release", encoding="utf-8")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_wait_for_marker(after), timeout=0.3)
        current = asyncio.current_task()
        assert current is not None
        assert {live for live in asyncio.all_tasks() if live is not current} == set()

    asyncio.run(scenario())
    assert reported == ["complete line\n"]


def test_bash_timeout_closes_unterminated_report_and_reaps_process_tree(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = workspace / "release.marker"
    after = workspace / "after.marker"
    waiting = workspace / "waiting.marker"
    registry = ToolRegistry()
    reported: list[str] = []
    first_record = asyncio.Event()

    def reporter(_call_id: str, _tool_name: str, text: str) -> None:
        reported.append(text)
        first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        command_timeout_seconds=1.0,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-tail-timeout",
                    "bash",
                    {"command": _tail_wait_command()},
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        await asyncio.wait_for(_wait_for_marker(waiting), timeout=3)
        assert reported == ["complete line\n"]
        result = await asyncio.wait_for(task, timeout=5)
        assert reported == ["complete line\n"]
        release.write_text("release", encoding="utf-8")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_wait_for_marker(after), timeout=0.3)
        current = asyncio.current_task()
        assert current is not None
        assert {live for live in asyncio.all_tasks() if live is not current} == set()
        return result

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command timed out"
    assert result.data is None
    assert reported == ["complete line\n"]


def test_bash_output_limit_stops_new_reports_after_excess_byte(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=lambda _call_id, _tool_name, text: reported.append(text),
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-report-limit",
                "bash",
                {"command": "printf 'before limit\\n'; head -c 262145 /dev/zero"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output exceeded limit"
    assert result.data is None
    assert reported == ["before limit\n"]


def test_ask_never_calls_bash_output_reporter_on_success_failure_or_cancel(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    requester_started = asyncio.Event()

    async def requester(
        interaction_id: str, prompt: AskPrompt, cancellation: CancellationToken
    ) -> UserInputResponse:
        if interaction_id == "ask-success":
            return UserInputResponse(interaction_id, "answer")
        requester_started.set()
        await asyncio.Future()
        raise AssertionError("requester should have been cancelled")

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        requester,
        output_reporter=lambda _call_id, _tool_name, text: reported.append(text),
    )

    async def scenario() -> tuple[object, object, object]:
        handler = registry.resolve("ask").handler
        success = await handler(
            ToolRequest("ask-success", "ask", {"question": "Question?"}),
            CancellationToken(),
        )
        failure = await handler(
            ToolRequest("ask-failure", "ask", {"question": "Question?", "extra": 1}),
            CancellationToken(),
        )
        cancellation = CancellationToken()
        task = asyncio.create_task(
            handler(
                ToolRequest("ask-cancel", "ask", {"question": "Wait?"}),
                cancellation,
            )
        )
        await asyncio.wait_for(requester_started.wait(), timeout=1)
        cancellation.cancel()
        cancelled = await asyncio.wait_for(task, timeout=1)
        return success, failure, cancelled

    success, failure, cancelled = asyncio.run(scenario())

    assert success.status is ToolResultStatus.SUCCESS
    assert success.data == {
        "answer": "answer",
        "selected_option_index": None,
        "notes": None,
    }
    assert failure.status is ToolResultStatus.INVALID_ARGUMENTS
    assert cancelled.status is ToolResultStatus.CANCELLED
    assert reported == []


def test_bash_reports_complete_output_while_process_is_running(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = workspace / "release.marker"
    registry = ToolRegistry()
    reported: list[tuple[str, str, str]] = []
    first_record = asyncio.Event()

    def reporter(call_id: str, tool_name: str, safe_text: str) -> None:
        reported.append((call_id, tool_name, safe_text))
        if safe_text == "first record\n":
            first_record.set()

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-streaming",
                    "bash",
                    {
                        "command": "printf 'first record\\n'; "
                        'while [ ! -f "$PWD/release.marker" ]; do '
                        "sleep 0.01; "
                        "done; printf 'second record\\n'"
                    },
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(first_record.wait(), timeout=3)
        assert not task.done()
        release.write_text("release", encoding="utf-8")
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {
        "exit_code": 0,
        "output": "first record\nsecond record\n",
    }
    assert reported == [
        ("bash-streaming", "bash", "first record\n"),
        ("bash-streaming", "bash", "second record\n"),
    ]


def test_bash_reporter_follows_merged_stream_order_and_keeps_result_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=lambda _call_id, _tool_name, text: reported.append(text),
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-merged-stream",
                "bash",
                {"command": "printf 'stdout\\n'; printf 'stderr\\n' >&2; exit 7"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command exited with non-zero status"
    assert result.data == {
        "exit_code": 7,
        "output": "stdout\nstderr\n",
    }
    assert reported == ["stdout\n", "stderr\n"]


def test_bash_reporter_reports_eof_tail_on_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=lambda _call_id, _tool_name, text: reported.append(text),
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-eof-tail-success",
                "bash",
                {"command": "printf 'complete line\\ntail without newline'"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {
        "exit_code": 0,
        "output": "complete line\ntail without newline",
    }
    assert reported == ["complete line\n", "tail without newline"]


def test_bash_reporter_reports_eof_tail_on_nonzero_exit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    reported: list[str] = []
    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=lambda _call_id, _tool_name, text: reported.append(text),
    )

    result = asyncio.run(
        registry.resolve("bash").handler(
            ToolRequest(
                "bash-eof-tail-failure",
                "bash",
                {"command": "printf 'complete line\\ntail without newline'; exit 7"},
            ),
            CancellationToken(),
        )
    )

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command exited with non-zero status"
    assert result.data == {
        "exit_code": 7,
        "output": "complete line\ntail without newline",
    }
    assert reported == ["complete line\n", "tail without newline"]


def test_bash_reporter_exception_is_safe_failure_and_reaps_process(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    marker = "reporter-sensitive-exception-marker"

    def reporter(_call_id: str, _tool_name: str, _safe_text: str) -> None:
        raise RuntimeError(marker)

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,
    )

    async def scenario() -> object:
        return await asyncio.wait_for(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-reporter-error",
                    "bash",
                    {"command": "printf 'safe output\\n'; sleep 5"},
                ),
                CancellationToken(),
            ),
            timeout=3,
        )

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output reporting failed"
    assert result.data is None
    assert marker not in result.message
    assert "safe output" not in result.message
    assert "traceback" not in result.message.lower()


def test_bash_sync_reporter_returning_awaitable_is_safe_failure_and_reaped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = workspace / "release.marker"
    after = workspace / "after.marker"
    waiting = workspace / "waiting.marker"
    registry = ToolRegistry()
    reporter_called = asyncio.Event()
    returned: list[object] = []

    async def pending_report() -> None:
        await asyncio.Future()

    def reporter(_call_id: str, _tool_name: str, _text: str) -> object:
        coroutine = pending_report()
        returned.append(coroutine)
        reporter_called.set()
        return coroutine

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        _answer,
        output_reporter=reporter,  # type: ignore[arg-type]
        command_timeout_seconds=1.0,
    )

    async def scenario() -> object:
        task = asyncio.create_task(
            registry.resolve("bash").handler(
                ToolRequest(
                    "bash-awaitable-reporter",
                    "bash",
                    {
                        "command": (
                            ": > waiting.marker; "
                            "printf 'complete line\\n'; "
                            "while [ ! -f release.marker ]; do sleep 0.01; done; "
                            ": > after.marker"
                        )
                    },
                ),
                CancellationToken(),
            )
        )
        await asyncio.wait_for(reporter_called.wait(), timeout=3)
        await asyncio.wait_for(_wait_for_marker(waiting), timeout=3)
        result = await asyncio.wait_for(task, timeout=3)
        release.write_text("release", encoding="utf-8")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_wait_for_marker(after), timeout=0.3)
        current = asyncio.current_task()
        assert current is not None
        assert {live for live in asyncio.all_tasks() if live is not current} == set()
        return result

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert result.message == "command output reporting failed"
    assert result.data is None
    assert len(returned) == 1
    assert getattr(returned[0], "cr_frame", None) is None
