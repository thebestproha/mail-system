from services.internal_mail import can_delete_internal_message, can_edit_internal_message
from flask import Flask, jsonify, request, render_template, redirect, url_for, session, Response
import requests
import os
import time
import psycopg2
import base64
import ssl
import mimetypes
from psycopg2.pool import SimpleConnectionPool
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from email.mime.text import MIMEText
from werkzeug.security import check_password_hash, generate_password_hash
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from services.external_mail import (
    external_delete_message,
    fetch_external_attachment_bytes,
    external_mark_spam,
    external_move_to_trash,
    external_mark_read,
    external_toggle_star,
    list_external_messages,
    send_gmail_message,
)
from services.internal_mail import (
    delete_internal_message_for_user,
    empty_internal_trash,
    fetch_internal_attachment,
    list_internal_messages,
    mark_internal_spam,
    move_internal_to_trash,
    normalize_internal_recipient,
    toggle_internal_star,
)
from services.router import configure_route_handlers, route_message
import hashlib


app = Flask(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 10000))

S1_URL = os.environ.get("S1_URL")
S2_URL = os.environ.get("S2_URL")
S3_URL = os.environ.get("S3_URL")
if not all([S1_URL, S2_URL, S3_URL]):
    raise RuntimeError(
        "S1_URL, S2_URL, and S3_URL environment variables are required"
    )

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "5"))
REPLICA_TIMEOUT_SECONDS = float(os.environ.get("REPLICA_TIMEOUT_SECONDS", "1"))

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or hashlib.sha256(
    DATABASE_URL.encode("utf-8")
).hexdigest()

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

if GOOGLE_REDIRECT_URI.startswith("http://") and (
    "127.0.0.1" in GOOGLE_REDIRECT_URI or "localhost" in GOOGLE_REDIRECT_URI
):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

GOOGLE_SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
GOOGLE_OAUTH_CONFIGURED = all(
    [GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI]
)

ADMIN_DASHBOARD_PASSWORD = os.environ.get("ADMIN_DASHBOARD_PASSWORD", "admin")

server_status = {
    "S1": "UP",
    "S2": "UP",
    "S3": "UP",
}

available_servers = ["S1", "S2", "S3"]
current_index = 0
last_routed = None

server_urls = {
    "S1": S1_URL,
    "S2": S2_URL,
    "S3": S3_URL,
}

db_pool = None
db_schema_initialized = False
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
MAX_TOTAL_ATTACHMENTS_BYTES = int(os.environ.get("MAX_TOTAL_ATTACHMENTS_BYTES", str(25 * 1024 * 1024)))


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
            application_name="editmail-load-balancer",
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
            CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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
            """
            CREATE TABLE IF NOT EXISTS event_logs (
            id SERIAL PRIMARY KEY,
            event TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_server ON messages(server_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)"
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
            cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_tokens (
            username TEXT PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            scopes TEXT,
            expiry TIMESTAMP
            )
            """
        )
            cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS message_attachments (
            id BIGSERIAL PRIMARY KEY,
            message_id BIGINT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            content_bytes BYTEA NOT NULL,
            size INTEGER NOT NULL,
            is_inline BOOLEAN DEFAULT FALSE,
            content_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id ON message_attachments(message_id)"
            )
        connection.commit()
        db_schema_initialized = True
    finally:
        pool.putconn(connection)


def add_log(message: str) -> None:
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO event_logs (event) VALUES (%s)", (message,))
        connection.commit()
    finally:
        db_pool.putconn(connection)


def get_current_username() -> str:
    session_username = session.get("username", "")
    query_username = (request.args.get("username") or "").strip()
    payload = request.get_json(silent=True) if request.method in {"POST", "PUT"} else None
    payload_username = ""
    if isinstance(payload, dict):
        payload_username = (payload.get("username") or "").strip()
    return session_username or query_username or payload_username


def _build_google_flow(state: str | None = None) -> Flow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES, state=state)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    flow.oauth_authorization_kwargs = {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return flow


def _get_gmail_token_row(username: str):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT access_token, refresh_token, scopes, expiry
                FROM gmail_tokens
                WHERE username = %s
                """,
                (username,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "access_token": row[0],
                "refresh_token": row[1],
                "scopes": row[2],
                "expiry": row[3],
            }
    finally:
        db_pool.putconn(connection)


def _save_gmail_credentials(username: str, credentials: Credentials) -> None:
    expiry = credentials.expiry
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)

    existing_row = _get_gmail_token_row(username)
    refresh_token_value = credentials.refresh_token
    if not refresh_token_value and existing_row is not None:
        refresh_token_value = existing_row.get("refresh_token")

    scopes = credentials.scopes
    if not scopes and existing_row is not None:
        scopes = [scope for scope in str(existing_row.get("scopes") or "").split(" ") if scope]
    scopes_value = " ".join(scopes or GOOGLE_SCOPES)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gmail_tokens (username, access_token, refresh_token, scopes, expiry)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    scopes = EXCLUDED.scopes,
                    expiry = EXCLUDED.expiry
                """,
                (
                    username,
                    credentials.token,
                    refresh_token_value,
                    scopes_value,
                    expiry,
                ),
            )
        connection.commit()
    finally:
        db_pool.putconn(connection)


