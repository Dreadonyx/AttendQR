"""
AttendQR — Security & business-logic regression tests.

One test per defect fixed, so a regression names the exact problem it brings
back. Each test creates its own event so the suite is order-independent.
"""
import os
import uuid

import pytest

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

import app as app_module  # noqa: E402
import db  # noqa: E402

ADMIN_PASSWORD = "test-admin-password"
app_module.ADMIN_PASSWORD = ADMIN_PASSWORD


@pytest.fixture(scope="module", autouse=True)
def _schema():
    db.init_db()


@pytest.fixture
def anon():
    app_module.app.config["TESTING"] = False
    return app_module.app.test_client()


@pytest.fixture
def admin():
    client = app_module.app.test_client()
    res = client.post("/login", data={"password": ADMIN_PASSWORD})
    assert res.status_code in (302, 200)
    # The dashboard JS sends this header on admin POSTs; do the same here.
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "admin-fixture-csrf"
    client.environ_base["HTTP_X_CSRF_TOKEN"] = "admin-fixture-csrf"
    return client


def make_event(admin_client, code=None, access="ACCESS-CODE-1", prefix="T-", width=3):
    code = code or "EV" + uuid.uuid4().hex[:6].upper()
    res = admin_client.post("/api/events", json={
        "name": f"Event {code}", "code": code,
        "access_code": access, "id_prefix": prefix, "id_width": width,
    })
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["event_id"], code, access


def auth_scanner(client, code, access, device_id="dev-test", device_name="Tester"):
    res = client.post("/api/auth/scanner", json={
        "event_code": code, "access_code": access,
        "device_id": device_id, "device_name": device_name,
    })
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["token"]


