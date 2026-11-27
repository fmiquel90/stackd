"""Out-of-band state changes (SPECS §19.7): an unmanaged-state environment's state written outside
Stackd between two runs. The agent reports the state's `serial` and `lineage` only — never anything
else of it — and the API compares them with the last pair it observed (the baseline)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.enums import AuditActorKind, RunType
from app.errors import ProblemException
from app.inbox.service import eligible_approver_ids, notify
from app.models.environment import Environment
from app.models.run import Run
from app.models.user import User
from app.runs.commands import is_mutating

# tofu/terraform write a UUID; anything shaped otherwise is not a lineage.
_LINEAGE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# Settings whose change points the environment at another state: the baseline no longer applies.
BACKEND_FIELDS = frozenset({"managed_state", "backend_config", "backend_config_file"})


def observes_state(run: Run, env: Environment) -> bool:
    """True when the run's job reads and reports the state's serial/lineage (`observe_state`,
    §7.2): unmanaged state, and reviewed code that already holds the credentials reading it — a
    tracked/destroy plan or apply, a drift plan, a mutating command. Never a pull request's
    proposed run (its code may point the backend elsewhere), never a read-only command. Managed
    state needs none: every write goes through the API's backend (§11.2)."""
    if env.managed_state:
        return False
    if run.type == RunType.proposed:
        return run.is_drift
    if run.type == RunType.command:
        return is_mutating((run.command or {}).get("name", ""))
    return True


def parse_observation(result: dict[str, Any]) -> tuple[int | None, str | None]:
    """The reported (serial, lineage): both set, or both null for an empty state. 400 otherwise."""
    serial, lineage = result.get("serial"), result.get("lineage")
    if serial is None and lineage is None:
        return None, None
    valid_serial = isinstance(serial, int) and not isinstance(serial, bool) and serial >= 0
    if not valid_serial or not isinstance(lineage, str) or not _LINEAGE.match(lineage):
        raise ProblemException(
            400,
            "Invalid state observation",
            "serial must be an integer >= 0 and lineage 1-64 chars of [A-Za-z0-9-], or both null.",
        )
    return serial, lineage


def is_flagged(env: Environment) -> bool:
    return env.out_of_band_detected_at is not None


def _clear_flag(env: Environment) -> None:
    env.out_of_band_detected_at = None
    env.out_of_band_run_id = None
    env.out_of_band_expected_serial = None
    env.out_of_band_expected_lineage = None
    env.out_of_band_observed_serial = None
    env.out_of_band_observed_lineage = None


async def record_observation(
    session: AsyncSession,
    env: Environment,
    run: Run,
    serial: int | None,
    lineage: str | None,
    *,
    baseline: bool,
    now: datetime,
) -> bool:
    """Apply one observation to the env (caller holds its row lock; same txn as the event). A
    `baseline` one is the state as a Stackd write left it: it becomes the baseline. A check that
    differs from the baseline is an out-of-band change: flagged, audited and notified on the edge
    into the flag, the baseline moved to what was observed. True when it flagged a change."""
    env.state_observed_at = now
    observed = (serial, lineage)
    known = (env.state_serial, env.state_lineage)
    if baseline or env.state_lineage is None or observed == known:
        env.state_serial, env.state_lineage = observed
        return False

    first = not is_flagged(env)
    if first:
        env.out_of_band_detected_at = now
        env.out_of_band_expected_serial, env.out_of_band_expected_lineage = known
    env.out_of_band_run_id = run.id
    env.out_of_band_observed_serial, env.out_of_band_observed_lineage = observed
    env.state_serial, env.state_lineage = observed
    if first:  # debounced while it stays flagged, like drift (§19.4)
        context = {
            "run_id": str(run.id),
            "tier": env.tier,
            "expected_serial": known[0],
            "observed_serial": serial,
            "lineage_changed": lineage != known[1],
        }
        await record_audit(
            session,
            action="environment.out_of_band_change_detected",
            actor_kind=AuditActorKind.system,
            target_kind="environment",
            target_id=env.id,
            context=context,
        )
        ctx = {
            "environment_id": str(env.id),
            "environment": env.name,
            "tier": env.tier,
            "expected_serial": known[0],
            "observed_serial": serial,
        }
        for uid in await eligible_approver_ids(session, env):
            await notify(session, uid, "out_of_band_change", run_id=run.id, context=ctx)
    return True


async def clear_on_apply(session: AsyncSession, env: Environment, run: Run) -> None:
    """A full apply finished (§19.7): its plan was computed on the changed state and reviewed, and
    tofu refuses a saved plan whose state moved since — the env is Stackd's again. The caller
    excludes a targeted apply."""
    if not is_flagged(env):
        return
    _clear_flag(env)
    await record_audit(
        session,
        action="environment.out_of_band_change_cleared",
        actor_kind=AuditActorKind.system,
        target_kind="environment",
        target_id=env.id,
        context={"reason": "applied", "run_id": str(run.id)},
    )


async def acknowledge(session: AsyncSession, env: Environment, user: User) -> None:
    """A human accepts the observed state (§19.7): the flag clears, the baseline (already the
    observed state) stays. 409 when nothing is flagged."""
    if not is_flagged(env):
        raise ProblemException(
            409, "Nothing to acknowledge", "No out-of-band state change is flagged on this env."
        )
    context = {
        "reason": "acknowledged",
        "run_id": str(env.out_of_band_run_id) if env.out_of_band_run_id else None,
        "expected_serial": env.out_of_band_expected_serial,
        "observed_serial": env.out_of_band_observed_serial,
    }
    _clear_flag(env)
    await record_audit(
        session,
        action="environment.out_of_band_change_cleared",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env.id,
        context=context,
    )


def reset_baseline(env: Environment) -> None:
    """The env now points at another state (a backend setting changed): forget the baseline and
    the flag; the next observation adopts the new state."""
    env.state_serial = None
    env.state_lineage = None
    env.state_observed_at = None
    _clear_flag(env)