def _delete_gmail_tokens(username: str) -> int:
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM gmail_tokens WHERE username = %s", (username,))
            deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
        return deleted_count
    finally:
        db_pool.putconn(connection)


def _get_header(headers: list[dict], header_name: str) -> str:
    for header in headers:
        if str(header.get("name", "")).lower() == header_name.lower():
            return header.get("value", "")
    return ""


def _is_gmail_reauth_error(error: Exception) -> bool:
    text = str(error).lower()
    keywords = (
        "invalid_grant",
        "invalid credentials",
        "token has been expired or revoked",
        "reauth",
    )
    return any(keyword in text for keyword in keywords)


def get_gmail_service(username: str):
    token_row = _get_gmail_token_row(username)
    if token_row is None:
        raise ValueError("Gmail account is not connected")

    scope_values = [scope for scope in str(token_row["scopes"] or "").split(" ") if scope]
    credentials = Credentials(
        token=token_row["access_token"],
        refresh_token=token_row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=scope_values or GOOGLE_SCOPES,
    )

    if token_row.get("expiry") is not None:
        credentials.expiry = token_row["expiry"]

    try:
        if (credentials.expired or not credentials.valid) and credentials.refresh_token:
            credentials.refresh(GoogleAuthRequest())
            _save_gmail_credentials(username, credentials)

        if credentials.expired and not credentials.refresh_token:
            _delete_gmail_tokens(username)
            raise ValueError("Gmail session expired. Reconnect Gmail and try again")

        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return service
    except RefreshError as error:
        if _is_gmail_reauth_error(error):
            _delete_gmail_tokens(username)
            raise ValueError("Gmail authorization expired. Please reconnect Gmail") from error
        raise
    except Exception as error:
        if _is_gmail_reauth_error(error):
            _delete_gmail_tokens(username)
            raise ValueError("Gmail authorization expired. Please reconnect Gmail") from error
        raise


def get_next_server():
    global current_index

    if not available_servers:
        raise ValueError("No available servers")

    total_servers = len(available_servers)
    checked = 0

    while checked < total_servers:
        index = current_index % total_servers
        server_id = available_servers[index]
        current_index = (index + 1) % total_servers

        if server_status.get(server_id) == "UP":
            return server_id

        checked += 1

    raise ValueError("No UP servers found")


def _ensure_internal_receiver_exists(receiver_username: str) -> None:
    connection = get_db_connection()
    pool = get_db_pool()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s)", (receiver_username,))
            matched_receiver = cursor.fetchone()
        if matched_receiver is None:
            raise ValueError("Receiver does not exist")
    finally:
        pool.putconn(connection)


def _is_password_hash(value: str) -> bool:
    text = str(value or "")
    return text.startswith("scrypt:") or text.startswith("pbkdf2:")


def _password_matches(stored_password: str, provided_password: str) -> bool:
    if _is_password_hash(stored_password):
        try:
            return bool(check_password_hash(stored_password, provided_password))
        except ValueError:
            return False
    return stored_password == provided_password


def send_internal_distributed(sender: str, receiver: str, subject: str, body: str, attachments=None):
    global last_routed

    receiver_username = normalize_internal_recipient(receiver)
    _ensure_internal_receiver_exists(receiver_username)

    max_bigint = (1 << 63) - 1
    message_id = int.from_bytes(os.urandom(8), "big") % max_bigint + 1
    payload = {
        "id": message_id,
        "sender": sender,
        "receiver": receiver_username,
        "subject": subject,
        "content": body,
    }

    attempted_servers = set()
    last_error_text = "No UP servers found"

    for _ in range(len(server_urls)):
        try:
            server_id = get_next_server()
        except ValueError as error:
            raise RuntimeError(str(error)) from error

        if server_id in attempted_servers:
            continue
        attempted_servers.add(server_id)

        target_url = f"{server_urls[server_id]}/receive"
        try:
            response = requests.post(
                target_url,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code >= 500:
                last_error_text = f"{server_id} temporary failure ({response.status_code})"
                continue

            response.raise_for_status()

            last_routed = server_id
            add_log(f"Message {message_id} routed to {server_id}")
            return {
                "id": message_id,
                "routed_to": server_id,
                "server_response": response.json(),
            }
        except requests.RequestException as error:
            last_error_text = str(error)
            continue

    raise RuntimeError(f"Unable to route internal message: {last_error_text}")


def send_external_via_gmail(sender: str, recipient_email: str, subject: str, body: str, attachments=None):
    service = get_gmail_service(sender)
    sent = send_gmail_message(service, recipient_email, subject, body, attachments=attachments or [])
    return {"id": sent.get("id")}


def _parse_send_attachments_from_request() -> list[dict]:
    if not request.files:
        return []

    uploaded_files = request.files.getlist("attachments")
    parsed_items = []
    total_size = 0

    for uploaded in uploaded_files:
        if uploaded is None:
            continue
        filename = (uploaded.filename or "").strip()
        if not filename:
            continue

        payload = uploaded.read() or b""
        size = len(payload)
        if size == 0:
            continue
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment too large: {filename}")

        total_size += size
        if total_size > MAX_TOTAL_ATTACHMENTS_BYTES:
            raise ValueError("Total attachment size exceeded")

        mime_type = (uploaded.mimetype or "").strip() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parsed_items.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "data": payload,
                "size": size,
                "is_inline": False,
                "content_id": "",
            }
        )

    return parsed_items


