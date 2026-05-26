from __future__ import annotations

import pytest

from app.services.bdd import decrypt_value, encrypt_value


def test_encrypt_decrypt_roundtrip():
    original = "sk-ant-secret-key"
    ciphertext = encrypt_value(original)
    assert ciphertext != original
    assert decrypt_value(ciphertext) == original


def test_encrypt_none_returns_none():
    assert encrypt_value(None) is None


def test_decrypt_none_returns_none():
    assert decrypt_value(None) is None


def test_encrypt_produces_different_ciphertext():
    value = "same-key"
    cipher1 = encrypt_value(value)
    cipher2 = encrypt_value(value)
    # Fernet uses a random IV, so each encryption produces a unique ciphertext
    assert cipher1 != cipher2
    # Both should still decrypt to the same original value
    assert decrypt_value(cipher1) == value
    assert decrypt_value(cipher2) == value
