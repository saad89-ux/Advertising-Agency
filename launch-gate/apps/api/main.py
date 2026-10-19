import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.alerts import router as alerts_router
from apps.api.approvals import router as approvals_router
from apps.api.auth import get_jwt_secret, router as auth_router
from apps.api.batches import router as batches_router
from apps.api.dead_letter import router as dead_letter_router
from apps.api.edits import router as edits_router
from apps.api.evidence import router as evidence_router
from apps.api.findings import router as findings_router
from apps.api.metrics import router as metrics_router
from apps.api.mock_data import router as mock_data_router, seed_demo_data
from apps.api.schema import init_schema
from apps.api.sends import router as sends_router
from apps.api.sse import router as sse_router
from apps.api.team import router as team_router
from apps.api.diagnostics import router as diagnostics_router
from apps.api.platforms import router as platforms_router
from apps.api.attribution import router as attribution_router
from apps.api.pipeline import router as pipeline_router
from apps.api.campaign_flow import router as campaign_flow_router
from apps.api.middleware_pii import PIISanitizationMiddleware
from apps.api.platforms_schema import seed_platform_demo_data
from apps.api.uploads import UploadBodyLimitMiddleware, router as upload_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_jwt_secret()
    init_schema()
    seed_opt_in = os.getenv("SEED_DEMO_DATA", "false").lower() in ("true", "1", "yes")
    if seed_opt_in:
        try:
            seed_demo_data(force=False)
            seed_platform_demo_data()
        except Exception as exc:
            print(f"[WARN] Failed to auto-seed demo data in lifespan: {exc}")
    yield


app = FastAPI(
    title="Launch Gate API",
    description="""
# 🚀 Launch Gate API — Pre-Launch Ad Quality & Compliance Gate

All operational endpoints (except `/health` and `/auth/login`) require a Bearer token.

### Quick Start Demo Credentials:
- **Media Buyer:** `buyer@launchgate.com` / `password123`
- **Account Manager:** `manager@launchgate.com` / `password123`

To test authenticated endpoints in this Swagger interface:
1. Call `POST /auth/login` with the credentials above.
2. Copy the returned `access_token`.
3. Click the green **Authorize 🔓** button at the top right of this page and enter `Bearer <your_token>`.
4. Test any endpoint!
    """,
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(UploadBodyLimitMiddleware)
app.add_middleware(PIISanitizationMiddleware)

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(batches_router)
app.include_router(approvals_router)
app.include_router(edits_router)
app.include_router(findings_router)
app.include_router(evidence_router)
app.include_router(sends_router)
app.include_router(metrics_router)
app.include_router(dead_letter_router)
app.include_router(alerts_router)
app.include_router(sse_router)
app.include_router(team_router)
app.include_router(diagnostics_router)
app.include_router(platforms_router)
app.include_router(attribution_router)
app.include_router(pipeline_router)
app.include_router(campaign_flow_router)
app.include_router(mock_data_router)


@app.get("/health")
def health():
    return {"status": "ok", "app": "Launch Gate API", "version": "2.0.0"}
