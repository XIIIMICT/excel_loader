from email.message import EmailMessage
from html import escape
from typing import TYPE_CHECKING

from aiosmtplib import send

if TYPE_CHECKING:
    from collections.abc import Sequence

    from config import Settings


async def send_log_email(
    settings: Settings,
    *,
    log_content: str,
    recipients: Sequence[str],
    subject: str,
) -> None:
    recipient_list = tuple(recipients)
    message = EmailMessage()
    message["From"] = settings.smtp_from_address
    message["To"] = ", ".join(recipient_list)
    message["Subject"] = subject
    message.set_content(log_content)
    message.add_alternative(
        _build_html_body(log_content),
        subtype="html",
    )

    await send(
        message,
        sender=settings.smtp_from_address,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        recipients=recipient_list,
        username=settings.smtp_username,
        password=settings.smtp_password_secret,
        use_tls=False,
        start_tls=True,
    )


def _build_html_body(log_content: str) -> str:
    escaped_log = escape(log_content)
    return (
        "<html><body>"
        "<pre style=\"font-family: Consolas, 'Courier New', monospace; white-space: pre-wrap; margin: 0;\">"
        f"{escaped_log}"
        "</pre>"
        "</body></html>"
    )
