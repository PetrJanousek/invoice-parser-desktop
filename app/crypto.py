"""Symmetric encryption for email credentials stored at rest.

Email passwords (Gmail app passwords, Seznam passwords) are encrypted with a
Fernet key from APP_ENCRYPTION_KEY before hitting the database, and decrypted
only in-process when we connect via IMAP. Plaintext is never stored or returned
to the client.
"""
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    key = settings.app_encryption_key
    if "REPLACE_ME" in key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret; returns a token safe to store as text."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a stored token back to plaintext."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Could not decrypt stored credential — APP_ENCRYPTION_KEY may have "
            "changed since it was saved."
        ) from exc
