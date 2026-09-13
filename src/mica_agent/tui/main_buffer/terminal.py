"""主缓冲区终端的唯一 IO 所有者。

本模块是 ``main_buffer`` 子包中唯一接触 stdin/stdout 与终端能力的模块：
它负责原始输入、终端尺寸、颜色能力判断和主缓冲区输出。它**不**进入
alternate screen、不捕获鼠标、不清除 scrollback，也不解释任何界面语义，
因此渲染器、编辑器和编排层都只能通过它输出字符。

安全边界：渲染器写出的可信 ANSI 与不可信正文在**语义正文边界**分开
（见 :mod:`mica_agent.tui.main_buffer.sanitize`），因此 :meth:`Terminal.write`
不再做二次清洗；真正的防线是 :func:`find_forbidden_sequences`，它对**实际
发送出去的完整字节流**做模式检测，而不是把字符串枚举当作主要防线。
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

# 明确禁止的终端效果（文档与测试引用的样例，不是主要防线）。
FORBIDDEN_SEQUENCES: tuple[str, ...] = (
    "\x1b[?1049h",  # alternate screen on
    "\x1b[?1049l",  # alternate screen off
    "\x1b[?47h",
    "\x1b[?1000h",  # 鼠标捕获
    "\x1b[?1002h",
    "\x1b[?1003h",
    "\x1b[?1005h",
    "\x1b[?1006h",
    "\x1b[?1015h",
    "\x1b[3J",  # 清除 scrollback
)

# 唯一允许出现的私有模式：自动换行与光标可见性。
_ALLOWED_PRIVATE_MODES = frozenset({"7", "25"})
# 整屏/滚动缓冲区擦除（ED 参数 1/2/3）；0 与缺省是「擦除光标以下」，允许。
_FORBIDDEN_ERASE_PARAMS = frozenset({"1", "2", "3"})
# 会改变终端窗口或剪贴板的 OSC 操作。
_FORBIDDEN_OSC_NUMBERS = frozenset({"0", "1", "2", "52"})

# 允许出现的控制字符：换行、制表与回车（CRLF 和回到行首是重绘原语）。
_ALLOWED_CONTROL_CHARS = frozenset({"\n", "\t", "\r"})

_AUTOWRAP_OFF = "\x1b[?7l"
_AUTOWRAP_ON = "\x1b[?7h"

DEFAULT_SIZE = (80, 24)


class KeyInput(Protocol):
    """prompt_toolkit ``Input`` 中主缓冲区前端实际使用的子集。"""

    def read_keys(self) -> list[Any]:
        """返回当前已就绪的按键，不阻塞。"""

    def flush_keys(self) -> list[Any]:
        """结束未完成的输入序列并返回因此产生的按键。"""

    def attach(self, callback: Callable[[], None]) -> AbstractContextManager[None]:
        """在事件循环中注册输入就绪回调。"""

    def raw_mode(self) -> AbstractContextManager[None]:
        """进入原始模式。"""


def default_input_factory() -> KeyInput:
    """构造平台默认的低层输入；只在进入原始模式时调用。

    Windows 控制台在 VT 输入模式下偶尔只给出 ``uChar`` 为 ``\\x00`` 的导航
    记录，锁定版读取器会丢弃它们，因此这里就地叠加一层 VirtualKeyCode 后备。
    其他平台、非 VT 模式与管道输入保持锁定版行为。
    """
    from prompt_toolkit.input import create_input

    acquired = create_input()
    install_windows_navigation_fallback(acquired)
    return acquired


# -- Windows 控制台记录的导航后备 ---------------------------------------------

# Windows 控制台记录类型（Win32 ABI）：1 = KEY_EVENT。
KEY_EVENT_TYPE = 0x0001

# 受支持的导航 VirtualKeyCode 及其等效 VT 序列。序列与锁定版 prompt_toolkit
# 的 ANSI 序列表一致，因此后备路径与标准终端路径产生完全相同的按键。
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
    """取出记录中的按键按下事件；非按键事件、key-up 与未知记录返回 ``None``。

    只按属性读取（``EventType``/``Event.KeyEvent``），因此不需要导入任何
    Win32 类型，测试也能用等价替身构造记录。
    """
    if getattr(record, "EventType", None) != KEY_EVENT_TYPE:
        return None
    event = getattr(getattr(record, "Event", None), "KeyEvent", None)
    if event is None or not getattr(event, "KeyDown", False):
        return None
    return event


def windows_record_sequences(records: Iterable[Any]) -> list[str]:
    """把一批 Windows 控制台记录解释为按原顺序排列的待解析输入串。

    * ``uChar != "\\x00"``：原样产出该字符，继续走标准 VT 路径；
    * ``uChar == "\\x00"`` 且是明确支持的导航 VirtualKeyCode：产出等效 VT
      序列（例如 ``VK_UP`` → ``"\\x1b[A"``），由同一个 ``Vt100Parser`` 解析；
    * key-up、鼠标/菜单/焦点事件、未知记录与不支持的 VirtualKeyCode：不产出。

    同一批记录里已经有字符记录的 VirtualKeyCode 不再使用后备，因此同一个
    物理按键既不会既走标准序列又走后备，也不会被合并成两次导航。
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
    """Windows 控制台记录到按键的纯解释器，不做任何平台 IO。

    它只持有锁定版 ``Vt100Parser``：标准字符与导航后备都变成同一批输入串，
    因此解析结果与真实 VT 序列完全一致（例如 ``Keys.Up`` + ``"\\x1b[A"``）。
    单独 ``Esc`` 仍要等 :meth:`flush` 结束未完成序列之后才产生按键。
    """

    def __init__(self) -> None:
        self._buffer: list[Any] = []
        self._parser = Vt100Parser(lambda key_press: self._buffer.append(key_press))

    def feed(self, text: str) -> None:
        """喂入一段已经确定是 VT 输入的文本。"""
        self._parser.feed(text)

    def feed_records(self, records: Iterable[Any]) -> None:
        """喂入一批控制台记录：记录 → 输入串 → 锁定版解析器。"""
        for sequence in windows_record_sequences(records):
            self._parser.feed(sequence)

    def flush(self) -> list[Any]:
        """结束未完成的序列并返回因此产生的按键（单独 ``Esc`` 在这里出现）。"""
        self._parser.flush()
        return self.take()

    def take(self) -> list[Any]:
        """取出并清空当前已解析出的按键。"""
        keys, self._buffer = self._buffer, []
        return keys


