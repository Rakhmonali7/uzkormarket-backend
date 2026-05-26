"""Async-in-Celery helper.

Celery's prefork worker pool drives task functions synchronously. Our
service / DB layer is async-only (asyncpg), so every task needs to:

  1. Open an asyncio event loop.
  2. Run the async coroutine that does the work.
  3. Tear the loop down.

Doing that with the process-wide ``core.database.engine`` doesn't work,
because :class:`AsyncEngine`'s connection pool binds to whichever loop
opens the first connection. The next task call uses a *new* loop and
trips over "Future attached to a different loop" / "Event loop is closed"
errors.

Two options to fix that:

* Long-lived loop: spin up one event loop in the worker process and
  reuse it. Requires a worker-init signal handler and means a hung task
  blocks all subsequent tasks on the same worker.
* Per-task loop and per-task engine: each task call gets its own loop
  *and* its own ``AsyncEngine``. Pools don't outlive a task.

We picked the per-task approach — the cost is one extra DB handshake
(~1 ms locally) per task, dwarfed by the Celery dispatch overhead, and
the model is dead-simple to reason about.

Usage::

    @celery_app.task
    def my_task(order_id: str):
        return run_async(_my_task_impl, order_id)

    async def _my_task_impl(order_id: str):
        async with task_session() as db:
            ...
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator, ParamSpec, TypeVar

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import settings

log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def run_async(
    coro_factory: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run ``coro_factory(*args, **kwargs)`` in a fresh asyncio loop.

    Two callers, two paths:

    * **Production worker.** Celery's prefork pool runs the task in a
      sync thread with no event loop. We build one, run, close.
    * **Eager-mode tests.** A pytest harness or smoke-test driver calls
      the task from inside an already-running event loop. ``asyncio``
      forbids nesting: ``loop.run_until_complete`` raises
      ``RuntimeError: Cannot run the event loop while another loop is
      running``. We side-step that by handing the work off to a single
      worker thread, which has no loop of its own, and ``asyncio.run``
      there.

    Either way, exceptions propagate unchanged so Celery's retry
    machinery and the test harness both see the original error.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_in_fresh_loop(coro_factory, *args, **kwargs)

    # Already inside an event loop (eager-mode tests, FastAPI
    # synchronous endpoint calling into us, etc.) — escape to a thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_in_fresh_loop, coro_factory, *args, **kwargs)
        return future.result()


def _run_in_fresh_loop(
    coro_factory: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Build a fresh loop, run the coroutine, drain leftovers, close."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro_factory(*args, **kwargs))
    finally:
        try:
            # Drain any cancelled background tasks before closing — keeps
            # asyncpg's transport-close warnings out of the worker logs.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        finally:
            loop.close()
            asyncio.set_event_loop(None)


@asynccontextmanager
async def task_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional async session bound to a per-task engine.

    The engine is created and disposed within the same event loop the
    session uses, so its connection pool is loop-clean. ``pool_size=1``
    because a Celery task is single-threaded and has no need for
    concurrency on its own DB pool — we'd just be paying for idle
    sockets. Errors propagate to the caller (and through the Celery
    retry decorator); successful completion commits implicitly because
    individual task helpers commit at sensible boundaries themselves.
    """
    engine = create_async_engine(
        settings.assembled_database_url,
        # No need for the API-process-style 30 connections per worker.
        pool_size=1,
        max_overflow=2,
        pool_pre_ping=True,
        echo=False,
    )
    sessionmaker = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    try:
        async with sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


@asynccontextmanager
async def task_redis() -> AsyncIterator[Redis]:
    """Yield a Redis client whose connection pool is local to the task.

    Same rationale as :func:`task_session`: ``redis.asyncio.Redis``'s
    pool binds to whichever event loop opens its first connection.
    Using the process-wide :data:`core.redis.redis_client` from inside
    a per-task loop trips the "Future attached to a different loop"
    error the moment the main thread had previously used the same
    client (common in tests). A short-lived per-task pool dodges the
    issue entirely.
    """
    client = Redis.from_url(
        str(settings.REDIS_URL),
        db=settings.REDIS_AUTH_DB,
        decode_responses=True,
        encoding="utf-8",
    )
    try:
        yield client
    finally:
        await client.aclose()


__all__ = ["run_async", "task_redis", "task_session"]
