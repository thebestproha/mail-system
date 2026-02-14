from flask import Flask, jsonify, request
from pathlib import Path
import hashlib
import sqlite3


app = Flask(__name__)

SERVER_ID = "S3"
SERVER_PORT = 5003
DB_PATH = Path("mail_system.db")


def get_db_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT CHECK(status IN ('UNREAD','READ')) DEFAULT 'UNREAD',
            timestamp_sent DATETIME DEFAULT CURRENT_TIMESTAMP,
            timestamp_read DATETIME,
            checksum TEXT NOT NULL,
            server_id TEXT NOT NULL
            )
            """
        )
        connection.commit()


init_db()


@app.get("/")
def home():
    return jsonify(
        {
            "message": "Server 3 is running",
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
            connection.execute(
                """
                INSERT INTO messages
                (id, sender, receiver, content, status, checksum, server_id)
                VALUES (?, ?, ?, ?, 'UNREAD', ?, ?)
                """,
                (message_id, sender, receiver, content, checksum, SERVER_ID),
            )
            connection.commit()
    except sqlite3.IntegrityError:
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
        rows = connection.execute(
            """
            SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
            FROM messages
            WHERE receiver = ? AND server_id = ?
            ORDER BY timestamp_sent DESC
            """,
            (username, SERVER_ID),
        ).fetchall()

        for row in rows:
            recalculated_checksum = hashlib.md5((row[3] or "").encode()).hexdigest()
            if row[7] != recalculated_checksum:
                return jsonify({"error": "Message corrupted", "message_id": row[0]}), 400

        connection.execute(
            """
            UPDATE messages
            SET status='READ', timestamp_read=CURRENT_TIMESTAMP
            WHERE receiver = ? AND server_id = ? AND status='UNREAD'
            """,
            (username, SERVER_ID),
        )
        connection.commit()

        updated_rows = connection.execute(
            """
            SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
            FROM messages
            WHERE receiver = ? AND server_id = ?
            ORDER BY timestamp_sent DESC
            """,
            (username, SERVER_ID),
        ).fetchall()

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
        cursor = connection.execute(
            """
            UPDATE messages
            SET content = ?, checksum = ?
            WHERE id = ? AND status = 'UNREAD' AND server_id = ?
            """,
            (new_content, checksum, message_id, SERVER_ID),
        )
        connection.commit()

        if cursor.rowcount == 0:
            return jsonify({"error": "Message already read and locked"}), 400

    return jsonify({"message": "Updated successfully", "id": message_id})


@app.delete("/delete/<message_id>")
def delete_message(message_id):
    with get_db_connection() as connection:
        existing_row = connection.execute(
            "SELECT status FROM messages WHERE id = ? AND server_id = ?",
            (message_id, SERVER_ID),
        ).fetchone()

        if existing_row is None:
            return jsonify({"error": "Message not found"}), 404

        if existing_row[0] == "READ":
            return jsonify({"error": "Message already read and locked"}), 400

        connection.execute(
            "DELETE FROM messages WHERE id = ? AND server_id = ?",
            (message_id, SERVER_ID),
        )
        connection.commit()

    return jsonify({"message": "Deleted successfully", "id": message_id})


@app.post("/corrupt/<message_id>")
def corrupt_message(message_id):
    with get_db_connection() as connection:
        cursor = connection.execute(
            "UPDATE messages SET content='corrupted data' WHERE id = ? AND server_id = ?",
            (message_id, SERVER_ID),
        )
        connection.commit()

        if cursor.rowcount == 0:
            return jsonify({"error": "Message not found"}), 404

    return jsonify({"message": "Message corrupted for testing", "id": message_id})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5003, debug=False)