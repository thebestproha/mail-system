import base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders


FOLDER_LABELS = {
    "inbox": ["INBOX"],
    "sent": ["SENT"],
    "trash": ["TRASH"],
    "starred": ["STARRED"],
    "junk": ["SPAM"],
}


def send_external_email(recipient_email: str, subject: str, body: str, current_user: str, sender_callable, attachments: list[dict] | None = None):
    response = sender_callable(
        sender=current_user,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        attachments=attachments or [],
    )
    return {
        "channel": "external",
        "toast": "Sent via Gmail",
        **response,
    }


def send_gmail_message(service, to_address: str, subject: str, body: str, attachments: list[dict] | None = None):
    items = attachments or []

    if items:
        message = MIMEMultipart()
        message.attach(MIMEText(body, "plain", "utf-8"))
        for item in items:
            mime_type = str(item.get("mime_type") or "application/octet-stream")
            main_type, _, sub_type = mime_type.partition("/")
            if not main_type or not sub_type:
                main_type, sub_type = "application", "octet-stream"

            part = MIMEBase(main_type, sub_type)
            part.set_payload(item.get("data") or b"")
            encoders.encode_base64(part)
            filename = item.get("filename") or "attachment"
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            message.attach(part)
    else:
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


def _payload_has_attachment(payload: dict | None) -> bool:
    if not payload:
        return False

    if payload.get("filename"):
        return True

    for part in payload.get("parts", []) or []:
        if _payload_has_attachment(part):
            return True

    return False


def _payload_has_unsupported_body(payload: dict | None) -> bool:
    if not payload:
        return False

    mime_type = str(payload.get("mimeType") or "").lower()
    if mime_type and mime_type not in {"text/plain", "text/html", "multipart/alternative", "multipart/mixed", "multipart/related"}:
        return True

    for part in payload.get("parts", []) or []:
        if _payload_has_unsupported_body(part):
            return True

    return False


def _decode_base64url(data: str) -> bytes:
    if not data:
        return b""
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _iter_payload_parts(payload: dict | None):
    if not payload:
        return
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _iter_payload_parts(part)


def _extract_message_body(payload: dict | None) -> tuple[str, str]:
    html_chunks: list[str] = []
    plain_chunks: list[str] = []

    for part in _iter_payload_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()
        body_data = ((part.get("body") or {}).get("data") or "").strip()
        if not body_data:
            continue

        try:
            decoded_text = _decode_base64url(body_data).decode("utf-8", errors="replace")
        except Exception:
            continue

        if mime_type == "text/html":
            html_chunks.append(decoded_text)
        elif mime_type == "text/plain":
            plain_chunks.append(decoded_text)

    if html_chunks:
        return "\n".join(html_chunks), "text/html"
    if plain_chunks:
        return "\n".join(plain_chunks), "text/plain"
    return "", "text/plain"


def _normalize_content_id(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    return text


def _collect_media_items(payload: dict | None) -> list[dict]:
    media_items: list[dict] = []

    for part in _iter_payload_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()

        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        body_data = (body.get("data") or "").strip()
        filename = (part.get("filename") or "").strip()
        size = int(body.get("size") or 0)
        is_container = mime_type.startswith("multipart/")
        headers = part.get("headers", []) or []
        content_id = _normalize_content_id(_extract_header(headers, "Content-Id"))
        disposition = str(_extract_header(headers, "Content-Disposition") or "").lower()

        if is_container:
            continue

        if not (filename or attachment_id or content_id or disposition.startswith("inline")):
            continue

        is_inline = bool(content_id) or disposition.startswith("inline")

        item: dict = {
            "mime_type": mime_type,
            "filename": filename,
            "content_id": content_id,
            "is_inline": is_inline,
            "size": size,
        }

        if body_data and not attachment_id:
            try:
                b64 = base64.b64encode(_decode_base64url(body_data)).decode("utf-8")
                item["data_url"] = f"data:{mime_type};base64,{b64}"
            except Exception:
                pass
        elif attachment_id:
            item["attachment_id"] = attachment_id

        media_items.append(item)

    return media_items


def list_external_messages(
    service,
    folder: str,
    max_results: int = 20,
    page_token: str | None = None,
):
    folder_key = (folder or "inbox").lower()

    list_params = {
        "userId": "me",
        "maxResults": max_results,
    }

    if page_token:
        list_params["pageToken"] = page_token

    if folder_key == "sent":
        list_params["q"] = "in:sent"
    elif folder_key == "starred":
        list_params["labelIds"] = ["STARRED"]
    else:
        labels = FOLDER_LABELS.get(folder_key, ["INBOX"])
        list_params["labelIds"] = labels

    listing = service.users().messages().list(**list_params).execute()

    refs = listing.get("messages", [])
    next_page_token = listing.get("nextPageToken")

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
                format="full",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        payload = data.get("payload", {}) or {}
        headers = payload.get("headers", [])
        label_ids = data.get("labelIds", [])
        has_attachment = _payload_has_attachment(payload)
        has_unsupported_body = _payload_has_unsupported_body(payload)
        body_content, body_content_type = _extract_message_body(payload)
        media_items = _collect_media_items(payload)

        messages.append(
            {
                "id": data.get("id"),
                "sender": _extract_header(headers, "From"),
                "receiver": _extract_header(headers, "To"),
                "subject": _extract_header(headers, "Subject"),
                "timestamp_sent": _extract_header(headers, "Date"),
                "snippet": data.get("snippet", ""),
                "content": body_content or data.get("snippet", ""),
                "content_type": body_content_type,
                "status": "READ" if "UNREAD" not in label_ids else "UNREAD",
                "is_starred": "STARRED" in label_ids,
                "is_spam": "SPAM" in label_ids,
                "labels": label_ids,
                "source": "external",
                "has_attachment": has_attachment,
                "has_unsupported_body": has_unsupported_body,
                "media_items": media_items,
            }
        )

    return {
        "messages": messages,
        "next_page_token": next_page_token,
    }


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


def external_mark_read(service, message_id: str):
    body = {
        "removeLabelIds": ["UNREAD"],
        "addLabelIds": [],
    }
    return service.users().messages().modify(userId="me", id=message_id, body=body).execute()


def fetch_external_attachment_bytes(service, message_id: str, attachment_id: str) -> bytes:
    payload = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = (payload or {}).get("data")
    if not data:
        return b""
    return _decode_base64url(data)
