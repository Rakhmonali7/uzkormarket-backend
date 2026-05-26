"""Seller business profile — extends a User with Korean-business metadata."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.document import SellerDocument
    from models.product import Product
    from models.user import User


class SellerApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Seller(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sellers"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    business_registration_number: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    business_address: Mapped[str] = mapped_column(Text, nullable=False)
    bank_account_number: Mapped[str] = mapped_column(String(64), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(120), nullable=False)

    approval_status: Mapped[SellerApprovalStatus] = mapped_column(
        Enum(SellerApprovalStatus, name="seller_approval_status", native_enum=True),
        nullable=False,
        default=SellerApprovalStatus.PENDING,
        index=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK to the admin user who acted on the application. ON DELETE SET NULL so
    # we don't lose the seller record if an admin account is removed.
    approved_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships ----
    user: Mapped["User"] = relationship(
        "User",
        back_populates="seller",
        foreign_keys=[user_id],
    )
    # No back-populates — we don't need a list of "approved sellers" hanging
    # off the User model. Query that explicitly when an admin needs it.
    approver: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[approved_by],
    )
    documents: Mapped[list["SellerDocument"]] = relationship(
        "SellerDocument",
        back_populates="seller",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    products: Mapped[list["Product"]] = relationship(
        "Product",
        back_populates="seller",
        cascade="all, delete",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"Seller(id={self.id!r}, user_id={self.user_id!r}, "
            f"approval_status={self.approval_status!r})"
        )
