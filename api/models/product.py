"""Product listing — owned by a seller, requires admin approval before going live."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.order_item import OrderItem
    from models.seller import Seller
    from models.user import User


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "products"

    seller_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sellers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    # Category-specific extras (size, dimensions, ingredients, etc).
    # MutableDict makes in-place mutations dirty-track correctly.
    attributes: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB), nullable=False, default=dict
    )

    # Prices stored separately in both currencies. Numeric(18,2) handles up to
    # 9.99 * 10^15 — far more than any realistic product price.
    price_krw: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    price_uzs: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    stock_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # MinIO object keys, never public URLs. Frontend asks the API for a
    # presigned GET when it needs to render an image.
    thumbnail_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_keys: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSONB), nullable=False, default=list
    )

    # ---- Lifecycle flags ---------------------------------------------------
    # is_active doubles as the soft-delete flag for the seller-side delete
    # endpoint. is_approved + rejection_reason together describe admin state:
    #   is_approved=False, rejection_reason IS NULL  -> pending review
    #   is_approved=False, rejection_reason IS NOT NULL -> rejected (with reason)
    #   is_approved=True                              -> live
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships -----------------------------------------------------
    seller: Mapped["Seller"] = relationship("Seller", back_populates="products")
    order_items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="product"
    )
    approver: Mapped["User | None"] = relationship(
        "User", foreign_keys=[approved_by]
    )

    __table_args__ = (
        # Hot path: public listing query filters approved + active products
        # ordered by recency. Composite index avoids a sequential scan.
        Index(
            "ix_products_listing",
            "is_approved",
            "is_active",
            "created_at",
        ),
        CheckConstraint("price_krw >= 0", name="price_krw_non_negative"),
        CheckConstraint("price_uzs >= 0", name="price_uzs_non_negative"),
        CheckConstraint("stock_quantity >= 0", name="stock_non_negative"),
    )

    def __repr__(self) -> str:
        return (
            f"Product(id={self.id!r}, title={self.title!r}, "
            f"seller_id={self.seller_id!r})"
        )
