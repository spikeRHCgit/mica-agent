"""P2-04 CLI 交互行为测试。

覆盖：环境变量缺失诊断、导入无副作用（subprocess + audit hook）、
多轮交互复用同一 App、流式渲染、终态渲染、空白输入、EOF 与
Ctrl+C（空闲/活动 Run）以及所有退出路径的事件流与 App 关闭。

测试替身（ScriptedProvider / HangingProvider）只存在于本文件；
不访问真实网络、真实配置与 ~/.mica。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from mica_agent.cli import run_interactive
from mica_agent.context_builder import ContextBuilder
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderReasoningDelta,
    ProviderRequest,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_service import SessionService
from mica_agent.workspace import resolve_workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
SECRET_KEY = "sk-test-secret-123456"


def clean_environment() -> dict[str, str]:
    """剔除全部 MICA_* 变量的环境副本，防止测试受本机配置影响。"""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("MICA_")
    }


def run_module(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mica_agent", *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class ScriptedProvider:
    """按请求次序回放事件序列的测试替身（仅存在于测试）。"""

    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self._scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        index = len(self.requests) - 1
        for event in self._scripts[index]:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1
        tempdir = getattr(self, "_mica_test_tempdir", None)
        if tempdir is not None:
            tempdir.cleanup()


class HangingProvider:
    """挂起的测试替身：可选先产出部分文本，用于活动 Run 取消。"""

    def __init__(self, emit_delta: bool = True) -> None:
        self._emit_delta = emit_delta
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        if self._emit_delta:
            yield ProviderTextDelta(text="partial")
        await asyncio.sleep(30)
        yield Finish()

    async def aclose(self) -> None:
        self.aclose_calls += 1
        tempdir = getattr(self, "_mica_test_tempdir", None)
        if tempdir is not None:
            tempdir.cleanup()


class InputScript:
    """按顺序返回输入行；元素为异常实例时抛出，耗尽后 EOFError。"""

    def __init__(self, *items: object) -> None:
        self._items = list(items)

    async def __call__(self) -> str:
        if not self._items:
            raise EOFError
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Recorder:
    """收集 write 调用的文本。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def text(self) -> str:
        return "".join(self.calls)


def make_session_dependencies(
    provider: object,
) -> tuple[SessionService, ContextBuilder]:
    tempdir = TemporaryDirectory(prefix="mica-cli-test-")
    root = Path(tempdir.name)
    workspace_path = root / "workspace"
    workspace_path.mkdir()
    data_root = root / "mica-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    provider._mica_test_tempdir = tempdir  # type: ignore[attr-defined]
    return service, ContextBuilder(data_root=data_root, workspace=workspace)


async def run_core(
    provider: object,
    *inputs: object,
) -> tuple[int, Recorder, Recorder]:
    out = Recorder()
    err = Recorder()
    service, context_builder = make_session_dependencies(provider)
    code = await run_interactive(
        "https://api.example.test/v1",
        SECRET_KEY,
        "test-model",
        provider_factory=lambda url, key: provider,  # type: ignore[return-value]
        read_line=InputScript(*inputs),
        write_out=out,
        write_err=err,
        session_service=service,
        context_builder=context_builder,
    )
    return code, out, err


# ---------------------------------------------------------------------------
# 环境变量缺失（子进程，两个公共入口）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [("mica",), (sys.executable, "-m", "mica_agent")],
)
def test_cli_missing_all_environment_variables(
    command: tuple[str, ...],
) -> None:
    result = subprocess.run(
        [*command, "cli"],
        capture_output=True,
        text=True,
        env=clean_environment(),
        check=False,
    )

    assert result.returncode == 2
    assert "MICA_API_URL" in result.stderr
    assert "MICA_API_KEY" in result.stderr
    assert "MICA_MODEL" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("missing", ["MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"])
def test_cli_missing_one_environment_variable(missing: str) -> None:
    env = clean_environment()
    env.update(
        MICA_API_URL="https://api.example.test/v1",
        MICA_API_KEY=SECRET_KEY,
        MICA_MODEL="test-model",
    )
    env.pop(missing)

    result = run_module("cli", env=env)

    assert result.returncode == 2
    assert missing in result.stderr
    for present in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
        if present != missing:
            assert present not in result.stderr
    assert SECRET_KEY not in result.stderr
    assert result.stdout == ""


def test_cli_missing_variables_fails_before_any_provider_request() -> None:
    # 缺少配置时 CLI 必须在任何网络/Provider 构造前失败；
    # 通过不提供任何配置 + 断言仅输出诊断验证。
    result = run_module("cli", env=clean_environment())

    assert result.returncode == 2
    assert result.stderr.startswith("mica: missing required environment variables")
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 导入无副作用
# ---------------------------------------------------------------------------


