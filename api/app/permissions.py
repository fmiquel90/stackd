from __future__ import annotations

from dataclasses import dataclass

from app.enums import Role
from app.models.environment import Environment
from app.models.user import User

_APPROVER_ROLES = {Role.approver, Role.admin}


@dataclass(frozen=True)
class ApplyDecision:
    allowed: bool
    reason: str | None = None  # human-readable disabled reason for the UI (DESIGN §5.2)


def can_apply(user: User, env: Environment, *, is_destroy: bool = False) -> ApplyDecision:
    """Apply-confirmation gate (SPECS §2.4).

    `confirm` allowed iff role ∈ {approver, admin} AND env.tier ∈ user.allowed_tiers (set
    membership — tiers are non-ordered, so prod no longer implies everything). A `destroy` run
    additionally requires `can_destroy`. This does NOT enforce 4-eyes (triggerer ≠ confirmer) —
    that lives in the run transition (Phase 2), since it needs the run's triggerer.
    """
    if user.role not in _APPROVER_ROLES:
        return ApplyDecision(False, "approver role required")
    allowed = user.allowed_tiers or []
    if not allowed:
        return ApplyDecision(False, "no allowed tiers — you cannot confirm applies")
    if env.tier not in allowed:
        return ApplyDecision(
            False,
            f"tier {env.tier} not in your allowed tiers ({', '.join(sorted(allowed))})",
        )
    if is_destroy and not user.can_destroy:
        return ApplyDecision(False, "destroy permission required")
    return ApplyDecision(True)