def _store_internal_attachments(connection, message_id: int, attachments: list[dict]) -> None:
    if not attachments:
        return

    with connection.cursor() as cursor:
        for item in attachments:
            cursor.execute(
                """
                INSERT INTO message_attachments (message_id, filename, mime_type, content_bytes, size, is_inline, content_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    message_id,
                    item.get("filename") or "attachment",
                    item.get("mime_type") or "application/octet-stream",
                    psycopg2.Binary(item.get("data") or b""),
                    int(item.get("size") or 0),
                    bool(item.get("is_inline") or False),
                    item.get("content_id") or "",
                ),
            )


configure_route_handlers(send_internal_distributed, send_external_via_gmail)


@app.get("/")
def home():
    return redirect(url_for("login_page"))


@app.get("/health")
def health():
    return jsonify(
        {
            "message": "Load Balancer is running",
            "host": HOST,
            "port": PORT,
            "gmail_oauth_configured": GOOGLE_OAUTH_CONFIGURED,
        }
    )


@app.get("/login")
def login_page():
    session.pop("username", None)
    session.pop("oauth_state", None)
    session.pop("oauth_code_verifier", None)
    session.pop("oauth_pkce_state", None)
    return render_template("login.html")


@app.get("/register")
def register_page():
    return render_template("register.html")


@app.get("/user-home")
def user_home_page():
    username = request.args.get("username", "")
    return render_template(
        "user_home.html",
        username=username,
        attachment_view_limit=MAX_ATTACHMENT_BYTES,
    )


@app.get("/dashboard")
def dashboard():
    if not session.get("admin_dashboard_access"):
        return redirect(url_for("dashboard_access"))
    return render_template("dashboard.html")


@app.get("/dashboard-access")
def dashboard_access():
    return render_template("dashboard_access.html")


@app.post("/dashboard-auth")
def dashboard_auth():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    password = (payload.get("password") or "").strip()

    if password != ADMIN_DASHBOARD_PASSWORD:
        if request.is_json:
            return jsonify({"error": "Invalid admin password"}), 401
        return redirect(url_for("dashboard_access"))

    session["admin_dashboard_access"] = True

    if request.is_json:
        return jsonify({"message": "Admin dashboard unlocked"})

    return redirect(url_for("dashboard"))


@app.post("/dashboard-logout")
def dashboard_logout():
    session.pop("admin_dashboard_access", None)
    if request.is_json:
        return jsonify({"message": "Admin dashboard locked"})
    return redirect(url_for("login_page"))


@app.get("/servers")
def get_servers():
    return jsonify(server_status)


@app.get("/dashboard-data")
def dashboard_data():
    if not session.get("admin_dashboard_access"):
        return jsonify({"error": "Admin password required"}), 403

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM messages WHERE server_id='S1'")
            s1_row = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE server_id='S2'")
            s2_row = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE server_id='S3'")
            s3_row = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM messages")
            total_row = cursor.fetchone()
            cursor.execute("SELECT event FROM event_logs ORDER BY id DESC LIMIT 20")
            log_rows = cursor.fetchall()
    finally:
        db_pool.putconn(connection)

    server_load = {
        "S1": int(s1_row[0] if s1_row else 0),
        "S2": int(s2_row[0] if s2_row else 0),
        "S3": int(s3_row[0] if s3_row else 0),
    }

    total_messages = int(total_row[0] if total_row else 0)
    logs = [row[0] for row in reversed(log_rows)]

    return jsonify(
        {
            "server_status": server_status,
            "available_servers": available_servers,
            "current_index": current_index,
            "server_load": server_load,
            "total_messages": total_messages,
            "algorithm": "Round Robin",
            "logs": logs,
            "last_routed": last_routed,
        }
    )


@app.post("/fail/<server_id>")
def fail_server(server_id):
    if server_id not in server_status:
        return jsonify({"error": "Invalid server_id"}), 400

    server_status[server_id] = "DOWN"
    if server_id in available_servers:
        available_servers.remove(server_id)
    add_log(f"Server {server_id} marked DOWN")

    return jsonify(server_status)


@app.post("/restore/<server_id>")
def restore_server(server_id):
    if server_id not in server_status:
        return jsonify({"error": "Invalid server_id"}), 400

    server_status[server_id] = "UP"
    if server_id not in available_servers:
        available_servers.append(server_id)
    add_log(f"Server {server_id} restored")

    return jsonify(server_status)


@app.post("/route")
def route_request():
    payload = request.get_json(silent=True) or {}
    sender = (payload.get("sender") or "").strip()
    receiver = (payload.get("receiver") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("content") or ""

    if not sender or not receiver or not body:
        return jsonify({"error": "sender, receiver and content are required"}), 400

    try:
        result = send_internal_distributed(
            sender=sender,
            receiver=receiver,
            subject=subject,
            body=body,
        )
        return jsonify(result)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except requests.RequestException as error:
        return jsonify({"error": str(error)}), 502

@app.post("/mail/internal/edit")
def internal_edit():
    payload = request.get_json(silent=True) or {}
    username = (session.get("username") or payload.get("username") or get_current_username() or "").strip()
    if not username:
        return jsonify({"error": "Not authenticated"}), 401
    message_id = payload.get("message_id")
    subject = payload.get("subject")
    body = payload.get("body")
    if not message_id:
        return jsonify({"error": "Missing message_id"}), 400
    if subject is None and body is None:
        return jsonify({"error": "Nothing to update"}), 400
    if body is not None and subject is not None and not str(body).strip() and not str(subject).strip():
        return jsonify({"error": "Missing fields"}), 400
    conn = get_db_connection()
    pool = get_db_pool()
    try:
        if not can_edit_internal_message(conn, message_id, username):
            return jsonify({"error": "Edit not allowed"}), 403
    finally:
        pool.putconn(conn)

    payload, status_code = _fanout_edit_message(
        str(message_id),
        None if body is None else str(body),
        None if subject is None else str(subject),
    )
    if status_code == 200:
        return jsonify({"success": True, **payload})
    return jsonify({"error": payload.get("error", "Edit failed")}), status_code

@app.post("/mail/internal/delete")
def internal_delete():
    payload = request.get_json(silent=True) or {}
    username = (session.get("username") or payload.get("username") or get_current_username() or "").strip()
    if not username:
        return jsonify({"error": "Not authenticated"}), 401
    message_id = payload.get("message_id")
    permanent = bool(payload.get("permanent"))
    hard_delete_unread = bool(payload.get("hard_delete_unread"))
    if not message_id:
        return jsonify({"error": "Missing fields"}), 400
    conn = get_db_connection()
    pool = get_db_pool()
    try:
        if hard_delete_unread:
            if not can_delete_internal_message(conn, message_id, username):
                return jsonify({"error": "Delete not allowed"}), 403
            response_payload, status_code = _fanout_delete_message(str(message_id))
            if status_code == 200:
                return jsonify({"success": True, **response_payload})
            return jsonify({"error": response_payload.get("error", "Delete failed")}), status_code

        if permanent:
            updated, purged = delete_internal_message_for_user(conn, username, str(message_id))
            if updated == 0:
                return jsonify({"error": "Delete not allowed"}), 403
            conn.commit()
            return jsonify(
                {
                    "success": True,
                    "message": "Message deleted",
                    "purged": purged,
                }
            )

        updated = move_internal_to_trash(conn, username, str(message_id))
        if updated == 0:
            return jsonify({"error": "Delete not allowed"}), 403
        conn.commit()
        return jsonify({"success": True, "message": "Message moved to trash"})
    finally:
        pool.putconn(conn)


@app.post("/register")
def register_user():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    hashed_password = generate_password_hash(password)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (%s, %s)",
                (username, hashed_password),
            )
        connection.commit()
    except psycopg2.IntegrityError:
        connection.rollback()
        return jsonify({"error": "Username already exists"}), 400
    finally:
        db_pool.putconn(connection)

    if request.is_json:
        return jsonify({"message": "registered", "username": username}), 201

    return redirect(url_for("login_page"))


@app.post("/login")
def login_user():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        if request.is_json:
            return jsonify({"error": "username and password are required"}), 400
        return redirect(url_for("login_page"))

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, password FROM users WHERE username = %s",
                (username,),
            )
            matched_row = cursor.fetchone()

        if matched_row is None:
            if request.is_json:
                return jsonify({"error": "invalid credentials"}), 401
            return redirect(url_for("login_page"))

        matched_id, stored_password = matched_row
        if not _password_matches(stored_password or "", password):
            if request.is_json:
                return jsonify({"error": "invalid credentials"}), 401
            return redirect(url_for("login_page"))

        # Upgrade legacy plaintext passwords to hashed form after successful login.
        if not _is_password_hash(stored_password or ""):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET password = %s WHERE id = %s",
                    (generate_password_hash(password), matched_id),
                )
            connection.commit()
    finally:
        db_pool.putconn(connection)

    session["username"] = username

    if request.is_json:
        return jsonify({"message": "login successful", "username": username})

    return redirect(url_for("user_home_page", username=username))


@app.get("/gmail/status")
def gmail_status():
    username = get_current_username()
    if not username:
        return jsonify({"connected": False, "error": "username is required"}), 400

    return jsonify(
        {
            "username": username,
            "connected": _get_gmail_token_row(username) is not None,
            "oauth_configured": GOOGLE_OAUTH_CONFIGURED,
        }
    )


@app.get("/gmail/login")
def gmail_login():
    if not GOOGLE_OAUTH_CONFIGURED:
        return jsonify({"error": "Google OAuth is not configured"}), 503

    username = get_current_username()
    if not username:
        return jsonify({"error": "username is required"}), 400

    session["username"] = username
    flow = _build_google_flow()
    authorization_url, state = flow.authorization_url(
        **flow.oauth_authorization_kwargs,
    )
    session["oauth_state"] = state
    code_verifier = getattr(flow, "code_verifier", None)
    if code_verifier:
        session["oauth_code_verifier"] = code_verifier
        session["oauth_pkce_state"] = state
    return redirect(authorization_url)


@app.get("/oauth2callback")
def oauth2callback():
    if not GOOGLE_OAUTH_CONFIGURED:
        return jsonify({"error": "Google OAuth is not configured"}), 503

    oauth_error = request.args.get("error")
    if oauth_error:
        return jsonify({"error": oauth_error}), 400

    expected_state = session.get("oauth_state")
    state = request.args.get("state", "")
    if expected_state and expected_state != state:
        return jsonify({"error": "OAuth state mismatch"}), 400

    username = session.get("username", "")
    if not username:
        return jsonify({"error": "No active user session for OAuth callback"}), 400

    try:
        flow = _build_google_flow(state=state)
        code_verifier = session.get("oauth_code_verifier", "")
        verifier_state = session.get("oauth_pkce_state", "")
        if code_verifier and (not verifier_state or verifier_state == state):
            flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        _save_gmail_credentials(username, credentials)
        add_log(f"Gmail connected for {username}")
    except Exception as error:
        return jsonify({"error": f"OAuth callback failed: {error}"}), 400
    finally:
        session.pop("oauth_state", None)
        session.pop("oauth_code_verifier", None)
        session.pop("oauth_pkce_state", None)

    return redirect(url_for("user_home_page", username=username))


@app.post("/gmail/logout")
def gmail_logout():
    username = get_current_username()
    if not username:
        return jsonify({"error": "username is required"}), 400

    if username.strip().lower() == "test":
        return jsonify({"error": "Can't disconnect Gmail for this user"}), 403

    deleted_count = _delete_gmail_tokens(username)
    if deleted_count:
        add_log(f"Gmail disconnected for {username}")

    return jsonify({"message": "Gmail disconnected", "username": username})


@app.post("/gmail/send")
def gmail_send():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or get_current_username() or "").strip()
    to_address = (payload.get("to") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("body") or ""

    if not username:
        return jsonify({"error": "username is required"}), 400
    if not to_address or not subject or not body:
        return jsonify({"error": "to, subject, and body are required"}), 400

    try:
        service = get_gmail_service(username)
        message = MIMEText(body)
        message["to"] = to_address
        message["subject"] = subject
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )
        return jsonify({"message": "Email sent", "id": sent.get("id")})
    except ValueError as error:
        return jsonify({"error": str(error)}), 401
    except HttpError as error:
        return jsonify({"error": f"Gmail API error: {error}"}), 502
    except Exception as error:
        return jsonify({"error": f"Failed to send email: {error}"}), 500


@app.get("/gmail/inbox")
def gmail_inbox():
    username = get_current_username()
    if not username:
        return jsonify({"error": "username is required"}), 400

    try:
        service = get_gmail_service(username)
        listing = (
            service.users()
            .messages()
            .list(userId="me", maxResults=10, labelIds=["INBOX"])
            .execute()
        )
        message_refs = listing.get("messages", [])
        inbox_messages = []

        for ref in message_refs:
            message_id = ref.get("id")
            if not message_id:
                continue

            full_message = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
            headers = full_message.get("payload", {}).get("headers", [])
            inbox_messages.append(
                {
                    "id": full_message.get("id"),
                    "sender": _get_header(headers, "From"),
                    "subject": _get_header(headers, "Subject"),
                    "snippet": full_message.get("snippet", ""),
                }
            )

        return jsonify({"messages": inbox_messages})
    except ValueError as error:
        return jsonify({"error": str(error), "messages": []}), 401
    except HttpError as error:
        return jsonify({"error": f"Gmail API error: {error}", "messages": []}), 502
    except Exception as error:
        return jsonify({"error": f"Failed to fetch inbox: {error}", "messages": []}), 500


@app.post("/mail/send")
def mail_send():
    payload = request.get_json(silent=True) or {}
    is_multipart = (request.content_type or "").lower().startswith("multipart/form-data")
    form_data = request.form if is_multipart else None

    username = (
        ((form_data.get("username") if form_data is not None else "") or payload.get("username") or get_current_username() or "")
        .strip()
    )
    recipient_email = (((form_data.get("to") if form_data is not None else "") or payload.get("to") or "")).strip()
    subject = (((form_data.get("subject") if form_data is not None else "") or payload.get("subject") or "")).strip()
    body = (form_data.get("body") if form_data is not None else payload.get("body")) or ""

    if not username:
        return jsonify({"error": "username is required"}), 400
    if not recipient_email or not subject or not body:
        return jsonify({"error": "to, subject, and body are required"}), 400

    try:
        attachments = _parse_send_attachments_from_request() if is_multipart else []
        result = route_message(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
            current_user=username,
            attachments=attachments,
        )

        if isinstance(result, dict) and result.get("error"):
            return jsonify(result), 400

        if result.get("channel") == "internal" and attachments:
            message_id_raw = result.get("id")
            if message_id_raw is None:
                return jsonify({"error": "internal send did not return message id"}), 502
            message_id = int(message_id_raw)
            connection = get_db_connection()
            pool = get_db_pool()
            try:
                _store_internal_attachments(connection, message_id, attachments)
                connection.commit()
            finally:
                pool.putconn(connection)

        if attachments:
            result = {**result, "attachments_count": len(attachments)}

        return jsonify(result)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except requests.RequestException as error:
        return jsonify({"error": str(error)}), 502
    except HttpError as error:
        return jsonify({"error": f"Gmail API error: {error}"}), 502
    except Exception as error:
        return jsonify({"error": f"send failed: {error}"}), 500


@app.get("/mail/list")
def mail_list():
    username = (request.args.get("username") or get_current_username() or "").strip()
    source = (request.args.get("source") or "internal").strip().lower()
    folder = (request.args.get("folder") or "inbox").strip().lower()
    page_token = (request.args.get("page_token") or "").strip() or None
    limit_raw = (request.args.get("limit") or "20").strip()

    try:
        limit = int(limit_raw)
    except ValueError:
        limit = 20

    limit = max(1, min(limit, 50))

    if not username:
        return jsonify({"error": "username is required", "messages": []}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            result = None
            last_error = None
            for attempt in range(3):
                try:
                    result = list_external_messages(
                        service,
                        folder,
                        max_results=limit,
                        page_token=page_token,
                    )
                    last_error = None
                    break
                except Exception as error:
                    error_text = str(error)
                    is_ssl_error = "SSL" in error_text or "DECRYPTION" in error_text or "WRONG_VERSION_NUMBER" in error_text
                    if is_ssl_error and attempt < 2:
                        time.sleep(0.7 * (attempt + 1))
                        continue
                    last_error = error
                    break

            if last_error is not None:
                raise last_error

            return jsonify(
                {
                    "source": source,
                    "folder": folder,
                    "messages": (result or {}).get("messages", []),
                    "next_page_token": (result or {}).get("next_page_token"),
                    "page_size": limit,
                }
            )
        except ValueError as error:
            return jsonify({"error": str(error), "messages": []}), 401
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}", "messages": []}), 502
        except Exception as error:
            return jsonify({"error": f"failed to load external mail: {error}", "messages": []}), 500

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        messages = list_internal_messages(connection, username, folder)
        if folder == "inbox":
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE messages
                    SET status='READ', timestamp_read=CURRENT_TIMESTAMP
                    WHERE receiver=%s
                      AND status='UNREAD'
                      AND hidden_for_receiver = FALSE
                    """,
                    (username,),
                )
            connection.commit()
            messages = list_internal_messages(connection, username, folder)
        return jsonify(
            {
                "source": source,
                "folder": folder,
                "messages": messages,
                "next_page_token": None,
                "page_size": len(messages),
            }
        )
    finally:
        pool.putconn(connection)


