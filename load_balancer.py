from flask import Flask, jsonify, request, render_template
import requests
from pathlib import Path
import json


app = Flask(__name__)

server_status = {
    "S1": "UP",
    "S2": "UP",
    "S3": "UP",
}

available_servers = ["S1", "S2", "S3"]
current_index = 0
event_logs = []
last_routed = None

server_urls = {
    "S1": "http://127.0.0.1:5001",
    "S2": "http://127.0.0.1:5002",
    "S3": "http://127.0.0.1:5003",
}


def count_messages(file_path: Path) -> int:
    if not file_path.exists():
        return 0

    content = file_path.read_text(encoding="utf-8").strip()
    if not content:
        return 0

    return len(json.loads(content))


def add_log(message: str) -> None:
    event_logs.append(message)
    if len(event_logs) > 20:
        event_logs.pop(0)


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
    return jsonify(
        {
            "message": "Load Balancer is running",
            "port": 5000,
        }
    )


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.get("/servers")
def get_servers():
    return jsonify(server_status)


@app.get("/dashboard-data")
def dashboard_data():
    server_load = {
        "S1": count_messages(Path("data/server1_messages.json")),
        "S2": count_messages(Path("data/server2_messages.json")),
        "S3": count_messages(Path("data/server3_messages.json")),
    }

    total_messages = sum(server_load.values())

    return jsonify(
        {
            "server_status": server_status,
            "available_servers": available_servers,
            "current_index": current_index,
            "server_load": server_load,
            "total_messages": total_messages,
            "algorithm": "Round Robin",
            "logs": event_logs,
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

    try:
        server_id = get_next_server()
    except ValueError as error:
        return jsonify({"error": str(error)}), 503

    payload = request.get_json(silent=True) or {}
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)