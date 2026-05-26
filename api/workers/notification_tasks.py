"""Internal-facing notifications.

Distinct from :mod:`workers.email_tasks` which targets external users
(buyers, sellers): this module sends to the **admin team** for moderation
events, and to the **banned user** so they know what happened.

Recipient address resolution:

* Admin notifications go to ``settings.ADMIN_NOTIFY_EMAILS``. If that
  list is empty, the task logs a warning and exits — better than
  blackholing into the default From: address.
* User-facing notifications use the user's own email address from the
  ``users`` table.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core.config import settings
from models.document import SellerDocument
from models.seller import Seller
from models.user import User
from workers._email import EmailMessage, send_email
from workers._runner import run_async, task_session
from workers.celery_app import celery_app

log = logging.getLogger(__name__)

_RETRY_POLICY: dict = dict(
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)


# ---------------------------------------------------------------------------
# Admin notifications
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.notification_tasks.notify_admins_new_seller",
    bind=True,
    **_RETRY_POLICY,
)
def notify_admins_new_seller(self, seller_id: str) -> None:
    """Email the admin team when a new seller registers and is awaiting
    review. Triggered by ``services.seller_service.register_seller``."""
    return run_async(_notify_admins_new_seller_impl, seller_id)


async def _notify_admins_new_seller_impl(seller_id: str) -> None:
    recipients = _admin_recipients()
    if not recipients:
        return

    async with task_session() as db:
        stmt = (
            select(Seller)
            .where(Seller.id == UUID(seller_id))
            .options(selectinload(Seller.user))
        )
        seller = (await db.execute(stmt)).scalar_one_or_none()
        if seller is None:
            log.warning("notify_admins_new_seller: seller %s missing", seller_id)
            return
        admin_url = (
            f"{str(settings.FRONTEND_URL).rstrip('/')}/admin/sellers/{seller.id}"
        )
        message = EmailMessage(
            to=recipients,
            subject=(
                f"[KorUZ Mall] New seller awaiting review: {seller.business_name}"
            ),
            body_text=(
                f"A new seller has applied and is awaiting your review.\n"
                "\n"
                f"Business name: {seller.business_name}\n"
                f"BRN: {seller.business_registration_number}\n"
                f"Contact: {seller.user.full_name} <{seller.user.email}>\n"
                "\n"
                f"Review: {admin_url}\n"
            ),
        )
    send_email(message)


# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.notification_tasks.notify_admins_document_uploaded",
    bind=True,
    **_RETRY_POLICY,
)
def notify_admins_document_uploaded(self, document_id: str) -> None:
    """Email the admin team after a seller confirms a document upload.

    Triggered from ``services.seller_service.confirm_document_upload``;
    helps moderators move on a freshly-completed dossier without
    polling."""
    return run_async(_notify_admins_document_uploaded_impl, document_id)


async def _notify_admins_document_uploaded_impl(document_id: str) -> None:
    recipients = _admin_recipients()
    if not recipients:
        return

    async with task_session() as db:
        stmt = (
            select(SellerDocument)
            .where(SellerDocument.id == UUID(document_id))
            .options(
                selectinload(SellerDocument.seller).selectinload(Seller.user)
            )
        )
        document = (await db.execute(stmt)).scalar_one_or_none()
        if document is None:
            log.warning(
                "notify_admins_document_uploaded: document %s missing",
                document_id,
            )
            return
        admin_url = (
            f"{str(settings.FRONTEND_URL).rstrip('/')}/admin/sellers/"
            f"{document.seller_id}#documents"
        )
        message = EmailMessage(
            to=recipients,
            subject=(
                f"[KorUZ Mall] Document uploaded: "
                f"{document.seller.business_name} — {document.document_type.value}"
            ),
            body_text=(
                f"A new {document.document_type.value} document has been "
                f"uploaded and is ready for review.\n"
                "\n"
                f"Seller: {document.seller.business_name} "
                f"({document.seller.user.email})\n"
                f"Filename: {document.original_filename}\n"
                f"Type: {document.mime_type}\n"
                "\n"
                f"Open in admin: {admin_url}\n"
            ),
        )
    send_email(message)


# ---------------------------------------------------------------------------
# User-facing
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.notification_tasks.notify_user_banned",
    bind=True,
    **_RETRY_POLICY,
)
def notify_user_banned(self, user_id: str, reason: str | None) -> None:
    """Tell a user their account was disabled and (optionally) why.

    Triggered by ``services.admin_service.ban_user``. Reason can be
    None — admins aren't required to provide one — and we render the
    message accordingly so the email reads naturally either way.
    """
    return run_async(_notify_user_banned_impl, user_id, reason)


async def _notify_user_banned_impl(user_id: str, reason: str | None) -> None:
    async with task_session() as db:
        user = await db.get(User, UUID(user_id))
        if user is None:
            log.warning("notify_user_banned: user %s missing", user_id)
            return
        contact_email = settings.EMAILS_FROM_ADDRESS or "support@koruz.example"
        reason_block = (
            f"\nReason given:\n  {reason}\n" if reason else ""
        )
        message = EmailMessage(
            to=(user.email,),
            subject="Your KorUZ Mall account has been disabled",
            body_text=(
                f"Hi {user.full_name},\n"
                "\n"
                "Your KorUZ Mall account has been disabled by an "
                "administrator.\n"
                f"{reason_block}"
                "\n"
                "If you believe this is a mistake, please contact us at "
                f"{contact_email}.\n"
                "\n"
                "— The KorUZ Mall team\n"
            ),
        )
    send_email(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_recipients() -> tuple[str, ...]:
    """Return the configured admin notification list as a tuple of strings.

    Empty in dev / test by default — the tasks log and exit cleanly so
    a freshly booted environment doesn't crash on first seller
    registration. Operators populate ``ADMIN_NOTIFY_EMAILS`` (CSV) in
    production.
    """
    addresses = tuple(str(a) for a in settings.ADMIN_NOTIFY_EMAILS)
    if not addresses:
        log.info("ADMIN_NOTIFY_EMAILS not configured; skipping admin notification.")
    return addresses


__all__ = [
    "notify_admins_document_uploaded",
    "notify_admins_new_seller",
    "notify_user_banned",
]
