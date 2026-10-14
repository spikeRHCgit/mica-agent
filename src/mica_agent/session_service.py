"""Session business-semantics boundary built on the workspace and the JSONL store."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .checkpoint_contracts import (
    RunCheckpoint,
    RunCheckpointProgress,
    RunCheckpointToolStatus,
)
from .continuation_contracts import ModelStepContinuation, check_step_association
from .jsonl_store import (
    JsonlSessionStore,
    ScanProblem,
    SessionDeleteStatus,
    SessionStorageError,
    StoredSession,
)
from .models import Message, MessageRole
from .session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionError,
    SessionFormatError,
    SessionMeta,
    SessionSelection,
    TurnEntry,
)
from .tool_contracts import to_mutable_json


class SessionServiceError(SessionError):
    """Session business-rule error."""


class NoHealthySessionError(SessionServiceError):
    """No healthy session is available in the current workspace."""

    def __init__(self, problems: Iterable[ScanProblem] = ()) -> None:
        super().__init__("no healthy session in the current workspace")
        self.problems = tuple(problems)


class TurnNotFoundError(SessionServiceError):
    """The rewind target turn does not exist."""

    def __init__(self, turn_id: str) -> None:
        super().__init__(f"turn not found: {turn_id}")
        self.turn_id = turn_id


@dataclass(frozen=True, slots=True)
class SessionListResult:
    """Healthy sessions and scan diagnostics for the current workspace."""

    sessions: tuple[StoredSession, ...]
    problems: tuple[ScanProblem, ...]


@dataclass(frozen=True, slots=True, repr=False)
class SessionContextSnapshot:
    """History and assistant-aligned continuations from one session read."""

    messages: tuple[Message, ...]
    continuations: tuple[ModelStepContinuation | None, ...]


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Deterministic outcome of one expiry cleanup pass."""

    deleted_session_ids: tuple[str, ...]
    skipped_current_session_ids: tuple[str, ...]
    skipped_locked_session_ids: tuple[str, ...]
    skipped_changed_session_ids: tuple[str, ...]
    problems: tuple[ScanProblem, ...]


