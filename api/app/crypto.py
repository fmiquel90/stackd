from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

# AES-256-GCM at rest (SPECS §1, §13). Layout: nonce(12) || ciphertext || tag.
# 96-bit random nonce per encryption, stored with the ciphertext — never reused with the same key.
NONCE_SIZE = 12


def encrypt(plaintext: str, *, aad: bytes | None = None) -> bytes:
    key = get_settings().encryption_key_bytes
    nonce = os.urandom(NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return nonce + ct


def decrypt(blob: bytes, *, aad: bytes | None = None) -> str:
    key = get_settings().encryption_key_bytes
    nonce, ct = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, aad).decode()


def encrypt_bytes(data: bytes, *, aad: bytes | None = None) -> bytes:
    """`encrypt` for binary payloads (e.g. a saved plan file, §11.3): same key, same layout."""
    key = get_settings().encryption_key_bytes
    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt_bytes(blob: bytes, *, aad: bytes | None = None) -> bytes:
    """Inverse of `encrypt_bytes`; raises cryptography's InvalidTag on a wrong key, AAD or blob."""
    key = get_settings().encryption_key_bytes
    nonce, ct = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, aad)
