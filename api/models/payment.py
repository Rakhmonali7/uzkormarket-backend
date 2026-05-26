"""Payment attempt / settlement record — multiple rows per Order are possible
(retries, partial refunds, multiple gateways)."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.order import Order


class PaymentGateway(str, enum.Enum):
    TOSS = "TOSS"
    KG_INICIS = "KG_INICIS"
    PAYME = "PAYME"
    CLICK = "CLICK"
    UZCARD = "UZCARD"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    gateway: Mapped[PaymentGateway] = mapped_column(
        Enum(PaymentGateway, name="payment_gateway", native_enum=True),
        nullable=False,
    )
    # Nullable until the gateway returns one (PENDING -> SUCCESS/FAILED).
    gateway_transaction_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )

    amount_uzs: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_krw: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    # Higher precision than the price columns so we can faithfully record
    # whatever rate the FX provider quoted at payment time.
    exchange_rate_used: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", native_enum=True),
        nullable=False,
        default=PaymentStatus.PENDING,
        index=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    order: Mapped["Order"] = relationship("Order", back_populates="payments")

    def __repr__(self) -> str:
        return (
            f"Payment(id={self.id!r}, gateway={self.gateway!r}, "
            f"status={self.status!r})"
        )
