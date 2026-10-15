from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.alerts import router as alerts_router
from apps.api.approvals import router as approvals_router
from apps.api.auth import get_jwt_secret, router as auth_router
from apps.api.batches import router as batches_router
from apps.api.dead_letter import router as dead_letter_router
from apps.api.edits import router as edits_router
from apps.api.evidence import router as evidence_router
from apps.api.findings import router as findings_router
from apps.api.metrics import router as metrics_router
from apps.api.schema import init_schema
from apps.api.sends import router as sends_router
from apps.api.sse import router as sse_router
from apps.api.uploads import UploadBodyLimitMiddleware, router as upload_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_jwt_secret()
    init_schema()
    yield


app = FastAPI(title="Launch Gate API", lifespan=lifespan)
app.add_middleware(UploadBodyLimitMiddleware)
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


@app.get("/health")
def health():
    return {"status": "ok"}
