# Decision Log

## D20 — Malformed upload row isolation

- CSV and XLSX headers use a case-, whitespace-, and punctuation-insensitive alias map in API configuration. The platform, link, and at least one text or headline column are required; a missing header rejects the whole file with `missing_required_columns` and names the missing fields.
- A row-number column is optional. Recognized aliases are Row, #, ad no, id, No., and row number. If present, row values must be positive integers or integral floats; if absent, valid ads use the 1-based data-row index.
- CSV records with a field count different from the header are isolated. In XLSX, a row with a non-empty cell beyond the header width is isolated. XLSX ingestion reads only the first worksheet.
- Trailing fully empty records are ignored. A fully empty record inside the data is isolated as `empty_record` and counts toward data-row indexes.
- Isolation checks row structure and required field values only. It does not judge ad quality, creative, UTMs, platform identity, client host, link scheme, or suspicious/injected text. Whitespace-only cells count as empty.
- The 200-ad limit counts valid ads only. More than 200 valid ads rejects the upload with `too_many_ads`; malformed rows do not count toward the limit.
- Each rejected row carries its 1-based data-row index, raw row number or null, reason code, human-readable reason, and raw cells as inert text truncated to 500 characters per cell. Valid rows continue. Rejected rows must be persisted with their batch.
- An unparseable CSV (including parser errors, unterminated quotes, NUL bytes, or non-UTF-8; UTF-8 BOM is allowed) rejects the whole file. Corrupt, encrypted, or macro-enabled XLSX files reject the whole file as unreadable.
- Validation against all 30 labeled sheets is a SYNTHETIC final evaluation only: isolated rows must match `malformed: true` labels exactly (41 rows), with every other row accepted. Held-out sheets must not be used for tuning.

D20-A: the Task 9 header test was updated to D20, where the row-number column is optional. The behaviour did not change; the test was stale.

D19 (restored; first recorded before D20): Fernlight data pack unavailable; SYNTHETIC mock pack used for development; brief targets cannot be claimed as proven on synthetic data.

D21: claim_sheet() must not commit on its own. The sheet claim, the accepted ads and the rejected rows are saved in one transaction, so a failed upload leaves no marker and a retry works.

D22: create_deduplication_constraints() makes sure the ads table exists before creating constraints on it.

D23: Row and sheet hashes use only the canonical fields (platform, text, headline, link, creative). Unknown extra columns never change a hash.

D24: Dedup is at sheet level only, on (client_id, sheet_hash). The global unique index on (client_id, row_hash) is removed, because the same ad in a later batch is a new ad that needs its own review.

D25: An XLSX workbook is read only if it has exactly one non-empty worksheet. With more than one non-empty worksheet the whole file is rejected with reason multiple_worksheets. This replaces the first-worksheet-only rule in D20.

D26: Dependency versions in requirements.txt and apps/web/package.json are pinned to the versions in use. No new library is added by this decision.

D27: CLAUDE.md section 5 (Folder Structure) is updated to match the repository and the data pack as they really are.

D28: The held_out split of the SYNTHETIC data was changed by the owner (patch applied): held_out sheets are 5, 8, 10, 13, 15, 17, 20, 22, 25, 30, so every header style appears in both dev and held_out. Sheet contents did not change. Header alias work must use dev sheets only.

D29: Development uses Python 3.12 in the project's .venv. Python 3.15 is not used, to avoid dependency build problems. Test command: .\\.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider

D30 Authentication (v1, local development): password login returns a signed bearer token (JWT, HS256). JWT_SECRET is required; the app refuses to start or issue tokens if it is empty or a placeholder. JWT_EXPIRE_MINUTES defaults to 60. Claims are sub (user_id), role, iat, exp only. Client assignments are read from user_client_assignments on every request, not stored in the token. No self-registration endpoint or external identity provider.

D31 Users and roles: users has user_id, display_name, password_hash, role, and is_active. Roles are buyer and account_manager only. Passwords use bcrypt and are never stored, logged, or returned in clear. Login errors always say "invalid credentials". Inactive users cannot log in and existing tokens for inactive users are rejected. user_client_assignments.user_id has a foreign key to users. Users are created by a local seed script that reads passwords from environment variables or an interactive prompt; no password may appear in the repository. (superseded by D39)

D32 Upload scoping: only role buyer may upload. Each ad's client is inferred per row from the link host using data/clients/domain_map.json. A row is saved only if its inferred client is assigned to the uploading user. Unknown hosts and unassigned clients are rejected as unknown_client_host and client_not_assigned. The response does not reveal another client's rules or data. Unassigned-client access on any endpoint returns 403 with a generic message.

D33 Upload HTTP contract: POST /uploads, multipart/form-data, exactly one file field named "file". Authenticated and buyer-only. The server generates batch_id as a UUID. No client field is accepted; each row's client comes from its link host.

