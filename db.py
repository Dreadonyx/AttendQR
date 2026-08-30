"""
AttendQR Database Abstraction Layer
Supports both PostgreSQL (Cloud) and SQLite (Local / Offline / Fallback).
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

BASE_DIR = Path(__file__).parent
SQLITE_DB_PATH = BASE_DIR / "attendqr.db"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Standardize Heroku/Render/Railway postgres:// to postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[11:]

IS_POSTGRES = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)

pg_pool: Optional[Any] = None


def get_pg_pool():
    global pg_pool, IS_POSTGRES
    if pg_pool is None and IS_POSTGRES:
        try:
            from psycopg2.pool import ThreadedConnectionPool
            pg_pool = ThreadedConnectionPool(1, 20, DATABASE_URL)
        except Exception as e:
            print(f"Warning: Failed to connect to PostgreSQL ({e}). Falling back to SQLite.")
            IS_POSTGRES = False
    return pg_pool


def _connect_sqlite() -> sqlite3.Connection:
    """
    Open SQLite with settings suited to several scanners writing at once.

    WAL lets the dashboard keep reading while a scan is being written, and a
    busy timeout makes a brief lock wait instead of raising
    "database is locked" in a volunteer's face mid-event.
    """
    conn = sqlite3.connect(
        str(SQLITE_DB_PATH),
        detect_types=sqlite3.PARSE_DECLTYPES,
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    return conn


class DBWrapper:
    """Wrapper that normalizes query execution and parameter binding across SQLite & PostgreSQL."""
    def __init__(self):
        self.is_postgres = IS_POSTGRES
        if self.is_postgres:
            p = get_pg_pool()
            if p:
                self.conn = p.getconn()
                self.conn.autocommit = False
                self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            else:
                self.is_postgres = False
                self.conn = _connect_sqlite()
                self.cursor = self.conn.cursor()
        else:
            self.conn = _connect_sqlite()
            self.cursor = self.conn.cursor()

    def _format_sql(self, sql: str) -> str:
        """Convert '?' to '%s' if running against PostgreSQL."""
        if self.is_postgres:
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Union[Tuple, List] = ()) -> Any:
        formatted_sql = self._format_sql(sql)
        self.cursor.execute(formatted_sql, tuple(params))
        return self.cursor

    def fetchone(self, sql: str, params: Union[Tuple, List] = ()) -> Optional[Dict[str, Any]]:
        self.execute(sql, params)
        row = self.cursor.fetchone()
        if row is None:
            return None
        if self.is_postgres:
            return dict(row)
        return {k: row[k] for k in row.keys()}

    def fetchall(self, sql: str, params: Union[Tuple, List] = ()) -> List[Dict[str, Any]]:
        self.execute(sql, params)
        rows = self.cursor.fetchall()
        if self.is_postgres:
            return [dict(r) for r in rows]
        return [{k: r[k] for k in r.keys()} for r in rows]

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass
        if self.is_postgres and pg_pool is not None:
            try:
                # Rollback any pending transaction state to keep connection clean for next borrow
                self.conn.rollback()
                pg_pool.putconn(self.conn)
            except Exception:
                pass
        elif hasattr(self, 'conn') and self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass


def get_db_connection() -> DBWrapper:
    return DBWrapper()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db():
    """Create all tables and perform migrations if necessary."""
    db = get_db_connection()
    try:
        # 1. Events Table
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                code VARCHAR(64) UNIQUE NOT NULL,
                access_code VARCHAR(64) NOT NULL,
                admin_password_hash VARCHAR(255),
                id_prefix VARCHAR(32) DEFAULT '',
                id_width INTEGER DEFAULT 3,
                extra_headers_json TEXT DEFAULT '[]',
                status VARCHAR(32) DEFAULT 'active',
                created_at VARCHAR(32) NOT NULL
            )
        """)

        # 2. Participants Table
        db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id VARCHAR(64) PRIMARY KEY,
                event_id VARCHAR(64) NOT NULL,
                reg_id VARCHAR(64) NOT NULL,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                department VARCHAR(255) NOT NULL,
                extra_json TEXT,
                attended INTEGER NOT NULL DEFAULT 0,
                scanned_at VARCHAR(32),
                scanned_by_device_id VARCHAR(64),
                scanned_by_device_name VARCHAR(64),
                scan_id VARCHAR(64),
                created_at VARCHAR(32) NOT NULL,
                CONSTRAINT uq_event_reg UNIQUE (event_id, reg_id)
            )
        """)

        # 3. Scanners / Devices Table
        db.execute("""
            CREATE TABLE IF NOT EXISTS scanners (
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
            )
        """)

        # 4. Attendance Audit Logs Table
        db.execute("""
            CREATE TABLE IF NOT EXISTS attendance_logs (
                id VARCHAR(64) PRIMARY KEY,
                event_id VARCHAR(64) NOT NULL,
                participant_id VARCHAR(64),
                reg_id VARCHAR(64) NOT NULL,
                device_id VARCHAR(64) NOT NULL,
                device_name VARCHAR(64) NOT NULL,
                scan_id VARCHAR(64),
                status VARCHAR(32) NOT NULL,
                scanned_at VARCHAR(32) NOT NULL,
                synced_at VARCHAR(32) NOT NULL
            )
        """)

        # 5. Global Settings Table
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(128) PRIMARY KEY,
                value TEXT
            )
        """)

        # 6. Upload Sessions Table — persists roster-upload wizard state
        #    so a stale/expired session cookie does not force a re-upload.
        db.execute("""
            CREATE TABLE IF NOT EXISTS upload_sessions (
                id VARCHAR(64) PRIMARY KEY,
                headers_json TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                file_ext VARCHAR(8) NOT NULL DEFAULT 'xlsx',
                target_event_id VARCHAR(64),
                created_at VARCHAR(32) NOT NULL
            )
        """)

        # 7. Blocked Devices Table — records revoked device IDs per event
        #    so revoked phones cannot immediately re-authenticate with the
        #    same device_id even if they still know the access code.
        db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_devices (
                id VARCHAR(64) PRIMARY KEY,
                event_id VARCHAR(64) NOT NULL,
                device_id VARCHAR(64) NOT NULL,
                blocked_at VARCHAR(32) NOT NULL,
                CONSTRAINT uq_blocked_event_device UNIQUE (event_id, device_id)
            )
        """)

        db.commit()

        # Create indexes
        try:
            db.execute("CREATE INDEX IF NOT EXISTS idx_part_event_reg ON participants(event_id, reg_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scanners_event ON scanners(event_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_logs_event_scan ON attendance_logs(event_id, scan_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_logs_event_time ON attendance_logs(event_id, scanned_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_blocked_event_device ON blocked_devices(event_id, device_id)")
            db.commit()
        except Exception:
            db.rollback()

        # 6. Legacy Data Auto-Migration
        _migrate_legacy_data(db)

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def _migrate_legacy_data(db: DBWrapper):
    """Migrate legacy 'roster' table into the 'events' & 'participants' schema if present."""
    try:
        # Check if any event exists
        row = db.fetchone("SELECT COUNT(*) as cnt FROM events")
        event_count = row["cnt"] if row else 0

        if event_count == 0:
            default_event_id = "default-event"
            created_at = now_utc_iso()
            
            # Read legacy extra headers if present in settings
            extra_headers_json = "[]"
            try:
                setting_row = db.fetchone("SELECT value FROM settings WHERE key = ?", ("extra_headers",))
                if setting_row and setting_row.get("value"):
                    extra_headers_json = setting_row["value"]
            except Exception:
                pass
            
            # Create default event
            db.execute("""
                INSERT INTO events (id, name, code, access_code, id_prefix, id_width, extra_headers_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
            """, (
                default_event_id,
                "Aazhi CTF 2026",
                "AAZHI26",
                "SCAN123",
                "",
                3,
                extra_headers_json,
                "active",
                created_at
            ))
            db.commit()

            # Check if legacy 'roster' table exists
            has_roster = False
            try:
                if not db.is_postgres:
                    r = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='roster'")
                    has_roster = bool(r)
                else:
                    r = db.fetchone("SELECT table_name FROM information_schema.tables WHERE table_name='roster' AND table_schema='public'")
                    has_roster = bool(r)
            except Exception:
                pass

            if has_roster:
                try:
                    roster_rows = db.fetchall("SELECT * FROM roster")
                    for r in roster_rows:
                        p_id = str(uuid.uuid4())
                        db.execute("""
                            INSERT INTO participants (
                                id, event_id, reg_id, name, email, department, extra_json, attended, scanned_at, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT DO NOTHING
                        """, (
                            p_id,
                            default_event_id,
                            r["reg_id"],
                            r["name"],
                            r["email"],
                            r["department"],
                            r.get("extra_json"),
                            r.get("attended", 0),
                            r.get("scanned_at"),
                            created_at
                        ))
                    db.commit()
                except Exception as e:
                    print(f"Legacy migration note: {e}")
                    db.rollback()
    except Exception as e:
        print(f"Migration note: {e}")
        db.rollback()
