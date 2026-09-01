"""Symmetric encryption for email credentials stored at rest.

Email passwords (Gmail app passwords, Seznam passwords) are encrypted with a
Fernet key stored in ``<data_dir>/secret.key`` before hitting the database,
and decrypted only in-process when we connect via IMAP. Plaintext is never
stored or returned to the client. The key is generated on first use.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


@lru_cache
def _load_key() -> bytes:
    """Read the Fernet key from ``secret.key``, or generate and persist one."""
    key_path = settings.data_dir / "secret.key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    return key


def _fernet() -> Fernet:
    return Fernet(_load_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret; returns a token safe to store as text."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a stored token back to plaintext."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Could not decrypt stored credential — the key in secret.key may "
            "have changed since it was saved."
        ) from exc
