"""``/api/v1/admin`` — privileged moderation, user management, analytics.

Every route here uses ``AdminUser`` so the role check is enforced at the
dependency layer (project rule: "All admin routes: verify both JWT and
that role == ADMIN in dependency"). Handlers stay thin: they shape
inputs into the service signature and validate outputs through Pydantic.

Sections (one per ``/admin/...`` resource):
    1. Sellers — list, detail, document presign, approve, reject.
    2. Users — list, detail, ban.
    3. Products — list, approve, reject.
    4. Orders — list, detail.
    5. Analytics — summary.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from dependencies.auth import AdminUser
from dependencies.db import DBSession
from models.order import OrderStatus
from models.seller import SellerApprovalStatus
from models.user import UserRole
from schemas.admin import (
    AnalyticsSummary,
    DocumentPresignedUrlResponse,
    OrderAdminListItem,
    ProductAdminListItem,
    ProductRejectRequest,
    SellerAdminDetail,
    SellerAdminListItem,
    SellerRejectRequest,
    UserAdminDetail,
    UserAdminListItem,
    UserBanRequest,
)
from schemas.common import MessageResponse, Page, PageMeta, PageParams
from schemas.order import OrderPublic
from schemas.product import ProductSellerView
from schemas.seller import SellerDocumentPublic
from services import admin_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Sellers
# ---------------------------------------------------------------------------


@router.get(
    "/sellers",
    response_model=Page[SellerAdminListItem],
    summary="List sellers with optional filters",
)
async def list_sellers(
    _: AdminUser,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    approval_status: Annotated[
        SellerApprovalStatus | None,
        Query(description="Filter by approval status"),
    ] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Match business name, BRN, or email"),
    ] = None,
    created_from: Annotated[
        datetime | None, Query(description="Inclusive lower bound on created_at")
    ] = None,
    created_to: Annotated[
        datetime | None, Query(description="Inclusive upper bound on created_at")
    ] = None,
) -> Page[SellerAdminListItem]:
    """Admin seller feed. ``selectinload(seller.user)`` happens in the
    service so we can flatten User columns into the response."""
    items, total = await admin_service.list_sellers(
        db,
        page=page_params.page,
        page_size=page_params.page_size,
        approval_status=approval_status,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )
    return Page[SellerAdminListItem](
        items=[
            SellerAdminListItem(
                id=s.id,
                user_id=s.user_id,
                email=s.user.email,
                full_name=s.user.full_name,
                business_name=s.business_name,
                business_registration_number=s.business_registration_number,
                approval_status=s.approval_status,
                created_at=s.created_at,
            )
            for s in items
        ],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.get(
    "/sellers/{seller_id}",
    response_model=SellerAdminDetail,
    summary="Get the full seller detail with documents",
)
async def read_seller(
    seller_id: UUID, _: AdminUser, db: DBSession
) -> SellerAdminDetail:
    """Single seller with user + documents loaded. Document bytes still
    require a separate presigned-URL call — never publicly accessible."""
    seller = await admin_service.get_seller_detail(db, seller_id)
    return SellerAdminDetail(
        id=seller.id,
        user_id=seller.user_id,
        email=seller.user.email,
        full_name=seller.user.full_name,
        phone_number=seller.user.phone_number,
        business_name=seller.business_name,
        business_registration_number=seller.business_registration_number,
        business_address=seller.business_address,
        bank_account_number=seller.bank_account_number,
        bank_name=seller.bank_name,
        approval_status=seller.approval_status,
        rejection_reason=seller.rejection_reason,
        approved_at=seller.approved_at,
        created_at=seller.created_at,
        documents=[SellerDocumentPublic.model_validate(d) for d in seller.documents],
    )


@router.get(
    "/sellers/{seller_id}/documents/{document_id}/url",
    response_model=DocumentPresignedUrlResponse,
    summary="Mint a short-lived presigned GET URL for a document",
)
async def get_document_url(
    seller_id: UUID,
    document_id: UUID,
    _: AdminUser,
    db: DBSession,
) -> DocumentPresignedUrlResponse:
    """Returns a 5-minute presigned URL for the raw document bytes.
    Never cached — every viewing call mints a fresh URL so leaked URLs
    have a tight expiry window."""
    url, ttl = await admin_service.create_document_view_url(
        db, seller_id, document_id
    )
    return DocumentPresignedUrlResponse(url=url, expires_in=ttl)


@router.post(
    "/sellers/{seller_id}/approve",
    response_model=SellerAdminDetail,
    summary="Approve a seller application",
)
async def approve_seller(
    seller_id: UUID, admin: AdminUser, db: DBSession
) -> SellerAdminDetail:
    """Move a seller from PENDING to APPROVED. Triggers the approval
    email via Celery. Idempotent on already-APPROVED sellers."""
    seller = await admin_service.approve_seller(db, admin, seller_id)
    return await read_seller(seller.id, admin, db)


@router.post(
    "/sellers/{seller_id}/reject",
    response_model=SellerAdminDetail,
    summary="Reject a seller application with a reason",
)
async def reject_seller(
    seller_id: UUID,
    payload: SellerRejectRequest,
    admin: AdminUser,
    db: DBSession,
) -> SellerAdminDetail:
    """Mark a seller REJECTED with a stored reason. Triggers the
    rejection email via Celery so the applicant knows why."""
    seller = await admin_service.reject_seller(
        db, admin, seller_id, payload.reason
    )
    return await read_seller(seller.id, admin, db)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=Page[UserAdminListItem],
    summary="List user accounts",
)
async def list_users(
    _: AdminUser,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    role: Annotated[
        UserRole | None,
        Query(description="Defaults to BUYER; pass explicit role to widen scope"),
    ] = UserRole.BUYER,
    is_active: Annotated[
        bool | None, Query(description="Filter on account active flag")
    ] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Match email or name"),
    ] = None,
) -> Page[UserAdminListItem]:
    """Defaults to BUYER-only per the rule (``GET /admin/users`` lists
    buyer accounts). Admins can pass ``role=ADMIN`` etc. for wider views."""
    items, total = await admin_service.list_users(
        db,
        page=page_params.page,
        page_size=page_params.page_size,
        role=role,
        is_active=is_active,
        search=search,
    )
    return Page[UserAdminListItem](
        items=[UserAdminListItem.model_validate(u) for u in items],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.get(
    "/users/{user_id}",
    response_model=UserAdminDetail,
    summary="Get user detail with order-history aggregates",
)
async def read_user(
    user_id: UUID, _: AdminUser, db: DBSession
) -> UserAdminDetail:
    """User detail + ``order_count`` and ``last_order_at`` aggregated in
    one round-trip. Full order list lives behind
    ``GET /admin/orders?buyer_id=...``."""
    user, order_count, last_order_at = await admin_service.get_user_detail(
        db, user_id
    )
    return UserAdminDetail(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        order_count=order_count,
        last_order_at=last_order_at,
    )


@router.post(
    "/users/{user_id}/ban",
    response_model=UserAdminDetail,
    summary="Disable a user account and revoke its refresh tokens",
)
async def ban_user(
    user_id: UUID,
    payload: UserBanRequest,
    admin: AdminUser,
    db: DBSession,
) -> UserAdminDetail:
    """Sets ``is_active=False`` and revokes every active refresh token
    for the user. Refuses to ban admin accounts — those go through a
    separate audit-trailed path."""
    await admin_service.ban_user(db, user_id, payload.reason)
    return await read_user(user_id, admin, db)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.get(
    "/products",
    response_model=Page[ProductAdminListItem],
    summary="List products including pending/rejected/inactive",
)
async def list_products(
    _: AdminUser,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    is_approved: Annotated[
        bool | None, Query(description="Filter on approval flag")
    ] = None,
    is_active: Annotated[
        bool | None, Query(description="Filter on lifecycle flag")
    ] = None,
    seller_id: Annotated[
        UUID | None, Query(description="Restrict to a single seller's listings")
    ] = None,
    search: Annotated[
        str | None, Query(min_length=1, max_length=120)
    ] = None,
) -> Page[ProductAdminListItem]:
    """Admin product feed — sees every row regardless of approval state."""
    items, total = await admin_service.list_all_products(
        db,
        page=page_params.page,
        page_size=page_params.page_size,
        is_approved=is_approved,
        is_active=is_active,
        seller_id=seller_id,
        search=search,
    )
    return Page[ProductAdminListItem](
        items=[ProductAdminListItem.model_validate(p) for p in items],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.post(
    "/products/{product_id}/approve",
    response_model=ProductSellerView,
    summary="Approve a product listing",
)
async def approve_product(
    product_id: UUID, admin: AdminUser, db: DBSession
) -> ProductSellerView:
    """Flip ``is_approved=True`` so the product surfaces in the public
    catalogue. Idempotent."""
    product = await admin_service.approve_product(db, admin, product_id)
    return ProductSellerView.model_validate(product)


@router.post(
    "/products/{product_id}/reject",
    response_model=ProductSellerView,
    summary="Reject a product listing with a reason",
)
async def reject_product(
    product_id: UUID,
    payload: ProductRejectRequest,
    admin: AdminUser,
    db: DBSession,
) -> ProductSellerView:
    """Sets ``is_approved=False`` with a stored ``rejection_reason`` —
    visible to the seller on their dashboard."""
    product = await admin_service.reject_product(
        db, admin, product_id, payload.reason
    )
    return ProductSellerView.model_validate(product)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@router.get(
    "/orders",
    response_model=Page[OrderAdminListItem],
    summary="List orders with admin-side filters",
)
async def list_orders(
    _: AdminUser,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    status_filter: Annotated[
        OrderStatus | None,
        Query(alias="status", description="Filter by order status"),
    ] = None,
    seller_id: Annotated[
        UUID | None, Query(description="Restrict to one seller's orders")
    ] = None,
    buyer_id: Annotated[
        UUID | None, Query(description="Restrict to one buyer's orders")
    ] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
) -> Page[OrderAdminListItem]:
    """Admin order feed — superset of buyer/seller views."""
    items, total = await admin_service.list_all_orders(
        db,
        page=page_params.page,
        page_size=page_params.page_size,
        status_filter=status_filter,
        seller_id=seller_id,
        buyer_id=buyer_id,
        created_from=created_from,
        created_to=created_to,
    )
    return Page[OrderAdminListItem](
        items=[
            OrderAdminListItem(
                id=o.id,
                buyer_id=o.buyer_id,
                seller_id=o.seller_id,
                status=o.status,
                total_amount_uzs=o.total_amount_uzs,
                total_amount_krw=o.total_amount_krw,
                payment_method=o.payment_method,
                payment_reference=o.payment_reference,
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
    "/orders/{order_id}",
    response_model=OrderPublic,
    summary="Full order detail",
)
async def read_order(
    order_id: UUID, _: AdminUser, db: DBSession
) -> OrderPublic:
    """Reuses the buyer-side ``OrderPublic`` shape — admins see the same
    structure plus access to every order regardless of viewer."""
    from api.v1.orders import _to_public  # local import to keep modules independent

    order = await admin_service.get_order_for_admin(db, order_id)
    return _to_public(order)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get(
    "/analytics/summary",
    response_model=AnalyticsSummary,
    summary="Headline KPIs over a configurable window (default last 30 days)",
)
async def analytics_summary(
    _: AdminUser,
    db: DBSession,
    period_start: Annotated[
        date | None,
        Query(description="Inclusive lower bound (defaults to today - 30 days)"),
    ] = None,
    period_end: Annotated[
        date | None,
        Query(description="Inclusive upper bound (defaults to today)"),
    ] = None,
) -> AnalyticsSummary:
    """Compute GMV, order count, new-buyer count, pending-seller count.
    Default window is the last 30 days per the project rule."""
    summary = await admin_service.analytics_summary(
        db, period_start=period_start, period_end=period_end
    )
    return AnalyticsSummary(**summary)


__all__ = ["router"]
