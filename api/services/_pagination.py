"""Shared async paginator used by every list-style service method.

The pattern across services is identical:

  1. Build a SELECT statement with all WHERE / ORDER BY clauses.
  2. Run ``COUNT(*)`` over the same predicate set.
  3. Run the original SELECT with the right OFFSET / LIMIT.

Doing this in one place is worth the underscore-prefixed module — the
alternative is copy-pasting a five-line helper across eight services and
quietly drifting on whether ``count_stmt`` reuses the original WHERE
clause or not.

The helper is private to ``services/`` (leading underscore), not part of
the package's public surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


async def paginate(
    db: AsyncSession,
    stmt: Select[tuple[ModelT]],
    *,
    page: int,
    page_size: int,
) -> tuple[Sequence[ModelT], int]:
    """Return ``(rows, total)`` for the supplied SELECT statement.

    ``stmt`` must already include any WHERE / JOIN / ORDER BY clauses the
    caller wants applied; this helper only adds ``OFFSET`` / ``LIMIT`` for
    the page slice and runs a separate ``COUNT(*)`` over the same query
    body to compute ``total``.

    Why a sub-select for the count rather than rewriting the WHERE clause
    on a fresh statement? Because services chain ``.where(...)`` calls in
    arbitrary orders and may include joins for filtering — ``select(func.count()).select_from(stmt.subquery())``
    handles all of that correctly without each call site reasoning about it.
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    page_stmt = stmt.offset(offset).limit(page_size)
    rows = (await db.execute(page_stmt)).scalars().all()
    return rows, total


__all__ = ["paginate"]
