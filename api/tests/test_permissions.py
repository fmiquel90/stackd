from __future__ import annotations

from app.enums import Role
from app.models.environment import Environment
from app.models.user import User
from app.permissions import can_apply


def _user(role: Role, allowed_tiers: list[str], can_destroy: bool = False) -> User:
    return User(
        google_sub="x",
        email="x@dev.local",
        role=role,
        allowed_tiers=allowed_tiers,
        can_destroy=can_destroy,
    )


def _env(tier: str) -> Environment:
    return Environment(name="e", tier=tier, branch="main")


def test_approver_can_apply_allowed_tier() -> None:
    assert can_apply(_user(Role.approver, ["prod"]), _env("prod")).allowed


def test_writer_cannot_confirm() -> None:
    d = can_apply(_user(Role.writer, ["prod"]), _env("dev"))
    assert not d.allowed and "approver" in d.reason


def test_membership_not_linear() -> None:
    # Non-contiguous set: dev + prod but NOT staging — the whole point of the new model.
    u = _user(Role.approver, ["dev", "prod"])
    assert can_apply(u, _env("dev")).allowed
    assert can_apply(u, _env("prod")).allowed
    refused = can_apply(u, _env("staging"))
    assert not refused.allowed and "staging" in refused.reason


def test_no_tiers_cannot_apply() -> None:
    d = can_apply(_user(Role.approver, []), _env("dev"))
    assert not d.allowed


def test_custom_tier() -> None:
    assert can_apply(_user(Role.approver, ["qa"]), _env("qa")).allowed


def test_destroy_requires_can_destroy() -> None:
    base = _user(Role.admin, ["prod"], can_destroy=False)
    refused = can_apply(base, _env("prod"), is_destroy=True)
    assert not refused.allowed and "destroy" in refused.reason
    allowed = can_apply(
        _user(Role.admin, ["prod"], can_destroy=True), _env("prod"), is_destroy=True
    )
    assert allowed.allowed
