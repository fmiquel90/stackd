"""Run artifacts in S3 (SPECS §11.3): the saved plan the apply job applies (§7.4), and the
errored state an apply (or a mutating command) could not persist (§7.3).

A plan file carries the run's variable values in clear — sensitive ones included — so it never
reaches S3 unencrypted: AES-256-GCM with the app key, the AAD binding the blob to its own key (a
blob copied under another key does not decrypt). It is served only to the run's apply job
(app.workers.worker_router) and deleted as soon as the run is terminal: transition() clears the
run's pointer in its transaction, and the object is deleted once that transaction commits.

Every upload gets a key of its own: the upload happens before the run's row lock is taken, so a
late upload refused under that lock must never have overwritten the reviewed plan's object."""

from __future__ import annotations

import asyncio
import uuid

from cryptography.exceptions import InvalidTag
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.crypto import decrypt_bytes, encrypt_bytes
from app.enums import RunType
from app.ids import uuid7
from app.logging import get_logger
from app.models.run import Run
from app.statebackend.store import delete_object, get_object, put_object

_log = get_logger("stackd.artifacts")

PLAN_FILE = "plan.tfplan"
# A plan file is a zip of the plan, the config snapshot and the prior state: generous for large
# states, while bounding what one upload holds in memory.
MAX_PLAN_BYTES = 64 * 1024 * 1024
# Run types whose plan can be applied, hence saved. A proposed run (PR plan, drift) never applies;
# a command run has no plan.
SAVED_PLAN_TYPES: frozenset[RunType] = frozenset({RunType.tracked, RunType.destroy})
# The state tofu could not write back (`errored.tfstate`): the only record of what the job changed.
# Encrypted like the plan (a state holds secrets) and, unlike it, kept once the run is terminal —
# recovering it is the operator's job (§7.3).
ERRORED_STATE_FILE = "errored.tfstate"
MAX_ERRORED_STATE_BYTES = MAX_PLAN_BYTES


class SavedPlanUnreadable(Exception):
    """The stored plan failed authenticated decryption (wrong key, tampered or truncated blob)."""


def plan_key(run_id: uuid.UUID) -> str:
    return f"artifacts/{run_id}/plan-{uuid7()}.tfplan"


def _aad(key: str) -> bytes:
    return f"stackd:artifact:{key}".encode()


async def save_plan(run_id: uuid.UUID, data: bytes) -> str:
    """Encrypt and store a plan file under a new key of the run's; returns that key. The run does
    not point to it until record_saved_plan()."""
    key = plan_key(run_id)
    blob = await asyncio.to_thread(encrypt_bytes, data, aad=_aad(key))
    await put_object(key, blob)
    return key


def record_saved_plan(session: AsyncSession, run: Run, key: str) -> None:
    """Point the run to its newly stored plan, in the caller's transaction; a plan it replaces (a
    retried upload) is deleted once that transaction commits."""
    if run.plan_artifact_key is not None and run.plan_artifact_key != key:
        session.info.setdefault(_PURGE, set()).add(run.plan_artifact_key)
    run.plan_artifact_key = key


async def drop_unrecorded_plan(key: str) -> None:
    """A plan stored by an upload refused afterwards: no run points to it."""
    await _purge([key])
async def save_errored_state(run_id: uuid.UUID, data: bytes) -> str:
    """Encrypt and store the state a job could not persist; returns its S3 key."""
    key = artifact_key(run_id, ERRORED_STATE_FILE)
    await put_object(key, encrypt_bytes(data, aad=_aad(key)))
    return key


async def load_plan(key: str) -> bytes | None:
    """The decrypted plan file, None if S3 says the object does not exist; SavedPlanUnreadable if
    it fails its integrity check; StoreUnavailable (503) if S3 does not answer — a transient error
    is never reported as a missing plan. Never falls back to anything."""
    blob = await get_object(key)
    if blob is None:
        return None
    try:
        return await asyncio.to_thread(decrypt_bytes, blob, aad=_aad(key))
    except InvalidTag as exc:
        raise SavedPlanUnreadable(key) from exc


# --- retention: delete the saved plan once the run is terminal ---------------------------------

_PURGE = "stackd.purge_artifacts"  # Session.info key: S3 keys to delete once the txn commits
_purges: set[asyncio.Task[None]] = set()


def discard_saved_plan(session: AsyncSession, run: Run) -> None:
    """Called by transition() on a terminal transition: a terminal run is never applied and its
    plan holds secrets. Clears the pointer in the caller's transaction; the object is deleted after
    it commits (a rolled-back transition keeps both)."""
    if run.plan_artifact_key is None:
        return
    session.info.setdefault(_PURGE, set()).add(run.plan_artifact_key)
    run.plan_artifact_key = None


@event.listens_for(Session, "after_commit")
def _purge_after_commit(session: Session) -> None:
    keys = session.info.pop(_PURGE, None)
    if not keys:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # a sync session outside the event loop: nothing in the API does this
        _log.warning("saved plan purge skipped", extra={"event": "artifact.purge_skipped"})
        return
    task = loop.create_task(_purge(sorted(keys)))
    _purges.add(task)
    task.add_done_callback(_purges.discard)


@event.listens_for(Session, "after_rollback")
def _forget_on_rollback(session: Session) -> None:
    session.info.pop(_PURGE, None)


async def _purge(keys: list[str]) -> None:
    for key in keys:
        try:
            await delete_object(key)
        except Exception as exc:  # best effort: the blob stays encrypted at rest
            _log.warning(
                "saved plan purge failed",
                extra={"event": "artifact.purge_failed", "key": key, "error": type(exc).__name__},
            )


async def wait_for_purges() -> None:
    """Await the in-flight post-commit deletions (tests; graceful shutdown)."""
    while _purges:
        await asyncio.gather(*list(_purges), return_exceptions=True)
