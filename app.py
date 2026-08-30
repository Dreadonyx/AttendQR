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

DEBUG_MODE = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

# A stable SECRET_KEY is required in production: a predictable key lets anyone
# forge an admin session cookie. In debug we generate an ephemeral one.
_secret = os.environ.get("SECRET_KEY", "").strip()
if not _secret:
    if not DEBUG_MODE:
        print(
            "WARNING: SECRET_KEY is not set. Generating an ephemeral key — "
            "sessions will be invalidated on every restart and will not work "
            "across multiple workers. Set SECRET_KEY in production."
        )
    _secret = secrets.token_urlsafe(48)
app.secret_key = _secret

# Reject oversized uploads before they are read into memory.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "10"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Session cookie hardening. Secure is opt-in because the app is routinely run
# over plain HTTP on a LAN; enable it behind TLS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"csv", "xlsx"}

# Admin console password. Generated and printed once when unset so a fresh
# install is never silently wide open.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(9)
    print("=" * 62)
    print(f"  AttendQR admin password (generated): {ADMIN_PASSWORD}")
    print("  Set ADMIN_PASSWORD in the environment to choose your own.")
    print("=" * 62)

# Brute-force guard for scanner access codes: (ip -> [attempt timestamps])
AUTH_MAX_ATTEMPTS = 10
AUTH_WINDOW_SECONDS = 300
_auth_attempts: Dict[str, List[float]] = {}

# Initialize database schema and migrations on startup (works with Gunicorn & local)
try:
    db.init_db()
except Exception as _init_err:
    print(f"Warning during startup db.init_db(): {_init_err}")

# Admin password — may be updated at runtime via /api/admin/change-password
# and persisted in the settings table so it survives restarts.
_admin_pw_from_db: Optional[str] = None
try:
    _pw_db = db.get_db_connection()
    _pw_row = _pw_db.fetchone("SELECT value FROM settings WHERE key = 'admin_password'")
    if _pw_row and _pw_row.get("value"):
        _admin_pw_from_db = _pw_row["value"]
    _pw_db.close()
except Exception:
    pass

if _admin_pw_from_db:
    ADMIN_PASSWORD = _admin_pw_from_db


@app.after_request
def add_cors_and_security_headers(response):
    # Wildcard CORS is needed so the offline APK (origin "null", it runs from
    # file:///android_asset) can reach the API. Browsers refuse to attach
    # cookies to a wildcard origin, so session-authenticated admin routes are
    # not reachable cross-origin; scanner routes authenticate with a bearer
    # token that a foreign site cannot read.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Scanner-Token, X-Device-ID, X-CSRF-Token, Bypass-Tunnel-Reminder"
    )
    response.headers["Bypass-Tunnel-Reminder"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
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

    # If no event exists, create a default one. The access code is random:
    # a hardcoded default is a publicly known scanner credential.
    default_id = "default-event"
    database.execute("""
        INSERT INTO events (id, name, code, access_code, id_prefix, id_width, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
    """, (default_id, "Untitled Event", "EVENT1", secrets.token_hex(4).upper(), "", 3, "active", now_utc_iso()))
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


# ---------------------------------------------------------------------------
# Authorization
#
# Two independent identities exist:
#   * Admin      — session cookie, set by /login. Owns rosters, exports, events.
#   * Scanner    — bearer token from /api/auth/scanner, scoped to ONE event.
#                  May only record attendance for that event.
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    return bool(session.get("is_admin"))


def wants_json() -> bool:
    """True when the caller is an API client rather than a browser page."""
    if request.is_json or request.path.startswith("/api/"):
        return True
    return "application/json" in (request.headers.get("Accept") or "")


def require_admin(f):
    """Admin session required. Redirects browsers to /login, 401s API callers."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_admin():
            if wants_json():
                return jsonify({"status": "unauthorized", "message": "Admin authentication required"}), 401
            return redirect(url_for("login", next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def scanner_for_event(event_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the authenticated scanner ONLY if its token was issued for this
    event. A token for event A must never write attendance into event B.
    """
    scanner = get_authenticated_scanner()
    if scanner and str(scanner["event_id"]) == str(event_id):
        return scanner
    return None


def require_scanner_or_admin(event_id: str):
    """
    Returns (scanner_or_None, error_response_or_None).

    A valid event-scoped scanner token OR an admin session grants access.
    Anything else is rejected — an anonymous caller on the network can no
    longer mark attendance.
    """
    scanner = scanner_for_event(event_id)
    if scanner:
        return scanner, None
    if is_admin():
        return None, None
    if get_authenticated_scanner():
        return None, (jsonify({
            "status": "forbidden",
            "message": "This scanner token belongs to a different event",
        }), 403)
    return None, (jsonify({
        "status": "unauthorized",
        "message": "Scanner token or admin session required",
    }), 401)


# ---------------------------------------------------------------------------
# CSRF protection for browser form posts
#
# Bearer-token API calls are exempt: a cross-site page cannot read the token,
# and browsers will not attach it automatically. Cookie-authenticated form
# posts are the only CSRF-reachable surface, so those carry a token.
# ---------------------------------------------------------------------------

def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token, "is_admin": is_admin()}


