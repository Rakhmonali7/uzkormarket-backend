"""Transactional email tasks.

Each task takes a primary-key id (string-serialised UUID, the JSON-safe
form Celery prefers) and re-loads the row inside the task so the message
content is always against the latest state — useful e.g. if an admin
changes a seller's name between dispatch and delivery.

All tasks are idempotent at the "send" boundary: retries may produce
duplicate emails but never inconsistent ones, so :data:`acks_late` and
the auto-retry decorator are safe.
"""

from __future__ import annotations

import logging
from uuid import UUID

from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core.config import settings
from models.order import Order
from models.seller import Seller
from workers._email import EmailMessage, send_email
from workers._runner import run_async, task_session
from workers.celery_app import celery_app

log = logging.getLogger(__name__)

# A default retry policy applied to every email task — a flapping SMTP
# server retries with exponential-ish backoff, capped by Celery's own
# ``task_default_max_retries``.
_RETRY_POLICY: dict = dict(
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    # Retry never autoretries on Retry itself; safe to list explicitly.
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)


# ---------------------------------------------------------------------------
# Seller approval / rejection
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.email_tasks.send_seller_approval_email",
    bind=True,
    **_RETRY_POLICY,
)
def send_seller_approval_email(self, seller_id: str) -> None:
    """Notify a seller their application was approved.

    Loaded with ``selectinload(Seller.user)`` because we project the
    user's full name and email into the message — without eager loading
    the lazy access would crash inside the worker's async loop.
    """
    return run_async(_send_seller_approval_email_impl, seller_id)


async def _send_seller_approval_email_impl(seller_id: str) -> None:
    async with task_session() as db:
        seller = await _load_seller(db, seller_id)
        if seller is None:
            log.warning("send_seller_approval_email: seller %s missing", seller_id)
            return
        message = EmailMessage(
            to=(seller.user.email,),
            subject="Your KorUZ Mall seller application is approved",
            body_text=_render_approval_text(seller),
        )
    send_email(message)


# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.email_tasks.send_seller_rejection_email",
    bind=True,
    **_RETRY_POLICY,
)
def send_seller_rejection_email(self, seller_id: str, reason: str) -> None:
    """Notify a seller their application was rejected with the stored
    reason. ``reason`` is passed in (rather than re-read from the DB)
    so the message reflects the exact string the admin submitted, even
    if the row is later edited."""
    return run_async(_send_seller_rejection_email_impl, seller_id, reason)


async def _send_seller_rejection_email_impl(
    seller_id: str, reason: str
) -> None:
    async with task_session() as db:
        seller = await _load_seller(db, seller_id)
        if seller is None:
            log.warning("send_seller_rejection_email: seller %s missing", seller_id)
            return
        message = EmailMessage(
            to=(seller.user.email,),
            subject="Update on your KorUZ Mall seller application",
            body_text=_render_rejection_text(seller, reason),
        )
    send_email(message)


# ---------------------------------------------------------------------------
# Order confirmation
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.email_tasks.send_order_confirmation_email",
    bind=True,
    **_RETRY_POLICY,
)
def send_order_confirmation_email(self, order_id: str) -> None:
    """Send the buyer an order-placed confirmation summarising the cart
    and payment instructions. Order + items + buyer are eager-loaded in
    one round-trip."""
    return run_async(_send_order_confirmation_email_impl, order_id)


async def _send_order_confirmation_email_impl(order_id: str) -> None:
    async with task_session() as db:
        stmt = (
            select(Order)
            .where(Order.id == UUID(order_id))
            .options(selectinload(Order.items), selectinload(Order.buyer))
        )
        order = (await db.execute(stmt)).scalar_one_or_none()
        if order is None:
            log.warning("send_order_confirmation_email: order %s missing", order_id)
            return
        message = EmailMessage(
            to=(order.buyer.email,),
            subject=f"KorUZ Mall — Order #{str(order.id)[:8]} received",
            body_text=_render_order_confirmation_text(order),
        )
    send_email(message)


# ---------------------------------------------------------------------------
# Delivery update (dispatched from ``sync_delivery_status`` on a status change)
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.email_tasks.send_delivery_update_email",
    bind=True,
    **_RETRY_POLICY,
)
def send_delivery_update_email(self, order_id: str) -> None:
    """Notify the buyer that the delivery status changed.

    Re-loads the Order + Delivery (carrier, tracking number, latest
    status, latest tracking event) so the email reflects the canonical
    DB state at send time, not whatever the carrier polling job saw a
    moment earlier."""
    return run_async(_send_delivery_update_email_impl, order_id)


