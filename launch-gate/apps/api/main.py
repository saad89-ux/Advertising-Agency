import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from apps.api.alerts import router as alerts_router
from apps.api.approvals import router as approvals_router
from apps.api.auth import get_jwt_secret, router as auth_router
from apps.api.guest_sessions import router as guest_router
from apps.api.batches import router as batches_router
from apps.api.dead_letter import router as dead_letter_router
from apps.api.edits import router as edits_router
from apps.api.evidence import router as evidence_router
from apps.api.findings import router as findings_router
from apps.api.metrics import router as metrics_router
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
from apps.api.uploads import UploadBodyLimitMiddleware, router as upload_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_jwt_secret()
    try:
        init_schema()
    except Exception as exc:
        print(f"[WARN] Database schema initialization warning: {exc}")
    yield


app = FastAPI(
    title="Launch Gate API",
    description="""
# 🚀 Launch Gate API — Pre-Launch Ad Quality & Compliance Gate

Employee operational endpoints require a Bearer token. Anonymous campaign review uses a
server-issued HttpOnly guest cookie through `/auth/guest-session`; dispatch and employee
controls continue to require employee authentication.

Create a real user with `python -m scripts.create_user` before signing in.

To test authenticated endpoints in this Swagger interface:
1. Call `POST /auth/login` with your account credentials.
2. Copy the returned `access_token`.
3. Click the green **Authorize 🔓** button at the top right of this page and enter `Bearer <your_token>`.
4. Test any endpoint!
    """,
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(UploadBodyLimitMiddleware)
app.add_middleware(PIISanitizationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "FRONTEND_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(guest_router)
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


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path, exc_info=exc)
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. See server logs."},
    )
    origin = request.headers.get("origin")
    if origin in {value.strip() for value in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")}:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
        response.headers["vary"] = "Origin"
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )})


@app.get("/health")
def health():
    return {"status": "ok", "app": "Launch Gate API", "version": "2.0.0"}
