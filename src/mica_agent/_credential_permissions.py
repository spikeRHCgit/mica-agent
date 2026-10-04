"""Read credentials only after verifying the actual file security boundary."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .config_contracts import ConfigError, CredentialProtectionError

_LIMIT = 4 * 1024 * 1024


def _verified_stat(fd: int) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError:
        raise CredentialProtectionError(
            "credential protection cannot be verified; protect authentication file and directory, then restart"
        ) from None


def read_credentials(path: Path) -> bytes:
    if os.name == "nt":
        return _read_windows(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        parent = os.open(path.parent, flags | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        raise
    except OSError:
        raise CredentialProtectionError(
            "credential directory protection cannot be verified; protect authentication file and directory, then restart"
        ) from None
    fd = None
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise FileNotFoundError() from None
            if error.errno == errno.ELOOP:
                raise CredentialProtectionError(
                    "credential file protection cannot be verified; protect authentication file and directory, then restart"
                ) from None
            raise
        info = _verified_stat(parent)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise CredentialProtectionError(
                "credential directory permissions are unsafe; protect authentication file and directory, then restart"
            )
        info = _verified_stat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise CredentialProtectionError(
                "credential file permissions are unsafe; protect authentication file and directory, then restart"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT:
            raise ConfigError("credential file is too large")
        return data
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _read_windows(path: Path) -> bytes:
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        c.c_void_p,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    kernel.ReadFile.argtypes = [
        w.HANDLE,
        c.c_void_p,
        w.DWORD,
        c.POINTER(w.DWORD),
        c.c_void_p,
    ]
    kernel.ReadFile.restype = w.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [
        w.HANDLE,
        c.c_int,
        c.c_void_p,
        w.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE,
        c.c_int,
        c.c_void_p,
        w.DWORD,
        c.POINTER(w.DWORD),
    ]
    advapi.GetSecurityInfo.argtypes = [
        w.HANDLE,
        c.c_int,
        w.DWORD,
        c.POINTER(c.c_void_p),
        c.c_void_p,
        c.POINTER(c.c_void_p),
        c.c_void_p,
        c.POINTER(c.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = w.DWORD
    advapi.EqualSid.argtypes = [c.c_void_p, c.c_void_p]
    advapi.EqualSid.restype = w.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, c.POINTER(c.c_void_p)]
    advapi.GetAce.argtypes = [c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]
    advapi.IsValidSid.argtypes = [c.c_void_p]
    advapi.GetLengthSid.argtypes = [c.c_void_p]
    advapi.GetLengthSid.restype = w.DWORD

    class ACL(c.Structure):
        _fields_ = [
            ("revision", w.BYTE),
            ("pad", w.BYTE),
            ("size", w.WORD),
            ("count", w.WORD),
            ("pad2", w.WORD),
        ]

    class Header(c.Structure):
        _fields_ = [("type", w.BYTE), ("flags", w.BYTE), ("size", w.WORD)]

    class AttributeTag(c.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

    def fail() -> None:
        raise CredentialProtectionError(
            "credential protection cannot be verified; protect authentication file and directory, then restart"
        )

    # Hold the non-linked parent without share-delete while checking and reading.
    parent = kernel.CreateFileW(str(path.parent), 0x20000, 1, None, 3, 0x02200000, None)
    if parent == c.c_void_p(-1).value:
        if c.get_last_error() in {2, 3}:
            raise FileNotFoundError()
        fail()
    handle = None
    token = w.HANDLE()
    allocated: list[c.c_void_p] = []
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
            fail()
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
        if not size.value:
            fail()
        user_buffer = c.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user_buffer, size, c.byref(size)):
            fail()
        user_sid = c.cast(user_buffer, c.POINTER(c.c_void_p))[0]
        allowed = [user_sid]
        for identity in ("S-1-5-18", "S-1-5-32-544"):
            sid = c.c_void_p()
            if not advapi.ConvertStringSidToSidW(identity, c.byref(sid)):
                fail()
            allocated.append(sid)
            allowed.append(sid)

        def validate(object_handle, *, directory: bool) -> None:
            attrs = AttributeTag()
            if (
                not kernel.GetFileInformationByHandleEx(
                    object_handle, 9, c.byref(attrs), c.sizeof(attrs)
                )
                or attrs.attributes & 0x400
                or bool(attrs.attributes & 0x10) != directory
            ):
                fail()
            owner, dacl, security = c.c_void_p(), c.c_void_p(), c.c_void_p()
            try:
                if advapi.GetSecurityInfo(
                    object_handle,
                    1,
                    1 | 4,
                    c.byref(owner),
                    None,
                    c.byref(dacl),
                    None,
                    c.byref(security),
                ):
                    fail()
                if not owner or not dacl or not advapi.EqualSid(owner, user_sid):
                    fail()
                acl = c.cast(dacl, c.POINTER(ACL)).contents
                for index in range(acl.count):
                    ace = c.c_void_p()
                    if not advapi.GetAce(dacl, index, c.byref(ace)):
                        fail()
                    header = c.cast(ace, c.POINTER(Header)).contents
                    if (
                        header.flags & 8
                    ):  # INHERIT_ONLY grants no rights to this object.
                        continue
                    if header.type == 1:  # A deny ACE never expands access.
                        continue
                    if header.type != 0 or header.size < 16:
                        fail()
                    mask = c.cast(ace.value + 4, c.POINTER(w.DWORD))[0]
                    sid = c.c_void_p(ace.value + 8)
                    if (
                        not advapi.IsValidSid(sid)
                        or 8 + advapi.GetLengthSid(sid) > header.size
                    ):
                        fail()
                    # Directory create/delete-child and control rights can replace auth.
                    if mask & (0x500D0156 if directory else 0xF00F01FF) and not any(
                        advapi.EqualSid(sid, trusted) for trusted in allowed
                    ):
                        fail()
            finally:
                if security:
                    kernel.LocalFree(security)

        handle = kernel.CreateFileW(
            str(path), 0x80000000 | 0x20000, 1, None, 3, 0x00200000, None
        )
        if handle == c.c_void_p(-1).value:
            handle = None
            if c.get_last_error() in {2, 3}:
                raise FileNotFoundError()
            raise OSError("credential file cannot be read")
        validate(parent, directory=True)
        validate(handle, directory=False)
        data = c.create_string_buffer(_LIMIT + 1)
        count = w.DWORD()
        if not kernel.ReadFile(handle, data, _LIMIT + 1, c.byref(count), None):
            raise OSError("credential file cannot be read")
        if count.value > _LIMIT:
            raise ConfigError("credential file is too large")
        return data.raw[: count.value]
    finally:
        for sid in allocated:
            kernel.LocalFree(sid)
        if token:
            kernel.CloseHandle(token)
        if handle is not None:
            kernel.CloseHandle(handle)
        kernel.CloseHandle(parent)