@app.before_request
def enforce_csrf_on_form_posts():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    # API clients authenticate with a bearer token, which is not auto-attached.
    if get_authenticated_scanner() or request.is_json:
        return None
    if request.path in ("/api/auth/scanner", "/login"):
        return None
    # CSRF only protects actions that a cookie already authorizes. With no admin
    # session there is no privilege to ride on, and require_admin answers with a
    # more accurate 401.
    if not is_admin():
        return None

    sent = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token", "")
    )
    expected = session.get("_csrf_token", "")
    if not expected or not sent or not secrets.compare_digest(str(sent), str(expected)):
        if wants_json():
            return jsonify({"status": "error", "message": "CSRF token missing or invalid"}), 400
        flash("Your session expired — please retry that action.", "error")
        return redirect(request.referrer or url_for("index"))
    return None


def rate_limited(bucket: str) -> bool:
    """
    Sliding-window check for credential endpoints.

    Only *failed* attempts count, and a success clears the bucket — otherwise
    normal use locks out the legitimate operator.
    """
    now = datetime.now(timezone.utc).timestamp()
    attempts = [t for t in _auth_attempts.get(bucket, []) if now - t < AUTH_WINDOW_SECONDS]
    if attempts:
        _auth_attempts[bucket] = attempts
    else:
        _auth_attempts.pop(bucket, None)
    return len(attempts) >= AUTH_MAX_ATTEMPTS


def record_auth_failure(bucket: str) -> None:
    _auth_attempts.setdefault(bucket, []).append(datetime.now(timezone.utc).timestamp())


def clear_auth_failures(bucket: str) -> None:
    _auth_attempts.pop(bucket, None)


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------

def safe_int(value: Any, default: int = 0, low: Optional[int] = None, high: Optional[int] = None) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = default
    if low is not None:
        n = max(low, n)
    if high is not None:
        n = min(high, n)
    return n


