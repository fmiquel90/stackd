from __future__ import annotations

from dataclasses import dataclass

from app.enums import Role
from app.models.environment import Environment
from app.models.space_membership import SpaceMembership
from app.models.user import User

_APPROVER_ROLES = {Role.approver, Role.admin}


@dataclass(frozen=True)
class ApplyDecision:
    allowed: bool
    reason: str | None = None  # human-readable disabled reason for the UI (DESIGN §5.2)


def effective_role(user: User, membership: SpaceMembership | None) -> Role:
    return membership.role if membership is not None else user.role


def effective_allowed_tiers(user: User, membership: SpaceMembership | None) -> list[str]:
    tiers = membership.allowed_tiers if membership is not None else user.allowed_tiers
    return tiers or []


def effective_can_destroy(user: User, membership: SpaceMembership | None) -> bool:
    return membership.can_destroy if membership is not None else user.can_destroy


def can_apply(
    user: User,
    env: Environment,
    membership: SpaceMembership | None = None,
    *,
    is_destroy: bool = False,
) -> ApplyDecision:
    """Apply-confirmation gate (SPECS §2.4, §6 Phase F).

    `confirm` allowed iff effective role ∈ {approver, admin} AND env.tier ∈ effective allowed_tiers
    (set membership — tiers are non-ordered, so prod no longer implies everything). A `destroy` run
    additionally requires effective `can_destroy`. The *effective* permission is the space
    membership when present, else the user's instance defaults. This does NOT enforce 4-eyes
    (triggerer ≠ confirmer) — that lives in the run transition, since it needs the run's triggerer.
    """
    if effective_role(user, membership) not in _APPROVER_ROLES:
        return ApplyDecision(False, "approver role required")
    allowed = effective_allowed_tiers(user, membership)
    if not allowed:
        return ApplyDecision(False, "no allowed tiers — you cannot confirm applies")
    if env.tier not in allowed:
        return ApplyDecision(
            False,
            f"tier {env.tier} not in your allowed tiers ({', '.join(sorted(allowed))})",
        )
    if is_destroy and not effective_can_destroy(user, membership):
        return ApplyDecision(False, "destroy permission required")
    return ApplyDecision(True)
