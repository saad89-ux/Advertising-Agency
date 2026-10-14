# Launch Gate

## Local setup

The local services are PostgreSQL and Redis. They are installed on the owner's machine; start them using the service controls for that installation. Service names and start commands depend on the local installation.

Copy `.env.example` to `.env`, then set `DATABASE_URL` and `REDIS_URL` to the local connection strings. Keep `.env` out of version control.

Python 3.12+ and the packages in `requirements.txt` are needed for the API and pytest checks. Node.js and npm are needed for the Next.js web stub. No Docker or cloud deployment setup is included.

Run the Python checks from the repository root with:

```text
python -m pytest
```

The API health app is in `apps/api/main.py`; the worker stub can be invoked with `python -m apps.worker.main`. The Next.js app is in `apps/web/`.

## Local API run

Use the project's Python 3.12 virtual environment, set `DATABASE_URL` in `.env` for the local PostgreSQL database, and replace the `JWT_SECRET` placeholder with a private signing secret. The API refuses to start with a missing or placeholder JWT secret. The API upload flow uses PostgreSQL; Redis is not used by the upload endpoint yet.

From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m apps.api.seed_demo
python -m uvicorn apps.api.main:app --reload
```

The seed command prints a `SYNTHETIC` notice, creates the three pilot client IDs from the read-only domain map, and prompts for synthetic buyer and account-manager demo credentials. Password input is hidden. The API is available at `http://127.0.0.1:8000`.

In another PowerShell window, enter credentials created by the seed command to log in and upload a CSV or XLSX. Replace the placeholders with your local values; do not place passwords in repository files:

```powershell
$login = curl.exe -s -X POST http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" --data-binary '{"email":"<seeded-email>","password":"<entered-password>"}' | ConvertFrom-Json
$token = $login.access_token
curl.exe -s -X POST http://127.0.0.1:8000/uploads -H "Authorization: Bearer $token" -F "file=@<path-to-sheet.csv>;type=text/csv"
```

For XLSX, use `-F "file=@<path-to-sheet.xlsx>;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"`.

Run tests locally with `python -m pytest` from the activated project virtual environment. API tests use fake database sessions; no PostgreSQL or Redis connection is made by the test suite.

## Data notice

The available project data is SYNTHETIC. Any report, metric, or evaluation based on it must be labelled SYNTHETIC. Develop and tune only on sheets labeled `dev`; reserve `held_out` sheets for final evaluation.
