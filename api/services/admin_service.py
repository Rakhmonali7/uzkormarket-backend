"""Admin service — moderation, user management, analytics.

Backs the entire ``/api/v1/admin/*`` surface. The route handlers (step 11)
already have the ``AdminUser`` role gate applied via dependency, so every
function here can assume an authorised admin caller and focus purely on
business logic.

Sections (one block per ``/admin/...`` resource):

  * Sellers — list, detail, document presign, approve, reject.
  * Users — list, detail, ban (which also revokes refresh tokens).
  * Products — list (incl. unapproved), approve, reject.
  * Orders — list, detail.
  * Analytics — :func:`analytics_summary` over a configurable window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core import storage
from core.security import get_refresh_token_store
from models.document import SellerDocument
from models.order import Order, OrderStatus
from models.product import Product
from models.seller import Seller, SellerApprovalStatus
from models.user import User, UserRole
from services._dispatch import dispatch_task
from services._pagination import paginate

# Statuses that count toward GMV. We exclude PENDING (no money) and the
# CANCELLED / REFUNDED tail (money was returned).
_GMV_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)


# ---------------------------------------------------------------------------
# Sellers
# ---------------------------------------------------------------------------


async def list_sellers(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    approval_status: SellerApprovalStatus | None = None,
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> tuple[Sequence[Seller], int]:
    """Paginated seller list with the filters the admin dashboard needs.

    ``search`` matches against business name, BRN, or the linked user's
    email — admins type whichever they remember. The ``selectinload`` on
    the user relationship keeps the response shape population to a
    single round-trip per page.
    """
    stmt = (
        select(Seller)
        .options(selectinload(Seller.user))
        .order_by(Seller.created_at.desc())
    )
    if approval_status is not None:
        stmt = stmt.where(Seller.approval_status == approval_status)
    if created_from is not None:
        stmt = stmt.where(Seller.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Seller.created_at <= created_to)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.join(User, Seller.user_id == User.id).where(
            or_(
                Seller.business_name.ilike(pattern),
                Seller.business_registration_number.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    return await paginate(db, stmt, page=page, page_size=page_size)


async def get_seller_detail(db: AsyncSession, seller_id: UUID) -> Seller:
    """Single seller with user + documents eager-loaded for ``GET /admin/sellers/{id}``."""
    stmt = (
        select(Seller)
        .where(Seller.id == seller_id)
        .options(
            selectinload(Seller.user),
            selectinload(Seller.documents),
        )
    )
    seller = (await db.execute(stmt)).scalar_one_or_none()
    if seller is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seller not found.",
        )
    return seller


async def create_document_view_url(
    db: AsyncSession,
    seller_id: UUID,
    document_id: UUID,
) -> tuple[str, int]:
    """Mint a short-lived presigned GET URL for an admin to view a document.

    Documents are never public — every read goes through this function,
    which validates that ``document_id`` belongs to ``seller_id`` (so a
    crafted URL can't disclose another seller's docs even with a valid
    admin session).
    """
    stmt = select(SellerDocument).where(
        SellerDocument.id == document_id,
        SellerDocument.seller_id == seller_id,
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    url = storage.create_presigned_get_url("seller-documents", document.file_key)
    from core.config import settings  # local import; keeps top-level small

    return url, settings.MINIO_PRESIGNED_GET_TTL_SECONDS


async def approve_seller(
    db: AsyncSession, admin: User, seller_id: UUID
) -> Seller:
    """Move a seller from PENDING to APPROVED.

    Idempotent on already-APPROVED sellers (no-op, no extra email).
    Rejects on REJECTED sellers — admins must explicitly re-pending one
    via a separate workflow if we ever need that.
    """
    seller = await get_seller_detail(db, seller_id)
    if seller.approval_status == SellerApprovalStatus.APPROVED:
        return seller
    if seller.approval_status == SellerApprovalStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seller has been rejected; cannot approve directly.",
        )

    seller.approval_status = SellerApprovalStatus.APPROVED
    seller.approved_by = admin.id
    seller.approved_at = datetime.now(timezone.utc)
    seller.rejection_reason = None
    await db.commit()
    await db.refresh(seller, attribute_names=["user", "documents"])

    dispatch_task(
        "workers.email_tasks.send_seller_approval_email", str(seller.id)
    )
    return seller


async def reject_seller(
    db: AsyncSession,
    admin: User,
    seller_id: UUID,
    reason: str,
) -> Seller:
    """Move a seller to REJECTED with a stored reason; emails the seller via Celery."""
    seller = await get_seller_detail(db, seller_id)
    if seller.approval_status == SellerApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seller is already approved; reject is not applicable.",
        )

    seller.approval_status = SellerApprovalStatus.REJECTED
    seller.rejection_reason = reason
    seller.approved_by = admin.id  # Audit who acted; column is "acted_by" semantically.
    seller.approved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(seller, attribute_names=["user", "documents"])

    dispatch_task(
        "workers.email_tasks.send_seller_rejection_email",
        str(seller.id),
        reason,
    )
    return seller


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def list_users(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    role: UserRole | None = UserRole.BUYER,
    is_active: bool | None = None,
    search: str | None = None,
) -> tuple[Sequence[User], int]:
    """Paginated user list. Defaults to BUYER-only per the project rule
    (``GET /admin/users`` lists buyer accounts); admins can pass an
    explicit ``role`` to widen the scope."""
    stmt = select(User).order_by(User.created_at.desc())
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(User.email.ilike(pattern), User.full_name.ilike(pattern))
        )
    return await paginate(db, stmt, page=page, page_size=page_size)


async def get_user_detail(
    db: AsyncSession, user_id: UUID
) -> tuple[User, int, datetime | None]:
    """Return ``(user, order_count, last_order_at)`` for ``GET /admin/users/{id}``.

    The aggregates are computed in one round-trip via correlated
    subqueries rather than fetching every order — admin dashboards open
    user details often and the orders list is paginated separately.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    agg_stmt = select(
        func.count(Order.id),
        func.max(Order.created_at),
    ).where(Order.buyer_id == user_id)
    order_count, last_order_at = (await db.execute(agg_stmt)).one()
    return user, int(order_count or 0), last_order_at


