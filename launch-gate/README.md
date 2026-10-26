# Launch Gate

Launch Gate is a pre-launch quality-control application for advertising campaigns. It helps an agency review a batch of ads before they go live, so problems can be found while the campaign is still being prepared rather than after launch.

## The Problem It Solves

Launch details often arrive in spreadsheets, while tracking conventions, platform limits, landing-page offers, and client-specific wording rules have to be checked separately. When those checks rely on a long manual checklist, some can be missed or applied inconsistently. Errors such as broken tracking parameters, incorrect offer details, platform rejections, or wording that does not follow a client's rules can lead to wasted ad spend, rework, and client dissatisfaction.

Launch Gate brings those checks into one review workflow. It is designed for advertising buyers and account managers who need to check many ads consistently without handing launch decisions over to an automated system.

## How It Works

1. A buyer uploads a campaign launch sheet in CSV or XLSX format.
2. Launch Gate normalizes the rows, avoids adding a duplicate sheet again, and isolates malformed rows so they do not prevent other ads from being checked.
3. It checks each ad for tracking and platform requirements, compares the ad with its landing page using read-only page checks, and checks wording against the relevant client's rules.
4. Each ad receives findings and a verdict, such as `ready`, `fix needed`, `blocked`, or `needs a person`. The reviewer sees the ad alongside the findings and can approve, edit, or reject it.
5. Only the approved version is eligible to be sent. By default, local development sends to a stand-in platform in dry-run mode; it does not publish real ads.

The goal is to make pre-launch checks more consistent and their results easier to review. Launch Gate provides decision support: a person remains responsible for approval, and uncertain cases are surfaced for human judgment.

## What It Does

- Accepts CSV and XLSX launch sheets and normalizes their data for review.
- Runs mechanical checks for tracking, platform text limits, and creative specifications.
- Checks landing pages in read-only mode for page availability, tracking redirects, and offer details.
- Applies the relevant client's brand and claims rules to wording checks, and presents citations with findings.
- Gives reviewers a per-ad verdict and findings screen where they can approve, edit, or reject ads.
- Records approvals and workflow data, and processes checks in the background.

The project is intended for local development and testing. Deployment and CI/CD are out of scope. See [CLAUDE.md](CLAUDE.md) for project constraints and [docs/Launch_Gate_Mini_PRD.md](docs/Launch_Gate_Mini_PRD.md) for product requirements.

## Requirements

- Python 3.12 or newer
- Node.js and npm
- PostgreSQL with the `vector` extension available
- Redis
- Playwright Chromium for landing-page checks

PostgreSQL and Redis should be running locally before starting the application.

## Setup

Run these commands from the repository root. PowerShell examples are shown first; POSIX alternatives are noted where they differ.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
playwright install chromium
npm ci --prefix apps/web
Copy-Item .env.example .env
```

On macOS or Linux, activate the environment with `source .venv/bin/activate` and copy the configuration with `cp .env.example .env`.

Edit `.env` and set `DATABASE_URL`, `REDIS_URL`, and a unique `JWT_SECRET` (replace the example placeholder). Configure `MODEL_API_KEY` to enable provider-backed wording checks. Keep `.env` private; do not commit it.

Create the database named in `DATABASE_URL` and ensure PostgreSQL can load the `vector` extension. The local runner attempts schema initialization at startup.

## Start Locally

With PostgreSQL and Redis running and `.env` configured, start the application from the repository root:

```powershell
python run_local.py
```

The runner starts the FastAPI API, background worker, Next.js development server, local stand-in platform, and mock storefront. Leave the terminal open while using the app. Press `Ctrl+C` to stop it.

- Web app: <http://localhost:3000>
- API health: <http://127.0.0.1:8000/health>
- API documentation: <http://127.0.0.1:8000/docs>
- Stand-in platform: <http://127.0.0.1:8100/admin/stats>
- Mock storefront: <http://127.0.0.1:8099/>

If a required port is already in use, stop the other process before starting Launch Gate. For separate API or frontend startup, configuration details, or troubleshooting, see [docs/RUNNING_LOCALLY.md](docs/RUNNING_LOCALLY.md).

## Accounts and Data

No demo users or batches are created automatically. User provisioning requires an existing client and prompts for a password:

```powershell
python -m scripts.create_user --email buyer@example.com --name "Campaign Buyer" --role buyer --client-id existing-client
```

The allowed roles are `buyer` and `account_manager`. Repeat `--client-id` to assign multiple clients. Client rules, sample sheets, and synthetic storefront pages are under `data/`; use synthetic data for local testing.

## Safety Defaults

- The stand-in sender is the default; local sends do not publish real ads.
- Meta live reads and writes are disabled by default. Real Meta writes require both `META_MCP_ENABLE_LIVE=true` and `META_MCP_ALLOW_WRITES=true`, as well as explicit human authorization in the application.
- Landing-page checks are read-only. They do not submit forms or make purchases.
- Reviewer approval is required before content can be sent.

Keep both Meta flags false for ordinary local development. See [docs/RUNNING_LOCALLY.md](docs/RUNNING_LOCALLY.md) for the read-only Meta diagnostics and integration-test procedure.

## Tests

Run the local test suite from the repository root:

```powershell
python -m pytest -q
```

Tests marked `integration` require local PostgreSQL and Redis configuration. Tests marked `live_meta` are opt-in and perform read-only checks against a configured sandbox account. Do not enable live Meta writes to run tests.

## Project Layout

- `apps/api/`: FastAPI endpoints, authentication, access control, and approval APIs.
- `apps/web/`: Next.js user interface.
- `apps/worker/`: background checks and sender workflows.
- `packages/`: shared contracts, guards, and mechanical rules.
- `data/`: client rules, synthetic fixtures, and evaluation inputs.
- `evals/`: local replay and evaluation scripts.
- `tests/`: automated tests.
- `storage/`: generated local artifacts; not source data.
- `docs/`: product, architecture, and local-running documentation.