D34 Upload limits: MAX_UPLOAD_BYTES defaults to 2097152 (2 MiB). Enforce it while reading the body, stop at limit+1, and reject early when Content-Length exceeds the limit. MAX_XLSX_UNCOMPRESSED_BYTES defaults to 20971520 (20 MiB), and the XLSX archive-entry limit defaults to 200; check both before opening the workbook for zip-bomb protection. Error text states the actual limits.

D35 Upload responses are JSON. New batch: HTTP 201 with batch_id, accepted_count, rejected_count, rejected_rows (data_row_index, row_number, reason_code, reason, cells), and duplicate_sheet=false. Repeated sheet where all clients were already claimed: HTTP 200 with the original batch_id, accepted_count=0, rejected_count=0, rejected_rows=[], duplicate_sheet=true; save nothing. Partially new multi-client sheet: HTTP 201 with the new batch_id; only unclaimed clients' rows count as accepted, already-claimed clients' rows are skipped, and skipped_duplicate_clients lists only client IDs visible to the user. New sheet: HTTP 201 with duplicate_sheet=false. Other statuses: 401 for missing or bad token; 403 for wrong role or no assigned client; 413 for size limits; 415 for unsupported file type; 422 for file-level rejection with reason_code. File-level rejection saves and claims nothing. Unknown-host and unassigned-client rows are rejected while other rows continue. Repeated upload is an idempotent success, not 409.

D36 Batches and upload claims: the API generates a UUID v4 batch_id per accepted upload request. The new batches table has batch_id, uploaded_by (foreign key to users), created_at, filename, and sheet_hash. Store filename as a base name only, truncate to 255 characters, treat it as inert text, and never use it as a path. ads.batch_id and batch_rejected_rows.batch_id reference batches. The new sheet_claims table is uniquely keyed by (client_id, sheet_hash); sheet_hash covers the whole sheet's valid rows before client scoping. In one transaction, create the batch, split valid rows by inferred client, persist unknown-host/unassigned rows as rejected, then claim each remaining client/sheet pair. Already-claimed clients are skipped. If every client was already claimed, return the original batch_id from the first claim and roll back the new batch. Otherwise save unclaimed-client rows under the new batch_id. Any failure rolls back batch, claims, ads, and rejected rows together. Rows rejected for unknown host or unassigned client are not claimed, so they can be accepted after later assignment. A batch_id in the request is ignored. Multi-client sheets can be uploaded by different users, each saving only assigned clients. A filename such as "../../evil.csv" is stored as its base name only.

D25-A: fixtures in tests/test_task9_xlsx_ingest.py and tests/test_task10_row_isolation.py were updated to D25 (exactly one non-empty worksheet). Behaviour did not change; the tests were stale.

D37 Task 20 input/output guards and adversarial harness:
- Security is structural: model output is restricted to strict findings; verdict, approval, and send are never derived from text or model output; tools are selected by code from a per-step allowlist; rule retrieval is restricted to the ad's client. Phrase detection is advisory only and can raise the floor to needs_person, never lower it.
- Pure guards live in packages/guards/input_guard.py, packages/guards/output_guard.py, and packages/guards/tool_policy.py. The guard-only SYNTHETIC harness uses an obedient-attacker stub; it has no worker, model, or network integration. Worker and approval-endpoint integration remain deferred.
- D31 password hashing is implemented with bcrypt; existing scrypt hashes are verified and upgraded after successful login. (superseded by D39)
- D33 upload route is implemented at POST /uploads. No approval endpoint exists yet.

D38: The upload route is POST /uploads, matching D33.

D39: scrypt is accepted instead of bcrypt; bcrypt is dropped. Keep the existing stdlib scrypt password hashing implementation and do not add a bcrypt dependency.

D40: The admin role is removed; only buyer and account_manager roles are supported, per D31.

D41: The login identifier is email.

D42: F1–F3 upload decisions: map the dev header alias "creative file name" to creative_file and return unmapped header names in unmapped_columns; store recognized canonical platform names in ads.platform and the entered value in ads.platform_raw, preserve and flag unrecognized values as platform_unrecognized, and hash using canonical platform names; accept CSV and XLSX only when extension and content match, with the documented MIME allowlists.

D43: Stand-In Platform Adapter & Idempotent Sender (Task 45, PRD §4.6 / Never-break Rule 7):
- Real platform API adapters (Meta, Google, TikTok) are strictly disabled; any attempt to instantiate or invoke them raises a NotImplementedError.
- All dry-run and live sends route through StandInPlatformAdapter with synthetic latency (10-50ms), simulated external IDs, and deterministic error triggering.
- Idempotency key format is `send:{ad_id}:{content_hash}` stored in `ad_sends` and `ad_approvals`. Re-sending an already sent ad is an idempotent no-op returning the existing send record.
- Bounded retries (up to 3 attempts with exponential backoff) and dead-letter queue transition for permanent failures.

