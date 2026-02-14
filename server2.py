from flask import Flask, jsonify, request
import hashlib
import os
import psycopg2


app = Flask(__name__)

SERVER_ID = "S2"
SERVER_PORT = 5002
DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                id BIGINT PRIMARY KEY,
                sender TEXT,
                receiver TEXT,
                content TEXT,
                status TEXT,
                checksum TEXT,
                server_id TEXT,
                timestamp_sent TIMESTAMP DEFAULT NOW(),
                timestamp_read TIMESTAMP,
                CHECK (status IN ('UNREAD', 'READ'))
                )
                """
            )
        connection.commit()


init_db()


@app.get("/")
def home():
    return jsonify(
        {
            "message": "Server 2 is running",
            "server_id": SERVER_ID,
            "port": SERVER_PORT,
        }
    )


@app.get("/health")
def health():
    return jsonify({"server": SERVER_ID, "status": "UP"})


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
    except psycopg2.IntegrityError:
        return jsonify({"error": "Message id already exists"}), 400

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
    init_db()
    app.run(host="0.0.0.0", port=5002, debug=False)