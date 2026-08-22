"""P3-04 Context Builder 与 AGENTS.md 加载测试。"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mica_agent.context_builder import (
    MAX_AGENTS_FILE_BYTES,
    AgentFileSource,
    ContextBuilder,
    ContextDiagnosticKind,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionMeta,
)
from mica_agent.workspace import resolve_workspace


def make_builder(tmp_path: Path) -> tuple[ContextBuilder, Path, Path]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    identity = resolve_workspace(workspace_path)
    return (
        ContextBuilder(data_root=data_root, workspace=identity),
        data_root,
        workspace_path,
    )


def test_build_orders_global_workspace_and_session_messages(
    tmp_path: Path,
) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").write_text("global rules", encoding="utf-8")
    (workspace_path / "AGENTS.md").write_text("workspace rules", encoding="utf-8")
    history = (
        Message(MessageRole.USER, "question"),
        Message(MessageRole.ASSISTANT, "answer"),
    )

    result = builder.build(history)

    assert result.messages == (
        Message(MessageRole.SYSTEM, "global rules"),
        Message(MessageRole.SYSTEM, "workspace rules"),
        *history,
    )
    assert result.diagnostics == ()


def test_each_agent_source_can_be_present_alone_or_missing(
    tmp_path: Path,
) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    session = (Message(MessageRole.USER, "hello"),)

    data_root.mkdir()
    (data_root / "AGENTS.md").write_text("global", encoding="utf-8")
    assert builder.build(session).messages == (
        Message(MessageRole.SYSTEM, "global"),
        *session,
    )

    (data_root / "AGENTS.md").unlink()
    (workspace_path / "AGENTS.md").write_text("workspace", encoding="utf-8")
    assert builder.build(session).messages == (
        Message(MessageRole.SYSTEM, "workspace"),
        *session,
    )

    (workspace_path / "AGENTS.md").unlink()
    result = builder.build()
    assert result.messages == ()
    assert result.diagnostics == ()


def test_empty_agents_file_is_skipped_without_empty_system_message(
    tmp_path: Path,
) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").write_bytes(b"")
    (workspace_path / "AGENTS.md").write_bytes(b"\n")

    result = builder.build()

    assert result.messages == (Message(MessageRole.SYSTEM, "\n"),)
    assert result.diagnostics == ()


def test_invalid_utf8_and_too_large_diagnostics_are_ordered_and_safe(
    tmp_path: Path,
) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").write_bytes(b"\xff")
    (workspace_path / "AGENTS.md").write_bytes(b"x" * (MAX_AGENTS_FILE_BYTES + 1))
    session = (Message(MessageRole.USER, "keep this"),)

    result = builder.build(session)

    assert result.messages == session
    assert [(item.source, item.kind) for item in result.diagnostics] == [
        (AgentFileSource.GLOBAL, ContextDiagnosticKind.INVALID_UTF8),
        (AgentFileSource.WORKSPACE, ContextDiagnosticKind.TOO_LARGE),
    ]
    assert [item.message for item in result.diagnostics] == [
        "global AGENTS.md is not valid UTF-8",
        "workspace AGENTS.md exceeds the size limit",
    ]


def test_unreadable_directory_does_not_block_valid_other_source(
    tmp_path: Path,
) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").mkdir()
    (workspace_path / "AGENTS.md").write_text("workspace", encoding="utf-8")
    session = (Message(MessageRole.ASSISTANT, "history"),)

    result = builder.build(session)

    assert result.messages == (
        Message(MessageRole.SYSTEM, "workspace"),
        *session,
    )
    assert result.diagnostics[0].source is AgentFileSource.GLOBAL
    assert result.diagnostics[0].kind is ContextDiagnosticKind.UNREADABLE


def test_exact_limit_is_allowed_but_limit_plus_one_is_not_used(
    tmp_path: Path,
) -> None:
    builder, data_root, _ = make_builder(tmp_path)
    data_root.mkdir()
    exact = b"a" * MAX_AGENTS_FILE_BYTES
    path = data_root / "AGENTS.md"
    path.write_bytes(exact)

    allowed = builder.build()

    assert allowed.messages == (Message(MessageRole.SYSTEM, exact.decode()),)
    assert allowed.diagnostics == ()

    path.write_bytes(exact + b"b")
    rejected = builder.build()

    assert rejected.messages == ()
    assert rejected.diagnostics[0].kind is ContextDiagnosticKind.TOO_LARGE
    assert "b" not in rejected.messages


def test_build_reads_agents_again_without_caching(tmp_path: Path) -> None:
    builder, data_root, workspace_path = make_builder(tmp_path)
    data_root.mkdir()
    global_path = data_root / "AGENTS.md"
    workspace_path.joinpath("AGENTS.md").write_text("first workspace", encoding="utf-8")
    global_path.write_text("first global", encoding="utf-8")

    first = builder.build()
    global_path.write_text("second global", encoding="utf-8")
    workspace_path.joinpath("AGENTS.md").write_text(
        "second workspace", encoding="utf-8"
    )
    second = builder.build()

    assert first.messages == (
        Message(MessageRole.SYSTEM, "first global"),
        Message(MessageRole.SYSTEM, "first workspace"),
    )
    assert second.messages == (
        Message(MessageRole.SYSTEM, "second global"),
        Message(MessageRole.SYSTEM, "second workspace"),
    )


def test_result_and_input_are_immutable_snapshots(tmp_path: Path) -> None:
    builder, data_root, _ = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").write_text("global", encoding="utf-8")
    history = [Message(MessageRole.USER, "before")]

    result = builder.build(history)
    history.append(Message(MessageRole.ASSISTANT, "after"))

    assert isinstance(result.messages, tuple)
    assert isinstance(result.diagnostics, tuple)
    assert result.messages == (
        Message(MessageRole.SYSTEM, "global"),
        Message(MessageRole.USER, "before"),
    )


def test_session_messages_must_be_standard_messages(tmp_path: Path) -> None:
    builder, _, _ = make_builder(tmp_path)

    with pytest.raises(TypeError, match=r"session_messages\[0\]"):
        builder.build(["not a Message"])  # type: ignore[list-item]

    with pytest.raises(TypeError, match="iterable of Message"):
        builder.build(None)  # type: ignore[arg-type]


def test_constructor_is_explicit_and_has_no_directory_side_effect(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "not-created"
    identity = resolve_workspace(workspace_path)

    builder = ContextBuilder(data_root=data_root, workspace=identity)

    assert builder.data_root == data_root
    assert not data_root.exists()
    assert not (workspace_path / "AGENTS.md").exists()
    builder.build()
    assert not data_root.exists()

    with pytest.raises(ValueError, match="positive"):
        ContextBuilder(
            data_root=data_root,
            workspace=identity,
            max_agents_file_bytes=0,
        )


def test_builder_does_not_modify_a_store_snapshot_or_jsonl_file(
    tmp_path: Path,
) -> None:
    builder, data_root, _ = make_builder(tmp_path)
    data_root.mkdir()
    (data_root / "AGENTS.md").write_text("startup", encoding="utf-8")
    store = JsonlSessionStore(builder.workspace, tmp_path / "store-data")
    timestamp = datetime(2026, 8, 22, tzinfo=UTC)
    meta = SessionMeta(
        session_id="session",
        format_version=CURRENT_SESSION_FORMAT_VERSION,
        workspace_id=builder.workspace.workspace_id,
        workspace_path=str(builder.workspace.workspace_path),
        title="title",
        created_at=timestamp,
        updated_at=timestamp,
    )
    stored = store.create(meta)
    before = store.path_for_session("session").read_bytes()

    result = builder.build((Message(MessageRole.USER, "history"),))

    assert result.messages == (
        Message(MessageRole.SYSTEM, "startup"),
        Message(MessageRole.USER, "history"),
    )
    assert store.path_for_session("session").read_bytes() == before
    assert store.load("session") == stored


def test_context_builder_import_does_not_load_runtime_or_provider_modules() -> None:
    code = (
        "import sys\n"
        "import mica_agent.context_builder\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
