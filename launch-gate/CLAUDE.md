# Launch Gate

## 1. Project Overview

Launch Gate is a pre-launch gate for batches of ads. A buyer uploads a messy CSV or XLSX launch sheet; the system normalizes the rows, checks each ad, assigns a verdict, and shows a reviewer each ad beside its findings. Only ads approved by a person are sent, and the default sender uses the stand-in platform in dry-run mode. Explicitly authorized Meta launches may use the MCP route with both live and write flags enabled.

It is for Fernlight Media, LLC. Gareth Pell is the economic buyer; buyers and account managers are the daily users. The pilot covers the three clients whose brand books and claims registers are in the data pack. The PRD describes v1 as running in the background of the existing flow and returning one review screen.

Out of scope for v1: auto-correction and automatic relaunch, continuous post-launch monitoring, a client approval link or portal, inbound client email/text/call integrations, fine-tuned or self-hosted models, multi-agent or free-form supervisor designs, multimodal image retrieval, chat as the main interface, learned reviewer habits or agentic memory, lawyer-grade legal judgment, reporting unrelated to the gate, crawling at scale or anti-bot evasion, enterprise SSO, compliance paperwork, and multi-region deployment. Deployment is deferred by the owner: work is limited to development and local testing. Do not create Dockerfiles, `docker-compose.yml`, CI/CD files, or cloud deployment configuration.

## 2. Architecture

The Next.js frontend provides upload, review, metrics, and verdict trace screens. It connects to the FastAPI backend, which provides authentication and role-based access, ingest, approval state management outside the model, and live status through Server-Sent Events. Buyers see only assigned clients, and a buyer cannot approve an ad they were not given.

The ingest API normalizes and deduplicates uploaded sheets, isolates malformed rows, and puts one job per ad on Redis. A fixed, bounded LangGraph worker runs mechanical checks, read-only page checks in a Playwright sandbox, and wording checks using retrieval restricted to that client's rules. Input and output guards treat text as data without authority. A context builder selects the resources and summaries needed for each step. The verdict and budget guard produces an ad verdict and saves workflow state to the PostgreSQL checkpointer.

PostgreSQL stores ads, findings, approvals, audit data, costs, and client-scoped records; pgvector in PostgreSQL supports vector retrieval. Large outputs are stored in the local `storage/` folder, with summaries and file references in shared state. The checkpointer resumes checks from the last saved step. It does not provide exactly-once sending; the sender's idempotency key does.

The review screen lets a reviewer approve, edit, or reject an ad. Edits and reasons are saved and the changed ad is re-checked in a bounded loop. Approval records who approved, when, and the approved content hash; client approval evidence is a reviewer-attached note or screenshot linked to that hash. The sender uses an idempotency key, bounded retries, timeouts, and a dead-letter list, and sends only to the stand-in dry-run platform. Rejections or uncertain send states notify a person; there is no automatic relaunch. Owner authorization (2026-10-21): explicitly human-authorized Meta launches are allowed only when META_MCP_ENABLE_LIVE and META_MCP_ALLOW_WRITES are enabled. Both remain disabled by default.

Langfuse traces each step with ad and batch IDs. Metrics include problems by type, ads blocked, reviewer time, false alarms, cost per batch, and simulated rejection rate with and without the gate. Replay uses 30 labeled sheets. CI and deployment work are deferred by the owner.

## 3. Tech Stack

