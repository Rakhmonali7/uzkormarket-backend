"""``/api/v1/delivery`` — read-only carrier tracking lookup.

Single endpoint by design: dispatch / tracking-event ingestion is
worker-side (step 12's ``sync_delivery_status`` task), and the public
write surface for sellers (creating a Delivery row when they ship) will
land in a later iteration.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from dependencies.auth import CurrentUser
from dependencies.db import DBSession
from schemas.delivery import DeliveryPublic
from services import delivery_service

router = APIRouter()


@router.get(
    "/{order_id}",
    response_model=DeliveryPublic,
    summary="Get delivery / tracking detail for an order",
)
async def read_delivery(
    order_id: UUID,
    viewer: CurrentUser,
    db: DBSession,
) -> DeliveryPublic:
    """Visibility mirrors ``GET /orders/{id}``: buyers see their own,
    sellers see orders against their products, admins see anything.
    404s when the order doesn't belong to the viewer **or** when no
    Delivery row has been registered yet (the seller hasn't dispatched)."""
    delivery = await delivery_service.get_delivery_for_viewer(
        db, viewer, order_id
    )
    return DeliveryPublic.model_validate(delivery)
