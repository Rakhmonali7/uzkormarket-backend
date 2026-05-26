"""Email transport — SMTP in production, log-only in dev, in-memory in tests.

A thin shim so ``workers.email_tasks`` doesn't deal with SMTP details
directly. The chosen backend depends on ``settings.SMTP_HOST``:

* **Unset** → :class:`LoggingEmailBackend`. Each ``send`` writes the
  message body to the worker log at INFO. This is the dev / test default
  — running the stack via ``docker compose up`` doesn't need a real SMTP
  server before you can exercise the approval flow.
* **Set** → :class:`SMTPEmailBackend`. Connects, optionally STARTTLS,
  optionally authenticates, and sends the MIME message synchronously.

Tests override the active backend via :func:`set_email_backend` to
capture sent messages in memory without monkey-patching ``smtplib``.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as MIMEEmail
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

from core.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    """Structured email payload — built by task helpers, sent by a backend.

    Kept frozen / dataclass so a captured message in tests is comparable
    field-for-field via simple equality. ``body_html`` is optional: a
    text-only email is fine for our internal notifications. The frontend
    will likely add HTML wrappers later via a Jinja template.
    """

    to: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str | None = None
    reply_to: str | None = None
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class EmailBackend(Protocol):
    """Backend interface — exists so tests can substitute a recorder."""

    def send(self, message: EmailMessage) -> None: ...


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


class LoggingEmailBackend:
    """Default backend in dev / test environments.

    Writes the message to the worker log at INFO; doesn't try to render
    HTML or do anything fancy. Useful as a "did the right task fire?"
    signal during local development."""

    def send(self, message: EmailMessage) -> None:
        log.info(
            "[email:logging] to=%s | subject=%s\n%s",
            list(message.to),
            message.subject,
            message.body_text,
        )


class SMTPEmailBackend:
    """Synchronous SMTP backend — used when ``SMTP_HOST`` is configured."""

    def send(self, message: EmailMessage) -> None:
        if not settings.SMTP_HOST or not settings.EMAILS_FROM_ADDRESS:
            # Defensive — operator unset SMTP after the backend was
            # picked. Better to log and keep the queue moving than 500
            # the task and force a retry storm.
            log.warning(
                "SMTP backend selected but SMTP_HOST/EMAILS_FROM_ADDRESS missing; "
                "dropping message to %s",
                list(message.to),
            )
            return
        mime = _to_mime(message)
        with smtplib.SMTP(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=30
        ) as smtp:
            smtp.ehlo()
            if settings.SMTP_USE_TLS:
                smtp.starttls()
                smtp.ehlo()
            if settings.SMTP_USER:
                smtp.login(
                    settings.SMTP_USER, settings.SMTP_PASSWORD or ""
                )
            smtp.send_message(mime)


@dataclass
class InMemoryEmailBackend:
    """Test-only backend: records every send into a list.

    Used in :file:`tests/...` and the step-12 smoke test to assert that
    a task fired and produced the expected message without actually
    talking to an SMTP server.
    """

    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_active_backend: EmailBackend | None = None


def get_email_backend() -> EmailBackend:
    """Return the current process-wide email backend.

    First call resolves based on settings; subsequent calls return the
    cached instance. Tests override via :func:`set_email_backend`.
    """
    global _active_backend
    if _active_backend is None:
        if settings.SMTP_HOST:
            _active_backend = SMTPEmailBackend()
        else:
            _active_backend = LoggingEmailBackend()
    return _active_backend


def set_email_backend(backend: EmailBackend | None) -> None:
    """Replace the active backend (for tests). Pass ``None`` to reset."""
    global _active_backend
    _active_backend = backend


def send_email(message: EmailMessage) -> None:
    """Send via the currently configured backend. Synchronous."""
    get_email_backend().send(message)


# ---------------------------------------------------------------------------
# MIME construction
# ---------------------------------------------------------------------------


def _to_mime(message: EmailMessage) -> MIMEEmail:
    """Convert our :class:`EmailMessage` into a stdlib MIME message.

    Handles plain-text only, plain+html (multipart/alternative), and
    sets the conventional ``Date``/``Message-ID`` headers that some
    servers grade reputation against.
    """
    mime = MIMEEmail()
    from_addr = str(settings.EMAILS_FROM_ADDRESS or "")
    mime["From"] = formataddr((settings.EMAILS_FROM_NAME, from_addr))
    mime["To"] = ", ".join(message.to)
    mime["Subject"] = message.subject
    mime["Date"] = formatdate(localtime=False, usegmt=True)
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "koruz.local"
    mime["Message-ID"] = make_msgid(domain=domain)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    for name, value in message.headers:
        mime[name] = value

    if message.body_html is not None:
        mime.set_content(message.body_text)
        mime.add_alternative(message.body_html, subtype="html")
    else:
        mime.set_content(message.body_text)
    return mime


__all__ = [
    "EmailBackend",
    "EmailMessage",
    "InMemoryEmailBackend",
    "LoggingEmailBackend",
    "SMTPEmailBackend",
    "get_email_backend",
    "send_email",
    "set_email_backend",
]
