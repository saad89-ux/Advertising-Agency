from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.auth import get_jwt_secret, router as auth_router
from apps.api.uploads import UploadBodyLimitMiddleware, router as upload_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_jwt_secret()
    yield


app = FastAPI(title="Launch Gate API", lifespan=lifespan)
app.add_middleware(UploadBodyLimitMiddleware)
app.include_router(auth_router)
app.include_router(upload_router)


@app.get("/health")
def health():
    return {"status": "ok"}
