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

    `confirm` allowed iff role ∈ {approver, admin} AND max_apply_tier >= env.tier.
    A `destroy` run additionally requires `can_destroy`. This does NOT enforce 4-eyes
    (triggerer ≠ confirmer) — that lives in the run transition (Phase 2), since it needs
    the run's triggerer.
    """
    if user.role not in _APPROVER_ROLES:
        return ApplyDecision(False, "approver role required")
    if user.max_apply_tier is None:
        return ApplyDecision(False, "no apply tier — you cannot confirm applies")
    if user.max_apply_tier.rank < env.tier.rank:
        return ApplyDecision(
            False,
            f"tier {env.tier.value} required — your ceiling is {user.max_apply_tier.value}",
        )
    if is_destroy and not user.can_destroy:
        return ApplyDecision(False, "destroy permission required")
    return ApplyDecision(True)
