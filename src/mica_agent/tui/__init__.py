"""Mica 终端前端的显式导入入口。

本包不依赖 Textual；顶层只导出共享视图模型和主缓冲区编排入口，
不扩展 ``mica_agent`` 顶层公共 API。编排入口按需加载，避免导入
共享快照时连带装配业务运行时。
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
