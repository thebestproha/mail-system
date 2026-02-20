from typing import Any


def normalize_internal_recipient(recipient_email: str) -> str:
    recipient = (recipient_email or "").strip().lower()
    if recipient.endswith("@editmail.com"):
        return recipient.split("@", 1)[0]
    return recipient


def send_internal_email(recipient_email: str, subject: str, body: str, current_user: str, sender_callable):
    recipient_username = normalize_internal_recipient(recipient_email)
    response = sender_callable(
        sender=current_user,
        receiver=recipient_username,
        subject=subject,
        body=body,
    )
    return {
        "channel": "internal",
        "toast": "Sent via Internal System",
        **response,
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
            WHERE receiver = %s
              AND hidden_for_receiver = FALSE
              AND COALESCE(deleted_for_receiver, FALSE) = FALSE
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
            WHERE sender = %s
              AND hidden_for_sender = FALSE
              AND COALESCE(deleted_for_sender, FALSE) = FALSE
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
            WHERE (receiver = %s AND hidden_for_receiver = TRUE AND COALESCE(deleted_for_receiver, FALSE) = FALSE)
               OR (sender = %s AND hidden_for_sender = TRUE AND COALESCE(deleted_for_sender, FALSE) = FALSE)
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
                    (receiver = %s AND hidden_for_receiver = FALSE AND COALESCE(deleted_for_receiver, FALSE) = FALSE)
                 OR (sender = %s AND hidden_for_sender = FALSE AND COALESCE(deleted_for_sender, FALSE) = FALSE)
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
                    (receiver = %s AND hidden_for_receiver = FALSE AND COALESCE(deleted_for_receiver, FALSE) = FALSE)
                 OR (sender = %s AND hidden_for_sender = FALSE AND COALESCE(deleted_for_sender, FALSE) = FALSE)
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

    return [
        {
            "id": row[0],
            "sender": row[1],
            "receiver": row[2],
            "subject": row[3] or "",
            "content": row[4] or "",
            "status": row[5],
            "timestamp_sent": row[6],
            "timestamp_read": row[7],
            "checksum": row[8],
            "server_id": row[9],
            "is_starred": bool(row[10]),
            "is_spam": bool(row[11]),
            "hidden_for_sender": bool(row[12]),
            "hidden_for_receiver": bool(row[13]),
        }
        for row in rows
    ]


def move_internal_to_trash(connection, username: str, message_id: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET hidden_for_sender = CASE WHEN sender = %s THEN TRUE ELSE hidden_for_sender END,
                hidden_for_receiver = CASE WHEN receiver = %s THEN TRUE ELSE hidden_for_receiver END
            WHERE id = %s
              AND (sender = %s OR receiver = %s)
            """,
            (username, username, message_id, username, username),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0


def empty_internal_trash(connection, username: str) -> int:
    changed_count = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET deleted_for_sender = TRUE
            WHERE sender = %s
              AND hidden_for_sender = TRUE
              AND COALESCE(deleted_for_sender, FALSE) = FALSE
            """,
            (username,),
        )
        changed_count += cursor.rowcount if cursor.rowcount is not None else 0

        cursor.execute(
            """
            UPDATE messages
            SET deleted_for_receiver = TRUE
            WHERE receiver = %s
              AND hidden_for_receiver = TRUE
              AND COALESCE(deleted_for_receiver, FALSE) = FALSE
            """,
            (username,),
        )
        changed_count += cursor.rowcount if cursor.rowcount is not None else 0

        cursor.execute(
            """
            DELETE FROM messages
            WHERE hidden_for_sender = TRUE
              AND hidden_for_receiver = TRUE
              AND COALESCE(deleted_for_sender, FALSE) = TRUE
              AND COALESCE(deleted_for_receiver, FALSE) = TRUE
            """
        )
        changed_count += cursor.rowcount if cursor.rowcount is not None else 0

    return changed_count


def toggle_internal_star(connection, username: str, message_id: str, is_starred: bool) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messages
            SET is_starred = %s
            WHERE id = %s
              AND (sender = %s OR receiver = %s)
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
              AND (sender = %s OR receiver = %s)
            """,
            (is_spam, message_id, username, username),
        )
        return cursor.rowcount if cursor.rowcount is not None else 0
