import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_windows_dpapi_round_trip_returns_bytes() -> None:
    from app.security import WindowsDpapiCipher

    cipher = WindowsDpapiCipher()
    assert cipher.unprotect(cipher.protect(b"snapshot")) == b"snapshot"
