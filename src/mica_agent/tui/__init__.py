"""Explicit import boundary for the Mica terminal frontend.

The package has no Textual dependency and exports only shared view models and the
main-buffer orchestration entry point. The entry point is loaded lazily so importing a
shared snapshot does not assemble the application runtime.
"""

from .models import ReasoningDisplayMode, StartupResourceSnapshot, TuiSessionSnapshot

__all__ = [
    "MainBufferScreen",
    "ReasoningDisplayMode",
    "StartupResourceSnapshot",
    "TuiSessionSnapshot",
]


def __getattr__(name: str):
    if name == "MainBufferScreen":
        from .main_buffer.screen import MainBufferScreen

        return MainBufferScreen
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
