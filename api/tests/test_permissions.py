from __future__ import annotations

from app.enums import Role, Tier
from app.models.environment import Environment
from app.models.user import User
from app.permissions import can_apply


def _user(role: Role, tier: Tier | None, can_destroy: bool = False) -> User:
    return User(
        google_sub="x",
        email="x@dev.local",
        role=role,
        max_apply_tier=tier,
        can_destroy=can_destroy,
    )


def _env(tier: Tier) -> Environment:
    return Environment(name="e", tier=tier, branch="main")


def test_approver_prod_can_apply_prod() -> None:
    assert can_apply(_user(Role.approver, Tier.prod), _env(Tier.prod)).allowed


def test_writer_cannot_confirm() -> None:
    d = can_apply(_user(Role.writer, Tier.prod), _env(Tier.dev))
    assert not d.allowed and "approver" in d.reason


def test_apply_everywhere_except_prod() -> None:
    # Bob: staging ceiling — ok on dev/staging, refused on prod (§2.4).
    bob = _user(Role.approver, Tier.staging)
    assert can_apply(bob, _env(Tier.dev)).allowed
    assert can_apply(bob, _env(Tier.staging)).allowed
    refused = can_apply(bob, _env(Tier.prod))
    assert not refused.allowed and "tier prod" in refused.reason


def test_no_tier_cannot_apply() -> None:
    d = can_apply(_user(Role.approver, None), _env(Tier.dev))
    assert not d.allowed


def test_destroy_requires_can_destroy() -> None:
    admin = _user(Role.admin, Tier.prod, can_destroy=False)
    refused = can_apply(admin, _env(Tier.prod), is_destroy=True)
    assert not refused.allowed and "destroy" in refused.reason
    allowed = can_apply(
        _user(Role.admin, Tier.prod, can_destroy=True), _env(Tier.prod), is_destroy=True
    )
    assert allowed.allowed
