"""Thin SMTP client for optional handoff alert emails (CLAUDE.md §5, §10).

Standard library only (``smtplib``), so no new dependency (§13). Port 465 uses
implicit TLS; any other port uses STARTTLS. Plain-text mail only.

Like every external call, it has a timeout and retries with backoff, and it
raises ``ExternalServiceError`` so the scheduler's retry-once rule applies.
Duplicates here only reach staff, so every failure is treated as definite
(not ambiguous) and retried.
"""

from __future__ import annotations

import smtplib
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage

from app.config import Settings
from app.http import ExternalServiceError
from app.logging import get_logger

log = get_logger(__name__)

TIMEOUT_SECONDS = 10.0


class EmailSender:
    """Sends one plain-text email per call through ``SMTP_HOST``."""

    def __init__(
        self,
        settings: Settings,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            settings: ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_FROM``, ``SMTP_PASSWORD``.
            smtp_factory: Injected in tests. Defaults to ``smtplib.SMTP`` or
                ``smtplib.SMTP_SSL`` (port 465).
            sleep: Injected in tests.
        """
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._from = settings.smtp_from
        self._password = settings.smtp_password
        self._factory = smtp_factory
        self._sleep = sleep

    def _connect(self) -> smtplib.SMTP:
        if self._factory is not None:
            return self._factory(self._host, self._port, timeout=TIMEOUT_SECONDS)
        context = ssl.create_default_context()
        if self._port == 465:
            return smtplib.SMTP_SSL(
                self._host, self._port, timeout=TIMEOUT_SECONDS, context=context
            )
        smtp = smtplib.SMTP(self._host, self._port, timeout=TIMEOUT_SECONDS)
        smtp.starttls(context=context)
        return smtp

    def send(self, to: str, subject: str, body: str, attempts: int = 3) -> None:
        """Send one email.

        Args:
            to: Recipient address (from ``ALERT_EMAILS``).
            subject: Subject line.
            body: Plain-text body. Never include full phone numbers (§2).
            attempts: Tries, including the first, with 1s/2s backoff.

        Raises:
            ExternalServiceError: Every attempt failed.
        """
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._from, to, subject
        msg.set_content(body)
        last = "no attempt"
        for attempt in range(1, attempts + 1):
            try:
                with self._connect() as smtp:
                    smtp.login(self._from, self._password.get_secret_value())
                    smtp.send_message(msg)
                return
            except (smtplib.SMTPException, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("smtp send failed (%s)", last, extra={"attempt": attempt})
                if attempt < attempts:
                    self._sleep(2 ** (attempt - 1))
        raise ExternalServiceError("smtp", last)
