"""会话业务语义边界，建立在 Workspace 与 JSONL Store 之上。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .jsonl_store import (
    JsonlSessionStore,
    ScanProblem,
    SessionDeleteStatus,
    SessionStorageError,
    StoredSession,
)
from .models import Message
from .session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionError,
    SessionFormatError,
    SessionMeta,
    TurnEntry,
)


class SessionServiceError(SessionError):
    """会话业务规则错误。"""


class NoHealthySessionError(SessionServiceError):
    """当前工作区没有可用的健康会话。"""

    def __init__(self, problems: Iterable[ScanProblem] = ()) -> None:
        super().__init__("no healthy session in the current workspace")
        self.problems = tuple(problems)


class TurnNotFoundError(SessionServiceError):
    """回退目标轮次不存在。"""

    def __init__(self, turn_id: str) -> None:
        super().__init__(f"turn not found: {turn_id}")
        self.turn_id = turn_id


@dataclass(frozen=True, slots=True)
class SessionListResult:
    """当前工作区的健康会话和扫描诊断。"""

    sessions: tuple[StoredSession, ...]
    problems: tuple[ScanProblem, ...]


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """一次过期清理的确定性结果。"""

    deleted_session_ids: tuple[str, ...]
    skipped_current_session_ids: tuple[str, ...]
    skipped_locked_session_ids: tuple[str, ...]
    skipped_changed_session_ids: tuple[str, ...]
    problems: tuple[ScanProblem, ...]


class SessionService:
    """拥有会话创建、变更、上下文和生命周期清理语义的业务服务。"""

    CLEANUP_AGE = timedelta(days=30)

    def __init__(
        self,
        store: JsonlSessionStore,
        *,
        now: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(store, JsonlSessionStore):
            raise TypeError("store must be a JsonlSessionStore")
        self.store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._session_id_factory = session_id_factory or (lambda: uuid4().hex)

    def _current_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime):
            raise TypeError("now must return a datetime")
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("now must return timezone-aware UTC")
        return value.astimezone(UTC)

    def _new_session_id(self) -> str:
        value = self._session_id_factory()
        if not isinstance(value, str):
            raise TypeError("session_id_factory must return a str")
        return value

    @staticmethod
    def _sorted_sessions(
        sessions: Iterable[StoredSession],
    ) -> tuple[StoredSession, ...]:
        return tuple(
            sorted(
                sessions,
                key=lambda session: (
                    -session.meta.updated_at.timestamp(),
                    session.meta.session_id,
                ),
            )
        )

    @staticmethod
    def _session_ids(values: Iterable[str]) -> set[str]:
        if isinstance(values, str):
            return {values}
        return set(values)

    def _activity_updated_at(
        self,
        meta: SessionMeta,
        turns: Iterable[TurnEntry] = (),
    ) -> datetime:
        latest_turn_at = max(
            (turn.completed_at for turn in turns),
            default=meta.created_at,
        )
        return max(meta.updated_at, self._current_time(), latest_turn_at)

    def create_session(self, title: str = "") -> StoredSession:
        """创建并持久化一个当前工作区的空会话。"""
        created_at = self._current_time()
        meta = SessionMeta(
            session_id=self._new_session_id(),
            format_version=CURRENT_SESSION_FORMAT_VERSION,
            workspace_id=self.store.workspace.workspace_id,
            workspace_path=str(self.store.workspace.workspace_path),
            title=title,
            created_at=created_at,
            updated_at=created_at,
        )
        return self.store.create(meta)

    def load_session(self, session_id: str) -> StoredSession:
        return self.store.load(session_id)

    def list_sessions(self) -> SessionListResult:
        result = self.store.scan()
        return SessionListResult(
            sessions=self._sorted_sessions(result.sessions),
            problems=result.problems,
        )

    def last_active_session(self) -> StoredSession:
        result = self.list_sessions()
        if not result.sessions:
            raise NoHealthySessionError(result.problems)
        return result.sessions[0]

    def commit_turn(self, session_id: str, turn: TurnEntry) -> StoredSession:
        """只提交已经完整构造并通过契约校验的一轮。"""
        return self.store.append_turn(session_id, turn)

    def rename_session(self, session_id: str, title: str) -> StoredSession:
        def rename(current: StoredSession) -> StoredSession:
            meta = SessionMeta(
                session_id=current.meta.session_id,
                format_version=CURRENT_SESSION_FORMAT_VERSION,
                workspace_id=current.meta.workspace_id,
                workspace_path=current.meta.workspace_path,
                title=title,
                created_at=current.meta.created_at,
                updated_at=self._activity_updated_at(current.meta, current.turns),
            )
            return StoredSession(meta, current.turns)

        return self.store.update(session_id, rename)

    def branch_session(
        self, source_session_id: str, title: str | None = None
    ) -> StoredSession:
        source = self.store.load(source_session_id)
        session_id = self._new_session_id()
        if session_id == source.meta.session_id:
            raise SessionServiceError("branch session id must differ from source")
        updated_at = self._activity_updated_at(source.meta, source.turns)
        meta = SessionMeta(
            session_id=session_id,
            format_version=CURRENT_SESSION_FORMAT_VERSION,
            workspace_id=source.meta.workspace_id,
            workspace_path=source.meta.workspace_path,
            title=source.meta.title if title is None else title,
            created_at=source.meta.created_at,
            updated_at=updated_at,
        )
        return self.store.create(meta, source.turns)

    copy_session = branch_session

    def rewind_session(self, session_id: str, from_turn_id: str) -> StoredSession:
        def rewind(current: StoredSession) -> StoredSession:
            for index, turn in enumerate(current.turns):
                if turn.turn_id == from_turn_id:
                    turns = current.turns[:index]
                    meta = SessionMeta(
                        session_id=current.meta.session_id,
                        format_version=CURRENT_SESSION_FORMAT_VERSION,
                        workspace_id=current.meta.workspace_id,
                        workspace_path=current.meta.workspace_path,
                        title=current.meta.title,
                        created_at=current.meta.created_at,
                        updated_at=self._activity_updated_at(current.meta, turns),
                    )
                    return StoredSession(meta, turns)
            raise TurnNotFoundError(from_turn_id)

        return self.store.update(session_id, rewind)

    def clear_session(self, session_id: str) -> StoredSession:
        def clear(current: StoredSession) -> StoredSession:
            meta = SessionMeta(
                session_id=current.meta.session_id,
                format_version=CURRENT_SESSION_FORMAT_VERSION,
                workspace_id=current.meta.workspace_id,
                workspace_path=current.meta.workspace_path,
                title=current.meta.title,
                created_at=current.meta.created_at,
                updated_at=self._activity_updated_at(current.meta),
            )
            return StoredSession(meta, ())

        return self.store.update(session_id, clear)

    def delete_session(self, session_id: str) -> None:
        self.store.delete(session_id)

    def context_snapshot(self, session_id: str) -> tuple[Message, ...]:
        session = self.store.load(session_id)
        return tuple(
            message for turn in session.turns for message in turn.context_messages
        )

    def cleanup_expired(
        self,
        protected_session_ids: Iterable[str] = (),
        *,
        current_session_ids: Iterable[str] = (),
        current_session_id: str | None = None,
        now: datetime | None = None,
    ) -> CleanupResult:
        """非阻塞清理超过 30 天未活动的健康会话。"""
        protected = self._session_ids(protected_session_ids)
        protected.update(self._session_ids(current_session_ids))
        if current_session_id is not None:
            protected.add(current_session_id)
        reference_time = (
            self._current_time() if now is None else self._validate_time(now)
        )
        cutoff = reference_time - self.CLEANUP_AGE
        scanned = self.store.scan(lock_timeout=0)
        ordered = self._sorted_sessions(scanned.sessions)
        deleted: list[str] = []
        skipped_current: list[str] = []
        skipped_locked: list[str] = []
        skipped_changed: list[str] = []
        problems = list(scanned.problems)

        for session in ordered:
            session_id = session.meta.session_id
            if session.meta.updated_at >= cutoff:
                continue
            if session_id in protected:
                skipped_current.append(session_id)
                continue
            try:
                status = self.store.try_delete(
                    session_id,
                    expected_snapshot=session,
                )
            except (SessionFormatError, SessionStorageError) as exc:
                problems.append(
                    ScanProblem(
                        file_id=session_id,
                        error_type=type(exc).__name__,
                        diagnostic=str(exc),
                    )
                )
                continue
            if status is SessionDeleteStatus.DELETED:
                deleted.append(session_id)
            elif status is SessionDeleteStatus.LOCKED:
                skipped_locked.append(session_id)
            elif status is SessionDeleteStatus.CHANGED:
                skipped_changed.append(session_id)
            else:
                problems.append(
                    ScanProblem(
                        file_id=session_id,
                        error_type="SessionNotFoundError",
                        diagnostic="session disappeared during cleanup",
                    )
                )

        return CleanupResult(
            deleted_session_ids=tuple(deleted),
            skipped_current_session_ids=tuple(skipped_current),
            skipped_locked_session_ids=tuple(skipped_locked),
            skipped_changed_session_ids=tuple(skipped_changed),
            problems=tuple(problems),
        )

    @staticmethod
    def _validate_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")
        return value.astimezone(UTC)


__all__ = [
    "CleanupResult",
    "NoHealthySessionError",
    "SessionListResult",
    "SessionService",
    "SessionServiceError",
    "TurnNotFoundError",
]
