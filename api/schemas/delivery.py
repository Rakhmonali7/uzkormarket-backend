"""Delivery / tracking schemas.

Carrier-side tracking events are persisted as JSONB on the ``deliveries``
table; this module gives them a typed shape both for ingest (when the
sync_delivery_status Celery task pushes new rows) and for the public
``GET /delivery/{order_id}`` response.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from models.delivery import DeliveryCarrier, DeliveryStatus
from schemas.common import APIModel


class TrackingEvent(APIModel):
    """One row of carrier-side scan history.

    ``raw_status`` carries whatever string the carrier returned (e.g.
    ``"in_transit_to_inco"``) so we can audit our own status mapping later
    without re-fetching from the carrier API.
    """

    timestamp: datetime
    location: str | None = Field(default=None, max_length=255)
    description: str = Field(min_length=1, max_length=500)
    raw_status: str | None = Field(
        default=None,
        max_length=80,
        description="Carrier-native status code, preserved verbatim for audit.",
    )


class DeliveryPublic(APIModel):
    """``GET /delivery/{order_id}`` response."""

    id: UUID
    order_id: UUID
    tracking_number: str
    carrier: DeliveryCarrier
    status: DeliveryStatus
    estimated_delivery_date: date | None
    last_synced_at: datetime | None
    tracking_events: list[TrackingEvent]
    created_at: datetime
    updated_at: datetime
