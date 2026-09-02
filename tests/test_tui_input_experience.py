"""P6-02B 输入溢出提示、长粘贴原子片段、双快照与用户消息卡片测试。"""

from __future__ import annotations

import asyncio

import pytest
from rich.color import Color
from textual.events import Paste

from mica_agent.tui import MicaComposer, MicaTuiApp
from mica_agent.tui.paste import PASTE_LINE_THRESHOLD, make_paste_fragment
from mica_agent.tui.theme import MUTED
from mica_agent.tui.transcript import UserMessageCard


class RecordingTuiApp(MicaTuiApp):
    """仅在测试中观察 Composer 的公开提交消息。"""

    def __init__(self, *, ansi_color: bool | None = None) -> None:
        super().__init__(ansi_color=ansi_color)
        self.submissions: list[MicaComposer.Submitted] = []

    def on_mica_composer_submitted(
        self,
        message: MicaComposer.Submitted,
    ) -> None:
        self.submissions.append(message)


# --------------------------------------------------------------------------- #
# 5. 输入溢出提示
# --------------------------------------------------------------------------- #
def test_empty_composer_has_no_overflow_hint() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            assert composer.content_region.height == 1
            assert composer.hint_above == ""
            assert composer.hint_below == ""

    asyncio.run(scenario())


def test_eight_lines_show_no_hint_ninth_does() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press(
                *[key for _ in range(8) for key in ("x", "shift+enter")],
                "backspace",
            )
            await pilot.pause()
            assert composer.content_region.height == 8
            assert composer.hint_above == ""
            assert composer.hint_below == ""

            await pilot.press("x", "shift+enter", "x")
            await pilot.pause()
            assert composer.content_region.height == 8
            assert composer.hint_above == "\u2191 1 more"

    asyncio.run(scenario())


