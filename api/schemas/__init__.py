"""Pydantic v2 schemas for request bodies and response models.

Every API route handler validates its inputs against — and returns instances
of — schemas declared in this package. Route handlers MUST NOT return raw
dicts; the response_model on each FastAPI route forces a typed projection.

Module layout follows the project rules:

- :mod:`schemas.common` — generic envelopes (Page, PageMeta, PageParams, MessageResponse)
- :mod:`schemas.auth`, :mod:`schemas.user` — auth flow + user identity
- :mod:`schemas.seller`, :mod:`schemas.product` — seller surface
- :mod:`schemas.order`, :mod:`schemas.delivery` — buyer surface
- :mod:`schemas.admin` — admin surface
"""

from schemas.admin import (
    AnalyticsSummary,
    DocumentPresignedUrlResponse,
    OrderAdminListItem,
    ProductAdminListItem,
    ProductRejectRequest,
    SellerAdminDetail,
    SellerAdminListItem,
    SellerRejectRequest,
    UserAdminDetail,
    UserAdminListItem,
    UserBanRequest,
)
from schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
)
from schemas.common import (
    APIModel,
    MessageResponse,
    NonEmptyStr,
    Page,
    PageMeta,
    PageParams,
)
from schemas.delivery import DeliveryPublic, TrackingEvent
from schemas.order import (
    OrderCreate,
    OrderItemCreate,
    OrderItemPublic,
    OrderListItem,
    OrderPublic,
    ShippingAddress,
)
from schemas.product import (
    ProductBase,
    ProductCreate,
    ProductPublic,
    ProductSellerView,
    ProductUpdate,
)
from schemas.seller import (
    SellerDocumentConfirmRequest,
    SellerDocumentCreateRequest,
    SellerDocumentCreateResponse,
    SellerDocumentPublic,
    SellerProfile,
    SellerRegisterRequest,
)
from schemas.user import UserMe, UserPublic

__all__ = [
    # common
    "APIModel",
    "MessageResponse",
    "NonEmptyStr",
    "Page",
    "PageMeta",
    "PageParams",
    # auth
    "LoginRequest",
    "LogoutRequest",
    "RefreshTokenRequest",
    "RegisterRequest",
    "TokenResponse",
    # user
    "UserMe",
    "UserPublic",
    # seller
    "SellerDocumentConfirmRequest",
    "SellerDocumentCreateRequest",
    "SellerDocumentCreateResponse",
    "SellerDocumentPublic",
    "SellerProfile",
    "SellerRegisterRequest",
    # product
    "ProductBase",
    "ProductCreate",
    "ProductPublic",
    "ProductSellerView",
    "ProductUpdate",
    # order
    "OrderCreate",
    "OrderItemCreate",
    "OrderItemPublic",
    "OrderListItem",
    "OrderPublic",
    "ShippingAddress",
    # delivery
    "DeliveryPublic",
    "TrackingEvent",
    # admin
    "AnalyticsSummary",
    "DocumentPresignedUrlResponse",
    "OrderAdminListItem",
    "ProductAdminListItem",
    "ProductRejectRequest",
    "SellerAdminDetail",
    "SellerAdminListItem",
    "SellerRejectRequest",
    "UserAdminDetail",
    "UserAdminListItem",
    "UserBanRequest",
]
