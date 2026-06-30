CN Mini Mail System
===================

A mini distributed mail system with:
- one Flask load balancer (`load_balancer.py`)
- three Flask mail servers (`server1.py`, `server2.py`, `server3.py`)
- PostgreSQL storage (`users`, `messages`, `event_logs`)
- HTML frontend templates in `templates/`

Quick Start Guide
-----------------

Demo login for EditMail testing:
- Username: `Test`
- Password: `test`
- Linked Gmail: `editmail.test@gmail.com`

What to know before testing:
- Use `@editmail.com` for internal mail inside the system, for example `Test@editmail.com`.
- Use a real external address like `name@gmail.com`, `name@yahoo.com`, or any other valid domain when you want to send outside the system.
- If you open the app and see `No messages`, click the Refresh button once and wait a little. Render free-tier instances can sleep or take time to wake up, and Gmail sync may also take a moment.
- If you just logged in or switched devices, refresh the mailbox before assuming messages are missing.
- The Gmail link for the `Test` demo account is protected so people do not accidentally disconnect it.
- Once a receiver opens an internal message, the sender can no longer edit or delete that message.
- The read view also shows a `Read at` timestamp when the message has been opened.
- If both sender and receiver delete a message and it is removed from Trash on both sides, the row is purged from PostgreSQL too, so it does not stay anywhere in the system.

Common questions
- Why do messages sometimes appear late? Render free hosting can pause the app, so the first request may take a few seconds.
- Why is the inbox empty after login? The mailbox often needs one manual refresh to fetch the latest Gmail and internal messages.
- Why does my Gmail stay connected across restarts? Gmail tokens are stored in PostgreSQL, so the link is not tied to a single process restart.
- Why do I need the `@editmail.com` domain for internal mail? The router treats that domain as internal and sends it through the distributed replicas.
- Why do edit and delete buttons disappear after a message is opened? The message becomes read-locked, which protects the sender/receiver flow and prevents post-read edits.

Useful browser actions
- Refresh mailbox: use the Refresh button in the top bar.
- Switch source: use the Internal / Gmail toggle.
- Open message details: click a message row.
- Disconnect Gmail: works for normal users, but is disabled for the `Test` demo account.

Live Deployment
---------------
- **Production (stable):** [Advanced Mail System on Railway](https://mail-system-production.up.railway.app/login) - live deployment of the last stable commit. This free-tier deployment is expected to remain available till 16th March 2026.
- **Render target:** deploy using the included `render.yaml` blueprint (recommended command: `gunicorn render_runner.render_app:app -c gunicorn.conf.py`).


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
- Inbox read marks unread messages as `READ` and sets `timestamp_read`.
- Edit/delete is blocked for already read messages, so once the receiver opens the message the sender cannot change or remove it.
- `POST /corrupt/<message_id>` intentionally changes content.
- Read detects checksum mismatch and returns corruption error.

5) User mailbox APIs exposed by LB
- `GET /inbox/<username>`
- `GET /sent/<username>`
- `PUT /edit-message/<message_id>` (fan-out to servers, first valid hit)
- `DELETE /delete-message/<message_id>` (fan-out to servers, first valid hit)
- `DELETE /sent-history/<username>` (soft-hide for sender)
- `DELETE /inbox-history/<username>` (soft-hide for receiver)
- `DELETE /sent-history/<username>` and `DELETE /inbox-history/<username>` only hide the message from that user; the database row is removed only after both sides delete it and the trash entry is fully cleared.

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


Render Hosting (Recommended)
----------------------------

This repository includes `render.yaml` for Render Blueprint deploys.

Why this mode is faster on free tier:
- Uses `render_runner/render_app.py`, which runs LB + replicas in one process.
- Internal LB->replica calls are short-circuited in-process (no network hop).
- Gunicorn defaults are tuned for low-memory instances (`WEB_CONCURRENCY=1`, threaded worker).

Steps:
1. Push this repository to GitHub.
2. In Render, create a new Blueprint and select this repository.
3. Set required environment variables:
	- `DATABASE_URL`
	- `GOOGLE_CLIENT_ID` (if Gmail integration is needed)
	- `GOOGLE_CLIENT_SECRET` (if Gmail integration is needed)
	- `GOOGLE_REDIRECT_URI` (set to `https://<your-render-host>/oauth2callback`)
4. Deploy.

Recommended free-tier env values:
- `WEB_CONCURRENCY=1`
- `GUNICORN_THREADS=4`
- `REQUEST_TIMEOUT_SECONDS=3`
- `REPLICA_TIMEOUT_SECONDS=0.8`


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
