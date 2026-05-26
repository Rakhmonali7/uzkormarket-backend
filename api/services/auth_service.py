"""Authentication service — register, login, refresh, logout.

Composition layer that glues together :mod:`core.security` (pure crypto)
with the database (user lookups, uniqueness checks). Every public function
is async, takes an ``AsyncSession`` first, and raises
:class:`fastapi.HTTPException` for caller-facing failures.

The refresh-token flow is the most subtle piece:

* ``login`` issues a fresh access + refresh pair and registers the
  refresh jti in :class:`~core.security.RefreshTokenStore`.
* ``refresh`` decodes the supplied refresh token, verifies its jti is
  still whitelisted, then atomically rotates the whitelist entry — this
  makes "each refresh invalidates the old token" a single Redis MULTI/EXEC.
* ``logout`` revokes the jti out of band so the natural JWT expiry is
  no longer the only line of defence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import (
    InvalidTokenError,
    RefreshTokenStore,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_refresh_token_store,
    hash_password,
    verify_password,
)
from models.user import User, UserRole
from schemas.auth import LoginRequest, RegisterRequest

log = logging.getLogger(__name__)


@dataclass
class IssuedTokens:
    """Container returned by ``login`` and ``refresh`` so the route handler
    can populate :class:`schemas.auth.TokenResponse` without duplicating
    field plumbing."""

    access_token: str
    refresh_token: str
    expires_in: int


async def register_buyer(db: AsyncSession, payload: RegisterRequest) -> User:
    """Create a new BUYER account and return the persisted ORM instance.

    Role is force-set to ``BUYER`` regardless of any field a malicious
    client could try to sneak in — sellers register through
    :func:`services.seller_service.register_seller` and admins are
    provisioned out-of-band.

    Raises ``409 Conflict`` if the email is already taken. We rely on the
    unique index for the race-safe check rather than a pre-INSERT SELECT
    (which would still race against a concurrent registration).
    """
    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        phone_number=payload.phone_number,
        role=UserRole.BUYER,
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    await db.refresh(user)
    return user


async def authenticate(
    db: AsyncSession,
    payload: LoginRequest,
) -> User:
    """Verify email + password and return the user, or raise ``401``.

    Returns the ORM instance so callers (``login``) can read role/id
    without a second query. Disabled accounts are rejected with the same
    generic message as a wrong password — we don't want to disclose
    whether an email exists or whether it's been banned.
    """
    stmt = select(User).where(User.email == payload.email.lower())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    if not user.is_active:
        # Same status code on purpose: don't leak the reason.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return user


async def login(db: AsyncSession, payload: LoginRequest) -> IssuedTokens:
    """End-to-end login: authenticate, issue tokens, register the refresh jti.

    The refresh-token store registration is intentionally *after* the JWTs
    are minted so that if Redis is briefly unavailable the user sees a
    consistent 500 rather than receiving a token that wasn't whitelisted.
    """
    user = await authenticate(db, payload)
    return await _issue_token_pair(user)


async def refresh(db: AsyncSession, refresh_token: str) -> IssuedTokens:
    """Rotate a refresh token: decode → check whitelist → re-issue → swap.

    The atomic ``rotate`` call inside :class:`RefreshTokenStore` is what
    enforces "each refresh invalidates the old token" — even under a
    concurrent reuse attempt only one of the racers wins.

    Re-fetches the user on every refresh (rather than trusting the role
    claim in the JWT) so a banned or downgraded user can't keep refreshing
    indefinitely until their original access token expires.
    """
    try:
        payload = decode_refresh_token(refresh_token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    store = get_refresh_token_store()
    assert payload.jti is not None  # Guaranteed by decode_refresh_token.
    if not await store.is_valid(payload.jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked.",
        )

    user = await db.get(User, payload.sub)
    if user is None or not user.is_active:
        # Defence in depth: revoke whatever's left of this user's tokens
        # so subsequent refresh attempts fail fast.
        if user is not None:
            await store.revoke_all_for_user(user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is no longer active.",
        )

    new_access = create_access_token(user.id, user.role)
    new_refresh, new_jti, ttl = create_refresh_token(user.id, user.role)
    await store.rotate(user.id, payload.jti, new_jti, ttl)

    return IssuedTokens(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def logout(refresh_token: str) -> None:
    """Best-effort logout: decode the supplied refresh token and revoke its jti.

    Silently no-ops on a malformed / already-expired token. The endpoint
    always returns 200 — clients shouldn't have to handle a logout failure.
    """
    try:
        payload = decode_refresh_token(refresh_token)
    except InvalidTokenError:
        log.info("Logout called with invalid refresh token; ignoring.")
        return

    assert payload.jti is not None
    store = get_refresh_token_store()
    await store.revoke(payload.sub, payload.jti)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _issue_token_pair(user: User) -> IssuedTokens:
    """Mint a fresh access+refresh pair and register the refresh jti.

    Extracted from :func:`login` so :mod:`services.seller_service` can
    reuse the same path on first registration without duplicating the
    Redis registration call.
    """
    access = create_access_token(user.id, user.role)
    refresh_token, jti, ttl = create_refresh_token(user.id, user.role)
    store: RefreshTokenStore = get_refresh_token_store()
    await store.register(user.id, jti, ttl)
    return IssuedTokens(
        access_token=access,
        refresh_token=refresh_token,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
