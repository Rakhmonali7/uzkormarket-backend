"""ORM model package.

Importing from ``models`` (e.g. ``from models import User``) is also what
ensures every model class is registered with ``Base.metadata`` so Alembic's
autogenerate sees the full schema. The Alembic ``env.py`` (step 6) only needs
to ``import models``.
"""

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

from models.delivery import Delivery, DeliveryCarrier, DeliveryStatus
from models.document import (
    SellerDocument,
    SellerDocumentStatus,
    SellerDocumentType,
)
from models.order import Order, OrderStatus
from models.order_item import OrderItem
from models.payment import Payment, PaymentGateway, PaymentStatus
from models.product import Product
from models.seller import Seller, SellerApprovalStatus
from models.user import User, UserRole

__all__ = [
    "Base",
    "Delivery",
    "DeliveryCarrier",
    "DeliveryStatus",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Payment",
    "PaymentGateway",
    "PaymentStatus",
    "Product",
    "Seller",
    "SellerApprovalStatus",
    "SellerDocument",
    "SellerDocumentStatus",
    "SellerDocumentType",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserRole",
]
