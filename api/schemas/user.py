"""User-facing response schemas.

Buyers don't currently get a user-management surface (no PUT /users/me yet),
so the inputs in this file are minimal and the focus is on response shapes
embedded in other resources (e.g. seller profile) and the ``/auth`` flow.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import EmailStr

from models.user import UserRole
from schemas.common import APIModel


class UserPublic(APIModel):
    """Minimal public projection of a User. Used embedded in admin listings
    and other resources where a full identity is more than the caller needs."""

    id: UUID
    full_name: str
    role: UserRole


class UserMe(APIModel):
    """The full self-view returned by ``/auth`` and any protected route that
    exposes the current user. Never includes the password hash."""

    id: UUID
    email: EmailStr
    full_name: str
    phone_number: str | None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime
