"""Email delivery over SMTP."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from .schemas import Settings


class NotifierError(Exception):
    """An email could not be built or delivered."""


def build_message(
    settings: Settings, recipients: list[str], subject: str, body: str, image: bytes | None = None
) -> EmailMessage:
    sender = settings.email_from or settings.smtp_username
    address = parseaddr(sender)[1]
    if "@" not in address:
        raise NotifierError("no From address is configured (Settings → Email)")
    message = EmailMessage()
    prefix = settings.subject_prefix.strip()
    message["Subject"] = f"{prefix} {subject}" if prefix else subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2])
    message.set_content(body)
    if image:
        message.add_attachment(image, maintype="image", subtype="jpeg", filename="snapshot.jpg")
    return message


class EmailNotifier:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def send(
        self,
        settings: Settings,
        recipients: list[str],
        subject: str,
        body: str,
        image: bytes | None = None,
    ) -> None:
        if not settings.smtp_host:
            raise NotifierError("no SMTP server is configured (Settings → Email)")
        if not recipients:
            raise NotifierError("no recipients: add addresses to the lamp, or admin addresses in Settings")
        message = build_message(settings, recipients, subject, body, image)
        await asyncio.to_thread(self._deliver, settings, message)

    def _deliver(self, settings: Settings, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        try:
            if settings.smtp_security == "ssl":
                client = smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, timeout=self.timeout, context=context
                )
            else:
                client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=self.timeout)
            with client:
                if settings.smtp_security == "starttls":
                    client.starttls(context=context)
                if settings.smtp_username:
                    client.login(settings.smtp_username, settings.smtp_password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifierError(f"SMTP delivery via {settings.smtp_host}:{settings.smtp_port} failed: {exc}") from exc
