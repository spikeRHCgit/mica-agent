"""主缓冲区专用工具视图测试。

覆盖六类视图的安全渲染规则、宽状态色块、NO_COLOR 状态符号、Bash 动态输出
边界、write/edit 折叠规则，以及扩展工厂的安全性。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import pytest

from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui.main_buffer.renderer import (
    MainBufferRenderer,
    Palette,
    strip_ansi,
)
from mica_agent.tui.main_buffer.terminal import find_forbidden_sequences
from mica_agent.tui.main_buffer.tool_views import (
    BASH_OUTPUT_LINES,
    STATE_SYMBOLS,
    GenericToolView,
    ToolView,
    ToolViewFactory,
    WriteToolView,
)

SNAPSHOT_STATUS = {
    "pending": ToolPresentationStatus.PENDING,
    "success": ToolPresentationStatus.SUCCESS,
    "failed": ToolPresentationStatus.FAILED,
    "cancelled": ToolPresentationStatus.CANCELLED,
}


def snapshot(
    kind: ToolPresentationKind,
    name: str,
    payload: Mapping[str, object],
    *,
    status: ToolPresentationStatus = ToolPresentationStatus.SUCCESS,
    message: str | None = None,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-1", name, kind, status, dict(payload), message
    )


def visible(rows: list[str]) -> list[str]:
    return [strip_ansi(row) for row in rows]


def test_every_state_uses_a_full_width_background_block() -> None:
    view = ToolViewFactory().create(
        snapshot(ToolPresentationKind.READ, "read", {"path": "docs/a.md"}),
        state="success",
    )
    assert view is not None
    rows = view.render(40, Palette(True), False)

    assert len(rows) == 1
    assert "48;2;20;83;45" in rows[0]
    assert len(strip_ansi(rows[0])) == 40

    pending = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.READ,
            "read",
            {"path": "docs/a.md"},
            status=ToolPresentationStatus.PENDING,
        ),
        state="pending",
    )
    assert pending is not None
    assert "48;2;30;58;138" in pending.render(40, Palette(True), False)[0]

    failed = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.READ,
            "read",
            {"path": "docs/a.md"},
            status=ToolPresentationStatus.FAILED,
            message="tool execution failed",
        ),
        state="failed",
    )
    assert failed is not None
    assert "48;2;127;29;29" in failed.render(40, Palette(True), False)[0]


def test_no_color_uses_symbols_and_safe_reasons() -> None:
    for state, symbol in STATE_SYMBOLS.items():
        status = SNAPSHOT_STATUS.get(state, ToolPresentationStatus.FAILED)
        view = ToolViewFactory().create(
            snapshot(
                ToolPresentationKind.READ,
                "read",
                {"path": "docs/a.md"},
                status=status,
                message="tool execution failed"
                if state in {"failed", "cancelled"}
                else None,
            ),
            state=state,
        )
        assert view is not None
        rows = visible(view.render(40, Palette(False), False))
        assert rows[0].startswith(f"{symbol} read")
        assert "\x1b[" not in "".join(rows)


def test_read_shows_only_name_and_relative_path() -> None:
    view = ToolViewFactory().create(
        snapshot(ToolPresentationKind.READ, "read", {"path": "src/mica_agent/app.py"}),
        state="success",
    )
    assert view is not None
    rows = visible(view.render(60, Palette(False), False))

    assert rows == ["✓ read  src/mica_agent/app.py"]
    assert "call-1" not in rows[0]
    assert "{" not in rows[0]


def test_write_folds_after_ten_logical_lines_and_keeps_plain_text() -> None:
    content = "\n".join(f"line {index}" for index in range(14))
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.WRITE,
            "write",
            {"path": "docs/a.md", "content": content},
        ),
        state="success",
    )
    assert isinstance(view, WriteToolView)
    rows = visible(view.render(60, Palette(False), False))

    assert rows[0] == "✓ write  docs/a.md"
    assert "line 9" in rows and "line 10" not in rows
    assert rows[-1] == "... (4 more lines, 14 total, ctrl+o to expand)"
    expanded = visible(view.render(60, Palette(False), True))
    assert "line 13" in expanded
    assert expanded[-1] == "... (14 total, ctrl+o to collapse)"
    # 窄终端下提示退化为仍可读的短形式，不丢关键信息。
    narrow = visible(view.render(30, Palette(False), False))
    assert narrow[-1].startswith("... (4 more lines")
    # 正文保持纯文本：Markdown 标记原样显示，不做渲染。
    marked = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.WRITE,
            "write",
            {"path": "a.md", "content": "# 标题 **不是粗体**"},
        ),
        state="success",
    )
    assert marked is not None
    assert "**不是粗体**" in "\n".join(
        visible(marked.render(40, Palette(False), False))
    )


def test_edit_uses_real_line_numbers_and_folds_after_twenty_changes() -> None:
    lines = [
        {
            "kind": "context",
            "text": f"ctx {index}",
            "old_line": index,
            "new_line": index,
        }
        for index in range(1, 4)
    ]
    for index in range(1, 26):
        lines.append(
            {
                "kind": "removed",
                "text": f"old {index}",
                "old_line": index,
                "new_line": None,
            }
        )
        lines.append(
            {
                "kind": "added",
                "text": f"new {index}",
                "old_line": None,
                "new_line": index,
            }
        )
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.EDIT,
            "edit",
            {"path": "x.py", "matched": True, "lines": tuple(lines)},
        ),
        state="success",
    )
    assert view is not None
    folded = visible(view.render(60, Palette(False), False))
    expanded = visible(view.render(60, Palette(False), True))
    text = "\n".join(folded)

    assert folded[0] == "✓ edit  x.py"
    assert "-  1  old 1" in text and "+  1  new 1" in text
    # 保留前十条与后十条变化行，中间的变化行折叠并给出准确计数。
    assert "old 3" in text and "old 5" in text and "old 21" in text and "old 25" in text
    assert "old 16" not in text
    assert "... (30 change lines hidden, ctrl+o to expand)" in text
    assert "old 12" in "\n".join(expanded)
    assert expanded[-1] == "... (50 change lines, ctrl+o to collapse)"


def test_edit_without_match_never_fabricates_line_numbers() -> None:
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.EDIT,
            "edit",
            {
                "path": "x.py",
                "matched": False,
                "lines": (
                    {
                        "kind": "removed",
                        "text": "old",
                        "old_line": None,
                        "new_line": None,
                    },
                    {
                        "kind": "added",
                        "text": "new",
                        "old_line": None,
                        "new_line": None,
                    },
                ),
            },
            status=ToolPresentationStatus.FAILED,
            message="file was not changed",
        ),
        state="failed",
    )
    assert view is not None
    rows = visible(view.render(40, Palette(False), False))
    text = "\n".join(rows)

    assert "- old" in text and "+ new" in text
    assert "  1  " not in text
    assert rows[-1] == "file was not changed"


def test_bash_active_output_is_bounded_and_terminal_uses_snapshot() -> None:
    payload = {"command": "uv run pytest -q", "output": "", "exit_code": None}
    view = ToolViewFactory().create(
        snapshot(ToolPresentationKind.BASH, "bash", payload),
        state="running",
        live_output="\n".join(f"out {index}" for index in range(4)),
        live_total=12,
    )
    assert view is not None
    active = visible(view.render(40, Palette(False), False))
    active_text = "\n".join(active)

    assert "$ uv run pytest -q" in active_text
    assert "out 0" in active_text and "out 3" in active_text
    assert "(8 earlier lines)" in active_text
    assert "ctrl+o" not in active_text
    # 活动工具即使全局展开也保持有界。
    assert "out 0" in "\n".join(visible(view.render(40, Palette(False), True)))

    finished = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.BASH,
            "bash",
            {
                "command": "uv run pytest -q",
                "output": "\n".join(f"line {index}" for index in range(20)),
                "exit_code": 1,
            },
        ),
        state="success",
    )
    assert finished is not None
    rows = visible(finished.render(40, Palette(False), False))
    text = "\n".join(rows)

    assert f"line {20 - BASH_OUTPUT_LINES}" in text and "line 0" not in text
    assert "(12 earlier lines, ctrl+o to expand)" in text
    assert rows[-1] == "exit 1"
    assert "line 0" in "\n".join(visible(finished.render(40, Palette(False), True)))

    empty = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.BASH,
            "bash",
            {"command": "true", "output": "", "exit_code": 0},
        ),
        state="success",
    )
    assert empty is not None
    assert "(no output)" in "\n".join(visible(empty.render(40, Palette(False), False)))


def test_bash_command_is_folded_to_three_display_rows() -> None:
    long_command = "echo " + " ".join(f"arg{index}" for index in range(30))
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.BASH,
            "bash",
            {"command": long_command, "output": "", "exit_code": 0},
        ),
        state="success",
    )
    assert view is not None
    rows = visible(view.render(40, Palette(False), False))

    rows = visible(view.render(30, Palette(False), False))

    assert any("more display lines" in row for row in rows)
    expanded = visible(view.render(60, Palette(False), True))
    assert "arg29" in "\n".join(expanded)


def test_failed_bash_shows_fixed_reason_without_exit_code() -> None:
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.BASH,
            "bash",
            {"command": "never", "output": "partial", "exit_code": None},
            status=ToolPresentationStatus.CANCELLED,
            message="operation cancelled",
        ),
        state="cancelled",
    )
    assert view is not None
    rows = visible(view.render(40, Palette(False), False))
    text = "\n".join(rows)

    assert "exit " not in text
    assert rows[-1] == "operation cancelled"
    assert "partial" in text


def test_ask_view_shows_question_and_answer_and_never_falls_back() -> None:
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.ASK,
            "ask",
            {"question": "继续吗？", "answer": "继续"},
        ),
        state="success",
    )
    assert view is not None
    rows = visible(view.render(40, Palette(False), False))

    assert "ask  继续吗？" in rows[0]
    assert "→ 继续" in rows[1]
    # Ask 保持专用视图，不退回通用兜底。
    assert not isinstance(view, GenericToolView)


def test_generic_view_only_shows_name_and_reason() -> None:
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.GENERIC,
            "custom_tool",
            {},
            status=ToolPresentationStatus.FAILED,
            message="tool execution failed",
        ),
        state="failed",
    )
    assert view is not None
    rows = visible(view.render(40, Palette(False), False))

    assert rows == ["× custom_tool", "tool execution failed"]
    assert not view.can_expand


def test_factory_copies_overrides_and_keeps_builtin_names() -> None:
    def builder(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(snapshot_obj, state=state)

    overrides = {"custom_tool": builder, "read": builder}
    factory = ToolViewFactory(overrides)
    overrides.clear()  # 构造后修改原映射不应影响工厂

    assert factory.overrides == ("custom_tool",)
    builtin = factory.create(
        snapshot(ToolPresentationKind.READ, "read", {"path": "a.md"}), state="success"
    )
    assert builtin is not None
    assert not isinstance(builtin, GenericToolView)


def test_factory_falls_back_safely_on_bad_builders() -> None:
    def raising(snapshot_obj: object, state: str) -> ToolView:
        raise RuntimeError("secret-token-abcdef")

    def wrong_type(snapshot_obj: object, state: str) -> ToolView:
        return object()  # type: ignore[return-value]

    def other_snapshot(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(
            snapshot(ToolPresentationKind.GENERIC, "x", {}), state=state
        )

    factories = []
    for builder in (raising, wrong_type, other_snapshot):
        factory = ToolViewFactory({"custom_tool": builder})
        factories.append(factory)
        view = factory.create(
            snapshot(ToolPresentationKind.GENERIC, "custom_tool", {}), state="success"
        )
        assert isinstance(view, GenericToolView)
        rows = visible(view.render(40, Palette(False), False))
        assert "secret-token-abcdef" not in "\n".join(rows)
        assert rows == ["✓ custom_tool"]
        assert factory.fallbacks == 1


def test_factory_rejects_builtin_name_with_wrong_kind() -> None:
    factory = ToolViewFactory()
    mismatched = snapshot(
        ToolPresentationKind.BASH,
        "read",
        {"command": "ls", "output": "", "exit_code": 0},
    )
    assert factory.create(mismatched, state="success") is None


# 工厂的完整快照值比较


@dataclass(frozen=True)
class _Snapshot:
    """可精确控制单个字段差异的测试用快照替身。"""

    call_id: str = "call-1"
    tool_name: str = "custom_tool"
    kind: object = ToolPresentationKind.GENERIC
    status: object = ToolPresentationStatus.PENDING
    payload: Mapping[str, object] = field(default_factory=dict)
    message: str | None = None


class _ExplodingSnapshot:
    """字段相同但比较时抛普通异常的替身（用于验证比较边界）。"""

    call_id = "call-1"
    tool_name = "custom_tool"
    kind = ToolPresentationKind.GENERIC
    status = ToolPresentationStatus.PENDING
    payload: Mapping[str, object] = {}
    message = None

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("secret-token-abcdef")

    __hash__ = None  # type: ignore[assignment]


def test_factory_accepts_an_equal_snapshot_copy() -> None:
    original = snapshot(ToolPresentationKind.GENERIC, "custom_tool", {})
    copy = snapshot(ToolPresentationKind.GENERIC, "custom_tool", {})
    assert copy is not original
    assert copy == original

    def builder(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(copy, state=state)

    factory = ToolViewFactory({"custom_tool": builder})
    view = factory.create(original, state="success")

    assert isinstance(view, GenericToolView)
    assert view.snapshot is copy  # 扩展视图保留自己的等值快照
    assert factory.fallbacks == 0
    assert visible(view.render(40, Palette(False), False)) == ["✓ custom_tool"]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("call_id", "call-2"),
        ("tool_name", "other_tool"),
        ("kind", ToolPresentationKind.READ),
        ("status", ToolPresentationStatus.SUCCESS),
        ("payload", {"path": "/b.txt"}),
        ("message", "tool execution failed"),
    ],
)
def test_factory_falls_back_when_any_snapshot_field_differs(
    field_name: str, value: object
) -> None:
    base = _Snapshot()
    variant = replace(base, **{field_name: value})
    assert variant != base

    def builder(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(variant, state=state)

    factory = ToolViewFactory({"custom_tool": builder})
    view = factory.create(base, state="success")

    assert isinstance(view, GenericToolView)
    # 回退视图必须绑定调用方的快照，而不是扩展返回的那一份。
    assert view.snapshot is base
    assert factory.fallbacks == 1


def test_factory_snapshot_comparison_failure_falls_back_safely() -> None:
    def builder(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(_ExplodingSnapshot(), state=state)

    factory = ToolViewFactory({"custom_tool": builder})
    view = factory.create(_Snapshot(), state="success")

    assert isinstance(view, GenericToolView)
    rows = visible(view.render(40, Palette(False), False))
    assert rows == ["✓ custom_tool"]
    assert "secret-token-abcdef" not in "\n".join(rows)
    assert factory.fallbacks == 1


def test_factory_never_swallows_base_exceptions() -> None:
    def interrupting(snapshot_obj: object, state: str) -> ToolView:
        raise KeyboardInterrupt

    factory = ToolViewFactory({"custom_tool": interrupting})
    with pytest.raises(KeyboardInterrupt):
        factory.create(_Snapshot(), state="success")


def test_builtin_names_still_cannot_be_overridden_by_an_equal_view() -> None:
    real = snapshot(ToolPresentationKind.READ, "read", {"path": "a.md"})

    def builder(snapshot_obj: object, state: str) -> ToolView:
        return GenericToolView(
            snapshot(ToolPresentationKind.READ, "read", {}), state=state
        )

    factory = ToolViewFactory({"read": builder})
    view = factory.create(real, state="success")

    assert view is not None
    assert not isinstance(view, GenericToolView)
    assert factory.fallbacks == 0


# 内置 kind 优先的统一解析契约


BUILTIN_KIND_PAYLOADS: dict[ToolPresentationKind, Mapping[str, object]] = {
    ToolPresentationKind.READ: {"path": "a.md"},
    ToolPresentationKind.WRITE: {"path": "a.md", "content": "body"},
    ToolPresentationKind.EDIT: {"path": "a.md", "matched": True, "lines": []},
    ToolPresentationKind.BASH: {"command": "ls", "output": "", "exit_code": 0},
    ToolPresentationKind.ASK: {"question": "q", "answer": "a"},
}


def _counting_builder(calls: list[str]) -> object:
    def builder(snapshot_obj: object, state: str) -> ToolView:
        calls.append(state)
        return GenericToolView(snapshot_obj, state=state)

    return builder


@pytest.mark.parametrize("kind", list(BUILTIN_KIND_PAYLOADS))
def test_custom_name_cannot_capture_a_builtin_kind(kind: ToolPresentationKind) -> None:
    """自定义名称不能截获内置 kind，也不回退通用视图。"""
    calls: list[str] = []
    factory = ToolViewFactory({"custom_tool": _counting_builder(calls)})  # type: ignore[dict-item]

    view = factory.create(
        snapshot(kind, "custom_tool", BUILTIN_KIND_PAYLOADS[kind]), state="success"
    )

    assert view is None
    assert calls == []
    assert factory.fallbacks == 0


@pytest.mark.parametrize("name", ["read", "write", "edit", "bash", "ask"])
def test_reserved_name_with_generic_kind_is_rejected(name: str) -> None:
    calls: list[str] = []
    factory = ToolViewFactory({name: _counting_builder(calls)})  # type: ignore[dict-item]

    view = factory.create(
        snapshot(ToolPresentationKind.GENERIC, name, {}), state="success"
    )

    assert view is None
    assert calls == []
    assert factory.fallbacks == 0


def test_reserved_name_still_uses_the_builtin_view_for_its_own_kind() -> None:
    calls: list[str] = []

    for name, kind in (
        ("read", ToolPresentationKind.READ),
        ("bash", ToolPresentationKind.BASH),
        ("ask", ToolPresentationKind.ASK),
    ):
        factory = ToolViewFactory({name: _counting_builder(calls)})  # type: ignore[dict-item]
        view = factory.create(
            snapshot(kind, name, BUILTIN_KIND_PAYLOADS[kind]), state="success"
        )
        assert view is not None
        assert not isinstance(view, GenericToolView)
        assert factory.fallbacks == 0

    assert calls == []


def test_custom_generic_still_prefers_the_builder_then_generic_fallback() -> None:
    calls: list[str] = []
    item = snapshot(ToolPresentationKind.GENERIC, "custom_tool", {})
    with_builder = ToolViewFactory({"custom_tool": _counting_builder(calls)}).create(  # type: ignore[dict-item]
        item, state="success"
    )
    without_builder = ToolViewFactory().create(item, state="success")

    assert calls == ["success"]
    assert isinstance(with_builder, GenericToolView)
    assert isinstance(without_builder, GenericToolView)
    assert visible(with_builder.render(40, Palette(False), False)) == ["✓ custom_tool"]
    assert visible(without_builder.render(40, Palette(False), False)) == [
        "✓ custom_tool"
    ]


def test_tool_views_never_emit_control_sequences_or_wrap_over_width() -> None:
    payloads = [
        snapshot(
            ToolPresentationKind.WRITE,
            "write",
            {"path": "a.md", "content": "恶意 \x1b[2J 内容\r\n第二行 \x1b]52;c;x\x07"},
        ),
        snapshot(
            ToolPresentationKind.BASH,
            "bash",
            {"command": "echo \x1b[2J", "output": "out \x1b[31mred", "exit_code": 0},
        ),
        snapshot(
            ToolPresentationKind.ASK,
            "ask",
            {"question": "问 \x1b[2J", "answer": "答 \x1b]0;t\x07"},
        ),
    ]
    for item in payloads:
        view = ToolViewFactory().create(item, state="success")
        assert view is not None
        rows = view.render(40, Palette(True), False)
        for row in rows:
            assert len(strip_ansi(row)) <= 40
        assert find_forbidden_sequences("\n".join(rows)) == []


def test_tool_record_updates_in_place_in_the_renderer() -> None:
    import io

    from mica_agent.tui.main_buffer.terminal import Terminal

    terminal = Terminal(
        output=io.StringIO(),
        input_factory=lambda: None,  # type: ignore[arg-type]
        size=(40, 12),
        interactive=True,
        environ={"NO_COLOR": "1"},
    )
    renderer = MainBufferRenderer(terminal, width=40, height=12, palette=Palette(False))
    factory = ToolViewFactory()
    prepared = snapshot(
        ToolPresentationKind.READ,
        "read",
        {"path": "docs/a.md"},
        status=ToolPresentationStatus.PENDING,
    )
    view = factory.create(prepared, state="pending")
    assert view is not None
    renderer.set_pending(view)
    renderer.set_chrome([], cursor=None)
    renderer.repaint()
    assert "read  docs/a.md" in "\n".join(renderer.document_lines())

    finished = snapshot(ToolPresentationKind.READ, "read", {"path": "docs/a.md"})
    done = factory.create(finished, state="success")
    assert done is not None
    renderer.set_pending(done)
    renderer.commit_pending()
    renderer.repaint()

    lines = renderer.document_lines()
    # 一次调用只有一条记录：状态原地更新，不追加第二行。
    assert sum(1 for line in lines if "read" in line) == 1


@pytest.mark.parametrize("width", [20, 40, 60])
def test_tool_view_rows_never_exceed_width(width: int) -> None:
    view = ToolViewFactory().create(
        snapshot(
            ToolPresentationKind.WRITE,
            "write",
            {"path": "docs/" + "很长的目录/" * 3 + "a.md", "content": "内容" * 40},
        ),
        state="success",
    )
    assert view is not None
    for row in view.render(width, Palette(True), False):
        assert len(strip_ansi(row)) <= width
