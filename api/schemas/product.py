"""Product schemas: seller create/update bodies and public/seller responses.

Currency fields use a constrained ``Decimal`` type (``MoneyAmount``) that
mirrors the ``Numeric(18, 2)`` precision in the database — so any value the
client could send that wouldn't round-trip through the column is rejected
before it ever reaches SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field

from schemas.common import APIModel

# Mirrors `Numeric(18, 2)` in the ORM. Reused below for both prices and totals.
MoneyAmount = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=2, ge=0),
]


class ProductBase(APIModel):
    """Fields a seller is allowed to set on create or update. Pulled out so
    :class:`ProductCreate` and :class:`ProductUpdate` can share definitions
    without duplicating constraints."""

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    category: str = Field(min_length=1, max_length=80)

    # Free-form per-category metadata (e.g. {"weight_g": 250, "spice_level": 3}).
    # No schema constraints here — the frontend / category guide define the
    # canonical keys; we just persist what arrives.
    attributes: dict[str, Any] = Field(default_factory=dict)

    price_krw: MoneyAmount
    price_uzs: MoneyAmount
    stock_quantity: int = Field(default=0, ge=0)

    thumbnail_key: str | None = Field(default=None, max_length=512)
    image_keys: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="MinIO object keys, never public URLs. Capped at 20 images per product.",
    )


class ProductCreate(ProductBase):
    """``POST /products`` body. Title + prices required, the rest defaulted."""

    pass


class ProductUpdate(APIModel):
    """``PUT /products/{id}`` body — every field is optional. The service
    layer only mutates columns that are explicitly present (uses
    ``model_dump(exclude_unset=True)``)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    attributes: dict[str, Any] | None = None
    price_krw: MoneyAmount | None = None
    price_uzs: MoneyAmount | None = None
    stock_quantity: int | None = Field(default=None, ge=0)
    thumbnail_key: str | None = Field(default=None, max_length=512)
    image_keys: list[str] | None = Field(default=None, max_length=20)


class ProductPublic(APIModel):
    """Buyer-facing product detail. Hides admin lifecycle fields like
    ``is_approved`` and ``rejection_reason`` — those are visible only on
    :class:`ProductSellerView` and :class:`schemas.admin.ProductAdminListItem`."""

    id: UUID
    seller_id: UUID
    title: str
    description: str
    category: str
    attributes: dict[str, Any]
    price_krw: Decimal
    price_uzs: Decimal
    stock_quantity: int
    thumbnail_key: str | None
    image_keys: list[str]
    created_at: datetime


class ProductSellerView(ProductPublic):
    """Seller-side product detail. Adds the lifecycle and approval flags so
    the seller dashboard can show "pending review" / "rejected" / "live"."""

    is_active: bool
    is_approved: bool
    rejection_reason: str | None
    approved_at: datetime | None
    updated_at: datetime
