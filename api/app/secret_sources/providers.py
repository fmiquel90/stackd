from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable

from app.crypto import decrypt
from app.enums import SecretProvider
from app.models.secret_source import SecretSource

# A provider fetch must be quick; a slow/hung source counts as unavailable so the run can fall back
# or fail closed rather than pinning the claim (§15.2).
FETCH_TIMEOUT_SECONDS = 10


class SecretUnavailable(Exception):
    """The referenced secret could not be fetched (provider down, timeout, not found)."""


async def _proton_pass(source: SecretSource, secret_ref: str) -> str:
    """Resolve a `pass://vault/item/field` reference via the Proton Pass CLI (§15.7). The bootstrap
    PAT / AI Access Token authenticates headlessly; Proton's E2E decryption happens client-side."""
    token = decrypt(source.bootstrap_secret_encrypted)

    def _run() -> str:
        env = {**os.environ, "PROTON_PASS_TOKEN": token}
        proc = subprocess.run(
            ["pass-cli", "read", secret_ref],
            env=env,
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise SecretUnavailable(proc.stderr.strip() or f"pass-cli exited {proc.returncode}")
        return proc.stdout.strip()

    try:
        return await asyncio.to_thread(_run)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise SecretUnavailable(str(exc)) from exc


_PROVIDERS: dict[SecretProvider, Callable[[SecretSource, str], Awaitable[str]]] = {
    SecretProvider.proton_pass: _proton_pass,
}


async def fetch_secret(source: SecretSource, secret_ref: str) -> str:
    """Resolve one reference to its live value, or raise SecretUnavailable. Indirection through
    this module-level function is the test seam (tests patch it to simulate up/down providers)."""
    fn = _PROVIDERS.get(source.provider)
    if fn is None:
        raise SecretUnavailable(f"unsupported provider {source.provider.value}")
    return await fn(source, secret_ref)
