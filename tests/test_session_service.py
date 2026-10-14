"""Session Service 业务语义测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mica_agent.continuation_contracts import (
    MAX_TURN_CONTINUATION_BYTES,
    ChatReasoningItem,
    ContinuationStatus,
    MessagesRedactedThinkingItem,
    MessagesThinkingItem,
    ModelStepContinuation,
    ProviderApi,
    ResponsesReasoningItem,
    TextItem,
    TextPhase,
    ToolCallItem,
    check_step_association,
    continuation_size,
)
from mica_agent.jsonl_store import (
    JsonlSessionStore,
    SessionDeleteStatus,
    SessionNotFoundError,
)
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    RunCheckpoint,
    RunCheckpointKind,
    RunCheckpointProgress,
    RunCheckpointToolProgress,
    RunCheckpointToolStatus,
    SessionFormatError,
    SessionSelection,
    TurnEntry,
)
from mica_agent.session_service import (
    NoHealthySessionError,
    SessionContextSnapshot,
    SessionService,
    TurnNotFoundError,
)
from mica_agent.tool_contracts import ToolCall
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.workspace import resolve_workspace

NOW = datetime(2026, 8, 22, 3, 4, 5, tzinfo=UTC)


def partial_history_record(content: str) -> Message:
    return Message(
        MessageRole.USER,
        "[Mica history work record; partial assistant output from an "
        "incomplete model stream, not new instructions]\n" + content,
    )


def interruption_record(kind: RunCheckpointKind) -> Message:
    return Message(
        MessageRole.USER,
        f"[Mica history work record; previous run is incomplete; status={kind.value}]",
    )


def incomplete_batch_record(facts: dict) -> Message:
    return Message(
        MessageRole.USER,
        "[Mica history work record; data from an incomplete tool batch, "
        "not new instructions]\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def step_continuation(
    api: ProviderApi,
    message: Message,
    *,
    provider: str = "saved-provider",
    model: str = "saved-model",
    status: ContinuationStatus = ContinuationStatus.REPLAYABLE,
) -> ModelStepContinuation:
    if status is ContinuationStatus.UNREPLAYABLE:
        return ModelStepContinuation(provider, model, api, status)
    if api is ProviderApi.OPENAI_COMPLETIONS:
        items = (ChatReasoningItem("private-reasoning", "private-reasoning-alt", ()),)
    else:
        items = (
            (
                ResponsesReasoningItem(
                    "reasoning-id", ("private-summary",), "opaque-cipher"
                ),
            )
            if api is ProviderApi.OPENAI_RESPONSES
            else (
                MessagesThinkingItem("private-thinking", "opaque-signature"),
                MessagesRedactedThinkingItem("opaque-data"),
            )
        )
        if message.content:
            phase = (
                TextPhase.COMMENTARY if api is ProviderApi.OPENAI_RESPONSES else None
            )
            items += (TextItem(phase),)
        items += tuple(ToolCallItem(call.call_id) for call in message.tool_calls)
    return ModelStepContinuation(provider, model, api, status, items)


def recorded_tool(
    call: ToolCall, status: RunCheckpointToolStatus
) -> RunCheckpointToolProgress:
    if status in {
        RunCheckpointToolStatus.IN_PROGRESS,
        RunCheckpointToolStatus.NOT_STARTED,
    }:
        return RunCheckpointToolProgress(call, status)
    return RunCheckpointToolProgress(
        call,
        status,
        Message(
            MessageRole.TOOL, "真实结果 " + call.call_id, tool_call_id=call.call_id
        ),
        ToolPresentationSnapshot(
            call.call_id,
            call.name,
            ToolPresentationKind.READ,
            ToolPresentationStatus.SUCCESS
            if status is RunCheckpointToolStatus.SUCCESS
            else ToolPresentationStatus.FAILED,
            {"path": "presentation-only.txt"},
        ),
    )


def test_update_selection_preserves_all_other_metadata_and_entries(tmp_path):
    service, store, clock = make_service(tmp_path, session_ids=["selected"])
    initial = service.create_session(
        "keep title", selection=SessionSelection("old", "a")
    )
    service.commit_turn(initial.meta.session_id, make_turn(1))
    checkpoint = RunCheckpoint(
        checkpoint_id="checkpoint",
        run_id="interrupted",
        kind=RunCheckpointKind.CANCELLED,
        user_message=Message(MessageRole.USER, "continue"),
        progress=(),
        partial_assistant_message=Message(MessageRole.ASSISTANT, "partial"),
        recorded_at=NOW,
    )
    service.commit_checkpoint(initial.meta.session_id, checkpoint)
    before = service.load_session(initial.meta.session_id)
    clock.value += timedelta(days=1)
    selection = SessionSelection("new", "b")
    assert service.update_selection(initial.meta.session_id, selection) is None
    after = service.load_session(initial.meta.session_id)
    assert after.meta == replace(before.meta, selection=selection)
    assert after.entries == before.entries
    wire = json.loads(
        store.path_for_session(initial.meta.session_id).read_text().splitlines()[0]
    )
    assert wire["provider_id"] == "new" and wire["model_id"] == "b"


def test_update_selection_upgrades_real_v4_checkpoint_session(tmp_path):
    service, store, _ = make_service(tmp_path)
    session_id = "legacy-v4"
    meta = {
        "record_type": "session_meta",
        "format_version": 4,
        "session_id": session_id,
        "workspace_id": store.workspace.workspace_id,
        "workspace_path": str(store.workspace.workspace_path),
        "title": "legacy",
        "created_at": NOW.isoformat(timespec="microseconds"),
        "updated_at": NOW.isoformat(timespec="microseconds"),
    }
    checkpoint = b'{"record_type":"run_checkpoint","format_version":4,"checkpoint_id":"old-checkpoint","run_id":"old-run","kind":"cancelled","user_message":{"role":"user","content":"continue"},"progress":[],"partial_assistant_message":{"role":"assistant","content":"partial"},"recorded_at":"2026-08-22T03:04:05.000000+00:00"}\n'
    path = store.path_for_session(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(meta).encode() + b"\n" + checkpoint)
    before = service.load_session(session_id)
    assert before.meta.format_version == 4
    target = SessionSelection("local", "new")
    service.update_selection(session_id, target)
    after = service.load_session(session_id)
    assert after.meta == replace(before.meta, format_version=6, selection=target)
    assert after.entries == before.entries
    assert all(
        json.loads(line)["format_version"] == 6
        for line in path.read_text().splitlines()
    )


def test_update_selection_upgrades_real_v1_session(tmp_path):
    service, store, _ = make_service(tmp_path)
    write_v1_session(store, "legacy-v1")
    before = service.load_session("legacy-v1")
    target = SessionSelection("local", "new")
    service.update_selection("legacy-v1", target)
    after = service.load_session("legacy-v1")
    assert after.meta == replace(before.meta, format_version=6, selection=target)
    assert after.entries == before.entries


def test_update_selection_missing_or_corrupt_session_keeps_safe_errors(tmp_path):
    service, store, _ = make_service(tmp_path)
    target = SessionSelection("local", "a")
    with pytest.raises(SessionNotFoundError) as caught:
        service.update_selection("missing", target)
    assert "missing" not in str(caught.value)
    path = store.path_for_session("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"private failure https://example.test/v1 TEST_ONLY_SECRET token-value"
    path.write_bytes(original)
    with pytest.raises(SessionFormatError) as caught:
        service.update_selection("broken", target)
    for private in ("https://example.test/v1", "TEST_ONLY_SECRET", "token-value"):
        assert private not in str(caught.value)
    assert path.read_bytes() == original


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_service(
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    session_ids: list[str] | None = None,
) -> tuple[SessionService, JsonlSessionStore, Clock]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    identity = resolve_workspace(workspace_path)
    store = JsonlSessionStore(identity, tmp_path / "mica-data", lock_timeout=0.1)
    active_clock = clock or Clock()
    values = iter(session_ids or [])
    service = SessionService(
        store,
        now=active_clock,
        session_id_factory=lambda: next(values),
    )
    return service, store, active_clock


def make_turn(number: int, completed_at: datetime = NOW) -> TurnEntry:
    return TurnEntry(
        turn_id=f"turn-{number}",
        run_id=f"run-{number}",
        user_message=Message(MessageRole.USER, f"user-{number}"),
        assistant_message=Message(MessageRole.ASSISTANT, f"assistant-{number}"),
        completed_at=completed_at,
    )


def write_v1_session(store: JsonlSessionStore, session_id: str) -> bytes:
    meta = {
        "record_type": "session_meta",
        "format_version": 1,
        "session_id": session_id,
        "workspace_id": store.workspace.workspace_id,
        "workspace_path": str(store.workspace.workspace_path),
        "title": "legacy",
        "created_at": NOW.isoformat(timespec="microseconds"),
        "updated_at": NOW.isoformat(timespec="microseconds"),
    }
    turn = {
        "record_type": "turn",
        "format_version": 1,
        "turn_id": "turn-1",
        "run_id": "run-1",
        "user_message": {"role": "user", "content": "legacy user"},
        "assistant_message": {"role": "assistant", "content": "legacy answer"},
        "completed_at": NOW.isoformat(timespec="microseconds"),
    }
    payload = b"".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        for record in (meta, turn)
    )
    path = store.path_for_session(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def test_v1_mutations_upgrade_and_branch_keeps_source_unchanged(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["branch-v3"])
    for session_id in ("rename-v1", "rewind-v1", "clear-v1", "source-v1"):
        write_v1_session(store, session_id)

    assert service.rename_session("rename-v1", "renamed").meta.format_version == 6
    assert service.rewind_session("rewind-v1", "turn-1").meta.format_version == 6
    assert service.clear_session("clear-v1").meta.format_version == 6

    source_bytes = store.path_for_session("source-v1").read_bytes()
    branch = service.branch_session("source-v1")
    assert branch.meta.format_version == 6
    assert branch.turns[0].completed_at == NOW
    assert store.path_for_session("source-v1").read_bytes() == source_bytes
    assert store.load("source-v1").meta.format_version == 1


def test_create_load_and_factory_are_injected(tmp_path: Path) -> None:
    clock = Clock()
    service, store, _ = make_service(
        tmp_path, clock=clock, session_ids=["created-by-test"]
    )

    session = service.create_session("first")
    loaded = service.load_session("created-by-test")

    assert session == loaded
    assert session.meta.session_id == "created-by-test"
    assert session.meta.workspace_id == store.workspace.workspace_id
    assert session.meta.workspace_path == str(store.workspace.workspace_path)
    assert session.meta.created_at == NOW
    assert session.meta.updated_at == NOW


def test_list_sorts_stably_and_preserves_corruption_diagnostics(
    tmp_path: Path,
) -> None:
    service, store, clock = make_service(
        tmp_path, session_ids=["same-b", "same-a", "newer"]
    )
    service.create_session("b")
    service.create_session("a")
    clock.value = NOW + timedelta(seconds=1)
    service.create_session("newer")
    corrupt = store.session_directory / ("f" * 64 + ".jsonl")
    corrupt.write_bytes(b"not-json\n")

    result = service.list_sessions()

    assert [item.meta.session_id for item in result.sessions] == [
        "newer",
        "same-a",
        "same-b",
    ]
    assert len(result.problems) == 1
    assert result.problems[0].error_type == "SessionFormatError"


def test_last_active_requires_a_healthy_session(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["healthy"])
    corrupt = store.session_directory / ("e" * 64 + ".jsonl")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"bad\n")

    with pytest.raises(NoHealthySessionError, match="no healthy session") as caught:
        service.last_active_session()
    assert caught.value.problems


def test_commit_turn_updates_activity_but_never_rewinds_it(tmp_path: Path) -> None:
    clock = Clock()
    service, _, _ = make_service(tmp_path, clock=clock, session_ids=["session"])
    service.create_session()
    clock.value = NOW + timedelta(seconds=5)
    committed = service.commit_turn("session", make_turn(1, clock.value))
    assert committed.meta.updated_at == clock.value

    clock.value = NOW - timedelta(days=1)
    same = service.commit_turn("session", make_turn(2, NOW + timedelta(seconds=6)))

    assert same.meta.updated_at == NOW + timedelta(seconds=6)
    assert len(same.turns) == 2


def test_rename_preserves_identity_and_history(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    original = service.create_session("old")
    with_turn = service.commit_turn("session", make_turn(1))

    renamed = service.rename_session("session", "new")

    assert renamed.meta.session_id == original.meta.session_id
    assert renamed.meta.workspace_id == original.meta.workspace_id
    assert renamed.meta.created_at == original.meta.created_at
    assert renamed.meta.title == "new"
    assert renamed.turns == with_turn.turns


def test_branch_has_new_id_preserves_history_and_time_contract(tmp_path: Path) -> None:
    clock = Clock(NOW - timedelta(days=1))
    service, _, _ = make_service(
        tmp_path, clock=clock, session_ids=["source", "branch"]
    )
    source = service.create_session("source title")
    source = service.commit_turn("source", make_turn(1, NOW))
    clock.value = NOW - timedelta(days=1)

    branch = service.branch_session("source")

    assert branch.meta.session_id == "branch"
    assert branch.meta.title == source.meta.title
    assert branch.meta.created_at == source.meta.created_at
    assert branch.meta.updated_at == source.meta.updated_at
    assert branch.turns == source.turns
    assert service.load_session("source") == source
    assert branch.turns[0].completed_at >= branch.meta.created_at


def test_rewind_and_clear_update_activity_without_rewinding_time(
    tmp_path: Path,
) -> None:
    clock = Clock(NOW)
    service, _, _ = make_service(
        tmp_path, clock=clock, session_ids=["session", "other"]
    )
    created = service.create_session("preserve me")
    service.create_session("other")
    service.commit_turn("session", make_turn(1, NOW))
    clock.value = NOW + timedelta(seconds=10)

    rewound = service.rewind_session("session", "turn-1")

    assert rewound.turns == ()
    assert rewound.meta.updated_at == NOW + timedelta(seconds=10)
    assert rewound.meta.title == created.meta.title
    assert rewound.meta.workspace_id == created.meta.workspace_id
    assert rewound.meta.workspace_path == created.meta.workspace_path
    assert service.last_active_session().meta.session_id == "session"

    clock.value = NOW + timedelta(seconds=20)
    cleared = service.clear_session("session")
    assert cleared.meta.updated_at == NOW + timedelta(seconds=20)
    assert cleared.meta.title == created.meta.title
    assert cleared.meta.workspace_id == created.meta.workspace_id
    assert cleared.meta.workspace_path == created.meta.workspace_path

    clock.value = NOW
    unchanged = service.clear_session("session")
    assert unchanged.meta.updated_at == cleared.meta.updated_at


def test_rewind_removes_target_and_following_turns(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    service.commit_turn("session", make_turn(1, NOW))
    service.commit_turn("session", make_turn(2, NOW + timedelta(seconds=1)))
    service.commit_turn("session", make_turn(3, NOW + timedelta(seconds=2)))

    middle = service.rewind_session("session", "turn-2")
    assert [turn.turn_id for turn in middle.turns] == ["turn-1"]
    first = service.rewind_session("session", "turn-1")
    assert first.turns == ()


def test_rewind_last_and_missing_target_are_explicit(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    service.commit_turn("session", make_turn(1))

    assert service.rewind_session("session", "turn-1").turns == ()
    with pytest.raises(TurnNotFoundError) as caught:
        service.rewind_session("session", "missing")
    assert caught.value.turn_id == "missing"


def test_context_snapshot_expands_complete_tool_history(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, session_ids=["tool-session"])
    service.create_session()
    call = ToolCall("call-1", "read", {"path": "notes.txt"})
    turn = TurnEntry(
        "turn-tools",
        "run-tools",
        Message(MessageRole.USER, "inspect"),
        Message(MessageRole.ASSISTANT, "done"),
        NOW,
        (
            Message(MessageRole.ASSISTANT, "checking", (call,)),
            Message(
                MessageRole.TOOL,
                '{"status":"success"}',
                tool_call_id="call-1",
            ),
        ),
    )
    service.commit_turn("tool-session", turn)

    assert service.context_snapshot("tool-session") == turn.context_messages


def test_clear_delete_and_context_snapshot_are_scoped_and_immutable(
    tmp_path: Path,
) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["first", "second"])
    first = service.create_session("keep title")
    service.create_session("other")
    service.commit_turn("first", make_turn(1))
    service.commit_turn("first", make_turn(2, NOW + timedelta(seconds=1)))

    context = service.context_snapshot("first")
    assert isinstance(context, tuple)
    assert [(message.role, message.content) for message in context] == [
        (MessageRole.USER, "user-1"),
        (MessageRole.ASSISTANT, "assistant-1"),
        (MessageRole.USER, "user-2"),
        (MessageRole.ASSISTANT, "assistant-2"),
    ]
    assert service.context_snapshot("second") == ()

    cleared = service.clear_session("first")
    assert cleared.meta.session_id == first.meta.session_id
    assert cleared.meta.title == first.meta.title
    assert cleared.turns == ()
    service.delete_session("first")
    assert not store.path_for_session("first").exists()
    assert store.path_for_session("second").exists()


def test_service_does_not_import_runtime_or_provider_modules() -> None:
    code = (
        "import sys\n"
        "import mica_agent.session_service\n"
        "assert 'mica_agent.app' not in sys.modules\n"
        "assert 'mica_agent.engine' not in sys.modules\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
        "assert 'mica_agent.cli' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_boundary_protection_corruption_and_determinism(
    tmp_path: Path,
) -> None:
    cutoff = NOW - timedelta(days=30)
    clock = Clock(cutoff - timedelta(days=1))
    service, store, _ = make_service(
        tmp_path,
        clock=clock,
        session_ids=["old-b", "old-a", "boundary", "fresh", "protected"],
    )
    old_b = service.create_session()
    old_a = service.create_session()
    boundary = service.create_session()
    fresh = service.create_session()
    protected = service.create_session()
    for session_id, updated_at in (
        (old_b.meta.session_id, cutoff - timedelta(seconds=2)),
        (old_a.meta.session_id, cutoff - timedelta(seconds=1)),
        (boundary.meta.session_id, cutoff),
        (fresh.meta.session_id, NOW),
        (protected.meta.session_id, cutoff - timedelta(seconds=3)),
    ):
        current = store.load(session_id)
        store.rewrite(
            type(current)(
                type(current.meta)(
                    session_id=current.meta.session_id,
                    format_version=current.meta.format_version,
                    workspace_id=current.meta.workspace_id,
                    workspace_path=current.meta.workspace_path,
                    title=current.meta.title,
                    created_at=current.meta.created_at,
                    updated_at=updated_at,
                ),
                current.turns,
            )
        )
    corrupt = store.session_directory / ("d" * 64 + ".jsonl")
    corrupt.write_bytes(b"broken\n")

    result = service.cleanup_expired(
        protected_session_ids={"protected"},
        now=NOW,
    )

    assert result.deleted_session_ids == ("old-a", "old-b")
    assert result.skipped_current_session_ids == ("protected",)
    assert result.skipped_locked_session_ids == ()
    assert result.skipped_changed_session_ids == ()
    assert len(result.problems) == 1
    assert result.problems[0].error_type == "SessionFormatError"
    assert store.path_for_session("boundary").exists()
    assert store.path_for_session("fresh").exists()


def test_cleanup_rechecks_snapshot_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(NOW - timedelta(days=31))
    service, store, _ = make_service(
        tmp_path, clock=clock, session_ids=["stale", "deletable"]
    )
    service.create_session("stale")
    service.create_session("deletable")
    cutoff = NOW - timedelta(days=30, seconds=1)
    for session_id in ("stale", "deletable"):
        current = store.load(session_id)
        store.rewrite(
            type(current)(
                replace(current.meta, updated_at=cutoff),
                current.turns,
            )
        )

    original_try_delete = store.try_delete

    def update_before_delete(
        session_id: str,
        *,
        expected_snapshot: object,
    ) -> SessionDeleteStatus:
        if session_id == "stale":
            current = store.load(session_id)
            store.rewrite(
                type(current)(
                    replace(
                        current.meta,
                        title="updated concurrently",
                        updated_at=NOW,
                    ),
                    current.turns,
                )
            )
        return original_try_delete(
            session_id,
            expected_snapshot=expected_snapshot,
        )

    monkeypatch.setattr(store, "try_delete", update_before_delete)
    result = service.cleanup_expired(now=NOW)

    assert result.deleted_session_ids == ("deletable",)
    assert result.skipped_changed_session_ids == ("stale",)
    assert store.load("stale").meta.title == "updated concurrently"
    assert store.path_for_session("deletable").exists() is False


def test_cleanup_keeps_snapshot_that_becomes_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(NOW - timedelta(days=31))
    service, store, _ = make_service(
        tmp_path, clock=clock, session_ids=["corrupt", "deletable"]
    )
    service.create_session("corrupt")
    service.create_session("deletable")
    cutoff = NOW - timedelta(days=30, seconds=1)
    for session_id in ("corrupt", "deletable"):
        current = store.load(session_id)
        store.rewrite(
            type(current)(replace(current.meta, updated_at=cutoff), current.turns)
        )

    original_try_delete = store.try_delete

    def corrupt_before_delete(
        session_id: str,
        *,
        expected_snapshot: object,
    ) -> SessionDeleteStatus:
        if session_id == "corrupt":
            store.path_for_session(session_id).write_bytes(b"broken\\n")
        return original_try_delete(
            session_id,
            expected_snapshot=expected_snapshot,
        )

    monkeypatch.setattr(store, "try_delete", corrupt_before_delete)
    result = service.cleanup_expired(now=NOW)

    assert result.deleted_session_ids == ("deletable",)
    assert store.path_for_session("corrupt").exists()
    assert any(
        problem.file_id == "corrupt" and problem.error_type == "SessionFormatError"
        for problem in result.problems
    )


def test_cleanup_skips_a_locked_expired_session_without_waiting(
    tmp_path: Path,
) -> None:
    clock = Clock(NOW - timedelta(days=31))
    service, store, _ = make_service(
        tmp_path, clock=clock, session_ids=["locked", "deletable"]
    )
    locked = service.create_session()
    deletable = service.create_session()
    cutoff = NOW - timedelta(days=30, seconds=1)
    for session in (locked, deletable):
        current = store.load(session.meta.session_id)
        store.rewrite(
            type(current)(
                type(current.meta)(
                    session_id=current.meta.session_id,
                    format_version=current.meta.format_version,
                    workspace_id=current.meta.workspace_id,
                    workspace_path=current.meta.workspace_path,
                    title=current.meta.title,
                    created_at=current.meta.created_at,
                    updated_at=cutoff,
                ),
                current.turns,
            )
        )

    from mica_agent._file_lock import FileLock

    lock_path = store._lock_path_for_session("locked")
    with FileLock(lock_path, timeout=1):
        result = service.cleanup_expired(now=NOW)

    assert result.deleted_session_ids == ("deletable",)
    assert result.skipped_locked_session_ids == ("locked",)
    assert result.skipped_changed_session_ids == ()
    assert not store.path_for_session("deletable").exists()
    assert store.path_for_session("locked").exists()


def make_checkpoint(
    *,
    checkpoint_id: str = "checkpoint-1",
    run_id: str = "checkpoint-run-1",
    kind: RunCheckpointKind = RunCheckpointKind.CANCELLED,
    recorded_at: datetime = NOW,
) -> RunCheckpoint:
    return RunCheckpoint(
        checkpoint_id,
        run_id,
        kind,
        Message(MessageRole.USER, "unfinished user request"),
        recorded_at,
        partial_assistant_message=Message(MessageRole.ASSISTANT, "partial output"),
    )


def test_checkpoint_timeline_context_and_lifecycle_operations(tmp_path: Path) -> None:
    service, _, clock = make_service(tmp_path, session_ids=["session", "branch"])
    service.create_session()
    first = make_turn(1, NOW)
    service.commit_turn("session", first)
    checkpoint = make_checkpoint(recorded_at=NOW + timedelta(seconds=1))
    committed = service.commit_checkpoint("session", checkpoint)
    last = make_turn(2, NOW + timedelta(seconds=2))
    committed = service.commit_turn("session", last)

    assert committed.entries == (first, checkpoint, last)
    assert committed.turns == (first, last)
    assert committed.checkpoints == (checkpoint,)
    expected = SessionContextSnapshot(
        (
            *first.context_messages,
            checkpoint.user_message,
            partial_history_record("partial output"),
            interruption_record(RunCheckpointKind.CANCELLED),
            *last.context_messages,
        ),
        (None, None),
    )
    assert service.model_context_snapshot("session") == expected
    assert service.context_snapshot("session") == expected.messages

    branched = service.branch_session("session")
    assert branched.entries == committed.entries
    assert service.model_context_snapshot("branch") == expected
    clock.value = NOW + timedelta(seconds=3)
    rewound = service.rewind_session("session", "turn-2")
    assert rewound.entries == (first, checkpoint)
    assert service.model_context_snapshot("session") == SessionContextSnapshot(
        expected.messages[:-2], (None,)
    )
    assert service.clear_session("session").entries == ()
    assert service.model_context_snapshot("session") == SessionContextSnapshot((), ())
    assert service.model_context_snapshot("branch") == expected


@pytest.mark.parametrize(
    "statuses",
    [
        (RunCheckpointToolStatus.SUCCESS, RunCheckpointToolStatus.NOT_STARTED),
        (RunCheckpointToolStatus.FAILED, RunCheckpointToolStatus.IN_PROGRESS),
        (RunCheckpointToolStatus.IN_PROGRESS, RunCheckpointToolStatus.NOT_STARTED),
        (RunCheckpointToolStatus.NOT_STARTED, RunCheckpointToolStatus.NOT_STARTED),
    ],
)
@pytest.mark.parametrize("assistant_text", ["working", ""])
def test_incomplete_tool_batch_preserves_all_facts_without_reissuing_calls(
    tmp_path: Path,
    statuses,
    assistant_text: str,
) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session(selection=SessionSelection("different", "current"))
    calls = (
        ToolCall("call-a", "read", {"path": "a.txt", "options": [1, {"flag": True}]}),
        ToolCall("call-b", "write", {"path": "b.txt", "content": "x"}),
    )
    assistant = Message(MessageRole.ASSISTANT, assistant_text, calls)
    record = step_continuation(ProviderApi.ANTHROPIC_MESSAGES, assistant)
    checkpoint = RunCheckpoint(
        "checkpoint-tools",
        "run-tools",
        RunCheckpointKind.RECOVERABLE_FAILURE,
        Message(MessageRole.USER, "run both tools"),
        NOW,
        progress=(
            RunCheckpointProgress(
                Message(MessageRole.ASSISTANT, assistant_text)
                if assistant_text
                else None,
                tuple(
                    recorded_tool(call, status)
                    for call, status in zip(calls, statuses, strict=True)
                ),
                record,
            ),
        ),
    )
    service.commit_checkpoint("session", checkpoint)
    following = replace(
        make_turn(1),
        continuations=(
            step_continuation(
                ProviderApi.OPENAI_RESPONSES,
                make_turn(1).assistant_message,
            ),
        ),
    )
    service.commit_turn("session", following)
    original = store.path_for_session("session").read_bytes()
    facts = {
        "assistant_text": assistant_text,
        "tools": [
            {
                "call_id": "call-a",
                "name": "read",
                "arguments": {"path": "a.txt", "options": [1, {"flag": True}]},
                "status": statuses[0].value,
                "result": "真实结果 call-a"
                if statuses[0]
                in {RunCheckpointToolStatus.SUCCESS, RunCheckpointToolStatus.FAILED}
                else None,
            },
            {
                "call_id": "call-b",
                "name": "write",
                "arguments": {"path": "b.txt", "content": "x"},
                "status": statuses[1].value,
                "result": None,
            },
        ],
    }
    expected = SessionContextSnapshot(
        (
            checkpoint.user_message,
            incomplete_batch_record(facts),
            interruption_record(checkpoint.kind),
            *following.context_messages,
        ),
        following.continuations,
    )
    assert service.model_context_snapshot("session") == expected
    assert service.context_snapshot("session") == expected.messages
    assert json.loads(expected.messages[1].content.split("\n", 1)[1]) == facts
    assert all(
        not message.tool_calls and message.tool_call_id is None
        for message in expected.messages
    )
    for private in (
        "presentation-only.txt",
        "private-thinking",
        "opaque-signature",
        "opaque-data",
    ):
        assert all(private not in message.content for message in expected.messages)
    assert store.path_for_session("session").read_bytes() == original
    assert (
        service.load_session("session").checkpoints[0].progress[0].continuation
        == record
    )


def test_only_checkpoint_session_is_healthy_and_activity_uses_checkpoint(
    tmp_path: Path,
) -> None:
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    checkpoint = make_checkpoint(recorded_at=NOW + timedelta(seconds=3))

    committed = service.commit_checkpoint("session", checkpoint)

    assert committed.turns == ()
    assert committed.checkpoints == (checkpoint,)
    assert committed.meta.updated_at == checkpoint.recorded_at
    assert service.last_active_session().entries == (checkpoint,)


def test_context_snapshot_redacts_checkpoint_absolute_paths(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    absolute = r"C:\Users\private\note.txt"
    url = "https://example.test/api/v1?name=value"
    call = ToolCall("path-call", "read", {"path": absolute, "url": url})
    result = Message(
        MessageRole.TOOL,
        json.dumps({"path": absolute, "url": url}),
        tool_call_id="path-call",
    )
    presentation = ToolPresentationSnapshot(
        "path-call",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "note.txt"},
    )
    checkpoint = RunCheckpoint(
        "path-checkpoint",
        "path-run",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "preserve user text"),
        NOW,
        progress=(
            RunCheckpointProgress(
                tools=(
                    RunCheckpointToolProgress(
                        call,
                        RunCheckpointToolStatus.SUCCESS,
                        result,
                        presentation,
                    ),
                )
            ),
        ),
        partial_assistant_message=Message(MessageRole.ASSISTANT, "partial output"),
    )
    service.commit_checkpoint("session", checkpoint)
    raw = store.path_for_session("session").read_text(encoding="utf-8")
    context = service.context_snapshot("session")
    assert absolute not in raw
    assert url in raw
    assert all(absolute not in repr(message) for message in context)
    assert context[0].content == "preserve user text"
    assert context[1].tool_calls[0].arguments == {
        "path": "[REDACTED_PATH]",
        "url": url,
    }
    assert json.loads(context[2].content) == {
        "path": "[REDACTED_PATH]",
        "url": url,
    }
    assert context[3] == partial_history_record("partial output")
    assert context[4] == interruption_record(RunCheckpointKind.CANCELLED)
    assert all(message.role is not MessageRole.SYSTEM for message in context)
    assert service.model_context_snapshot("session") == SessionContextSnapshot(
        context, (None,)
    )


def test_store_nonblocking_delete_distinguishes_statuses(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()

    assert store.try_delete("missing") is SessionDeleteStatus.NOT_FOUND
    assert store.try_delete("session") is SessionDeleteStatus.DELETED
    assert store.try_delete("session") is SessionDeleteStatus.NOT_FOUND


def test_selection_survives_business_metadata_operations_and_timeline_writes(tmp_path):
    from mica_agent.session_contracts import SessionSelection

    service, store, _ = make_service(tmp_path, session_ids=["selected", "branch"])
    selection = SessionSelection("local", "chat")
    selected = service.create_session(selection=selection)
    store.append_turn(selected.meta.session_id, make_turn(1))
    assert service.rename_session("selected", "renamed").meta.selection == selection
    assert service.branch_session("selected", "turn-1").meta.selection == selection
    assert service.rewind_session("selected", "turn-1").meta.selection == selection
    assert service.clear_session("selected").meta.selection == selection
    assert store.load("selected").entries == ()


def test_model_context_snapshot_is_empty_frozen_and_content_free_in_repr(tmp_path):
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    assert service.model_context_snapshot("session") == SessionContextSnapshot((), ())
    call = ToolCall("private-call", "read", {"path": "private-argument.txt"})
    assistant = Message(MessageRole.ASSISTANT, "private-content", (call,))
    record = step_continuation(ProviderApi.ANTHROPIC_MESSAGES, assistant)
    checkpoint = RunCheckpoint(
        "cp",
        "run",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "private-user"),
        NOW,
        (
            RunCheckpointProgress(
                Message(MessageRole.ASSISTANT, assistant.content),
                (recorded_tool(call, RunCheckpointToolStatus.SUCCESS),),
                record,
            ),
        ),
    )
    service.commit_checkpoint("session", checkpoint)
    snapshot = service.model_context_snapshot("session")
    assert isinstance(snapshot.messages, tuple) and isinstance(
        snapshot.continuations, tuple
    )
    assert "SessionContextSnapshot" in repr(snapshot)
    for private in (
        "private-user",
        "private-content",
        "private-call",
        "private-argument.txt",
        "saved-provider",
        "saved-model",
        "private-thinking",
        "opaque-signature",
        "opaque-data",
    ):
        assert private not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.messages = ()
    assert not hasattr(snapshot, "__dict__")
    assert store.load("session").checkpoints == (checkpoint,)


def test_model_context_snapshot_loads_once_and_old_entry_delegates(
    tmp_path, monkeypatch
):
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    turn = replace(
        make_turn(1),
        continuations=(
            step_continuation(
                ProviderApi.OPENAI_RESPONSES,
                make_turn(1).assistant_message,
            ),
        ),
    )
    service.commit_turn("session", turn)
    original_load = store.load
    reads = []

    def load_once(session_id):
        reads.append(session_id)
        assert reads == ["session"]
        return original_load(session_id)

    monkeypatch.setattr(store, "load", load_once)
    expected = SessionContextSnapshot(turn.context_messages, turn.continuations)
    assert service.model_context_snapshot("session") == expected
    assert reads == ["session"]
    delegated = []

    def snapshot(session_id):
        delegated.append(session_id)
        return expected

    monkeypatch.setattr(service, "model_context_snapshot", snapshot)
    assert service.context_snapshot("session") == expected.messages
    assert delegated == ["session"] and reads == ["session"]


@pytest.mark.parametrize("api", list(ProviderApi))
@pytest.mark.parametrize("text", ["working", ""])
def test_complete_checkpoint_batch_keeps_all_calls_real_results_and_continuation(
    tmp_path, api, text
):
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session(selection=SessionSelection("current", "different"))
    calls = (
        ToolCall("a", "read", {"path": "a.txt"}),
        ToolCall("b", "read", {"path": "b.txt"}),
    )
    tools = (
        recorded_tool(calls[0], RunCheckpointToolStatus.SUCCESS),
        recorded_tool(calls[1], RunCheckpointToolStatus.FAILED),
    )
    assistant = Message(MessageRole.ASSISTANT, text, calls)
    record = step_continuation(api, assistant)
    checkpoint = RunCheckpoint(
        "cp",
        "run",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "inspect"),
        NOW,
        (
            RunCheckpointProgress(),
            RunCheckpointProgress(
                Message(MessageRole.ASSISTANT, text) if text else None, tools, record
            ),
            RunCheckpointProgress(Message(MessageRole.ASSISTANT, "after failed tool")),
        ),
    )
    service.commit_checkpoint("session", checkpoint)
    path = store.path_for_session("session")
    original = path.read_bytes()
    expected = SessionContextSnapshot(
        (
            checkpoint.user_message,
            assistant,
            tools[0].result,
            tools[1].result,
            Message(MessageRole.ASSISTANT, "after failed tool"),
            interruption_record(checkpoint.kind),
        ),
        (record, None),
    )
    assert service.model_context_snapshot("session") == expected
    assert service.context_snapshot("session") == expected.messages
    check_step_association(expected.continuations[0], expected.messages[1])
    assert path.read_bytes() == original


def test_mixed_timeline_preserves_sources_statuses_thinking_and_lifecycle(tmp_path):
    service, store, _ = make_service(tmp_path, session_ids=["source", "branch"])
    service.create_session(
        selection=SessionSelection("current", "ignored-for-projection")
    )
    call = ToolCall("turn-call", "read", {"path": "turn.txt"})
    tool_message = Message(MessageRole.ASSISTANT, "", (call,))
    chat_record = step_continuation(
        ProviderApi.OPENAI_COMPLETIONS,
        tool_message,
        provider="chat-provider",
        model="chat-model",
    )
    first = replace(
        make_turn(1),
        intermediate_messages=(
            tool_message,
            Message(MessageRole.TOOL, "turn fact", tool_call_id=call.call_id),
        ),
        continuations=(chat_record, None),
    )
    service.commit_turn("source", first)
    empty = Message(MessageRole.ASSISTANT, "")
    thinking_record = step_continuation(
        ProviderApi.ANTHROPIC_MESSAGES,
        empty,
        provider="messages-provider",
        model="messages-model",
    )
    text = Message(MessageRole.ASSISTANT, "completed text")
    responses_record = step_continuation(
        ProviderApi.OPENAI_RESPONSES,
        text,
        provider="responses-provider",
        model="responses-model",
    )
    checkpoint = RunCheckpoint(
        "cp",
        "run-cp",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "continue"),
        NOW,
        (
            RunCheckpointProgress(),
            RunCheckpointProgress(empty),
            RunCheckpointProgress(continuation=thinking_record),
            RunCheckpointProgress(text, continuation=responses_record),
        ),
        partial_assistant_message=Message(MessageRole.ASSISTANT, "unfinished text"),
    )
    service.commit_checkpoint("source", checkpoint)
    last = replace(
        make_turn(2),
        assistant_message=empty,
        continuations=(
            step_continuation(
                ProviderApi.OPENAI_RESPONSES,
                empty,
                provider="another-provider",
                model="another-model",
                status=ContinuationStatus.UNREPLAYABLE,
            ),
        ),
    )
    service.commit_turn("source", last)
    expected = SessionContextSnapshot(
        (
            *first.context_messages,
            checkpoint.user_message,
            empty,
            empty,
            text,
            partial_history_record("unfinished text"),
            interruption_record(checkpoint.kind),
            *last.context_messages,
        ),
        (
            *first.continuations,
            None,
            thinking_record,
            responses_record,
            *last.continuations,
        ),
    )
    path = store.path_for_session("source")
    original = path.read_bytes()
    assert service.model_context_snapshot("source") == expected
    assert service.context_snapshot("source") == expected.messages
    assert len(expected.continuations) == sum(
        m.role is MessageRole.ASSISTANT for m in expected.messages
    )
    for message, record in zip(
        (m for m in expected.messages if m.role is MessageRole.ASSISTANT),
        expected.continuations,
        strict=True,
    ):
        if record is not None:
            check_step_association(record, message)
    assert path.read_bytes() == original
    service.branch_session("source")
    assert service.model_context_snapshot("branch") == expected
    service.rewind_session("branch", last.turn_id)
    assert service.model_context_snapshot("branch") == SessionContextSnapshot(
        expected.messages[:-2], expected.continuations[:-1]
    )
    service.clear_session("branch")
    assert service.model_context_snapshot("branch") == SessionContextSnapshot((), ())
    assert service.model_context_snapshot("source") == expected
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", list(RunCheckpointKind))
@pytest.mark.parametrize("partial", [None, ""])
def test_checkpoint_without_progress_records_real_interruption_as_user(
    tmp_path, kind, partial
):
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    checkpoint = RunCheckpoint(
        "cp",
        "run",
        kind,
        Message(MessageRole.USER, "request"),
        NOW,
        partial_assistant_message=None
        if partial is None
        else Message(MessageRole.ASSISTANT, partial),
    )
    service.commit_checkpoint("session", checkpoint)
    messages = (checkpoint.user_message,)
    if partial is not None:
        messages += (partial_history_record(partial),)
    expected = SessionContextSnapshot((*messages, interruption_record(kind)), ())
    assert service.model_context_snapshot("session") == expected
    assert service.context_snapshot("session") == expected.messages


def test_snapshot_does_not_apply_one_turn_limit_to_entire_session(tmp_path):
    service, _, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()
    record = ModelStepContinuation(
        "provider",
        "model",
        ProviderApi.OPENAI_RESPONSES,
        ContinuationStatus.REPLAYABLE,
        (
            ResponsesReasoningItem("reasoning", (), "x" * 600_000),
            TextItem(TextPhase.FINAL_ANSWER),
        ),
    )
    assert (
        continuation_size(record)
        < MAX_TURN_CONTINUATION_BYTES
        < continuation_size(record) * 2
    )
    turns = tuple(replace(make_turn(n), continuations=(record,)) for n in (1, 2))
    for turn in turns:
        service.commit_turn("session", turn)
    assert service.model_context_snapshot("session") == SessionContextSnapshot(
        tuple(message for turn in turns for message in turn.context_messages),
        (record, record),
    )


@pytest.mark.parametrize("entry_point", ["model_context_snapshot", "context_snapshot"])
def test_snapshot_keeps_missing_corrupt_and_invalid_association_errors_safe(
    tmp_path, entry_point
):
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    snapshot = getattr(service, entry_point)
    with pytest.raises(SessionNotFoundError):
        snapshot("missing")
    service.create_session()
    call = ToolCall("expected-call", "read", {"path": "a.txt"})
    assistant = Message(MessageRole.ASSISTANT, "", (call,))
    checkpoint = RunCheckpoint(
        "cp",
        "run",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "request"),
        NOW,
        (
            RunCheckpointProgress(
                tools=(recorded_tool(call, RunCheckpointToolStatus.SUCCESS),),
                continuation=step_continuation(
                    ProviderApi.ANTHROPIC_MESSAGES, assistant
                ),
            ),
        ),
    )
    service.commit_checkpoint("session", checkpoint)
    path = store.path_for_session("session")
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    records[1]["progress"][0]["continuation"]["items"][-1]["call_id"] = (
        "private-wrong-call"
    )
    invalid = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    for payload in (
        invalid,
        b"private corrupt content https://private.test sk-test-private",
    ):
        path.write_bytes(payload)
        with pytest.raises(SessionFormatError) as caught:
            snapshot("session")
        for private in (
            "private-wrong-call",
            "private corrupt",
            "https://private.test",
            "sk-test-private",
        ):
            assert private not in str(caught.value)
        assert path.read_bytes() == payload


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_snapshot_reads_fixed_legacy_wire_without_migration(tmp_path, version):
    service, store, _ = make_service(tmp_path)
    session_id = f"legacy-v{version}"
    path = store.path_for_session(session_id)
    if version == 1:
        original = write_v1_session(store, session_id)
        expected = SessionContextSnapshot(
            (
                Message(MessageRole.USER, "legacy user"),
                Message(MessageRole.ASSISTANT, "legacy answer"),
            ),
            (None,),
        )
    else:
        # 按实际旧版字段手写固定记录，不调用新 serializer 或改写其版本号。
        entries = {
            2: b'{"record_type":"turn","format_version":2,"turn_id":"old-turn","run_id":"old-run","messages":[{"role":"user","content":"inspect"},{"role":"assistant","content":"","tool_calls":[{"call_id":"a","name":"read","arguments":{"path":"a.txt"}}]},{"role":"tool","content":"one","tool_call_id":"a"},{"role":"assistant","content":"done"}],"completed_at":"2026-08-22T03:04:05.000000+00:00"}\n',
            3: b'{"record_type":"turn","format_version":3,"turn_id":"old-turn","run_id":"old-run","messages":[{"role":"user","content":"inspect"},{"role":"assistant","content":"","tool_calls":[{"call_id":"a","name":"read","arguments":{"path":"a.txt"}}]},{"role":"tool","content":"one","tool_call_id":"a"},{"role":"assistant","content":"done"}],"tool_presentations":[{"call_id":"a","tool_name":"read","kind":"read","status":"success","payload":{"path":"a.txt"},"message":null}],"completed_at":"2026-08-22T03:04:05.000000+00:00"}\n',
            4: b'{"record_type":"run_checkpoint","format_version":4,"checkpoint_id":"old-checkpoint","run_id":"old-run","kind":"cancelled","user_message":{"role":"user","content":"continue"},"progress":[{"assistant_message":{"role":"assistant","content":"old step"},"tools":[]}],"partial_assistant_message":{"role":"assistant","content":"partial"},"recorded_at":"2026-08-22T03:04:05.000000+00:00"}\n',
            5: b'{"record_type":"turn","format_version":5,"turn_id":"old-turn","run_id":"old-run","messages":[{"role":"user","content":"inspect"},{"role":"assistant","content":"","tool_calls":[{"call_id":"a","name":"read","arguments":{"path":"a.txt"}}]},{"role":"tool","content":"one","tool_call_id":"a"},{"role":"assistant","content":"done"}],"tool_presentations":[{"call_id":"a","tool_name":"read","kind":"read","status":"success","payload":{"path":"a.txt"},"message":null}],"completed_at":"2026-08-22T03:04:05.000000+00:00"}\n'
            b'{"record_type":"run_checkpoint","format_version":5,"checkpoint_id":"old-checkpoint","run_id":"old-checkpoint-run","kind":"cancelled","user_message":{"role":"user","content":"continue"},"progress":[{"assistant_message":{"role":"assistant","content":"old step"},"tools":[]}],"partial_assistant_message":{"role":"assistant","content":"partial"},"recorded_at":"2026-08-22T03:04:05.000000+00:00"}\n',
        }
        meta = {
            "record_type": "session_meta",
            "format_version": version,
            "session_id": session_id,
            "workspace_id": store.workspace.workspace_id,
            "workspace_path": str(store.workspace.workspace_path),
            "title": "legacy",
            "created_at": NOW.isoformat(timespec="microseconds"),
            "updated_at": NOW.isoformat(timespec="microseconds"),
        }
        if version == 5:
            meta.update(provider_id="old-provider", model_id="old-model")
        original = json.dumps(meta).encode() + b"\n" + entries[version]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
        tool_history = (
            Message(MessageRole.USER, "inspect"),
            Message(
                MessageRole.ASSISTANT, "", (ToolCall("a", "read", {"path": "a.txt"}),)
            ),
            Message(MessageRole.TOOL, "one", tool_call_id="a"),
            Message(MessageRole.ASSISTANT, "done"),
        )
        checkpoint_history = (
            Message(MessageRole.USER, "continue"),
            Message(MessageRole.ASSISTANT, "old step"),
            partial_history_record("partial"),
            interruption_record(RunCheckpointKind.CANCELLED),
        )
        messages = checkpoint_history if version == 4 else tool_history
        if version == 5:
            messages += checkpoint_history
        expected = SessionContextSnapshot(
            messages, (None,) * (1 if version == 4 else 3 if version == 5 else 2)
        )
    assert service.model_context_snapshot(session_id) == expected
    assert service.context_snapshot(session_id) == expected.messages
    assert service.load_session(session_id).meta.format_version == version
    assert path.read_bytes() == original
