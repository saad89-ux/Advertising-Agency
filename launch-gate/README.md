# Launch Gate

**Pre-Launch Ad Verification Gate for Performance Marketing Agencies**  
*Stop finding out about mistakes from the client.* (PRD §1)

Launch Gate is an automated pre-launch verification pipeline and reviewer cockpit. Media buyers upload spreadsheet batches (CSV or XLSX) containing ad copy, platform targeting, URLs, and creative assets. Launch Gate turns messy rows into validated records, evaluates mechanical platform rules, opens landing pages headlessly to verify offers and UTM survivability, verifies wording claims against client brand books and claims registers, derives deterministic composite verdicts, and provides an interactive review screen for buyers and account managers. Only ads approved by a person can ever be sent to ad platforms (switched to dry-run stand-in platform in v1).

---

## Architecture & Core Invariants

### Verification Dataflow Architecture

```
[ Upload Sheet (CSV/XLSX) ]
           │
           ▼
[ Ingest & Deduplication ] ───► Isolates malformed rows, hashes row and sheet
           │
           ▼
[ Mechanical Layer ] ────────► UTM presence, UTM naming, character limits, creative ratios
           │
           ▼
[ Headless Page Layer ] ─────► Playwright opens URL: 404s, UTM redirect drops, discount/threshold mismatches, offer expiration
           │
           ▼
[ Wording & Citation Layer ] ─► Client-scoped retrieval over brand book & claims register, Gemini LLM check, output guard
           │
           ▼
[ Verdict Engine ] ──────────► Deterministic aggregation: ready | fix needed | blocked | needs a person (LangGraph flow)
           │
           ▼
[ Reviewer Cockpit & SSE ] ──► Interactive review screen: approve (with evidence), edit copy, dismiss finding
           │
           ▼
[ Idempotent Sender ] ───────► Dry-run stand-in platform with backoff, timeouts, dead-letter queue, person alerts
           │
           ▼
[ Metrics & Release Gate ] ──► Quality, reviewer, financial ledger, simulated rejection rates, CI release gate
```

### The 7 Never-Break Invariants

1. **Human Approval Gate (Rule 1)**: No ad is sent to any platform without a human reviewer's approval recorded in the audit trail.
2. **Approved Version Idempotency (Rule 2 / PRD D13)**: Only the exact version approved by the reviewer (verified via cryptographic content hash) can be sent. Each send is keyed by a deterministic idempotency key.
3. **False Alarm Ceiling (Rule 3 / PRD §3)**: Clean rows false alarms must never exceed 5.0% (target $\le 3.0\%$).
4. **Tenant Isolation & Least Privilege (Rule 4 / Needs: 28)**: Strict separation of client rules and access per row (`client_id` scoping). Zero cross-client rule citations, data leaks, or unauthorized access.
5. **Step & Spend Caps (Rule 5 / PRD §4.5)**: Hard cap of 5 steps per row and spend cap of $\$0.01$ per row ($\$0.25$ per 100 ads average target, $\$15.00$ assessment budget cap).
6. **No Automatic Relaunch (Rule 6 / PRD §4.6)**: Any platform rejection or uncertain send state alerts a person and returns the ad to review. Never automatically relaunched.
7. **Real Platform APIs Switched Off (Rule 7 / PRD §4.6)**: Real external ad platform calls (Meta, Google, TikTok) are designed and documented, but strictly switched OFF in v1. Sends dispatch only to the stand-in dry-run platform.

---

## Local Services & Port Map

| Service | Port | Description |
| :--- | :---: | :--- |
| **FastAPI Backend** | `8000` | REST API, SSE live event stream, authentication, upload ingestion, review actions, and metrics endpoints |
| **Next.js Frontend** | `3000` | Interactive web dashboard: Home, Upload, Live Status, Reviewer Cockpit, and Quality Metrics screen |
| **Stand-In Ad Platform** | `8100` | Synthetic dry-run ad platform simulating Meta/Google/TikTok APIs with idempotency and chaos testing |
| **Mock Storefront Pages** | `8099` | Local mock landing pages server for headless browser page checks and discount verification |
| **PostgreSQL + pgvector**| `5432` | Relational storage for batches, ads, approvals, findings, edits, audit logs, and vector rule embeddings |
| **Redis** | `6379` | Message broker and job queue for asynchronous ad ingestion and outbox processing |

