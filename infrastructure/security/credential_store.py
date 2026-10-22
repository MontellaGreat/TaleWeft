"""DPAPI protected credentials; SQLite stores opaque references, never new secrets.

Uses Windows' open API, as exposed by the open-source pywin32 win32crypt module.
Immutable files are written and decrypted before any database reference is changed.
Do not delete older vault entries automatically: a database backup may reference them.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import tempfile
import uuid
from datetime import datetime, timezone
import hashlib

PREFIX = "ppcred:v1:"
_REFERENCE = re.compile(r"^ppcred:v1:([0-9a-f]{32})$")


class CredentialUnavailable(RuntimeError):
    """Missing, damaged, or unavailable OS-protected credentials."""


class WindowsDPAPI:
    def _transform(self, value: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise CredentialUnavailable("当前系统不支持 Windows 凭据保护，未写入明文密钥")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        source = Blob(len(value), buffer)
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN, current Windows user (no LOCAL_MACHINE flag).
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise CredentialUnavailable("无法读取或保护凭据；请在原 Windows 用户下恢复凭据目录")
        try:
            return ctypes.string_at(output.data, output.length)
        finally:
            kernel.LocalFree(output.data)

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, False)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, True)


class CredentialStore:
    def __init__(self, directory: Path, protector=None):
        self.directory = Path(directory)
        self.protector = protector or WindowsDPAPI()

    def resolve(self, value: str) -> str:
        if not value.startswith(PREFIX):
            return value
        match = _REFERENCE.fullmatch(value)
        if not match:
            raise CredentialUnavailable("凭据引用无效，请重新设置密钥")
        try:
            ciphertext = (self.directory / (match[1] + ".bin")).read_bytes()
            return self.protector.unprotect(ciphertext).decode("utf-8")
        except CredentialUnavailable:
            raise
        except Exception:
            raise CredentialUnavailable("凭据文件缺失或损坏；请恢复凭据目录或重新设置密钥") from None

    def store(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(PREFIX):
            self.resolve(value)  # Reject an unresolved reference before saving it.
            return value
        try:
            plaintext = value.encode("utf-8")
            ciphertext = self.protector.protect(plaintext)
            if self.protector.unprotect(ciphertext) != plaintext:
                raise CredentialUnavailable("凭据写入校验失败，原配置保持不变")
            self.directory.mkdir(parents=True, exist_ok=True)
            reference = PREFIX + uuid.uuid4().hex
            target = self.directory / (reference[len(PREFIX):] + ".bin")
            descriptor, staging = tempfile.mkstemp(prefix="pending-", dir=self.directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(ciphertext)
                    stream.flush()
                    os.fsync(stream.fileno())
                if self.protector.unprotect(Path(staging).read_bytes()) != plaintext:
                    raise CredentialUnavailable("凭据写入校验失败，原配置保持不变")
                os.replace(staging, target)
            finally:
                Path(staging).unlink(missing_ok=True)
            return reference
        except CredentialUnavailable:
            raise
        except Exception:
            raise CredentialUnavailable("安全凭据无法写入，原配置保持不变") from None

    def inventory(self) -> list[dict]:
        """Return non-secret vault metadata for the local management surface.

        The ciphertext is never returned.  A SHA-256 fingerprint is useful for
        backup/audit correlation while remaining unable to recover the secret.
        """
        if not self.directory.exists():
            return []
        entries: list[dict] = []
        for path in sorted(self.directory.glob("*.bin")):
            if not re.fullmatch(r"[0-9a-f]{32}\.bin", path.name):
                continue
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({
                "credential_ref": PREFIX + path.stem,
                "ciphertext_sha256": digest,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        return entries

    def delete(self, reference: str) -> bool:
        """Delete one vault object after the caller has checked references."""
        match = _REFERENCE.fullmatch(reference or "")
        if not match:
            raise CredentialUnavailable("凭据引用无效")
        target = self.directory / (match[1] + ".bin")
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False


def credentials_for_database(database) -> CredentialStore:
    path = getattr(database, "db_path", None)
    if not path or path == ":memory:":
        raise CredentialUnavailable("凭据存储需要明确的数据库路径")
    return CredentialStore(Path(path).resolve().parent / ".credentials")
