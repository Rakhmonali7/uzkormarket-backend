"""``/api/v1/users`` — current-user identity.

The project rule's endpoint matrix doesn't include a buyer-side
``/users/{id}`` surface (admins handle user listing through
``/admin/users``). What every authenticated frontend nonetheless needs
is "who am I" so it can render the navbar without re-decoding the JWT
client-side. ``GET /users/me`` is that endpoint and nothing more.
"""

from __future__ import annotations

from fastapi import APIRouter

from dependencies.auth import CurrentUser
from schemas.user import UserMe

router = APIRouter()


@router.get(
    "/me",
    response_model=UserMe,
    summary="Get the currently-authenticated user",
)
async def read_me(user: CurrentUser) -> UserMe:
    """Return the caller's identity (id, email, role, account flags)."""
    return UserMe.model_validate(user)
