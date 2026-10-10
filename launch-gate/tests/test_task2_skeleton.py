import json
from pathlib import Path

from apps.api.main import app
from apps.worker.main import main as worker_main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_api_exposes_health_route():
    route = next(
        route for route in app.routes if getattr(route, "path", None) == "/health"
    )

    assert route.methods == {"GET"}
    assert route.endpoint() == {"status": "ok"}


def test_worker_stub_does_not_process_jobs(capsys):
    worker_main()

    assert capsys.readouterr().out == "Launch Gate worker stub; no jobs processed.\n"


def test_web_stub_declares_next_dev_and_home_page():
    web_dir = PROJECT_ROOT / "apps" / "web"
    package = json.loads((web_dir / "package.json").read_text(encoding="utf-8"))
    page = (web_dir / "app" / "page.jsx").read_text(encoding="utf-8")

    assert package["scripts"]["dev"] == "next dev"
    assert "next" in package["dependencies"]
    assert "Launch Gate" in page
