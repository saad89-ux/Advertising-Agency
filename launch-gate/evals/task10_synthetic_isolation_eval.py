"""SYNTHETIC final evaluation of Task 10 malformed-row isolation.

Run once for the requested all-sheet final comparison with:
    python evals/task10_synthetic_isolation_eval.py --split all

Use --split dev for development checks. Do not tune against held_out results.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.ingest import (
    UploadValidationError,
    parse_csv_upload,
    parse_xlsx_upload,
)


DATA = ROOT / "data"
LABELS = DATA / "labels"
SHEETS = DATA / "labeled_sheets"


def evaluate(split: str) -> tuple[int, int, int, list[str]]:
    if split not in {"dev", "held_out", "all"}:
        raise ValueError("split must be dev, held_out, or all")

    sheet_count = rejected_count = valid_count = 0
    failures: list[str] = []
    for label_path in sorted(LABELS.glob("sheet_*.labels.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        if split != "all" and label.get("split") != split:
            continue
        sheet_count += 1

        sheet_path = SHEETS / label["sheet"]
        content = sheet_path.read_bytes()
        byte_limit = max(1, len(content))
        try:
            if sheet_path.suffix.casefold() == ".csv":
                parsed = parse_csv_upload(
                    sheet_path.name, content, max_file_bytes=byte_limit
                )
            elif sheet_path.suffix.casefold() == ".xlsx":
                parsed = parse_xlsx_upload(
                    sheet_path.name, content, max_file_bytes=byte_limit
                )
            else:
                raise AssertionError(f"unexpected sheet type: {sheet_path.name}")
        except UploadValidationError as exc:
            failures.append(
                f"{label['sheet']}: {exc.reason_code}: {exc}"
            )
            continue

        expected_rejected_ids = {
            str(row["row_id"])
            for row in label["rows"]
            if row.get("malformed") is True
        }
        actual_rejected_ids = {
            str(row.row_number) for row in parsed.rejected_rows
        }
        if Counter(actual_rejected_ids) != Counter(expected_rejected_ids):
            failures.append(
                f"{label['sheet']}: expected malformed row ids "
                f"{sorted(expected_rejected_ids)}, got {sorted(actual_rejected_ids)}"
            )
        expected_valid_ids = {
            str(row["row_id"])
            for row in label["rows"]
            if row.get("malformed") is not True
        }
        actual_valid_ids = {str(row["row_number"]) for row in parsed.rows}
        if Counter(actual_valid_ids) != Counter(expected_valid_ids):
            failures.append(f"{label['sheet']}: valid row ids differ from the labels")
        expected_total = len(label["rows"])
        if len(parsed.rows) != expected_total - len(expected_rejected_ids):
            failures.append(
                f"{label['sheet']}: expected {expected_total - len(expected_rejected_ids)} "
                f"valid rows, got {len(parsed.rows)}"
            )

        rejected_count += len(parsed.rejected_rows)
        valid_count += len(parsed.rows)

    return sheet_count, valid_count, rejected_count, failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "held_out", "all"), default="dev")
    args = parser.parse_args()
    sheet_count, valid_count, rejected_count, failures = evaluate(args.split)
    phase = "final evaluation" if args.split in {"held_out", "all"} else "development check"
    result = "PASS" if not failures else "FAIL"
    if args.split == "all" and (sheet_count != 30 or rejected_count != 41):
        result = "FAIL"
    print(
        f"SYNTHETIC {phase} ({args.split}): {result}, sheets={sheet_count}, "
        f"valid_ads={valid_count}, isolated_rows={rejected_count}"
    )
    for failure in failures:
        print(f"SYNTHETIC mismatch: {failure}")
    if result == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
