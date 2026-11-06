"""Configuration export / import of a space (SPECS §22)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.space import Space
from app.portability.export import export_space
from app.portability.importer import Importer
from app.portability.schemas import ConfigDocument
from app.spaces import require_space_access

router = APIRouter(prefix="/api/v1/spaces", tags=["portability"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _space(session: AsyncSession, user: Any, space_id: uuid.UUID) -> Space:
    # The membership gate first: a non-member gets 403 whether the space exists or not.
    await require_space_access(session, user, space_id, min_role=Role.admin)
    space = await session.get(Space, space_id)
    if space is None:
        raise ProblemException(404, "Space not found", None)
    return space


def _attachment(space_name: str, stamp: str) -> dict[str, str]:
    """Content-Disposition for any space name: headers are Latin-1, and a name may hold anything
    (`"`, non-Latin scripts). An ASCII-safe `filename` (RFC 6266 fallback) + the exact name as
    `filename*` (RFC 5987, UTF-8 percent-encoded)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", space_name).strip("-") or "space"
    exact = quote(f"stackd-{space_name}-{stamp}.json", safe="")
    return {
        "Content-Disposition": (
            f"attachment; filename=\"stackd-{safe}-{stamp}.json\"; filename*=UTF-8''{exact}"
        )
    }


@router.get("/{space_id}/export")
async def export_config(space_id: uuid.UUID, user: CurrentUser, session: DbSession) -> JSONResponse:
    """The space's configuration as a `stackd.config` document (§22.2). Space admin: it is the
    whole configuration of the space in one file — never a secret value."""
    space = await _space(session, user, space_id)
    doc = await export_space(session, space)
    await record_audit(
        session,
        action="space.config_exported",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space.id,
        context={
            "stacks": len(doc.stacks),
            "environments": sum(len(s.environments) for s in doc.stacks),
            "variable_sets": len(doc.variable_sets),
        },
    )
    await session.commit()
    stamp = doc.exported_at.strftime("%Y%m%dT%H%M%SZ") if doc.exported_at else "export"
    return JSONResponse(doc.model_dump(mode="json"), headers=_attachment(space.name, stamp))


@router.post("/{space_id}/import")
async def import_config(
    space_id: uuid.UUID,
    body: ConfigDocument,
    user: CurrentUser,
    session: DbSession,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge a `stackd.config` document into the space (§22.3): create what is missing, update
    what differs, never delete. All or nothing; `?dry_run=true` returns the same report and
    rolls back."""
    space = await _space(session, user, space_id)
    try:
        report = await Importer(session, user, space, body, dry_run=dry_run).run()
    except IntegrityError as exc:
        await session.rollback()
        raise ProblemException(
            409,
            "Conflicting change",
            "Something the import writes changed concurrently; nothing was imported. Retry.",
        ) from exc
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report
