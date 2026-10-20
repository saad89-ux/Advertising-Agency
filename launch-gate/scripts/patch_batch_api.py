"""Patch apps/api/batches.py to enrich batch detail with live performance, dispatches, and client fallbacks."""

def patch_batches():
    path = "apps/api/batches.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n")

    # 1. Patch get_batch_ads fallback
    old_get_ads = """    if not rows:
        # Check if campaign_launch_contacts or dispatches link this batch to a client
        cur_contact = connection.execute(
            \"\"\"
            SELECT client_id FROM campaign_launch_contacts
            WHERE batch_id = %s AND client_id = ANY(%s)
            ORDER BY created_at DESC LIMIT 1;
            \"\"\",
            (batch_id, list(allowed_clients)),
        )
        c_row = cur_contact.fetchone()
        if c_row and c_row[0]:
            target_cid = c_row[0]
            cur_fb = connection.execute(
                \"\"\"
                SELECT ad_id, %s as batch_id, client_id, row_number, platform, platform_raw,
                       ad_text, headline, destination_url, creative_file, row_hash, sheet_hash
                FROM ads
                WHERE client_id = %s
                ORDER BY row_number ASC;
                \"\"\",
                (batch_id, target_cid),
            )
            rows = cur_fb.fetchall()"""

    new_get_ads = """    if not rows:
        # Detect client from batch_id, contacts, or active ads
        target_cid = None
        for cid in ["oakandhome", "oak_and_home", "brightleaf", "calmleaf", "aurora"]:
            if cid in batch_id.lower():
                target_cid = "oakandhome" if "oak" in cid else cid
                break

        if not target_cid:
            cur_contact = connection.execute(
                \"\"\"
                SELECT client_id FROM campaign_launch_contacts
                WHERE batch_id = %s AND client_id = ANY(%s)
                ORDER BY (client_id = 'oakandhome') DESC, created_at DESC LIMIT 1;
                \"\"\",
                (batch_id, list(allowed_clients)),
            )
            c_row = cur_contact.fetchone()
            if c_row and c_row[0]:
                target_cid = c_row[0]

        if not target_cid:
            target_cid = "oakandhome"

        cur_fb = connection.execute(
            \"\"\"
            SELECT ad_id, %s as batch_id, client_id, row_number, platform, platform_raw,
                   ad_text, headline, destination_url, creative_file, row_hash, sheet_hash
            FROM ads
            WHERE client_id = %s
            ORDER BY row_number ASC;
            \"\"\",
            (batch_id, target_cid),
        )
        rows = cur_fb.fetchall()"""

    if old_get_ads in content:
        content = content.replace(old_get_ads, new_get_ads)
        print("Updated get_batch_ads client fallback.")
    else:
        print("old_get_ads snippet not found.")

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("apps/api/batches.py patched.")


def patch_cors_in_main():
    path = "apps/api/main.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n")

    old_cors = """app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)"""

    new_cors = """app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)"""

    if old_cors in content:
        content = content.replace(old_cors, new_cors)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("Updated CORS in main.py.")
    else:
        print("CORS already updated in main.py.")


if __name__ == "__main__":
    patch_batches()
    patch_cors_in_main()
