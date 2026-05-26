"""``/api/v1/orders`` — buyer-facing checkout, listing, cancel.

* ``POST /orders`` — checkout. Requires BUYER. Stock locking, single-seller
  enforcement, server-side pricing, FX conversion, and Payment row creation
  all live in :func:`services.order_service.create_order`.
* ``GET /orders`` and ``GET /orders/{id}`` — visible to BUYER (own only)
  and SELLER (own products' orders). The detail handler uses ``CurrentUser``
  rather than ``BuyerUser`` so sellers can hit it too — the service decides
  what the viewer is allowed to see.
* ``POST /orders/{id}/cancel`` — buyer-only, PENDING-only.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from dependencies.auth import BuyerUser, CurrentUser
from dependencies.db import DBSession
from models.order import OrderStatus
from schemas.common import Page, PageMeta, PageParams
from schemas.order import (
    OrderCreate,
    OrderItemPublic,
    OrderListItem,
    OrderPublic,
    ShippingAddress,
)
from services import order_service

router = APIRouter()


def _to_public(order) -> OrderPublic:
    """Map an ORM Order (with ``items`` loaded) to the public schema.

    Extracted because both ``create_order`` and ``read_order`` need it
    and inlining a 10-field ``OrderPublic(...)`` constructor twice is
    noise. ``shipping_address`` is stored as raw JSONB so we coerce
    through the schema to make sure the response shape matches.
    """
    return OrderPublic(
        id=order.id,
        buyer_id=order.buyer_id,
        seller_id=order.seller_id,
        status=order.status,
        total_amount_uzs=order.total_amount_uzs,
        total_amount_krw=order.total_amount_krw,
        payment_method=order.payment_method,
        payment_reference=order.payment_reference,
        shipping_address=ShippingAddress.model_validate(order.shipping_address),
        items=[OrderItemPublic.model_validate(i) for i in order.items],
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


@router.post(
    "",
    response_model=OrderPublic,
    summary="Place an order and initiate payment",
)
async def create_order(
    payload: OrderCreate,
    buyer: BuyerUser,
    db: DBSession,
) -> OrderPublic:
    """Run checkout. The service:

    * locks every product row ``FOR UPDATE`` to prevent oversell,
    * verifies all items share a single seller,
    * snapshots prices and decrements stock atomically,
    * inserts a PENDING ``Payment`` row alongside the Order,
    * enqueues the order-confirmation email via Celery.
    """
    order = await order_service.create_order(db, buyer, payload)
    return _to_public(order)


@router.get(
    "",
    response_model=Page[OrderListItem],
    summary="List the buyer's own orders",
)
async def list_orders(
    buyer: BuyerUser,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    status_filter: Annotated[
        OrderStatus | None,
        Query(alias="status", description="Filter by order status"),
    ] = None,
) -> Page[OrderListItem]:
    """Buyer-only: returns orders ``buyer_id == self.id``. Sellers use
    ``GET /sellers/me/orders`` for the seller-side equivalent."""
    items, total = await order_service.list_buyer_orders(
        db,
        buyer,
        page=page_params.page,
        page_size=page_params.page_size,
        status_filter=status_filter,
    )
    return Page[OrderListItem](
        items=[
            OrderListItem(
                id=o.id,
                seller_id=o.seller_id,
                status=o.status,
                total_amount_uzs=o.total_amount_uzs,
                total_amount_krw=o.total_amount_krw,
                payment_method=o.payment_method,
                item_count=len(o.items),
                created_at=o.created_at,
            )
            for o in items
        ],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.get(
    "/{order_id}",
    response_model=OrderPublic,
    summary="Get the full order detail",
)
async def read_order(
    order_id: UUID,
    viewer: CurrentUser,
    db: DBSession,
) -> OrderPublic:
    """Viewer-aware detail endpoint:
    * BUYER sees their own orders only.
    * SELLER sees orders against their products.
    * ADMIN sees any order (richer projection: ``GET /admin/orders/{id}``).
    Cross-tenant access returns 404 (never 403) to avoid leaking ids."""
    order = await order_service.get_order_for_viewer(db, viewer, order_id)
    return _to_public(order)


@router.post(
    "/{order_id}/cancel",
    response_model=OrderPublic,
    summary="Cancel a PENDING order",
)
async def cancel_order(
    order_id: UUID,
    buyer: BuyerUser,
    db: DBSession,
) -> OrderPublic:
    """Buyer-side cancellation — only allowed while the order is still
    PENDING. Stock is restored on every line item in the same transaction."""
    order = await order_service.cancel_order(db, buyer, order_id)
    return _to_public(order)
