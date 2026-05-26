"""Security primitives: password hashing, JWT issuance/decode, refresh-token store.

This module is the single source of truth for every cryptographic operation
in the app. Three concerns live here:

1.  **Password hashing** — ``hash_password`` / ``verify_password`` using
    bcrypt with a configurable cost factor (≥ 12). Inputs are pre-hashed
    with SHA-256 + base64 to safely sidestep bcrypt's 72-byte truncation.
2.  **JWT issuance and decode** — short-lived access tokens (15 min default)
    and longer-lived refresh tokens (7 days default), each with a strict
    ``type`` claim so neither can be replayed on the wrong endpoint.
3.  **Refresh-token whitelist** — :class:`RefreshTokenStore` records every
    issued refresh token's ``jti`` in Redis so that logout, rotation, and
    admin "revoke all sessions" become single-command operations. Storage
    lives on a dedicated Redis DB (``REDIS_AUTH_DB``) separate from Celery.

The module deliberately never reads from the database: callers in the
service layer compose these primitives with user lookups themselves.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import ClassVar, Literal
from uuid import UUID, uuid4

import bcrypt
import jwt
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

from core.config import settings
from models.user import UserRole

TokenType = Literal["access", "refresh"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidTokenError(Exception):
    """Raised when a JWT is missing, expired, has a bad signature, or carries
    an unexpected payload shape.

    Caught at the dependency layer and translated into ``401 Unauthorized``;
    the message text is safe to surface to clients (no internal details)."""


# ---------------------------------------------------------------------------
# Token payload schema
# ---------------------------------------------------------------------------


class TokenPayload(BaseModel):
    """Strongly-typed view of the JWT claims we issue.

    ``jti`` is mandatory on refresh tokens (where it backs Redis-side
    revocation) and absent on access tokens (which are stateless and
    short-lived — there's nothing to revoke).
    """

    model_config = ConfigDict(extra="ignore")

    sub: UUID
    role: UserRole
    type: TokenType
    iat: int
    exp: int
    jti: str | None = None


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def _prepare_password(password: str) -> bytes:
    """Pre-hash a password with SHA-256 + base64 before handing it to bcrypt.

    Bcrypt silently truncates inputs at 72 bytes — passing a 200-byte
    UTF-8 password would mean the suffix is ignored, which is a real
    vulnerability when paired with our schema's 128-char limit. Pre-hashing
    yields a fixed 44-byte ASCII string for any input, which fits inside
    bcrypt's window without changing its security properties. This is the
    same trick passlib's ``bcrypt_sha256`` and Django's password hashers
    use.

    Note: SHA-256 includes no per-user salt (bcrypt itself supplies that),
    so this is purely a length-normalising step, not a separate hash."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password.

    The cost factor is read from :attr:`settings.BCRYPT_ROUNDS` (validated
    at startup to be ≥ 12). The returned string is the standard bcrypt
    encoding (``$2b$<rounds>$<salt><hash>``) and is safe to store as-is in
    the ``users.hashed_password`` column.
    """
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_prepare_password(password), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of a plaintext password against a stored hash.

    Returns ``False`` rather than raising for malformed hashes so callers
    can treat verification failure uniformly without leaking which
    component (password vs hash) was the problem.
    """
    try:
        return bcrypt.checkpw(_prepare_password(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


def _now() -> int:
    """Return the current UTC Unix timestamp.

    Centralising this in one place makes it trivial to monkeypatch from
    tests that need to fast-forward expiry, and keeps both encode and
    decode paths free of any timezone reasoning."""
    return int(time.time())


def _encode(claims: dict) -> str:
    """Sign and encode a JWT with the configured secret + algorithm."""
    return jwt.encode(
        claims,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_access_token(user_id: UUID, role: UserRole) -> str:
    """Issue a short-lived access token (default 15 min).

    The ``role`` claim is duplicated into the JWT so role checks at the
    dependency layer don't need a database round-trip. The user lookup in
    :func:`dependencies.auth.get_current_user` re-fetches the active
    record before allowing access, which is what guards against role
    drift if a user is downgraded mid-token-lifetime.
    """
    iat = _now()
    return _encode(
        {
            "sub": str(user_id),
            "role": role.value,
            "type": "access",
            "iat": iat,
            "exp": iat + settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )


def create_refresh_token(user_id: UUID, role: UserRole) -> tuple[str, str, int]:
    """Issue a refresh token. Returns ``(token, jti, ttl_seconds)``.

    The caller (``auth_service``) is responsible for persisting the ``jti``
    in :class:`RefreshTokenStore` — an unregistered refresh token is
    rejected at validation time even if its signature is valid, which is
    the entire point of the whitelist.

    Returning the TTL alongside the token avoids re-deriving it inside the
    store and ensures the Redis entry expires at exactly the same moment
    as the JWT itself.
    """
    iat = _now()
    ttl = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    jti = uuid4().hex
    token = _encode(
        {
            "sub": str(user_id),
            "role": role.value,
            "type": "refresh",
            "iat": iat,
            "exp": iat + ttl,
            "jti": jti,
        }
    )
    return token, jti, ttl


# ---------------------------------------------------------------------------
# Token decode
# ---------------------------------------------------------------------------


def _decode(
    token: str,
    *,
    expected_type: TokenType,
    require_jti: bool,
) -> TokenPayload:
    """Shared decode + validation pipeline used by both access and refresh.

    Splits the work into three stages so each failure mode has a clear,
    non-leaky message:

    1. ``jwt.decode`` — signature, expiry, and required-claim presence.
    2. ``TokenPayload.model_validate`` — payload shape + type coercion.
    3. ``type`` claim check — refuses to accept e.g. a refresh token on a
       protected access route, even with a valid signature.
    """
    require_claims = ["exp", "iat", "sub", "type"]
    if require_jti:
        require_claims.append("jti")

    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": require_claims},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        # Catch-all for signature errors, missing-claim errors, malformed
        # tokens, etc. The original PyJWT message is intentionally not
        # surfaced — clients only need to know "this token is invalid".
        raise InvalidTokenError("Could not validate credentials.") from exc

    try:
        payload = TokenPayload.model_validate(claims)
    except ValidationError as exc:
        raise InvalidTokenError("Token payload is malformed.") from exc

    if payload.type != expected_type:
        raise InvalidTokenError(
            f"Wrong token type for this endpoint; expected {expected_type}."
        )

    return payload


def decode_access_token(token: str) -> TokenPayload:
    """Decode and validate an **access** token.

    Returns the parsed payload on success; raises :class:`InvalidTokenError`
    on every failure mode (expired, bad signature, wrong type, etc.).
    Callers don't need to know about the underlying PyJWT exception
    hierarchy.
    """
    return _decode(token, expected_type="access", require_jti=False)


def decode_refresh_token(token: str) -> TokenPayload:
    """Decode and validate a **refresh** token.

    Verifies signature, expiry, payload shape, and that ``type == "refresh"``
    and that a ``jti`` claim is present. **Does not** check the Redis
    whitelist — that's :meth:`RefreshTokenStore.is_valid`'s job, and
    keeping it separate lets the auth service compose the two checks
    (e.g. decode first, then verify, then rotate atomically).
    """
    payload = _decode(token, expected_type="refresh", require_jti=True)
    # _decode already enforces the presence of jti via require_claims, but
    # the field is typed as Optional on TokenPayload (because access tokens
    # don't carry it). Asserting here narrows the type for callers without
    # a runtime cost in the happy path.
    assert payload.jti is not None  # noqa: S101  (developer-only invariant)
    return payload


# ---------------------------------------------------------------------------
# Refresh-token whitelist (Redis)
# ---------------------------------------------------------------------------


class RefreshTokenStore:
    """Whitelist-backed refresh-token store.

    Each issued refresh token's ``jti`` is recorded in Redis under
    ``refresh:jti:{jti}`` with a TTL matching the token's ``exp`` claim.
    The set ``refresh:user:{user_id}`` mirrors all live jtis for a user,
    enabling cheap revocation of every active session — used by the admin
    ban flow and by self-service password changes.

    The full validation pipeline at refresh time is:

    1.  :func:`decode_refresh_token` — pure crypto, no I/O.
    2.  :meth:`is_valid` — check the whitelist; reject if missing.
    3.  :meth:`rotate` — atomically delete the old jti and insert a fresh
        one in a single Redis transaction.

    Keeping crypto separate from I/O means unit tests can validate the
    decode logic without spinning up Redis, and integration tests can
    verify the storage layer in isolation.
    """

    JTI_PREFIX: ClassVar[str] = "refresh:jti:"
    USER_INDEX_PREFIX: ClassVar[str] = "refresh:user:"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @classmethod
    def _jti_key(cls, jti: str) -> str:
        return f"{cls.JTI_PREFIX}{jti}"

    @classmethod
    def _user_key(cls, user_id: UUID | str) -> str:
        return f"{cls.USER_INDEX_PREFIX}{user_id}"

    async def register(self, user_id: UUID, jti: str, ttl_seconds: int) -> None:
        """Whitelist a freshly-issued refresh token.

        Uses a single MULTI/EXEC pipeline so the jti record and the user
        index entry land atomically — there's no observable window in
        which one exists without the other.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.setex(self._jti_key(jti), ttl_seconds, str(user_id))
            pipe.sadd(self._user_key(user_id), jti)
            # Pin the user-index TTL to the longest live jti so the set
            # itself never lives longer than its members.
            pipe.expire(self._user_key(user_id), ttl_seconds)
            await pipe.execute()

    async def is_valid(self, jti: str) -> bool:
        """``True`` iff the jti is still whitelisted (i.e. not revoked / expired)."""
        return bool(await self._redis.exists(self._jti_key(jti)))

    async def revoke(self, user_id: UUID, jti: str) -> bool:
        """Invalidate a single refresh token (used on logout).

        Returns ``True`` if the token existed at the moment of revocation,
        ``False`` if it had already been revoked or had expired. Either
        way the post-condition (token cannot be used again) holds.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._jti_key(jti))
            pipe.srem(self._user_key(user_id), jti)
            results = await pipe.execute()
        return bool(results[0])

    async def rotate(
        self,
        user_id: UUID,
        old_jti: str,
        new_jti: str,
        ttl_seconds: int,
    ) -> None:
        """Atomically revoke ``old_jti`` and whitelist ``new_jti``.

        Implements the "each refresh invalidates the old token" requirement
        in a single Redis transaction so a concurrent reuse of the old
        token cannot win a race. Caller must have already verified that
        ``old_jti`` was valid; this method does not re-check.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._jti_key(old_jti))
            pipe.srem(self._user_key(user_id), old_jti)
            pipe.setex(self._jti_key(new_jti), ttl_seconds, str(user_id))
            pipe.sadd(self._user_key(user_id), new_jti)
            pipe.expire(self._user_key(user_id), ttl_seconds)
            await pipe.execute()

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        """Revoke every active refresh token for ``user_id``.

        Used by the admin ban flow (``POST /admin/users/{id}/ban``) and by
        any self-service password-change handler that wants to log out
        all other sessions. Returns the count of tokens actually revoked
        — useful for audit logs.
        """
        user_key = self._user_key(user_id)
        jtis = await self._redis.smembers(user_key)
        if not jtis:
            return 0
        # ``decode_responses=True`` on the client guarantees these are str,
        # but keep a defensive fallback in case the client is reconfigured.
        jti_keys = [
            self._jti_key(j if isinstance(j, str) else j.decode())
            for j in jtis
        ]
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(*jti_keys)
            pipe.delete(user_key)
            await pipe.execute()
        return len(jti_keys)


def get_refresh_token_store() -> RefreshTokenStore:
    """Return a :class:`RefreshTokenStore` bound to the shared Redis client.

    Imported lazily inside the function to avoid pulling in the Redis
    connection at module load time — keeps unit tests of the pure-crypto
    helpers (hashing, JWT encode/decode) free of network dependencies."""
    from core.redis import redis_client

    return RefreshTokenStore(redis_client)


__all__ = [
    "InvalidTokenError",
    "RefreshTokenStore",
    "TokenPayload",
    "TokenType",
    "create_access_token",
    "create_refresh_token",
    "decode_access_token",
    "decode_refresh_token",
    "get_refresh_token_store",
    "hash_password",
    "verify_password",
]
