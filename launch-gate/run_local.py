"""Unified local runner for Launch Gate.

Launches all services concurrently:
1. Database schema initialization & demo user seeding
2. Mock Storefront Server (port 8099)
3. Stand-in Ad Platform (port 8100)
4. Launch Gate FastAPI Backend (port 8000)
5. Background Worker processor
6. Next.js Web Frontend (port 3000)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent

# Ensure nodejs is in PATH if installed in user local appdata
local_node = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs"
if local_node.exists() and str(local_node) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{local_node};{os.environ.get('PATH', '')}"


def init_environment():
    """Verify database and seed initial demo accounts."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
        if not os.environ.get("JWT_SECRET") or "placeholder" in os.environ.get("JWT_SECRET", "").lower():
            os.environ["JWT_SECRET"] = "launch_gate_secure_secret_key_development_2026_x87"
        from apps.api.auth import hash_password
        from apps.api.database import get_session
        from apps.api.schema import init_schema
        from apps.api.users import assign_user_to_client, create_user, create_users_table

        init_schema()
        create_users_table()

        pwd = hash_password("password123")
        create_user(
            user_id="demo_buyer",
            email="buyer@launchgate.com",
            display_name="Demo Buyer",
            password_hash=pwd,
            role="buyer",
        )
        for client_id in ["brightleaf", "calmleaf", "oakandhome"]:
            assign_user_to_client(user_id="demo_buyer", client_id=client_id)

        create_user(
            user_id="demo_manager",
            email="manager@launchgate.com",
            display_name="Demo Manager",
            password_hash=pwd,
            role="account_manager",
        )
        for client_id in ["brightleaf", "calmleaf", "oakandhome"]:
            assign_user_to_client(user_id="demo_manager", client_id=client_id)

        from apps.api.mock_data import seed_demo_data

        seed_demo_data(force=False)

        print("[OK] Database schema initialized and demo users/batches seeded.")
    except Exception as exc:
        print(f"[WARN] Database initialization warning (may already be initialized): {exc}")


def run_storefront_server():
    """Run local storefront HTTP server on port 8099."""
    import http.server
    import socketserver

    storefront_dir = REPO_ROOT / "data" / "storefront_pages"
    if not storefront_dir.exists():
        return

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(storefront_dir), **kwargs)

        def log_message(self, format, *args):
            pass  # Suppress noisy HTTP logs

    try:
        with socketserver.TCPServer(("0.0.0.0", 8099), Handler) as httpd:
            httpd.serve_forever()
    except Exception:
        pass


def run_stand_in_platform():
    """Run simulated ad platform on port 8100."""
    import uvicorn

    try:
        uvicorn.run(
            "data.stand_in_platform.app:app",
            host="0.0.0.0",
            port=8100,
            log_level="warning",
            access_log=False,
        )
    except Exception:
        pass


def run_worker_loop():
    """Continuously pop and process ad jobs from the Redis queue."""
    from apps.worker.job_runner import run_once

    while True:
        try:
            run_once(run_wording_checks=True)
        except Exception:
            pass
        time.sleep(1.0)


def is_port_in_use(port: int) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
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
            [npm_cmd, "run", "dev", "--", "--port", "3000"],
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
    import uvicorn

    uvicorn.run(
        "apps.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=False,
    )


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

  Demo Credentials:
    * Buyer:           buyer@launchgate.com   / password123
    * Account Manager: manager@launchgate.com / password123

  Press CTRL+C anytime to stop all servers.
======================================================================
"""
    print(banner)


def main():
    print("Starting Launch Gate services...")
    init_environment()

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