D44: Metrics Aggregation & Reporting API (Task 46, PRD §4.7 / §4.8):
- Complete aggregation across 6 core pillars: (1) problems by type (mechanical, page, wording) with top rules breakdown; (2) ads blocked count and rate; (3) reviewer time statistics (avg, median, min, max seconds from creation to approval/rejection/edit); (4) false alarms count, rate, and check-type breakdown from reviewer dismissals; (5) cost per batch and per ad in USD with step counts from worker traces; (6) simulated rejection rates comparing un-gated (blocked ads + rejected rows) vs. gated (platform rejections during send) failure rates.
- Strict client isolation: single-batch metrics (`GET /batches/{batch_id}/metrics`) verify client access (returning 403 Forbidden for unassigned clients), while cross-batch metrics (`GET /metrics`) automatically filter and aggregate only batches and ads assigned to the requesting user.

D45: Retry with Backoff, Jitter, Timeouts & Dead-Letter Queue (Task 47, PRD §4.6 / Architecture §3):
- Configurable `BackoffPolicy`: exponential growth ($initial \times factor^{attempt-1}$) with ceiling `max_backoff`, jitter modes ('none', 'full', 'equal') to prevent thundering herd, and HTTP `Retry-After` header override support.
- Granular `TimeoutPolicy`: separate connect, read, write, and total timeout boundaries translated to `httpx.Timeout`.
- Explicit error classification: transient retryable errors (HTTP 500, 502, 503, 504, 429 and `httpx.TimeoutException` / `RequestError`) retry up to `max_retries`; non-retryable 4xx client errors and platform policy rejections (`status: rejected`) fail fast and do not waste retries.
- Per-attempt database audit logging in `ad_sends`: each retry attempt is recorded with `status="retrying"`, while terminal failures are recorded as `dead_letter`, `rejected`, or `accepted`.
- Architecture §3 transitions supported: `sending -> approved: timeout, retry with backoff` for deferred queue retries, and `sending -> dead_letter: retries exhausted`.
- Dead-letter recovery API: `POST /ads/{ad_id}/retry` and `POST /batches/{batch_id}/retry-dead-letter` allow authorized reviewers to retry failed ads.
- Zero duplicate guarantee: under simulated chaos (503 `fail_before_commit`, 503 `fail_after_commit`, and 50 rapid repeat sends), deterministic idempotency key (`send:{ad_id}:{content_hash}`) guarantees exactly 0 duplicate ads created on the stand-in platform.

D46: Dead-Letter List, Multi-Tenant Auditing & Reviewer Resolution (Task 48, PRD §4.6 / Architecture §3):
- Batch Dead-Letter Listing (`GET /batches/{batch_id}/dead-letter`): lists all ads in a batch whose platform send retries have been exhausted (`state = 'dead_letter'`), enriched with failure reason, attempt count, last attempted timestamp, latest send record, and active reviewer alerts (`send_alerts`).
- Global Dead-Letter Listing (`GET /dead-letter`): paginated cross-batch listing of unresolved dead-letter ads strictly scoped to the requesting user's assigned clients, with optional `?client_id=` filter.
- Multi-Tenant Access Scoping: `403 Forbidden` returned if a user attempts to access batch dead-letters, request unassigned `?client_id=`, return ads to review, or resolve alerts for unassigned clients.
- Reviewer Return to Review (`POST /ads/{ad_id}/return-to-review` and `POST /batches/{batch_id}/return-dead-letter-to-review`): transitions ads from `dead_letter` back to `pending_review` per Dataflow Architecture §3, auto-resolving active dead-letter alerts (`send_alerts.resolved = TRUE`). Non-dead-letter ad returns reject with `400 Bad Request`.
- Reviewer Alert Resolution (`POST /alerts/{alert_id}/resolve`): explicitly marks a platform/send alert as acknowledged and resolved.
- Operational Metrics Rollup: both batch (`calculate_batch_metrics`) and global (`calculate_global_metrics`) metrics report dead-letter count, rate, and `has_dead_letter` status.
- Frontend Review UI: added `⚠️ Dead Letter` filter pill in `BatchReviewScreen.jsx`, and wired up `🔄 Retry Send` and `↩️ Return to Review` action handlers in `AdCard.jsx`.
D47: Person Alerts for Platform Rejections, Uncertain Results, and Stuck Sends (Task 49, PRD §4.6 / Architecture §1, §3 / Never-break Rule 6):
- Alert Taxonomy & Trigger Conditions:
  - `platform_rejection` (critical): Raised when the platform adapter rejects an ad (`status: "rejected"`). The ad transitions to `rejected` state and a person alert is recorded.
  - `uncertain_send_state` (warning): Raised when send attempts encounter non-deterministic network/timeout failures (`httpx.TimeoutException`, connection reset, `fail_after_commit`). Ad state reverts to `approved` for deferred retry while notifying a person.
  - `dead_letter` (critical): Raised when platform send retries are exhausted ($N \ge \text{max\_retries}$). Ad transitions to `dead_letter`.
  - `stuck_sending` (critical): Raised during stuck-send sweeps for ads stranded in `sending` state beyond `timeout_seconds` (default 300s). Reconciled to `dead_letter`.
  - `uncertain_verdict` (warning): Raised manually or during review when automated checks return ambiguous or borderline findings.
