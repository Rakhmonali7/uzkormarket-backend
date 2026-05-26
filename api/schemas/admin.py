"""Admin-facing schemas: list/detail items, action bodies, analytics summary.

These shapes back the ``/api/v1/admin/*`` routes. The corresponding service
layer is in :mod:`services.admin_service` (step 10).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import EmailStr, Field

from models.order import OrderStatus
from models.seller import SellerApprovalStatus
from models.user import UserRole
from schemas.common import APIModel
from schemas.seller import SellerDocumentPublic


# ---------------------------------------------------------------------------
# Sellers
# ---------------------------------------------------------------------------


class SellerAdminListItem(APIModel):
    """Row in ``GET /admin/sellers``. Combines key User and Seller columns so
    the admin dashboard table needs only one fetch per page."""

    id: UUID
    user_id: UUID
    email: EmailStr
    full_name: str
    business_name: str
    business_registration_number: str
    approval_status: SellerApprovalStatus
    created_at: datetime


class SellerAdminDetail(SellerAdminListItem):
    """``GET /admin/sellers/{id}`` — adds full business + bank details and
    every uploaded document so the admin can review without extra requests.
    Documents include their MinIO ids; the actual byte access still requires
    a separate ``/admin/sellers/{id}/documents/{doc_id}/url`` call."""

    phone_number: str | None
    business_address: str
    bank_account_number: str
    bank_name: str
    rejection_reason: str | None
    approved_at: datetime | None
    documents: list[SellerDocumentPublic]


class SellerRejectRequest(APIModel):
    """``POST /admin/sellers/{id}/reject`` body. Reason is mandatory and is
    propagated into the rejection email sent via Celery."""

    reason: str = Field(min_length=1, max_length=2000)


class DocumentPresignedUrlResponse(APIModel):
    """Returned by ``GET /admin/sellers/{id}/documents/{doc_id}/url``.

    ``expires_in`` mirrors ``settings.MINIO_PRESIGN_GET_TTL_SECONDS``
    (5 minutes by default) — short on purpose because anyone with the URL
    can read the document until it expires."""

    url: str
    expires_in: int = Field(gt=0, description="Presigned GET URL lifetime, in seconds.")


# ---------------------------------------------------------------------------
# Users (buyer / admin accounts)
# ---------------------------------------------------------------------------


class UserAdminListItem(APIModel):
    """Row in ``GET /admin/users``."""

    id: UUID
    email: EmailStr
    full_name: str
    phone_number: str | None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


class UserAdminDetail(UserAdminListItem):
    """``GET /admin/users/{id}`` — adds aggregated order history fields. The
    full order list lives behind ``GET /admin/orders?buyer_id=...``."""

    order_count: int = Field(default=0, ge=0)
    last_order_at: datetime | None = None


class UserBanRequest(APIModel):
    """``POST /admin/users/{id}/ban`` body. Sets ``is_active=False`` and
    invalidates any active refresh tokens for that user."""

    reason: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class ProductAdminListItem(APIModel):
    """Row in ``GET /admin/products``. Includes lifecycle and approval flags
    that are hidden from buyers in :class:`schemas.product.ProductPublic`."""

    id: UUID
    seller_id: UUID
    title: str
    category: str
    price_krw: Decimal
    price_uzs: Decimal
    stock_quantity: int
    is_active: bool
    is_approved: bool
    rejection_reason: str | None
    approved_at: datetime | None
    created_at: datetime


class ProductRejectRequest(APIModel):
    """``POST /admin/products/{id}/reject`` body."""

    reason: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class OrderAdminListItem(APIModel):
    """Row in ``GET /admin/orders``. Trimmed compared to the buyer-side
    ``OrderListItem`` because admins typically filter on different fields
    (e.g. seller_id, payment status)."""

    id: UUID
    buyer_id: UUID
    seller_id: UUID
    status: OrderStatus
    total_amount_uzs: Decimal
    total_amount_krw: Decimal
    payment_method: str
    payment_reference: str | None
    item_count: int = Field(ge=0)
    created_at: datetime


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class AnalyticsSummary(APIModel):
    """``GET /admin/analytics/summary`` response.

    Window defaults to the last 30 days but the service layer accepts
    optional ``period_start`` / ``period_end`` query params for ad-hoc ranges.
    """

    period_start: date
    period_end: date
    gmv_uzs: Decimal = Field(
        ge=0, description="Gross merchandise value (UZS) over the window."
    )
    gmv_krw: Decimal = Field(
        ge=0, description="Gross merchandise value (KRW) over the window."
    )
    order_count: int = Field(ge=0)
    new_users_count: int = Field(ge=0)
    pending_sellers_count: int = Field(ge=0)
