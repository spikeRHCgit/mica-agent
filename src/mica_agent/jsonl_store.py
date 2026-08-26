"""可靠的单工作区 JSONL 会话文件存储边界。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ._file_lock import locked_file
from .session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionError,
    SessionFormatError,
    SessionMeta,
    TurnEntry,
    deserialize_session_record,
    serialize_session_record,
)
from .workspace import WorkspaceIdentity, default_data_root


class SessionStorageError(SessionError):
    """文件系统存储错误的稳定边界。"""


class SessionNotFoundError(SessionStorageError):
    """目标会话文件不存在。"""

    def __init__(self) -> None:
        super().__init__("session not found")


class SessionAlreadyExistsError(SessionStorageError):
    """创建目标会话时文件已经存在。"""

    def __init__(self) -> None:
        super().__init__("session already exists")


class SessionLockTimeoutError(SessionStorageError):
    """在限定时间内无法获得会话锁。"""

    def __init__(self) -> None:
        super().__init__("session lock timed out")


class SessionStorageIOError(SessionStorageError):
    """未能完成文件系统操作。"""

    def __init__(self, operation: str) -> None:
        super().__init__(f"session storage operation failed: {operation}")


class SessionDeleteStatus(Enum):
    """非阻塞单会话删除尝试的结果。"""

    DELETED = "deleted"
    LOCKED = "locked"
    NOT_FOUND = "not_found"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class StoredSession:
    """完整、不可变且已通过跨记录校验的会话快照。"""

    meta: SessionMeta
    turns: tuple[TurnEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.meta, SessionMeta):
            raise TypeError("meta must be a SessionMeta")
        if not isinstance(self.turns, tuple):
            raise TypeError("turns must be a tuple")
        turn_ids: set[str] = set()
        run_ids: set[str] = set()
        previous_time = self.meta.created_at
        for turn in self.turns:
            if not isinstance(turn, TurnEntry):
                raise TypeError("turns must contain only TurnEntry values")
            if turn.turn_id in turn_ids:
                raise ValueError("duplicate turn_id")
            if turn.run_id in run_ids:
                raise ValueError("duplicate run_id")
            if turn.completed_at < self.meta.created_at:
                raise ValueError("turn completed_at is earlier than session created_at")
            if turn.completed_at < previous_time:
                raise ValueError("turn times must be nondecreasing")
            turn_ids.add(turn.turn_id)
            run_ids.add(turn.run_id)
            previous_time = turn.completed_at
        if self.turns and self.meta.updated_at < self.turns[-1].completed_at:
            raise ValueError("updated_at is earlier than the last turn")


@dataclass(frozen=True, slots=True)
class ScanProblem:
    """单个损坏或不可读正式文件的安全扫描诊断。"""

    file_id: str
    error_type: str
    diagnostic: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """扫描返回的健康快照和独立问题集合。"""

    sessions: tuple[StoredSession, ...]
    problems: tuple[ScanProblem, ...]


_SESSION_FILENAME_RE = re.compile(r"[0-9a-f]{64}\.jsonl\Z")


def session_filename(session_id: str) -> str:
    """把任意合法 session ID 映射为安全的固定文件名。"""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be non-empty")
    return f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.jsonl"


class _DuplicateJsonKey(ValueError):
    """JSON object key 重复的内部解析标记。"""


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _snapshot_from_records(records: list[object]) -> StoredSession:
    if not records:
        raise SessionFormatError("session file is empty")
    if all(isinstance(record, dict) for record in records):
        versions = [record.get("format_version") for record in records]
        if any(version != versions[0] for version in versions[1:]):
            raise SessionFormatError("session file contains mixed format versions")
    parsed: list[SessionMeta | TurnEntry] = []
    for record in records:
        parsed.append(deserialize_session_record(record))
    if not isinstance(parsed[0], SessionMeta):
        raise SessionFormatError("first record must be session_meta")
    if any(isinstance(record, SessionMeta) for record in parsed[1:]):
        raise SessionFormatError("session_meta must be the first record")
    try:
        return StoredSession(parsed[0], tuple(parsed[1:]))
    except (TypeError, ValueError) as exc:
        raise SessionFormatError(str(exc)) from None


class JsonlSessionStore:
    """当前工作区一个会话一个 JSONL 文件的可靠存储。"""

    def __init__(
        self,
        workspace: WorkspaceIdentity,
        data_root: Path | str | os.PathLike[str] | None = None,
        *,
        lock_timeout: float = 5.0,
    ) -> None:
        if not isinstance(workspace, WorkspaceIdentity):
            raise TypeError("workspace must be a WorkspaceIdentity")
        if lock_timeout < 0:
            raise ValueError("lock_timeout must not be negative")
        self.workspace = workspace
        self.data_root = (
            Path(data_root) if data_root is not None else default_data_root()
        )
        self.lock_timeout = lock_timeout

    @property
    def session_directory(self) -> Path:
        return self.data_root / "sessions" / self.workspace.session_directory_name

    def path_for_session(self, session_id: str) -> Path:
        return self.session_directory / session_filename(session_id)

    def _lock_path_for_session(self, session_id: str) -> Path:
        return self.session_directory / f"{session_filename(session_id)}.lock"

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        try:
            with locked_file(
                self._lock_path_for_session(session_id), self.lock_timeout
            ):
                yield
        except TimeoutError:
            raise SessionLockTimeoutError() from None

    def _validate_meta(self, meta: SessionMeta) -> None:
        if not isinstance(meta, SessionMeta):
            raise TypeError("meta must be a SessionMeta")
        if meta.workspace_id != self.workspace.workspace_id:
            raise SessionFormatError("workspace_id does not match current workspace")
        if meta.workspace_path != str(self.workspace.workspace_path):
            raise SessionFormatError("workspace_path does not match current workspace")

    def _make_snapshot(
        self, meta: SessionMeta, turns: Iterable[TurnEntry] = ()
    ) -> StoredSession:
        self._validate_meta(meta)
        try:
            snapshot = StoredSession(meta, tuple(turns))
        except (TypeError, ValueError) as exc:
            raise SessionFormatError(str(exc)) from None
        return snapshot

    @staticmethod
    def _as_current_snapshot(snapshot: StoredSession) -> StoredSession:
        meta = snapshot.meta
        if meta.format_version == CURRENT_SESSION_FORMAT_VERSION:
            return snapshot
        return StoredSession(
            SessionMeta(
                session_id=meta.session_id,
                format_version=CURRENT_SESSION_FORMAT_VERSION,
                workspace_id=meta.workspace_id,
                workspace_path=meta.workspace_path,
                title=meta.title,
                created_at=meta.created_at,
                updated_at=meta.updated_at,
            ),
            snapshot.turns,
        )

    def _read_bytes(self, path: Path) -> bytes:
        try:
            with path.open("rb") as stream:
                return stream.read()
        except FileNotFoundError:
            raise SessionNotFoundError() from None
        except OSError as exc:
            raise SessionStorageIOError("read") from exc

    def _parse_bytes(self, data: bytes) -> StoredSession:
        if data.startswith(b"\xef\xbb\xbf"):
            raise SessionFormatError("UTF-8 BOM is not allowed")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SessionFormatError("invalid UTF-8") from None
        if not text:
            raise SessionFormatError("session file is empty")
        if not text.endswith("\n"):
            raise SessionFormatError("truncated final line")
        lines = text[:-1].split("\n")
        records: list[object] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                raise SessionFormatError(f"blank line at line {line_number}")
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_strict_object_pairs,
                    parse_constant=_reject_json_constant,
                )
            except _DuplicateJsonKey:
                raise SessionFormatError(
                    f"duplicate JSON object key at line {line_number}"
                ) from None
            except (ValueError, json.JSONDecodeError):
                raise SessionFormatError(
                    f"invalid JSON at line {line_number}"
                ) from None
            records.append(record)
        return _snapshot_from_records(records)

    def _load_path(
        self, path: Path, requested_session_id: str | None = None
    ) -> StoredSession:
        snapshot = self._parse_bytes(self._read_bytes(path))
        expected_filename = session_filename(snapshot.meta.session_id)
        if path.name != expected_filename:
            raise SessionFormatError("session filename does not match metadata")
        if (
            requested_session_id is not None
            and snapshot.meta.session_id != requested_session_id
        ):
            raise SessionFormatError("session_id does not match requested session")
        self._validate_meta(snapshot.meta)
        return snapshot

    def _write_snapshot(
        self, path: Path, snapshot: StoredSession, *, replace: bool
    ) -> None:
        snapshot = self._as_current_snapshot(snapshot)
        records = (snapshot.meta, *snapshot.turns)
        try:
            payload = b"".join(
                (
                    json.dumps(
                        serialize_session_record(record),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                for record in records
            )
        except (TypeError, ValueError) as exc:
            raise SessionFormatError("session record cannot be serialized") from exc

        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary_path, path)
            else:
                os.rename(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise SessionStorageIOError("write") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def create(
        self, meta: SessionMeta, turns: Iterable[TurnEntry] = ()
    ) -> StoredSession:
        snapshot = self._as_current_snapshot(self._make_snapshot(meta, turns))
        path = self.path_for_session(meta.session_id)
        with self._session_lock(meta.session_id):
            if path.exists():
                raise SessionAlreadyExistsError()
            self._write_snapshot(path, snapshot, replace=False)
        return snapshot

    def load(self, session_id: str) -> StoredSession:
        path = self.path_for_session(session_id)
        with self._session_lock(session_id):
            if not path.is_file():
                raise SessionNotFoundError()
            return self._load_path(path, requested_session_id=session_id)

    read = load

    def append_turn(self, session_id: str, turn: TurnEntry) -> StoredSession:
        if not isinstance(turn, TurnEntry):
            raise TypeError("turn must be a TurnEntry")
        with self._session_lock(session_id):
            path = self.path_for_session(session_id)
            if not path.is_file():
                raise SessionNotFoundError()
            current = self._load_path(path, requested_session_id=session_id)
            snapshot = self._make_snapshot(
                SessionMeta(
                    session_id=current.meta.session_id,
                    format_version=CURRENT_SESSION_FORMAT_VERSION,
                    workspace_id=current.meta.workspace_id,
                    workspace_path=current.meta.workspace_path,
                    title=current.meta.title,
                    created_at=current.meta.created_at,
                    updated_at=max(current.meta.updated_at, turn.completed_at),
                ),
                (*current.turns, turn),
            )
            self._write_snapshot(path, snapshot, replace=True)
            return snapshot

    def update(
        self,
        session_id: str,
        transform: Callable[[StoredSession], StoredSession],
    ) -> StoredSession:
        """在一次 session lock 内读取、变换并原子替换快照。"""
        if not callable(transform):
            raise TypeError("transform must be callable")
        path = self.path_for_session(session_id)
        with self._session_lock(session_id):
            if not path.is_file():
                raise SessionNotFoundError()
            current = self._load_path(path, requested_session_id=session_id)
            snapshot = transform(current)
            if not isinstance(snapshot, StoredSession):
                raise TypeError("transform must return a StoredSession")
            self._validate_meta(snapshot.meta)
            if snapshot.meta.session_id != current.meta.session_id:
                raise SessionFormatError("session identity cannot change")
            if (
                snapshot.meta.workspace_id != current.meta.workspace_id
                or snapshot.meta.workspace_path != current.meta.workspace_path
            ):
                raise SessionFormatError("session workspace identity cannot change")
            snapshot = self._as_current_snapshot(snapshot)
            self._write_snapshot(path, snapshot, replace=True)
            return snapshot

    def rewrite(self, snapshot: StoredSession) -> StoredSession:
        if not isinstance(snapshot, StoredSession):
            raise TypeError("snapshot must be a StoredSession")
        self._validate_meta(snapshot.meta)
        snapshot = self._as_current_snapshot(snapshot)
        path = self.path_for_session(snapshot.meta.session_id)
        with self._session_lock(snapshot.meta.session_id):
            if not path.is_file():
                raise SessionNotFoundError()
            current = self._load_path(
                path, requested_session_id=snapshot.meta.session_id
            )
            if (
                snapshot.meta.workspace_id != current.meta.workspace_id
                or snapshot.meta.workspace_path != current.meta.workspace_path
            ):
                raise SessionFormatError("session workspace identity cannot change")
            self._write_snapshot(path, snapshot, replace=True)
        return snapshot

    def delete(self, session_id: str) -> None:
        with self._session_lock(session_id):
            path = self.path_for_session(session_id)
            if not path.is_file():
                raise SessionNotFoundError()
            try:
                path.unlink()
            except FileNotFoundError:
                raise SessionNotFoundError() from None
            except OSError as exc:
                raise SessionStorageIOError("delete") from exc

    def try_delete(
        self,
        session_id: str,
        *,
        expected_snapshot: StoredSession | None = None,
    ) -> SessionDeleteStatus:
        """不等待锁地尝试删除，并可在锁内校验预期快照。"""
        if expected_snapshot is not None and not isinstance(
            expected_snapshot, StoredSession
        ):
            raise TypeError("expected_snapshot must be a StoredSession or None")
        path = self.path_for_session(session_id)
        try:
            with locked_file(self._lock_path_for_session(session_id), timeout=0):
                try:
                    path.stat()
                except FileNotFoundError:
                    return SessionDeleteStatus.NOT_FOUND
                except OSError as exc:
                    raise SessionStorageIOError("delete") from exc
                if expected_snapshot is not None:
                    try:
                        current = self._load_path(path, requested_session_id=session_id)
                    except SessionNotFoundError:
                        return SessionDeleteStatus.NOT_FOUND
                    if current != expected_snapshot:
                        return SessionDeleteStatus.CHANGED
                try:
                    path.unlink()
                except FileNotFoundError:
                    return SessionDeleteStatus.NOT_FOUND
                except OSError as exc:
                    raise SessionStorageIOError("delete") from exc
                return SessionDeleteStatus.DELETED
        except TimeoutError:
            return SessionDeleteStatus.LOCKED
        except OSError as exc:
            raise SessionStorageIOError("delete") from exc

    def _scan_file_id(self, path: Path) -> str:
        if _SESSION_FILENAME_RE.fullmatch(path.name):
            return path.name
        return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]

    def scan(self, *, lock_timeout: float | None = None) -> ScanResult:
        if lock_timeout is not None and lock_timeout < 0:
            raise ValueError("lock_timeout must not be negative")
        effective_lock_timeout = (
            self.lock_timeout if lock_timeout is None else lock_timeout
        )
        directory = self.session_directory
        if not directory.is_dir():
            return ScanResult((), ())
        try:
            candidates = sorted(
                (
                    entry
                    for entry in directory.iterdir()
                    if entry.is_file() and entry.suffix == ".jsonl"
                ),
                key=lambda entry: entry.name,
            )
        except OSError as exc:
            raise SessionStorageIOError("scan") from exc

        healthy: list[StoredSession] = []
        problems: list[ScanProblem] = []
        for path in candidates:
            file_id = self._scan_file_id(path)
            try:
                if _SESSION_FILENAME_RE.fullmatch(path.name):
                    try:
                        with locked_file(
                            path.with_name(f"{path.name}.lock"), effective_lock_timeout
                        ):
                            snapshot = self._load_path(path)
                    except TimeoutError:
                        # Writers use copy-on-write, so an unlocked read still sees
                        # a complete old or new snapshot. Cleanup can then use
                        # try_delete() to make the non-blocking lock decision.
                        snapshot = self._load_path(path)
                else:
                    snapshot = self._load_path(path)
                healthy.append(snapshot)
            except SessionError as exc:
                problems.append(
                    ScanProblem(
                        file_id=file_id,
                        error_type=type(exc).__name__,
                        diagnostic=str(exc),
                    )
                )
        return ScanResult(tuple(healthy), tuple(problems))


JsonlStore = JsonlSessionStore


__all__ = [
    "JsonlSessionStore",
    "JsonlStore",
    "ScanProblem",
    "ScanResult",
    "SessionAlreadyExistsError",
    "SessionDeleteStatus",
    "SessionLockTimeoutError",
    "SessionNotFoundError",
    "SessionStorageError",
    "SessionStorageIOError",
    "StoredSession",
    "session_filename",
]
