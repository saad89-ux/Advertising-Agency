"""Safely purge seeded demo data from the database.

Dry-run by default to ensure no accidental data loss.
Pass --confirm to permanently delete identified demo rows.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from apps.api.database import get_session


def find_demo_records(conn: Any) -> dict[str, list[Any]]:
    records: dict[str, list[Any]] = {}

    # Demo batches
    records["batches"] = conn.execute(
        "SELECT batch_id, filename, created_at FROM batches WHERE batch_id LIKE 'demo-%' OR is_sample = TRUE"
    ).fetchall()
    demo_batch_ids = [b[0] for b in records["batches"]]

    # Demo ads
    records["ads"] = conn.execute(
        """SELECT ad_id, batch_id, client_id, headline 
           FROM ads 
           WHERE is_sample = TRUE OR ad_id LIKE 'demo-%' OR batch_id LIKE 'demo-%'"""
    ).fetchall()
    demo_ad_ids = [a[0] for a in records["ads"]]

    # Demo platform accounts
    records["platform_accounts"] = conn.execute(
        "SELECT account_id, client_id, platform, handle FROM platform_accounts WHERE is_sample = TRUE OR account_id LIKE 'acc_%'"
    ).fetchall()
    demo_account_ids = [p[0] for p in records["platform_accounts"]]

    # Child rows dependent on ads or batches
    if demo_ad_ids:
        records["findings"] = conn.execute(
            "SELECT count(*) FROM findings WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_approvals"] = conn.execute(
            "SELECT count(*) FROM ad_approvals WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["approval_history"] = conn.execute(
            "SELECT count(*) FROM approval_history WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["approval_evidence"] = conn.execute(
            "SELECT count(*) FROM approval_evidence WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_edits"] = conn.execute(
            "SELECT count(*) FROM ad_edits WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["send_alerts"] = conn.execute(
            "SELECT count(*) FROM send_alerts WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_sends"] = conn.execute(
            "SELECT count(*) FROM ad_sends WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_job_outbox"] = conn.execute(
            "SELECT count(*) FROM ad_job_outbox WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_performance_tracking"] = conn.execute(
            "SELECT count(*) FROM ad_performance_tracking WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
        records["ad_live_dispatches"] = conn.execute(
            "SELECT count(*) FROM ad_live_dispatches WHERE ad_id = ANY(%s)", (demo_ad_ids,)
        ).fetchone()[0]
    else:
        for k in [
            "findings", "ad_approvals", "approval_history", "approval_evidence",
            "ad_edits", "send_alerts", "ad_sends", "ad_job_outbox",
            "ad_performance_tracking", "ad_live_dispatches",
        ]:
            records[k] = 0

    if demo_batch_ids:
        records["batch_rejected_rows"] = conn.execute(
            "SELECT count(*) FROM batch_rejected_rows WHERE batch_id = ANY(%s)", (demo_batch_ids,)
        ).fetchone()[0]
        records["sheet_claims"] = conn.execute(
            "SELECT count(*) FROM sheet_claims WHERE batch_id = ANY(%s)", (demo_batch_ids,)
        ).fetchone()[0]
        records["campaign_launch_contacts"] = conn.execute(
            "SELECT count(*) FROM campaign_launch_contacts WHERE batch_id = ANY(%s)", (demo_batch_ids,)
        ).fetchone()[0]
    else:
        records["batch_rejected_rows"] = 0
        records["sheet_claims"] = 0
        records["campaign_launch_contacts"] = 0

    return records


def purge_demo_data(confirm: bool = False) -> None:
    with get_session() as conn:
        records = find_demo_records(conn)

        print("=" * 60)
        print("LAUNCHGATE DEMO DATA PURGE AUDIT")
        print("=" * 60)
        print(f"Batches to purge:           {len(records['batches'])}")
        for b in records["batches"]:
            print(f"  - {b[0]} ({b[1]})")
        print(f"Ads to purge:               {len(records['ads'])}")
        print(f"Platform accounts to purge: {len(records['platform_accounts'])}")
        for p in records["platform_accounts"]:
            print(f"  - {p[0]} ({p[1]} {p[2]} {p[3]})")
        print("-" * 60)
        print("Dependent Child Records:")
        for tbl in [
            "findings", "ad_approvals", "approval_history", "approval_evidence",
            "ad_edits", "send_alerts", "ad_sends", "ad_job_outbox",
            "ad_performance_tracking", "ad_live_dispatches",
            "batch_rejected_rows", "sheet_claims", "campaign_launch_contacts",
        ]:
            print(f"  - {tbl:<25} {records[tbl]} rows")
        print("=" * 60)

        if not confirm:
            print("\n[DRY RUN] No changes were made to the database.")
            print("To delete these records permanently, run:")
            print("  python scripts/purge_demo_data.py --confirm\n")
            return

        print("\n[EXECUTING PURGE] Deleting demo records inside a transaction...")
        demo_ad_ids = [a[0] for a in records["ads"]]
        demo_batch_ids = [b[0] for b in records["batches"]]
        demo_account_ids = [p[0] for p in records["platform_accounts"]]

        if demo_ad_ids:
            conn.execute("DELETE FROM findings WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_approvals WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM approval_history WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM approval_evidence WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_edits WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM send_alerts WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_sends WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_job_outbox WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_performance_tracking WHERE ad_id = ANY(%s)", (demo_ad_ids,))
            conn.execute("DELETE FROM ad_live_dispatches WHERE ad_id = ANY(%s)", (demo_ad_ids,))

        if demo_batch_ids:
            conn.execute("DELETE FROM batch_rejected_rows WHERE batch_id = ANY(%s)", (demo_batch_ids,))
            conn.execute("DELETE FROM sheet_claims WHERE batch_id = ANY(%s)", (demo_batch_ids,))
            conn.execute("DELETE FROM campaign_launch_contacts WHERE batch_id = ANY(%s)", (demo_batch_ids,))
            conn.execute("DELETE FROM ad_job_outbox WHERE batch_id = ANY(%s)", (demo_batch_ids,))

        if demo_ad_ids:
            conn.execute("DELETE FROM ads WHERE ad_id = ANY(%s)", (demo_ad_ids,))

        if demo_batch_ids:
            conn.execute("DELETE FROM batches WHERE batch_id = ANY(%s)", (demo_batch_ids,))

        if demo_account_ids:
            conn.execute("DELETE FROM ad_live_dispatches WHERE account_id = ANY(%s)", (demo_account_ids,))
            conn.execute("DELETE FROM platform_accounts WHERE account_id = ANY(%s)", (demo_account_ids,))

        print("[SUCCESS] All identified demo rows successfully purged.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge seeded demo data from LaunchGate DB.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Execute deletion (defaults to dry-run mode without this flag)",
    )
    args = parser.parse_args()
    purge_demo_data(confirm=args.confirm)


if __name__ == "__main__":
    main()
