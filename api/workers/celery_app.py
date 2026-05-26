"""Shared Celery application.

Imported by:

* the worker container (entrypoint ``celery -A workers.celery_app worker``)
* the beat container (``celery -A workers.celery_app beat``)
* every ``workers.*_tasks`` module — they register tasks against this
  instance via the ``@celery_app.task`` decorator
* :mod:`services._dispatch` — calls ``celery_app.send_task(name)`` so
  the API process can enqueue tasks without importing their modules

The configuration is intentionally conservative: ``acks_late=True`` so a
crashing worker doesn't drop a queued email; ``task_reject_on_worker_lost``
to requeue work on hard kills; explicit visibility timeout on the broker
to keep redelivery semantics predictable.

Beat schedule:

* ``update_exchange_rates`` — every ``settings.EXCHANGE_RATE_REFRESH_HOURS``
  hours (default 6).
* ``sync_active_deliveries`` — every 30 minutes; fans out one
  ``sync_delivery_status`` per active order.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from core.config import settings

celery_app = Celery(
    "koruz_mall",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
    # Auto-discover the per-resource task modules. The ``include`` argument
    # forces eager registration at app build time so ``send_task(name)``
    # works even before any task is invoked locally — important for the
    # API process which never imports the task modules but does dispatch
    # by name.
    include=[
        "workers.email_tasks",
        "workers.notification_tasks",
        "workers.delivery_tasks",
        "workers.fx_tasks",
    ],
)

celery_app.conf.update(
    # ---- Serialisation ----
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # UTC throughout. The frontend renders timestamps in the buyer's
    # locale; storing UTC keeps the worker, API, and DB unambiguous.
    timezone="UTC",
    enable_utc=True,
    # ---- Reliability ----
    # Tasks must be idempotent because acks_late=True allows redelivery
    # of any task whose worker died mid-execution. The handlers below
    # are written with that contract in mind.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Cap retries server-side so a misbehaving task (e.g. wedged carrier
    # API) can't loop forever and starve the queue.
    task_default_retry_delay=60,
    task_default_max_retries=5,
    # Drop results after a day — we don't poll AsyncResult anywhere; the
    # backend exists mostly for ``celery inspect`` and beat coordination.
    result_expires=24 * 60 * 60,
    # ---- Worker tuning ----
    worker_prefetch_multiplier=1,  # fairness > throughput for our task mix
    worker_max_tasks_per_child=200,  # recycle to bound memory creep
    # ---- Broker resilience ----
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        # Tasks must complete within an hour or they're considered lost
        # and redelivered; matches our task_default_max_retries window.
        "visibility_timeout": 3600,
    },
)

# ---------------------------------------------------------------------------
# Beat schedule
# ---------------------------------------------------------------------------
# Built from settings so an operator can re-tune the FX cadence per env
# without code changes.
celery_app.conf.beat_schedule = {
    "update-exchange-rates": {
        "task": "workers.fx_tasks.update_exchange_rates",
        # ``crontab(minute=0, hour='*/N')`` runs at the top of the hour
        # every N hours. Falls back to 6h via the settings default.
        "schedule": crontab(
            minute=0,
            hour=f"*/{max(1, settings.EXCHANGE_RATE_REFRESH_HOURS)}",
        ),
        "options": {"expires": 60 * 60},  # don't pile up if a worker is offline
    },
    "sync-active-deliveries": {
        "task": "workers.delivery_tasks.sync_active_deliveries",
        # Every 30 minutes — frequent enough that a buyer reloading the
        # order page sees fresh tracking, infrequent enough not to anger
        # carriers' rate limits.
        "schedule": crontab(minute="*/30"),
        "options": {"expires": 25 * 60},
    },
}

__all__ = ["celery_app"]


# ---------------------------------------------------------------------------
# Task registration
# ---------------------------------------------------------------------------
# ``include=[...]`` above only triggers imports when the *worker process*
# starts. The API process and tests (which run tasks via ``task_always_eager``)
# import this module directly and therefore need the task modules loaded
# eagerly. Doing the imports at the bottom of the file avoids the circular
# ``celery_app -> task_module -> celery_app`` problem because by the time
# we get here ``celery_app`` is already a fully-built attribute of this
# module.
#
# noqa: E402, F401 — order is intentional, side effects only.
from workers import (  # noqa: E402, F401
    delivery_tasks,
    email_tasks,
    fx_tasks,
    notification_tasks,
)
