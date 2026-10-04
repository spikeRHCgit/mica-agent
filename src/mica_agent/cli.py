"""Mica command-line entry point.

Public contract: running with no arguments prints help; ``--help``, ``--version``, and
exit code 2 for invalid arguments are supported; the ``mica`` and
``python -m mica_agent`` entry points behave identically. ``mica cli`` starts the
interactive chat: it reads connection settings from environment variables, assembles
``OpenAICompatibleProvider`` and ``MicaApp``, and renders ``EngineEvent`` values as they
arrive.

``run_interactive`` is the injectable interaction core: tests drive it with stand-ins
(provider factory, stdin reader, stdout/stderr writers), while the product defaults
build a real provider and use standard input and output. ``mica cli`` enters the main
buffer terminal frontend by default; ``--plain`` keeps the basic text interface.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from .config_contracts import ConfigError, EffectiveSettings, SelectionResolution
from .config_store import ConfigStore
from .context_builder import AgentFileSource, ContextBuilder
from .events import (
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
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
from .session_contracts import SessionSelection
from .session_service import NoHealthySessionError, SessionService
from .system_prompt import build_system_prompt
from .workspace import (
    WorkspaceIdentity,
    WorkspaceResolutionError,
    default_data_root,
    resolve_workspace,
)

if TYPE_CHECKING:
    from .app import MicaApp
    from .config_service import ConfigService
    from .providers import Provider

ReadLine: TypeAlias = Callable[[], Awaitable[str]]
Write: TypeAlias = Callable[[str], None]
ProviderFactory: TypeAlias = Callable[[str, str], "Provider"]
TurnIdFactory: TypeAlias = Callable[[], str]
Clock: TypeAlias = Callable[[], datetime]
SessionMode: TypeAlias = Literal["new", "continue", "resume"]

_ENVIRONMENT_VARIABLES = ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL")


class _SelectionArgument(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if (
            getattr(namespace, self.dest, None) is not None
            or not isinstance(values, str)
            or not values.strip()
            or not values.isprintable()
        ):
            parser.error(
                "provider/model selection requires one nonblank value for each option"
            )
        setattr(namespace, self.dest, values)


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
        description=(
            "Interactive chat with an OpenAI-compatible provider. "
            "Starts the terminal interface by default; "
            "use --plain for the basic text interface."
        ),
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
        "--plain",
        action="store_true",
        help="use the basic text interface instead of the default terminal interface",
    )
    cli_parser.add_argument(
        "--permission-mode",
        choices=("default", "solo"),
        default=None,
        help="select tool permission behavior",
    )
    cli_parser.add_argument(
        "--provider",
        action=_SelectionArgument,
        help="temporary provider identifier override",
    )
    cli_parser.add_argument(
        "--model", action=_SelectionArgument, help="temporary model identifier override"
    )
    cli_parser.set_defaults(session_mode="new")
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if (args.provider is None) != (args.model is None):
        parser.error("provider and model must be specified together")
    permission_mode = (
        None if args.permission_mode is None else PermissionMode(args.permission_mode)
    )
    explicit_selection = (
        None if args.provider is None else SessionSelection(args.provider, args.model)
    )
    sys.exit(
        _main_cli(
            args.session_mode,
            permission_mode,
            plain=args.plain,
            explicit_selection=explicit_selection,
        )
    )


def _main_cli(
    session_mode: SessionMode = "new",
    permission_mode: PermissionMode | None = None,
    *,
    plain: bool = False,
    explicit_selection: SessionSelection | None = None,
) -> int:
    """Synchronous ``mica cli`` entry point: a manual event loop allows graceful Ctrl+C
    exit."""
    if plain and session_mode == "new":
        from .config_service import ConfigService

        try:
            workspace = resolve_workspace(Path.cwd())
            service = ConfigService(
                ConfigStore(default_data_root(), workspace.workspace_path),
                permission_override=permission_mode,
            )
            resolution = service.resolve(service.load(), explicit=explicit_selection)
            if resolution.status != "ready":
                _report_unconfigured(resolution, sys.stderr.write)
                return 2
        except ConfigError as error:
            print("mica: " + str(error), file=sys.stderr)
            return 2
        except WorkspaceResolutionError:
            print("mica: unable to resolve the current workspace", file=sys.stderr)
            return 2
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(
            run_interactive(
                explicit_selection=explicit_selection,
                session_mode=session_mode,
                permission_mode=permission_mode,
                plain=plain,
            )
        )
        try:
            loop.run_until_complete(task)
        except KeyboardInterrupt:
            # Ctrl+C: cancel the main task so the cleanup path (event stream and app
            # close) can finish.
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
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    *,
    session_mode: SessionMode = "new",
    permission_mode: PermissionMode | None = None,
    explicit_selection: SessionSelection | None = None,
    plain: bool = True,
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
    """Assemble session, context, provider, and app, then enter basic interaction."""
    if session_mode not in {"new", "continue", "resume"}:
        raise ValueError("session_mode must be new, continue, or resume")
    if permission_mode is not None and not isinstance(permission_mode, PermissionMode):
        raise TypeError("permission_mode must be a PermissionMode")
    factory = provider_factory or _default_provider_factory
    read = read_line or _default_read_line
    out = write_out or _default_write
    err = write_err or _default_write_err

    from .config_service import ConfigService

    try:
        config_workspace = workspace or (
            session_service.store.workspace
            if session_service is not None
            else resolve_workspace(Path.cwd())
        )
        config_root = (
            data_root
            if data_root is not None
            else (
                session_service.store.data_root
                if session_service is not None
                else default_data_root()
            )
        )
        configuration = ConfigService(
            ConfigStore(config_root, config_workspace.workspace_path),
            permission_override=permission_mode,
        )
        snapshot = configuration.load()
        direct = None
        if any(value is not None for value in (api_url, api_key, model)):
            direct = configuration.direct_connection(api_url, api_key, model)
        initial = (
            SelectionResolution(direct.selection, "direct", "ready", direct)
            if direct is not None
            else configuration.resolve(snapshot, explicit=explicit_selection)
        )
    except ConfigError as error:
        err("mica: " + str(error) + "\n")
        return 2
    except WorkspaceResolutionError:
        err("mica: unable to resolve the current workspace\n")
        return 2
    if plain and session_mode == "new" and initial.status != "ready":
        _report_unconfigured(initial, err)
        return 2
    for warning in snapshot.diagnostics:
        err("warning: " + warning + "\n")
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

    initial_selection = (
        initial.selection
        if initial.status == "ready"
        and initial.source in {"workspace default", "global default"}
        else None
    )
    selected = await _select_session(
        service, session_mode, read, out, err, initial_selection=initial_selection
    )
    if selected is None:
        return 2
    resolution = (
        SelectionResolution(direct.selection, "direct", "ready", direct)
        if direct is not None
        else configuration.resolve(
            snapshot,
            explicit=explicit_selection,
            saved=selected.meta.selection if session_mode != "new" else None,
        )
    )
    if plain and resolution.status != "ready":
        _report_unconfigured(resolution, err)
        return 2
    if direct is None and session_mode != "new" and selected.meta.selection is None:
        err(
            "warning: legacy session has no saved selection; using current startup source\n"
        )
    if direct is None and resolution.source not in {"environment", "unconfigured"}:
        err("model source: " + resolution.source + "\n")
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
        snapshot.settings.permission_mode,
        turn_id_factory,
        clock,
        plain=plain,
        configuration=configuration,
        resolution=resolution,
        reasoning_display=snapshot.settings.reasoning_display,
        startup_settings=snapshot.settings,
    )


def _report_unconfigured(resolution: SelectionResolution, err: Write) -> None:
    if resolution.status == "unconfigured":
        err(
            "mica: missing required environment variables: "
            + ", ".join(_ENVIRONMENT_VARIABLES)
            + "; or configure a saved connection\n"
        )
    else:
        err(
            "mica: "
            + (resolution.diagnostic or "selected connection unavailable")
            + "\n"
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
    *,
    plain: bool,
    configuration: ConfigService,
    resolution: SelectionResolution,
    reasoning_display: object,
    startup_settings: EffectiveSettings,
) -> int:
    from .app import MicaApp

    session_id = selected.meta.session_id
    _report_cleanup(service, session_id, err)
    try:
        service.context_snapshot(session_id)
        context = builder.build()
    except Exception:  # noqa: BLE001 - do not expose storage details
        err("mica: unable to prepare session context\n")
        return 2
    try:
        system_prompt = build_system_prompt(workspace)
    except Exception:  # noqa: BLE001 - prompt details must stay private
        err("mica: unable to prepare system prompt\n")
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
        executor = ToolExecutor(
            registry,
            PolicyEngine(permission_mode),
            interaction_broker=broker,
            workspace=workspace,
        )

        register_file_tools(registry, workspace)
        register_runtime_tools(
            registry,
            workspace,
            broker,
            output_reporter=executor.output_reporter,
        )
    except Exception:  # noqa: BLE001 - tool startup diagnostics must stay safe
        err("mica: unable to initialize tools\n")
        return 2

    try:
        connection = resolution.connection
        provider = (
            None
            if connection is None
            else factory(connection.api_url, connection.api_key)
        )
        model = None if connection is None else connection.selection.model_id
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
            system_prompt=system_prompt,
            context_snapshot=context,
            turn_id_factory=turn_id_factory,
            clock=clock,
            tool_executor=executor,
            config_service=configuration,
            connection=connection,
            connection_status=resolution.status,
            connection_diagnostic=resolution.diagnostic,
            startup_settings=startup_settings,
            selection_source=resolution.source,
        )
    except Exception:  # noqa: BLE001 - startup diagnostics must stay safe
        if provider is not None:
            task = asyncio.create_task(
                provider.aclose(), name="mica-startup-provider-close"
            )
            task.add_done_callback(
                lambda finished: None if finished.cancelled() else finished.exception()
            )
            done, _ = await asyncio.wait({task}, timeout=2.0)
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=2.0)
                err("mica: provider release unconfirmed\n")
            elif task.cancelled() or task.exception() is not None:
                err("mica: provider release unconfirmed\n")
        err("mica: unable to initialize application\n")
        return 2

    if plain:
        result = 0
        try:
            await _interact(app, read, out, err)
        except asyncio.CancelledError:
            # Ctrl+C: cancel the current run, finish cleanup, and exit normally.
            pass
        except Exception:  # noqa: BLE001 - do not expose session details
            err("error: session unavailable\n")
            result = 2
        finally:
            if not await _close_app(app, err):
                result = 2
        return result

    try:
        from .tui.models import StartupResourceSnapshot, TuiSessionSnapshot

        startup_resources = StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            agent_sources=context.loaded_sources,
            global_agent_path=(
                _global_agents_display_path(builder)
                if AgentFileSource.GLOBAL in context.loaded_sources
                else None
            ),
            workspace_agent_path=(
                "./AGENTS.md"
                if AgentFileSource.WORKSPACE in context.loaded_sources
                else None
            ),
            tool_names=tuple(spec.name for spec in executor.tool_specs),
            skill_names=(),
            extension_names=(),
        )
        session_snapshot = TuiSessionSnapshot(
            workspace=_workspace_display_value(workspace.workspace_path),
            session_label=selected.meta.title.strip() or selected.meta.session_id,
            model=(model + " (" + resolution.source + ")")
            if model is not None and resolution.source not in {"direct", "environment"}
            else model or "unconfigured",
        )
    except Exception:  # noqa: BLE001 - TUI startup diagnostics stay safe
        await _close_app(app, err)
        err("mica: unable to start terminal interface\n")
        return 2

    return await _run_main_buffer(
        app,
        err=err,
        startup_resources=startup_resources,
        session_snapshot=session_snapshot,
        stored_session=selected,
        workspace=workspace,
        reasoning_display=reasoning_display,
    )


async def _close_app(app: MicaApp, err: Write) -> bool:
    try:
        await app.aclose()
    except Exception:  # noqa: BLE001 - cleanup failures must never expose backend details
        err("mica: provider release unconfirmed\n")
        return False
    return True


async def _run_main_buffer(
    app: MicaApp,
    *,
    err: Write,
    startup_resources: object,
    session_snapshot: object,
    stored_session: StoredSession,
    workspace: WorkspaceIdentity,
    reasoning_display: object = None,
) -> int:
    """Run the default main-buffer frontend; failures produce only fixed safe diagnostics."""
    try:
        from .tui.main_buffer import MainBufferScreen, Terminal

        screen = MainBufferScreen(
            app,
            terminal=Terminal(),
            startup_resources=startup_resources,
            session_snapshot=session_snapshot,
            stored_session=stored_session,
            workspace_path=workspace.workspace_path,
            reasoning_mode=reasoning_display,
        )
    except Exception:  # noqa: BLE001 - frontend startup diagnostics must stay safe
        await _close_app(app, err)
        err("mica: unable to start terminal interface\n")
        return 2
    try:
        await screen.run()
    except asyncio.CancelledError:
        if not getattr(screen, "closed", False):
            with suppress(Exception):  # close diagnostics must stay private
                await app.aclose()
        raise
    except Exception:  # noqa: BLE001 - frontend details must stay private
        # Failure path closes business resources as a fallback; on a normal close the
        # screen has already converged, so the provider is not closed twice.
        if not getattr(screen, "closed", False):
            with suppress(Exception):  # close diagnostics must stay private
                await app.aclose()
        err("mica: unable to start terminal interface\n")
        return 2
    if getattr(screen, "cleanup_errors", ()):
        err("mica: terminal cleanup incomplete; provider release may be unconfirmed\n")
        return 2
    return 0


def _workspace_display_value(path: Path) -> str:
    """Return a safe, compact workspace label for the TUI facts row."""
    try:
        relative = path.relative_to(Path.home())
    except (ValueError, RuntimeError):
        return f".{os.sep}{path.name}"
    if str(relative) == ".":
        return "~"
    return "~" + os.sep + str(relative)


def _global_agents_display_path(builder: ContextBuilder) -> str:
    """Return a safe path label without exposing a user absolute path."""
    try:
        if builder.data_root.resolve() == default_data_root().resolve():
            return "~/.mica/AGENTS.md"
    except OSError:
        pass
    return "data-root/AGENTS.md"


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
    *,
    initial_selection: SessionSelection | None = None,
) -> StoredSession | None:
    if session_mode == "new":
        try:
            return service.create_session(selection=initial_selection)
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
    """Multi-turn interaction loop: read input, render ``EngineEvent``, translate exit
    signals."""
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
        pending_tool_output_newline = False

        def flush_tool_output_line() -> None:
            nonlocal pending_tool_output_newline
            if pending_tool_output_newline:
                write_err("\n")
                pending_tool_output_newline = False

        binding = None
        try:
            binding = app.prepare_run()
            if app.reasoning_notice is not None:
                write_err("warning: " + app.reasoning_notice + "\n")
            async with aclosing(app.run(line, binding=binding)) as stream:
                async for event in stream:
                    if isinstance(event, ReasoningDelta):
                        continue
                    if isinstance(event, TextDelta):
                        # An empty string is written verbatim without changing the
                        # newline state.
                        write_out(event.text)
                        if event.text:
                            has_output = True
                            ends_with_newline = event.text.endswith("\n")
                    elif isinstance(event, ToolOutputDelta):
                        write_err(event.text)
                        pending_tool_output_newline = not event.text.endswith("\n")
                    elif isinstance(event, ToolStarted):
                        flush_tool_output_line()
                        write_err(f"tool {event.tool_name}: started\n")
                    elif isinstance(event, ToolFinished):
                        flush_tool_output_line()
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
                        response = await _read_user_input_response(
                            event, read_line, write_err
                        )
                        if response is None:
                            return
                        app.respond_interaction(response)
                    elif isinstance(event, (RunCompleted, RunFailed, RunCancelled)):
                        flush_tool_output_line()
                        # Add a newline only when there is non-empty text that does not
                        # end with one; a terminal event without text must not write a
                        # blank line to stdout.
                        if has_output and not ends_with_newline:
                            write_out("\n")
                        if isinstance(event, RunFailed):
                            write_err(
                                f"error: {event.category.value}: {event.message}\n"
                            )
                        elif isinstance(event, RunCancelled):
                            write_err("cancelled\n")
        except asyncio.CancelledError:
            # The active run was cancelled: aclosing has cleaned the event stream, exit
            # the CLI.
            return
        finally:
            if binding is not None:
                app.release_run(binding)


async def _read_user_input_response(
    event: UserInputRequested,
    read_line: ReadLine,
    write_err: Write,
) -> UserInputResponse | None:
    prompt = event.prompt
    if prompt is None or not prompt.options:
        write_err(f"{event.question}\n")
        try:
            answer = await read_line()
        except asyncio.CancelledError:
            raise
        except (EOFError, KeyboardInterrupt):
            return None
        return UserInputResponse(event.interaction_id, answer)

    if prompt.title is not None:
        write_err(f"[{prompt.title}]\n")
    write_err(f"{prompt.question}\n")
    for index, option in enumerate(prompt.options, start=1):
        suffix = " (recommended)" if option.recommended else ""
        write_err(f"{index}. {option.label}{suffix}\n")
        if option.description is not None:
            write_err(f"   {option.description}\n")
    custom_index = len(prompt.options) + 1 if prompt.allow_custom else None
    if custom_index is not None:
        write_err(f"{custom_index}. Type something.\n")

    while True:
        write_err("selection: ")
        try:
            value = await read_line()
        except asyncio.CancelledError:
            raise
        except (EOFError, KeyboardInterrupt):
            return None
        normalized = value.strip() if isinstance(value, str) else None
        if normalized is not None and normalized.isascii() and normalized.isdecimal():
            try:
                selected = int(normalized)
            except (TypeError, ValueError, OverflowError):
                selected = None
            if selected is not None and 1 <= selected <= len(prompt.options):
                option = prompt.options[selected - 1]
                return UserInputResponse(
                    event.interaction_id, option.label, selected - 1
                )
            if (
                selected is not None
                and custom_index is not None
                and selected == custom_index
            ):
                write_err("answer: ")
                try:
                    answer = await read_line()
                except asyncio.CancelledError:
                    raise
                except (EOFError, KeyboardInterrupt):
                    return None
                return UserInputResponse(event.interaction_id, answer)
        write_err("mica: invalid ask selection; enter a listed number\n")


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
    """Read stdin in a daemon thread.

    This avoids blocking the event loop; the daemon thread keeps the process from
    hanging in ``input()`` after a Ctrl+C exit (non-daemon threads are joined at
    interpreter exit).
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
    except BaseException as exc:  # noqa: BLE001 - any daemon-thread exception must reach the event loop
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
    """Write immediately and flush so streaming text becomes visible."""
    sys.stdout.write(text)
    sys.stdout.flush()


def _default_write_err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()