| Layer | Chosen technology |
|---|---|
| Language | Python 3.12+ |
| Backend | FastAPI |
| Frontend | Next.js |
| Database | PostgreSQL |
| Vector retrieval | pgvector inside PostgreSQL |
| Queue and cache | Redis |
| Orchestration | LangGraph, one fixed bounded flow |
| Page check | Playwright in a sandbox container, read-only |
| Model | One hosted provider; provider not named. One key from `.env`; spend cap in code. |
| File store | Local `storage/` folder; Docker volume deferred by owner (PRD D7). |
| Live status | Server-Sent Events |
| Tracing | Langfuse |
| Tests | pytest, run locally |
| CI | Deferred by owner (PRD D9, GitHub Actions). |
| Start and local services | Run locally; PostgreSQL and Redis are installed on the owner's machine and connection strings are read from `.env`. The `docker compose up` milestone is deferred by owner. |
| Cloud deployment | Deferred by owner (PRD D10); host not named. |
| Ad platform | Stand-in dry-run sender by default; explicitly human-authorized Meta MCP launches require live/write environment opt-in. |
| Secrets and configuration | Environment-based configuration; `.env` holds model keys, secrets, and connection strings and is never committed. Provide `.env.example` only. Keep paths out of hard-coded configuration so the owner can containerize later without code changes. |

## 4. Data Flow

### Upload and ingest

1. A buyer uploads a CSV or XLSX launch sheet containing ads.
2. The ingest API normalizes column names and platform spellings, handles blank cells, creates one clean record per ad, and hashes each row and the sheet.
3. A repeated sheet adds nothing. A malformed row is isolated so other rows continue. Concurrent work is queued in Redis, one job per ad.
4. PostgreSQL stores ads and associated findings, approvals, audit data, and costs. Each row carries `client_id` for client separation.

### Checks and verdict

1. The worker gets each ad job from Redis.
2. Mechanical checks run as code without an LLM: UTM presence and spelling, platform text and headline limits, and creative size and ratio. Recognizable but incorrect values are suggested for correction, never silently changed. Findings name the row, field, and proposed fix. Old platform limits are flagged for a person; client naming preferences take precedence over the 2023 naming document and are recorded.
3. The page checker opens the ad's page with Playwright in read-only mode. It checks that the page loads, UTMs survive redirects, and offer, price, and dates match the ad. It stores a screenshot as evidence. It never submits a form or buys anything.
4. The wording checker retrieves only the current client's brand book and claims register. Each finding cites an actual entry or section; unknown situations go to a person.
5. Large outputs, including page text, screenshots, rule chunks, and full findings, go to local `storage/`. Shared state keeps IDs, status, a short summary, and file references. The context builder selects the needed tools, top-k rules for that client, and summary for each step; it reads a stored file on demand.
6. The bounded verdict engine returns `ready`, `fix needed`, `blocked`, or `needs a person`, with a batch summary. Code enforces a step cap and spend cap per row. State is checkpointed to PostgreSQL after every step with `thread_id = batch_id:ad_id`, allowing checks to resume after a crash, restart, or deploy.

### Review, approval, and send

1. The review screen presents each ad beside its findings. A reviewer can approve, edit, or reject; edits and reasons are saved.
2. An edited ad is re-checked in a bounded loop. A rejection is recorded with its reason.
3. Approval records the reviewer, time, and content hash. A reviewer can attach a note or screenshot of client approval, linked to the content hash. The approved content version is the only version eligible to send. v1 does not read email, text, or calls.
4. The sender uses an idempotency key and state transitions `pending_review` → `approved` → `sending` → `sent`. A reject moves to `rejected`; exhausted retries move to `dead_letter`. Timeouts retry with backoff. A duplicate approval click or retry after a crash does not duplicate a send.
5. The dry-run sender calls the stand-in platform; the explicitly authorized campaign launch route may call Meta MCP when live/write configuration is enabled. The result is accepted or rejected with a reason and is stored in PostgreSQL. A rejection or uncertain state alerts a person and returns to review; there is no automatic relaunch. Real Meta calls require explicit human approval and both live/write environment flags.

### Metrics and trace

1. Worker steps emit Langfuse traces tagged with ad and batch IDs, including step costs and information needed to reconstruct a verdict.
2. Metrics aggregate problems by type, ads blocked, reviewer time, dismissed findings as false alarms, cost per batch, and simulated rejection rate with and without the gate. Replay uses 30 labeled sheets and is run locally; CI is deferred by owner.

