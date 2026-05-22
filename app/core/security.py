from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.encryption_key.encode()
        _fernet = Fernet(key)
    return _fernet


def encrypt_token(plain: str) -> str:
    """Encrypt an integration token for storage."""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_token(cipher: str) -> str:
    """Decrypt an integration token retrieved from storage."""
    return _get_fernet().decrypt(cipher.encode()).decode()
