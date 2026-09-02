# AttendQR

A lightweight Flask companion app to **CertFlow** for QR-based event attendance tracking.

Upload a Google Form response sheet → assign sequential registration IDs → generate QR codes → scan on event day (from several phones at once, online or offline) → export attendance CSV.

> 📖 **Comprehensive System Documentation**: For full architecture diagrams, end-to-end workflows, REST API specifications, schema definitions, and security mechanics, see [PROJECT_INFO.md](PROJECT_INFO.md).

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3, Flask |
| Storage | PostgreSQL (`psycopg2`) when `DATABASE_URL` is set, otherwise SQLite — no ORM |
| Templating | Jinja2 |
| QR generation | `qrcode[pil]` |
| XLSX parsing | `openpyxl` |
| QR scanning | `html5-qrcode` JS library |
| Serving | `gunicorn` in production, Flask dev server locally |

`db.py` abstracts the two backends: app SQL is written once in SQLite dialect and
`?` placeholders are rewritten to `%s` for PostgreSQL. Development can use
SQLite; production requires PostgreSQL and fails fast if it cannot connect.

---

## Setup

```bash
# 1. Create & activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements-dev.txt

# 3. Configure (see Environment below)
export ADMIN_PASSWORD='choose-something-strong'
export SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"

# 4. Run
python app.py
```

The app starts on **http://0.0.0.0:5001** (reachable from any device on the same
network). The database is created automatically on first run.

### Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `ATTENDQR_ENV` | `development` | Set to `production` for deployed instances. It enables fail-fast checks and secure cookies. |
| `ADMIN_PASSWORD` | *randomly generated locally* | Admin console password. **Required, at least 12 characters, in production.** |
| `SECRET_KEY` | *ephemeral random locally* | Signs session cookies. **Required, at least 32 characters, in production.** |
| `DATABASE_URL` | *(unset → SQLite)* | PostgreSQL connection string. **Required in production.** `postgres://` is normalised automatically. |
| `PORT` | `5001` | Listen port |
| `FLASK_DEBUG` | `0` | `1` enables the Werkzeug debugger. **Never in production** — it is remote code execution for anyone who can reach the port |
| `MAX_UPLOAD_MB` | `10` | Upload size cap |
| `TRUSTED_PROXY_HOPS` | `0` | Set to `1` behind the supplied Nginx reverse proxy. Never enable this without a trusted proxy. |
| `SESSION_MAX_AGE_HOURS` | `8` | Admin session lifetime (1–24 hours). |

## Production deployment

Use PostgreSQL, HTTPS, Gunicorn and a reverse proxy. Do **not** run Flask's
development server for an event.

```bash
cp .env.example /etc/attendqr/attendqr.env
# Edit the file: generate a random SECRET_KEY, set a strong ADMIN_PASSWORD,
# and point DATABASE_URL at managed PostgreSQL.

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ATTENDQR_ENV=production .venv/bin/gunicorn --config gunicorn.conf.py app:app
```

For a system service, adapt [deploy/attendqr.service](deploy/attendqr.service)
and set `TRUSTED_PROXY_HOPS=1`. Configure TLS using
[deploy/nginx.conf](deploy/nginx.conf). The proxy must be the only process
publicly reachable; Gunicorn should listen only on the private host/network.

The included `Dockerfile` also runs Gunicorn in production mode. Supply the
three required secrets (`SECRET_KEY`, `ADMIN_PASSWORD`, `DATABASE_URL`) at
runtime; never bake them into an image or commit a `.env` file.

---

## Access model

Two independent identities:

**Admin** — a session cookie obtained at `/login` with `ADMIN_PASSWORD`. Required
for everything that reads or writes roster data: the upload/mapping flow, the
dashboard, event management, QR generation, CSV export, adding participants,
reversing check-ins, and revoking devices.

**Scanner** — a bearer token obtained at `/api/auth/scanner` with the **event
code + access code**. Scoped to a single event: it may record attendance for
that event and read that event's roster, and nothing else. Volunteers only ever
need the event code and access code, never the admin password.

The scanner page at `/scan` is intentionally public — it is an empty scanner
shell containing no roster data and no credentials. Every API call it makes
requires a token.

---

## Intended Flow

