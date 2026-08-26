# AttendQR — Security, Logic & Reliability Review

**Date:** 2026-08-23
**Baseline commit:** `8e1d671` (`fix(db): ensure uncommitted transactions are rolled back…`)
**Scope:** `app.py`, `db.py`, `templates/`, `static/`, `tests/`, Android WebView wrapper (read-only review)

---

## 1. Executive summary

| Severity | Found | Fixed |
|----------|-------|-------|
| Critical | 2 | 2 |
| High | 5 | 5 |
| Medium | 9 | 9 |
| Low | 4 | 4 |
| **Total** | **20** | **20** |

Five features were added to close gaps that had no remedy in the product, and
the README was rewritten (it documented a schema that no longer existed).

Tests: **1 → 48 passing** (47 new).

**The headline finding:** the entire admin surface was unauthenticated. Anyone
who could reach the port could read every attendee's name and email, download
the roster CSV, wipe a roster mid-event, or record attendance. The `events`
table already carried an unused `admin_password_hash` column, so admin auth was
an intended-but-unbuilt feature; it is now built.

**The subtlest finding:** the multi-device "no double counting" guarantee was
only half-implemented. The conditional `UPDATE … WHERE attended = 0` was
correct, but its result was discarded, so two devices scanning at once could
both be told "checked in" and both write an official audit row.

**Not a finding:** no XSS. Both templates escape every interpolated value
(`escHtml` / `esc`), which is worth stating explicitly for an app that renders
uploaded spreadsheet data into innerHTML.

---

## 2. Critical

### C1 — Werkzeug debugger enabled in production
`app.run(debug=True)` was hardcoded, and the app binds `0.0.0.0`. The debug
console is arbitrary code execution for anyone who can reach the port.

**Fix:** `debug=DEBUG_MODE`, off unless `FLASK_DEBUG=1`.
**Test:** `TestOperational::test_debug_is_off_by_default`

### C2 — Whole admin surface open to anonymous callers
`/`, `/dashboard`, `/mapping`, `/export-roster`, `/api/events`, `/api/roster`,
`/add-participant`, `/generate-qr-zip`, `/qr/<id>` required no credential.
`/mapping` is destructive — it runs `DELETE FROM participants`.

**Impact:** full disclosure of attendee PII (names, emails, departments) and
unauthenticated destruction of an in-progress event's attendance.
**Fix:** session-based admin auth — `/login`, `/logout`, `require_admin` — applied
to every roster-reading or roster-writing route. Password from `ADMIN_PASSWORD`;
when unset a random one is generated and printed at startup, so a fresh install
is never silently open.
**Tests:** `TestAdminSurfaceIsProtected` (6 tests)

---

## 3. High

### H1 — `/api/events` published every event's `access_code` in plaintext
That code is the scanner credential. Any anonymous caller could read it,
authenticate a device and record attendance.
**Fix:** endpoint is admin-only.
**Test:** `test_event_access_codes_are_not_exposed_anonymously`

### H2 — `/scan` pre-filled the access code into the HTML
`value="{{ active_event['access_code'] }}"` handed the shared secret to every
visitor of a deliberately public page.
**Fix:** removed; the view now selects only `id, name, code`.
**Test:** `test_scan_page_does_not_leak_the_access_code`

### H3 — A scanner token for one event worked on every other event
`get_authenticated_scanner()` matched on token alone and never compared against
the `event_id` in the URL, so a volunteer's phone for Event A could write
attendance into Event B.
**Fix:** `scanner_for_event()` requires `scanner.event_id == event_id`; a
mismatch is now `403`.
**Test:** `test_token_for_one_event_cannot_scan_another`

### H4 — `SECRET_KEY` defaulted to a hardcoded string
Admin session cookies were forgeable by anyone who had read the source.
**Fix:** generated per process, with a startup warning when unset outside debug.
Documented as required in production.
**Test:** `TestProductionSecuritySettings` equivalents in `TestOperational`

### H5 — Conditional UPDATE result discarded → double-counted check-ins
`if part["attended"] == 0` was read outside any lock, then the conditional
UPDATE ran. Two devices could both pass the read; the loser's UPDATE matched
zero rows, yet the code logged `status="ok"` and returned `"ok"`. The `attended`
flag stayed correct, but **the responses and the audit trail double-counted** —
two volunteers see a green tick, and `attendance_logs` records two official
check-ins for one badge.

**Fix:** both `/scan` and `/sync` now branch on `cursor.rowcount`. The duplicate
path re-reads the winning row, so the losing device reports the *first*
scanner's name and time rather than its own.
**Tests:** `TestConcurrencyInvariants` — see §8 for how this one was validated.

---

## 4. Medium

