CN Mini Mail System
===================

Project: Mini Multi-User Mail System
Architecture: Browser Client -> Load Balancer (5000) -> Server1 (5001), Server2 (5002), Server3 (5003) -> JSON Files

Files
-----
- load_balancer.py
- server1.py
- server2.py
- server3.py
- data/server1_messages.json
- data/server2_messages.json
- data/server3_messages.json
- templates/dashboard.html

Requirements
------------
- Python 3.x
- Flask
- requests

Install
-------
Run in project folder:

pip install flask requests

Run Services
------------
Open 4 terminals in project folder and run:

python server1.py
python server2.py
python server3.py
python load_balancer.py

Open Dashboard
--------------
- Dashboard UI: http://127.0.0.1:5000/dashboard
- Dashboard JSON: http://127.0.0.1:5000/dashboard-data

Core Features Implemented
-------------------------
1) Round Robin Load Balancing
- /route forwards messages across S1/S2/S3 in cyclic order.

2) Failure Simulation + Self-Healing
- POST /fail/S1 (or S2/S3)
- POST /restore/S1 (or S2/S3)
- DOWN servers are removed from available list and skipped.

3) Message Lifecycle (Editable Until Read)
- POST /receive stores message with UNREAD status.
- PUT /edit/<id> works only while UNREAD.
- GET /messages/<username> marks UNREAD messages as READ.

4) MD5 Integrity + Corruption Simulation
- checksum generated on store/edit.
- POST /corrupt/<id> modifies content without checksum update.
- GET /messages/<username> detects mismatch and returns corruption error.

5) Live Dashboard
- Auto-refresh every 2 seconds.
- Shows status, load, total messages, algorithm, available servers, last routed server, event logs.
- Includes Fail/Restore buttons.

Main Load Balancer Endpoints
----------------------------
- GET  /
- GET  /dashboard
- GET  /servers
- GET  /dashboard-data
- POST /route
- POST /fail/<server_id>
- POST /restore/<server_id>

Server Endpoints (all three servers)
------------------------------------
- GET  /
- GET  /health
- POST /receive
- GET  /messages/<username>
- PUT  /edit/<message_id>
- POST /corrupt/<message_id>

Quick Demo Flow (Viva)
----------------------
1) Round Robin fairness
- Send 9 messages to POST http://127.0.0.1:5000/route
- Verify each JSON file receives 3 messages.

2) Fault tolerance
- POST /fail/S2
- Send 6 messages
- Verify S1 and S3 receive traffic, S2 receives 0.

3) Self-healing
- POST /restore/S2
- Send 3 more messages
- Verify S2 receives again.

4) Editable-until-read
- Send message
- Edit once before reading (success)
- Read via GET /messages/B
- Edit again (blocked)

5) Corruption detection
- POST /corrupt/<id>
- GET /messages/B
- Verify response: {"error": "Message corrupted", "message_id": <id>}

Notes
-----
- Keep all 4 services running during demo.
- Data is stored in JSON files under /data.