```
0. SIGN IN
   └─ GET/POST /login    Admin console sign-in.

1. UPLOAD SHEET
   └─ GET  /             Upload page — drag & drop CSV or XLSX
   └─ POST /upload       Parse file, store raw data, go to column mapping

2. MAP COLUMNS
   └─ GET/POST /mapping
      Pick which column = Name, Email, Department; optionally carry extra
      columns through to the export. Columns are auto-detected by keyword
      matching (best-effort). Confirming assigns reg IDs 001, 002, … in
      original row order (= timestamp order).
      Replacing a roster that already has check-ins requires an explicit
      confirmation, because it erases that attendance.

3. GENERATE QR CODES
   └─ POST /generate-qr-zip
      Downloads qr_codes.zip — one PNG per participant (001.png, 002.png, …).
      Upload this ZIP into CertFlow for email distribution.

4. RUN EVENT — SCAN ATTENDANCE
   └─ Option A: Mobile App (APK)
      Download from http://<server-ip>:5001/download-apk
      Fully offline-capable; point it at the server in its ⚙️ settings and
      authenticate with the event + access code.
   └─ Option B: Web Browser / PWA
      Open http://<server-ip>:5001/scan on any phone or tablet, connect once
      with the event + access code, then scan.
      Instant green / yellow / red feedback; auto-resumes after 1.5 s.
      Scans made while offline queue in the browser and sync automatically.

5. MONITOR LIVE
   └─ GET /dashboard
      Registered / attended / percentage, searchable roster, connected scanner
      devices, and recent scans. Polls every 3.5 s.
      Also where you reverse a mistaken check-in (↩️) or revoke a device.

6. EXPORT RECORDS
   └─ GET /export-roster
      Downloads <EVENTCODE>_attendance_<timestamp>.csv with all fields,
      attended status, scan time and the device that scanned.
```

---

## API Reference

Auth column: **admin** = session cookie · **scanner** = bearer token for that
event (admin session also accepted) · **public** = no auth.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| GET/POST | `/login` | public | Admin sign-in |
| POST | `/logout` | admin | End the admin session |
| GET | `/healthz` | public | Liveness probe + active DB backend |
| GET | `/` | admin | Upload page |
| POST | `/upload` | admin | Parse sheet, redirect to `/mapping` |
| GET/POST | `/mapping` | admin | Column-mapping step |
| GET | `/dashboard` | admin | Live attendance dashboard |
| GET | `/scan` | public | Scanner page (shell only, no data) |
| POST | `/api/auth/scanner` | public | Event code + access code → device token |
| GET | `/api/events` | admin | List events with summary stats |
| POST | `/api/events` | admin | Create an event |
| POST | `/api/events/<id>/select` | admin | Set the active event |
| POST | `/api/events/<id>/scan` | scanner | Record one check-in |
| POST | `/api/events/<id>/sync` | scanner | Batch-sync queued offline scans |
| POST | `/api/events/<id>/heartbeat` | scanner | Report device liveness + queue depth |
| GET | `/api/events/<id>/stats` | scanner | Live counts, devices, recent scans |
| GET | `/api/events/<id>/roster`, `/api/roster` | scanner | Full roster JSON |
| POST | `/api/events/<id>/add-participant` | admin | Add a late registration |
| POST | `/api/events/<id>/participants/<reg_id>/undo` | admin | Reverse a mistaken check-in |
| POST | `/api/events/<id>/scanners/<device_id>/revoke` | admin | Invalidate a device's token |
| GET | `/api/events/<id>/qr/<reg_id>`, `/qr/<reg_id>` | admin | One participant's QR PNG |
| POST | `/api/events/<id>/generate-qr-zip`, `/generate-qr-zip` | admin | All QR codes as `.zip` |
| GET | `/api/events/<id>/export`, `/export-roster` | admin | Attendance CSV |
| POST | `/api/scan` | scanner | Legacy alias → active event's scan |
| GET | `/download-apk` | public | Android APK |

Browser form posts made with an admin cookie carry a CSRF token
(`csrf_token` field or `X-CSRF-Token` header). Bearer-token API calls are
exempt — a foreign site cannot read the token, and browsers do not attach it
automatically.

### `/api/events/<id>/scan` response shapes

```jsonc
// Success (first scan)
{ "status": "ok", "reg_id": "001", "name": "Jane Doe", "department": "CS",
  "scanned_at": "2026-08-20T06:30:00Z", "scanner": "Scanner-Alpha" }

// Already scanned — the first scan always wins and is never overwritten
{ "status": "duplicate", "reg_id": "001", "name": "Jane Doe",
  "scanned_at": "2026-08-20T06:30:00Z", "scanner": "Scanner-Alpha" }

// Same scan_id seen before (network retry) — no extra audit row is written
{ "status": "ok", "reg_id": "001", "replayed": true }

// Unknown QR
{ "status": "not_found", "reg_id": "999" }
```

