"""
AttendQR — Cloud-First Multi-Device Comprehensive Test Suite
Tests:
1. Multi-Event Isolation
2. Scanner Authentication & Token Issuance
3. Concurrent Transactional Scanning (Race Condition Prevention)
4. Offline Batch Sync & Idempotency
5. Heartbeat & Device Online/Offline Calculation
6. Late Participant Addition & QR Generation
7. Multi-Device Conflict Handling
8. Dynamic CSV Export
"""
import concurrent.futures
import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

import app
import db

ADMIN_PASSWORD = "test-admin-password"


def test_full_cloud_suite():
    # Bootstrap DB
    db.init_db()
    app.ADMIN_PASSWORD = ADMIN_PASSWORD
    client = app.app.test_client()

    # Event management, rosters, QR passes and exports now require an admin
    # session; scanners continue to use their own bearer tokens.
    login = client.post('/login', data={'password': ADMIN_PASSWORD})
    assert login.status_code == 302

    # -------------------------------------------------------------
    # 1. Multi-Event Creation & Isolation
    # -------------------------------------------------------------
    # Create Event 1
    res1 = client.post('/api/events', json={
        'name': 'Aazhi CTF 2026',
        'code': 'AAZHI26',
        'access_code': 'SCAN123',
        'id_prefix': 'AAZHI-',
        'id_width': 3
    })
    assert res1.status_code in (200, 201, 400) # 400 if already created, which is fine
    
    # Create Event 2
    res2 = client.post('/api/events', json={
        'name': 'Cyber Olympiad 2026',
        'code': 'CYV26',
        'access_code': 'CYBER999',
        'id_prefix': 'CYV-',
        'id_width': 4
    })
    assert res2.status_code in (200, 201, 400)

    # Get events list
    events_res = client.get('/api/events')
    assert events_res.status_code == 200
    events_list = events_res.get_json()['events']
    
    ev1 = next((e for e in events_list if e['code'] == 'AAZHI26'), None)
    ev2 = next((e for e in events_list if e['code'] == 'CYV26'), None)
    assert ev1 is not None
    assert ev2 is not None

    ev1_id = ev1['id']
    ev2_id = ev2['id']

    # -------------------------------------------------------------
    # 2. Scanner Authentication
    # -------------------------------------------------------------
    # Valid login for Scanner 1 on Event 1
    auth1_res = client.post('/api/auth/scanner', json={
        'event_code': 'AAZHI26',
        'access_code': 'SCAN123',
        'device_id': 'phone_scanner_01',
        'device_name': 'Scanner-Alpha'
    })
    assert auth1_res.status_code == 200
    auth1_data = auth1_res.get_json()
    assert auth1_data['status'] == 'ok'
    scanner1_token = auth1_data['token']
    assert scanner1_token is not None

    # Valid login for Scanner 2 on Event 1
    auth2_res = client.post('/api/auth/scanner', json={
        'event_code': 'AAZHI26',
        'access_code': 'SCAN123',
        'device_id': 'phone_scanner_02',
        'device_name': 'Scanner-Beta'
    })
    assert auth2_res.status_code == 200
    scanner2_token = auth2_res.get_json()['token']

    # Invalid access code test
    bad_auth = client.post('/api/auth/scanner', json={
        'event_code': 'AAZHI26',
        'access_code': 'WRONG_CODE',
        'device_id': 'phone_hacker',
        'device_name': 'Hacker Phone'
    })
    assert bad_auth.status_code == 401

    # -------------------------------------------------------------
    # 3. Add Participants into Event 1 & Event 2
    # -------------------------------------------------------------
    # Add to Event 1
    add1 = client.post(f'/api/events/{ev1_id}/add-participant', json={
        'name': 'Alice Smith',
        'email': 'alice@aazhictf.org',
        'department': 'CS',
        'reg_id': 'AAZHI-001'
    })
    assert add1.status_code in (200, 201)

    add2 = client.post(f'/api/events/{ev1_id}/add-participant', json={
        'name': 'Bob Jones',
        'email': 'bob@aazhictf.org',
        'department': 'IT',
        'reg_id': 'AAZHI-002'
    })
    assert add2.status_code in (200, 201)

    # Add to Event 2
    add_ev2 = client.post(f'/api/events/{ev2_id}/add-participant', json={
        'name': 'Charlie Brown',
        'email': 'charlie@cyv.org',
        'department': 'CyberSec',
        'reg_id': 'CYV-0001'
    })
    assert add_ev2.status_code in (200, 201)

    # Verify Isolation
    roster1 = client.get(f'/api/events/{ev1_id}/roster').get_json()['roster']
    roster2 = client.get(f'/api/events/{ev2_id}/roster').get_json()['roster']
    
    assert any(p['reg_id'] == 'AAZHI-001' for p in roster1)
    assert not any(p['reg_id'] == 'CYV-0001' for p in roster1)
    assert any(p['reg_id'] == 'CYV-0001' for p in roster2)
    assert not any(p['reg_id'] == 'AAZHI-001' for p in roster2)

    # -------------------------------------------------------------
    # 4. Concurrent Scanning & Race Condition Prevention
    # -------------------------------------------------------------
    # Reset attendance for AAZHI-001
    db_conn = db.get_db_connection()
    db_conn.execute("UPDATE participants SET attended = 0, scanned_at = NULL WHERE event_id = ? AND reg_id = 'AAZHI-001'", (ev1_id,))
    db_conn.commit()
    db_conn.close()

    def do_scan(scanner_name, scanner_tok):
        c = app.app.test_client()
        return c.post(
            f'/api/events/{ev1_id}/scan',
            headers={'Authorization': f'Bearer {scanner_tok}'},
            json={
                'reg_id': 'AAZHI-001',
                'scan_id': str(uuid.uuid4()),
                'device_name': scanner_name
            }
        ).get_json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(do_scan, f"Scanner-{i}", scanner1_token if i % 2 == 0 else scanner2_token)
            for i in range(8)
        ]
        results = [f.result() for f in futures]

    statuses = [r['status'] for r in results]
    ok_count = statuses.count('ok')
    dup_count = statuses.count('duplicate')
    
    assert ok_count == 1, f"Expected exactly 1 OK scan, got {ok_count}"
    assert dup_count == 7, f"Expected 7 duplicate scans, got {dup_count}"
    print(f"Concurrent scan test passed: 1 OK, 7 Duplicates")

    # -------------------------------------------------------------
    # 5. Offline Batch Sync & Idempotency
    # -------------------------------------------------------------
    # Add AAZHI-003 and AAZHI-004
    client.post(f'/api/events/{ev1_id}/add-participant', json={'name': 'David', 'email': 'david@test.com', 'department': 'ECE', 'reg_id': 'AAZHI-003'})
    client.post(f'/api/events/{ev1_id}/add-participant', json={'name': 'Eve', 'email': 'eve@test.com', 'department': 'Mech', 'reg_id': 'AAZHI-004'})

    scan3_id = 'offline_uuid_003'
    scan4_id = 'offline_uuid_004'

    sync_batch = {
        'device_id': 'phone_scanner_01',
        'device_name': 'Scanner-Alpha',
        'scans': [
            {'scan_id': scan3_id, 'reg_id': 'AAZHI-003', 'scanned_at': '2026-08-21T10:00:00Z'},
            {'scan_id': scan4_id, 'reg_id': 'AAZHI-004', 'scanned_at': '2026-08-21T10:01:00Z'},
            {'scan_id': 'offline_uuid_999', 'reg_id': 'NON_EXISTENT', 'scanned_at': '2026-08-21T10:02:00Z'},
        ]
    }

    # First sync
    sync_res1 = client.post(
        f'/api/events/{ev1_id}/sync',
        headers={'Authorization': f'Bearer {scanner1_token}'},
        json=sync_batch
    )
    assert sync_res1.status_code == 200
    sync_data1 = sync_res1.get_json()
    assert sync_data1['processed_count'] == 3
    assert sync_data1['results'][0]['status'] == 'ok'
    assert sync_data1['results'][1]['status'] == 'ok'
    assert sync_data1['results'][2]['status'] == 'not_found'

    # Second sync (same batch) -> verify idempotency
    sync_res2 = client.post(
        f'/api/events/{ev1_id}/sync',
        headers={'Authorization': f'Bearer {scanner1_token}'},
        json=sync_batch
    )
    assert sync_res2.status_code == 200
    sync_data2 = sync_res2.get_json()
    assert sync_data2['results'][0]['status'] == 'ok'  # Retains idempotent result
    assert sync_data2['results'][1]['status'] == 'ok'

    # Check participant state in roster
    p3 = next(p for p in client.get(f'/api/events/{ev1_id}/roster').get_json()['roster'] if p['reg_id'] == 'AAZHI-003')
    assert p3['attended'] == 1
    assert p3['scanned_at'] == '2026-08-21T10:00:00Z'
    assert p3['scanned_by_device_name'] == 'Scanner-Alpha'
    print("Offline Sync & Idempotency test passed!")

    # -------------------------------------------------------------
    # 6. Heartbeat & Live Stats Calculation
    # -------------------------------------------------------------
    hb_res = client.post(
        f'/api/events/{ev1_id}/heartbeat',
        headers={'Authorization': f'Bearer {scanner1_token}'},
        json={'pending_count': 2, 'status': 'online'}
    )
    assert hb_res.status_code == 200

    stats_res = client.get(f'/api/events/{ev1_id}/stats')
    assert stats_res.status_code == 200
    stats_data = stats_res.get_json()
    
    assert stats_data['summary']['total'] >= 4
    assert stats_data['summary']['attended'] >= 3
    
    sc1 = next((s for s in stats_data['scanners'] if s['device_id'] == 'phone_scanner_01'), None)
    assert sc1 is not None
    assert sc1['is_online'] is True
    assert sc1['pending_sync_count'] == 2
    print("Heartbeat and Live Stats test passed!")

    # -------------------------------------------------------------
    # 7. QR Pass Download & CSV Export
    # -------------------------------------------------------------
    qr_res = client.get(f'/api/events/{ev1_id}/qr/AAZHI-001')
    assert qr_res.status_code == 200
    assert qr_res.content_type == 'image/png'

    csv_res = client.get(f'/api/events/{ev1_id}/export')
    assert csv_res.status_code == 200
    csv_text = csv_res.data.decode('utf-8')
    csv_rows = list(csv.reader(io.StringIO(csv_text)))
    # Required columns must appear (extra columns may be present depending on event settings)
    required_cols = ["Registration ID", "Name", "Email", "Department", "Attended", "Scanned At", "Scanner Device"]
    header_row = csv_rows[0]
    for col in required_cols:
        assert col in header_row, f"Expected column '{col}' in CSV header, got: {header_row}"
    
    print("\n🎉 ALL CLOUD & MULTI-DEVICE TESTS PASSED PERFECTLY!")

if __name__ == '__main__':
    test_full_cloud_suite()