def test_import_cli_is_side_effect_free() -> None:
    code = (
        "import sys\n"
        "def reject(event, arguments):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:\n"
        "        raise RuntimeError(f'prohibited import side effect: {event}')\n"
        "    if event == 'open' and any(flag in str(arguments[1]) for flag in ('w', 'a', 'x', '+')):\n"
        "        raise RuntimeError('attempted file modification')\n"
        "sys.addaudithook(reject)\n"
        "import mica_agent\n"
        "import mica_agent.cli\n"
        "print('ok')\n"
    )
    env = clean_environment()
    env["PYTHONPATH"] = str(SRC)

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# 交互行为（进程内，注入替身）
# ---------------------------------------------------------------------------


def test_multi_turn_reuses_single_app_and_carries_context() -> None:
    provider = ScriptedProvider(
        [
            [ProviderTextDelta(text="hi"), Finish()],
            [ProviderTextDelta(text="yo"), Finish()],
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "hello", "again"))

    assert code == 0
    assert provider.aclose_calls == 1  # 会话结束恰好关闭一次
    assert len(provider.requests) == 2
    # 第二轮复用同一系统头部，并携带第一轮已提交的上下文。
    header = provider.requests[0].messages[:1]
    assert header[0].role is MessageRole.SYSTEM
    assert provider.requests[1].messages == header + (
        Message(role=MessageRole.USER, content="hello"),
        Message(role=MessageRole.ASSISTANT, content="hi"),
        Message(role=MessageRole.USER, content="again"),
    )
    assert out.calls == ["hi", "\n", "yo", "\n"]
    assert err.text == ""


def test_streaming_deltas_are_written_verbatim_in_order() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="Hello,"),
                ProviderTextDelta(text=" world"),
                ProviderTextDelta(text="!"),
                Finish(),
            ]
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "q"))

    assert code == 0
    # 增量原样、按序、无额外空格；RunCompleted 只补一个换行。
    assert out.text == "Hello, world!\n"
    assert err.text == ""


def test_cli_ignores_reasoning_and_keeps_formal_output() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderReasoningDelta("private thought"),
                ProviderTextDelta("formal answer"),
                Finish(),
            ]
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == "formal answer\n"
    assert err.text == ""
    assert "private thought" not in out.text + err.text
    assert provider.requests[0].reasoning_effort is ReasoningEffort.OFF
    assert provider.aclose_calls == 1


def test_completed_run_does_not_double_newline() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="line one\n"),
                ProviderTextDelta(text="line two"),
                Finish(),
            ]
        ]
    )

    code, out, _ = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == "line one\nline two\n"  # 文本自带换行时不重复补


def test_failed_run_renders_category_and_message_then_allows_next_turn() -> None:
    provider = ScriptedProvider(
        [
            [ProviderErrorEvent(error=AuthenticationError("bad key"))],
            [ProviderTextDelta(text="ok"), Finish()],
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "first", "second"))

    assert code == 0
    assert provider.aclose_calls == 1
    assert err.text == "error: authentication: bad key\n"
    assert "error:" not in out.text
    assert out.text == "ok\n"  # 失败轮无文本：不写空行；下一轮正常
    assert len(provider.requests) == 2


def test_blank_lines_are_ignored_without_provider_request() -> None:
    provider = ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])

    code, out, _ = asyncio.run(run_core(provider, "", "   ", "\t", "q"))

    assert code == 0
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[0].messages[1:] == (
        Message(role=MessageRole.USER, content="q"),
    )
    assert out.text == "ok\n"


def test_eof_exits_normally_and_closes_app() -> None:
    provider = ScriptedProvider([])

    code, out, err = asyncio.run(run_core(provider))

    assert code == 0
    assert provider.aclose_calls == 1
    assert provider.requests == []
    assert out.text == ""
    assert err.text == ""


def test_ctrl_c_while_idle_exits_normally() -> None:
    provider = ScriptedProvider([])

    code, _, _ = asyncio.run(run_core(provider, KeyboardInterrupt()))

    assert code == 0
    assert provider.aclose_calls == 1
    assert provider.requests == []


def test_ctrl_c_during_active_run_cancels_stream_and_exits() -> None:
    provider = HangingProvider()
    out = Recorder()
    err = Recorder()
    service, context_builder = make_session_dependencies(provider)

    async def main() -> int:
        task = asyncio.create_task(
            run_interactive(
                "https://api.example.test/v1",
                SECRET_KEY,
                "test-model",
                provider_factory=lambda url, key: provider,
                read_line=InputScript("question"),
                write_out=out,
                write_err=err,
                session_service=service,
                context_builder=context_builder,
            )
        )
        await asyncio.sleep(0.05)  # 等待 Provider 产出部分文本并挂起
        task.cancel()
        return await task  # CLI 必须吞掉取消、完成清理并正常返回

    code = asyncio.run(main())

    assert code == 0
    assert provider.aclose_calls == 1
    assert len(provider.requests) == 1
    # 已产出的文本保留；取消渲染成取消提示而非失败。
    assert out.text == "partial\n"
    assert err.text == "cancelled\n"
    assert "error:" not in err.text