---

## Prerequisites & Installation

### 1. System Requirements

- **Python**: 3.12+ (tested with 3.12.10)
- **Node.js**: 18+ and `npm`
- **PostgreSQL**: 15+ (with `vector` / pgvector extension)
- **Redis**: 7+
- **Playwright Chromium**: for headless landing page checks

### 2. Clone and Setup Python Environment

From the repository root (e.g. in PowerShell on Windows or bash on macOS/Linux):

```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1    # On Linux/macOS: source .venv/bin/activate

# 2. Upgrade pip and install all Python dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 3. Install Playwright browser binary
playwright install chromium
```

---

## Environment Configuration (`.env`)

Copy `.env.example` to `.env` in the repository root:

```powershell
Copy-Item .env.example .env     # On Linux/macOS: cp .env.example .env
```

Edit `.env` with your local settings:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `DATABASE_URL` | Local PostgreSQL connection string | `postgresql://postgres:postgres@localhost:5432/launch_gate` |
| `TEST_DATABASE_URL` | Isolated test PostgreSQL database (for integration tests) | `postgresql://postgres:postgres@localhost:5432/launch_gate_test` |
| `REDIS_URL` | Local Redis connection string | `redis://localhost:6379/0` |
| `TEST_REDIS_URL` | Isolated test Redis database | `redis://localhost:6379/1` |
| `JWT_SECRET` | Private secret signing key (min 32 chars; must replace placeholder) | `your-secure-random-jwt-secret-key-32chars` |
| `JWT_EXPIRE_MINUTES` | Access token lifespan in minutes | `60` |
| `MODEL_API_KEY` | Optional: Google Gemini API key from Google AI Studio | `AIzaSy...` (falls back to mock/heuristic rules if omitted) |
| `SPEND_CAP_PER_ROW` | Hard spend cap per ad row in USD | `0.01` |
| `SPEND_CAP_TOTAL` | Total assessment budget spend cap in USD | `15.00` |
| `STORAGE_DIR` | Local directory for uploaded sheets, screenshots, evidence | `storage` |
| `MAX_UPLOAD_BYTES` | Maximum CSV upload file size in bytes (2 MB) | `2097152` |
| `MAX_XLSX_UNCOMPRESSED_BYTES` | Maximum XLSX uncompressed file size (20 MB) | `20971520` |
| `MAX_EDIT_LOOPS` | Maximum allowed bounded reviewer re-check iterations | `5` |
| `STAND_IN_PLATFORM_URL` | URL of the stand-in dry-run ad platform | `http://127.0.0.1:8100` |
| `LOCAL_STOREFRONT_BASE` | URL for local storefront page checks | `http://127.0.0.1:8099` |

> [!IMPORTANT]
> The API will refuse to start if `JWT_SECRET` is unset or contains the default placeholder.

---

## Database Initialization & Demo Seeding

### Initialize Database Schema

Ensure your local PostgreSQL service is running, then initialize all database tables and constraints:

```powershell
python -c "from apps.api.schema import init_schema; init_schema(); print('Schema initialized successfully.')"
```

### Seed Synthetic Pilot Clients and Users

Run the interactive demo seed script to set up synthetic clients (`brightleaf`, `calmleaf`, `oakandhome`) and create test buyer and account manager credentials:

```powershell
python -m apps.api.seed_demo
```

The script will prompt you for emails and passwords for the synthetic buyer and account manager accounts. Passwords are saved hashed via scrypt.

---

## Step-by-Step Local Run Instructions

