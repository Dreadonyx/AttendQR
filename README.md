# AttendQR

A lightweight Flask companion app to **CertFlow** for QR-based event attendance tracking.

Upload a Google Form response sheet → assign sequential registration IDs → generate QR codes → scan on event day → export attendance CSV.

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3, Flask |
| Storage | SQLite (stdlib `sqlite3`, no ORM) |
| Templating | Jinja2 |
| QR generation | `qrcode[pil]` |
| XLSX parsing | `openpyxl` |
| QR scanning | `html5-qrcode` JS library (CDN) |

---

## Setup

```bash
# 1. Create & activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

The app starts on **http://0.0.0.0:5001** (accessible from any device on the same network).  
The database (`attendqr.db`) is created automatically on first run.

---

## Intended Flow

```
1. UPLOAD SHEET
   └─ GET  /           Upload page — drag & drop CSV or XLSX
   └─ POST /upload     Parse file, store raw data, go to column mapping

2. MAP COLUMNS
   └─ GET/POST /mapping
      Pick which column = Name, Email, Department.
      Columns auto-detected by keyword matching (best-effort).
      Confirms: assigns reg IDs 001, 002, … in original row order (= timestamp order).

3. GENERATE QR CODES
   └─ POST /generate-qr-zip
      Downloads qr_codes.zip — one PNG per participant (001.png, 002.png, …).
      Upload this ZIP into CertFlow for email distribution.

4. RUN EVENT — SCAN ATTENDANCE
   └─ Option A: Mobile App (APK)
      Download AttendQR.apk directly on phone from:
      http://<server-ip>:5001/download-apk
      Has built-in camera permissions, full screen view, and in-app IP settings (⚙️).
   └─ Option B: Web Browser / PWA
      Open http://<server-ip>:5001/scan on any phone/tablet.
      Scan attendee QR → instant green / yellow / red feedback.
      Auto-resumes after 1.5 s.

5. MONITOR LIVE
   └─ GET /dashboard
      Total registered / attended / percentage + searchable table.
      Polls every 4 s automatically.

6. EXPORT RECORDS
   └─ GET /export-roster
      Downloads attendance_<timestamp>.csv with all fields + attended status.
```

---

## API Reference

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/` | Upload page |
| POST | `/upload` | Parse sheet, redirect to `/mapping` |
| GET/POST | `/mapping` | Column-mapping step |
| POST | `/generate-qr-zip` | Return QR codes as `.zip` |
| GET | `/scan` | Mobile scanner page |
| POST | `/api/scan` | JSON: `{reg_id}` → `{status, name, …}` |
| GET | `/dashboard` | Attendance dashboard |
| GET | `/api/roster` | Full roster as JSON |
| GET | `/export-roster` | Download roster CSV |

### `/api/scan` response shapes

```jsonc
// Success (first scan)
{ "status": "ok", "name": "Jane Doe", "department": "CS" }

// Already scanned
{ "status": "duplicate", "name": "Jane Doe", "scanned_at": "2025-08-20T06:30:00Z" }

// Unknown QR
{ "status": "not_found" }
```

---

## Database Schema

```sql
CREATE TABLE roster (
    reg_id      TEXT PRIMARY KEY,   -- zero-padded: "001", "002", …
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    department  TEXT NOT NULL,
    attended    INTEGER NOT NULL DEFAULT 0,
    scanned_at  TEXT                -- ISO 8601 UTC timestamp, nullable
);
```

---

## Notes

- **No authentication** — intended for use on a trusted local network or private EC2 instance.
- Uploading a new sheet **replaces** the existing roster entirely.
- QR data encodes only the `reg_id` (e.g. `"001"`), keeping codes simple and scannable in bad lighting (error correction level H).
- QR filenames are exactly `{reg_id}.png` so they sort in registration order — CertFlow can match them by filename.
