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
