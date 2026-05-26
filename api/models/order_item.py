"""Order line item — links an Order to a Product with a quantity snapshot."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.order import Order
    from models.product import Product


class OrderItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # RESTRICT on product — we don't want a deleted product to silently
    # erase order history. Soft-delete the product instead (is_active=False).
    product_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    # Unit prices are *snapshotted at order time* — they are not derived from
    # the Product row, so a later price change doesn't rewrite history.
    unit_price_uzs: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    unit_price_krw: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product"] = relationship("Product", back_populates="order_items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_uzs >= 0", name="unit_price_uzs_non_negative"),
        CheckConstraint("unit_price_krw >= 0", name="unit_price_krw_non_negative"),
    )

    def __repr__(self) -> str:
        return (
            f"OrderItem(id={self.id!r}, order_id={self.order_id!r}, "
            f"product_id={self.product_id!r}, qty={self.quantity})"
        )
