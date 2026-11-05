"""Anti-replay for inbound GitHub webhooks (SPECS §13): one processing per `X-GitHub-Delivery` id
within the replay window."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhook_delivery import WebhookDelivery

REPLAY_WINDOW = timedelta(minutes=5)
# GitHub sends a GUID; anything much longer is not a GitHub delivery id.
MAX_DELIVERY_ID_LENGTH = 128


async def record_delivery(session: AsyncSession, delivery_id: str) -> bool:
    """Record `delivery_id` in the caller's transaction; False when it was already received within
    the replay window (a duplicate: do nothing). An id older than the window counts as new again,
    and its timestamp moves. Concurrent deliveries of one id serialize on its row: the second sees
    the first once it commits (a duplicate), or inserts if the first rolled back."""
    stmt = (
        pg_insert(WebhookDelivery)
        .values(delivery_id=delivery_id)
        .on_conflict_do_update(
            index_elements=[WebhookDelivery.delivery_id],
            set_={"received_at": func.now()},
            where=WebhookDelivery.received_at < func.now() - REPLAY_WINDOW,
        )
        .returning(WebhookDelivery.delivery_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def purge_webhook_deliveries(session: AsyncSession, now: datetime) -> int:
    """Delete the delivery ids past the replay window (daily retention task, §7.5): they no longer
    block anything."""
    result = await session.execute(
        delete(WebhookDelivery).where(WebhookDelivery.received_at < now - REPLAY_WINDOW)
    )
    await session.commit()
    return result.rowcount or 0  # type: ignore[attr-defined]