class SessionService:
    """Business service owning session creation, mutation, context, and cleanup semantics."""

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
        entries: Iterable[TurnEntry | RunCheckpoint] = (),
    ) -> datetime:
        latest_entry_at = max(
            (
                entry.completed_at
                if isinstance(entry, TurnEntry)
                else entry.recorded_at
                for entry in entries
            ),
            default=meta.created_at,
        )
        return max(meta.updated_at, self._current_time(), latest_entry_at)

    def create_session(
        self, title: str = "", *, selection: SessionSelection | None = None
    ) -> StoredSession:
        """Create and persist an empty session in the current workspace."""
        created_at = self._current_time()
        meta = SessionMeta(
            session_id=self._new_session_id(),
            format_version=CURRENT_SESSION_FORMAT_VERSION,
            workspace_id=self.store.workspace.workspace_id,
            workspace_path=str(self.store.workspace.workspace_path),
            title=title,
            created_at=created_at,
            updated_at=created_at,
            selection=selection,
        )
        return self.store.create(meta)

    def load_session(self, session_id: str) -> StoredSession:
        return self.store.load(session_id)

    def update_selection(self, session_id: str, selection: SessionSelection) -> None:
        """Change only the selection under the store lock, upgrading legacy sessions."""

        def update(current: StoredSession) -> StoredSession:
            meta = replace(
                current.meta,
                format_version=CURRENT_SESSION_FORMAT_VERSION,
                selection=selection,
            )
            return StoredSession(meta, current.entries)

        self.store.update(session_id, update)

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
        """Commit only a turn that is fully constructed and contract-validated."""
        return self.store.append_turn(session_id, turn)

    def commit_checkpoint(
        self, session_id: str, checkpoint: RunCheckpoint
    ) -> StoredSession:
        """Commit an interruption checkpoint independent of any successful turn."""
        return self.store.append_checkpoint(session_id, checkpoint)

    def rename_session(self, session_id: str, title: str) -> StoredSession:
        def rename(current: StoredSession) -> StoredSession:
            meta = SessionMeta(
                session_id=current.meta.session_id,
                format_version=CURRENT_SESSION_FORMAT_VERSION,
                workspace_id=current.meta.workspace_id,
                workspace_path=current.meta.workspace_path,
                title=title,
                created_at=current.meta.created_at,
                updated_at=self._activity_updated_at(current.meta, current.entries),
                selection=current.meta.selection,
            )
            return StoredSession(meta, current.entries)

        return self.store.update(session_id, rename)

    def branch_session(
        self, source_session_id: str, title: str | None = None
    ) -> StoredSession:
        source = self.store.load(source_session_id)
        session_id = self._new_session_id()
        if session_id == source.meta.session_id:
            raise SessionServiceError("branch session id must differ from source")
        updated_at = self._activity_updated_at(source.meta, source.entries)
        meta = SessionMeta(
            session_id=session_id,
            format_version=CURRENT_SESSION_FORMAT_VERSION,
            workspace_id=source.meta.workspace_id,
            workspace_path=source.meta.workspace_path,
            title=source.meta.title if title is None else title,
            created_at=source.meta.created_at,
            updated_at=updated_at,
            selection=source.meta.selection,
        )
        return self.store.create(meta, source.entries)

    copy_session = branch_session

    def rewind_session(self, session_id: str, from_turn_id: str) -> StoredSession:
        def rewind(current: StoredSession) -> StoredSession:
            for index, entry in enumerate(current.entries):
                if isinstance(entry, TurnEntry) and entry.turn_id == from_turn_id:
                    entries = current.entries[:index]
                    meta = SessionMeta(
                        session_id=current.meta.session_id,
                        format_version=CURRENT_SESSION_FORMAT_VERSION,
                        workspace_id=current.meta.workspace_id,
                        workspace_path=current.meta.workspace_path,
                        title=current.meta.title,
                        created_at=current.meta.created_at,
                        updated_at=self._activity_updated_at(current.meta, entries),
                        selection=current.meta.selection,
                    )
                    return StoredSession(meta, entries)
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
                selection=current.meta.selection,
            )
            return StoredSession(meta, ())

        return self.store.update(session_id, clear)

    def delete_session(self, session_id: str) -> None:
        self.store.delete(session_id)

    @staticmethod
    def _incomplete_tool_batch_record(step: RunCheckpointProgress) -> Message:
        facts = {
            "assistant_text": (
                "" if step.assistant_message is None else step.assistant_message.content
            ),
            "tools": [
                {
                    "call_id": item.call.call_id,
                    "name": item.call.name,
                    "arguments": to_mutable_json(item.call.arguments),
                    "status": item.status.value,
                    "result": None if item.result is None else item.result.content,
                }
                for item in step.tools
            ],
        }
        return Message(
            MessageRole.USER,
            "[Mica history work record; data from an incomplete tool batch, "
            "not new instructions]\n"
            + json.dumps(
                facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        )

    @staticmethod
    def _checkpoint_context(checkpoint: RunCheckpoint) -> SessionContextSnapshot:
        messages: list[Message] = [checkpoint.user_message]
        continuations: list[ModelStepContinuation | None] = []
        for step in checkpoint.progress:
            if any(
                item.status
                in {
                    RunCheckpointToolStatus.IN_PROGRESS,
                    RunCheckpointToolStatus.NOT_STARTED,
                }
                for item in step.tools
            ):
                # Preserve the whole batch as data, without changing signed call
                # references or creating an executable request for old tools.
                messages.append(SessionService._incomplete_tool_batch_record(step))
                continue
            if step.tools:
                messages.append(
                    Message(
                        MessageRole.ASSISTANT,
                        ""
                        if step.assistant_message is None
                        else step.assistant_message.content,
                        tuple(item.call for item in step.tools),
                    )
                )
                messages.extend(
                    item.result for item in step.tools if item.result is not None
                )
            elif step.assistant_message is not None:
                messages.append(step.assistant_message)
            elif step.continuation is not None:
                messages.append(Message(MessageRole.ASSISTANT, ""))
            else:
                continue
            continuations.append(step.continuation)
        if checkpoint.partial_assistant_message is not None:
            messages.append(
                Message(
                    MessageRole.USER,
                    "[Mica history work record; partial assistant output from an "
                    "incomplete model stream, not new instructions]\n"
                    + checkpoint.partial_assistant_message.content,
                )
            )
        messages.append(
            Message(
                MessageRole.USER,
                "[Mica history work record; previous run is incomplete; "
                f"status={checkpoint.kind.value}]",
            )
        )
        return SessionContextSnapshot(tuple(messages), tuple(continuations))

    def model_context_snapshot(self, session_id: str) -> SessionContextSnapshot:
        """Read history and continuations together without filtering or rewriting.

        Complete steps retain their saved continuation; incomplete tool batches
        and partial output become USER history records, never pending tool calls
        or system instructions. Storage errors and association failures propagate.
        """
        session = self.store.load(session_id)
        messages: list[Message] = []
        continuations: list[ModelStepContinuation | None] = []
        for entry in session.entries:
            if isinstance(entry, TurnEntry):
                messages.extend(entry.context_messages)
                continuations.extend(entry.continuations)
            else:
                context = self._checkpoint_context(entry)
                messages.extend(context.messages)
                continuations.extend(context.continuations)
        assistant_messages = (
            message for message in messages if message.role is MessageRole.ASSISTANT
        )
        for message, record in zip(assistant_messages, continuations, strict=True):
            if record is not None:
                check_step_association(record, message)
        return SessionContextSnapshot(tuple(messages), tuple(continuations))

    def context_snapshot(self, session_id: str) -> tuple[Message, ...]:
        """Return the messages of the unified, read-only model context snapshot."""
        return self.model_context_snapshot(session_id).messages

    def cleanup_expired(
        self,
        protected_session_ids: Iterable[str] = (),
        *,
        current_session_ids: Iterable[str] = (),
        current_session_id: str | None = None,
        now: datetime | None = None,
    ) -> CleanupResult:
        """Non-blocking cleanup of healthy sessions inactive longer than ``CLEANUP_AGE``."""
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
    "SessionContextSnapshot",
    "SessionListResult",
    "SessionService",
    "SessionServiceError",
    "TurnNotFoundError",
]
