"""Shared async Redis client for direct app-side reads and writes.

Celery brings its own broker/backend client; **this module is for everything
else** — currently the refresh-token whitelist (step 9) and, soon, the FX
rate cache populated by the Celery beat job (step 12).

Why a dedicated module rather than co-locating with ``core/security.py``?
The same reason :mod:`core.database` owns the SQLAlchemy engine while
:mod:`dependencies.db` owns the request-scoped session: keeping the
connection pool in one place lets unrelated callers share it without
sprouting circular imports through the security layer.

The pool is configured against ``REDIS_AUTH_DB`` (a separate logical
database from the one Celery uses) so flushing it during a forced auth
reset can never corrupt queue state.
"""

from __future__ import annotations

from redis.asyncio import Redis

from core.config import settings

redis_client: Redis = Redis.from_url(
    str(settings.REDIS_URL),
    db=settings.REDIS_AUTH_DB,
    # Strings everywhere — UUIDs, jtis, role names. We never store binary
    # blobs in this DB, so eliminating the bytes ↔ str dance at every call
    # site is a clear win.
    decode_responses=True,
    encoding="utf-8",
    # Cheap heartbeat that catches dead connections sitting idle in the
    # pool (NAT timeouts, container restarts) before a request hits them.
    health_check_interval=30,
)
"""Process-wide async Redis client.

Backed by a connection pool that lazily opens its first TCP connection on
the first issued command. Safe to import at module load time — no I/O
happens until something is actually called.
"""


async def dispose_redis() -> None:
    """Close the pool — wired into FastAPI's shutdown hook in ``main.py``."""
    await redis_client.aclose()


__all__ = ["redis_client", "dispose_redis"]
