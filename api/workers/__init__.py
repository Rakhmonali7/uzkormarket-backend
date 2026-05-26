"""Celery worker package — task definitions and the shared Celery app.

Public modules:

* :mod:`workers.celery_app` — the Celery instance plus broker/backend
  config and the beat schedule.
* :mod:`workers.email_tasks` — transactional emails sent from service
  layer events.
* :mod:`workers.notification_tasks` — internal admin notifications.
* :mod:`workers.delivery_tasks` — carrier tracking polls.
* :mod:`workers.fx_tasks` — KRW→UZS rate refresh (beat-scheduled).

Service code never imports task functions directly — it dispatches by
string name through :func:`services._dispatch.dispatch_task` so a service
unit can run without Celery installed (helpful for tests and the API
container at boot before the worker is up).
"""
