# AttendQR — Complete System Architecture & Technical Documentation

> **Version:** 2.0.0 (Cloud-First Multi-Device Platform)  
> **Ecosystem:** Companion System to **CertFlow** for QR-Based Event Attendance Tracking  
> **Status:** Production-Ready & Security-Hardened  

---

## Table of Contents

1. [Executive Summary & Purpose](#1-executive-summary--purpose)
2. [High-Level Architecture & Workflows](#2-high-level-architecture--workflows)
3. [Core Feature Breakdown](#3-core-feature-breakdown)
4. [Security Architecture & Concurrency Model](#4-security-architecture--concurrency-model)
5. [Database Architecture & Schema](#5-database-architecture--schema)
6. [Complete REST API Reference](#6-complete-rest-api-reference)
7. [Android Mobile App & Native Build Pipeline](#7-android-mobile-app--native-build-pipeline)
8. [Environment Variables & Configuration](#8-environment-variables--configuration)
9. [Deployment & Operations Guide](#9-deployment--operations-guide)
10. [Repository File Map](#10-repository-file-map)

---

## 1. Executive Summary & Purpose

**AttendQR** is a high-throughput, cloud-first event attendance management platform designed for hackathons, conferences, academic symposiums, and corporate events. Built as a companion application to **CertFlow**, AttendQR eliminates bottlenecked registration desks by enabling multiple volunteers to scan attendee QR codes simultaneously from their smartphones (via Web PWA or native Android APK) with absolute zero double-counting, offline resilience, and live real-time dashboard analytics.

### Key Value Propositions

- **Multi-Device Concurrent Scanning**: Scale from 1 to 50+ scanner devices on the same event floor without race conditions or duplicated check-ins.
- **Offline-First Resilience**: Scanners automatically cache check-ins in local device storage during network blackouts and flush queued batches via idempotent sync when connectivity resumes.
- **Instant Roster Ingestion & Column Mapping**: Upload Google Forms or ticketing spreadsheets (`.xlsx` or `.csv`), auto-detect Name, Email, Department, and carry arbitrary custom columns into final exports.
- **Automated High-Res QR Generation**: Generates high error-correction (Level H) QR codes named sequentially (`001.png`, `002.png`, etc.) zipped for direct email distribution in CertFlow.
- **Real-Time Live Dashboard**: Live metrics, auto-refreshing attendance counters, search/filter rosters, active scanner heartbeat monitoring, and one-click check-in reversals.
- **Enterprise-Grade Security**: Dual-tier identity model (Session-authenticated Admin vs. Scoped Bearer Token Scanner), CSRF protection, sliding-window rate limiting, and CSV formula injection neutralization.

---

## 2. High-Level Architecture & Workflows

### 2.1 System Architecture Diagram

```mermaid
graph TD
    subgraph Clients ["Client Layer"]
        PWA["Web PWA Scanner (/scan)"]
        APK["Native Android APK (Offline Assets)"]
        AdminBrowser["Admin Console & Dashboard (/dashboard)"]
    end

    subgraph Server ["Application Layer (Flask / Gunicorn)"]
        AuthModule["Auth & Rate Limiter Module"]
        RosterEngine["Roster Ingestion & Mapping Engine"]
        QRGenerator["QR Generation Engine (PIL / qrcode)"]
        ScanProcessor["Atomic Scan Processor"]
        SyncProcessor["Idempotent Sync Engine"]
        StatsEngine["Live Stats & Heartbeat Monitor"]
        ExportEngine["Sanitized CSV Exporter"]
    end

    subgraph Data ["Storage Layer"]
        DBW["db.py DBWrapper (Dialect Normalizer)"]
        PG[("PostgreSQL (Cloud Pool)")]
        SQLITE[("SQLite (Local WAL Mode)")]
    end

    PWA -->|"Bearer Token / JSON"| ScanProcessor
    PWA -->|"Batch Sync"| SyncProcessor
    APK -->|"REST API"| ScanProcessor
    AdminBrowser -->|"Session Cookie + CSRF"| RosterEngine
    AdminBrowser -->|"Session Cookie"| StatsEngine
    AdminBrowser -->|"Download"| QRGenerator
    AdminBrowser -->|"Download"| ExportEngine

    ScanProcessor --> DBW
    SyncProcessor --> DBW
    RosterEngine --> DBW
    StatsEngine --> DBW
    AuthModule --> DBW

    DBW -->|"DATABASE_URL set"| PG
    DBW -->|"Default / Fallback"| SQLITE
```

---

### 2.2 End-to-End Operational Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Admin as Event Organizer (Admin)
    actor Volunteer as Gate Volunteer (Scanner)
    participant App as AttendQR Server
    participant DB as PostgreSQL / SQLite
    actor Attendee as Event Attendee

    Note over Admin, App: Phase 1: Setup & Roster Ingestion
    Admin->>App: Sign in via /login (ADMIN_PASSWORD)
    Admin->>App: Upload Google Form Sheet (.xlsx / .csv)
    Admin->>App: Map columns (Name, Email, Dept, Custom Headers)
    App->>DB: Populate `events` & `participants` (assigns 001, 002, ...)
    Admin->>App: Download QR ZIP (/generate-qr-zip)
    Admin-->>Attendee: Distribute QR codes via CertFlow Email

    Note over Volunteer, App: Phase 2: Volunteer Authentication
    Volunteer->>App: Open /scan or launch Android APK
    Volunteer->>App: Authenticate with Event Code + Access Code
    App->>DB: Verify credentials & register scanner device
    App-->>Volunteer: Issue Event-Scoped Bearer Token

    Note over Volunteer, Attendee: Phase 3: Live Event Check-In
    Attendee->>Volunteer: Present badge QR code
    Volunteer->>App: Scan QR code (POST /api/events/{id}/scan)
    App->>DB: Atomic UPDATE participants SET attended=1 WHERE attended=0
    alt First scan (Success)
        App->>DB: Insert log with status 'ok'
        App-->>Volunteer: 200 OK (Green Screen + Success Chime)
    else Already scanned (Duplicate)
        App->>DB: Insert audit log with status 'duplicate'
        App-->>Volunteer: 200 OK (Yellow Screen + Duplicate Tone)
    else Unregistered Badge
        App-->>Volunteer: 404 Not Found (Red Screen + Error Buzz)
    end

    Note over Admin, App: Phase 4: Live Monitoring & Export
    Admin->>App: Monitor /dashboard (auto-polls every 3.5s)
    Admin->>App: Download final attendance report (/export-roster)
    App-->>Admin: Sanitized CSV with timestamps & scanner metadata
```

---

## 3. Core Feature Breakdown

### 3.1 Multi-Event Management & Data Isolation
- Multiple events can exist simultaneously in the database without collision.
- Each event features an independent **Event Code** (e.g., `AAZHI26`), **Access Code** (e.g., `SCAN123`), **Custom ID Prefix** (e.g., `AAZHI-`), and **Configurable ID Width** (zero-padding).
- Scanner bearer tokens are strictly validated against the target `event_id`, preventing tokens issued for Event A from recording scans into Event B.

### 3.2 Intelligent Roster Ingestion & Column Mapping
- **Dual Format Support**: Ingests `.csv` (with UTF-8 / UTF-8-BOM support) and `.xlsx` workbooks (via `openpyxl`).
- **Heuristic Auto-Detection**: Inspects header strings using regex/fuzzy matching to automatically suggest mappings for `Name`, `Email`, and `Department`.
- **Custom Extra Headers**: Allows selecting extra columns (e.g., *T-Shirt Size*, *Food Preference*, *Team Name*) that are serialized into `extra_json` and restored seamlessly during final export.
- **Destructive Roster Replacement Protection**: Uploading a replacement sheet to an event with existing attendance triggers an explicit confirmation requirement to prevent accidental data loss.

### 3.3 Dynamic High-Resolution QR Generation
- Encodes clean, uncluttered registration strings (`001`, `002`, `AAZHI-001`) into QR matrices.
- Utilizes **Error Correction Level H** (30% recovery capability), ensuring badges scan reliably even under low lighting, scratched screens, or creased paper.
- Packages all generated QR PNGs into an in-memory `.zip` archive structured as `{reg_id}.png`, enabling automated 1:1 matching in CertFlow.

### 3.4 Multi-Device Web PWA & Native Android Scanner
- **Zero Installation Needed**: Volunteers can simply open `http://<server-ip>:5001/scan` on Chrome, Safari, Firefox, or Edge.
- **Embedded Audio Synthesizer**: Uses Web Audio API (`static/audio.js`) to produce real-time audio frequencies (Success chime, Duplicate warning tone, Error buzz) without requiring external audio asset downloads.
- **Instant Visual Feedback**: Fullscreen status banners with color coding:
  - 🟢 **Green (Valid Check-in)**: Displays attendee name, department, registration ID, and timestamp.
  - 🟡 **Yellow (Duplicate Scan)**: Displays initial check-in time and scanning device name.
  - 🔴 **Red (Not Found / Error)**: Highlights invalid or unregistered badge IDs.
- **Automatic Camera Resumption**: Viewfinder automatically resets after 1.5 seconds to maintain high line throughput.
- **Manual ID Entry & Camera Switcher**: Allows toggling between environment (back) and user (front) cameras, torch/flashlight control, or manual keyboard registration entry.

### 3.5 Offline Queueing & Idempotent Batch Sync
- **Local Storage / IndexedDB Storage**: When a scanner loses Wi-Fi or cellular connectivity, scans are queued locally on the client device.
- **Client Timestamp Preservation**: Scans retain their true local scan timestamp so offline queues reflect actual arrival times.
- **Timestamp Sanity Filtering**: Timestamps are validated against server clocks; any timestamp > 5 minutes in the future or > 30 days in the past is safely clamped to server UTC.
- **Idempotent Sync Mechanism**: Every scan generates a unique client UUID (`scan_id`). If network timeouts trigger repeated sync retries, the server handles duplicates gracefully without double-logging or altering records.

### 3.6 Real-Time Live Operations Dashboard
- **Live Summary Metrics**: Real-time cards displaying Total Registered, Attended Count, Pending Count, and Attendance Percentage.
- **Scanner Fleet Telemetry**: Tracks active devices, IP addresses, last seen timestamps, online/offline status (heartbeat window of 45 seconds), and pending offline queue depths.
- **Searchable Roster Grid**: Instant client-side search across Registration ID, Name, Email, and Department.
- **One-Click Check-In Undo**: Admin operators can reverse an accidental check-in directly from the dashboard (`/undo`), returning the participant to unattended state and logging an audit event.
- **Late Registration Desk**: Add walk-in attendees on the fly with automatic sequential ID assignment and instant QR code preview.
- **Remote Scanner Revocation**: Instantly invalidate compromised or lost volunteer devices from the dashboard.

---

## 4. Security Architecture & Concurrency Model

AttendQR underwent a complete security audit and architectural hardening to eliminate common vulnerabilities found in event systems.

### 4.1 Threat Model & Security Controls

| Threat / Risk | Mitigating Control in AttendQR |
|---|---|
| **Anonymous Admin Access** | All administrative endpoints enforce `require_admin` session validation with cryptographically secure cookies (`HttpOnly`, `SameSite=Lax`). |
| **Scanner Privilege Escalation** | Scanners receive an unprivileged bearer token tied exclusively to their registered `event_id`. They cannot view credentials or perform administrative operations. |
| **Credential Guessing / Brute-Force** | Scanner authentication (`/api/auth/scanner`) employs an in-memory sliding-window rate limiter (max 10 failed attempts per 5-minute window per IP). |
| **CSRF on Admin Actions** | Cookie-authenticated state-changing POST requests require a matching CSRF token (`csrf_token` form field or `X-CSRF-Token` header). Bearer-token API calls are inherently CSRF-immune. |
| **CSV Formula Injection** | All exported CSV cells beginning with `= `, `+`, `-`, `@`, `\t`, or `\r` are automatically prefixed with an apostrophe (`'`) to prevent remote execution in Excel/Google Sheets. |
| **Clickjacking & MIME Sniffing** | Global HTTP response headers enforce `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: same-origin`. |
| **Session Fixation / Tampering** | `SECRET_KEY` is enforced in production. When unset, a secure ephemeral key is generated per process with prominent console warnings. |

### 4.2 Concurrency & Race-Condition Elimination

In high-traffic events, multiple gate volunteers may scan the same badge within milliseconds of each other. AttendQR guarantees **absolute atomicity** using database-level conditional updates:

```sql
UPDATE participants
SET attended = 1,
    scanned_at = ?,
    scanned_by_device_id = ?,
    scanned_by_device_name = ?,
    scan_id = ?
WHERE event_id = ? AND reg_id = ? AND attended = 0;
```

#### Execution Logic:
1. **Row Count Check**: The database driver inspects `cursor.rowcount`.
2. **First Scan Wins**: Exactly **one** transaction successfully modifies the row (`rowcount == 1`). It is assigned `status = 'ok'` and logged.
3. **Subsequent Scans**: Concurrent or delayed scans for the same badge match `attended = 0` to false (`rowcount == 0`). The server fetches the original check-in details, logs an audit record with `status = 'duplicate'`, and returns the existing scan timestamp without overwriting data.

---

## 5. Database Architecture & Schema

### 5.1 Dual-Engine Abstraction Layer (`db.py`)

AttendQR implements a unified abstraction layer (`DBWrapper`) that enables seamless operation across both **PostgreSQL** (for cloud deployments on Heroku, Render, Railway, AWS) and **SQLite** (for local laptops, offline servers, or embedded setups).

- **Parameter Normalization**: Queries are authored using standard `?` syntax and dynamically rewritten to `%s` when executing against PostgreSQL.
- **Connection Pooling**: Uses `psycopg2.pool.ThreadedConnectionPool` (1 to 20 connections) for PostgreSQL with automatic rollback on connection release.
- **SQLite Performance Tuning**: Configured with Write-Ahead Logging (`PRAGMA journal_mode=WAL`), `synchronous=NORMAL`, and a 10-second busy timeout (`busy_timeout=10000`) to prevent file lock contention across multiple threads.

---

### 5.2 Table Schemas

```sql
-- 1. Events Table: Container for independent events
CREATE TABLE events (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(64) UNIQUE NOT NULL,      -- Public identifier (e.g. "AAZHI26")
    access_code VARCHAR(64) NOT NULL,      -- Secret shared with scanner devices
    admin_password_hash VARCHAR(255),      -- Reserved for multi-admin extensions
    id_prefix VARCHAR(32) DEFAULT '',      -- Optional prefix (e.g. "AAZHI-")
    id_width INTEGER DEFAULT 3,            -- Zero-padding width (e.g. 3 -> "001")
    extra_headers_json TEXT DEFAULT '[]',  -- Serialized list of extra column names
    status VARCHAR(32) DEFAULT 'active',   -- 'active' | 'archived'
    created_at VARCHAR(32) NOT NULL
);

-- 2. Participants Table: Event roster & check-in state
CREATE TABLE participants (
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    reg_id VARCHAR(64) NOT NULL,           -- Sequential ID encoded in QR
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    department VARCHAR(255) NOT NULL,
    extra_json TEXT,                       -- JSON dictionary of custom extra columns
    attended INTEGER NOT NULL DEFAULT 0,   -- 0 = Unattended, 1 = Attended
    scanned_at VARCHAR(32),                -- ISO8601 UTC timestamp of check-in
    scanned_by_device_id VARCHAR(64),      -- UUID / identifier of scanning device
    scanned_by_device_name VARCHAR(64),    -- Human-friendly device name
    scan_id VARCHAR(64),                   -- Idempotency key from client scan
    created_at VARCHAR(32) NOT NULL,
    CONSTRAINT uq_event_reg UNIQUE (event_id, reg_id)
);

-- 3. Scanners Table: Registered volunteer scanning devices
CREATE TABLE scanners (
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64) NOT NULL,        -- Hardware/client persistent UUID
    device_name VARCHAR(64) NOT NULL,      -- e.g. "Gate 1 - Alpha"
    token VARCHAR(128) UNIQUE NOT NULL,    -- Secure Bearer token
    last_seen VARCHAR(32) NOT NULL,        -- ISO8601 UTC timestamp of last activity
    status VARCHAR(32) DEFAULT 'online',   -- 'online' | 'revoked'
    pending_sync_count INTEGER DEFAULT 0,  -- Offline queue depth reported by device
    ip_address VARCHAR(64),
    CONSTRAINT uq_event_device UNIQUE (event_id, device_id)
);

-- 4. Attendance Logs Table: Append-only audit trail of every scan event
CREATE TABLE attendance_logs (
    id VARCHAR(64) PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    participant_id VARCHAR(64),
    reg_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64) NOT NULL,
    device_name VARCHAR(64) NOT NULL,
    scan_id VARCHAR(64),
    status VARCHAR(32) NOT NULL,          -- 'ok' | 'duplicate' | 'not_found' | 'undo'
    scanned_at VARCHAR(32) NOT NULL,       -- Client-reported scan timestamp
    synced_at VARCHAR(32) NOT NULL         -- Server receipt timestamp
);

-- 5. Settings Table: Key-value configuration store
CREATE TABLE settings (
    key VARCHAR(128) PRIMARY KEY,
    value TEXT
);
```

#### Database Indexes
- `idx_part_event_reg`: Index on `participants(event_id, reg_id)` for sub-millisecond badge lookups.
- `idx_scanners_event`: Index on `scanners(event_id)` for fast telemetry aggregation.
- `idx_logs_event_scan`: Index on `attendance_logs(event_id, scan_id)` for instant idempotency checks.

---

## 6. Complete REST API Reference

### Authentication Scope Legend
- **`public`**: Accessible by anyone without credentials.
- **`admin`**: Requires active Admin session cookie (obtained via `/login`).
- **`scanner`**: Requires valid `Authorization: Bearer <token>` or `X-Scanner-Token` header scoped to the requested event. (Admin session is also accepted).

---

### 6.1 Authentication Endpoints

#### 1. Authenticate Scanner Device
```http
POST /api/auth/scanner
```
- **Auth**: `public` (Protected by sliding-window rate limiting)
- **Request Body**:
  ```json
  {
    "event_code": "AAZHI26",
    "access_code": "SCAN123",
    "device_id": "c7a84e22-83b4-4b5c-9c60-1e5f88421c9a",
    "device_name": "Gate 1 - North"
  }
  ```
- **Success Response (200 OK)**:
  ```json
  {
    "status": "ok",
    "token": "d76c382f1bc24ef382b6832...",
    "event": {
      "id": "e8913b72-...",
      "name": "Aazhi CTF 2026",
      "code": "AAZHI26",
      "id_prefix": "AAZHI-",
      "id_width": 3
    },
    "device": {
      "device_id": "c7a84e22-...",
      "device_name": "Gate 1 - North"
    }
  }
  ```

#### 2. Admin Login
```http
POST /login
```
- **Auth**: `public`
- **Request Form Fields**: `password=<ADMIN_PASSWORD>`
- **Response**: `302 Redirect` to `/` or requested URL, sets session cookie.

#### 3. Admin Logout
```http
POST /logout
```
- **Auth**: `admin`
- **Response**: `302 Redirect` to `/login`, clears session cookie.

---

### 6.2 Event Management Endpoints

#### 1. List Events
```http
GET /api/events
```
- **Auth**: `admin`
- **Response (200 OK)**:
  ```json
  {
    "status": "ok",
    "events": [
      {
        "id": "default-event",
        "name": "Aazhi CTF 2026",
        "code": "AAZHI26",
        "access_code": "SCAN123",
        "id_prefix": "",
        "id_width": 3,
        "total_participants": 250,
        "attended_count": 185,
        "active_scanners": 4,
        "created_at": "2026-08-20T05:00:00Z"
      }
    ],
    "active_event_id": "default-event"
  }
  ```

#### 2. Create Event
```http
POST /api/events
```
- **Auth**: `admin`
- **Request Body**:
  ```json
  {
    "name": "Cyber Olympiad 2026",
    "code": "CYV26",
    "access_code": "CYBER999",
    "id_prefix": "CYV-",
    "id_width": 4
  }
  ```
- **Response (201 Created)**: Returns created event JSON.

#### 3. Select Active Event
```http
POST /api/events/<event_id>/select
```
- **Auth**: `admin`
- **Response (200 OK)**: Updates session active event.

---

### 6.3 Scan & Synchronization Endpoints

#### 1. Record Single Scan
```http
POST /api/events/<event_id>/scan
```
- **Auth**: `scanner` (Token must match `<event_id>`)
- **Request Body**:
  ```json
  {
    "reg_id": "001",
    "device_id": "c7a84e22-...",
    "device_name": "Gate 1 - North",
    "scanned_at": "2026-08-20T06:30:00Z",
    "scan_id": "optional-client-uuid"
  }
  ```
- **Response Status Shapes**:
  - **First Scan Success (200 OK)**:
    ```json
    {
      "status": "ok",
      "reg_id": "001",
      "name": "Jane Doe",
      "department": "Computer Science",
      "email": "jane@example.com",
      "extra": { "T-Shirt": "M" },
      "scanned_at": "2026-08-20T06:30:00Z",
      "scanner": "Gate 1 - North"
    }
    ```
  - **Already Attended (200 OK)**:
    ```json
    {
      "status": "duplicate",
      "reg_id": "001",
      "name": "Jane Doe",
      "department": "Computer Science",
      "scanned_at": "2026-08-20T06:30:00Z",
      "scanner": "Gate 1 - North"
    }
    ```
  - **Replayed Network Retry (200 OK)**:
    ```json
    {
      "status": "ok",
      "reg_id": "001",
      "replayed": true
    }
    ```
  - **Unregistered Badge (404 Not Found)**:
    ```json
    {
      "status": "not_found",
      "reg_id": "999",
      "message": "Badge 999 not registered in roster"
    }
    ```

#### 2. Batch Sync Offline Scans
```http
POST /api/events/<event_id>/sync
```
- **Auth**: `scanner`
- **Request Body**:
  ```json
  {
    "device_id": "c7a84e22-...",
    "device_name": "Gate 1 - North",
    "scans": [
      { "reg_id": "002", "scanned_at": "2026-08-20T06:31:10Z", "scan_id": "uuid-1" },
      { "reg_id": "003", "scanned_at": "2026-08-20T06:31:15Z", "scan_id": "uuid-2" }
    ]
  }
  ```
- **Response (200 OK)**:
  ```json
  {
    "status": "ok",
    "synced_count": 2,
    "results": [
      { "scan_id": "uuid-1", "reg_id": "002", "status": "ok", "name": "Alice Smith" },
      { "scan_id": "uuid-2", "reg_id": "003", "status": "duplicate", "name": "Bob Lee" }
    ]
  }
  ```

#### 3. Scanner Heartbeat & Telemetry
```http
POST /api/events/<event_id>/heartbeat
```
- **Auth**: `scanner`
- **Request Body**:
  ```json
  {
    "device_id": "c7a84e22-...",
    "device_name": "Gate 1 - North",
    "pending_sync_count": 0
  }
  ```
- **Response (200 OK)**: `{"status": "ok"}`

---

### 6.4 Analytics & Administration Endpoints

#### 1. Live Stats & Telemetry
```http
GET /api/events/<event_id>/stats
```
- **Auth**: `scanner` or `admin`
- **Response (200 OK)**: Returns total registered, attended count, attendance percentage, active scanner list with heartbeat status, and recent scan activity stream.

#### 2. Full Event Roster
```http
GET /api/events/<event_id>/roster
```
- **Auth**: `scanner` or `admin`
- **Response (200 OK)**: Returns complete array of participant records.

#### 3. Add Participant (Late Walk-in)
```http
POST /api/events/<event_id>/add-participant
```
- **Auth**: `admin`
- **Request Body / Form**:
  ```json
  {
    "name": "Charlie Brown",
    "email": "charlie@example.com",
    "department": "Electronics",
    "reg_id": "" 
  }
  ```
  *(If `reg_id` is empty, the system calculates the next sequential ID automatically).*

#### 4. Undo Check-In
```http
POST /api/events/<event_id>/participants/<reg_id>/undo
```
- **Auth**: `admin`
- **Response (200 OK)**: Clears `attended = 0`, `scanned_at = NULL`, logs undo event.

#### 5. Revoke Scanner Device
```http
POST /api/events/<event_id>/scanners/<device_id>/revoke
```
- **Auth**: `admin`
- **Response (200 OK)**: Sets scanner status to `revoked` and invalidates token.

---

### 6.5 Export & Media Endpoints

#### 1. Download Single Participant QR Code
```http
GET /api/events/<event_id>/qr/<reg_id>
```
- **Auth**: `admin`
- **Response**: `image/png` payload of participant QR code.

#### 2. Download Batch QR ZIP
```http
POST /api/events/<event_id>/generate-qr-zip
```
- **Auth**: `admin`
- **Response**: `application/zip` stream containing `{reg_id}.png` for all participants.

#### 3. Export Attendance CSV
```http
GET /api/events/<event_id>/export
```
- **Auth**: `admin`
- **Response**: Formula-sanitized CSV attachment `<event_code>_attendance_<timestamp>.csv`.

#### 4. Download Android APK
```http
GET /download-apk
```
- **Auth**: `public`
- **Response**: Serves `static/AttendQR.apk`.

#### 5. System Health Check
```http
GET /healthz
```
- **Auth**: `public`
- **Response (200 OK)**:
  ```json
  {
    "status": "healthy",
    "database": "postgresql",
    "time": "2026-08-30T09:25:00Z"
  }
  ```

---

## 7. Android Mobile App & Native Build Pipeline

AttendQR includes a dedicated native Android wrapper located in the `android-app/` directory.

### 7.1 Architecture of the Android Client
- **Native Container**: Implemented in pure Java (`MainActivity.java`, `CustomWebViewClient.java`, `CustomWebChromeClient.java`, `WebAppInterface.java`).
- **JavaScript Bridge (`AndroidBridge`)**: Exposes native Android functions to the web runtime, including Scoped Storage downloads via `MediaStore.Downloads`, native camera permission prompts, and haptic feedback.
- **Embedded Offline Assets**: Bundles complete client-side scanning and spreadsheet libraries inside `android-app/src/main/assets/`:
  - `html5-qrcode.min.js`: Camera capture and barcode parsing.
  - `xlsx.full.min.js`: Client-side spreadsheet parsing.
  - `jszip.min.js`: Client-side ZIP unpacking/packing.
  - `qrcode.min.js`: Client-side QR generation.
  - `index.html`: Complete standalone offline scanner interface.

---

### 7.2 Native Toolchain Build Script (`build_apk.sh`)

Rather than requiring large Gradle installations, AttendQR features a lightweight bash build script (`build_apk.sh`) that builds and signs the production APK using direct Android SDK command-line utilities:

```mermaid
graph LR
    A["Res Directory"] -->|"aapt2 compile"| B["res.zip"]
    B -->|"aapt2 link + Assets + Manifest"| C["app-unsigned.apk"]
    D["Java Sources"] -->|"javac (Java 8)"| E[".class Files"]
    E -->|"d8"| F["classes.dex"]
    F -->|"aapt add"| C
    C -->|"zipalign -p 4"| G["app-aligned.apk"]
    G -->|"apksigner (v1+v2+v3)"| H["AttendQR.apk"]
    H -->|"Copy"| I["static/AttendQR.apk"]
```

#### Build Steps in `build_apk.sh`:
1. **AAPT2 Compile**: Compiles XML drawables, layouts, and resource values into `res.zip`.
2. **AAPT2 Link**: Links resources with `android.jar`, bundles assets (`file:///android_asset`), generates `R.java`, and creates `app-unsigned.apk`.
3. **Javac Compilation**: Compiles Java source files targeting Java 8 bytecode.
4. **D8 Dexing**: Converts compiled `.class` files into optimized Android Dalvik Executable (`classes.dex`).
5. **AAPT Packaging**: Adds `classes.dex` into `app-unsigned.apk`.
6. **Zipalign**: Performs 4-byte boundary alignment for memory-mapped efficiency.
7. **Apksigner**: Signs the aligned APK with debug/release keystores supporting V1, V2, and V3 signing schemes.
8. **Web Distribution**: Copies the final signed binary to `static/AttendQR.apk` for instant volunteer download via `/download-apk`.

---

## 8. Environment Variables & Configuration

| Environment Variable | Default Value | Production Requirement | Description |
|---|---|---|---|
| `ADMIN_PASSWORD` | *Random 9-char token printed to console* | **Mandatory** | Password required to access the admin console, upload rosters, and view participant PII. |
| `SECRET_KEY` | *Random 48-char token* | **Mandatory** | Cryptographic secret used to sign Flask session cookies. Must remain static across restarts/workers. |
| `DATABASE_URL` | *(Unset → SQLite `attendqr.db`)* | Recommended for Cloud | PostgreSQL connection string (`postgresql://user:pass@host:5432/dbname`). Automatically normalizes legacy `postgres://` prefixes. |
| `PORT` | `5001` | Optional | Port on which Gunicorn / Flask listens. |
| `FLASK_DEBUG` | `0` | **Must be `0`** | Enables Werkzeug interactive debugger. **Never enable in production** as it permits remote code execution. |
| `MAX_UPLOAD_MB` | `10` | Optional | Maximum allowed upload size (in MB) for `.xlsx` and `.csv` files. |
| `SESSION_COOKIE_SECURE`| `0` | Recommended with HTTPS | Set to `1` when terminating SSL/TLS to enforce `Secure` cookie transmission. |

---

## 9. Deployment & Operations Guide

### 9.1 Local Development Setup

```bash
# 1. Clone the repository and navigate to root
cd AttendQR

# 2. Initialize Python virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install core dependencies
pip install -r requirements.txt

# 4. Configure local environment
export ADMIN_PASSWORD="admin-secure-password"
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

# 5. Start the local server
python app.py
```
*The local development server binds to `http://0.0.0.0:5001`.*

---

### 9.2 Running Automated Test Suites

The project includes an extensive test suite verifying concurrency, authentication, CSRF, and data integrity:

```bash
# Run all automated tests
pytest tests/ -v
```

#### Test Suite Structure:
- `tests/test_cloud_multidevice.py`: Validates multi-event data isolation, scanner token issuance, concurrent multi-threaded scans, batch sync idempotency, and heartbeat telemetry.
- `tests/test_security_and_logic.py`: Validates admin authorization barriers, CSRF token validation, rate-limiting guards, access code leaks prevention, and formula injection filters.

---

### 9.3 Production Cloud Deployment (Gunicorn + PostgreSQL)

For production deployments on Linux servers, Docker containers, or PaaS providers (Render, Heroku, Railway, DigitalOcean):

```bash
# Production launch command using Gunicorn
gunicorn \
  --bind 0.0.0.0:5001 \
  --workers 4 \
  --threads 2 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  app:app
```

#### Production Checklist:
1. Set a strong, static `SECRET_KEY` in environment variables.
2. Set `ADMIN_PASSWORD` in environment variables.
3. Configure `DATABASE_URL` pointing to a managed PostgreSQL instance.
4. Put the application behind a reverse proxy (Nginx, Caddy, Cloudflare) with SSL/TLS enabled.
5. Set `SESSION_COOKIE_SECURE=1` in the environment.

---

## 10. Repository File Map

```
AttendQR/
├── app.py                      # Core Flask application, routing, auth logic, and API endpoints
├── db.py                       # Database abstraction layer (PostgreSQL pool + SQLite WAL)
├── build_apk.sh                # Direct SDK bash build pipeline for the Android APK
├── requirements.txt            # Python dependencies (Flask, psycopg2-binary, openpyxl, etc.)
├── attendqr.db                 # Default local SQLite database file
├── README.md                   # Quickstart guide and overview
├── SECURITY_REVIEW.md          # Full security audit report (20 resolved vulnerabilities)
├── PROJECT_INFO.md             # Complete in-depth system architecture & technical documentation
├── sample_event_registration.xlsx # Example spreadsheet for testing roster ingestion
│
├── android-app/                # Native Android application source
│   └── src/main/
│       ├── AndroidManifest.xml # Android app manifest & permissions (Camera, Internet)
│       ├── java/com/certflow/attendqr/
│       │   ├── MainActivity.java           # Android activity lifecycle & download manager
│       │   ├── WebAppInterface.java        # JavaScript to Android bridge methods
│       │   ├── CustomWebViewClient.java   # WebView navigation handler
│       │   └── CustomWebChromeClient.java # Permission & progress handler
│       ├── res/                            # Native XML layouts, drawables, strings
│       └── assets/                         # Offline web bundle (index.html, html5-qrcode, etc.)
│
├── templates/                  # Jinja2 server-rendered web templates
│   ├── login.html              # Admin console login screen
│   ├── upload.html             # Drag-and-drop spreadsheet upload screen
│   ├── mapping.html            # Column mapping & confirmation interface
│   ├── dashboard.html          # Live real-time attendance ops dashboard
│   └── scan.html               # Mobile PWA scanner interface
│
├── static/                     # Static web assets
│   ├── style.css               # Modern dark-mode UI styling & responsive CSS tokens
│   ├── audio.js                # Web Audio API real-time sound synthesizer
│   ├── sw.js                   # Service Worker for PWA offline caching
│   ├── manifest.json           # PWA installation manifest
│   ├── icon-192.png            # Application launcher icon (192x192)
│   ├── icon-512.png            # Application launcher icon (512x512)
│   └── AttendQR.apk            # Pre-compiled distributable Android APK
│
├── tests/                      # Automated test suites
│   ├── test_cloud_multidevice.py   # Multi-device, concurrency & sync tests
│   └── test_security_and_logic.py  # Security regression & authorization tests
│
└── uploads/                    # Ephemeral staging directory for uploaded rosters
```

---

*AttendQR Documentation — Engineered for speed, concurrency, and reliability.*
