"""Async SQLAlchemy engine, session factory, declarative ``Base``, and the
common model mixins used across every ORM model.

Every ORM model lives in ``models/*.py`` and inherits from ``Base`` plus the
``UUIDPrimaryKeyMixin`` and ``TimestampMixin`` defined here, so the project
rule "every model has UUID id + created_at + updated_at" is enforced by
construction rather than convention.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from core.config import settings

# ---------------------------------------------------------------------------
# Naming convention — keeps Alembic autogenerate output deterministic and
# makes constraint names self-explanatory in the database.
# ---------------------------------------------------------------------------
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base. All ORM models inherit from this."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Mixins — composed onto every model. Order matters: put the mixins *before*
# Base in the inheritance list so SQLAlchemy resolves the metadata correctly.
#
#     class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
#         __tablename__ = "users"
#         ...
# ---------------------------------------------------------------------------
class UUIDPrimaryKeyMixin:
    """Adds a UUID primary key generated client-side via :func:`uuid.uuid4`."""

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` columns maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Engine + session factory — one engine per process, sessions per request.
# ---------------------------------------------------------------------------
engine: AsyncEngine = create_async_engine(
    settings.assembled_database_url,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    future=True,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    class_=AsyncSession,
)

# Note: the request-scoped session generator (``get_async_session``) lives in
# ``dependencies/db.py`` per the project rules — this module owns the engine
# and session factory only.


async def dispose_engine() -> None:
    """Dispose the engine — call from FastAPI's shutdown hook."""
    await engine.dispose()
