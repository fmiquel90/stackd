from __future__ import annotations


class Masker:
    """Replaces every known secret value with *** before logs leave the worker (SPECS §5.1).

    Limitation (documented in §5.1): a transformed secret (base64, substring) escapes value-based
    masking — do not put exploitable secrets in non-sensitive tfvars.
    """

    def __init__(self, secrets: list[str]) -> None:
        # Longest first so overlapping secrets are fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***")
        return text
