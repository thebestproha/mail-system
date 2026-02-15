from flask import Flask, jsonify, request
import hashlib
import os


app = Flask(__name__)

SERVER_ID = "S1"
SERVER_PORT = os.getenv("PORT", "")


class DatabaseConnectionError(Exception):
    pass


def get_db_connection():
    import os
    import psycopg2

    database_url = (os.environ.get("DATABASE_URL") or "").strip()

    if not database_url:
        raise DatabaseConnectionError("DATABASE_URL not set")

    if "sslmode" not in database_url:
        if "?" in database_url:
            database_url += "&sslmode=require"
        else:
            database_url += "?sslmode=require"

    try:
        return psycopg2.connect(database_url, connect_timeout=5)
    except Exception as error:
        raise DatabaseConnectionError(str(error)) from error


@app.errorhandler(DatabaseConnectionError)
def handle_db_connection_error(error):
    return jsonify({"error": "Database unavailable", "details": str(error)}), 503


@app.get("/")
def home():
    return jsonify(
        {
            "message": "Server 1 is running",
            "server_id": SERVER_ID,
            "port": SERVER_PORT,
        }
    )


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.post("/receive")
def receive_message():
    payload = request.get_json(silent=True) or {}

    message_id = payload.get("id")
    sender = payload.get("sender")
    receiver = payload.get("receiver")
    content = payload.get("content", "")

    checksum = hashlib.md5(content.encode()).hexdigest()

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO messages
                    (id, sender, receiver, content, status, checksum, server_id)
                    VALUES (%s, %s, %s, %s, 'UNREAD', %s, %s)
                    """,
                    (message_id, sender, receiver, content, checksum, SERVER_ID),
                )
            connection.commit()
    except Exception as error:
        error_text = str(error).lower()
        if "duplicate key" in error_text or "already exists" in error_text:
            return jsonify({"error": "Message id already exists"}), 400
        return jsonify({"error": "Database unavailable", "details": str(error)}), 503

    return jsonify(
        {
            "message": "Stored successfully",
            "server": SERVER_ID,
            "id": message_id,
        }
    )


@app.get("/messages/<username>")
def get_messages(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE receiver = %s AND server_id = %s
                ORDER BY timestamp_sent DESC
                """,
                (username, SERVER_ID),
            )
            rows = cursor.fetchall()

        for row in rows:
            recalculated_checksum = hashlib.md5((row[3] or "").encode()).hexdigest()
            if row[7] != recalculated_checksum:
                return jsonify({"error": "Message corrupted", "message_id": row[0]}), 400

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET status='READ', timestamp_read=CURRENT_TIMESTAMP
                WHERE receiver = %s AND server_id = %s AND status='UNREAD'
                """,
                (username, SERVER_ID),
            )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE receiver = %s AND server_id = %s
                ORDER BY timestamp_sent DESC
                """,
                (username, SERVER_ID),
            )
            updated_rows = cursor.fetchall()

    user_messages = [
        {
            "id": row[0],
            "sender": row[1],
            "receiver": row[2],
            "content": row[3],
            "status": row[4],
            "timestamp_sent": row[5],
            "timestamp_read": row[6],
            "checksum": row[7],
            "server_id": row[8],
        }
        for row in updated_rows
    ]

    return jsonify(user_messages)


@app.put("/edit/<message_id>")
def edit_message(message_id):
    payload = request.get_json(silent=True) or {}
    new_content = payload.get("content", "")

    checksum = hashlib.md5(new_content.encode()).hexdigest()

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET content = %s, checksum = %s
                WHERE id = %s AND status = 'UNREAD' AND server_id = %s
                """,
                (new_content, checksum, message_id, SERVER_ID),
            )
            updated_count = cursor.rowcount
        connection.commit()

        if updated_count == 0:
            return jsonify({"error": "Message already read and locked"}), 400

    return jsonify({"message": "Updated successfully", "id": message_id})


@app.delete("/delete/<message_id>")
def delete_message(message_id):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM messages WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            existing_row = cursor.fetchone()

        if existing_row is None:
            return jsonify({"error": "Message not found"}), 404

        if existing_row[0] == "READ":
            return jsonify({"error": "Message already read and locked"}), 400

        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM messages WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
        connection.commit()

    return jsonify({"message": "Deleted successfully", "id": message_id})


@app.post("/corrupt/<message_id>")
def corrupt_message(message_id):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE messages SET content='corrupted data' WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            updated_count = cursor.rowcount
        connection.commit()

        if updated_count == 0:
            return jsonify({"error": "Message not found"}), 404

    return jsonify({"message": "Message corrupted for testing", "id": message_id})


@app.get("/sent/<username>")
def get_sent_messages(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE sender = %s AND server_id = %s
                ORDER BY timestamp_sent DESC
                """,
                (username, SERVER_ID),
            )
            rows = cursor.fetchall()

    sent_messages = [
        {
            "id": row[0],
            "sender": row[1],
            "receiver": row[2],
            "content": row[3],
            "status": row[4],
            "timestamp_sent": row[5],
            "timestamp_read": row[6],
            "checksum": row[7],
            "server_id": row[8],
        }
        for row in rows
    ]

    return jsonify(sent_messages)


@app.delete("/sent-history/<username>")
def clear_sent_history(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM messages WHERE sender = %s AND server_id = %s",
                (username, SERVER_ID),
            )
            deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()

    return jsonify({"message": "Sent history cleared", "deleted": deleted_count})


@app.delete("/inbox-history/<username>")
def clear_inbox_history(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM messages WHERE receiver = %s AND server_id = %s",
                (username, SERVER_ID),
            )
            deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()

    return jsonify({"message": "Inbox history cleared", "deleted": deleted_count})


@app.get("/stats")
def get_stats():
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE server_id = %s",
                (SERVER_ID,),
            )
            row = cursor.fetchone()

    return jsonify({"server_id": SERVER_ID, "message_count": int(row[0] if row else 0)})

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
