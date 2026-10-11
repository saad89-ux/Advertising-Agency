# SYNTHETIC data pack (mock). Not from Fernlight.

Everything here is invented by the project owner to stand in for the data pack described in the brief (section 8).
Any result produced from it must be labelled **SYNTHETIC**. It proves the system works; it does not prove the brief's targets.

## What is here, mapped to the brief's list
| Brief item | Location |
|---|---|
| 30 labeled launch sheets (40-200 rows, planted errors) | `labeled_sheets/sheet_01..30` (24 CSV, 6 XLSX), truth in `labels/sheet_XX.labels.json` |
| 3 client brand books and claim registers | `clients/<client>/brand_book.md`, `claims_register.md`; valid citation IDs in `clients/rule_index.json` |
| Platform limits file (partly stale) | `platform_limits.json` (3 stale fields, see `labels/expected_stale_flags.json`) |
| Naming-convention document | `naming_convention.md` and `naming_convention.json` |
| Storefront pages with changing offers | `storefront_pages/` + `serve_storefronts.py` (port 8099) |
| Approval history file | `approval_history.csv` |
| 12 adversarial rows | `adversarial_rows.csv` (+ `labels/adversarial_expected.json`) |
| Stand-in platform with realistic failures | `stand_in_platform/` (FastAPI, port 8100, chaos via `/admin/config`) |
| Extra: creative sizes | `creatives_manifest.csv` (file name to width and height) |
| Extra: link host to client | `clients/domain_map.json` |

## Rules the labels follow
- Verdict of a row: `blocked` if any blocking error (forbidden claim, expired offer, page not found, handmade misuse); else `needs_person` (unknown claim, unknown client); else `fix_needed` (anything else); else `ready`.
- Malformed rows are not ads; they must be isolated, not lost or merged (`expected_verdict: malformed_isolated`).
- `split`: 20 sheets are `dev`, 10 are `held_out` (sheets 5, 8, 10, 13, 15, 17, 20, 22, 25, 30). Every header style appears in both groups. Tune on dev only; report held_out honestly.
- "Meta" in the platform column means facebook. Client is found from the link host (`domain_map.json`). Hosts not in the map have no client rules.
- Mechanical checks use `platform_limits.json` values as truth. Ratio tolerance 2%. Google creative `n/a` or blank is fine; blank elsewhere is an error.
- Today is 2026-10-10 for offer dates.

## Sheet layout
Six different header styles (combined or split text and headline) and varied platform spellings. Links are as typed (scheme often missing). Columns may carry stray spaces.

## Truth you can score against (counts)
Rows: 3260 valid ads, 41 malformed rows. Verdicts: {"blocked": 386, "ready": 1667, "needs_person": 106, "fix_needed": 1101}.