def add_participant(admin_client, event_id, name="Ada Lovelace", email="ada@example.com", dept="CS"):
    res = admin_client.post(
        f"/api/events/{event_id}/add-participant",
        json={"name": name, "email": email, "department": dept},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["reg_id"]


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------

class TestAdminSurfaceIsProtected:
    def test_admin_pages_redirect_anonymous_to_login(self, anon):
        for path in ("/", "/dashboard", "/export-roster"):
            res = anon.get(path)
            assert res.status_code == 302, path
            assert "/login" in res.headers["Location"], path

    def test_admin_apis_reject_anonymous(self, anon):
        assert anon.get("/api/events").status_code == 401
        assert anon.post("/api/events", json={"name": "x", "code": "XX1"}).status_code == 401
        assert anon.get("/api/roster").status_code == 401

    def test_event_access_codes_are_not_exposed_anonymously(self, anon, admin):
        _eid, code, access = make_event(admin, access="SUPER-SECRET-9")
        body = anon.get("/api/events").get_data(as_text=True)
        assert "SUPER-SECRET-9" not in body
        assert code not in body

    def test_scan_page_does_not_leak_the_access_code(self, anon, admin):
        make_event(admin, access="LEAKY-CODE-77")
        body = anon.get("/scan").get_data(as_text=True)
        assert body.count("LEAKY-CODE-77") == 0

    def test_admin_can_sign_in_and_out(self, anon):
        assert anon.post("/login", data={"password": "wrong"}).status_code == 401
        assert anon.post("/login", data={"password": ADMIN_PASSWORD}).status_code == 302
        assert anon.get("/dashboard").status_code == 200
        with anon.session_transaction() as sess:
            token = sess["_csrf_token"] = "logout-csrf"
        anon.post("/logout", data={"csrf_token": token})
        assert anon.get("/dashboard").status_code == 302

    def test_login_next_only_redirects_to_local_paths(self, anon):
        res = anon.post("/login?next=https://evil.example/x", data={"password": ADMIN_PASSWORD})
        assert res.headers["Location"].endswith("/dashboard")
        anon.post("/logout")
        res = anon.post("/login?next=//evil.example", data={"password": ADMIN_PASSWORD})
        assert res.headers["Location"].endswith("/dashboard")


class TestScannerAuthorization:
    def test_anonymous_cannot_mark_attendance(self, anon, admin):
        event_id, _code, _access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        res = anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id})
        assert res.status_code == 401
        roster = admin.get(f"/api/events/{event_id}/roster").get_json()
        assert roster["summary"]["attended"] == 0

    def test_token_for_one_event_cannot_scan_another(self, anon, admin):
        event_a, code_a, access_a = make_event(admin, access="AAA-ACCESS")
        event_b, _code_b, _access_b = make_event(admin, access="BBB-ACCESS")
        reg_b = add_participant(admin, event_b, name="Bob B")

        token_a = auth_scanner(anon, code_a, access_a, device_id="dev-a")
        res = anon.post(
            f"/api/events/{event_b}/scan",
            json={"reg_id": reg_b},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert res.status_code == 403
        assert admin.get(f"/api/events/{event_b}/roster").get_json()["summary"]["attended"] == 0

    def test_roster_pii_requires_authentication(self, anon, admin):
        event_id, code, access = make_event(admin)
        add_participant(admin, event_id, name="Grace Hopper", email="grace@example.com")

        assert anon.get(f"/api/events/{event_id}/roster").status_code == 401

        token = auth_scanner(anon, code, access, device_id="dev-roster")
        ok = anon.get(
            f"/api/events/{event_id}/roster",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200
        assert ok.get_json()["summary"]["total"] == 1

    def test_heartbeat_requires_a_token_for_that_event(self, anon, admin):
        event_a, code_a, access_a = make_event(admin)
        event_b, _c, _a = make_event(admin)
        token_a = auth_scanner(anon, code_a, access_a, device_id="dev-hb")

        assert anon.post(f"/api/events/{event_a}/heartbeat", json={"pending_count": 0}).status_code == 401
        assert anon.post(
            f"/api/events/{event_b}/heartbeat", json={"pending_count": 0},
            headers={"Authorization": f"Bearer {token_a}"},
        ).status_code == 401
        assert anon.post(
            f"/api/events/{event_a}/heartbeat", json={"pending_count": 3},
            headers={"Authorization": f"Bearer {token_a}"},
        ).status_code == 200

    def test_invalid_access_code_is_rejected_without_confirming_the_event(self, anon, admin):
        _eid, code, _access = make_event(admin, access="RIGHT-CODE")
        res = anon.post("/api/auth/scanner", json={
            "event_code": code, "access_code": "WRONG-CODE", "device_id": "d1",
        })
        assert res.status_code == 401
        # Same status/message for a nonexistent event: no event enumeration.
        missing = anon.post("/api/auth/scanner", json={
            "event_code": "NOSUCHEVENT", "access_code": "WRONG-CODE", "device_id": "d1",
        })
        assert missing.status_code == 401
        assert missing.get_json()["message"] == res.get_json()["message"]

    def test_repeated_bad_access_codes_are_rate_limited(self, anon, admin):
        _eid, code, _access = make_event(admin, access="THROTTLE-ME")
        app_module._auth_attempts.clear()
        statuses = [
            anon.post("/api/auth/scanner", json={
                "event_code": code, "access_code": f"bad-{i}", "device_id": "d",
            }).status_code
            for i in range(app_module.AUTH_MAX_ATTEMPTS + 3)
        ]
        assert 429 in statuses
        app_module._auth_attempts.clear()

    def test_revoked_device_token_stops_working(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-revoke")

        assert admin.post(f"/api/events/{event_id}/scanners/dev-revoke/revoke").status_code == 200
        res = anon.post(
            f"/api/events/{event_id}/scan",
            json={"reg_id": reg_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 401


class TestCSRF:
    def test_cookie_authenticated_form_post_without_token_is_rejected(self, admin):
        event_id, _c, _a = make_event(admin)
        # A separate signed-in client that sends NO CSRF token, i.e. what a
        # cross-site form post from another origin would look like.
        forger = app_module.app.test_client()
        forger.post("/login", data={"password": ADMIN_PASSWORD})
        res = forger.post(
            f"/api/events/{event_id}/add-participant",
            data={"name": "No Token", "email": "n@example.com", "department": "X"},
        )
        assert res.status_code == 400
        assert "CSRF" in res.get_json()["message"]
        # ...and nothing was written.
        roster = admin.get(f"/api/events/{event_id}/roster").get_json()
        assert not any(r["name"] == "No Token" for r in roster["roster"])

    def test_form_post_with_token_succeeds(self, admin):
        event_id, _c, _a = make_event(admin)
        with admin.session_transaction() as sess:
            token = sess["_csrf_token"] = "fixed-test-csrf-token"
        res = admin.post(
            f"/api/events/{event_id}/add-participant",
            data={
                "name": "With Token", "email": "w@example.com",
                "department": "X", "csrf_token": token,
            },
        )
        assert res.status_code == 302   # form posts redirect back to the dashboard
        roster = admin.get(f"/api/events/{event_id}/roster").get_json()
        assert any(r["name"] == "With Token" for r in roster["roster"])

    def test_bearer_token_api_calls_are_csrf_exempt(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-csrf")
        res = anon.post(
            f"/api/events/{event_id}/scan",
            json={"reg_id": reg_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        assert res.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

class TestScanLogic:
    def test_first_scan_then_duplicate(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-dup")
        headers = {"Authorization": f"Bearer {token}"}

        first = anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "s-1"}, headers=headers)
        assert first.get_json()["status"] == "ok"
        second = anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "s-2"}, headers=headers)
        assert second.get_json()["status"] == "duplicate"

    def test_retrying_the_same_scan_id_is_idempotent(self, anon, admin):
        """A network retry must not append a second audit row."""
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-idem")
        headers = {"Authorization": f"Bearer {token}"}
        body = {"reg_id": reg_id, "scan_id": "retry-me"}

        first = anon.post(f"/api/events/{event_id}/scan", json=body, headers=headers)
        again = anon.post(f"/api/events/{event_id}/scan", json=body, headers=headers)
        assert first.get_json()["status"] == "ok"
        assert again.get_json()["status"] == "ok"
        assert again.get_json().get("replayed") is True

        stats = admin.get(f"/api/events/{event_id}/stats").get_json()
        assert len([s for s in stats["recent_scans"] if s["reg_id"] == reg_id]) == 1

    def test_unknown_qr_reports_not_found(self, anon, admin):
        event_id, code, access = make_event(admin)
        token = auth_scanner(anon, code, access, device_id="dev-404")
        res = anon.post(
            f"/api/events/{event_id}/scan",
            json={"reg_id": "NOPE-999"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.get_json()["status"] == "not_found"

    def test_implausible_client_timestamp_is_replaced(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-clock")
        res = anon.post(
            f"/api/events/{event_id}/scan",
            json={"reg_id": reg_id, "scanned_at": "2999-01-01T00:00:00Z"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        assert not res.get_json()["scanned_at"].startswith("2999")

    def test_garbage_timestamp_does_not_crash(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-badts")
        res = anon.post(
            f"/api/events/{event_id}/scan",
            json={"reg_id": reg_id, "scanned_at": "not-a-timestamp"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        assert res.get_json()["status"] == "ok"

    def test_offline_sync_is_idempotent(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-sync")
        headers = {"Authorization": f"Bearer {token}"}
        batch = {"scans": [{"reg_id": reg_id, "scan_id": "offline-1"}]}

        first = anon.post(f"/api/events/{event_id}/sync", json=batch, headers=headers)
        replay = anon.post(f"/api/events/{event_id}/sync", json=batch, headers=headers)
        assert first.get_json()["results"][0]["status"] == "ok"
        assert replay.get_json()["results"][0]["status"] == "ok"
        assert admin.get(f"/api/events/{event_id}/roster").get_json()["summary"]["attended"] == 1

    def test_oversized_sync_batch_is_refused(self, anon, admin):
        event_id, code, access = make_event(admin)
        token = auth_scanner(anon, code, access, device_id="dev-big")
        res = anon.post(
            f"/api/events/{event_id}/sync",
            json={"scans": [{"reg_id": "X", "scan_id": str(i)} for i in range(5001)]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 413

    def test_malformed_heartbeat_payload_does_not_crash(self, anon, admin):
        event_id, code, access = make_event(admin)
        token = auth_scanner(anon, code, access, device_id="dev-hb2")
        res = anon.post(
            f"/api/events/{event_id}/heartbeat",
            json={"pending_count": "not-a-number", "status": "x" * 500},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200


class TestUndoAttendance:
    def test_admin_can_reverse_a_mistaken_check_in(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        token = auth_scanner(anon, code, access, device_id="dev-undo")
        headers = {"Authorization": f"Bearer {token}"}

        anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "u-1"}, headers=headers)
        assert admin.get(f"/api/events/{event_id}/roster").get_json()["summary"]["attended"] == 1

        undo = admin.post(f"/api/events/{event_id}/participants/{reg_id}/undo")
        assert undo.status_code == 200
        assert admin.get(f"/api/events/{event_id}/roster").get_json()["summary"]["attended"] == 0

        # ...and they can be scanned in again afterwards.
        again = anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "u-2"}, headers=headers)
        assert again.get_json()["status"] == "ok"

    def test_undo_requires_admin(self, anon, admin):
        event_id, _c, _a = make_event(admin)
        reg_id = add_participant(admin, event_id)
        assert anon.post(f"/api/events/{event_id}/participants/{reg_id}/undo").status_code == 401

    def test_undo_on_an_absent_participant_is_a_noop(self, admin):
        event_id, _c, _a = make_event(admin)
        reg_id = add_participant(admin, event_id)
        res = admin.post(f"/api/events/{event_id}/participants/{reg_id}/undo")
        assert res.status_code == 200
        assert res.get_json()["status"] == "noop"

    def test_admin_can_manually_mark_present_and_absent(self, admin):
        event_id, _c, _a = make_event(admin)
        reg_id = add_participant(admin, event_id)

        # Mark present
        res = admin.post(f"/api/events/{event_id}/participants/{reg_id}/set-attendance", json={"attended": 1})
        assert res.status_code == 200
        assert res.get_json()["status"] == "ok"
        assert res.get_json()["attended"] == 1

        # Check roster
        roster = admin.get(f"/api/events/{event_id}/roster").get_json()["roster"]
        assert roster[0]["attended"] == 1

        # Mark absent
        res2 = admin.post(f"/api/events/{event_id}/participants/{reg_id}/set-attendance", json={"attended": 0})
        assert res2.status_code == 200
        assert res2.get_json()["status"] == "ok"
        assert res2.get_json()["attended"] == 0

        # Check roster again
        roster2 = admin.get(f"/api/events/{event_id}/roster").get_json()["roster"]
        assert roster2[0]["attended"] == 0

    def test_set_attendance_requires_admin(self, anon, admin):
        event_id, _c, _a = make_event(admin)
        reg_id = add_participant(admin, event_id)
        res = anon.post(f"/api/events/{event_id}/participants/{reg_id}/set-attendance", json={"attended": 1})
        assert res.status_code in (401, 302)


class TestParticipantCreation:
    def test_form_post_with_blank_field_returns_400_not_500(self, admin):
        """request.json was touched on form posts, turning a validation error into a crash."""
        event_id, _c, _a = make_event(admin)
        with admin.session_transaction() as sess:
            token = sess["_csrf_token"] = "csrf-blank-test"
        res = admin.post(
            f"/api/events/{event_id}/add-participant",
            data={"name": "", "email": "", "department": "", "csrf_token": token},
            follow_redirects=False,
        )
        assert res.status_code in (302, 400)

    def test_duplicate_reg_id_is_rejected_cleanly(self, admin):
        event_id, _c, _a = make_event(admin)
        reg_id = add_participant(admin, event_id)
        res = admin.post(f"/api/events/{event_id}/add-participant", json={
            "name": "Clash", "email": "c@example.com", "department": "X", "reg_id": reg_id,
        })
        assert res.status_code == 400

    def test_overlong_field_is_rejected(self, admin):
        event_id, _c, _a = make_event(admin)
        res = admin.post(f"/api/events/{event_id}/add-participant", json={
            "name": "A" * 300, "email": "a@example.com", "department": "X",
        })
        assert res.status_code == 400

    def test_reg_ids_increment(self, admin):
        event_id, _c, _a = make_event(admin, prefix="SEQ-", width=3)
        first = add_participant(admin, event_id, email="one@example.com")
        second = add_participant(admin, event_id, email="two@example.com")
        assert first == "SEQ-001"
        assert second == "SEQ-002"


class TestEventValidation:
    def test_bad_event_code_is_rejected(self, admin):
        for code in ("", "x", "has space", "A" * 40, "sym$bol"):
            res = admin.post("/api/events", json={"name": "N", "code": code, "access_code": "abcd"})
            assert res.status_code == 400, code

    def test_short_access_code_is_rejected(self, admin):
        res = admin.post("/api/events", json={"name": "N", "code": "OKCODE1", "access_code": "ab"})
        assert res.status_code == 400

    def test_duplicate_event_code_is_rejected(self, admin):
        _eid, code, _a = make_event(admin)
        res = admin.post("/api/events", json={"name": "Dup", "code": code, "access_code": "abcd"})
        assert res.status_code == 400

    def test_absurd_id_width_is_clamped(self, admin):
        event_id, _c, _a = make_event(admin, prefix="W-", width=999999)
        reg_id = add_participant(admin, event_id)
        assert len(reg_id) < 32


class TestExport:
    def test_csv_formula_injection_is_neutralised(self, admin):
        event_id, _c, _a = make_event(admin)
        admin.post(f"/api/events/{event_id}/add-participant", json={
            "name": "=1+1", "email": "+cmd@example.com", "department": "@SUM(A1)",
        })
        body = admin.get(f"/api/events/{event_id}/export").get_data(as_text=True)
        assert "'=1+1" in body
        assert "'+cmd@example.com" in body
        assert "'@SUM(A1)" in body

    def test_export_requires_admin(self, anon):
        assert anon.get("/export-roster").status_code == 302


class TestOperational:
    def test_healthz_reports_backend(self, anon):
        res = anon.get("/healthz")
        assert res.status_code == 200
        assert res.get_json()["backend"] in ("sqlite", "postgres")

    def test_security_headers_present(self, anon):
        headers = anon.get("/healthz").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"

    def test_upload_size_cap_is_configured(self):
        assert app_module.app.config["MAX_CONTENT_LENGTH"] == app_module.MAX_UPLOAD_MB * 1024 * 1024

    def test_debug_is_off_by_default(self):
        assert app_module.DEBUG_MODE is False


class TestConcurrencyInvariants:
    """The conditional UPDATE must decide the winner, not the earlier read."""

    def test_stale_read_cannot_produce_a_second_official_check_in(self, anon, admin, monkeypatch):
        """
        Deterministic reproduction of the two-devices-at-once interleaving.

        A real thread race is not reproducible on SQLite (it serialises writes),
        so instead the participant lookup is forced to return a stale
        attended=0 row for an attendee who is already checked in — exactly what
        the losing device sees. The UPDATE then matches zero rows, and the
        endpoint must report 'duplicate'. Trusting the stale read instead of the
        UPDATE's rowcount yields a second 'ok' and a second audit row.
        """
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id, name="Stale Read")
        token = auth_scanner(anon, code, access, device_id="dev-stale", device_name="LoserPhone")
        headers = {"Authorization": f"Bearer {token}"}

        first = anon.post(f"/api/events/{event_id}/scan",
                          json={"reg_id": reg_id, "scan_id": "stale-1"}, headers=headers)
        assert first.get_json()["status"] == "ok"

        real_fetchone = db.DBWrapper.fetchone

        def stale_fetchone(self, sql, params=()):
            row = real_fetchone(self, sql, params)
            if row and "FROM participants" in sql and "SELECT *" in sql:
                row = dict(row)
                row["attended"] = 0          # the losing device's stale view
            return row

        monkeypatch.setattr(db.DBWrapper, "fetchone", stale_fetchone)
        second = anon.post(f"/api/events/{event_id}/scan",
                           json={"reg_id": reg_id, "scan_id": "stale-2"}, headers=headers)
        monkeypatch.undo()

        assert second.get_json()["status"] == "duplicate", second.get_json()

        conn = db.get_db_connection()
        try:
            row = conn.fetchone(
                "SELECT COUNT(*) as cnt FROM attendance_logs WHERE event_id = ? AND reg_id = ? AND status = 'ok'",
                (event_id, reg_id),
            )
        finally:
            conn.close()
        assert row["cnt"] == 1, "a stale read produced a second official check-in"

    def test_concurrent_scans_produce_exactly_one_ok(self, anon, admin):
        """
        End-to-end smoke test of 8 simultaneous scans. Note: on SQLite this
        does not by itself prove the rowcount guard works — writes serialise,
        so the interleaving rarely happens. See the stale-read test above for
        the deterministic case.
        """
        import concurrent.futures

        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id, name="Race Target")
        tokens = [
            auth_scanner(anon, code, access, device_id=f"race-dev-{i}", device_name=f"Phone-{i}")
            for i in range(4)
        ]

        def scan(i):
            client = app_module.app.test_client()
            return client.post(
                f"/api/events/{event_id}/scan",
                json={"reg_id": reg_id, "scan_id": f"race-{i}"},
                headers={"Authorization": f"Bearer {tokens[i % len(tokens)]}"},
            ).get_json()["status"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(scan, range(8)))

        assert statuses.count("ok") == 1, statuses
        assert statuses.count("duplicate") == 7, statuses

        # The audit log must agree with the responses: exactly one official
        # check-in was recorded, not one per device that thought it won.
        conn = db.get_db_connection()
        try:
            row = conn.fetchone(
                "SELECT COUNT(*) as cnt FROM attendance_logs WHERE event_id = ? AND reg_id = ? AND status = 'ok'",
                (event_id, reg_id),
            )
        finally:
            conn.close()
        assert row["cnt"] == 1

        assert admin.get(f"/api/events/{event_id}/roster").get_json()["summary"]["attended"] == 1

    def test_duplicate_reports_the_first_scanner_not_the_loser(self, anon, admin):
        event_id, code, access = make_event(admin)
        reg_id = add_participant(admin, event_id)
        first = auth_scanner(anon, code, access, device_id="dev-first", device_name="FirstPhone")
        second = auth_scanner(anon, code, access, device_id="dev-second", device_name="SecondPhone")

        anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "f-1"},
                  headers={"Authorization": f"Bearer {first}"})
        dup = anon.post(f"/api/events/{event_id}/scan", json={"reg_id": reg_id, "scan_id": "s-1"},
                        headers={"Authorization": f"Bearer {second}"}).get_json()

        assert dup["status"] == "duplicate"
        assert dup["scanner"] == "FirstPhone"


class TestOfflineShell:
    """The PWA advertises offline scanning; the worker must actually cache."""

    def _sw(self):
        import pathlib
        return pathlib.Path(__file__).resolve().parent.parent.joinpath("static/sw.js").read_text()

    def test_service_worker_precaches_the_scanner_shell(self):
        sw = self._sw()
        assert "cache.add" in sw or "addAll" in sw, "nothing is ever written to the cache"
        for asset in ("'/scan'", "style.css", "html5-qrcode"):
            assert asset in sw, asset

    def test_service_worker_never_caches_attendance_api_calls(self):
        sw = self._sw()
        assert "startsWith('/api/')" in sw


class TestSqliteReliability:
    def test_sqlite_uses_wal_and_a_busy_timeout(self):
        if db.IS_POSTGRES:
            import pytest as _pytest
            _pytest.skip("running against PostgreSQL")
        conn = db.get_db_connection()
        try:
            mode = conn.fetchone("PRAGMA journal_mode")
            timeout = conn.fetchone("PRAGMA busy_timeout")
        finally:
            conn.close()
        assert str(list(mode.values())[0]).lower() == "wal"
        assert int(list(timeout.values())[0]) >= 5000
