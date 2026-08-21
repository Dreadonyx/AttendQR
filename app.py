"""
AttendQR — Cloud-First Multi-Device Attendance Platform
Supports PostgreSQL and SQLite, Multi-Event Roster Management,
Scanner Device Authentication, Concurrent Transactional Scans,
Offline Sync with Idempotency, and Live Real-Time Dashboard.
"""
import csv
import io
import json
import math
import os
import re
import secrets
import uuid
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl
import qrcode
import qrcode.constants
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import db

# ---------------------------------------------------------------------------
# App Bootstrap
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "attendqr-cloud-dev-secret-change-me")

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"csv", "xlsx"}


@app.after_request
def add_cors_and_security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Scanner-Token, X-Device-ID, Bypass-Tunnel-Reminder"
    )
    response.headers["Bypass-Tunnel-Reminder"] = "true"
    return response


# ---------------------------------------------------------------------------
# Database Context Helpers
# ---------------------------------------------------------------------------

def get_db() -> db.DBWrapper:
    if "db_conn" not in g:
        g.db_conn = db.get_db_connection()
    return g.db_conn


@app.teardown_appcontext
def close_db_connection(exc=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def pad_id(n: int, total: int, prefix: str = "", width: Optional[int] = None) -> str:
    """Zero-pad n to specified width or auto-calculated width with optional prefix."""
    if width is None or width < 1:
        width = max(3, math.ceil(math.log10(total + 1))) if total >= 1 else 3
    return f"{prefix}{str(n).zfill(width)}"


def get_default_or_active_event_id() -> str:
    """Return the active event_id from session or the first active event in database."""
    database = get_db()
    if "active_event_id" in session:
        ev = database.fetchone("SELECT id FROM events WHERE id = ?", (session["active_event_id"],))
        if ev:
            return ev["id"]

    ev = database.fetchone("SELECT id FROM events WHERE status = 'active' ORDER BY created_at DESC LIMIT 1")
    if ev:
        session["active_event_id"] = ev["id"]
        return ev["id"]

    # If no event exists, create a default one
    default_id = "default-event"
    database.execute("""
        INSERT INTO events (id, name, code, access_code, id_prefix, id_width, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (default_id, "Aazhi CTF 2026", "AAZHI26", "SCAN123", "", 3, "active", now_utc_iso()))
    database.commit()
    session["active_event_id"] = default_id
    return default_id


def next_reg_id_for_event(event_id: str) -> str:
    """Continue whatever ID pattern is already in the event roster."""
    database = get_db()
    event = database.fetchone("SELECT id_prefix, id_width FROM events WHERE id = ?", (event_id,))
    default_prefix = event["id_prefix"] if event and event["id_prefix"] else ""
    default_width = event["id_width"] if event and event["id_width"] else 3

    rows = database.fetchall("SELECT reg_id FROM participants WHERE event_id = ?", (event_id,))
    if not rows:
        return f"{default_prefix}{str(1).zfill(default_width)}"

    pattern = re.compile(r"^(.*?)(\d+)$")
    best_prefix, best_num, best_width = default_prefix, 0, default_width
    for r in rows:
        m = pattern.match(r["reg_id"])
        if not m:
            continue
        prefix, digits = m.group(1), m.group(2)
        num = int(digits)
        if num >= best_num:
            best_num, best_prefix, best_width = num, prefix, max(len(digits), default_width)

    return f"{best_prefix}{str(best_num + 1).zfill(best_width)}"


def get_authenticated_scanner() -> Optional[Dict[str, Any]]:
    """Check Authorization Bearer header or X-Scanner-Token header."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.headers.get("X-Scanner-Token", "").strip()

    if not token:
        return None

    database = get_db()
    scanner = database.fetchone(
        "SELECT * FROM scanners WHERE token = ?", (token,)
    )
    return scanner


def require_scanner_auth(f):
    """Decorator to require valid scanner authentication token."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        scanner = get_authenticated_scanner()
        if not scanner:
            # Check if web session is authenticated or active
            if "active_event_id" in session:
                return f(*args, **kwargs)
            return jsonify({"status": "unauthorized", "error": "Scanner authentication token required"}), 401
        g.scanner = scanner
        return f(*args, **kwargs)
    return decorated_function


def parse_csv_bytes(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def parse_xlsx_bytes(data: bytes) -> tuple[list[str], list[list[str]]]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not all_rows:
        return [], []
    headers = [str(c) if c is not None else "" for c in all_rows[0]]
    data_rows = [
        [str(c) if c is not None else "" for c in row]
        for row in all_rows[1:]
    ]
    return headers, data_rows


# ---------------------------------------------------------------------------
# Authentication APIs
# ---------------------------------------------------------------------------

@app.route("/api/auth/scanner", methods=["POST"])
def api_auth_scanner():
    """
    Authenticate a mobile scanner device via Event Code + Access Code.
    Generates or refreshes a device session token.
    """
    payload = request.get_json(silent=True) or {}
    event_code = str(payload.get("event_code", "")).strip().upper()
    access_code = str(payload.get("access_code", "")).strip()
    device_id = str(payload.get("device_id", "")).strip() or str(uuid.uuid4())
    device_name = str(payload.get("device_name", "")).strip() or f"Scanner-{device_id[:6]}"

    if not event_code or not access_code:
        return jsonify({"status": "error", "message": "Event code and access code are required"}), 400

    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE UPPER(code) = ? AND status = 'active'", (event_code,))
    if not event:
        return jsonify({"status": "error", "message": f"Event '{event_code}' not found or inactive"}), 404

    if event["access_code"] != access_code:
        return jsonify({"status": "error", "message": "Invalid scanner access code"}), 401

    token = generate_token()
    now_ts = now_utc_iso()
    ip_addr = request.remote_addr

    # Insert or update scanner record
    existing_scanner = database.fetchone(
        "SELECT id FROM scanners WHERE event_id = ? AND device_id = ?",
        (event["id"], device_id)
    )

    if existing_scanner:
        database.execute("""
            UPDATE scanners
            SET token = ?, device_name = ?, last_seen = ?, status = 'online', ip_address = ?
            WHERE id = ?
        """, (token, device_name, now_ts, ip_addr, existing_scanner["id"]))
    else:
        scanner_id = str(uuid.uuid4())
        database.execute("""
            INSERT INTO scanners (id, event_id, device_id, device_name, token, last_seen, status, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (scanner_id, event["id"], device_id, device_name, token, now_ts, "online", ip_addr))

    database.commit()

    return jsonify({
        "status": "ok",
        "token": token,
        "event": {
            "id": event["id"],
            "name": event["name"],
            "code": event["code"],
            "id_prefix": event["id_prefix"] or "",
            "id_width": event["id_width"] or 3,
        },
        "scanner": {
            "device_id": device_id,
            "device_name": device_name,
        }
    }), 200


# ---------------------------------------------------------------------------
# Event Management APIs
# ---------------------------------------------------------------------------

@app.route("/api/events", methods=["GET"])
def api_get_events():
    """List all events with summary stats."""
    database = get_db()
    events = database.fetchall("SELECT * FROM events ORDER BY created_at DESC")
    result = []
    for ev in events:
        p_row = database.fetchone("SELECT COUNT(*) as total, SUM(attended) as attended FROM participants WHERE event_id = ?", (ev["id"],))
        total = p_row["total"] if p_row and p_row["total"] else 0
        attended = p_row["attended"] if p_row and p_row["attended"] else 0
        pct = round(attended / total * 100, 1) if total > 0 else 0.0

        s_row = database.fetchone("SELECT COUNT(*) as scanners_cnt FROM scanners WHERE event_id = ?", (ev["id"],))
        scanners_cnt = s_row["scanners_cnt"] if s_row and s_row["scanners_cnt"] else 0

        extra_headers = []
        try:
            extra_headers = json.loads(ev["extra_headers_json"] or "[]")
        except Exception:
            pass

        result.append({
            "id": ev["id"],
            "name": ev["name"],
            "code": ev["code"],
            "access_code": ev["access_code"],
            "id_prefix": ev["id_prefix"] or "",
            "id_width": ev["id_width"] or 3,
            "status": ev["status"],
            "created_at": ev["created_at"],
            "extra_headers": extra_headers,
            "summary": {
                "total": total,
                "attended": attended,
                "percentage": pct,
                "scanners": scanners_cnt,
            }
        })
    return jsonify({"events": result}), 200


@app.route("/api/events", methods=["POST"])
def api_create_event():
    """Create a new event."""
    payload = request.get_json(silent=True) or request.form
    name = str(payload.get("name", "")).strip()
    code = str(payload.get("code", "")).strip().upper()
    access_code = str(payload.get("access_code", "")).strip() or "SCAN123"
    id_prefix = str(payload.get("id_prefix", "")).strip()
    id_width_raw = str(payload.get("id_width", "3")).strip()
    id_width = int(id_width_raw) if id_width_raw.isdigit() else 3

    if not name or not code:
        return jsonify({"status": "error", "message": "Event name and event code are required"}), 400

    database = get_db()
    existing = database.fetchone("SELECT id FROM events WHERE UPPER(code) = ?", (code,))
    if existing:
        return jsonify({"status": "error", "message": f"Event code '{code}' already exists"}), 400

    event_id = str(uuid.uuid4())
    database.execute("""
        INSERT INTO events (id, name, code, access_code, id_prefix, id_width, extra_headers_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (event_id, name, code, access_code, id_prefix, id_width, "[]", "active", now_utc_iso()))
    database.commit()

    session["active_event_id"] = event_id
    if request.is_json:
        return jsonify({"status": "ok", "event_id": event_id, "name": name, "code": code}), 201
    flash(f"Created event '{name}' ({code}).", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/events/<event_id>/select", methods=["POST"])
def api_select_event(event_id):
    """Set active event in session."""
    database = get_db()
    ev = database.fetchone("SELECT id, name FROM events WHERE id = ?", (event_id,))
    if not ev:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    session["active_event_id"] = ev["id"]
    if request.is_json:
        return jsonify({"status": "ok", "event_id": ev["id"], "name": ev["name"]})
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Scanning, Sync & Heartbeat APIs
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/scan", methods=["POST"])
def api_event_scan(event_id):
    """
    Real-time atomic participant check-in for a specific event.
    Guarantees no race conditions or double-counting across concurrent scanner APKs.
    """
    payload = request.get_json(silent=True) or {}
    reg_id = str(payload.get("reg_id", "")).strip()
    scan_id = str(payload.get("scan_id", "")).strip() or str(uuid.uuid4())
    client_scanned_at = str(payload.get("scanned_at", "")).strip() or now_utc_iso()

    scanner = get_authenticated_scanner()
    device_id = scanner["device_id"] if scanner else payload.get("device_id", "web-scanner")
    device_name = scanner["device_name"] if scanner else payload.get("device_name", "Web Client")

    if not reg_id:
        return jsonify({"status": "not_found", "message": "Missing registration ID"}), 200

    database = get_db()
    # Verify event exists
    event = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    # Update scanner heartbeat if scanner authenticated
    if scanner:
        database.execute(
            "UPDATE scanners SET last_seen = ?, status = 'online' WHERE id = ?",
            (now_utc_iso(), scanner["id"])
        )

    # 1. Fetch participant record
    part = database.fetchone(
        "SELECT * FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)",
        (event_id, reg_id)
    )

    if not part:
        # Log unknown QR scan
        log_id = str(uuid.uuid4())
        database.execute("""
            INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (log_id, event_id, None, reg_id, device_id, device_name, scan_id, "not_found", client_scanned_at, now_utc_iso()))
        database.commit()
        return jsonify({"status": "not_found", "reg_id": reg_id}), 200

    # 2. Check if already attended
    if part["attended"] == 0:
        # Atomic update with WHERE attended = 0 condition
        database.execute("""
            UPDATE participants
            SET attended = 1, scanned_at = ?, scanned_by_device_id = ?, scanned_by_device_name = ?, scan_id = ?
            WHERE event_id = ? AND LOWER(reg_id) = LOWER(?) AND attended = 0
        """, (client_scanned_at, device_id, device_name, scan_id, event_id, reg_id))

        log_id = str(uuid.uuid4())
        database.execute("""
            INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (log_id, event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "ok", client_scanned_at, now_utc_iso()))
        database.commit()

        return jsonify({
            "status": "ok",
            "reg_id": part["reg_id"],
            "name": part["name"],
            "department": part["department"],
            "scanned_at": client_scanned_at,
            "scanner": device_name,
        }), 200
    else:
        # Already scanned earlier
        log_id = str(uuid.uuid4())
        database.execute("""
            INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (log_id, event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "duplicate", client_scanned_at, now_utc_iso()))
        database.commit()

        return jsonify({
            "status": "duplicate",
            "reg_id": part["reg_id"],
            "name": part["name"],
            "department": part["department"],
            "scanned_at": part["scanned_at"],
            "scanner": part["scanned_by_device_name"] or "Scanner",
        }), 200


@app.route("/api/events/<event_id>/sync", methods=["POST"])
def api_event_sync(event_id):
    """
    Offline batch synchronization endpoint with idempotency.
    Processes pending offline scans uploaded by an APK when network returns.
    """
    payload = request.get_json(silent=True) or {}
    scans = payload.get("scans", [])
    device_id = str(payload.get("device_id", "")).strip() or "offline-scanner"
    device_name = str(payload.get("device_name", "")).strip() or "Offline Scanner"

    scanner = get_authenticated_scanner()
    if scanner:
        device_id = scanner["device_id"]
        device_name = scanner["device_name"]

    database = get_db()
    event = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    results = []
    now_sync = now_utc_iso()

    for item in scans:
        reg_id = str(item.get("reg_id", "")).strip()
        scan_id = str(item.get("scan_id", "")).strip() or str(uuid.uuid4())
        scanned_at = str(item.get("scanned_at", "")).strip() or now_sync

        if not reg_id:
            results.append({"scan_id": scan_id, "reg_id": reg_id, "status": "not_found"})
            continue

        # Check for idempotency: was this exact scan_id already processed?
        existing_log = database.fetchone(
            "SELECT status FROM attendance_logs WHERE event_id = ? AND scan_id = ?",
            (event_id, scan_id)
        )
        if existing_log:
            results.append({"scan_id": scan_id, "reg_id": reg_id, "status": existing_log["status"]})
            continue

        # Look up participant
        part = database.fetchone(
            "SELECT * FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)",
            (event_id, reg_id)
        )
        if not part:
            log_id = str(uuid.uuid4())
            database.execute("""
                INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_id, event_id, None, reg_id, device_id, device_name, scan_id, "not_found", scanned_at, now_sync))
            results.append({"scan_id": scan_id, "reg_id": reg_id, "status": "not_found"})
            continue

        if part["attended"] == 0:
            # Mark official attendance
            database.execute("""
                UPDATE participants
                SET attended = 1, scanned_at = ?, scanned_by_device_id = ?, scanned_by_device_name = ?, scan_id = ?
                WHERE event_id = ? AND LOWER(reg_id) = LOWER(?) AND attended = 0
            """, (scanned_at, device_id, device_name, scan_id, event_id, reg_id))

            log_id = str(uuid.uuid4())
            database.execute("""
                INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_id, event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "ok", scanned_at, now_sync))

            results.append({
                "scan_id": scan_id,
                "reg_id": part["reg_id"],
                "name": part["name"],
                "status": "ok",
                "scanned_at": scanned_at
            })
        else:
            # Conflict / Duplicate: record in log but do not overwrite earlier authoritative scan
            log_id = str(uuid.uuid4())
            database.execute("""
                INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_id, event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "duplicate", scanned_at, now_sync))

            results.append({
                "scan_id": scan_id,
                "reg_id": part["reg_id"],
                "name": part["name"],
                "status": "duplicate",
                "scanned_at": part["scanned_at"],
                "scanner": part["scanned_by_device_name"]
            })

    # Update scanner pending count
    if scanner:
        database.execute(
            "UPDATE scanners SET last_seen = ?, pending_sync_count = 0 WHERE id = ?",
            (now_sync, scanner["id"])
        )

    database.commit()
    return jsonify({
        "status": "ok",
        "processed_count": len(results),
        "results": results
    }), 200


@app.route("/api/events/<event_id>/heartbeat", methods=["POST"])
def api_event_heartbeat(event_id):
    """Update scanner online status and reported pending sync count."""
    payload = request.get_json(silent=True) or {}
    pending_count = int(payload.get("pending_count", 0))
    status_str = str(payload.get("status", "online")).strip()

    scanner = get_authenticated_scanner()
    if not scanner:
        return jsonify({"status": "unauthorized"}), 401

    database = get_db()
    database.execute("""
        UPDATE scanners
        SET last_seen = ?, pending_sync_count = ?, status = ?, ip_address = ?
        WHERE id = ?
    """, (now_utc_iso(), pending_count, status_str, request.remote_addr, scanner["id"]))
    database.commit()

    return jsonify({"status": "ok", "last_seen": now_utc_iso()}), 200


@app.route("/api/events/<event_id>/stats", methods=["GET"])
def api_event_stats(event_id):
    """Live summary statistics, scanner statuses, and recent scans."""
    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    # Attendance counts
    p_row = database.fetchone("SELECT COUNT(*) as total, SUM(attended) as attended FROM participants WHERE event_id = ?", (event_id,))
    total = p_row["total"] if p_row and p_row["total"] else 0
    attended = p_row["attended"] if p_row and p_row["attended"] else 0
    pct = round(attended / total * 100, 1) if total > 0 else 0.0

    # Scanners status (online if seen in last 45 seconds)
    scanners = database.fetchall("SELECT * FROM scanners WHERE event_id = ? ORDER BY last_seen DESC", (event_id,))
    scanner_list = []
    now_dt = datetime.now(timezone.utc)

    for sc in scanners:
        is_online = False
        try:
            last_seen_dt = datetime.fromisoformat(sc["last_seen"].replace("Z", "+00:00"))
            diff_seconds = (now_dt - last_seen_dt).total_seconds()
            is_online = diff_seconds <= 45
        except Exception:
            pass

        scanner_list.append({
            "device_id": sc["device_id"],
            "device_name": sc["device_name"],
            "is_online": is_online,
            "status": "online" if is_online else "offline",
            "last_seen": sc["last_seen"],
            "pending_sync_count": sc["pending_sync_count"] or 0,
        })

    # Recent Scans
    recent_logs = database.fetchall("""
        SELECT reg_id, device_name, status, scanned_at
        FROM attendance_logs
        WHERE event_id = ?
        ORDER BY scanned_at DESC
        LIMIT 10
    """, (event_id,))

    return jsonify({
        "event": {
            "id": event["id"],
            "name": event["name"],
            "code": event["code"],
        },
        "summary": {
            "total": total,
            "attended": attended,
            "absent": total - attended,
            "percentage": pct,
        },
        "scanners": scanner_list,
        "recent_scans": recent_logs,
    })


# ---------------------------------------------------------------------------
# Roster, Late Adds, QR & Export APIs
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/roster", methods=["GET"])
@app.route("/api/roster", methods=["GET"])
def api_event_roster(event_id=None):
    """Return full roster with extra fields for the event."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"summary": {"total": 0, "attended": 0, "percentage": 0}, "roster": [], "extra_headers": []})

    rows = database.fetchall(
        "SELECT reg_id, name, email, department, attended, scanned_at, scanned_by_device_name, extra_json FROM participants WHERE event_id = ? ORDER BY reg_id",
        (event_id,)
    )
    total = len(rows)
    attended = sum(1 for r in rows if r["attended"])
    pct = round(attended / total * 100, 1) if total > 0 else 0.0

    extra_headers = []
    try:
        extra_headers = json.loads(event["extra_headers_json"] or "[]")
    except Exception:
        pass

    roster_list = []
    for r in rows:
        d = dict(r)
        extra_raw = d.pop("extra_json", None)
        try:
            d["extra"] = json.loads(extra_raw) if extra_raw else {}
        except Exception:
            d["extra"] = {}
        roster_list.append(d)

    return jsonify({
        "event_id": event_id,
        "event_name": event["name"],
        "event_code": event["code"],
        "summary": {"total": total, "attended": attended, "percentage": pct},
        "extra_headers": extra_headers,
        "roster": roster_list,
    })


@app.route("/api/events/<event_id>/add-participant", methods=["POST"])
@app.route("/add-participant", methods=["POST"])
def api_add_participant(event_id=None):
    """Insert a single late registration without wiping existing scans or roster."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    name = request.form.get("name", "").strip() or request.json.get("name", "").strip()
    email = request.form.get("email", "").strip() or request.json.get("email", "").strip()
    dept = request.form.get("department", "").strip() or request.json.get("department", "").strip()
    custom_id = request.form.get("reg_id", "").strip() or (request.json.get("reg_id", "").strip() if request.is_json else "")

    if not name or not email or not dept:
        if request.is_json:
            return jsonify({"status": "error", "message": "Name, email and department are required"}), 400
        flash("Name, email and department are all required.", "error")
        return redirect(url_for("dashboard"))

    database = get_db()
    reg_id = custom_id if custom_id else next_reg_id_for_event(event_id)

    if database.fetchone("SELECT 1 FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)", (event_id, reg_id)):
        if request.is_json:
            return jsonify({"status": "error", "message": f"Registration ID {reg_id} is already taken"}), 400
        flash(f"Registration ID {reg_id} is already taken — pick another.", "error")
        return redirect(url_for("dashboard"))

    event = database.fetchone("SELECT extra_headers_json FROM events WHERE id = ?", (event_id,))
    extra_headers = []
    try:
        extra_headers = json.loads(event["extra_headers_json"] or "[]")
    except Exception:
        pass

    extra = {}
    for h in extra_headers:
        val = request.form.get(f"extra_{h}", "").strip() if not request.is_json else request.json.get("extra", {}).get(h, "")
        if val:
            extra[h] = val

    part_id = str(uuid.uuid4())
    database.execute("""
        INSERT INTO participants (id, event_id, reg_id, name, email, department, extra_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (part_id, event_id, reg_id, name, email, dept, json.dumps(extra) if extra else None, now_utc_iso()))
    database.commit()

    if request.is_json:
        return jsonify({"status": "ok", "reg_id": reg_id, "name": name}), 201

    flash(f"Added participant {name} as {reg_id}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/events/<event_id>/qr/<reg_id>")
@app.route("/qr/<reg_id>")
def qr_single(reg_id, event_id=None):
    """Download one participant's QR PNG pass."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    database = get_db()
    row = database.fetchone("SELECT reg_id FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)", (event_id, reg_id))
    if row is None:
        return "Participant not found", 404

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(row["reg_id"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=f"{row['reg_id']}.png",
    )


@app.route("/api/events/<event_id>/generate-qr-zip", methods=["POST"])
@app.route("/generate-qr-zip", methods=["POST"])
def generate_qr_zip(event_id=None):
    """Generate one QR PNG per roster row in the event, zip them, return for download."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    database = get_db()
    rows = database.fetchall("SELECT reg_id FROM participants WHERE event_id = ? ORDER BY reg_id", (event_id,))

    if not rows:
        flash("Roster is empty — upload a sheet first.", "error")
        return redirect(url_for("dashboard"))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            r_id: str = row["reg_id"]
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
            qr.add_data(r_id)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            img_buf = io.BytesIO()
            img.save(img_buf, format="PNG")
            img_buf.seek(0)
            zf.writestr(f"{r_id}.png", img_buf.read())

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="qr_codes.zip",
    )


@app.route("/api/events/<event_id>/export")
@app.route("/export-roster")
def export_roster(event_id=None):
    """Download attendance CSV for the event."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    rows = database.fetchall(
        "SELECT reg_id, name, email, department, attended, scanned_at, scanned_by_device_name, extra_json FROM participants WHERE event_id = ? ORDER BY reg_id",
        (event_id,)
    )

    extra_headers = []
    if event and event["extra_headers_json"]:
        try:
            extra_headers = json.loads(event["extra_headers_json"])
        except Exception:
            pass

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Registration ID", "Name", "Email", "Department"] + extra_headers + ["Attended", "Scanned At", "Scanner Device"])
    for row in rows:
        extra = {}
        if row["extra_json"]:
            try:
                extra = json.loads(row["extra_json"])
            except Exception:
                pass
        writer.writerow(
            [
                row["reg_id"],
                row["name"],
                row["email"],
                row["department"],
            ]
            + [extra.get(h, "") for h in extra_headers]
            + [
                "Yes" if row["attended"] else "No",
                row["scanned_at"] or "",
                row["scanned_by_device_name"] or "",
            ]
        )

    buf.seek(0)
    event_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", event["code"] if event else "attendance")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        io.BytesIO(buf.read().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{event_slug}_attendance_{timestamp}.csv",
    )


# ---------------------------------------------------------------------------
# Legacy Web Route Compatibility (Single Scan from Web)
# ---------------------------------------------------------------------------

@app.route("/api/scan", methods=["POST"])
def legacy_api_scan():
    """Legacy route forwarding to active event scan."""
    event_id = get_default_or_active_event_id()
    return api_event_scan(event_id)


# ---------------------------------------------------------------------------
# Web App UI Views
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Upload & Event Selection page."""
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall("SELECT * FROM events ORDER BY created_at DESC")
    count = database.fetchone("SELECT COUNT(*) as cnt FROM participants WHERE event_id = ?", (event_id,))["cnt"]
    return render_template("upload.html", roster_count=count, events=events, active_event_id=event_id)


@app.route("/upload", methods=["POST"])
def upload():
    """Receive file, parse headers & rows, store in session, redirect to mapping."""
    file = request.files.get("sheet")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Only .csv and .xlsx files are accepted.", "error")
        return redirect(url_for("index"))

    target_event_id = request.form.get("target_event_id") or get_default_or_active_event_id()
    session["upload_target_event_id"] = target_event_id

    data = file.read()
    ext = file.filename.rsplit(".", 1)[1].lower()

    try:
        if ext == "csv":
            headers, rows = parse_csv_bytes(data)
        else:
            headers, rows = parse_xlsx_bytes(data)
    except Exception as exc:
        flash(f"Could not parse file: {exc}", "error")
        return redirect(url_for("index"))

    if not headers or not rows:
        flash("The file appears to be empty.", "error")
        return redirect(url_for("index"))

    tmp_id = str(uuid.uuid4())
    tmp_path = UPLOAD_DIR / f"{tmp_id}.{ext}"
    tmp_path.write_bytes(data)

    session["upload_id"] = tmp_id
    session["upload_ext"] = ext
    session["headers"] = headers
    session["row_count"] = len(rows)

    return redirect(url_for("mapping"))


@app.route("/mapping", methods=["GET", "POST"])
def mapping():
    """Column-mapping step — maps columns and populates participants for the event."""
    if "headers" not in session:
        return redirect(url_for("index"))

    headers: list[str] = session["headers"]
    event_id = session.get("upload_target_event_id") or get_default_or_active_event_id()
    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))

    if request.method == "POST":
        col_name = request.form.get("col_name")
        col_email = request.form.get("col_email")
        col_dept = request.form.get("col_dept")
        extra_cols = request.form.getlist("extra_cols")
        id_prefix = request.form.get("id_prefix", "").strip()
        id_width_raw = request.form.get("id_width", "").strip()
        id_width = int(id_width_raw) if id_width_raw.isdigit() else None

        if not all([col_name, col_email, col_dept]):
            flash("Please select all three required column mappings.", "error")
            return redirect(url_for("mapping"))

        if len({col_name, col_email, col_dept}) < 3:
            flash("Each required field must map to a different column.", "error")
            return redirect(url_for("mapping"))

        tmp_id = session["upload_id"]
        ext = session["upload_ext"]
        tmp_path = UPLOAD_DIR / f"{tmp_id}.{ext}"

        try:
            raw = tmp_path.read_bytes()
            if ext == "csv":
                _, rows = parse_csv_bytes(raw)
            else:
                _, rows = parse_xlsx_bytes(raw)
        except Exception as exc:
            flash(f"Could not re-read uploaded file: {exc}", "error")
            return redirect(url_for("index"))

        rows = [r for r in rows if any(str(c).strip() for c in r)]
        if not rows:
            flash("No data rows found after skipping blanks.", "error")
            return redirect(url_for("index"))

        idx_name = headers.index(col_name)
        idx_email = headers.index(col_email)
        idx_dept = headers.index(col_dept)
        extra_idx = {c: headers.index(c) for c in extra_cols if c in headers and c not in (col_name, col_email, col_dept)}
        valid_extra_cols = [c for c in extra_cols if c in extra_idx]
        total = len(rows)

        # Clear existing participants for this event only
        database.execute("DELETE FROM participants WHERE event_id = ?", (event_id,))
        created_at = now_utc_iso()

        for i, row in enumerate(rows, start=1):
            reg_id = pad_id(i, total, id_prefix, id_width)
            name = str(row[idx_name]).strip() if idx_name < len(row) else ""
            email = str(row[idx_email]).strip() if idx_email < len(row) else ""
            dept = str(row[idx_dept]).strip() if idx_dept < len(row) else ""
            extra = {c: (str(row[j]).strip() if j < len(row) and row[j] is not None else "") for c, j in extra_idx.items()}
            part_id = str(uuid.uuid4())
            database.execute("""
                INSERT INTO participants (id, event_id, reg_id, name, email, department, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (part_id, event_id, reg_id, name, email, dept, json.dumps(extra) if extra else None, created_at))

        # Update event settings
        database.execute("""
            UPDATE events
            SET id_prefix = ?, id_width = ?, extra_headers_json = ?
            WHERE id = ?
        """, (id_prefix, id_width or 3, json.dumps(valid_extra_cols), event_id))

        database.commit()

        try:
            tmp_path.unlink()
        except OSError:
            pass

        session.pop("upload_id", None)
        session.pop("upload_ext", None)
        session.pop("headers", None)
        session.pop("row_count", None)

        flash(f"Roster loaded: {total} participants for '{event['name'] if event else event_id}'.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "mapping.html",
        headers=headers,
        row_count=session.get("row_count", 0),
        event=event,
    )


@app.route("/dashboard")
def dashboard():
    """Live Admin Attendance Dashboard."""
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall("SELECT * FROM events ORDER BY created_at DESC")
    active_event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    return render_template("dashboard.html", events=events, active_event=active_event)


@app.route("/scan")
def scan():
    """Mobile Scanner Web Page."""
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall("SELECT * FROM events WHERE status = 'active' ORDER BY created_at DESC")
    active_event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    return render_template("scan.html", events=events, active_event=active_event)


@app.route("/download-apk")
def download_apk():
    """Download compiled Android APK."""
    apk_path = BASE_DIR / "AttendQR.apk"
    if not apk_path.exists():
        apk_path = BASE_DIR / "static" / "AttendQR.apk"
    if apk_path.exists():
        return send_file(
            str(apk_path),
            mimetype="application/vnd.android.package-archive",
            as_attachment=True,
            download_name="AttendQR.apk",
        )
    return "APK is not available yet.", 404


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
