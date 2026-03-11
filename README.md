CN Mini Mail System
===================

A mini distributed mail system with:
- one Flask load balancer (`load_balancer.py`)
- three Flask mail servers (`server1.py`, `server2.py`, `server3.py`)
- PostgreSQL storage (`users`, `messages`, `event_logs`)
- HTML frontend templates in `templates/`

Live Deployment
---------------
- **Production (stable):** [Advanced Mail System on Railway](https://mail-system-production.up.railway.app/login) - live deployment of the last stable commit. This free-tier deployment is expected to remain available till 16th March 2026.
- **Render preview:** [mail-system-eude.onrender.com/login](https://mail-system-eude.onrender.com/login) - preview deployment of newer in-progress code. It is currently under construction and not functional.


Current Architecture
--------------------

Request path:

Browser/UI
	-> Load Balancer (`/route`, `/inbox`, `/sent`, `/dashboard`)
	-> Round-robin target server (`S1`/`S2`/`S3`)
	-> PostgreSQL

Ports (local):
- Load Balancer: `5000`
- Server S1: `5001`
- Server S2: `5002`
- Server S3: `5003`

Environment variables:
- `DATABASE_URL` (required): PostgreSQL connection string
- `PORT` (optional): used by load balancer and by Railway entrypoint


How the System Works
--------------------

1) Authentication/User flow
- `POST /register` creates users in `users` table.
- `POST /login` validates credentials.
- UI routes: `/login`, `/register`, `/user-home`, `/dashboard`.

2) Message routing (core LB logic)
- `POST /route` checks receiver exists.
- LB chooses the next `UP` server from `available_servers` (round robin).
- LB forwards to `http://127.0.0.1:500X/receive`.
- LB logs routing events into `event_logs`.

3) Failover and restore
- `POST /fail/<server_id>` marks server `DOWN` and removes it from rotation.
- `POST /restore/<server_id>` marks server `UP` and adds it back.

4) Message lifecycle/integrity
- Server stores message with status `UNREAD` + MD5 checksum.
- Inbox read marks unread messages as `READ`.
- Edit/delete is blocked for already read messages.
- `POST /corrupt/<message_id>` intentionally changes content.
- Read detects checksum mismatch and returns corruption error.

5) User mailbox APIs exposed by LB
- `GET /inbox/<username>`
- `GET /sent/<username>`
- `PUT /edit-message/<message_id>` (fan-out to servers, first valid hit)
- `DELETE /delete-message/<message_id>` (fan-out to servers, first valid hit)
- `DELETE /sent-history/<username>` (soft-hide for sender)
- `DELETE /inbox-history/<username>` (soft-hide for receiver)

6) Dashboard
- `GET /dashboard` renders UI.
- `GET /dashboard-data` returns server status/load/logs/current algorithm.


Project Structure
-----------------
- `load_balancer.py` - public entrypoint, routing, auth APIs, mailbox APIs, dashboard APIs
- `server1.py` / `server2.py` / `server3.py` - mail server replicas (`/receive`, `/messages`, `/edit`, `/delete`, `/corrupt`)
- `templates/` - `login.html`, `register.html`, `user_home.html`, `dashboard.html`
- `requirements.txt` - Python dependencies
- `start.sh` - Railway startup script (starts S1/S2/S3 + gunicorn LB)
- `test_all.ps1` - PowerShell validation script for RR/failover/restore/edit-lock/corruption


Run Locally (Terminal)
----------------------

Prerequisites:
- Python 3.10+
- PostgreSQL running
- A database URL exported as `DATABASE_URL`

Install dependencies:

Windows PowerShell:
`pip install -r requirements.txt`

Linux/macOS:
`pip install -r requirements.txt`

Set database URL:

Windows PowerShell:
`$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DBNAME"`

Linux/macOS:
`export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/DBNAME"`

Start services in 4 terminals:

Terminal 1:
`python server1.py`

Terminal 2:
`python server2.py`

Terminal 3:
`python server3.py`

Terminal 4:
`python load_balancer.py`

Open:
- App/Login: `http://127.0.0.1:5000/login`
- Dashboard: `http://127.0.0.1:5000/dashboard`
- Dashboard JSON: `http://127.0.0.1:5000/dashboard-data`


Run with Custom Local Ports (Optional)
--------------------------------------

Current code has fixed internal server URLs in LB:
- `S1 -> 127.0.0.1:5001`
- `S2 -> 127.0.0.1:5002`
- `S3 -> 127.0.0.1:5003`

So if you change server ports, you must update `S1_URL`, `S2_URL`, `S3_URL` in `load_balancer.py` to match.


Railway Hosting (Current Setup)
-------------------------------

Deployment model used here:
- One Railway service runs all 4 processes in a single container.
- `start.sh` starts `server1.py`, `server2.py`, `server3.py` in background.
- `gunicorn load_balancer:app --bind 0.0.0.0:$PORT` exposes the public app.

Steps:
1. Push this repo to GitHub.
2. In Railway, create a new project from the repo.
3. Add PostgreSQL plugin/service in Railway.
4. Ensure `DATABASE_URL` is available to the app service (Railway usually injects this).
5. Set start command to:
	 `bash start.sh`
6. Deploy.

Important notes for Railway:
- Public traffic should go only to load balancer (`gunicorn` on `$PORT`).
- Internal S1/S2/S3 calls work via `127.0.0.1` inside the same container.
- If you scale multiple replicas of this Railway service, each replica runs its own S1/S2/S3 set.


Testing
-------
Run full behavior test (PowerShell):

`./test_all.ps1`

It checks:
- round robin
- failover
- restore
- edit lock after read
- corruption detection


Main Endpoints
--------------

Load Balancer:
- `GET /health`
- `POST /register`
- `POST /login`
- `POST /route`
- `POST /fail/<server_id>`
- `POST /restore/<server_id>`
- `GET /inbox/<username>`
- `GET /sent/<username>`
- `PUT /edit-message/<message_id>`
- `DELETE /delete-message/<message_id>`
- `DELETE /sent-history/<username>`
- `DELETE /inbox-history/<username>`

Mail servers (each of S1/S2/S3):
- `GET /health`
- `POST /receive`
- `GET /messages/<username>`
- `PUT /edit/<message_id>`
- `DELETE /delete/<message_id>`
- `POST /corrupt/<message_id>`
