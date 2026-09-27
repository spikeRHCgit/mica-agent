"""Sole sanitization seam between untrusted text and trusted ANSI.

All main-buffer text -- user input, model output, visible tool fields, interaction copy,
and dynamic facts -- is untrusted: it may appear only as characters and must never
change terminal state. Only renderer-generated styling, cursor, and erase sequences
are trusted controls.

Sanitization removes complete control content rather than echoing dangerous bytes:

* CSI, OSC, DCS, and two-character escapes beginning with ``ESC`` are removed whole;
* C1 ``CSI`` (0x9B) and ``OSC`` (0x9D) sequences are removed likewise;
* ``BEL``, ``BS``, ``DEL``, and all other C0/C1 controls are removed;
* ``CRLF`` and lone ``CR`` are normalized to ``LF``;
* ordinary Unicode, including CJK, emoji, combining characters, and ZWJ sequences, is
  preserved.

The sanitizer only removes content and normalizes newlines. It never inserts ``ESC``
or another control, so its output cannot form a new valid control sequence.
"""

from __future__ import annotations

# Controls that cannot begin recognized sequences but still must not enter body text.
_DEL = 0x7F
_C1_START = 0x80
_C1_END = 0x9F
_BEL = "\x07"
_ESC = "\x1b"
_CR = "\r"
_LF = "\n"
_TAB = "\t"

_CSI_ENTRY = "["
_OSC_ENTRY = "]"
_STRING_ENTRIES = frozenset({"P", "X", "^", "_"})
_C1_ST = "\x9c"


def sanitize_text(value: str, *, newlines: bool = True, tabs: bool = True) -> str:
    """Remove terminal control content from untrusted text.

    ``newlines=False`` flattens newlines to spaces for single-line fields such as tool
    names, fact lines, and panel labels; ``tabs=False`` does the same for tabs.
    """
    if not isinstance(value, str):
        return ""
    output: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        code = ord(character)
        if character == _ESC:
            index = _skip_escape(value, index)
            continue
        if code == 0x9B:  # C1 CSI
            index = _skip_csi(value, index + 1)
            continue
        if code == 0x9D:  # C1 OSC
            index = _skip_osc(value, index + 1)
            continue
        if character == _CR:
            if newlines:
                if index + 1 < length and value[index + 1] == _LF:
                    index += 1
                output.append(_LF)
            index += 1
            continue
        if character == _LF:
            output.append(_LF if newlines else " ")
            index += 1
            continue
        if character == _TAB:
            output.append(_TAB if tabs else " ")
            index += 1
            continue
        if code == _DEL or code < 0x20 or _C1_START <= code <= _C1_END:
            index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def sanitize_scalar(value: object, *, fallback: str = "none") -> str:
    """Sanitize and flatten a single-line field; use a fixed fallback when empty."""
    text = sanitize_text(str(value), newlines=False, tabs=False).strip()
    return text or fallback


def _skip_escape(value: str, index: int) -> int:
    """Skip one complete escape sequence from ``ESC`` and return the next index."""
    cursor = index + 1
    if cursor >= len(value):
        return cursor
    introducer = value[cursor]
    if introducer == _CSI_ENTRY:
        return _skip_csi(value, cursor + 1)
    if introducer == _OSC_ENTRY:
        return _skip_osc(value, cursor + 1)
    if introducer in _STRING_ENTRIES:
        return _skip_string(value, cursor + 1)
    return cursor + 1


def _skip_csi(value: str, index: int) -> int:
    """Skip CSI parameters, intermediate bytes, and the final byte."""
    length = len(value)
    while index < length:
        code = ord(value[index])
        index += 1
        if 0x40 <= code <= 0x7E:
            return index
        if not (0x20 <= code <= 0x3F):
            return index
    return length


def _skip_osc(value: str, index: int) -> int:
    """Skip OSC content through BEL, ST/C1-ST, or the end of the string."""
    length = len(value)
    while index < length:
        character = value[index]
        if character == _BEL or character == _C1_ST:
            return index + 1
        if character == _ESC and index + 1 < length:
            if value[index + 1] == "\\":
                return index + 2
            return index + 1
        index += 1
    return length


def _skip_string(value: str, index: int) -> int:
    """Skip DCS/SOS/PM/APC content through ST/C1-ST or the end of the string."""
    length = len(value)
    while index < length:
        character = value[index]
        if character == _C1_ST:
            return index + 1
        if character == _ESC and index + 1 < length and value[index + 1] == "\\":
            return index + 2
        index += 1
    return length


__all__ = ["sanitize_scalar", "sanitize_text"]
