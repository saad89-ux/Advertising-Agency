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

## Data notice

The available project data is SYNTHETIC. Any report, metric, or evaluation based on it must be labelled SYNTHETIC. Develop and tune only on sheets labeled `dev`; reserve `held_out` sheets for final evaluation.