def test_api_key_never_appears_in_any_output() -> None:
    provider = ScriptedProvider(
        [
            [ProviderTextDelta(text="answer"), Finish()],
            [ProviderErrorEvent(error=AuthenticationError("denied"))],
        ]
    )
    code, out, err = asyncio.run(run_core(provider, "first", "second"))

    assert code == 0

    assert SECRET_KEY not in out.text
    assert SECRET_KEY not in err.text
    assert SECRET_KEY not in repr(provider)
    assert SECRET_KEY not in str(provider.requests)


def test_every_exit_path_closes_event_stream_and_app() -> None:
    # EOF、空闲 Ctrl+C、失败轮结束、正常多轮——每条路径都恰好关闭一次。
    for provider, inputs in [
        (ScriptedProvider([]), ()),  # EOF
        (ScriptedProvider([]), (KeyboardInterrupt(),)),  # 空闲 Ctrl+C
        (
            ScriptedProvider([[ProviderErrorEvent(error=AuthenticationError("x"))]]),
            ("q",),
        ),  # 失败轮
        (
            ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]]),
            ("q",),
        ),  # 正常轮
    ]:
        code, _, _ = asyncio.run(run_core(provider, *inputs))
        assert code == 0
        assert provider.aclose_calls == 1


# ---------------------------------------------------------------------------
# 终态换行状态（P2-04 返工）
# ---------------------------------------------------------------------------


def test_failed_run_without_text_leaves_stdout_empty() -> None:
    provider = ScriptedProvider(
        [[ProviderErrorEvent(error=AuthenticationError("denied"))]]
    )

    code, out, err = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == ""  # 无文本：不得写空行
    assert err.text == "error: authentication: denied\n"  # 诊断只进 stderr


def test_empty_text_delta_does_not_change_newline_state() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="line\n"),
                ProviderTextDelta(text=""),
                Finish(),
            ]
        ]
    )

    code, out, _ = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == "line\n"  # 空 delta 原样写出但不重置换行状态


def test_failed_run_after_partial_text_ends_line_once() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderErrorEvent(error=AuthenticationError("boom")),
            ]
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == "partial\n"  # 未以换行结尾时只补一个换行
    assert err.text == "error: authentication: boom\n"


def test_failed_run_after_text_ending_in_newline_does_not_add_blank_line() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial\n"),
                ProviderErrorEvent(error=AuthenticationError("boom")),
            ]
        ]
    )

    code, out, err = asyncio.run(run_core(provider, "q"))

    assert code == 0
    assert out.text == "partial\n"  # 已以换行结尾：不重复补
    assert err.text == "error: authentication: boom\n"


def test_cancelled_run_without_text_keeps_stdout_empty() -> None:
    provider = HangingProvider(emit_delta=False)
    out = Recorder()
    err = Recorder()
    service, context_builder = make_session_dependencies(provider)

    async def main() -> int:
        task = asyncio.create_task(
            run_interactive(
                "https://api.example.test/v1",
                SECRET_KEY,
                "test-model",
                provider_factory=lambda url, key: provider,
                read_line=InputScript("question"),
                write_out=out,
                write_err=err,
                session_service=service,
                context_builder=context_builder,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    code = asyncio.run(main())

    assert code == 0
    assert provider.aclose_calls == 1
    assert out.text == ""  # 无文本取消：不写空行
    assert err.text == "cancelled\n"


# ---------------------------------------------------------------------------
# 纯空白环境变量（P2-04 返工）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "blank"),
    [
        ("MICA_MODEL", "   "),
        ("MICA_MODEL", "\t\n"),
        ("MICA_API_URL", "   "),
        ("MICA_API_KEY", "   "),
    ],
)
def test_cli_blank_environment_variable_is_rejected(
    variable: str,
    blank: str,
) -> None:
    # 审计钩子证明：空白配置被拒绝的路径上没有任何网络/子进程副作用。
    code = (
        "import sys\n"
        "sys.argv = ['mica', 'cli']\n"
        "def reject(event, arguments):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:\n"
        "        raise RuntimeError('prohibited network access')\n"
        "    if event == 'open' and any(flag in str(arguments[1]) for flag in ('w', 'a', 'x', '+')):\n"
        "        raise RuntimeError('prohibited write')\n"
        "sys.addaudithook(reject)\n"
        "import mica_agent\n"
        "from mica_agent.cli import main\n"
        "main()\n"
    )
    env = clean_environment()
    env.update(
        MICA_API_URL="https://api.example.test/v1",
        MICA_API_KEY=SECRET_KEY,
        MICA_MODEL="test-model",
    )
    env[variable] = blank
    env["PYTHONPATH"] = str(SRC)

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2
    assert variable in result.stderr
    assert result.stdout == ""
    assert SECRET_KEY not in result.stderr
    assert "RuntimeError" not in result.stderr  # 未触发审计拦截（无网络请求）
