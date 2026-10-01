"""工作区身份测试。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mica_agent.workspace import (
    WorkspaceIdentity,
    WorkspaceResolutionError,
    default_data_root,
    resolve_workspace,
    workspace_slug,
)


def test_non_git_directory_uses_normalized_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()

    identity = resolve_workspace(workspace / ".")

    assert identity.workspace_path == workspace.resolve()
    normalized = os.path.normcase(str(workspace.resolve()))
    assert (
        identity.workspace_id == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    )
    assert len(identity.workspace_id) == 64
    assert identity.session_directory_name.endswith(f"-{identity.workspace_id[:16]}")


@pytest.mark.parametrize("marker_kind", ["directory", "file"])
def test_git_root_is_found_from_nested_directory(
    tmp_path: Path, marker_kind: str
) -> None:
    root = tmp_path / "repo"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    marker = root / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: elsewhere", encoding="utf-8")

    identity = resolve_workspace(nested)

    assert identity.workspace_path == root.resolve()


def test_relative_and_absolute_paths_have_same_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "same"
    workspace.mkdir()
    absolute = workspace.resolve()
    relative = Path(os.path.relpath(absolute, Path.cwd()))

    assert resolve_workspace(relative) == resolve_workspace(absolute)


def test_same_name_workspaces_are_separated_by_path_hash(tmp_path: Path) -> None:
    first = tmp_path / "a" / "project"
    second = tmp_path / "b" / "project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    first_identity = resolve_workspace(first)
    second_identity = resolve_workspace(second)

    assert first_identity.slug == second_identity.slug == "project"
    assert first_identity.workspace_id != second_identity.workspace_id
    assert (
        first_identity.session_directory_name != second_identity.session_directory_name
    )


def test_slug_sanitizes_runs_and_limits_length() -> None:
    source = Path("  a///b\\\\c:..---___中文 " + "x" * 80)

    slug = workspace_slug(source)

    assert len(slug) <= 48
    assert slug == slug.casefold()
    assert "/" not in slug and "\\" not in slug and ".." not in slug
    assert slug.strip("-_") == slug


def test_slug_falls_back_for_empty_name() -> None:
    assert workspace_slug(Path("/")) == "workspace"


def test_unicode_casefold_slug_remains_a_valid_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "İ"
    workspace.mkdir()

    identity = resolve_workspace(workspace)

    assert identity.slug == "i"
    assert identity.workspace_path == workspace.resolve()
    assert identity.session_directory_name == (
        f"{identity.slug}-{identity.workspace_id[:16]}"
    )
    assert Path(identity.session_directory_name).name == identity.session_directory_name
    assert all(character.isalnum() or character in "-_" for character in identity.slug)


def test_workspace_identity_rejects_inconsistent_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = resolve_workspace(workspace)

    with pytest.raises(ValueError):
        WorkspaceIdentity(
            workspace_path=identity.workspace_path,
            workspace_id="0" * 64,
            slug=identity.slug,
            session_directory_name=identity.session_directory_name,
        )


def test_invalid_start_directory_is_stable_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceResolutionError, match="does not exist"):
        resolve_workspace(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceResolutionError, match="not a directory"):
        resolve_workspace(file_path)


def test_default_data_root_is_lazy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_data_root() == tmp_path / ".mica"


def test_importing_workspace_module_does_not_create_default_directory(
    tmp_path: Path,
) -> None:
    code = (
        "import pathlib, sys\n"
        "import mica_agent.workspace\n"
        "assert not (pathlib.Path.home() / '.mica').exists()\n"
        "assert 'mica_agent.providers' not in sys.modules\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "HOMEDRIVE": str(tmp_path.drive),
            "HOMEPATH": str(tmp_path)[len(str(tmp_path.drive)) :],
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    # 子进程只用于检查副作用；包导入由 uv 提供。
    if result.returncode != 0 and "No module named" in result.stderr:
        pytest.skip("isolated interpreter cannot import the source package")
    assert result.returncode == 0, result.stderr