## 5. Folder Structure

Assuming `launch-gate/` is the repository root, I’d use this structure. It keeps the three runtime applications separate, puts reusable mechanical checks in a package, and keeps source data separate from generated files.

```text
launch-gate/
  CLAUDE.md
  README.md
  .env.example
  apps/
    api/
    web/
    worker/
  packages/
    rules/
  data/
    client-rules/
    labeled-sheets/
  storage/
  evals/
  tests/
  docs/
```

| Folder | What belongs here—and what does not |
|---|---|
| `apps/api/` | FastAPI ingest, authentication/access control, approval APIs, and SSE status; not ad-checking workflows or UI. |
| `apps/web/` | Next.js upload, review, metrics, and verdict-trace screens; not approval state logic or worker jobs. |
| `apps/worker/` | The bounded LangGraph flow, page and wording checks, verdict/budget guard, context building, checkpoint integration, and sender; not API routes or frontend screens. |
| `packages/rules/` | Reusable mechanical checks implemented as pure code; not LLM-based wording checks or page browsing. |
| `data/client-rules/` | Read-only client brand books, claims registers, naming guidance, and platform limits; not generated findings or cross-client shared rules. |
| `data/labeled-sheets/` | Read-only labeled sheets for evaluation replay; not live uploads or generated outputs. |
| `storage/` | Local file-store outputs such as page text, screenshots, rule chunks, and full findings; not source data or committed files. |
| `evals/` | Replay and scoring logic for the labeled sheets; not CI/CD configuration, which is deferred. |
| `tests/` | Local pytest tests for the API, web-facing contracts, worker, rules, and system behavior; not evaluation source data. |
| `docs/` | The PRD, architecture document, and decision log; not implementation code. |
| Repository root | `CLAUDE.md`, `README.md` with local setup instructions, and `.env.example`; not secrets, Docker files, CI/CD files, or cloud deployment configuration. |

**Architecture mapping:** `apps/web/` is the frontend; `apps/api/` is the FastAPI backend; `apps/worker/` is the LangGraph worker and sender; `packages/rules/` contains mechanical checks; `data/` supplies client rules and evaluation inputs; `storage/` holds large file-store outputs; PostgreSQL, Redis, pgvector, Playwright, Langfuse, and the model provider are connected services or tools configured for the relevant apps, rather than separate top-level folders. `evals/` and `tests/` support replay and local verification. Deployment and CI folders are omitted because those are deferred.

## 6. Documents Index

none yet

## 7. Development Rules

- Work on one small, self-contained task at a time. Never build a full feature in one go.
- Do not move to the next task until I explicitly say "confirmed, next task".
- After every task, report: (a) files created, (b) files modified, (c) assumptions made, (d) edge cases handled.
- If you modify an existing file, mark it clearly as "MODIFIED EXISTING FILE" and explain why, and which other parts of the system depend on it and could be affected.
- Before creating any new file, confirm which folder it belongs to according to the Folder Structure section.
- Do not introduce libraries, patterns, or features that are not in the Tech Stack or PRD without asking me first.
- When an error occurs, explain the root cause first. Do not fix it until I say so.
- End every response with a short change summary: new, modified, unchanged.

## Authorized correction brief (2026-10-21)

The owner authorized the complete Launch Gate correction brief in this chat, overriding the incremental confirmation workflow for this task. The launch popup Authorize button records the authenticated approver and canonical content hash before Meta dispatch. Notification email and phone are metadata only; phone has no SMS delivery provider. No demo accounts or batches are created automatically. Live Meta writes require explicit environment opt-in and human campaign authorization.

Owner follow-up: launch contact collection is email-only. Do not add a phone field or require a phone number. Resolve the authorized client automatically from the real account, batch, or campaign details; do not add manual client-selection or reload-client controls to the launch popup.