To run the complete Launch Gate system locally with live UI and dry-run execution, open five terminal windows from the repository root:

### Terminal 1: Stand-In Ad Platform (Port 8100)

Start the stand-in ad platform server which receives dry-run ad sends:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn data.stand_in_platform.app:app --port 8100
```
*Health check:* `http://127.0.0.1:8100/admin/stats`

### Terminal 2: Local Storefront Server (Port 8099)

Serve local mock landing pages used by headless page checks:

```powershell
.\.venv\Scripts\Activate.ps1
python -m http.server 8099 -d data/storefront_pages
```
*Health check:* `http://127.0.0.1:8099/`

### Terminal 3: FastAPI Backend API (Port 8000)

Start the core FastAPI backend with reload enabled:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn apps.api.main:app --port 8000 --reload
```
*Interactive Swagger docs:* `http://127.0.0.1:8000/docs`  
*API Health check:* `http://127.0.0.1:8000/health`

### Terminal 4: Asynchronous Worker Runner

Start the background worker runner that processes outbox jobs and executes checks:

```powershell
.\.venv\Scripts\Activate.ps1
python -m apps.worker.main
```

### Terminal 5: Next.js Frontend Dashboard (Port 3000)

Install Node dependencies and start the Next.js development server:

```powershell
cd apps/web
npm install
npm run dev
```
*Frontend UI:* `http://localhost:3000`

---

## End-to-End Walkthrough

### 1. Log In to the Web UI

1. Open `http://localhost:3000` in your browser.
2. Enter the credentials seeded during `python -m apps.api.seed_demo` (e.g. `buyer_brightleaf@example.com` or your custom seeded email).
3. The JWT token is securely stored in browser session storage and attached to subsequent API requests.

### 2. Upload a Launch Sheet

1. On the home screen, select **Upload Launch Sheet**.
2. Drag and drop or browse for any sample sheet from `data/labeled_sheets/` (e.g., `sheet_01.csv` or `sheet_02.xlsx`).
3. Click **Upload and Verify**.
4. The file is uploaded, hashed, deduplicated against prior sheets, and partitioned into ad jobs.

### 3. Live Progress Stream (SSE)

1. The app automatically navigates to the batch review page with an active SSE live status pill (`connected`).
2. As worker jobs process ads through mechanical, page, and wording layers, real-time status updates stream in via Server-Sent Events (`GET /batches/{batch_id}/events`).

### 4. Reviewer Cockpit (`/review` or `/batches/{batchId}/review`)

1. **Batch Summary Banner**: Displays batch status, total ads, and verdict counts:
   - `Ready` (green): Passed all checks, 0 findings, ready for approval.
   - `Fix Needed` (amber): Fixable mechanical, page, or wording findings.
   - `Blocked` (red): Fatal violations (expired offer, 404 dead page, forbidden claim).
   - `Needs a Person` (purple): Novel unregistered claims, unknown client rules, or guard triggers.
2. **Filter & Search Tabs**: Filter ads by verdict status (`All`, `Ready`, `Fix Needed`, `Blocked`, `Needs Person`).
3. **Ad Card & Finding Inspector**:
   - View ad copy, platform badge, headline, and creative asset preview.
   - Inspect findings side-by-side with rule citations, error explanations, and proposed fixes.
   - **Reviewer Actions**:
     - **Approve**: Opens modal to attach client approval evidence (mandatory approval note or screenshot upload) linked cryptographically to the content hash.
     - **Edit Copy**: Edit headline, body text, or destination URL. Requires a mandatory reason; automatically re-evaluates the edited ad through a bounded re-check loop (up to 5 iterations).
     - **Dismiss Finding**: Dismiss false-alarm findings with an explanation note; automatically recalculates the ad's verdict.
     - **Reject Ad**: Mark an unviable ad as rejected with a permanent rejection reason.

### 5. Idempotent Stand-In Send (`POST /batches/{batch_id}/send-approved`)

