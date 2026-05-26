"""Order schemas: shipping address, checkout body, line items, public detail.

Money totals on the response side are ``Decimal`` (not the constrained
``MoneyAmount`` alias) because the values come from the database and we
trust them to already match the column precision. On the input side we
deliberately do **not** accept any price information from the client —
unit and total prices are computed server-side from the referenced
products at order-creation time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from models.order import OrderStatus
from schemas.common import APIModel


class ShippingAddress(APIModel):
    """Buyer-side delivery address. Snapshotted into ``Order.shipping_address``
    at checkout so the historical order remains accurate even if the buyer
    later edits their saved address.

    Field names are tuned to Uzbekistan's address structure
    (region = viloyat, district = tuman) — the country defaults to ``UZ``
    since this platform only ships to Uzbekistan.
    """

    full_name: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(min_length=1, max_length=32)
    country: str = Field(
        default="UZ",
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 country code. Currently always 'UZ'.",
    )
    region: str = Field(min_length=1, max_length=80, description="Viloyat / oblast.")
    district: str | None = Field(
        default=None, max_length=80, description="Tuman / district. Optional in cities."
    )
    city: str = Field(min_length=1, max_length=80)
    address_line: str = Field(min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(
        default=None,
        max_length=500,
        description="Free-form delivery instructions, e.g. landmark or door code.",
    )


class OrderItemCreate(APIModel):
    """One product + quantity in a checkout payload."""

    product_id: UUID
    quantity: int = Field(gt=0, le=999)


class OrderCreate(APIModel):
    """``POST /orders`` body. Prices and totals are intentionally absent —
    the service layer fetches them from the products table to prevent any
    client-side tampering with amounts."""

    items: list[OrderItemCreate] = Field(min_length=1, max_length=50)
    shipping_address: ShippingAddress
    payment_method: str = Field(
        min_length=1,
        max_length=40,
        description="Identifier of a supported gateway (TOSS, KG_INICIS, PAYME, CLICK, UZCARD).",
    )


class OrderItemPublic(APIModel):
    """Line item as returned in order responses. Prices are the snapshotted
    values from order-creation time, not live product prices."""

    id: UUID
    product_id: UUID
    quantity: int
    unit_price_uzs: Decimal
    unit_price_krw: Decimal


class OrderListItem(APIModel):
    """Lightweight summary used in paginated lists (``GET /orders``,
    admin order feed). ``item_count`` is computed in the service layer to
    avoid eager-loading the items relationship for list views."""

    id: UUID
    seller_id: UUID
    status: OrderStatus
    total_amount_uzs: Decimal
    total_amount_krw: Decimal
    payment_method: str
    item_count: int = Field(ge=0)
    created_at: datetime


class OrderPublic(APIModel):
    """Full order detail. Used as the response model for
    ``POST /orders``, ``GET /orders/{id}``, and the buyer-side cancel endpoint."""

    id: UUID
    buyer_id: UUID
    seller_id: UUID
    status: OrderStatus
    total_amount_uzs: Decimal
    total_amount_krw: Decimal
    payment_method: str
    payment_reference: str | None
    shipping_address: ShippingAddress
    items: list[OrderItemPublic]
    created_at: datetime
    updated_at: datetime