| ID | Issue | Fix |
|----|-------|-----|
| M1 | Attendance recordable by anyone on the network. The `require_scanner_auth` decorator meant to prevent this was **dead code** (defined, never applied) and had a bypass anyway: it passed any request whose session held `active_event_id`, which every visit to `/` set. | Replaced with `require_scanner_or_admin()`, actually applied to scan/sync/stats/roster. |
| M2 | No CSRF protection anywhere. Cookie-authenticated form posts (create event, add participant, replace roster) were forgeable cross-site. | Session CSRF token enforced in `before_request` for cookie-authenticated non-JSON posts; hidden inputs in all 6 forms; `X-CSRF-Token` from dashboard JS. Bearer-token API calls are exempt by design — a foreign site cannot read the token and browsers do not auto-attach it. |
| M3 | Access codes brute-forceable: no throttling, `!=` comparison (timing-leaky), and error messages distinguished "no such event" from "wrong code". | Sliding-window limiter (10 failures / 5 min), `secrets.compare_digest`, and identical 401s for both failure modes. |
| M4 | Replacing a roster silently destroyed recorded attendance and left `attendance_logs.participant_id` dangling. | Explicit confirmation checkbox required once any check-in exists; audit rows detached rather than orphaned. |
| M5 | `add-participant` returned **500** on any form post with a blank field — `request.form.get(k) or request.json.get(k)` evaluated `request.json` on a form-encoded request, which raises. | Reads JSON only when the request actually is JSON. |
| M6 | Concurrent late-adds crashed: both computed the same next reg ID and the `UNIQUE(event_id, reg_id)` violation surfaced as an unhandled 500. | Caught → `409` with a retry message. |
| M7 | No upload size limit; `file.read()` pulled the whole upload into memory. | `MAX_CONTENT_LENGTH` capped at `MAX_UPLOAD_MB` (default 10). |
| M8 | The service worker **cached nothing** — it called `caches.match()` as a fallback but never `cache.put`/`addAll`. The app installs as a PWA advertising offline scanning, yet a volunteer who lost Wi-Fi and reloaded got the browser error page, and `html5-qrcode` (CDN-loaded) could not initialize the camera offline. | Rewrote `sw.js`: precaches the scanner shell, CSS, audio, icons and the QR library (individually, so one unreachable CDN does not void the batch); cache-first assets, network-first navigations with shell fallback. **`/api/` is never cached** — a stale roster or replayed scan result is worse than an honest error, and the page already queues failed scans locally. |
| M9 | SQLite opened with no concurrency settings. With several phones scanning while the dashboard polls every 3.5 s, readers and writers contend and volunteers hit "database is locked" mid-event. | Single `_connect_sqlite()` helper opening in **WAL** with a 10 s busy timeout and `synchronous=NORMAL`. |

---

## 5. Low

| ID | Issue | Fix |
|----|-------|-----|
| L1 | CSV formula injection — a participant named `=cmd\|calc` executed on open in Excel/Sheets. | Cells starting with `= + - @` (or tab/CR) are apostrophe-prefixed. |
| L2 | Client-supplied `scanned_at` unvalidated; a device could record a scan dated year 2999. | Parsed and rejected if unparseable, >5 min in the future, or >30 days old — while still preserving genuine offline timestamps. |
| L3 | `int(payload["pending_count"])` crashed on non-numeric input; `id_width` accepted absurd values (`zfill(1000000)`). | Both clamped via `safe_int`. |
| L4 | Abandoned upload temp files accumulated forever; columns were matched by header *name*, so duplicate header names silently mapped to the first match. | Age-based pruning (6 h) and header de-duplication. |

