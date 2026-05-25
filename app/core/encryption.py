"""
Fernet-based encryption for company API integration keys.

Uses a deterministic Fernet key derived from settings.SECRET_KEY via SHA-256.
Keys are encrypted at rest and decrypted only at tool-invocation time.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def _derive_fernet_key() -> bytes:
    """Derive a 32-byte Fernet-compatible key from SECRET_KEY via SHA-256."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_fernet_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string → base64 ciphertext."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64 ciphertext → plaintext string."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt value — encryption key may have rotated")
        raise ValueError("Decryption failed. The encryption key may have changed.")
