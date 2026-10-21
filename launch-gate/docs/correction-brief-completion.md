# Launch Gate correction brief — implementation and verification

Date: 21 October 2026. Existing uncommitted corrections were preserved and completed.

## Implemented behavior

- Prompt ads use the ingest row/sheet hash helpers, persist approval records and durable worker jobs, and dispatch the stored URL/copy/creative reference. Launch never borrows another batch's ads.
- The launch authorization records the authenticated human approver and canonical content hash before platform calls. Recorded successful submissions are reused on repeat authorization.
- Launch results distinguish `launched`, `partial`, `blocked`, and `failed`, with an outcome per ad/platform. Blocked attempts record an alert and the actual SMTP result. There is no SMS success claim or invented notification recipient.
- All three input tabs use the same launch submission handler and contact modal. Zero-accepted uploads stop the flow and their newly created empty batch is removed. Creative uploads preserve `file_ref`.
- Preflight uses the existing UTM, naming, text-limit, creative-ratio, and wording checks. It reports `pass`, `fail`, or `not_checked`; it never invents a compliance score or connected account. The UI prevents launching failed preflight checks.
- Dispatch queries use `dispatched_at`. History counts distinct ads, excludes empty/sample batches, respects client assignment, and scopes uploader identity using a shared IP helper. Forwarded chains are trusted only through explicitly configured proxy networks.
- Unhandled errors return JSON with CORS restricted to configured origins; flow errors are logged and returned as string HTTP details. The frontend shares error parsing across operational pages.
- Missing performance values remain null and display as `Not tracked`. Unsubmitted ads do not default to live/approved. Remediation saves edits for checks/human review instead of declaring a fictitious relaunch.
- Automatic demo user/data creation and mock routes were removed. The reviewed purge script was corrected to avoid deleting arbitrary real account IDs, and run successfully; the configured database had no remaining identified demo rows.
- Framework versions are pinned to the lockfile. Source-rewriting patch scripts remain removed. The dotenv import is corrected.

## New source files

- `apps/api/client_ip.py`: uploader IP/proxy normalization.
- `apps/api/preflight.py`: real checks and explicit unavailable-check results.
- `apps/web/lib/api.js`: error parsing and SMTP result labels.
- `scripts/create_user.py`: one-off real account creation, with an interactive password prompt and explicit client assignments.
- `tests/test_correction_brief.py`: isolated PostgreSQL acceptance tests.

These locations follow the API, web, scripts, and tests responsibilities of the existing repository.

## Modified existing files

- API: `campaign_flow.py`, `main.py`, `approvals.py`, `batches.py`, `uploads.py`, `diagnostics.py`, `platforms_schema.py`, `schema.py`, and `users.py`. These affect launch, approval, upload, reporting, history, contacts, and startup behavior.
- Worker: `mcp_client.py` checks the explicit live/write/account configuration before dispatch. The bounded worker architecture and dry-run sender remain in place.
- Web: home, batch review, campaigns, dispatches, remediation, alerts, dead-letter, platforms, review index, and login pages; package manifest and lockfile. These depend on the updated launch/status/contact/error contracts.
- Setup and documentation: `.env.example`, `CLAUDE.md`, `run_local.py`, `init_db.py`, and `scripts/purge_demo_data.py`.
- Existing tests for creative uploads, platform selection, and issue alerts were updated to the explicitly changed contracts: client required, string validation errors, and alerts requested explicitly instead of sent during reads.

## Authorization and assumptions

The owner explicitly approved live Meta launches when a campaign is human-authorized and the required environment flags/credentials are configured. Flags remain off by default; no credentials were changed and no live campaign was submitted during verification. The launch Authorize action is the recorded human approval. Email/phone remain notification metadata, separate from authentication and authorization.

Unavailable model/creative evidence is visibly `not_checked`; it requires human judgment. Remote/video creative dimensions that cannot be verified are never reported as passing. Phone numbers are retained as metadata only.

## Verification

- Baseline full suite: 820 passed, 14 skipped.
- Final full-suite run: 831 passed, 14 skipped. The later partial-metric regression is included in the additional targeted verification below.
- Updated affected tests plus acceptance checks: 48 passed.
- Additional partial-metric checks and related API tests: 33 passed, 1 skipped.
- `npm ci`: succeeded; dependency audit reported zero vulnerabilities.
- Production Next.js build: succeeded.
- Browser smoke checks: prompt blocked/SMTP status, zero-accepted upload stop, creative reference preservation, empty batch/remediation contacts, and absence of browser runtime errors passed.
- Fresh-schema endpoint sweep: all endpoints listed in the brief returned 200; removed mock seed route returned 404.
- `git diff --check`: passed. Personal notification fallback and fabricated SMS/preflight claims are absent from source/tests/scripts/docs/data.

Runtime logs and the browser smoke script are under ignored `storage/verification/`. Live Meta delivery, SMTP delivery, and a complete Redis-worker/browser-page-check run were not exercised. No deployment or Git push was performed. Architecture and existing page/route names are retained.

## Follow-up: email-only popup and automatic client resolution

The owner removed phone collection and manual client selection from the launch flow. Home no longer contains the connection labels, headline/copy/URL inputs, or brand/client selection strip. Review and Launch opens the form immediately. The popup collects notification email, platforms, and any required campaign details. Phone is neither required nor sent by the UI; new contact records store an empty legacy phone column.

Clients are resolved from authorized account assignments, stored batch ads, the landing URL, or an unambiguous client name in the prompt. The system does not invent a client or select an arbitrary account when multiple clients match. Authentication failures remain in the popup with a sign-in link and preserve the entered notification email. The navbar now exposes sign-in/sign-out and reports actual API health; the history count is read from saved records.

Follow-up verification: full suite 834 passed / 14 skipped; 51 affected tests passed; 15 acceptance tests including real JWT authentication and PostgreSQL persistence passed. A separate real Redis queue/worker test also passed and persisted mechanical findings for a prompt-created ad in an isolated test schema and queue. No test rows were added to the production history, and no live Meta writes or SMTP messages were sent. The live environment reports Meta not configured, SMTP configured, and Redis reachable.

The final production build was restarted on port 3000 and the updated API on port 8000. The actual browser was refreshed and verified with the email-only popup open; a screenshot is saved as `storage/verification/email-only-popup.png`.

Follow-up role mismatch: the sheet upload route was buyer-only even though campaign launch permits buyers and account managers. The upload role gate now permits both. Verified with 62 passing upload/authentication/acceptance tests, including a real JWT account-manager sheet → client resolution → preflight → blocked launch → batch review flow, and an unassigned-client denial. No user account roles were changed.