def test_rendered_overflow_hint_preserves_counted_hidden_rows() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            await pilot.press(
                *[key for _ in range(12) for key in ("x", "shift+enter")],
                "x",
            )
            await pilot.pause()
            composer.scroll_y = 0
            await pilot.pause()
            content_top = composer.content_region.y - composer.region.y
            rendered = composer.render_line(
                content_top + composer.content_region.height - 1
            )
            assert composer.hint_below in rendered.text
            assert "x" in rendered.text
            assert (
                composer.hidden_below
                == composer.virtual_size.height - composer.content_region.height
            )
            assert all(segment.style is not None for segment in rendered)
            hint_segments = [
                segment for segment in rendered if composer.hint_below in segment.text
            ]
            assert hint_segments
            assert all(
                segment.style is not None
                and segment.style.color == Color.parse(MUTED)
                and segment.style.color != Color.parse("red")
                for segment in hint_segments
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("no_color", [False, True])
def test_overflow_hints_survive_full_render_chain(
    monkeypatch: pytest.MonkeyPatch,
    no_color: bool,
) -> None:
    """默认颜色与 Monochrome 链都能完整刷新上下溢出提示。"""
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        app = RecordingTuiApp(ansi_color=False if no_color else None)
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            await pilot.press(
                *[key for _ in range(12) for key in ("x", "shift+enter")],
                "x",
            )
            await pilot.pause()

            hidden = composer.virtual_size.height - composer.content_region.height
            composer.scroll_y = 0
            composer.refresh()
            await pilot.pause()
            assert composer.hint_above == ""
            assert composer.hint_below == f"\u2193 {hidden} more"

            bottom_y = (
                composer.content_region.y
                - composer.region.y
                + composer.content_region.height
                - 1
            )
            bottom = composer.render_line(bottom_y)
            assert composer.hint_below in bottom.text
            assert all(segment.style is not None for segment in bottom)

            composer.scroll_y = composer.max_scroll_y
            composer.refresh()
            await pilot.pause()
            assert composer.hint_above == f"\u2191 {hidden} more"
            assert composer.hint_below == ""

            top_y = composer.content_region.y - composer.region.y
            top = composer.render_line(top_y)
            assert composer.hint_above in top.text
            assert all(segment.style is not None for segment in top)

    asyncio.run(scenario())


def test_hint_counts_hidden_rows_and_tracks_scroll() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press(
                *[key for _ in range(6) for key in ("x", "shift+enter")],
                "x",
            )
            for index in range(7):
                await pilot.press("x", "shift+enter")
            await pilot.pause()
            expected_below = (
                composer.virtual_size.height - composer.content_region.height
            )
            composer.scroll_y = 0
            await pilot.pause()
            assert composer.hint_above == ""
            assert composer.hint_below == f"\u2193 {expected_below} more"

            composer.scroll_y = composer.max_scroll_y
            await pilot.pause()
            assert composer.hint_above == f"\u2191 {expected_below} more"
            assert composer.hint_below == ""

    asyncio.run(scenario())


def dispatch_paste(app: RecordingTuiApp, text: str) -> None:
    app.post_message(Paste(text))


# --------------------------------------------------------------------------- #
# 6. 长粘贴片段
# --------------------------------------------------------------------------- #
def test_eight_lines_are_ordinary_and_nine_lines_fold_via_paste_event() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            dispatch_paste(app, "\n".join(f"short {i}" for i in range(8)))
            await pilot.pause()
            assert composer.display_fragments == ()
            composer.clear()
            dispatch_paste(app, "\n".join(f"long {i}" for i in range(9)))
            await pilot.pause()
            assert composer.text == "[paste #1 +9 lines]"

    asyncio.run(scenario())


def test_paste_folding_threshold() -> None:
    """通过公开工厂验证达到阈值逻辑行时折叠片段。"""
    short = "\n".join(f"line {i}" for i in range(PASTE_LINE_THRESHOLD - 1))
    long = "\n".join(f"line {i}" for i in range(PASTE_LINE_THRESHOLD))

    assert make_paste_fragment(short, 1) is None
    fragment = make_paste_fragment(long, 1)
    assert fragment is not None
    assert fragment.line_count == PASTE_LINE_THRESHOLD
    assert fragment.number == 1


def test_short_paste_inserts_as_ordinary_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            dispatch_paste(app, "alpha\nbeta\ngamma\ndelta\n" * 2)
            await pilot.pause()
            assert (
                composer.text
                == "alpha\nbeta\ngamma\ndelta\nalpha\nbeta\ngamma\ndelta\n"
            )
            assert composer.display_fragments == ()

    asyncio.run(scenario())


def test_long_paste_folds_to_summary() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            text = "\n".join(f"line {i}" for i in range(9))
            dispatch_paste(app, text)
            await pilot.pause()

            assert composer.text == "[paste #1 +9 lines]"
            assert len(composer.display_fragments) == 1
            fragment = composer.display_fragments[0]
            assert fragment.original == text
            assert fragment.line_count == 9
            assert fragment.number == 1
            # 原文不进入 composer.text（不回显）
            assert "line 0" not in composer.text

    asyncio.run(scenario())


def test_cursor_skips_over_paste_fragment() -> None:
    """左右方向键在跨越折叠片段时整体跳过，不进入片段内部。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            long_text = "\n".join(f"line {i}" for i in range(9))
            dispatch_paste(app, long_text)
            await pilot.pause()
            assert composer.text == "[paste #1 +9 lines]"

            # 光标位于片段末尾；按左键应整体跨过片段到起点。
            await pilot.press("left")
            await pilot.pause()
            assert composer.cursor_location == (0, 0)

            # 再按一次左键：已在文档起点，不再移动。
            await pilot.press("left")
            await pilot.pause()
            assert composer.cursor_location == (0, 0)

            # 从起点按右键：整体跨过片段到末尾。
            await pilot.press("right")
            await pilot.pause()
            assert composer.cursor_location == (0, len("[paste #1 +9 lines]"))

    asyncio.run(scenario())


def test_cursor_skips_fragment_with_surrounding_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            composer.insert("hello ")
            composer.move_cursor((0, len("hello ")))
            await pilot.pause()
            dispatch_paste(app, "\n".join(f"line {i}" for i in range(9)))
            await pilot.pause()
            composer.insert(" world")
            await pilot.pause()
            expected = "hello [paste #1 +9 lines] world"
            assert composer.text == expected

            # 从片段末尾（空格前）按左键，跨过片段回到 hello 末尾。
            composer.move_cursor((0, len("hello [paste #1 +9 lines]")))
            await pilot.press("left")
            await pilot.pause()
            assert composer.cursor_location == (0, len("hello "))

            # 从 hello 末尾按右键跨过片段到片段末尾。
            await pilot.press("right")
            await pilot.pause()
            assert composer.cursor_location == (0, len("hello [paste #1 +9 lines]"))

    asyncio.run(scenario())


def test_backspace_deletes_whole_fragment() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            dispatch_paste(app, "\n".join(f"line {i}" for i in range(9)))
            await pilot.pause()
            assert composer.text == "[paste #1 +9 lines]"

            # 光标在片段末尾；按一次 Backspace 应删除整个片段。
            await pilot.press("backspace")
            await pilot.pause()
            assert composer.text == ""
            assert composer.display_fragments == ()
            assert composer.cursor_location == (0, 0)

    asyncio.run(scenario())


def test_delete_deletes_whole_fragment() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            composer.insert("lead ")
            composer.move_cursor((0, len("lead ")))
            await pilot.pause()
            dispatch_paste(app, "\n".join(f"line {i}" for i in range(9)))
            await pilot.pause()

            # 光标在片段起点；按 Delete 应删除整个片段。
            composer.move_cursor((0, len("lead ")))
            await pilot.press("delete")
            await pilot.pause()
            assert composer.text == "lead "
            assert composer.display_fragments == ()

    asyncio.run(scenario())


def test_cursor_cannot_edit_inside_fragment() -> None:
    """方向键不能停在片段内部，也就无法逐字编辑片段。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            dispatch_paste(app, "\n".join(f"line {i}" for i in range(9)))
            await pilot.pause()

            # 从片段末尾反复按左键，即使多次也不应停在片段内部。
            for _ in range(6):
                await pilot.press("left")
                await pilot.pause()
            col = composer.cursor_location[1]
            assert col in (0, len("[paste #1 +9 lines]"))

    asyncio.run(scenario())


def test_submit_restores_full_original_text() -> None:
    """在 TUI 提交边界恢复完整原文，不把摘要文字当作真实消息。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            long_text = "\n".join(f"line {i}" for i in range(9))
            dispatch_paste(app, long_text)
            await pilot.pause()
            composer.insert(" tail")
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            assert len(app.submissions) == 1
            submitted = app.submissions[0]
            assert submitted.text == long_text + " tail"
            assert submitted.display_text == "[paste #1 +9 lines] tail"

    asyncio.run(scenario())


def test_submit_fragment_boundary_restoration() -> None:
    """片段的还原原文与紧凑显示分别正确，且摘要不被并入原文。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            long_text = "\n".join(f"line {i}" for i in range(12))
            dispatch_paste(app, long_text)
            await pilot.pause()
            # 在片段前插入普通文本。
            composer.insert("prefix: ")
            composer.move_cursor((0, 0))
            await pilot.pause()
            dispatch_paste(app, "short")
            await pilot.pause()
            composer.move_cursor((0, len(composer.text)))
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            submitted = app.submissions[0]
            # 最终显示为 short[paste #1 +12 lines]prefix:，原文还原为 short<long>prefix:。
            assert submitted.text == "short" + long_text + "prefix: "
            assert submitted.display_text == "short[paste #1 +12 lines]prefix: "

    asyncio.run(scenario())


def test_multiple_fragments_number_sequentially() -> None:
    """同一草稿内多个片段按出现顺序编号，互不覆盖。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            first = "\n".join(f"a{i}" for i in range(9))
            second = "\n".join(f"b{i}" for i in range(9))
            dispatch_paste(app, first)
            await pilot.pause()
            composer.insert(" middle ")
            await pilot.pause()
            dispatch_paste(app, second)
            await pilot.pause()

            assert composer.text == "[paste #1 +9 lines] middle [paste #2 +9 lines]"
            assert len(composer.display_fragments) == 2
            assert [f.number for f in composer.display_fragments] == [1, 2]
            assert [f.line_count for f in composer.display_fragments] == [9, 9]

            await pilot.press("enter")
            await pilot.pause()
            submitted = app.submissions[0]
            assert submitted.text == first + " middle " + second
            assert (
                submitted.display_text
                == "[paste #1 +9 lines] middle [paste #2 +9 lines]"
            )

    asyncio.run(scenario())


def test_backspace_across_boundary_deletes_only_touching_fragment() -> None:
    """Backspace 接触片段时删除整个片段，但不会误删相邻普通文本。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            composer.insert("hello ")
            await pilot.pause()
            dispatch_paste(app, "\n".join(f"line {i}" for i in range(9)))
            await pilot.pause()

            # 光标在片段末尾（hello | 片段）。
            composer.move_cursor((0, len(composer.text)))
            await pilot.press("backspace")
            await pilot.pause()
            assert composer.text == "hello "
            assert composer.display_fragments == ()

    asyncio.run(scenario())


def test_submit_appends_user_message_card_to_transcript() -> None:
    """提交后，transcript 追加一张用户消息卡片，展示紧凑显示快照。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()

            long_text = "\n".join(f"line {i}" for i in range(9))
            dispatch_paste(app, long_text)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            cards = app.query(UserMessageCard)
            assert len(cards) == 1
            card = cards.first()
            assert card.submitted.display_text == "[paste #1 +9 lines]"
            assert card.submitted.text == long_text
            # 用户消息卡片不显示角色标题或头像。
            rendered = str(card.render())
            assert rendered == "[paste #1 +9 lines]"

    asyncio.run(scenario())


def test_multiple_submits_append_cards_in_order() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()

            await pilot.press("h", "i", "enter")
            await pilot.pause()
            await pilot.press("y", "o", "enter")
            await pilot.pause()

            cards = list(app.query(UserMessageCard))
            assert len(cards) == 2
            assert [c.submitted.display_text for c in cards] == ["hi", "yo"]

    asyncio.run(scenario())


def test_clear_resets_fragment_number_for_next_draft() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            dispatch_paste(app, "\n".join(f"first {i}" for i in range(9)))
            await pilot.pause()
            assert composer.text == "[paste #1 +9 lines]"

            composer.clear()
            await pilot.pause()
            dispatch_paste(app, "\n".join(f"second {i}" for i in range(9)))
            await pilot.pause()
            assert composer.text == "[paste #1 +9 lines]"

    asyncio.run(scenario())


def test_manual_summary_cannot_steal_real_fragment_identity() -> None:
    """公开替换触碰真实片段时，用户手动同名摘要仍是普通原文。"""

    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(60, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            manual = "[paste #1 +9 lines]"
            composer.insert(manual + " ")
            real = "\n".join(f"real {i}" for i in range(9))
            dispatch_paste(app, real)
            await pilot.pause()
            assert composer.text == manual + " [paste #1 +9 lines]"

            # 通过公开 replace 接触真实片段内部；实现必须原子删除真实片段，
            # 不能因摘要损坏而回头替换前面的手动同名文字。
            start = len(manual) + 1
            composer.replace("edited", (0, start), (0, start + 1))
            await pilot.pause()
            assert composer.text == manual + " edited"

            await pilot.press("enter")
            await pilot.pause()
            assert app.submissions[0].text == manual + " edited"
            assert real not in app.submissions[0].text

    asyncio.run(scenario())


def test_deleted_fragment_does_not_restore_original_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            original = "\n".join(f"deleted {i}" for i in range(9))
            dispatch_paste(app, original)
            await pilot.pause()
            await pilot.press("backspace")
            await pilot.press("enter")
            await pilot.pause()
            assert app.submissions == []
            assert list(app.query(UserMessageCard)) == []

    asyncio.run(scenario())


def test_crlf_unicode_spaces_and_trailing_newline_are_restored_exactly() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            original = "  一\r\n二😀\r\n三  \r\n"
            dispatch_paste(app, original * 3)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.submissions[0].text == original * 3

    asyncio.run(scenario())


def test_whitespace_long_paste_has_no_submission_or_card() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            dispatch_paste(app, " \n" * 9)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.submissions == []
            assert list(app.query(UserMessageCard)) == []

    asyncio.run(scenario())


def test_markdown_characters_are_plain_text_in_user_card() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            text = "*text* # heading `code`"
            await pilot.press(*list(text), "enter")
            await pilot.pause()
            card = app.query(UserMessageCard).first()
            assert str(card.render()) == text
            assert card.submitted.display_text == text

    asyncio.run(scenario())


def test_hint_not_part_of_submitted_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press(
                *[key for _ in range(6) for key in ("x", "shift+enter")],
                "x",
            )
            for index in range(7):
                await pilot.press("x", "shift+enter")
            await pilot.pause()
            assert composer.hint_below != ""

            await pilot.press("enter")
            await pilot.pause()
            submitted_text = app.submissions[0].text
            assert "more" not in submitted_text
            assert "\u2191" not in submitted_text
            assert "\u2193" not in submitted_text
            assert composer.hint_above == ""
            assert composer.hint_below == ""
            assert composer.content_region.height == 1

    asyncio.run(scenario())