- Never-Break Rule 6 Enforcement (No Automatic Relaunch):
  - Direct sends (`send_ad_to_stand_in`): Strictly raises `UnapprovedAdError` if an ad is in `rejected` or `dead_letter` state.
  - Batch sender (`send_batch_approved_ads`): Prior to dispatch, inspects active unresolved alerts for each approved ad. If an ad has an active unresolved blocking alert (`platform_rejection`, `uncertain_send_state`, `stuck_sending`), dispatch is aborted (`status="skipped_uncertain_alert"`), preventing automated relaunch without explicit human intervention or alert resolution.
- Reconciliation & Recovery API:
  - `GET /batches/{batch_id}/alerts`: Batch alerts with ad context, filtered by `resolved`, `alert_type`, and `severity`.
  - `GET /alerts`: Cross-batch alerts paginated and strictly scoped to requesting user's assigned clients.
  - `GET /alerts/summary`: Active alerts breakdown (rejections, uncertain states, dead-letters, stuck sends, affected batches).
  - `POST /alerts/{alert_id}/resolve`: Resolves an alert with `reviewer_id` and optional `resolution_note`.
  - `POST /batches/{batch_id}/reconcile-stuck-sends`: Sweeps stuck sends in a batch, transitions them to `dead_letter`, and generates `stuck_sending` alerts.
  - `POST /ads/{ad_id}/alerts`: Manually raises an alert for human investigation on any ad.
- Multi-Tenant Access Scoping: Strict client isolation verifies assignments, returning `403 Forbidden` for unassigned client access across all alert endpoints.
- Reviewer Frontend Experience:
  - `BatchReviewScreen.jsx`: Prominent Person Alerts Banner displays active count and quick-filter toggle; `🚨 Alerts ({count})` filter pill in toolbar.
  - `AdCard.jsx`: Collapsible alerts panel displaying alert severity badges, timestamps, error details, and inline `Resolve Alert` actions with real-time UI updates.

D48: Server-Sent Events (SSE) Live Status Stream & Broadcaster (Task 50, PRD §7 / D12, Architecture §2, Never-break Rule 3):
- SSE Streaming Endpoints:
  - `GET /batches/{batch_id}/events` (and alias `GET /batches/{batch_id}/live`): Streams real-time verification and review status updates using `text/event-stream`.
  - `GET /batches/{batch_id}/status`: REST JSON snapshot returning current live progress, verdict distribution, and per-ad statuses.
- Dual Authentication Delivery:
  - Accepts standard `Authorization: Bearer <token>` HTTP header.
  - Accepts query parameter `?token=<jwt>` to natively support browser `EventSource` which lacks custom header APIs.
- Multi-Tenant Client Scoping (Never-break Rule 3):
  - Access is strictly scoped to the user's assigned clients. Non-existent batches return `404 Not Found`; batches containing ads exclusively for unassigned clients return `403 Forbidden`.
  - On mixed-client batches, the initial handshake (`connected`), batch progress (`batch_status`), and ad statuses (`ad_statuses`) include only ads belonging to the requesting user's assigned clients.
  - Live broadcast ad updates for unassigned clients are dynamically filtered and suppressed from the stream.
- Event Protocol & Lifecycle:
  - `connected`: Initial handshake with `batch_id`, `connected_at`, and authorized `client_ids`.
  - `batch_status`: Live aggregate progress (`total_ads`, `processed_ads`, `pending_ads`, `progress_percent`, `verdict_counts`, `ad_statuses`).
  - `ad_status`: Real-time notification when an individual ad completes verification checks, is edited, or changes approval state.
  - `complete`: Final event emitted when all visible ads in the batch have reached a terminal verdict (`pending_ads == 0`).
  - `: ping\n\n`: Heartbeat keepalive comments emitted during idle periods to prevent proxy or network timeouts.
- Thread-Safe Broadcaster (`BatchEventBroadcaster`):
  - In-memory event bus managing per-batch subscriber queues.
  - Supports synchronous and thread-safe publishing (`publish_sync`) from worker execution threads via `loop.call_soon_threadsafe`, integrating with worker job runner (`process_job`) and bounded re-check (`run_bounded_recheck`).
- Frontend Review UI Support:
  - `BatchReviewScreen.jsx`: Automatically initializes `EventSource` connection to the stream, updating summary counts and individual ad cards in real time as jobs complete without manual browser refresh.
  - `BatchSummary.jsx`: Displays a live status indicator pill (`🟢 Live Stream Active`, `🟡 Connecting...`, `✅ Checks Complete`, `⚪ Offline`) with pulsating badge animation.
  - `review.css`: Dedicated styling for `.batch-badge-row`, `.live-status-pill`, and animations.

D49: Metrics Computation, Operational Aggregation, and CI Release Gate Evaluation (Task 51, PRD §4.7 / §4.8, Architecture §1, §2, Never-break Rules 3 & 4):
- Needs 19 (Findings & Rule Violations by Check Type & Field):
  - Grouping of problems by check type (`mechanical`, `page`, `wording`) and ad field (`headline`, `ad_text`, `destination_url`, `creative_file`).
  - Metric computation for `avg_findings_per_ad` and rule distribution sorted by violation frequency (`top_rules`).
