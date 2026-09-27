"""Sole IO owner for the main-buffer terminal.

This is the only ``main_buffer`` module that touches stdin, stdout, or terminal
capabilities. It owns raw input, terminal size, color capability, and main-buffer
output. It never enters the alternate screen, captures the mouse, clears scrollback,
or interprets UI semantics; renderer, editor, and orchestration output only through it.

Trusted renderer ANSI and untrusted body text separate at the semantic-text boundary in
:mod:`mica_agent.tui.main_buffer.sanitize`, so :meth:`Terminal.write` does not sanitize
again. :func:`find_forbidden_sequences` is the safety backstop: it parses the complete
emitted byte stream instead of relying on a fixed string list.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from prompt_toolkit.input.vt100_parser import Vt100Parser

_BEL = "\x07"
_ESC = "\x1b"
_ST = "\x1b\\"

# Explicitly forbidden effects used as examples by documentation and tests, not the main guard.
FORBIDDEN_SEQUENCES: tuple[str, ...] = (
    "\x1b[?1049h",  # alternate screen on
    "\x1b[?1049l",  # alternate screen off
    "\x1b[?47h",
    "\x1b[?1000h",  # mouse capture
    "\x1b[?1002h",
    "\x1b[?1003h",
    "\x1b[?1005h",
    "\x1b[?1006h",
    "\x1b[?1015h",
    "\x1b[3J",  # clear scrollback
)

# The only allowed private modes control autowrap and cursor visibility.
_ALLOWED_PRIVATE_MODES = frozenset({"7", "25"})
# ED 1/2/3 erase the screen or scrollback; default/0 erases below the cursor and is allowed.
_FORBIDDEN_ERASE_PARAMS = frozenset({"1", "2", "3"})
# OSC operations that modify the terminal window or clipboard.
_FORBIDDEN_OSC_NUMBERS = frozenset({"0", "1", "2", "52"})

# Newline, tab, and carriage return are allowed; CRLF and row return are repaint primitives.
_ALLOWED_CONTROL_CHARS = frozenset({"\n", "\t", "\r"})

_AUTOWRAP_OFF = "\x1b[?7l"
_AUTOWRAP_ON = "\x1b[?7h"

DEFAULT_SIZE = (80, 24)


class KeyInput(Protocol):
    """Subset of prompt_toolkit ``Input`` used by the main-buffer frontend."""

    def read_keys(self) -> list[Any]:
        """Return all currently ready keys without blocking."""

    def flush_keys(self) -> list[Any]:
        """Finish incomplete input sequences and return the keys they produce."""

    def attach(self, callback: Callable[[], None]) -> AbstractContextManager[None]:
        """Register an input-ready callback with the event loop."""

    def raw_mode(self) -> AbstractContextManager[None]:
        """Enter raw mode."""


def default_input_factory() -> KeyInput:
    """Build the platform input only when entering raw mode.

    A Windows console in VT input mode can yield navigation records whose ``uChar`` is
    ``\\x00``; the locked reader drops them, so this instance receives a
    ``VirtualKeyCode`` fallback. Other platforms, non-VT mode, and piped input retain
    locked-reader behavior.
    """
    from prompt_toolkit.input import create_input

    acquired = create_input()
    install_windows_navigation_fallback(acquired)
    return acquired


# Win32 console record type 1 is KEY_EVENT.
KEY_EVENT_TYPE = 0x0001

# Supported navigation VirtualKeyCodes and equivalent VT sequences. Matching the
# locked prompt_toolkit ANSI table makes fallback and standard paths produce equal keys.
NAVIGATION_VIRTUAL_KEYS: dict[int, str] = {
    0x26: "\x1b[A",  # VK_UP
    0x28: "\x1b[B",  # VK_DOWN
    0x25: "\x1b[D",  # VK_LEFT
    0x27: "\x1b[C",  # VK_RIGHT
    0x24: "\x1b[H",  # VK_HOME
    0x23: "\x1b[F",  # VK_END
    0x2E: "\x1b[3~",  # VK_DELETE
}


def windows_key_event(record: Any) -> Any | None:
    """Extract a key-down event, returning ``None`` for other or unknown records.

    Attribute-only access through ``EventType`` and ``Event.KeyEvent`` avoids importing
    Win32 types and lets tests construct equivalent stand-ins.
    """
    if getattr(record, "EventType", None) != KEY_EVENT_TYPE:
        return None
    event = getattr(getattr(record, "Event", None), "KeyEvent", None)
    if event is None or not getattr(event, "KeyDown", False):
        return None
    return event


def windows_record_sequences(records: Iterable[Any]) -> list[str]:
    """Interpret Windows console records as ordered parser input sequences.

    A non-null ``uChar`` follows the standard VT path unchanged. A null ``uChar`` with a
    supported navigation ``VirtualKeyCode`` yields its equivalent VT sequence for the
    same ``Vt100Parser``. Key-up, mouse, menu, focus, unknown, and unsupported records
    yield nothing. If a batch already contains a character record for a virtual key,
    fallback is suppressed so one physical key cannot be emitted twice.
    """
    events = [event for event in map(windows_key_event, records) if event is not None]
    typed = {
        event.VirtualKeyCode for event in events if event.uChar.UnicodeChar != "\x00"
    }
    sequences: list[str] = []
    for event in events:
        character = event.uChar.UnicodeChar
        if character != "\x00":
            sequences.append(character)
            continue
        sequence = NAVIGATION_VIRTUAL_KEYS.get(event.VirtualKeyCode)
        if sequence is not None and event.VirtualKeyCode not in typed:
            sequences.append(sequence)
    return sequences


class WindowsVtKeyReader:
    """Pure Windows console-record interpreter with no platform IO.

    A locked ``Vt100Parser`` receives both standard characters and navigation fallback
    as the same input sequences, making results equal to real VT input. A lone ``Esc``
    becomes a key only when :meth:`flush` finishes the incomplete sequence.
    """

    def __init__(self) -> None:
        self._buffer: list[Any] = []
        self._parser = Vt100Parser(lambda key_press: self._buffer.append(key_press))

    def feed(self, text: str) -> None:
        """Feed text already known to be VT input."""
        self._parser.feed(text)

    def feed_records(self, records: Iterable[Any]) -> None:
        """Feed console records through input sequences into the locked parser."""
        for sequence in windows_record_sequences(records):
            self._parser.feed(sequence)

    def flush(self) -> list[Any]:
        """Finish incomplete sequences and return produced keys, including lone ``Esc``."""
        self._parser.flush()
        return self.take()

    def take(self) -> list[Any]:
        """Take and clear all currently parsed keys."""
        keys, self._buffer = self._buffer, []
        return keys


def is_windows_vt_reader(reader: Any) -> bool:
    """Return whether an object accepts the Windows navigation fallback.

    Shape-checking ``read``, ``flush_keys``, ``handle``, and ``_vt100_parser`` avoids any
    Win32 type import and supports equivalent test stand-ins.
    """
    if not callable(getattr(reader, "read", None)):
        return False
    if not callable(getattr(reader, "flush_keys", None)):
        return False
    if not hasattr(reader, "handle"):
        return False
    return hasattr(reader, "_vt100_parser")


def _hybrid_reader_type(base_type: type) -> type:
    """Build a reader type combining standard VT characters and null-char navigation.

    Locked prompt_toolkit retains platform IO, handles, event waits, and
    ``ReadConsoleInputW``. This layer owns only record-to-parser-input interpretation and
    reuses locked parser semantics.
    """

    class WindowsVtInputReader(base_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self._mica_keys = WindowsVtKeyReader()

        def _get_keys(self, count: Any, input_records: Any) -> tuple[()]:
            """Send this record batch to Mica's interpreter instead of the locked parser."""
            self._mica_keys.feed_records(
                input_records[index] for index in range(count.value)
            )
            return ()

        def read(self) -> list[Any]:
            """Use locked platform IO and return keys interpreted by Mica."""
            super().read()
            return self._mica_keys.take()

        def flush_keys(self) -> list[Any]:
            """Finish incomplete sequences; a lone ``Esc`` becomes a key only here."""
            return self._mica_keys.flush()

    return WindowsVtInputReader