async def _send_delivery_update_email_impl(order_id: str) -> None:
    async with task_session() as db:
        stmt = (
            select(Order)
            .where(Order.id == UUID(order_id))
            .options(
                selectinload(Order.buyer),
                selectinload(Order.delivery),
            )
        )
        order = (await db.execute(stmt)).scalar_one_or_none()
        if order is None or order.delivery is None:
            log.warning(
                "send_delivery_update_email: order %s missing or no delivery",
                order_id,
            )
            return
        message = EmailMessage(
            to=(order.buyer.email,),
            subject=(
                f"KorUZ Mall — Order #{str(order.id)[:8]} delivery update: "
                f"{order.delivery.status.value}"
            ),
            body_text=_render_delivery_update_text(order),
        )
    send_email(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_seller(db, seller_id: str) -> Seller | None:
    stmt = (
        select(Seller)
        .where(Seller.id == UUID(seller_id))
        .options(selectinload(Seller.user))
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _render_approval_text(seller: Seller) -> str:
    """Plain-text body for the approval email.

    Kept inline rather than in a Jinja template — at this stage the copy
    is short enough that a template engine is overkill, and inline
    f-strings keep the relationship between fields and message obvious
    in code review. Step 14+ may move these into ``templates/emails/``.
    """
    next_steps_url = f"{str(settings.FRONTEND_URL).rstrip('/')}/seller/dashboard"
    return (
        f"Hi {seller.user.full_name},\n"
        "\n"
        f"Good news — your seller application for {seller.business_name} "
        "has been approved.\n"
        "\n"
        "You can now create product listings, manage orders, and arrange "
        "shipping from your seller dashboard:\n"
        f"  {next_steps_url}\n"
        "\n"
        "Welcome aboard!\n"
        "— The KorUZ Mall team\n"
    )


def _render_rejection_text(seller: Seller, reason: str) -> str:
    """Plain-text body for the rejection email."""
    contact_email = settings.EMAILS_FROM_ADDRESS or "support@koruz.example"
    return (
        f"Hi {seller.user.full_name},\n"
        "\n"
        f"Thank you for applying to sell on KorUZ Mall as {seller.business_name}.\n"
        "After reviewing your application, we are unable to approve it at this time.\n"
        "\n"
        "Reason provided:\n"
        f"  {reason}\n"
        "\n"
        "If you believe this was a mistake or have questions, please reply "
        f"to this email or contact {contact_email}.\n"
        "\n"
        "— The KorUZ Mall team\n"
    )


def _render_delivery_update_text(order: Order) -> str:
    """Plain-text body for the delivery-update email."""
    delivery = order.delivery
    assert delivery is not None  # guarded by the task body
    last_event = delivery.tracking_events[-1] if delivery.tracking_events else None
    return (
        f"Hi {order.buyer.full_name},\n"
        "\n"
        f"Your order #{str(order.id)[:8]} has a new delivery status:\n"
        f"  Status: {delivery.status.value}\n"
        f"  Carrier: {delivery.carrier.value}\n"
        f"  Tracking number: {delivery.tracking_number}\n"
        + (
            f"\nLatest update:\n"
            f"  {last_event.get('timestamp', '')} — "
            f"{last_event.get('description', '')} "
            f"({last_event.get('location', '')})\n"
            if last_event
            else ""
        )
        + "\n— The KorUZ Mall team\n"
    )


def _render_order_confirmation_text(order: Order) -> str:
    """Plain-text body for the order-confirmation email."""
    lines = [
        f"Hi {order.buyer.full_name},",
        "",
        f"We received your order #{str(order.id)[:8]}.",
        "",
        "Items:",
    ]
    for item in order.items:
        lines.append(
            f"  - {item.quantity} × product {item.product_id} "
            f"@ {item.unit_price_uzs} UZS / {item.unit_price_krw} KRW"
        )
    lines += [
        "",
        f"Total: {order.total_amount_uzs} UZS  ({order.total_amount_krw} KRW)",
        # ``Order.payment_method`` is a String column (the gateway is
        # captured as an enum on Payment instead). Print the raw value.
        f"Payment method: {order.payment_method}",
        "",
        "We'll send another email once the seller dispatches your parcel.",
        "",
        "— The KorUZ Mall team",
        "",
    ]
    return "\n".join(lines)


# Re-export — useful for tests that want to invoke the task functions directly.
__all__ = [
    "send_delivery_update_email",
    "send_order_confirmation_email",
    "send_seller_approval_email",
    "send_seller_rejection_email",
]


# Suppress unused-import lint for ``Retry`` — kept available so tests can
# import and assert on the retry-exception path without re-importing
# from celery.
_ = Retry
