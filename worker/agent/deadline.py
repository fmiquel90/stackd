"""The job's credential deadline (SPECS §7.4): when the first of its short-lived credentials — the
managed-state token, the OIDC workload token — expires, on this host's monotonic clock.

Both are JWTs minted at the claim. Their lifetime (`exp - iat`, both from the API's clock) is
anchored to the moment the agent received the claim, so a clock skew between the worker and the
API never moves the deadline; the claim's own latency is covered by the safety margin. Nothing of a
token but those two claims is read, and nothing is verified: the API does that on every use."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

# A command must be over this long before the first credential expires: tofu's last state write and
# UNLOCK, sent as it exits, must still go out with a valid token.
SAFETY_MARGIN_SECONDS = 60
# A step started at the last moment still gets this much: `output -json` after a successful apply
# reads the state with a token that is still valid inside the margin.
MIN_COMMAND_SECONDS = 30
# The apply (or a mutating command) is not started with less than this left before the deadline —
# or its whole budget, when that is shorter: nothing is applied rather than an apply cut short.
MIN_APPLY_SECONDS = 5 * 60

# A token whose lifetime can't be read (never the case with this API's tokens): the documented
# windows (§10.2, §11.2), the shortest for its credential phase — fail closed.
_PLAN_FALLBACK_SECONDS = 30 * 60
_APPLY_GRACE_FALLBACK_SECONDS = 5 * 60


def token_lifetime(token: str) -> int | None:
    """`exp - iat` of a JWT, in seconds; None when it is not a JWT carrying both as integers."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp, iat = claims["exp"], claims["iat"]
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (exp, iat)):
        return None
    return max(0, exp - iat)


def _fallback_lifetime(job: dict[str, Any]) -> int:
    if job.get("cred_phase") == "plan":
        return _PLAN_FALLBACK_SECONDS
    budget = job.get("apply_timeout_seconds") or _PLAN_FALLBACK_SECONDS
    return int(budget) + _APPLY_GRACE_FALLBACK_SECONDS


class Deadline:
    """`at`: the monotonic time the job's first credential expires; None when it holds none (static
    credentials, unmanaged state): its steps keep their own budgets."""

    def __init__(self, at: float | None) -> None:
        self.at = at

    @classmethod
    def for_job(cls, job: dict[str, Any], received_at: float) -> Deadline:
        tokens = [
            (job.get("backend") or {}).get("password"),
            (job.get("cloud_credentials") or {}).get("oidc_token"),
        ]
        lifetimes = [
            token_lifetime(t) if isinstance(t, str) else None for t in tokens if t is not None
        ]
        resolved = [_fallback_lifetime(job) if life is None else life for life in lifetimes]
        return cls(received_at + min(resolved) if resolved else None)

    def left(self) -> float | None:
        """Seconds left before the deadline less the safety margin; None without a deadline."""
        if self.at is None:
            return None
        return self.at - SAFETY_MARGIN_SECONDS - time.monotonic()

    def timeout(self, own: int) -> int:
        """`own`, cut to what is left before the deadline — never below MIN_COMMAND_SECONDS."""
        left = self.left()
        if left is None:
            return own
        return min(own, max(MIN_COMMAND_SECONDS, int(left)))

    def too_short_for(self, budget: int) -> int | None:
        """The seconds left when they are too few to start a command that changes infra or state
        with this `budget` (MIN_APPLY_SECONDS, or the whole budget when shorter); else None."""
        left = self.left()
        if left is None or left >= min(budget, MIN_APPLY_SECONDS):
            return None
        return max(0, int(left))