1. Click **Send Approved Ads** in the batch header.
2. All approved ads belonging to the user's assigned clients are dispatched to the stand-in ad platform.
3. Each ad uses a deterministic idempotency key. Stand-in platform returns `201 Created` or `200 OK` (`Idempotent-Replay: true`).
4. Replaying send on already-sent ads is guaranteed to create **0 duplicate platform records**.

### 6. Dead-Letter Queue & Person Alerts (`/alerts`)

1. If an ad encounters persistent network timeouts or repeated platform 500s across retries, it moves to the `dead_letter` state and creates a person alert.
2. Reviewers can visit the alerts page to inspect the failure, resolve the alert with a note, and trigger a manual retry via `POST /ads/{ad_id}/retry`.

### 7. Quality & Operational Metrics Screen (`/metrics`)

1. Navigate to **Metrics** in the navigation bar.
2. View real-time operational and quality KPIs:
   - **CI Release Gate Banner**: Visual pass/fail status checking compliance against the 5% false alarm ceiling and 5% dead-letter ceiling.
   - **Simulated Rejection Rates**: Without-gate predicted failure rate vs. with-gate platform rejection rate, showing total rejections avoided and protection effectiveness.
   - **Problem Breakdown**: Finding counts grouped by check type (`mechanical`, `page`, `wording`), field breakdown, and top violated rules table.
   - **Reviewer Performance**: Approvals, rejections, manual edits, dismissed false alarms, and cleaned ads count.
   - **Financial Ledger**: Model spend per ad, total spend vs. $15 cap, total steps, and cost per 100 ads.
   - **Export Metrics**: One-click download of the complete metrics JSON report.

---

## Evaluation Suites & Verification Scripts

Launch Gate includes reproducible evaluation scripts that score the complete verification pipeline against ground truth datasets:

### 1. 30-Sheet Replay & Scoring Gate (PRD §4.7 / §11 / D9)

Evaluates all 30 labeled sheets (3,260 valid ad rows, 41 malformed rows isolated) across synthetic clients:

```powershell
# Run dev split (20 sheets)
python evals/replay_30_sheets.py --split dev

# Run held-out split (10 sheets, protected with --final)
python evals/replay_30_sheets.py --split held_out --final

# Run complete 30-sheet benchmark and export JSON report
python evals/replay_30_sheets.py --split all --final --output data/replay_30_sheets_report.json
```

**Results:**
- **Planted Recall**: **100.00%** across all 19 planted error types (1,972 / 1,972 errors detected).
- **Verdict Accuracy**: **100.00%** (3,260 / 3,260 valid rows correctly classified).
- **False Alarm Rate**: **0.00%** (0 false alarms on 1,667 clean rows, target $\le 5.0\%$).
- **False-Ready Count**: **0** (0 bad ads misclassified as `ready`).
- **CI Release Gate**: **PASSED** in **2.62s**.

### 2. Cross-Cutting Safety Suite (PRD §3 / Needs: 28, 39, 47)

Evaluates 500 repeat clicks, worker crashes mid-send, and cross-client penetration probes:

```powershell
python evals/cross_cutting_safety_eval.py --output data/cross_cutting_safety_report.json
```

**Results:**
- **500 Repeat Clicks**: 1 initial send + 499 idempotent replays $\rightarrow$ exactly **0 duplicate records** on platform; 500 unapproved clicks 100% blocked; 500 tampered clicks 100% blocked.
- **Worker Crash Mid-Send**: Pre-network crash and post-commit crash simulations recover cleanly with 0 platform duplicates; orphaned `sending` ads reconciled to `dead_letter` with person alert and no auto-relaunch.
- **Cross-Client Isolation**: 0 rule overlap in rule index; 10 / 10 multi-tenant API penetration probes blocked with `403 Forbidden` / `404 Not Found`; 12 / 12 adversarial prompt injection rows contained.

### 3. Adversarial Guard Replay

Replays the 12 owner-provided adversarial rows against input guards and tool policies:

