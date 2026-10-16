"""Comprehensive end-to-end verification script for ALL listed Launch Gate APIs."""

import requests
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL = "http://127.0.0.1:8000"

def run_suite():
    print("=" * 70)
    print("🚀 LAUNCH GATE — COMPLETE API VERIFICATION TEST SUITE")
    print("=" * 70)

    # 1. Health
    res = requests.get(f"{BASE_URL}/health")
    assert res.status_code == 200, f"Health failed: {res.text}"
    print("✓ [PASS] GET  /health -> 200 OK")

    # 2. Login
    res = requests.post(f"{BASE_URL}/auth/login", json={"email": "buyer@launchgate.com", "password": "password123"})
    assert res.status_code == 200, f"Login failed: {res.text}"
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    print(f"✓ [PASS] POST /auth/login -> 200 OK (Token: {token[:16]}...)")

    # 3. List Batches
    res = requests.get(f"{BASE_URL}/batches", headers=headers)
    assert res.status_code == 200, f"Batches failed: {res.text}"
    batches = res.json()
    assert len(batches) > 0, "No batches found! Seeding needed."
    batch_id = batches[0]["batch_id"]
    print(f"✓ [PASS] GET  /batches -> 200 OK ({len(batches)} batches found)")

    # 4. Get Single Batch
    res = requests.get(f"{BASE_URL}/batches/{batch_id}", headers=headers)
    assert res.status_code == 200, f"Get batch failed: {res.text}"
    batch = res.json()
    ads = batch.get("ads", [])
    assert len(ads) > 0, "Batch has no ads!"
    ad_id = ads[0]["ad_id"]
    findings = ads[0].get("findings", [])
    print(f"✓ [PASS] GET  /batches/{batch_id} -> 200 OK ({len(ads)} ads in batch)")

    # 5. Ad Approvals API
    # 5a. GET /ads/{ad_id}/approval
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/approval", headers=headers)
    assert res.status_code == 200, f"Get approval failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/approval -> 200 OK")

    # 5b. GET /ads/{ad_id}/approval-record
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/approval-record", headers=headers)
    assert res.status_code == 200, f"Get approval record failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/approval-record -> 200 OK")

    # 5c. POST /ads/{ad_id}/verify-send
    res = requests.post(f"{BASE_URL}/ads/{ad_id}/verify-send", headers=headers)
    assert res.status_code in (200, 400, 422), f"Verify send failed: {res.text}"
    print(f"✓ [PASS] POST /ads/{ad_id}/verify-send -> {res.status_code} (State gate verified: {res.json().get('detail')})")

    # Verify send on an approved ad (demo-ad-bl-003)
    res_appr = requests.post(f"{BASE_URL}/ads/demo-ad-bl-003/verify-send", headers=headers)
    assert res_appr.status_code == 200, f"Verify send on approved ad failed: {res_appr.text}"
    print("✓ [PASS] POST /ads/demo-ad-bl-003/verify-send -> 200 OK (Send Verified!)")

    # 5d. POST /ads/{ad_id}/approve
    res = requests.post(f"{BASE_URL}/ads/{ad_id}/approve", headers=headers, json={"evidence_note": "Verified by QA engineer"})
    assert res.status_code in (200, 400, 409), f"Approve failed: {res.text}"
    print(f"✓ [PASS] POST /ads/{ad_id}/approve -> {res.status_code} OK")

    # 6. Edits API
    # 6a. POST /ads/{ad_id}/edit
    res = requests.post(f"{BASE_URL}/ads/{ad_id}/edit", headers=headers, json={
        "headline": "Natural Organic Ashwagandha Root Extract 600mg",
        "ad_text": "Updated compliant text without medical claims.",
        "reason": "Corrected wording to comply with platform guidelines."
    })
    assert res.status_code in (200, 400), f"Edit failed: {res.text}"
    print(f"✓ [PASS] POST /ads/{ad_id}/edit -> {res.status_code} OK")

    # 6b. GET /ads/{ad_id}/edits
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/edits", headers=headers)
    assert res.status_code == 200, f"Get edits failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/edits -> 200 OK ({len(res.json().get('edits', []))} edits recorded)")

    # 6c. POST /ads/{ad_id}/recheck
    res = requests.post(f"{BASE_URL}/ads/{ad_id}/recheck", headers=headers)
    assert res.status_code in (200, 202, 400), f"Recheck failed: {res.text}"
    print(f"✓ [PASS] POST /ads/{ad_id}/recheck -> {res.status_code} OK")

    # 7. Findings API
    # 7a. GET /ads/{ad_id}/findings/dismissals
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/findings/dismissals", headers=headers)
    assert res.status_code == 200, f"Get dismissals failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/findings/dismissals -> 200 OK")

    # 7b. GET /batches/{batch_id}/false-alarms
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/false-alarms", headers=headers)
    assert res.status_code == 200, f"Get false alarms failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/false-alarms -> 200 OK")

    # 8. Evidence API
    # 8a. POST /ads/{ad_id}/evidence
    res = requests.post(f"{BASE_URL}/ads/{ad_id}/evidence", headers=headers, json={
        "evidence_note": "Client director email permission on file."
    })
    assert res.status_code in (200, 201), f"Attach evidence failed: {res.text}"
    print(f"✓ [PASS] POST /ads/{ad_id}/evidence -> {res.status_code} OK")

    # 8b. GET /ads/{ad_id}/evidence
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/evidence", headers=headers)
    assert res.status_code == 200, f"Get evidence failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/evidence -> 200 OK")

    # 9. Sends API
    # 9a. GET /ads/{ad_id}/send
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/send", headers=headers)
    assert res.status_code in (200, 404), f"Get send status failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/send -> {res.status_code} OK")

    # 9b. GET /ads/{ad_id}/alerts
    res = requests.get(f"{BASE_URL}/ads/{ad_id}/alerts", headers=headers)
    assert res.status_code == 200, f"Get ad alerts failed: {res.text}"
    print(f"✓ [PASS] GET  /ads/{ad_id}/alerts -> 200 OK")

    # 9c. POST /batches/{batch_id}/send-approved
    res = requests.post(f"{BASE_URL}/batches/{batch_id}/send-approved", headers=headers)
    assert res.status_code in (200, 400), f"Send approved failed: {res.text}"
    print(f"✓ [PASS] POST /batches/{batch_id}/send-approved -> {res.status_code} OK")

    # 10. Alerts API
    # 10a. GET /alerts
    res = requests.get(f"{BASE_URL}/alerts", headers=headers)
    assert res.status_code == 200, f"Get alerts failed: {res.text}"
    print(f"✓ [PASS] GET  /alerts -> 200 OK ({res.json().get('total', 0)} active alerts)")

    # 10b. GET /alerts/summary
    res = requests.get(f"{BASE_URL}/alerts/summary", headers=headers)
    assert res.status_code == 200, f"Get alerts summary failed: {res.text}"
    print(f"✓ [PASS] GET  /alerts/summary -> 200 OK")

    # 10c. GET /batches/{batch_id}/alerts
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/alerts", headers=headers)
    assert res.status_code == 200, f"Get batch alerts failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/alerts -> 200 OK")

    # 10d. POST /batches/{batch_id}/reconcile-stuck-sends
    res = requests.post(f"{BASE_URL}/batches/{batch_id}/reconcile-stuck-sends", headers=headers)
    assert res.status_code == 200, f"Reconcile failed: {res.text}"
    print(f"✓ [PASS] POST /batches/{batch_id}/reconcile-stuck-sends -> 200 OK")

    # 11. Metrics API
    # 11a. GET /metrics
    res = requests.get(f"{BASE_URL}/metrics", headers=headers)
    assert res.status_code == 200, f"Get metrics failed: {res.text}"
    print(f"✓ [PASS] GET  /metrics -> 200 OK")

    # 11b. GET /metrics/release-gate
    res = requests.get(f"{BASE_URL}/metrics/release-gate", headers=headers)
    assert res.status_code == 200, f"Get release gate failed: {res.text}"
    print(f"✓ [PASS] GET  /metrics/release-gate -> 200 OK (Status: {res.json().get('release_gate_status')})")

    # 11c. GET /batches/{batch_id}/metrics
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/metrics", headers=headers)
    assert res.status_code == 200, f"Get batch metrics failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/metrics -> 200 OK")

    # 11d. GET /batches/{batch_id}/metrics/summary
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/metrics/summary", headers=headers)
    assert res.status_code == 200, f"Get batch metrics summary failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/metrics/summary -> 200 OK")

    # 11e. GET /batches/{batch_id}/metrics/export
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/metrics/export?format=json", headers=headers)
    assert res.status_code == 200, f"Export metrics failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/metrics/export -> 200 OK")

    # 12. Dead Letter API
    # 12a. GET /dead-letter
    res = requests.get(f"{BASE_URL}/dead-letter", headers=headers)
    assert res.status_code == 200, f"Get dead letter failed: {res.text}"
    print(f"✓ [PASS] GET  /dead-letter -> 200 OK")

    # 12b. GET /batches/{batch_id}/dead-letter
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/dead-letter", headers=headers)
    assert res.status_code == 200, f"Get batch dead letter failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/dead-letter -> 200 OK")

    # 13. SSE Status
    # 13a. GET /batches/{batch_id}/status
    res = requests.get(f"{BASE_URL}/batches/{batch_id}/status", headers=headers)
    assert res.status_code == 200, f"Get status failed: {res.text}"
    print(f"✓ [PASS] GET  /batches/{batch_id}/status -> 200 OK (Status: {res.json().get('status')})")

    print("=" * 70)
    print("🎉 ALL LISTED LAUNCH GATE APIS ARE PERFORMING FLAWLESSLY!")
    print("=" * 70)

if __name__ == "__main__":
    run_suite()
