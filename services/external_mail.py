import base64
from email.mime.text import MIMEText


FOLDER_LABELS = {
    "inbox": ["INBOX"],
    "sent": ["SENT"],
    "trash": ["TRASH"],
    "starred": ["STARRED"],
    "junk": ["SPAM"],
}


def send_external_email(recipient_email: str, subject: str, body: str, current_user: str, sender_callable):
    response = sender_callable(
        sender=current_user,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
    )
    return {
        "channel": "external",
        "toast": "Sent via Gmail",
        **response,
    }


def send_gmail_message(service, to_address: str, subject: str, body: str):
    message = MIMEText(body)
    message["to"] = to_address
    message["subject"] = subject
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw_message}).execute()


def _extract_header(headers: list[dict], key: str) -> str:
    for header in headers:
        if str(header.get("name", "")).lower() == key.lower():
            return header.get("value", "")
    return ""


def list_external_messages(service, folder: str, max_results: int = 20):
    labels = FOLDER_LABELS.get((folder or "inbox").lower(), ["INBOX"])
    listing = (
        service.users()
        .messages()
        .list(userId="me", labelIds=labels, maxResults=max_results)
        .execute()
    )
    refs = listing.get("messages", [])

    messages = []
    for ref in refs:
        message_id = ref.get("id")
        if not message_id:
            continue
        data = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = data.get("payload", {}).get("headers", [])
        label_ids = data.get("labelIds", [])
        messages.append(
            {
                "id": data.get("id"),
                "sender": _extract_header(headers, "From"),
                "receiver": _extract_header(headers, "To"),
                "subject": _extract_header(headers, "Subject"),
                "timestamp_sent": _extract_header(headers, "Date"),
                "snippet": data.get("snippet", ""),
                "status": "READ" if "UNREAD" not in label_ids else "UNREAD",
                "is_starred": "STARRED" in label_ids,
                "is_spam": "SPAM" in label_ids,
                "labels": label_ids,
                "source": "external",
            }
        )
    return messages


def external_move_to_trash(service, message_id: str):
    return service.users().messages().trash(userId="me", id=message_id).execute()


def external_delete_message(service, message_id: str):
    return service.users().messages().delete(userId="me", id=message_id).execute()


def external_toggle_star(service, message_id: str, is_starred: bool):
    body = {
        "addLabelIds": ["STARRED"] if is_starred else [],
        "removeLabelIds": [] if is_starred else ["STARRED"],
    }
    return service.users().messages().modify(userId="me", id=message_id, body=body).execute()


def external_mark_spam(service, message_id: str, is_spam: bool):
    if is_spam:
        body = {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]}
    else:
        body = {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]}
    return service.users().messages().modify(userId="me", id=message_id, body=body).execute()
