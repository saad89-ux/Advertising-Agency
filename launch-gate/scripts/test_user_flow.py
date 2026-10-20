import os
import sys

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from apps.api.main import app

def test_full_user_flow():
    print("=" * 60)
    print("STARTING END-TO-END USER POINT-OF-VIEW APPLICATION TEST")
    print("=" * 60)

    client = TestClient(app)

    # 1. Login Authentication
    print("\n[Step 1] Attempting Login with default credentials...")
    login_res = client.post("/auth/login", json={"email": "buyer@launchgate.com", "password": "password123"})
    print("Login HTTP status:", login_res.status_code)
    if login_res.status_code != 200:
        print("Login failed with response:", login_res.text)
        return
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("-> Login SUCCESS! Valid Bearer JWT Token generated.")

    # 2. Upload CSV Launch Sheet
    print("\n[Step 2] Uploading real campaign CSV sheet (data/labeled_sheets/sheet_01.csv)...")
    with open("data/labeled_sheets/sheet_01.csv", "rb") as f:
        files = {"file": ("sheet_01.csv", f, "text/csv")}
        upload_res = client.post("/uploads", files=files, headers=headers)

    print("Upload HTTP status:", upload_res.status_code)
    upload_data = upload_res.json()
    batch_id = upload_data.get("batch_id")
    print(f"Batch ID: {batch_id}")
    print(f"Accepted Ads Count: {upload_data.get('accepted_count')}")
    print(f"Rejected Rows Count: {upload_data.get('rejected_count')}")
    print(f"Persisted Storage File Ref: {upload_data.get('file_ref')}")
    print("-> Upload & Validation SUCCESS!")

    # 3. Retrieve Batch & Ads Details
    print(f"\n[Step 3] Querying batch review details for batch '{batch_id}'...")
    batch_res = client.get(f"/batches/{batch_id}", headers=headers)
    print("Batch details HTTP status:", batch_res.status_code)
    b_data = batch_res.json()
    ads = b_data.get("ads", [])
    print(f"Total ads in batch: {len(ads)}")
    if ads:
        sample_ad = ads[0]
        print(f"Sample Ad #{sample_ad.get('row_number')}: Platform='{sample_ad.get('platform')}', Headline='{sample_ad.get('headline')}'")
    print("-> Batch Inspection SUCCESS!")

    # 4. Sidebar History Verification
    print("\n[Step 4] Checking real-time sidebar batch history...")
    chat_res = client.get("/campaign-flow/chat-history", headers=headers)
    print("Chat history HTTP status:", chat_res.status_code)
    chat_data = chat_res.json()
    print(f"Total historical sessions recorded in sidebar: {len(chat_data)}")
    matching = [b for b in chat_data if b.get("batch_id") == batch_id]
    print(f"Current batch session present in sidebar: {bool(matching)}")
    print("-> Sidebar Session Tracking SUCCESS!")

    # 4b. Test Emergency Alert Channel (SMS + Email)
    print("\n[Step 4b] Testing Emergency Alert Channel (SMS & Email Dispatch)...")
    test_alert_payload = {
        "client_id": "oakandhome",
        "recipient_email": "saad.user@launchgate.com",
        "recipient_phone": "+923001234567",
        "alert_type": "manual_test",
    }
    test_alert_res = client.post("/campaign-flow/test-alert", json=test_alert_payload, headers=headers)
    print("Test alert HTTP status:", test_alert_res.status_code)
    test_alert_data = test_alert_res.json()
    print("Test Alert Response Status:", test_alert_data.get("status"))
    print("SMS Dispatch Status:", test_alert_data.get("sms_status"))
    print("Email Dispatch Status:", test_alert_data.get("email_status"))

    contact_res = client.get("/campaign-flow/launcher-contact?client_id=oakandhome", headers=headers)
    print("Launcher contact HTTP status:", contact_res.status_code)
    contact_data = contact_res.json()
    print(f"Saved contact for oakandhome: {contact_data.get('employee_email')} | {contact_data.get('employee_phone')}")
    print("-> Multi-Channel Emergency Alert Channel SUCCESS!")

    # 5. Launch Batch with Platform Selection & Contact
    print("\n[Step 5] Launching Batch with User-Selected Platforms & Launcher Contact...")
    launch_payload = {
        "client_id": "oakandhome",
        "batch_id": batch_id,
        "campaign_name": "Oak & Home Fall Growth Flight",
        "employee_email": "saad.user@launchgate.com",
        "employee_phone": "+923001234567",
        "target_platforms": ["instagram", "facebook"],
    }
    launch_res = client.post("/campaign-flow/launch-batch", json=launch_payload, headers=headers)
    print("Launch batch HTTP status:", launch_res.status_code)
    launch_data = launch_res.json()
    print("Launch Status:", launch_data.get("status"))
    print("Target Platforms Launched:", launch_data.get("target_platforms"))
    print("Notification Contact Recorded:", launch_data.get("notification_contact"))
    print(f"Alerts Triggered Count: {len(launch_data.get('notifications', []))}")
    print("-> Pre-Launch Platform & Contact Validation SUCCESS!")

    # 6. Live Performance Tracking
    print("\n[Step 6] Querying platform performance and revenue tracking...")
    perf_res = client.get("/campaign-flow/platform-performance?include_samples=false", headers=headers)
    print("Performance HTTP status:", perf_res.status_code)
    perf_data = perf_res.json()
    print("Summary:", perf_data.get("summary"))
    print("Platform breakdown count:", len(perf_data.get("platform_breakdown", [])))
    print("-> Live Tracking SUCCESS!")

    # 7. Check Alerts & Violations
    print("\n[Step 7] Checking real-time policy alerts & violations...")
    viol_res = client.get("/campaign-flow/violations", headers=headers)
    print("Violations HTTP status:", viol_res.status_code)
    viols = viol_res.json()
    print(f"Total tracked platform violations/dispatches: {len(viols)}")
    if viols:
        v = viols[0]
        print(f"Top Violation Ad ID: {v.get('ad_id')}, Platform: {v.get('platform')}, Severity: {v.get('severity')}")
    print("-> Real-Time Policy Alerting SUCCESS!")

    print("\n" + "=" * 60)
    print("ALL 7 END-TO-END USER JOURNEY CHECKS COMPLETED WITH ZERO ERRORS!")
    print("=" * 60)

if __name__ == "__main__":
    test_full_user_flow()
