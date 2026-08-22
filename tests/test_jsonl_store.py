"""P3-02 JSONL Store、故障与并发测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mica_agent.jsonl_store import (
    JsonlSessionStore,
    ScanResult,
    SessionAlreadyExistsError,
    SessionDeleteStatus,
    SessionFormatError,
    SessionLockTimeoutError,
    SessionNotFoundError,
    SessionStorageIOError,
    StoredSession,
    session_filename,
)
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionMeta,
    TurnEntry,
    serialize_session_record,
)
from mica_agent.workspace import resolve_workspace

NOW = datetime(2026, 8, 21, 3, 4, 5, tzinfo=UTC)


def make_store(tmp_path: Path) -> tuple[JsonlSessionStore, object]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    identity = resolve_workspace(workspace_path)
    return JsonlSessionStore(identity, tmp_path / "mica-data"), identity


def make_meta(identity: object, **changes: object) -> SessionMeta:
    workspace = identity
    values = {
        "session_id": "session/unsafe:1",
        "format_version": CURRENT_SESSION_FORMAT_VERSION,
        "workspace_id": workspace.workspace_id,
        "workspace_path": str(workspace.workspace_path),
        "title": "title",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return SessionMeta(**values)  # type: ignore[arg-type]


def make_turn(number: int, completed_at: datetime = NOW) -> TurnEntry:
    return TurnEntry(
        turn_id=f"turn-{number}",
        run_id=f"run-{number}",
        user_message=Message(MessageRole.USER, f"用户 {number}"),
        assistant_message=Message(MessageRole.ASSISTANT, f"助手 {number}"),
        completed_at=completed_at,
    )


def test_session_filename_is_hash_only_and_path_safe() -> None:
    session_id = r"folder/..\secret:会话"

    filename = session_filename(session_id)

    assert filename.endswith(".jsonl")
    assert (
        filename
        == __import__("hashlib").sha256(session_id.encode("utf-8")).hexdigest()
        + ".jsonl"
    )
    assert "/" not in filename and "\\" not in filename and ".." not in filename


def test_writer_uses_stable_sorted_json_keys(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(
        identity, session_id="ordered", updated_at=NOW + timedelta(seconds=1)
    )
    turn = make_turn(1, NOW + timedelta(seconds=1))

    store.create(meta, (turn,))

    expected = b"".join(
        json.dumps(
            serialize_session_record(record),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
        + b"\n"
        for record in (meta, turn)
    )
    raw = store.path_for_session(meta.session_id).read_bytes()
    assert raw == expected
    assert raw.splitlines()[0].startswith(b'{"created_at":')
    assert b'"record_type":"session_meta"' in raw.splitlines()[0]


def test_create_load_and_append_preserve_snapshot_and_utf8(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="session-1")

    created = store.create(meta)
    assert created == StoredSession(meta, ())
    loaded = store.load("session-1")
    assert loaded == created

    completed = NOW + timedelta(seconds=1)
    appended = store.append_turn("session-1", make_turn(1, completed))

    assert appended.turns == (make_turn(1, completed),)
    assert appended.meta.updated_at == completed
    assert store.load("session-1") == appended
    raw = store.path_for_session("session-1").read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 2
    assert "用户".encode() in raw


def test_create_with_turns_and_duplicate_create_do_not_overwrite(
    tmp_path: Path,
) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="session-2")
    original = store.create(meta, (make_turn(1),))
    original_bytes = store.path_for_session(meta.session_id).read_bytes()

    with pytest.raises(SessionAlreadyExistsError):
        store.create(meta, ())

    assert store.load(meta.session_id) == original
    assert store.path_for_session(meta.session_id).read_bytes() == original_bytes


def test_duplicate_turn_and_run_and_old_timestamp_leave_file_unchanged(
    tmp_path: Path,
) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(
        identity, session_id="session-3", updated_at=NOW + timedelta(seconds=2)
    )
    store.create(meta, (make_turn(1, NOW + timedelta(seconds=2)),))
    path = store.path_for_session(meta.session_id)
    original_bytes = path.read_bytes()

    with pytest.raises(SessionFormatError, match="duplicate turn_id"):
        store.append_turn(meta.session_id, make_turn(1, NOW + timedelta(seconds=1)))
    with pytest.raises(SessionFormatError, match="duplicate run_id"):
        store.append_turn(
            meta.session_id,
            TurnEntry(
                turn_id="turn-2",
                run_id="run-1",
                user_message=Message(MessageRole.USER, "u"),
                assistant_message=Message(MessageRole.ASSISTANT, "a"),
                completed_at=NOW + timedelta(seconds=1),
            ),
        )
    with pytest.raises(SessionFormatError, match="nondecreasing"):
        store.append_turn(meta.session_id, make_turn(2, NOW + timedelta(seconds=1)))
    assert path.read_bytes() == original_bytes


def test_rewrite_changes_title_and_turns_but_not_identity(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="session-4")
    store.create(meta, (make_turn(1),))
    rewritten_meta = make_meta(
        identity,
        session_id="session-4",
        title="renamed",
        updated_at=NOW + timedelta(seconds=2),
    )
    rewritten = store.rewrite(StoredSession(rewritten_meta, ()))

    assert rewritten == store.load("session-4")
    assert rewritten.meta.title == "renamed"
    assert rewritten.turns == ()

    wrong = make_meta(
        identity,
        session_id="session-4",
        workspace_id="0" * 64,
    )
    with pytest.raises(SessionFormatError):
        store.rewrite(StoredSession(wrong, ()))


def test_rewrite_and_delete_missing_sessions_are_explicit(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="missing")

    with pytest.raises(SessionNotFoundError):
        store.rewrite(StoredSession(meta, ()))
    with pytest.raises(SessionNotFoundError):
        store.delete(meta.session_id)


def test_strict_reader_rejects_corruption_without_echoing_message(
    tmp_path: Path,
) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="session-corrupt")
    store.create(meta, (make_turn(1),))
    path = store.path_for_session(meta.session_id)
    sensitive = "private message marker"
    cases = {
        b"": "empty",
        b"   \n": "blank",
        b"\xef\xbb\xbf{}\n": "BOM",
        b"\xff\n": "UTF-8",
        b"not-json\n": "JSON",
        b"[]\n": "JSON object",
        b"{}": "truncated",
        b'{"record_type":"session_meta","record_type":"turn"}\n': "duplicate",
        (
            b'{"record_type":"session_meta","format_version":1,"session_id":"x",'
            b'"workspace_id":"x","workspace_path":"x","title":"x",'
            b'"created_at":"2026-08-21T03:04:05+00:00",'
            b'"updated_at":"2026-08-21T03:04:05+00:00"}\n\n'
        ): "blank",
    }
    for content, category in cases.items():
        path.write_bytes(content)
        with pytest.raises(SessionFormatError) as caught:
            store.load(meta.session_id)
        assert category.lower() in str(caught.value).lower() or category == "JSON"
        assert sensitive not in str(caught.value)


def test_filename_and_workspace_identity_mismatch_are_rejected(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="session-name")
    store.create(meta)
    path = store.path_for_session(meta.session_id)
    renamed = path.with_name("0" * 64 + ".jsonl")
    path.rename(renamed)

    result = store.scan()
    assert len(result.sessions) == 0
    assert len(result.problems) == 1

    renamed.rename(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(identity.workspace_id, "0" * 64),
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(SessionFormatError, match="workspace_id"):
        store.load(meta.session_id)


def test_scan_keeps_healthy_files_when_one_file_is_corrupt(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    healthy = make_meta(identity, session_id="healthy")
    store.create(healthy)
    bad_path = store.session_directory / ("f" * 64 + ".jsonl")
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"bad\n")

    result = store.scan()

    assert isinstance(result, ScanResult)
    assert [item.meta.session_id for item in result.sessions] == ["healthy"]
    assert len(result.problems) == 1
    assert result.problems[0].error_type == "SessionFormatError"
    assert not list(store.session_directory.glob("*.tmp"))


def test_lock_timeout_and_atomic_replace_failure_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="locked")
    store.create(meta)
    locked_store = JsonlSessionStore(
        identity, tmp_path / "mica-data", lock_timeout=0.05
    )

    from mica_agent._file_lock import FileLock

    lock_path = store._lock_path_for_session(meta.session_id)
    with FileLock(lock_path, timeout=1), pytest.raises(SessionLockTimeoutError):
        locked_store.load(meta.session_id)

    path = store.path_for_session(meta.session_id)
    original = path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise PermissionError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SessionStorageIOError):
        store.append_turn(meta.session_id, make_turn(1, NOW + timedelta(seconds=1)))

    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))

    monkeypatch.undo()
    restored = store.append_turn(
        meta.session_id, make_turn(1, NOW + timedelta(seconds=1))
    )
    assert len(restored.turns) == 1
    assert not list(path.parent.glob("*.tmp"))


def test_session_locks_are_per_session_and_thread_safe(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    first = make_meta(identity, session_id="thread-first")
    second = make_meta(identity, session_id="thread-second")
    store.create(first)
    store.create(second)
    from mica_agent._file_lock import FileLock

    first_lock = store._lock_path_for_session(first.session_id)
    limited_store = JsonlSessionStore(
        identity, tmp_path / "mica-data", lock_timeout=0.05
    )
    outcomes: list[str] = []
    with FileLock(first_lock, timeout=1):
        thread = threading.Thread(
            target=lambda: _load_in_thread(limited_store, first.session_id, outcomes)
        )
        thread.start()
        assert store.load(second.session_id).meta.session_id == second.session_id
        thread.join(timeout=5)
    assert outcomes == ["timeout"]


def _load_in_thread(
    store: JsonlSessionStore, session_id: str, outcomes: list[str]
) -> None:
    try:
        store.load(session_id)
    except SessionLockTimeoutError:
        outcomes.append("timeout")


def test_two_processes_append_without_interleaving(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="process-shared")
    store.create(meta)
    code = """
