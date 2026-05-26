"""Authentication schemas: register, login, token issuance, refresh, logout.

The route handlers in :mod:`api.v1.auth` are intentionally thin — they do
nothing more than validate against these shapes and delegate to
:mod:`services.auth_service`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field

from schemas.common import APIModel


class RegisterRequest(APIModel):
    """Buyer self-registration body.

    Role is **always** forced to ``BUYER`` by the service layer regardless of
    what the client sends — sellers register through ``/sellers/register`` and
    admins are provisioned out-of-band.
    """

    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Minimum 8 characters; bcrypt cost factor is enforced server-side.",
    )
    full_name: str = Field(min_length=1, max_length=120)
    phone_number: str | None = Field(default=None, max_length=32)


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshTokenRequest(APIModel):
    """Body for ``POST /auth/refresh``. Each refresh rotates the token and
    invalidates the old one in Redis."""

    refresh_token: str = Field(min_length=10, max_length=2048)


class LogoutRequest(APIModel):
    """Body for ``POST /auth/logout``. The supplied refresh token is
    blacklisted in Redis until its natural expiry."""

    refresh_token: str = Field(min_length=10, max_length=2048)


class TokenResponse(APIModel):
    """Issued by ``/auth/login`` and ``/auth/refresh``.

    ``expires_in`` is the access-token lifetime in seconds (refresh token TTL
    is fixed by config and not exposed to clients to discourage assumptions)."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0, description="Access-token TTL in seconds.")