def windows_navigation_reader(reader: Any) -> Any:
    """Return a new reader with null-character navigation fallback.

    Extending ``type(reader)`` avoids Win32 imports and does not duplicate console
    handles or record polling.
    """
    return _hybrid_reader_type(type(reader))()


def install_windows_navigation_fallback(inner: Any) -> bool:
    """Install navigation fallback on one Windows VT input and report success.

    The replaced reader provides every Windows-specific capability, so this module
    imports no Win32 type. Replacement affects only this input instance, modifies no
    prompt_toolkit source or process-global state, and leaves the locked reader intact
    on failure.
    """
    if sys.platform != "win32":
        return False
    current = getattr(inner, "console_input_reader", None)
    if not is_windows_vt_reader(current):
        return False
    try:
        replacement = windows_navigation_reader(current)
    except Exception:  # noqa: BLE001 - installation failure preserves the locked reader
        return False
    close = getattr(current, "close", None)
    if callable(close):
        # Failure to close the old handle cannot invalidate a replacement with its own handle.
        with suppress(Exception):
            close()
    inner.console_input_reader = replacement
    return True


def find_forbidden_sequences(text: str) -> list[str]:
    """Scan the emitted output stream for unacceptable terminal effects.

    This main-buffer safety guard parses terminal sequence syntax rather than matching a
    fixed string list, catching unknown private modes, full-screen erasure, title
    changes, and clipboard operations.
    """
    findings: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == _ESC and index + 1 < length:
            index = _inspect_introducer(text, index + 1, findings)
            continue
        if ord(character) in {0x9B, 0x9D}:
            cursor = index + 1
            if ord(character) == 0x9B:
                index = _inspect_csi(text, cursor, findings)
            else:
                index = _inspect_osc(text, cursor, findings)
            continue
        if character == _BEL:
            findings.append("BEL")
            index += 1
            continue
        if ord(character) < 0x20 and character not in {"\n", "\t", "\r"}:
            findings.append(f"C0 0x{ord(character):02x}")
            index += 1
            continue
        if ord(character) == 0x7F:
            findings.append("DEL")
            index += 1
            continue
        index += 1
    return findings


