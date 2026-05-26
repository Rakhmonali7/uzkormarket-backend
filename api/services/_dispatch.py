"""Best-effort Celery task dispatch — used by every service module.

Service code calls :func:`dispatch_task` with the task name and args,
and this shim takes care of:

* falling back to a log-and-continue if the celery app couldn't be
  imported (``ImportError`` — nominally never happens in normal
  deployments, but useful during partial-stack tests);
* preferring the locally-registered task's ``apply_async`` so that
  ``task_always_eager=True`` (used in tests) is honoured — Celery's
  ``app.send_task`` deliberately ignores eager mode and emits
  ``AlwaysEagerIgnored``;
* swallowing broker outages so a flaky Redis can't 500 the originating
  HTTP request — the worst case is an email doesn't get queued, which
  is recoverable on the next workflow trigger.

Why string-based dispatch rather than ``from workers.email_tasks import
send_X; send_X.delay(...)``? Two reasons: (1) it lets services compile
and run even if a future re-org moves a task module, and (2) it keeps
the service layer free of imports from ``workers/`` — easier on type
checkers and humans alike.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def dispatch_task(name: str, *args: object, **kwargs: object) -> None:
    """Send a task by string name through the shared Celery app.

    Path A (preferred): if the task is registered in this process, use
    ``apply_async`` on the registered task — this respects
    ``task_always_eager`` and integrates with Celery signals correctly.

    Path B (fallback): the task isn't locally known (e.g. a future
    sharding setup where the API process loads only a subset of task
    modules) — go through ``send_task`` against the broker.
    """
    try:
        from workers.celery_app import celery_app  # noqa: PLC0415
    except ImportError:
        log.warning(
            "celery app not yet available; would have dispatched %s args=%s kwargs=%s",
            name,
            args,
            kwargs,
        )
        return
    try:
        task = celery_app.tasks.get(name)
        if task is not None:
            task.apply_async(args=list(args), kwargs=dict(kwargs))
        else:
            celery_app.send_task(
                name, args=list(args), kwargs=dict(kwargs)
            )
    except Exception:  # noqa: BLE001 — broker outage must not 500 the request
        log.exception("Failed to dispatch celery task %s", name)


__all__ = ["dispatch_task"]
