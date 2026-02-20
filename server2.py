from flask import Flask, jsonify, request
import hashlib
import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool


app = Flask(__name__)

SERVER_ID = "S2"
SERVER_PORT = 5002

DATABASE_URL = os.environ.get("DATABASE_URL")
db_pool = None
db_schema_initialized = False


class DatabaseConnectionError(Exception):
    pass


def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=os.environ.get("DATABASE_URL"),
        )
    return db_pool


def get_db_connection():
    try:
        init_db()
        pool = get_db_pool()
        return pool.getconn()
    except Exception as error:
        raise DatabaseConnectionError(str(error))


def init_db() -> None:
    global db_schema_initialized
    if db_schema_initialized:
        return

    pool = get_db_pool()
    connection = pool.getconn()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
            id BIGINT PRIMARY KEY,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'UNREAD',
            timestamp_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            timestamp_read TIMESTAMP,
            checksum TEXT NOT NULL,
            server_id TEXT NOT NULL,
            hidden_for_sender BOOLEAN DEFAULT FALSE,
            hidden_for_receiver BOOLEAN DEFAULT FALSE
            )
            """
        )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS hidden_for_sender BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS hidden_for_receiver BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS subject TEXT DEFAULT ''"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_starred BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_spam BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_sender BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_receiver BOOLEAN DEFAULT FALSE"
            )

        connection.commit()
        db_schema_initialized = True
    finally:
        pool.putconn(connection)


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
    subject = payload.get("subject", "")
    content = payload.get("content", "")

    checksum = hashlib.md5(content.encode()).hexdigest()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO messages
                (id, sender, receiver, subject, content, status, checksum, server_id)
                VALUES (%s, %s, %s, %s, %s, 'UNREAD', %s, %s)
                """,
                (message_id, sender, receiver, subject, content, checksum, SERVER_ID),
            )
        connection.commit()
    except psycopg2.IntegrityError:
        connection.rollback()
        return jsonify({"error": "Message id already exists"}), 400
    finally:
        db_pool.putconn(connection)

    return jsonify(
        {
            "message": "Stored successfully",
            "server": SERVER_ID,
            "id": message_id,
        }
    )


@app.get("/messages/<username>")
def get_messages(username):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE receiver = %s AND hidden_for_receiver = FALSE
                ORDER BY timestamp_sent DESC
                """,
                (username,),
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
                WHERE receiver = %s AND status='UNREAD' AND hidden_for_receiver = FALSE
                """,
                (username,),
            )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE receiver = %s AND hidden_for_receiver = FALSE
                ORDER BY timestamp_sent DESC
                """,
                (username,),
            )
            updated_rows = cursor.fetchall()
    finally:
        db_pool.putconn(connection)

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

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content, status FROM messages WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            existing_row = cursor.fetchone()

        if existing_row is None:
            return jsonify({"error": "Message not found"}), 404

        if existing_row[1] == "READ":
            return jsonify({"error": "Message already read and locked"}), 400

        if (existing_row[0] or "") == new_content:
            return jsonify({"message": "No changes to update", "id": message_id})

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET content = %s, checksum = %s
                WHERE id = %s AND status = 'UNREAD' AND server_id = %s
                """,
                (new_content, checksum, message_id, SERVER_ID),
            )
        connection.commit()
    finally:
        db_pool.putconn(connection)

    return jsonify({"message": "Updated successfully", "id": message_id})


@app.delete("/delete/<message_id>")
def delete_message(message_id):
    connection = get_db_connection()
    try:
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
    finally:
        db_pool.putconn(connection)

    return jsonify({"message": "Deleted successfully", "id": message_id})


@app.post("/corrupt/<message_id>")
def corrupt_message(message_id):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE messages SET content='corrupted data' WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            updated_count = cursor.rowcount
        connection.commit()

        if updated_count == 0:
            return jsonify({"error": "Message not found"}), 404
    finally:
        db_pool.putconn(connection)

    return jsonify({"message": "Message corrupted for testing", "id": message_id})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5002, debug=False)