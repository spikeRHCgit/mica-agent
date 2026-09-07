"""固定底部控制区的只读运行事实。"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

from .models import TuiSessionSnapshot
from .theme import BRAND_LIGHT, MUTED


class TuiFactsView(Static):
    """显示 CLI 注入的当前工作区、模型和会话标签。"""

    DEFAULT_CSS = """
    TuiFactsView {
        width: 1fr;
        height: 2;
        padding: 0 1;
        color: $foreground;
        background: transparent;
    }
    """

    def __init__(
        self,
        snapshot: TuiSessionSnapshot,
        *,
        id: str | None = None,
    ) -> None:
        if not isinstance(snapshot, TuiSessionSnapshot):
            raise TypeError("snapshot must be a TuiSessionSnapshot")
        self.snapshot = snapshot
        super().__init__(id=id)

    def on_mount(self) -> None:
        self.styles.height = 2

    def on_resize(self, event: object) -> None:
        self.styles.height = 2

    def render(self) -> Text:
        width = max(1, self.content_region.width or self.size.width)
        workspace = self.snapshot.workspace or "none"
        model = self.snapshot.model or "none"
        session = self.snapshot.session_label or "none"

        model_width = min(28, max(6, min(cell_len(model), width // 2)))
        workspace_width = max(5, width - model_width - 1)
        if workspace_width + model_width + 1 > width:
            workspace_width = max(1, width - model_width - 1)
        workspace_text = self._truncate_tail(workspace, workspace_width)
        model_text = self._truncate_tail(model, min(model_width, width))
        first_row = Text()
        first_row.append(workspace_text, style=BRAND_LIGHT)
        gap = max(1, width - cell_len(workspace_text) - cell_len(model_text))
        first_row.append(" " * gap)
        first_row.append(model_text, style=BRAND_LIGHT)

        session_prefix = "sessions: "
        session_width = max(1, width - cell_len(session_prefix))
        output = Text()
        output.append(first_row)
        output.append("\n" + session_prefix, style=MUTED)
        output.append(self._truncate_tail(session, session_width), style=BRAND_LIGHT)
        return output

    @staticmethod
    def _truncate_tail(value: str, limit: int) -> str:
        if limit <= 0:
            return ""
        if cell_len(value) <= limit:
            return value
        if limit == 1:
            return "…"
        remaining = limit - 1
        suffix: list[str] = []
        used = 0
        for character in reversed(value):
            character_width = cell_len(character)
            if used + character_width > remaining:
                break
            suffix.append(character)
            used += character_width
        return "…" + "".join(reversed(suffix))


__all__ = ["TuiFactsView"]
