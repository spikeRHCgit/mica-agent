"""Workspace identity resolution and the Mica data-root boundary."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


class WorkspaceResolutionError(Exception):
    """The start directory cannot be resolved to a stable workspace identity."""


_WORKSPACE_ID_RE = re.compile(r"[0-9a-f]{64}\Z")


def _is_safe_slug_char(character: str) -> bool:
    return character.isalnum() or character in "-_"


def _identity_path_text(path: Path) -> str:
    """Return the canonical path text used for identity on this platform."""
    value = os.path.normpath(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        value = os.path.normcase(value)
    return value


def _workspace_id_for_path(path: Path) -> str:
    return hashlib.sha256(_identity_path_text(path).encode("utf-8")).hexdigest()


def workspace_slug(path: Path | str) -> str:
    """Turn a workspace directory name into a safe, readable, stable slug."""
    name = Path(path).name
    chunks: list[str] = []
    separator_pending = False
    for character in name:
        if _is_safe_slug_char(character):
            if separator_pending and chunks:
                chunks.append("-")
            chunks.append(character)
            separator_pending = False
        else:
            separator_pending = True
    folded = "".join(chunks).casefold()
    safe_chunks: list[str] = []
    separator_pending = False
    for character in folded:
        if _is_safe_slug_char(character):
            if separator_pending and safe_chunks:
                safe_chunks.append("-")
            safe_chunks.append(character)
            separator_pending = False
        else:
            separator_pending = True
    slug = "".join(safe_chunks).strip("-_")
    slug = slug[:48].strip("-_")
    return slug or "workspace"


def default_data_root() -> Path:
    """Return the default Mica data root; HOME is read only when called."""
    return Path.home() / ".mica"


def _is_git_marker(path: Path) -> bool:
    try:
        return path.is_dir() or path.is_file()
    except OSError:
        return False


def _find_git_root(start: Path) -> Path | None:
    current = start
    while True:
        if _is_git_marker(current / ".git"):
            return current
        if current.parent == current:
            return None
        current = current.parent


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Immutable canonical workspace identity; does not own data-directory duties."""

    workspace_path: Path
    workspace_id: str
    slug: str
    session_directory_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_path, Path):
            raise TypeError("workspace_path must be a Path")
        if not self.workspace_path.is_absolute():
            raise ValueError("workspace_path must be absolute")
        canonical_path = Path(_identity_path_text(self.workspace_path))
        if canonical_path != self.workspace_path:
            raise ValueError("workspace_path must be normalized")
        if not _WORKSPACE_ID_RE.fullmatch(self.workspace_id):
            raise ValueError("workspace_id must be a full lowercase SHA-256 hex value")
        if self.workspace_id != _workspace_id_for_path(self.workspace_path):
            raise ValueError("workspace_id does not match workspace_path")
        if not self.slug or len(self.slug) > 48:
            raise ValueError("slug is invalid")
        if self.slug != self.slug.casefold() or not all(
            _is_safe_slug_char(character) for character in self.slug
        ):
            raise ValueError("slug contains unsafe characters")
        expected_name = f"{self.slug}-{self.workspace_id[:16]}"
        if self.session_directory_name != expected_name:
            raise ValueError("session_directory_name does not match identity")
        if any(
            part in {"", ".", ".."} for part in Path(self.session_directory_name).parts
        ):
            raise ValueError("session_directory_name contains unsafe path parts")
        if Path(self.session_directory_name).name != self.session_directory_name:
            raise ValueError("session_directory_name must be a single path component")


def resolve_workspace(start_dir: Path | str | os.PathLike[str]) -> WorkspaceIdentity:
    """Resolve the nearest Git root, or a non-Git workspace, from an explicit start
    directory."""
    try:
        candidate = Path(start_dir)
    except (TypeError, ValueError, OSError):
        raise WorkspaceResolutionError("workspace path is invalid") from None
    try:
        if not candidate.exists():
            raise WorkspaceResolutionError("workspace path does not exist")
        if not candidate.is_dir():
            raise WorkspaceResolutionError("workspace path is not a directory")
        resolved_start = candidate.resolve(strict=True)
        root = _find_git_root(resolved_start) or resolved_start
        normalized_root = Path(_identity_path_text(root))
        identity_id = _workspace_id_for_path(normalized_root)
        slug = workspace_slug(normalized_root)
        return WorkspaceIdentity(
            workspace_path=normalized_root,
            workspace_id=identity_id,
            slug=slug,
            session_directory_name=f"{slug}-{identity_id[:16]}",
        )
    except WorkspaceResolutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceResolutionError("workspace path cannot be resolved") from None


# Kept as a descriptive alias for callers that prefer resolver terminology.
resolve_workspace_identity = resolve_workspace


__all__ = [
    "WorkspaceIdentity",
    "WorkspaceResolutionError",
    "default_data_root",
    "resolve_workspace",
    "resolve_workspace_identity",
    "workspace_slug",
]