@app.get("/mail/external/attachment")
def mail_external_attachment():
    username = (request.args.get("username") or get_current_username() or "").strip()
    message_id = (request.args.get("message_id") or "").strip()
    attachment_id = (request.args.get("attachment_id") or "").strip()
    mime_type = (request.args.get("mime_type") or "application/octet-stream").strip()
    filename = (request.args.get("filename") or "attachment").strip() or "attachment"
    size_raw = (request.args.get("size") or "").strip()

    try:
        size = int(size_raw) if size_raw else 0
    except ValueError:
        size = 0

    if not username or not message_id or not attachment_id:
        return jsonify({"error": "username, message_id and attachment_id are required"}), 400

    if size and size > MAX_ATTACHMENT_BYTES:
        return jsonify({"error": "Attachment exceeds our view limit"}), 413

    try:
        service = get_gmail_service(username)
        content = fetch_external_attachment_bytes(service, message_id, attachment_id)
        if len(content) > MAX_ATTACHMENT_BYTES:
            return jsonify({"error": "Attachment exceeds our view limit"}), 413
        return Response(
            content,
            mimetype=mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 401
    except HttpError as error:
        return jsonify({"error": f"Gmail API error: {error}"}), 502
    except Exception as error:
        return jsonify({"error": f"Failed to fetch attachment: {error}"}), 500


@app.get("/mail/internal/attachment")
def mail_internal_attachment():
    username = (request.args.get("username") or get_current_username() or "").strip()
    attachment_id_raw = (request.args.get("attachment_id") or "").strip()

    if not username or not attachment_id_raw:
        return jsonify({"error": "username and attachment_id are required"}), 400

    try:
        attachment_id = int(attachment_id_raw)
    except ValueError:
        return jsonify({"error": "attachment_id must be an integer"}), 400

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        item = fetch_internal_attachment(connection, username, attachment_id)
        if item is None:
            return jsonify({"error": "Attachment not found"}), 404

        return Response(
            item.get("content_bytes") or b"",
            mimetype=item.get("mime_type") or "application/octet-stream",
            headers={
                "Content-Disposition": f'inline; filename="{item.get("filename") or "attachment"}"',
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )
    finally:
        pool.putconn(connection)


@app.post("/mail/mark-read")
def mail_mark_read():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "").strip().lower()
    username = (payload.get("username") or get_current_username() or "").strip()
    message_id = (payload.get("message_id") or "").strip()

    if not username or not message_id or source not in {"internal", "external"}:
        return jsonify({"error": "source, username and message_id are required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            external_mark_read(service, message_id)
            return jsonify({"message": "External message marked read"})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}"}), 502

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET status = 'READ', timestamp_read = COALESCE(timestamp_read, CURRENT_TIMESTAMP)
                WHERE id = %s
                  AND LOWER(receiver) = LOWER(%s)
                  AND status = 'UNREAD'
                """,
                (message_id, username),
            )
            updated_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
        return jsonify({"message": "Internal message marked read", "updated": updated_count})
    finally:
        pool.putconn(connection)


@app.post("/mail/trash")
def mail_trash():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or get_current_username() or "").strip()
    source = (payload.get("source") or "internal").strip().lower()
    message_id = str(payload.get("message_id") or "").strip()

    if not username or not message_id:
        return jsonify({"error": "username and message_id are required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            external_move_to_trash(service, message_id)
            return jsonify({"message": "Moved to Gmail Trash"})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except (HttpError, ssl.SSLError) as error:
            return jsonify({"error": f"Gmail temporarily unavailable: {error}"}), 502
        except Exception as error:
            return jsonify({"error": f"Failed to move to trash: {error}"}), 500

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        updated = move_internal_to_trash(connection, username, message_id)
        connection.commit()
        if updated == 0:
            return jsonify({"error": "Message not found"}), 404
        return jsonify({"message": "Moved to Internal Trash"})
    finally:
        pool.putconn(connection)


@app.post("/mail/delete")
def mail_delete():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or get_current_username() or "").strip()
    source = (payload.get("source") or "internal").strip().lower()
    message_id = str(payload.get("message_id") or "").strip()

    if not username or not message_id:
        return jsonify({"error": "username and message_id are required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            external_delete_message(service, message_id)
            return jsonify({"message": "Deleted permanently from Gmail"})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except (HttpError, ssl.SSLError) as error:
            return jsonify({"error": f"Gmail temporarily unavailable: {error}"}), 502
        except Exception as error:
            return jsonify({"error": f"Failed to delete message: {error}"}), 500

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        updated, purged = delete_internal_message_for_user(connection, username, message_id)
        if updated == 0:
            return jsonify({"error": "Message not found"}), 404
        connection.commit()
        return jsonify({"message": "Deleted from Internal Trash", "purged": purged})
    finally:
        pool.putconn(connection)


@app.delete("/mail/trash/empty")
def empty_trash():
    username = (request.args.get("username") or get_current_username() or "").strip()
    source = (request.args.get("source") or "internal").strip().lower()

    if not username:
        return jsonify({"error": "username is required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            deleted_count = 0
            page_token = None

            while True:
                trashed_result = list_external_messages(
                    service,
                    "trash",
                    max_results=100,
                    page_token=page_token,
                )
                trashed_messages = trashed_result.get("messages", [])

                for message in trashed_messages:
                    message_id = message.get("id")
                    if message_id:
                        external_delete_message(service, message_id)
                        deleted_count += 1

                page_token = trashed_result.get("next_page_token")
                if not page_token:
                    break

            return jsonify({"message": "Gmail Trash emptied", "deleted": deleted_count})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}"}), 502

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        deleted = empty_internal_trash(connection, username)
        connection.commit()
        return jsonify({"message": "Internal Trash emptied", "deleted": deleted})
    finally:
        pool.putconn(connection)


@app.post("/mail/star")
def mail_star():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or get_current_username() or "").strip()
    source = (payload.get("source") or "internal").strip().lower()
    message_id = str(payload.get("message_id") or "").strip()
    is_starred = bool(payload.get("is_starred"))

    if not username or not message_id:
        return jsonify({"error": "username and message_id are required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            external_toggle_star(service, message_id, is_starred)
            return jsonify({"message": "External star updated"})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except (HttpError, ssl.SSLError) as error:
            return jsonify({"error": f"Gmail temporarily unavailable: {error}"}), 502
        except Exception as error:
            return jsonify({"error": f"Failed to update star: {error}"}), 500

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        updated = toggle_internal_star(connection, username, message_id, is_starred)
        connection.commit()
        if updated == 0:
            return jsonify({"error": "Message not found"}), 404
        return jsonify({"message": "Internal star updated"})
    finally:
        pool.putconn(connection)


@app.post("/mail/spam")
def mail_spam():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or get_current_username() or "").strip()
    source = (payload.get("source") or "internal").strip().lower()
    message_id = str(payload.get("message_id") or "").strip()
    is_spam = bool(payload.get("is_spam"))

    if not username or not message_id:
        return jsonify({"error": "username and message_id are required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            external_mark_spam(service, message_id, is_spam)
            return jsonify({"message": "External spam updated"})
        except ValueError as error:
            return jsonify({"error": str(error)}), 401
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}"}), 502

    connection = get_db_connection()
    pool = get_db_pool()
    try:
        updated = mark_internal_spam(connection, username, message_id, is_spam)
        connection.commit()
        if updated == 0:
            return jsonify({"error": "Message not found"}), 404
        return jsonify({"message": "Internal spam updated"})
    finally:
        pool.putconn(connection)


@app.get("/inbox/<username>")
def get_inbox(username):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE receiver = %s
                AND hidden_for_receiver = FALSE
                ORDER BY timestamp_sent DESC
                """,
                (username,),
            )
            rows = cursor.fetchall()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET status='READ', timestamp_read=CURRENT_TIMESTAMP
                WHERE receiver=%s
                AND status='UNREAD'
                AND hidden_for_receiver = FALSE
                """,
                (username,),
            )
        connection.commit()
    finally:
        db_pool.putconn(connection)

    inbox_messages = [
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

    return jsonify(inbox_messages)


@app.get("/sent/<username>")
def get_sent_messages(username):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE sender = %s AND hidden_for_sender = FALSE
                ORDER BY timestamp_sent DESC
                """,
                (username,),
            )
            rows = cursor.fetchall()
    finally:
        db_pool.putconn(connection)

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
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET hidden_for_sender = TRUE
                WHERE sender = %s
                """,
                (username,),
            )
            hidden_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
    finally:
        db_pool.putconn(connection)

    add_log(f"Cleared sent history for {username} ({hidden_count} messages hidden)")
    return jsonify({"message": "Sent history cleared", "deleted": hidden_count})


@app.delete("/inbox-history/<username>")
def clear_inbox_history(username):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messages
                SET hidden_for_receiver = TRUE
                WHERE receiver = %s
                """,
                (username,),
            )
            hidden_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
    finally:
        db_pool.putconn(connection)

    add_log(f"Cleared inbox history for {username} ({hidden_count} messages hidden)")
    return jsonify({"message": "Inbox history cleared", "deleted": hidden_count})


