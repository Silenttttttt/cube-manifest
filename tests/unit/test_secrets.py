"""Unit tests for the Fernet ENC[...] decrypt bridge (secrets.py) - uses a
synthetic key/token, never touches this machine's real
~/.config/cubernetes/secrets.key, so these tests are self-contained and
don't depend on (or risk) the real key file."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from cube_manifest import secrets as secrets_mod


@pytest.fixture
def fake_fernet(monkeypatch):
    key = Fernet.generate_key()
    fernet = Fernet(key)
    monkeypatch.setattr(secrets_mod, "_fernet", lambda: fernet)
    return fernet


def _wrap(fernet: Fernet, plaintext: str) -> str:
    return f"ENC[{fernet.encrypt(plaintext.encode()).decode()}]"


def test_is_encrypted():
    assert secrets_mod.is_encrypted("ENC[abc]")
    assert not secrets_mod.is_encrypted("plain-value")
    assert not secrets_mod.is_encrypted("ENC[abc")  # missing closing bracket


def test_decrypt_value_roundtrip(fake_fernet):
    token = _wrap(fake_fernet, "super-secret-password")
    assert secrets_mod.decrypt_value(token) == "super-secret-password"


def test_decrypt_value_passthrough_for_plaintext(fake_fernet):
    assert secrets_mod.decrypt_value("already-plain") == "already-plain"


def test_decrypt_value_wrong_key_raises():
    other_fernet = Fernet(Fernet.generate_key())
    token = f"ENC[{other_fernet.encrypt(b'x').decode()}]"
    with pytest.raises(ValueError, match="Failed to decrypt"):
        secrets_mod.decrypt_value(token)


def test_decrypt_secrets_mixed_dict(fake_fernet):
    encrypted = {
        "DB_PASSWORD": _wrap(fake_fernet, "hunter2"),
        "API_KEY": _wrap(fake_fernet, "abc123"),
        "PLAIN": "not-encrypted",
    }
    result = secrets_mod.decrypt_secrets(encrypted)
    assert result == {"DB_PASSWORD": "hunter2", "API_KEY": "abc123", "PLAIN": "not-encrypted"}


def test_decrypt_secrets_skips_key_lookup_when_all_plaintext(monkeypatch):
    """No ENC[...] values at all -> must never touch the key file, so an
    app.yml with only plaintext/no secrets works even with no key present."""

    def _boom():
        raise AssertionError("should never be called - no encrypted values present")

    monkeypatch.setattr(secrets_mod, "_fernet", _boom)
    result = secrets_mod.decrypt_secrets({"FOO": "bar", "BAZ": "qux"})
    assert result == {"FOO": "bar", "BAZ": "qux"}
