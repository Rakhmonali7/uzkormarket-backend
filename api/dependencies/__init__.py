"""Shared FastAPI dependencies.

Two modules per the project rules:

- :mod:`dependencies.db` — request-scoped ``AsyncSession`` injection.
- :mod:`dependencies.auth` — Bearer-token decode, role guards, seller helpers.

Re-exporting the public surface here keeps route handlers' imports short::

    from dependencies import AdminUser, DBSession
"""

from dependencies.auth import (
    AdminUser,
    ApprovedSeller,
    BuyerUser,
    CurrentSeller,
    CurrentUser,
    SellerUser,
    get_current_approved_seller,
    get_current_seller,
    get_current_user,
    require_admin,
    require_buyer,
    require_role,
    require_seller,
)
from dependencies.db import DBSession, get_async_session

__all__ = [
    # db
    "DBSession",
    "get_async_session",
    # auth — types
    "AdminUser",
    "ApprovedSeller",
    "BuyerUser",
    "CurrentSeller",
    "CurrentUser",
    "SellerUser",
    # auth — callables
    "get_current_approved_seller",
    "get_current_seller",
    "get_current_user",
    "require_admin",
    "require_buyer",
    "require_role",
    "require_seller",
]
