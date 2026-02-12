from flask import Flask, jsonify, request
from pathlib import Path
import json
from datetime import datetime
import hashlib


app = Flask(__name__)

SERVER_ID = "S2"
SERVER_PORT = 5002
DATA_FILE = Path("data/server2_messages.json")


def ensure_data_file() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text(json.dumps([], indent=2), encoding="utf-8")


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

    message = {
        "id": message_id,
        "sender": sender,
        "receiver": receiver,
        "content": content,
        "status": "UNREAD",
        "timestamp_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_read": None,
        "checksum": hashlib.md5(content.encode()).hexdigest(),
        "server_id": SERVER_ID,
    }

    ensure_data_file()
    messages = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    messages.append(message)
    DATA_FILE.write_text(json.dumps(messages, indent=2), encoding="utf-8")

    return jsonify(
        {
            "message": "Stored successfully",
            "server": SERVER_ID,
            "id": message_id,
        }
    )


@app.get("/messages/<username>")
def get_messages(username):
    ensure_data_file()
    messages = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    user_messages = []
    updated = False

    for message in messages:
        if message.get("receiver") == username:
            recalculated_checksum = hashlib.md5(message.get("content", "").encode()).hexdigest()
            if message.get("checksum") != recalculated_checksum:
                return jsonify({"error": "Message corrupted", "message_id": message.get("id")}), 400

            if message.get("status") == "UNREAD":
                message["status"] = "READ"
                message["timestamp_read"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                updated = True
            user_messages.append(message)

    if updated:
        DATA_FILE.write_text(json.dumps(messages, indent=2), encoding="utf-8")

    return jsonify(user_messages)


@app.put("/edit/<message_id>")
def edit_message(message_id):
    payload = request.get_json(silent=True) or {}
    new_content = payload.get("content", "")

    ensure_data_file()
    messages = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    for message in messages:
        if str(message.get("id")) == str(message_id):
            if message.get("status") == "READ":
                return jsonify({"error": "Message already read and locked"}), 400

            message["content"] = new_content
            message["checksum"] = hashlib.md5(new_content.encode()).hexdigest()
            DATA_FILE.write_text(json.dumps(messages, indent=2), encoding="utf-8")
            return jsonify({"message": "Updated successfully", "id": message.get("id")})

    return jsonify({"error": "Message not found"}), 404


@app.post("/corrupt/<message_id>")
def corrupt_message(message_id):
    ensure_data_file()
    messages = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    for message in messages:
        if str(message.get("id")) == str(message_id):
            message["content"] = f"{message.get('content', '')} [CORRUPTED]"
            DATA_FILE.write_text(json.dumps(messages, indent=2), encoding="utf-8")
            return jsonify({"message": "Message corrupted for testing", "id": message.get("id")})

    return jsonify({"error": "Message not found"}), 404


if __name__ == "__main__":
    ensure_data_file()
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=True)