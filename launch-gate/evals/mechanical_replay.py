"""SYNTHETIC evaluation of mechanical rules on labeled sheets.

Scores UTM presence and naming, platform text and headline limits,
and creative dimensions, aspect ratio, and missing creatives.

Usage:
    python evals/mechanical_replay.py [--split dev]
    python evals/mechanical_replay.py --split held_out --final
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.client_inference import infer_client_id, load_domain_map
from apps.api.ingest import parse_csv_upload, parse_xlsx_upload
from packages.contracts import AdContract
from packages.rules.creative_size_ratio import check_creative_size_ratio
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.utm_naming import check_utm_naming
from packages.rules.utm_presence import check_utm_presence


def _load_manifest_and_limits(repo_root: Path):
    manifest_path = repo_root / "data" / "creatives_manifest.csv"
    with manifest_path.open(newline="", encoding="utf-8") as source:
        manifest = {
            row["file"]: {"width": int(row["width"]), "height": int(row["height"])}
            for row in csv.DictReader(source)
        }
    limits_path = repo_root / "data" / "platform_limits.json"
    limits = json.loads(limits_path.read_text(encoding="utf-8"))["platforms"]
    domain_map_path = repo_root / "data" / "clients" / "domain_map.json"
    domain_map = load_domain_map(domain_map_path)
    return manifest, limits, domain_map


def run_mechanical_replay(
    split: str = "dev",
    *,
    is_final: bool = False,
    repo_root: Path | None = None,
) -> dict:
    root = repo_root or REPO_ROOT

    if split in {"held_out", "all"} and not is_final:
        raise ValueError("held_out evaluation requires --final flag")

    if is_final:
        print("WARNING: held_out final evaluation must be run only once.")

    manifest, limits, domain_map = _load_manifest_and_limits(root)

    planted_counts: Counter[str] = Counter()
    detected_counts: Counter[str] = Counter()
    clean_rows_total = 0
    clean_rows_false_alarms = 0
    sheets_evaluated = 0
    total_valid_rows = 0

    labels_dir = root / "data" / "labels"
    sheets_dir = root / "data" / "labeled_sheets"

    for label_path in sorted(labels_dir.glob("sheet_*.labels.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        if split != "all" and label.get("split") != split:
            continue

        sheets_evaluated += 1
        sheet_name = label["sheet"]
        sheet_path = sheets_dir / sheet_name
        content = sheet_path.read_bytes()

        if sheet_path.suffix.casefold() == ".csv":
            parsed = parse_csv_upload(sheet_name, content, max_file_bytes=len(content))
        elif sheet_path.suffix.casefold() == ".xlsx":
            parsed = parse_xlsx_upload(sheet_name, content, max_file_bytes=len(content))
        else:
            raise ValueError(f"unsupported sheet format: {sheet_name}")

        parsed_rows_by_id = {row["row_number"]: row for row in parsed.rows}

        for row_info in label["rows"]:
            if row_info.get("malformed"):
                continue
            row_id = str(row_info["row_id"])
            if row_id not in parsed_rows_by_id:
                continue

            total_valid_rows += 1
            p_row = parsed_rows_by_id[row_id]
            link = p_row.get("destination_url", "")
            client_id = row_info.get("client") or infer_client_id(link, domain_map) or "unknown"

            ad = AdContract(
                ad_id=f"{sheet_name}:{row_id}",
                batch_id=f"replay:{sheet_name}",
                client_id=client_id,
                row_number=int(row_id),
                platform=p_row.get("platform", ""),
                ad_text=p_row.get("ad_text") or None,
                headline=p_row.get("headline") or None,
                destination_url=link,
                creative_file=p_row.get("creative_file") or None,
                row_hash="synthetic-replay-row-hash",
                sheet_hash="synthetic-replay-sheet-hash",
            )

            findings = []
            findings.extend(check_utm_presence(ad))
            findings.extend(check_utm_naming(ad))
            findings.extend(check_platform_text_limits(ad, limits))
            findings.extend(check_creative_size_ratio(ad, manifest, limits))

            finding_ids = [f.finding_id for f in findings]

            planted_mech = [
                error for error in row_info.get("planted_errors", [])
                if error.get("check") == "mechanical"
            ]

            if not planted_mech:
                clean_rows_total += 1
                if findings:
                    clean_rows_false_alarms += 1
            else:
                for error in planted_mech:
                    etype = error.get("type", "")
                    planted_counts[etype] += 1
                    hit = False

                    if etype == "utm_missing" and any(":utm_presence:" in fid for fid in finding_ids):
                        hit = True
                    elif etype == "utm_misspelled" and any(
                        ":utm_naming:" in fid and ("key" in fid or "utm_source" in fid or "utm_medium" in fid or "order" in fid)
                        for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "utm_campaign_format" and any(
                        ":utm_naming:" in fid and "utm_campaign" in fid for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "utm_malformed_query" and any(
                        ":utm_presence:" in fid or ":utm_naming:" in fid for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "text_too_long" and any(
                        ":platform_limit:ad_text" in fid for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "headline_too_long" and any(
                        ":platform_limit:headline" in fid for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "creative_ratio_mismatch" and any(
                        ":creative:ratio" in fid for fid in finding_ids
                    ):
                        hit = True
                    elif etype == "creative_missing" and any(
                        ":creative:missing" in fid for fid in finding_ids
                    ):
                        hit = True

                    if hit:
                        detected_counts[etype] += 1

    recall_by_type = {
        etype: {
            "planted": planted_counts[etype],
            "detected": detected_counts[etype],
            "recall": (detected_counts[etype] / planted_counts[etype]) if planted_counts[etype] else 1.0,
        }
        for etype in sorted(planted_counts)
    }

    fa_rate = (clean_rows_false_alarms / clean_rows_total) if clean_rows_total else 0.0

    return {
        "label": "SYNTHETIC",
        "split": split,
        "sheets_evaluated": sheets_evaluated,
        "total_valid_rows": total_valid_rows,
        "clean_rows_total": clean_rows_total,
        "clean_rows_false_alarms": clean_rows_false_alarms,
        "false_alarm_rate": fa_rate,
        "recall_by_type": recall_by_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "held_out", "all"), default="dev")
    parser.add_argument("--final", action="store_true", help="Authorize final held_out evaluation run once")
    args = parser.parse_args()

    try:
        results = run_mechanical_replay(split=args.split, is_final=args.final)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print("================================================================================")
    print(f"SYNTHETIC Mechanical Replay Evaluation (split: {results['split']})")
    print("================================================================================")
    print(f"Sheets evaluated : {results['sheets_evaluated']}")
    print(f"Total valid rows : {results['total_valid_rows']}")
    print(f"Clean rows       : {results['clean_rows_total']}")
    print(f"False alarms     : {results['clean_rows_false_alarms']} ({results['false_alarm_rate']*100:.2f}%)")
    print("--------------------------------------------------------------------------------")
    print("Per-Type Recall:")
    for etype, data in results["recall_by_type"].items():
        pct = data["recall"] * 100
        print(f"  {etype:<25} : {data['detected']:>3} / {data['planted']:>3} ({pct:>5.1f}%)")
    print("================================================================================")


if __name__ == "__main__":
    main()