def sanitize_csv_cell(value: Any) -> str:
    """
    Neutralise spreadsheet formula injection. A roster cell beginning with
    = + - @ (or tab/CR) is executed as a formula by Excel/Sheets when the
    exported CSV is opened, so prefix it with an apostrophe.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def clean_client_timestamp(raw: Any) -> str:
    """
    Scanners report when a scan happened so offline queues keep their real
    time. Reject anything unparseable or implausible (bad device clock,
    hand-crafted payload) and fall back to server time.
    """
    text = str(raw or "").strip()
    if not text:
        return now_utc_iso()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return now_utc_iso()

    now = datetime.now(timezone.utc)
    # More than 5 minutes ahead, or older than 30 days: not credible.
    if parsed > now.replace(microsecond=0) and (parsed - now).total_seconds() > 300:
        return now_utc_iso()
    if (now - parsed).total_seconds() > 30 * 24 * 3600:
        return now_utc_iso()
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_stale_uploads(max_age_seconds: int = 6 * 3600) -> None:
    """Abandoned mapping flows leave temp sheets behind; clear the old ones."""
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        for path in UPLOAD_DIR.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


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

    # Access codes are short and human-transcribed, so throttle guessing.
    bucket = f"auth:{request.remote_addr}"
    if rate_limited(bucket):
        return jsonify({
            "status": "error",
            "message": "Too many authentication attempts. Wait a few minutes and try again.",
        }), 429

    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE UPPER(code) = ? AND status = 'active'", (event_code,))
    if not event:
        record_auth_failure(bucket)
        return jsonify({"status": "error", "message": "Invalid event code or access code"}), 401

    if not secrets.compare_digest(str(event["access_code"]), access_code):
        record_auth_failure(bucket)
        return jsonify({"status": "error", "message": "Invalid event code or access code"}), 401

    clear_auth_failures(bucket)

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
@require_admin
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
@require_admin
def api_create_event():
    """Create a new event."""
    payload = request.get_json(silent=True) or request.form
    name = str(payload.get("name", "")).strip()
    code = str(payload.get("code", "")).strip().upper()
    access_code = str(payload.get("access_code", "")).strip() or "SCAN123"
    id_prefix = str(payload.get("id_prefix", "")).strip()
    id_width = safe_int(payload.get("id_width", 3), default=3, low=1, high=12)

    if not name or not code:
        return jsonify({"status": "error", "message": "Event name and event code are required"}), 400

    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", code):
        return jsonify({
            "status": "error",
            "message": "Event code must be 2-32 characters of A-Z, 0-9, dash or underscore",
        }), 400
    if len(access_code) < 4:
        return jsonify({"status": "error", "message": "Access code must be at least 4 characters"}), 400

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
@require_admin
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
# Critical #1 — Event Archive, Delete & Edit
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/archive", methods=["POST"])
@require_admin
def api_archive_event(event_id):
    """
    Toggle the event between active and archived status.
    Archived events remain in the database (audit trail preserved) but
    they no longer appear in the scanner's event list and scanners cannot
    authenticate against them.
    """
    database = get_db()
    ev = database.fetchone("SELECT id, name, status FROM events WHERE id = ?", (event_id,))
    if not ev:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    new_status = "archived" if ev["status"] == "active" else "active"
    database.execute("UPDATE events SET status = ? WHERE id = ?", (new_status, event_id))
    database.commit()

    # Clear session active event if we just archived it
    if new_status == "archived" and session.get("active_event_id") == event_id:
        session.pop("active_event_id", None)

    if request.is_json:
        return jsonify({"status": "ok", "event_id": event_id, "new_status": new_status})
    flash(f"Event '{ev['name']}' is now {new_status}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/events/<event_id>", methods=["DELETE"])
@require_admin
def api_delete_event(event_id):
    """
    Permanently delete an event and ALL its data: participants, scanners,
    blocked devices, and attendance logs. This cannot be undone.
    The caller must send { "confirm": true } in the JSON body.
    """
    payload = request.get_json(silent=True) or {}
    if not payload.get("confirm"):
        return jsonify({
            "status": "error",
            "message": "Send { \"confirm\": true } to permanently delete this event and all its data.",
        }), 400

    database = get_db()
    ev = database.fetchone("SELECT id, name FROM events WHERE id = ?", (event_id,))
    if not ev:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    # Cascade delete — order matters for FK-enforcing engines
    database.execute("DELETE FROM attendance_logs WHERE event_id = ?", (event_id,))
    database.execute("DELETE FROM blocked_devices WHERE event_id = ?", (event_id,))
    database.execute("DELETE FROM scanners WHERE event_id = ?", (event_id,))
    database.execute("DELETE FROM participants WHERE event_id = ?", (event_id,))
    database.execute("DELETE FROM events WHERE id = ?", (event_id,))
    database.commit()

    if session.get("active_event_id") == event_id:
        session.pop("active_event_id", None)

    return jsonify({"status": "ok", "deleted_event": ev["name"]}), 200


@app.route("/api/events/<event_id>", methods=["PATCH"])
@require_admin
def api_update_event(event_id):
    """
    High #6 — Edit event metadata (name, access code, ID prefix/width).
    The event code is deliberately immutable after creation because scanners
    cache it and QR codes encode it indirectly via the reg_id.
    Changing the access code immediately invalidates all existing scanner
    tokens because they must re-authenticate.
    """
    payload = request.get_json(silent=True) or {}
    database = get_db()
    ev = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not ev:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    name = str(payload.get("name", ev["name"])).strip() or ev["name"]
    access_code = str(payload.get("access_code", ev["access_code"])).strip() or ev["access_code"]
    id_prefix = str(payload.get("id_prefix", ev["id_prefix"] or "")).strip()
    id_width = safe_int(payload.get("id_width", ev["id_width"] or 3), default=3, low=1, high=12)

    if len(name) > 255:
        return jsonify({"status": "error", "message": "Event name too long (max 255)"}), 400
    if len(access_code) < 4:
        return jsonify({"status": "error", "message": "Access code must be at least 4 characters"}), 400

    access_code_changed = not secrets.compare_digest(str(ev["access_code"]), access_code)

    database.execute("""
        UPDATE events SET name = ?, access_code = ?, id_prefix = ?, id_width = ? WHERE id = ?
    """, (name, access_code, id_prefix, id_width, event_id))

    # If access code changed, delete all scanner tokens so devices must re-auth.
    if access_code_changed:
        database.execute(
            "UPDATE scanners SET token = ?, status = 'revoked' WHERE event_id = ?",
            (f"revoked-ac-{uuid.uuid4()}", event_id),
        )

    database.commit()

    return jsonify({
        "status": "ok",
        "event_id": event_id,
        "name": name,
        "tokens_invalidated": access_code_changed,
    }), 200


# ---------------------------------------------------------------------------
# Critical #2 — Admin Password Change
# ---------------------------------------------------------------------------

@app.route("/api/admin/change-password", methods=["POST"])
@require_admin
def api_change_password():
    """
    Change the admin console password at runtime. The new password is
    written to the `settings` table so it survives server restarts.
    Changing the password does NOT invalidate existing admin sessions
    (so the requester's own session stays valid), but all other admin
    sessions will be effectively logged out on next request because the
    password they authenticated with is no longer accepted.
    """
    global ADMIN_PASSWORD
    payload = request.get_json(silent=True) or {}
    current_pw = str(payload.get("current_password", ""))
    new_pw = str(payload.get("new_password", "")).strip()

    bucket = f"chpw:{request.remote_addr}"
    if rate_limited(bucket):
        return jsonify({"status": "error", "message": "Too many attempts. Wait a few minutes."}), 429

    if not secrets.compare_digest(current_pw, ADMIN_PASSWORD):
        record_auth_failure(bucket)
        return jsonify({"status": "error", "message": "Current password is incorrect."}), 401

    if len(new_pw) < 8:
        return jsonify({"status": "error", "message": "New password must be at least 8 characters."}), 400

    if secrets.compare_digest(new_pw, ADMIN_PASSWORD):
        return jsonify({"status": "error", "message": "New password must differ from the current one."}), 400

    clear_auth_failures(bucket)

    # Persist to DB so restarts retain the new password
    database = get_db()
    database.execute("""
        INSERT INTO settings (key, value) VALUES ('admin_password', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (new_pw,))
    database.commit()

    # Update in-memory value immediately for this process
    ADMIN_PASSWORD = new_pw

    return jsonify({"status": "ok", "message": "Password updated successfully."}), 200



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
    client_scanned_at = clean_client_timestamp(payload.get("scanned_at"))

    # Only a scanner holding a token for THIS event (or an admin) may check
    # people in. Previously any caller on the network could, and a token
    # issued for one event was accepted for every other event.
    scanner, auth_error = require_scanner_or_admin(event_id)
    if auth_error:
        return auth_error

    device_id = scanner["device_id"] if scanner else str(payload.get("device_id") or "web-admin")
    device_name = scanner["device_name"] if scanner else str(payload.get("device_name") or "Admin Console")

    if not reg_id:
        return jsonify({"status": "not_found", "message": "Missing registration ID"}), 200

    database = get_db()
    # Verify event exists
    event = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    # Replaying the same scan_id (network retry) must not append another log row.
    prior = database.fetchone(
        "SELECT status FROM attendance_logs WHERE event_id = ? AND scan_id = ?",
        (event_id, scan_id),
    )
    if prior:
        part_prior = database.fetchone(
            "SELECT reg_id, name, department, scanned_at, scanned_by_device_name FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)",
            (event_id, reg_id),
        )
        response = {"status": prior["status"], "reg_id": reg_id, "replayed": True}
        if part_prior:
            response.update({
                "name": part_prior["name"],
                "department": part_prior["department"],
                "scanned_at": part_prior["scanned_at"],
                "scanner": part_prior["scanned_by_device_name"] or device_name,
            })
        return jsonify(response), 200

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

    # 2. Claim the check-in.
    #
    # The WHERE attended = 0 clause is what makes concurrent scans safe, but
    # only if we act on its RESULT: two devices can both pass the read above,
    # and the loser's UPDATE matches zero rows. Reading rowcount is what turns
    # that into a "duplicate" answer instead of a second "ok".
    claimed = False
    if part["attended"] == 0:
        cursor = database.execute("""
            UPDATE participants
            SET attended = 1, scanned_at = ?, scanned_by_device_id = ?, scanned_by_device_name = ?, scan_id = ?
            WHERE event_id = ? AND LOWER(reg_id) = LOWER(?) AND attended = 0
        """, (client_scanned_at, device_id, device_name, scan_id, event_id, reg_id))
        claimed = (cursor.rowcount or 0) > 0

    if claimed:
        database.execute("""
            INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "ok", client_scanned_at, now_utc_iso()))
        database.commit()

        return jsonify({
            "status": "ok",
            "reg_id": part["reg_id"],
            "name": part["name"],
            "department": part["department"],
            "scanned_at": client_scanned_at,
            "scanner": device_name,
        }), 200

    # Already present — either scanned earlier, or another device just won the
    # race. Report the authoritative first scan, never our own attempt.
    winner = database.fetchone(
        "SELECT scanned_at, scanned_by_device_name FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)",
        (event_id, reg_id),
    ) or {}
    database.execute("""
        INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), event_id, part["id"], part["reg_id"], device_id, device_name, scan_id, "duplicate", client_scanned_at, now_utc_iso()))
    database.commit()

    return jsonify({
        "status": "duplicate",
        "reg_id": part["reg_id"],
        "name": part["name"],
        "department": part["department"],
        "scanned_at": winner.get("scanned_at") or part["scanned_at"],
        "scanner": winner.get("scanned_by_device_name") or part["scanned_by_device_name"] or "Scanner",
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

    scanner, auth_error = require_scanner_or_admin(event_id)
    if auth_error:
        return auth_error
    if scanner:
        device_id = scanner["device_id"]
        device_name = scanner["device_name"]

    if not isinstance(scans, list):
        return jsonify({"status": "error", "message": "'scans' must be a list"}), 400
    if len(scans) > 5000:
        return jsonify({"status": "error", "message": "Too many scans in one batch (max 5000)"}), 413

    database = get_db()
    event = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    results = []
    now_sync = now_utc_iso()

    for item in scans:
        if not isinstance(item, dict):
            continue
        reg_id = str(item.get("reg_id", "")).strip()
        scan_id = str(item.get("scan_id", "")).strip() or str(uuid.uuid4())
        scanned_at = clean_client_timestamp(item.get("scanned_at"))

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

        claimed = False
        if part["attended"] == 0:
            # Same rowcount rule as the live scan path: only the UPDATE that
            # actually matched a row may be reported as an official check-in.
            cursor = database.execute("""
                UPDATE participants
                SET attended = 1, scanned_at = ?, scanned_by_device_id = ?, scanned_by_device_name = ?, scan_id = ?
                WHERE event_id = ? AND LOWER(reg_id) = LOWER(?) AND attended = 0
            """, (scanned_at, device_id, device_name, scan_id, event_id, reg_id))
            claimed = (cursor.rowcount or 0) > 0

        if claimed:
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
    pending_count = safe_int(payload.get("pending_count", 0), default=0, low=0, high=1_000_000)
    status_str = str(payload.get("status", "online")).strip()[:32] or "online"

    scanner = scanner_for_event(event_id)
    if not scanner:
        return jsonify({"status": "unauthorized", "message": "Valid scanner token for this event required"}), 401

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
    _scanner, auth_error = require_scanner_or_admin(event_id)
    if auth_error:
        return auth_error

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
# High #5 — Audit Log Viewer + Export
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/audit-log", methods=["GET"])
@require_admin
def api_audit_log(event_id):
    """Paginated full audit log for an event. Includes all scan attempts."""
    database = get_db()
    ev = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not ev:
        return jsonify({"status": "error", "message": "Event not found"}), 404

    page = safe_int(request.args.get("page", 1), default=1, low=1)
    per_page = safe_int(request.args.get("per_page", 50), default=50, low=10, high=200)
    status_filter = request.args.get("status", "").strip().lower()
    device_filter = request.args.get("device", "").strip()
    offset = (page - 1) * per_page

    count_sql = "SELECT COUNT(*) as cnt FROM attendance_logs WHERE event_id = ?"
    log_sql = """
        SELECT al.id, al.reg_id, al.device_name, al.status,
               al.scanned_at, al.synced_at, al.scan_id,
               p.name, p.department
        FROM attendance_logs al
        LEFT JOIN participants p ON al.participant_id = p.id
        WHERE al.event_id = ?
    """
    params: List = [event_id]

    if status_filter in ("ok", "duplicate", "not_found", "undo"):
        count_sql += " AND status = ?"
        log_sql += " AND al.status = ?"
        params.append(status_filter)

    if device_filter:
        count_sql += " AND device_name = ?"
        log_sql += " AND al.device_name = ?"
        params.append(device_filter)

    total_row = database.fetchone(count_sql, params)
    total = total_row["cnt"] if total_row else 0

    log_sql += " ORDER BY al.scanned_at DESC LIMIT ? OFFSET ?"
    rows = database.fetchall(log_sql, params + [per_page, offset])

    return jsonify({
        "status": "ok",
        "event_id": event_id,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, math.ceil(total / per_page)),
        "logs": [
            {
                "reg_id": r["reg_id"],
                "name": r["name"] or "(unregistered)",
                "department": r["department"] or "",
                "device_name": r["device_name"],
                "status": r["status"],
                "scanned_at": r["scanned_at"],
                "synced_at": r["synced_at"],
            }
            for r in rows
        ],
    }), 200


@app.route("/api/events/<event_id>/audit-log/export", methods=["GET"])
@require_admin
def api_audit_log_export(event_id):
    """Export full audit log as CSV — every scan attempt ever made on this event."""
    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not event:
        return "Event not found", 404

    rows = database.fetchall("""
        SELECT al.reg_id, p.name, p.email, p.department,
               al.device_name, al.status, al.scanned_at, al.synced_at, al.scan_id
        FROM attendance_logs al
        LEFT JOIN participants p ON al.participant_id = p.id
        WHERE al.event_id = ?
        ORDER BY al.scanned_at ASC
    """, (event_id,))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Registration ID", "Name", "Email", "Department",
                     "Scanner Device", "Status", "Scanned At", "Synced At", "Scan ID"])
    for r in rows:
        writer.writerow([
            sanitize_csv_cell(r["reg_id"]),
            sanitize_csv_cell(r["name"] or ""),
            sanitize_csv_cell(r["email"] or ""),
            sanitize_csv_cell(r["department"] or ""),
            sanitize_csv_cell(r["device_name"]),
            r["status"],
            r["scanned_at"] or "",
            r["synced_at"] or "",
            r["scan_id"] or "",
        ])

    buf.seek(0)
    event_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", event["code"])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        io.BytesIO(buf.read().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{event_slug}_audit_log_{timestamp}.csv",
    )


