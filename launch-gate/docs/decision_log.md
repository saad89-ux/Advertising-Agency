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

D31 Users and roles: users has user_id, display_name, password_hash, role, and is_active. Roles are buyer and account_manager only. Passwords use bcrypt and are never stored, logged, or returned in clear. Login errors always say "invalid credentials". Inactive users cannot log in and existing tokens for inactive users are rejected. user_client_assignments.user_id has a foreign key to users. Users are created by a local seed script that reads passwords from environment variables or an interactive prompt; no password may appear in the repository.

D32 Upload scoping: only role buyer may upload. Each ad's client is inferred per row from the link host using data/clients/domain_map.json. A row is saved only if its inferred client is assigned to the uploading user. Unknown hosts and unassigned clients are rejected as unknown_client_host and client_not_assigned. The response does not reveal another client's rules or data. Unassigned-client access on any endpoint returns 403 with a generic message.

D33 Upload HTTP contract: POST /uploads, multipart/form-data, exactly one file field named "file". Authenticated and buyer-only. The server generates batch_id as a UUID. No client field is accepted; each row's client comes from its link host.

D34 Upload limits: MAX_UPLOAD_BYTES defaults to 2097152 (2 MiB). Enforce it while reading the body, stop at limit+1, and reject early when Content-Length exceeds the limit. MAX_XLSX_UNCOMPRESSED_BYTES defaults to 20971520 (20 MiB), and the XLSX archive-entry limit defaults to 200; check both before opening the workbook for zip-bomb protection. Error text states the actual limits.

D35 Upload responses are JSON. New batch: HTTP 201 with batch_id, accepted_count, rejected_count, rejected_rows (data_row_index, row_number, reason_code, reason, cells), and duplicate_sheet=false. Repeated sheet where all clients were already claimed: HTTP 200 with the original batch_id, accepted_count=0, rejected_count=0, rejected_rows=[], duplicate_sheet=true; save nothing. Partially new multi-client sheet: HTTP 201 with the new batch_id; only unclaimed clients' rows count as accepted, already-claimed clients' rows are skipped, and skipped_duplicate_clients lists only client IDs visible to the user. New sheet: HTTP 201 with duplicate_sheet=false. Other statuses: 401 for missing or bad token; 403 for wrong role or no assigned client; 413 for size limits; 415 for unsupported file type; 422 for file-level rejection with reason_code. File-level rejection saves and claims nothing. Unknown-host and unassigned-client rows are rejected while other rows continue. Repeated upload is an idempotent success, not 409.

D36 Batches and upload claims: the API generates a UUID v4 batch_id per accepted upload request. The new batches table has batch_id, uploaded_by (foreign key to users), created_at, filename, and sheet_hash. Store filename as a base name only, truncate to 255 characters, treat it as inert text, and never use it as a path. ads.batch_id and batch_rejected_rows.batch_id reference batches. The new sheet_claims table is uniquely keyed by (client_id, sheet_hash); sheet_hash covers the whole sheet's valid rows before client scoping. In one transaction, create the batch, split valid rows by inferred client, persist unknown-host/unassigned rows as rejected, then claim each remaining client/sheet pair. Already-claimed clients are skipped. If every client was already claimed, return the original batch_id from the first claim and roll back the new batch. Otherwise save unclaimed-client rows under the new batch_id. Any failure rolls back batch, claims, ads, and rejected rows together. Rows rejected for unknown host or unassigned client are not claimed, so they can be accepted after later assignment. A batch_id in the request is ignored. Multi-client sheets can be uploaded by different users, each saving only assigned clients. A filename such as "../../evil.csv" is stored as its base name only.

D25-A: fixtures in tests/test_task9_xlsx_ingest.py and tests/test_task10_row_isolation.py were updated to D25 (exactly one non-empty worksheet). Behaviour did not change; the tests were stale.