---

## Multi-device behaviour

- **No double counting.** Check-in is a conditional `UPDATE … WHERE attended = 0`,
  so when several phones scan the same badge simultaneously exactly one wins.
- **First scan wins.** A duplicate is logged but never overwrites the original
  time or device.
- **Offline queues.** A failed request is queued in the device's local storage
  and flushed when the network returns.
- **Idempotent sync.** Each queued scan carries a client-generated `scan_id`;
  replaying a batch (or retrying a single scan) is safe.
- **Device liveness.** A scanner counts as online if its heartbeat arrived
  within the last 45 seconds.
- **Client clocks.** Devices report when a scan happened so offline queues keep
  their real time. Values that cannot be parsed, sit more than 5 minutes in the
  future, or are older than 30 days are replaced with server time.

---

## Database Schema

```sql
CREATE TABLE events (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(64) UNIQUE NOT NULL,      -- e.g. "AAZHI26"
    access_code VARCHAR(64) NOT NULL,      -- shared with scanner devices
    admin_password_hash VARCHAR(255),      -- reserved; not currently used
    id_prefix VARCHAR(32) DEFAULT '',
    id_width INTEGER DEFAULT 3,
    extra_headers_json TEXT DEFAULT '[]',  -- spreadsheet columns carried through
    status VARCHAR(32) DEFAULT 'active',
    created_at VARCHAR(32) NOT NULL
);

CREATE TABLE participants (
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    reg_id VARCHAR(64) NOT NULL,           -- zero-padded, optional prefix
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    department VARCHAR(255) NOT NULL,
    extra_json TEXT,                       -- extra columns, as JSON
    attended INTEGER NOT NULL DEFAULT 0,
    scanned_at VARCHAR(32),
    scanned_by_device_id VARCHAR(64),
    scanned_by_device_name VARCHAR(64),
    scan_id VARCHAR(64),
    created_at VARCHAR(32) NOT NULL,
    CONSTRAINT uq_event_reg UNIQUE (event_id, reg_id)
);

CREATE TABLE scanners (       -- registered devices, one row per phone per event
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64) NOT NULL,
    device_name VARCHAR(64) NOT NULL,
    token VARCHAR(128) UNIQUE NOT NULL,
    last_seen VARCHAR(32) NOT NULL,
    status VARCHAR(32) DEFAULT 'online',
    pending_sync_count INTEGER DEFAULT 0,
    ip_address VARCHAR(64),
    CONSTRAINT uq_event_device UNIQUE (event_id, device_id)
);

CREATE TABLE attendance_logs (   -- append-only audit of every scan attempt
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    participant_id VARCHAR(64),
    reg_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64) NOT NULL,
    device_name VARCHAR(64) NOT NULL,
    scan_id VARCHAR(64),
    status VARCHAR(32) NOT NULL,     -- ok | duplicate | not_found | undo
    scanned_at VARCHAR(32) NOT NULL,
    synced_at VARCHAR(32) NOT NULL
);

CREATE TABLE settings (key VARCHAR(128) PRIMARY KEY, value TEXT);
```

A legacy single-table `roster` schema is migrated into `events` + `participants`
automatically on first startup.

---

## Tests

```bash
pytest tests/ -q
```

- `tests/test_cloud_multidevice.py` — multi-event isolation, scanner auth,
  concurrent scanning, offline sync, heartbeat, QR + CSV export.
- `tests/test_security_and_logic.py` — authorization, CSRF, rate limiting,
  scan-state and validation regressions.

Both suites write to the configured database, so point `DATABASE_URL` at a
throwaway database (or let them use the local SQLite file) rather than
production.

---

## Security

The access model, the hardening applied to this codebase and the remaining
known issues are documented in [SECURITY_REVIEW.md](SECURITY_REVIEW.md).
Read the deployment checklist there before running this in production.

---

## Notes

- QR data encodes only the `reg_id` (e.g. `"001"`), keeping codes simple and
  scannable in bad lighting (error correction level H).
- QR filenames are exactly `{reg_id}.png` so they sort in registration order —
  CertFlow can match them by filename.
- Uploading a new sheet **replaces** that event's roster; other events are
  untouched.
- Exported CSV cells beginning with `=`, `+`, `-` or `@` are prefixed with an
  apostrophe so spreadsheet apps do not execute them as formulas.
