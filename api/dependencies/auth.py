"""Authentication / authorization dependencies.

These are the dependencies the project rule "All admin routes: verify both
JWT and that role == ADMIN in dependency" relies on. Routes never inspect
the JWT or user role themselves — they declare a guard via :func:`Depends`
and trust that the request is authorized by the time their body runs.

Public surface:

- :func:`get_current_user` — extracts and validates the Bearer token, loads
  the associated active User row.
- :func:`require_role` — factory producing a guard for an arbitrary set of
  roles.
- :data:`require_buyer`, :data:`require_seller`, :data:`require_admin` —
  pre-built guards for the three roles.
- :func:`get_current_seller` — current user's Seller profile in any state.
- :func:`get_current_approved_seller` — same, but rejects PENDING/REJECTED.

Each dependency is also exported as an ``Annotated`` type alias so route
signatures can read::

    async def list_admin_sellers(user: AdminUser, db: DBSession) -> Page[...]:
        ...
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.security import InvalidTokenError, decode_access_token
from dependencies.db import DBSession
from models.seller import Seller, SellerApprovalStatus
from models.user import User, UserRole

# ``HTTPBearer`` matches our JSON-based ``/auth/login`` flow better than
# ``OAuth2PasswordBearer`` (which expects form-encoded credentials).
#
# ``auto_error=False`` is deliberate. The default would have FastAPI raise
# **403** for a missing Authorization header, but per RFC 6750 a missing or
# invalid token is **401**, and 403 is reserved for "authenticated but
# unauthorized". Handling the missing-header case ourselves keeps the
# semantics consistent across all failure modes.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT access token issued by POST /api/v1/auth/login.",
)


def _unauthorized(detail: str) -> HTTPException:
    """Build a 401 with the WWW-Authenticate hint, per RFC 6750."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    creds: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: DBSession,
) -> User:
    """Decode the Bearer token, load the user, ensure the account is active.

    Returns the :class:`models.user.User` ORM instance so downstream
    dependencies (role guards, seller helpers) can refine without re-querying.
    """
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized("Missing or invalid Authorization header.")

    try:
        payload = decode_access_token(creds.credentials)
    except InvalidTokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user = await db.get(User, payload.sub)
    if user is None:
        # The token was signed by us, but the referenced account no longer
        # exists. Treat as 401 — a re-login on the client side is the right
        # response.
        raise _unauthorized("User no longer exists.")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole):
    """Build a dependency that only lets through users in the given roles.

    Usage::

        require_admin = require_role(UserRole.ADMIN)

        @router.get("/admin/sellers")
        async def list_sellers(user: Annotated[User, Depends(require_admin)]):
            ...

    Returns the underlying :class:`User` so handlers can use it directly.
    """
    if not roles:
        raise ValueError("require_role() needs at least one role.")
    allowed: frozenset[UserRole] = frozenset(roles)

    async def _checker(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_format_role_error(allowed),
            )
        return user

    # Give the inner function a useful name so FastAPI's debug output and
    # OpenAPI security descriptions are readable.
    _checker.__name__ = f"require_role_{'_'.join(r.value.lower() for r in roles)}"
    return _checker


def _format_role_error(allowed: Iterable[UserRole]) -> str:
    names = ", ".join(sorted(r.value for r in allowed))
    return f"Requires one of the following roles: {names}."


require_buyer = require_role(UserRole.BUYER)
require_seller = require_role(UserRole.SELLER)
require_admin = require_role(UserRole.ADMIN)

BuyerUser = Annotated[User, Depends(require_buyer)]
SellerUser = Annotated[User, Depends(require_seller)]
AdminUser = Annotated[User, Depends(require_admin)]


# ---------------------------------------------------------------------------
# Seller-specific helpers
# ---------------------------------------------------------------------------


async def get_current_seller(user: SellerUser, db: DBSession) -> Seller:
    """Return the current user's :class:`Seller` profile in **any** approval
    state. Used by routes that must work while the application is pending
    review (``GET /sellers/me``, document upload, etc.)."""
    stmt = select(Seller).where(Seller.user_id == user.id)
    seller = (await db.execute(stmt)).scalar_one_or_none()
    if seller is None:
        # Theoretically shouldn't happen — registering through
        # ``/sellers/register`` always creates the row — but a 409 is the
        # honest response if the data is inconsistent.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seller profile is missing for this account.",
        )
    return seller


CurrentSeller = Annotated[Seller, Depends(get_current_seller)]


async def get_current_approved_seller(seller: CurrentSeller) -> Seller:
    """Like :func:`get_current_seller` but only accepts approved sellers.

    Project rule: "verify seller is APPROVED before allowing product creation".
    Use this guard on every endpoint that creates / modifies products and on
    any other action that requires a vetted business identity.
    """
    if seller.approval_status is not SellerApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Seller account is not approved. "
                f"Current status: {seller.approval_status.value}."
            ),
        )
    return seller


ApprovedSeller = Annotated[Seller, Depends(get_current_approved_seller)]


# Re-exported so test code can override them via ``app.dependency_overrides``
# without having to know which guard they wrap.
__all__ = [
    "AdminUser",
    "ApprovedSeller",
    "BuyerUser",
    "CurrentSeller",
    "CurrentUser",
    "SellerUser",
    "get_current_approved_seller",
    "get_current_seller",
    "get_current_user",
    "require_admin",
    "require_buyer",
    "require_role",
    "require_seller",
]
