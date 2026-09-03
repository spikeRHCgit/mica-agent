"""Mica TUI 的唯一工作状态组件。"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from time import monotonic

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from .theme import BRAND, BRAND_DEEP, BRAND_LIGHT, ERROR, MUTED, SUCCESS


def _interpolate_hex(start: str, end: str, fraction: float) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    channels = tuple(
        round(first + (second - first) * fraction)
        for first, second in zip(start_rgb, end_rgb, strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in channels)


_MIDDLE_BRAND = _interpolate_hex(BRAND, BRAND_DEEP, 0.5)


class WorkStatus(Enum):
    """工作状态条公开的稳定状态。"""

    READY = "ready"
    WORKING = "working"
    DONE = "done"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"


class WorkPhase(Enum):
    """工作状态条公开的稳定阶段。"""

    NONE = "none"
    WAITING = "waiting"
    THINKING = "thinking"
    RESPONDING = "responding"


class WorkStatusView(Static):
    """固定在 Composer 上方、不会写入 transcript 的唯一状态条。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        auto_animate: bool = True,
        id: str | None = None,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._auto_animate = auto_animate
        self._timer: Timer | None = None
        self._last_clock_value: float | None = None
        self._animation_frame = 0
        self._status = WorkStatus.READY
        self._phase = WorkPhase.NONE
        self._phase_started_at: float | None = None
        self._run_started_at: float | None = None
        self._thought_elapsed = 0.0
        self._had_reasoning = False
        self._had_progress = False
        self._first_responding_at: float | None = None
        self._waited_elapsed = 0.0
        self._terminal_total: float | None = None
        self._terminal_thought: float | None = None
        self._terminal_waited = 0.0
        super().__init__(id=id)

    @property
    def status(self) -> WorkStatus:
        """当前稳定状态。"""
        return self._status

    @property
    def phase(self) -> WorkPhase:
        """当前工作阶段；非 working 状态为 NONE。"""
        return self._phase

    def on_mount(self) -> None:
        """整个组件生命周期只创建一个 Textual Timer。"""
        self._timer = self.set_interval(
            0.5,
            self.advance_animation,
            name="mica-work-status",
            pause=True,
        )

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def start_working(self) -> None:
        """开始一轮新的 working 状态。"""
        now = self._now()
        self._status = WorkStatus.WORKING
        self._phase = WorkPhase.WAITING
        self._animation_frame = 0
        self._run_started_at = now
        self._thought_elapsed = 0.0
        self._had_reasoning = False
        self._had_progress = False
        self._first_responding_at = None
        self._waited_elapsed = 0.0
        self._phase_started_at = now
        self._last_progress_at = now
        if self._auto_animate and self._timer is not None:
            self._timer.resume()
        self.refresh()

    @property
    def had_progress(self) -> bool:
        """本轮是否已出现 RunStarted 之外的可观察进展。"""
        return self._had_progress

    def observe_progress(self, phase: WorkPhase) -> None:
        """记录一个已经通过 Run/seq 校验的可观察进展。"""
        if self._status is not WorkStatus.WORKING:
            return
        if not isinstance(phase, WorkPhase) or phase is WorkPhase.NONE:
            raise ValueError("progress phase must be a working phase")
        now = self._now()
        self._had_progress = True
        self._last_progress_at = now
        if phase is WorkPhase.RESPONDING and self._first_responding_at is None:
            self._first_responding_at = now
            if self._run_started_at is not None:
                self._waited_elapsed = max(0.0, now - self._run_started_at)
        if phase is not self._phase:
            if self._phase is WorkPhase.THINKING and self._phase_started_at is not None:
                self._thought_elapsed += max(0.0, now - self._phase_started_at)
            self._phase = phase
            self._phase_started_at = now
        if phase is WorkPhase.THINKING:
            self._had_reasoning = True
        self.refresh()

    def advance_animation(self) -> None:
        """推进一个确定性的点号动画帧，并刷新当前计时显示。"""
        if self._status is not WorkStatus.WORKING:
            return
        self._animation_frame = (self._animation_frame + 1) % 3
        self.refresh()

    def finish(self, status: WorkStatus) -> None:
        """冻结一轮 Run 的安全终态和耗时摘要。"""
        if status not in (WorkStatus.DONE, WorkStatus.CANCEL, WorkStatus.INTERRUPT):
            raise ValueError("finish requires a terminal work status")
        now = self._now()
        total = (
            0.0
            if self._run_started_at is None
            else max(0.0, now - self._run_started_at)
        )
        thought = self._thought_elapsed
        if self._phase is WorkPhase.THINKING and self._phase_started_at is not None:
            thought += max(0.0, now - self._phase_started_at)
        self._status = status
        self._phase = WorkPhase.NONE
        self._terminal_total = total
        self._terminal_thought = thought if self._had_reasoning else None
        self._terminal_waited = (
            self._waited_elapsed if self._first_responding_at is not None else total
        )
        if self._timer is not None:
            self._timer.pause()
        self.refresh()

    def render(self) -> Text:
        if self._status is not WorkStatus.WORKING:
            if self._status is WorkStatus.DONE and self._terminal_total is not None:
                summary = (
                    f" (thought {self._terminal_thought:.1f}s · total {self._terminal_total:.1f}s)"
                    if self._terminal_thought is not None
                    else f" (waited {self._terminal_waited:.1f}s · total {self._terminal_total:.1f}s)"
                )
                output = Text("Mica · done", style=SUCCESS)
                output.append(summary, style=MUTED)
                return output
            style = (
                ERROR
                if self._status in (WorkStatus.CANCEL, WorkStatus.INTERRUPT)
                else MUTED
            )
            return Text(f"Mica · {self._status.value}", style=style)
        now = self._now()
        started_at = (
            self._phase_started_at if self._phase_started_at is not None else now
        )
        elapsed = max(0.0, now - started_at)
        dots = "." * (self._animation_frame + 1)
        output = Text(f"Mica · working{dots}", style=self._working_color(now))
        output.append(f" ({self._phase.value} {elapsed:.1f}s)", style=MUTED)
        return output

    def _working_color(self, now: float) -> str:
        last_progress = self._last_progress_at
        elapsed = 0.0 if last_progress is None else max(0.0, now - last_progress)
        if elapsed <= 3.0:
            return _interpolate_hex(BRAND_LIGHT, BRAND, elapsed / 3.0)
        if elapsed <= 10.0:
            return _interpolate_hex(BRAND, _MIDDLE_BRAND, (elapsed - 3.0) / 7.0)
        return _interpolate_hex(
            _MIDDLE_BRAND,
            BRAND_DEEP,
            min((elapsed - 10.0) / 20.0, 1.0),
        )

    def _now(self) -> float:
        value = float(self._clock())
        if self._last_clock_value is not None:
            value = max(value, self._last_clock_value)
        self._last_clock_value = value
        return value

    def __repr__(self) -> str:
        return "<WorkStatusView>"


__all__ = ["WorkPhase", "WorkStatus", "WorkStatusView"]
