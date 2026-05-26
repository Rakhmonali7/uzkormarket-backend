"""Order header — one row per buyer-side checkout."""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.delivery import Delivery
    from models.order_item import OrderItem
    from models.payment import Payment
    from models.seller import Seller
    from models.user import User


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    # RESTRICT instead of CASCADE on the FKs — we never want to lose an order
    # row just because a buyer or seller is removed; financial / tax records
    # have to outlive account deletions.
    # buyer_id / seller_id are NOT marked index=True because the composite
    # indexes below ((buyer_id, status) and (seller_id, status)) are
    # left-prefix-usable for plain "WHERE buyer_id = ?" queries.
    buyer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    seller_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sellers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    total_amount_uzs: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_amount_krw: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", native_enum=True),
        nullable=False,
        default=OrderStatus.PENDING,
        index=True,
    )

    # Snapshot the buyer's address at order time. Schema-level shape lives in
    # schemas/order.py; here we store whatever JSON the schema produced so the
    # buyer's profile can mutate later without rewriting historical orders.
    shipping_address: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB), nullable=False
    )

    payment_method: Mapped[str] = mapped_column(String(40), nullable=False)
    payment_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # ---- Relationships -----------------------------------------------------
    buyer: Mapped["User"] = relationship(
        "User", back_populates="orders", foreign_keys=[buyer_id]
    )
    seller: Mapped["Seller"] = relationship("Seller", foreign_keys=[seller_id])
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    delivery: Mapped["Delivery | None"] = relationship(
        "Delivery",
        back_populates="order",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    payments: Mapped[list["Payment"]] = relationship(
        "Payment",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Common dashboard queries: "my orders by status" and "seller orders by status"
        Index("ix_orders_buyer_status", "buyer_id", "status"),
        Index("ix_orders_seller_status", "seller_id", "status"),
        # For the admin /orders feed and analytics summary range filters.
        Index("ix_orders_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"Order(id={self.id!r}, status={self.status!r})"
