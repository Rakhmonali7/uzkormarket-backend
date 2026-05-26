"""Request-scoped database session dependency.

The engine and session factory live in :mod:`core.database`; this module owns
the FastAPI dependency that yields a session for the lifetime of a single
request and the convenience type alias used by route handlers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import SessionLocal


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional ``AsyncSession`` scoped to a single request.

    On any unhandled exception the transaction is rolled back before the
    session is returned to the pool. Committing is the caller's
    responsibility — services should ``await session.commit()`` once their
    unit of work is complete.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Ergonomic alias for route signatures::
#
#     async def list_things(db: DBSession) -> ...
#
# Using a single alias project-wide keeps handler signatures short and lets
# us switch to a different injection mechanism later in exactly one place.
DBSession = Annotated[AsyncSession, Depends(get_async_session)]
