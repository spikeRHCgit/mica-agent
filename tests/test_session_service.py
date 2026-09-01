"""P3-03 Session Service 业务语义测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mica_agent.jsonl_store import (
    JsonlSessionStore,
    SessionDeleteStatus,
)
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import TurnEntry
from mica_agent.session_service import (
    NoHealthySessionError,
    SessionService,
    TurnNotFoundError,
)
from mica_agent.tool_contracts import ToolCall
from mica_agent.workspace import resolve_workspace

NOW = datetime(2026, 8, 22, 3, 4, 5, tzinfo=UTC)


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

    assert service.rename_session("rename-v1", "renamed").meta.format_version == 3
    assert service.rewind_session("rewind-v1", "turn-1").meta.format_version == 3
    assert service.clear_session("clear-v1").meta.format_version == 3

    source_bytes = store.path_for_session("source-v1").read_bytes()
    branch = service.branch_session("source-v1")
    assert branch.meta.format_version == 3
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


def test_store_nonblocking_delete_distinguishes_statuses(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path, session_ids=["session"])
    service.create_session()

    assert store.try_delete("missing") is SessionDeleteStatus.NOT_FOUND
    assert store.try_delete("session") is SessionDeleteStatus.DELETED
    assert store.try_delete("session") is SessionDeleteStatus.NOT_FOUND
