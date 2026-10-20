"""Purge all fake batch history and configure dynamic IP-based sidebar batch tracking."""

import os
from apps.api.database import get_session

def purge_all_batch_history():
    with get_session() as conn:
        with conn.cursor() as cur:
            # 1. Add uploader_ip column to batches and contacts
            cur.execute("""
                ALTER TABLE batches ADD COLUMN IF NOT EXISTS uploader_ip VARCHAR(64) DEFAULT '127.0.0.1';
            """)
            cur.execute("""
                ALTER TABLE campaign_launch_contacts ADD COLUMN IF NOT EXISTS uploader_ip VARCHAR(64) DEFAULT '127.0.0.1';
            """)

            # 2. Purge all prior batch history
            cur.execute("DELETE FROM ad_performance_tracking;")
            cur.execute("DELETE FROM ad_live_dispatches;")
            cur.execute("DELETE FROM ad_approvals;")
            cur.execute("DELETE FROM ad_sends;")
            cur.execute("DELETE FROM batch_rejected_rows;")
            cur.execute("DELETE FROM findings;")
            cur.execute("DELETE FROM campaign_launch_contacts;")
            cur.execute("DELETE FROM ads;")
            cur.execute("DELETE FROM batches;")

        conn.commit()
    print("Successfully purged all prior batch history from database and added uploader_ip column.")


def patch_campaign_flow_ip_tracking():
    path = "apps/api/campaign_flow.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n")

    # 1. Update launch_batch signature to accept request: Request
    old_launch_sig = """def launch_batch(
    req: LaunchBatchRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:"""

    new_launch_sig = """def launch_batch(
    req: LaunchBatchRequest,
    request: Request,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:"""

    if old_launch_sig in content:
        content = content.replace(old_launch_sig, new_launch_sig)
        print("Updated launch_batch signature with Request.")

    # 2. Extract client_ip and save to batches and contacts
    old_batch_insert = """                cur.execute(
                    \"\"\"
                    INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (batch_id) DO NOTHING;
                    \"\"\",
                    (actual_batch_id, user.user_id, f"{req.client_id}_campaign.csv", f"hash_{uuid.uuid4().hex[:8]}"),
                )"""

    new_batch_insert = """                client_ip = (
                    request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                    or request.headers.get("x-real-ip", "").strip()
                    or (request.client.host if request.client else "127.0.0.1")
                )
                cur.execute(
                    \"\"\"
                    INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash, uploader_ip)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (batch_id) DO UPDATE SET uploader_ip = EXCLUDED.uploader_ip;
                    \"\"\",
                    (actual_batch_id, user.user_id, f"{req.client_id}_campaign.csv", f"hash_{uuid.uuid4().hex[:8]}", client_ip),
                )"""

    if old_batch_insert in content:
        content = content.replace(old_batch_insert, new_batch_insert)
        print("Updated batch insert with client_ip.")

    # 3. Also record uploader_ip in campaign_launch_contacts
    old_contact_insert = """            cur.execute(
                \"\"\"
                INSERT INTO campaign_launch_contacts (
                    contact_id, batch_id, client_id, employee_email, employee_phone
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (contact_id) DO NOTHING;
                \"\"\",
                (contact_id, actual_batch_id, req.client_id, req.employee_email, req.employee_phone),
            )"""

    new_contact_insert = """            client_ip = (
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or request.headers.get("x-real-ip", "").strip()
                or (request.client.host if request.client else "127.0.0.1")
            )
            cur.execute(
                \"\"\"
                INSERT INTO campaign_launch_contacts (
                    contact_id, batch_id, client_id, employee_email, employee_phone, uploader_ip
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (contact_id) DO NOTHING;
                \"\"\",
                (contact_id, actual_batch_id, req.client_id, req.employee_email, req.employee_phone, client_ip),
            )"""

    if old_contact_insert in content:
        content = content.replace(old_contact_insert, new_contact_insert)
        print("Updated contact insert with client_ip.")

    # 4. Update get_chat_history to filter dynamically by client IP
    old_chat_history = """@router.get("/chat-history")
def get_chat_history(
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    \"\"\"Return historical batch sessions for the left chat sidebar.\"\"\"
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                \"\"\"
                SELECT b.batch_id,
                       COALESCE(MAX(a.client_id), MAX(c.client_id), '') as client_id,
                       COUNT(a.ad_id) as ad_count,
                       b.created_at,
                       MAX(c.employee_email) as employee_email
                FROM batches b
                LEFT JOIN ads a ON b.batch_id = a.batch_id
                LEFT JOIN campaign_launch_contacts c ON b.batch_id = c.batch_id
                GROUP BY b.batch_id, b.created_at
                ORDER BY b.created_at DESC
                LIMIT 20;
                \"\"\",
            )
            rows = cur.fetchall()"""

    new_chat_history = """@router.get("/chat-history")
def get_chat_history(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    \"\"\"Return real historical batch sessions for the left chat sidebar scoped dynamically by user IP.\"\"\"
    init_flow_tables()
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.headers.get("x-real-ip", "").strip()
        or (request.client.host if request.client else "127.0.0.1")
    )
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                \"\"\"
                SELECT b.batch_id,
                       COALESCE(MAX(a.client_id), MAX(c.client_id), '') as client_id,
                       COUNT(a.ad_id) as ad_count,
                       b.created_at,
                       MAX(c.employee_email) as employee_email
                FROM batches b
                LEFT JOIN ads a ON b.batch_id = a.batch_id
                LEFT JOIN campaign_launch_contacts c ON b.batch_id = c.batch_id
                WHERE b.uploader_ip = %s
                   OR (%s IN ('127.0.0.1', '::1', 'localhost') AND (b.uploader_ip IS NULL OR b.uploader_ip IN ('127.0.0.1', '::1', 'localhost')))
                GROUP BY b.batch_id, b.created_at
                ORDER BY b.created_at DESC
                LIMIT 25;
                \"\"\",
                (client_ip, client_ip),
            )
            rows = cur.fetchall()"""

    if old_chat_history in content:
        content = content.replace(old_chat_history, new_chat_history)
        print("Updated get_chat_history with dynamic IP filtering.")

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("apps/api/campaign_flow.py updated.")


if __name__ == "__main__":
    purge_all_batch_history()
    patch_campaign_flow_ip_tracking()