def is_windows_vt_reader(reader: Any) -> bool:
    """该对象是否为可叠加导航后备的 Windows VT 读取器。

    按形状判断（``read``/``flush_keys``/``handle``/``_vt100_parser``）而不是
    导入具体类型，因此不需要任何 Win32 导入，测试也能用等价替身验证适配器。
    """
    if not callable(getattr(reader, "read", None)):
        return False
    if not callable(getattr(reader, "flush_keys", None)):
        return False
    if not hasattr(reader, "handle"):
        return False
    return hasattr(reader, "_vt100_parser")


def _hybrid_reader_type(base_type: type) -> type:
    """构造「标准 VT 字符 + 零字符导航后备」的混合读取器类型。

    平台 IO（控制台句柄、事件等待、``ReadConsoleInputW``）仍由锁定版
    prompt_toolkit 完成；这里只接管「记录 → 待解析输入串」这一步并复用它的
    解析器语义。
    """

    class WindowsVtInputReader(base_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self._mica_keys = WindowsVtKeyReader()

        def _get_keys(self, count: Any, input_records: Any) -> tuple[()]:
            """把这一批记录交给 Mica 的解释器，锁定版解析器不再经手。"""
            self._mica_keys.feed_records(
                input_records[index] for index in range(count.value)
            )
            return ()

        def read(self) -> list[Any]:
            """走锁定版平台 IO，返回 Mica 解释后的按键。"""
            super().read()
            return self._mica_keys.take()

        def flush_keys(self) -> list[Any]:
            """结束未完成的序列；单独 ``Esc`` 只在这里成为按键。"""
            return self._mica_keys.flush()

    return WindowsVtInputReader


def windows_navigation_reader(reader: Any) -> Any:
    """返回叠加了零字符导航后备的新读取器。

    以 ``type(reader)`` 为基类就地扩展，因此不导入任何 Win32 类型，也不
    复制控制台句柄与记录轮询。
    """
    return _hybrid_reader_type(type(reader))()


def install_windows_navigation_fallback(inner: Any) -> bool:
    """就地为 Windows VT 输入安装导航后备；返回是否已安装。

    Windows 专属能力全部通过被替换的读取器本身获得，本模块不导入 Win32
    类型；替换只作用于**这一个输入实例**，不修改 prompt_toolkit 源码，也不
    做进程级全局替换。任何失败都保留锁定版读取器不变。
    """
    if sys.platform != "win32":
        return False
    current = getattr(inner, "console_input_reader", None)
    if not is_windows_vt_reader(current):
        return False
    try:
        replacement = windows_navigation_reader(current)
    except Exception:  # noqa: BLE001 - 安装失败时保留锁定版读取器
        return False
    close = getattr(current, "close", None)
    if callable(close):
        # 旧读取器的句柄释放失败不该影响替换：新读取器已经建好自己的句柄。
        with suppress(Exception):
            close()
    inner.console_input_reader = replacement
    return True


def find_forbidden_sequences(text: str) -> list[str]:
    """扫描实际输出流，返回其中不可接受的终端效果描述。

    这是主缓冲区安全边界的主要防线：它按终端序列的语法解析，而不是匹配
    固定字符串，因此未知的私有模式、整屏擦除、标题与剪贴板操作都会被抓出。
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
    """检查 ``ESC``/C1 之后的序列并返回下一个待扫描下标。"""
    introducer = text[index]
    if introducer == "[":
        return _inspect_csi(text, index + 1, findings)
    if introducer == "]":
        return _inspect_osc(text, index + 1, findings)
    if introducer in {"P", "X", "^", "_"}:
        findings.append(f"ESC {introducer} 字符串控制")
        return _skip_to_string_end(text, index + 1)
    if introducer == "c":  # RIS 全量复位
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
    """一段 CSI 的参数区间、终止字节下标与下一扫描位置。"""

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
    """当前终端的只读能力快照。"""

    ansi: bool
    interactive: bool
    width: int
    height: int


class Terminal:
    """主缓冲区的唯一 IO 所有者。

    除本类外，``main_buffer`` 的任何模块都不允许写 stdout 或读取原始输入。
    ``emitted`` 保存实际发送的全部文本，供测试与视觉证据核对。
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

    # -- 能力 ---------------------------------------------------------------

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
        """注入终端尺寸；``None`` 表示回到真实终端查询（测试接缝）。"""
        self._forced_size = size

    def interactive_enabled(self) -> bool:
        """当前输出是否是可进行光标寻址的交互式终端。"""
        if self._forced_interactive is not None:
            return self._forced_interactive
        return bool(getattr(self._output, "isatty", lambda: False)())

    def ansi_enabled(self) -> bool:
        """是否使用颜色；与交互能力独立，NO_COLOR 只关闭颜色。"""
        return self.interactive_enabled() and not (
            self._environ.get("NO_COLOR")
            or self._environ.get("TERM", "").strip().lower() == "dumb"
        )

    # -- 输出 ---------------------------------------------------------------

    def write(self, text: str) -> None:
        """发送一段已经完成换行处理的文本。

        这是**可信控制**入口：渲染器生成的光标与擦除序列必须原样写出，
        因此这里不做清洗。不可信正文在进入语义块之前就已清洗。
        """
        if not text:
            return
        self.emitted.append(text)
        self._output.write(text)
        self._output.flush()

    def write_line(self, text: str) -> None:
        """发送一行并换行。"""
        self.write(text + "\r\n")

    def emitted_text(self) -> str:
        """实际发送过的完整字节流（测试与诊断用）。"""
        return "".join(self.emitted)

    def forbidden_findings(self) -> list[str]:
        """对实际发送过的完整字节流做安全扫描。"""
        return find_forbidden_sequences(self.emitted_text())

    # -- 输入 ---------------------------------------------------------------

    @property
    def input(self) -> KeyInput:
        if self._input is None:
            raise RuntimeError("terminal input is not open")
        return self._input

    @property
    def is_open(self) -> bool:
        return self._input is not None

    def open_input(self) -> None:
        """进入原始模式并关闭自动换行；重复调用安全。

        任何一步失败都会回收已经获取的资源后再向上抛出，因此调用方只需
        在 ``finally`` 中调用一次 :meth:`close_input`。
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
        """注册输入就绪回调；成功注册后由 :meth:`close_input` detach。"""
        if self._attached is not None:
            return
        self._attached = self.input.attach(callback)
        self._attached.__enter__()

    def drain_keys(self) -> list[Any]:
        """消费当前所有就绪按键。"""
        return self.input.read_keys()

    def flush_keys(self) -> list[Any]:
        """结束输入中未完成的序列并返回因此产生的按键。

        单独 ``Esc`` 只有在有界延迟后调用本方法时才成为按键，因此调用方必须
        使用结果，而不是像丢弃缓冲那样忽略返回值。与 :meth:`drain_keys`
        对称：没有未完成序列时返回空列表。
        """
        return self.input.flush_keys()

    def close_input(self) -> None:
        """恢复终端状态并释放输入对象；重复调用安全。

        每一项清理互相隔离：单项失败只记录在 :attr:`cleanup_errors`，
        不会阻止其他必要清理，也不会以异常形式打扰使用者。
        普通 ``Exception`` 之外（例如 ``CancelledError``）不在此吞掉。
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
        except Exception as error:  # noqa: BLE001 - 清理失败只记账不外抛
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
