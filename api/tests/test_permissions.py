from __future__ import annotations

import uuid

from app.enums import Role
from app.models.environment import Environment
from app.models.space_membership import SpaceMembership
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


def _membership(role: Role, tiers: list[str], can_destroy: bool = False) -> SpaceMembership:
    return SpaceMembership(
        space_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=role,
        allowed_tiers=tiers,
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


def test_membership_overrides_instance_tiers() -> None:
    # Instance defaults allow everything; the space membership narrows to dev only (§6, Phase F).
    user = _user(Role.admin, ["dev", "staging", "prod"], can_destroy=True)
    space = _membership(Role.approver, ["dev"])
    assert can_apply(user, _env("dev"), space).allowed
    refused = can_apply(user, _env("prod"), space)
    assert not refused.allowed and "prod" in refused.reason


def test_membership_role_can_downgrade() -> None:
    # A reader membership cannot confirm even if the instance role is admin.
    user = _user(Role.admin, ["prod"], can_destroy=True)
    d = can_apply(user, _env("prod"), _membership(Role.reader, ["prod"]))
    assert not d.allowed and "approver" in d.reason


def test_membership_can_destroy_overrides_instance() -> None:
    user = _user(Role.admin, ["prod"], can_destroy=True)  # instance allows destroy
    space = _membership(Role.approver, ["prod"], can_destroy=False)  # space forbids it
    assert not can_apply(user, _env("prod"), space, is_destroy=True).allowed


def test_destroy_requires_can_destroy() -> None:
    base = _user(Role.admin, ["prod"], can_destroy=False)
    refused = can_apply(base, _env("prod"), is_destroy=True)
    assert not refused.allowed and "destroy" in refused.reason
    allowed = can_apply(
        _user(Role.admin, ["prod"], can_destroy=True), _env("prod"), is_destroy=True
    )
    assert allowed.allowed
