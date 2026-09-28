"""Building the RFC 822 message for an approved email version."""

from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate

from app.email.models import OutboundEmail


def compose_mime(email: OutboundEmail, *, sender_address: str | None) -> bytes:
    """The message Gmail sends: exactly the approved recipient, subject and body.

    The From display name follows the email's signature, so an order under
    another name doesn't reveal the user's usual name. Without a configured
    sender address the From header is left to Gmail (the account's own).
    """
    message = EmailMessage(policy=SMTP)
    if sender_address:
        message["From"] = formataddr((email.signature_name or "", sender_address))
    message["To"] = email.to_address
    message["Subject"] = email.subject
    message["Date"] = formatdate(localtime=False)
    if email.rfc822_message_id:
        message["Message-ID"] = email.rfc822_message_id
    message.set_content(email.body)
    return message.as_bytes()
