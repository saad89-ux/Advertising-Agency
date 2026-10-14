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
