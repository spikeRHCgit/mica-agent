"""P5-03 工作区路径边界的公共行为测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mica_agent.workspace import resolve_workspace
from mica_agent.workspace_paths import WorkspacePathError, WorkspacePathResolver


def test_resolver_accepts_relative_path_and_rejects_absolute_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(resolve_workspace(workspace))

    assert resolver.resolve("notes/hello.txt") == workspace / "notes" / "hello.txt"

    with pytest.raises(WorkspacePathError) as raised:
        resolver.resolve(str((tmp_path / "outside.txt").resolve()))

    assert str(workspace) not in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "../outside.txt",
        "nested/../outside.txt",
        "C:temp.txt",
        r"\\server\\share\\x",
        r"\\?\\C:\\x",
        ".npmrc.",
        ".npmrc ",
        ".git./config",
        ".git /config",
        "normal.txt:secret",
        "NUL",
        "NUL.txt",
        "CON",
        "COM1.log",
        "bad\x00path",
    ],
)
def test_resolver_rejects_non_workspace_path_syntax(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(resolve_workspace(workspace))

    with pytest.raises(WorkspacePathError, match="workspace path is not allowed"):
        resolver.resolve(path)


def test_resolver_rejects_existing_symbolic_link_in_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    resolver = WorkspacePathResolver(resolve_workspace(workspace))

    with pytest.raises(WorkspacePathError, match="workspace path is not allowed"):
        resolver.resolve("linked/secret.txt")


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows reparse point test")
def test_resolver_rejects_directory_junction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = workspace / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("directory junction creation is unavailable")

    resolver = WorkspacePathResolver(resolve_workspace(workspace))

    with pytest.raises(WorkspacePathError, match="workspace path is not allowed"):
        resolver.resolve("junction/secret.txt")
