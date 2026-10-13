from __future__ import annotations

import base64
import os
from typing import Protocol


class Cipher(Protocol):
    def protect(self, raw: bytes) -> bytes: ...
    def unprotect(self, protected: bytes) -> bytes: ...


class WindowsDpapiCipher:
    """DPAPI wrapper. pywin32 is intentionally a Windows-only dependency."""

    def __init__(self) -> None:
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise RuntimeError("Instale o extra [windows] para habilitar DPAPI.") from exc
        self._api = win32crypt

    def protect(self, raw: bytes) -> bytes:
        result = self._api.CryptProtectData(raw, "meraki-switch-migrator", None, None, None, 0)
        # Recent pywin32 returns bytes directly; older releases returned a
        # (description, bytes) tuple. Supporting both prevents SQLite from
        # accidentally receiving a single integer from bytes[1].
        return bytes(result[1] if isinstance(result, tuple) else result)

    def unprotect(self, protected: bytes) -> bytes:
        result = self._api.CryptUnprotectData(bytes(protected), None, None, None, 0)
        return bytes(result[1] if isinstance(result, tuple) else result)


class TestCipher:
    """Reversible codec solely for automated tests; never construct in production."""

    def protect(self, raw: bytes) -> bytes:
        return base64.b64encode(raw)

    def unprotect(self, protected: bytes) -> bytes:
        return base64.b64decode(protected)


def production_cipher() -> Cipher:
    if os.name != "nt":
        raise RuntimeError("Esta aplicação deve ser executada em Windows para proteger snapshots com DPAPI.")
    return WindowsDpapiCipher()