async def ban_user(
    db: AsyncSession, user_id: UUID, reason: str
) -> User:
    """Disable an account and revoke every active refresh token for it.

    The refresh-token revoke is best-effort — if Redis is briefly down
    the database mutation still commits and the user is locked out at
    the access-token expiry boundary. Belt and braces.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    if user.role == UserRole.ADMIN:
        # Soft guard against accidental self/peer-banning. Privileged
        # account management should go through a separate audit trail.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admin accounts cannot be banned through this endpoint.",
        )

    user.is_active = False
    await db.commit()

    store = get_refresh_token_store()
    try:
        await store.revoke_all_for_user(user.id)
    except Exception:  # noqa: BLE001 — Redis outage shouldn't block the ban
        # The DB-level is_active=False is sufficient for the access path
        # (every protected endpoint re-checks ``user.is_active``).
        pass

    # ``reason`` is currently kept only in the audit log path (Celery email
    # to the user); we don't have a dedicated user_bans table yet.
    dispatch_task(
        "workers.notification_tasks.notify_user_banned", str(user.id), reason
    )

    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


async def list_all_products(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    is_approved: bool | None = None,
    is_active: bool | None = None,
    seller_id: UUID | None = None,
    search: str | None = None,
) -> tuple[Sequence[Product], int]:
    """Admin-side product list — sees everything, including pending/rejected."""
    stmt = select(Product).order_by(Product.created_at.desc())
    if is_approved is not None:
        stmt = stmt.where(Product.is_approved == is_approved)
    if is_active is not None:
        stmt = stmt.where(Product.is_active == is_active)
    if seller_id is not None:
        stmt = stmt.where(Product.seller_id == seller_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(Product.title.ilike(pattern), Product.description.ilike(pattern))
        )
    return await paginate(db, stmt, page=page, page_size=page_size)


async def approve_product(
    db: AsyncSession, admin: User, product_id: UUID
) -> Product:
    """Flip a product to ``is_approved=True`` so it surfaces in the public catalogue."""
    product = await _get_product_for_admin(db, product_id)
    if product.is_approved:
        return product

    product.is_approved = True
    product.approved_by = admin.id
    product.approved_at = datetime.now(timezone.utc)
    product.rejection_reason = None
    await db.commit()
    await db.refresh(product)
    return product


async def reject_product(
    db: AsyncSession,
    admin: User,
    product_id: UUID,
    reason: str,
) -> Product:
    """Mark a product as rejected with the supplied reason. Sellers see
    this on their dashboard (:class:`schemas.product.ProductSellerView`)."""
    product = await _get_product_for_admin(db, product_id)
    product.is_approved = False
    product.rejection_reason = reason
    product.approved_by = admin.id
    product.approved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(product)
    return product


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


async def list_all_orders(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    status_filter: OrderStatus | None = None,
    seller_id: UUID | None = None,
    buyer_id: UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> tuple[Sequence[Order], int]:
    """Admin order feed used by ``GET /admin/orders``.

    ``items`` eager-loaded so :class:`schemas.admin.OrderAdminListItem`'s
    ``item_count`` resolves via ``len(order.items)`` in the route handler.
    """
    stmt = (
        select(Order)
        .order_by(Order.created_at.desc())
        .options(selectinload(Order.items))
    )
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    if seller_id is not None:
        stmt = stmt.where(Order.seller_id == seller_id)
    if buyer_id is not None:
        stmt = stmt.where(Order.buyer_id == buyer_id)
    if created_from is not None:
        stmt = stmt.where(Order.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Order.created_at <= created_to)
    return await paginate(db, stmt, page=page, page_size=page_size)


async def get_order_for_admin(db: AsyncSession, order_id: UUID) -> Order:
    """Full order detail with items and delivery loaded — ``GET /admin/orders/{id}``."""
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.items),
            selectinload(Order.delivery),
            selectinload(Order.payments),
        )
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )
    return order


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def analytics_summary(
    db: AsyncSession,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    """Compute ``GET /admin/analytics/summary``.

    Default window is the last 30 days (rule). GMV counts only orders in
    a "money has changed hands" status — see :data:`_GMV_STATUSES`. The
    aggregates run as separate single-row queries; for a few-tens-of-rows
    response this is simpler than a single CTE-stitched query and the
    cost difference is negligible.
    """
    today = date.today()
    end = period_end or today
    start = period_start or (today - timedelta(days=30))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="period_start must be on or before period_end.",
        )

    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    # +1 day so the upper bound is exclusive in datetime terms.
    end_dt = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )

    gmv_stmt = select(
        func.coalesce(func.sum(Order.total_amount_uzs), Decimal("0")),
        func.coalesce(func.sum(Order.total_amount_krw), Decimal("0")),
        func.count(Order.id),
    ).where(
        and_(
            Order.status.in_(_GMV_STATUSES),
            Order.created_at >= start_dt,
            Order.created_at < end_dt,
        )
    )
    gmv_uzs, gmv_krw, order_count = (await db.execute(gmv_stmt)).one()

    new_users_stmt = select(func.count(User.id)).where(
        and_(
            User.role == UserRole.BUYER,
            User.created_at >= start_dt,
            User.created_at < end_dt,
        )
    )
    new_users_count = (await db.execute(new_users_stmt)).scalar_one()

    # Pending sellers is a point-in-time number, not windowed — admins
    # care about "what's queued right now", regardless of when it landed.
    pending_sellers_stmt = select(func.count(Seller.id)).where(
        Seller.approval_status == SellerApprovalStatus.PENDING
    )
    pending_sellers_count = (await db.execute(pending_sellers_stmt)).scalar_one()

    return {
        "period_start": start,
        "period_end": end,
        "gmv_uzs": Decimal(gmv_uzs),
        "gmv_krw": Decimal(gmv_krw),
        "order_count": int(order_count),
        "new_users_count": int(new_users_count),
        "pending_sellers_count": int(pending_sellers_count),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _get_product_for_admin(db: AsyncSession, product_id: UUID) -> Product:
    """Fetch any product (incl. inactive / unapproved) or 404."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )
    return product
