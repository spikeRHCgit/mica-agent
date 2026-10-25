"""Read and create credentials only behind a verified security boundary."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Self

from .config_contracts import ConfigError, CredentialProtectionError

_LIMIT = 4 * 1024 * 1024
_TEMP_ATTEMPTS = 8
_DIRECTORY_MESSAGE = (
    "credential directory protection cannot be verified; protect authentication "
    "file and directory, then restart"
)
_UNSAFE_DIRECTORY_MESSAGE = (
    "credential directory permissions are unsafe; protect authentication file and "
    "directory, then restart"
)
_FILE_MESSAGE = (
    "credential file protection cannot be verified; protect authentication file and "
    "directory, then restart"
)
_UNSAFE_FILE_MESSAGE = (
    "credential file permissions are unsafe; protect authentication file and "
    "directory, then restart"
)


class ProtectedTempFile:
    """Exclusive temp file whose protection is verified before any data is written."""

    def __init__(
        self,
        path: Path,
        *,
        fd: int | None = None,
        handle: int | None = None,
        api: Any = None,
    ) -> None:
        self.path = path
        self.protection_verified = True
        self._fd = fd
        self._handle = handle
        self._api = api

    def write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("credential content must be bytes")
        if not self.protection_verified:
            raise RuntimeError("credential protection must be verified before writing")
        if self._fd is not None:
            self._write_posix(data)
        elif self._handle is not None:
            self._write_windows(data)
        else:
            raise RuntimeError("credential temp file is closed")

    def verify(self) -> None:
        """Re-check the open handle's protection without reopening the file."""
        if self._fd is not None:
            _validate_posix_file(self._fd)
        elif self._handle is not None and self._api is not None:
            self._api.validate(self._handle, directory=False)
        else:
            raise RuntimeError("credential temp file is closed")

    def _write_posix(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("credential file cannot be written")
            view = view[written:]
        os.fsync(self._fd)

    def _write_windows(self, data: bytes) -> None:
        api = self._api
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 1024 * 1024]
            buffer = api.c.create_string_buffer(chunk, len(chunk))
            count = api.w.DWORD()
            if not api.kernel.WriteFile(
                self._handle, buffer, len(chunk), api.c.byref(count), None
            ) or count.value != len(chunk):
                raise OSError("credential file cannot be written")
            offset += count.value
        if not api.kernel.FlushFileBuffers(self._handle):
            raise OSError("credential file cannot be written")

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._handle is not None and self._api is not None:
            self._api.kernel.CloseHandle(self._handle)
            self._handle = None
        if self._api is not None:
            self._api.close()
            self._api = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _verified_stat(fd: int) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError:
        raise CredentialProtectionError(_FILE_MESSAGE) from None


def _temp_name() -> str:
    return f".auth-{secrets.token_hex(8)}.tmp"


def read_credentials(path: Path) -> bytes:
    if os.name == "nt":
        return _read_windows(path)
    return _read_posix(path)


def ensure_credential_directory(path: Path) -> None:
    """Create the credential directory with protection or reject an unsafe one."""
    if os.name == "nt":
        _ensure_windows_directory(path)
    else:
        _ensure_posix_directory(path)


def create_protected_temp(directory: Path) -> ProtectedTempFile:
    """Create one exclusive temp file inside an already protected directory."""
    if os.name == "nt":
        return _create_windows_temp(directory)
    return _create_posix_temp(directory)


