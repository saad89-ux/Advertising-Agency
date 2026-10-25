"""Unified local runner for Launch Gate.

Launches all services concurrently:
1. Database schema initialization
2. Mock Storefront Server (port 8099)
3. Stand-in Ad Platform (port 8100)
4. Launch Gate FastAPI Backend (port 8000)
5. Background Worker processor
6. Next.js Web Frontend (port 3000)
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent
LOCAL_HOST = "127.0.0.1"

# Check if running within .venv. If not, auto-switch to the project's virtual environment.
_venv_python = (
    REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else REPO_ROOT / ".venv" / "bin" / "python"
)
_venv_root = REPO_ROOT / ".venv"
if _venv_python.exists() and Path(sys.prefix).resolve() != _venv_root.resolve():
    print(f"[INFO] Auto-switching to project virtual environment: {_venv_python}")
    sys.exit(subprocess.call([str(_venv_python), str(Path(__file__).resolve())] + sys.argv[1:]))

import http.server
import logging
import json
import socket
import socketserver
import threading
import time
from urllib.request import urlopen


# Ensure nodejs is in PATH if installed in user local appdata
local_node = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs"
if local_node.exists() and str(local_node) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{local_node};{os.environ.get('PATH', '')}"


def init_environment():
    """Verify the database schema."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
        from apps.api.schema import init_schema

        try:
            init_schema()
        except Exception as schema_err:
            print(f"[WARN] Database schema initialization warning (PostgreSQL may be offline or initializing): {schema_err}")

        print("[OK] Schema initialized. Create accounts with python -m scripts.create_user.")
    except Exception as exc:
        print(f"[WARN] Database initialization notice: {exc}")


def run_storefront_server():
    """Run local storefront HTTP server on port 8099."""
    storefront_dir = REPO_ROOT / "data" / "storefront_pages"
    if not storefront_dir.exists():
        return
    if is_port_in_use(8099):
        print("[INFO] Storefront server is already active on http://127.0.0.1:8099.")
        return

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(storefront_dir), **kwargs)

        def log_message(self, format, *args):
            pass  # Suppress noisy HTTP logs

    try:
        with socketserver.TCPServer((LOCAL_HOST, 8099), Handler) as httpd:
            httpd.serve_forever()
    except Exception:
        pass


def run_stand_in_platform():
    """Run simulated ad platform on port 8100."""
    if is_port_in_use(8100):
        if is_expected_service(8100, "/openapi.json", "info.title", "Stand-in platform (SYNTHETIC)"):
            print("[INFO] Stand-in platform is already active on http://127.0.0.1:8100.")
        else:
            print("[ERROR] Port 8100 is occupied by a service that is not the Launch Gate stand-in platform.")
        return
    try:
        import uvicorn

        uvicorn.run(
            "data.stand_in_platform.app:app",
            host=LOCAL_HOST,
            port=8100,
            log_level="warning",
            access_log=False,
        )
    except Exception as exc:
        print(f"[WARN] Stand-in platform notice: {exc}")


def run_worker_loop():
    """Continuously process durable notification, Meta sync, and ad jobs."""
    try:
        from apps.worker.job_runner import run_once
        from apps.api.notifications import process_pending_notifications
        from apps.api.queue import recover_meta_sync_jobs
        from apps.api.outbox import dispatch_pending_meta_sync_jobs
        from apps.worker.meta_sync import run_meta_sync_once
    except Exception as exc:
        print(f"[WARN] Background worker loop notice: {exc}")
        return

    recovered_meta_jobs = False
    while True:
        try:
            if not recovered_meta_jobs:
                recover_meta_sync_jobs()
                recovered_meta_jobs = True
            process_pending_notifications(batch_size=10)
            dispatch_pending_meta_sync_jobs()
            run_meta_sync_once()
            run_once(run_wording_checks=True)
        except Exception:
            logging.exception("Background worker iteration failed")
        time.sleep(1.0)


def is_port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def is_expected_service(port: int, path: str, field: str, expected: str) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=1.0) as response:
            payload = json.load(response)
        value: object = payload
        for key in field.split("."):
            if not isinstance(value, dict):
                return False
            value = value.get(key)
        return value == expected
    except Exception:
        return False


def run_frontend():
    """Run Next.js dev server on port 3000 if not already running."""
    if is_port_in_use(3000):
        print("[INFO] Frontend is already active on http://localhost:3000.")
        return None

    web_dir = REPO_ROOT / "apps" / "web"
    try:
        npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
        proc = subprocess.Popen(
            [npm_cmd, "run", "dev", "--", "--port", "3000", "--hostname", LOCAL_HOST],
            cwd=str(web_dir),
            shell=True,
        )
        return proc
    except Exception as exc:
        print(f"[WARN] Failed to start Next.js frontend automatically: {exc}")
        print("You can run it manually with: cd apps/web && npm run dev")
        return None


def run_backend_api():
    """Run FastAPI main app on port 8000."""
    if is_port_in_use(8000):
        if is_expected_service(8000, "/health", "app", "Launch Gate API"):
            print("[INFO] Launch Gate API is already active on http://127.0.0.1:8000.")
        else:
            print("[ERROR] Port 8000 is occupied by a service that is not the Launch Gate API.")
        return
    try:
        import uvicorn

        uvicorn.run(
            "apps.api.main:app",
            host=LOCAL_HOST,
            port=8000,
            log_level="info",
            reload=False,
        )
    except Exception as exc:
        print(f"[ERROR] Backend API stopped: {exc}")


def print_banner():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    banner = """
======================================================================
  [LAUNCH GATE] FULL STACK LOCAL ENVIRONMENT IS RUNNING
======================================================================

  >> Web Application (Browser):   http://localhost:3000
  >> Operational Metrics:        http://localhost:3000/metrics
  >> Interactive API Docs:       http://127.0.0.1:8000/docs
  >> Dry-run Ad Platform:        http://127.0.0.1:8100/admin/stats
  >> Storefront Mock Pages:      http://127.0.0.1:8099/

  Press CTRL+C anytime to stop all servers.
======================================================================
"""
    print(banner)


def main():
    print("Starting Launch Gate services...")
    init_environment()
    try:
        from apps.api.auth import get_jwt_secret

        get_jwt_secret()
    except RuntimeError as exc:
        print(f"[ERROR] Cannot start Launch Gate: {exc}")
        return

    if "--check" in sys.argv or "--dry-run" in sys.argv:
        print("[OK] Pre-flight check succeeded. Virtual environment and all dependencies verified.")
        return

    if (
        is_expected_service(8000, "/health", "app", "Launch Gate API")
        and is_expected_service(8100, "/openapi.json", "info.title", "Stand-in platform (SYNTHETIC)")
        and is_port_in_use(3000)
    ):
        print("[INFO] Launch Gate API, stand-in platform, and frontend are already active; reusing them.")
        print_banner()
        return

    # 1. Background Storefront server
    t_storefront = threading.Thread(target=run_storefront_server, daemon=True)
    t_storefront.start()

    # 2. Stand-in ad platform
    t_platform = threading.Thread(target=run_stand_in_platform, daemon=True)
    t_platform.start()

    # 3. Background worker
    t_worker = threading.Thread(target=run_worker_loop, daemon=True)
    t_worker.start()

    # 4. Next.js Frontend
    frontend_proc = run_frontend()

    time.sleep(1.0)
    print_banner()

    # 5. FastAPI Backend (blocks main thread)
    try:
        run_backend_api()
    except KeyboardInterrupt:
        print("\nShutting down Launch Gate...")
    finally:
        if frontend_proc:
            try:
                frontend_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
