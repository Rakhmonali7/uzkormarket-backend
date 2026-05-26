"""Payment service — FX rate access + payment-record bookkeeping.

The full payment lifecycle (gateway redirects, webhook handling, refunds)
will be filled in once we have concrete gateway credentials per the
project rules. Today this module owns the parts that other services need
right now:

* :func:`get_current_exchange_rate` — reads the KRW→UZS rate from the
  Redis cache populated by the step 12 beat job, with a configured
  fallback so order creation works on a fresh dev environment before the
  worker has run.
* :func:`convert_krw_to_uzs` — the canonical conversion function used by
  ``order_service`` so prices on order_items are computed identically to
  product prices.
* :func:`create_pending_payment` — INSERT a Payment row at status PENDING
  during checkout. Step 11+ will add ``mark_succeeded`` / ``mark_failed``
  driven by gateway webhooks.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.redis import redis_client
from models.payment import Payment, PaymentGateway, PaymentStatus

log = logging.getLogger(__name__)

# Where the FX worker stores the latest KRW→UZS quote. The leading
# ``fx:`` namespace keeps it from colliding with the ``refresh:*`` keys
# used by RefreshTokenStore.
EXCHANGE_RATE_REDIS_KEY = "fx:krw_to_uzs"

# UZS amounts are stored as Numeric(18,2). Quantize at conversion time
# so we never persist precision below a tiyin (UZS subunit).
_UZS_QUANTUM = Decimal("0.01")


# ---------------------------------------------------------------------------
# FX rate
# ---------------------------------------------------------------------------


async def get_current_exchange_rate() -> Decimal:
    """Return the current 1 KRW → UZS rate.

    Looks up Redis first; falls back to ``settings.EXCHANGE_RATE_FALLBACK_KRW_TO_UZS``
    on a cache miss or parse error so checkout works in a freshly-booted
    dev environment before the FX beat job has run.
    """
    try:
        cached = await redis_client.get(EXCHANGE_RATE_REDIS_KEY)
    except Exception:  # noqa: BLE001 — Redis outage must not fail checkout
        log.exception("Redis unavailable for FX lookup; using fallback rate")
        return settings.EXCHANGE_RATE_FALLBACK_KRW_TO_UZS

    if cached is None:
        return settings.EXCHANGE_RATE_FALLBACK_KRW_TO_UZS

    try:
        rate = Decimal(cached)
    except (InvalidOperation, ValueError, TypeError) as exc:
        # ``Decimal("garbage")`` raises InvalidOperation, not ValueError —
        # caught explicitly so a corrupt cache entry can't 500 a checkout.
        log.warning("Bad FX value in Redis (%r): %s; using fallback", cached, exc)
        return settings.EXCHANGE_RATE_FALLBACK_KRW_TO_UZS

    if rate <= 0:
        log.warning("Non-positive FX value in Redis: %s; using fallback", rate)
        return settings.EXCHANGE_RATE_FALLBACK_KRW_TO_UZS
    return rate


def convert_krw_to_uzs(amount_krw: Decimal, rate: Decimal) -> Decimal:
    """Convert a KRW amount to UZS at the supplied rate.

    The caller passes the rate in explicitly (rather than this function
    fetching it) so an entire order is priced against a single
    consistent rate — there's no scenario where item 1 uses one rate and
    item 5 uses another because the cache refreshed mid-checkout.

    Result is quantised to two decimal places using bankers' rounding
    away from zero (``ROUND_HALF_UP``) to match the persistence layer's
    ``Numeric(18, 2)`` columns.
    """
    return (amount_krw * rate).quantize(_UZS_QUANTUM, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Payment record bookkeeping
# ---------------------------------------------------------------------------


def parse_gateway(value: str) -> PaymentGateway:
    """Validate ``OrderCreate.payment_method`` against the gateway enum.

    Lifted out of order_service because the order route handler also
    accepts a ``payment_method`` field and needs the same coercion. We
    return 422 (semantic validation error) rather than 400 to mirror
    Pydantic's normal validation responses.
    """
    try:
        return PaymentGateway(value.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported payment method '{value}'. "
                f"Allowed: {', '.join(g.value for g in PaymentGateway)}."
            ),
        ) from exc


async def create_pending_payment(
    db: AsyncSession,
    *,
    order_id: UUID,
    gateway: PaymentGateway,
    amount_uzs: Decimal,
    amount_krw: Decimal,
    exchange_rate_used: Decimal,
) -> Payment:
    """Insert a Payment row at status PENDING for a freshly-created order.

    Caller is responsible for committing the surrounding transaction —
    we just ``db.add`` the row so it lands together with the Order and
    OrderItems in a single unit of work. Returning the in-flight ORM
    instance is enough; refresh happens after the commit.
    """
    payment = Payment(
        order_id=order_id,
        gateway=gateway,
        amount_uzs=amount_uzs,
        amount_krw=amount_krw,
        exchange_rate_used=exchange_rate_used,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    return payment


__all__ = [
    "EXCHANGE_RATE_REDIS_KEY",
    "convert_krw_to_uzs",
    "create_pending_payment",
    "get_current_exchange_rate",
    "parse_gateway",
]