def _fanout_edit_message(
    message_id: str,
    content: str | None = None,
    subject: str | None = None,
) -> tuple[dict, int]:
    edit_payload = {}
    if content is not None:
        edit_payload["content"] = content
    if subject is not None:
        edit_payload["subject"] = subject

    responses_by_server = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                requests.put,
                f"{server_url}/edit/{message_id}",
                json=edit_payload,
                timeout=REPLICA_TIMEOUT_SECONDS,
            ): server_id
            for server_id, server_url in server_urls.items()
        }
        for future in as_completed(futures):
            server_id = futures[future]
            try:
                responses_by_server[server_id] = future.result()
            except requests.RequestException:
                continue

    for server_id in server_urls:
        response = responses_by_server.get(server_id)
        if response is None:
            continue

        if response.status_code == 200:
            add_log(f"Message {message_id} edited on {server_id}")
            return {"server": server_id, **response.json()}, 200

        if response.status_code == 400:
            return response.json(), 400

    return {"error": "Message not found"}, 404


def _fanout_delete_message(message_id: str) -> tuple[dict, int]:
    responses_by_server = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                requests.delete,
                f"{server_url}/delete/{message_id}",
                timeout=REPLICA_TIMEOUT_SECONDS,
            ): server_id
            for server_id, server_url in server_urls.items()
        }
        for future in as_completed(futures):
            server_id = futures[future]
            try:
                responses_by_server[server_id] = future.result()
            except requests.RequestException:
                continue

    for server_id in server_urls:
        response = responses_by_server.get(server_id)
        if response is None:
            continue

        if response.status_code == 200:
            add_log(f"Message {message_id} deleted on {server_id}")
            return {"server": server_id, **response.json()}, 200

        if response.status_code == 400:
            return response.json(), 400

    return {"error": "Message not found"}, 404


@app.put("/edit-message/<message_id>")
def edit_message(message_id):
    payload = request.get_json(silent=True) or {}
    content = payload.get("content")
    subject = payload.get("subject")
    if content is None and subject is None:
        return jsonify({"error": "Nothing to update"}), 400
    response_payload, status_code = _fanout_edit_message(
        message_id,
        "" if content is None else str(content),
        None if subject is None else str(subject),
    )
    return jsonify(response_payload), status_code


@app.delete("/delete-message/<message_id>")
def delete_message(message_id):
    response_payload, status_code = _fanout_delete_message(message_id)
    return jsonify(response_payload), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