def _inspect_introducer(text: str, index: int, findings: list[str]) -> int:
    """Inspect a sequence after ``ESC``/C1 and return the next scan index."""
    introducer = text[index]
    if introducer == "[":
        return _inspect_csi(text, index + 1, findings)
    if introducer == "]":
        return _inspect_osc(text, index + 1, findings)
    if introducer in {"P", "X", "^", "_"}:
        findings.append(f"ESC {introducer} 字符串控制")
        return _skip_to_string_end(text, index + 1)
    if introducer == "c":  # full RIS reset
        findings.append("ESC c")
        return index + 1
    return index + 1


def _inspect_csi(text: str, index: int, findings: list[str]) -> int:
    points = _split_sequence(text, index, 0x40)
    params = text[index : points.param_end]
    final = text[points.final] if points.final < len(text) else ""
    if params.startswith("?"):
        modes = [item for item in params[1:].split(";") if item]
        offenders = [item for item in modes if item not in _ALLOWED_PRIVATE_MODES]
        if offenders or not modes:
            findings.append(f"CSI {params}{final}")
    elif final == "J" and params in _FORBIDDEN_ERASE_PARAMS:
        findings.append(f"CSI {params}J")
    return points.next_index


def _inspect_osc(text: str, index: int, findings: list[str]) -> int:
    end = _skip_to_string_end(text, index)
    cursor = index
    while cursor < len(text) and text[cursor].isdigit():
        cursor += 1
    number = text[index:cursor]
    if number in _FORBIDDEN_OSC_NUMBERS:
        findings.append(f"OSC {number}")
    return end


@dataclass(frozen=True, slots=True)
class _Sequence:
    """CSI parameter extent, final-byte index, and next scan position."""

    param_end: int
    final: int
    next_index: int


def _split_sequence(text: str, index: int, final_low: int) -> _Sequence:
    length = len(text)
    cursor = index
    while cursor < length:
        code = ord(text[cursor])
        if final_low <= code <= 0x7E:
            return _Sequence(cursor, cursor, cursor + 1)
        if not (0x20 <= code <= 0x3F):
            return _Sequence(cursor, cursor, cursor)
        cursor += 1
    return _Sequence(cursor, cursor, cursor)


