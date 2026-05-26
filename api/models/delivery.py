"""Delivery / shipment record — one per Order, populated once we have a tracking number."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.order import Order


class DeliveryCarrier(str, enum.Enum):
    CJ_LOGISTICS = "CJ_LOGISTICS"
    KOREA_POST = "KOREA_POST"
    DHL = "DHL"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"


class Delivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deliveries"

    # Unique FK enforces the 1:1 with Order at the database level.
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    tracking_number: Mapped[str] = mapped_column(
        String(120), nullable=False, index=True
    )
    carrier: Mapped[DeliveryCarrier] = mapped_column(
        Enum(DeliveryCarrier, name="delivery_carrier", native_enum=True),
        nullable=False,
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", native_enum=True),
        nullable=False,
        default=DeliveryStatus.PENDING,
        index=True,
    )

    estimated_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Populated by the sync_delivery_status Celery task. Each event is a dict
    # of the shape {timestamp, location, description}.
    tracking_events: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSONB), nullable=False, default=list
    )

    order: Mapped["Order"] = relationship("Order", back_populates="delivery")

    def __repr__(self) -> str:
        return (
            f"Delivery(id={self.id!r}, tracking_number={self.tracking_number!r}, "
            f"status={self.status!r})"
        )