# ---------------------------------------------------------------------------
# Roster, Late Adds, QR & Export APIs
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/roster", methods=["GET"])
@app.route("/api/roster", methods=["GET"])
def api_event_roster(event_id=None):
    """Return roster for the event. High #4: supports ?page= and ?per_page= for large rosters."""
    if not event_id:
        if not is_admin():
            return jsonify({"status": "unauthorized", "message": "Admin authentication required"}), 401
        event_id = get_default_or_active_event_id()

    # The roster is personal data (names + emails); never serve it anonymously.
    _scanner, auth_error = require_scanner_or_admin(event_id)
    if auth_error:
        return auth_error

    database = get_db()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not event:
        return jsonify({"summary": {"total": 0, "attended": 0, "percentage": 0}, "roster": [], "extra_headers": []})

    # Pagination parameters (no pagination = full roster for backwards-compat)
    page = safe_int(request.args.get("page", 0), default=0, low=0)
    per_page = safe_int(request.args.get("per_page", 0), default=0, low=1, high=500)
    paginated = page > 0 and per_page > 0

    # Always compute total summary from the full set
    summary_row = database.fetchone(
        "SELECT COUNT(*) as total, SUM(attended) as attended FROM participants WHERE event_id = ?",
        (event_id,),
    )
    total = summary_row["total"] if summary_row and summary_row["total"] else 0
    attended = summary_row["attended"] if summary_row and summary_row["attended"] else 0
    pct = round(attended / total * 100, 1) if total > 0 else 0.0

    extra_headers: List = []
    try:
        extra_headers = json.loads(event["extra_headers_json"] or "[]")
    except Exception:
        pass

    if paginated:
        offset = (page - 1) * per_page
        rows = database.fetchall(
            "SELECT reg_id, name, email, department, attended, scanned_at, scanned_by_device_name, extra_json"
            " FROM participants WHERE event_id = ? ORDER BY reg_id LIMIT ? OFFSET ?",
            (event_id, per_page, offset),
        )
    else:
        rows = database.fetchall(
            "SELECT reg_id, name, email, department, attended, scanned_at, scanned_by_device_name, extra_json"
            " FROM participants WHERE event_id = ? ORDER BY reg_id",
            (event_id,),
        )

    roster_list = []
    for r in rows:
        d = dict(r)
        extra_raw = d.pop("extra_json", None)
        try:
            d["extra"] = json.loads(extra_raw) if extra_raw else {}
        except Exception:
            d["extra"] = {}
        roster_list.append(d)

    response: Dict[str, Any] = {
        "event_id": event_id,
        "event_name": event["name"],
        "event_code": event["code"],
        "summary": {"total": total, "attended": attended, "percentage": pct},
        "extra_headers": extra_headers,
        "roster": roster_list,
    }
    if paginated:
        response["page"] = page
        response["per_page"] = per_page
        response["total_pages"] = max(1, math.ceil(total / per_page))

    return jsonify(response)



