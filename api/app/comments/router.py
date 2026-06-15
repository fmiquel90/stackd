from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.comments.schemas import CommentCreate, CommentOut, CommentUpdate
from app.db import get_session
from app.enums import Role
from app.errors import ProblemException
from app.models.run import Run
from app.models.run_comment import RunComment

router = APIRouter(prefix="/api/v1/runs/{run_id}/comments", tags=["comments"])
DbSession = Annotated[AsyncSession, Depends(get_session)]

_APPROVERS = {Role.approver, Role.admin}


async def _publish(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Light WS fan-out so the run page's subscription invalidates its comment query (§5.3)."""
    signal = json.dumps({"kind": "comment_event", "run_id": str(run_id)})
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(chan=f"run_{run_id}", payload=signal)
    )


async def _get_comment(session: AsyncSession, run_id: uuid.UUID, cid: uuid.UUID) -> RunComment:
    c = await session.get(RunComment, cid)
    if c is None or c.run_id != run_id:
        raise ProblemException(404, "Comment not found", None)
    return c


@router.get("", response_model=list[CommentOut])
async def list_comments(run_id: uuid.UUID, _: CurrentUser, session: DbSession) -> list[CommentOut]:
    rows = (
        (
            await session.execute(
                select(RunComment)
                .where(RunComment.run_id == run_id)
                .order_by(RunComment.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [CommentOut.of(c) for c in rows]


@router.post("", response_model=CommentOut, status_code=201)
async def create_comment(
    run_id: uuid.UUID, body: CommentCreate, user: CurrentUser, session: DbSession
) -> CommentOut:
    if await session.get(Run, run_id) is None:
        raise ProblemException(404, "Run not found", None)
    if body.parent_id is not None:
        parent = await _get_comment(session, run_id, body.parent_id)  # must be on this run
        if parent.parent_id is not None:
            raise ProblemException(422, "Nested reply", "Reply to the root comment, not a reply.")
    comment = RunComment(
        run_id=run_id,
        parent_id=body.parent_id,
        author_user_id=user.id,
        author_email=user.email,
        body=body.body,
        anchor=body.anchor.model_dump(exclude_none=True) if body.anchor else None,
    )
    session.add(comment)
    await _publish(session, run_id)
    await session.commit()
    await session.refresh(comment)
    return CommentOut.of(comment)


@router.patch("/{cid}", response_model=CommentOut)
async def update_comment(
    run_id: uuid.UUID, cid: uuid.UUID, body: CommentUpdate, user: CurrentUser, session: DbSession
) -> CommentOut:
    comment = await _get_comment(session, run_id, cid)
    is_author = comment.author_user_id == user.id
    if body.body is not None:
        if not is_author:
            raise ProblemException(403, "Forbidden", "Only the author can edit a comment.")
        comment.body = body.body
        comment.edited_at = datetime.now(UTC)
    if body.resolved is not None:
        if not (is_author or user.role in _APPROVERS):
            raise ProblemException(403, "Forbidden", "Resolving needs the author or an approver.")
        if body.resolved:
            comment.resolved_at = datetime.now(UTC)
            comment.resolved_by_user_id = user.id
        else:
            comment.resolved_at = None
            comment.resolved_by_user_id = None
    await _publish(session, run_id)
    await session.commit()
    await session.refresh(comment)
    return CommentOut.of(comment)


@router.delete("/{cid}", status_code=204)
async def delete_comment(
    run_id: uuid.UUID, cid: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    comment = await _get_comment(session, run_id, cid)
    if comment.author_user_id != user.id and user.role != Role.admin:
        raise ProblemException(403, "Forbidden", "Only the author or an admin can delete.")
    await session.delete(comment)
    await _publish(session, run_id)
    await session.commit()
