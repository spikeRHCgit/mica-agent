"""Mica 命令行入口（P2-04）。

公共契约保持不变：无参数输出帮助、``--help``、``--version``、
无效参数退出码 2，两个入口（``mica`` 与 ``python -m mica_agent``）
行为一致。``mica cli`` 启动基础交互对话：从环境变量读取连接参数，
装配 OpenAICompatibleProvider 与 MicaApp，实时渲染 EngineEvent。

``run_interactive`` 是注入式交互核心：测试通过替身驱动
（Provider 工厂、stdin 读取、stdout/stderr 写入），产品默认值
分别构造真实 Provider、读取标准输入并写入标准输出。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from .context_builder import ContextBuilder
from .events import (
    PermissionRequested,
    RunCancelled,
    RunCompleted,
    RunFailed,
    TextDelta,
    ToolFinished,
    ToolStarted,
    UserInputRequested,
)
from .interactions import (
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
)
from .jsonl_store import JsonlSessionStore, StoredSession
from .permissions import PermissionMode
from .session_service import NoHealthySessionError, SessionService
from .workspace import (
    WorkspaceIdentity,
    WorkspaceResolutionError,
    default_data_root,
    resolve_workspace,
)

if TYPE_CHECKING:
    from .app import MicaApp
    from .providers import Provider

ReadLine: TypeAlias = Callable[[], Awaitable[str]]
Write: TypeAlias = Callable[[str], None]
ProviderFactory: TypeAlias = Callable[[str, str], "Provider"]
TurnIdFactory: TypeAlias = Callable[[], str]
Clock: TypeAlias = Callable[[], datetime]
SessionMode: TypeAlias = Literal["new", "continue", "resume"]

_ENVIRONMENT_VARIABLES = ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mica",
        description="A lightweight agent foundation for personal developers.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('mica-agent')}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    cli_parser = subparsers.add_parser(
        "cli",
        help="start the interactive chat CLI",
        description="Interactive text chat with an OpenAI-compatible provider.",
    )
    selection = cli_parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--continue",
        dest="session_mode",
        action="store_const",
        const="continue",
        help="continue the current workspace's last active session",
    )
    selection.add_argument(
        "--resume",
        dest="session_mode",
        action="store_const",
        const="resume",
        help="choose a healthy session from the current workspace",
    )
    cli_parser.add_argument(
        "--permission-mode",
        choices=("default", "accept-edits", "solo"),
        default="default",
        help="select tool permission behavior",
    )
    cli_parser.set_defaults(session_mode="new")
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    permission_mode = {
        "default": PermissionMode.DEFAULT,
        "accept-edits": PermissionMode.ACCEPT_EDITS,
        "solo": PermissionMode.SOLO,
    }[args.permission_mode]
    sys.exit(_main_cli(args.session_mode, permission_mode))


def _load_config() -> tuple[str, str, str] | None:
    """读取并校验三个阶段性连接参数；缺失时输出诊断并返回 None。"""
    values = {name: os.environ.get(name, "") for name in _ENVIRONMENT_VARIABLES}
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        print(
            "mica: missing required environment variables: " + ", ".join(missing),
            file=sys.stderr,
        )
        return None
    return tuple(values[name].strip() for name in _ENVIRONMENT_VARIABLES)  # type: ignore[return-value]


def _main_cli(
    session_mode: SessionMode = "new",
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
) -> int:
    """mica cli 同步入口：手动事件循环以支持 Ctrl+C 优雅退出。"""
    config = _load_config()
    if config is None:
        return 2
    api_url, api_key, model = config
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(
            run_interactive(
                api_url,
                api_key,
                model,
                session_mode=session_mode,
                permission_mode=permission_mode,
            )
        )
        try:
            loop.run_until_complete(task)
        except KeyboardInterrupt:
            # Ctrl+C：取消主任务，让清理路径（事件流与 App 关闭）完成。
            task.cancel()
            try:
                loop.run_until_complete(task)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        if task.cancelled():
            return 0
        return task.result()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001, S110 - cleanup must not mask CLI result
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


async def run_interactive(
    api_url: str,
    api_key: str,
    model: str,
    *,
    session_mode: SessionMode = "new",
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    provider_factory: ProviderFactory | None = None,
    read_line: ReadLine | None = None,
    write_out: Write | None = None,
    write_err: Write | None = None,
    workspace: WorkspaceIdentity | None = None,
    data_root: Path | str | None = None,
    session_service: SessionService | None = None,
    context_builder: ContextBuilder | None = None,
    turn_id_factory: TurnIdFactory | None = None,
    clock: Clock | None = None,
) -> int:
    """装配会话、上下文、Provider 与 App，再进入基础交互。"""
    if session_mode not in {"new", "continue", "resume"}:
        raise ValueError("session_mode must be new, continue, or resume")
    if not isinstance(permission_mode, PermissionMode):
        raise TypeError("permission_mode must be a PermissionMode")
    factory = provider_factory or _default_provider_factory
    read = read_line or _default_read_line
    out = write_out or _default_write
    err = write_err or _default_write_err

    try:
        resolved_workspace, service, builder = _assemble_session(
            workspace=workspace,
            data_root=data_root,
            session_service=session_service,
            context_builder=context_builder,
        )
    except WorkspaceResolutionError:
        err("mica: unable to resolve the current workspace\n")
        return 2
    except Exception:  # noqa: BLE001 - startup diagnostics must stay safe
        err("mica: unable to initialize session storage\n")
        return 2

    selected = await _select_session(service, session_mode, read, out, err)
    if selected is None:
        return 2
    return await _run_selected_session(
        api_url,
        api_key,
        model,
        service,
        builder,
        resolved_workspace,
        selected,
        factory,
        read,
        out,
        err,
        permission_mode,
        turn_id_factory,
        clock,
    )


async def _run_selected_session(
    api_url: str,
    api_key: str,
    model: str,
    service: SessionService,
    builder: ContextBuilder,
    workspace: WorkspaceIdentity,
    selected: StoredSession,
    factory: ProviderFactory,
    read: ReadLine,
    out: Write,
    err: Write,
    permission_mode: PermissionMode,
    turn_id_factory: TurnIdFactory | None,
    clock: Clock | None,
) -> int:
    from .app import MicaApp

    session_id = selected.meta.session_id
    _report_cleanup(service, session_id, err)
    try:
        context = builder.build(service.context_snapshot(session_id))
    except Exception:  # noqa: BLE001 - do not expose storage details
        err("mica: unable to prepare session context\n")
        return 2
    for diagnostic in context.diagnostics:
        err(f"warning: {diagnostic.message}\n")

    try:
        from .file_tools import register_file_tools
        from .interactions import InteractionBroker
        from .policy import PolicyEngine
        from .runtime_tools import register_runtime_tools
        from .tool_executor import ToolExecutor
        from .tool_registry import ToolRegistry

        broker = InteractionBroker()
        registry = ToolRegistry()
        register_file_tools(registry, workspace)
        register_runtime_tools(registry, workspace, broker)
        executor = ToolExecutor(
            registry,
            PolicyEngine(permission_mode),
            interaction_broker=broker,
        )
    except Exception:  # noqa: BLE001 - tool startup diagnostics must stay safe
        err("mica: unable to initialize tools\n")
        return 2

    try:
        provider = factory(api_url, api_key)
    except Exception:  # noqa: BLE001 - provider details may contain secrets
        err("mica: unable to initialize provider\n")
        return 2
    try:
        app = MicaApp(
            provider=provider,
            model=model,
            session_service=service,
            session_id=session_id,
            context_builder=builder,
            turn_id_factory=turn_id_factory,
            clock=clock,
            tool_executor=executor,
        )
    except Exception:  # noqa: BLE001 - startup diagnostics must stay safe
        await provider.aclose()
        err("mica: unable to initialize application\n")
        return 2

    try:
        await _interact(app, read, out, err)
    except asyncio.CancelledError:
        # Ctrl+C：取消当前 Run 后完成清理并正常退出。
        pass
    except Exception:  # noqa: BLE001 - do not expose session details
        err("error: session unavailable\n")
        return 2
    finally:
        await app.aclose()
    return 0


def _assemble_session(
    *,
    workspace: WorkspaceIdentity | None,
    data_root: Path | str | None,
    session_service: SessionService | None,
    context_builder: ContextBuilder | None,
) -> tuple[WorkspaceIdentity, SessionService, ContextBuilder]:
    if session_service is not None and not isinstance(session_service, SessionService):
        raise TypeError("session_service must be a SessionService")
    if workspace is None:
        workspace = (
            session_service.store.workspace
            if session_service is not None
            else resolve_workspace(Path.cwd())
        )
    elif not isinstance(workspace, WorkspaceIdentity):
        raise TypeError("workspace must be a WorkspaceIdentity")
    if session_service is None:
        root = default_data_root() if data_root is None else data_root
        session_service = SessionService(JsonlSessionStore(workspace, root))
    elif session_service.store.workspace != workspace:
        raise ValueError("session service workspace does not match current workspace")
    if context_builder is None:
        root = session_service.store.data_root if data_root is None else data_root
        context_builder = ContextBuilder(data_root=root, workspace=workspace)
    elif not isinstance(context_builder, ContextBuilder):
        raise TypeError("context_builder must be a ContextBuilder")
    if context_builder.workspace != workspace:
        raise ValueError("context builder workspace does not match current workspace")
    return workspace, session_service, context_builder


async def _select_session(
    service: SessionService,
    session_mode: SessionMode,
    read_line: ReadLine,
    write_out: Write,
    write_err: Write,
) -> StoredSession | None:
    if session_mode == "new":
        try:
            return service.create_session()
        except Exception:  # noqa: BLE001 - do not expose storage details
            write_err("mica: unable to create session\n")
            return None
    if session_mode == "continue":
        try:
            return service.last_active_session()
        except NoHealthySessionError:
            write_err(
                "mica: no healthy session in the current workspace; "
                "use mica cli to create one\n"
            )
            return None
        except Exception:  # noqa: BLE001 - do not expose storage details
            write_err("mica: unable to select the last active session\n")
            return None

    try:
        listed = service.list_sessions()
    except Exception:  # noqa: BLE001 - do not expose storage details
        write_err("mica: unable to list sessions\n")
        return None
    if listed.problems:
        write_err("warning: some session files could not be read\n")
    if not listed.sessions:
        write_err(
            "mica: no healthy session in the current workspace; "
            "use mica cli to create one\n"
        )
        return None
    for index, session in enumerate(listed.sessions, start=1):
        title = session.meta.title.strip() or "untitled session"
        write_out(f"{index}. {title}\n")
    try:
        choice = await read_line()
    except asyncio.CancelledError:
        raise
    except (EOFError, KeyboardInterrupt):
        write_err("mica: session selection requires a number\n")
        return None
    try:
        if not choice.isascii() or not choice.isdecimal():
            raise ValueError
        selected_index = int(choice)
    except (TypeError, ValueError, OverflowError):
        write_err("mica: invalid session selection; enter a listed number\n")
        return None
    if not 1 <= selected_index <= len(listed.sessions):
        write_err("mica: invalid session selection; enter a listed number\n")
        return None
    return listed.sessions[selected_index - 1]


def _report_cleanup(service: SessionService, session_id: str, write_err: Write) -> None:
    try:
        result = service.cleanup_expired(current_session_id=session_id)
    except Exception:  # noqa: BLE001 - cleanup must not block a healthy session
        write_err("warning: session cleanup was not completed\n")
        return
    if (
        result.problems
        or result.skipped_locked_session_ids
        or result.skipped_changed_session_ids
    ):
        write_err("warning: some expired sessions were not cleaned\n")


async def _interact(
    app: MicaApp,
    read_line: ReadLine,
    write_out: Write,
    write_err: Write,
) -> None:
    """多轮交互循环：读取输入、渲染 EngineEvent、转换退出信号。"""
    while True:
        try:
            line = await read_line()
        except EOFError:
            return
        except KeyboardInterrupt:
            return
        if not line.strip():
            continue
        has_output = False
        ends_with_newline = False
        try:
            async with aclosing(app.run(line)) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        # 空字符串也原样写出，但不改变换行状态。
                        write_out(event.text)
                        if event.text:
                            has_output = True
                            ends_with_newline = event.text.endswith("\n")
                    elif isinstance(event, ToolStarted):
                        write_err(f"tool {event.tool_name}: started\n")
                    elif isinstance(event, ToolFinished):
                        write_err(f"tool {event.tool_name}: {event.status.value}\n")
                    elif isinstance(event, PermissionRequested):
                        answer = await _read_permission_answer(
                            event.tool_name, read_line, write_err
                        )
                        if answer is None:
                            return
                        app.respond_interaction(
                            PermissionResponse(event.interaction_id, answer)
                        )
                    elif isinstance(event, UserInputRequested):
                        write_err(f"{event.question}\n")
                        try:
                            answer = await read_line()
                        except asyncio.CancelledError:
                            raise
                        except (EOFError, KeyboardInterrupt):
                            return
                        app.respond_interaction(
                            UserInputResponse(event.interaction_id, answer)
                        )
                    elif isinstance(event, (RunCompleted, RunFailed, RunCancelled)):
                        # 有非空文本且未以换行结尾时才补一个必要换行；
                        # 无文本的终态不得向 stdout 写空行。
                        if has_output and not ends_with_newline:
                            write_out("\n")
                        if isinstance(event, RunFailed):
                            write_err(
                                f"error: {event.category.value}: {event.message}\n"
                            )
                        elif isinstance(event, RunCancelled):
                            write_err("cancelled\n")
        except asyncio.CancelledError:
            # 活动 Run 被取消：事件流已由 aclosing 清理，退出 CLI。
            return


async def _read_permission_answer(
    tool_name: str,
    read_line: ReadLine,
    write_err: Write,
) -> PermissionAnswer | None:
    while True:
        write_err(f"permission required for tool {tool_name} [y/N]: ")
        try:
            value = await read_line()
        except asyncio.CancelledError:
            raise
        except (EOFError, KeyboardInterrupt):
            return None
        normalized = value.strip().lower()
        if normalized in {"y", "yes"} and normalized.isascii():
            return PermissionAnswer.ALLOW_ONCE
        if normalized in {"", "n", "no"}:
            return PermissionAnswer.DENY
        write_err("please answer y or n\n")


def _default_provider_factory(api_url: str, api_key: str) -> Provider:
    from .providers import OpenAICompatibleProvider

    return OpenAICompatibleProvider(api_key=api_key, base_url=api_url)


def _default_read_line() -> Awaitable[str]:
    """在 daemon 线程读取 stdin。

    避免阻塞事件循环；daemon 线程保证 Ctrl+C 退出后进程不会被
    ``input()`` 挂起（非 daemon 线程会在解释器退出时被 join）。
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    thread = threading.Thread(
        target=_read_line_worker,
        args=(loop, future),
        name="mica-stdin",
        daemon=True,
    )
    thread.start()
    return future


def _read_line_worker(
    loop: asyncio.AbstractEventLoop, future: asyncio.Future[str]
) -> None:
    try:
        value = input()
    except BaseException as exc:  # noqa: BLE001 - daemon 线程内任何异常都必须转发给事件循环
        loop.call_soon_threadsafe(_finish_read_line, future, exc)
    else:
        loop.call_soon_threadsafe(_finish_read_line, future, value)


def _finish_read_line(future: asyncio.Future[str], result: object) -> None:
    if future.cancelled():
        return
    if isinstance(result, BaseException):
        future.set_exception(result)
    else:
        future.set_result(result)  # type: ignore[arg-type]


def _default_write(text: str) -> None:
    """实时写出：立即 flush，保证流式文本可见。"""
    sys.stdout.write(text)
    sys.stdout.flush()


def _default_write_err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()
