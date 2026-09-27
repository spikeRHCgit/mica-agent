"""Path boundary shared by file tools inside the workspace."""

from __future__ import annotations

import ntpath
import stat
from pathlib import Path

from .workspace import WorkspaceIdentity

_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class WorkspacePathError(ValueError):
    """The tool path violates a hard workspace boundary."""


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks, junctions and other reparse points on Windows."""
    try:
        details = path.lstat()
    except OSError:
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def is_sensitive_workspace_path(path: Path, workspace: WorkspaceIdentity) -> bool:
    """Report whether the path hits the centrally defined workspace credential boundary."""
    try:
        parts = path.relative_to(workspace.workspace_path).parts
    except ValueError:
        return True
    return is_sensitive_path_text("/".join(parts))


def is_sensitive_path_text(path: str) -> bool:
    """Pure lexical sensitive-path classification shared by file authorization and command analysis."""
    folded = tuple(part.casefold() for part in path.replace("\\", "/").split("/"))
    if ".git" in folded or "credentials" in folded:
        return True
    if not folded:
        return False
    name = folded[-1]
    if name in {".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}:
        return True
    if name in {".env.example", ".env.sample", ".env.template"}:
        return False
    return name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".key"))


class WorkspacePathResolver:
    """Restrict explicit relative paths to one resolved ``WorkspaceIdentity``."""

    def __init__(self, workspace: WorkspaceIdentity) -> None:
        if not isinstance(workspace, WorkspaceIdentity):
            raise TypeError("workspace must be a WorkspaceIdentity")
        self._workspace = workspace

    def is_sensitive(self, path: Path) -> bool:
        """Report whether a resolved path hits the workspace credential boundary."""
        return is_sensitive_workspace_path(path, self._workspace)

    def resolve(self, path: str) -> Path:
        """Resolve one workspace-relative path; reject absolute and ".." paths."""
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise WorkspacePathError("workspace path is not allowed")
        windows_path = ntpath.splitdrive(path)
        if windows_path[0] or ntpath.isabs(path) or Path(path).is_absolute():
            raise WorkspacePathError("workspace path is not allowed")
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            raise WorkspacePathError("workspace path is not allowed")
        candidate = self._workspace.workspace_path
        for part in parts:
            if part in {"", "."}:
                continue
            name = part.casefold()
            if (
                part.endswith((".", " "))
                or ":" in part
                or name.split(".", 1)[0] in _WINDOWS_DEVICE_NAMES
            ):
                raise WorkspacePathError("workspace path is not allowed")
            candidate /= part
            if _is_reparse_point(candidate):
                raise WorkspacePathError("workspace path is not allowed")
        return candidate


__all__ = [
    "WorkspacePathError",
    "WorkspacePathResolver",
    "is_sensitive_path_text",
    "is_sensitive_workspace_path",
]
