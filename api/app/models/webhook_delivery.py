from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WebhookDelivery(Base):
    """An authenticated GitHub delivery already processed — anti-replay (SPECS §13): a delivery
    whose `X-GitHub-Delivery` id was received within the replay window is not acted on again.
    Rows past the window are purged by the daily retention task (§7.5)."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
