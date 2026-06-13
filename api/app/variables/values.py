from __future__ import annotations

from app.crypto import decrypt, encrypt
from app.models.variable import Variable

MASK = "•••"


def store_value(var: Variable, value: str, *, sensitive: bool) -> None:
    """Persist a value. Sensitive values are AES-GCM encrypted, write-only (§3.3, §13)."""
    var.sensitive = sensitive
    if sensitive:
        var.value_encrypted = encrypt(value)
        var.value = None
    else:
        var.value = value
        var.value_encrypted = None


def reveal_value(var: Variable) -> str | None:
    """Plaintext value. Only ever called at claim time to build the worker payload (§7.2)."""
    if var.value_encrypted is not None:
        return decrypt(var.value_encrypted)
    return var.value