def _read_posix(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        parent = os.open(path.parent, flags | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        raise
    except OSError:
        raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None
    fd = None
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=parent)
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise FileNotFoundError() from None
            if error.errno == errno.ELOOP:
                raise CredentialProtectionError(_FILE_MESSAGE) from None
            raise
        _validate_posix_directory(parent)
        _validate_posix_file(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT:
            raise ConfigError("credential file is too large")
        return data
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _open_posix_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open(path, flags | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        raise
    except OSError:
        raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None


def _validate_posix_directory(fd: int) -> None:
    info = _verified_stat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise CredentialProtectionError(_UNSAFE_DIRECTORY_MESSAGE)


def _validate_posix_file(fd: int) -> None:
    info = _verified_stat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise CredentialProtectionError(_UNSAFE_FILE_MESSAGE)


def _ensure_posix_directory(path: Path) -> None:
    try:
        fd = _open_posix_directory(path)
    except FileNotFoundError:
        _create_posix_directory(path)
        try:
            fd = _open_posix_directory(path)
        except FileNotFoundError:
            raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None
    try:
        _validate_posix_directory(fd)
    finally:
        os.close(fd)


def _create_posix_directory(path: Path) -> None:
    if path.parent != path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    except OSError:
        raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None


def _create_posix_temp(directory: Path) -> ProtectedTempFile:
    parent = _open_posix_directory(directory)
    try:
        _validate_posix_directory(parent)
        for _ in range(_TEMP_ATTEMPTS):
            name = _temp_name()
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                continue
            except OSError:
                raise CredentialProtectionError(_FILE_MESSAGE) from None
            try:
                _validate_posix_file(fd)
            except BaseException:
                os.close(fd)
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError:
                    pass
                raise
            return ProtectedTempFile(directory / name, fd=fd)
        raise CredentialProtectionError(_FILE_MESSAGE)
    finally:
        os.close(parent)


class _WindowsApi:
    """Win32 calls for creating and validating credential security boundaries."""

    def __init__(self) -> None:
        import ctypes as c
        from ctypes import wintypes as w

        self.c = c
        self.w = w
        kernel = c.WinDLL("kernel32", use_last_error=True)
        advapi = c.WinDLL("advapi32", use_last_error=True)
        self.kernel = kernel
        self.advapi = advapi
        self.token = None
        self._token_buffer = None
        self.user_sid = None
        self.allowed: list[Any] = []
        self.allocated: list[Any] = []

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
        kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, c.c_void_p]
        kernel.CreateDirectoryW.restype = w.BOOL
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
        kernel.WriteFile.argtypes = [
            w.HANDLE,
            c.c_void_p,
            w.DWORD,
            c.POINTER(w.DWORD),
            c.c_void_p,
        ]
        kernel.WriteFile.restype = w.BOOL
        kernel.FlushFileBuffers.argtypes = [w.HANDLE]
        kernel.FlushFileBuffers.restype = w.BOOL
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
        advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(c.c_void_p)]
        advapi.ConvertSidToStringSidW.restype = w.BOOL
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            c.POINTER(c.c_void_p),
            c.POINTER(w.DWORD),
        ]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
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

        class SecurityAttributes(c.Structure):
            _fields_ = [
                ("length", w.DWORD),
                ("descriptor", c.c_void_p),
                ("inherit", w.BOOL),
            ]

        self.ACL = ACL
        self.Header = Header
        self.AttributeTag = AttributeTag
        self.SecurityAttributes = SecurityAttributes

    def _fail(self) -> None:
        raise CredentialProtectionError(_FILE_MESSAGE)

    def open_user(self) -> None:
        c, w = self.c, self.w
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 8, c.byref(token)
        ):
            self._fail()
        self.token = token
        size = w.DWORD()
        self.advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
        if not size.value:
            self._fail()
        buffer = c.create_string_buffer(size.value)
        if not self.advapi.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            self._fail()
        self._token_buffer = buffer
        user_sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        allowed = [user_sid]
        for identity in ("S-1-5-18", "S-1-5-32-544"):
            sid = c.c_void_p()
            if not self.advapi.ConvertStringSidToSidW(identity, c.byref(sid)):
                self._fail()
            self.allocated.append(sid)
            allowed.append(sid)
        self.user_sid = user_sid
        self.allowed = allowed

    def close(self) -> None:
        for sid in self.allocated:
            self.kernel.LocalFree(sid)
        self.allocated = []
        if self.token:
            self.kernel.CloseHandle(self.token)
            self.token = None

    def validate(self, handle: int, *, directory: bool) -> None:
        c, w = self.c, self.w
        attrs = self.AttributeTag()
        if (
            not self.kernel.GetFileInformationByHandleEx(
                handle, 9, c.byref(attrs), c.sizeof(attrs)
            )
            or attrs.attributes & 0x400
            or bool(attrs.attributes & 0x10) != directory
        ):
            self._fail()
        owner, dacl, security = c.c_void_p(), c.c_void_p(), c.c_void_p()
        try:
            if self.advapi.GetSecurityInfo(
                handle,
                1,
                1 | 4,
                c.byref(owner),
                None,
                c.byref(dacl),
                None,
                c.byref(security),
            ):
                self._fail()
            if not owner or not dacl or not self.advapi.EqualSid(owner, self.user_sid):
                self._fail()
            acl = c.cast(dacl, c.POINTER(self.ACL)).contents
            for index in range(acl.count):
                ace = c.c_void_p()
                if not self.advapi.GetAce(dacl, index, c.byref(ace)):
                    self._fail()
                header = c.cast(ace, c.POINTER(self.Header)).contents
                if header.flags & 8:  # INHERIT_ONLY grants no rights to this object.
                    continue
                if header.type == 1:  # A deny ACE never expands access.
                    continue
                if header.type != 0 or header.size < 16:
                    self._fail()
                mask = c.cast(ace.value + 4, c.POINTER(w.DWORD))[0]
                sid = c.c_void_p(ace.value + 8)
                if (
                    not self.advapi.IsValidSid(sid)
                    or 8 + self.advapi.GetLengthSid(sid) > header.size
                ):
                    self._fail()
                # Directory create/delete-child and control rights can replace auth.
                if mask & (0x500D0156 if directory else 0xF00F01FF) and not any(
                    self.advapi.EqualSid(sid, trusted) for trusted in self.allowed
                ):
                    self._fail()
        finally:
            if security:
                self.kernel.LocalFree(security)

    def open_directory(self, path: Path) -> int:
        handle = self.kernel.CreateFileW(
            str(path), 0x20000, 1, None, 3, 0x02200000, None
        )
        if handle == self.c.c_void_p(-1).value:
            if self.c.get_last_error() in {2, 3}:
                raise FileNotFoundError()
            self._fail()
        return handle

    def open_file(self, path: Path, *, access: int, share: int) -> int:
        handle = self.kernel.CreateFileW(
            str(path), access, share, None, 3, 0x00200000, None
        )
        if handle == self.c.c_void_p(-1).value:
            if self.c.get_last_error() in {2, 3}:
                raise FileNotFoundError()
            raise OSError("credential file cannot be read")
        return handle

    def _security_descriptor(self, *, directory: bool) -> Any:
        c, w = self.c, self.w
        pointer = c.c_void_p()
        if not self.advapi.ConvertSidToStringSidW(self.user_sid, c.byref(pointer)):
            self._fail()
        try:
            owner = c.wstring_at(pointer.value)
        finally:
            self.kernel.LocalFree(pointer)
        flags = "OICI" if directory else ""
        sddl = (
            f"O:{owner}D:P(A;{flags};FA;;;SY)"
            f"(A;{flags};FA;;;BA)(A;{flags};FA;;;{owner})"
        )
        descriptor = c.c_void_p()
        size = w.DWORD()
        if not self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, c.byref(descriptor), c.byref(size)
        ):
            self._fail()
        return descriptor

    def create_directory(self, path: Path) -> None:
        c = self.c
        descriptor = self._security_descriptor(directory=True)
        try:
            attributes = self.SecurityAttributes(
                c.sizeof(self.SecurityAttributes), descriptor, False
            )
            if (
                not self.kernel.CreateDirectoryW(str(path), c.byref(attributes))
                and c.get_last_error() != 183
            ):  # ERROR_ALREADY_EXISTS
                raise OSError("credential directory cannot be created")
        finally:
            self.kernel.LocalFree(descriptor)

    def create_file(self, path: Path) -> int:
        c = self.c
        descriptor = self._security_descriptor(directory=False)
        try:
            attributes = self.SecurityAttributes(
                c.sizeof(self.SecurityAttributes), descriptor, False
            )
            handle = self.kernel.CreateFileW(
                str(path),
                0x40000000,  # GENERIC_WRITE
                0,
                c.byref(attributes),
                1,  # CREATE_NEW
                0x80,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
        finally:
            self.kernel.LocalFree(descriptor)
        if handle == c.c_void_p(-1).value:
            error = c.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError()
            if error == 183:  # ERROR_ALREADY_EXISTS
                raise FileExistsError()
            raise OSError("credential file cannot be created")
        return handle


def _open_windows_verified(path: Path) -> tuple[_WindowsApi, int, int]:
    api = _WindowsApi()
    parent = None
    handle = None
    try:
        api.open_user()
        parent = api.open_directory(path.parent)
        handle = api.open_file(path, access=0x80000000 | 0x20000, share=1)
        api.validate(parent, directory=True)
        api.validate(handle, directory=False)
        return api, parent, handle
    except BaseException:
        if handle is not None:
            api.kernel.CloseHandle(handle)
        if parent is not None:
            api.kernel.CloseHandle(parent)
        api.close()
        raise


def _read_windows(path: Path) -> bytes:
    api, parent, handle = _open_windows_verified(path)
    try:
        data = api.c.create_string_buffer(_LIMIT + 1)
        count = api.w.DWORD()
        if not api.kernel.ReadFile(handle, data, _LIMIT + 1, api.c.byref(count), None):
            raise OSError("credential file cannot be read")
        if count.value > _LIMIT:
            raise ConfigError("credential file is too large")
        return data.raw[: count.value]
    finally:
        api.kernel.CloseHandle(handle)
        api.kernel.CloseHandle(parent)
        api.close()


def _ensure_windows_directory(path: Path) -> None:
    api = _WindowsApi()
    try:
        api.open_user()
        try:
            handle = api.open_directory(path)
        except FileNotFoundError:
            if path.parent != path:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise CredentialProtectionError(_DIRECTORY_MESSAGE) from None
            api.create_directory(path)
            handle = api.open_directory(path)
        try:
            api.validate(handle, directory=True)
        finally:
            api.kernel.CloseHandle(handle)
    finally:
        api.close()


def _create_windows_temp(directory: Path) -> ProtectedTempFile:
    api = _WindowsApi()
    parent = None
    handle = None
    try:
        api.open_user()
        parent = api.open_directory(directory)
        api.validate(parent, directory=True)
        for _ in range(_TEMP_ATTEMPTS):
            path = directory / _temp_name()
            try:
                handle = api.create_file(path)
            except FileExistsError:
                continue
            try:
                api.validate(handle, directory=False)
            except BaseException:
                api.kernel.CloseHandle(handle)
                handle = None
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            api.kernel.CloseHandle(parent)
            parent = None
            return ProtectedTempFile(path, handle=handle, api=api)
        raise CredentialProtectionError(_FILE_MESSAGE)
    except BaseException:
        if handle is not None:
            api.kernel.CloseHandle(handle)
        api.close()
        raise
    finally:
        if parent is not None:
            api.kernel.CloseHandle(parent)
