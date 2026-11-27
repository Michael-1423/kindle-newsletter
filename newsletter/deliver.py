"""Deliver the EPUB to Kindle via Amazon's Send-to-Kindle email."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def send_to_kindle(settings: Settings, epub_path: Path, subject: str | None = None) -> bool:
    """Email the EPUB to the Kindle address. Returns True on success."""
    missing = [
        name
        for name, value in {
            "KINDLE_EMAIL": settings.kindle_email,
            "SMTP_HOST": settings.smtp_host,
            "SMTP_USER": settings.smtp_user,
            "SMTP_PASS": settings.smtp_pass,
        }.items()
        if not value
    ]
    if missing:
        log.error("cannot send to Kindle; missing env vars: %s", ", ".join(missing))
        return False

    msg = EmailMessage()
    msg["From"] = f"{settings.sender_name} <{settings.smtp_user}>"
    msg["To"] = settings.kindle_email
    msg["Subject"] = subject or "Your daily newsletter"
    msg.set_content(
        "Your newsletter is attached. Amazon will convert it for your Kindle.\n"
        "If it does not appear, check that this sender is on your Approved Personal "
        "Document Email List in Amazon > Manage Your Content and Devices > Preferences."
    )

    mime, _ = mimetypes.guess_type(epub_path.name)
    with open(epub_path, "rb") as fh:
        msg.add_attachment(
            fh.read(),
            maintype="application",
            subtype="epub+zip",
            filename=epub_path.name,
        )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(msg)
        log.info("sent %s to %s", epub_path.name, settings.kindle_email)
        return True
    except Exception as exc:  # noqa: BLE001 - surface any SMTP failure to caller
        log.error("SMTP send failed: %s", exc)
        return False
