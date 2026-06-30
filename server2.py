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
            connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "6")),
            keepalives=1,
            keepalives_idle=int(os.environ.get("DB_KEEPALIVE_IDLE_SECONDS", "30")),
            keepalives_interval=int(os.environ.get("DB_KEEPALIVE_INTERVAL_SECONDS", "10")),
            keepalives_count=int(os.environ.get("DB_KEEPALIVE_COUNT", "5")),
            application_name="editmail-server2",
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
            DO $$
            BEGIN
                CREATE TYPE message_delete_state AS ENUM ('ACTIVE', 'TRASHED', 'DELETED');
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )
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
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_sender message_delete_state DEFAULT 'ACTIVE'"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_receiver message_delete_state DEFAULT 'ACTIVE'"
            )
            cursor.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'messages'
                      AND column_name = 'deleted_for_sender'
                      AND data_type = 'boolean'
                ) THEN
                    ALTER TABLE messages ALTER COLUMN deleted_for_sender DROP DEFAULT;
                    ALTER TABLE messages
                    ALTER COLUMN deleted_for_sender
                    TYPE message_delete_state
                    USING (
                        CASE
                            WHEN COALESCE(deleted_for_sender, FALSE) = TRUE THEN 'DELETED'::message_delete_state
                            WHEN COALESCE(hidden_for_sender, FALSE) = TRUE THEN 'TRASHED'::message_delete_state
                            ELSE 'ACTIVE'::message_delete_state
                        END
                    );
                END IF;
            END
            $$;
            """
        )
            cursor.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'messages'
                      AND column_name = 'deleted_for_receiver'
                      AND data_type = 'boolean'
                ) THEN
                    ALTER TABLE messages ALTER COLUMN deleted_for_receiver DROP DEFAULT;
                    ALTER TABLE messages
                    ALTER COLUMN deleted_for_receiver
                    TYPE message_delete_state
                    USING (
                        CASE
                            WHEN COALESCE(deleted_for_receiver, FALSE) = TRUE THEN 'DELETED'::message_delete_state
                            WHEN COALESCE(hidden_for_receiver, FALSE) = TRUE THEN 'TRASHED'::message_delete_state
                            ELSE 'ACTIVE'::message_delete_state
                        END
                    );
                END IF;
            END
            $$;
            """
        )
            cursor.execute(
                "UPDATE messages SET deleted_for_sender = 'TRASHED' WHERE hidden_for_sender = TRUE AND COALESCE(deleted_for_sender::text, 'ACTIVE') = 'ACTIVE'"
            )
            cursor.execute(
                "UPDATE messages SET deleted_for_receiver = 'TRASHED' WHERE hidden_for_receiver = TRUE AND COALESCE(deleted_for_receiver::text, 'ACTIVE') = 'ACTIVE'"
            )
            cursor.execute(
                "ALTER TABLE messages ALTER COLUMN deleted_for_sender SET DEFAULT 'ACTIVE'"
            )
            cursor.execute(
                "ALTER TABLE messages ALTER COLUMN deleted_for_receiver SET DEFAULT 'ACTIVE'"
            )
            cursor.execute(
                "ALTER TABLE messages ALTER COLUMN deleted_for_sender SET NOT NULL"
            )
            cursor.execute(
                "ALTER TABLE messages ALTER COLUMN deleted_for_receiver SET NOT NULL"
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
    content_provided = "content" in payload
    subject_provided = "subject" in payload

    requested_content = payload.get("content") if content_provided else None
    requested_subject = payload.get("subject") if subject_provided else None

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT subject, content, status, timestamp_read FROM messages WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            existing_row = cursor.fetchone()

        if existing_row is None:
            return jsonify({"error": "Message not found"}), 404

        current_subject, current_content, current_status, timestamp_read = existing_row

        if current_status == "READ" or timestamp_read is not None:
            return jsonify({"error": "Message already read and locked"}), 400

        next_subject = (current_subject or "") if not subject_provided else ("" if requested_subject is None else str(requested_subject))
        next_content = (current_content or "") if not content_provided else ("" if requested_content is None else str(requested_content))

        if (current_subject or "") == next_subject and (current_content or "") == next_content:
            return jsonify({"message": "No changes to update", "id": message_id})

        checksum = hashlib.md5(next_content.encode()).hexdigest()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET subject = %s, content = %s, checksum = %s
                WHERE id = %s AND status = 'UNREAD' AND server_id = %s
                """,
                (next_subject, next_content, checksum, message_id, SERVER_ID),
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
                "SELECT status, timestamp_read FROM messages WHERE id = %s AND server_id = %s",
                (message_id, SERVER_ID),
            )
            existing_row = cursor.fetchone()

        if existing_row is None:
            return jsonify({"error": "Message not found"}), 404

        if existing_row[0] == "READ" or existing_row[1] is not None:
            return jsonify({"error": "Message already read and locked"}), 400

        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM messages WHERE id = %s AND server_id = %s AND status = 'UNREAD'",
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