"""不可信正文与可信 ANSI 之间的唯一清洗接缝。

主缓冲区的全部正文——用户输入、模型输出、工具可见字段、交互文案、动态
事实——都视为**不可信文本**：它只能作为字符出现在屏幕上，绝不能改变
终端状态。渲染器自己生成的样式、光标与擦除序列才是**可信控制**。

清洗规则以「删除整段控制内容」为准，而不是把危险字节原样回显：

* ``ESC`` 起始的 CSI / OSC / DCS / 两字符转义整段丢弃；
* C1 区的 ``CSI``(0x9B) 与 ``OSC``(0x9D) 同样整段丢弃；
* ``BEL``、``BS``、``DEL`` 与其余 C0/C1 控制字符丢弃；
* ``CRLF`` / 单独 ``CR`` 归一为 ``LF``；
* 普通 Unicode（含中文、Emoji、组合字符、ZWJ 序列）原样保留。

只做删除与换行归一，**从不插入** ``ESC`` 或任何控制字符，因此清洗结果
不能再组成新的有效控制序列。
"""

from __future__ import annotations

# 未被识别为序列起始、但同样不得出现在正文里的控制字符。
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
    """清除非可信文本中的终端控制内容。

    ``newlines=False`` 时把换行压平为空格，供单行字段（工具名、事实行、
    面板标签）使用；``tabs=False`` 同理处理制表符。
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
    """清洗单行字段并压平空白；空结果回退为固定占位符。"""
    text = sanitize_text(str(value), newlines=False, tabs=False).strip()
    return text or fallback


def _skip_escape(value: str, index: int) -> int:
    """从 ``ESC`` 位置跳过一个完整转义序列，返回下一个待处理下标。"""
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
    """跳过 CSI 参数、中间字节与终止字节。"""
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
    """跳过 OSC 正文，直到 BEL、ST/C1-ST 或字符串结束。"""
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
    """跳过 DCS/SOS/PM/APC 正文，直到 ST/C1-ST 或字符串结束。"""
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