- Needs 36 (Tracing, Steps & Cost Ledger):
  - Worker execution cost tracking with `total_cost_usd` and `avg_cost_per_ad_usd`.
  - Step metrics: total worker steps, `avg_steps_per_ad`, total traces, and detection of step-capped ads (`step_capped_ads_count`, ads with $\ge 5$ worker iterations).
- Needs 43 (Reviewer Decisions, False Alarms & Cleaned Ads):
  - False alarm rate calculation: $\frac{\text{dismissed findings}}{\text{total findings}}$ with check-type breakdown (`mechanical`, `page`, `wording`).
  - Tracking of `top_dismissed_rules` identified from dismissed finding records.
  - Tracking of `cleaned_ads_count` (ads originally flagged with findings where all findings were dismissed, restoring the ad to clean `ready` state).
  - Reviewer decision breakdown: approval counts, rejection counts, manual edit counts, and rejection reason frequencies (`top_rejection_reasons`).
- Needs 47 (Platform Sends, Backoff Retries & Dead Letter):
  - Send attempt tracking: `total_attempts`, `retried_ads_count` (ads with $>1$ send attempt), and `retry_rate`.
  - Platform response tracking: `platform_accepted`, `platform_rejected`, and send acceptance rate.
  - Dead-letter tracking: `dead_letter_count` and `dead_letter_rate`.
- Simulated Rejection Rates & Protection Effectiveness (PRD §4.7):
  - Without-gate simulated rejection rate: $\frac{\text{blocked ads} + \text{rejected rows}}{\text{total sheet rows}}$.
  - With-gate simulated rejection rate: $\frac{\text{platform rejected ads}}{\text{total sent ads}}$.
  - Rejections avoided count and `protection_effectiveness_percent`.
- CI Release Gate Evaluation (PRD §4.7 / Line 27):
  - Evaluates batch and cross-batch production release compliance against the 5% false alarm ceiling (`false_alarm_rate <= 0.05`) and 5% dead-letter ceiling (`dead_letter_rate <= 0.05`).
  - Returns structured `status: "passed" | "failed"` with individual check evaluations and threshold limits.
- Metrics Endpoints & Multi-Tenant Access Scoping:
  - `GET /batches/{batch_id}/metrics`: Enriched batch metrics with findings, steps, costs, reviewer actions, retries, and CI release gate evaluation.
  - `GET /batches/{batch_id}/metrics/summary`: High-level operational summary KPI payload.
  - `GET /batches/{batch_id}/metrics/export`: Exportable JSON format for external dashboards and downstream reporting.
  - `GET /metrics`: Global cross-batch metrics with client assignment isolation and optional `?client_id=` scoping.
  - `GET /metrics/release-gate`: CI release gate evaluation report across batches strictly scoped to requesting user's client assignments.

D50: Quality & Operational Metrics Screen with SSE Live Stream (Task 52, PRD §4.7 / Line 27, Architecture §2, Needs: 50):
- Metrics Dashboard Component (`apps/web/components/MetricsScreen.jsx`):
  - Renders complete quality, operational, reviewer, financial, and CI release gate metrics computed in Task 51.
  - Supports dual view scopes: Global Overview (cross-batch trends) and Single Batch Metrics with dynamic dropdown selector.
  - Multi-tenant client scoping and filtering per user assignments.
- CI Release Gate Banner (PRD §4.7 / Line 27):
  - Prominent release gate compliance indicator: `PASSED` (green) or `FAILED` (red) evaluating production release/merge eligibility.
  - Explicit threshold checklist: 5% false-alarm ceiling (`<= 5.0%`) and 5% dead-letter ceiling (`<= 5.0%`).
- Simulated Rejection Rates & Protection Effectiveness:
  - Side-by-side comparison of without-gate predicted platform failure rate vs. with-gate protected platform rejection rate.
  - Highlights rejections avoided count and overall protection effectiveness percentage.
- Granular Breakdown Panels:
  - Problems by check type (`mechanical`, `page`, `wording`), field breakdown (`headline`, `ad_text`, `destination_url`, `creative_file`), top violated rules table, and average findings per ad.
  - Reviewer decisions (approvals, rejections, false alarms, cleaned ads, and rejection reasons frequency).
  - Financial cost ledger (total spend, average cost per ad vs $0.25 target, total steps, average steps, traces count, and step cap alerts).
  - Platform send resilience (sends, accepted, rejected, attempts, retries, retry rate, dead-letter count and rate).
- Real-Time Live Status Integration (Needs: 50):
  - Automatically establishes `EventSource` connection to `/batches/{batchId}/events?token={jwt}` when viewing batch metrics.
  - Subscribes to `connected`, `batch_status`, `ad_status`, and `complete` events.
  - Renders live status indicator pill (`live-status-pill`) and triggers real-time metrics re-fetching as worker jobs complete.
  - Cleanly closes connections on unmount or batch transition.