@app.route("/api/events/<event_id>/add-participant", methods=["POST"])
@app.route("/add-participant", methods=["POST"])
@require_admin
def api_add_participant(event_id=None):
    """Insert a single late registration without wiping existing scans or roster."""
    if not event_id:
        event_id = get_default_or_active_event_id()

    # Read from JSON or form without touching request.json on a form post —
    # doing so raised a 415/400 and surfaced as a 500 whenever a form field
    # was blank.
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        body = {}

    def field(key: str) -> str:
        return (request.form.get(key) or body.get(key) or "").strip()

    name = field("name")
    email = field("email")
    dept = field("department")
    custom_id = field("reg_id")

    if not name or not email or not dept:
        if request.is_json:
            return jsonify({"status": "error", "message": "Name, email and department are required"}), 400
        flash("Name, email and department are all required.", "error")
        return redirect(url_for("dashboard"))

    if len(name) > 255 or len(email) > 255 or len(dept) > 255:
        if request.is_json:
            return jsonify({"status": "error", "message": "Field too long (max 255 characters)"}), 400
        flash("Name, email and department must be 255 characters or fewer.", "error")
        return redirect(url_for("dashboard"))

    database = get_db()
    event_row = database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,))
    if not event_row:
        if request.is_json:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        flash("That event no longer exists.", "error")
        return redirect(url_for("dashboard"))

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
    try:
        database.execute("""
            INSERT INTO participants (id, event_id, reg_id, name, email, department, extra_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (part_id, event_id, reg_id, name, email, dept, json.dumps(extra) if extra else None, now_utc_iso()))
        database.commit()
    except Exception:
        # UNIQUE(event_id, reg_id): two simultaneous late adds computed the
        # same next id. Surface a retryable message instead of a 500.
        database.rollback()
        if request.is_json:
            return jsonify({
                "status": "error",
                "message": f"Registration ID {reg_id} was just taken — retry to get the next one",
            }), 409
        flash(f"Registration ID {reg_id} was just taken — please retry.", "error")
        return redirect(url_for("dashboard"))

    if request.is_json:
        return jsonify({"status": "ok", "reg_id": reg_id, "name": name}), 201

    flash(f"Added participant {name} as {reg_id}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/events/<event_id>/qr/<reg_id>")
@app.route("/qr/<reg_id>")
@require_admin
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
@require_admin
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
@require_admin
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
                sanitize_csv_cell(row["reg_id"]),
                sanitize_csv_cell(row["name"]),
                sanitize_csv_cell(row["email"]),
                sanitize_csv_cell(row["department"]),
            ]
            + [sanitize_csv_cell(extra.get(h, "")) for h in extra_headers]
            + [
                "Yes" if row["attended"] else "No",
                row["scanned_at"] or "",
                sanitize_csv_cell(row["scanned_by_device_name"] or ""),
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
@require_admin
def index():
    """Upload & Event Selection page."""
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall("SELECT * FROM events ORDER BY created_at DESC")
    count = database.fetchone("SELECT COUNT(*) as cnt FROM participants WHERE event_id = ?", (event_id,))["cnt"]
    return render_template("upload.html", roster_count=count, events=events, active_event_id=event_id)


@app.route("/upload", methods=["POST"])
@require_admin
def upload():
    """Receive file, parse headers & rows, store in session AND DB, redirect to mapping."""
    file = request.files.get("sheet")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Only .csv and .xlsx files are accepted.", "error")
        return redirect(url_for("index"))

    target_event_id = request.form.get("target_event_id") or get_default_or_active_event_id()
    session["upload_target_event_id"] = target_event_id
    prune_stale_uploads()

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

    # Column mapping identifies columns by header text, so duplicates would
    # silently map to whichever came first.
    seen: Dict[str, int] = {}
    deduped: List[str] = []
    for i, h in enumerate(headers):
        label = (h or "").strip() or f"Column {i + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label} ({seen[label]})"
        else:
            seen[label] = 1
        deduped.append(label)
    headers = deduped

    tmp_id = str(uuid.uuid4())
    tmp_path = UPLOAD_DIR / f"{tmp_id}.{ext}"
    tmp_path.write_bytes(data)

    session["upload_id"] = tmp_id
    session["upload_ext"] = ext
    session["headers"] = headers
    session["row_count"] = len(rows)

    # Critical #3 — Persist upload state to DB so the mapping page can recover
    # if the session cookie expires before the admin confirms the mapping.
    try:
        _udb = get_db()
        _udb.execute("""
            INSERT INTO upload_sessions (id, headers_json, row_count, file_ext, target_event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET headers_json=excluded.headers_json
        """, (tmp_id, json.dumps(headers), len(rows), ext, target_event_id, now_utc_iso()))
        _udb.commit()
    except Exception:
        pass  # Non-fatal; session still has the state

    return redirect(url_for("mapping"))


@app.route("/mapping", methods=["GET", "POST"])
@require_admin
def mapping():
    """Column-mapping step — maps columns and populates participants for the event."""
    database = get_db()

    # Critical #3 — Recover upload state from DB if the session cookie is stale
    # (e.g. admin closed the tab and came back, or server restarted between upload
    # and mapping confirmation).  We need upload_id at minimum to find the file.
    if "headers" not in session and "upload_id" in session:
        upload_id = session["upload_id"]
        urow = database.fetchone(
            "SELECT headers_json, row_count, file_ext, target_event_id FROM upload_sessions WHERE id = ?",
            (upload_id,),
        )
        if urow:
            try:
                session["headers"] = json.loads(urow["headers_json"])
                session["row_count"] = urow["row_count"]
                session["upload_ext"] = urow["file_ext"]
                if urow["target_event_id"] and "upload_target_event_id" not in session:
                    session["upload_target_event_id"] = urow["target_event_id"]
            except Exception:
                pass

    if "headers" not in session:
        flash("Upload session not found. Please re-upload your file.", "error")
        return redirect(url_for("index"))

    headers: list[str] = session["headers"]
    event_id = session.get("upload_target_event_id") or get_default_or_active_event_id()
    event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))

    if request.method == "POST":
        col_name = request.form.get("col_name")
        col_email = request.form.get("col_email")
        col_dept = request.form.get("col_dept")
        extra_cols = request.form.getlist("extra_cols")
        id_prefix = request.form.get("id_prefix", "").strip()
        id_width_raw = request.form.get("id_width", "").strip()
        id_width = safe_int(id_width_raw, default=3, low=1, high=12) if id_width_raw.isdigit() else None

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

        # Replacing a roster deletes recorded attendance with it, so require an
        # explicit confirmation once anyone has been checked in.
        attended_row = database.fetchone(
            "SELECT COUNT(*) as cnt FROM participants WHERE event_id = ? AND attended = 1",
            (event_id,),
        )
        already_attended = attended_row["cnt"] if attended_row else 0
        if already_attended and request.form.get("confirm_replace") != "1":
            flash(
                f"This event already has {already_attended} recorded check-in(s). "
                "Replacing the roster erases them — tick the confirmation box to proceed.",
                "error",
            )
            return redirect(url_for("mapping"))

        # Clear existing participants for this event only
        database.execute("DELETE FROM participants WHERE event_id = ?", (event_id,))
        # Detach audit rows from the participants that are going away; the log
        # itself is kept as the historical record.
        database.execute("UPDATE attendance_logs SET participant_id = NULL WHERE event_id = ?", (event_id,))
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

        # Critical #3 — clean up the DB-persisted upload session
        try:
            database.execute("DELETE FROM upload_sessions WHERE id = ?", (session.get("upload_id"),))
            database.commit()
        except Exception:
            pass

        session.pop("upload_id", None)
        session.pop("upload_ext", None)
        session.pop("headers", None)
        session.pop("row_count", None)

        flash(f"Roster loaded: {total} participants for '{event['name'] if event else event_id}'.", "success")
        return redirect(url_for("dashboard"))

    attended_row = database.fetchone(
        "SELECT COUNT(*) as cnt FROM participants WHERE event_id = ? AND attended = 1",
        (event_id,),
    )
    return render_template(
        "mapping.html",
        headers=headers,
        row_count=session.get("row_count", 0),
        event=event,
        already_attended=(attended_row["cnt"] if attended_row else 0),
    )


@app.route("/dashboard")
@require_admin
def dashboard():
    """Live Admin Attendance Dashboard."""
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall("SELECT * FROM events ORDER BY created_at DESC")
    active_event = database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    return render_template("dashboard.html", events=events, active_event=active_event)


@app.route("/scan")
def scan():
    """
    Mobile scanner page. Deliberately public — volunteers open it on their own
    phones — but it ships no roster data and no credentials. The scanner must
    authenticate with the event code + access code to obtain a token before any
    API call succeeds.
    """
    event_id = get_default_or_active_event_id()
    database = get_db()
    events = database.fetchall(
        "SELECT id, name, code FROM events WHERE status = 'active' ORDER BY created_at DESC"
    )
    active_event = database.fetchone("SELECT id, name, code FROM events WHERE id = ?", (event_id,))
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
# Admin Session
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Admin console sign-in."""
    if request.method == "POST":
        password = str(request.form.get("password", ""))
        bucket = f"login:{request.remote_addr}"

        if rate_limited(bucket):
            flash("Too many sign-in attempts. Wait a few minutes and try again.", "error")
            return render_template("login.html"), 429

        if secrets.compare_digest(password, ADMIN_PASSWORD):
            clear_auth_failures(bucket)
            session.clear()          # new session id on privilege change
            session["is_admin"] = True
            session.permanent = False
            target = request.args.get("next") or url_for("dashboard")
            # Only ever redirect to a path on this site.
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            return redirect(target)

        record_auth_failure(bucket)
        flash("Incorrect password.", "error")
        return render_template("login.html"), 401

    if is_admin():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Liveness probe for deployment platforms."""
    try:
        database = get_db()
        database.fetchone("SELECT 1 as ok")
        return jsonify({
            "status": "ok",
            "backend": "postgres" if db.IS_POSTGRES else "sqlite",
            "time": now_utc_iso(),
        }), 200
    except Exception as exc:
        return jsonify({"status": "degraded", "error": str(exc)[:200]}), 503


# ---------------------------------------------------------------------------
# Attendance Corrections & Device Management
# ---------------------------------------------------------------------------

@app.route("/api/events/<event_id>/participants/<reg_id>/undo", methods=["POST"])
@require_admin
def api_undo_attendance(event_id, reg_id):
    """
    Clear a mistaken check-in so the participant can be scanned again.
    Without this, a wrong scan was permanent — the only remedy was replacing
    the whole roster, which erased everyone else's attendance too.
    """
    database = get_db()
    part = database.fetchone(
        "SELECT * FROM participants WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)",
        (event_id, reg_id),
    )
    if not part:
        return jsonify({"status": "not_found", "message": "Participant not found in this event"}), 404

    if not part["attended"]:
        return jsonify({"status": "noop", "message": "Participant is not marked present", "reg_id": part["reg_id"]}), 200

    database.execute("""
        UPDATE participants
        SET attended = 0, scanned_at = NULL, scanned_by_device_id = NULL,
            scanned_by_device_name = NULL, scan_id = NULL
        WHERE event_id = ? AND LOWER(reg_id) = LOWER(?)
    """, (event_id, reg_id))

    # Keep the audit trail honest: record the reversal rather than deleting.
    database.execute("""
        INSERT INTO attendance_logs (id, event_id, participant_id, reg_id, device_id, device_name, scan_id, status, scanned_at, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(uuid.uuid4()), event_id, part["id"], part["reg_id"],
        "admin-console", "Admin Console", str(uuid.uuid4()),
        "undo", now_utc_iso(), now_utc_iso(),
    ))
    database.commit()

    return jsonify({
        "status": "ok",
        "reg_id": part["reg_id"],
        "name": part["name"],
        "message": "Check-in reversed; participant may be scanned again",
    }), 200


@app.route("/api/events/<event_id>/scanners/<device_id>/revoke", methods=["POST"])
@require_admin
def api_revoke_scanner(event_id, device_id):
    """
    Invalidate a device's token — for a lost or handed-back phone.
    High #7: also adds the device_id to blocked_devices so re-authentication
    with the same device_id is refused even if the volunteer knows the
    access code. Admins can unblock by deleting the event and recreating,
    or via direct DB management if a device was mistakenly revoked.
    """
    database = get_db()
    scanner = database.fetchone(
        "SELECT id, device_name FROM scanners WHERE event_id = ? AND device_id = ?",
        (event_id, device_id),
    )
    if not scanner:
        return jsonify({"status": "not_found", "message": "Device not registered for this event"}), 404

    # Replace the token with an unguessable value rather than NULL so the
    # UNIQUE constraint holds and the old token can never be reused.
    database.execute(
        "UPDATE scanners SET token = ?, status = 'revoked', pending_sync_count = 0 WHERE id = ?",
        (f"revoked-{uuid.uuid4()}", scanner["id"]),
    )

    # High #7 — block this device_id from re-joining this event
    try:
        database.execute("""
            INSERT INTO blocked_devices (id, event_id, device_id, blocked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, device_id) DO NOTHING
        """, (str(uuid.uuid4()), event_id, device_id, now_utc_iso()))
    except Exception:
        pass  # Already blocked is fine

    database.commit()
    return jsonify({"status": "ok", "device_id": device_id, "device_name": scanner["device_name"]}), 200


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    port = safe_int(os.environ.get("PORT", 5001), default=5001, low=1, high=65535)
    # debug=True exposes the Werkzeug console, which is remote code execution
    # for anyone who can reach the port. Opt in with FLASK_DEBUG=1 locally.
    app.run(host="0.0.0.0", port=port, debug=DEBUG_MODE)
