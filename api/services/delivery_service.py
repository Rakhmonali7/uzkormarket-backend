"""Delivery service — read-only tracking lookups + Celery-driven sync hooks.

Most of the delivery lifecycle (creating Delivery rows when sellers
dispatch, polling carrier APIs, updating tracking events) lives in
:mod:`workers.delivery_tasks` (step 12). This module covers the
read-only API surface that route handlers wire to today plus a thin
helper the worker uses to upsert tracking events.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.delivery import Delivery
from models.order import Order
from models.seller import Seller
from models.user import User, UserRole


async def get_delivery_for_viewer(
    db: AsyncSession,
    viewer: User,
    order_id: UUID,
) -> Delivery:
    """Return the Delivery for ``order_id`` if ``viewer`` is allowed to see it.

    Visibility mirrors :func:`services.order_service.get_order_for_viewer`:
    buyers see their own orders, sellers see orders against their products,
    admins see anything. Both the order existence check and the visibility
    check collapse to 404 to avoid leaking ids by probing.

    A 404 is also raised when an order exists but no Delivery has been
    attached yet (e.g. the seller hasn't dispatched). The frontend
    distinguishes "no delivery yet" from "order doesn't exist" by first
    fetching ``GET /orders/{id}``.
    """
    stmt = select(Order).where(Order.id == order_id)
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Delivery not found.",
        )

    if viewer.role == UserRole.BUYER and order.buyer_id != viewer.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Delivery not found.",
        )
    if viewer.role == UserRole.SELLER:
        seller_id = (
            await db.execute(select(Seller.id).where(Seller.user_id == viewer.id))
        ).scalar_one_or_none()
        if seller_id is None or order.seller_id != seller_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Delivery not found.",
            )
    # ADMIN passes through unchecked.

    delivery = (
        await db.execute(select(Delivery).where(Delivery.order_id == order_id))
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No delivery has been registered for this order yet.",
        )
    return delivery