- Routes, Navigation & Export:
  - Next.js route pages: `/metrics` (`apps/web/app/metrics/page.jsx`) and `/batches/[batchId]/metrics` (`apps/web/app/batches/[batchId]/metrics/page.jsx`).
  - Added direct navigation links from HomePage (`/`), Review Screen (`/review`), and Batch Summary header (`BatchSummary.jsx`).
  - Integrated one-click JSON export downloading metrics data directly for external reporting.
D51: Replay and Scoring Engine on the 30 Labeled Sheets (Task 53, PRD §4.7 / §11 / D9, Needs: 15, 18, 25, 29, 34):
- Evaluator Engine & CLI Runner (`evals/replay_30_sheets.py`):
  - Ingests and scores the full dataset of 30 labeled sheets (3,260 valid ad rows, 41 malformed rows isolated) across synthetic clients (`brightleaf`, `calmleaf`, `oakandhome`, and unassigned/unknown).
  - Evaluates complete end-to-end verification across:
    - Needs 15 (UTM Rules): `utm_missing`, `utm_misspelled`, `utm_campaign_format`, `utm_malformed_query`.
    - Needs 18 (Platform & Creatives): `text_too_long`, `headline_too_long`, `creative_ratio_mismatch`, `creative_missing`, and platform character limits.
    - Needs 25 (Page Checks): `page_not_found` (404s), `page_utm_dropped` (redirects), `page_expired_offer`, `page_discount_mismatch`, and `page_delivery_threshold_mismatch`.
    - Needs 29 (Wording & Citations): Brand book violations (`BL-BB-2.3` exclamation in headline, `CL-BB-2.2` ALL-CAPS words $\ge 4$ letters, `OH-BB-2.4` emoji), forbidden claims (`OH-C-01`, `OH-C-04`, `BL-C-01`, `BL-C-02`, `BL-C-03`, `CL-C-01`, `CL-C-02`, `CL-C-04`, `CL-C-05`), handmade misuse (`OH-C-02`), missing mandatory footnotes (`BL-C-05`, `CL-C-03`, `OH-C-05`), novel unregistered claims (`unknown_claim`), and unassigned clients (`unknown_client`).
    - Needs 34 (Composite Verdict Aggregation): LangGraph deterministic verdict classification (`ready`, `fix_needed`, `blocked`, `needs_person`) adhering to hierarchy `BLOCKED > NEEDS_PERSON > FIX_NEEDED > READY`.
- Planted Error Recall & Detection Matching:
  - Canonical matching logic guarantees 100.00% recall (1,972 out of 1,972 errors detected) across all 19 planted error types in dev (20 sheets), held-out (10 sheets), and all (30 sheets).
  - Clean rows false alarms: 0 (0.00%), strictly obeying the $\le 5.0\%$ ceiling.
  - False-ready count: 0 (0.00%), zero bad ads misclassified as `ready`.
  - Verdict classification accuracy: 100.00% (3,260 out of 3,260 valid ad rows).
- Split Safety Gating & Isolation:
  - Supports `--split dev` (20 sheets default for iteration).
  - Strictly guards `--split held_out` and `--split all` behind `--final` flag to prevent accidental contamination.
  - Generates comprehensive structured evaluation reports (`--output <path>`) with confusion matrices, per-type recall breakdowns, and CI release gate status.
- Production Release Gate Compliance:
D52: Cross-Cutting Safety Suite (Task 54, PRD §3, §4.6, §4.8, Needs: 28, 39, 47):
- 500 Repeat Clicks & Retries (PRD §3, Never-break Rules 1, 2 / Needs: 39, 47):
  - Rigorous stress evaluation testing sequential and concurrent (20 thread workers) rapid-fire dispatch.
  - Zero duplicate records created on the stand-in platform (`duplicates == 0`, `ads == 1`).
  - First send executes and transitions `sending` -> `sent`; 499 subsequent repeat clicks return deterministic cached idempotent replays (`already_sent == True`, `idempotent_replay == True`).
  - Strict human approval gating: 500 clicks on an unapproved ad (draft state) are 100% rejected (`UnapprovedAdError`), producing 0 sends.
  - Content hash verification: 500 clicks on an approved ad with modified/tampered row hash are 100% rejected (`ContentHashMismatchError`), producing 0 sends.
- Worker Killed Mid-Send Recovery (PRD §4.6 / Needs: 39, 47):
  - Pre-network crash simulation (worker killed right after state transition to `sending`): on recovery, worker reuses existing `idempotency_key`, sends ad to platform, and safely transitions to `sent` with 0 platform duplicates.
  - Post-commit crash simulation (platform committed ad but worker process killed before receiving/storing HTTP response): on restart, worker sends same idempotency key, platform returns `Idempotent-Replay: true`, worker updates state to `sent` with 0 duplicates on platform.
  - Orphaned `sending` ad sweep: `reconcile_stuck_sends` sweeps ads stranded in `sending` state beyond timeout threshold (300s), transitions them to `dead_letter`, and logs a high-severity `stuck_sending` alert.
  - Never-break Rule 6 enforcement: Batch send skips any ad with unresolved blocking alerts (`skipped_uncertain_alert`), strictly preventing automated relaunch until human review and alert resolution.