def _skip_to_string_end(text: str, index: int) -> int:
    length = len(text)
    while index < length:
        character = text[index]
        if character == _BEL or character == "\x9c":
            return index + 1
        if character == _ESC and index + 1 < length and text[index + 1] == "\\":
            return index + 2
        index += 1
    return length


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Read-only snapshot of current terminal capabilities."""

    ansi: bool
    interactive: bool
    width: int
    height: int


class Terminal:
    """Sole IO owner for the main buffer.

    No other ``main_buffer`` module may write stdout or read raw input. ``emitted``
    records all actual output for tests and visual evidence.
    """

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        input_factory: Callable[[], KeyInput] | None = None,
        environ: dict[str, str] | None = None,
        size: tuple[int, int] | None = None,
        interactive: bool | None = None,
    ) -> None:
        self._output: TextIO = sys.stdout if output is None else output
        self._input_factory = (
            default_input_factory if input_factory is None else input_factory
        )
        self._environ = dict(os.environ) if environ is None else environ
        self._forced_size = size
        self._forced_interactive = interactive
        self._input: KeyInput | None = None
        self._raw: AbstractContextManager[None] | None = None
        self._attached: AbstractContextManager[None] | None = None
        self._autowrap_disabled = False
        self.emitted: list[str] = []
        self.cleanup_errors: list[str] = []

    def capabilities(self) -> Capabilities:
        width, height = self.size()
        return Capabilities(
            ansi=self.ansi_enabled(),
            interactive=self.interactive_enabled(),
            width=width,
            height=height,
        )

    def size(self) -> tuple[int, int]:
        if self._forced_size is not None:
            return self._forced_size
        try:
            columns, lines = shutil.get_terminal_size(DEFAULT_SIZE)
        except (OSError, ValueError):
            return DEFAULT_SIZE
        return max(1, columns), max(1, lines)

    def set_size(self, size: tuple[int, int] | None) -> None:
        """Inject terminal size, or use ``None`` to resume real size queries."""
        self._forced_size = size

    def interactive_enabled(self) -> bool:
        """Whether output is an interactive terminal that supports cursor addressing."""
        if self._forced_interactive is not None:
            return self._forced_interactive
        return bool(getattr(self._output, "isatty", lambda: False)())

    def ansi_enabled(self) -> bool:
        """Whether to use color; NO_COLOR disables color without disabling interaction."""
        return self.interactive_enabled() and not (
            self._environ.get("NO_COLOR")
            or self._environ.get("TERM", "").strip().lower() == "dumb"
        )

    def write(self, text: str) -> None:
        """Emit text whose newline handling is already complete.

        This is the trusted-control entry point: renderer-generated cursor and erase
        sequences must pass through unchanged, so no sanitization occurs here. Untrusted
        body text is sanitized before entering a semantic block.
        """
        if not text:
            return
        self.emitted.append(text)
        self._output.write(text)
        self._output.flush()

    def write_line(self, text: str) -> None:
        """Emit one row followed by a newline."""
        self.write(text + "\r\n")

    def emitted_text(self) -> str:
        """Return the complete emitted byte stream for tests and diagnostics."""
        return "".join(self.emitted)

    def forbidden_findings(self) -> list[str]:
        """Safety-scan the complete emitted byte stream."""
        return find_forbidden_sequences(self.emitted_text())

    @property
    def input(self) -> KeyInput:
        if self._input is None:
            raise RuntimeError("terminal input is not open")
        return self._input

    @property
    def is_open(self) -> bool:
        return self._input is not None

    def open_input(self) -> None:
        """Enter raw mode and disable autowrap; repeated calls are safe.

        Failure at any step releases acquired resources before propagating, so callers
        need only one :meth:`close_input` call in ``finally``.
        """
        if self._input is not None:
            return
        acquired = self._input_factory()
        self._input = acquired
        try:
            self._raw = acquired.raw_mode()
            self._raw.__enter__()
        except BaseException:
            self._raw = None
            self._close_input_object()
            raise
        try:
            if self.interactive_enabled():
                self.write(_AUTOWRAP_OFF)
                self._autowrap_disabled = True
        except BaseException:
            self.close_input()
            raise

    def attach_input(self, callback: Callable[[], None]) -> None:
        """Attach an input-ready callback that :meth:`close_input` will detach."""
        if self._attached is not None:
            return
        self._attached = self.input.attach(callback)
        self._attached.__enter__()

    def drain_keys(self) -> list[Any]:
        """Consume all currently ready keys."""
        return self.input.read_keys()

    def flush_keys(self) -> list[Any]:
        """Finish incomplete input sequences and return the resulting keys.

        A lone ``Esc`` becomes a key only when this method runs after a bounded delay,
        so callers must consume the result rather than treat it as a discarded buffer.
        Symmetrically with :meth:`drain_keys`, no incomplete sequence returns an empty
        list.
        """
        return self.input.flush_keys()

    def close_input(self) -> None:
        """Restore terminal state and release input; repeated calls are safe.

        Cleanup steps are isolated: one failure is recorded in :attr:`cleanup_errors`
        without blocking other required cleanup or surfacing to the user. Exceptions
        outside ordinary ``Exception``, such as ``CancelledError``, are not swallowed.
        """
        if self._input is None and self._attached is None:
            return
        attached, self._attached = self._attached, None
        if attached is not None:
            self._attempt("detach input", lambda: attached.__exit__(None, None, None))
        if self._autowrap_disabled:
            self._autowrap_disabled = False
            if self.interactive_enabled():
                self._attempt("restore autowrap", lambda: self.write(_AUTOWRAP_ON))
        if self._raw is not None:
            raw, self._raw = self._raw, None
            self._attempt("leave raw mode", lambda: raw.__exit__(None, None, None))
        self._close_input_object()

    def _close_input_object(self) -> None:
        acquired, self._input = self._input, None
        if acquired is None:
            return
        close = getattr(acquired, "close", None)
        if callable(close):
            self._attempt("close input", close)

    def _attempt(self, label: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as error:  # noqa: BLE001 - record cleanup failure without raising
            self.cleanup_errors.append(f"{label}: {type(error).__name__}")


__all__ = [
    "FORBIDDEN_SEQUENCES",
    "KEY_EVENT_TYPE",
    "NAVIGATION_VIRTUAL_KEYS",
    "Capabilities",
    "KeyInput",
    "Terminal",
    "WindowsVtKeyReader",
    "default_input_factory",
    "find_forbidden_sequences",
    "install_windows_navigation_fallback",
    "is_windows_vt_reader",
    "windows_key_event",
    "windows_navigation_reader",
    "windows_record_sequences",
]
