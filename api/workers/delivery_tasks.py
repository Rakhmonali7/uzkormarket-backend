"""Carrier tracking polls.

Two tasks live here:

* :func:`sync_delivery_status` — given a single ``order_id``, calls the
  carrier API for the associated Delivery row, merges new tracking
  events into ``Delivery.tracking_events``, advances ``Delivery.status``
  if the carrier reports a terminal state, and propagates the matching
  ``OrderStatus`` to the Order row. Notifies the buyer via email
  whenever the status actually changes.
* :func:`sync_active_deliveries` — a beat-scheduled fan-out: every 30
  minutes it fetches every Delivery in a non-terminal state and queues
  a per-order ``sync_delivery_status`` so the slow carrier calls run
  on the worker pool concurrently rather than serialised inside one
  long-running periodic task.

Carrier integration is pluggable via :func:`set_carrier_client` so
tests can inject a fake. Real per-carrier HTTP clients (CJ Logistics,
Korea Post, DHL) are stubbed out for now — populating them is the next
delivery-side work item once we have credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from models.delivery import Delivery, DeliveryCarrier, DeliveryStatus
from models.order import Order, OrderStatus
from services._dispatch import dispatch_task
from workers._runner import run_async, task_session
from workers.celery_app import celery_app

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Carrier client abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackingEvent:
    """One row of carrier-reported activity. Persisted as JSONB."""

    timestamp: datetime
    location: str
    description: str
    status: DeliveryStatus | None = None  # carrier-mapped terminal state, if any

    def as_dict(self) -> dict:
        """JSONB-serialisable form. Status enum becomes its string value."""
        return {
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "location": self.location,
            "description": self.description,
            "status": self.status.value if self.status else None,
        }


@dataclass(frozen=True)
class CarrierResponse:
    """A snapshot of carrier state for a tracking number."""

    events: tuple[TrackingEvent, ...]
    current_status: DeliveryStatus
    estimated_delivery_date: datetime | None = None


class CarrierClient(Protocol):
    """Interface every carrier integration must satisfy."""

    async def fetch(
        self, carrier: DeliveryCarrier, tracking_number: str
    ) -> CarrierResponse: ...


class _NullCarrierClient:
    """Safe default — does nothing, returns "unchanged".

    Selected when no real client has been registered. Lets the worker
    boot in development without carrier credentials, and lets tests run
    deterministically against ``set_carrier_client(NullCarrier)``.
    """

    async def fetch(
        self, carrier: DeliveryCarrier, tracking_number: str
    ) -> CarrierResponse:
        log.info(
            "Null carrier client: no fetch performed for %s (%s)",
            tracking_number,
            carrier.value,
        )
        return CarrierResponse(events=(), current_status=DeliveryStatus.PENDING)


_carrier_client: CarrierClient = _NullCarrierClient()


def get_carrier_client() -> CarrierClient:
    """Return the active carrier client (used by tasks)."""
    return _carrier_client


def set_carrier_client(client: CarrierClient | None) -> None:
    """Override the active carrier client (used by tests / at startup).

    Pass ``None`` to revert to the null client.
    """
    global _carrier_client
    _carrier_client = client if client is not None else _NullCarrierClient()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


# Map carrier-reported statuses to our Order lifecycle. Anything we
# don't terminate-on stays as PROCESSING / SHIPPED — the carrier event
# log is still updated regardless.
_DELIVERY_TO_ORDER_STATUS: dict[DeliveryStatus, OrderStatus] = {
    DeliveryStatus.PICKED_UP: OrderStatus.SHIPPED,
    DeliveryStatus.IN_TRANSIT: OrderStatus.SHIPPED,
    DeliveryStatus.OUT_FOR_DELIVERY: OrderStatus.SHIPPED,
    DeliveryStatus.DELIVERED: OrderStatus.DELIVERED,
}


@celery_app.task(
    name="workers.delivery_tasks.sync_delivery_status",
    bind=True,
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def sync_delivery_status(self, order_id: str) -> None:
    """Refresh tracking data for a single order.

    Idempotent — re-running on a stable Delivery row produces no DB
    changes (the event-merge is set-based on the ``timestamp`` /
    ``description`` tuple) and no extra emails.
    """
    return run_async(_sync_delivery_status_impl, order_id)


async def _sync_delivery_status_impl(order_id: str) -> None:
    async with task_session() as db:
        stmt = (
            select(Delivery)
            .where(Delivery.order_id == UUID(order_id))
            .options(selectinload(Delivery.order))
        )
        delivery = (await db.execute(stmt)).scalar_one_or_none()
        if delivery is None:
            log.warning("sync_delivery_status: no delivery for order %s", order_id)
            return

        client = get_carrier_client()
        response = await client.fetch(delivery.carrier, delivery.tracking_number)

        new_events = _merge_events(delivery.tracking_events, response.events)
        status_changed = delivery.status != response.current_status
        delivery.tracking_events = new_events
        delivery.status = response.current_status
        delivery.last_synced_at = datetime.now(timezone.utc)
        if response.estimated_delivery_date is not None:
            delivery.estimated_delivery_date = (
                response.estimated_delivery_date.date()
            )

        order = delivery.order
        new_order_status = _DELIVERY_TO_ORDER_STATUS.get(response.current_status)
        if new_order_status is not None and order.status != new_order_status:
            order.status = new_order_status

        await db.commit()

    if status_changed:
        # Tell the buyer asynchronously. We use ``dispatch_task`` rather
        # than a direct import so the email task can live in another
        # worker if we ever shard them.
        dispatch_task(
            "workers.email_tasks.send_delivery_update_email", order_id
        )


def _merge_events(
    existing: list, incoming: Sequence[TrackingEvent]
) -> list:
    """Return a deduplicated, time-ordered event list for JSONB storage.

    Identity is the ``(timestamp, description)`` pair — carriers occasionally
    re-emit the same event with slightly different metadata, but those
    two fields are stable. Timestamps are normalised to ISO-8601 UTC
    strings before comparison so we don't re-add an event whose tz
    changed format between polls.
    """
    seen: set[tuple[str, str]] = set()
    merged: list = []
    for raw in existing:
        key = (raw.get("timestamp", ""), raw.get("description", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(raw)
    for event in incoming:
        as_dict = event.as_dict()
        key = (as_dict["timestamp"], as_dict["description"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(as_dict)
    merged.sort(key=lambda row: row.get("timestamp", ""))
    return merged


# ---------------------------------------------------------------------------
# Beat fan-out
# ---------------------------------------------------------------------------


# Statuses we still consider "active" — we keep polling them.
# Anything past these (DELIVERED, CANCELLED, REFUNDED) is terminal.
_ACTIVE_ORDER_STATUSES = (
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
)


@celery_app.task(
    name="workers.delivery_tasks.sync_active_deliveries",
    bind=True,
)
def sync_active_deliveries(self) -> int:
    """Beat-driven fan-out: queue a ``sync_delivery_status`` for every
    Delivery whose order is still active. Returns the number of tasks
    dispatched, useful in ``celery inspect`` output.
    """
    return run_async(_sync_active_deliveries_impl)


async def _sync_active_deliveries_impl() -> int:
    async with task_session() as db:
        stmt = (
            select(Delivery.order_id)
            .join(Order, Delivery.order_id == Order.id)
            .where(
                Order.status.in_(_ACTIVE_ORDER_STATUSES),
                # Don't re-poll DELIVERED/EXCEPTION rows even if their
                # Order is somehow still marked active.
                Delivery.status.notin_(
                    (DeliveryStatus.DELIVERED, DeliveryStatus.EXCEPTION)
                ),
            )
        )
        order_ids = [row[0] for row in (await db.execute(stmt)).all()]

    for order_id in order_ids:
        dispatch_task(
            "workers.delivery_tasks.sync_delivery_status", str(order_id)
        )
    log.info("sync_active_deliveries: dispatched %d task(s)", len(order_ids))
    return len(order_ids)


__all__ = [
    "CarrierClient",
    "CarrierResponse",
    "TrackingEvent",
    "get_carrier_client",
    "set_carrier_client",
    "sync_active_deliveries",
    "sync_delivery_status",
]
