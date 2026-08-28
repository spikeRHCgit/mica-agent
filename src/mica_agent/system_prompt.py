"""显式加载并组装 Mica 内置系统提示词。"""

from __future__ import annotations

import platform
from datetime import date, datetime
from html import escape
from importlib import metadata, resources

from .workspace import WorkspaceIdentity

_TEMPLATE_PLACEHOLDER = "{{ENVIRONMENT_CONTEXT}}"


class SystemPromptError(Exception):
    """内置系统提示词无法安全加载或组装。"""


def _load_template() -> str:
    try:
        return (
            resources.files("mica_agent")
            .joinpath("prompts", "system.md")
            .read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001 - resource details must remain private
        raise SystemPromptError("unable to load built-in system prompt") from None


def _default_platform_name() -> str:
    value = platform.system()
    return "macOS" if value == "Darwin" else value


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a str")
    if not value.strip():
        raise ValueError(f"{field} must be non-blank")
    return value


def _environment_context(
    workspace: WorkspaceIdentity,
    *,
    mica_version: str,
    platform_name: str,
    current_date: date,
    timezone_name: str,
) -> str:
    workspace_path = workspace.workspace_path.as_posix()
    fields = {
        "mica_version": escape(mica_version, quote=True),
        "platform": escape(platform_name, quote=True),
        "cwd": escape(workspace_path, quote=True),
        "current_date": escape(current_date.isoformat(), quote=True),
        "timezone": escape(timezone_name, quote=True),
    }
    return (
        "<environment_context>\n"
        f"  <mica_version>{fields['mica_version']}</mica_version>\n"
        f"  <platform>{fields['platform']}</platform>\n"
        f"  <cwd>{fields['cwd']}</cwd>\n"
        "  <shell>bash</shell>\n"
        f"  <current_date>{fields['current_date']}</current_date>\n"
        f"  <timezone>{fields['timezone']}</timezone>\n"
        "  <filesystem>\n"
        "    <workspace_roots>\n"
        f"      <root>{fields['cwd']}</root>\n"
        "    </workspace_roots>\n"
        "  </filesystem>\n"
        "</environment_context>"
    )


def build_system_prompt(
    workspace: WorkspaceIdentity,
    *,
    mica_version: str | None = None,
    platform_name: str | None = None,
    current_date: date | datetime | None = None,
    timezone_name: str | None = None,
    template_text: str | None = None,
) -> str:
    """在显式调用时读取资源并返回一次性的不可变提示词字符串。"""
    if not isinstance(workspace, WorkspaceIdentity):
        raise TypeError("workspace must be a WorkspaceIdentity")
    try:
        now = (
            datetime.now().astimezone()
            if current_date is None or timezone_name is None
            else None
        )
        resolved_version = _validate_text(
            metadata.version("mica-agent") if mica_version is None else mica_version,
            "mica_version",
        )
        resolved_platform = _validate_text(
            _default_platform_name() if platform_name is None else platform_name,
            "platform_name",
        )
        if current_date is None:
            assert now is not None
            resolved_date = now.date()
        elif isinstance(current_date, datetime):
            resolved_date = current_date.date()
        elif isinstance(current_date, date):
            resolved_date = current_date
        else:
            raise TypeError("current_date must be a date or datetime")
        resolved_timezone = _validate_text(
            (now.tzname() or "UTC") if timezone_name is None else timezone_name,
            "timezone_name",
        )
        template = _load_template() if template_text is None else template_text
        if not isinstance(template, str) or template.count(_TEMPLATE_PLACEHOLDER) != 1:
            raise SystemPromptError("built-in system prompt template is invalid")
        environment = _environment_context(
            workspace,
            mica_version=resolved_version,
            platform_name=resolved_platform,
            current_date=resolved_date,
            timezone_name=resolved_timezone,
        )
        return template.replace(_TEMPLATE_PLACEHOLDER, environment, 1)
    except SystemPromptError:
        raise
    except (TypeError, ValueError):
        raise
    except Exception:  # noqa: BLE001 - environment details must remain private
        raise SystemPromptError("unable to build built-in system prompt") from None


__all__ = ["SystemPromptError", "build_system_prompt"]