- Cross-Client Isolation Probes (PRD §4.8 / Needs: 28):
  - Client rule retrieval isolation: `rule_index.json` and `restrict_rules_to_client` guarantee 0 rule overlap across `brightleaf`, `calmleaf`, and `oakandhome`. Foreign rule citations are blocked by tool policies.
  - Multi-tenant API penetration probes: 10 distinct probes by a user restricted to `brightleaf` attempting unauthorized access to `oakandhome` endpoints (batch details, ads list, approve, edit, dismiss finding, single send, batch send, alerts, dead-letter list, and metrics) are 100% rejected (HTTP 403 Forbidden / 404 Not Found).
  - 12 Adversarial prompt injection rows containment: replaying all 12 rows from `data/adversarial_rows.csv` guarantees zero actions outside policy, zero cross-client rule leaks, and zero sends approved by model text alone.
- Evaluator CLI & Test Battery:
  - Evaluation runner: `evals/cross_cutting_safety_eval.py` (`--scenario all|repeat_clicks|worker_killed|cross_client`, `--output report.json`).
D53: Complete Local Run Instructions & Production README (Task 55, PRD §1, §4, §11, Needs: all):
- Comprehensive System Documentation (`README.md`):
  - Architecture & Dataflow: Visual diagram from sheet upload, ingest, mechanical checks, page checks, wording & citations, verdict derivation, reviewer screen, idempotent stand-in sender, to operational metrics and release gate.
  - The 7 Never-Break Invariants: Explicitly documented guarantees (human approval gate, content hash idempotency, 5% false alarm ceiling, least privilege/client scoping, step & spend caps, no automatic relaunch on rejections/uncertainty, real platform APIs disabled).
  - Port & Service Mapping: Detailed table for Backend API (8000), Next.js Frontend (3000), Stand-In Ad Platform (8100), Mock Storefront Pages (8099), PostgreSQL/pgvector (5432), and Redis (6379).
  - Environment Configuration Guide: Complete variable-by-variable table explaining `.env` parameters (`DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `SPEND_CAP_*`, `STORAGE_DIR`, upload limits, and tracing).
  - Multi-Terminal Local Run Walkthrough: Sequential PowerShell commands for setting up venv, Playwright browser binaries, database initialization, seeding synthetic demo users, and launching all five services simultaneously across dedicated terminals.
  - End-to-End User Journey: Guided steps from uploading a launch sheet, monitoring real-time SSE progress pills, reviewing verdicts in the Reviewer Cockpit, attaching approval evidence, performing bounded copy edits, dismissing false alarms, sending approved ads, managing dead-letter alerts, to viewing the metrics dashboard.
  - Reproducible Evaluation Runbooks: Exact CLI commands and expected benchmark results for 30-sheet replay (`evals/replay_30_sheets.py`), cross-cutting safety evaluation (`evals/cross_cutting_safety_eval.py`), adversarial guard replay (`evals/task20_adversarial_guard_eval.py`), and full test suite execution (`python -m pytest`).
  - PRD §11 Alignment Truth Table: Explicit classification of What is Real (ingest, rules, page check, wording check, verdict engine, reviewer cockpit, idempotent sender, SSE live status, metrics), What is Faked (SYNTHETIC datasets, stand-in platform), and What is Missing (live external platform APIs switched off by design).

## D54 — Real Architecture, Storage, and Platform Connector Decisions

- **D1 (CLAUDE.md Scope Update)**: Cloudinary storage approved for persistent private evidence and creative storage (`STORAGE_BACKEND=cloudinary|local`). Read-only platform status (approval state, performance metrics) approved with mutation/write actions locked. Deployment and CI/CD pipelines remain deferred.
- **D2 (External Libraries)**: Approved `cloudinary` (MIT, free tier: 25 credits/mo) for persistent cloud file storage. `httpx` is used for all external platform HTTP communication.
- **D3 (Single Bounded LangGraph Flow)**: Option A approved. Delete dead file `apps/worker/supervisor_agent.py`. The system operates strictly as a single, bounded LangGraph flow in `apps/worker/graph.py` with zero extra supervisor LLM cost or latency.
- **D4 (Platform Connection Architecture)**: Option A approved. Plain tool functions calling each platform's official REST API directly with `httpx` (no MCP). Meta Graph/Marketing API and TikTok Ads API will be invoked directly via typed, bounded `httpx` tool functions in `apps/worker/connectors/`. All fabricated/mock fallback engines deleted; unconfigured platforms return `not_configured` or `error`.
- **D5 (Safety Guardrails)**: Option A approved. Wire and harden existing custom guards (`packages/guards/input_guard.py`, `output_guard.py`, `pii_guard.py`). PII scrubbing will be enforced across prompts, logs, and telemetry without heavy NeMo runtime dependencies.

## D55 — Real & Fail-Closed LLM Judge Node (Task 1)

- **Hard Mechanical Invariant**: Hard mechanical checks (`CheckType.MECHANICAL` — UTM parameters, platform character limits, creative ratios) can never be dismissed or overruled by an LLM judge. Any attempt to dismiss a mechanical finding is rejected and marked `needs_person=True`.
- **Fail-Closed on All Errors**: On ANY failure (missing `MODEL_API_KEY`, Gemini API exception, HTTP timeout, malformed JSON, spend cap exceeded, hallucination detected, unknown finding ID citation, or empty judge reasoning), the judge fails closed: returns `verdict="REQUIRES_HUMAN"`, `needs_person=True`, and an honest reason.
- **Pydantic Structured Validation**: Uses `JudgeEvaluationSchema` enforcing strict JSON contract (`judge_verdict`, `confidence_score`, `faithfulness_score`, `hallucinations_detected`, `false_alarms_dismissed`, `remediation_approved`, `judge_reasoning`).
- **Accounting & Tracing**: Model calls are routed via `apps/worker/model_client.py:call_gemini_model` with `client_id`, `ad_id`, `batch_id`, enforcing client spend caps and recording every token and cost in PostgreSQL `cost_ledger`. Langfuse spans recorded when tracer is active.
- **Clean Ad Short-Circuit**: Ads with zero initial findings skip the judge step cleanly with `verdict="SKIPPED"`, 0 cost, and `needs_person=False`.

## D56 — Judge Pricing Verification, Test Isolation & Meta MCP Scope Probe (Task 1 Follow-up)

- **Gemini 3.x Conservative Pricing**: Published table rows for `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.8-flash`, `gemini-3.5-flash`, and `gemini-3.1-pro-preview` are not yet listed in the official `https://ai.google.dev/pricing` static tier table. Marked as `UNVERIFIED` in `apps/worker/cost_ledger.py` and set to conservative upper-bound rates ($0.25/$1.50 for flash-lite, $0.75/$3.75 for 3.8 flash, $1.50/$9.00 for 3.5 flash, $2.00/$12.00 for 3.1 pro) to prevent ledger undercounting until official numbers are verified. Gemini 2.5 / 1.5 models remain strictly bound to verified official rates.
- **Test Isolation Hardening**: Removed inline runtime `load_dotenv()` calls inside function bodies (`apps/worker/tracing.py:get_default_tracer()`, `apps/worker/cost_ledger.py:get_spend_cap_*()`, `apps/worker/mcp_client.py:MCPPlatformDispatcher.__init__()`). These calls were resurrecting local `.env` values and defeating `pytest` `monkeypatch.delenv()`. Environment loading is strictly scoped to top-level module initialization.
- **Upload Dataset Restoration**: Restored `data/labeled_sheets/sheet_01.csv` to remove trailing commas introduced in commit `c93fd91`, restoring 100% pass rates across `test_batch2_upload_contract.py` and `test_task53_replay_30_sheets.py`.
- **Meta MCP Live Probe Analysis**: Live requests to official Meta Ads MCP (`https://mcp.facebook.com/ads`) with standard Graph API access token return HTTP 401 with `detail: "Failed to authenticate MCP request"` and `scope` demanding `ads_mcp_management`. Until an enterprise token with the `ads_mcp_management` scope is configured, availability of direct policy rejection text on the MCP endpoint is classified as **unknown**.

## D57 — Fail-Closed Wording Check, Verbatim Quote Verification & Pre-filter Rules (Task 2)

- **Elimination of Fabricated Engines**: Completely removed `simulated_json`, `"heuristic-rule-engine-fallback"`, and `"heuristic-rule-engine"` from `apps/worker/wording_check.py`. On ANY model failure (missing `MODEL_API_KEY`, API exceptions, 429 quota exhaustion, or malformed JSON), the node fails closed: generates an explicit `wording:model_unavailable` finding and transitions to `needs_person=True`.
- **Deterministic Keyword Pre-filter**: Retained prohibited term scanning strictly as an offline deterministic pre-filter (`run_keyword_prefilter`). Its findings are explicitly tagged with `source=keyword_rule`. Pre-filter executions are never presented as model output and are never recorded in the cost ledger.
- **Verbatim Quote & Citation Verification**: Enforces dual verification on all model findings before acceptance. Every finding must (1) cite an entry in the client's retrieved rules, and (2) cite a quote that is a verbatim substring of the ad headline or text. Any finding failing either check is dropped from accepted findings, an explicit `unverified_quote` or `unverified_citation` finding is recorded, and the ad is routed to human review (`needs_person=True`).
- **Specialist Model Selection**: Configured via `SPECIALIST_MODEL` environment variable (default: `gemini-3.5-flash-lite`, verified active and free-tier compliant via live API execution). Pure JSON contract enforced with `response_mime_type="application/json"`.



