"""v1 router aggregator.

Imports each per-resource router and stitches them under the ``/api/v1``
prefix that ``create_app`` mounts. Order matters only for the OpenAPI
docs grouping — route resolution is done by exact path so collisions
would be surfaced at startup.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.v1 import (
    admin,
    auth,
    delivery,
    orders,
    products,
    sellers,
    users,
)

api_router = APIRouter()

# Auth + identity come first in the OpenAPI sidebar.
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(sellers.router, prefix="/sellers", tags=["sellers"])
api_router.include_router(products.router, prefix="/products", tags=["products"])
api_router.include_router(orders.router, prefix="/orders", tags=["orders"])
api_router.include_router(delivery.router, prefix="/delivery", tags=["delivery"])
# Admin last — most specific role gate, easiest to find at the bottom.
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

__all__ = ["api_router"]
