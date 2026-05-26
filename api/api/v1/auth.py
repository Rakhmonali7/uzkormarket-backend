"""``/api/v1/auth`` — registration and token lifecycle.

Handlers are deliberately one-liners over :mod:`services.auth_service`.
The service is the single place where bcrypt, JWT signing, and the
Redis refresh-token store interact.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from dependencies.db import DBSession
from schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
)
from schemas.common import MessageResponse
from schemas.user import UserMe
from services import auth_service

router = APIRouter()


@router.post(
    "/register",
    response_model=UserMe,
    status_code=status.HTTP_201_CREATED,
    summary="Register a buyer account",
)
async def register(payload: RegisterRequest, db: DBSession) -> UserMe:
    """Create a buyer account.

    Sellers register through ``POST /sellers/register``; admins are
    provisioned out-of-band. The role is force-set to BUYER server-side.
    """
    user = await auth_service.register_buyer(db, payload)
    return UserMe.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access + refresh token pair",
)
async def login(payload: LoginRequest, db: DBSession) -> TokenResponse:
    """Issue a fresh access+refresh pair. Returns 401 on invalid
    credentials or a disabled account; both flow through the same
    response message to avoid disclosing which one applies."""
    issued = await auth_service.login(db, payload)
    return TokenResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate an existing refresh token for a new pair",
)
async def refresh(payload: RefreshTokenRequest, db: DBSession) -> TokenResponse:
    """Rotate the refresh token. The old token is invalidated atomically
    in Redis, even under concurrent reuse attempts (only one racer wins)."""
    issued = await auth_service.refresh(db, payload.refresh_token)
    return TokenResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoke a refresh token",
)
async def logout(payload: LogoutRequest) -> MessageResponse:
    """Best-effort logout — silently no-ops on a malformed/expired token
    so the client never has to handle a logout failure."""
    await auth_service.logout(payload.refresh_token)
    return MessageResponse(message="Logged out.")
