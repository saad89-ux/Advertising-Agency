"""SYNTHETIC dev-split evaluation for Task 25 page offer comparisons."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date
import json
from pathlib import Path
import tempfile
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.worker.file_storage import LocalFileStore
from apps.worker.page_text_capture import capture_page_snapshot
from apps.worker.page_offer_check import check_offer_price_dates
from packages.contracts import AdContract


LABEL_ROOT = REPO_ROOT / "data" / "data" / "labels"
PAGES_INDEX = REPO_ROOT / "data" / "storefront_pages" / "pages_index.json"
AS_OF = date(2026, 10, 11)
TARGET_TYPES = {
    "page_expired_offer": "page_offer.expired",
    "page_discount_mismatch": "page_offer.discount_mismatch",
    "page_delivery_threshold_mismatch": "page_offer.threshold_mismatch",
}


def _load_dev_rows() -> tuple[list[dict], dict[tuple[str, str], dict]]:
    page_index = json.loads(PAGES_INDEX.read_text(encoding="utf-8"))
    pages = {
        (page["client"], page["path"].strip("/")): page
        for page in page_index["pages"]
    }
    rows: list[dict] = []
    for label_path in sorted(LABEL_ROOT.glob("sheet_*.labels.json")):
        labels = json.loads(label_path.read_text(encoding="utf-8"))
        if labels.get("split") != "dev":
            continue
        for row in labels.get("rows", []):
            if row.get("malformed"):
                continue
            truth = row.get("truth") or {}
            page = pages.get((row.get("client", ""), str(row.get("page", "")).strip("/")))
            if not page:
                continue
            target_types = {
                error.get("type")
                for error in row.get("planted_errors", [])
                if error.get("type") in TARGET_TYPES
            }
            rows.append(
                {
                    "sheet": labels["sheet"],
                    "row_id": row["row_id"],
                    "client": row.get("client"),
                    "truth": truth,
                    "page": page,
                    "target_types": target_types,
                }
            )
    return rows, pages


async def _evaluate(browser_channel: str | None) -> dict:
    rows, _ = _load_dev_rows()
    pages_by_url = {row["page"]["local_url"]: row["page"] for row in rows}
    snapshots: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="launch-gate-task25-") as temp_storage:
        store = LocalFileStore(temp_storage)
        for url in sorted(pages_by_url):
            capture = await capture_page_snapshot(
                url,
                allowed_origins={"http://localhost:8099"},
                timeout_ms=8000,
                file_store=store,
                browser_channel=browser_channel,
            )
            if capture.page_error is not None:
                snapshots[url] = {
                    "loaded": False,
                    "http_status": None,
                    "final_url": url,
                    "visible_text": "",
                    "screenshot_ref": None,
                    "text_ref": None,
                }
            else:
                snapshots[url] = capture.page_snapshot

    counts = Counter()
    mismatch_examples: list[str] = []
    for item in rows:
        truth = item["truth"]
        if not truth.get("link"):
            continue
        ad = AdContract(
            ad_id=f"{item['sheet']}:{item['row_id']}",
            batch_id=f"eval:{item['sheet']}",
            client_id=item["client"],
            row_number=int(item["row_id"]),
            platform=str(truth.get("platform") or "unknown"),
            ad_text=truth.get("text") or None,
            headline=truth.get("headline") or None,
            destination_url=str(truth["link"]),
            row_hash="synthetic-eval-row-hash",
            sheet_hash="synthetic-eval-sheet-hash",
        )
        snapshot = snapshots[item["page"]["local_url"]]
        found = {
            finding.rule_id
            for finding in check_offer_price_dates(ad, snapshot, as_of=AS_OF)
        }
        expected = {TARGET_TYPES[label] for label in item["target_types"]}
        counts["rows"] += 1
        if expected:
            counts["target_rows"] += 1
            matched = bool(expected & found)
            counts["target_hits"] += matched
            if not matched and len(mismatch_examples) < 12:
                mismatch_examples.append(
                    f"{item['sheet']} row {item['row_id']}: expected {sorted(expected)}, got {sorted(found)}"
                )
        elif snapshot["loaded"] and snapshot["http_status"] != 404:
            counts["negative_rows"] += 1
            if found:
                counts["false_alarm_rows"] += 1
                if len(mismatch_examples) < 12:
                    mismatch_examples.append(
                        f"{item['sheet']} row {item['row_id']}: unexpected {sorted(found)}"
                    )

    target_count = counts["target_rows"]
    negative_count = counts["negative_rows"]
    return {
        "label": "SYNTHETIC",
        "split": "dev",
        "as_of": AS_OF.isoformat(),
        "rows_scored": counts["rows"],
        "target_rows": target_count,
        "target_hits": counts["target_hits"],
        "found_rate": counts["target_hits"] / target_count if target_count else None,
        "negative_rows": negative_count,
        "false_alarm_rows": counts["false_alarm_rows"],
        "false_alarm_rate": counts["false_alarm_rows"] / negative_count if negative_count else None,
        "captured_pages": len(snapshots),
        "pages_loaded": sum(page["loaded"] for page in snapshots.values()),
        "js_built_pages": sorted(
            page["local_url"] for page in pages_by_url.values() if page.get("js_built")
        ),
        "mismatch_examples": mismatch_examples,
        "finding_type_mapping": TARGET_TYPES,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    result = asyncio.run(_evaluate(args.browser_channel or None))
    print("SYNTHETIC Task 25 dev evaluation")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
