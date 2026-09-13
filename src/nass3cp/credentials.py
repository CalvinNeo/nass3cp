import ctypes
import sys
from typing import Any, Optional
from urllib.parse import urlsplit

from .errors import Nass3cpError


_DEFAULT_BACKEND = object()
_ERROR_NOT_FOUND = 1168
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_MAX_CREDENTIAL_BLOB_SIZE = 2560


def credential_target(base_url: str, insecure: bool = False) -> str:
    """Return a stable, human-readable credential identity for one NAS endpoint."""
    parsed = urlsplit(base_url)
    hostname = parsed.hostname
    port = parsed.port
    if not hostname or port is None:
        raise ValueError("NAS service URL must include a hostname and port")
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    if parsed.scheme not in ("http", "https"):
        raise ValueError("NAS service URL must use HTTP or HTTPS")
    if insecure and parsed.scheme != "https":
        raise ValueError("insecure credential mode requires HTTPS")
    mode = "https-insecure" if insecure else parsed.scheme
    # Keep credentials for authenticated TLS, explicitly unverified TLS, and
    # overlay/tunnel HTTP separate so a weaker mode cannot silently reuse one.
    return "nass3cp+%s://%s:%d" % (mode, hostname, port)


if sys.platform == "win32":
    from ctypes import wintypes


    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


    class _WindowsCredentialBackend:
        description = "Windows Credential Manager"

        def __init__(self) -> None:
            self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            self._api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
            self._api.CredWriteW.restype = wintypes.BOOL
            self._api.CredReadW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
            ]
            self._api.CredReadW.restype = wintypes.BOOL
            self._api.CredDeleteW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            self._api.CredDeleteW.restype = wintypes.BOOL
            self._api.CredFree.argtypes = [ctypes.c_void_p]
            self._api.CredFree.restype = None

        @staticmethod
        def _error(operation: str) -> Nass3cpError:
            code = ctypes.get_last_error()
            if code == 1312:
                return Nass3cpError(
                    "%s in Windows Credential Manager failed because the current process "
                    "has no interactive Windows credential session; run it as the signed-in "
                    "user or use --password-file" % operation
                )
            detail = ctypes.FormatError(code).strip()
            return Nass3cpError(
                "%s in Windows Credential Manager failed: %s (error %d)"
                % (operation, detail or "unknown Windows error", code)
            )

        def load(self, target: str) -> Optional[str]:
            pointer = ctypes.POINTER(_CREDENTIALW)()
            if not self._api.CredReadW(
                target,
                _CRED_TYPE_GENERIC,
                0,
                ctypes.byref(pointer),
            ):
                if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                    return None
                raise self._error("reading a password")
            try:
                credential = pointer.contents
                raw = ctypes.string_at(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize,
                )
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise Nass3cpError(
                        "saved password in Windows Credential Manager is corrupted; "
                        "remove it with --forget-password"
                    ) from exc
            finally:
                self._api.CredFree(ctypes.cast(pointer, ctypes.c_void_p))

        def save(self, target: str, password: str) -> None:
            raw = password.encode("utf-8")
            if len(raw) > _MAX_CREDENTIAL_BLOB_SIZE:
                raise Nass3cpError(
                    "password is too long for Windows Credential Manager (%d-byte limit)"
                    % _MAX_CREDENTIAL_BLOB_SIZE
                )
            blob = (wintypes.BYTE * len(raw)).from_buffer_copy(raw)
            credential = _CREDENTIALW()
            credential.Type = _CRED_TYPE_GENERIC
            credential.TargetName = target
            credential.Comment = "nass3cp NAS password"
            credential.CredentialBlobSize = len(raw)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(wintypes.BYTE))
            credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = "nass3cp"
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise self._error("saving a password")

        def delete(self, target: str) -> bool:
            if self._api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
                return True
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return False
            raise self._error("deleting a password")


def _platform_backend() -> Optional[Any]:
    if sys.platform == "win32":
        return _WindowsCredentialBackend()
    return None


class CredentialStore:
    """Small wrapper around the current operating system's secure credential store."""

    def __init__(self, backend: Any = _DEFAULT_BACKEND):
        self._backend = _platform_backend() if backend is _DEFAULT_BACKEND else backend

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def description(self) -> str:
        if self._backend is None:
            return "unavailable"
        return str(self._backend.description)

    def load(self, target: str) -> Optional[str]:
        if self._backend is None:
            return None
        return self._backend.load(target)

    def save(self, target: str, password: str) -> None:
        if self._backend is None:
            raise Nass3cpError(
                "secure password storage is not available on this platform; "
                "use the OS credential service or --password-file"
            )
        self._backend.save(target, password)

    def delete(self, target: str) -> bool:
        if self._backend is None:
            raise Nass3cpError("secure password storage is not available on this platform")
        return bool(self._backend.delete(target))
