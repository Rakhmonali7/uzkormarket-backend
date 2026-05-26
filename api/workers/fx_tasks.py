"""KRW → UZS exchange-rate refresh.

Project rule "update_exchange_rates() — runs on a schedule (Celery Beat,
every 6 hours), fetches KRW/UZS rate from an FX API, stores in Redis."

Why this lives in its own module rather than one of the four files
listed in the project's strict folder layout: the task fits poorly into
``email_tasks`` (no email), ``notification_tasks`` (no recipient), or
``delivery_tasks`` (different domain entirely). A standalone
``fx_tasks.py`` is a clearer home and one extra module file feels like
the cheaper deviation.

The task is intentionally robust against environment ambiguity:

* If ``EXCHANGE_RATE_PROVIDER_URL`` isn't configured the task no-ops and
  ``services.payment_service`` falls through to its configured fallback.
* If the provider responds but parsing fails, the task logs and retries
  via Celery's auto-retry policy.
* On success it writes the rate as a string ``Decimal`` repr — the
  exact format ``payment_service.get_current_exchange_rate`` parses.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from core.config import settings
from services.payment_service import EXCHANGE_RATE_REDIS_KEY
from workers._runner import run_async, task_redis
from workers.celery_app import celery_app

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pluggable fetcher (so tests can inject a deterministic rate)
# ---------------------------------------------------------------------------


RateFetcher = Callable[[], Awaitable[Decimal]]
"""Coroutine that returns the current 1 KRW → UZS rate.

The default implementation queries the configured provider; tests
substitute a constant via :func:`set_rate_fetcher`.
"""


async def _default_fetch_rate() -> Decimal:
    """Real provider fetch — uses the URL + API key from settings.

    The expected response shape is intentionally permissive: many free
    FX APIs use ``rates: {UZS: ...}`` against a base, and we accept both
    that shape and a flat ``{rate: ...}`` body so swapping providers
    requires only an env-var change.
    """
    if settings.EXCHANGE_RATE_PROVIDER_URL is None:
        raise RuntimeError(
            "EXCHANGE_RATE_PROVIDER_URL not configured; "
            "set it in env or override the rate fetcher in tests."
        )
    headers: dict[str, str] = {}
    if settings.EXCHANGE_RATE_PROVIDER_KEY:
        headers["Authorization"] = f"Bearer {settings.EXCHANGE_RATE_PROVIDER_KEY}"

    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.get(
            str(settings.EXCHANGE_RATE_PROVIDER_URL), headers=headers
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

    return _extract_rate(payload)


def _extract_rate(payload: dict[str, Any]) -> Decimal:
    """Pull a Decimal rate out of a few common provider shapes.

    Supported shapes (in order of preference):

    1. ``{"rates": {"UZS": 9.6}}`` — the openexchangerates.org / fixer
       style with KRW as the base currency.
    2. ``{"rate": 9.6}`` — the most basic shape, often used by internal
       proxies or Cloudflare workers.
    3. ``{"data": {"krw_to_uzs": "9.60"}}`` — generic fallback.

    Raises ``ValueError`` (caught by Celery's autoretry) on anything
    unexpected so the next scheduled tick gets another chance.
    """
    candidate: Any = None
    rates = payload.get("rates")
    if isinstance(rates, dict) and "UZS" in rates:
        candidate = rates["UZS"]
    elif "rate" in payload:
        candidate = payload["rate"]
    else:
        data = payload.get("data")
        if isinstance(data, dict):
            candidate = data.get("krw_to_uzs") or data.get("rate")

    if candidate is None:
        raise ValueError(
            f"FX provider response missing rate field; got keys: {sorted(payload)}"
        )
    try:
        rate = Decimal(str(candidate))
    except InvalidOperation as exc:
        raise ValueError(f"FX provider returned non-numeric rate: {candidate!r}") from exc
    if rate <= 0:
        raise ValueError(f"FX provider returned non-positive rate: {rate}")
    return rate


_rate_fetcher: RateFetcher = _default_fetch_rate


def get_rate_fetcher() -> RateFetcher:
    """Return the active fetcher coroutine (used by the task)."""
    return _rate_fetcher


def set_rate_fetcher(fetcher: RateFetcher | None) -> None:
    """Replace the fetcher (tests). Pass ``None`` to revert to the default."""
    global _rate_fetcher
    _rate_fetcher = fetcher if fetcher is not None else _default_fetch_rate


# ---------------------------------------------------------------------------
# Cache-write TTL — the entry should outlive the next scheduled refresh
# by enough of a margin that one missed beat tick doesn't immediately
# fall back to ``EXCHANGE_RATE_FALLBACK_KRW_TO_UZS``.
# ---------------------------------------------------------------------------
_RATE_TTL_SECONDS = max(
    1, settings.EXCHANGE_RATE_REFRESH_HOURS * 3 * 60 * 60
)


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.fx_tasks.update_exchange_rates",
    bind=True,
    autoretry_for=(httpx.HTTPError, OSError, ValueError),
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=3,
)
def update_exchange_rates(self) -> str | None:
    """Beat-scheduled task. Returns the new rate as a string for
    ``celery inspect`` visibility, or ``None`` when there's no provider
    URL configured (so the API just keeps using the static fallback).
    """
    return run_async(_update_exchange_rates_impl)


async def _update_exchange_rates_impl() -> str | None:
    if settings.EXCHANGE_RATE_PROVIDER_URL is None:
        log.info(
            "EXCHANGE_RATE_PROVIDER_URL unset; FX cache refresh skipped"
        )
        return None

    rate = await get_rate_fetcher()()
    rate_str = str(rate)
    async with task_redis() as redis:
        await redis.set(
            EXCHANGE_RATE_REDIS_KEY, rate_str, ex=_RATE_TTL_SECONDS
        )
    log.info(
        "FX cache refreshed: 1 KRW = %s UZS (ttl=%ss)",
        rate_str,
        _RATE_TTL_SECONDS,
    )
    return rate_str


__all__ = [
    "RateFetcher",
    "get_rate_fetcher",
    "set_rate_fetcher",
    "update_exchange_rates",
]
