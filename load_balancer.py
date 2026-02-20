from flask import Flask, jsonify, request, render_template, redirect, url_for, session
import requests
import os
import psycopg2
import base64
from psycopg2.pool import SimpleConnectionPool
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from email.mime.text import MIMEText
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from services.external_mail import (
    external_delete_message,
    external_mark_spam,
    external_move_to_trash,
    external_toggle_star,
    list_external_messages,
    send_gmail_message,
)
from services.internal_mail import (
    empty_internal_trash,
    list_internal_messages,
    mark_internal_spam,
    move_internal_to_trash,
    normalize_internal_recipient,
    toggle_internal_star,
)
from services.router import configure_route_handlers, route_message


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

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

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
GOOGLE_OAUTH_CONFIGURED = all(
    [GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI]
)

gmail_service_cache = {}

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
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_sender BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_receiver BOOLEAN DEFAULT FALSE"
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

    scopes_value = " ".join(credentials.scopes or GOOGLE_SCOPES)
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
                    credentials.refresh_token,
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


def get_gmail_service(username: str):
    cached = gmail_service_cache.get(username)
    if cached:
        cached_credentials = cached.get("credentials")
        if cached_credentials is not None:
            if cached_credentials.expired and cached_credentials.refresh_token:
                cached_credentials.refresh(GoogleAuthRequest())
                _save_gmail_credentials(username, cached_credentials)
            if not cached_credentials.expired:
                return cached.get("service")

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

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        _save_gmail_credentials(username, credentials)

    if credentials.expired and not credentials.refresh_token:
        raise ValueError("Stored Gmail token is expired and cannot be refreshed")

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    gmail_service_cache[username] = {"service": service, "credentials": credentials}
    return service


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
            cursor.execute("SELECT 1 FROM users WHERE username = %s", (receiver_username,))
            matched_receiver = cursor.fetchone()
        if matched_receiver is None:
            raise ValueError("Receiver does not exist")
    finally:
        pool.putconn(connection)


def send_internal_distributed(sender: str, receiver: str, subject: str, body: str):
    global last_routed

    receiver_username = normalize_internal_recipient(receiver)
    _ensure_internal_receiver_exists(receiver_username)

    try:
        server_id = get_next_server()
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    message_id = int.from_bytes(os.urandom(8), "big")
    payload = {
        "id": message_id,
        "sender": sender,
        "receiver": receiver_username,
        "subject": subject,
        "content": body,
    }

    target_url = f"{server_urls[server_id]}/receive"
    response = requests.post(
        target_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    last_routed = server_id
    add_log(f"Message {message_id} routed to {server_id}")

    return {
        "id": message_id,
        "routed_to": server_id,
        "server_response": response.json(),
    }


def send_external_via_gmail(sender: str, recipient_email: str, subject: str, body: str):
    service = get_gmail_service(sender)
    sent = send_gmail_message(service, recipient_email, subject, body)
    return {"id": sent.get("id")}


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
    return render_template("login.html")


@app.get("/register")
def register_page():
    return render_template("register.html")


@app.get("/user-home")
def user_home_page():
    username = request.args.get("username", "")
    return render_template("user_home.html", username=username)


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.get("/servers")
def get_servers():
    return jsonify(server_status)


@app.get("/dashboard-data")
def dashboard_data():
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


@app.post("/register")
def register_user():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (%s, %s)",
                (username, password),
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

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE username = %s AND password = %s",
                (username, password),
            )
            matched = cursor.fetchone()
    finally:
        db_pool.putconn(connection)

    if matched is None:
        if request.is_json:
            return jsonify({"error": "invalid credentials"}), 401
        return redirect(url_for("login_page"))

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
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        _save_gmail_credentials(username, credentials)
        gmail_service_cache.pop(username, None)
        add_log(f"Gmail connected for {username}")
    except Exception as error:
        return jsonify({"error": f"OAuth callback failed: {error}"}), 400

    return redirect(url_for("user_home_page", username=username))


@app.post("/gmail/logout")
def gmail_logout():
    username = get_current_username()
    if not username:
        return jsonify({"error": "username is required"}), 400

    deleted_count = _delete_gmail_tokens(username)
    gmail_service_cache.pop(username, None)
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
    username = (payload.get("username") or get_current_username() or "").strip()
    recipient_email = (payload.get("to") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("body") or ""

    if not username:
        return jsonify({"error": "username is required"}), 400
    if not recipient_email or not subject or not body:
        return jsonify({"error": "to, subject, and body are required"}), 400

    try:
        result = route_message(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
            current_user=username,
        )
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

    if not username:
        return jsonify({"error": "username is required", "messages": []}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            messages = list_external_messages(service, folder)
            return jsonify({"source": source, "folder": folder, "messages": messages})
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
        return jsonify({"source": source, "folder": folder, "messages": messages})
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
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}"}), 502

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


@app.delete("/mail/trash/empty")
def empty_trash():
    username = (request.args.get("username") or get_current_username() or "").strip()
    source = (request.args.get("source") or "internal").strip().lower()

    if not username:
        return jsonify({"error": "username is required"}), 400

    if source == "external":
        try:
            service = get_gmail_service(username)
            trashed = list_external_messages(service, "trash", max_results=100)
            for message in trashed:
                message_id = message.get("id")
                if message_id:
                    external_delete_message(service, message_id)
            return jsonify({"message": "Gmail Trash emptied", "deleted": len(trashed)})
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
        except HttpError as error:
            return jsonify({"error": f"Gmail API error: {error}"}), 502

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


@app.put("/edit-message/<message_id>")
def edit_message(message_id):
    payload = request.get_json(silent=True) or {}
    content = payload.get("content", "")

    responses_by_server = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                requests.put,
                f"{server_url}/edit/{message_id}",
                json={"content": content},
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
            return jsonify({"server": server_id, **response.json()})

        if response.status_code == 400:
            return jsonify(response.json()), 400

    return jsonify({"error": "Message not found"}), 404


@app.delete("/delete-message/<message_id>")
def delete_message(message_id):
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
            return jsonify({"server": server_id, **response.json()})

        if response.status_code == 400:
            return jsonify(response.json()), 400

    return jsonify({"error": "Message not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