from datetime import UTC, datetime
from pathlib import Path
import sys
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import TurnEntry
from mica_agent.workspace import resolve_workspace
root, data_root, session_id, number = sys.argv[1:]
identity = resolve_workspace(Path(root))
store = JsonlSessionStore(identity, Path(data_root), lock_timeout=10)
now = datetime(2026, 8, 21, 3, 4, 5, tzinfo=UTC)
turn = TurnEntry(
    turn_id=f'turn-{number}', run_id=f'run-{number}',
    user_message=Message(MessageRole.USER, f'user-{number}'),
    assistant_message=Message(MessageRole.ASSISTANT, f'assistant-{number}'),
    completed_at=now,
)
store.append_turn(session_id, turn)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(identity.workspace_path),
                str(store.data_root),
                meta.session_id,
                str(number),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in (1, 2)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    loaded = store.load(meta.session_id)
    assert {turn.turn_id for turn in loaded.turns} == {"turn-1", "turn-2"}
    assert store.path_for_session(meta.session_id).read_bytes().count(b"\n") == 3


def test_try_delete_is_nonblocking_and_distinguishes_lock(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="try-delete")
    store.create(meta)

    from mica_agent._file_lock import FileLock

    lock_path = store._lock_path_for_session(meta.session_id)
    with FileLock(lock_path, timeout=1):
        assert store.try_delete(meta.session_id) is SessionDeleteStatus.LOCKED
    assert store.try_delete(meta.session_id) is SessionDeleteStatus.DELETED
    assert store.try_delete(meta.session_id) is SessionDeleteStatus.NOT_FOUND


def test_try_delete_skips_changed_expected_snapshot(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    meta = make_meta(identity, session_id="changed")
    store.create(meta)
    old_snapshot = store.load(meta.session_id)
    new_snapshot = store.rewrite(
        StoredSession(
            replace(old_snapshot.meta, title="new title"),
            old_snapshot.turns,
        )
    )

    status = store.try_delete(
        meta.session_id,
        expected_snapshot=old_snapshot,
    )

    assert status is SessionDeleteStatus.CHANGED
    assert store.load(meta.session_id) == new_snapshot


def test_delete_only_removes_one_session_and_keeps_lock_file(tmp_path: Path) -> None:
    store, identity = make_store(tmp_path)
    first = make_meta(identity, session_id="first")
    second = make_meta(identity, session_id="second")
    store.create(first)
    store.create(second)
    first_path = store.path_for_session(first.session_id)
    second_path = store.path_for_session(second.session_id)
    lock_path = store._lock_path_for_session(first.session_id)

    store.delete(first.session_id)

    assert not first_path.exists()
    assert second_path.exists()
    assert lock_path.exists()
    assert store.session_directory.exists()
