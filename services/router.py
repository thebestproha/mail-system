from services.external_mail import send_external_email
from services.internal_mail import send_internal_email

_internal_sender = None
_external_sender = None


def configure_route_handlers(internal_sender, external_sender):
    global _internal_sender, _external_sender
    _internal_sender = internal_sender
    _external_sender = external_sender


def route_message(recipient_email, subject, body, current_user, attachments=None):
    if _internal_sender is None or _external_sender is None:
        raise RuntimeError("Router handlers are not configured")

    recipient = (recipient_email or "").strip().lower()

    if "@" not in recipient:
        raise ValueError(
            "Recipient must include a domain. Use @editmail.com for internal mail, or @gmail.com/@yahoo.com/etc for external mail"
        )

    parts = recipient.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            "Invalid recipient format. Use username@editmail.com for internal mail or a valid external email"
        )

    if recipient.endswith("@editmail.com"):
        return send_internal_email(
            recipient_email=recipient,
            subject=subject,
            body=body,
            current_user=current_user,
            attachments=attachments or [],
            sender_callable=_internal_sender,
        )

    # Otherwise, treat as external mail
    return send_external_email(
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        current_user=current_user,
        attachments=attachments or [],
        sender_callable=_external_sender,
    )
