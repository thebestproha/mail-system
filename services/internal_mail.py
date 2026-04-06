def can_edit_internal_message(connection, message_id: str, username: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sender, status FROM messages WHERE id = %s
            """,
            (message_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return False
    sender, status = row
    return str(sender or "").lower() == str(username or "").lower() and status == "UNREAD"

def can_delete_internal_message(connection, message_id: str, username: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sender, status FROM messages WHERE id = %s
            """,
            (message_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return False
    sender, status = row
    return str(sender or "").lower() == str(username or "").lower() and status == "UNREAD"
from typing import Any


def normalize_internal_recipient(recipient_email: str) -> str:
    recipient = (recipient_email or "").strip().lower()
    if recipient.endswith("@editmail.com"):
        return recipient.split("@", 1)[0]
    return recipient


def send_internal_email(recipient_email: str, subject: str, body: str, current_user: str, sender_callable, attachments: list[dict] | None = None):
    recipient_username = normalize_internal_recipient(recipient_email)
    # Ensure sender is just the username (strip any @...)
    sender_username = current_user.split("@", 1)[0] if "@" in current_user else current_user
    response = sender_callable(
        sender=sender_username,
        receiver=recipient_username,
        subject=subject,
        body=body,
        attachments=attachments or [],
    )
    return {
        "channel": "internal",
        "toast": "Sent via Internal System",
        **response,
    }


def _load_internal_media_by_message_ids(connection, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not message_ids:
        return {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, message_id, filename, mime_type, size, COALESCE(is_inline, FALSE) AS is_inline, COALESCE(content_id, '') AS content_id
            FROM message_attachments
            WHERE message_id = ANY(%s)
            ORDER BY id ASC
            """,
            (message_ids,),
        )
        rows = cursor.fetchall()

    media_by_message: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        message_id = int(row[1])
        media_by_message.setdefault(message_id, []).append(
            {
                "attachment_id": int(row[0]),
                "filename": row[2] or "",
                "mime_type": row[3] or "application/octet-stream",
                "size": int(row[4] or 0),
                "is_inline": bool(row[5]),
                "content_id": row[6] or "",
            }
        )

    return media_by_message


def fetch_internal_attachment(connection, username: str, attachment_id: int):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.message_id, a.filename, a.mime_type, a.content_bytes
            FROM message_attachments a
            JOIN messages m ON m.id = a.message_id
            WHERE a.id = %s
              AND (m.sender = %s OR m.receiver = %s)
            """,
            (attachment_id, username, username),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "message_id": int(row[0]),
        "filename": row[1] or "attachment",
        "mime_type": row[2] or "application/octet-stream",
        "content_bytes": bytes(row[3] or b""),
    }


def list_internal_messages(connection, username: str, folder: str) -> list[dict[str, Any]]:
    folder_name = (folder or "inbox").strip().lower()

    query_map = {
        "inbox": (
            """
            SELECT id, sender, receiver, subject, content, status, timestamp_sent, timestamp_read, checksum, server_id,
                   COALESCE(is_starred, FALSE) AS is_starred,
                   COALESCE(is_spam, FALSE) AS is_spam,
                   hidden_for_sender,
                   hidden_for_receiver
            FROM messages
                        WHERE LOWER(receiver) = LOWER(%s)
              AND hidden_for_receiver = FALSE
                            AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'ACTIVE'
              AND COALESCE(is_spam, FALSE) = FALSE
            ORDER BY timestamp_sent DESC
            """,
            (username,),
        ),
        "sent": (
            """
            SELECT id, sender, receiver, subject, content, status, timestamp_sent, timestamp_read, checksum, server_id,
                   COALESCE(is_starred, FALSE) AS is_starred,
                   COALESCE(is_spam, FALSE) AS is_spam,
                   hidden_for_sender,
                   hidden_for_receiver
            FROM messages
                        WHERE LOWER(sender) = LOWER(%s)
              AND hidden_for_sender = FALSE
                            AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'ACTIVE'
              AND receiver NOT LIKE '%%@%%'
            ORDER BY timestamp_sent DESC
            """,
            (username,),
        ),
        "trash": (
            """
            SELECT id, sender, receiver, subject, content, status, timestamp_sent, timestamp_read, checksum, server_id,
                   COALESCE(is_starred, FALSE) AS is_starred,
                   COALESCE(is_spam, FALSE) AS is_spam,
                   hidden_for_sender,
                   hidden_for_receiver
            FROM messages
                WHERE (LOWER(receiver) = LOWER(%s) AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'TRASHED')
                    OR (LOWER(sender) = LOWER(%s) AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'TRASHED' AND receiver NOT LIKE '%%@%%')
            ORDER BY timestamp_sent DESC
            """,
            (username, username),
        ),
        "starred": (
            """
            SELECT id, sender, receiver, subject, content, status, timestamp_sent, timestamp_read, checksum, server_id,
                   COALESCE(is_starred, FALSE) AS is_starred,
                   COALESCE(is_spam, FALSE) AS is_spam,
                   hidden_for_sender,
                   hidden_for_receiver
            FROM messages
            WHERE COALESCE(is_starred, FALSE) = TRUE
              AND (
                                                                                (LOWER(receiver) = LOWER(%s) AND hidden_for_receiver = FALSE AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'ACTIVE')
                                                                 OR (LOWER(sender) = LOWER(%s) AND hidden_for_sender = FALSE AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'ACTIVE' AND receiver NOT LIKE '%%@%%')
                  )
              AND COALESCE(is_spam, FALSE) = FALSE
            ORDER BY timestamp_sent DESC
            """,
            (username, username),
        ),
        "junk": (
            """
            SELECT id, sender, receiver, subject, content, status, timestamp_sent, timestamp_read, checksum, server_id,
                   COALESCE(is_starred, FALSE) AS is_starred,
                   COALESCE(is_spam, FALSE) AS is_spam,
                   hidden_for_sender,
                   hidden_for_receiver
            FROM messages
            WHERE COALESCE(is_spam, FALSE) = TRUE
              AND (
                                    (LOWER(receiver) = LOWER(%s) AND hidden_for_receiver = FALSE AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'ACTIVE')
                                 OR (LOWER(sender) = LOWER(%s) AND hidden_for_sender = FALSE AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'ACTIVE' AND receiver NOT LIKE '%%@%%')
                  )
            ORDER BY timestamp_sent DESC
            """,
            (username, username),
        ),
    }

    selected_query, params = query_map.get(folder_name, query_map["inbox"])
    with connection.cursor() as cursor:
        cursor.execute(selected_query, params)
        rows = cursor.fetchall()

    message_ids = [int(row[0]) for row in rows if row and row[0] is not None]
    media_by_message = _load_internal_media_by_message_ids(connection, message_ids)

    return [
        {
            "id": str(row[0]),
            "sender": row[1],
            "receiver": row[2],
            "subject": row[3] or "",
            "content": row[4] or "",
            "content_type": "text/plain",
            "status": row[5],
            "timestamp_sent": row[6],
            "timestamp_read": row[7],
            "checksum": row[8],
            "server_id": row[9],
            "is_starred": bool(row[10]),
            "is_spam": bool(row[11]),
            "hidden_for_sender": bool(row[12]),
            "hidden_for_receiver": bool(row[13]),
            "has_attachment": bool(media_by_message.get(int(row[0]))),
            "has_unsupported_body": False,
            "media_items": media_by_message.get(int(row[0]), []),
        }
        for row in rows
    ]


def move_internal_to_trash(connection, username: str, message_id: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET hidden_for_sender = CASE WHEN LOWER(sender) = LOWER(%s) THEN TRUE ELSE hidden_for_sender END,
                hidden_for_receiver = CASE WHEN LOWER(receiver) = LOWER(%s) THEN TRUE ELSE hidden_for_receiver END,
                deleted_for_sender = CASE
                    WHEN LOWER(sender) = LOWER(%s) THEN 'TRASHED'::message_delete_state
                    ELSE deleted_for_sender
                END,
                deleted_for_receiver = CASE
                    WHEN LOWER(receiver) = LOWER(%s) THEN 'TRASHED'::message_delete_state
                    ELSE deleted_for_receiver
                END
            WHERE id = %s
              AND (LOWER(sender) = LOWER(%s) OR LOWER(receiver) = LOWER(%s))
            """,
            (username, username, username, username, message_id, username, username),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0


def _purge_fully_deleted_messages(connection, message_id: str | None = None) -> int:
    with connection.cursor() as cursor:
        if message_id is None:
            cursor.execute(
                """
                SELECT id
                FROM messages
                WHERE COALESCE(deleted_for_sender::text, 'ACTIVE') = 'DELETED'
                  AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'DELETED'
                """
            )
        else:
            cursor.execute(
                """
                SELECT id
                FROM messages
                WHERE id = %s
                  AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'DELETED'
                  AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'DELETED'
                """,
                (message_id,),
            )

        rows = cursor.fetchall()
        message_ids = [int(row[0]) for row in rows if row and row[0] is not None]
        if not message_ids:
            return 0

        cursor.execute(
            "DELETE FROM message_attachments WHERE message_id = ANY(%s)",
            (message_ids,),
        )
        cursor.execute(
            "DELETE FROM messages WHERE id = ANY(%s)",
            (message_ids,),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0


def delete_internal_message_for_user(connection, username: str, message_id: str) -> tuple[int, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET deleted_for_sender = CASE
                    WHEN LOWER(sender) = LOWER(%s) THEN 'DELETED'::message_delete_state
                    ELSE deleted_for_sender
                END,
                hidden_for_sender = CASE
                    WHEN LOWER(sender) = LOWER(%s) THEN TRUE
                    ELSE hidden_for_sender
                END,
                deleted_for_receiver = CASE
                    WHEN LOWER(receiver) = LOWER(%s) THEN 'DELETED'::message_delete_state
                    ELSE deleted_for_receiver
                END,
                hidden_for_receiver = CASE
                    WHEN LOWER(receiver) = LOWER(%s) THEN TRUE
                    ELSE hidden_for_receiver
                END
            WHERE id = %s
              AND (LOWER(sender) = LOWER(%s) OR LOWER(receiver) = LOWER(%s))
              AND (
                    (LOWER(sender) = LOWER(%s) AND COALESCE(deleted_for_sender::text, 'ACTIVE') <> 'DELETED')
                 OR (LOWER(receiver) = LOWER(%s) AND COALESCE(deleted_for_receiver::text, 'ACTIVE') <> 'DELETED')
              )
            """,
            (
                username,
                username,
                username,
                username,
                message_id,
                username,
                username,
                username,
                username,
            ),
        )
        updated = cursor.rowcount if cursor.rowcount is not None else 0

    if updated == 0:
        return 0, 0

    purged = _purge_fully_deleted_messages(connection, message_id=message_id)
    return updated, purged


def empty_internal_trash(connection, username: str) -> int:
    changed_count = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
                        SET deleted_for_sender = 'DELETED'::message_delete_state
                        WHERE LOWER(sender) = LOWER(%s)
                            AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'TRASHED'
            """,
            (username,),
        )
        changed_count += cursor.rowcount if cursor.rowcount is not None else 0

        cursor.execute(
            """
            UPDATE messages
                        SET deleted_for_receiver = 'DELETED'::message_delete_state
                        WHERE LOWER(receiver) = LOWER(%s)
                            AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'TRASHED'
            """,
            (username,),
        )
        changed_count += cursor.rowcount if cursor.rowcount is not None else 0

        changed_count += _purge_fully_deleted_messages(connection)

    return changed_count


def toggle_internal_star(connection, username: str, message_id: str, is_starred: bool) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET is_starred = %s
            WHERE id = %s
                            AND (LOWER(sender) = LOWER(%s) OR LOWER(receiver) = LOWER(%s))
            """,
            (is_starred, message_id, username, username),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0


def mark_internal_spam(connection, username: str, message_id: str, is_spam: bool) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET is_spam = %s
            WHERE id = %s
                            AND (LOWER(sender) = LOWER(%s) OR LOWER(receiver) = LOWER(%s))
            """,
            (is_spam, message_id, username, username),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0
