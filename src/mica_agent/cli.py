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
from importlib.metadata import version
from typing import TYPE_CHECKING, TypeAlias

from .events import (
    RunCancelled,
    RunCompleted,
    RunFailed,
    TextDelta,
)

if TYPE_CHECKING:
    from .app import MicaApp
    from .providers import Provider

ReadLine: TypeAlias = Callable[[], Awaitable[str]]
Write: TypeAlias = Callable[[str], None]
ProviderFactory: TypeAlias = Callable[[str, str], "Provider"]

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
    subparsers.add_parser(
        "cli",
        help="start the interactive chat CLI",
        description="Interactive text chat with an OpenAI-compatible provider.",
    )
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    sys.exit(_main_cli())


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


def _main_cli() -> int:
    """mica cli 同步入口：手动事件循环以支持 Ctrl+C 优雅退出。"""
    config = _load_config()
    if config is None:
        return 2
    api_url, api_key, model = config
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(run_interactive(api_url, api_key, model))
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
        loop.close()


async def run_interactive(
    api_url: str,
    api_key: str,
    model: str,
    *,
    provider_factory: ProviderFactory | None = None,
    read_line: ReadLine | None = None,
    write_out: Write | None = None,
    write_err: Write | None = None,
) -> int:
    """一次完整 CLI 会话：装配 → 多轮交互 → 关闭。

    Provider 与 App 只装配一次；任何退出路径（EOF、Ctrl+C、
    正常结束）都会关闭事件流与 App。``api_key`` 只传给工厂，
    永不进入任何输出。

    App/Provider 在函数内惰性导入：``mica --version``/``--help``
    等基础入口不依赖 httpx 即可启动。
    """
    from .app import MicaApp

    factory = provider_factory or _default_provider_factory
    read = read_line or _default_read_line
    out = write_out or _default_write
    err = write_err or _default_write_err
    provider = factory(api_url, api_key)
    app = MicaApp(provider=provider, model=model)
    try:
        await _interact(app, read, out, err)
    except asyncio.CancelledError:
        # Ctrl+C：取消当前 Run 后完成清理并正常退出。
        pass
    finally:
        await app.aclose()
    return 0


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
        ends_with_newline = False
        try:
            async with aclosing(app.run(line)) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        write_out(event.text)
                        ends_with_newline = event.text.endswith("\n")
                    elif isinstance(event, RunCompleted):
                        if not ends_with_newline:
                            write_out("\n")
                    elif isinstance(event, RunFailed):
                        if not ends_with_newline:
                            write_out("\n")
                        write_err(f"error: {event.category.value}: {event.message}\n")
                    elif isinstance(event, RunCancelled):
                        if not ends_with_newline:
                            write_out("\n")
                        write_err("cancelled\n")
        except asyncio.CancelledError:
            # 活动 Run 被取消：事件流已由 aclosing 清理，退出 CLI。
            return


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
