from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mica_agent._credential_permissions import read_credentials
from mica_agent.config_contracts import ConfigError


def protect(path: Path, *, shared: bool = False, rights: str = "Read") -> None:
    """仅修改隔离文件/目录的真实 ACL；宽泛授权用于负向验证。"""
    directory = path.is_dir()
    if not directory:
        protect(path.parent)
    if os.name != "nt":
        path.chmod(
            (0o722 if shared else 0o700) if directory else (0o644 if shared else 0o600)
        )
        return
    script = """
$p = $env:MICA_TEST_ACL_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = if ($env:MICA_TEST_ACL_DIRECTORY -eq '1') {
    New-Object System.Security.AccessControl.DirectorySecurity
} else {
    New-Object System.Security.AccessControl.FileSecurity
}
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = if ($env:MICA_TEST_ACL_DIRECTORY -eq '1') {
    New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
} else {
    New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
}
$acl.AddAccessRule($rule)
if ($env:MICA_TEST_ACL_SHARED -eq '1') {
    $everyone = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($everyone, $env:MICA_TEST_ACL_RIGHTS, 'Allow')))
}
if ($env:MICA_TEST_ACL_DIRECTORY -eq '1') {
    [System.IO.Directory]::SetAccessControl($p, $acl)
} else {
    [System.IO.File]::SetAccessControl($p, $acl)
}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={
            **os.environ,
            "MICA_TEST_ACL_PATH": str(path),
            "MICA_TEST_ACL_SHARED": "1" if shared else "0",
            "MICA_TEST_ACL_DIRECTORY": "1" if directory else "0",
            "MICA_TEST_ACL_RIGHTS": rights,
        },
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, "isolated ACL setup failed"


def test_real_user_only_permissions_allow_read_and_shared_access_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "auth.json"
    payload = b'{"providers":{"local":{"type":"api_key","apiKey":"TEST_ONLY_KEY"}}}'
    path.write_bytes(payload)
    protect(path)
    assert read_credentials(path) == payload
    protect(path, shared=True)
    with pytest.raises(ConfigError) as caught:
        read_credentials(path)
    assert "TEST_ONLY_KEY" not in str(caught.value)
    assert str(path) not in str(caught.value)
    assert path.read_bytes() == payload


def test_missing_credentials_do_not_create_a_file(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    with pytest.raises(FileNotFoundError):
        read_credentials(path)
    assert not path.exists()


def test_directory_is_not_a_credential_file(tmp_path: Path) -> None:
    with pytest.raises((ConfigError, OSError)):
        read_credentials(tmp_path)


@pytest.mark.parametrize("rights", ["CreateFiles", "DeleteSubdirectoriesAndFiles"])
def test_protected_file_in_replaceable_parent_is_rejected(tmp_path, rights):
    parent = tmp_path / "private"
    parent.mkdir()
    path = parent / "auth.json"
    payload = b'{"providers":{}}'
    path.write_bytes(payload)
    protect(parent)
    protect(path)
    assert read_credentials(path) == payload
    protect(parent, shared=True, rights=rights)
    try:
        with pytest.raises(ConfigError) as caught:
            read_credentials(path)
        assert str(path) not in str(caught.value)
        assert path.read_bytes() == payload
    finally:
        protect(parent)


def test_credential_parent_link_is_rejected(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir()
    path = parent / "auth.json"
    path.write_bytes(b'{"providers":{}}')
    protect(parent)
    protect(path)
    link = tmp_path / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(parent)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, "isolated directory junction setup failed"
    else:
        link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(ConfigError):
        read_credentials(link / "auth.json")


@pytest.mark.skipif(os.name != "nt", reason="Win32 security query boundary")
def test_failed_security_query_disables_auth_without_reading_credentials(
    tmp_path, monkeypatch
):
    import ctypes

    from test_config_service import make_service

    from mica_agent.session_contracts import SessionSelection

    configuration = make_service(tmp_path)
    assert (
        configuration.resolve(
            configuration.load(), explicit=SessionSelection("local", "a")
        ).status
        == "ready"
    )
    load_dll = ctypes.WinDLL
    reads = []

    def failed_query(*args):
        return 5

    def forbidden_read(*args):
        reads.append(True)
        raise AssertionError("unverified credentials must not be read")

    class Library:
        def __init__(self, name, *args, **kwargs):
            self.dll = load_dll(name, *args, **kwargs)

        def __getattr__(self, name):
            if name == "GetSecurityInfo":
                return failed_query
            if name == "ReadFile":
                return forbidden_read
            return getattr(self.dll, name)

    monkeypatch.setattr(ctypes, "WinDLL", Library)
    snapshot = configuration.load()
    assert reads == []
    assert "credential protection" in snapshot.diagnostics[0]
    assert (
        configuration.resolve(snapshot, explicit=SessionSelection("local", "a")).status
        == "unresolved"
    )
    assert "TEST_ONLY_SECRET" not in repr(snapshot)
