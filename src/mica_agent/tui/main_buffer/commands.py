"""Local command matching and composer candidates, independent of application state."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .renderer import Palette, cell_len, truncate_head
from .sanitize import sanitize_scalar

MAX_CANDIDATES = 5


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    handler: Callable[[], None]


class CommandRegistry:
    def __init__(self, commands: Iterable[Command]) -> None:
        self._commands = tuple(commands)

    def candidates(self, draft: str) -> tuple[Command, ...]:
        if not draft.startswith("/") or "\n" in draft or "\r" in draft:
            return ()
        prefix = draft.casefold()
        return tuple(
            item for item in self._commands if item.name.casefold().startswith(prefix)
        )

    def exact(self, draft: str) -> Command | None:
        name = draft.strip().casefold()
        return next(
            (item for item in self._commands if item.name.casefold() == name), None
        )


class CommandCandidates:
    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry
        self._draft: str | None = None
        self._matches: tuple[Command, ...] = ()
        self._index = 0
        self._dismissed = False

    def sync(self, draft: str) -> None:
        if draft == self._draft:
            return
        self._draft = draft
        self._matches = self._registry.candidates(draft)
        self._index = min(self._index, max(0, len(self._matches) - 1))
        self._dismissed = False

    def move(self, delta: int) -> None:
        if self.visible:
            self._index = min(max(0, self._index + delta), len(self._matches) - 1)

    def dismiss(self) -> None:
        self._dismissed = True

    @property
    def selected(self) -> Command | None:
        return self._matches[self._index] if self.visible else None

    @property
    def visible(self) -> bool:
        return bool(self._matches) and not self._dismissed

    def lines(self, width: int, palette: Palette) -> list[str]:
        if not self.visible:
            return []
        start = min(
            max(0, self._index - MAX_CANDIDATES + 1),
            max(0, len(self._matches) - MAX_CANDIDATES),
        )
        column = max(cell_len(sanitize_scalar(item.name)) for item in self._matches)
        rows = []
        for index in range(start, min(len(self._matches), start + MAX_CANDIDATES)):
            item = self._matches[index]
            name = sanitize_scalar(item.name)
            description = sanitize_scalar(item.description)
            marker = "›" if index == self._index else " "
            text = (
                f"  {marker} {name}{' ' * (column - cell_len(name))}    {description}"
            )
            rows.append(
                palette.paint(
                    truncate_head(text, max(1, width)),
                    "interaction" if index == self._index else "muted",
                )
            )
        return rows
