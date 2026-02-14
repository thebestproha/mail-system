from flask import Flask, jsonify, request, render_template, redirect, url_for
import requests
import os
import psycopg2


app = Flask(__name__)

server_status = {
    "S1": "UP",
    "S2": "UP",
    "S3": "UP",
}

available_servers = ["S1", "S2", "S3"]
current_index = 0
last_routed = None

server_urls = {
    "S1": os.getenv("S1_URL", "http://127.0.0.1:5001"),
    "S2": os.getenv("S2_URL", "http://127.0.0.1:5002"),
    "S3": os.getenv("S3_URL", "http://127.0.0.1:5003"),
}

DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    with get_db_connection() as connection:
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
                id INTEGER PRIMARY KEY,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT DEFAULT 'UNREAD',
                timestamp_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                timestamp_read TIMESTAMP,
                checksum TEXT NOT NULL,
                server_id TEXT NOT NULL
                )
                """
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
        connection.commit()


init_db()


def add_log(message: str) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO event_logs (event) VALUES (%s)", (message,))
        connection.commit()


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


@app.get("/")
def home():
    return redirect(url_for("login_page"))


@app.get("/health")
def health():
    return jsonify(
        {
            "message": "Load Balancer is running",
            "port": 5000,
        }
    )


@app.get("/login")
def login_page():
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
    with get_db_connection() as connection:
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
    global last_routed

    payload = request.get_json(silent=True) or {}
    receiver = (payload.get("receiver") or "").strip()

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM users WHERE username = %s",
                (receiver,),
            )
            matched_receiver = cursor.fetchone()

    if matched_receiver is None:
        return jsonify({"error": "Receiver does not exist"}), 400

    try:
        server_id = get_next_server()
    except ValueError as error:
        return jsonify({"error": str(error)}), 503

    message_id = payload.get("id")
    target_url = f"{server_urls[server_id]}/receive"

    try:
        response = requests.post(target_url, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        return jsonify({"error": str(error)}), 502

    last_routed = server_id
    add_log(f"Message {message_id} routed to {server_id}")

    return jsonify(
        {
            "routed_to": server_id,
            "server_response": response.json(),
        }
    )


@app.post("/register")
def register_user():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (username, password) VALUES (%s, %s)",
                    (username, password),
                )
            connection.commit()
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400

    if request.is_json:
        return jsonify({"message": "registered", "username": username}), 201

    return redirect(url_for("login_page"))


@app.post("/login")
def login_user():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE username = %s AND password = %s",
                (username, password),
            )
            matched = cursor.fetchone()

    if matched is None:
        if request.is_json:
            return jsonify({"error": "invalid credentials"}), 401
        return redirect(url_for("login_page"))

    if request.is_json:
        return jsonify({"message": "login successful", "username": username})

    return redirect(url_for("user_home_page", username=username))


@app.get("/inbox/<username>")
def get_inbox(username):
    merged_messages = []
    seen_ids = set()

    for _, server_url in server_urls.items():
        try:
            response = requests.get(f"{server_url}/messages/{username}", timeout=5)
            if response.status_code == 200:
                server_messages = response.json()
                if isinstance(server_messages, list):
                    visible_messages = [
                        message
                        for message in server_messages
                    ]
                    for message in visible_messages:
                        message_id = message.get("id")
                        if message_id in seen_ids:
                            continue
                        seen_ids.add(message_id)
                        merged_messages.append(message)
        except requests.RequestException:
            continue

    merged_messages.sort(key=lambda item: item.get("timestamp_sent", ""), reverse=True)
    return jsonify(merged_messages)


@app.get("/sent/<username>")
def get_sent_messages(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, receiver, content, status, timestamp_sent, timestamp_read, checksum, server_id
                FROM messages
                WHERE sender = %s
                ORDER BY timestamp_sent DESC
                """,
                (username,),
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
            cursor.execute("DELETE FROM messages WHERE sender = %s", (username,))
            hidden_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()

    add_log(f"Cleared sent history for {username} ({hidden_count} messages hidden)")
    return jsonify({"message": "Sent history cleared", "deleted": hidden_count})


@app.delete("/inbox-history/<username>")
def clear_inbox_history(username):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM messages WHERE receiver = %s", (username,))
            hidden_count = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()

    add_log(f"Cleared inbox history for {username} ({hidden_count} messages hidden)")
    return jsonify({"message": "Inbox history cleared", "deleted": hidden_count})


@app.put("/edit-message/<message_id>")
def edit_message(message_id):
    payload = request.get_json(silent=True) or {}
    content = payload.get("content", "")

    for server_id, server_url in server_urls.items():
        try:
            response = requests.put(
                f"{server_url}/edit/{message_id}",
                json={"content": content},
                timeout=5,
            )

            if response.status_code == 200:
                add_log(f"Message {message_id} edited on {server_id}")
                return jsonify({"server": server_id, **response.json()})

            if response.status_code == 400:
                return jsonify(response.json()), 400
        except requests.RequestException:
            continue

    return jsonify({"error": "Message not found"}), 404


@app.delete("/delete-message/<message_id>")
def delete_message(message_id):
    for server_id, server_url in server_urls.items():
        try:
            response = requests.delete(f"{server_url}/delete/{message_id}", timeout=5)

            if response.status_code == 200:
                add_log(f"Message {message_id} deleted on {server_id}")
                return jsonify({"server": server_id, **response.json()})

            if response.status_code == 400:
                return jsonify(response.json()), 400
        except requests.RequestException:
            continue

    return jsonify({"error": "Message not found"}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)