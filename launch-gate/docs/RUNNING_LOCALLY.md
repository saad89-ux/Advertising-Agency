# Running Launch Gate locally

## Prerequisites

- Python dependencies installed in `.venv` (`.venv\\Scripts\\python.exe -m pip install -r requirements.txt` on Windows).
- Node dependencies installed (`cd apps/web; npm ci`).
- PostgreSQL and Redis running and reachable at the `DATABASE_URL` and `REDIS_URL` values in `.env`.

## Start services

1. Start PostgreSQL using your local installation (for example, `pg_ctl -D $env:PGDATA start`). Confirm the database named in `DATABASE_URL` exists.
2. Start Redis with `redis-server` (or start its installed Windows service). The default URL is `redis://localhost:6379/0`.
3. Start the API, continuous worker loop, local stand-in services, and web development server together from the repository root:

   ```powershell
   .\\.venv\\Scripts\\python.exe run_local.py
   ```

   `run_local.py` uses `.venv` automatically when it exists. It starts FastAPI on port 8000, the worker loop, Next.js on port 3000, and the local stand-in services. It attempts schema initialization first; keep PostgreSQL available before launching it. Leave this terminal open to see startup errors.

   The API and web app can also be started separately when needed:

   ```powershell
   # API, from the repository root
   .\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000

   # Web app, in a second terminal
   cd apps/web
   npm run dev -- --hostname 127.0.0.1 --port 3000
   ```

   The worker loop is started by `run_local.py`. `python -m apps.worker.main` runs a single worker pass and is not a persistent worker service.

4. Open `http://localhost:3000`. The browser API origin is read from `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`); set it in the repository-root `.env` before starting Next.js if using a different API origin, then restart Next.js.

## Check the API

Run `Invoke-RestMethod http://127.0.0.1:8000/health` in PowerShell. A healthy Launch Gate API returns JSON with `status: "ok"` and app name `Launch Gate API`. The navbar checks this response and polls every 10 seconds; a different API, non-200 response, or network failure is shown as unavailable.

The API and web ports must be free before startup. If another service owns port 8000 or 3000, stop that service or configure/launch Launch Gate on an available port; `/health` must identify Launch Gate, not merely return HTTP 200.

## Verify a Meta Workspace (Read Only)

Configure `META_SYSTEM_USER_TOKEN` and `MCP_META_SERVER_URL` in the repository-root `.env`. Map each client workspace to its numeric Meta account through the account-manager endpoint `POST /team/clients/meta-account`. The API stores the account mapping and verification health, but never stores the System User token or returns secret-manager references.

With an authenticated user assigned to the client, call `GET /diagnostics/meta/client/{client_id}`. This checks account visibility and performs a limited campaign read. Set `META_MCP_ENABLE_LIVE=true` to permit that read; leave `META_MCP_ALLOW_WRITES=false`. The diagnostic does not create or activate Meta objects. Its response reports `HEALTHY`, `UNAUTHORIZED`, `DEGRADED`, `CRITICAL`, or `READS_DISABLED` and includes only safe account information.

## Synchronize Meta Reporting Data (Read Only)

With the same read configuration and client access, call `POST /diagnostics/meta/client/{client_id}/sync` to queue an `INITIAL_IMPORT` (the default) or pass `sync_type=METRIC_REFRESH` to refresh metrics for previously imported objects. The optional `lookback_days` parameter accepts values from 1 to 90. Sync requests are saved to a PostgreSQL transactional outbox before dispatch to Redis, so Redis outages leave requests pending for the local worker to retry. Claimed Redis jobs remain recoverable if the worker stops before acknowledging them. Structural data and daily-window metrics are read through Meta Ads MCP and saved with a per-workspace sync log; pagination and bounded rate-limit retries are handled by the worker. Poll `GET /diagnostics/meta/client/{client_id}/sync-state` for `NO_DATA`, `LOADING`, `META_REQUEST_FAILED`, `STALE`, or `SYNCED` status. These operations do not create or modify Meta campaigns and do not require enabling writes.

## Create and Activate Meta Campaigns

Ordinary launches use the stand-in sender in dry-run mode. Reaching Meta creation requires an explicit `meta_launch` authorization, a matching hierarchy preview hash, and both `META_MCP_ENABLE_LIVE=true` and `META_MCP_ALLOW_WRITES=true`; the flags alone never route a default launch to Meta. Campaign creation uses Meta's paused-only create tools and rejects status or activation overrides. The authenticated launch review shows the resolved ad account, planned budget, and campaign/ad-set targeting before the user authorizes creation.

Creation does not publish or activate the hierarchy. Activation is a separate, authenticated `POST /campaign-flow/activate-hierarchy` request requiring the reviewed hierarchy hash and exact confirmed budget. The API records the approver and approval snapshot before dispatch, then activates each campaign, ad set, and ad separately. A Meta response that does not confirm `ACTIVE` is recorded as needing reconciliation rather than reported as live. Keep both Meta flags false for local dry-run use; never enable them as a substitute for human review.

## Verify Meta Changes Safely

Run local grouping, create-safety, and mocked activation checks without Meta traffic:

```powershell
pytest -q tests/unit/ tests/test_hierarchy_activation.py
```

The real-account sync check is opt-in and read-only. It requires a dedicated sandbox client already mapped to its sandbox ad account, database access, `META_SYSTEM_USER_TOKEN`, and live reads enabled. Keep live writes disabled:

```powershell
$env:RUN_LIVE_META_TESTS = "true"
$env:META_SANDBOX_CLIENT_ID = "your-sandbox-client-id"
$env:META_SANDBOX_AD_ACCOUNT_ID = "your-sandbox-account-id"
$env:META_MCP_ENABLE_LIVE = "true"
$env:META_MCP_ALLOW_WRITES = "false"
pytest -q -m live_meta tests/integration/test_stage_read_only.py
```

The live stage verifies the configured workspace/account mapping, initial structural import, and a metrics refresh. It writes sync results only to the local Launch Gate database; it never creates or activates Meta objects. Stages covering paused creation and activation use mocked connector responses, so they are safe to run in local verification.

After changing `.env`, stop the local `run_local.py` process with `Ctrl+C` and start it again using `python run_local.py` (or `.\.venv\Scripts\python.exe run_local.py`). This reloads the API and worker process configuration. Docker restart commands are not part of the local setup.
