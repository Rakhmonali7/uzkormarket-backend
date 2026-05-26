"""Order service — checkout, listing, cancellation.

Checkout is the sharpest path in the codebase. The careful bits:

* **Single-seller orders.** The data model has one ``seller_id`` per
  order (rule). Mixed-cart checkouts must be split client-side; the
  service layer enforces this with a 422.
* **Stock locking.** Products referenced by the cart are SELECTed
  ``FOR UPDATE`` inside the checkout transaction so two concurrent
  buyers can't both decrement the same stock unit to zero. The lock is
  released when the surrounding transaction commits or rolls back.
* **Server-side pricing.** :class:`schemas.order.OrderCreate` is
  deliberately devoid of price fields — we read live ``price_krw`` and
  ``price_uzs`` from each Product row at checkout time and compute the
  totals ourselves. Clients never get to dictate amounts.
* **FX consistency.** A single exchange rate is fetched once at the
  start of checkout and used for every line item and the payment row.
* **Cancellation reverses stock.** Buyers can cancel only while the
  order is still PENDING (i.e. unpaid); we restore stock on the
  affected products in the same transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.order import Order, OrderStatus
from models.order_item import OrderItem
from models.product import Product
from models.seller import Seller
from models.user import User, UserRole
from schemas.order import OrderCreate, OrderItemCreate
from services._dispatch import dispatch_task
from services._pagination import paginate
from services.payment_service import (
    convert_krw_to_uzs,
    create_pending_payment,
    get_current_exchange_rate,
    parse_gateway,
)


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


async def create_order(
    db: AsyncSession,
    buyer: User,
    payload: OrderCreate,
) -> Order:
    """Run the full checkout transaction.

    Steps:
      1. Validate the cart shape (gateway is real, no duplicate products).
      2. ``SELECT ... FOR UPDATE`` every product in one shot so a single
         lock acquisition covers the whole cart and we keep the lock-order
         deterministic (sorted by id) — this avoids deadlocks against a
         concurrent checkout for the same cart.
      3. Confirm every product is approved + active + has enough stock,
         and that they all share a single seller.
      4. Decrement stock, build OrderItems with snapshotted prices,
         insert the Order + Payment, commit once.
    """
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Order must contain at least one item.",
        )

    cart = _consolidate_cart(payload.items)
    gateway = parse_gateway(payload.payment_method)
    rate = await get_current_exchange_rate()

    # Sorted lookup keeps lock acquisition order stable across concurrent
    # checkouts that touch overlapping product sets — eliminates a class
    # of cross-checkout deadlocks. PostgreSQL row-locks honour the order
    # rows are returned, so ORDER BY id is sufficient.
    product_ids = sorted(cart.keys())
    stmt = (
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
    )
    products: Sequence[Product] = (await db.execute(stmt)).scalars().all()

    if len(products) != len(product_ids):
        # At least one referenced product doesn't exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more products in the order were not found.",
        )

    seller_ids = {p.seller_id for p in products}
    if len(seller_ids) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="All items in an order must come from a single seller.",
        )
    seller_id = seller_ids.pop()

    items: list[OrderItem] = []
    total_krw = Decimal("0")
    total_uzs = Decimal("0")

    for product in products:
        qty = cart[product.id]
        if not (product.is_approved and product.is_active):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product '{product.title}' is not currently available.",
            )
        if product.stock_quantity < qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Insufficient stock for '{product.title}' "
                    f"(requested {qty}, available {product.stock_quantity})."
                ),
            )

        # Snapshot the price at this exact rate so the order_item rows
        # remain accurate even if the rate or product price moves later.
        unit_uzs = convert_krw_to_uzs(product.price_krw, rate)

        items.append(
            OrderItem(
                product_id=product.id,
                quantity=qty,
                unit_price_krw=product.price_krw,
                unit_price_uzs=unit_uzs,
            )
        )
        total_krw += product.price_krw * qty
        total_uzs += unit_uzs * qty
        product.stock_quantity -= qty

    order = Order(
        buyer_id=buyer.id,
        seller_id=seller_id,
        status=OrderStatus.PENDING,
        total_amount_krw=total_krw,
        total_amount_uzs=total_uzs,
        shipping_address=payload.shipping_address.model_dump(),
        payment_method=gateway.value,
        items=items,
    )
    db.add(order)
    # ``flush`` so the order has an id before we attach the payment row.
    await db.flush()

    await create_pending_payment(
        db,
        order_id=order.id,
        gateway=gateway,
        amount_uzs=total_uzs,
        amount_krw=total_krw,
        exchange_rate_used=rate,
    )

    await db.commit()
    await db.refresh(order, attribute_names=["items"])

    dispatch_task(
        "workers.email_tasks.send_order_confirmation_email", str(order.id)
    )
    return order


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_buyer_orders(
    db: AsyncSession,
    buyer: User,
    *,
    page: int,
    page_size: int,
    status_filter: OrderStatus | None = None,
) -> tuple[Sequence[Order], int]:
    """Paginated listing for ``GET /orders`` — only orders this user placed.

    ``items`` is eager-loaded so the response shape's ``item_count`` is
    a simple ``len()`` in the route handler. With ``page_size`` capped at
    100 and the typical cart at 1-10 items, the IN-query load is well
    under a thousand rows per request.
    """
    stmt = (
        select(Order)
        .where(Order.buyer_id == buyer.id)
        .order_by(Order.created_at.desc())
        .options(selectinload(Order.items))
    )
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    return await paginate(db, stmt, page=page, page_size=page_size)


async def get_order_for_viewer(
    db: AsyncSession, viewer: User, order_id: UUID
) -> Order:
    """Single-order detail visible to ``viewer``.

    Visibility rules:
      * BUYER: only their own orders.
      * SELLER: only orders against their seller record.
      * ADMIN: any order (use :func:`services.admin_service.get_order_for_admin`
        for a richer projection).

    A buyer requesting a seller's order id and vice-versa both 404 (not
    403) so we don't leak the existence of unrelated orders.
    """
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.items),
            selectinload(Order.delivery),
        )
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )

    if viewer.role == UserRole.BUYER and order.buyer_id != viewer.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )
    if viewer.role == UserRole.SELLER:
        # Async SQLAlchemy can't lazy-load ``viewer.seller`` here, and the
        # generic CurrentUser dependency doesn't eager-load it. Resolve
        # the seller id with one explicit query keyed off the user.
        seller_stmt = select(Seller.id).where(Seller.user_id == viewer.id)
        seller_id = (await db.execute(seller_stmt)).scalar_one_or_none()
        if seller_id is None or order.seller_id != seller_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found.",
            )
    return order


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def cancel_order(
    db: AsyncSession, buyer: User, order_id: UUID
) -> Order:
    """Buyer-side cancellation — only allowed while the order is still PENDING.

    Restores stock on every line item in the same transaction so an
    eventual reconciliation pass would find the totals consistent.
    Statuses past PENDING (PAID / PROCESSING / SHIPPED / DELIVERED) require
    going through the refund flow instead.
    """
    stmt = (
        select(Order)
        .where(Order.id == order_id, Order.buyer_id == buyer.id)
        .options(selectinload(Order.items))
        .with_for_update()
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )

    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot cancel an order in status {order.status.value}; "
                "only PENDING orders can be cancelled."
            ),
        )

    # Re-add stock. Lock the products in id order to match checkout's lock
    # ordering and prevent a checkout vs cancel deadlock.
    product_ids = sorted({item.product_id for item in order.items})
    products_stmt = (
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
    )
    products = {
        p.id: p for p in (await db.execute(products_stmt)).scalars().all()
    }
    for item in order.items:
        product = products.get(item.product_id)
        if product is not None:
            product.stock_quantity += item.quantity

    order.status = OrderStatus.CANCELLED
    order.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return order


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _consolidate_cart(items: list[OrderItemCreate]) -> dict[UUID, int]:
    """Collapse duplicate product entries into a single quantity per id.

    Frontends sometimes submit ``[{p1,2},{p1,3}]`` instead of ``[{p1,5}]``.
    Both must produce the same OrderItem and the same stock decrement.
    """
    consolidated: dict[UUID, int] = {}
    for item in items:
        consolidated[item.product_id] = (
            consolidated.get(item.product_id, 0) + item.quantity
        )
    return consolidated