```powershell
python evals/task20_adversarial_guard_eval.py
```

### 4. Pure Mechanical & Page Offer Replays

```powershell
# Mechanical limits and UTM naming replay
python evals/mechanical_replay.py

# Headless page check offer/discount replay
python evals/task25_page_offer_eval.py
```

---

## Running the Automated Test Suite

Launch Gate features a comprehensive automated test battery with over 680 tests spanning all 54 development milestones:

```powershell
# Run the complete test suite
python -m pytest

# Run with concise summary and disable cache writes
python -m pytest -q -p no:cacheprovider

# Run a specific task test file (e.g. Task 54 Safety Suite)
python -m pytest tests/test_task54_safety_suite.py -v
```

### Optional: Real-Service Integration Tests

To run real database and Redis integration tests against local test instances (opt-in):

```powershell
# Configure test database and redis in .env, then run:
$env:RUN_INTEGRATION="1"
python -m pytest tests/test_integration.py
```

---

## What is Real, Faked, Missing (PRD §11 Alignment)

| Subsystem | Status | Implementation Details |
| :--- | :---: | :--- |
| **Ingest Engine** | **REAL** | CSV and XLSX parsing, platform and column normalization, blank handling, SHA-256 row & sheet hashing, duplicate sheet detection, malformed row isolation. |
| **Mechanical Rules** | **REAL** | UTM presence, platform-specific UTM naming conventions, headline/body character limits, creative size and aspect ratio matching. |
| **Headless Page Checks** | **REAL** | Playwright Chromium browser navigation, UTM redirect preservation verification, offer text/price/date extraction, discount and threshold mismatch checks, expired offer detection. Read-only: never submits forms or purchases. |
| **Wording & Citations** | **REAL** | Tenant-scoped retrieval over client brand books and claims registers, citation verification against real rule IDs, unknown claim human routing, prompt injection containment. |
| **Composite Verdict Engine** | **REAL** | Deterministic LangGraph state machine aggregating ad findings into `ready`, `fix_needed`, `blocked`, and `needs_person`. Hard enforcement of 5 steps and $0.01 spend caps per row. |
| **Reviewer Cockpit UI** | **REAL** | Next.js interactive reviewer dashboard, findings inspector, client approval evidence attachment, reviewer edits with bounded re-check loops, finding dismissal with reason audit trail. |
| **Idempotent Sender** | **REAL** | Idempotency key per content hash, exponential backoff with jitter, request timeouts, dead-letter queue, person alerts, atomic state transitions (`approved` $\rightarrow$ `sending` $\rightarrow$ `sent` / `rejected` / `dead_letter`). |
| **SSE Live Updates** | **REAL** | Server-Sent Events endpoint (`/batches/{batch_id}/events`) broadcasting real-time progress pills to frontend. |
| **Metrics & Release Gate** | **REAL** | Batch and cross-batch quality metrics computation, reviewer performance tracking, financial cost ledger, simulated rejection rates with/without gate, CI production release gate enforcement. |
| **Ad Data & Clients** | **SYNTHETIC** | 30 labeled sheets, 12 adversarial rows, synthetic clients (`brightleaf`, `calmleaf`, `oakandhome`), and mock storefront HTML pages. All evaluation reports labeled SYNTHETIC per PRD. |
| **Stand-In Platform** | **SYNTHETIC** | FastAPI dry-run platform server (`data/stand_in_platform/app.py`) simulating platform policy rejections and network chaos (commit fails, timeouts). |
| **Live External Ad APIs** | **MISSING BY DESIGN** | Direct calls to live Meta Marketing API, Google Ads API, and TikTok Marketing API are designed and documented in adapter contracts, but **strictly switched OFF** per Never-break Rule 7. |

---

## License & Operational Notice

Built for **Fernlight Media, LLC** (Denver, CO) under the Launch Gate Mini PRD specification.  
All sample brands, ad copies, and evaluation sheets are **SYNTHETIC** datasets for performance verification and release gating.
