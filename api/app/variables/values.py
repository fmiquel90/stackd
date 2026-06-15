from __future__ import annotations

import uuid

from app.crypto import decrypt, encrypt
from app.enums import SecretFallback
from app.models.variable import Variable

MASK = "•••"


def store_value(var: Variable, value: str, *, sensitive: bool) -> None:
    """Persist a value. Sensitive values are AES-GCM encrypted, write-only (§3.3, §13)."""
    var.sensitive = sensitive
    var.secret_source_id = None  # a stored value and a reference are mutually exclusive (§15.1)
    var.secret_ref = None
    var.secret_fallback_encrypted = None
    if sensitive:
        var.value_encrypted = encrypt(value)
        var.value = None
    else:
        var.value = value
        var.value_encrypted = None


def set_reference(
    var: Variable,
    *,
    source_id: uuid.UUID,
    secret_ref: str,
    fallback_mode: SecretFallback,
    fallback_value: str | None,
) -> None:
    """Point a variable at an external secret (§15.1). The value is fetched live at claim time, so
    nothing is stored here; a referenced variable is always sensitive."""
    var.sensitive = True
    var.value = None
    var.value_encrypted = None
    var.secret_source_id = source_id
    var.secret_ref = secret_ref
    var.secret_fallback_mode = fallback_mode
    var.secret_fallback_encrypted = encrypt(fallback_value) if fallback_value is not None else None


def reveal_value(var: Variable) -> str | None:
    """Plaintext value. Only ever called at claim time to build the worker payload (§7.2)."""
    if var.value_encrypted is not None:
        return decrypt(var.value_encrypted)
    return var.value