**Additional hardening:** predictable `"SCAN123"` default access code → random;
event codes validated (`[A-Z0-9_-]{2,32}`) and access codes min length 4;
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` headers; session
cookie `HttpOnly` + `SameSite=Lax` with `Secure` opt-in; sync batches capped at
5000; `login?next=` restricted to local paths.

---

## 6. Features added

1. **Admin console authentication** — `/login`, `/logout`, sign-out in the nav.
2. **Undo a mistaken check-in** — `POST /api/events/<id>/participants/<reg_id>/undo`
   plus an ↩️ button on every present row. Previously a wrong scan was
   *permanent*: the only remedy was replacing the whole roster, which erased
   everyone else's attendance. The reversal is recorded in `attendance_logs` as
   `status='undo'` rather than deleted, so the audit trail stays honest.
3. **Revoke a device** — `POST /api/events/<id>/scanners/<device_id>/revoke`
   plus a `revoke` link on each scanner chip, for a lost or handed-back phone.
   The token is replaced with an unguessable value so it can never be reused;
   the phone can re-authenticate with the access code.
4. **Scan idempotency** — replaying a `scan_id` on `/scan` (network retry)
   previously appended a duplicate audit row every time. Now returns the prior
   result with `"replayed": true` and writes nothing.
5. **`GET /healthz`** — liveness probe reporting the active DB backend, for
   gunicorn/Render deployments.

---

## 7. Access model after the change

Two independent identities:

- **Admin** — session cookie from `/login` with `ADMIN_PASSWORD`. Owns the
  upload/mapping flow, dashboard, event management, QR generation, CSV export,
  participant additions, undo and revoke.
- **Scanner** — bearer token from `/api/auth/scanner` using the **event code +
  access code**, scoped to a single event. May record attendance and read the
  roster for that event only.

`/scan` stays public: it is an empty scanner shell with no roster data and no
credentials, so volunteers can open it on their own phones. Every API call it
makes requires a token. Volunteers never need the admin password.

---

## 8. Testing

```
pytest tests/ -q   →   48 passed
```

- `tests/test_security_and_logic.py` — **47 new tests**, roughly one per defect,
  so a regression names the problem it brings back.
- `tests/test_cloud_multidevice.py` — the original suite, still passing. One
  change was required: an admin login, because event management now needs auth.

### Two testing problems worth recording

**A self-inflicted bug, caught by the suite.** The first rate limiter counted
*successful* logins too, so the test fixture locked itself out after 10 tests
(14 passed, 26 errored). That was a real defect — it would have locked out the
operator during normal use, not just tests. Fixed so only failures count and a
success clears the bucket.

**A vacuous test, caught by mutation.** The first test for H5 spawned 8
concurrent scans and asserted exactly one `ok`. It **passed with the bug
reintroduced** — SQLite serialises writes, so the read/update interleaving
essentially never happens locally. It proved nothing.

It was replaced with a deterministic reproduction
(`test_stale_read_cannot_produce_a_second_official_check_in`) that forces the
losing device's stale `attended=0` view via monkeypatch. Verified by reverting
the fix: the test fails with `- duplicate / + ok`, and passes once restored. The
8-thread test was kept as a smoke check, with a docstring stating plainly what
it does and does not prove.

### Live verification

Every fix was also exercised against a running server, not only the test client:

| Check | Result |
|---|---|
| Anonymous `/dashboard`, `/`, `/export-roster` | `302` → `/login` |
| Anonymous `/api/events`, `/api/roster`, scan | `401` |
| `/scan` (public shell) | `200` |
| Admin login → create event → add participant | `302` / `201` / `201` |
| Scanner auth → scan → replay | `ok` → `ok (replayed)` |
| Second device scanning the same badge | `duplicate`, attributed to the first phone |
| Cross-event token | `403` |
| Admin POST without CSRF token | `400` |
| Undo → re-scan | `ok` → `ok` |
| Revoke → scan → re-auth | `200` → `401` → `200` |
| CSV export of `=cmd\|calc` | 1 cell apostrophe-prefixed |
| `PRAGMA journal_mode` / `busy_timeout` | `wal` / `10000` |
| `/static/sw.js` | `200`, precache list populated |

---

## 9. Files changed

| File | Change |
|------|--------|
| `app.py` | Auth, CSRF, rate limiting, input sanitisation, scan/sync correctness, 5 new endpoints |
| `db.py` | Single SQLite connection helper with WAL + busy timeout |
| `static/sw.js` | Rewritten — real precaching, `/api/` never cached |
| `templates/login.html` | **New** — admin sign-in page |
| `templates/dashboard.html` | CSRF tokens, sign-out, undo + revoke controls |
| `templates/mapping.html` | CSRF token, destructive-replace confirmation |
| `templates/upload.html` | CSRF token, sign-out |
| `templates/scan.html` | Access-code pre-fill removed |
| `tests/test_security_and_logic.py` | **New** — 47 regression tests |
| `tests/test_cloud_multidevice.py` | Admin login added |
| `README.md` | Rewritten — documented a SQLite-only single-`roster` schema that no longer exists |
| `.gitignore` | WAL sidecar files |

10 modified, 2 new. ~1030 insertions.

---

## 10. Remaining issues

Genuinely open, in priority order:

1. **The PostgreSQL path was not exercised.** `psycopg2-binary` cannot install in
   this environment (no `pg_config`), so all verification ran on SQLite. This
   matters most for H5: the read/update race is far more reachable under real
   parallelism than under SQLite's write serialisation. **Re-run the suite
   against a real PostgreSQL instance before the next event.**
2. **`next_reg_id_for_event` mixes prefixes.** It derives the next ID from
   whichever row holds the highest number, so a roster containing both
   `A-001…A-010` and `B-003` produces an `A`-prefixed ID. Only affects
   mixed-prefix rosters; left alone rather than guessing the intended rule.
3. **`events.admin_password_hash` is still unused.** Per-event admin passwords
   would be a real feature; one global admin was built rather than shipping half
   of it.
4. **The dashboard polls the full roster every 3.5 s.** Fine at event scale;
   large rosters would want pagination or ETags.
5. **Tests write to the configured database.** No fixture isolation — point
   `DATABASE_URL` at a throwaway DB, never production.
6. **Wildcard CORS retained deliberately.** The offline APK runs from `file://`
   (origin `null`) and needs it. Not a CSRF vector: browsers refuse cookies on
   wildcard origins, so admin routes are unreachable cross-origin, and scanner
   routes need a bearer token a foreign page cannot read. Documented in-code.

## 11. Deployment checklist

```bash
export SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
export ADMIN_PASSWORD='…'          # else a random one is printed at startup
export DATABASE_URL='postgresql://…'
export SESSION_COOKIE_SECURE=1     # when serving over HTTPS
unset FLASK_DEBUG                  # never set this in production
```

Serve with gunicorn, not `app.py`. `SECRET_KEY` **must** be set and identical
across workers, or admin sessions will break between requests.
