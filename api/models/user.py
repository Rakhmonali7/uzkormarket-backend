"""User account model — root identity for buyers, sellers, and admins."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.order import Order
    from models.seller import Seller


class UserRole(str, enum.Enum):
    """Project rule: exactly three roles, enforced at the dependency level."""

    BUYER = "BUYER"
    SELLER = "SELLER"
    ADMIN = "ADMIN"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True),
        nullable=False,
        default=UserRole.BUYER,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ---- Relationships -----------------------------------------------------
    # 1:1 — a user MAY have an associated seller record. Cascade so deleting
    # the user removes the seller (and its documents/products via FKs).
    seller: Mapped["Seller | None"] = relationship(
        "Seller",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="Seller.user_id",
        passive_deletes=True,
    )

    # 1:N — orders placed by this user as a buyer. Restrict deletion when
    # there are orders so we don't lose financial history.
    orders: Mapped[list["Order"]] = relationship(
        "Order",
        back_populates="buyer",
        foreign_keys="Order.buyer_id",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r}, role={self.role!r})"
