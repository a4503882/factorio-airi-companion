"""Secret storage used by the AIRI Factorio research control center."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from typing import Protocol


class CredentialError(RuntimeError):
    """Raised when the operating-system credential vault cannot be used."""


class CredentialStore(Protocol):
    def set_secret(self, credential_id: str, secret: str) -> None: ...

    def get_secret(self, credential_id: str) -> str | None: ...

    def delete_secret(self, credential_id: str) -> bool: ...


class MemoryCredentialStore:
    """Non-persistent store used by automated tests."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def set_secret(self, credential_id: str, secret: str) -> None:
        if not secret:
            raise CredentialError("credential secret must not be blank")
        self._secrets[credential_id] = secret

    def get_secret(self, credential_id: str) -> str | None:
        return self._secrets.get(credential_id)

    def delete_secret(self, credential_id: str) -> bool:
        return self._secrets.pop(credential_id, None) is not None


if os.name == "nt":
    _LPBYTE = ctypes.POINTER(ctypes.c_ubyte)

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", _LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    """Store API keys as generic credentials in Windows Credential Manager."""

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _TARGET_PREFIX = "AIRI Factorio/"

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager is only available on Windows")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [
            ctypes.POINTER(_CREDENTIALW),
            wintypes.DWORD,
        ]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [wintypes.LPVOID]
        self._advapi32.CredFree.restype = None

    @classmethod
    def _target(cls, credential_id: str) -> str:
        credential_id = credential_id.strip()
        if not credential_id or any(character in credential_id for character in "\r\n"):
            raise CredentialError("credential id is invalid")
        return cls._TARGET_PREFIX + credential_id

    @staticmethod
    def _windows_error(action: str) -> CredentialError:
        error_code = ctypes.get_last_error()
        return CredentialError(f"Windows Credential Manager could not {action} ({error_code})")

    def set_secret(self, credential_id: str, secret: str) -> None:
        if not secret:
            raise CredentialError("credential secret must not be blank")
        encoded = secret.encode("utf-16-le")
        if len(encoded) > 5 * 512:
            raise CredentialError("credential secret is too large")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _CREDENTIALW()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = self._target(credential_id)
        credential.Comment = "AIRI Factorio Control Center API credential"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, _LPBYTE)
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "AIRI Factorio"
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise self._windows_error("save the credential")

    def get_secret(self, credential_id: str) -> str | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(
            self._target(credential_id),
            self._CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            if ctypes.get_last_error() == self._ERROR_NOT_FOUND:
                return None
            raise self._windows_error("read the credential")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return raw.decode("utf-16-le")
        finally:
            self._advapi32.CredFree(pointer)

    def delete_secret(self, credential_id: str) -> bool:
        if self._advapi32.CredDeleteW(
            self._target(credential_id),
            self._CRED_TYPE_GENERIC,
            0,
        ):
            return True
        if ctypes.get_last_error() == self._ERROR_NOT_FOUND:
            return False
        raise self._windows_error("delete the credential")
